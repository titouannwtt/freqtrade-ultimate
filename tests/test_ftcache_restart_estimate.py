"""Tests for scripts/ftcache_restart_estimate.py."""

import importlib.util
import sys
import time
from pathlib import Path

import pandas as pd


_SPEC = importlib.util.spec_from_file_location(
    "ftcache_restart_estimate",
    Path(__file__).parent.parent / "scripts" / "ftcache_restart_estimate.py",
)
est = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = est
_SPEC.loader.exec_module(est)


def test_hyperliquid_weight_model():
    assert est.call_weight(5000) == 20 + 84  # 5000 candles ~ 104
    assert est.gap_weight(5000) == 104
    assert est.gap_weight(10000) == 208
    assert est.gap_weight(60) == 21
    assert est.call_weight(500, funding=True) == 45


def test_polluted_series_is_counted_and_costs_a_refetch(tmp_path, capsys):
    d = tmp_path / "futures"
    d.mkdir()
    now = int(time.time() * 1000)
    h = 3_600_000
    last = now // h * h
    ms = [1_700_000 + i * 3_600 for i in range(100)] + [last - 2 * h, last - h, last]
    pd.DataFrame(
        {
            "date": pd.to_datetime(ms, unit="ms", utc=True).as_unit("ms"),
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
        }
    ).to_feather(d / "BTC_USDC_USDC-1h.feather")  # fmt: skip
    (d / "BTC_USDC_USDC-1h.meta.json").write_text(f'{{"last_live_refresh_wall_ms": {now}}}')
    sys.argv = ["x", "--root", str(tmp_path)]
    assert est.main() == 0
    out = capsys.readouterr().out
    assert "polluted (1970 rows): 1" in out
    assert "corrupt rows dropped: 100" in out
    # 3 real rows left of 5000 requested: one full-size call
    assert "TOTAL weight, active series: 104" in out
