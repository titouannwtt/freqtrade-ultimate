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

    _ACQUIRE_TIMEOUT_S: float = 120.0
    _STALE_POSITIONS_WARN_AGE_S: float = 120.0

    # Local fallback caches for rate-limited scenarios
    _ftcache_last_positions: list | None = None
    _ftcache_last_positions_ts: float = 0.0
    _ftcache_tickers_fresh_ts: float = 0.0
    _ftcache_last_balances: dict | None = None
    _ftcache_last_backoff_active: bool = False
    _ftcache_last_backoff_ts: float = 0.0

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

    def _ftcache_init_priority(self, requested: int | None) -> int | None:
        """During init phase, escalate essential calls to CRITICAL."""
        if self._ftcache_init_complete:
            return requested
        return OhlcvCacheClient.CRITICAL

    def _ftcache_enabled(self) -> bool:
        from freqtrade.enums import RunMode
        runmode = self._config.get("runmode", RunMode.OTHER)  # type: ignore[attr-defined]
        if runmode in (RunMode.BACKTEST, RunMode.HYPEROPT):
            return False
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
                    "direct ccxt for this bot", e,
                )
                self._ftcache_warned = True
            self._ftcache_client = False

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
                    type(api).__name__, old_rate, target_rate_ms,
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
        self, method: str, pair: str | None = None, latency_ms: float = 0.0,
    ) -> None:
        metrics = getattr(self, "_metrics", None)
        if metrics is None:
            return
        try:
            from freqtrade.exchange.exchange_metrics import ApiCall

            metrics.record(ApiCall(
                ts=time.time(),
                method=method,
                exchange=getattr(self, "name", "unknown"),
                latency_ms=latency_ms,
                cached=True,
                success=True,
                error_type=None,
                pair=pair,
            ))
        except Exception:  # noqa: S110
            pass

    def ftcache_get_stats(self) -> dict:
        """Return diagnostic counters for the cache layer."""
        return dict(getattr(self, "_ftcache_stats", {}))

    def _ftcache_save_positions(self, positions: list) -> None:
        self._ftcache_last_positions = positions
        self._ftcache_last_positions_ts = time.monotonic()

    def _ftcache_get_stale_positions(self) -> list | None:
        if self._ftcache_last_positions is None:
            return None
        age = time.monotonic() - self._ftcache_last_positions_ts
        if age > self._STALE_POSITIONS_WARN_AGE_S:
            logger.warning(
                "positions rate-limited — using %.0fs-old local cache"
                " (data may be outdated, NOT falling back to ccxt)", age,
            )
        else:
            logger.info(
                "positions rate-limited — using %.0fs-old local cache", age,
            )
        return self._ftcache_last_positions

    _LOOP_LOCK_TIMEOUT_S: float = 5.0

    def _ftcache_run_on_loop(self, coro):
        """Run an async daemon call, serialized with _loop_lock.

        Prevents event loop races between the worker thread
        (refresh_latest_ohlcv) and the Uvicorn API thread.

        Returns (True, result) on success.
        Returns (False, None) if the lock is held beyond timeout.
        Exceptions from the coroutine propagate normally.
        """
        lock = getattr(self, '_loop_lock', None)
        if lock is None:
            return False, None
        if not lock.acquire(timeout=self._LOOP_LOCK_TIMEOUT_S):
            return False, None
        try:
            return True, self.loop.run_until_complete(coro)  # type: ignore[attr-defined]
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
                    " — all bots will queue by priority",
                    method, pair,
                )
        except Exception as e:
            logger.debug("report_429 to daemon failed (non-fatal): %s", e)

    def _ftcache_acquire_sync(self, priority: int | None = None, cost: float = 1.0) -> bool:
        """Acquire a rate token synchronously (blocks until granted).

        Called before any non-OHLCV REST call so that ALL API traffic
        from all bots shares the daemon's centralized rate limit.

        During a 429 backoff, the daemon queues this request and serves it
        by priority (CRITICAL first). This call blocks until the token is
        granted or times out.

        Returns True when the token was granted (or daemon unavailable).
        Returns False only when the _loop_lock is unavailable and local
        backoff check rejects the request.
        """
        client = self._ftcache_get_client()
        if client is None:
            return True
        try:
            lock = getattr(self, '_loop_lock', None)
            if lock is None or not lock.acquire(timeout=self._LOOP_LOCK_TIMEOUT_S):
                self._ftcache_bump("acquire_skip_loop")
                return self._ftcache_local_backoff_check(priority)
            try:
                self.loop.run_until_complete(  # type: ignore[attr-defined]
                    asyncio.wait_for(
                        client.acquire_rate_token(priority=priority, cost=cost),
                        timeout=self._ACQUIRE_TIMEOUT_S,
                    ),
                )
                self._ftcache_last_backoff_active = False
                return True
            finally:
                lock.release()
        except CacheTimedOut:
            self._ftcache_bump("acquire_timeout")
            logger.info(
                "rate token acquire timed out after %.0fs — allowing but not rate-limited "
                "(priority=%s, cost=%.0f)",
                self._ACQUIRE_TIMEOUT_S, priority, cost,
            )
            return True
        except CacheUnavailable:
            self._ftcache_bump("acquire_timeout")
            return True
        except TimeoutError:
            self._ftcache_bump("acquire_timeout")
            logger.info(
                "rate token acquire timed out after %.0fs — allowing "
                "(priority=%s, cost=%.0f)",
                self._ACQUIRE_TIMEOUT_S, priority, cost,
            )
            return True
        except Exception as e:
            logger.debug("rate token acquire failed (%s), proceeding without", e)
            return True

    # -------------------------------------------------------------------- OHLCV

    _CACHEABLE_CANDLE_TYPES = frozenset({CandleType.SPOT, CandleType.FUTURES})

    async def _async_get_candle_history(
        self,
        pair: str,
        timeframe: str,
        candle_type: CandleType,
        since_ms: int | None = None,
    ) -> OHLCVResponse:
        if candle_type not in self._CACHEABLE_CANDLE_TYPES:
            return await super()._async_get_candle_history(  # type: ignore[misc]
                pair, timeframe, candle_type, since_ms,
            )

        self._ftcache_maybe_init()

        if not self._ftcache_client:
            return await super()._async_get_candle_history(  # type: ignore[misc]
                pair, timeframe, candle_type, since_ms,
            )

        client: OhlcvCacheClient = self._ftcache_client  # type: ignore[assignment]
        try:
            limit = self.ohlcv_candle_limit(  # type: ignore[attr-defined]
                timeframe, candle_type=candle_type, since_ms=since_ms,
            )
            priority: int | None = None
            if pair in self._ftcache_open_pairs:
                priority = OhlcvCacheClient.CRITICAL
            result = await client.fetch(
                pair=pair, timeframe=timeframe,
                candle_type=candle_type, since_ms=since_ms, limit=limit,
                priority=priority,
            )
            self._ftcache_record_cached("_async_get_candle_history", pair=pair)
            return result
        except CacheRateLimited:
            self._ftcache_bump("rate_limited")
            logger.info(
                "daemon rate-limited for %s %s — skipping this cycle"
                " (NOT falling back to ccxt)",
                pair, timeframe,
            )
            raise
        except CacheTimedOut:
            logger.info(
                "daemon busy (timeout) for %s %s"
                " — will retry next cycle, not falling back to ccxt",
                pair, timeframe,
            )
            raise
        except CacheUnavailable as e:
            self._ftcache_bump("fallback_ccxt")
            logger.warning(
                "cache unavailable for %s %s (%s) — falling back to ccxt",
                pair, timeframe, e,
            )
            return await super()._async_get_candle_history(  # type: ignore[misc]
                pair, timeframe, candle_type, since_ms,
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
                symbols=symbols, cached=cached, market_type=market_type,
            )
        if symbols is not None:
            prio_gt = self._ftcache_init_priority(OhlcvCacheClient.NORMAL)
            self._ftcache_acquire_sync(priority=prio_gt)
            return super().get_tickers(  # type: ignore[misc]
                symbols=symbols, cached=cached, market_type=market_type,
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
            ok, tickers = self._ftcache_run_on_loop(
                client.get_tickers(market_type=mt_str),
            )
            if not ok:
                prio_gt = self._ftcache_init_priority(OhlcvCacheClient.NORMAL)
                self._ftcache_acquire_sync(priority=prio_gt)
                return super().get_tickers(  # type: ignore[misc]
                    symbols=symbols, cached=cached, market_type=market_type,
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
            cache_key = f"fetch_tickers_{market_type}" if market_type else "fetch_tickers"
            with self._cache_lock:  # type: ignore[attr-defined]
                stale = self._fetch_tickers_cache.get(cache_key)  # type: ignore[attr-defined]
            if stale:
                logger.info(
                    "shared tickers rate-limited — using %.0fs-old local cache"
                    " (NOT falling back to ccxt)", age,
                )
                return stale
            logger.info("shared tickers rate-limited, no local cache — returning empty")
            return {}
        except CacheUnavailable as e:
            self._ftcache_bump("fallback_ccxt")
            logger.warning("shared tickers failed (%s) — falling back to ccxt", e)
            return super().get_tickers(  # type: ignore[misc]
                symbols=symbols, cached=cached, market_type=market_type,
            )

    # -------------------------------------------------------------------- positions

    def fetch_positions(
        self, pair: str | None = None, params: dict | None = None,
    ) -> list[CcxtPosition]:
        """Shared positions: first bot fetches, others read from cache."""
        if pair is not None:
            if self._ftcache_last_positions is not None:
                age = time.monotonic() - self._ftcache_last_positions_ts
                if age < 30.0:
                    return [
                        p for p in self._ftcache_last_positions
                        if p.get("symbol") == pair
                    ]
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
                        "fetch_positions", positions, add_info="from ftcache",
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
            logger.warning(
                "positions rate-limited, no local fallback"
                " — forced HIGH-priority fetch (first call, cost=2)",
            )
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
        return super().create_order(**kwargs)  # type: ignore[misc]

    def cancel_order(
        self, order_id: str, pair: str, params: dict | None = None,
    ) -> dict[str, Any]:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL, cost=1.0)
        return super().cancel_order(order_id, pair, params)  # type: ignore[misc]

    def fetch_order(
        self, order_id: str, pair: str, params: dict | None = None,
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
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
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
                                type(markets).__name__, len(markets),
                            )
                            raise CacheUnavailable("markets data is not a dict")
                        self._markets = markets  # type: ignore[attr-defined]
                        api_sync = getattr(self, "_api", None)
                        api_async = getattr(self, "_api_async", None)
                        ws_async = getattr(self, "_ws_async", None)
                        if api_async and hasattr(api_async, "markets"):
                            api_async.markets = markets
                            api_async.markets_by_id = None
                            api_async.set_markets(markets)
                        if api_sync and api_async:
                            api_sync.set_markets_from_exchange(api_async)
                            api_sync.options = api_async.options
                        if ws_async and api_async:
                            ws_async.set_markets_from_exchange(api_async)
                            ws_async.options = api_async.options
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
        Falls through to ccxt when cached ticker lacks bid/ask.
        """
        client = self._ftcache_get_client()
        if client is not None and not self._config.get("dry_run"):  # type: ignore[attr-defined]
            cache_key = "fetch_tickers"
            with self._cache_lock:  # type: ignore[attr-defined]
                tickers = self._fetch_tickers_cache.get(cache_key)  # type: ignore[attr-defined]
            if tickers and pair in tickers and self._ticker_has_pricing(tickers[pair]):
                self._ftcache_record_cached("fetch_ticker", pair=pair)
                return tickers[pair]
            fresh_ts = getattr(self, "_ftcache_tickers_fresh_ts", 0) or 0
            cache_age = time.monotonic() - fresh_ts
            if tickers and cache_age < 15.0:
                pass
            else:
                try:
                    ok, all_tickers = self._ftcache_run_on_loop(
                        client.get_tickers(market_type=""),
                    )
                    if ok:
                        with self._cache_lock:  # type: ignore[attr-defined]
                            self._fetch_tickers_cache[cache_key] = all_tickers  # type: ignore[attr-defined]
                        self._ftcache_tickers_fresh_ts = time.monotonic()
                        if pair in all_tickers and self._ticker_has_pricing(all_tickers[pair]):
                            self._ftcache_record_cached("fetch_ticker", pair=pair)
                            return all_tickers[pair]
                except (CacheRateLimited, CacheTimedOut, CacheUnavailable):
                    pass
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            prio_tick = self._ftcache_init_priority(OhlcvCacheClient.NORMAL)
            if not self._ftcache_acquire_sync(priority=prio_tick):
                cache_key = "fetch_tickers"
                with self._cache_lock:  # type: ignore[attr-defined]
                    stale = self._fetch_tickers_cache.get(cache_key)  # type: ignore[attr-defined]
                if stale and pair in stale and self._ticker_has_pricing(stale[pair]):
                    logger.debug("fetch_ticker shed — using stale cache for %s", pair)
                    return stale[pair]
                raise DDosProtection(f"fetch_ticker shed for {pair} during 429 backoff")
        return super().fetch_ticker(pair)  # type: ignore[misc]

    def fetch_funding_rate(self, pair: str) -> FundingRate:
        """Fetch funding rate from daemon's bulk cache when possible."""
        if self._config.get("dry_run"):  # type: ignore[attr-defined]
            return super().fetch_funding_rate(pair)  # type: ignore[misc]

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
        self, symbols: list[str] | None = None, *, cached: bool = False,
    ) -> dict[str, Any]:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
        return super().fetch_bids_asks(symbols=symbols, cached=cached)  # type: ignore[misc]

    def get_trades_for_order(
        self, order_id: str, pair: str, since: datetime, params: dict | None = None,
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
        self, leverage: float, pair: str | None = None, accept_fail: bool = False,
    ):
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
        return super()._set_leverage(leverage, pair, accept_fail)  # type: ignore[misc]

    def set_margin_mode(
        self, pair: str, margin_mode: MarginMode,
        accept_fail: bool = False, params: dict | None = None,
    ):
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.LOW)
        return super().set_margin_mode(pair, margin_mode, accept_fail, params)  # type: ignore[misc]

    def _fetch_orders(
        self, pair: str, since: datetime, params: dict | None = None,
    ) -> list[CcxtOrder]:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
        return super()._fetch_orders(pair, since, params)  # type: ignore[misc]

    def create_stoploss(
        self, pair: str, amount: float, stop_price: float,
        order_types: dict, side: Any, leverage: float,
    ) -> CcxtOrder:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL)
        return super().create_stoploss(  # type: ignore[misc]
            pair, amount, stop_price, order_types, side, leverage,
        )

    async def _async_fetch_trades(
        self, pair: str, since: int | None = None, params: dict | None = None,
    ) -> tuple[list[list], Any]:
        client = self._ftcache_get_client()
        if client is not None:
            try:
                await client.acquire_rate_token(
                    priority=OhlcvCacheClient.LOW, cost=1.0,
                )
            except (CacheUnavailable, CacheTimedOut):
                pass
        return await super()._async_fetch_trades(pair, since, params)  # type: ignore[misc]

    async def get_market_leverage_tiers(
        self, symbol: str,
    ) -> tuple[str, list[dict]]:
        client = self._ftcache_get_client()
        if client is not None:
            try:
                await client.acquire_rate_token(
                    priority=OhlcvCacheClient.LOW, cost=1.0,
                )
            except (CacheUnavailable, CacheTimedOut):
                pass
        return await super().get_market_leverage_tiers(symbol)  # type: ignore[misc]

    async def _fetch_funding_rate_history(
        self, pair: str, timeframe: str, limit: int, since_ms: int | None = None,
    ) -> list[list]:
        client = self._ftcache_get_client()
        if client is not None:
            try:
                await client.acquire_rate_token(
                    priority=OhlcvCacheClient.LOW, cost=1.0,
                )
            except CacheUnavailable:
                pass
        return await super()._fetch_funding_rate_history(  # type: ignore[misc]
            pair, timeframe, limit, since_ms,
        )

