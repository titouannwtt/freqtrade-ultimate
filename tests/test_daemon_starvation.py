"""A saturated high-priority band must not postpone lower bands indefinitely.

The heap is ordered `(priority, -capital, counter, ...)` and the drain loop only ever
inspected `_waiters[0]`, so a band with enough standing demand to spend the whole
weight budget kept every lower band from EVER reaching the head. On Hyperliquid the
HIGH band (open positions, hot timeframes, builder-dex refreshes) reached exactly that
point once a live bot's universe grew to 90 pairs: NORMAL cold fetches only progressed
in the gaps between HIGH arrivals, which is what stopped a restarting bot from
completing its pairlist warmup. Two consequences are covered here: lower bands get a
guaranteed share of the grants, and waiters abandoned by their client are swept instead
of accumulating out of reach behind the busy band.
"""

import asyncio
import contextlib

import pytest

from freqtrade.ohlcv_cache.daemon import TokenBucket


@pytest.fixture(autouse=True)
def _preserve_event_loop():
    """Give back the event loop these tests found (see test_daemon_acquire_shed)."""
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        previous = None
    yield
    asyncio.set_event_loop(previous)


def _bucket(**kw) -> TokenBucket:
    b = TokenBucket(rate_per_s=kw.pop("rate_per_s", 1000.0), burst=kw.pop("burst", 50.0))
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _enqueue(bucket: TokenBucket, priority: int, capital: float = 0.0, cost: float = 1.0):
    """Push a waiter the way `acquire` does, without needing a running loop to grant."""
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    import heapq

    entry = (priority, -capital, bucket._counter, cost, future)
    bucket._counter += 1
    heapq.heappush(bucket._waiters, entry)
    return future


# --- _select_next -----------------------------------------------------------------


def test_select_next_returns_head_below_the_yield_interval():
    async def scenario():
        bucket = _bucket()
        _enqueue(bucket, TokenBucket.HIGH)
        _enqueue(bucket, TokenBucket.NORMAL)
        bucket._grants_since_yield = bucket.starvation_grant_interval - 1
        index, entry = bucket._select_next()
        assert index == 0
        assert entry[0] == TokenBucket.HIGH

    asyncio.run(scenario())


def test_select_next_yields_to_the_starved_band_at_the_interval():
    async def scenario():
        bucket = _bucket()
        _enqueue(bucket, TokenBucket.HIGH)
        _enqueue(bucket, TokenBucket.NORMAL)
        bucket._grants_since_yield = bucket.starvation_grant_interval
        index, entry = bucket._select_next()
        assert index != 0
        assert entry[0] == TokenBucket.NORMAL

    asyncio.run(scenario())


def test_yield_picks_the_highest_starved_band_first():
    async def scenario():
        bucket = _bucket()
        _enqueue(bucket, TokenBucket.CRITICAL)
        _enqueue(bucket, TokenBucket.LOW)
        _enqueue(bucket, TokenBucket.NORMAL)
        bucket._grants_since_yield = bucket.starvation_grant_interval
        _, entry = bucket._select_next()
        assert entry[0] == TokenBucket.NORMAL, "NORMAL outranks LOW among starved bands"

    asyncio.run(scenario())


def test_yield_prefers_the_larger_capital_within_the_starved_band():
    async def scenario():
        bucket = _bucket()
        _enqueue(bucket, TokenBucket.HIGH)
        _enqueue(bucket, TokenBucket.NORMAL, capital=150.0)
        _enqueue(bucket, TokenBucket.NORMAL, capital=3000.0)
        bucket._grants_since_yield = bucket.starvation_grant_interval
        _, entry = bucket._select_next()
        assert entry[1] == -3000.0

    asyncio.run(scenario())


def test_yield_skips_abandoned_waiters_of_the_starved_band():
    async def scenario():
        bucket = _bucket()
        _enqueue(bucket, TokenBucket.HIGH)
        dead = _enqueue(bucket, TokenBucket.NORMAL, capital=3000.0)
        alive = _enqueue(bucket, TokenBucket.NORMAL, capital=150.0)
        dead.cancel()
        bucket._grants_since_yield = bucket.starvation_grant_interval
        _, entry = bucket._select_next()
        assert entry[4] is alive

    asyncio.run(scenario())


def test_select_next_falls_back_to_the_head_when_no_lower_band_waits():
    async def scenario():
        bucket = _bucket()
        _enqueue(bucket, TokenBucket.HIGH)
        _enqueue(bucket, TokenBucket.HIGH)
        bucket._grants_since_yield = bucket.starvation_grant_interval * 3
        index, entry = bucket._select_next()
        assert (index, entry[0]) == (0, TokenBucket.HIGH)

    asyncio.run(scenario())


def test_select_next_on_an_empty_queue():
    async def scenario():
        assert _bucket()._select_next() is None

    asyncio.run(scenario())


# --- _remove_at / heap invariant --------------------------------------------------


def test_remove_at_preserves_the_heap_invariant():
    async def scenario():
        bucket = _bucket()
        for priority in (TokenBucket.LOW, TokenBucket.NORMAL, TokenBucket.HIGH, TokenBucket.LOW):
            _enqueue(bucket, priority)
        bucket._remove_at(len(bucket._waiters) - 1)
        heap = bucket._waiters
        assert all(
            heap[i] <= heap[c]
            for i in range(len(heap))
            for c in (2 * i + 1, 2 * i + 2)
            if c < len(heap)
        )
        assert bucket._waiters[0][0] == TokenBucket.HIGH

    asyncio.run(scenario())


# --- _reap_dead_waiters -----------------------------------------------------------


def test_reap_drops_abandoned_waiters_out_of_reach_of_the_head():
    async def scenario():
        bucket = _bucket()
        _enqueue(bucket, TokenBucket.HIGH)
        for _ in range(20):
            _enqueue(bucket, TokenBucket.LOW).cancel()
        assert len(bucket._waiters) == 21
        assert bucket._reap_dead_waiters() == 20
        assert len(bucket._waiters) == 1
        assert bucket.reaped_count == 20

    asyncio.run(scenario())


def test_reap_keeps_live_waiters_and_the_heap_order():
    async def scenario():
        bucket = _bucket()
        live_high = _enqueue(bucket, TokenBucket.HIGH)
        _enqueue(bucket, TokenBucket.NORMAL).cancel()
        live_low = _enqueue(bucket, TokenBucket.LOW)
        assert bucket._reap_dead_waiters() == 1
        assert {w[4] for w in bucket._waiters} == {live_high, live_low}
        assert bucket._waiters[0][4] is live_high

    asyncio.run(scenario())


def test_reap_on_an_empty_queue_is_a_noop():
    async def scenario():
        bucket = _bucket()
        assert bucket._reap_dead_waiters() == 0
        assert bucket.reaped_count == 0

    asyncio.run(scenario())


# --- end-to-end through acquire ---------------------------------------------------


async def _saturate(bucket: TokenBucket, priority: int, stop: asyncio.Event):
    """Keep `priority` permanently non-empty, the way a 90-pair universe does."""
    while not stop.is_set():
        with contextlib.suppress(asyncio.CancelledError):
            await bucket.acquire(cost=1.0, priority=priority)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_saturated_high_band_no_longer_postpones_a_normal_request():
    # Rate low enough that the budget is the binding constraint, as on Hyperliquid.
    # The deadline has to clear `_POST_BACKOFF_MAX_BURST`, which floors the spacing of
    # a fresh drain loop's first five grants at 2s regardless of the configured rate.
    bucket = _bucket(rate_per_s=40.0, burst=2.0)
    bucket.tokens = 0.0
    stop = asyncio.Event()
    floods = [asyncio.create_task(_saturate(bucket, TokenBucket.HIGH, stop)) for _ in range(6)]
    await asyncio.sleep(0.05)
    try:
        await asyncio.wait_for(bucket.acquire(cost=1.0, priority=TokenBucket.NORMAL), timeout=25.0)
    finally:
        stop.set()
        for task in floods:
            task.cancel()
        await asyncio.gather(*floods, return_exceptions=True)
    assert bucket.starvation_yields >= 1


@pytest.mark.asyncio
async def test_the_high_band_keeps_the_bulk_of_the_budget():
    """The guarantee is a floor for the lower bands, not fair-share round-robin."""
    bucket = _bucket(rate_per_s=400.0, burst=2.0)
    bucket.tokens = 0.0
    served: list[int] = []

    async def one(priority: int):
        await bucket.acquire(cost=1.0, priority=priority)
        served.append(priority)

    tasks = [asyncio.create_task(one(TokenBucket.HIGH)) for _ in range(40)]
    tasks += [asyncio.create_task(one(TokenBucket.NORMAL)) for _ in range(40)]
    await asyncio.gather(*tasks)
    first_eighteen = served[:18]
    highs = first_eighteen.count(TokenBucket.HIGH)
    # One grant in six goes to the starved band, so HIGH keeps 15 of any 18.
    # This is the sizing the interval was chosen for: HIGH has to stay above
    # its own measured demand (18 refreshes/min out of ~22 grants/min) or the
    # bands carrying open positions start falling behind.
    assert highs == 15, f"HIGH got {highs}/18 of the first grants, expected 15"
    assert first_eighteen.count(TokenBucket.NORMAL) == 3


# --- stats ------------------------------------------------------------------------


def test_stats_separate_the_live_depth_from_the_abandoned_one():
    async def scenario():
        from freqtrade.ohlcv_cache.daemon import Daemon

        daemon = Daemon.__new__(Daemon)
        bucket = _bucket()
        daemon.budgets = {"hyperliquid": bucket}
        _enqueue(bucket, TokenBucket.NORMAL)
        for _ in range(5):
            _enqueue(bucket, TokenBucket.NORMAL).cancel()
        stats = daemon._collect_budget_stats()["budget_hyperliquid"]
        assert stats["queue_depths"]["normal"] == 6
        assert stats["queue_live_depths"]["normal"] == 1
        assert stats["starvation_grant_interval"] == bucket.starvation_grant_interval

    asyncio.run(scenario())
