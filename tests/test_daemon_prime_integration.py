"""A private daemon under a simulated fleet at about 80 % of the weight budget.

Three daemons run side by side for the same 60 seconds, each with its own copy of the
same fleet (30 closed-loop bots, a candle-close burst, an open-loop trickle of LOW
background refreshes, the weight window starting at 80 % of the budget) and two bots
sending trading calls:

* ``prime``   the daemon of this branch, the two bots registered as priority bots;
* ``reserve`` the same daemon, the two bots as ordinary bots (calls in the bands they
              use today): isolates the cost of the 75 % gate from that of PRIME;
* ``legacy``  the daemon the fleet runs today (``daemon.py`` before this branch, read
              from git): the "before" picture. Skipped when git cannot provide it.

The exchange behind every daemon is a fake that sleeps; nothing leaves the process and
no real socket, persistence directory or exchange is involved.

The table is printed (``pytest -s``) and written next to the test's temporary files.

Stated bounds (measured values, over several runs, in DESIGN terms):

* priority calls: p95 < 0.25 s and max < 1.5 s (measured: p95 ~ 1 ms, max < 10 ms),
  against p95 ~ 11 s and max ~ 14 s on the pre-PRIME daemon;
* orders of priority bots: same bound;
* non-priority bots, at the cost of the 25 % reserve and of the 150 weight/min the two
  priority bots use: no request shed, HIGH p95 < 20 s, and the fleet still gets at
  least 60 % of what it got with the reserve armed but unused (``reserve`` scenario).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from tests.ftcache_sim import PrivateDaemon, load_legacy_daemon_module, run_fleet


DURATION_S = 60.0


@pytest.fixture(autouse=True)
def _preserve_event_loop():
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        previous = None
    yield
    asyncio.set_event_loop(previous)


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    """Run the scenarios once; the tests below read their results."""
    root = tmp_path_factory.mktemp("ftcache-sim")
    legacy_module = load_legacy_daemon_module()
    scenarios: dict[str, tuple[PrivateDaemon, bool]] = {}
    for name, module, priority_mode in (
        ("prime", None, True),
        ("reserve", None, False),
        ("legacy", legacy_module, False),
    ):
        if name == "legacy" and legacy_module is None:
            continue
        d = root / name
        d.mkdir()
        scenarios[name] = (PrivateDaemon(d, daemon_module=module).start(), priority_mode)

    async def run_all():
        names = list(scenarios)
        results = await asyncio.gather(
            *(
                run_fleet(
                    scenarios[n][0].socket_path,
                    daemon=scenarios[n][0],
                    duration_s=DURATION_S,
                    priority_mode=scenarios[n][1],
                    seed=11,
                )
                for n in names
            )
        )
        return dict(zip(names, results, strict=True))

    try:
        results = asyncio.run(run_all())
    finally:
        for daemon, _ in scenarios.values():
            with contextlib.suppress(Exception):
                daemon.stop()
    report = []
    for name, res in results.items():
        report.append(f"=== {name}\n{res.table()}\n")
    text = "\n".join(report)
    Path(root / "latency_table.txt").write_text(text)
    print("\n" + text)
    results["_report_path"] = root / "latency_table.txt"
    return results


TRADING = "trading calls (priority bots)"


def test_priority_calls_are_served_at_once_under_a_fleet_at_80_percent(sim):
    row = sim["prime"].row(TRADING)
    assert row["n"] >= 50, "the bots must have been able to trade freely"
    assert row["p95"] < 0.25, row
    assert row["max"] < 1.5, row
    assert sim["prime"].oldest_unserved(TRADING) < 1.5
    assert sim["prime"].mean_util_pct >= 65, "the scenario really ran near the budget"


def test_orders_of_priority_bots_are_served_at_once(sim):
    row = sim["prime"].row("  of which orders")
    assert row["n"] >= 15
    assert row["p95"] < 0.25 and row["max"] < 1.5, row


def test_the_same_calls_wait_for_seconds_without_priority(sim):
    """On the same load, with the bots as ordinary bots, the calls queue behind the fleet."""
    row = sim["reserve"].row(TRADING)
    assert row["p95"] > 1.0, f"the scenario is not loaded enough to show the problem: {row}"
    assert row["p95"] > 10 * sim["prime"].row(TRADING)["p95"]


def test_the_pre_prime_daemon_is_worse_for_the_same_calls(sim):
    if "legacy" not in sim:
        pytest.skip("pre-PRIME daemon not readable from git")
    legacy = sim["legacy"].row(TRADING)
    assert legacy["p95"] > 1.0, legacy
    assert legacy["p95"] > 10 * sim["prime"].row(TRADING)["p95"]


def test_non_priority_bots_are_not_starved_beyond_the_stated_bound(sim):
    prime = sim["prime"]
    # nobody is shed (the daemon's own deadline is 90 s)
    assert not prime.shed, prime.shed
    # HIGH is only held back by the 75 % gate. The number of requests the closed-loop bots
    # manage to issue in 60 s depends on timing (a bot only sends its next request once the
    # previous one is served), so no minimum is asserted on it: what is asserted is what
    # became of the requests that WERE issued.
    high = prime.row("fleet HIGH")
    still_waiting = len(prime.unserved.get("fleet HIGH", []))
    assert high["n"] + still_waiting >= 1, "the fleet really issued HIGH requests"
    if high["n"]:
        assert high["p95"] < 20.0, high
        assert high["n"] >= still_waiting, "more HIGH requests served than left waiting"
    # no HIGH request is left behind at the end of the run
    assert prime.oldest_unserved("fleet HIGH") < 20.0
    assert prime.oldest_unserved("background HIGH") < 20.0


def test_the_fleet_keeps_most_of_its_throughput(sim):
    """The reserve plus the priority bots' own traffic cost the fleet a bounded share."""
    prime = sim["prime"]
    reserve = sim["reserve"]
    served_prime = prime.served_weight("fleet") + prime.served_weight("background")
    served_reserve = reserve.served_weight("fleet") + reserve.served_weight("background")
    assert served_reserve > 0
    assert served_prime >= 0.60 * served_reserve, (served_prime, served_reserve)
    # and the whole budget is still being used, not parked behind the reserve
    assert prime.mean_util_pct >= 65


def test_the_pre_prime_fleet_throughput_is_the_reference(sim):
    if "legacy" not in sim:
        pytest.skip("pre-PRIME daemon not readable from git")
    legacy = sim["legacy"]
    prime = sim["prime"]
    served_legacy = legacy.served_weight("fleet") + legacy.served_weight("background")
    served_prime = prime.served_weight("fleet") + prime.served_weight("background")
    # the fleet gets at least 60 % of what it got before, while the priority bots now
    # complete several times more calls in the same minute
    assert served_prime >= 0.60 * served_legacy, (served_prime, served_legacy)
    assert prime.row(TRADING)["n"] > 2 * legacy.row(TRADING)["n"]


def test_the_latency_table_was_written(sim):
    text = Path(sim["_report_path"]).read_text()
    assert "=== prime" in text and "trading calls (priority bots)" in text


# --------------------------------------------------------------------------- standing queue


def test_prime_p95_stays_under_a_second_behind_a_standing_queue_of_300():
    """DESIGN L2 acceptance: 300 fleet requests queued, PRIME arriving all the while."""
    from freqtrade.ohlcv_cache.daemon import TokenBucket

    async def scenario():
        b = TokenBucket(
            rate_per_s=19.0,
            burst=150.0,
            weight_mode=True,
            weight_budget_per_min=1140,
            exchange="hyperliquid",
        )
        b.prime_enabled = True
        fleet = []
        for i in range(300):
            band = (TokenBucket.HIGH, TokenBucket.NORMAL, TokenBucket.LOW)[i % 3]
            fleet.append(asyncio.ensure_future(b.acquire(21.0, priority=band, capital=i)))
        await asyncio.sleep(0.2)
        assert len(b._waiters) >= 250, "the queue really is standing"
        waits = []
        for i in range(60):
            started = asyncio.get_event_loop().time()
            await b.acquire(1.0 + (i % 2), priority=TokenBucket.PRIME, bot_id=f"bot{i % 2}")
            waits.append(asyncio.get_event_loop().time() - started)
            await asyncio.sleep(0.1)
        for f in fleet:
            f.cancel()
        await asyncio.gather(*fleet, return_exceptions=True)
        drain = b._drain_task
        if drain is not None and not drain.done():
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain
        return sorted(waits)

    waits = asyncio.run(scenario())
    p95 = waits[int(len(waits) * 0.95)]
    assert p95 < 1.0, waits[-5:]
    assert waits[len(waits) // 2] < 0.1
