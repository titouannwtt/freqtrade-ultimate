"""
CachedExchangeMixin: intercepts Exchange methods to route through the
ftcache daemon for centralized rate limiting and shared caching.

Intercepts:
  - _async_get_candle_history → OHLCV via daemon (already rate-limited)
  - get_tickers → shared tickers cache (one fetch for all bots)
  - fetch_positions → shared positions cache (push/pull)
  - create_order, cancel_order, fetch_order, fetch_balance → rate token
    acquisition before calling ccxt
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from typing import TYPE_CHECKING, Any

from freqtrade.enums import CandleType, MarginMode, TradingMode
from freqtrade.exceptions import DDosProtection, TemporaryError
from freqtrade.ohlcv_cache.client import (
    CacheRateLimited,
    CacheTimedOut,
    CacheUnavailable,
    OhlcvCacheClient,
)


if TYPE_CHECKING:
    from datetime import datetime

    from ccxt.base.types import FundingRate, OrderBook

    from freqtrade.exchange.exchange_types import (
        CcxtBalances,
        CcxtOrder,
        CcxtPosition,
        OHLCVResponse,
        Ticker,
        Tickers,
    )


logger = logging.getLogger("ftcache.client")


class _LocalRateLimiter:
    """Simple per-bot rate limiter used when the daemon is unavailable.

    When the centralized daemon crashes, bots must not flood the exchange
    with unmetered requests.  This provides a conservative local throttle
    based on a sliding-window weight counter.

    Design choices:
    - Assumes ``assumed_bots`` processes share the exchange budget
      → each bot gets ``exchange_budget / assumed_bots`` weight/min
    - CRITICAL priority (orders) always passes immediately — we never
      block order placement
    - Non-CRITICAL requests sleep until budget is available
    - Thread-safe via a simple lock (no asyncio needed)
    """

    def __init__(
        self,
        exchange_budget_per_min: float = 1200.0,
        assumed_bots: int = 6,
    ):
        self._budget = exchange_budget_per_min / assumed_bots
        self._window: list[tuple[float, float]] = []  # (timestamp, cost)
        self._lock = threading.Lock()

    def __getstate__(self):
        state = self.__dict__.copy()
        del state["_lock"]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = threading.Lock()

    def _purge(self, now: float) -> float:
        """Remove entries older than 60s, return current weight used."""
        cutoff = now - 60.0
        self._window = [(ts, c) for ts, c in self._window if ts > cutoff]
        return sum(c for _, c in self._window)

    def acquire(self, cost: float = 1.0, priority: int | None = None) -> None:
        """Block until weight budget allows ``cost``.

        CRITICAL priority (0) is never blocked — orders must always go through.
        """
        if priority is not None and priority <= OhlcvCacheClient.CRITICAL:
            with self._lock:
                now = time.monotonic()
                self._purge(now)
                self._window.append((now, cost))
            return

        while True:
            with self._lock:
                now = time.monotonic()
                used = self._purge(now)
                if used + cost <= self._budget:
                    self._window.append((now, cost))
                    return
                # Calculate sleep time: wait for oldest entry to expire
                if self._window:
                    sleep_until = self._window[0][0] + 60.0
                    wait = max(sleep_until - now, 0.5)
                else:
                    wait = 1.0
            logger.debug(
                "local rate limiter: budget %.0f/%.0f — sleeping %.1fs (cost=%.0f)",
                used,
                self._budget,
                wait,
                cost,
            )
            time.sleep(min(wait, 5.0))  # cap individual sleeps at 5s


class CachedExchangeMixin:
    """Mixin intended to sit before Exchange in the MRO.

    Routes API calls through the ftcache daemon for centralized rate
    limiting and shared caching across all bots.
    """

    _ftcache_client: Any = None  # OhlcvCacheClient | False sentinel
    _ftcache_warned: bool = False
    _ftcache_open_pairs: frozenset[str] = frozenset()
    _ftcache_init_complete: bool = False
    _ftcache_pending_identity: dict | None = None
    _ftcache_is_offline_mode: bool = False  # True for BACKTEST/HYPEROPT/WALKFORWARD
    _ftcache_is_utility_mode: bool = False  # True for UTIL_EXCHANGE
    _ftcache_is_dry_run_mode: bool = False  # True for DRY_RUN (floored to LOW)
    _ftcache_rate_limit_only: bool = False  # True when daemon is used only for rate limiting

    _ACQUIRE_TIMEOUT_S: float = 120.0
    _STALE_POSITIONS_WARN_AGE_S: float = 120.0
    _STALE_POSITIONS_MAX_AGE_S: float = 45.0  # force CRITICAL fetch if older
    _STALE_TICKERS_MAX_AGE_S: float = 60.0  # force CRITICAL fetch if older

    # Local fallback caches for rate-limited scenarios
    _ftcache_last_positions: list | None = None
    _ftcache_last_positions_ts: float = 0.0
    # Monotonic timestamp of the *fetch* that produced _ftcache_last_positions
    # (captured before the network call). Used to reject out-of-order writes so a
    # slow refresh can never overwrite fresher data with staler data. See
    # docs/dev/positions_refresher_plan_v2.md (invariant I2).
    _pos_last_fetched_at: float = 0.0
    # Wall-clock (epoch seconds) of the same snapshot. The monotonic stamps above
    # are perfect for measuring age but cannot be compared against a trade's fill
    # timestamp, which is wall-clock. Deciding "this position no longer exists"
    # requires exactly that comparison: a snapshot taken BEFORE our fill landed
    # legitimately shows no position, and concluding an external close from it
    # strands the real position on-chain as an orphan. See
    # `positions_snapshot_wall_ts()` and FreqtradeBot._positions_view_covers_fill().
    _ftcache_last_positions_wall: float = 0.0

    # --- Phase 2: mixin-side positions refresher (dormant until started in phase 3) ---
    # docs/dev/positions_refresher_plan_v2.md. All references are guarded by
    # _pos_refresher_active (False here) so this code is inert until the lifecycle
    # wiring (phase 3) creates the thread/lock/event and flips the flag.
    _pos_refresher_active: bool = False
    _pos_source: str = "signed"  # "hl_public" | "signed" (telemetry/creds hint)
    _pos_consecutive_fail: int = 0
    _pos_interval: float = 10.0
    _pos_jitter_pct: float = 0.3
    _pos_backoff_max: float = 120.0
    _pos_soft_stale: float = 45.0
    _pos_hard_stale: float = 90.0
    _pos_report_to_daemon: bool = True
    _pos_stop: Any = None  # threading.Event (created at start)
    _pos_force_event: Any = None  # threading.Event (created at start)
    _pos_lock: Any = None  # threading.Lock (created at start)
    _pos_thread: Any = None  # threading.Thread (created at start)
    _pos_fetcher_api: Any = None  # dedicated ccxt client for the refresh thread
    _ftcache_tickers_fresh_ts: float = 0.0
    _ftcache_last_balances: dict | None = None
    _ftcache_last_backoff_active: bool = False
    _ftcache_last_backoff_ts: float = 0.0
    _BACKOFF_CCXT_BLOCK_S: float = 30.0

    # Local fallback rate limiter (when daemon is unavailable)
    _ftcache_local_limiter: _LocalRateLimiter | None = None
    _LOCAL_LIMITER_ASSUMED_BOTS: int = 6

    def _ftcache_get_local_limiter(self) -> _LocalRateLimiter:
        """Lazy-init the local fallback rate limiter."""
        if self._ftcache_local_limiter is None:
            from freqtrade.ohlcv_cache.defaults import EXCHANGE_DEFAULTS

            exchange_id = getattr(self, "id", "unknown")
            cfg = EXCHANGE_DEFAULTS.get(exchange_id, {})
            budget = cfg.get("weight_budget_per_min", 1200.0)
            # Use the real exchange budget (not the 85% daemon budget)
            self._ftcache_local_limiter = _LocalRateLimiter(
                exchange_budget_per_min=budget,
                assumed_bots=self._LOCAL_LIMITER_ASSUMED_BOTS,
            )
            logger.warning(
                "local fallback rate limiter activated: %.0f weight/min budget "
                "(assuming %d concurrent bots, exchange=%s)",
                budget / self._LOCAL_LIMITER_ASSUMED_BOTS,
                self._LOCAL_LIMITER_ASSUMED_BOTS,
                exchange_id,
            )
        return self._ftcache_local_limiter

    def _ftcache_should_block_ccxt(self) -> bool:
        """Return True when daemon backoff is active — direct ccxt calls must be blocked.

        During backoff, falling through to ccxt causes uncontrolled API pressure
        (retrier does 4 retries per call × N bots = 429 snowball).
        """
        if not self._ftcache_last_backoff_active:
            return False
        age = time.monotonic() - self._ftcache_last_backoff_ts
        if age > self._BACKOFF_CCXT_BLOCK_S:
            self._ftcache_last_backoff_active = False
            return False
        return True

    def ftcache_set_open_pairs(self, pairs: set[str] | frozenset[str]) -> None:
        """Inform the cache layer which pairs currently have open positions.

        These pairs will be fetched at CRITICAL priority so exit decisions
        use the freshest possible data.
        """
        self._ftcache_open_pairs = frozenset(pairs)

    def ftcache_mark_init_complete(self) -> None:
        """Signal that the bot has completed initialization.

        After this call, rate-limited calls use their normal priorities
        instead of being escalated to CRITICAL.
        """
        if not self._ftcache_init_complete:
            self._ftcache_init_complete = True
            logger.info("bot init complete — switching to normal rate-limit priorities")

    @property
    def _ftcache_is_deprioritized(self) -> bool:
        """True for any mode that should yield to live bots."""
        return (
            self._ftcache_is_offline_mode
            or self._ftcache_is_utility_mode
            or self._ftcache_is_dry_run_mode
        )

    def _ftcache_init_priority(self, requested: int | None) -> int | None:
        """During init phase, escalate essential calls to CRITICAL.

        For offline/utility/dry-run modes, init escalation is capped at LOW
        — these processes must never starve live bots even during their
        own startup.
        """
        if self._ftcache_init_complete:
            return self._ftcache_apply_priority_floor(requested)
        # Deprioritized modes: init is still LOW priority (no starving live bots)
        if self._ftcache_is_deprioritized:
            return OhlcvCacheClient.LOW
        return OhlcvCacheClient.CRITICAL

    def _ftcache_apply_priority_floor(self, requested: int | None) -> int | None:
        """Downgrade priority for non-live modes.

        Backtests, hyperopts, utility commands, and dry-run bots are never
        more urgent than live bots.  Their requests are capped at LOW so
        they yield to all live trading traffic.
        """
        if not self._ftcache_is_deprioritized:
            return requested
        floor = OhlcvCacheClient.LOW
        if requested is None:
            return floor
        return max(requested, floor)  # higher number = lower priority

    _ftcache_last_wait_log_ts: float = 0.0
    _WAIT_LOG_INTERVAL_S: float = 10.0  # don't spam, log every 10s
    _OFFLINE_ACQUIRE_MAX_S: float = 600.0  # 10 min max wait for offline modes
    _OFFLINE_RETRY_INTERVAL_S: float = 5.0  # retry every 5s

    def _ftcache_enabled(self) -> bool:
        from freqtrade.enums import RunMode

        runmode = self._config.get("runmode", RunMode.OTHER)  # type: ignore[attr-defined]
        # All modes that create an Exchange go through the daemon for
        # centralized rate limiting.  Offline modes (backtest/hyperopt)
        # only use the daemon for rate-token acquisition, not for caching.
        if runmode in (RunMode.BACKTEST, RunMode.HYPEROPT, RunMode.WALKFORWARD):
            self._ftcache_is_offline_mode = True
            self._ftcache_rate_limit_only = True
        elif runmode == RunMode.UTIL_EXCHANGE:
            self._ftcache_is_utility_mode = True
        elif runmode == RunMode.DRY_RUN:
            self._ftcache_is_dry_run_mode = True
        cfg = self._config.get("shared_ohlcv_cache")  # type: ignore[attr-defined]
        if cfg is None:
            return True  # default ON in this fork
        return bool(cfg.get("enabled", True))

    def _ftcache_maybe_init(self) -> None:
        if not hasattr(self, "_ftcache_stats"):
            self._ftcache_stats = {
                "rate_limited": 0,
                "fallback_ccxt": 0,
                "stale_tickers": 0,
                "stale_positions": 0,
                "acquire_timeout": 0,
                "acquire_skip_loop": 0,
            }
        if self._ftcache_client is not None:
            return
        if not self._ftcache_enabled():
            self._ftcache_client = False
            return
        try:
            trading_mode_val = (
                str(self.trading_mode.value)  # type: ignore[attr-defined]
                if getattr(self, "trading_mode", None) is not None
                else "spot"
            )
            self._ftcache_client = OhlcvCacheClient.get_or_spawn(
                exchange_id=self.id,  # type: ignore[attr-defined]
                trading_mode=trading_mode_val,
                bot_config=self._config,  # type: ignore[attr-defined]
            )
            if self._ftcache_pending_identity:
                self._ftcache_client.set_bot_identity(self._ftcache_pending_identity)
                self._ftcache_pending_identity = None
            logger.info(
                "cache client ready for %s/%s",
                self.id,  # type: ignore[attr-defined]
                trading_mode_val,
            )
            self._ftcache_disable_ccxt_ratelimit()
        except Exception as e:
            if not self._ftcache_warned:
                logger.warning(
                    "could not initialise cache client (%s) — falling back to "
                    "direct ccxt for this bot",
                    e,
                )
                self._ftcache_warned = True
            self._ftcache_client = False
        # Start the mixin-side positions refresher (no-op unless live +
        # positions_refresh_enabled). Kept outside the try so a daemon-client
        # failure above doesn't prevent the (daemon-independent) refresher.
        try:
            self._ftcache_maybe_start_positions_refresher()
        except Exception as e:
            logger.warning("[positions-refresh] start failed: %r", e)

    def _ftcache_disable_ccxt_ratelimit(self) -> None:
        """Reduce ccxt's built-in rate limiter when daemon is active.

        The daemon handles rate limiting for mixin-controlled calls (OHLCV,
        tickers, positions, balances).  But non-mixin calls (fetch_order,
        create_order, fetch_l2_order_book) still go through ccxt directly
        and need a throttle to avoid exhausting the shared rate limit budget.

        We reduce from the config value (often 1000ms) to 200ms — fast enough
        to not slow down order management, slow enough to prevent 15 bots from
        flooding the API with unmetered calls.
        """
        target_rate_ms = 200
        for api in (
            getattr(self, "_api", None),
            getattr(self, "_api_async", None),
            getattr(self, "_ws_async", None),
        ):
            if api is None:
                continue
            old_rate = getattr(api, "rateLimit", 0)
            if old_rate > target_rate_ms:
                api.rateLimit = target_rate_ms
                logger.info(
                    "reduced ccxt rateLimit on %s from %dms to %dms"
                    " (daemon handles mixin calls, ccxt throttles the rest)",
                    type(api).__name__,
                    old_rate,
                    target_rate_ms,
                )

    def _ftcache_warn_deprecated_config(self) -> None:
        """Inform the user that ccxt-level rate-limit knobs are now managed
        by the daemon."""
        if not self._ftcache_enabled():
            return
        exchange_conf = self._config.get("exchange") or {}  # type: ignore[attr-defined]
        ccxt_cfg = exchange_conf.get("ccxt_config") or {}
        if "rateLimit" in ccxt_cfg or ccxt_cfg.get("enableRateLimit") is True:
            logger.warning(
                "`exchange.ccxt_config.rateLimit` / `enableRateLimit` are "
                "ignored while the shared OHLCV cache is active. Rate "
                "limiting is centralised in the ftcache daemon across all "
                "bots. You can remove these keys, or opt out with "
                "`shared_ohlcv_cache.enabled: false`."
            )

    def _ftcache_get_client(self) -> OhlcvCacheClient | None:
        """Return the cache client if available, or None."""
        self._ftcache_maybe_init()
        if not self._ftcache_client:
            return None
        return self._ftcache_client  # type: ignore[return-value]

    def _ftcache_bump(self, key: str) -> None:
        if hasattr(self, "_ftcache_stats"):
            self._ftcache_stats[key] = self._ftcache_stats.get(key, 0) + 1

    def _ftcache_record_cached(
        self,
        method: str,
        pair: str | None = None,
        latency_ms: float = 0.0,
    ) -> None:
        metrics = getattr(self, "_metrics", None)
        if metrics is None:
            return
        try:
            from freqtrade.exchange.exchange_metrics import ApiCall

            metrics.record(
                ApiCall(
                    ts=time.time(),
                    method=method,
                    exchange=getattr(self, "name", "unknown"),
                    latency_ms=latency_ms,
                    cached=True,
                    success=True,
                    error_type=None,
                    pair=pair,
                )
            )
        except Exception:  # noqa: S110
            pass

    def ftcache_get_stats(self) -> dict:
        """Return diagnostic counters for the cache layer."""
        stats = dict(getattr(self, "_ftcache_stats", {}))
        if self._pos_refresher_active:
            stats["positions_refresher_active"] = True
            stats["positions_source"] = self._pos_source
            stats["positions_cache_age_s"] = round(
                time.monotonic() - (self._ftcache_last_positions_ts or 0), 1
            )
            stats["positions_consecutive_fail"] = self._pos_consecutive_fail
            stats["positions_thread_alive"] = bool(
                self._pos_thread is not None and self._pos_thread.is_alive()
            )
        return stats

    def _ftcache_save_positions(self, positions: list, *, fetched_at: float | None = None) -> None:
        """Store the latest positions, rejecting out-of-order writes.

        ``fetched_at`` is the monotonic time captured *before* the fetch that
        produced ``positions``. When the background refresher and a fallback
        direct fetch run concurrently, a slow fetch started earlier could return
        after a newer one; without this guard it would clobber fresher data with
        staler data. Callers that don't pass ``fetched_at`` (in-order legacy
        paths) get ``now`` and always win, preserving current behaviour.
        (Not yet lock-protected — the refresher thread lands in a later phase
        and will add the lock; today all callers run on the worker thread.)
        """
        fa = fetched_at if fetched_at is not None else time.monotonic()
        if fa <= self._pos_last_fetched_at:
            logger.debug(
                "positions write ignored — out of order (fetched_at=%.3f <= last=%.3f)",
                fa,
                self._pos_last_fetched_at,
            )
            return
        self._ftcache_last_positions = positions
        self._ftcache_last_positions_ts = time.monotonic()
        self._ftcache_last_positions_wall = time.time()
        self._pos_last_fetched_at = fa

    def positions_snapshot_wall_ts(self) -> float:
        """Wall-clock epoch of the newest positions snapshot (0.0 if none yet).

        Callers that must decide whether a position is *gone* (rather than merely
        unknown) compare this against the trade's last fill: a snapshot older than
        the fill simply predates the position and proves nothing.
        """
        return self._ftcache_last_positions_wall

    def ftcache_report_iso_breach(
        self,
        *,
        pair: str,
        expected: float,
        observed: float,
        delta: float,
        phase: str,
        bot: str = "",
    ) -> None:
        """Forward a position ISO breach to the daemon. Never raises."""
        try:
            client = self._ftcache_get_client()
            if client is None:
                return
            self._ftcache_run_on_loop(
                client.report_iso_breach(
                    pair=pair,
                    expected=expected,
                    observed=observed,
                    delta=delta,
                    phase=phase,
                    bot=bot,
                )
            )
        except Exception as exc:
            logger.debug("could not report ISO breach to daemon (%s)", exc)

    def fetch_positions_authoritative(self, pair: str | None = None) -> list:
        """Fetch positions straight from the exchange, bypassing every cache layer.

        Reserved for decisions that cannot tolerate a stale read — chiefly "is this
        position really gone?". Costs one rate token at CRITICAL priority, so it must
        stay on cold paths, never in the per-cycle loop. The result is written back
        into the shared snapshot so the rest of the cycle benefits from it.
        """
        self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL, cost=2.0)
        positions = super().fetch_positions(pair=pair, params=None)  # type: ignore[misc]
        if pair is None:
            self._ftcache_save_positions(positions)
        else:
            # A per-pair read cannot replace the fleet-wide snapshot, but it is
            # still the freshest truth we have for that pair: stamp it so callers
            # can prove the read post-dates their fill.
            self._ftcache_last_positions_wall = time.time()
        return positions

    def _ftcache_get_stale_positions(self, *, reject_if_too_old: bool = True) -> list | None:
        if self._ftcache_last_positions is None:
            return None
        age = time.monotonic() - self._ftcache_last_positions_ts
        # If we have open positions and data is too old, reject it
        # so the caller can force a CRITICAL fetch
        if reject_if_too_old and self._ftcache_open_pairs and age > self._STALE_POSITIONS_MAX_AGE_S:
            logger.warning(
                "positions stale (%.0fs > %.0fs max) with %d open pairs "
                "— rejecting stale data, forcing fresh fetch",
                age,
                self._STALE_POSITIONS_MAX_AGE_S,
                len(self._ftcache_open_pairs),
            )
            return None
        if age > self._STALE_POSITIONS_WARN_AGE_S:
            logger.warning(
                "positions rate-limited — using %.0fs-old local cache"
                " (data may be outdated, NOT falling back to ccxt)",
                age,
            )
        else:
            logger.info(
                "positions rate-limited — using %.0fs-old local cache",
                age,
            )
        return self._ftcache_last_positions

    # ---------------------------------------------------------------- positions refresher (phase 2)

    def _resolve_positions_source(self) -> str:
        """HL live futures can read positions from the *public* clearinghouseState
        /info endpoint (ccxt.hyperliquid.fetch_positions uses handle_public_address
        + publicPostInfo — no signing, address only). Everything else is signed."""
        if (
            getattr(self, "id", None) == "hyperliquid"  # ccxt id (lowercase), not self.name
            and self.trading_mode == TradingMode.FUTURES
            and not self._config.get("dry_run", True)
            and getattr(self._api, "walletAddress", None)
        ):
            return "hl_public"
        return "signed"

    def _positions_refresher_fetcher(self) -> Any:
        """Dedicated ccxt client for the background refresh thread.

        Kept separate from ``self._api`` because ccxt sync clients are NOT
        thread-safe: the refresher thread must never share session state with the
        worker thread. HL gets only the public wallet address (public /info path);
        other exchanges get full credentials (signed fetch_positions).
        """
        if self._pos_fetcher_api is not None:
            return self._pos_fetcher_api
        import ccxt

        name = self.id  # ccxt module attribute is the lowercase id (self.name is the display name)
        cfg: dict[str, Any] = {"enableRateLimit": True}
        opts = getattr(self._api, "options", {}) or {}
        if self.trading_mode == TradingMode.FUTURES and opts.get("defaultType"):
            cfg["options"] = {"defaultType": opts["defaultType"]}
        if self._pos_source == "hl_public":
            cfg["walletAddress"] = getattr(self._api, "walletAddress", None)
        else:
            for k in ("apiKey", "secret", "password", "walletAddress", "privateKey"):
                v = getattr(self._api, k, None)
                if v:
                    cfg[k] = v
        self._pos_fetcher_api = getattr(ccxt, name)(cfg)
        logger.info(
            "[positions-refresh] dedicated fetcher client created (%s, source=%s)",
            name,
            self._pos_source,
        )
        return self._pos_fetcher_api

    def _positions_daemon_read(self, wallet: str | None) -> tuple[bool, list] | None:
        """Read the daemon's central positions cache (phase 5) from the refresher
        thread WITHOUT touching the shared exchange event loop.

        The refresher runs in its own background thread; driving ``self.loop``
        (the exchange's asyncio loop, owned by the main thread) from here races
        with the main thread's ccxt calls — the reason the earlier
        ``_ftcache_run_on_loop`` bridge silently failed and every bot fell back
        to its own /info. Instead we spin up a dedicated short-lived client on a
        private event loop: fully thread-safe, one unix-socket connect per pass
        (negligible at a 45s cadence). The client is never given a bot identity,
        so ``_auto_register`` no-ops and it does not pollute the fleet roster.

        Returns ``(hit, data)``, or ``None`` if the daemon is unreachable so the
        caller falls back to its own direct fetch.
        """
        client = self._ftcache_get_client()
        sock = getattr(client, "socket_path", None) if client is not None else None
        if not sock:
            return None
        from freqtrade.ohlcv_cache.client import OhlcvCacheClient

        loop = asyncio.new_event_loop()
        tmp = OhlcvCacheClient(
            socket_path=sock,
            exchange_id=getattr(self, "id", ""),
            trading_mode=getattr(client, "trading_mode", "spot"),
        )
        try:
            hit, data, _ = loop.run_until_complete(tmp.get_positions(wallet_address=wallet))
            return hit, data
        finally:
            try:
                loop.run_until_complete(tmp.close())
            except Exception:  # noqa: S110 — best-effort teardown
                pass
            loop.close()

    def _ftcache_refresh_positions_once(self) -> None:
        """One refresh pass: fetch positions via the dedicated client and update
        the local cache (monotonic-guarded). Raises on failure so the loop can
        count consecutive failures for adaptive backoff."""
        fetched_at = time.monotonic()
        # Share the IP-level backoff: the public /info call still hits the same IP
        # as the OHLCV fetches, so back off with the daemon rather than pile on.
        if (
            self._ftcache_last_backoff_active
            and (time.monotonic() - self._ftcache_last_backoff_ts) < self._BACKOFF_CCXT_BLOCK_S
        ):
            logger.debug("[positions-refresh] IP backoff actif — skip ce tour")
            return
        # Phase 5: prefer the daemon's central positions cache (one /info for the
        # whole fleet). Sending our wallet_address also teaches the daemon which
        # wallet to fetch. Fall through to our own /info only when it's a miss
        # (daemon central fetch off/cold/failed) — that's the fallback path.
        wallet = getattr(self._api, "walletAddress", None)
        try:
            res = self._positions_daemon_read(wallet)
        except Exception as e:  # daemon unreachable/slow — fall back to own /info
            res = None
            logger.debug("[positions-refresh] lecture cache daemon échouée: %s", e)
        if res is not None:
            hit, positions = res
            if hit and isinstance(positions, list):
                self._ftcache_save_positions(positions, fetched_at=fetched_at)
                self._ftcache_bump("positions_refresh_daemon")
                logger.debug(
                    "[positions-refresh] servi du cache daemon central (%d positions)",
                    len(positions),
                )
                return
        fetcher = self._positions_refresher_fetcher()
        positions = fetcher.fetch_positions()
        self._ftcache_save_positions(positions, fetched_at=fetched_at)  # monotonic guard (I2)
        self._ftcache_bump("positions_refresh_ok")
        logger.debug(
            "[positions-refresh] %s: %d positions en %.2fs",
            self._pos_source,
            len(positions),
            time.monotonic() - fetched_at,
        )
        if self._pos_report_to_daemon:
            try:
                client = self._ftcache_get_client()
                if client is not None:
                    self._ftcache_run_on_loop(client.push_positions(positions))
            except Exception as e:  # non-blocking: observability only
                logger.debug("[positions-refresh] push daemon échoué (non bloquant): %s", e)

    def _positions_refresh_loop(self) -> None:
        """Background loop: refresh at a jittered cadence, adaptive backoff on
        failure, wakes early on request_positions_refresh(). Never dies silently."""
        logger.info(
            "[positions-refresh] thread démarré (source=%s, interval=%.0fs)",
            self._pos_source,
            self._pos_interval,
        )
        while not self._pos_stop.is_set():
            try:
                self._ftcache_refresh_positions_once()
                self._pos_consecutive_fail = 0
            except DDosProtection as e:
                self._pos_consecutive_fail += 1
                self._ftcache_bump("positions_refresh_429")
                age = time.monotonic() - self._ftcache_last_positions_ts
                logger.warning(
                    "[positions-refresh] 429 (#%d) — cache conservé (age=%.0fs): %s",
                    self._pos_consecutive_fail,
                    age,
                    e,
                )
            except Exception as e:  # the thread must survive anything
                self._pos_consecutive_fail += 1
                self._ftcache_bump("positions_refresh_err")
                age = time.monotonic() - (self._ftcache_last_positions_ts or 0)
                logger.warning(
                    "[positions-refresh] échec (#%d) — cache conservé (age=%.0fs): %r",
                    self._pos_consecutive_fail,
                    age,
                    e,
                )
            base = self._pos_interval
            if self._pos_consecutive_fail:
                base = min(
                    self._pos_interval * (2**self._pos_consecutive_fail),
                    self._pos_backoff_max,
                )
            jitter = base * self._pos_jitter_pct * random.uniform(-1.0, 1.0)  # noqa: S311
            wait = max(1.0, base + jitter)
            self._pos_force_event.wait(timeout=wait)
            self._pos_force_event.clear()
        logger.info("[positions-refresh] thread arrêté")

    def request_positions_refresh(self) -> None:
        """Wake the refresher immediately (event-driven freshness, e.g. after a fill)."""
        ev = self._pos_force_event
        if ev is not None:
            ev.set()

    def _positions_watchdog(self) -> None:
        """Cheap liveness check (call from the worker heartbeat): restart a dead
        refresher, force a refresh if the cache is frozen past the hard threshold."""
        if not self._pos_refresher_active:
            return
        thread = self._pos_thread
        if thread is None or not thread.is_alive():
            logger.error("[positions-refresh] thread MORT — redémarrage")
            self._ftcache_bump("positions_watchdog_restart")
            self._ftcache_start_positions_refresher(restart=True)
            return
        age = time.monotonic() - (self._ftcache_last_positions_ts or 0)
        if age > self._pos_hard_stale:
            logger.error(
                "[positions-refresh] cache figé (age=%.0fs > hard=%.0fs) — refresh forcé",
                age,
                self._pos_hard_stale,
            )
            self.request_positions_refresh()

    def positions_are_trustworthy(self) -> tuple[bool, float]:
        """Circuit-breaker helper (used in phase 4): positions are trustworthy iff
        the cache age is within the hard-stale threshold. Returns (ok, age_s).
        When the refresher is inactive, always trustworthy (legacy behaviour)."""
        if not self._pos_refresher_active:
            return True, 0.0
        age = time.monotonic() - (self._ftcache_last_positions_ts or 0)
        return age <= self._pos_hard_stale, age

    def _positions_serve_from_refresher(self, pair: str | None) -> list | None:
        """Fast path for fetch_positions when the refresher is active: return the
        fresh local cache (filtered by ``pair``), or None to fall through to the
        daemon/ccxt path when the cache is older than the soft-stale threshold."""
        with self._pos_lock:
            cached = self._ftcache_last_positions
            ts = self._ftcache_last_positions_ts
        age = time.monotonic() - (ts or 0)
        if cached is not None and age <= self._pos_soft_stale:
            self._ftcache_bump("positions_served_cache")
            if pair is not None:
                return [p for p in cached if p.get("symbol") == pair]
            return cached
        logger.warning(
            "[positions] cache mixin vieux (age=%.0fs > %.0fs) — chemin de secours",
            age,
            self._pos_soft_stale,
        )
        self._ftcache_bump("positions_fallback_direct")
        return None

    # ---- positions refresher lifecycle (phase 3) ----

    def _ftcache_maybe_start_positions_refresher(self) -> None:
        """Start the refresher iff: live (not dry/offline/util), the shared cache is
        usable, and positions_refresh_enabled is set. Idempotent."""
        if self._pos_refresher_active:
            return
        if getattr(self, "_ftcache_is_offline_mode", False) or getattr(
            self, "_ftcache_is_utility_mode", False
        ):
            return
        if self._config.get("dry_run", True):  # live only (invariant I4)
            return
        try:
            from freqtrade.ohlcv_cache.defaults import resolve_global_config

            gc = resolve_global_config(self._config.get("shared_ohlcv_cache") or {})
        except Exception as e:
            logger.debug("[positions-refresh] config resolve failed: %s", e)
            return
        if not gc.get("positions_refresh_enabled", False):
            return
        self._ftcache_start_positions_refresher(gc)

    def _ftcache_start_positions_refresher(
        self, gc: dict | None = None, *, restart: bool = False
    ) -> None:
        if restart:
            self._ftcache_stop_positions_refresher()
        if gc is None:
            from freqtrade.ohlcv_cache.defaults import resolve_global_config

            gc = resolve_global_config(self._config.get("shared_ohlcv_cache") or {})
        self._pos_source = self._resolve_positions_source()
        self._pos_interval = float(gc.get("positions_refresh_interval_s", 10))
        self._pos_jitter_pct = float(gc.get("positions_refresh_jitter_pct", 0.3))
        self._pos_backoff_max = float(gc.get("positions_refresh_backoff_max_s", 120))
        self._pos_soft_stale = float(gc.get("positions_soft_stale_s", 45))
        self._pos_hard_stale = float(gc.get("positions_hard_stale_s", 90))
        self._pos_report_to_daemon = bool(gc.get("positions_report_to_daemon", True))
        self._pos_lock = threading.Lock()
        self._pos_stop = threading.Event()
        self._pos_force_event = threading.Event()
        self._pos_consecutive_fail = 0
        # Synchronous initial refresh so the cache is never empty on the first
        # process() cycle (a failure here is non-fatal — the fast-path fallback
        # covers it until the thread lands a fresh copy).
        try:
            self._ftcache_refresh_positions_once()
            logger.info("[positions-refresh] refresh initial OK (source=%s)", self._pos_source)
        except Exception as e:
            logger.warning(
                "[positions-refresh] refresh initial échoué (fallback prend le relais): %r", e
            )
        self._pos_thread = threading.Thread(
            target=self._positions_refresh_loop,
            name=f"pos-refresh-{getattr(self, 'id', 'x')}",
            daemon=True,
        )
        self._pos_refresher_active = True
        self._pos_thread.start()
        logger.info(
            "[positions-refresh] démarré (mode=%s, interval=%.0fs, soft=%.0fs, hard=%.0fs)",
            self._pos_source,
            self._pos_interval,
            self._pos_soft_stale,
            self._pos_hard_stale,
        )

    def _ftcache_stop_positions_refresher(self) -> None:
        if not self._pos_refresher_active and self._pos_thread is None:
            return
        self._pos_refresher_active = False
        if self._pos_stop is not None:
            self._pos_stop.set()
        if self._pos_force_event is not None:
            self._pos_force_event.set()
        th = self._pos_thread
        if th is not None and th.is_alive():
            th.join(timeout=5.0)
        self._pos_thread = None
        fetcher = self._pos_fetcher_api
        if fetcher is not None:
            try:
                if hasattr(fetcher, "close"):
                    fetcher.close()
            except Exception:  # noqa: S110
                pass
            self._pos_fetcher_api = None
        logger.info("[positions-refresh] arrêté")

    def close(self):  # override Exchange.close to stop the refresher first
        try:
            self._ftcache_stop_positions_refresher()
        except Exception as e:
            logger.debug("[positions-refresh] stop on close failed: %s", e)
        return super().close()

    _LOOP_LOCK_TIMEOUT_S: float = 5.0

    def _ftcache_run_on_loop(self, coro):
        """Run an async daemon call, serialized with _loop_lock.

        Prevents event loop races between the worker thread
        (refresh_latest_ohlcv) and the Uvicorn API thread.

        Returns (True, result) on success.
        Returns (False, None) if the lock is held beyond timeout.
        Exceptions from the coroutine propagate normally.
        """
        lock = getattr(self, "_loop_lock", None)
        if lock is None:
            coro.close()
            return False, None
        if not lock.acquire(timeout=self._LOOP_LOCK_TIMEOUT_S):
            coro.close()
            return False, None
        try:
            loop = self.loop  # type: ignore[attr-defined]
            client = self._ftcache_client
            if client and client is not False:
                # WFA / repeated Backtesting instances create new event loops.
                # The singleton client's asyncio.Lock and streams are bound to the
                # loop that was active when they were created.  If the exchange's
                # loop changed, we must recreate them to avoid
                # "Future attached to a different loop".
                client_loop = getattr(client, "_bound_loop", None)
                if client_loop is not loop:
                    client._reader = None
                    client._writer = None
                    client._lock = asyncio.Lock()
                    client._registered = False
                    client._bound_loop = loop
            return True, loop.run_until_complete(coro)
        finally:
            lock.release()

    def _ftcache_local_backoff_check(self, priority: int | None) -> bool:
        """Fallback when _loop_lock unavailable — conservative by default.

        Only allows CRITICAL (orders) through. Everything else is shed
        to prevent unmetered direct API calls.
        """
        effective_prio = priority if priority is not None else 2
        if effective_prio <= OhlcvCacheClient.CRITICAL:
            return True
        return False

    def _ftcache_report_429(self, method: str = "", pair: str = "") -> None:
        """Notify daemon that this bot received a 429 on a direct ccxt call.

        The daemon triggers backoff so ALL bots' subsequent requests are
        queued by priority (CRITICAL first) at a reduced rate.
        """
        self._ftcache_last_backoff_active = True
        self._ftcache_last_backoff_ts = time.monotonic()
        client = self._ftcache_get_client()
        if client is None:
            return
        try:
            ok, _ = self._ftcache_run_on_loop(
                client.report_429(method=method, pair=pair),
            )
            if ok:
                logger.info(
                    "reported 429 to daemon (method=%s pair=%s)"
                    " — all bots will queue by priority, blocking ccxt fallback for %.0fs",
                    method,
                    pair,
                    self._BACKOFF_CCXT_BLOCK_S,
                )
        except Exception as e:
            logger.debug("report_429 to daemon failed (non-fatal): %s", e)

    def _ftcache_report_order(
        self,
        pair: str,
        side: str,
        order_type: str,
        amount: float,
        action: str = "create",
        order_id: str = "",
    ) -> None:
        """Fire-and-forget: notify daemon about an order create/cancel."""
        client = self._ftcache_get_client()
        if client is None:
            return
        try:
            self._ftcache_run_on_loop(
                client.report_order(
                    pair=pair,
                    side=side,
                    order_type=order_type,
                    amount=amount,
                    action=action,
                    order_id=order_id,
                ),
            )
        except Exception as e:
            logger.debug("report_order to daemon failed (non-fatal): %s", e)

    def _ftcache_acquire_sync(self, priority: int | None = None, cost: float = 1.0) -> bool:
        """Acquire a rate token synchronously (blocks until granted).

        Called before any non-OHLCV REST call so that ALL API traffic
        from all bots shares the daemon's centralized rate limit.

        For offline/utility modes (backtest, hyperopt, utility commands):
        - Priority is floored to LOW
        - On shed or timeout, retries in a loop (up to 10 min) instead of
          falling through to unmetered ccxt calls
        - Logs informative messages every 10s while waiting
        - After 10 min, raises TemporaryError (clean crash, no silent bypass)

        For live modes: existing behavior (timeout → allow through).

        Returns True when the token was granted (or daemon unavailable in live mode).
        Returns False only when the _loop_lock is unavailable and local
        backoff check rejects the request.
        """
        # Apply priority floor for non-live modes
        priority = self._ftcache_apply_priority_floor(priority)
        is_offline = self._ftcache_is_offline_mode or self._ftcache_is_utility_mode

        client = self._ftcache_get_client()
        if client is None:
            self._ftcache_get_local_limiter().acquire(cost=cost, priority=priority)
            return True

        deadline = time.monotonic() + self._OFFLINE_ACQUIRE_MAX_S if is_offline else 0.0
        attempt = 0

        while True:
            attempt += 1

            # Log when offline mode is waiting
            if is_offline:
                now = time.monotonic()
                if now - self._ftcache_last_wait_log_ts > self._WAIT_LOG_INTERVAL_S:
                    mode = "backtest/hyperopt" if self._ftcache_is_offline_mode else "utility"
                    elapsed = self._OFFLINE_ACQUIRE_MAX_S - (deadline - now)
                    logger.info(
                        "[%s] waiting for rate token (priority=LOW, attempt=%d, "
                        "elapsed=%.0fs/%.0fs) — live bots have priority, please wait",
                        mode,
                        attempt,
                        elapsed,
                        self._OFFLINE_ACQUIRE_MAX_S,
                    )
                    self._ftcache_last_wait_log_ts = now

            try:
                lock = getattr(self, "_loop_lock", None)
                if lock is None or not lock.acquire(timeout=self._LOOP_LOCK_TIMEOUT_S):
                    self._ftcache_bump("acquire_skip_loop")
                    if is_offline:
                        if time.monotonic() > deadline:
                            logger.warning(
                                "rate token acquire failed after %.0fs"
                                " — falling back to local limiter",
                                self._OFFLINE_ACQUIRE_MAX_S,
                            )
                            self._ftcache_get_local_limiter().acquire(
                                cost=cost,
                                priority=priority,
                            )
                            return True
                        time.sleep(self._OFFLINE_RETRY_INTERVAL_S)
                        continue
                    return self._ftcache_local_backoff_check(priority)
                try:
                    self.loop.run_until_complete(  # type: ignore[attr-defined]
                        asyncio.wait_for(
                            client.acquire_rate_token(priority=priority, cost=cost),
                            timeout=min(self._ACQUIRE_TIMEOUT_S, 30.0)
                            if is_offline
                            else self._ACQUIRE_TIMEOUT_S,
                        ),
                    )
                    return True
                finally:
                    lock.release()

            except CacheRateLimited:
                self._ftcache_bump("rate_limited")
                if is_offline:
                    if time.monotonic() > deadline:
                        logger.warning(
                            "rate token shed after %.0fs — falling back to local limiter",
                            self._OFFLINE_ACQUIRE_MAX_S,
                        )
                        self._ftcache_get_local_limiter().acquire(
                            cost=cost,
                            priority=priority,
                        )
                        return True
                    time.sleep(self._OFFLINE_RETRY_INTERVAL_S)
                    continue
                # Live mode: shed = return False (caller decides)
                return False

            except (CacheTimedOut, TimeoutError):
                self._ftcache_bump("acquire_timeout")
                if is_offline:
                    if time.monotonic() > deadline:
                        logger.warning(
                            "rate token acquire timed out after %.0fs"
                            " — falling back to local limiter",
                            self._OFFLINE_ACQUIRE_MAX_S,
                        )
                        self._ftcache_get_local_limiter().acquire(
                            cost=cost,
                            priority=priority,
                        )
                        return True
                    logger.info(
                        "rate token acquire timed out — retrying (offline mode, %.0fs remaining)",
                        deadline - time.monotonic(),
                    )
                    continue
                # Live mode: timeout → local limiter fallback
                logger.info(
                    "rate token acquire timed out after %.0fs — using local limiter "
                    "(priority=%s, cost=%.0f)",
                    self._ACQUIRE_TIMEOUT_S,
                    priority,
                    cost,
                )
                self._ftcache_get_local_limiter().acquire(cost=cost, priority=priority)
                return True

            except CacheUnavailable:
                self._ftcache_bump("acquire_timeout")
                if is_offline:
                    if time.monotonic() > deadline:
                        logger.warning(
                            "daemon unavailable after %.0fs — falling back to local limiter",
                            self._OFFLINE_ACQUIRE_MAX_S,
                        )
                        self._ftcache_get_local_limiter().acquire(
                            cost=cost,
                            priority=priority,
                        )
                        return True
                    time.sleep(self._OFFLINE_RETRY_INTERVAL_S)
                    continue
                self._ftcache_get_local_limiter().acquire(cost=cost, priority=priority)
                return True

            except TemporaryError:
                raise  # don't catch our own TemporaryError

            except Exception as e:
                if is_offline:
                    logger.warning("unexpected error acquiring rate token: %s — retrying", e)
                    if time.monotonic() > deadline:
                        logger.warning(
                            "rate token acquire failed after %.0fs: %s"
                            " — falling back to local limiter",
                            self._OFFLINE_ACQUIRE_MAX_S,
                            e,
                        )
                        self._ftcache_get_local_limiter().acquire(
                            cost=cost,
                            priority=priority,
                        )
                        return True
                    time.sleep(self._OFFLINE_RETRY_INTERVAL_S)
                    continue
                logger.debug("rate token acquire failed (%s), using local limiter", e)
                self._ftcache_get_local_limiter().acquire(cost=cost, priority=priority)
                return True

    def _ftcache_offline_fetch_cost(
        self, timeframe: str, candle_type: CandleType, since_ms: int | None
    ) -> float:
        """Realistic HL weight for an offline (backtest/hyperopt) OHLCV fetch.

        candleSnapshot costs 20 base + 1 weight per 60 candles returned (see
        HL_WEIGHT_MAP in defaults.py).  A fixed cost of 30 under-billed large
        warmup chunks (a 5000-candle fetch really weighs ~104): the bucket
        thought it spent 30 while the exchange counted 104, overran the
        per-minute budget and got the whole fleet 429'd.  Over-billing the
        pipeline is always safe — it only makes backtests/hyperopts slower.
        """
        try:
            limit = int(self.ohlcv_candle_limit(timeframe, candle_type, since_ms))
            if since_ms:
                from freqtrade.exchange.exchange_utils_timeframe import timeframe_to_msecs

                # Incremental fetches (recent gap-fills) return far fewer
                # candles than the exchange cap — bill the expected count.
                expected = int((time.time() * 1000 - since_ms) / timeframe_to_msecs(timeframe))
                limit = max(1, min(limit, expected + 1))
            # Cap below the bucket burst (150) so the request stays grantable.
            return min(20.0 + limit / 60.0, 120.0)
        except Exception:
            return 30.0  # previous conservative default

    # -------------------------------------------------------------------- OHLCV

    _CACHEABLE_CANDLE_TYPES = frozenset(
        {CandleType.SPOT, CandleType.FUTURES, CandleType.MARK, CandleType.FUNDING_RATE}
    )

    async def _async_get_candle_history(
        self,
        pair: str,
        timeframe: str,
        candle_type: CandleType,
        since_ms: int | None = None,
    ) -> OHLCVResponse:
        if candle_type not in self._CACHEABLE_CANDLE_TYPES:
            return await super()._async_get_candle_history(  # type: ignore[misc]
                pair,
                timeframe,
                candle_type,
                since_ms,
            )

        self._ftcache_maybe_init()

        # In offline mode (backtest/hyperopt), don't use daemon's OHLCV cache —
        # data is loaded from local files. But still rate-limit if ccxt is called.
        if self._ftcache_rate_limit_only:
            client = self._ftcache_get_client()
            if client is not None:
                deadline = time.monotonic() + self._OFFLINE_ACQUIRE_MAX_S
                attempt = 0
                fetch_cost = self._ftcache_offline_fetch_cost(timeframe, candle_type, since_ms)
                while True:
                    attempt += 1
                    try:
                        await asyncio.wait_for(
                            client.acquire_rate_token(
                                priority=OhlcvCacheClient.LOW,
                                cost=fetch_cost,
                            ),
                            timeout=30.0,
                        )
                        break  # token granted
                    except (CacheUnavailable, CacheTimedOut, CacheRateLimited, TimeoutError):
                        if time.monotonic() > deadline:
                            logger.warning(
                                "OHLCV rate token for %s failed after %.0fs"
                                " — falling back to local limiter",
                                pair,
                                self._OFFLINE_ACQUIRE_MAX_S,
                            )
                            limiter = self._ftcache_get_local_limiter()
                            await asyncio.to_thread(
                                limiter.acquire,
                                4.0,
                                OhlcvCacheClient.LOW,
                            )
                            break
                        now = time.monotonic()
                        if now - self._ftcache_last_wait_log_ts > self._WAIT_LOG_INTERVAL_S:
                            logger.info(
                                "[backtest/hyperopt] OHLCV rate token for %s: "
                                "attempt %d, %.0fs remaining — live bots have priority",
                                pair,
                                attempt,
                                deadline - now,
                            )
                            self._ftcache_last_wait_log_ts = now
                        await asyncio.sleep(self._OFFLINE_RETRY_INTERVAL_S)
            else:
                # Daemon unavailable in offline mode — use local limiter
                limiter = self._ftcache_get_local_limiter()
                await asyncio.to_thread(
                    limiter.acquire,
                    4.0,
                    OhlcvCacheClient.LOW,
                )
            return await super()._async_get_candle_history(  # type: ignore[misc]
                pair,
                timeframe,
                candle_type,
                since_ms,
            )

        if not self._ftcache_client:
            # Daemon unavailable in live mode — use local limiter
            limiter = self._ftcache_get_local_limiter()
            await asyncio.to_thread(limiter.acquire, 30.0, None)
            return await super()._async_get_candle_history(  # type: ignore[misc]
                pair,
                timeframe,
                candle_type,
                since_ms,
            )

        client: OhlcvCacheClient = self._ftcache_client  # type: ignore[assignment]
        try:
            limit = self.ohlcv_candle_limit(  # type: ignore[attr-defined]
                timeframe,
                candle_type=candle_type,
                since_ms=since_ms,
            )
            priority: int | None = None
            if pair in self._ftcache_open_pairs:
                priority = OhlcvCacheClient.CRITICAL
            result = await client.fetch(
                pair=pair,
                timeframe=timeframe,
                candle_type=candle_type,
                since_ms=since_ms,
                limit=limit,
                priority=priority,
            )
            self._ftcache_record_cached("_async_get_candle_history", pair=pair)
            return result
        except CacheRateLimited:
            self._ftcache_bump("rate_limited")
            logger.info(
                "daemon rate-limited for %s %s — skipping this cycle (NOT falling back to ccxt)",
                pair,
                timeframe,
            )
            raise
        except CacheTimedOut:
            logger.info(
                "daemon busy (timeout) for %s %s — will retry next cycle, not falling back to ccxt",
                pair,
                timeframe,
            )
            raise
        except CacheUnavailable as e:
            self._ftcache_bump("fallback_ccxt")
            logger.warning(
                "cache unavailable for %s %s (%s) — falling back to ccxt",
                pair,
                timeframe,
                e,
            )
            return await super()._async_get_candle_history(  # type: ignore[misc]
                pair,
                timeframe,
                candle_type,
                since_ms,
            )

    # -------------------------------------------------------------------- tickers

    def get_tickers(
        self,
        symbols: list[str] | None = None,
        *,
        cached: bool = False,
        market_type: Any = None,
    ) -> Tickers:
        """Shared tickers: one fetch via daemon for all bots."""
        client = self._ftcache_get_client()
        if client is None:
            return super().get_tickers(  # type: ignore[misc]
                symbols=symbols,
                cached=cached,
                market_type=market_type,
            )
        if symbols is not None:
            if self._ftcache_should_block_ccxt():
                cache_key = f"fetch_tickers_{market_type}" if market_type else "fetch_tickers"
                with self._cache_lock:  # type: ignore[attr-defined]
                    stale = self._fetch_tickers_cache.get(cache_key)  # type: ignore[attr-defined]
                if stale:
                    filtered = {s: stale[s] for s in symbols if s in stale}
                    if filtered:
                        return filtered
            # Use HIGH priority when any requested symbol has an open position
            has_open = bool(self._ftcache_open_pairs & set(symbols))
            base_prio = OhlcvCacheClient.HIGH if has_open else OhlcvCacheClient.NORMAL
            prio_gt = self._ftcache_init_priority(base_prio)
            # A direct fetch_tickers costs the full non-whitelisted info
            # weight (HL: metaAndAssetCtxs = 20), not the default 1.
            self._ftcache_acquire_sync(priority=prio_gt, cost=20.0)
            return super().get_tickers(  # type: ignore[misc]
                symbols=symbols,
                cached=cached,
                market_type=market_type,
            )

        if cached:
            cache_key = f"fetch_tickers_{market_type}" if market_type else "fetch_tickers"
            with self._cache_lock:  # type: ignore[attr-defined]
                local_cached = self._fetch_tickers_cache.get(cache_key)  # type: ignore[attr-defined]
            if local_cached:
                return local_cached

        try:
            mt_str = ""
            if market_type is not None:
                mt_str = market_type.value if hasattr(market_type, "value") else str(market_type)
            # Use HIGH priority when we have open positions (critical for FreqUI display)
            has_open = bool(self._ftcache_open_pairs)
            tickers_prio = OhlcvCacheClient.HIGH if has_open else OhlcvCacheClient.NORMAL
            ok, tickers = self._ftcache_run_on_loop(
                client.get_tickers(market_type=mt_str, priority=tickers_prio),
            )
            if not ok:
                if self._ftcache_should_block_ccxt():
                    cache_key = f"fetch_tickers_{market_type}" if market_type else "fetch_tickers"
                    with self._cache_lock:  # type: ignore[attr-defined]
                        stale = self._fetch_tickers_cache.get(cache_key)  # type: ignore[attr-defined]
                    if stale:
                        return stale
                    return {}
                base_prio = OhlcvCacheClient.HIGH if has_open else OhlcvCacheClient.NORMAL
                prio_gt = self._ftcache_init_priority(base_prio)
                self._ftcache_acquire_sync(priority=prio_gt, cost=20.0)
                return super().get_tickers(  # type: ignore[misc]
                    symbols=symbols,
                    cached=cached,
                    market_type=market_type,
                )
            if not isinstance(tickers, dict):
                logger.warning(
                    "daemon returned tickers as %s — falling back to ccxt",
                    type(tickers).__name__,
                )
                raise CacheUnavailable("tickers data is not a dict")
            cache_key = f"fetch_tickers_{market_type}" if market_type else "fetch_tickers"
            with self._cache_lock:  # type: ignore[attr-defined]
                self._fetch_tickers_cache[cache_key] = tickers  # type: ignore[attr-defined]
            self._ftcache_tickers_fresh_ts = time.monotonic()
            self._ftcache_record_cached("get_tickers")
            return tickers
        except CacheRateLimited:
            self._ftcache_bump("rate_limited")
            self._ftcache_bump("stale_tickers")
            self._ftcache_last_backoff_active = True
            self._ftcache_last_backoff_ts = time.monotonic()
            if self._ftcache_tickers_fresh_ts:
                age = time.monotonic() - self._ftcache_tickers_fresh_ts
            else:
                age = float("inf")
            # If data is too old and we have open positions, force a fresh fetch
            if self._ftcache_open_pairs and age > self._STALE_TICKERS_MAX_AGE_S:
                logger.warning(
                    "tickers stale (%.0fs > %.0fs max) with %d open pairs — forcing CRITICAL fetch",
                    age,
                    self._STALE_TICKERS_MAX_AGE_S,
                    len(self._ftcache_open_pairs),
                )
                self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL, cost=20.0)
                return super().get_tickers(  # type: ignore[misc]
                    symbols=symbols,
                    cached=cached,
                    market_type=market_type,
                )
            cache_key = f"fetch_tickers_{market_type}" if market_type else "fetch_tickers"
            with self._cache_lock:  # type: ignore[attr-defined]
                stale = self._fetch_tickers_cache.get(cache_key)  # type: ignore[attr-defined]
            if stale:
                logger.info(
                    "shared tickers rate-limited — using %.0fs-old local cache"
                    " (NOT falling back to ccxt)",
                    age,
                )
                return stale
            logger.info("shared tickers rate-limited, no local cache — returning empty")
            return {}
        except CacheUnavailable as e:
            if self._ftcache_should_block_ccxt():
                cache_key = f"fetch_tickers_{market_type}" if market_type else "fetch_tickers"
                with self._cache_lock:  # type: ignore[attr-defined]
                    stale = self._fetch_tickers_cache.get(cache_key)  # type: ignore[attr-defined]
                if stale:
                    logger.info("tickers unavailable during backoff — using stale cache")
                    return stale
                logger.info("tickers unavailable during backoff, no stale — returning empty")
                return {}
            self._ftcache_bump("fallback_ccxt")
            logger.warning("shared tickers failed (%s) — falling back to ccxt", e)
            return super().get_tickers(  # type: ignore[misc]
                symbols=symbols,
                cached=cached,
                market_type=market_type,
            )

    # -------------------------------------------------------------------- positions

    def fetch_positions(
        self,
        pair: str | None = None,
        params: dict | None = None,
    ) -> list[CcxtPosition]:
        """Shared positions: first bot fetches, others read from cache."""
        # HIP-3 (builder-dex) positions are per-bot configuration and are NOT
        # covered by the shared, main-dex-only positions cache: a sibling bot that
        # does not trade the builder dex populates the shared cache with main-dex
        # positions only. Serving that cache to a builder-dex bot makes its own
        # HIP-3 positions read as absent (0), which drives handle_onexchange_order
        # to fabricate an external_close (no real exit order) and strand the live
        # position on-chain — where it silently accumulates. So whenever this bot
        # trades any HIP-3 dex, bypass the shared cache and fetch directly through
        # the native Hyperliquid override (main dex + every configured HIP-3 dex).
        if getattr(self, "_get_configured_hip3_dexes", lambda: [])():
            return super().fetch_positions(pair=pair, params=params)  # type: ignore[misc]
        # Phase 2: when the mixin-side refresher is active, serve from its
        # always-fresh local cache (instant, never blocks). Returns None to fall
        # through to the daemon/ccxt path when the cache is too old. Inert when the
        # refresher is off — legacy behaviour preserved.
        if self._pos_refresher_active:
            self._positions_watchdog()  # self-heal a dead thread / force refresh if frozen
            served = self._positions_serve_from_refresher(pair)
            if served is not None:
                return served

        if pair is not None:
            if self._ftcache_last_positions is not None:
                age = time.monotonic() - self._ftcache_last_positions_ts
                if age < 30.0:
                    return [p for p in self._ftcache_last_positions if p.get("symbol") == pair]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.HIGH, cost=2.0)
            return super().fetch_positions(pair=pair, params=params)  # type: ignore[misc]

        client = self._ftcache_get_client()
        if client is None:
            return super().fetch_positions(pair=pair, params=params)  # type: ignore[misc]

        # Try shared cache first (thread-safe via _loop_lock)
        auto_granted = False
        _t_cache = time.monotonic()
        try:
            ok, result = self._ftcache_run_on_loop(client.get_positions())
            if ok:
                hit, positions, auto_granted = result
                if hit:
                    if not isinstance(positions, list) or (
                        positions and not isinstance(positions[0], dict)
                    ):
                        logger.warning(
                            "daemon returned positions as %s — falling back to ccxt",
                            type(positions[0]).__name__ if positions else type(positions).__name__,
                        )
                        raise CacheUnavailable("positions data corrupted")
                    self._log_exchange_response(  # type: ignore[attr-defined]
                        "fetch_positions",
                        positions,
                        add_info="from ftcache",
                    )
                    self._ftcache_save_positions(positions)
                    self._ftcache_record_cached("fetch_positions")
                    return positions
        except CacheRateLimited:
            self._ftcache_bump("rate_limited")
            self._ftcache_last_backoff_active = True
            self._ftcache_last_backoff_ts = time.monotonic()
            stale = self._ftcache_get_stale_positions()
            if stale is not None:
                self._ftcache_bump("stale_positions")
                return stale
            # Stale data rejected (too old or missing) — force CRITICAL fetch
            logger.warning(
                "positions rate-limited, stale data rejected"
                " — forcing CRITICAL-priority fetch (cost=2)",
            )
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL, cost=2.0)
            positions = super().fetch_positions(pair=pair, params=params)  # type: ignore[misc]
            self._ftcache_save_positions(positions)
            return positions
        except CacheUnavailable:
            pass
        _t_after_cache = time.monotonic()

        # Cache miss — do the actual fetch and push result
        # If daemon auto-granted a rate token, skip the separate acquire call
        if not auto_granted:
            prio_pos = self._ftcache_init_priority(OhlcvCacheClient.HIGH)
            if not self._ftcache_acquire_sync(priority=prio_pos, cost=2.0):
                stale = self._ftcache_get_stale_positions()
                if stale is not None:
                    return stale
                if not self._ftcache_init_complete:
                    logger.warning("positions shed during init — retrying CRITICAL")
                    self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL, cost=2.0)
                else:
                    logger.warning("positions acquire shed + no stale data — returning empty")
                    return []
        _t_after_acquire = time.monotonic()
        try:
            positions = super().fetch_positions(pair=pair, params=params)  # type: ignore[misc]
        except DDosProtection:
            self._ftcache_last_backoff_active = True
            self._ftcache_last_backoff_ts = time.monotonic()
            stale = self._ftcache_get_stale_positions()
            if stale is not None:
                return stale
            raise
        _t_after_fetch = time.monotonic()
        self._ftcache_save_positions(positions)

        try:
            self._ftcache_run_on_loop(client.push_positions(positions))
        except CacheUnavailable:
            pass

        _total = _t_after_fetch - _t_cache
        if _total > 2.0:
            logger.info(
                "[fetch_positions] breakdown: cache_check=%.1fs, acquire=%.1fs, "
                "exchange_fetch=%.1fs, total=%.1fs auto_grant=%s",
                _t_after_cache - _t_cache,
                _t_after_acquire - _t_after_cache,
                _t_after_fetch - _t_after_acquire,
                _total,
                auto_granted,
            )

        return positions

    # -------------------------------------------------------------------- rate-limited REST calls
    # Weights match Hyperliquid API costs (see defaults.py HL_WEIGHT_MAP).
    # For non-HL exchanges these are still 1.0 (flat mode in TokenBucket).

    def create_order(self, **kwargs) -> CcxtOrder:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL, cost=1.0)
        result = super().create_order(**kwargs)  # type: ignore[misc]
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_report_order(
                pair=kwargs.get("pair", ""),
                side=kwargs.get("side", ""),
                order_type=kwargs.get("ordertype", kwargs.get("order_type", "")),
                amount=float(kwargs.get("amount", 0)),
                action="create",
                order_id=result.get("id", "") if isinstance(result, dict) else "",
            )
        return result

    def cancel_order(
        self,
        order_id: str,
        pair: str,
        params: dict | None = None,
    ) -> dict[str, Any]:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL, cost=1.0)
        result = super().cancel_order(order_id, pair, params)  # type: ignore[misc]
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_report_order(
                pair=pair,
                side="",
                order_type="",
                amount=0,
                action="cancel",
                order_id=order_id,
            )
        return result

    def fetch_order(
        self,
        order_id: str,
        pair: str,
        params: dict | None = None,
    ) -> CcxtOrder:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.HIGH, cost=1.0)
        return super().fetch_order(order_id, pair, params)  # type: ignore[misc]

    def get_balances(self, params: dict | None = None) -> CcxtBalances:
        """Shared balances: all bots on the same wallet share one fetch."""
        if self._config.get("dry_run"):  # type: ignore[attr-defined]
            return super().get_balances(params)  # type: ignore[misc]

        client = self._ftcache_get_client()
        auto_granted = False
        if client is not None:
            try:
                ok, result = self._ftcache_run_on_loop(client.get_balances())
                if ok:
                    hit, balances, auto_granted = result
                    if hit:
                        self._ftcache_record_cached("get_balances")
                        return balances
            except (CacheUnavailable, CacheTimedOut, CacheRateLimited):
                pass

        if not auto_granted:
            prio = self._ftcache_init_priority(OhlcvCacheClient.NORMAL)
            if not self._ftcache_acquire_sync(priority=prio, cost=2.0):
                if hasattr(self, "_ftcache_last_balances") and self._ftcache_last_balances:
                    logger.info("get_balances shed — using last known balances")
                    return self._ftcache_last_balances
                logger.warning(
                    "get_balances shed with no stale data (init?) "
                    "— retrying with CRITICAL priority to unblock startup",
                )
                if not self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL, cost=2.0):
                    raise DDosProtection("get_balances shed even at CRITICAL priority")
        try:
            balances = super().get_balances(params)  # type: ignore[misc]
        except DDosProtection:
            self._ftcache_last_backoff_active = True
            self._ftcache_last_backoff_ts = time.monotonic()
            raise
        self._ftcache_last_balances = balances

        if client is not None:
            try:
                self._ftcache_run_on_loop(client.push_balances(balances))
            except CacheUnavailable:
                pass

        return balances

    def fetch_l2_order_book(self, pair: str, limit: int = 100) -> OrderBook:
        # Metered in dry-run too: unlike order endpoints, dry bots hit the
        # REAL l2Book API for pricing and dry-order fill simulation.  The
        # priority floor downgrades dry bots to LOW so they yield to live.
        self._ftcache_acquire_sync(priority=OhlcvCacheClient.HIGH, cost=2.0)
        return super().fetch_l2_order_book(pair, limit)  # type: ignore[misc]

    # -------------------------------------------------------------------- remaining REST calls
    # Every ccxt REST call must go through the daemon's rate limiter so that
    # the token bucket sees the true global request rate.

    def reload_markets(self, force: bool = False, *, load_leverage_tiers: bool = True) -> None:
        from freqtrade.util.datetime_helpers import dt_ts

        client = self._ftcache_get_client()
        if client is not None:
            try:
                ok, result = self._ftcache_run_on_loop(client.get_markets())
                if ok:
                    hit, markets = result
                    if hit and markets:
                        if not isinstance(markets, dict):
                            logger.warning(
                                "daemon returned markets as %s (len=%d)"
                                " — falling back to direct ccxt fetch",
                                type(markets).__name__,
                                len(markets),
                            )
                            raise CacheUnavailable("markets data is not a dict")
                        self._markets = markets  # type: ignore[attr-defined]
                        for api in (
                            getattr(self, "_api", None),
                            getattr(self, "_api_async", None),
                            getattr(self, "_ws_async", None),
                        ):
                            if api is not None:
                                api.set_markets(markets)
                        self._last_markets_refresh = dt_ts()
                        self._ftcache_record_cached("reload_markets")
                        logger.debug(
                            "reload_markets from shared cache (%d symbols)",
                            len(markets),
                        )
                        if (
                            load_leverage_tiers
                            and getattr(self, "trading_mode", None) == TradingMode.FUTURES
                        ):
                            self.fill_leverage_tiers()  # type: ignore[attr-defined]
                        return
            except (CacheRateLimited, CacheTimedOut):
                if hasattr(self, "_markets") and self._markets:  # type: ignore[attr-defined]
                    logger.info("reload_markets shed — using existing markets")
                    return
            except CacheUnavailable:
                pass
        prio_mkts = self._ftcache_init_priority(OhlcvCacheClient.HIGH)
        self._ftcache_acquire_sync(priority=prio_mkts, cost=20.0)
        return super().reload_markets(force, load_leverage_tiers=load_leverage_tiers)  # type: ignore[misc]

    @staticmethod
    def _ticker_has_pricing(ticker: dict) -> bool:
        return ticker.get("bid") is not None or ticker.get("ask") is not None

    def fetch_ticker(self, pair: str) -> Ticker:
        """Extract ticker from shared tickers cache when possible.

        Avoids per-pair API calls — all bots share one bulk fetch.
        Falls through to ccxt only when pair is absent from cache entirely.
        """
        client = self._ftcache_get_client()
        # Exchanges without bid/ask in bulk tickers (e.g. Hyperliquid) still
        # return lastPrice which is sufficient for pricing. Only fall through
        # to per-pair ccxt if the pair is completely absent from the cache.
        exchange_has_pricing = getattr(self, "_ft_has", {}).get("tickers_have_bid_ask", True)
        if client is not None:
            cache_key = "fetch_tickers"
            with self._cache_lock:  # type: ignore[attr-defined]
                tickers = self._fetch_tickers_cache.get(cache_key)  # type: ignore[attr-defined]
            if tickers and pair in tickers:
                if exchange_has_pricing and not self._ticker_has_pricing(tickers[pair]):
                    pass  # need bid/ask but don't have it — try refresh
                else:
                    self._ftcache_record_cached("fetch_ticker", pair=pair)
                    return tickers[pair]
            fresh_ts = getattr(self, "_ftcache_tickers_fresh_ts", 0) or 0
            cache_age = time.monotonic() - fresh_ts
            if tickers and pair in tickers and cache_age < 30.0:
                self._ftcache_record_cached("fetch_ticker", pair=pair)
                return tickers[pair]
            if not tickers or cache_age >= 30.0:
                try:
                    is_open_pair = pair in self._ftcache_open_pairs
                    tick_prio = OhlcvCacheClient.HIGH if is_open_pair else OhlcvCacheClient.NORMAL
                    ok, all_tickers = self._ftcache_run_on_loop(
                        client.get_tickers(market_type="", priority=tick_prio),
                    )
                    if ok:
                        with self._cache_lock:  # type: ignore[attr-defined]
                            self._fetch_tickers_cache[cache_key] = all_tickers  # type: ignore[attr-defined]
                        self._ftcache_tickers_fresh_ts = time.monotonic()
                        if pair in all_tickers:
                            self._ftcache_record_cached("fetch_ticker", pair=pair)
                            return all_tickers[pair]
                except (CacheRateLimited, CacheTimedOut) as exc:
                    if isinstance(exc, CacheRateLimited):
                        self._ftcache_last_backoff_active = True
                        self._ftcache_last_backoff_ts = time.monotonic()
                except CacheUnavailable:
                    pass
        is_open_pair = pair in self._ftcache_open_pairs
        base_prio = OhlcvCacheClient.HIGH if is_open_pair else OhlcvCacheClient.NORMAL
        if self._ftcache_should_block_ccxt():
            cache_key = "fetch_tickers"
            with self._cache_lock:  # type: ignore[attr-defined]
                stale = self._fetch_tickers_cache.get(cache_key)  # type: ignore[attr-defined]
            if stale and pair in stale:
                logger.debug("fetch_ticker blocked during backoff — stale for %s", pair)
                return stale[pair]
            # ccxt fetchTicker on HL resolves to fetchTickers (weight 20).
            if is_open_pair and self._ftcache_acquire_sync(
                priority=OhlcvCacheClient.HIGH, cost=20.0
            ):
                return super().fetch_ticker(pair)  # type: ignore[misc]
            raise DDosProtection(f"fetch_ticker blocked for {pair} during backoff (no stale data)")
        prio_tick = self._ftcache_init_priority(base_prio)
        if not self._ftcache_acquire_sync(priority=prio_tick, cost=20.0):
            cache_key = "fetch_tickers"
            with self._cache_lock:  # type: ignore[attr-defined]
                stale = self._fetch_tickers_cache.get(cache_key)  # type: ignore[attr-defined]
            if stale and pair in stale:
                logger.debug("fetch_ticker shed — using stale cache for %s", pair)
                return stale[pair]
            raise DDosProtection(f"fetch_ticker shed for {pair} during 429 backoff")
        return super().fetch_ticker(pair)  # type: ignore[misc]

    def fetch_funding_rate(self, pair: str) -> FundingRate:
        """Fetch funding rate from daemon's bulk cache when possible."""
        client = self._ftcache_get_client()
        if client is not None:
            try:
                ok, result = self._ftcache_run_on_loop(client.get_funding_rates())
                if ok:
                    hit, all_rates = result
                    if hit and pair in all_rates:
                        self._ftcache_record_cached("fetch_funding_rate")
                        return all_rates[pair]
            except (CacheRateLimited, CacheTimedOut):
                pass
            except CacheUnavailable:
                pass

        if not self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL):
            raise DDosProtection(f"fetch_funding_rate shed for {pair} during 429 backoff")
        return super().fetch_funding_rate(pair)  # type: ignore[misc]

    def fetch_trading_fees(self) -> dict[str, Any]:
        self._ftcache_acquire_sync(priority=OhlcvCacheClient.LOW)
        return super().fetch_trading_fees()  # type: ignore[misc]

    def fetch_bids_asks(
        self,
        symbols: list[str] | None = None,
        *,
        cached: bool = False,
    ) -> dict[str, Any]:
        # Metered in dry-run too — real API call (see fetch_l2_order_book).
        self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
        return super().fetch_bids_asks(symbols=symbols, cached=cached)  # type: ignore[misc]

    def get_trades_for_order(
        self,
        order_id: str,
        pair: str,
        since: datetime,
        params: dict | None = None,
    ) -> list[dict]:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
        return super().get_trades_for_order(order_id, pair, since, params)  # type: ignore[misc]

    def _get_funding_fees_from_exchange(self, pair: str, since: datetime | int) -> float:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.LOW)
        return super()._get_funding_fees_from_exchange(pair, since)  # type: ignore[misc]

    def get_leverage_tiers(self) -> dict[str, list[dict]]:
        """Fetch leverage tiers from daemon's shared cache when possible."""
        client = self._ftcache_get_client()
        if client is not None:
            try:
                ok, result = self._ftcache_run_on_loop(client.get_leverage_tiers())
                if ok:
                    hit, tiers = result
                    if hit and tiers:
                        self._ftcache_record_cached("get_leverage_tiers")
                        return tiers
            except (CacheRateLimited, CacheTimedOut):
                pass
            except CacheUnavailable:
                pass

        self._ftcache_acquire_sync(priority=OhlcvCacheClient.LOW)
        return super().get_leverage_tiers()  # type: ignore[misc]

    def _set_leverage(
        self,
        leverage: float,
        pair: str | None = None,
        accept_fail: bool = False,
    ):
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
        return super()._set_leverage(leverage, pair, accept_fail)  # type: ignore[misc]

    def set_margin_mode(
        self,
        pair: str,
        margin_mode: MarginMode,
        accept_fail: bool = False,
        params: dict | None = None,
    ):
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.LOW)
        return super().set_margin_mode(pair, margin_mode, accept_fail, params)  # type: ignore[misc]

    def _fetch_orders(
        self,
        pair: str,
        since: datetime,
        params: dict | None = None,
    ) -> list[CcxtOrder]:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
        return super()._fetch_orders(pair, since, params)  # type: ignore[misc]

    def create_stoploss(
        self,
        pair: str,
        amount: float,
        stop_price: float,
        order_types: dict,
        side: Any,
        leverage: float,
    ) -> CcxtOrder:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL)
        return super().create_stoploss(  # type: ignore[misc]
            pair,
            amount,
            stop_price,
            order_types,
            side,
            leverage,
        )

    async def _async_fetch_trades(
        self,
        pair: str,
        since: int | None = None,
        params: dict | None = None,
    ) -> tuple[list[list], Any]:
        client = self._ftcache_get_client()
        if client is not None:
            try:
                await client.acquire_rate_token(
                    priority=OhlcvCacheClient.LOW,
                    cost=1.0,
                )
            except (CacheUnavailable, CacheTimedOut):
                pass
        return await super()._async_fetch_trades(pair, since, params)  # type: ignore[misc]

    async def get_market_leverage_tiers(
        self,
        symbol: str,
    ) -> tuple[str, list[dict]]:
        client = self._ftcache_get_client()
        if client is not None:
            try:
                await client.acquire_rate_token(
                    priority=OhlcvCacheClient.LOW,
                    cost=1.0,
                )
            except (CacheUnavailable, CacheTimedOut):
                pass
        return await super().get_market_leverage_tiers(symbol)  # type: ignore[misc]

    async def _fetch_funding_rate_history(
        self,
        pair: str,
        timeframe: str,
        limit: int,
        since_ms: int | None = None,
    ) -> list[list]:
        client = self._ftcache_get_client()
        if client is not None:
            try:
                await client.acquire_rate_token(
                    priority=OhlcvCacheClient.LOW,
                    cost=1.0,
                )
            except CacheUnavailable:
                pass
        return await super()._fetch_funding_rate_history(  # type: ignore[misc]
            pair,
            timeframe,
            limit,
            since_ms,
        )
