"""
End-to-end determinism regression test for the replay harness.

This is the safety net for the performance work: a replay over fixed synthetic
data must produce **byte-identical** results before and after optimisation.
Running it twice must also yield identical summaries (no hidden randomness).
"""

import math
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from freqtrade.enums import CandleType, RunMode
from freqtrade.replay.runner import run_replay


STRATS_DIR = str(Path(__file__).parent / "strats")
PAIR = "BTC/USDC:USDC"


def _osc_feather(datadir, tf, ctype, start, n, freq):
    base = PAIR.replace("/", "_").replace(":", "_")
    suffix = "" if ctype == CandleType.SPOT else f"-{ctype.value}"
    path = Path(datadir) / f"{base}-{tf}{suffix}.feather"
    dates = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    closes = [100 + 10 * math.sin(i / 4.0) for i in range(n)]
    pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [100.0] * n,
        }
    ).to_feather(path)


def _funding_feather(datadir, start, n):
    base = PAIR.replace("/", "_").replace(":", "_")
    path = Path(datadir) / f"{base}-1h-funding_rate.feather"
    dates = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    pd.DataFrame(
        {
            "date": dates,
            "open": [0.0001] * n,  # funding rate is read from 'open'
            "high": [0.0] * n,
            "low": [0.0] * n,
            "close": [0.0] * n,
            "volume": [0.0] * n,
        }
    ).to_feather(path)


@pytest.fixture
def datadir(tmp_path):
    d = tmp_path / "hyperliquid" / "futures"
    d.mkdir(parents=True)
    # Warmup from 2025-12-31; ~6 days of 15m + 1h mark + 1h funding.
    _osc_feather(d, "15m", CandleType.FUTURES, "2025-12-31", 600, "15min")
    _osc_feather(d, "1h", CandleType.FUTURES, "2025-12-31", 160, "1h")
    _funding_feather(d, "2025-12-31", 160)
    return d


def _config(udir):
    # Base on freqtrade's validated default config, then switch to HL futures.
    from tests.conftest import get_default_conf

    cfg = get_default_conf(udir)
    cfg.update(
        {
            "dry_run": True,
            "runmode": RunMode.DRY_RUN,
            "trading_mode": "futures",
            "margin_mode": "isolated",
            "stake_currency": "USDC",
            "stake_amount": "unlimited",
            "max_open_trades": 3,
            "strategy_path": STRATS_DIR,
        }
    )
    cfg["exchange"]["name"] = "hyperliquid"
    cfg["exchange"]["pair_whitelist"] = [PAIR]
    cfg["exchange"]["pair_blacklist"] = []
    # Let the strategy's own timeframe (15m) win, not the default conf's 5m.
    cfg.pop("timeframe", None)
    return cfg


def _run(datadir, db_name, tmp_path, **kw):
    return run_replay(
        config=deepcopy(_config(tmp_path)),
        pairs=[PAIR],
        start_dt=datetime(2026, 1, 1, tzinfo=UTC),
        end_dt=datetime(2026, 1, 4, tzinfo=UTC),
        strategy="ReplayDetStrategy",
        wallet=1000.0,
        datadir=str(datadir),
        db_url=f"sqlite:///{tmp_path / db_name}",
        sub_step=900,
        **kw,
    )


class TestReplayE2E:
    # Behaviour pinned on the pre-optimisation code. Any optimisation must keep
    # these EXACT results (this is the fidelity lock, not just run-to-run equality).
    EXPECTED = {
        "closed_trades": 11,
        "open_trades": 1,
        "wins": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        # Updated for the intra-candle stoploss enforcement (commit f271ac2f8):
        # at 15m sub-step the SL now fires on the sub-candle low instead of the
        # next process() close, which legitimately changes the coarse result.
        # Re-frozen 2026-07-08 after the tradable_balance_ratio-with-available_capital
        # sizing fix (commit 0e82b4f1c) legitimately changed position sizes.
        # Re-frozen 2026-07-10 after the replay wallet-lock (replay_lock_wallet) stopped
        # paper profits from compounding into stake sizes.
        "total_profit_abs": -390.9842,
        "total_profit_ratio": -1.451276,
    }

    def test_deterministic_and_matches_baseline(self, datadir, tmp_path):
        s1 = _run(datadir, "r1.replay.sqlite", tmp_path)
        s2 = _run(datadir, "r2.replay.sqlite", tmp_path)
        # 1) Run-to-run determinism (no hidden randomness).
        d1 = {k: v for k, v in s1.items() if k != "db_url"}
        d2 = {k: v for k, v in s2.items() if k != "db_url"}
        assert d1 == d2, f"Replay is not deterministic:\n{d1}\n{d2}"
        # 2) Exact match to the frozen baseline (fidelity guard for optimisation).
        for key, expected in self.EXPECTED.items():
            assert s1[key] == pytest.approx(expected), f"{key}: {s1[key]} != {expected}"

    def test_optimisations_preserve_1m_baseline(self, datadir, tmp_path):
        # 1-minute sub-step is where the analyze short-circuit + open-trades cache are
        # most active; pin the exact result so those optimisations stay faithful.
        s = run_replay(
            config=deepcopy(_config(tmp_path)),
            pairs=[PAIR],
            start_dt=datetime(2026, 1, 1, tzinfo=UTC),
            end_dt=datetime(2026, 1, 2, tzinfo=UTC),
            strategy="ReplayDetStrategy",
            wallet=1000.0,
            datadir=str(datadir),
            db_url=f"sqlite:///{tmp_path / 'm1.replay.sqlite'}",
            sub_step=60,
        )
        assert s["closed_trades"] == 30
        assert s["total_profit_abs"] == pytest.approx(239.18)

    def test_seed_marker_written(self, datadir, tmp_path):
        import sqlite3

        db = tmp_path / "seed.sqlite"
        _run(datadir, "seed.sqlite", tmp_path, seed=True)
        cur = sqlite3.connect(db).cursor()
        row = cur.execute(
            "select string_value from KeyValueStore where key='ft_replay_seed'"
        ).fetchone()
        assert row is not None and "timerange" in row[0]

    def test_seed_refuses_live_config(self, datadir, tmp_path):
        cfg = _config(tmp_path)
        cfg["dry_run"] = False
        with pytest.raises(RuntimeError, match="not in dry-run"):
            run_replay(
                config=cfg,
                pairs=[PAIR],
                start_dt=datetime(2026, 1, 1, tzinfo=UTC),
                end_dt=datetime(2026, 1, 2, tzinfo=UTC),
                strategy="ReplayDetStrategy",
                datadir=str(datadir),
                db_url=f"sqlite:///{tmp_path / 'live.sqlite'}",
                seed=True,
            )

    def test_skip_missing_pairs_in_seed(self, datadir, tmp_path):
        # An extra pair with no data must be skipped (seed mode), not abort the run.
        s = run_replay(
            config=deepcopy(_config(tmp_path)),
            pairs=[PAIR, "GHOST/USDC:USDC"],
            start_dt=datetime(2026, 1, 1, tzinfo=UTC),
            end_dt=datetime(2026, 1, 3, tzinfo=UTC),
            strategy="ReplayDetStrategy",
            datadir=str(datadir),
            db_url=f"sqlite:///{tmp_path / 'skip.sqlite'}",
            seed=True,
            sub_step=900,
        )
        assert isinstance(s, dict)  # ran to completion despite the missing pair
