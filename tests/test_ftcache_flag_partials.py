"""Tests for scripts/ftcache_flag_partials.py (read-only estimate of frozen persisted candles)."""

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

from freqtrade.ohlcv_cache.persistence import FeatherPersistence
from freqtrade.ohlcv_cache.store import CandleStore


_SPEC = importlib.util.spec_from_file_location(
    "ftcache_flag_partials",
    Path(__file__).parent.parent / "scripts" / "ftcache_flag_partials.py",
)
flag = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = flag
_SPEC.loader.exec_module(flag)

DAY0 = pd.Timestamp("2026-10-01", tz="UTC")
TF_MS = {"5m": 300_000, "1h": 3_600_000}


def _write_series(root: Path, tf: str, volumes: list[float]) -> None:
    store = CandleStore()
    s = store.get_or_create("hyperliquid", "futures", "BTC/USDC:USDC", tf, "futures", TF_MS[tf])
    t0 = int(DAY0.timestamp() * 1000)
    s.merge([[t0 + i * TF_MS[tf], 1, 2, 0.5, 1.5, v] for i, v in enumerate(volumes)])
    FeatherPersistence(root, store).flush_dirty()


@pytest.fixture
def cache_root(tmp_path):
    root = tmp_path / "ftcache"
    _write_series(root, "5m", [2.0] * 288)  # one complete day of 5m
    vols = [24.0] * 24
    vols[7] = 0.5  # hour 7 was cached while forming
    _write_series(root, "1h", vols)
    return root


def _run(root, *extra):
    return flag.main(["--root", str(root), "--since", "2026-10-01", *extra])


def test_report_is_read_only(cache_root, capsys):
    metas = {p: p.read_bytes() for p in cache_root.rglob("*.meta.json")}
    assert _run(cache_root) == 0
    out = capsys.readouterr().out
    assert "total frozen candles: 1" in out
    assert "read-only" in out
    assert {p: p.read_bytes() for p in cache_root.rglob("*.meta.json")} == metas


def test_script_has_no_write_mode():
    """The daemon scans and heals itself; an offline writer would race its relaunch."""
    with pytest.raises(SystemExit):
        flag.main(["--root", "/nonexistent", "--apply"])
