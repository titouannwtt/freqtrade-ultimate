"""The PRIME band of the ftcache token bucket (daemon side of ``priority_bot``).

Rules pinned here, one test group each:

* PRIME is served before CRITICAL, outside the weight gate and without the post-grant
  spacing; it is never abandoned by the daemon while the fleet's deadline stays 90 s.
* The non-urgent bands stop at 75 % of the weight budget once a priority bot is
  registered (80 % before, and on a fleet without priority bots), on the fast path too.
* One bot's PRIME grants are capped; over the cap the request is demoted to CRITICAL,
  never refused.
* The drain loop never sleeps while holding the bucket lock, so an urgent request
  arriving during a gate or spacing sleep is served at once.
* A dry-run bot, or a connection that never said it is a priority bot, cannot obtain
  PRIME; it is served as CRITICAL.
* Old clients (no ``prime``, no ``priority_bot``) keep exactly their former behaviour.
"""

import asyncio
import contextlib
import heapq
import time

import pytest

from freqtrade.ohlcv_cache.daemon import BotRegistry, Daemon, TokenBucket


@pytest.fixture(autouse=True)
def _preserve_event_loop():
    """Give back the event loop these tests found (see test_daemon_acquire_shed)."""
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        previous = None
    yield
    asyncio.set_event_loop(previous)


BUDGET = 1140  # weight per minute, as shipped for Hyperliquid


def _bucket(armed: bool = True, **kw) -> TokenBucket:
    b = TokenBucket(
        rate_per_s=kw.pop("rate_per_s", 19.0),
        burst=kw.pop("burst", 150.0),
        weight_mode=True,
        weight_budget_per_min=kw.pop("budget", BUDGET),
        exchange="hyperliquid",
    )
    b.prime_enabled = armed
    b.prime_reserve_pct = 0.25  # the tests below reason on a 75 % gate / 285 weight reserve
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _fill_window(b: TokenBucket, weight: float) -> None:
    """Make the 60 s window look ``weight`` heavy, spread over the last 50 seconds."""
    now = time.monotonic()
    n = 10
    for i in range(n):
        b._weight_window.append((now - 50 + i * 4, weight / n))
    b._weight_used_last_min = weight


def _enqueue(b: TokenBucket, priority: int, capital: float = 0.0, cost: float = 1.0):
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    heapq.heappush(b._waiters, (priority, -capital, b._counter, cost, future))
    b._counter += 1
    return future


async def _stop(b: TokenBucket) -> None:
    """Cancel the drain task so no pending task survives ``asyncio.run``."""
    task = b._drain_task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# -------------------- ordering


def test_prime_sorts_before_critical_in_the_heap():
    async def scenario():
        b = _bucket()
        _enqueue(b, TokenBucket.HIGH)
        _enqueue(b, TokenBucket.CRITICAL)
        _enqueue(b, TokenBucket.PRIME)
        _enqueue(b, TokenBucket.LOW)
        assert heapq.heappop(b._waiters)[0] == TokenBucket.PRIME
        assert heapq.heappop(b._waiters)[0] == TokenBucket.CRITICAL

    asyncio.run(scenario())


def test_a_queued_prime_is_granted_before_a_queued_critical():
    async def scenario():
        b = _bucket(armed=False)
        b.tokens = 0.0  # force the queue
        order = []
        fut_c = _enqueue(b, TokenBucket.CRITICAL)
        fut_p = _enqueue(b, TokenBucket.PRIME)
        fut_c.add_done_callback(lambda _: order.append("critical"))
        fut_p.add_done_callback(lambda _: order.append("prime"))
        b._ensure_drain()
        await asyncio.wait_for(asyncio.gather(fut_c, fut_p), timeout=5)
        assert order == ["prime", "critical"]
        await _stop(b)

    asyncio.run(scenario())


# -------------------- the 75 % gate


def test_gate_is_legacy_until_a_priority_bot_arms_the_bucket():
    assert _bucket(armed=False).gate_ratio == pytest.approx(0.80)
    assert _bucket(armed=True).gate_ratio == pytest.approx(0.75)
    assert _bucket(armed=True, prime_reserve_pct=0.10).gate_ratio == pytest.approx(0.90)


def test_non_priority_request_waits_above_75_percent_once_armed():
    async def scenario():
        b = _bucket(armed=True)
        _fill_window(b, BUDGET * 0.78)  # above 75 %, below 80 %
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(b.acquire(2.0, priority=TokenBucket.HIGH), timeout=0.4)
        assert time.monotonic() - started >= 0.39
        await _stop(b)

    asyncio.run(scenario())


def test_same_load_does_not_gate_when_nobody_is_armed():
    """78 % is under the historical 80 % gate: the fleet is served as before."""

    async def scenario():
        b = _bucket(armed=False)
        _fill_window(b, BUDGET * 0.78)
        await asyncio.wait_for(b.acquire(2.0, priority=TokenBucket.HIGH), timeout=0.4)

    asyncio.run(scenario())


def test_critical_is_not_gated():
    async def scenario():
        b = _bucket(armed=True)
        _fill_window(b, BUDGET * 0.90)
        await asyncio.wait_for(b.acquire(1.0, priority=TokenBucket.CRITICAL), timeout=0.2)

    asyncio.run(scenario())


def test_prime_passes_between_75_and_100_percent():
    async def scenario():
        b = _bucket(armed=True)
        _fill_window(b, BUDGET * 0.95)
        started = time.monotonic()
        await asyncio.wait_for(
            b.acquire(2.0, priority=TokenBucket.PRIME, bot_id="multi"), timeout=0.2
        )
        assert time.monotonic() - started < 0.1

    asyncio.run(scenario())


def test_the_reserve_is_held_back_on_the_fast_path_too():
    """An empty queue must not let the fleet spend the PRIME reserve."""

    async def scenario():
        b = _bucket(armed=True)
        _fill_window(b, BUDGET * 0.80)
        assert not b._waiters
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(b.acquire(2.0, priority=TokenBucket.NORMAL), timeout=0.3)
        await _stop(b)

    asyncio.run(scenario())


def test_a_starvation_yield_cannot_leak_through_the_gate():
    """With a PRIME head and the window over the gate, the yield skips non-urgent bands."""

    async def scenario():
        b = _bucket(armed=True)
        _fill_window(b, BUDGET * 0.90)
        _enqueue(b, TokenBucket.PRIME)
        _enqueue(b, TokenBucket.LOW)
        b._grants_since_yield = b.starvation_grant_interval
        index, entry = b._select_next(over_gate=True)
        assert (index, entry[0]) == (0, TokenBucket.PRIME)
        index, entry = b._select_next(over_gate=False)
        assert entry[0] == TokenBucket.LOW

    asyncio.run(scenario())


# -------------------- (a) urgent wake


def test_prime_arriving_during_a_gate_wait_is_served_in_under_100ms():
    """(a) The head is a gated HIGH asleep for >= 1 s; a PRIME must not wait for it."""

    async def scenario():
        b = _bucket(armed=True)
        _fill_window(b, BUDGET * 0.90)
        gated = asyncio.ensure_future(b.acquire(2.0, priority=TokenBucket.HIGH))
        await asyncio.sleep(0.15)  # the drain loop is now asleep on the gate
        assert b._drain_task is not None and not b._drain_task.done()
        assert not b._lock.locked(), "the loop sleeps with the lock held: the original bug"
        arrived = time.monotonic()
        await asyncio.wait_for(
            b.acquire(2.0, priority=TokenBucket.PRIME, bot_id="multi"), timeout=2.0
        )
        assert time.monotonic() - arrived < 0.1
        assert not gated.done(), "the gated request keeps waiting for the window to decay"
        gated.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gated
        await _stop(b)

    asyncio.run(scenario())


def test_critical_arriving_during_a_gate_wait_is_served_in_under_100ms():
    async def scenario():
        b = _bucket(armed=True)
        _fill_window(b, BUDGET * 0.90)
        gated = asyncio.ensure_future(b.acquire(2.0, priority=TokenBucket.NORMAL))
        await asyncio.sleep(0.15)
        arrived = time.monotonic()
        await asyncio.wait_for(b.acquire(1.0, priority=TokenBucket.CRITICAL), timeout=2.0)
        assert time.monotonic() - arrived < 0.1
        gated.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gated
        await _stop(b)

    asyncio.run(scenario())


def test_the_lock_is_free_during_the_spacing_sleep_after_a_grant():
    """The 2 s spacing after a grant used to be slept under the lock as well."""

    async def scenario():
        b = _bucket(armed=False)
        b.tokens = 0.0
        first = _enqueue(b, TokenBucket.HIGH)
        second = _enqueue(b, TokenBucket.HIGH)
        b._ensure_drain()
        await asyncio.wait_for(first, timeout=2.0)
        await asyncio.sleep(0.1)
        assert not second.done(), "the second grant waits out the spacing"
        assert not b._lock.locked()
        # ...and an order arriving in that window does not wait for it.
        arrived = time.monotonic()
        await asyncio.wait_for(b.acquire(1.0, priority=TokenBucket.PRIME, bot_id="x"), timeout=2)
        assert time.monotonic() - arrived < 0.2
        second.cancel()
        await _stop(b)

    asyncio.run(scenario())


def test_positions_cache_miss_is_not_blocked_by_a_gate_wait():
    """``_handle_positions_get`` takes ``budget._lock`` to auto-grant its token."""

    async def scenario():
        b = _bucket(armed=True)
        _fill_window(b, BUDGET * 0.90)
        gated = asyncio.ensure_future(b.acquire(2.0, priority=TokenBucket.HIGH))
        await asyncio.sleep(0.15)
        started = time.monotonic()
        async with b._lock:
            b._refill()
        assert time.monotonic() - started < 0.05
        gated.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gated
        await _stop(b)

    asyncio.run(scenario())


def test_prime_grants_are_not_spaced():
    async def scenario():
        b = _bucket(armed=True)
        b.tokens = 0.0
        futures = [_enqueue(b, TokenBucket.PRIME, cost=2.0) for _ in range(6)]
        b._ensure_drain()
        started = time.monotonic()
        await asyncio.wait_for(asyncio.gather(*futures), timeout=5)
        # 6 grants of 2 tokens at 19 tokens/s: ~0.6 s. The 2 s spacing would give 10 s.
        assert time.monotonic() - started < 2.0
        await _stop(b)

    asyncio.run(scenario())


# -------------------- (b) the cap


def test_over_the_cap_a_request_is_demoted_to_critical_not_refused():
    async def scenario():
        b = _bucket(armed=True, prime_cap_weight_per_min=150.0)
        for _ in range(7):  # 7 x 20 = 140 weight
            await asyncio.wait_for(
                b.acquire(20.0, priority=TokenBucket.PRIME, bot_id="runaway"), timeout=1
            )
        assert b.grants_by_band.get(TokenBucket.PRIME) == 7
        assert b.prime_demotions == 0
        # 140 + 20 > 150: served, as CRITICAL.
        await asyncio.wait_for(
            b.acquire(20.0, priority=TokenBucket.PRIME, bot_id="runaway"), timeout=1
        )
        assert b.prime_demotions == 1
        assert b.prime_demotions_by_bot == {"runaway": 1}
        assert b.grants_by_band.get(TokenBucket.PRIME) == 7
        assert b.grants_by_band.get(TokenBucket.CRITICAL) == 1

    asyncio.run(scenario())


def test_the_cap_is_per_bot():
    async def scenario():
        b = _bucket(armed=True, prime_cap_weight_per_min=150.0)
        for _ in range(7):
            await b.acquire(20.0, priority=TokenBucket.PRIME, bot_id="a")
        await b.acquire(20.0, priority=TokenBucket.PRIME, bot_id="b")
        assert b.prime_demotions == 0
        assert b.prime_weight_by_bot == {"a": 140.0, "b": 20.0}

    asyncio.run(scenario())


def test_the_cap_window_slides():
    async def scenario():
        b = _bucket(armed=True, prime_cap_weight_per_min=150.0)
        for _ in range(7):
            await b.acquire(20.0, priority=TokenBucket.PRIME, bot_id="a")
        # Two minutes later the weight has left the window.
        b._prime_windows["a"] = type(b._prime_windows["a"])(
            (ts - 120.0, c) for ts, c in b._prime_windows["a"]
        )
        await b.acquire(20.0, priority=TokenBucket.PRIME, bot_id="a")
        assert b.prime_demotions == 0

    asyncio.run(scenario())


def test_a_demoted_request_is_still_served_ahead_of_the_fleet():
    async def scenario():
        b = _bucket(armed=True, prime_cap_weight_per_min=10.0)
        b.tokens = 0.0
        order = []
        high = _enqueue(b, TokenBucket.HIGH, cost=2.0)
        high.add_done_callback(lambda _: order.append("high"))
        demoted = asyncio.ensure_future(
            b.acquire(20.0, priority=TokenBucket.PRIME, bot_id="a")
        )  # 20 > 10: CRITICAL from the start
        await asyncio.sleep(0)
        demoted.add_done_callback(lambda _: order.append("demoted"))
        await asyncio.wait_for(asyncio.gather(high, demoted), timeout=5)
        assert order == ["demoted", "high"]
        await _stop(b)

    asyncio.run(scenario())


def test_no_cap_without_a_bot_id():
    async def scenario():
        b = _bucket(armed=True, prime_cap_weight_per_min=10.0)
        for _ in range(3):
            await b.acquire(20.0, priority=TokenBucket.PRIME)
        assert b.prime_demotions == 0

    asyncio.run(scenario())


# -------------------- (c) backoff


def test_hard_backoff_serves_prime_first_and_without_spacing():
    async def scenario():
        b = _bucket(armed=True)
        # A HARD backoff in progress: rate divided by 10, everything is queued.
        b._backoff_until = time.monotonic() + 30
        b._backoff_factor = 10.0
        b._current_shed_threshold = TokenBucket.HIGH
        b._current_backoff_label = "HARD"
        b.tokens = 0.0
        low = asyncio.ensure_future(b.acquire(2.0, priority=TokenBucket.LOW))
        normal = asyncio.ensure_future(b.acquire(2.0, priority=TokenBucket.NORMAL))
        await asyncio.sleep(0)
        started = time.monotonic()
        await asyncio.wait_for(
            b.acquire(2.0, priority=TokenBucket.PRIME, bot_id="multi"), timeout=3.0
        )
        # tokens refill at 1.9/s in HARD: one PRIME of cost 2 needs ~1 s, not a 2 s slot.
        assert time.monotonic() - started < 2.0
        assert not low.done() and not normal.done()
        for f in (low, normal):
            f.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await f
        await _stop(b)

    asyncio.run(scenario())


def test_backoff_never_sheds_a_prime_request_through_handle_acquire():
    """The fleet's 90 s deadline does not apply to PRIME (600 s)."""

    class _SlowButGranting:
        backoff_active = True
        backoff_remaining_s = 5.0

        async def acquire(self, cost=1.0, priority=2, capital=0.0, bot_id=None):
            await asyncio.sleep(0.5)  # longer than the fleet deadline below

    d = Daemon.__new__(Daemon)
    d._acquire_max_wait_s = 0.2
    d._prime_acquire_max_wait_s = 5.0
    d.registry = BotRegistry()
    d.registry.register("multi", {"exchange": "hyperliquid", "priority_bot": True}, 1)
    d._get_budget = lambda exchange: _SlowButGranting()
    d._get_weight = lambda exchange, op: 1.0

    class _S:
        acquire_total = 0
        prime_ineligible = 0

    d.stats = _S()
    prime = {"op": "acquire", "req_id": "p", "exchange": "hyperliquid", "priority": -1, "cost": 2}
    normal = {"op": "acquire", "req_id": "n", "exchange": "hyperliquid", "priority": 2, "cost": 2}
    assert asyncio.run(d._handle_acquire(dict(prime), conn_id=1))["ok"] is True
    shed = asyncio.run(d._handle_acquire(dict(normal), conn_id=1))
    assert shed["ok"] is False and shed["throttled"] is True


def test_prime_is_not_demoted_into_the_hard_shed_by_the_tickers_escalation():
    """Under backoff HIGH is escalated to CRITICAL; ``min`` keeps PRIME as PRIME."""
    assert min(TokenBucket.PRIME, TokenBucket.CRITICAL) == TokenBucket.PRIME
    assert min(TokenBucket.HIGH, TokenBucket.CRITICAL) == TokenBucket.CRITICAL


# -------------------- (d) starvation


def test_two_saturated_prime_bots_leave_the_low_band_one_grant_in_six():
    async def scenario():
        b = _bucket(armed=True, rate_per_s=10_000.0, burst=10_000.0)
        b.tokens = 0.0
        order = []
        futures = []
        for i in range(60):  # two bots' standing demand
            f = _enqueue(b, TokenBucket.PRIME)
            f.add_done_callback(lambda _, i=i: order.append("P"))
            futures.append(f)
        for i in range(12):
            f = _enqueue(b, TokenBucket.LOW)
            f.add_done_callback(lambda _: order.append("L"))
            futures.append(f)
        b._ensure_drain()
        await asyncio.wait_for(asyncio.gather(*futures), timeout=10)
        first_36 = order[:36]
        assert first_36.count("L") >= 6, "the starvation share is one grant in six"
        # And the PRIME bots keep the other five.
        assert first_36.count("P") >= 30
        await _stop(b)

    asyncio.run(scenario())


# -------------------- statistics


def test_band_stats_report_grants_and_wait_percentiles():
    b = _bucket()
    now = time.monotonic()
    for wait in (0.01, 0.02, 0.03, 0.04, 0.5):
        b._note_grant(TokenBucket.PRIME, 2.0, now - wait)
    stats = b.band_stats()
    prime = stats["prime"]
    assert prime["grants_total"] == 5 and prime["weight_total"] == 10.0
    assert prime["wait_p50_ms"] == pytest.approx(30, abs=15)
    assert prime["wait_max_ms"] == pytest.approx(500, abs=15)
    assert "wait_p95_ms" in prime
    assert stats["low"]["grants_total"] == 0 and "wait_p50_ms" not in stats["low"]


def test_an_acquire_records_which_band_it_was_served_in():
    async def scenario():
        b = _bucket(armed=True)
        await b.acquire(2.0, priority=TokenBucket.PRIME, bot_id="a")
        await b.acquire(1.0, priority=TokenBucket.CRITICAL)
        await b.acquire(1.0, priority=TokenBucket.HIGH)
        assert b.grants_by_band == {-1: 1, 0: 1, 1: 1}
        assert b.weight_by_band[TokenBucket.PRIME] == 2.0

    asyncio.run(scenario())


def test_external_weight_is_booked_in_the_window():
    b = _bucket()
    before = b.weight_used_last_min
    b.record_external_weight(2.0)
    assert b.weight_used_last_min == pytest.approx(before + 2.0)
    b.record_external_weight(0.0)
    assert b.weight_used_last_min == pytest.approx(before + 2.0)


# -------------------- the Daemon level


def _daemon(**cfg) -> Daemon:
    return Daemon(socket_path="/nonexistent/ftcache-test.sock", global_cfg=cfg)


def _register(d: Daemon, conn: int, bot: str, **info):
    base = {"bot_id": bot, "exchange": "hyperliquid", "pid": 1}
    base.update(info)
    return d._handle_register(base, conn)


def test_defaults_ship_the_documented_prime_settings():
    from freqtrade.ohlcv_cache.defaults import GLOBAL_DEFAULTS

    assert GLOBAL_DEFAULTS["prime_reserve_pct"] == 0.20
    assert GLOBAL_DEFAULTS["prime_cap_weight_per_min"] == 150
    assert GLOBAL_DEFAULTS["prime_swr_max_stale_candles"] == 1
    assert GLOBAL_DEFAULTS["prime_acquire_max_wait_s"] == 100
    assert GLOBAL_DEFAULTS["prime_sync_fill_max_s"] == 5
    assert GLOBAL_DEFAULTS["prime_sync_fill_window_s"] == 30
    assert GLOBAL_DEFAULTS["prime_global_cap_weight_per_min"] == 0
    d = _daemon()
    b = d._get_budget("hyperliquid")
    assert b.prime_reserve_pct == 0.20 and b.prime_cap_weight_per_min == 150
    assert b.prime_enabled is False


def test_registering_a_priority_bot_arms_the_bucket_and_the_last_exit_disarms_it():
    d = _daemon()
    bucket = d._get_budget("hyperliquid")
    _register(d, 1, "fleet-bot")
    assert bucket.prime_enabled is False
    _register(d, 2, "multi", priority_bot=True)
    assert bucket.prime_enabled is True and bucket.gate_ratio == pytest.approx(0.80)
    d.registry.unregister(2)
    d._sync_prime_state()
    assert bucket.prime_enabled is False and bucket.gate_ratio == pytest.approx(0.80)


def test_a_bucket_created_after_the_priority_bot_registered_starts_armed():
    d = _daemon()
    _register(d, 1, "multi", priority_bot=True)
    assert d._get_budget("hyperliquid").prime_enabled is True


def test_a_dry_run_bot_flagged_as_priority_is_not_one():
    d = _daemon()
    _register(d, 1, "dry-bot", priority_bot=True, dry_run=True)
    assert d.registry.is_priority(1) is False
    assert d._get_budget("hyperliquid").prime_enabled is False


def test_a_priority_bot_gets_no_admission_hold_off():
    d = _daemon()
    for i in range(1, 6):  # five bots initializing: ~60 s of stagger for the next one
        _register(d, i, f"b{i}", state="initializing")
    waiting = _register(d, 10, "late-fleet-bot", state="initializing")
    assert waiting["hold_off_s"] > 0
    prime = _register(d, 11, "multi", priority_bot=True, state="initializing")
    assert prime["hold_off_s"] == 0 and prime["hold_off_reason"] == "priority bot"


def test_old_registration_without_the_field_behaves_as_before():
    d = _daemon()
    resp = _register(d, 1, "old-client")
    assert resp["ok"] is True
    assert d.registry.is_priority(1) is False


def test_prime_is_granted_to_a_registered_priority_bot_only():
    d = _daemon()
    _register(d, 1, "multi", priority_bot=True)
    _register(d, 2, "dry-bot", priority_bot=True, dry_run=True)
    _register(d, 3, "fleet-bot")
    ask = {"priority": -1}
    assert d._resolve_priority(dict(ask), 1, 2) == TokenBucket.PRIME
    # (e) a dry bot that asks for PRIME is served, as CRITICAL
    assert d._resolve_priority({"priority": -1, "prime": True}, 2, 2) == TokenBucket.CRITICAL
    # a fleet bot that never said it is a priority bot cannot take the band
    assert d._resolve_priority(dict(ask), 3, 2) == TokenBucket.CRITICAL
    assert d.stats.prime_ineligible == 2


def test_the_per_request_flag_covers_a_connection_registered_before_the_flag_existed():
    d = _daemon()
    assert d._resolve_priority({"priority": -1, "prime": True}, 77, 2) == TokenBucket.PRIME
    assert d._resolve_priority({"priority": -1}, 77, 2) == TokenBucket.CRITICAL


def test_old_client_requests_are_resolved_unchanged():
    d = _daemon()
    _register(d, 1, "old-client")
    for prio in (0, 1, 2, 3):
        assert d._resolve_priority({"priority": prio}, 1, 2) == prio
    assert d._resolve_priority({}, 1, 2) == 2
    assert d.stats.prime_ineligible == 0


def test_the_stats_op_exposes_bands_and_the_prime_block():
    async def scenario():
        d = _daemon()
        _register(d, 1, "multi", priority_bot=True)
        bucket = d._get_budget("hyperliquid")
        await bucket.acquire(2.0, priority=TokenBucket.PRIME, bot_id="multi")
        stats = await d._dispatch({"op": "stats", "req_id": "s"}, 0)
        assert stats["ok"] is True
        assert stats["bands"]["prime"]["grants_total"] == 1
        assert stats["prime"]["armed"] is True and stats["prime"]["gate_pct"] == 80.0
        assert stats["prime"]["weight_by_bot"] == {"multi": 2.0}
        assert "prime" in stats["queue_depths"]
        assert stats["prime_ineligible_requests"] == 0

    asyncio.run(scenario())


def test_the_stats_summary_line_carries_prime_and_wait_columns(caplog):
    d = _daemon(stats_interval_s=60)
    _register(d, 1, "multi", priority_bot=True)
    d._get_budget("hyperliquid")
    with caplog.at_level("INFO", logger="ftcache.daemon"):
        d._log_stats_summary()
    line = next(r.message for r in caplog.records if r.message.startswith("STATS"))
    assert "prime=on/0g/0dem" in line and "waitp95ms=" in line


# -------------------- candles of a priority bot


class _RecordingFetcher:
    """Stands in for ExchangeFetcher: records what the bucket was asked for."""

    def __init__(self, calls):
        self.calls = calls

    async def fetch_ohlcv(self, pair, timeframe, since_ms, limit, candle_type, **kw):
        self.calls.append({"pair": pair, "priority": kw["priority"], "capital": kw["capital"]})
        tf = 15 * 60_000
        end = (int(time.time() * 1000) // tf) * tf
        return [[end - i * tf, 1, 1, 1, 1, 1] for i in range(limit, 0, -1)]


def _fetch_req(**kw):
    req = {
        "op": "fetch",
        "req_id": "f",
        "exchange": "hyperliquid",
        "trading_mode": "futures",
        "pair": "BTC/USDC:USDC",
        "timeframe": "15m",
        "candle_type": "futures",
        "since_ms": None,
        "limit": 5,
        "priority": 1,
        "capital": 100.0,
    }
    req.update(kw)
    return req


def test_candles_of_a_priority_bot_lead_their_band_but_stay_out_of_prime():
    async def scenario():
        d = _daemon()
        calls: list = []
        d._get_fetcher = lambda ex, tm: _RecordingFetcher(calls)
        _register(d, 1, "multi", priority_bot=True)
        _register(d, 2, "fleet-bot")
        await d._handle_fetch(_fetch_req(pair="A/USDC:USDC"), conn_id=1)
        await d._handle_fetch(_fetch_req(pair="B/USDC:USDC"), conn_id=2)
        by_pair = {c["pair"]: c for c in calls}
        assert by_pair["A/USDC:USDC"]["priority"] == TokenBucket.HIGH
        assert by_pair["A/USDC:USDC"]["capital"] > 1e8
        assert by_pair["B/USDC:USDC"]["capital"] == 100.0

    asyncio.run(scenario())


def test_the_bonus_orders_a_priority_bot_first_inside_a_band():
    b = _bucket()
    b.tokens = 0.0
    loop_futures = []

    async def scenario():
        for capital in (3000.0, 100.0 + Daemon._PRIME_CAPITAL_BONUS, 150.0):
            loop_futures.append(_enqueue(b, TokenBucket.HIGH, capital=capital))
        first = heapq.heappop(b._waiters)
        assert first[1] == -(100.0 + Daemon._PRIME_CAPITAL_BONUS)

    asyncio.run(scenario())


class _Ticking:
    """A fetcher whose first fill takes `delay` seconds, counted from the call."""

    def __init__(self, delay):
        self.delay = delay

    async def fetch_ohlcv(self, pair, timeframe, since_ms, limit, candle_type, **kw):
        await asyncio.sleep(self.delay)
        tf = 15 * 60_000
        end = (int(time.time() * 1000) // tf) * tf
        return [[end - i * tf, 1, 1, 1, 1, 1] for i in range(limit, 0, -1)]


def _seeded_daemon(offset: int, fetcher) -> tuple[Daemon, str]:
    """A daemon holding a 15m series whose last cached candle opened `offset` periods
    before the forming one (0: the forming candle is cached; 1: only up to the candle that
    just closed...). The daemon measures freshness from the open of that last candle."""
    tf = 15 * 60_000
    d = _daemon(prime_sync_fill_max_s=0.3)
    d._get_fetcher = lambda ex, tm: fetcher
    series = d.store.get_or_create("hyperliquid", "futures", "BTC/USDC:USDC", "15m", "futures", tf)
    forming = (int(time.time() * 1000) // tf) * tf
    end = forming - offset * tf
    rows = [[end - i * tf, 1, 1, 1, 1, 1] for i in range(40, -1, -1)]
    series.merge(rows, fetched_at_ms=int(time.time() * 1000), covered=(rows[0][0], end + tf))
    return d, "BTC/USDC:USDC"


def test_a_hot_series_must_be_within_one_period_for_a_priority_bot():
    async def scenario():
        # Last cached candle opened 1 period before the forming one, i.e. more than one
        # period ago: the fleet still gets it stale (SWR allows 8), a priority bot waits
        # for a (bounded) synchronous refill.
        d, _ = _seeded_daemon(1, _Ticking(0.0))
        _register(d, 1, "multi", priority_bot=True)
        _register(d, 2, "fleet-bot")
        fleet = await d._handle_fetch(_fetch_req(hot=True), conn_id=2)
        assert fleet["served_from"] == "stale"
        d2, _ = _seeded_daemon(1, _Ticking(0.0))
        _register(d2, 1, "multi", priority_bot=True)
        prime = await d2._handle_fetch(_fetch_req(hot=True), conn_id=1)
        assert prime["served_from"] in {"partial", "fetch"}, "refilled synchronously"

    asyncio.run(scenario())


def test_a_priority_bot_without_a_hot_timeframe_keeps_the_fleet_staleness():
    async def scenario():
        d, _ = _seeded_daemon(1, _Ticking(0.0))
        _register(d, 1, "multi", priority_bot=True)
        resp = await d._handle_fetch(_fetch_req(), conn_id=1)  # no `hot`
        assert resp["served_from"] == "stale"

    asyncio.run(scenario())


def test_the_synchronous_fill_of_a_priority_bot_is_bounded_then_serves_stale():
    async def scenario():
        d, _ = _seeded_daemon(1, _Ticking(5.0))  # the exchange is slow
        _register(d, 1, "multi", priority_bot=True)
        started = time.monotonic()
        resp = await d._handle_fetch(_fetch_req(hot=True), conn_id=1)
        assert resp["ok"] is True and resp["served_from"] == "stale"
        assert time.monotonic() - started < 1.5, "bounded by prime_sync_fill_max_s (0.3 here)"
        for task in list(d._swr_tasks):
            task.cancel()

    asyncio.run(scenario())


def test_a_series_within_one_period_is_served_without_a_synchronous_fill():
    async def scenario():
        calls: list = []
        d, _ = _seeded_daemon(0, _RecordingFetcher(calls))
        _register(d, 1, "multi", priority_bot=True)
        resp = await d._handle_fetch(_fetch_req(hot=True), conn_id=1)
        assert resp["served_from"] in {"cache", "stale"}
        await asyncio.gather(*list(d._swr_tasks))

    asyncio.run(scenario())


# -------------------- central positions


def test_the_central_positions_fetch_is_booked_in_the_weight_window():
    async def scenario():
        d = _daemon(positions_daemon_fetch_enabled=True, positions_daemon_fetch_interval_s=0.05)
        d._positions_fetch_targets[("hyperliquid", "0xabc")] = "0xAbC"

        class _Client:
            async def fetch_positions(self):
                return []

        async def _client(exchange, wallet):
            return _Client()

        d._positions_fetch_client = _client
        bucket = d._get_budget("hyperliquid")
        before = bucket.weight_used_last_min
        task = asyncio.ensure_future(d._periodic_positions_fetch())
        await asyncio.sleep(0.2)
        d._shutdown_event.set()
        await asyncio.wait_for(task, timeout=2)
        assert bucket.weight_used_last_min > before

    asyncio.run(scenario())


# -------------------- independent review fixes


def _tight_window(b: TokenBucket, weight: float, first_leaves_in: float = 0.3) -> None:
    """A window at ``weight`` whose ten entries leave it one after the other, 0.1 s apart."""
    now = time.monotonic()
    for i in range(10):
        b._weight_window.append((now - 60.0 + first_leaves_in + i * 0.1, weight / 10))
    b._weight_used_last_min = weight


def test_prime_and_critical_never_push_the_window_past_the_budget():
    """Review 2: the drain loop granted PRIME and CRITICAL with the window already full
    (a repro reached 1420/1140). They now wait for the oldest entries to leave."""

    async def scenario():
        b = _bucket(armed=True)
        _tight_window(b, BUDGET)  # 100 % used
        peak = 0.0
        started = time.monotonic()
        first_grant_after = None
        for i in range(20):
            band = TokenBucket.PRIME if i % 2 == 0 else TokenBucket.CRITICAL
            kw = {"bot_id": "multi"} if band == TokenBucket.PRIME else {}
            await asyncio.wait_for(b.acquire(2.0, priority=band, **kw), timeout=10)
            if first_grant_after is None:
                first_grant_after = time.monotonic() - started
            peak = max(peak, b.weight_used_last_min)
        await _stop(b)
        return peak, first_grant_after

    peak, first_grant_after = asyncio.run(scenario())
    assert peak <= BUDGET + 1e-6, f"window reached {peak:.0f}/{BUDGET}"
    assert first_grant_after >= 0.25, "the first grant waited for the window to decay"


def test_the_ceiling_wait_does_not_hold_the_bucket_lock():
    async def scenario():
        b = _bucket(armed=True)
        _tight_window(b, BUDGET, first_leaves_in=1.5)
        task = asyncio.ensure_future(b.acquire(2.0, priority=TokenBucket.PRIME, bot_id="multi"))
        await asyncio.sleep(0.2)
        assert not task.done()
        assert not b._lock.locked(), "the pause happens outside the lock"
        await asyncio.wait_for(task, timeout=5)
        await _stop(b)

    asyncio.run(scenario())


def test_without_a_priority_bot_critical_is_not_held_by_the_ceiling():
    """Review 10: disarmed, the bucket behaves as it always did (CRITICAL passes)."""

    async def scenario():
        b = _bucket(armed=False)
        _tight_window(b, BUDGET, first_leaves_in=5.0)
        started = time.monotonic()
        await asyncio.wait_for(b.acquire(2.0, priority=TokenBucket.CRITICAL), timeout=2)
        assert time.monotonic() - started < 0.5
        await _stop(b)

    asyncio.run(scenario())


def test_the_ceiling_wait_computation():
    b = _bucket(armed=True)
    assert b._ceiling_wait(10.0) == 0.0
    _tight_window(b, BUDGET, first_leaves_in=0.5)
    assert b._ceiling_wait(1.0) == pytest.approx(0.5, abs=0.1)  # first entry frees 114
    assert b._ceiling_wait(200.0) == pytest.approx(0.6, abs=0.1)  # needs two entries
    flat = TokenBucket(rate_per_s=5.0, burst=10.0, exchange="x")
    assert flat._ceiling_wait(1000.0) == 0.0, "no weight mode, no ceiling"


def _differential_order(bucket_cls) -> list[str]:
    """Order in which a CRITICAL flood and two NORMAL waiters are served over the gate."""

    async def scenario():
        b = bucket_cls(
            rate_per_s=19.0,
            burst=150.0,
            weight_mode=True,
            weight_budget_per_min=BUDGET,
            exchange="hyperliquid",
        )
        b._POST_BACKOFF_MAX_BURST = 0  # no 2 s spacing: the order is what is compared
        _fill_window(b, BUDGET * 0.85)  # above the 80 % gate, below the ceiling
        order: list[str] = []

        def tag(name):
            return lambda fut: None if fut.cancelled() else order.append(name)

        waiters = []
        for i in range(8):
            fut = _enqueue(b, TokenBucket.CRITICAL, capital=100.0 - i)
            fut.add_done_callback(tag(f"C{i}"))
            waiters.append(fut)
        for i in range(2):
            fut = _enqueue(b, TokenBucket.NORMAL, capital=50.0 - i)
            fut.add_done_callback(tag(f"N{i}"))
            waiters.append(fut)
        b._ensure_drain()
        await asyncio.sleep(1.5)
        for fut in waiters:
            fut.cancel()
        await _stop(b)
        return order

    return asyncio.run(scenario())


def test_the_disarmed_bucket_selects_exactly_like_the_daemon_of_today():
    """Review 10: ``over_gate`` must only apply once a priority bot arms the bucket. The
    starvation share of the daemon in production reaches a NORMAL waiter even above the
    gate; the new daemon must do the same while it has no priority bot."""
    from tests.ftcache_sim import load_legacy_daemon_module

    legacy = load_legacy_daemon_module()
    if legacy is None:
        pytest.skip("pre-PRIME daemon.py not readable from git in this checkout")
    expected = _differential_order(legacy.TokenBucket)
    assert "N0" in expected and expected.index("N0") < len(expected) - 1, (
        "the scenario really exercises the starvation share over the gate",
        expected,
    )
    assert _differential_order(TokenBucket) == expected


def test_an_armed_bucket_does_not_leak_a_non_urgent_waiter_over_the_gate():
    order = _differential_order_armed()
    assert "N0" not in order and "N1" not in order


def _differential_order_armed() -> list[str]:
    async def scenario():
        b = _bucket(armed=True)
        b._POST_BACKOFF_MAX_BURST = 0
        _fill_window(b, BUDGET * 0.85)
        order: list[str] = []
        waiters = []
        for i in range(8):
            fut = _enqueue(b, TokenBucket.CRITICAL, capital=100.0 - i)
            fut.add_done_callback(lambda f, n=f"C{i}": None if f.cancelled() else order.append(n))
            waiters.append(fut)
        fut = _enqueue(b, TokenBucket.NORMAL)
        fut.add_done_callback(lambda f: None if f.cancelled() else order.append("N0"))
        waiters.append(fut)
        b._ensure_drain()
        await asyncio.sleep(1.5)
        for fut in waiters:
            fut.cancel()
        await _stop(b)
        return order

    return asyncio.run(scenario())


def test_all_priority_bots_together_stay_inside_the_reserve():
    """Review 9: three bots under their own cap could still take 3 x 150 = 450 of the
    285 the reserve is worth. A global ceiling demotes the excess to CRITICAL."""

    async def scenario():
        b = _bucket(armed=True, prime_cap_weight_per_min=150.0)
        assert b.prime_global_cap == pytest.approx(BUDGET * 0.25)
        for bot in ("a", "b", "c"):
            for _ in range(7):  # 140 each, under the per-bot cap of 150
                await asyncio.wait_for(
                    b.acquire(20.0, priority=TokenBucket.PRIME, bot_id=bot), timeout=2
                )
        await _stop(b)
        return b

    b = asyncio.run(scenario())
    assert b.weight_by_band[TokenBucket.PRIME] <= BUDGET * 0.25
    assert b.prime_global_demotions >= 1 and b.prime_demotions >= b.prime_global_demotions
    assert b.grants_by_band[TokenBucket.PRIME] + b.grants_by_band[TokenBucket.CRITICAL] == 21, (
        "demoted, never refused"
    )


def test_the_global_prime_ceiling_is_configurable_and_slides():
    async def scenario():
        b = _bucket(armed=True, prime_global_cap_weight_per_min=60.0)
        assert b.prime_global_cap == 60.0
        for _ in range(3):
            await b.acquire(20.0, priority=TokenBucket.PRIME, bot_id="a")
        assert b.prime_global_demotions == 0
        await b.acquire(20.0, priority=TokenBucket.PRIME, bot_id="b")
        assert b.prime_global_demotions == 1
        b._prime_global_window = type(b._prime_global_window)(
            (ts - 120.0, c) for ts, c in b._prime_global_window
        )
        await b.acquire(20.0, priority=TokenBucket.PRIME, bot_id="b")
        assert b.prime_global_demotions == 1
        await _stop(b)

    asyncio.run(scenario())


def test_the_global_ceiling_reaches_the_bucket_from_the_daemon_config_and_the_stats():
    async def scenario():
        d = _daemon(prime_global_cap_weight_per_min=90)
        _register(d, 1, "multi", priority_bot=True)
        assert d._get_budget("hyperliquid").prime_global_cap_weight_per_min == 90
        stats = await d._dispatch({"op": "stats", "req_id": "s"}, 0)
        assert stats["prime"]["global_cap_weight_per_min"] == 90
        assert stats["prime"]["global_demotions"] == 0

    asyncio.run(scenario())


def test_the_candles_of_a_priority_bot_get_no_bonus_in_critical():
    """Review 4: at CRITICAL the bonus put a candle read ahead of other bots' orders."""

    async def scenario():
        d = _daemon()
        calls: list = []
        d._get_fetcher = lambda ex, tm: _RecordingFetcher(calls)
        _register(d, 1, "multi", priority_bot=True)
        await d._handle_fetch(_fetch_req(pair="A/USDC:USDC", priority=0), conn_id=1)
        await d._handle_fetch(_fetch_req(pair="B/USDC:USDC", priority=1), conn_id=1)
        by_pair = {c["pair"]: c for c in calls}
        assert by_pair["A/USDC:USDC"]["priority"] == TokenBucket.CRITICAL
        assert by_pair["A/USDC:USDC"]["capital"] == 100.0, "no bonus in CRITICAL"
        assert by_pair["B/USDC:USDC"]["capital"] > 1e8, "the bonus stays below CRITICAL"

    asyncio.run(scenario())


def _seed_series(d: Daemon, pair: str, offset: int) -> None:
    tf = 15 * 60_000
    series = d.store.get_or_create("hyperliquid", "futures", pair, "15m", "futures", tf)
    forming = (int(time.time() * 1000) // tf) * tf
    end = forming - offset * tf
    rows = [[end - i * tf, 1, 1, 1, 1, 1] for i in range(40, -1, -1)]
    series.merge(rows, fetched_at_ms=int(time.time() * 1000), covered=(rows[0][0], end + tf))


def test_a_priority_bot_gets_one_synchronous_fill_per_window_not_one_per_series():
    """Review 5: the client sends its requests one after the other; twenty stale series at a
    few seconds each held the bot's cycle for minutes."""

    async def scenario():
        d = _daemon(prime_sync_fill_max_s=2.0, prime_sync_fill_window_s=30.0)
        d._get_fetcher = lambda ex, tm: _Ticking(0.3)
        _register(d, 1, "multi", priority_bot=True)
        _register(d, 2, "other-prime", priority_bot=True)
        pairs = [f"P{i}/USDC:USDC" for i in range(4)]
        for pair in pairs:
            _seed_series(d, pair, 1)
        started = time.monotonic()
        first = await d._handle_fetch(_fetch_req(pair=pairs[0], hot=True), conn_id=1)
        first_took = time.monotonic() - started
        assert first["served_from"] in {"partial", "fetch"}, "the one synchronous fill"
        assert first_took >= 0.25
        started = time.monotonic()
        rest = [await d._handle_fetch(_fetch_req(pair=p, hot=True), conn_id=1) for p in pairs[1:]]
        assert time.monotonic() - started < 0.25, "the others do not stack their waits"
        assert [r["served_from"] for r in rest] == ["stale"] * 3
        # another bot has its own budget
        other = await d._handle_fetch(_fetch_req(pair=pairs[1], hot=True), conn_id=2)
        assert other["served_from"] in {"partial", "fetch"}
        # the window passes: the first bot gets a synchronous fill again
        d._prime_sync_fill_at["multi"] -= 31.0
        again = await d._handle_fetch(_fetch_req(pair=pairs[2], hot=True), conn_id=1)
        assert again["served_from"] in {"partial", "fetch", "cache"}
        await asyncio.gather(*list(d._swr_tasks), return_exceptions=True)

    asyncio.run(scenario())


def test_the_stale_series_served_after_the_budget_are_still_filled_in_the_background():
    async def scenario():
        calls: list = []
        d = _daemon(prime_sync_fill_window_s=30.0)
        d._get_fetcher = lambda ex, tm: _RecordingFetcher(calls)
        _register(d, 1, "multi", priority_bot=True)
        d._prime_sync_fill_at["multi"] = time.monotonic()  # budget already spent
        _seed_series(d, "Z/USDC:USDC", 1)
        resp = await d._handle_fetch(_fetch_req(pair="Z/USDC:USDC", hot=True), conn_id=1)
        assert resp["served_from"] == "stale"
        await asyncio.gather(*list(d._swr_tasks), return_exceptions=True)
        assert [c["pair"] for c in calls] == ["Z/USDC:USDC"], "the fill still happens"

    asyncio.run(scenario())


def test_the_prime_deadline_of_the_daemon_is_under_the_client_timeout():
    """Review 6: the daemon waited 600 s for a request the client abandoned after 120 s,
    then granted (and counted) it for nobody, at the head of the PRIME band."""
    from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin

    d = _daemon()
    assert d._prime_acquire_max_wait_s < CachedExchangeMixin._ACQUIRE_TIMEOUT_S
    assert d._prime_acquire_max_wait_s >= CachedExchangeMixin._ACQUIRE_TIMEOUT_S - 60


def test_an_abandoned_prime_request_leaves_the_queue_and_is_not_counted():
    async def scenario():
        d = _daemon()
        d._prime_acquire_max_wait_s = 0.3
        _register(d, 1, "multi", priority_bot=True)
        bucket = d._get_budget("hyperliquid")
        _tight_window(bucket, BUDGET, first_leaves_in=1.2)  # full for 1.2 s
        before = bucket.weight_used_last_min
        resp = await d._handle_acquire(
            {"op": "acquire", "req_id": "p", "exchange": "hyperliquid", "priority": -1, "cost": 4},
            conn_id=1,
        )
        assert resp["ok"] is False and resp["throttled"] is True
        await asyncio.sleep(2.5)  # the window decays: the dead request must not be served now
        assert not any(not w[4].done() for w in bucket._waiters), "nothing left waiting"
        assert bucket.grants_by_band.get(TokenBucket.PRIME, 0) == 0
        assert bucket.weight_used_last_min <= before, "the abandoned request is not booked"
        await _stop(bucket)

    asyncio.run(scenario())


def test_reported_weight_is_booked_with_the_next_acquire():
    """Review 3: weight a priority bot spent without a token (loop lock busy) is reported."""

    async def scenario():
        d = _daemon()
        _register(d, 1, "multi", priority_bot=True)
        bucket = d._get_budget("hyperliquid")
        req = {"op": "acquire", "req_id": "a", "exchange": "hyperliquid", "priority": -1}
        await d._handle_acquire({**req, "cost": 2}, conn_id=1)
        base = bucket.weight_used_last_min
        await d._handle_acquire({**req, "cost": 2, "reported_weight": 9.0}, conn_id=1)
        assert bucket.weight_used_last_min == pytest.approx(base + 2 + 9.0, abs=0.5)
        await d._handle_acquire({**req, "cost": 2, "reported_weight": "garbage"}, conn_id=1)
        await d._handle_acquire({**req, "cost": 2, "reported_weight": -5}, conn_id=1)
        assert bucket.weight_used_last_min == pytest.approx(base + 2 + 9.0 + 4, abs=0.5)
        await d._handle_acquire({**req, "cost": 2, "reported_weight": 1e6}, conn_id=1)
        assert bucket.weight_used_last_min <= base + 2 + 9.0 + 4 + 2 + bucket.burst + 0.5
        await _stop(bucket)

    asyncio.run(scenario())


# -------------------- second review


def _plain_bucket() -> TokenBucket:
    return TokenBucket(
        rate_per_s=19.0,
        burst=150.0,
        weight_mode=True,
        weight_budget_per_min=BUDGET,
        exchange="hyperliquid",
    )


def test_a_critical_request_does_not_wake_the_pacing_sleep_without_a_priority_bot():
    """Review A: without a priority bot a CRITICAL flood skipped the 2 s spacing of the
    drain loop (8 grants in 0.44 s during a 429 backoff, one per 2 s on main)."""

    async def flag_after_enqueue(bucket, priority, **kw) -> bool:
        bucket._ensure_drain = lambda: None  # no drain loop to clear the flag under us
        bucket._urgent.clear()
        task = asyncio.ensure_future(bucket.acquire(1.0, priority=priority, **kw))
        bucket.tokens = 0.0
        await asyncio.sleep(0.05)
        flag = bucket._urgent.is_set()
        task.cancel()
        return flag

    async def scenario():
        plain = _plain_bucket()
        assert plain.prime_enabled is False
        plain.tokens = 0.0
        critical = await flag_after_enqueue(plain, TokenBucket.CRITICAL)
        prime = await flag_after_enqueue(_plain_bucket(), TokenBucket.PRIME, bot_id="x")
        armed = await flag_after_enqueue(_bucket(armed=True), TokenBucket.CRITICAL)
        high = await flag_after_enqueue(_bucket(armed=True), TokenBucket.HIGH)
        return critical, prime, armed, high

    critical, prime, armed, high = asyncio.run(scenario())
    assert critical is False, "CRITICAL does not cut the pacing short when disarmed"
    assert prime is True
    assert armed is True, "once armed, CRITICAL is urgent too"
    assert high is False


def test_disarmed_critical_requests_keep_the_two_second_spacing_in_a_backoff():
    async def scenario():
        b = _plain_bucket()
        b.trigger_backoff(2.0)
        b.tokens = 150.0
        started = time.monotonic()
        tasks = []
        for _ in range(3):  # arrivals while the loop sleeps its spacing: what woke it
            tasks.append(asyncio.ensure_future(b.acquire(1.0, priority=TokenBucket.CRITICAL)))
            await asyncio.sleep(0.05)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
        elapsed = time.monotonic() - started
        await _stop(b)
        return elapsed

    # 3 grants, the first two gaps spaced 2 s: >= 4 s; the bug made it ~0.2 s
    assert asyncio.run(scenario()) >= 3.5


def test_prime_is_spaced_lightly_in_a_backoff():
    """In a 429 backoff PRIME keeps the head of the line but is spaced 0.5 s (not the fleet's
    2 s): unspaced PRIME retries hammered the exchange while it refused the IP (22/10)."""
    async def scenario():
        b = _bucket(armed=True)
        b.trigger_backoff(2.0)
        b.tokens = 150.0
        started = time.monotonic()
        tasks = [
            asyncio.ensure_future(b.acquire(1.0, priority=TokenBucket.PRIME, bot_id="m"))
            for _ in range(5)
        ]
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
        elapsed = time.monotonic() - started
        await _stop(b)
        return elapsed

    elapsed = asyncio.run(scenario())
    # 5 grants, 4 gaps of 0.5 s: about 2 s; the fleet spacing would be 8 s
    assert 1.8 <= elapsed < 3.5


class _FullFetcher:
    """Answers any range with candles up to the forming one (what the exchange returns)."""

    def __init__(self, delay=0.0):
        self.delay = delay

    async def fetch_ohlcv(self, pair, timeframe, since_ms, limit, candle_type, **kw):
        await asyncio.sleep(self.delay)
        tf = 15 * 60_000
        end = (int(time.time() * 1000) // tf) * tf
        return [[end - i * tf, 1, 1, 1, 1, 1] for i in range(limit, -1, -1)]


def _seed_n(d: Daemon, pair: str, offset: int, n: int) -> None:
    tf = 15 * 60_000
    series = d.store.get_or_create("hyperliquid", "futures", pair, "15m", "futures", tf)
    forming = (int(time.time() * 1000) // tf) * tf
    end = forming - offset * tf
    rows = [[end - i * tf, 1, 1, 1, 1, 1] for i in range(n, -1, -1)]
    series.merge(rows, fetched_at_ms=int(time.time() * 1000), covered=(rows[0][0], end + tf))


def test_the_sync_fill_budget_never_gives_a_priority_bot_less_than_a_fleet_bot():
    """Review B: with the budget spent, a series 20 candles behind (beyond the fleet SWR cap)
    came back stale with a priority bot while any other bot waited for its fill."""

    async def scenario():
        d = _daemon()
        d._get_fetcher = lambda ex, tm: _FullFetcher()
        _register(d, 1, "multi", priority_bot=True)
        d._prime_sync_fill_at["multi"] = time.monotonic()  # the budget is spent
        _seed_n(d, "A/USDC:USDC", 20, 40)
        resp = await d._handle_fetch(_fetch_req(pair="A/USDC:USDC", hot=True), conn_id=1)
        await asyncio.gather(*list(d._swr_tasks), return_exceptions=True)
        return resp

    resp = asyncio.run(scenario())
    assert resp["served_from"] in {"partial", "fetch"}, "refilled synchronously, like any bot"


def test_a_history_longer_than_the_cache_is_never_truncated_for_a_priority_bot():
    async def scenario():
        d = _daemon()
        d._get_fetcher = lambda ex, tm: _FullFetcher()
        _register(d, 1, "multi", priority_bot=True)
        d._prime_sync_fill_at["multi"] = time.monotonic()
        _seed_n(d, "B/USDC:USDC", 0, 40)  # 41 candles cached, 300 requested
        resp = await d._handle_fetch(_fetch_req(pair="B/USDC:USDC", limit=300, hot=True), conn_id=1)
        await asyncio.gather(*list(d._swr_tasks), return_exceptions=True)
        return resp

    resp = asyncio.run(scenario())
    assert resp["served_from"] in {"partial", "fetch"}


def test_outside_the_fleet_envelope_the_wait_is_not_cut_at_the_sync_fill_bound():
    async def scenario():
        d = _daemon(prime_sync_fill_max_s=0.1)
        d._get_fetcher = lambda ex, tm: _FullFetcher(delay=0.4)
        _register(d, 1, "multi", priority_bot=True)
        _seed_n(d, "C/USDC:USDC", 20, 40)  # beyond the fleet cap: a fleet bot waits 0.4 s
        started = time.monotonic()
        resp = await d._handle_fetch(_fetch_req(pair="C/USDC:USDC", hot=True), conn_id=1)
        return resp, time.monotonic() - started

    resp, took = asyncio.run(scenario())
    assert resp["served_from"] in {"partial", "fetch"} and took >= 0.35


def test_inside_the_fleet_envelope_the_budget_still_protects_the_cycle():
    async def scenario():
        d = _daemon(prime_sync_fill_max_s=0.1)
        d._get_fetcher = lambda ex, tm: _FullFetcher(delay=0.5)
        _register(d, 1, "multi", priority_bot=True)
        _seed_n(d, "D/USDC:USDC", 1, 40)  # one period late: inside the fleet's 8 candles
        started = time.monotonic()
        resp = await d._handle_fetch(_fetch_req(pair="D/USDC:USDC", hot=True), conn_id=1)
        took = time.monotonic() - started
        for task in list(d._swr_tasks):
            task.cancel()
        return resp, took

    resp, took = asyncio.run(scenario())
    assert resp["served_from"] == "stale" and took < 0.4
