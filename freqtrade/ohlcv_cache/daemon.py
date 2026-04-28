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
import heapq
import json
import logging
import os
import signal
import sys
import time
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


class TokenBucket:
    """Async token-bucket rate limiter with priority queue and adaptive back-off.

    Priority levels (lower = higher priority):
        0 = CRITICAL  (pairs with open positions)
        1 = HIGH      (live candle fetches from live bots)
        2 = NORMAL    (historic warmup fetches)
        3 = LOW       (dry_run bots)

    Within the same priority level, requests from bots with more capital
    are served first (via the ``-capital`` trick in the heap tuple).
    """

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3

    def __init__(self, rate_per_s: float, burst: float) -> None:
        self.rate_per_s = rate_per_s
        self.burst = burst
        self.tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._backoff_until = 0.0
        self._backoff_factor = 1.0
        # Priority queue: (priority, -capital, counter, cost, future)
        self._waiters: list[tuple[int, float, int, float, asyncio.Future]] = []
        self._counter = 0  # monotonic tiebreaker for heap stability
        self._drain_task: asyncio.Task | None = None

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        effective_rate = self.rate_per_s / self._backoff_factor
        self.tokens = min(self.burst, self.tokens + elapsed * effective_rate)

    async def acquire(
        self, cost: float = 1.0, priority: int = 2, capital: float = 0.0,
    ) -> None:
        async with self._lock:
            self._refill()
            # Fast path: no waiters queued and enough tokens available
            if not self._waiters and self.tokens >= cost:
                self.tokens -= cost
                return
        # Slow path: register in the priority queue and wait for the
        # drain loop to grant us a token.
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

    async def _drain_loop(self) -> None:
        min_spacing = 1.0 / self.rate_per_s
        while self._waiters:
            async with self._lock:
                now = time.monotonic()
                if now < self._backoff_until:
                    wait = self._backoff_until - now
                else:
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
                            await asyncio.sleep(min_spacing)
                            continue
                    # Not enough tokens — compute how long to wait
                    effective_rate = self.rate_per_s / self._backoff_factor
                    needed = (self._waiters[0][3] - self.tokens) if self._waiters else 1.0
                    wait = needed / max(effective_rate, 0.001)
            await asyncio.sleep(wait)

    def trigger_backoff(self, duration_s: float = 60.0, factor: float = 2.0) -> None:
        self._backoff_factor = max(self._backoff_factor, factor)
        self._backoff_until = time.monotonic() + duration_s
        logger.warning(
            "rate-limit back-off triggered: factor=%.1fx for %.0fs",
            self._backoff_factor, duration_s,
        )

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

    def __init__(self, exchange: str, trading_mode: str, budget: TokenBucket) -> None:
        self.exchange = exchange
        self.trading_mode = trading_mode
        self.budget = budget
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            import ccxt.async_support as ccxt_async  # noqa: PLC0415
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
            data = await client.fetch_ohlcv(
                pair, timeframe=timeframe, since=since_ms,
                limit=limit, params=params,
            )
            return data
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RateLimit" in e.__class__.__name__:
                self.budget.trigger_backoff(60.0, 2.0)
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
        self._server: asyncio.base_events.Server | None = None
        self._shutdown_event = asyncio.Event()
        self._idle_shutdown_s = float(global_cfg.get("idle_daemon_shutdown_s", 600))
        self._max_candles_per_series = int(global_cfg.get("max_candles_per_series", 5000))
        self._flush_interval_s = float(global_cfg.get("flush_interval_s", 30))
        self._pending_fetches = 0
        self._peak_pending = 0
        # Shared caches for centralized rate limiting
        self._tickers_cache: dict[str, _TickersCacheEntry] = {}
        self._tickers_ttl_s = float(global_cfg.get("tickers_cache_ttl_s", 5.0))
        self._tickers_inflight: dict[str, asyncio.Event] = {}
        self._positions_cache: dict[str, _PositionsCacheEntry] = {}
        self._positions_ttl_s = float(global_cfg.get("positions_cache_ttl_s", 3.0))

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
                "backoff_active": bucket._backoff_factor > 1.0,
                "backoff_factor": round(bucket._backoff_factor, 2),
                "backoff_remaining_s": round(backoff_remaining, 1),
                "queue_depths": q_depths,
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
            f = ExchangeFetcher(exchange, trading_mode, self._get_budget(exchange))
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
        """
        exchange = req.get("exchange", "hyperliquid")
        priority = int(req.get("priority", TokenBucket.NORMAL))
        capital = float(req.get("capital", 0.0))
        cost = float(req.get("cost", 1.0))
        budget = self._get_budget(exchange)
        self.stats.acquire_total += 1
        await budget.acquire(cost, priority=priority, capital=capital)
        return {"req_id": req.get("req_id", ""), "ok": True}

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
            data = await client.fetch_tickers(params=params)
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
                budget.trigger_backoff(60.0, 2.0)
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
        return {"req_id": req.get("req_id", ""), "ok": True}

    async def _handle_positions_get(self, req: dict) -> dict:
        """Bot reads cached positions. Returns miss if stale or absent."""
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
        return {
            "req_id": req.get("req_id", ""), "ok": True,
            "hit": False, "data": [],
        }

    # --------- dispatch

    async def _dispatch(self, req: dict) -> dict:
        op = req.get("op", "fetch")
        if op == "ping":
            return {
                "req_id": req.get("req_id", ""),
                "ok": True,
                "daemon_version": PROTOCOL_VERSION,
                "uptime_s": self.stats.uptime_s(),
            }
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
        peer = writer.get_extra_info("peername") or "unix"
        logger.info(
            "client connected (%s) — active=%d", peer, self.stats.active_clients,
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
                resp = await self._dispatch(req)
                try:
                    writer.write(dumps(resp))
                    await writer.drain()
                except ConnectionResetError:
                    break
        finally:
            self.stats.active_clients -= 1
            self.stats.last_client_disconnect_monotonic = time.monotonic()
            logger.info(
                "client disconnected — active=%d", self.stats.active_clients,
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

    async def _periodic_flush(self) -> None:
        if not self.persistence:
            return
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self._flush_interval_s)
                if self._shutdown_event.is_set():
                    break
                n = self.persistence.flush_dirty()
                if n:
                    logger.debug("flushed %d dirty series", n)
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

        if self.persistence:
            try:
                loaded = self.persistence.load_all()
                if loaded:
                    logger.info("loaded %d series from persistence", loaded)
            except Exception as e:
                logger.warning("persistence load failed: %s", e)

        watchdog_task = asyncio.create_task(self._idle_watchdog())
        flush_task = asyncio.create_task(self._periodic_flush())

        try:
            await self._shutdown_event.wait()
        finally:
            watchdog_task.cancel()
            flush_task.cancel()
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
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
            logger.info("daemon stopped cleanly")

    def request_shutdown(self) -> None:
        self._shutdown_event.set()


# -------------------------------------------------------------------- main


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
