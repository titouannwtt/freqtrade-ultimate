"""Tests for scripts/repair_partial_candles.py (rebuild of candles stored while forming)."""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


_SPEC = importlib.util.spec_from_file_location(
    "repair_partial_candles",
    Path(__file__).parent.parent / "scripts" / "repair_partial_candles.py",
)
repair = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = repair
_SPEC.loader.exec_module(repair)

DAY0 = pd.Timestamp("2026-10-01", tz="UTC")


def _ohlcv(tf: str, start: pd.Timestamp, n: int, vol: float = 1.0) -> pd.DataFrame:
    dates = pd.date_range(start, periods=n, freq=tf, tz="UTC").as_unit("ms")
    base = 100 + np.arange(n, dtype=float)
    return pd.DataFrame(
        {
            "date": dates,
            "open": base,
            "high": base + 2,
            "low": base - 2,
            "close": base + 1,
            "volume": np.full(n, vol),
        }
    )


def _aggregate(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    g = df.set_index("date").resample(rule)
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
        }
    ).reset_index()
    out["date"] = out["date"].dt.as_unit("ms")
    return out


@pytest.fixture
def datadir(tmp_path):
    fdir = tmp_path / "futures"
    fdir.mkdir()
    m5 = _ohlcv("5min", DAY0, 288 * 3, vol=2.0)  # 3 complete days
    h1 = _aggregate(m5, "1h")
    d1 = _aggregate(m5, "1D")
    # Day 2 / hour 5 of day 1 were stored while forming: tiny volume, truncated range.
    h1.loc[29, ["high", "close", "volume"]] = [h1.loc[29, "open"] + 0.1, h1.loc[29, "open"], 0.5]
    d1.loc[1, ["close", "volume"]] = [d1.loc[1, "open"], 1.5]
    for tf, df in (("5m", m5), ("1h", h1), ("1d", d1)):
        df.to_feather(fdir / f"BTC_USDC_USDC-{tf}-futures.feather")
    return tmp_path, h1, d1


def _run(datadir_path, *extra):
    return repair.main(["-d", str(datadir_path), "--since", "2026-10-01", *extra])


def test_dry_run_reports_and_writes_nothing(datadir, capsys):
    path, *_ = datadir
    before = {f: f.read_bytes() for f in (path / "futures").iterdir()}
    assert _run(path) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "nothing written" in out
    # 1 bad hour, 1 bad day (+ the week-less data: no 1w file)
    lines = {ln.split()[0]: ln.split() for ln in out.splitlines() if ln[:2] in ("1h", "1d", "5m")}
    assert lines["1h"][3] == "1"
    assert lines["1d"][3] == "1"
    assert {f: f.read_bytes() for f in (path / "futures").iterdir()} == before


def test_apply_rebuilds_from_finer_and_is_atomic(datadir):
    path, *_ = datadir
    assert _run(path, "--apply", "--force") == 0
    fdir = path / "futures"
    fixed_h1 = pd.read_feather(fdir / "BTC_USDC_USDC-1h-futures.feather")
    fixed_d1 = pd.read_feather(fdir / "BTC_USDC_USDC-1d-futures.feather")
    m5 = pd.read_feather(fdir / "BTC_USDC_USDC-5m-futures.feather")
    pd.testing.assert_frame_equal(fixed_h1, _aggregate(m5, "1h"), check_dtype=False)
    pd.testing.assert_frame_equal(fixed_d1, _aggregate(m5, "1D"), check_dtype=False)
    assert fixed_h1.loc[29, "volume"] == 24.0
    assert fixed_d1.loc[1, "volume"] == 576.0
    assert not [p for p in fdir.iterdir() if p.suffix == ".tmp"]
    # Second pass finds nothing left to fix.
    stats = {tf: repair.TfStats() for tf in repair.ORDER}
    frames = {
        tf: pd.read_feather(fdir / f"BTC_USDC_USDC-{tf}-futures.feather")
        for tf in ("5m", "1h", "1d")
    }
    _, fixes = repair.analyse_pair(frames, 0, 10**14, 0.01, stats, "BTC_USDC_USDC")
    assert fixes == []


def test_stored_above_aggregate_is_flagged_not_fixed(datadir):
    path, h1, _ = datadir
    fdir = path / "futures"
    m5 = pd.read_feather(fdir / "BTC_USDC_USDC-5m-futures.feather")
    m5.loc[3, "volume"] = 0.1  # one partial 5m candle: the stored 1h is the complete side
    stats = {tf: repair.TfStats() for tf in repair.ORDER}
    frames = {"5m": m5, "1h": h1.copy()}
    frames["1h"].loc[29, "volume"] = 24.0  # undo the damage for hour 29
    _, fixes = repair.analyse_pair(frames, 0, 10**14, 0.01, stats, "BTC_USDC_USDC")
    assert fixes == []
    assert stats["1h"].finer_suspect == 1


def test_incomplete_source_coverage_is_not_used(datadir):
    path, _, d1 = datadir
    m5 = pd.read_feather(path / "futures" / "BTC_USDC_USDC-5m-futures.feather")
    m5 = m5.drop(index=range(24 * 12 + 5, 24 * 12 + 9)).reset_index(drop=True)  # hole in day 2
    stats = {tf: repair.TfStats() for tf in repair.ORDER}
    _, fixes = repair.analyse_pair({"5m": m5, "1d": d1}, 0, 10**14, 0.01, stats, "BTC_USDC_USDC")
    # day 1 (index 1) is bad but its 5m coverage is incomplete -> no proposal for it.
    assert [f["date"][:10] for f in fixes] == []
    assert stats["1d"].no_source == 1


def test_best_source_wins_and_cascades():
    """A partial 5m candle is rebuilt from 1m, and the 1h built on it follows."""
    m1 = _ohlcv("1min", DAY0, 288 * 5 * 3, vol=0.4)
    m5 = _aggregate(m1, "5min")
    m5.loc[10, "volume"] = 0.3  # partial 5m (true: 2.0)
    h1 = _aggregate(_aggregate(m1, "5min"), "1h")
    h1.loc[0, "volume"] = 0.1
    stats = {tf: repair.TfStats() for tf in repair.ORDER}
    repaired, fixes = repair.analyse_pair(
        {"1m": m1, "5m": m5, "1h": h1}, 0, 10**14, 0.01, stats, "X_USDC_USDC"
    )
    assert repaired["5m"].loc[10, "volume"] == pytest.approx(2.0)
    assert repaired["1h"].loc[0, "volume"] == pytest.approx(24.0)
    assert {f["timeframe"] for f in fixes} == {"5m", "1h"}
