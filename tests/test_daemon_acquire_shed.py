"""The daemon must refuse a rate token it cannot grant, rather than let the bot time out.

A bot waits 120s on `acquire`; past that it falls back to a LOCAL limiter sized for a
small fleet (1200/6 = 200 weight/min each). With 37 bots that self-granted budget is a
six-fold overshoot of the exchange limit, so the timeout path is what actually produces
the hard 429s. Refusing before the client's deadline keeps the bot on the safe path,
where it raises DDosProtection instead of calling ccxt unmetered.
"""

import asyncio
import contextlib
import heapq

import pytest

from freqtrade.ohlcv_cache.daemon import Daemon, TokenBucket


@pytest.fixture(autouse=True)
def _preserve_event_loop():
    """Give back the event loop these tests found.

    `asyncio.run` closes its loop and leaves no current one. Other modules here build a
    loop once per fixture and call `run_until_complete` on it, so a cleared loop made
    their timing tests fail depending on collection order, never on their own.
    """
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        previous = None
    yield
    asyncio.set_event_loop(previous)


class _NeverGrants:
    backoff_active = False
    backoff_remaining_s = 0.0

    async def acquire(self, cost=1.0, priority=2, capital=0.0):
        await asyncio.sleep(3600)


class _GrantsAtOnce:
    backoff_active = False
    backoff_remaining_s = 0.0

    def __init__(self):
        self.calls = []

    async def acquire(self, cost=1.0, priority=2, capital=0.0):
        self.calls.append((cost, priority))


def _daemon(budget, max_wait=0.2):
    d = Daemon.__new__(Daemon)
    d._acquire_max_wait_s = max_wait
    d._get_budget = lambda exchange: budget
    d._get_weight = lambda exchange, op: 1.0
    d._exchange_cfg = lambda exchange: {}

    class _S:
        acquire_total = 0

    d.stats = _S()
    return d


REQ = {"op": "acquire", "req_id": "a1", "exchange": "hyperliquid", "priority": 2, "cost": 20}


def test_acquire_that_cannot_be_granted_is_refused_not_left_hanging():
    d = _daemon(_NeverGrants())

    resp = asyncio.run(d._handle_acquire(dict(REQ)))

    assert resp["ok"] is False
    assert resp["throttled"] is True, "the client keys off `throttled` to raise CacheRateLimited"
    assert resp["error_type"] == "CacheRateLimited"


def test_the_shipped_deadline_leaves_margin_before_the_client_gives_up():
    """The margin IS the fix: a deadline above the client timeout changes nothing."""
    import inspect
    import re

    from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin

    src = inspect.getsource(Daemon.__init__)
    m = re.search(r'global_cfg\.get\("acquire_max_wait_s", ([0-9.]+)\)', src)
    assert m, "the daemon must ship a default acquire deadline"
    assert float(m.group(1)) < CachedExchangeMixin._ACQUIRE_TIMEOUT_S


def test_a_grantable_acquire_still_succeeds():
    budget = _GrantsAtOnce()
    d = _daemon(budget)

    resp = asyncio.run(d._handle_acquire(dict(REQ)))

    assert resp["ok"] is True
    assert budget.calls == [(20.0, 2)]


def test_shedding_one_request_does_not_shed_the_next():
    budget = _GrantsAtOnce()
    d = _daemon(budget)
    slow = _daemon(_NeverGrants())

    assert asyncio.run(slow._handle_acquire(dict(REQ)))["ok"] is False
    assert asyncio.run(d._handle_acquire(dict(REQ)))["ok"] is True


@pytest.mark.parametrize("abandoned", [1, 5])
def test_drain_loop_drops_waiters_whose_client_gave_up(abandoned):
    """Tokens spent on an abandoned waiter are stolen from callers still waiting."""

    async def _run():
        b = TokenBucket(rate_per_s=1000.0, burst=100.0, exchange="hyperliquid")
        b.tokens = 0.0  # force everyone through the queue

        loop = asyncio.get_running_loop()
        for _ in range(abandoned):
            f = loop.create_future()
            f.cancel()  # the client already gave up
            heapq.heappush(b._waiters, (2, 0.0, b._counter, 10.0, f))
            b._counter += 1

        live = loop.create_future()
        heapq.heappush(b._waiters, (2, 0.0, b._counter, 10.0, live))

        b._ensure_drain()
        await asyncio.wait_for(live, timeout=5.0)
        # Leave no pending task behind: a drain task still running when asyncio.run()
        # closes the loop raises unraisably, and pytest charges that to whichever test
        # happens to run next.
        b._drain_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await b._drain_task
        return b

    b = asyncio.run(_run())
    assert b._waiters == [], "abandoned entries must be discarded, not left in the heap"


class TestLocalFallbackSizing:
    """The fallback limiter must share the exchange budget across the REAL fleet.

    It is only reached when the daemon cannot answer, which is exactly when the fleet is
    largest and most loaded. Sizing it for 6 bots while 37 are running hands out six
    times the exchange budget and manufactures the 429s it exists to prevent.
    """

    @staticmethod
    def _venue_budget():
        from freqtrade.ohlcv_cache.defaults import EXCHANGE_DEFAULTS

        return EXCHANGE_DEFAULTS.get("hyperliquid", {}).get("weight_budget_per_min", 1200.0)

    def _mixin(self, fleet_size=None):
        """A bare mixin instance: only what `_ftcache_get_local_limiter` reads.

        Deliberately not built from the shared fixture in test_ohlcv_cache: importing it
        pulled that module's state into this one and made its timing tests fail depending
        on collection order.
        """
        from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin

        class _Client:
            pass

        mixin = CachedExchangeMixin.__new__(CachedExchangeMixin)
        mixin.id = "hyperliquid"
        mixin._ftcache_local_limiter = None
        client = _Client()
        client.fleet_size = fleet_size or 0
        mixin._ftcache_client = client
        return mixin

    def test_budget_is_shared_across_the_fleet_the_daemon_reported(self):
        from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin

        mixin = self._mixin(fleet_size=37)
        limiter = CachedExchangeMixin._ftcache_get_local_limiter(mixin)

        assert limiter.assumed_bots == 37
        assert limiter._budget == pytest.approx(self._venue_budget() / 37)

    def test_an_unknown_fleet_falls_back_to_the_conservative_floor(self):
        from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin

        mixin = self._mixin(fleet_size=0)
        limiter = CachedExchangeMixin._ftcache_get_local_limiter(mixin)

        assert limiter.assumed_bots == CachedExchangeMixin._LOCAL_LIMITER_ASSUMED_BOTS

    def test_a_growing_fleet_shrinks_the_share_of_an_existing_limiter(self):
        from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin

        mixin = self._mixin(fleet_size=0)
        limiter = CachedExchangeMixin._ftcache_get_local_limiter(mixin)
        first = limiter._budget

        mixin._ftcache_client.fleet_size = 37
        again = CachedExchangeMixin._ftcache_get_local_limiter(mixin)

        assert again is limiter, "the limiter is resized in place, not replaced"
        assert again._budget < first
        assert again.assumed_bots == 37

    def test_a_shrinking_fleet_never_widens_the_allowance(self):
        """Protecting the exchange outranks a stale estimate."""
        from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin

        mixin = self._mixin(fleet_size=37)
        limiter = CachedExchangeMixin._ftcache_get_local_limiter(mixin)
        tight = limiter._budget

        mixin._ftcache_client.fleet_size = 2
        again = CachedExchangeMixin._ftcache_get_local_limiter(mixin)

        assert again._budget == tight
        assert again.assumed_bots == 37

    def test_the_fleet_share_stays_under_the_exchange_budget(self):
        """The property that actually matters: N bots must not exceed the venue limit."""
        from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin

        venue = self._venue_budget()
        for fleet in (6, 12, 37, 60):
            mixin = self._mixin(fleet_size=fleet)
            limiter = CachedExchangeMixin._ftcache_get_local_limiter(mixin)
            assert limiter._budget * fleet <= venue + 1e-6, f"depassement a fleet={fleet}"
