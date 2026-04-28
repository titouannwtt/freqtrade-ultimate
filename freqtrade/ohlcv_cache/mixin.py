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

from freqtrade.enums import CandleType, MarginMode
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

    _ACQUIRE_TIMEOUT_S: float = 30.0
    _STALE_POSITIONS_WARN_AGE_S: float = 120.0

    # Local fallback caches for rate-limited scenarios
    _ftcache_last_positions: list | None = None
    _ftcache_last_positions_ts: float = 0.0
    _ftcache_tickers_fresh_ts: float = 0.0

    def ftcache_set_open_pairs(self, pairs: set[str] | frozenset[str]) -> None:
        """Inform the cache layer which pairs currently have open positions.

        These pairs will be fetched at CRITICAL priority so exit decisions
        use the freshest possible data.
        """
        self._ftcache_open_pairs = frozenset(pairs)

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
            logger.info(
                "cache client ready for %s/%s",
                self.id,  # type: ignore[attr-defined]
                trading_mode_val,
            )
        except Exception as e:
            if not self._ftcache_warned:
                logger.warning(
                    "could not initialise cache client (%s) — falling back to "
                    "direct ccxt for this bot", e,
                )
                self._ftcache_warned = True
            self._ftcache_client = False

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

    def _ftcache_acquire_sync(self, priority: int | None = None, cost: float = 1.0) -> None:
        """Acquire a rate token synchronously (blocks until granted).

        Called before any non-OHLCV REST call so that ALL API traffic
        from all bots shares the daemon's centralized rate limit.
        """
        client = self._ftcache_get_client()
        if client is None:
            return
        try:
            loop = self.loop  # type: ignore[attr-defined]
            if loop.is_running():
                self._ftcache_bump("acquire_skip_loop")
                logger.debug("rate token skipped (event loop already running)")
                return
            loop.run_until_complete(
                asyncio.wait_for(
                    client.acquire_rate_token(priority=priority, cost=cost),
                    timeout=self._ACQUIRE_TIMEOUT_S,
                ),
            )
        except (CacheUnavailable, TimeoutError):
            self._ftcache_bump("acquire_timeout")
        except Exception as e:
            logger.debug("rate token acquire failed (%s), proceeding without", e)

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
            self._ftcache_record_cached("fetch_ohlcv", pair=pair)
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
                "cache fetch failed for %s %s (%s) — falling back to ccxt",
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
        loop = self.loop  # type: ignore[attr-defined]
        if client is None:
            return super().get_tickers(  # type: ignore[misc]
                symbols=symbols, cached=cached, market_type=market_type,
            )
        if symbols is not None or loop.is_running():
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
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
            tickers = loop.run_until_complete(
                client.get_tickers(market_type=mt_str),
            )
            cache_key = f"fetch_tickers_{market_type}" if market_type else "fetch_tickers"
            with self._cache_lock:  # type: ignore[attr-defined]
                self._fetch_tickers_cache[cache_key] = tickers  # type: ignore[attr-defined]
            self._ftcache_tickers_fresh_ts = time.monotonic()
            self._ftcache_record_cached("get_tickers")
            return tickers
        except CacheRateLimited:
            self._ftcache_bump("rate_limited")
            self._ftcache_bump("stale_tickers")
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
        loop = self.loop  # type: ignore[attr-defined]

        if pair is not None:
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.HIGH)
            return super().fetch_positions(pair=pair, params=params)  # type: ignore[misc]

        client = self._ftcache_get_client()
        if client is None:
            return super().fetch_positions(pair=pair, params=params)  # type: ignore[misc]
        if loop.is_running():
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.HIGH)
            return super().fetch_positions(pair=pair, params=params)  # type: ignore[misc]

        # Try shared cache first
        try:
            hit, positions = loop.run_until_complete(
                client.get_positions(),
            )
            if hit:
                self._log_exchange_response(  # type: ignore[attr-defined]
                    "fetch_positions", positions, add_info="from ftcache",
                )
                self._ftcache_save_positions(positions)
                self._ftcache_record_cached("fetch_positions")
                return positions
        except CacheRateLimited:
            self._ftcache_bump("rate_limited")
            stale = self._ftcache_get_stale_positions()
            if stale is not None:
                self._ftcache_bump("stale_positions")
                return stale
            logger.warning(
                "positions rate-limited, no local fallback"
                " — forced direct fetch (first call)",
            )
        except CacheUnavailable:
            pass

        # Cache miss — do the actual fetch (with rate token) and push result
        self._ftcache_acquire_sync(priority=OhlcvCacheClient.HIGH)
        positions = super().fetch_positions(pair=pair, params=params)  # type: ignore[misc]
        self._ftcache_save_positions(positions)

        try:
            loop.run_until_complete(
                client.push_positions(positions),
            )
        except CacheUnavailable:
            pass

        return positions

    # -------------------------------------------------------------------- rate-limited REST calls

    def create_order(self, **kwargs) -> CcxtOrder:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL)
        return super().create_order(**kwargs)  # type: ignore[misc]

    def cancel_order(
        self, order_id: str, pair: str, params: dict | None = None,
    ) -> dict[str, Any]:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.CRITICAL)
        return super().cancel_order(order_id, pair, params)  # type: ignore[misc]

    def fetch_order(
        self, order_id: str, pair: str, params: dict | None = None,
    ) -> CcxtOrder:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.HIGH)
        return super().fetch_order(order_id, pair, params)  # type: ignore[misc]

    def get_balances(self, params: dict | None = None) -> CcxtBalances:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
        return super().get_balances(params)  # type: ignore[misc]

    def fetch_l2_order_book(self, pair: str, limit: int = 100) -> OrderBook:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.HIGH)
        return super().fetch_l2_order_book(pair, limit)  # type: ignore[misc]

    # -------------------------------------------------------------------- remaining REST calls
    # Every ccxt REST call must go through the daemon's rate limiter so that
    # the token bucket sees the true global request rate.

    def reload_markets(self, force: bool = False, *, load_leverage_tiers: bool = True) -> None:
        self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
        return super().reload_markets(force, load_leverage_tiers=load_leverage_tiers)  # type: ignore[misc]

    def fetch_ticker(self, pair: str) -> Ticker:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
        return super().fetch_ticker(pair)  # type: ignore[misc]

    def fetch_funding_rate(self, pair: str) -> FundingRate:
        if not self._config.get("dry_run"):  # type: ignore[attr-defined]
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.NORMAL)
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

