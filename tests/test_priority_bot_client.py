"""Client side of ``shared_ohlcv_cache.priority_bot``: which call goes out in which band.

The table under test (``-1`` = PRIME):

======================================  ===============  ===============  =========
call                                    priority bot     other live bot   dry bot
======================================  ===============  ===============  =========
create_order / cancel_order             PRIME            CRITICAL         no token
create_stoploss, set_margin_mode,       PRIME            CRITICAL         no token
_set_leverage
fetch_order (order, stop follow-up)     PRIME in a       HIGH             no token
                                        trading call
order book of an entry / a position     PRIME in a       CRITICAL         LOW
adjustment (``get_rate``, ``get_rates``)  trading call
any other order book                    PRIME in a       HIGH             LOW
                                        trading call
positions, balances, ticker             PRIME in a       unchanged        LOW
                                        trading call
anything inside ``notification_call``   never PRIME,     as before        LOW
                                        never CRITICAL
======================================  ===============  ===============  =========
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from freqtrade.enums import MarginMode
from freqtrade.exceptions import TemporaryError
from freqtrade.exchange.common import _reacquire_rate_token, retrier
from freqtrade.ohlcv_cache import mixin as mixin_mod
from freqtrade.ohlcv_cache import trading_context
from freqtrade.ohlcv_cache.client import (
    CacheRateLimited,
    CacheTimedOut,
    CacheUnavailable,
    OhlcvCacheClient,
)
from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin, _LocalRateLimiter


PRIME = OhlcvCacheClient.PRIME
CRITICAL = OhlcvCacheClient.CRITICAL
HIGH = OhlcvCacheClient.HIGH
NORMAL = OhlcvCacheClient.NORMAL
LOW = OhlcvCacheClient.LOW


# --------------------------------------------------------------------------- fixtures


class _FakeBase:
    """Stands in for ``Exchange``: the REST calls the mixin forwards to."""

    def create_order(self, **kwargs):
        return {"id": "o1"}

    def cancel_order(self, order_id, pair, params=None):
        return {"id": order_id}

    def fetch_order(self, order_id, pair, params=None):
        return {"id": order_id}

    def fetch_l2_order_book(self, pair, limit=100):
        return {"bids": [[100.0, 1.0]], "asks": [[100.1, 1.0]]}

    def get_rate(self, pair, refresh, side, is_short, order_book=None, ticker=None):
        if order_book is None:
            order_book = self.fetch_l2_order_book(pair, 1)
        return order_book["bids"][0][0]

    def get_rates(self, pair, refresh, is_short):
        # Like Exchange.get_rates: ONE book read for the entry and the exit rate.
        book = self.fetch_l2_order_book(pair, 1)
        return (
            self.get_rate(pair, refresh, "entry", is_short, order_book=book),
            self.get_rate(pair, refresh, "exit", is_short, order_book=book),
        )

    def _set_leverage(self, leverage, pair=None, accept_fail=False):
        return None

    def set_margin_mode(self, pair, margin_mode, accept_fail=False, params=None):
        return None

    def create_stoploss(self, pair, amount, stop_price, order_types, side, leverage):
        # Exchange.create_stoploss calls _lev_prep -> set_margin_mode first.
        self.set_margin_mode(pair, MarginMode.ISOLATED)
        return {"id": "sl1"}

    def fetch_positions(self, pair=None, params=None):
        return []

    def get_balances(self, params=None):
        return {}

    def fetch_ticker(self, pair):
        return {"last": 1.0}


class _Exchange(CachedExchangeMixin, _FakeBase):
    pass


class _ExitBookOnlyExchange(_Exchange):
    """``get_rates`` where the entry is priced on the ticker and only the exit reads a book."""

    def get_rates(self, pair, refresh, is_short):
        return 1.0, self.get_rate(pair, refresh, "exit", is_short)


class _FakeClient:
    """Records the band of every token request, like the real client would send."""

    fleet_size = 0

    def __init__(self):
        self.calls: list[tuple[int | None, float]] = []
        self.reported: list[float] = []
        self.fail_with: Exception | None = None

    async def acquire_rate_token(self, priority=None, cost=1.0, reported_weight=0.0):
        self.calls.append((priority, cost))
        if reported_weight:
            self.reported.append(reported_weight)
        if self.fail_with is not None:
            raise self.fail_with


def _exchange(*, priority_bot=False, dry_run=False, client=None) -> tuple[_Exchange, _FakeClient]:
    ex = _Exchange.__new__(_Exchange)
    ex._config = {
        "dry_run": dry_run,
        "shared_ohlcv_cache": {"priority_bot": priority_bot},
    }
    ex.id = "hyperliquid"
    ex.loop = asyncio.new_event_loop()
    ex._loop_lock = threading.Lock()
    fake = client or _FakeClient()
    ex._ftcache_client = fake
    ex._ftcache_stats = {}
    ex._ftcache_init_complete = True
    ex._ftcache_is_dry_run_mode = dry_run
    ex._ftcache_open_pairs = frozenset()
    ex._ftcache_local_limiter = None
    return ex, fake


@pytest.fixture(autouse=True)
def _clean_thread_state():
    """Neither the call markers nor the retry memory may leak between tests."""
    trading_context._stack().clear()
    from freqtrade.ohlcv_cache import mixin

    mixin._LAST_ACQUIRE.value = None
    mixin._UNREPORTED.value = 0.0
    yield
    trading_context._stack().clear()


def _bands(fake: _FakeClient) -> list[int | None]:
    return [p for p, _ in fake.calls]


# --------------------------------------------------------------------------- trading_context


class TestTradingContext:
    def test_nothing_is_marked_by_default(self):
        assert not trading_context.is_trading_call()
        assert not trading_context.is_notification_call()

    def test_trading_call_marks_its_block_and_clears_on_exit(self):
        with trading_context.trading_call():
            assert trading_context.is_trading_call()
        assert not trading_context.is_trading_call()

    def test_marker_is_cleared_when_the_block_raises(self):
        with pytest.raises(RuntimeError), trading_context.trading_call():
            raise RuntimeError("boom")
        assert not trading_context.is_trading_call()

    def test_a_notification_inside_a_trading_call_is_not_trading(self):
        """A message is sent from inside execute_entry: the innermost marker wins."""
        with trading_context.trading_call():
            with trading_context.notification_call():
                assert trading_context.is_notification_call()
                assert not trading_context.is_trading_call()
            assert trading_context.is_trading_call()

    def test_other_threads_do_not_inherit_the_marker(self):
        seen = []
        with trading_context.trading_call():
            t = threading.Thread(target=lambda: seen.append(trading_context.is_trading_call()))
            t.start()
            t.join()
        assert seen == [False]

    def test_decorator_wraps_and_returns(self):
        @trading_context.trading
        def work(x):
            return trading_context.is_trading_call(), x

        assert work(3) == (True, 3)
        assert not trading_context.is_trading_call()
        assert work.__name__ == "work"


# --------------------------------------------------------------------------- the priority flag


class TestPriorityFlag:
    def test_flag_requires_a_live_bot(self):
        assert _exchange(priority_bot=True)[0]._ftcache_is_priority_bot is True
        assert _exchange(priority_bot=False)[0]._ftcache_is_priority_bot is False
        assert _exchange(priority_bot=True, dry_run=True)[0]._ftcache_is_priority_bot is False

    @pytest.mark.parametrize("attr", ["_ftcache_is_offline_mode", "_ftcache_is_utility_mode"])
    def test_flag_is_ignored_for_backtest_and_utility_processes(self, attr):
        ex, _ = _exchange(priority_bot=True)
        setattr(ex, attr, True)
        assert ex._ftcache_is_priority_bot is False

    def test_config_without_the_section_is_not_a_priority_bot(self):
        ex, _ = _exchange()
        ex._config = {"dry_run": False}
        assert ex._ftcache_is_priority_bot is False


# --------------------------------------------------------------------------- the table


class TestIntrinsicTradingCalls:
    """Orders, stops, leverage, margin: nothing but trading sends them."""

    @pytest.mark.parametrize(
        "priority_bot,expected",
        [(True, [PRIME] * 4), (False, [CRITICAL, CRITICAL, NORMAL, LOW])],
        ids=["priority", "other"],
    )
    @pytest.mark.parametrize("in_context", [False, True], ids=["bare", "in_trading_call"])
    def test_order_class_calls(self, priority_bot, expected, in_context):
        ex, fake = _exchange(priority_bot=priority_bot)
        ctx = trading_context.trading_call() if in_context else trading_context.notification_call()
        with ctx:
            ex.create_order(pair="BTC/USDC", side="buy")
            ex.cancel_order("o1", "BTC/USDC")
            ex._set_leverage(3.0, "BTC/USDC")
            ex.set_margin_mode("BTC/USDC", MarginMode.ISOLATED)
        # another bot keeps the bands it always had: orders CRITICAL, leverage NORMAL, margin LOW
        assert _bands(fake) == expected

    def test_create_stoploss_and_its_margin_preflight_are_both_prime(self):
        ex, fake = _exchange(priority_bot=True)
        ex.create_stoploss("BTC/USDC", 1.0, 90.0, {}, "sell", 1.0)
        assert _bands(fake) == [PRIME, PRIME]

    def test_create_stoploss_of_another_bot_keeps_its_bands(self):
        ex, fake = _exchange(priority_bot=False)
        ex.create_stoploss("BTC/USDC", 1.0, 90.0, {}, "sell", 1.0)
        assert _bands(fake) == [CRITICAL, LOW]

    @pytest.mark.parametrize("priority_bot", [True, False])
    def test_dry_run_takes_no_token(self, priority_bot):
        ex, fake = _exchange(priority_bot=priority_bot, dry_run=True)
        ex.create_order(pair="BTC/USDC")
        ex.cancel_order("o1", "BTC/USDC")
        ex.fetch_order("o1", "BTC/USDC")
        ex.create_stoploss("BTC/USDC", 1.0, 90.0, {}, "sell", 1.0)
        assert fake.calls == []


class TestContextualCalls:
    def test_fetch_order_is_prime_inside_a_trading_call_only(self):
        ex, fake = _exchange(priority_bot=True)
        ex.fetch_order("o1", "BTC/USDC")
        with trading_context.trading_call():
            ex.fetch_order("o1", "BTC/USDC")
        assert _bands(fake) == [HIGH, PRIME]

    def test_fetch_order_of_another_bot_is_high_everywhere(self):
        ex, fake = _exchange(priority_bot=False)
        ex.fetch_order("o1", "BTC/USDC")
        with trading_context.trading_call():
            ex.fetch_order("o1", "BTC/USDC")
        assert _bands(fake) == [HIGH, HIGH]

    def test_plain_book_read_is_high_for_all_but_prime_in_a_trading_call(self):
        ex, fake = _exchange(priority_bot=True)
        ex.fetch_l2_order_book("BTC/USDC", 20)
        with trading_context.trading_call():
            ex.fetch_l2_order_book("BTC/USDC", 20)
        assert _bands(fake) == [HIGH, PRIME]

    def test_book_costs_two_and_a_priority_bot_keeps_that_cost(self):
        ex, fake = _exchange(priority_bot=True)
        with trading_context.trading_call():
            ex.fetch_l2_order_book("BTC/USDC", 20)
        assert fake.calls == [(PRIME, 2.0)]

    @pytest.mark.parametrize(
        "side,expected_other", [("entry", HIGH), ("exit", HIGH)], ids=["entry", "exit"]
    )
    def test_priced_book_of_a_regular_bot(self, side, expected_other):
        ex, fake = _exchange(priority_bot=False)
        ex.get_rate("BTC/USDC", True, side, False)
        assert _bands(fake) == [expected_other]

    @pytest.mark.parametrize("side", ["entry", "exit"])
    def test_priced_book_of_a_priority_bot_in_a_trading_call(self, side):
        ex, fake = _exchange(priority_bot=True)
        with trading_context.trading_call():
            ex.get_rate("BTC/USDC", True, side, False)
        assert _bands(fake) == [PRIME]

    def test_position_adjustment_book_is_covered_through_get_rates(self):
        """DCA: ``check_and_call_adjust_trade_position`` prices through ``get_rates``,
        which reads the book directly, not through ``get_rate`` (review point 3)."""
        regular, fake = _exchange(priority_bot=False)
        regular.get_rates("BTC/USDC", True, False)
        assert _bands(fake) == [HIGH], "CRITICAL for an entry book is for priority bots only"

        prime, fake = _exchange(priority_bot=True)
        with trading_context.trading_call():
            prime.get_rates("BTC/USDC", True, False)
        assert _bands(fake) == [PRIME]

    def test_get_rates_does_not_make_a_later_plain_book_urgent(self):
        ex, fake = _exchange(priority_bot=False)
        ex.get_rates("BTC/USDC", True, False)
        fake.calls.clear()
        ex.fetch_l2_order_book("BTC/USDC", 20)
        assert _bands(fake) == [HIGH]

    def test_the_entry_book_is_critical_for_a_priority_bot_outside_a_trading_call(self):
        ex, fake = _exchange(priority_bot=True)
        ex.get_rate("BTC/USDC", True, "entry", False)
        ex.get_rates("BTC/USDC", True, False)
        assert _bands(fake) == [CRITICAL, CRITICAL]

    def test_an_exit_priced_inside_get_rates_is_not_marked_as_an_entry(self):
        """When only the exit side reads the book it must not inherit the entry flag."""
        ex, fake = _exchange(priority_bot=False)
        ex.__class__ = _ExitBookOnlyExchange
        ex.get_rates("BTC/USDC", True, False)
        assert _bands(fake) == [HIGH]

    def test_dry_bot_books_stay_low_whatever_the_flag_or_context(self):
        ex, fake = _exchange(priority_bot=True, dry_run=True)
        with trading_context.trading_call():
            ex.fetch_l2_order_book("BTC/USDC", 20)
            ex.get_rate("BTC/USDC", True, "entry", False)
        assert _bands(fake) == [LOW, LOW]

    def test_positions_fetch_by_pair_is_prime_in_a_trading_call(self):
        ex, fake = _exchange(priority_bot=True)
        ex._ftcache_save_positions = MagicMock()
        ex.fetch_positions(pair="BTC/USDC")
        with trading_context.trading_call():
            ex.fetch_positions(pair="BTC/USDC")
        assert _bands(fake) == [HIGH, PRIME]

    def test_authoritative_positions_read_is_prime_in_a_trading_call_else_critical(self):
        ex, fake = _exchange(priority_bot=True)
        ex._ftcache_save_positions = MagicMock()
        ex.fetch_positions_authoritative()
        with trading_context.trading_call():
            ex.fetch_positions_authoritative()
        assert _bands(fake) == [CRITICAL, PRIME]

    def test_balances_before_an_entry_are_prime_in_a_trading_call(self):
        ex, _ = _exchange(priority_bot=True)
        ex._ftcache_get_client = lambda: None  # no shared cache: straight to the token
        ex._ftcache_acquire_sync = MagicMock(return_value=True)
        ex.get_balances()
        with trading_context.trading_call():
            ex.get_balances()
        assert [c.kwargs["priority"] for c in ex._ftcache_acquire_sync.call_args_list] == [
            NORMAL,
            PRIME,
        ]

    def test_ticker_read_is_prime_in_a_trading_call_for_a_priority_bot(self):
        ex, _ = _exchange(priority_bot=True)
        ex._ftcache_get_client = lambda: None
        ex._ftcache_acquire_sync = MagicMock(return_value=True)
        ex._fetch_tickers_cache = {}
        ex._cache_lock = threading.Lock()
        ex.fetch_ticker("BTC/USDC")
        with trading_context.trading_call():
            ex.fetch_ticker("BTC/USDC")
        assert [c.kwargs["priority"] for c in ex._ftcache_acquire_sync.call_args_list] == [
            NORMAL,
            PRIME,
        ]


class TestNotificationsAreNeverTopPriority:
    @pytest.mark.parametrize("priority_bot", [True, False])
    @pytest.mark.parametrize("side", ["entry", "exit"])
    def test_a_rate_read_for_a_message_is_high_at_most(self, priority_bot, side):
        """Review point 4: ``_notify_enter`` and ``_notify_enter_cancel`` priced an entry
        (CRITICAL since 29541c04d) from inside execute_entry (PRIME for a priority bot)."""
        ex, fake = _exchange(priority_bot=priority_bot)
        with trading_context.trading_call(), trading_context.notification_call():
            ex.get_rate("BTC/USDC", False, side, False)
        assert _bands(fake) == [HIGH]

    def test_a_notification_does_not_make_the_next_trading_read_slow(self):
        ex, fake = _exchange(priority_bot=True)
        with trading_context.trading_call():
            with trading_context.notification_call():
                ex.get_rate("BTC/USDC", False, "entry", False)
            ex.get_rate("BTC/USDC", True, "entry", False)
        assert _bands(fake) == [HIGH, PRIME]


# --------------------------------------------------------------------------- never -1 unless asked


class TestNonPriorityBotNeverEmitsPrime:
    def test_a_realistic_sequence_stays_above_zero(self):
        ex, fake = _exchange(priority_bot=False)
        ex._ftcache_save_positions = MagicMock()
        with trading_context.trading_call():
            ex.get_rates("BTC/USDC", True, False)
            ex.get_rate("BTC/USDC", True, "entry", False)
            ex.get_rate("BTC/USDC", True, "exit", False)
            ex._set_leverage(2.0, "BTC/USDC")
            ex.set_margin_mode("BTC/USDC", MarginMode.ISOLATED)
            ex.create_order(pair="BTC/USDC")
            ex.create_stoploss("BTC/USDC", 1.0, 90.0, {}, "sell", 1.0)
            ex.fetch_order("o1", "BTC/USDC")
            ex.cancel_order("o1", "BTC/USDC")
            ex.fetch_positions(pair="BTC/USDC")
            ex.fetch_positions_authoritative()
        assert fake.calls and all(p >= CRITICAL for p in _bands(fake))

    def test_the_real_client_never_tags_or_registers_a_regular_bot(self):
        c = OhlcvCacheClient("/nonexistent", priority_bot=False)
        assert c.priority_bot is False


# --------------------------------------------------------------------------- the local limiter


class TestLocalLimiterLetsPrimeThrough:
    def test_prime_is_not_blocked_and_is_counted(self):
        limiter = _LocalRateLimiter(exchange_budget_per_min=1200.0, assumed_bots=100)
        limiter._window.append((time.monotonic(), limiter._budget * 10))  # budget exhausted
        started = time.monotonic()
        limiter.acquire(cost=2.0, priority=PRIME)
        assert time.monotonic() - started < 0.1
        assert limiter._window[-1][1] == 2.0

    def test_local_backoff_check_lets_prime_through_like_critical(self):
        ex, _ = _exchange()
        assert CachedExchangeMixin._ftcache_local_backoff_check(ex, PRIME) is True
        assert CachedExchangeMixin._ftcache_local_backoff_check(ex, HIGH) is False


# --------------------------------------------------------------------------- loop lock, shed


class TestUrgentCallsNeverLeaveUncounted:
    def test_a_prime_call_waits_the_normal_bound_for_the_loop_lock_then_books_locally(self):
        """After the bound an order goes out with its weight booked locally (not 30 s later)."""
        ex, fake = _exchange(priority_bot=True)
        ex._LOOP_LOCK_TIMEOUT_S = 0.3
        ex._loop_lock.acquire()  # a candle batch holds the loop
        try:
            started = time.monotonic()
            granted = ex._ftcache_acquire_sync(priority=PRIME, cost=2.0)
            waited = time.monotonic() - started
        finally:
            ex._loop_lock.release()
        assert granted is True
        assert 0.25 <= waited < 1.5
        assert fake.calls == []
        assert ex._ftcache_local_limiter._window[-1][1] == 2.0, "booked locally"

    def test_the_lock_bound_of_a_prime_call_is_the_regular_five_seconds(self):
        """Regression: PRIME used to wait 30 s (``_LOOP_LOCK_TIMEOUT_URGENT_S``) for the lock."""
        assert CachedExchangeMixin._LOOP_LOCK_TIMEOUT_S <= 5.0
        assert not hasattr(CachedExchangeMixin, "_LOOP_LOCK_TIMEOUT_URGENT_S")

    def test_weight_booked_without_a_token_is_reported_with_the_next_acquire(self):
        ex, fake = _exchange(priority_bot=True)
        ex._LOOP_LOCK_TIMEOUT_S = 0.05
        ex._loop_lock.acquire()
        try:
            ex._ftcache_acquire_sync(priority=PRIME, cost=2.0)
            ex._ftcache_acquire_sync(priority=PRIME, cost=3.0)
        finally:
            ex._loop_lock.release()
        assert fake.calls == [] and fake.reported == []
        ex._ftcache_acquire_sync(priority=PRIME, cost=1.0)
        assert fake.calls == [(PRIME, 1.0)]
        assert fake.reported == [5.0], "both skipped tokens, once"
        ex._ftcache_acquire_sync(priority=PRIME, cost=1.0)
        assert fake.reported == [5.0], "not reported twice"

    @pytest.mark.parametrize(
        "exc", [CacheRateLimited("shed"), CacheTimedOut("t")], ids=["shed", "timed_out"]
    )
    def test_reported_weight_is_not_sent_twice_when_the_daemon_answered_anyway(self, exc):
        """The daemon books ``reported_weight`` on arrival, before the token wait."""
        ex, fake = _exchange(priority_bot=True)
        ex._LOOP_LOCK_TIMEOUT_S = 0.05
        ex._loop_lock.acquire()
        try:
            ex._ftcache_acquire_sync(priority=PRIME, cost=4.0)  # skipped: 4 unreported
        finally:
            ex._loop_lock.release()
        fake.fail_with = exc
        ex._ftcache_acquire_sync(priority=PRIME, cost=1.0)  # carries the 4, daemon answers
        fake.fail_with = None
        ex._ftcache_acquire_sync(priority=PRIME, cost=1.0)
        assert fake.reported == [4.0], "reported once only"

    def test_reported_weight_is_kept_when_the_daemon_is_unreachable(self):
        ex, fake = _exchange(priority_bot=True)
        mixin_mod._UNREPORTED.value = 4.0
        fake.fail_with = CacheUnavailable("down")
        ex._ftcache_acquire_sync(priority=PRIME, cost=1.0)
        fake.fail_with = None
        ex._ftcache_acquire_sync(priority=PRIME, cost=1.0)
        assert fake.reported == [4.0, 4.0], "the first never reached the daemon"

    def test_the_lock_wait_of_a_prime_call_ends_as_soon_as_the_lock_frees(self):
        ex, fake = _exchange(priority_bot=True)
        ex._loop_lock.acquire()
        threading.Timer(0.2, ex._loop_lock.release).start()
        started = time.monotonic()
        assert ex._ftcache_acquire_sync(priority=PRIME, cost=1.0) is True
        assert time.monotonic() - started < 1.5
        assert _bands(fake) == [PRIME]

    def test_a_critical_call_of_a_regular_bot_keeps_the_historical_lock_handling(self):
        """CRITICAL is not PRIME: no local booking, the historical answer after the bound."""
        ex, _ = _exchange()
        ex._LOOP_LOCK_TIMEOUT_S = 0.05
        ex._loop_lock.acquire()
        try:
            assert ex._ftcache_acquire_sync(priority=CRITICAL, cost=1.0) is True
        finally:
            ex._loop_lock.release()
        assert ex._ftcache_local_limiter is None, "not booked: as before"
        assert mixin_mod._UNREPORTED.value == 0.0

    def test_a_non_urgent_call_still_gives_up_after_the_short_timeout(self):
        ex, _ = _exchange()
        ex._LOOP_LOCK_TIMEOUT_S = 0.1
        ex._loop_lock.acquire()
        try:
            started = time.monotonic()
            assert ex._ftcache_acquire_sync(priority=HIGH, cost=1.0) is False
        finally:
            ex._loop_lock.release()
        assert time.monotonic() - started < 1.0
        assert ex._ftcache_local_limiter is None, "not booked: the caller was told to skip"

    def test_a_shed_urgent_call_is_booked_locally_not_refused(self):
        ex, fake = _exchange(priority_bot=True)
        fake.fail_with = CacheRateLimited("shed")
        assert ex._ftcache_acquire_sync(priority=PRIME, cost=2.0) is True
        assert ex._ftcache_local_limiter._window[-1][1] == 2.0

    def test_a_shed_regular_call_still_reports_false(self):
        ex, fake = _exchange()
        fake.fail_with = CacheRateLimited("shed")
        assert ex._ftcache_acquire_sync(priority=HIGH, cost=2.0) is False

    @pytest.mark.parametrize("exc", [CacheTimedOut("t"), CacheUnavailable("down")])
    def test_timeouts_and_outages_were_already_booked_locally(self, exc):
        ex, fake = _exchange(priority_bot=True)
        fake.fail_with = exc
        assert ex._ftcache_acquire_sync(priority=PRIME, cost=2.0) is True
        assert ex._ftcache_local_limiter._window[-1][1] == 2.0


class TestRefusedTokensAreBookedByTheCallersThatGoAheadAnyway:
    """Review point 5: fetch_l2_order_book, _set_leverage and set_margin_mode ignored a False.

    Only for a priority bot: any other bot keeps what it always did (the call goes out, no
    local booking, so no new local-limiter sleep for it).
    """

    def test_book_read_after_a_refusal_is_booked_locally_by_a_priority_bot(self):
        ex, fake = _exchange(priority_bot=True)
        fake.fail_with = CacheRateLimited("shed")
        with trading_context.trading_call():
            ex.fetch_l2_order_book("BTC/USDC", 20)  # PRIME: refused, then goes ahead
        assert ex._ftcache_local_limiter._window[-1][1] == 2.0

    def test_book_read_after_a_refusal_is_left_alone_for_a_regular_bot(self):
        ex, fake = _exchange()
        fake.fail_with = CacheRateLimited("shed")
        ex.fetch_l2_order_book("BTC/USDC", 20)
        assert ex._ftcache_local_limiter is None, "no booking, hence no local sleep"
        assert ex._ftcache_book_cache is None, "nothing remembered either"

    def test_a_regular_bot_never_reuses_a_book(self):
        ex, fake = _exchange()
        ex.fetch_l2_order_book("BTC/USDC", 20)
        fake.fail_with = CacheRateLimited("shed")
        fake.calls.clear()
        calls = []
        base_read = _FakeBase.fetch_l2_order_book
        _FakeBase.fetch_l2_order_book = lambda self, pair, limit=100: (
            calls.append(pair) or base_read(self, pair, limit)
        )
        try:
            ex.fetch_l2_order_book("BTC/USDC", 20)
        finally:
            _FakeBase.fetch_l2_order_book = base_read
        assert calls == ["BTC/USDC"], "the read goes out, as it always did"

    def test_book_read_after_a_refusal_prefers_a_fresh_book_for_a_priority_bot(self):
        """A read outside a trading call (HIGH) is refusable; the fresh book stands in."""
        ex, fake = _exchange(priority_bot=True)
        first = ex.fetch_l2_order_book("BTC/USDC", 20)
        fake.fail_with = CacheRateLimited("shed")
        fake.calls.clear()
        again = ex.fetch_l2_order_book("BTC/USDC", 20)
        assert again is first, "a book read seconds ago stands in for the refused read"
        assert ex._ftcache_local_limiter is None

    def test_a_stale_book_is_not_reused(self):
        ex, fake = _exchange(priority_bot=True)
        mixin_mod._ENTRY_PRICING.active = True  # an entry book: CRITICAL, booked when refused
        try:
            ex.fetch_l2_order_book("BTC/USDC", 20)
            ts, depth, book = ex._ftcache_book_cache["BTC/USDC"]
            ex._ftcache_book_cache["BTC/USDC"] = (
                ts - mixin_mod._BOOK_REUSE_MAX_AGE_S - 1,
                depth,
                book,
            )
            fake.fail_with = CacheRateLimited("shed")
            ex.fetch_l2_order_book("BTC/USDC", 20)
        finally:
            mixin_mod._ENTRY_PRICING.active = False
        assert ex._ftcache_local_limiter is not None

    def test_a_shallow_book_does_not_stand_in_for_a_deeper_one(self):
        ex, fake = _exchange(priority_bot=True)
        mixin_mod._ENTRY_PRICING.active = True
        try:
            ex.fetch_l2_order_book("BTC/USDC", 5)
            fake.fail_with = CacheRateLimited("shed")
            ex.fetch_l2_order_book("BTC/USDC", 50)
        finally:
            mixin_mod._ENTRY_PRICING.active = False
        assert ex._ftcache_local_limiter is not None

    def test_a_refused_high_read_of_a_priority_bot_is_never_booked_in_the_sleeping_limiter(self):
        """A HIGH read outside a trading call would sleep up to 30 s in the local limiter."""
        ex, fake = _exchange(priority_bot=True)
        fake.fail_with = CacheRateLimited("shed")
        limiter = ex._ftcache_get_local_limiter()
        limiter.acquire(cost=limiter._budget * 10, priority=CRITICAL)  # budget exhausted
        started = time.monotonic()
        ex.fetch_l2_order_book("BTC/USDC", 20)  # HIGH, refused, local budget exhausted
        ex.fetch_order("o1", "BTC/USDC")  # HIGH too
        assert time.monotonic() - started < 1.0
        assert len(ex._ftcache_local_limiter._window) == 1, "nothing more booked"

    def test_hard_backoff_makes_a_priority_bot_reuse_a_recent_book(self):
        ex, fake = _exchange(priority_bot=True)
        with trading_context.trading_call():
            first = ex.fetch_l2_order_book("BTC/USDC", 20)
            ex._ftcache_last_backoff_active = True
            ex._ftcache_last_backoff_ts = time.monotonic()
            fake.calls.clear()
            again = ex.fetch_l2_order_book("BTC/USDC", 20)
        assert again is first and fake.calls == [], "no token spent in a HARD backoff"

    def test_hard_backoff_does_not_change_a_regular_bot(self):
        ex, fake = _exchange()
        ex.fetch_l2_order_book("BTC/USDC", 20)
        ex._ftcache_last_backoff_active = True
        ex._ftcache_last_backoff_ts = time.monotonic()
        fake.calls.clear()
        ex.fetch_l2_order_book("BTC/USDC", 20)
        assert _bands(fake) == [HIGH]

    @pytest.mark.parametrize("name", ["_set_leverage", "set_margin_mode"])
    def test_leverage_and_margin_calls_are_booked_by_a_priority_bot_when_refused(self, name):
        ex, _ = _exchange(priority_bot=True)
        ex._ftcache_acquire_sync = MagicMock(return_value=False)
        args = (3.0, "BTC/USDC") if name == "_set_leverage" else ("BTC/USDC", MarginMode.ISOLATED)
        getattr(ex, name)(*args)
        assert ex._ftcache_local_limiter._window[-1][1] == 1.0

    @pytest.mark.parametrize("name", ["_set_leverage", "set_margin_mode"])
    def test_leverage_and_margin_calls_of_a_regular_bot_are_not_booked_when_refused(self, name):
        ex, _ = _exchange()
        ex._ftcache_acquire_sync = MagicMock(return_value=False)
        args = (3.0, "BTC/USDC") if name == "_set_leverage" else ("BTC/USDC", MarginMode.ISOLATED)
        getattr(ex, name)(*args)
        assert ex._ftcache_local_limiter is None

    def test_a_granted_token_is_not_booked_twice(self):
        ex, _ = _exchange(priority_bot=True)
        ex._ftcache_acquire_sync = MagicMock(return_value=True)
        ex._set_leverage(3.0, "BTC/USDC")
        assert ex._ftcache_local_limiter is None


# --------------------------------------------------------------------------- retries


class _Flaky(_FakeBase):
    """The base ``Exchange`` method is retrier-decorated; the mixin sits in front of it."""

    failures = 1

    @retrier
    def create_order(self, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise TemporaryError("exchange hiccup")
        return {"id": "o1"}

    @retrier
    def fetch_l2_order_book(self, pair, limit=100):
        if self.failures > 0:
            self.failures -= 1
            raise TemporaryError("exchange hiccup")
        return {"bids": [[1.0, 1.0]], "asks": [[1.1, 1.0]]}


class _FlakyExchange(CachedExchangeMixin, _Flaky):
    pass


def _flaky(priority_bot: bool) -> tuple[_FlakyExchange, _FakeClient]:
    ex = _FlakyExchange.__new__(_FlakyExchange)
    ref, fake = _exchange(priority_bot=priority_bot)
    ex.__dict__.update(ref.__dict__)
    return ex, fake


class TestRetriesKeepTheirPriority:
    """Review point 2: ``_reacquire_rate_token`` re-acquired a default HIGH weight-1 token.

    Only a trading call of a priority bot hands its band to the retry.
    """

    def test_a_retried_order_of_a_priority_bot_stays_prime(self):
        ex, fake = _flaky(priority_bot=True)
        with trading_context.trading_call():
            ex.create_order(pair="BTC/USDC")
        assert fake.calls == [(PRIME, 1.0), (PRIME, 1.0)]

    def test_a_retried_order_outside_a_trading_call_does_not_inherit_prime(self):
        """Regression: a 120 s memory let any retry inherit the last band of the thread."""
        ex, fake = _flaky(priority_bot=True)
        ex.create_order(pair="BTC/USDC")
        assert fake.calls[0] == (PRIME, 1.0)
        assert fake.calls[1][0] != PRIME, "the retry is the historical default"

    def test_a_retried_order_of_a_regular_bot_is_the_historical_default(self):
        ex, fake = _flaky(priority_bot=False)
        with trading_context.trading_call():
            ex.create_order(pair="BTC/USDC")
        assert fake.calls[0] == (CRITICAL, 1.0)
        assert fake.calls[1] == (None, 1.0), "the historical re-acquisition: default band"

    def test_a_retry_in_a_notification_never_inherits_the_trading_call_band(self):
        ex, fake = _flaky(priority_bot=True)
        with trading_context.trading_call():
            ex.create_order(pair="BTC/USDC")  # records PRIME
            with trading_context.notification_call():
                ex.failures = 1
                ex.fetch_l2_order_book("BTC/USDC", 20)
        assert PRIME not in [p for p, _ in fake.calls[2:]]

    def test_a_retried_book_keeps_its_band_and_its_weight(self):
        ex, fake = _flaky(priority_bot=True)
        with trading_context.trading_call():
            ex.fetch_l2_order_book("BTC/USDC", 20)
        assert fake.calls == [(PRIME, 2.0), (PRIME, 2.0)]

    def test_a_retried_plain_book_keeps_the_default_for_a_regular_bot(self):
        ex, fake = _flaky(priority_bot=False)
        ex.fetch_l2_order_book("BTC/USDC", 20)
        assert fake.calls[0] == (HIGH, 2.0)
        assert fake.calls[1] == (None, 1.0), "the historical re-acquisition: default, weight 1"

    def test_each_attempt_tracks_the_last_call_made_on_the_thread(self):
        ex, fake = _flaky(priority_bot=True)
        ex.failures = 0
        with trading_context.trading_call():
            ex.create_order(pair="x")
            ex.fetch_l2_order_book("x", 20)
            ex.failures = 1
            ex.create_order(pair="x")
        assert fake.calls[-2:] == [(PRIME, 1.0), (PRIME, 1.0)]

    def test_memory_expires(self):
        from freqtrade.ohlcv_cache import mixin

        ex, _ = _flaky(priority_bot=True)
        with trading_context.trading_call():
            ex.create_order(pair="x")
            assert CachedExchangeMixin.ftcache_last_acquire() == (PRIME, 1.0)
            prio, cost, ts = mixin._LAST_ACQUIRE.value
            mixin._LAST_ACQUIRE.value = (prio, cost, ts - 1000)
            assert CachedExchangeMixin.ftcache_last_acquire() is None

    def test_the_memory_is_only_readable_inside_the_trading_call(self):
        ex, _ = _flaky(priority_bot=True)
        with trading_context.trading_call():
            ex.create_order(pair="x")
            assert CachedExchangeMixin.ftcache_last_acquire() == (PRIME, 1.0)
        assert CachedExchangeMixin.ftcache_last_acquire() is None

    def test_without_memory_the_retry_falls_back_to_the_historical_default(self):
        acquired = []
        obj = MagicMock()
        obj._ftcache_acquire_sync = lambda **kw: acquired.append(kw)
        obj.ftcache_last_acquire = lambda: None
        _reacquire_rate_token(obj)
        assert acquired == [{}]

    def test_an_exchange_without_the_mixin_is_left_alone(self):
        _reacquire_rate_token(object())  # no attribute: must not raise

    def test_memory_is_per_thread(self):
        ex, _ = _flaky(priority_bot=True)
        seen = []

        def other_thread():
            with trading_context.trading_call():
                seen.append(CachedExchangeMixin.ftcache_last_acquire())

        with trading_context.trading_call():
            ex.create_order(pair="x")
            t = threading.Thread(target=other_thread)
            t.start()
            t.join()
        assert seen == [None]


# --------------------------------------------------------------------------- telemetry


class TestWaitTelemetry:
    def test_waits_are_reported_per_band_and_reset(self):
        ex, _ = _exchange(priority_bot=True)
        ex.create_order(pair="x")
        with trading_context.trading_call():
            ex.fetch_order("o1", "x")
        ex.fetch_l2_order_book("x", 5)
        summary = ex.ftcache_wait_summary()
        assert set(summary) == {"PRIME", "HIGH"}
        assert summary["PRIME"]["n"] == 2
        assert set(summary["PRIME"]) >= {"lock_p50_s", "lock_max_s", "token_p50_s", "token_max_s"}
        assert ex.ftcache_wait_summary() == {}, "read-and-reset"

    def test_the_lock_wait_is_measured(self):
        ex, _ = _exchange(priority_bot=True)
        ex._ftcache_acquire_sync(priority=HIGH)  # warm up
        ex.ftcache_wait_summary()
        ex._loop_lock.acquire()
        threading.Timer(0.25, ex._loop_lock.release).start()
        ex._ftcache_acquire_sync(priority=PRIME)
        assert ex.ftcache_wait_summary()["PRIME"]["lock_max_s"] >= 0.2

    def test_no_telemetry_object_means_an_empty_summary(self):
        ex, _ = _exchange()
        assert ex.ftcache_wait_summary() == {}


# --------------------------------------------------------------------------- the real client


class _Recorder:
    """A fake daemon on a private socket: records every payload, answers ok."""

    def __init__(self, path, *, legacy: bool = False, hold_off: float = 0.0):
        self.path = path
        self.payloads: list[dict] = []
        self.legacy = legacy
        self.hold_off = hold_off
        self._server = None

    async def _handle(self, reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                break
            req = json.loads(line)
            self.payloads.append(req)
            resp = {"req_id": req.get("req_id", ""), "ok": True}
            if req.get("op") == "register":
                resp.update(fleet_size=1, hold_off_s=self.hold_off, hold_off_reason="stagger")
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        writer.close()

    async def __aenter__(self):
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


def _client(path, **kw) -> OhlcvCacheClient:
    c = OhlcvCacheClient(
        path, timeout_s=5.0, exchange_id="hyperliquid", trading_mode="futures", **kw
    )
    c.set_bot_identity(
        {"bot_id": "multi", "exchange": "hyperliquid", "dry_run": kw.get("dry_run", False)}
    )
    return c


class TestWireFormat:
    def test_a_priority_client_registers_as_one_and_tags_every_request(self, tmp_path):
        async def scenario():
            async with _Recorder(str(tmp_path / "d.sock")) as d:
                c = _client(d.path, priority_bot=True)
                await c.acquire_rate_token(priority=PRIME, cost=2.0)
                await c.get_tickers(priority=PRIME)
                await c.fetch("BTC/USDC", "15m", "futures", None, 5)
                await c.close()
                return d.payloads

        payloads = asyncio.run(scenario())
        register = next(p for p in payloads if p["op"] == "register")
        assert register["priority_bot"] is True
        acquire = next(p for p in payloads if p["op"] == "acquire")
        assert acquire["priority"] == PRIME and acquire["prime"] is True and acquire["cost"] == 2.0
        for op in ("tickers", "fetch"):
            assert next(p for p in payloads if p["op"] == op)["prime"] is True

    def test_reported_weight_travels_only_when_there_is_some(self, tmp_path):
        async def scenario():
            async with _Recorder(str(tmp_path / "d.sock")) as d:
                c = _client(d.path, priority_bot=True)
                await c.acquire_rate_token(priority=PRIME, cost=2.0)
                await c.acquire_rate_token(priority=PRIME, cost=2.0, reported_weight=7.0)
                await c.close()
                return [p for p in d.payloads if p["op"] == "acquire"]

        plain, reporting = asyncio.run(scenario())
        assert "reported_weight" not in plain
        assert reporting["reported_weight"] == 7.0

    def test_a_regular_client_never_sends_prime_or_the_flag(self, tmp_path):
        async def scenario():
            async with _Recorder(str(tmp_path / "d.sock")) as d:
                c = _client(d.path)
                await c.acquire_rate_token(priority=CRITICAL)
                await c.fetch("BTC/USDC", "15m", "futures", None, 5)
                await c.close()
                return d.payloads

        payloads = asyncio.run(scenario())
        assert next(p for p in payloads if p["op"] == "register")["priority_bot"] is False
        assert all("prime" not in p for p in payloads)

    def test_a_dry_client_cannot_be_a_priority_bot(self, tmp_path):
        c = _client(str(tmp_path / "x.sock"), priority_bot=True, dry_run=True)
        assert c.priority_bot is False

        async def scenario():
            async with _Recorder(str(tmp_path / "d.sock")) as d:
                c = _client(d.path, priority_bot=True, dry_run=True)
                await c.acquire_rate_token(priority=LOW)
                await c.close()
                return d.payloads

        payloads = asyncio.run(scenario())
        assert next(p for p in payloads if p["op"] == "register")["priority_bot"] is False
        assert all("prime" not in p for p in payloads)

    def test_a_priority_client_ignores_a_stagger_asked_by_an_older_daemon(self, tmp_path):
        async def scenario(priority):
            async with _Recorder(str(tmp_path / f"d{priority}.sock"), hold_off=60.0) as d:
                c = _client(d.path, priority_bot=priority)
                await c.ping()
                hold = c.hold_off_s
                await c.close()
                return hold

        assert asyncio.run(scenario(True)) == 0.0
        assert asyncio.run(scenario(False)) == 60.0

    def test_the_flag_travels_with_get_or_spawn(self, monkeypatch, tmp_path):
        from freqtrade.ohlcv_cache import client as client_mod

        monkeypatch.setattr(client_mod, "_ensure_daemon_running", lambda **kw: None)
        monkeypatch.setattr(client_mod, "_sync_ping_daemon", lambda *a, **k: 1000.0)
        monkeypatch.setattr(client_mod, "_CLIENT_SINGLETONS", {})
        sock = str(tmp_path / "ft.sock")
        conf = {"dry_run": False, "shared_ohlcv_cache": {"socket_path": sock, "priority_bot": True}}
        c = OhlcvCacheClient.get_or_spawn("hyperliquid", "futures", conf)
        assert c.priority_bot is True
        conf_dry = {
            "dry_run": True,
            "shared_ohlcv_cache": {"socket_path": sock + "2", "priority_bot": True},
        }
        assert (
            OhlcvCacheClient.get_or_spawn("hyperliquid", "futures", conf_dry).priority_bot is False
        )
        conf_plain = {"dry_run": False, "shared_ohlcv_cache": {"socket_path": sock + "3"}}
        assert (
            OhlcvCacheClient.get_or_spawn("hyperliquid", "futures", conf_plain).priority_bot
            is False
        )

    def test_the_prime_constant_mirrors_the_daemon(self):
        from freqtrade.ohlcv_cache.daemon import TokenBucket

        assert OhlcvCacheClient.PRIME == TokenBucket.PRIME == -1
        assert OhlcvCacheClient.PRIME < OhlcvCacheClient.CRITICAL


# --------------------------------------------------------------------------- worker


class TestWorkerJitter:
    BASE = {"bot_name": "hippo", "timeframe": "5m", "dry_run": False, "internals": {}}

    @staticmethod
    def _expected_hash_jitter(name: str) -> float:
        import hashlib

        return (int(hashlib.md5(name.encode()).hexdigest()[:8], 16) % 50) / 10.0  # noqa: S324

    def test_priority_bot_has_no_candle_jitter(self, mocker):
        from freqtrade.worker import Worker

        mocker.patch("freqtrade.worker.FreqtradeBot")
        plain = Worker(args=None, config={**self.BASE})
        prime = Worker(
            args=None, config={**self.BASE, "shared_ohlcv_cache": {"priority_bot": True}}
        )
        assert plain._candle_jitter_s == self._expected_hash_jitter("hippo") > 0.0
        assert prime._candle_jitter_s == 0.0

    def test_a_dry_bot_keeps_its_wide_post_close_window_whatever_the_flag(self, mocker):
        from freqtrade.worker import Worker

        mocker.patch("freqtrade.worker.FreqtradeBot")
        cfg = {**self.BASE, "dry_run": True, "shared_ohlcv_cache": {"priority_bot": True}}
        assert Worker(args=None, config=cfg)._candle_jitter_s >= 5.0
