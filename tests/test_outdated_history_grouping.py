"""One grouped 'Outdated history' line, not one per pair.

Upstream warns per pair, throttled per pair. With a large whitelist that is still
hundreds of near-identical lines an hour, per bot: measured on a live fleet,
"Outdated history" accounted for ~7400 of the warnings in a single log sample and
buried every other signal. A stale feed is a fleet-level condition, so the useful
unit is the SET of pairs plus the worst age.
"""

import logging
from types import SimpleNamespace

from freqtrade.strategy.interface import IStrategy


class _Concrete(IStrategy):
    """IStrategy is abstract; only the reporting state is exercised here."""

    def populate_indicators(self, dataframe, metadata):  # pragma: no cover - unused
        return dataframe


def _strat(monkeypatch, clock):
    """A bare strategy carrying only the outdated-report state."""
    s = _Concrete.__new__(_Concrete)
    s._outdated_pending = {}
    s._outdated_pending_since = 0.0
    s._outdated_last_flush = 0.0
    monkeypatch.setattr("time.monotonic", lambda: clock.t)
    return s


def _lines(caplog):
    return [r.message for r in caplog.records if "Outdated history" in r.message]


def test_nothing_is_logged_before_the_batch_window(monkeypatch, caplog):
    """The first stale pair must not report alone: the cycle has not finished."""
    clock = SimpleNamespace(t=1000.0)
    s = _strat(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        s._note_outdated_pair("BTC/USDC:USDC", 30)
    assert _lines(caplog) == []
    assert "BTC/USDC:USDC" in s._outdated_pending


def test_one_line_carries_every_pair(monkeypatch, caplog):
    clock = SimpleNamespace(t=1000.0)
    s = _strat(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        for i, p in enumerate(["AAA/USDC:USDC", "BBB/USDC:USDC", "CCC/USDC:USDC"]):
            s._note_outdated_pair(p, 10 + i)
        clock.t += s.OUTDATED_BATCH_WINDOW_S + 1
        s._note_outdated_pair("DDD/USDC:USDC", 99)

    lines = _lines(caplog)
    assert len(lines) == 1, "one grouped line, never one per pair"
    assert "4 pair(s)" in lines[0]
    for p in ("AAA", "BBB", "CCC", "DDD"):
        assert p in lines[0]


def test_the_worst_offender_leads(monkeypatch, caplog):
    """A truncated list must keep the pairs that matter."""
    clock = SimpleNamespace(t=1000.0)
    s = _strat(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        for i in range(s.OUTDATED_MAX_NAMES + 5):
            s._note_outdated_pair(f"P{i:02d}/USDC:USDC", i)
        clock.t += s.OUTDATED_BATCH_WINDOW_S + 1
        s._note_outdated_pair("WORST/USDC:USDC", 9999)

    line = _lines(caplog)[0]
    assert "worst 9999m behind" in line
    assert "WORST/USDC:USDC (9999m)" in line
    # 17 pairs + the worst = 18 recorded, 12 shown, so 6 are folded away.
    assert "and 6 more" in line, "truncation must be declared, never silent"
    assert "P00/USDC:USDC" not in line, "the least stale pairs are the ones dropped"


def test_a_pair_keeps_its_worst_age_within_a_batch(monkeypatch, caplog):
    """The peak is the fact; a later, smaller reading must not erase it."""
    clock = SimpleNamespace(t=1000.0)
    s = _strat(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        s._note_outdated_pair("BTC/USDC:USDC", 120)
        s._note_outdated_pair("BTC/USDC:USDC", 3)
        clock.t += s.OUTDATED_BATCH_WINDOW_S + 1
        s._note_outdated_pair("BTC/USDC:USDC", 5)
    assert "(120m)" in _lines(caplog)[0]


def test_reporting_is_throttled_but_never_silenced(monkeypatch, caplog):
    """A feed that stays behind must keep saying so: silence would read as recovery."""
    clock = SimpleNamespace(t=1000.0)
    s = _strat(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        s._note_outdated_pair("BTC/USDC:USDC", 30)
        clock.t += s.OUTDATED_BATCH_WINDOW_S + 1
        s._note_outdated_pair("BTC/USDC:USDC", 30)          # 1re parution
        clock.t += 60
        s._note_outdated_pair("BTC/USDC:USDC", 30)          # étranglée
        assert len(_lines(caplog)) == 1

        clock.t += s.OUTDATED_REPORT_INTERVAL_S
        s._note_outdated_pair("BTC/USDC:USDC", 30)
        clock.t += s.OUTDATED_BATCH_WINDOW_S + 1
        s._note_outdated_pair("BTC/USDC:USDC", 30)          # 2e parution
    assert len(_lines(caplog)) == 2


def test_the_batch_empties_after_a_report(monkeypatch, caplog):
    """Otherwise a recovered pair would be named forever."""
    clock = SimpleNamespace(t=1000.0)
    s = _strat(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        s._note_outdated_pair("OLD/USDC:USDC", 30)
        clock.t += s.OUTDATED_BATCH_WINDOW_S + 1
        s._note_outdated_pair("OLD/USDC:USDC", 30)
    assert s._outdated_pending == {}
