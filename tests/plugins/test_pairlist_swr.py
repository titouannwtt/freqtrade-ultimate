"""
Tests of the opt-in stale-while-revalidate mode of TrendRegularityFilter and ExtremeMoveFilter.

Both filters are run through the same scenarios (parametrized on `kind`). The exchange is a
mock whose `refresh_ohlcv_with_cache` serves candles from a dict and records each request.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from freqtrade.enums import CandleType
from freqtrade.exceptions import OperationalException
from freqtrade.plugins.pairlist.ExtremeMoveFilter import ExtremeMoveFilter
from freqtrade.plugins.pairlist.swr_verdicts import (
    MAX_AGE_FACTOR,
    RETRY_AFTER_FAILURE_S,
    StaleVerdictStore,
)
from freqtrade.plugins.pairlist.TrendRegularityFilter import TrendRegularityFilter


PERIOD = 3600
N = 2
START = datetime(2024, 8, 1, 0, 7, 0, tzinfo=UTC)  # 7 min after a 15m close: not quiet by default


def _trend_series(rng: np.random.Generator) -> pd.DataFrame:
    slope = rng.uniform(-1, 1)
    noise = rng.uniform(0, 12)
    closes = 100 + slope * np.arange(60) + rng.normal(0, noise, 60)
    return pd.DataFrame({"close": np.abs(closes) + 1})


def _move_series(rng: np.random.Generator) -> pd.DataFrame:
    change = rng.uniform(-0.5, 0.8)
    closes = np.linspace(100, 100 * (1 + change), 8) * (1 + rng.normal(0, 0.01, 8))
    return pd.DataFrame({"close": closes})


KINDS = {
    "trend": (
        TrendRegularityFilter,
        {"lookback_timeframe": "1h", "lookback_period": 60, "min_r2": 0.5},
        _trend_series,
    ),
    "move": (
        ExtremeMoveFilter,
        {"lookback_days": 7, "max_up_pct": 30, "max_down_pct": 30},
        _move_series,
    ),
}


class FakeOhlcv:
    """Stands for Exchange.refresh_ohlcv_with_cache: serves `data`, records the requests."""

    def __init__(self, data: dict[str, pd.DataFrame | None]):
        self.data = data
        self.calls: list[list[str]] = []

    def __call__(self, pairs, *, lookback_period):
        if pairs:  # the plain filters also call it with an empty list: not a request
            self.calls.append([p[0] for p in pairs])
        return {p: self.data[p[0]] for p in pairs if self.data.get(p[0]) is not None}


class FakeSharedClient:
    """In-memory stand-in of PairlistCacheClient (mget / mput only)."""

    def __init__(self):
        self.store: dict[str, dict] = {}
        self.mget_calls = 0
        self.mput_calls = 0

    def mget(self, method, params_hash, pairs):
        self.mget_calls += 1
        return {p: self.store.get(p) for p in pairs}

    def mput(self, method, params_hash, entries, ttl):
        self.mput_calls += 1
        self.store.update(entries)


@pytest.fixture(autouse=True)
def no_shared_cache(mocker):
    """Never talk to (or spawn) the real pairlist cache daemon from a test."""
    mocker.patch(
        "freqtrade.pairlist_cache.client.PairlistCacheClient.get_or_spawn",
        side_effect=RuntimeError("shared cache disabled in tests"),
    )


def _data(kind: str, pairs: list[str], seed: int) -> dict[str, pd.DataFrame | None]:
    rng = np.random.default_rng(seed)
    gen = KINDS[kind][2]
    return {p: gen(rng) for p in pairs}


def _make(kind: str, data, swr: bool = False, **extra):
    cls, base, _ = KINDS[kind]
    cfg = {"method": cls.__name__, **base, "refresh_period": PERIOD, **extra}
    if swr:
        cfg["stale_while_revalidate"] = True
        cfg.setdefault("max_recompute_per_cycle", N)
    exchange = MagicMock()
    exchange.ohlcv_candle_limit.return_value = 5000
    fake = FakeOhlcv(data)
    exchange.refresh_ohlcv_with_cache.side_effect = fake
    flt = cls(exchange, MagicMock(), {"candle_type_def": CandleType.FUTURES}, cfg, 1)
    return flt, fake


def _at(time_machine, seconds: float):
    time_machine.move_to(START + timedelta(seconds=seconds), tick=False)


PAIRS = [f"P{i:02d}/USDC:USDC" for i in range(24)]


@pytest.mark.parametrize("kind", KINDS)
def test_swr_off_by_default(kind, time_machine):
    """Default config: no SWR object, one burst per expiry, filter result unchanged."""
    _at(time_machine, 0)
    data = _data(kind, PAIRS, 1)
    flt, fake = _make(kind, data)
    assert flt._swr is None

    first = flt.filter_pairlist(PAIRS, {})
    assert fake.calls == [PAIRS]
    assert 0 < len(first) < len(PAIRS)  # the data set exercises both verdicts

    _at(time_machine, PERIOD - 1)
    assert flt.filter_pairlist(PAIRS, {}) == first
    assert len(fake.calls) == 1  # still cached, nothing requested

    _at(time_machine, PERIOD + 1)
    assert flt.filter_pairlist(PAIRS, {}) == first
    assert fake.calls[1] == PAIRS  # all expire together: the burst SWR exists to avoid


@pytest.mark.parametrize("kind", KINDS)
def test_swr_parameters_are_declared(kind):
    cls = KINDS[kind][0]
    params = cls.available_parameters()
    assert params["stale_while_revalidate"]["default"] is False
    assert params["max_recompute_per_cycle"]["default"] == 2
    assert params["quiet_after_close_s"]["default"] == 0
    for key in ("stale_while_revalidate", "max_recompute_per_cycle", "quiet_after_close_s"):
        assert params[key]["description"] and params[key]["help"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "extra, message",
    [
        ({"max_recompute_per_cycle": 0}, "max_recompute_per_cycle must be >= 1"),
        ({"max_recompute_per_cycle": 1.5}, "max_recompute_per_cycle must be an integer"),
        ({"quiet_after_close_s": 900}, "quiet_after_close_s must be between"),
        ({"quiet_after_close_s": -1}, "quiet_after_close_s must be between"),
        ({"swr_jitter_pct": 0.95}, "swr_jitter_pct must be between"),
    ],
)
def test_swr_invalid_config(kind, extra, message):
    with pytest.raises(OperationalException, match=message):
        _make(kind, {}, swr=True, **extra)


@pytest.mark.parametrize("kind", KINDS)
def test_swr_invalid_values_ignored_when_off(kind):
    flt, _ = _make(kind, {}, max_recompute_per_cycle=0)
    assert flt._swr is None


@pytest.mark.parametrize("kind", KINDS)
def test_swr_cold_start_is_full_and_equal_to_plain(kind, time_machine):
    _at(time_machine, 0)
    data = _data(kind, PAIRS, 2)
    plain, _ = _make(kind, data)
    swr, fake = _make(kind, data, swr=True)

    assert swr.filter_pairlist(PAIRS, {}) == plain.filter_pairlist(PAIRS, {})
    # Cold start: every pair at once, even though max_recompute_per_cycle is 2.
    assert fake.calls == [PAIRS]


@pytest.mark.parametrize("kind", KINDS)
def test_swr_cold_start_ignores_quiet_window(kind, time_machine):
    _at(time_machine, -7 * 60 + 30)  # 30 s after a 15m close
    data = _data(kind, PAIRS, 3)
    swr, fake = _make(kind, data, swr=True, quiet_after_close_s=180)
    assert len(swr.filter_pairlist(PAIRS, {})) > 0
    assert fake.calls == [PAIRS]


@pytest.mark.parametrize("kind", KINDS)
def test_swr_bounded_requests_and_stale_served(kind, time_machine):
    _at(time_machine, 0)
    old = _data(kind, PAIRS, 4)
    swr, fake = _make(kind, old, swr=True)
    first = swr.filter_pairlist(PAIRS, {})
    fake.calls.clear()

    # Everything changes on the market, but verdicts are not due yet: nothing requested.
    new = _data(kind, PAIRS, 5)
    fake.data = new
    plain_new, _ = _make(kind, new)
    expected_new = plain_new.filter_pairlist(PAIRS, {})
    assert expected_new != first  # the scenario is meaningful

    _at(time_machine, PERIOD * 0.7)
    assert swr.filter_pairlist(PAIRS, {}) == first
    assert fake.calls == []

    # Past the expiry of every verdict (jitter included): stale verdicts, N recomputes per call.
    _at(time_machine, PERIOD + 1)
    results = []
    for _ in range(len(PAIRS)):
        results.append(swr.filter_pairlist(PAIRS, {}))
        if len(fake.calls) * N >= len(PAIRS):
            break
    assert fake.calls and all(len(c) <= N for c in fake.calls)
    assert all(len(c) == N for c in fake.calls[:-1])  # budget fully used while backlog remains
    # The first call after expiry still serves (mostly) the old verdicts: only N pairs moved.
    assert len(set(results[0]) ^ set(first)) <= N
    # Each pair recomputed exactly once.
    requested = [p for c in fake.calls for p in c]
    assert sorted(requested) == sorted(PAIRS)
    # Fully recomputed: identical to a plain filter fed with the same data.
    assert swr.filter_pairlist(PAIRS, {}) == expected_new
    assert len(fake.calls) == len(requested) // N


@pytest.mark.parametrize("kind", KINDS)
def test_swr_oldest_verdict_first(kind, time_machine):
    pairs = PAIRS[:6]
    data = _data(kind, pairs, 6)
    swr, fake = _make(kind, data, swr=True, swr_jitter_pct=0)
    # Seed verdicts at different times, pairs listed in an order unrelated to age.
    store = swr._swr
    ages = {pairs[0]: 50, pairs[1]: 10, pairs[2]: 40, pairs[3]: 0, pairs[4]: 30, pairs[5]: 20}
    for p, age in ages.items():
        store.put(p, False, START.timestamp() - age)
    store.warm = True

    _at(time_machine, PERIOD + 100)  # every verdict is due
    swr.filter_pairlist(pairs, {})
    swr.filter_pairlist(pairs, {})
    swr.filter_pairlist(pairs, {})
    order = [p for c in fake.calls for p in c]
    assert order == [pairs[0], pairs[2], pairs[4], pairs[5], pairs[1], pairs[3]]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("seed", [10, 11, 12, 13, 14])
def test_swr_converges_to_plain_verdicts(kind, seed, time_machine):
    """Property: once everything is recomputed, SWR and plain filters keep the same pairs."""
    rng = np.random.default_rng(seed)
    pairs = [f"R{i:02d}/USDC:USDC" for i in range(int(rng.integers(8, 30)))]
    swr, fake = _make(kind, _data(kind, pairs, seed), swr=True, swr_jitter_pct=0.3)
    t = 0.0
    _at(time_machine, t)
    swr.filter_pairlist(pairs, {})

    for round_ in range(3):
        market = _data(kind, pairs, seed * 100 + round_)
        fake.data = market
        fake.calls.clear()
        t += PERIOD + 1
        _at(time_machine, t)
        for _ in range(len(pairs) + 2):
            swr.filter_pairlist(pairs, {})
        assert all(len(c) <= N for c in fake.calls)
        assert sorted(p for c in fake.calls for p in c) == sorted(pairs)
        plain, _ = _make(kind, market)
        assert swr.filter_pairlist(pairs, {}) == plain.filter_pairlist(pairs, {})


@pytest.mark.parametrize("kind", KINDS)
def test_swr_new_pairs_not_allowed_until_computed(kind, time_machine):
    _at(time_machine, 0)
    known = PAIRS[:10]
    new = PAIRS[10:15]
    data = _data(kind, PAIRS, 7)
    swr, fake = _make(kind, data, swr=True)
    swr.filter_pairlist(known, {})
    fake.calls.clear()

    plain, _ = _make(kind, data)
    expected = plain.filter_pairlist(PAIRS[:15], {})
    assert set(expected) & set(new)  # scenario check: some new pairs are allowed

    # Verdicts are not due, but five pairs have none: N per call, never allowed before computed.
    for i in range(2):
        _at(time_machine, 10 * (i + 1))
        result = swr.filter_pairlist(known + new, {})
        assert len(fake.calls[-1]) <= N
        assert set(fake.calls[-1]) <= set(new)
        computed = {p for c in fake.calls for p in c}
        assert set(result) & set(new) <= computed
    assert [p for c in fake.calls for p in c] == new[: 2 * N]
    assert not set(result) & {new[-1]}  # the fifth one is still waiting, hence removed

    result = swr.filter_pairlist(known + new, {})
    assert [p for c in fake.calls for p in c] == new
    assert sorted(result) == sorted(expected)
    assert len(fake.calls) == 3


@pytest.mark.parametrize("kind", KINDS)
def test_swr_missing_pairs_before_due_ones(kind, time_machine):
    _at(time_machine, 0)
    known = PAIRS[:6]
    data = _data(kind, PAIRS, 8)
    swr, fake = _make(kind, data, swr=True)
    swr.filter_pairlist(known, {})
    fake.calls.clear()

    _at(time_machine, PERIOD + 5)  # all known pairs due
    swr.filter_pairlist(known + PAIRS[6:8], {})
    assert fake.calls == [PAIRS[6:8]]


@pytest.mark.parametrize("kind", KINDS)
def test_swr_too_old_verdict_is_dropped(kind, time_machine):
    _at(time_machine, 0)
    data = _data(kind, PAIRS[:4], 9)
    swr, fake = _make(kind, data, swr=True)
    swr.filter_pairlist(PAIRS[:4], {})
    fake.calls.clear()

    # No call for a long time: past the hard limit the old verdicts are no longer trusted.
    _at(time_machine, PERIOD * MAX_AGE_FACTOR + 10)
    result = swr.filter_pairlist(PAIRS[:4], {})
    assert len(fake.calls) == 1 and len(fake.calls[0]) == N
    allowed_now = set(result)
    assert allowed_now <= set(fake.calls[0])  # only the pairs recomputed just now can be allowed


@pytest.mark.parametrize("kind", KINDS)
def test_swr_jitter_spreads_expirations(kind, time_machine):
    pairs = [f"J{i:02d}/USDC:USDC" for i in range(40)]
    _at(time_machine, 0)
    data = _data(kind, pairs, 15)
    swr, _ = _make(kind, data, swr=True, swr_jitter_pct=0.2)
    store = swr._swr
    swr.filter_pairlist(pairs, {})  # cold start: everything computed at the same instant

    ts = START.timestamp()
    dues = sorted(store.due_at(p, ts) for p in pairs)
    assert len(set(dues)) > 30
    assert dues[0] >= ts + PERIOD * 0.8 - 1e-6
    assert dues[-1] <= ts + PERIOD
    assert dues[-1] - dues[0] > PERIOD * 0.1  # spread over a good part of the window

    # Deterministic: a new instance (a restart) draws the same jitter.
    other, _ = _make(kind, data, swr=True, swr_jitter_pct=0.2)
    assert [other._swr.due_at(p, ts) for p in pairs] == [store.due_at(p, ts) for p in pairs]

    # Without jitter they would all expire together.
    flat = StaleVerdictStore(PERIOD, jitter_pct=0)
    assert len({flat.due_at(p, ts) for p in pairs}) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_swr_jitter_desynchronises_over_cycles(kind, time_machine):
    """Stepping through the expiry window, pairs fall due progressively, not all at once."""
    pairs = [f"S{i:02d}/USDC:USDC" for i in range(40)]
    _at(time_machine, 0)
    swr, fake = _make(kind, _data(kind, pairs, 16), swr=True, swr_jitter_pct=0.2)
    swr.filter_pairlist(pairs, {})
    fake.calls.clear()

    sizes = []
    for step in range(0, int(PERIOD * 0.25), 15):  # 15 s cycles, window of the last 25 %
        _at(time_machine, PERIOD * 0.75 + step)
        swr.filter_pairlist(pairs, {})
        sizes.append(len(fake.calls[-1]) if fake.calls and len(fake.calls) > len(sizes) else 0)
    requested = [p for c in fake.calls for p in c]
    assert len(requested) == len(set(requested))  # each pair recomputed once in the window
    assert len(requested) > 30  # most of them, spread over many cycles
    assert max(len(c) for c in fake.calls) <= N
    assert len(fake.calls) > 15


@pytest.mark.parametrize("kind", KINDS)
def test_swr_quiet_window_after_15m_close(kind, time_machine):
    _at(time_machine, 0)
    data = _data(kind, PAIRS, 17)
    swr, fake = _make(kind, data, swr=True, quiet_after_close_s=180, swr_jitter_pct=0)
    start_ts = START.timestamp()
    # START is 7 min after a close; run the cold start there, then go to a quiet moment.
    first = swr.filter_pairlist(PAIRS, {})
    fake.calls.clear()

    quiet_start = start_ts - 7 * 60 + 3600 + 900 * 1  # a 15m close an hour later
    time_machine.move_to(datetime.fromtimestamp(quiet_start + 60, tz=UTC), tick=False)
    assert swr.filter_pairlist(PAIRS, {}) == first
    assert fake.calls == []  # all due, but within the 3 minutes after the close

    time_machine.move_to(datetime.fromtimestamp(quiet_start + 181, tz=UTC), tick=False)
    swr.filter_pairlist(PAIRS, {})
    assert len(fake.calls) == 1 and len(fake.calls[0]) == N


@pytest.mark.parametrize("kind", KINDS)
def test_swr_failed_recompute_keeps_verdict_and_backs_off(kind, time_machine):
    _at(time_machine, 0)
    pairs = PAIRS[:6]
    data = _data(kind, pairs, 18)
    swr, fake = _make(kind, data, swr=True, swr_jitter_pct=0)
    swr.filter_pairlist(pairs, {})
    fake.calls.clear()

    broken = pairs[0]
    fake.data = {**data, broken: None}  # the exchange has no candles for it anymore
    _at(time_machine, PERIOD + 1)
    store = swr._swr
    store.put(broken, False, START.timestamp() - 5)  # make it the oldest of the due pairs
    result = swr.filter_pairlist(pairs, {})
    assert fake.calls[0][0] == broken
    assert broken in result  # old verdict (False = allowed) kept
    assert store.get(broken) is False

    # It is not retried right away, so it cannot starve the other pairs.
    swr.filter_pairlist(pairs, {})
    swr.filter_pairlist(pairs, {})
    assert [c for c in fake.calls[1:] if broken in c] == []
    assert {p for c in fake.calls for p in c} >= set(pairs)

    # ...but it is retried after the back-off.
    fake.calls.clear()
    _at(time_machine, PERIOD + 1 + RETRY_AFTER_FAILURE_S + 1)
    swr.filter_pairlist(pairs, {})
    assert fake.calls and broken in fake.calls[0]


@pytest.mark.parametrize("kind", KINDS)
def test_swr_new_pair_without_candles_stays_removed(kind, time_machine):
    _at(time_machine, 0)
    data = _data(kind, PAIRS[:4], 19)
    swr, fake = _make(kind, data, swr=True)
    swr.filter_pairlist(PAIRS[:4], {})
    ghost = "GHOST/USDC:USDC"
    fake.data = {**data, ghost: None}
    for _ in range(3):
        result = swr.filter_pairlist([*PAIRS[:4], ghost], {})
        assert ghost not in result
    assert sum(ghost in c for c in fake.calls) == 1  # not requested again at every cycle


@pytest.mark.parametrize("kind", KINDS)
def test_swr_shared_cache_roundtrip(kind, time_machine, mocker):
    _at(time_machine, 0)
    client = FakeSharedClient()
    mocker.patch(
        "freqtrade.pairlist_cache.client.PairlistCacheClient.get_or_spawn", return_value=client
    )
    data = _data(kind, PAIRS[:6], 20)
    swr, _ = _make(kind, data, swr=True)
    first = swr.filter_pairlist(PAIRS[:6], {})
    assert client.mput_calls == 1
    assert set(client.store) == set(PAIRS[:6])
    assert all(
        type(v["exclude"]) is bool and v["ts"] == START.timestamp() for v in client.store.values()
    )

    # A second bot (same config) starts: it adopts the shared verdicts, no OHLCV request.
    other, other_fake = _make(kind, data, swr=True)
    assert other.filter_pairlist(PAIRS[:6], {}) == first
    assert other_fake.calls == []

    # Verdicts recomputed elsewhere in the meantime are adopted instead of being recomputed.
    _at(time_machine, PERIOD + 10)
    for p in PAIRS[:6]:
        client.store[p] = {"exclude": True, "ts": START.timestamp() + PERIOD}
    assert other.filter_pairlist(PAIRS[:6], {}) == []
    assert other_fake.calls == []


def test_swr_store_hard_limits():
    store = StaleVerdictStore(PERIOD, max_per_cycle=2, jitter_pct=0)
    store.put("A", True, 0)
    assert store.classify(["A"], PERIOD - 1) == ([], [])
    assert store.classify(["A"], PERIOD) == ([], ["A"])
    assert store.get("A") is True  # served although due
    assert store.classify(["A"], PERIOD * MAX_AGE_FACTOR + 1) == (["A"], [])
    assert store.get("A") is None  # no longer served


def test_swr_store_cold_start_until_all_verdicts():
    clock = {"t": 1_000_000.0}
    store = StaleVerdictStore(PERIOD, max_per_cycle=2, jitter_pct=0, timer=lambda: clock["t"])
    pairs = [f"P{i}/USDC:USDC" for i in range(10)]
    now = store.now()
    # cold call where only 3 pairs got candles: failures are not backed off, still cold
    for p in pairs[:3]:
        store.put(p, False, now)
    for p in pairs[3:]:
        store.fail(p, now)
    store.mark_warm(pairs)
    assert not store.warm
    missing, due = store.classify(pairs, now + 60)
    assert missing == pairs[3:]
    assert store.select(missing, due, now + 60) == pairs[3:]  # unbounded while cold
    for p in pairs[3:]:
        store.put(p, False, now + 60)
    store.mark_warm(pairs)
    assert store.warm


def test_swr_store_cold_start_ends_after_max_calls():
    from freqtrade.plugins.pairlist.swr_verdicts import COLD_MAX_CALLS

    store = StaleVerdictStore(PERIOD, max_per_cycle=2, jitter_pct=0)
    for _ in range(COLD_MAX_CALLS - 1):
        store.mark_warm(["NEVER/USDC:USDC"])
    assert not store.warm
    store.mark_warm(["NEVER/USDC:USDC"])
    assert store.warm


def test_swr_store_urgent_verdicts_raise_budget(caplog):
    from freqtrade.plugins.pairlist.swr_verdicts import URGENT_AGE_FACTOR, URGENT_BUDGET_FACTOR

    store = StaleVerdictStore(PERIOD, max_per_cycle=2, jitter_pct=0)
    store.warm = True
    now = 2_000_000.0
    pairs = [f"P{i}/USDC:USDC" for i in range(20)]
    for p in pairs:
        store.put(p, False, now - (URGENT_AGE_FACTOR * PERIOD + 10))
    missing, due = store.classify(pairs, now)
    picked = store.select(missing, due, now)
    assert len(picked) == URGENT_BUDGET_FACTOR * 2
    assert "falling behind" in caplog.text
    # not urgent: back to the normal bound
    store2 = StaleVerdictStore(PERIOD, max_per_cycle=2, jitter_pct=0)
    store2.warm = True
    for p in pairs:
        store2.put(p, False, now - PERIOD - 10)
    missing, due = store2.classify(pairs, now)
    assert len(store2.select(missing, due, now)) == 2


def test_swr_store_timer_resolved_at_call(mocker):
    store = StaleVerdictStore(PERIOD)
    mocker.patch("freqtrade.plugins.pairlist.swr_verdicts.time.time", return_value=42.0)
    assert store.now() == 42.0
