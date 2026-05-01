"""
Shared OHLCV cache daemon (Phase 1).

Adds over Phase 0:
  - Partial-range merge: requests that partially overlap with cache only
    fetch the missing slices
  - Historic fetch routing: since_ms != None now goes through the cache,
    dramatically speeding up warmup across restarts / across bots
  - Range-aligned in-flight coalescing: concurrent bots asking for
    overlapping ranges dedup to one exchange call
  - Refresh overlap: live fetches re-request the last N candles to catch
    retroactive corrections
  - Feather persistence: series are flushed to disk periodically and
    restored at daemon startup
  - Per-exchange knobs (rate budget, max_candles_per_call, refresh
    overlap, history depth clamp) pulled from defaults + user overrides

Listens on a Unix socket and speaks newline-delimited JSON (see
protocol.py). Spawned on-demand by OhlcvCacheClient; shuts itself down
after idle_daemon_shutdown_s with no connections.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import heapq
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from freqtrade.ohlcv_cache.coordinator import RequestCoordinator
from freqtrade.ohlcv_cache.defaults import (
    resolve_exchange_config,
    resolve_global_config,
)
from freqtrade.ohlcv_cache.gaps import Gap, chunk_gap, compute_gaps
from freqtrade.ohlcv_cache.logger_setup import setup_daemon_logger
from freqtrade.ohlcv_cache.persistence import FeatherPersistence
from freqtrade.ohlcv_cache.protocol import PROTOCOL_VERSION, dumps, loads_request
from freqtrade.ohlcv_cache.store import CandleSeries, CandleStore


logger = logging.getLogger("ftcache.daemon")


def tf_to_ms(tf: str) -> int:
    unit = tf[-1]
    n = int(tf[:-1])
    return n * {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000,
                "w": 604_800_000}.get(unit, 60_000)


# -------------------------------------------------------------------- token bucket


class RateLimitShed(Exception):
    """Raised when a non-critical acquire is refused during backoff."""


class TokenBucket:
    """Async token-bucket rate limiter with priority queue and adaptive back-off.

    Priority levels (lower = higher priority):
        0 = CRITICAL  (order placement, exits — NEVER shed)
        1 = HIGH      (fetch_order, fetch_positions for open trades)
        2 = NORMAL    (tickers, balances, markets)
        3 = LOW       (dry_run bots, leverage tiers, funding)

    During a 429 backoff:
      - CRITICAL/HIGH: still queued and served (slower rate)
      - NORMAL/LOW: immediately refused (circuit breaker)
    After backoff expires, rate ramps back up over 10s instead of
    dumping the entire queue at once.
    """

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3

    # Requests at or above this priority are shed during backoff
    _SHED_THRESHOLD = NORMAL

    _BACKOFF_DURATIONS = [30, 60, 120, 240]  # escalating on consecutive 429s
    _BACKOFF_COOLDOWN_S = 300  # reset escalation after 5 min without 429

    def __init__(self, rate_per_s: float, burst: float) -> None:
        self.rate_per_s = rate_per_s
        self.burst = burst
        self.tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._backoff_until = 0.0
        self._backoff_factor = 1.0
        self._rampup_until = 0.0
        self._consecutive_backoffs = 0
        self._last_backoff_trigger = 0.0
        # Priority queue: (priority, -capital, counter, cost, future)
        self._waiters: list[tuple[int, float, int, float, asyncio.Future]] = []
        self._counter = 0
        self._drain_task: asyncio.Task | None = None
        # Stats
        self.shed_count = 0
        self.backoff_count = 0

    @property
    def backoff_active(self) -> bool:
        return time.monotonic() < self._backoff_until

    @property
    def backoff_remaining_s(self) -> float:
        return max(0.0, self._backoff_until - time.monotonic())

    def _effective_rate(self) -> float:
        now = time.monotonic()
        rate = self.rate_per_s / self._backoff_factor
        # Ramp-up: after backoff expires, start at 25% and linearly
        # increase to 100% to avoid a burst triggering another 429.
        if self._rampup_until > now and self._backoff_until <= now:
            rampup_total = self._rampup_until - self._backoff_until
            if rampup_total > 0:
                rampup_progress = 1.0 - (self._rampup_until - now) / rampup_total
                ramp_factor = 0.25 + 0.75 * max(0.0, rampup_progress)
                rate *= ramp_factor
        return rate

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        self.tokens = min(self.burst, self.tokens + elapsed * self._effective_rate())

    async def acquire(
        self, cost: float = 1.0, priority: int = 2, capital: float = 0.0,
    ) -> None:
        # Circuit breaker: shed non-critical requests during backoff
        if priority >= self._SHED_THRESHOLD and self.backoff_active:
            self.shed_count += 1
            raise RateLimitShed(
                f"request shed (priority={priority}) during 429 backoff "
                f"({self.backoff_remaining_s:.0f}s remaining)"
            )

        async with self._lock:
            self._refill()
            if not self._waiters and self.tokens >= cost:
                self.tokens -= cost
                return
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        entry = (priority, -capital, self._counter, cost, future)
        self._counter += 1
        heapq.heappush(self._waiters, entry)
        self._ensure_drain()
        await future

    def _ensure_drain(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain_loop())

    _POST_BACKOFF_MAX_BURST = 5

    async def _drain_loop(self) -> None:
        served_since_backoff = 0
        while self._waiters:
            async with self._lock:
                now = time.monotonic()
                if now < self._backoff_until:
                    self._shed_low_priority_waiters()
                    wait = self._backoff_until - now
                else:
                    if self._backoff_factor > 1.0:
                        self._shed_low_priority_waiters()
                        self._backoff_factor = max(1.0, self._backoff_factor / 2.0)
                        served_since_backoff = 0
                        if self._backoff_factor <= 1.0:
                            logger.info("back-off fully relaxed, resuming normal rate")
                        else:
                            logger.info(
                                "back-off relaxed to %.1fx", self._backoff_factor,
                            )
                    self._refill()
                    if self._waiters:
                        entry = self._waiters[0]
                        cost = entry[3]
                        if self.tokens >= cost:
                            heapq.heappop(self._waiters)
                            self.tokens -= cost
                            future = entry[4]
                            if not future.done():
                                future.set_result(None)
                            served_since_backoff += 1
                            effective_rate = self._effective_rate()
                            spacing = 1.0 / max(effective_rate, 0.1)
                            if served_since_backoff <= self._POST_BACKOFF_MAX_BURST:
                                spacing = max(spacing, 2.0)
                            await asyncio.sleep(spacing)
                            continue
                    effective_rate = self._effective_rate()
                    needed = (self._waiters[0][3] - self.tokens) if self._waiters else 1.0
                    wait = needed / max(effective_rate, 0.001)
            await asyncio.sleep(wait)

    def _shed_low_priority_waiters(self) -> None:
        """Cancel all NORMAL/LOW waiters during backoff — they should not wait."""
        kept: list[tuple[int, float, int, float, asyncio.Future]] = []
        for entry in self._waiters:
            priority, _cap, _cnt, _cost, future = entry
            if priority >= self._SHED_THRESHOLD:
                if not future.done():
                    future.set_exception(RateLimitShed("shed during backoff"))
                self.shed_count += 1
            else:
                kept.append(entry)
        if len(kept) < len(self._waiters):
            shed_n = len(self._waiters) - len(kept)
            logger.info("shed %d low-priority waiters during backoff", shed_n)
            self._waiters = kept
            heapq.heapify(self._waiters)

    def trigger_backoff(
        self, factor: float = 2.0, event_log: EventLog | None = None,
        exchange: str = "",
    ) -> None:
        now = time.monotonic()
        if now < self._backoff_until:
            return
        # Reset escalation if enough time passed since last 429
        if (now - self._last_backoff_trigger) > self._BACKOFF_COOLDOWN_S:
            self._consecutive_backoffs = 0
        idx = min(self._consecutive_backoffs, len(self._BACKOFF_DURATIONS) - 1)
        duration_s = self._BACKOFF_DURATIONS[idx]
        self._consecutive_backoffs += 1
        self._last_backoff_trigger = now
        self._backoff_factor = min(max(self._backoff_factor, factor), 4.0)
        self._backoff_until = now + duration_s
        rampup_s = min(duration_s * 0.5, 30.0)
        self._rampup_until = now + duration_s + rampup_s
        self.backoff_count += 1
        logger.warning(
            "rate-limit back-off triggered: factor=%.1fx for %.0fs "
            "(level %d/%d, NORMAL/LOW requests will be shed)",
            self._backoff_factor, duration_s,
            idx + 1, len(self._BACKOFF_DURATIONS),
        )
        if event_log:
            event_log.emit(
                "backoff_start", exchange=exchange,
                duration_s=duration_s,
                level=idx + 1,
                factor=self._backoff_factor,
            )
        self._shed_low_priority_waiters()

    def relax_backoff(self) -> None:
        if self._backoff_factor > 1.0:
            self._backoff_factor = max(1.0, self._backoff_factor / 2.0)


# -------------------------------------------------------------------- ccxt fetcher


class ExchangeFetcher:
    """One ccxt async client per (exchange, trading_mode)."""

    # Exchanges that need `defaultType: swap` for perpetuals. Same logic as
    # freqtrade's per-exchange `_ccxt_config`.
    _DEFAULT_TYPE_MAP = {
        "hyperliquid": "swap",
        "binance": "future",
        "binanceusdm": "future",
        "bybit": "swap",
        "gate": "swap",
        "okx": "swap",
        "kucoin": "swap",
    }

    def __init__(
        self, exchange: str, trading_mode: str, budget: TokenBucket,
        event_log: EventLog | None = None,
    ) -> None:
        self.exchange = exchange
        self.trading_mode = trading_mode
        self.budget = budget
        self._event_log = event_log
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            import ccxt.async_support as ccxt_async
            if not hasattr(ccxt_async, self.exchange):
                raise RuntimeError(f"ccxt has no exchange '{self.exchange}'")
            config: dict[str, Any] = {"enableRateLimit": False}
            if self.trading_mode == "futures":
                dt = self._DEFAULT_TYPE_MAP.get(self.exchange)
                if dt:
                    config["options"] = {"defaultType": dt}
            self._client = getattr(ccxt_async, self.exchange)(config)
            logger.info(
                "initialised ccxt async client for %s (trading_mode=%s)",
                self.exchange, self.trading_mode,
            )
            return self._client

    _FETCH_TIMEOUT_S = 60.0

    async def fetch_ohlcv(
        self, pair: str, timeframe: str, since_ms: int | None,
        limit: int | None, candle_type: str,
        priority: int = TokenBucket.NORMAL, capital: float = 0.0,
    ) -> list[list]:
        client = await self._ensure_client()
        params: dict[str, Any] = {}
        if candle_type and candle_type not in ("spot", "futures"):
            params["price"] = candle_type
        await self.budget.acquire(1.0, priority=priority, capital=capital)
        try:
            data = await asyncio.wait_for(
                client.fetch_ohlcv(
                    pair, timeframe=timeframe, since=since_ms,
                    limit=limit, params=params,
                ),
                timeout=self._FETCH_TIMEOUT_S,
            )
            return data
        except TimeoutError:
            logger.warning(
                "fetch_ohlcv timed out after %.0fs for %s %s",
                self._FETCH_TIMEOUT_S, pair, timeframe,
            )
            raise
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RateLimit" in e.__class__.__name__:
                if self._event_log:
                    self._event_log.emit(
                        "rate_limit_429", exchange=self.exchange,
                        pair=pair, timeframe=timeframe,
                    )
                self.budget.trigger_backoff(
                    2.0, event_log=self._event_log, exchange=self.exchange,
                )
            raise

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass


# -------------------------------------------------------------------- stats


@dataclass
class DaemonStats:
    started_monotonic: float = field(default_factory=time.monotonic)
    requests_total: int = 0
    cache_hits: int = 0          # served fully from cache, no fetch
    cache_partial: int = 0       # partial-range: some from cache, some fetched
    cache_misses: int = 0        # nothing in cache, full fetch
    fetch_errors: int = 0
    active_clients: int = 0
    last_client_disconnect_monotonic: float | None = None
    # Centralized rate limiter stats
    acquire_total: int = 0
    tickers_requests: int = 0
    tickers_cache_hits: int = 0
    tickers_fetches: int = 0
    positions_puts: int = 0
    positions_gets: int = 0
    positions_cache_hits: int = 0
    # Connection churn tracking
    total_connects: int = 0
    total_disconnects: int = 0
    peak_clients: int = 0
    short_lived_connections: int = 0

    def uptime_s(self) -> float:
        return time.monotonic() - self.started_monotonic


# -------------------------------------------------------------------- shared caches


@dataclass
class _TickersCacheEntry:
    """Cached tickers result with wall-clock expiry."""
    data: dict
    fetched_at: float  # time.monotonic()
    market_type: str


@dataclass
class _PositionsCacheEntry:
    """Cached positions result pushed by a bot."""
    data: list
    pushed_at: float  # time.monotonic()


@dataclass
class _BalancesCacheEntry:
    """Cached balances result pushed by a bot."""
    data: dict
    pushed_at: float  # time.monotonic()


@dataclass
class _MarketsCacheEntry:
    """Cached markets result (one fetch shared by all bots)."""
    data: dict
    fetched_at: float  # time.monotonic()


@dataclass
class _FundingRatesCacheEntry:
    """Cached funding rates (bulk fetch, all pairs)."""
    data: dict
    fetched_at: float  # time.monotonic()


@dataclass
class _LeverageTiersCacheEntry:
    """Cached leverage tiers (bulk fetch, all pairs)."""
    data: dict
    fetched_at: float  # time.monotonic()


# -------------------------------------------------------------------- fleet registry


@dataclass
class BotEntry:
    bot_id: str
    config_file: str
    exchange: str
    trading_mode: str
    strategy: str
    timeframe: str
    pairs_count: int
    dry_run: bool
    api_port: int
    pid: int
    connected_at: float
    last_heartbeat: float
    state: str
    connection_id: int


class BotRegistry:
    def __init__(self) -> None:
        self._bots: dict[str, BotEntry] = {}
        self._conn_to_bot: dict[int, str] = {}

    def register(self, bot_id: str, info: dict, conn_id: int) -> BotEntry:
        now = time.monotonic()
        entry = BotEntry(
            bot_id=bot_id,
            config_file=info.get("config_file", ""),
            exchange=info.get("exchange", ""),
            trading_mode=info.get("trading_mode", ""),
            strategy=info.get("strategy", ""),
            timeframe=info.get("timeframe", ""),
            pairs_count=info.get("pairs_count", 0),
            dry_run=info.get("dry_run", False),
            api_port=info.get("api_port", 0),
            pid=info.get("pid", 0),
            connected_at=now,
            last_heartbeat=now,
            state="initializing",
            connection_id=conn_id,
        )
        old = self._bots.get(bot_id)
        if old is not None:
            self._conn_to_bot.pop(old.connection_id, None)
        self._bots[bot_id] = entry
        self._conn_to_bot[conn_id] = bot_id
        return entry

    def heartbeat(self, conn_id: int) -> None:
        bot_id = self._conn_to_bot.get(conn_id)
        if bot_id is not None:
            entry = self._bots.get(bot_id)
            if entry is not None:
                entry.last_heartbeat = time.monotonic()

    def update_state(self, conn_id: int, state: str, pairs_count: int = 0) -> None:
        bot_id = self._conn_to_bot.get(conn_id)
        if bot_id is not None:
            entry = self._bots.get(bot_id)
            if entry is not None:
                entry.state = state
                if pairs_count > 0:
                    entry.pairs_count = pairs_count

    def unregister(self, conn_id: int, reason: str = "disconnect") -> BotEntry | None:
        bot_id = self._conn_to_bot.pop(conn_id, None)
        if bot_id is None:
            return None
        return self._bots.pop(bot_id, None)

    def get_fleet_status(self) -> list[dict]:
        now = time.monotonic()
        result = []
        for entry in self._bots.values():
            result.append({
                "bot_id": entry.bot_id,
                "config_file": entry.config_file,
                "exchange": entry.exchange,
                "trading_mode": entry.trading_mode,
                "strategy": entry.strategy,
                "timeframe": entry.timeframe,
                "pairs_count": entry.pairs_count,
                "dry_run": entry.dry_run,
                "api_port": entry.api_port,
                "pid": entry.pid,
                "state": entry.state,
                "uptime_s": round(now - entry.connected_at, 1),
                "last_heartbeat_ago_s": round(now - entry.last_heartbeat, 1),
            })
        return result

    def count_initializing(self, exchange: str) -> int:
        return sum(
            1 for b in self._bots.values()
            if b.exchange == exchange and b.state == "initializing"
        )

    @property
    def size(self) -> int:
        return len(self._bots)


# -------------------------------------------------------------------- event log


@dataclass
class FleetEvent:
    ts: float
    event_type: str
    bot_id: str | None
    details: dict


class EventLog:
    _MAX_EVENTS = 10_000

    def __init__(self, persist_path: Path | None = None) -> None:
        self._events: deque[FleetEvent] = deque(maxlen=self._MAX_EVENTS)
        self._persist_path = persist_path
        self._unsaved: list[FleetEvent] = []

    def emit(self, event_type: str, bot_id: str | None = None, **details: Any) -> None:
        event = FleetEvent(
            ts=time.time(),
            event_type=event_type,
            bot_id=bot_id,
            details=details,
        )
        self._events.append(event)
        self._unsaved.append(event)
        logger.info(
            "FLEET_EVENT %s bot=%s %s",
            event_type, bot_id or "-", details,
        )

    def query(
        self,
        since_ts: float = 0,
        event_types: list[str] | None = None,
        bot_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        result = []
        for evt in reversed(self._events):
            if evt.ts < since_ts:
                break
            if event_types and evt.event_type not in event_types:
                continue
            if bot_id and evt.bot_id != bot_id:
                continue
            result.append({
                "ts": evt.ts,
                "event_type": evt.event_type,
                "bot_id": evt.bot_id,
                "details": evt.details,
            })
            if len(result) >= limit:
                break
        return result

    def recent_counts(self, window_s: float = 3600) -> dict[str, int]:
        cutoff = time.time() - window_s
        counts: dict[str, int] = {}
        for evt in reversed(self._events):
            if evt.ts < cutoff:
                break
            counts[evt.event_type] = counts.get(evt.event_type, 0) + 1
        return counts

    def flush(self) -> int:
        if not self._persist_path or not self._unsaved:
            return 0
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        n = len(self._unsaved)
        with self._persist_path.open("a") as f:
            for evt in self._unsaved:
                line = json.dumps({
                    "ts": evt.ts,
                    "type": evt.event_type,
                    "bot": evt.bot_id,
                    "d": evt.details,
                }, separators=(",", ":"))
                f.write(line + "\n")
        self._unsaved.clear()
        return n


# -------------------------------------------------------------------- daemon


class Daemon:
    def __init__(
        self,
        socket_path: str,
        global_cfg: dict,
        exchange_overrides: dict[str, dict] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.global_cfg = global_cfg
        self.exchange_overrides = exchange_overrides or {}
        self.store = CandleStore()
        self.budgets: dict[str, TokenBucket] = {}
        self.fetchers: dict[tuple[str, str], ExchangeFetcher] = {}
        self.coordinator = RequestCoordinator()
        self.stats = DaemonStats()
        self.persistence = FeatherPersistence(
            root=Path(global_cfg.get("persistence_path", "")),
            store=self.store,
        ) if global_cfg.get("persistence_path") else None
        self.registry = BotRegistry()
        events_path = Path(global_cfg.get("persistence_path", "")) / "fleet_events.jsonl"
        self.event_log = EventLog(
            persist_path=events_path if global_cfg.get("persistence_path") else None,
        )
        self._next_conn_id = 0
        self._server: asyncio.base_events.Server | None = None
        self._shutdown_event = asyncio.Event()
        self._idle_shutdown_s = float(global_cfg.get("idle_daemon_shutdown_s", 600))
        self._max_candles_per_series = int(global_cfg.get("max_candles_per_series", 5000))
        self._flush_interval_s = float(global_cfg.get("flush_interval_s", 30))
        self._pending_fetches = 0
        self._peak_pending = 0
        # Shared caches for centralized rate limiting
        self._tickers_cache: dict[str, _TickersCacheEntry] = {}
        self._tickers_ttl_s = float(global_cfg.get("tickers_cache_ttl_s", 15.0))
        self._tickers_inflight: dict[str, asyncio.Event] = {}
        self._positions_cache: dict[str, _PositionsCacheEntry] = {}
        self._positions_ttl_s = float(global_cfg.get("positions_cache_ttl_s", 3.0))
        self._positions_inflight: dict[str, asyncio.Event] = {}
        self._balances_cache: dict[str, _BalancesCacheEntry] = {}
        self._balances_ttl_s = float(global_cfg.get("balances_cache_ttl_s", 5.0))
        self._balances_inflight: dict[str, asyncio.Event] = {}
        self._markets_cache: dict[str, _MarketsCacheEntry] = {}
        self._markets_ttl_s = float(global_cfg.get("markets_cache_ttl_s", 3600.0))
        self._markets_inflight: dict[str, asyncio.Event] = {}
        self._funding_rates_cache: dict[str, _FundingRatesCacheEntry] = {}
        self._funding_rates_ttl_s = float(global_cfg.get("funding_rates_cache_ttl_s", 300.0))
        self._funding_rates_inflight: dict[str, asyncio.Event] = {}
        self._leverage_tiers_cache: dict[str, _LeverageTiersCacheEntry] = {}
        self._leverage_tiers_ttl_s = float(global_cfg.get("leverage_tiers_cache_ttl_s", 3600.0))
        self._leverage_tiers_inflight: dict[str, asyncio.Event] = {}

    # --------- helpers

    def _exchange_cfg(self, exchange: str) -> dict:
        return resolve_exchange_config(
            exchange, self.exchange_overrides.get(exchange),
        )

    def _get_budget(self, exchange: str) -> TokenBucket:
        b = self.budgets.get(exchange)
        if b is None:
            ex_cfg = self._exchange_cfg(exchange)
            b = TokenBucket(
                rate_per_s=ex_cfg.get("rate_per_s", 5),
                burst=ex_cfg.get("burst", 10),
            )
            self.budgets[exchange] = b
        return b

    def _collect_budget_stats(self) -> dict:
        """Aggregate token bucket state across all exchanges for stats op."""
        result: dict = {}
        for exchange, bucket in self.budgets.items():
            now = time.monotonic()
            backoff_remaining = max(0.0, bucket._backoff_until - now)
            q_depths = {"critical": 0, "high": 0, "normal": 0, "low": 0}
            prio_names = {0: "critical", 1: "high", 2: "normal", 3: "low"}
            for waiter in bucket._waiters:
                prio_name = prio_names.get(waiter[0], "low")
                q_depths[prio_name] += 1
            result[f"budget_{exchange}"] = {
                "tokens_available": round(bucket.tokens, 2),
                "tokens_max": bucket.burst,
                "refill_rate": bucket.rate_per_s,
                "backoff_active": bucket.backoff_active,
                "backoff_factor": round(bucket._backoff_factor, 2),
                "backoff_remaining_s": round(backoff_remaining, 1),
                "queue_depths": q_depths,
                "shed_count": bucket.shed_count,
                "backoff_count": bucket.backoff_count,
                "consecutive_backoffs": bucket._consecutive_backoffs,
                "current_backoff_duration_s": (
                    bucket._BACKOFF_DURATIONS[
                        min(bucket._consecutive_backoffs - 1,
                            len(bucket._BACKOFF_DURATIONS) - 1)
                    ] if bucket._consecutive_backoffs > 0 else 0
                ),
            }
        if len(self.budgets) == 1:
            only_key = next(iter(result))
            for k, v in result[only_key].items():
                result[k] = v
        return result

    def _get_fetcher(self, exchange: str, trading_mode: str) -> ExchangeFetcher:
        k = (exchange, trading_mode)
        f = self.fetchers.get(k)
        if f is None:
            f = ExchangeFetcher(exchange, trading_mode, self._get_budget(exchange),
                                event_log=self.event_log)
            self.fetchers[k] = f
        return f

    # --------- fetch handler

    @staticmethod
    def _is_server_error(exc: Exception) -> bool:
        """Return True if the exception looks like a transient 500/503."""
        msg = str(exc)
        return (
            "500" in msg
            or "Internal Server Error" in msg
            or "503" in msg
            or "Service Unavailable" in msg
        )

    async def _fetch_chunk(
        self, series: CandleSeries, chunk: Gap, exchange_cfg: dict,
        priority: int = TokenBucket.NORMAL, capital: float = 0.0,
    ) -> None:
        """Execute one exchange fetch and merge the result into `series`."""
        fetcher = self._get_fetcher(series.exchange, series.trading_mode)
        limit = chunk.n_candles(series.tf_ms)
        # Ccxt generally returns <= limit candles; never go over the
        # exchange's documented max_candles_per_call.
        max_per_call = int(exchange_cfg.get("max_candles_per_call", 1000))
        limit = min(limit, max_per_call) if limit else max_per_call

        max_retries = 3
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                data = await fetcher.fetch_ohlcv(
                    pair=series.pair, timeframe=series.timeframe,
                    since_ms=chunk.start_ms, limit=limit,
                    candle_type=series.candle_type,
                    priority=priority, capital=capital,
                )
                break  # success
            except Exception as e:
                last_exc = e
                # Only retry on transient server errors (500/503),
                # not on rate-limits (handled by TokenBucket) or other errors.
                if attempt < max_retries and self._is_server_error(e):
                    delay = 2 ** (attempt + 1)  # 2s, 4s, 8s
                    logger.warning(
                        "chunk fetch attempt %d/%d failed with server error "
                        "%s %s [%d..%d): %s — retrying in %ds",
                        attempt + 1, max_retries + 1,
                        series.pair, series.timeframe,
                        chunk.start_ms, chunk.end_ms,
                        e, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                # Non-retryable error or last attempt exhausted
                self.stats.fetch_errors += 1
                logger.warning(
                    "chunk fetch failed %s %s [%d..%d): %s: %s",
                    series.pair, series.timeframe,
                    chunk.start_ms, chunk.end_ms,
                    e.__class__.__name__, e,
                )
                raise
        else:
            # All retries exhausted (should not normally reach here since
            # the last iteration raises, but guard defensively)
            self.stats.fetch_errors += 1
            raise last_exc  # type: ignore[misc]

        if not data:
            return

        # Detect historic boundary: first returned ts is well past what we
        # asked for AND we got fewer than requested → earliest available.
        first_ts = int(data[0][0])
        tolerance_ms = series.tf_ms  # allow 1 candle of slack
        if (
            series.earliest_available_ts is None
            and first_ts > chunk.start_ms + tolerance_ms
            and len(data) < limit
        ):
            series.earliest_available_ts = first_ts
            logger.info(
                "detected earliest_available_ts=%d for %s %s",
                first_ts, series.pair, series.timeframe,
            )

        series.merge(data)

        # Trim if we've grown past the cap
        series.trim_to(self._max_candles_per_series)

    async def _handle_fetch(self, req: dict) -> dict:
        t0 = time.monotonic()
        self.stats.requests_total += 1
        exchange = req["exchange"]
        trading_mode = req.get("trading_mode", "spot")
        pair = req["pair"]
        timeframe = req["timeframe"]
        candle_type = req.get("candle_type") or trading_mode
        since_ms = req.get("since_ms")
        limit = int(req.get("limit") or 500)
        tf_ms = tf_to_ms(timeframe)
        priority = int(req.get("priority", TokenBucket.NORMAL))
        capital = float(req.get("capital", 0.0))

        series = self.store.get_or_create(
            exchange, trading_mode, pair, timeframe, candle_type, tf_ms,
        )
        ex_cfg = self._exchange_cfg(exchange)
        refresh_overlap = int(ex_cfg.get("refresh_overlap_candles", 3))

        is_live = since_ms is None
        # Compute requested range (half-open end)
        if is_live:
            now_ms = int(time.time() * 1000)
            end_ms = ((now_ms // tf_ms) + 1) * tf_ms
            start_ms = end_ms - limit * tf_ms
        else:
            start_ms = (int(since_ms) // tf_ms) * tf_ms
            end_ms = start_ms + limit * tf_ms

        # Fast-path for live: if we refreshed within the current tf window
        # AND the cache fully covers the requested range, just serve it.
        now_wall_ms = int(time.time() * 1000)
        if (
            is_live
            and series.last_live_refresh_wall_ms > 0
            and (now_wall_ms - series.last_live_refresh_wall_ms) < tf_ms
            and series.range_start_ms is not None
            and series.range_end_ms is not None
            and series.range_start_ms <= start_ms
            and series.range_end_ms >= (end_ms - tf_ms)
        ):
            series.hits += 1
            self.stats.cache_hits += 1
            return self._ok(
                req["req_id"], series, start_ms, end_ms, t0, served_from="cache",
            )

        gaps = compute_gaps(
            requested_start_ms=start_ms,
            requested_end_ms=end_ms,
            cached_start_ms=series.range_start_ms,
            cached_end_ms=series.range_end_ms,
            tf_ms=tf_ms,
            refresh_overlap_candles=refresh_overlap if is_live else 0,
            earliest_available_ts=series.earliest_available_ts,
        )

        if not gaps:
            series.hits += 1
            self.stats.cache_hits += 1
            return self._ok(
                req["req_id"], series, start_ms, end_ms, t0, served_from="cache",
            )

        max_chunk = int(ex_cfg.get("max_candles_per_call", 1000))
        chunks: list[Gap] = []
        for g in gaps:
            chunks.extend(chunk_gap(g, max_chunk, tf_ms))

        had_any_cache = series.n_candles > 0
        errors = 0
        for chunk in chunks:
            key = (
                exchange, trading_mode, pair, timeframe, candle_type,
                chunk.start_ms, chunk.end_ms,
            )
            async def _do_fetch(c=chunk):
                await self._fetch_chunk(
                    series, c, ex_cfg,
                    priority=priority, capital=capital,
                )
            try:
                await self.coordinator.run(key, _do_fetch)
            except Exception:
                errors += 1

        if is_live and errors == 0:
            series.last_live_refresh_wall_ms = int(time.time() * 1000)

        served_from: str
        if had_any_cache and errors == 0:
            series.misses += 1  # partial counts as miss from the series' POV
            self.stats.cache_partial += 1
            served_from = "partial"
        else:
            series.misses += 1
            self.stats.cache_misses += 1
            served_from = "fetch"

        data_rows = series.slice_range(start_ms, end_ms)
        if not data_rows and errors:
            return {
                "req_id": req["req_id"], "ok": False,
                "pair": pair, "timeframe": timeframe,
                "candle_type": candle_type,
                "error_type": "FetchFailed",
                "error_message": f"{errors} chunk(s) failed, no cached data",
                "latency_ms": (time.monotonic() - t0) * 1000,
            }

        return {
            "req_id": req["req_id"], "ok": True,
            "pair": pair, "timeframe": timeframe,
            "candle_type": candle_type,
            "data": data_rows,
            "drop_incomplete": True if candle_type != "funding_rate" else False,
            "served_from": served_from,
            "latency_ms": (time.monotonic() - t0) * 1000,
        }

    def _ok(
        self, req_id: str, series: CandleSeries,
        start_ms: int, end_ms: int, t0: float, served_from: str,
    ) -> dict:
        return {
            "req_id": req_id, "ok": True,
            "pair": series.pair, "timeframe": series.timeframe,
            "candle_type": series.candle_type,
            "data": series.slice_range(start_ms, end_ms),
            "drop_incomplete": True if series.candle_type != "funding_rate" else False,
            "served_from": served_from,
            "latency_ms": (time.monotonic() - t0) * 1000,
        }

    # --------- centralized rate limiter: acquire

    async def _handle_acquire(self, req: dict) -> dict:
        """Acquire a rate token from the exchange's TokenBucket.

        Bots call this before any non-OHLCV REST call (create_order, etc.)
        so ALL API traffic shares one rate limit.

        During a 429 backoff, NORMAL/LOW priority requests are immediately
        refused with ``throttled: true`` so bots can skip non-essential work.
        CRITICAL/HIGH requests (orders, exits) always go through.
        """
        exchange = req.get("exchange", "hyperliquid")
        priority = int(req.get("priority", TokenBucket.NORMAL))
        capital = float(req.get("capital", 0.0))
        cost = float(req.get("cost", 1.0))
        budget = self._get_budget(exchange)
        self.stats.acquire_total += 1
        try:
            await budget.acquire(cost, priority=priority, capital=capital)
        except RateLimitShed:
            return {
                "req_id": req.get("req_id", ""), "ok": False,
                "throttled": True,
                "backoff_remaining_s": budget.backoff_remaining_s,
                "error_type": "RateLimitShed",
                "error_message": (
                    f"429 backoff active ({budget.backoff_remaining_s:.0f}s left)"
                    f" — non-critical request shed (priority={priority})"
                ),
            }
        resp: dict[str, Any] = {"req_id": req.get("req_id", ""), "ok": True}
        if budget.backoff_active:
            resp["backoff_active"] = True
            resp["backoff_remaining_s"] = budget.backoff_remaining_s
        return resp

    # --------- centralized rate limiter: shared tickers

    async def _handle_tickers(self, req: dict) -> dict:
        """Return cached tickers or fetch them (one fetch coalesced for all bots)."""
        exchange = req.get("exchange", "hyperliquid")
        trading_mode = req.get("trading_mode", "futures")
        market_type = req.get("market_type", "")
        self.stats.tickers_requests += 1

        cache_key = f"{exchange}:{trading_mode}:{market_type}"
        now = time.monotonic()

        entry = self._tickers_cache.get(cache_key)
        if entry and (now - entry.fetched_at) < self._tickers_ttl_s:
            self.stats.tickers_cache_hits += 1
            return {
                "req_id": req.get("req_id", ""), "ok": True,
                "data": entry.data, "served_from": "cache",
            }

        # Coalesce concurrent requests: if a fetch is already in-flight,
        # wait for it instead of sending a duplicate.
        inflight = self._tickers_inflight.get(cache_key)
        if inflight is not None:
            await inflight.wait()
            entry = self._tickers_cache.get(cache_key)
            if entry:
                self.stats.tickers_cache_hits += 1
                return {
                    "req_id": req.get("req_id", ""), "ok": True,
                    "data": entry.data, "served_from": "cache",
                }

        # We do the fetch — set inflight event
        evt = asyncio.Event()
        self._tickers_inflight[cache_key] = evt
        try:
            budget = self._get_budget(exchange)
            await budget.acquire(1.0, priority=TokenBucket.NORMAL)
            fetcher = self._get_fetcher(exchange, trading_mode)
            client = await fetcher._ensure_client()
            params: dict[str, Any] = {}
            if market_type:
                market_types_map = {"futures": "swap"}
                params["type"] = market_types_map.get(market_type, market_type)
            data = await asyncio.wait_for(
                client.fetch_tickers(params=params), timeout=60.0,
            )
            self._tickers_cache[cache_key] = _TickersCacheEntry(
                data=data, fetched_at=time.monotonic(), market_type=market_type,
            )
            self.stats.tickers_fetches += 1
            return {
                "req_id": req.get("req_id", ""), "ok": True,
                "data": data, "served_from": "fetch",
            }
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RateLimit" in e.__class__.__name__:
                budget.trigger_backoff(2.0)
            return {
                "req_id": req.get("req_id", ""), "ok": False,
                "error_type": e.__class__.__name__,
                "error_message": str(e),
            }
        finally:
            evt.set()
            self._tickers_inflight.pop(cache_key, None)

    # --------- centralized rate limiter: shared positions cache

    async def _handle_positions_put(self, req: dict) -> dict:
        """Bot pushes its fetch_positions() result into the shared cache."""
        exchange = req.get("exchange", "hyperliquid")
        data = req.get("data", [])
        cache_key = exchange
        self.stats.positions_puts += 1
        self._positions_cache[cache_key] = _PositionsCacheEntry(
            data=data, pushed_at=time.monotonic(),
        )
        inflight = self._positions_inflight.pop(cache_key, None)
        if inflight is not None:
            inflight.set()
        return {"req_id": req.get("req_id", ""), "ok": True}

    async def _handle_positions_get(self, req: dict) -> dict:
        """Bot reads cached positions, coalescing concurrent fetches.

        If cache is stale and another bot is already fetching, wait for
        its push instead of returning a miss (which would cause a second
        redundant API call).
        """
        exchange = req.get("exchange", "hyperliquid")
        cache_key = exchange
        self.stats.positions_gets += 1
        entry = self._positions_cache.get(cache_key)
        if entry and (time.monotonic() - entry.pushed_at) < self._positions_ttl_s:
            self.stats.positions_cache_hits += 1
            return {
                "req_id": req.get("req_id", ""), "ok": True,
                "hit": True, "data": entry.data,
                "age_s": time.monotonic() - entry.pushed_at,
            }

        inflight = self._positions_inflight.get(cache_key)
        if inflight is not None:
            try:
                await asyncio.wait_for(inflight.wait(), timeout=15.0)
                entry = self._positions_cache.get(cache_key)
                if entry:
                    self.stats.positions_cache_hits += 1
                    return {
                        "req_id": req.get("req_id", ""), "ok": True,
                        "hit": True, "data": entry.data,
                        "age_s": time.monotonic() - entry.pushed_at,
                    }
            except TimeoutError:
                self._positions_inflight.pop(cache_key, None)

        self._positions_inflight[cache_key] = asyncio.Event()
        return {
            "req_id": req.get("req_id", ""), "ok": True,
            "hit": False, "data": [],
        }

    # --------- centralized rate limiter: shared balances cache

    async def _handle_balances_put(self, req: dict) -> dict:
        exchange = req.get("exchange", "hyperliquid")
        data = req.get("data", {})
        self._balances_cache[exchange] = _BalancesCacheEntry(
            data=data, pushed_at=time.monotonic(),
        )
        inflight = self._balances_inflight.pop(exchange, None)
        if inflight is not None:
            inflight.set()
        return {"req_id": req.get("req_id", ""), "ok": True}

    async def _handle_balances_get(self, req: dict) -> dict:
        """Bot reads cached balances, coalescing concurrent fetches."""
        exchange = req.get("exchange", "hyperliquid")
        entry = self._balances_cache.get(exchange)
        if entry and (time.monotonic() - entry.pushed_at) < self._balances_ttl_s:
            return {
                "req_id": req.get("req_id", ""), "ok": True,
                "hit": True, "data": entry.data,
            }

        inflight = self._balances_inflight.get(exchange)
        if inflight is not None:
            try:
                await asyncio.wait_for(inflight.wait(), timeout=15.0)
                entry = self._balances_cache.get(exchange)
                if entry:
                    return {
                        "req_id": req.get("req_id", ""), "ok": True,
                        "hit": True, "data": entry.data,
                    }
            except TimeoutError:
                self._balances_inflight.pop(exchange, None)

        self._balances_inflight[exchange] = asyncio.Event()
        return {
            "req_id": req.get("req_id", ""), "ok": True,
            "hit": False, "data": {},
        }

    # --------- centralized: shared markets cache

    async def _handle_markets(self, req: dict) -> dict:
        """Return cached markets or fetch them (one fetch for all bots)."""
        exchange = req.get("exchange", "hyperliquid")
        trading_mode = req.get("trading_mode", "futures")
        cache_key = f"{exchange}:{trading_mode}"
        now = time.monotonic()

        entry = self._markets_cache.get(cache_key)
        if entry and (now - entry.fetched_at) < self._markets_ttl_s:
            return {
                "req_id": req.get("req_id", ""), "ok": True,
                "data": entry.data, "served_from": "cache",
                "age_s": now - entry.fetched_at,
            }

        inflight = self._markets_inflight.get(cache_key)
        if inflight is not None:
            await inflight.wait()
            entry = self._markets_cache.get(cache_key)
            if entry:
                return {
                    "req_id": req.get("req_id", ""), "ok": True,
                    "data": entry.data, "served_from": "cache",
                    "age_s": time.monotonic() - entry.fetched_at,
                }

        evt = asyncio.Event()
        self._markets_inflight[cache_key] = evt
        try:
            budget = self._get_budget(exchange)
            await budget.acquire(1.0, priority=TokenBucket.NORMAL)
            fetcher = self._get_fetcher(exchange, trading_mode)
            client = await fetcher._ensure_client()
            data = await asyncio.wait_for(
                client.load_markets(), timeout=120.0,
            )
            if not isinstance(data, dict):
                logger.error(
                    "load_markets returned %s instead of dict — "
                    "discarding (exchange=%s)",
                    type(data).__name__, exchange,
                )
                return {
                    "req_id": req.get("req_id", ""), "ok": False,
                    "error_type": "TypeError",
                    "error_message": f"load_markets returned {type(data).__name__}",
                }
            self._markets_cache[cache_key] = _MarketsCacheEntry(
                data=data, fetched_at=time.monotonic(),
            )
            logger.info(
                "markets fetched for %s/%s: %d symbols",
                exchange, trading_mode, len(data),
            )
            return {
                "req_id": req.get("req_id", ""), "ok": True,
                "data": data, "served_from": "fetch",
            }
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RateLimit" in e.__class__.__name__:
                budget.trigger_backoff(2.0)
            return {
                "req_id": req.get("req_id", ""), "ok": False,
                "error_type": e.__class__.__name__,
                "error_message": str(e),
            }
        finally:
            evt.set()
            self._markets_inflight.pop(cache_key, None)

    # --------- centralized: shared funding rates cache

    async def _handle_funding_rates(self, req: dict) -> dict:
        """Return cached funding rates or fetch them (one bulk fetch for all bots)."""
        exchange = req.get("exchange", "hyperliquid")
        trading_mode = req.get("trading_mode", "futures")
        cache_key = f"{exchange}:{trading_mode}"
        now = time.monotonic()

        entry = self._funding_rates_cache.get(cache_key)
        if entry and (now - entry.fetched_at) < self._funding_rates_ttl_s:
            return {
                "req_id": req.get("req_id", ""), "ok": True,
                "data": entry.data, "served_from": "cache",
                "age_s": now - entry.fetched_at,
            }

        inflight = self._funding_rates_inflight.get(cache_key)
        if inflight is not None:
            await inflight.wait()
            entry = self._funding_rates_cache.get(cache_key)
            if entry:
                return {
                    "req_id": req.get("req_id", ""), "ok": True,
                    "data": entry.data, "served_from": "cache",
                    "age_s": time.monotonic() - entry.fetched_at,
                }

        evt = asyncio.Event()
        self._funding_rates_inflight[cache_key] = evt
        try:
            budget = self._get_budget(exchange)
            await budget.acquire(1.0, priority=TokenBucket.NORMAL)
            fetcher = self._get_fetcher(exchange, trading_mode)
            client = await fetcher._ensure_client()
            data = await asyncio.wait_for(
                client.fetch_funding_rates(), timeout=60.0,
            )
            self._funding_rates_cache[cache_key] = _FundingRatesCacheEntry(
                data=data, fetched_at=time.monotonic(),
            )
            logger.info(
                "funding rates fetched for %s/%s: %d pairs",
                exchange, trading_mode, len(data),
            )
            return {
                "req_id": req.get("req_id", ""), "ok": True,
                "data": data, "served_from": "fetch",
            }
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RateLimit" in e.__class__.__name__:
                budget.trigger_backoff(2.0)
            return {
                "req_id": req.get("req_id", ""), "ok": False,
                "error_type": e.__class__.__name__,
                "error_message": str(e),
            }
        finally:
            evt.set()
            self._funding_rates_inflight.pop(cache_key, None)

    # --------- centralized: shared leverage tiers cache

    async def _handle_leverage_tiers(self, req: dict) -> dict:
        """Return cached leverage tiers or fetch them (one bulk fetch for all bots)."""
        exchange = req.get("exchange", "hyperliquid")
        trading_mode = req.get("trading_mode", "futures")
        cache_key = f"{exchange}:{trading_mode}"
        now = time.monotonic()

        entry = self._leverage_tiers_cache.get(cache_key)
        if entry and (now - entry.fetched_at) < self._leverage_tiers_ttl_s:
            return {
                "req_id": req.get("req_id", ""), "ok": True,
                "data": entry.data, "served_from": "cache",
                "age_s": now - entry.fetched_at,
            }

        inflight = self._leverage_tiers_inflight.get(cache_key)
        if inflight is not None:
            await inflight.wait()
            entry = self._leverage_tiers_cache.get(cache_key)
            if entry:
                return {
                    "req_id": req.get("req_id", ""), "ok": True,
                    "data": entry.data, "served_from": "cache",
                    "age_s": time.monotonic() - entry.fetched_at,
                }

        evt = asyncio.Event()
        self._leverage_tiers_inflight[cache_key] = evt
        try:
            budget = self._get_budget(exchange)
            await budget.acquire(1.0, priority=TokenBucket.LOW)
            fetcher = self._get_fetcher(exchange, trading_mode)
            client = await fetcher._ensure_client()
            data = await asyncio.wait_for(
                client.fetch_leverage_tiers(), timeout=120.0,
            )
            self._leverage_tiers_cache[cache_key] = _LeverageTiersCacheEntry(
                data=data, fetched_at=time.monotonic(),
            )
            logger.info(
                "leverage tiers fetched for %s/%s: %d pairs",
                exchange, trading_mode, len(data),
            )
            return {
                "req_id": req.get("req_id", ""), "ok": True,
                "data": data, "served_from": "fetch",
            }
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RateLimit" in e.__class__.__name__:
                budget.trigger_backoff(2.0)
            return {
                "req_id": req.get("req_id", ""), "ok": False,
                "error_type": e.__class__.__name__,
                "error_message": str(e),
            }
        finally:
            evt.set()
            self._leverage_tiers_inflight.pop(cache_key, None)

    # --------- fleet handlers

    def _handle_register(self, req: dict, conn_id: int) -> dict:
        bot_id = req.get("bot_id", "")
        if not bot_id:
            return {"req_id": req.get("req_id", ""), "ok": False,
                    "error_message": "bot_id required"}
        entry = self.registry.register(bot_id, req, conn_id)
        initializing_count = self.registry.count_initializing(entry.exchange)
        stagger_s = max(0.0, (initializing_count - 1) * 15.0)
        self.event_log.emit("bot_connect", bot_id=bot_id,
                            exchange=entry.exchange, strategy=entry.strategy,
                            pid=entry.pid, config_file=entry.config_file)
        if stagger_s > 0:
            self.event_log.emit("stagger_applied", bot_id=bot_id,
                                stagger_s=stagger_s,
                                initializing_count=initializing_count)
        logger.info(
            "bot registered: %s (exchange=%s strategy=%s pid=%d) fleet_size=%d stagger=%.0fs",
            bot_id, entry.exchange, entry.strategy, entry.pid,
            self.registry.size, stagger_s,
        )
        return {
            "req_id": req.get("req_id", ""),
            "ok": True,
            "stagger_s": stagger_s,
            "fleet_size": self.registry.size,
        }

    def _handle_unregister(self, req: dict, conn_id: int) -> dict:
        entry = self.registry.unregister(conn_id, reason="clean_shutdown")
        if entry:
            self.event_log.emit("bot_disconnect", bot_id=entry.bot_id,
                                reason="clean_shutdown",
                                uptime_s=round(time.monotonic() - entry.connected_at, 1))
            logger.info("bot unregistered: %s (clean shutdown)", entry.bot_id)
        return {"req_id": req.get("req_id", ""), "ok": True}

    def _handle_state_update(self, req: dict, conn_id: int) -> dict:
        new_state = req.get("state", "")
        pairs_count = req.get("pairs_count", 0)
        self.registry.update_state(conn_id, new_state, pairs_count)
        return {"req_id": req.get("req_id", ""), "ok": True}

    def _handle_fleet_status(self, req: dict) -> dict:
        budget_stats: dict[str, dict] = {}
        for exchange, bucket in self.budgets.items():
            budget_stats[exchange] = {
                "tokens_available": round(bucket.tokens, 1),
                "tokens_max": bucket.burst,
                "backoff_active": bucket.backoff_active,
                "shed_count": bucket.shed_count,
                "backoff_count": bucket.backoff_count,
            }
        return {
            "req_id": req.get("req_id", ""),
            "ok": True,
            "daemon": {
                "uptime_s": round(self.stats.uptime_s(), 1),
                "socket_path": self.socket_path,
                "active_connections": self.stats.active_clients,
                "total_series": len(self.store.all()),
                "total_events": len(self.event_log._events),
            },
            "bots": self.registry.get_fleet_status(),
            "rate_limiters": budget_stats,
            "recent_events_count": self.event_log.recent_counts(),
        }

    def _handle_fleet_events(self, req: dict) -> dict:
        since_ts = req.get("since_ts", 0)
        event_types = req.get("event_types")
        bot_id = req.get("bot_id")
        limit = req.get("limit", 100)
        return {
            "req_id": req.get("req_id", ""),
            "ok": True,
            "events": self.event_log.query(since_ts, event_types, bot_id, limit),
        }

    # --------- dispatch

    async def _dispatch(self, req: dict, conn_id: int = 0) -> dict:
        op = req.get("op", "fetch")
        self.registry.heartbeat(conn_id)
        if op == "ping":
            return {
                "req_id": req.get("req_id", ""),
                "ok": True,
                "daemon_version": PROTOCOL_VERSION,
                "uptime_s": self.stats.uptime_s(),
            }
        if op == "register":
            return self._handle_register(req, conn_id)
        if op == "unregister":
            return self._handle_unregister(req, conn_id)
        if op == "state_update":
            return self._handle_state_update(req, conn_id)
        if op == "fleet_status":
            return self._handle_fleet_status(req)
        if op == "fleet_events":
            return self._handle_fleet_events(req)
        if op == "stats":
            budget_stats = self._collect_budget_stats()
            return {
                "req_id": req.get("req_id", ""),
                "ok": True,
                "uptime_s": self.stats.uptime_s(),
                "active_clients": self.stats.active_clients,
                "requests_total": self.stats.requests_total,
                "cache_hits": self.stats.cache_hits,
                "cache_partial": self.stats.cache_partial,
                "cache_misses": self.stats.cache_misses,
                "fetch_errors": self.stats.fetch_errors,
                "series_count": len(self.store.all()),
                "inflight_count": self.coordinator.active_count(),
                "pending_fetches": self._pending_fetches,
                "peak_pending": self._peak_pending,
                "acquire_total": self.stats.acquire_total,
                "tickers_requests": self.stats.tickers_requests,
                "tickers_cache_hits": self.stats.tickers_cache_hits,
                "tickers_fetches": self.stats.tickers_fetches,
                "positions_puts": self.stats.positions_puts,
                "positions_gets": self.stats.positions_gets,
                "positions_cache_hits": self.stats.positions_cache_hits,
                "total_connects": self.stats.total_connects,
                "total_disconnects": self.stats.total_disconnects,
                "peak_clients": self.stats.peak_clients,
                "short_lived_connections": self.stats.short_lived_connections,
                **budget_stats,
            }
        if op == "acquire":
            return await self._handle_acquire(req)
        if op == "tickers":
            return await self._handle_tickers(req)
        if op == "positions_put":
            return await self._handle_positions_put(req)
        if op == "positions_get":
            return await self._handle_positions_get(req)
        if op == "balances_put":
            return await self._handle_balances_put(req)
        if op == "balances_get":
            return await self._handle_balances_get(req)
        if op == "markets":
            return await self._handle_markets(req)
        if op == "funding_rates":
            return await self._handle_funding_rates(req)
        if op == "leverage_tiers":
            return await self._handle_leverage_tiers(req)
        if op == "fetch":
            self._pending_fetches += 1
            if self._pending_fetches > self._peak_pending:
                self._peak_pending = self._pending_fetches
            if self._pending_fetches > 10 and self._pending_fetches % 10 == 0:
                logger.info(
                    "fetch queue depth: %d pending (peak=%d, inflight=%d)",
                    self._pending_fetches, self._peak_pending,
                    self.coordinator.active_count(),
                )
            try:
                resp = await self._handle_fetch(req)
                resp["pending_fetches"] = self._pending_fetches
                return resp
            except Exception as e:
                logger.exception("fetch failed: %s", e)
                return {
                    "req_id": req.get("req_id", ""),
                    "ok": False,
                    "pair": req.get("pair", ""),
                    "timeframe": req.get("timeframe", ""),
                    "candle_type": req.get("candle_type", ""),
                    "error_type": e.__class__.__name__,
                    "error_message": str(e),
                    "pending_fetches": self._pending_fetches,
                }
            finally:
                self._pending_fetches -= 1
        return {
            "req_id": req.get("req_id", ""),
            "ok": False,
            "error_type": "UnknownOp",
            "error_message": f"Unknown op: {op}",
        }

    # --------- server loop

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        self.stats.active_clients += 1
        self.stats.total_connects += 1
        if self.stats.active_clients > self.stats.peak_clients:
            self.stats.peak_clients = self.stats.active_clients
        connect_time = time.monotonic()
        conn_id = self._next_conn_id
        self._next_conn_id += 1
        clean_disconnect = False
        peer = writer.get_extra_info("peername") or "unix"
        logger.info(
            "client connected (%s) conn=%d — active=%d total=%d",
            peer, conn_id, self.stats.active_clients, self.stats.total_connects,
        )
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = loads_request(line)
                except Exception as e:
                    logger.warning("bad json from client: %s", e)
                    continue
                if req.get("op") == "unregister":
                    clean_disconnect = True
                resp = await self._dispatch(req, conn_id)
                try:
                    writer.write(dumps(resp))
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError, ConnectionError):
                    break
        except (ConnectionResetError, BrokenPipeError, ConnectionError):
            pass
        finally:
            if not clean_disconnect:
                entry = self.registry.unregister(conn_id, reason="connection_lost")
                if entry:
                    self.event_log.emit(
                        "bot_crash", bot_id=entry.bot_id,
                        reason="connection_lost",
                        uptime_s=round(time.monotonic() - entry.connected_at, 1),
                    )
                    logger.warning(
                        "bot crashed: %s (connection lost after %.0fs)",
                        entry.bot_id, time.monotonic() - entry.connected_at,
                    )
            self.stats.active_clients -= 1
            self.stats.total_disconnects += 1
            self.stats.last_client_disconnect_monotonic = time.monotonic()
            session_s = time.monotonic() - connect_time
            if session_s < 1.0:
                self.stats.short_lived_connections += 1
            elif session_s < 3.0:
                logger.warning(
                    "short-lived connection (%.1fs) — active=%d"
                    " (possible churn: client reconnecting per-request)",
                    session_s, self.stats.active_clients,
                )
            else:
                logger.info(
                    "client disconnected (%.0fs session) — active=%d",
                    session_s, self.stats.active_clients,
                )
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _idle_watchdog(self) -> None:
        while not self._shutdown_event.is_set():
            await asyncio.sleep(5)
            if self.stats.active_clients > 0:
                continue
            if self.stats.last_client_disconnect_monotonic is None:
                idle_s = self.stats.uptime_s()
            else:
                idle_s = time.monotonic() - self.stats.last_client_disconnect_monotonic
            if idle_s >= self._idle_shutdown_s:
                logger.info(
                    "idle for %.0fs (threshold %.0fs) — shutting down",
                    idle_s, self._idle_shutdown_s,
                )
                self._shutdown_event.set()
                return

    async def _periodic_stats(self) -> None:
        stats_interval = float(self.global_cfg.get("stats_interval_s", 60))
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(stats_interval)
                if self._shutdown_event.is_set():
                    break
                self._log_stats_summary()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("periodic stats failed: %s", e)

    def _log_stats_summary(self) -> None:
        s = self.stats
        uptime = s.uptime_s()
        total = s.cache_hits + s.cache_partial + s.cache_misses
        hit_rate = (s.cache_hits / total * 100) if total > 0 else 0.0
        budget_lines = []
        for exchange, bucket in self.budgets.items():
            q_total = len(bucket._waiters)
            budget_lines.append(
                f"{exchange}: tokens={bucket.tokens:.1f}/{bucket.burst} "
                f"backoff={'ACTIVE' if bucket.backoff_active else 'off'} "
                f"queue={q_total} shed={bucket.shed_count} "
                f"429s={bucket.backoff_count} "
                f"escalation={bucket._consecutive_backoffs}"
            )
        budget_str = " | ".join(budget_lines) if budget_lines else "none"
        conn_stats = (
            f" conn={s.total_connects}/{s.total_disconnects} "
            f"peak={s.peak_clients}"
        )
        logger.info(
            "STATS uptime=%.0fs clients=%d ohlcv=%d(%.0f%% hit) "
            "acquire=%d tickers=%d/%d pos=%d/%d pending=%d peak=%d "
            "errors=%d%s | %s",
            uptime, s.active_clients,
            total, hit_rate,
            s.acquire_total,
            s.tickers_cache_hits, s.tickers_requests,
            s.positions_cache_hits, s.positions_gets,
            self._pending_fetches, self._peak_pending,
            s.fetch_errors,
            conn_stats,
            budget_str,
        )

    async def _periodic_flush(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self._flush_interval_s)
                if self._shutdown_event.is_set():
                    break
                if self.persistence:
                    n = self.persistence.flush_dirty()
                    if n:
                        logger.debug("flushed %d dirty series", n)
                self.event_log.flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("periodic flush failed: %s", e)

    async def serve(self) -> None:
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self.socket_path,
            limit=16 * 1024 * 1024,
        )
        os.chmod(self.socket_path, 0o600)
        logger.info(
            "daemon listening on %s (pid=%d, proto=%d)",
            self.socket_path, os.getpid(), PROTOCOL_VERSION,
        )
        self.event_log.emit("daemon_start", pid=os.getpid(),
                            socket_path=self.socket_path)

        if self.persistence:
            try:
                loaded = self.persistence.load_all()
                if loaded:
                    logger.info("loaded %d series from persistence", loaded)
            except Exception as e:
                logger.warning("persistence load failed: %s", e)

        watchdog_task = asyncio.create_task(self._idle_watchdog())
        flush_task = asyncio.create_task(self._periodic_flush())
        stats_task = asyncio.create_task(self._periodic_stats())

        try:
            await self._shutdown_event.wait()
        finally:
            watchdog_task.cancel()
            flush_task.cancel()
            stats_task.cancel()
            self._server.close()
            await self._server.wait_closed()
            for f in list(self.fetchers.values()):
                await f.close()
            if self.persistence:
                try:
                    n = self.persistence.flush_dirty()
                    logger.info("final flush: %d series written", n)
                except Exception as e:
                    logger.warning("final flush failed: %s", e)
            self.event_log.emit("daemon_stop",
                                uptime_s=round(self.stats.uptime_s(), 1))
            self.event_log.flush()
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
            logger.info("daemon stopped cleanly")

    def request_shutdown(self) -> None:
        self._shutdown_event.set()


# -------------------------------------------------------------------- main


def _acquire_pid_lock(socket_path: str) -> int | None:
    """Acquire an exclusive PID lock. Returns the fd if acquired, None if another
    daemon is already running (in which case this process should exit silently)."""
    pid_path = socket_path + ".pid"
    fd = os.open(pid_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    return fd


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=False)
    parser.add_argument("--config", required=False,
                        help="JSON string with global+exchanges overrides")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    raw_cfg: dict = {}
    if args.config:
        try:
            raw_cfg = json.loads(args.config)
        except Exception as e:
            print(f"bad --config JSON: {e}", file=sys.stderr)
            return 2

    global_cfg = resolve_global_config(raw_cfg.get("global") if raw_cfg else None)
    if args.socket:
        global_cfg["socket_path"] = args.socket

    pid_lock_fd = _acquire_pid_lock(global_cfg["socket_path"])
    if pid_lock_fd is None:
        print("another ftcache daemon is already running — exiting", file=sys.stderr)
        return 0

    setup_daemon_logger(global_cfg.get("log_path"), level=args.log_level)
    logger.info(
        "starting ftcache daemon — socket=%s log=%s persistence=%s",
        global_cfg["socket_path"], global_cfg.get("log_path"),
        global_cfg.get("persistence_path"),
    )

    exchange_overrides = raw_cfg.get("exchanges") if raw_cfg else None
    daemon = Daemon(
        socket_path=global_cfg["socket_path"],
        global_cfg=global_cfg,
        exchange_overrides=exchange_overrides,
    )

    def _sig_handler(*_):
        logger.info("signal received — requesting shutdown")
        daemon.request_shutdown()

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    try:
        asyncio.run(daemon.serve())
    except Exception as e:
        logger.exception("daemon crashed: %s", e)
        return 1
    finally:
        try:
            fcntl.flock(pid_lock_fd, fcntl.LOCK_UN)
            os.close(pid_lock_fd)
            os.unlink(global_cfg["socket_path"] + ".pid")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
