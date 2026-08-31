"""One grouped 'No data found' line per feed, not one per pair.

Upstream warns once per (pair, timeframe, candle_type). On a futures fleet whose
venue publishes no funding rate for a whole family of instruments, that is one
line per pair per cycle, forever: ~2400 of the warnings in a single live log
sample came from this one call site.

Grouping is keyed by FEED rather than flattened, because the two symptoms are not
the same fact. "No funding_rate for 38 pairs" is a venue characteristic; a missing
OHLCV feed is a data problem. One sentence covering both would hide both.
"""

import logging
from types import SimpleNamespace

from freqtrade.data.dataprovider import DataProvider


def _dp(monkeypatch, clock):
    dp = DataProvider.__new__(DataProvider)
    # Name-mangled attributes: the class body writes them as __nodata_*.
    setattr(dp, "_DataProvider__nodata_pending", {})
    setattr(dp, "_DataProvider__nodata_pending_since", 0.0)
    setattr(dp, "_DataProvider__nodata_last_flush", 0.0)
    monkeypatch.setattr("time.monotonic", lambda: clock.t)
    return dp


def _lines(caplog):
    return [r.message for r in caplog.records if "No data found" in r.message]


def test_nothing_is_logged_before_the_batch_window(monkeypatch, caplog):
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        dp._note_missing_data("XYZ-AAA/USDC:USDC", "1h", "funding_rate")
    assert _lines(caplog) == []


def test_one_line_carries_every_pair_of_a_feed(monkeypatch, caplog):
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        for i in range(4):
            dp._note_missing_data(f"XYZ-P{i}/USDC:USDC", "1h", "funding_rate")
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_missing_data("XYZ-LAST/USDC:USDC", "1h", "funding_rate")

    lines = _lines(caplog)
    assert len(lines) == 1
    assert "5 pair(s) on (1h, funding_rate)" in lines[0]
    assert "XYZ-LAST/USDC:USDC" in lines[0]


def test_distinct_feeds_are_never_merged(monkeypatch, caplog):
    """A missing funding rate and a missing candle feed are different facts."""
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        dp._note_missing_data("AAA/USDC:USDC", "1h", "funding_rate")
        dp._note_missing_data("BBB/USDC:USDC", "5m", "spot")
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_missing_data("CCC/USDC:USDC", "1h", "funding_rate")

    lines = _lines(caplog)
    assert len(lines) == 2, "one line per feed, never one merged sentence"
    assert any("(1h, funding_rate)" in ln and "2 pair(s)" in ln for ln in lines)
    assert any("(5m, spot)" in ln and "1 pair(s)" in ln for ln in lines)


def test_truncation_is_declared(monkeypatch, caplog):
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        for i in range(dp.NODATA_MAX_NAMES + 7):
            dp._note_missing_data(f"P{i:02d}/USDC:USDC", "1h", "funding_rate")
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_missing_data("P99/USDC:USDC", "1h", "funding_rate")

    line = _lines(caplog)[0]
    assert "and 8 more" in line, "a silent cut would misreport the scope"


def test_reporting_is_throttled_but_never_silenced(monkeypatch, caplog):
    """A venue that never publishes funding must keep saying so."""
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        dp._note_missing_data("AAA/USDC:USDC", "1h", "funding_rate")
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_missing_data("AAA/USDC:USDC", "1h", "funding_rate")   # 1re parution
        clock.t += 120
        dp._note_missing_data("AAA/USDC:USDC", "1h", "funding_rate")   # étranglée
        assert len(_lines(caplog)) == 1

        clock.t += dp.NODATA_REPORT_INTERVAL_S
        dp._note_missing_data("AAA/USDC:USDC", "1h", "funding_rate")
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_missing_data("AAA/USDC:USDC", "1h", "funding_rate")   # 2e parution
    assert len(_lines(caplog)) == 2


def test_the_batch_empties_after_a_report(monkeypatch, caplog):
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        dp._note_missing_data("AAA/USDC:USDC", "1h", "funding_rate")
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_missing_data("AAA/USDC:USDC", "1h", "funding_rate")
    assert getattr(dp, "_DataProvider__nodata_pending") == {}
