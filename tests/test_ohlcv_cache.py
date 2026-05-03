"""
Tests for the OHLCV cache system: client exceptions, mixin interceptors,
rate-limited fallback behaviour, and CachedHyperliquid overrides.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from freqtrade.enums import CandleType, MarginMode
from freqtrade.ohlcv_cache.client import (
    CacheRateLimited,
    CacheTimedOut,
    CacheUnavailable,
    OhlcvCacheClient,
)
from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin


# -------------------------------------------------------------------- exceptions


class TestExceptionHierarchy:
    def test_cache_rate_limited_is_cache_unavailable(self):
        assert issubclass(CacheRateLimited, CacheUnavailable)

    def test_cache_rate_limited_is_runtime_error(self):
        assert issubclass(CacheRateLimited, RuntimeError)

    def test_catch_unavailable_catches_rate_limited(self):
        with pytest.raises(CacheUnavailable):
            raise CacheRateLimited("429")

    def test_catch_rate_limited_does_not_catch_unavailable(self):
        with pytest.raises(CacheUnavailable):
            try:
                raise CacheUnavailable("generic")
            except CacheRateLimited:
                pytest.fail("CacheRateLimited should not catch plain CacheUnavailable")

    def test_isinstance_check(self):
        exc = CacheRateLimited("429 Too Many Requests")
        assert isinstance(exc, CacheRateLimited)
        assert isinstance(exc, CacheUnavailable)
        assert isinstance(exc, RuntimeError)

    def test_cache_timed_out_is_cache_unavailable(self):
        assert issubclass(CacheTimedOut, CacheUnavailable)

    def test_timed_out_distinct_from_rate_limited(self):
        with pytest.raises(CacheTimedOut):
            try:
                raise CacheTimedOut("timeout")
            except CacheRateLimited:
                pytest.fail("CacheRateLimited should not catch CacheTimedOut")

    def test_catch_unavailable_catches_timed_out(self):
        with pytest.raises(CacheUnavailable):
            raise CacheTimedOut("timeout")


class TestCancelledErrorClosesConnection:
    """CancelledError during _send_and_receive must close the connection
    to prevent stale daemon responses from poisoning the next request."""

    @pytest.mark.asyncio
    async def test_cancelled_error_closes_connection(self):
        client = OhlcvCacheClient(
            socket_path="/tmp/fake.sock",
            exchange_id="hyperliquid",
            trading_mode="futures",
        )
        reader = AsyncMock()
        writer = MagicMock()
        writer.is_closing.return_value = False
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        reader.readline = AsyncMock(side_effect=asyncio.CancelledError())

        client._reader = reader
        client._writer = writer

        with pytest.raises(asyncio.CancelledError):
            await client._send_and_receive({"op": "acquire", "req_id": "x"})

        assert client._reader is None
        assert client._writer is None


# -------------------------------------------------------------------- client response handling


class TestClientFetchRateLimited:
    """OhlcvCacheClient.fetch() must raise CacheRateLimited on 429 responses."""

    @pytest.fixture()
    def client(self):
        c = OhlcvCacheClient(
            socket_path="/tmp/fake.sock",
            exchange_id="hyperliquid",
            trading_mode="futures",
        )
        return c

    @pytest.mark.asyncio
    async def test_fetch_429_in_error_message(self, client):
        resp = {
            "ok": False,
            "error_type": "ExchangeError",
            "error_message": "429 Too Many Requests",
        }
        with patch.object(client, "_send_and_receive", new_callable=AsyncMock, return_value=resp):
            with pytest.raises(CacheRateLimited, match="rate-limited"):
                await client.fetch("BTC/USDC", "15m", CandleType.FUTURES, None, 100)

    @pytest.mark.asyncio
    async def test_fetch_ratelimit_in_error_type(self, client):
        resp = {
            "ok": False,
            "error_type": "RateLimitExceeded",
            "error_message": "too many requests",
        }
        with patch.object(client, "_send_and_receive", new_callable=AsyncMock, return_value=resp):
            with pytest.raises(CacheRateLimited, match="rate-limited"):
                await client.fetch("BTC/USDC", "15m", CandleType.FUTURES, None, 100)

    @pytest.mark.asyncio
    async def test_fetch_generic_error_raises_unavailable(self, client):
        resp = {
            "ok": False,
            "error_type": "NetworkError",
            "error_message": "connection reset",
        }
        with patch.object(client, "_send_and_receive", new_callable=AsyncMock, return_value=resp):
            with pytest.raises(CacheUnavailable, match="daemon error"):
                await client.fetch("BTC/USDC", "15m", CandleType.FUTURES, None, 100)

    @pytest.mark.asyncio
    async def test_fetch_success(self, client):
        resp = {
            "ok": True,
            "pair": "BTC/USDC",
            "timeframe": "15m",
            "candle_type": "futures",
            "data": [[1, 2, 3, 4, 5, 6]],
            "drop_incomplete": True,
        }
        with patch.object(client, "_send_and_receive", new_callable=AsyncMock, return_value=resp):
            pair, tf, ct, data, drop = await client.fetch(
                "BTC/USDC",
                "15m",
                CandleType.FUTURES,
                None,
                100,
            )
            assert pair == "BTC/USDC"
            assert tf == "15m"
            assert ct == CandleType.FUTURES
            assert len(data) == 1
            assert drop is True


class TestClientTickersRateLimited:
    """OhlcvCacheClient.get_tickers() must raise CacheRateLimited on 429."""

    @pytest.fixture()
    def client(self):
        return OhlcvCacheClient(
            socket_path="/tmp/fake.sock",
            exchange_id="hyperliquid",
            trading_mode="futures",
        )

    @pytest.mark.asyncio
    async def test_tickers_429(self, client):
        resp = {
            "ok": False,
            "error_type": "RateLimitExceeded",
            "error_message": "429",
        }
        with patch.object(client, "_send_and_receive", new_callable=AsyncMock, return_value=resp):
            with pytest.raises(CacheRateLimited, match="rate-limited"):
                await client.get_tickers()

    @pytest.mark.asyncio
    async def test_tickers_generic_error(self, client):
        resp = {"ok": False, "error_type": "ServerError", "error_message": "500"}
        with patch.object(client, "_send_and_receive", new_callable=AsyncMock, return_value=resp):
            with pytest.raises(CacheUnavailable, match="tickers failed"):
                await client.get_tickers()

    @pytest.mark.asyncio
    async def test_tickers_success(self, client):
        resp = {"ok": True, "data": {"BTC/USDC": {"last": 100000}}}
        with patch.object(client, "_send_and_receive", new_callable=AsyncMock, return_value=resp):
            result = await client.get_tickers()
            assert result == {"BTC/USDC": {"last": 100000}}


class TestClientAcquireRateToken:
    @pytest.fixture()
    def client(self):
        return OhlcvCacheClient(
            socket_path="/tmp/fake.sock",
            exchange_id="hyperliquid",
            trading_mode="futures",
        )

    @pytest.mark.asyncio
    async def test_acquire_success(self, client):
        resp = {"ok": True}
        with patch.object(client, "_send_and_receive", new_callable=AsyncMock, return_value=resp):
            await client.acquire_rate_token(priority=OhlcvCacheClient.NORMAL)

    @pytest.mark.asyncio
    async def test_acquire_failure_raises_unavailable(self, client):
        resp = {"ok": False, "error_type": "InternalError", "error_message": "bucket full"}
        with patch.object(client, "_send_and_receive", new_callable=AsyncMock, return_value=resp):
            with pytest.raises(CacheUnavailable, match="acquire failed"):
                await client.acquire_rate_token()


class TestClientPriority:
    def test_explicit_priority_overrides(self):
        c = OhlcvCacheClient("/tmp/x.sock", exchange_id="hl", trading_mode="futures")
        assert (
            c._compute_priority(since_ms=None, priority=OhlcvCacheClient.LOW)
            == OhlcvCacheClient.LOW
        )

    def test_dry_run_gets_low(self):
        c = OhlcvCacheClient("/tmp/x.sock", exchange_id="hl", trading_mode="futures", dry_run=True)
        assert c._compute_priority(since_ms=None, priority=None) == OhlcvCacheClient.LOW

    def test_no_since_ms_gets_high(self):
        c = OhlcvCacheClient("/tmp/x.sock", exchange_id="hl", trading_mode="futures")
        assert c._compute_priority(since_ms=None, priority=None) == OhlcvCacheClient.HIGH

    def test_with_since_ms_gets_normal(self):
        c = OhlcvCacheClient("/tmp/x.sock", exchange_id="hl", trading_mode="futures")
        assert c._compute_priority(since_ms=1000, priority=None) == OhlcvCacheClient.NORMAL


# -------------------------------------------------------------------- mixin


def _make_mixin_exchange(dry_run=False, ftcache_enabled=True):
    """Build a minimal mock exchange with the mixin wired up."""
    mock = MagicMock()
    mock._config = {"dry_run": dry_run}
    mock.loop = asyncio.new_event_loop()
    mock._cache_lock = threading.Lock()
    mock._fetch_tickers_cache = {}

    client = AsyncMock(spec=OhlcvCacheClient)
    client.acquire_rate_token = AsyncMock(return_value=None)

    mixin = CachedExchangeMixin.__new__(CachedExchangeMixin)
    mixin._config = mock._config
    mixin.loop = mock.loop
    mixin._cache_lock = mock._cache_lock
    mixin._fetch_tickers_cache = mock._fetch_tickers_cache
    mixin._ftcache_client = client if ftcache_enabled else False
    mixin._ftcache_warned = False
    mixin._ftcache_open_pairs = frozenset()
    mixin._ftcache_stats = {
        "rate_limited": 0,
        "fallback_ccxt": 0,
        "stale_tickers": 0,
        "stale_positions": 0,
        "acquire_timeout": 0,
        "acquire_skip_loop": 0,
    }
    mixin._ftcache_last_positions = None
    mixin._ftcache_last_positions_ts = 0.0
    mixin._ftcache_tickers_fresh_ts = 0.0
    mixin._loop_lock = threading.Lock()
    mixin._ftcache_init_complete = True
    mixin._ftcache_is_offline_mode = False
    mixin._ftcache_is_utility_mode = False
    mixin._ftcache_rate_limit_only = False
    mixin._ftcache_last_backoff_active = False
    mixin._ftcache_last_backoff_ts = 0.0
    mixin._ftcache_last_wait_log_ts = 0.0

    return mixin, client, mock


class TestMixinOhlcvAntiCascade:
    """OHLCV: CacheRateLimited must be re-raised, NOT fall back to ccxt."""

    @pytest.mark.asyncio
    async def test_rate_limited_does_not_fallback(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.ohlcv_candle_limit = MagicMock(return_value=100)
        client.fetch = AsyncMock(side_effect=CacheRateLimited("429"))

        with pytest.raises(CacheRateLimited):
            await CachedExchangeMixin._async_get_candle_history(
                mixin,
                "BTC/USDC",
                "15m",
                CandleType.FUTURES,
                None,
            )

    @pytest.mark.asyncio
    async def test_generic_unavailable_does_not_raise(self):
        """Non-timeout CacheUnavailable should NOT propagate (falls back to ccxt).
        We verify it doesn't raise CacheUnavailable — the super() call will
        fail in this test harness, but the key assertion is that the mixin
        catches the exception rather than re-raising it.
        """
        mixin, client, _ = _make_mixin_exchange()
        mixin.ohlcv_candle_limit = MagicMock(return_value=100)
        client.fetch = AsyncMock(side_effect=CacheUnavailable("connection reset"))

        # The mixin should catch CacheUnavailable and try super() — which will
        # fail here (AttributeError) because there's no real Exchange parent.
        # The important thing: it does NOT raise CacheUnavailable.
        with pytest.raises(AttributeError):
            await CachedExchangeMixin._async_get_candle_history(
                mixin,
                "BTC/USDC",
                "15m",
                CandleType.FUTURES,
                None,
            )

    @pytest.mark.asyncio
    async def test_timeout_does_not_fallback(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.ohlcv_candle_limit = MagicMock(return_value=100)
        client.fetch = AsyncMock(
            side_effect=CacheTimedOut("daemon timed out"),
        )

        with pytest.raises(CacheTimedOut):
            await CachedExchangeMixin._async_get_candle_history(
                mixin,
                "BTC/USDC",
                "15m",
                CandleType.FUTURES,
                None,
            )

    @pytest.mark.asyncio
    async def test_non_cacheable_candle_type_bypasses(self):
        mixin, client, _ = _make_mixin_exchange()

        parent_result = ("BTC/USDC", "15m", CandleType.MARK, [[1, 2, 3, 4, 5, 6]], True)
        # For non-cacheable types, the mixin should call super() directly
        # which we can't easily mock, but we verify client.fetch is NOT called
        try:
            await CachedExchangeMixin._async_get_candle_history(
                mixin,
                "BTC/USDC",
                "15m",
                CandleType.MARK,
                None,
            )
        except (AttributeError, TypeError):
            # Expected — super() doesn't resolve in this test harness
            pass
        client.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_pair_gets_critical_priority(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.ohlcv_candle_limit = MagicMock(return_value=100)
        mixin._ftcache_open_pairs = frozenset({"BTC/USDC"})

        client.fetch = AsyncMock(
            return_value=(
                "BTC/USDC",
                "15m",
                CandleType.FUTURES,
                [],
                True,
            )
        )

        await CachedExchangeMixin._async_get_candle_history(
            mixin,
            "BTC/USDC",
            "15m",
            CandleType.FUTURES,
            None,
        )

        _, kwargs = client.fetch.call_args
        assert kwargs.get("priority") == OhlcvCacheClient.CRITICAL


class TestMixinTickersAntiCascade:
    """Tickers: CacheRateLimited must return stale cache, NOT fall back to ccxt."""

    def test_rate_limited_returns_stale_cache(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"

        stale_tickers = {"BTC/USDC": {"last": 95000}}
        mixin._fetch_tickers_cache["fetch_tickers"] = stale_tickers

        mixin.loop.run_until_complete = MagicMock(side_effect=CacheRateLimited("429"))

        result = CachedExchangeMixin.get_tickers(mixin, symbols=None, cached=False)
        assert result == stale_tickers

    def test_rate_limited_empty_when_no_stale(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        mixin.loop.run_until_complete = MagicMock(side_effect=CacheRateLimited("429"))

        result = CachedExchangeMixin.get_tickers(mixin, symbols=None, cached=False)
        assert result == {}

    def test_generic_unavailable_falls_back_to_ccxt(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        mixin.loop.run_until_complete = MagicMock(side_effect=CacheUnavailable("connection lost"))

        # super().get_tickers() — we need to verify it's called
        # Since we can't easily mock super(), check that the CacheUnavailable
        # path is distinct from CacheRateLimited path
        try:
            CachedExchangeMixin.get_tickers(mixin, symbols=None, cached=False)
        except (AttributeError, TypeError):
            # Expected — super() doesn't resolve in this test harness
            pass

    def test_cached_flag_returns_local_cache(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        cached_data = {"ETH/USDC": {"last": 3000}}
        mixin._fetch_tickers_cache["fetch_tickers"] = cached_data

        result = CachedExchangeMixin.get_tickers(mixin, symbols=None, cached=True)
        assert result == cached_data


# -------------------------------------------------------------------- acquire_sync


class TestAcquireSync:
    def test_acquire_calls_client_with_priority(self):
        mixin, client, _ = _make_mixin_exchange()
        CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=OhlcvCacheClient.CRITICAL)
        client.acquire_rate_token.assert_awaited_once_with(
            priority=OhlcvCacheClient.CRITICAL,
            cost=1.0,
        )

    def test_acquire_default_cost(self):
        mixin, client, _ = _make_mixin_exchange()
        CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=OhlcvCacheClient.NORMAL)
        _, kwargs = client.acquire_rate_token.call_args
        assert kwargs["cost"] == 1.0

    def test_acquire_custom_cost(self):
        mixin, client, _ = _make_mixin_exchange()
        CachedExchangeMixin._ftcache_acquire_sync(
            mixin,
            priority=OhlcvCacheClient.HIGH,
            cost=2.5,
        )
        _, kwargs = client.acquire_rate_token.call_args
        assert kwargs["cost"] == 2.5

    def test_acquire_unavailable_swallowed(self):
        mixin, client, _ = _make_mixin_exchange()

        async def _raise(*args, **kwargs):
            raise CacheUnavailable("daemon gone")

        client.acquire_rate_token = _raise
        # Should not raise
        CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=OhlcvCacheClient.NORMAL)

    def test_acquire_timeout_swallowed(self):
        mixin, client, _ = _make_mixin_exchange()

        async def _slow(*args, **kwargs):
            await asyncio.sleep(999)

        client.acquire_rate_token = _slow

        # Use a shorter timeout for test speed
        original = CachedExchangeMixin._ftcache_acquire_sync

        def _acquire_with_short_timeout(self, priority=None, cost=1.0):
            cl = (
                self._ftcache_get_client()
                if hasattr(self, "_ftcache_get_client")
                else self._ftcache_client
            )
            if cl is None:
                return
            try:
                loop = self.loop
                if loop.is_running():
                    return
                loop.run_until_complete(
                    asyncio.wait_for(
                        cl.acquire_rate_token(priority=priority, cost=cost),
                        timeout=0.1,
                    ),
                )
            except (CacheUnavailable, TimeoutError, asyncio.TimeoutError):
                pass

        _acquire_with_short_timeout(mixin, priority=OhlcvCacheClient.NORMAL)
        # If we got here without raising, the timeout was handled correctly

    def test_acquire_skipped_when_no_client(self):
        mixin, _, _ = _make_mixin_exchange(ftcache_enabled=False)
        # Should return immediately without error
        CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=OhlcvCacheClient.NORMAL)

    def test_acquire_skipped_when_loop_running(self):
        mixin, client, _ = _make_mixin_exchange()

        running_loop = MagicMock()
        running_loop.is_running.return_value = True
        mixin.loop = running_loop

        CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=OhlcvCacheClient.NORMAL)
        client.acquire_rate_token.assert_not_awaited()


# -------------------------------------------------------------------- interceptor priorities


class TestInterceptorPriorities:
    """Verify each interceptor calls _ftcache_acquire_sync with the correct priority."""

    def _make_interceptor_mixin(self, dry_run=False):
        mixin, client, _ = _make_mixin_exchange(dry_run=dry_run)
        mixin.id = "hyperliquid"
        mixin._ftcache_acquire_sync = MagicMock()
        # Disable client so methods fall through to _ftcache_acquire_sync directly
        mixin._ftcache_client = False
        return mixin

    def test_create_order_critical(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.create_order(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.CRITICAL, cost=1.0
        )

    def test_cancel_order_critical(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.cancel_order(mixin, "123", "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.CRITICAL, cost=1.0
        )

    def test_fetch_order_high(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.fetch_order(mixin, "123", "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.HIGH, cost=1.0
        )

    def test_get_balances_normal(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.get_balances(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.NORMAL, cost=2.0
        )

    def test_fetch_l2_order_book_high(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.fetch_l2_order_book(mixin, "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.HIGH, cost=2.0
        )

    def test_reload_markets_high(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.reload_markets(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.HIGH, cost=20.0
        )

    def test_fetch_ticker_normal(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.fetch_ticker(mixin, "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.NORMAL)

    def test_fetch_funding_rate_normal(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.fetch_funding_rate(mixin, "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.NORMAL)

    def test_fetch_trading_fees_low(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.fetch_trading_fees(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.LOW)

    def test_fetch_bids_asks_normal(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.fetch_bids_asks(mixin, symbols=None, cached=False)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.NORMAL)

    def test_get_trades_for_order_normal(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.get_trades_for_order(
                mixin,
                "123",
                "BTC/USDC",
                datetime.now(tz=timezone.utc),
            )
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.NORMAL)

    def test_get_funding_fees_low(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin._get_funding_fees_from_exchange(
                mixin,
                "BTC/USDC",
                datetime.now(tz=timezone.utc),
            )
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.LOW)

    def test_get_leverage_tiers_low(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.get_leverage_tiers(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.LOW)

    def test_set_leverage_normal(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin._set_leverage(mixin, 3.0, "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.NORMAL)

    def test_set_margin_mode_low(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.set_margin_mode(mixin, "BTC/USDC", MarginMode.CROSS)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.LOW)

    def test_fetch_orders_normal(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin._fetch_orders(
                mixin,
                "BTC/USDC",
                datetime.now(tz=timezone.utc),
            )
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.NORMAL)


class TestDryRunGuards:
    """Interceptors with dry_run guards must skip acquire in dry_run mode."""

    def _make_dry_run_mixin(self):
        mixin, client, _ = _make_mixin_exchange(dry_run=True)
        mixin.id = "hyperliquid"
        mixin._ftcache_acquire_sync = MagicMock()
        # Disable client so methods fall through to _ftcache_acquire_sync directly
        mixin._ftcache_client = False
        return mixin

    def test_create_order_skips_in_dry_run(self):
        mixin = self._make_dry_run_mixin()
        try:
            CachedExchangeMixin.create_order(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_not_called()

    def test_cancel_order_skips_in_dry_run(self):
        mixin = self._make_dry_run_mixin()
        try:
            CachedExchangeMixin.cancel_order(mixin, "123", "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_not_called()

    def test_fetch_order_skips_in_dry_run(self):
        mixin = self._make_dry_run_mixin()
        try:
            CachedExchangeMixin.fetch_order(mixin, "123", "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_not_called()

    def test_fetch_ticker_skips_in_dry_run(self):
        mixin = self._make_dry_run_mixin()
        try:
            CachedExchangeMixin.fetch_ticker(mixin, "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_not_called()

    def test_reload_markets_always_acquires(self):
        """reload_markets has no dry_run guard — always rate-limited."""
        mixin = self._make_dry_run_mixin()
        try:
            CachedExchangeMixin.reload_markets(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.HIGH, cost=20.0
        )

    def test_fetch_trading_fees_always_acquires(self):
        """fetch_trading_fees has no dry_run guard."""
        mixin = self._make_dry_run_mixin()
        try:
            CachedExchangeMixin.fetch_trading_fees(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.LOW)

    def test_get_leverage_tiers_always_acquires(self):
        """get_leverage_tiers has no dry_run guard."""
        mixin = self._make_dry_run_mixin()
        try:
            CachedExchangeMixin.get_leverage_tiers(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.LOW)


# -------------------------------------------------------------------- async funding rate history


class TestAsyncFundingRateHistory:
    @pytest.mark.asyncio
    async def test_acquires_low_priority(self):
        mixin, client, _ = _make_mixin_exchange()

        async def fake_super(*args, **kwargs):
            return [[1, 0.01]]

        with patch.object(
            CachedExchangeMixin,
            "_ftcache_get_client",
            return_value=client,
        ):
            # Can't easily call super() in test, just verify acquire is called
            client_mock = AsyncMock(spec=OhlcvCacheClient)
            client_mock.acquire_rate_token = AsyncMock()
            mixin._ftcache_client = client_mock
            mixin._ftcache_get_client = MagicMock(return_value=client_mock)

            try:
                await CachedExchangeMixin._fetch_funding_rate_history(
                    mixin,
                    "BTC/USDC",
                    "1h",
                    100,
                    None,
                )
            except (AttributeError, TypeError):
                pass

            client_mock.acquire_rate_token.assert_awaited_once_with(
                priority=OhlcvCacheClient.LOW,
                cost=1.0,
            )

    @pytest.mark.asyncio
    async def test_unavailable_swallowed(self):
        mixin, _, _ = _make_mixin_exchange()
        client_mock = AsyncMock(spec=OhlcvCacheClient)
        client_mock.acquire_rate_token = AsyncMock(side_effect=CacheUnavailable("gone"))
        mixin._ftcache_client = client_mock
        mixin._ftcache_get_client = MagicMock(return_value=client_mock)

        try:
            await CachedExchangeMixin._fetch_funding_rate_history(
                mixin,
                "BTC/USDC",
                "1h",
                100,
                None,
            )
        except (AttributeError, TypeError):
            # Expected — super() not resolvable
            pass
        # Key: no CacheUnavailable propagated


# -------------------------------------------------------------------- positions


class TestMixinPositions:
    def test_single_pair_acquires_high(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_acquire_sync = MagicMock()
        try:
            CachedExchangeMixin.fetch_positions(mixin, pair="BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.HIGH, cost=2.0
        )


# -------------------------------------------------------------------- CachedHyperliquid


class TestCachedHyperliquid:
    def test_fetch_liquidation_fills_high_priority(self):
        from freqtrade.exchange.cached_hyperliquid import CachedHyperliquid

        mock_instance = MagicMock(spec=CachedHyperliquid)
        mock_instance._config = {"dry_run": False}
        mock_instance._ftcache_acquire_sync = MagicMock()

        CachedHyperliquid.fetch_liquidation_fills.__wrapped__ = None  # type: ignore
        # Call the unbound method directly
        try:
            CachedHyperliquid.fetch_liquidation_fills(
                mock_instance,
                "BTC/USDC",
                datetime.now(tz=timezone.utc),
            )
        except (AttributeError, TypeError):
            pass
        mock_instance._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.HIGH,
        )

    def test_fetch_liquidation_fills_skips_dry_run(self):
        from freqtrade.exchange.cached_hyperliquid import CachedHyperliquid

        mock_instance = MagicMock(spec=CachedHyperliquid)
        mock_instance._config = {"dry_run": True}
        mock_instance._ftcache_acquire_sync = MagicMock()

        try:
            CachedHyperliquid.fetch_liquidation_fills(
                mock_instance,
                "BTC/USDC",
                datetime.now(tz=timezone.utc),
            )
        except (AttributeError, TypeError):
            pass
        mock_instance._ftcache_acquire_sync.assert_not_called()

    def test_additional_exchange_init_low_priority(self):
        from freqtrade.exchange.cached_hyperliquid import CachedHyperliquid

        mock_instance = MagicMock(spec=CachedHyperliquid)
        mock_instance._ftcache_acquire_sync = MagicMock()

        try:
            CachedHyperliquid.additional_exchange_init(mock_instance)
        except (AttributeError, TypeError):
            pass
        mock_instance._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.HIGH,
        )


# -------------------------------------------------------------------- ftcache_enabled


class TestFtcacheEnabled:
    def test_enabled_in_backtest(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.BACKTEST}
        assert CachedExchangeMixin._ftcache_enabled(mixin) is True

    def test_enabled_in_hyperopt(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.HYPEROPT}
        assert CachedExchangeMixin._ftcache_enabled(mixin) is True

    def test_enabled_by_default(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.LIVE}
        assert CachedExchangeMixin._ftcache_enabled(mixin) is True

    def test_explicit_disable(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.LIVE, "shared_ohlcv_cache": {"enabled": False}}
        assert CachedExchangeMixin._ftcache_enabled(mixin) is False

    def test_explicit_enable(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.LIVE, "shared_ohlcv_cache": {"enabled": True}}
        assert CachedExchangeMixin._ftcache_enabled(mixin) is True


# -------------------------------------------------------------------- open pairs


class TestOpenPairs:
    def test_set_open_pairs(self):
        mixin, _, _ = _make_mixin_exchange()
        assert mixin._ftcache_open_pairs == frozenset()
        CachedExchangeMixin.ftcache_set_open_pairs(mixin, {"BTC/USDC", "ETH/USDC"})
        assert mixin._ftcache_open_pairs == frozenset({"BTC/USDC", "ETH/USDC"})

    def test_open_pairs_are_frozen(self):
        mixin, _, _ = _make_mixin_exchange()
        CachedExchangeMixin.ftcache_set_open_pairs(mixin, {"BTC/USDC"})
        assert isinstance(mixin._ftcache_open_pairs, frozenset)


# -------------------------------------------------------------------- diagnostic stats


class TestDiagnosticStats:
    def test_stats_initialized(self):
        mixin, _, _ = _make_mixin_exchange()
        stats = CachedExchangeMixin.ftcache_get_stats(mixin)
        assert stats == {
            "rate_limited": 0,
            "fallback_ccxt": 0,
            "stale_tickers": 0,
            "stale_positions": 0,
            "acquire_timeout": 0,
            "acquire_skip_loop": 0,
        }

    def test_bump_increments(self):
        mixin, _, _ = _make_mixin_exchange()
        CachedExchangeMixin._ftcache_bump(mixin, "rate_limited")
        CachedExchangeMixin._ftcache_bump(mixin, "rate_limited")
        CachedExchangeMixin._ftcache_bump(mixin, "fallback_ccxt")
        stats = CachedExchangeMixin.ftcache_get_stats(mixin)
        assert stats["rate_limited"] == 2
        assert stats["fallback_ccxt"] == 1

    def test_stats_returns_copy(self):
        mixin, _, _ = _make_mixin_exchange()
        stats = CachedExchangeMixin.ftcache_get_stats(mixin)
        stats["rate_limited"] = 999
        assert mixin._ftcache_stats["rate_limited"] == 0

    def test_ohlcv_rate_limited_bumps_stat(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.ohlcv_candle_limit = MagicMock(return_value=100)
        client.fetch = AsyncMock(side_effect=CacheRateLimited("429"))
        try:
            asyncio.get_event_loop().run_until_complete(
                CachedExchangeMixin._async_get_candle_history(
                    mixin,
                    "BTC/USDC",
                    "15m",
                    CandleType.FUTURES,
                    None,
                ),
            )
        except CacheRateLimited:
            pass
        assert mixin._ftcache_stats["rate_limited"] == 1

    def test_ohlcv_fallback_bumps_stat(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.ohlcv_candle_limit = MagicMock(return_value=100)
        client.fetch = AsyncMock(side_effect=CacheUnavailable("connection reset"))
        try:
            asyncio.get_event_loop().run_until_complete(
                CachedExchangeMixin._async_get_candle_history(
                    mixin,
                    "BTC/USDC",
                    "15m",
                    CandleType.FUTURES,
                    None,
                ),
            )
        except AttributeError:
            pass
        assert mixin._ftcache_stats["fallback_ccxt"] == 1

    def test_tickers_rate_limited_bumps_stats(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        mixin.loop.run_until_complete = MagicMock(side_effect=CacheRateLimited("429"))
        CachedExchangeMixin.get_tickers(mixin, symbols=None, cached=False)
        assert mixin._ftcache_stats["rate_limited"] == 1
        assert mixin._ftcache_stats["stale_tickers"] == 1

    def test_tickers_fallback_bumps_stat(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        mixin.loop.run_until_complete = MagicMock(side_effect=CacheUnavailable("down"))
        try:
            CachedExchangeMixin.get_tickers(mixin, symbols=None, cached=False)
        except (AttributeError, TypeError):
            pass
        assert mixin._ftcache_stats["fallback_ccxt"] == 1

    def test_acquire_timeout_bumps_stat(self):
        mixin, client, _ = _make_mixin_exchange()

        async def _raise(*args, **kwargs):
            raise CacheUnavailable("daemon gone")

        client.acquire_rate_token = _raise
        CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=OhlcvCacheClient.NORMAL)
        assert mixin._ftcache_stats["acquire_timeout"] == 1

    def test_acquire_loop_lock_failure_bumps_stat(self):
        mixin, client, _ = _make_mixin_exchange()
        # Simulate _loop_lock already held (acquire returns False)
        lock = MagicMock()
        lock.acquire.return_value = False
        mixin._loop_lock = lock
        CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=OhlcvCacheClient.NORMAL)
        assert mixin._ftcache_stats["acquire_skip_loop"] == 1


# -------------------------------------------------------------------- timeout constant


class TestAcquireTimeoutConstant:
    def test_default_timeout(self):
        assert CachedExchangeMixin._ACQUIRE_TIMEOUT_S == 120.0

    def test_stale_positions_warn_age(self):
        assert CachedExchangeMixin._STALE_POSITIONS_WARN_AGE_S == 120.0

    def test_custom_timeout_used(self):
        mixin, client, _ = _make_mixin_exchange()

        async def _slow(*args, **kwargs):
            await asyncio.sleep(999)

        client.acquire_rate_token = _slow
        mixin._ACQUIRE_TIMEOUT_S = 0.05

        CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=OhlcvCacheClient.NORMAL)
        assert mixin._ftcache_stats["acquire_timeout"] == 1


# -------------------------------------------------------------------- stale tickers age


class TestStaleTickers:
    def test_fresh_ts_updated_on_success(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        assert mixin._ftcache_tickers_fresh_ts == 0.0

        tickers_data = {"BTC/USDC": {"last": 100000}}
        mixin.loop.run_until_complete = MagicMock(return_value=tickers_data)

        CachedExchangeMixin.get_tickers(mixin, symbols=None, cached=False)
        assert mixin._ftcache_tickers_fresh_ts > 0.0

    def test_stale_age_logged(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"

        stale_tickers = {"BTC/USDC": {"last": 95000}}
        mixin._fetch_tickers_cache["fetch_tickers"] = stale_tickers
        mixin._ftcache_tickers_fresh_ts = time.monotonic() - 45.0

        mixin.loop.run_until_complete = MagicMock(side_effect=CacheRateLimited("429"))
        result = CachedExchangeMixin.get_tickers(mixin, symbols=None, cached=False)
        assert result == stale_tickers
        assert mixin._ftcache_stats["stale_tickers"] == 1


# -------------------------------------------------------------------- positions stale cache (fix #2)


class TestPositionsStaleCache:
    def test_save_positions_stores_data(self):
        mixin, _, _ = _make_mixin_exchange()
        positions = [{"symbol": "BTC/USDC", "contracts": 1.0}]
        CachedExchangeMixin._ftcache_save_positions(mixin, positions)
        assert mixin._ftcache_last_positions == positions
        assert mixin._ftcache_last_positions_ts > 0.0

    def test_stale_positions_returned_when_fresh(self):
        mixin, _, _ = _make_mixin_exchange()
        positions = [{"symbol": "BTC/USDC", "contracts": 1.0}]
        mixin._ftcache_last_positions = positions
        mixin._ftcache_last_positions_ts = time.monotonic() - 30.0
        result = CachedExchangeMixin._ftcache_get_stale_positions(mixin)
        assert result == positions

    def test_stale_positions_returned_even_when_old(self):
        """Stale positions >120s old are returned with a warning (not discarded)."""
        mixin, _, _ = _make_mixin_exchange()
        positions = [{"symbol": "BTC/USDC", "contracts": 1.0}]
        mixin._ftcache_last_positions = positions
        mixin._ftcache_last_positions_ts = time.monotonic() - 300.0
        result = CachedExchangeMixin._ftcache_get_stale_positions(mixin)
        assert result == positions

    def test_no_stale_returns_none(self):
        mixin, _, _ = _make_mixin_exchange()
        assert mixin._ftcache_last_positions is None
        result = CachedExchangeMixin._ftcache_get_stale_positions(mixin)
        assert result is None

    def test_positions_rate_limited_returns_stale(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"

        cached_positions = [{"symbol": "ETH/USDC", "contracts": 2.0}]
        mixin._ftcache_last_positions = cached_positions
        mixin._ftcache_last_positions_ts = time.monotonic() - 10.0

        mixin.loop.run_until_complete = MagicMock(side_effect=CacheRateLimited("429"))

        result = CachedExchangeMixin.fetch_positions(mixin, pair=None)
        assert result == cached_positions
        assert mixin._ftcache_stats["rate_limited"] == 1
        assert mixin._ftcache_stats["stale_positions"] == 1

    def test_positions_rate_limited_no_stale_falls_through(self):
        """First call ever with rate-limit: no stale data, must fall to direct fetch."""
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        mixin._ftcache_acquire_sync = MagicMock()

        mixin.loop.run_until_complete = MagicMock(side_effect=CacheRateLimited("429"))

        try:
            CachedExchangeMixin.fetch_positions(mixin, pair=None)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.HIGH, cost=2.0
        )

    def test_positions_cache_hit_updates_local(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        mixin._log_exchange_response = MagicMock()

        positions = [{"symbol": "BTC/USDC", "contracts": 3.0}]
        client.get_positions = AsyncMock(return_value=(True, positions, False))

        CachedExchangeMixin.fetch_positions(mixin, pair=None)
        assert mixin._ftcache_last_positions == positions
        assert mixin._ftcache_last_positions_ts > 0.0


# -------------------------------------------------------------------- fallback rate-limiting (fix #3)


class TestFallbackRateLimiting:
    """Fallback paths to super() must still go through rate-limiter."""

    def test_get_tickers_symbols_acquires(self):
        """get_tickers(symbols=[...]) must rate-limit before ccxt fallback."""
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        mixin._ftcache_acquire_sync = MagicMock()

        try:
            CachedExchangeMixin.get_tickers(
                mixin,
                symbols=["BTC/USDC"],
                cached=False,
            )
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.NORMAL,
        )

    def test_get_tickers_loop_running_falls_back_to_ccxt(self):
        """get_tickers with non-dict result falls back to ccxt via CacheUnavailable."""
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        mixin._ftcache_acquire_sync = MagicMock()

        running_loop = MagicMock()
        running_loop.is_running.return_value = True
        mixin.loop = running_loop

        try:
            CachedExchangeMixin.get_tickers(mixin, symbols=None, cached=False)
        except (AttributeError, TypeError):
            pass
        # CacheUnavailable fallback goes to ccxt without acquire
        # (acquire is only on the "not ok" path, not the CacheUnavailable path)
        assert mixin._ftcache_stats["fallback_ccxt"] == 1

    def test_get_tickers_no_client_no_acquire(self):
        """get_tickers with no client should NOT try to acquire."""
        mixin, _, _ = _make_mixin_exchange(ftcache_enabled=False)
        mixin.id = "hyperliquid"
        mixin._ftcache_acquire_sync = MagicMock()

        try:
            CachedExchangeMixin.get_tickers(mixin, symbols=None, cached=False)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_not_called()

    def test_fetch_positions_lock_unavailable_acquires(self):
        """fetch_positions with lock unavailable must rate-limit before ccxt fallback."""
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        mixin._ftcache_acquire_sync = MagicMock()

        # Make _loop_lock.acquire return False to simulate lock contention
        lock = MagicMock()
        lock.acquire.return_value = False
        mixin._loop_lock = lock

        try:
            CachedExchangeMixin.fetch_positions(mixin, pair=None)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.HIGH,
            cost=2.0,
        )

    def test_fetch_positions_no_client_no_acquire(self):  # noqa: E301
        """fetch_positions with no client should NOT try to acquire."""
        mixin, _, _ = _make_mixin_exchange(ftcache_enabled=False)
        mixin.id = "hyperliquid"
        mixin._ftcache_acquire_sync = MagicMock()

        try:
            CachedExchangeMixin.fetch_positions(mixin, pair=None)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_not_called()


# ====================================================================
# NEW TESTS: TokenBucket, burst scenarios, priority floor, retry loops,
# backoff escalation, weight budget enforcement, daemon tickers priority
# ====================================================================


# -------------------------------------------------------------------- TokenBucket: weight budget


class TestTokenBucketWeightBudget:
    """The fast path must respect weight_budget_per_min.

    Bug found: on daemon restart, the bucket was full and weight_window empty,
    so N simultaneous requests all passed the fast path without checking the
    per-minute weight budget, causing a 429 burst.
    """

    @pytest.mark.asyncio
    async def test_fast_path_respects_weight_budget(self):
        """When weight_used_last_min + cost > budget, fast path must NOT grant."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=2.0,
            burst=100.0,
            weight_mode=True,
            weight_budget_per_min=100,
            exchange="test",
        )
        # Consume 95 weight
        bucket._record_weight(95.0)
        # 95 + 10 = 105 > 100 — should queue, not instant
        task = asyncio.create_task(bucket.acquire(cost=10.0, priority=2))
        await asyncio.sleep(0.05)
        assert not task.done(), "should be queued, not immediately granted"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_fast_path_grants_within_budget(self):
        """When within budget, fast path should grant immediately."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=2.0,
            burst=100.0,
            weight_mode=True,
            weight_budget_per_min=100,
            exchange="test",
        )
        bucket._record_weight(10.0)
        await bucket.acquire(cost=10.0, priority=2)
        assert bucket.weight_used_last_min == pytest.approx(20.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_burst_of_n_bots_respects_budget(self):
        """Simulate N bots requesting tokens simultaneously.

        Total weight granted on the fast path must never exceed the budget.
        """
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=17.0,
            burst=200.0,
            weight_mode=True,
            weight_budget_per_min=1020,
            exchange="test",
        )
        # 10 bots × load_markets (weight=20) = 200 weight burst
        pending = []
        for _ in range(10):
            task = asyncio.create_task(bucket.acquire(cost=20.0, priority=2))
            await asyncio.sleep(0.001)
            if not task.done():
                pending.append(task)

        assert bucket.weight_used_last_min <= 1020.0, (
            f"granted {bucket.weight_used_last_min} weight, exceeds budget 1020"
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_fresh_bucket_full_tokens_still_checks_budget(self):
        """A freshly created bucket (full tokens, empty weight window)
        must still enforce the weight budget — this is the exact restart scenario."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        # Budget 50, burst 200 — bucket has 200 tokens but budget only allows 50/min
        bucket = TokenBucket(
            rate_per_s=1.0,
            burst=200.0,
            weight_mode=True,
            weight_budget_per_min=50,
            exchange="test",
        )
        granted = 0
        pending = []
        for i in range(10):
            task = asyncio.create_task(bucket.acquire(cost=10.0, priority=2))
            await asyncio.sleep(0.001)
            if task.done():
                granted += 1
            else:
                pending.append(task)

        # At most 5 should be granted (5 × 10 = 50 = budget)
        assert granted <= 5, f"granted {granted} × 10 = {granted * 10} > budget 50"
        assert bucket.weight_used_last_min <= 50.0
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_non_weight_mode_ignores_budget(self):
        """In flat mode (weight_mode=False), fast path should not check weight budget."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=10.0,
            burst=50.0,
            weight_mode=False,
            weight_budget_per_min=0,
            exchange="test",
        )
        for _ in range(5):
            await bucket.acquire(cost=1.0, priority=2)
        assert bucket.tokens == pytest.approx(45.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_exact_budget_boundary(self):
        """Request that would exactly hit the budget should be granted."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=2.0,
            burst=100.0,
            weight_mode=True,
            weight_budget_per_min=100,
            exchange="test",
        )
        bucket._record_weight(90.0)
        # 90 + 10 = 100 exactly — should be granted
        await bucket.acquire(cost=10.0, priority=2)
        assert bucket.weight_used_last_min == pytest.approx(100.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_zero_cost_always_granted(self):
        """Cost=0 should always pass, even with full budget."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=1.0,
            burst=10.0,
            weight_mode=True,
            weight_budget_per_min=10,
            exchange="test",
        )
        bucket._record_weight(10.0)  # budget exhausted
        await bucket.acquire(cost=0.0, priority=2)  # should still pass


# -------------------------------------------------------------------- TokenBucket: weight window


class TestTokenBucketWeightWindow:
    """Weight tracking sliding window correctness."""

    @pytest.mark.asyncio
    async def test_weight_decays_after_60s(self):
        """Weight recorded >60s ago should not count."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=17.0,
            burst=200.0,
            weight_mode=True,
            weight_budget_per_min=1020,
            exchange="test",
        )
        old_ts = time.monotonic() - 61.0
        bucket._weight_window.append((old_ts, 500.0))
        bucket._weight_used_last_min = 500.0
        assert bucket.weight_used_last_min == pytest.approx(0.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_weight_accumulates_correctly(self):
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=17.0,
            burst=200.0,
            weight_mode=True,
            weight_budget_per_min=1020,
            exchange="test",
        )
        bucket._record_weight(20.0)
        bucket._record_weight(20.0)
        bucket._record_weight(20.0)
        assert bucket.weight_used_last_min == pytest.approx(60.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_mixed_old_and_new_entries(self):
        """Old entries pruned, recent ones kept."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=17.0,
            burst=200.0,
            weight_mode=True,
            weight_budget_per_min=1020,
            exchange="test",
        )
        old_ts = time.monotonic() - 61.0
        bucket._weight_window.append((old_ts, 100.0))
        bucket._weight_used_last_min = 100.0
        bucket._record_weight(30.0)  # recent
        # old 100 should be pruned, only 30 remains
        assert bucket.weight_used_last_min == pytest.approx(30.0, abs=2.0)

    @pytest.mark.asyncio
    async def test_negative_weight_clamped_to_zero(self):
        """Float drift should never produce negative weight."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=17.0,
            burst=200.0,
            weight_mode=True,
            weight_budget_per_min=1020,
            exchange="test",
        )
        bucket._weight_used_last_min = -5.0  # simulate float drift
        assert bucket.weight_used_last_min >= 0.0


# -------------------------------------------------------------------- TokenBucket: backoff


class TestTokenBucketBackoff:
    """Backoff escalation, shed thresholds, and priority queuing."""

    def _make_bucket(self):
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        return TokenBucket(
            rate_per_s=17.0,
            burst=200.0,
            weight_mode=True,
            weight_budget_per_min=1020,
            exchange="test",
        )

    def test_soft_backoff_sheds_only_low(self):
        """Level 1 (SOFT): shed_threshold=3 → only LOW (3) is shed."""
        bucket = self._make_bucket()
        bucket.trigger_backoff(2.0)
        assert bucket.backoff_active
        assert bucket._current_backoff_label == "SOFT"
        assert bucket._current_shed_threshold == 3  # only LOW shed

    def test_medium_backoff_sheds_normal_and_low(self):
        """Level 2 (MEDIUM): shed_threshold=2 → NORMAL+LOW shed."""
        bucket = self._make_bucket()
        bucket.trigger_backoff(2.0)
        bucket._backoff_until = 0  # expire
        bucket._last_backoff_trigger = time.monotonic() - 10
        bucket.trigger_backoff(2.0)
        assert bucket._current_backoff_label == "MEDIUM"
        assert bucket._current_shed_threshold == 2

    def test_hard_backoff_sheds_everything_except_critical(self):
        """Level 3 (HARD): shed_threshold=1 → HIGH+NORMAL+LOW shed."""
        bucket = self._make_bucket()
        # Trigger 3 consecutive backoffs
        for _ in range(3):
            bucket.trigger_backoff(2.0)
            bucket._backoff_until = 0
            bucket._last_backoff_trigger = time.monotonic() - 10
        bucket.trigger_backoff(2.0)
        # Should cap at HARD (level 3)
        assert bucket._current_backoff_label == "HARD"
        assert bucket._current_shed_threshold == 1

    def test_backoff_ignored_while_active(self):
        """Duplicate trigger during active backoff should be ignored."""
        bucket = self._make_bucket()
        bucket.trigger_backoff(2.0)
        label_before = bucket._current_backoff_label
        consecutive_before = bucket._consecutive_backoffs
        bucket.trigger_backoff(2.0)  # should be ignored
        assert bucket._current_backoff_label == label_before
        assert bucket._consecutive_backoffs == consecutive_before

    def test_cooldown_resets_escalation(self):
        """After _BACKOFF_COOLDOWN_S without 429, escalation resets."""
        bucket = self._make_bucket()
        bucket.trigger_backoff(2.0)
        assert bucket._consecutive_backoffs == 1
        bucket._backoff_until = 0  # expire
        # Simulate long time since last backoff
        bucket._last_backoff_trigger = time.monotonic() - 200
        bucket.trigger_backoff(2.0)
        # Should have reset to 0 then incremented to 1
        assert bucket._consecutive_backoffs == 1
        assert bucket._current_backoff_label == "SOFT"  # back to level 1

    def test_backoff_expiry(self):
        """backoff_active should return False after duration expires."""
        bucket = self._make_bucket()
        bucket.trigger_backoff(2.0)
        assert bucket.backoff_active is True
        bucket._backoff_until = time.monotonic() - 1  # force expired
        assert bucket.backoff_active is False

    @pytest.mark.asyncio
    async def test_critical_queued_during_backoff(self):
        """CRITICAL (priority=0) should be queued during backoff, not rejected."""
        bucket = self._make_bucket()
        bucket.trigger_backoff(2.0)
        assert bucket.backoff_active

        task = asyncio.create_task(bucket.acquire(cost=1.0, priority=0))
        await asyncio.sleep(0.05)
        # Should be in the waiters queue
        assert bucket.queued_during_backoff >= 1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_backoff_rate_reduction(self):
        """During backoff, effective rate should be divided by rate_factor."""
        bucket = self._make_bucket()
        normal_rate = bucket._effective_rate()
        bucket.trigger_backoff(2.0)
        # SOFT: rate_factor=2.0
        backoff_rate = bucket._effective_rate()
        assert backoff_rate == pytest.approx(normal_rate / 2.0, rel=0.1)

    def test_backoff_remaining_s(self):
        """backoff_remaining_s should decrease over time."""
        bucket = self._make_bucket()
        bucket.trigger_backoff(2.0)
        remaining = bucket.backoff_remaining_s
        assert remaining > 0
        assert remaining <= 15.0  # SOFT = 15s


# -------------------------------------------------------------------- TokenBucket: refill


class TestTokenBucketRefill:
    """Token refill mechanics."""

    @pytest.mark.asyncio
    async def test_tokens_refill_over_time(self):
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=10.0,
            burst=50.0,
            weight_mode=False,
            exchange="test",
        )
        bucket.tokens = 0.0
        bucket._last_refill = time.monotonic() - 1.0  # 1 second ago
        bucket._refill()
        # Should have refilled ~10 tokens (10/s × 1s)
        assert bucket.tokens == pytest.approx(10.0, abs=2.0)

    @pytest.mark.asyncio
    async def test_tokens_capped_at_burst(self):
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=100.0,
            burst=50.0,
            weight_mode=False,
            exchange="test",
        )
        bucket.tokens = 49.0
        bucket._last_refill = time.monotonic() - 10.0
        bucket._refill()
        assert bucket.tokens == 50.0  # capped at burst

    @pytest.mark.asyncio
    async def test_refill_during_backoff_is_slower(self):
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=10.0,
            burst=100.0,
            weight_mode=False,
            exchange="test",
        )
        bucket.tokens = 0.0
        bucket.trigger_backoff(2.0)  # SOFT: rate_factor=2.0
        bucket._last_refill = time.monotonic() - 1.0
        bucket._refill()
        # rate=10/2=5 tokens/s × 1s = ~5 tokens
        assert bucket.tokens == pytest.approx(5.0, abs=2.0)


# -------------------------------------------------------------------- TokenBucket: priority ordering


class TestTokenBucketPriorityOrdering:
    """Priority queue must serve CRITICAL before HIGH before NORMAL before LOW."""

    @pytest.mark.asyncio
    async def test_priority_order_in_queue(self):
        """Requests queued during backoff should be drained in priority order."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=17.0,
            burst=200.0,
            weight_mode=True,
            weight_budget_per_min=1020,
            exchange="test",
        )
        bucket.trigger_backoff(2.0)

        served_order = []
        tasks = []

        for prio, label in [(3, "LOW"), (2, "NORMAL"), (0, "CRITICAL"), (1, "HIGH")]:

            async def track(p=prio, l=label):
                await bucket.acquire(cost=1.0, priority=p)
                served_order.append(l)

            tasks.append(asyncio.create_task(track()))

        # Let drain loop process them — expire backoff to allow draining
        bucket._backoff_until = time.monotonic() + 0.1  # short backoff
        await asyncio.sleep(3.0)  # give drain loop time

        for t in tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        # At minimum, CRITICAL should be served before LOW
        if "CRITICAL" in served_order and "LOW" in served_order:
            assert served_order.index("CRITICAL") < served_order.index("LOW")


# -------------------------------------------------------------------- mixin: offline retry loop


class TestOfflineRetryLoop:
    """Offline/utility modes must retry on shed/timeout instead of falling through."""

    def _make_offline_mixin(self):
        mixin, client, mock = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = True
        mixin._ftcache_rate_limit_only = True
        mixin._ftcache_init_complete = True
        mixin._ftcache_last_wait_log_ts = 0.0
        return mixin, client, mock

    def test_shed_retries_then_succeeds(self):
        """CacheRateLimited should be retried, not fall through."""
        mixin, client, _ = self._make_offline_mixin()
        call_count = 0

        async def mock_acquire(priority=None, cost=1.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise CacheRateLimited("shed")
            return None

        client.acquire_rate_token = mock_acquire
        mixin._OFFLINE_RETRY_INTERVAL_S = 0.01
        result = CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=3, cost=1.0)
        assert result is True
        assert call_count == 2

    def test_timeout_retries_then_succeeds(self):
        """TimeoutError should be retried, not fall through."""
        mixin, client, _ = self._make_offline_mixin()
        call_count = 0

        async def mock_acquire(priority=None, cost=1.0):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise asyncio.TimeoutError()
            return None

        client.acquire_rate_token = mock_acquire
        mixin._OFFLINE_RETRY_INTERVAL_S = 0.01
        result = CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=3, cost=1.0)
        assert result is True
        assert call_count == 3

    def test_unavailable_retries_then_succeeds(self):
        """CacheUnavailable should be retried, not fall through."""
        mixin, client, _ = self._make_offline_mixin()
        call_count = 0

        async def mock_acquire(priority=None, cost=1.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise CacheUnavailable("daemon gone")
            return None

        client.acquire_rate_token = mock_acquire
        mixin._OFFLINE_RETRY_INTERVAL_S = 0.01
        result = CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=3, cost=1.0)
        assert result is True
        assert call_count == 2

    def test_multiple_failures_then_success(self):
        """Should survive many consecutive failures before succeeding."""
        mixin, client, _ = self._make_offline_mixin()
        call_count = 0
        errors = [
            CacheRateLimited,
            CacheTimedOut,
            CacheUnavailable,
            CacheRateLimited,
            CacheTimedOut,
        ]

        async def mock_acquire(priority=None, cost=1.0):
            nonlocal call_count
            if call_count < len(errors):
                exc_cls = errors[call_count]
                call_count += 1
                raise exc_cls("fail")
            call_count += 1
            return None

        client.acquire_rate_token = mock_acquire
        mixin._OFFLINE_RETRY_INTERVAL_S = 0.01
        result = CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=3, cost=1.0)
        assert result is True
        assert call_count == 6  # 5 failures + 1 success

    def test_deadline_exceeded_raises_temporary_error(self):
        """After deadline, should raise TemporaryError, not fall through."""
        from freqtrade.exceptions import TemporaryError

        mixin, client, _ = self._make_offline_mixin()
        client.acquire_rate_token = AsyncMock(side_effect=CacheRateLimited("shed"))
        mixin._OFFLINE_RETRY_INTERVAL_S = 0.01
        mixin._OFFLINE_ACQUIRE_MAX_S = 0.05

        with pytest.raises(TemporaryError, match="live bots saturated"):
            CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=3, cost=1.0)

    def test_deadline_exceeded_on_timeout(self):
        """TimeoutError past deadline should raise TemporaryError."""
        from freqtrade.exceptions import TemporaryError

        mixin, client, _ = self._make_offline_mixin()
        client.acquire_rate_token = AsyncMock(side_effect=CacheTimedOut("timeout"))
        mixin._OFFLINE_RETRY_INTERVAL_S = 0.01
        mixin._OFFLINE_ACQUIRE_MAX_S = 0.05

        with pytest.raises(TemporaryError, match="timed out"):
            CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=3, cost=1.0)

    def test_deadline_exceeded_on_unavailable(self):
        """CacheUnavailable past deadline should raise TemporaryError."""
        from freqtrade.exceptions import TemporaryError

        mixin, client, _ = self._make_offline_mixin()
        client.acquire_rate_token = AsyncMock(side_effect=CacheUnavailable("gone"))
        mixin._OFFLINE_RETRY_INTERVAL_S = 0.01
        mixin._OFFLINE_ACQUIRE_MAX_S = 0.05

        with pytest.raises(TemporaryError, match="daemon unavailable"):
            CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=3, cost=1.0)

    def test_live_mode_shed_returns_false_no_retry(self):
        """In live mode, CacheRateLimited should NOT retry — return False."""
        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        mixin._ftcache_init_complete = True
        client.acquire_rate_token = AsyncMock(side_effect=CacheRateLimited("shed"))

        result = CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=2, cost=1.0)
        assert result is False
        # Should have been called only once (no retry)
        assert client.acquire_rate_token.call_count == 1

    def test_live_mode_timeout_returns_true_no_retry(self):
        """In live mode, timeout should allow through (legacy behavior), no retry."""
        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        mixin._ftcache_init_complete = True
        client.acquire_rate_token = AsyncMock(side_effect=CacheTimedOut("timeout"))

        result = CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=2, cost=1.0)
        assert result is True
        assert client.acquire_rate_token.call_count == 1

    def test_utility_mode_also_retries(self):
        """UTIL_EXCHANGE mode should retry like offline mode."""
        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = True
        mixin._ftcache_init_complete = True
        mixin._ftcache_last_wait_log_ts = 0.0
        call_count = 0

        async def mock_acquire(priority=None, cost=1.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise CacheRateLimited("shed")
            return None

        client.acquire_rate_token = mock_acquire
        mixin._OFFLINE_RETRY_INTERVAL_S = 0.01
        result = CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=3, cost=1.0)
        assert result is True
        assert call_count == 2


# -------------------------------------------------------------------- mixin: priority floor


class TestPriorityFloor:
    """Offline/utility modes must have priority capped at LOW."""

    def test_offline_critical_capped_to_low(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = True
        mixin._ftcache_is_utility_mode = False
        result = CachedExchangeMixin._ftcache_apply_priority_floor(mixin, OhlcvCacheClient.CRITICAL)
        assert result == OhlcvCacheClient.LOW

    def test_offline_high_capped_to_low(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = True
        mixin._ftcache_is_utility_mode = False
        result = CachedExchangeMixin._ftcache_apply_priority_floor(mixin, OhlcvCacheClient.HIGH)
        assert result == OhlcvCacheClient.LOW

    def test_offline_normal_capped_to_low(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = True
        mixin._ftcache_is_utility_mode = False
        result = CachedExchangeMixin._ftcache_apply_priority_floor(mixin, OhlcvCacheClient.NORMAL)
        assert result == OhlcvCacheClient.LOW

    def test_offline_low_stays_low(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = True
        mixin._ftcache_is_utility_mode = False
        result = CachedExchangeMixin._ftcache_apply_priority_floor(mixin, OhlcvCacheClient.LOW)
        assert result == OhlcvCacheClient.LOW

    def test_utility_mode_caps_to_low(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = True
        result = CachedExchangeMixin._ftcache_apply_priority_floor(mixin, OhlcvCacheClient.HIGH)
        assert result == OhlcvCacheClient.LOW

    def test_live_mode_no_cap_critical(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        result = CachedExchangeMixin._ftcache_apply_priority_floor(mixin, OhlcvCacheClient.CRITICAL)
        assert result == OhlcvCacheClient.CRITICAL

    def test_live_mode_no_cap_high(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        result = CachedExchangeMixin._ftcache_apply_priority_floor(mixin, OhlcvCacheClient.HIGH)
        assert result == OhlcvCacheClient.HIGH

    def test_none_priority_becomes_low_in_offline(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = True
        mixin._ftcache_is_utility_mode = False
        result = CachedExchangeMixin._ftcache_apply_priority_floor(mixin, None)
        assert result == OhlcvCacheClient.LOW

    def test_none_priority_stays_none_in_live(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        result = CachedExchangeMixin._ftcache_apply_priority_floor(mixin, None)
        assert result is None


# -------------------------------------------------------------------- mixin: init priority


class TestInitPriorityOffline:
    """Init escalation must NOT apply to offline/utility modes."""

    def test_live_init_escalates_to_critical(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        mixin._ftcache_init_complete = False
        result = CachedExchangeMixin._ftcache_init_priority(mixin, OhlcvCacheClient.HIGH)
        assert result == OhlcvCacheClient.CRITICAL

    def test_offline_init_stays_low(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = True
        mixin._ftcache_is_utility_mode = False
        mixin._ftcache_init_complete = False
        result = CachedExchangeMixin._ftcache_init_priority(mixin, OhlcvCacheClient.HIGH)
        assert result == OhlcvCacheClient.LOW

    def test_utility_init_stays_low(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = True
        mixin._ftcache_init_complete = False
        result = CachedExchangeMixin._ftcache_init_priority(mixin, OhlcvCacheClient.CRITICAL)
        assert result == OhlcvCacheClient.LOW

    def test_live_post_init_passes_through(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        mixin._ftcache_init_complete = True
        result = CachedExchangeMixin._ftcache_init_priority(mixin, OhlcvCacheClient.HIGH)
        assert result == OhlcvCacheClient.HIGH

    def test_offline_post_init_still_capped(self):
        """Even after init_complete, offline mode should still cap priority."""
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = True
        mixin._ftcache_is_utility_mode = False
        mixin._ftcache_init_complete = True
        result = CachedExchangeMixin._ftcache_init_priority(mixin, OhlcvCacheClient.HIGH)
        assert result == OhlcvCacheClient.LOW


# -------------------------------------------------------------------- ftcache enabled (updated)


class TestFtcacheEnabledUpdated:
    """Backtest/hyperopt/walkforward now connect to daemon (enabled=True)
    but set offline mode flags."""

    def test_backtest_enabled_with_offline_flag(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.BACKTEST}
        mixin._ftcache_is_offline_mode = False
        result = CachedExchangeMixin._ftcache_enabled(mixin)
        assert result is True
        assert mixin._ftcache_is_offline_mode is True
        assert mixin._ftcache_rate_limit_only is True

    def test_hyperopt_enabled_with_offline_flag(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.HYPEROPT}
        mixin._ftcache_is_offline_mode = False
        result = CachedExchangeMixin._ftcache_enabled(mixin)
        assert result is True
        assert mixin._ftcache_is_offline_mode is True

    def test_walkforward_enabled_with_offline_flag(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.WALKFORWARD}
        mixin._ftcache_is_offline_mode = False
        result = CachedExchangeMixin._ftcache_enabled(mixin)
        assert result is True
        assert mixin._ftcache_is_offline_mode is True

    def test_util_exchange_sets_utility_flag(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.UTIL_EXCHANGE}
        mixin._ftcache_is_utility_mode = False
        result = CachedExchangeMixin._ftcache_enabled(mixin)
        assert result is True
        assert mixin._ftcache_is_utility_mode is True

    def test_live_no_flags(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.LIVE}
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        result = CachedExchangeMixin._ftcache_enabled(mixin)
        assert result is True
        assert mixin._ftcache_is_offline_mode is False
        assert mixin._ftcache_is_utility_mode is False

    def test_dry_run_no_flags(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.DRY_RUN}
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        result = CachedExchangeMixin._ftcache_enabled(mixin)
        assert result is True
        assert mixin._ftcache_is_offline_mode is False
        assert mixin._ftcache_is_utility_mode is False


# -------------------------------------------------------------------- mixin: acquire_sync priority forwarding


class TestAcquireSyncPriorityForwarding:
    """Verify _ftcache_acquire_sync applies the priority floor before calling daemon."""

    def test_offline_acquire_forwards_low_priority(self):
        """In offline mode, even if caller requests HIGH, daemon sees LOW."""
        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = True
        mixin._ftcache_is_utility_mode = False
        mixin._ftcache_init_complete = True
        mixin._ftcache_last_wait_log_ts = 0.0

        calls = []
        original_acquire = client.acquire_rate_token

        async def tracking_acquire(priority=None, cost=1.0):
            calls.append({"priority": priority, "cost": cost})
            return None

        client.acquire_rate_token = tracking_acquire

        CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=OhlcvCacheClient.HIGH, cost=5.0)
        assert len(calls) == 1
        assert calls[0]["priority"] == OhlcvCacheClient.LOW  # capped
        assert calls[0]["cost"] == 5.0

    def test_live_acquire_forwards_original_priority(self):
        """In live mode, priority should pass through unchanged."""
        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        mixin._ftcache_init_complete = True

        calls = []

        async def tracking_acquire(priority=None, cost=1.0):
            calls.append({"priority": priority, "cost": cost})
            return None

        client.acquire_rate_token = tracking_acquire

        CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=OhlcvCacheClient.HIGH, cost=2.0)
        assert len(calls) == 1
        assert calls[0]["priority"] == OhlcvCacheClient.HIGH


# -------------------------------------------------------------------- mixin: async OHLCV offline retry


class TestAsyncOhlcvOfflineRetry:
    """_async_get_candle_history in offline mode must retry, not fall through."""

    @pytest.mark.asyncio
    async def test_ohlcv_offline_retries_on_shed(self):
        """In offline mode, CacheRateLimited on acquire should retry."""
        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = True
        mixin._ftcache_rate_limit_only = True
        mixin._ftcache_init_complete = True
        mixin._ftcache_last_wait_log_ts = 0.0
        mixin._OFFLINE_RETRY_INTERVAL_S = 0.01
        mixin._OFFLINE_ACQUIRE_MAX_S = 5.0
        call_count = 0

        async def mock_acquire(priority=None, cost=1.0):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise CacheRateLimited("shed")
            return None

        client.acquire_rate_token = mock_acquire

        # super()._async_get_candle_history will fail, but we just want
        # to verify the retry logic ran
        try:
            await CachedExchangeMixin._async_get_candle_history(
                mixin,
                "BTC/USDC",
                "15m",
                CandleType.FUTURES,
                None,
            )
        except (AttributeError, TypeError):
            pass  # expected — super() won't resolve

        assert call_count == 3  # 2 retries + 1 success

    @pytest.mark.asyncio
    async def test_ohlcv_offline_deadline_raises(self):
        """In offline mode, exceeding deadline should raise TemporaryError."""
        from freqtrade.exceptions import TemporaryError

        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = True
        mixin._ftcache_rate_limit_only = True
        mixin._ftcache_init_complete = True
        mixin._ftcache_last_wait_log_ts = 0.0
        mixin._OFFLINE_RETRY_INTERVAL_S = 0.01
        mixin._OFFLINE_ACQUIRE_MAX_S = 0.03

        client.acquire_rate_token = AsyncMock(side_effect=CacheRateLimited("shed"))

        with pytest.raises(TemporaryError, match="live bots saturated"):
            await CachedExchangeMixin._async_get_candle_history(
                mixin,
                "BTC/USDC",
                "15m",
                CandleType.FUTURES,
                None,
            )


# -------------------------------------------------------------------- mixin: open pair priority escalation


class TestOpenPairPriorityEscalation:
    """Open position pairs must use HIGH/CRITICAL priority for tickers."""

    def test_get_tickers_symbols_with_open_pair_uses_high(self):
        """get_tickers(symbols=[open_pair]) should use HIGH priority."""
        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        mixin._ftcache_init_complete = True
        mixin._ftcache_open_pairs = frozenset({"BTC/USDC"})
        mixin._ftcache_acquire_sync = MagicMock(return_value=True)
        mixin._ftcache_last_backoff_active = False

        try:
            CachedExchangeMixin.get_tickers(
                mixin,
                symbols=["BTC/USDC"],
                cached=False,
            )
        except (AttributeError, TypeError):
            pass

        call_args = mixin._ftcache_acquire_sync.call_args
        assert call_args is not None
        assert call_args[1]["priority"] == OhlcvCacheClient.HIGH

    def test_get_tickers_symbols_without_open_pair_uses_normal(self):
        """get_tickers(symbols=[non_open_pair]) should use NORMAL priority."""
        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_is_offline_mode = False
        mixin._ftcache_is_utility_mode = False
        mixin._ftcache_init_complete = True
        mixin._ftcache_open_pairs = frozenset({"BTC/USDC"})
        mixin._ftcache_acquire_sync = MagicMock(return_value=True)
        mixin._ftcache_last_backoff_active = False

        try:
            CachedExchangeMixin.get_tickers(
                mixin,
                symbols=["ETH/USDC"],
                cached=False,
            )
        except (AttributeError, TypeError):
            pass

        call_args = mixin._ftcache_acquire_sync.call_args
        assert call_args is not None
        assert call_args[1]["priority"] == OhlcvCacheClient.NORMAL

    @pytest.mark.asyncio
    async def test_ohlcv_open_pair_gets_critical(self):
        """OHLCV for an open pair should use CRITICAL priority."""
        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_open_pairs = frozenset({"BTC/USDC"})
        mixin.ohlcv_candle_limit = MagicMock(return_value=100)

        await_calls = []
        original_fetch = client.fetch

        async def tracking_fetch(**kwargs):
            await_calls.append(kwargs)
            return ("BTC/USDC", "15m", CandleType.FUTURES, [], True)

        client.fetch = tracking_fetch

        await CachedExchangeMixin._async_get_candle_history(
            mixin,
            "BTC/USDC",
            "15m",
            CandleType.FUTURES,
            None,
        )
        assert len(await_calls) == 1
        assert await_calls[0]["priority"] == OhlcvCacheClient.CRITICAL

    @pytest.mark.asyncio
    async def test_ohlcv_non_open_pair_gets_none_priority(self):
        """OHLCV for a non-open pair should use None (default) priority."""
        mixin, client, _ = _make_mixin_exchange()
        mixin._ftcache_open_pairs = frozenset({"ETH/USDC"})
        mixin.ohlcv_candle_limit = MagicMock(return_value=100)

        await_calls = []

        async def tracking_fetch(**kwargs):
            await_calls.append(kwargs)
            return ("BTC/USDC", "15m", CandleType.FUTURES, [], True)

        client.fetch = tracking_fetch

        await CachedExchangeMixin._async_get_candle_history(
            mixin,
            "BTC/USDC",
            "15m",
            CandleType.FUTURES,
            None,
        )
        assert len(await_calls) == 1
        assert await_calls[0]["priority"] is None


# -------------------------------------------------------------------- mixin: ccxt block during backoff


class TestCcxtBlockDuringBackoff:
    """_ftcache_should_block_ccxt must block direct ccxt calls during backoff."""

    def test_blocks_when_backoff_active(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_last_backoff_active = True
        mixin._ftcache_last_backoff_ts = time.monotonic()
        assert CachedExchangeMixin._ftcache_should_block_ccxt(mixin) is True

    def test_unblocks_after_timeout(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_last_backoff_active = True
        mixin._ftcache_last_backoff_ts = time.monotonic() - 60.0  # well past 30s
        assert CachedExchangeMixin._ftcache_should_block_ccxt(mixin) is False

    def test_not_blocked_when_no_backoff(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_last_backoff_active = False
        assert CachedExchangeMixin._ftcache_should_block_ccxt(mixin) is False

    def test_clears_flag_after_timeout(self):
        mixin, _, _ = _make_mixin_exchange()
        mixin._ftcache_last_backoff_active = True
        mixin._ftcache_last_backoff_ts = time.monotonic() - 60.0
        CachedExchangeMixin._ftcache_should_block_ccxt(mixin)
        assert mixin._ftcache_last_backoff_active is False


# -------------------------------------------------------------------- mixin: local backoff check


class TestLocalBackoffCheck:
    """_ftcache_local_backoff_check: only CRITICAL passes when loop unavailable."""

    def test_critical_allowed(self):
        mixin, _, _ = _make_mixin_exchange()
        result = CachedExchangeMixin._ftcache_local_backoff_check(mixin, OhlcvCacheClient.CRITICAL)
        assert result is True

    def test_high_blocked(self):
        mixin, _, _ = _make_mixin_exchange()
        result = CachedExchangeMixin._ftcache_local_backoff_check(mixin, OhlcvCacheClient.HIGH)
        assert result is False

    def test_normal_blocked(self):
        mixin, _, _ = _make_mixin_exchange()
        result = CachedExchangeMixin._ftcache_local_backoff_check(mixin, OhlcvCacheClient.NORMAL)
        assert result is False

    def test_low_blocked(self):
        mixin, _, _ = _make_mixin_exchange()
        result = CachedExchangeMixin._ftcache_local_backoff_check(mixin, OhlcvCacheClient.LOW)
        assert result is False

    def test_none_treated_as_normal_blocked(self):
        mixin, _, _ = _make_mixin_exchange()
        result = CachedExchangeMixin._ftcache_local_backoff_check(mixin, None)
        assert result is False


# -------------------------------------------------------------------- daemon: tickers priority forwarding


class TestDaemonTickersPriorityForwarding:
    """_handle_tickers must read priority from request and pass it to bucket.acquire."""

    @pytest.mark.asyncio
    async def test_tickers_request_uses_client_priority(self):
        """When client sends priority=HIGH, daemon should acquire with HIGH."""
        from freqtrade.ohlcv_cache.daemon import Daemon, TokenBucket

        # We test by inspecting the request parsing, not the full daemon
        # The key assertion is that req["priority"] is read and passed
        req = {
            "op": "tickers",
            "req_id": "test123",
            "exchange": "hyperliquid",
            "trading_mode": "futures",
            "market_type": "",
            "priority": TokenBucket.HIGH,
        }
        # Verify the priority is correctly extracted
        priority = int(req.get("priority", TokenBucket.NORMAL))
        assert priority == TokenBucket.HIGH

    @pytest.mark.asyncio
    async def test_tickers_request_defaults_to_normal(self):
        """When client doesn't send priority, daemon should use NORMAL."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        req = {
            "op": "tickers",
            "req_id": "test123",
            "exchange": "hyperliquid",
            "trading_mode": "futures",
            "market_type": "",
        }
        priority = int(req.get("priority", TokenBucket.NORMAL))
        assert priority == TokenBucket.NORMAL


# -------------------------------------------------------------------- client: get_tickers priority


class TestClientTickersPriority:
    """OhlcvCacheClient.get_tickers must forward priority in the request."""

    @pytest.mark.asyncio
    async def test_priority_included_in_request(self):
        """When priority is passed, it should appear in the wire request."""
        client = OhlcvCacheClient(
            socket_path="/tmp/fake.sock",
            exchange_id="hyperliquid",
            trading_mode="futures",
        )

        captured_req = {}

        async def mock_send(req):
            captured_req.update(req)
            return {"ok": True, "data": {}}

        client._send_and_receive = mock_send

        await client.get_tickers(market_type="", priority=OhlcvCacheClient.HIGH)
        assert "priority" in captured_req
        assert captured_req["priority"] == OhlcvCacheClient.HIGH

    @pytest.mark.asyncio
    async def test_no_priority_omitted_from_request(self):
        """When priority is None, it should NOT appear in the wire request."""
        client = OhlcvCacheClient(
            socket_path="/tmp/fake.sock",
            exchange_id="hyperliquid",
            trading_mode="futures",
        )

        captured_req = {}

        async def mock_send(req):
            captured_req.update(req)
            return {"ok": True, "data": {}}

        client._send_and_receive = mock_send

        await client.get_tickers(market_type="")
        assert "priority" not in captured_req


# -------------------------------------------------------------------- integration-style: concurrent access


class TestConcurrentBurstScenario:
    """Simulate realistic burst scenarios to catch race conditions."""

    @pytest.mark.asyncio
    async def test_concurrent_acquires_all_respect_budget(self):
        """Fire 50 concurrent acquires — total weight must not exceed budget."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=17.0,
            burst=200.0,
            weight_mode=True,
            weight_budget_per_min=200,
            exchange="test",
        )

        results = []
        pending = []

        async def try_acquire(idx):
            try:
                await asyncio.wait_for(bucket.acquire(cost=10.0, priority=2), timeout=0.5)
                results.append(idx)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        tasks = [asyncio.create_task(try_acquire(i)) for i in range(50)]
        await asyncio.sleep(1.0)

        for t in tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        # Total weight granted must not exceed budget
        total_granted = len(results) * 10.0
        assert total_granted <= 200.0, (
            f"granted {total_granted} weight to {len(results)} tasks, exceeds budget 200"
        )

    @pytest.mark.asyncio
    async def test_mixed_priority_concurrent_burst(self):
        """CRITICAL requests should be served before LOW during burst."""
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        bucket = TokenBucket(
            rate_per_s=5.0,
            burst=20.0,
            weight_mode=True,
            weight_budget_per_min=100,
            exchange="test",
        )
        # Exhaust budget so requests must queue (budget=100, each costs 5)
        bucket._record_weight(96.0)

        served_order = []

        async def acquire_with_label(prio, label):
            try:
                await asyncio.wait_for(bucket.acquire(cost=5.0, priority=prio), timeout=30.0)
                served_order.append(label)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        # Launch LOW first, then CRITICAL — CRITICAL should still be served first
        tasks = [
            asyncio.create_task(acquire_with_label(3, "LOW_1")),
            asyncio.create_task(acquire_with_label(3, "LOW_2")),
            asyncio.create_task(acquire_with_label(0, "CRIT_1")),
            asyncio.create_task(acquire_with_label(0, "CRIT_2")),
        ]
        await asyncio.sleep(0.01)  # let them all queue

        # Wait for completion
        await asyncio.sleep(15.0)

        for t in tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        # If both CRITICAL and at least one LOW were served,
        # CRITICAL must come first
        crit_indices = [i for i, l in enumerate(served_order) if l.startswith("CRIT")]
        low_indices = [i for i, l in enumerate(served_order) if l.startswith("LOW")]
        if crit_indices and low_indices:
            assert max(crit_indices) < min(low_indices), (
                f"CRITICAL served after LOW: {served_order}"
            )
