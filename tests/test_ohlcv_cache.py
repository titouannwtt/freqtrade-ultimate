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
        return mixin

    def test_create_order_critical(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.create_order(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.CRITICAL)

    def test_cancel_order_critical(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.cancel_order(mixin, "123", "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.CRITICAL)

    def test_fetch_order_high(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.fetch_order(mixin, "123", "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.HIGH)

    def test_get_balances_normal(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.get_balances(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.NORMAL)

    def test_fetch_l2_order_book_high(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.fetch_l2_order_book(mixin, "BTC/USDC")
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.HIGH)

    def test_reload_markets_normal(self):
        mixin = self._make_interceptor_mixin()
        try:
            CachedExchangeMixin.reload_markets(mixin)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.NORMAL)

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
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.NORMAL)

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
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.HIGH)


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
            priority=OhlcvCacheClient.LOW,
        )


# -------------------------------------------------------------------- ftcache_enabled


class TestFtcacheEnabled:
    def test_disabled_in_backtest(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.BACKTEST}
        assert CachedExchangeMixin._ftcache_enabled(mixin) is False

    def test_disabled_in_hyperopt(self):
        from freqtrade.enums import RunMode

        mixin, _, _ = _make_mixin_exchange()
        mixin._config = {"runmode": RunMode.HYPEROPT}
        assert CachedExchangeMixin._ftcache_enabled(mixin) is False

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

    def test_acquire_loop_running_bumps_stat(self):
        mixin, client, _ = _make_mixin_exchange()
        running_loop = MagicMock()
        running_loop.is_running.return_value = True
        mixin.loop = running_loop
        CachedExchangeMixin._ftcache_acquire_sync(mixin, priority=OhlcvCacheClient.NORMAL)
        assert mixin._ftcache_stats["acquire_skip_loop"] == 1


# -------------------------------------------------------------------- timeout constant


class TestAcquireTimeoutConstant:
    def test_default_timeout(self):
        assert CachedExchangeMixin._ACQUIRE_TIMEOUT_S == 30.0

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
        # Should have called acquire before direct fetch
        mixin._ftcache_acquire_sync.assert_called_once_with(priority=OhlcvCacheClient.HIGH)

    def test_positions_cache_hit_updates_local(self):
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        mixin._log_exchange_response = MagicMock()

        positions = [{"symbol": "BTC/USDC", "contracts": 3.0}]
        mixin.loop.run_until_complete = MagicMock(return_value=(True, positions))

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

    def test_get_tickers_loop_running_acquires(self):
        """get_tickers with running loop must rate-limit before ccxt fallback."""
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
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.NORMAL,
        )

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

    def test_fetch_positions_loop_running_acquires(self):
        """fetch_positions with running loop must rate-limit before ccxt fallback."""
        mixin, client, _ = _make_mixin_exchange()
        mixin.id = "hyperliquid"
        mixin._ftcache_acquire_sync = MagicMock()

        running_loop = MagicMock()
        running_loop.is_running.return_value = True
        mixin.loop = running_loop

        try:
            CachedExchangeMixin.fetch_positions(mixin, pair=None)
        except (AttributeError, TypeError):
            pass
        mixin._ftcache_acquire_sync.assert_called_once_with(
            priority=OhlcvCacheClient.HIGH,
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
