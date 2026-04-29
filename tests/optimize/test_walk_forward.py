# pragma pylint: disable=missing-docstring,W0212,C0103
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import rapidjson
from filelock import Timeout

from freqtrade.commands.walk_forward_commands import start_walk_forward
from freqtrade.enums import RunMode
from freqtrade.exceptions import OperationalException
from freqtrade.optimize.walk_forward import (
    CPCVResult,
    MCResult,
    MultiSeedResult,
    OOSEquityCurve,
    PerturbResult,
    RegimeAnalysis,
    WalkForward,
    WalkForwardWindow,
    WindowResult,
)
from tests.conftest import (
    get_args,
    log_has,
    log_has_re,
    patch_exchange,
    patched_configuration_load_config_file,
)


@pytest.fixture(scope="function")
def walkforward_conf(default_conf, tmp_path):
    wf_conf = deepcopy(default_conf)
    wf_conf.update(
        {
            "runmode": RunMode.WALKFORWARD,
            "strategy": "HyperoptableStrategy",
            "hyperopt_loss": "ShortTradeDurHyperOptLoss",
            "hyperopt_path": str(Path(__file__).parent / "hyperopts"),
            "epochs": 1,
            "timerange": "20180101-20200101",
            "spaces": ["default"],
            "hyperopt_jobs": 1,
            "hyperopt_min_trades": 1,
            "wf_windows": 3,
            "wf_train_ratio": 0.75,
            "wf_embargo_days": 7,
            "wf_holdout_months": 0,
            "wf_min_test_trades": 30,
            "user_data_dir": tmp_path,
        }
    )
    return wf_conf


# ------------------------------------------------------------------
# Window computation
# ------------------------------------------------------------------


class TestComputeWindows:
    def test_basic_window_computation(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()

        assert len(windows) == 3
        for i, w in enumerate(windows):
            assert w.index == i
            assert w.train_start < w.train_end
            assert w.test_start < w.test_end
            assert w.train_end <= w.test_start

    def test_windows_disjoint_test_periods(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()

        for i in range(len(windows) - 1):
            assert windows[i].test_end <= windows[i + 1].test_start

    def test_embargo_gap(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()

        for w in windows:
            gap = (w.test_start - w.train_end).days
            assert gap == walkforward_conf["wf_embargo_days"]

    def test_holdout_reserve(self, walkforward_conf):
        walkforward_conf["wf_holdout_months"] = 3
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()
        full_end = datetime.strptime("20200101", "%Y%m%d").replace(tzinfo=UTC)
        holdout_start = full_end - timedelta(days=3 * 30)

        for w in windows:
            assert w.test_end <= holdout_start + timedelta(days=1)

    def test_holdout_window(self, walkforward_conf):
        walkforward_conf["wf_holdout_months"] = 2
        wf = WalkForward(walkforward_conf)
        full_end = datetime.strptime("20200101", "%Y%m%d").replace(tzinfo=UTC)
        ho = wf._compute_holdout_window(full_end)

        assert ho is not None
        assert ho.test_end == full_end
        assert ho.index == -1

    def test_no_holdout_when_zero(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        ho = wf._compute_holdout_window(datetime.strptime("20200101", "%Y%m%d").replace(tzinfo=UTC))
        assert ho is None

    def test_too_many_windows_raises(self, walkforward_conf):
        walkforward_conf["wf_windows"] = 50
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match=r"window too short"):
            wf._compute_windows()

    def test_short_timerange_raises(self, walkforward_conf):
        walkforward_conf["timerange"] = "20180101-20180301"
        walkforward_conf["wf_windows"] = 5
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match=r"(Train|Test) window too short"):
            wf._compute_windows()

    def test_holdout_eats_all_data(self, walkforward_conf):
        walkforward_conf["wf_holdout_months"] = 30
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match=r"Not enough data"):
            wf._compute_windows()

    def test_large_embargo_raises(self, walkforward_conf):
        walkforward_conf["wf_embargo_days"] = 300
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match=r"Not enough data"):
            wf._compute_windows()


# ------------------------------------------------------------------
# Timerange parsing
# ------------------------------------------------------------------


class TestParseTimerange:
    def test_valid_timerange(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        start, end = wf._parse_timerange()
        assert start == datetime(2018, 1, 1, tzinfo=UTC)
        assert end == datetime(2020, 1, 1, tzinfo=UTC)

    def test_missing_timerange(self, walkforward_conf):
        walkforward_conf["timerange"] = ""
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match=r"requires --timerange"):
            wf._parse_timerange()

    def test_open_ended_timerange(self, walkforward_conf):
        walkforward_conf["timerange"] = "20180101-"
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match=r"both start and end"):
            wf._parse_timerange()

    def test_no_dash_timerange(self, walkforward_conf):
        walkforward_conf["timerange"] = "20180101"
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match=r"requires --timerange"):
            wf._parse_timerange()


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------


class TestValidation:
    def test_invalid_train_ratio_low(self, walkforward_conf):
        walkforward_conf["wf_train_ratio"] = 0.3
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()
        with pytest.raises(OperationalException, match=r"must be between 0.5 and 0.9"):
            wf._validate(windows)

    def test_invalid_train_ratio_high(self, walkforward_conf):
        walkforward_conf["wf_train_ratio"] = 0.95
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()
        with pytest.raises(OperationalException, match=r"must be between 0.5 and 0.9"):
            wf._validate(windows)

    def test_long_train_period_warning(self, walkforward_conf, caplog):
        walkforward_conf["wf_windows"] = 1
        walkforward_conf["timerange"] = "20150101-20200101"
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()
        wf._validate(windows)
        assert log_has_re(r".*train period is .* days.*outdated regimes.*", caplog)

    def test_no_timeframe_detail_warning(self, walkforward_conf, caplog):
        walkforward_conf.pop("timeframe_detail", None)
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()
        wf._validate(windows)
        assert log_has_re(r".*No --timeframe-detail.*", caplog)

    def test_high_epochs_warning(self, walkforward_conf, caplog):
        walkforward_conf["epochs"] = 500
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()
        wf._validate(windows)
        assert log_has_re(r".*Epochs per window.*300.*per-window overfitting.*", caplog)


# ------------------------------------------------------------------
# Parameter stability analysis
# ------------------------------------------------------------------


class TestParamStability:
    def test_stable_params(self):
        all_params = [
            {"buy": {"rsi": 30, "adx": 25}},
            {"buy": {"rsi": 31, "adx": 26}},
            {"buy": {"rsi": 29, "adx": 24}},
        ]
        ranges = {"rsi": (10.0, 90.0), "adx": (10.0, 50.0)}
        result = WalkForward._analyze_param_stability(all_params, ranges)

        assert "rsi" in result
        assert "adx" in result
        assert result["rsi"]["stable"] is True
        assert result["rsi"]["unstable"] is False

    def test_unstable_params(self):
        all_params = [
            {"buy": {"rsi": 10}},
            {"buy": {"rsi": 50}},
            {"buy": {"rsi": 90}},
        ]
        ranges = {"rsi": (10.0, 90.0)}
        result = WalkForward._analyze_param_stability(all_params, ranges)

        assert result["rsi"]["unstable"] is True
        assert result["rsi"]["stable"] is False

    def test_missing_range_fallback(self):
        all_params = [
            {"buy": {"unknown_param": 100}},
            {"buy": {"unknown_param": 102}},
        ]
        result = WalkForward._analyze_param_stability(all_params, {})
        assert "unknown_param" in result
        assert result["unknown_param"]["std_over_range"] >= 0

    def test_single_value_skipped(self):
        all_params = [
            {"buy": {"rsi": 30}},
        ]
        ranges = {"rsi": (10.0, 90.0)}
        result = WalkForward._analyze_param_stability(all_params, ranges)
        assert "rsi" not in result

    def test_non_numeric_skipped(self):
        all_params = [
            {"buy": {"method": "sma"}},
            {"buy": {"method": "ema"}},
        ]
        ranges = {}
        result = WalkForward._analyze_param_stability(all_params, ranges)
        assert "method" not in result


# ------------------------------------------------------------------
# Consensus params
# ------------------------------------------------------------------


class TestConsensusParams:
    def test_median_numeric(self):
        all_params = [
            {"buy": {"rsi": 30}},
            {"buy": {"rsi": 40}},
            {"buy": {"rsi": 50}},
        ]
        result = WalkForward._compute_consensus_params(all_params)
        assert result["buy"]["rsi"] == 40

    def test_median_int_preserved(self):
        all_params = [
            {"buy": {"rsi": 30}},
            {"buy": {"rsi": 40}},
            {"buy": {"rsi": 50}},
        ]
        result = WalkForward._compute_consensus_params(all_params)
        assert isinstance(result["buy"]["rsi"], int)

    def test_median_float_preserved(self):
        all_params = [
            {"buy": {"threshold": 0.1}},
            {"buy": {"threshold": 0.2}},
            {"buy": {"threshold": 0.3}},
        ]
        result = WalkForward._compute_consensus_params(all_params)
        assert isinstance(result["buy"]["threshold"], float)
        assert abs(result["buy"]["threshold"] - 0.2) < 1e-6

    def test_categorical_mode(self):
        all_params = [
            {"buy": {"method": "sma"}},
            {"buy": {"method": "sma"}},
            {"buy": {"method": "ema"}},
        ]
        result = WalkForward._compute_consensus_params(all_params)
        assert result["buy"]["method"] == "sma"

    def test_multiple_spaces(self):
        all_params = [
            {"buy": {"rsi": 30}, "sell": {"profit": 0.01}},
            {"buy": {"rsi": 40}, "sell": {"profit": 0.02}},
            {"buy": {"rsi": 50}, "sell": {"profit": 0.03}},
        ]
        result = WalkForward._compute_consensus_params(all_params)
        assert "buy" in result
        assert "sell" in result
        assert result["buy"]["rsi"] == 40
        assert abs(result["sell"]["profit"] - 0.02) < 1e-6

    def test_even_number_of_values(self):
        all_params = [
            {"buy": {"rsi": 30}},
            {"buy": {"rsi": 40}},
            {"buy": {"rsi": 50}},
            {"buy": {"rsi": 60}},
        ]
        result = WalkForward._compute_consensus_params(all_params)
        assert result["buy"]["rsi"] == 45


# ------------------------------------------------------------------
# Degradation
# ------------------------------------------------------------------


class TestDegradation:
    def test_normal_degradation(self):
        train = {"profit_pct": 10.0, "calmar": 2.0, "sharpe": 1.5, "profit_factor": 2.0}
        test = {"profit_pct": 5.0, "calmar": 1.0, "sharpe": 0.75, "profit_factor": 1.5}
        result = WalkForward._compute_degradation(train, test)

        assert result["profit_pct"] == pytest.approx(-0.5, abs=0.01)
        assert result["calmar"] == pytest.approx(-0.5, abs=0.01)

    def test_zero_train_value(self):
        train = {"profit_pct": 0.0, "calmar": 0.0, "sharpe": 0.0, "profit_factor": 0.0}
        test = {"profit_pct": 5.0, "calmar": 1.0, "sharpe": 0.5, "profit_factor": 1.0}
        result = WalkForward._compute_degradation(train, test)

        for v in result.values():
            assert v == 0.0

    def test_improvement(self):
        train = {"profit_pct": 5.0, "calmar": 1.0, "sharpe": 0.5, "profit_factor": 1.0}
        test = {"profit_pct": 10.0, "calmar": 2.0, "sharpe": 1.0, "profit_factor": 2.0}
        result = WalkForward._compute_degradation(train, test)

        assert result["profit_pct"] == pytest.approx(1.0, abs=0.01)


# ------------------------------------------------------------------
# Warning flags
# ------------------------------------------------------------------


class TestWarningFlags:
    def _make_wf(self, conf):
        return WalkForward(conf)

    def test_low_trade_count_warning(self, walkforward_conf):
        wf = self._make_wf(walkforward_conf)
        window = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=window,
                test_metrics={"trades": 5},
                test_trade_count=5,
            )
        ]
        warnings = wf._generate_warning_flags(results, {}, 1)
        assert any("test trades" in w for w in warnings)

    def test_unstable_param_warning(self, walkforward_conf):
        wf = self._make_wf(walkforward_conf)
        stability = {
            "rsi": {"unstable": True, "stable": False},
        }
        warnings = wf._generate_warning_flags([], stability, 1)
        assert any("Unstable" in w for w in warnings)

    def test_no_timeframe_detail_warning(self, walkforward_conf):
        walkforward_conf.pop("timeframe_detail", None)
        wf = self._make_wf(walkforward_conf)
        warnings = wf._generate_warning_flags([], {}, 1)
        assert any("timeframe-detail" in w for w in warnings)

    def test_meta_overfitting_warning(self, walkforward_conf):
        wf = self._make_wf(walkforward_conf)
        warnings = wf._generate_warning_flags([], {}, 3)
        assert any("run #3" in w for w in warnings)

    def test_no_warnings_clean(self, walkforward_conf):
        walkforward_conf["timeframe_detail"] = "1m"
        wf = self._make_wf(walkforward_conf)
        window = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=window,
                train_metrics={"trades": 200, "max_dd_pct": 5},
                test_metrics={
                    "trades": 200,
                    "profit_pct": 5,
                    "profit_factor": 1.5,
                    "max_dd_pct": 4,
                },
                test_trade_count=200,
            )
        ]
        warnings = wf._generate_warning_flags(results, {}, 1)
        assert len(warnings) == 0


# ------------------------------------------------------------------
# Window dataclass helpers
# ------------------------------------------------------------------


class TestWalkForwardWindow:
    def test_train_timerange_str(self):
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        assert w.train_timerange_str() == "20180101-20180601"

    def test_test_timerange_str(self):
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        assert w.test_timerange_str() == "20180608-20180901"


# ------------------------------------------------------------------
# Extract metrics
# ------------------------------------------------------------------


class TestExtractMetrics:
    def test_extract_full_metrics(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        strat_data = {
            "profit_total": 0.15,
            "profit_total_abs": 150.0,
            "total_trades": 42,
            "calmar": 2.1,
            "sharpe": 1.5,
            "sortino": 2.0,
            "max_drawdown_account": 0.08,
            "profit_factor": 1.8,
            "winrate": 0.65,
            "holding_avg": "1:30:00",
        }
        result = wf._extract_metrics(strat_data)

        assert result["profit_pct"] == pytest.approx(15.0)
        assert result["trades"] == 42
        assert result["calmar"] == 2.1
        assert result["max_dd_pct"] == pytest.approx(8.0)

    def test_extract_missing_keys(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        result = wf._extract_metrics({})

        assert result["profit_pct"] == 0
        assert result["trades"] == 0
        assert result["calmar"] == 0


# ------------------------------------------------------------------
# Export / persistence
# ------------------------------------------------------------------


class TestExport:
    def test_export_consensus_json(self, walkforward_conf, tmp_path):
        wf = WalkForward(walkforward_conf)
        consensus = {
            "buy": {"rsi": 35, "adx": 25},
            "sell": {"profit": 0.02},
        }
        path = wf._export_consensus_json(consensus)
        assert path.exists()

        with path.open("r") as f:
            data = rapidjson.load(f)
        assert data["strategy_name"] == "HyperoptableStrategy"
        assert data["params"]["buy"]["rsi"] == 35

    def test_export_consensus_does_not_write_strategy_json(self, walkforward_conf, tmp_path):
        strategy_json = tmp_path / "HyperoptableStrategy.json"
        wf = WalkForward(walkforward_conf)
        consensus = {"buy": {"rsi": 35}}
        wf._export_consensus_json(consensus)

        assert not strategy_json.exists()

    def test_export_results_json(self, walkforward_conf, tmp_path):
        wf = WalkForward(walkforward_conf)
        window = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=window,
                train_metrics={"profit_pct": 10},
                test_metrics={"profit_pct": 5},
            )
        ]
        data = wf._build_results_data(results, {}, {"buy": {"rsi": 30}})
        path = wf._export_results_json(data)
        assert path.exists()

        with path.open("r") as f:
            loaded = rapidjson.load(f)
        assert loaded["strategy"] == "HyperoptableStrategy"
        assert len(loaded["windows"]) == 1
        assert loaded["consensus_params"]["buy"]["rsi"] == 30

    def test_log_wfa_run_appends(self, walkforward_conf, tmp_path):
        wf = WalkForward(walkforward_conf)
        window = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=window,
                test_metrics={"profit_pct": 5},
                test_trade_count=10,
            )
        ]
        stability = {"rsi": {"stable": True}}

        count1 = wf._log_wfa_run(results, stability)
        assert count1 == 1

        count2 = wf._log_wfa_run(results, stability)
        assert count2 == 2

        log_file = tmp_path / "walk_forward" / "wfa_log.jsonl"
        assert log_file.exists()
        lines = [line for line in log_file.read_text().splitlines() if line.strip()]
        assert len(lines) == 2


# ------------------------------------------------------------------
# Strategy JSON helpers
# ------------------------------------------------------------------


class TestStrategyJsonHelpers:
    def test_delete_strategy_json(self, walkforward_conf, tmp_path, caplog):
        json_path = tmp_path / "test.json"
        json_path.write_text("{}")
        wf = WalkForward(walkforward_conf)
        wf._delete_strategy_json(json_path)
        assert not json_path.exists()
        assert log_has(f"Deleted co-located JSON: {json_path}", caplog)

    def test_delete_nonexistent_json(self, walkforward_conf, tmp_path):
        json_path = tmp_path / "nonexistent.json"
        wf = WalkForward(walkforward_conf)
        wf._delete_strategy_json(json_path)

    def test_delete_none_json(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        wf._delete_strategy_json(None)

    def test_save_window_params(self, walkforward_conf, tmp_path):
        json_path = tmp_path / "strategy.json"
        params_data = {
            "strategy_name": "test",
            "params": {"buy": {"rsi": 30}},
            "ft_stratparam_v": 1,
        }
        with json_path.open("w") as f:
            rapidjson.dump(params_data, f)

        wf = WalkForward(walkforward_conf)
        result = wf._save_window_params(0, json_path)

        assert result == {"buy": {"rsi": 30}}
        saved = tmp_path / "walk_forward" / "window_0_params.json"
        assert saved.exists()

    def test_restore_params(self, walkforward_conf, tmp_path):
        json_path = tmp_path / "strategy.json"
        wf = WalkForward(walkforward_conf)
        wf._restore_params({"buy": {"rsi": 30}}, json_path)

        assert json_path.exists()
        with json_path.open("r") as f:
            data = rapidjson.load(f)
        assert data["params"]["buy"]["rsi"] == 30
        assert data["strategy_name"] == "HyperoptableStrategy"

    def test_restore_original_json_restores_content(self, tmp_path):
        json_path = tmp_path / "strategy.json"
        original = b'{"original": true}'
        json_path.write_text("overwritten")
        WalkForward._restore_original_json(json_path, original)
        assert json_path.read_bytes() == original

    def test_restore_original_json_removes_if_none_existed(self, tmp_path):
        json_path = tmp_path / "strategy.json"
        json_path.write_text("should be removed")
        WalkForward._restore_original_json(json_path, None)
        assert not json_path.exists()

    def test_restore_original_json_noop_if_no_path(self):
        WalkForward._restore_original_json(None, None)


# ------------------------------------------------------------------
# Lock file
# ------------------------------------------------------------------


class TestLockFile:
    def test_get_lock_filename(self, walkforward_conf, tmp_path):
        path = WalkForward.get_lock_filename(walkforward_conf)
        assert "walk_forward.lock" in path
        assert str(tmp_path) in path


# ------------------------------------------------------------------
# Report formatting (smoke test)
# ------------------------------------------------------------------


class TestFormatReport:
    def test_format_report_logs(self, walkforward_conf, caplog):
        wf = WalkForward(walkforward_conf)
        window = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=window,
                train_metrics={"profit_pct": 10, "calmar": 2, "max_dd_pct": 5, "trades": 50},
                test_metrics={"profit_pct": 5, "calmar": 1, "max_dd_pct": 8, "trades": 30},
                baseline_metrics={"profit_pct": 3, "calmar": 0.5, "max_dd_pct": 10, "trades": 25},
            )
        ]
        stability = {
            "rsi": {
                "median": 30.0,
                "std_over_range": 0.1,
                "stable": True,
                "unstable": False,
                "values": [29, 30, 31],
            }
        }
        consensus = {"buy": {"rsi": 30}}

        with caplog.at_level("INFO"):
            wf._format_report(results, stability, consensus, ["test warning"])

        assert log_has_re(r".*WFA Results.*", caplog)
        assert log_has_re(r".*Consensus.*weighted by Calmar.*", caplog)
        assert log_has_re(r".*rsi=30.*", caplog)
        assert log_has_re(r".*test warning.*", caplog)
        assert log_has_re(r".*VERDICT:.*", caplog)


# ------------------------------------------------------------------
# Phase 1 new metrics tests
# ------------------------------------------------------------------


class TestWFE:
    def test_compute_wfe_basic(self):
        # 10% profit over 90 days train, 5% over 30 days test
        wfe = WalkForward._compute_wfe(10.0, 5.0, 90, 30)
        # Annualized train ~46%, annualized test ~74% -> WFE > 1
        assert wfe > 0

    def test_compute_wfe_negative_test(self):
        wfe = WalkForward._compute_wfe(10.0, -2.0, 90, 30)
        assert wfe < 0

    def test_compute_wfe_zero_train(self):
        wfe = WalkForward._compute_wfe(0.0, 5.0, 90, 30)
        assert wfe == 0.0

    def test_compute_wfe_zero_days(self):
        assert WalkForward._compute_wfe(10.0, 5.0, 0, 30) == 0.0
        assert WalkForward._compute_wfe(10.0, 5.0, 90, 0) == 0.0


class TestSQNExpectancy:
    def test_sqn_basic(self):
        import numpy as np

        profits = [1.0, 2.0, -0.5, 1.5, 0.8, -0.3, 1.2, 0.6, -0.2, 1.0]
        arr = np.array(profits)
        sqn = len(profits) ** 0.5 * float(np.mean(arr)) / float(np.std(arr))
        assert sqn > 0

    def test_expectancy_positive(self):
        import numpy as np

        profits = [1.0, 2.0, -0.5, 1.5]
        assert float(np.mean(profits)) > 0

    def test_sqn_zero_std(self):
        from math import sqrt

        import numpy as np

        profits = [1.0, 1.0, 1.0]
        arr = np.array(profits)
        std = float(np.std(arr))
        sqn = sqrt(len(arr)) * float(np.mean(arr)) / max(std, 1e-10) if std > 1e-10 else 0.0
        # std is 0, so SQN falls back to 0
        assert sqn == 0.0


class TestDSRWithSkewKurt:
    def test_dsr_skew_kurtosis_changes_result(self):
        # With non-normal moments, DSR should differ from normal-assumption DSR
        dsr_normal = WalkForward._deflated_sharpe_ratio(2.0, 10, 100, skewness=0.0, kurtosis=3.0)
        dsr_skewed = WalkForward._deflated_sharpe_ratio(2.0, 10, 100, skewness=-2.0, kurtosis=10.0)
        # High kurtosis increases SE(SR), which can change the z-score
        assert dsr_skewed != dsr_normal


class TestVerdict:
    @staticmethod
    def _realistic_profits(n, mean=0.1, std=0.5):
        import numpy as np

        rng = np.random.RandomState(42)
        return list(rng.normal(mean, std, n))

    def _make_results(
        self,
        n_windows=5,
        profit=5.0,
        trades=60,
        pf=1.5,
        train_trades=200,
        train_dd=5.0,
        test_dd=4.0,
    ):
        results = []
        for i in range(n_windows):
            w = WalkForwardWindow(
                index=i,
                train_start=datetime(2018, 1, 1, tzinfo=UTC),
                train_end=datetime(2018, 6, 1, tzinfo=UTC),
                test_start=datetime(2018, 6, 8, tzinfo=UTC),
                test_end=datetime(2018, 9, 1, tzinfo=UTC),
            )
            tp = self._realistic_profits(trades)
            results.append(
                WindowResult(
                    window=w,
                    train_metrics={
                        "profit_pct": 20,
                        "calmar": 5,
                        "max_dd_pct": train_dd,
                        "trades": train_trades,
                    },
                    test_metrics={
                        "profit_pct": profit,
                        "calmar": 2,
                        "max_dd_pct": test_dd,
                        "trades": trades,
                        "profit_factor": pf,
                        "sqn": 2.5,
                        "expectancy": 0.01,
                    },
                    test_trade_count=trades,
                    test_trade_profits=tp,
                    wfe=0.6,
                )
            )
        return results

    def test_grade_a(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = self._make_results(n_windows=5, trades=60, profit=5.0)
        stability = {f"p{i}": {"stable": True, "unstable": False} for i in range(10)}
        all_profits = self._realistic_profits(300, mean=0.1, std=0.5)
        grade, _checks = wf._compute_verdict(results, stability, 0.97, 10, all_profits)
        assert grade == "A"

    def test_grade_f_no_profitable(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = self._make_results(n_windows=5, profit=-5.0)
        stability = {f"p{i}": {"stable": True, "unstable": False} for i in range(10)}
        all_profits = self._realistic_profits(300, mean=-0.1, std=0.5)
        grade, _checks = wf._compute_verdict(results, stability, 0.3, 10, all_profits)
        assert grade in ("D", "F")

    def test_grade_c_mixed(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = self._make_results(n_windows=5, trades=30, profit=2.0, pf=1.1)
        stability = {
            **{f"p{i}": {"stable": True, "unstable": False} for i in range(5)},
            **{f"q{i}": {"stable": False, "unstable": True} for i in range(5)},
        }
        all_profits = self._realistic_profits(150, mean=0.01, std=0.5)
        grade, _checks = wf._compute_verdict(results, stability, 0.80, 5, all_profits)
        assert grade in ("C", "D")


class TestNewWarnings:
    def _make_wf(self, conf):
        return WalkForward(conf)

    def test_total_oos_trades_warning(self, walkforward_conf):
        wf = self._make_wf(walkforward_conf)
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=w,
                train_metrics={"trades": 100, "max_dd_pct": 5},
                test_metrics={"trades": 50, "profit_pct": 5, "profit_factor": 1.5, "max_dd_pct": 4},
                test_trade_count=50,
            )
        ]
        walkforward_conf["timeframe_detail"] = "1m"
        warnings = wf._generate_warning_flags(results, {}, 1)
        assert any("Aronson" in w for w in warnings)

    def test_trades_params_ratio_warning(self, walkforward_conf):
        wf = self._make_wf(walkforward_conf)
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=w,
                train_metrics={"trades": 30, "max_dd_pct": 5},
                test_metrics={
                    "trades": 200,
                    "profit_pct": 5,
                    "profit_factor": 1.5,
                    "max_dd_pct": 4,
                },
                test_trade_count=200,
            )
        ]
        walkforward_conf["timeframe_detail"] = "1m"
        warnings = wf._generate_warning_flags(results, {}, 1, n_params=10)
        assert any("Chan" in w for w in warnings)

    def test_dd_ratio_warning(self, walkforward_conf):
        wf = self._make_wf(walkforward_conf)
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=w,
                train_metrics={"trades": 200, "max_dd_pct": 5},
                test_metrics={
                    "trades": 200,
                    "profit_pct": 5,
                    "profit_factor": 1.5,
                    "max_dd_pct": 15,
                },
                test_trade_count=200,
            )
        ]
        walkforward_conf["timeframe_detail"] = "1m"
        warnings = wf._generate_warning_flags(results, {}, 1)
        assert any("Davey" in w and "DD ratio" in w for w in warnings)

    def test_pardo_profitable_windows_warning(self, walkforward_conf):
        wf = self._make_wf(walkforward_conf)
        results = []
        for i in range(4):
            w = WalkForwardWindow(
                index=i,
                train_start=datetime(2018, 1, 1, tzinfo=UTC),
                train_end=datetime(2018, 6, 1, tzinfo=UTC),
                test_start=datetime(2018, 6, 8, tzinfo=UTC),
                test_end=datetime(2018, 9, 1, tzinfo=UTC),
            )
            results.append(
                WindowResult(
                    window=w,
                    train_metrics={"trades": 200, "max_dd_pct": 5},
                    test_metrics={
                        "trades": 100,
                        "profit_pct": -2,
                        "profit_factor": 0.8,
                        "max_dd_pct": 4,
                    },
                    test_trade_count=100,
                )
            )
        walkforward_conf["timeframe_detail"] = "1m"
        warnings = wf._generate_warning_flags(results, {}, 1)
        assert any("Pardo" in w for w in warnings)


# ------------------------------------------------------------------
# CLI command tests
# ------------------------------------------------------------------


class TestStartWalkForward:
    def test_start_walk_forward_missing_deps(self, mocker, walkforward_conf, caplog):
        mocker.patch(
            "freqtrade.commands.walk_forward_commands.start_walk_forward",
            side_effect=OperationalException(
                "No module named 'filelock'. "
                "Please ensure that the hyperopt dependencies are installed."
            ),
        )

    def test_start_filelock(self, mocker, walkforward_conf, caplog):
        hyperopt_mock = MagicMock(
            side_effect=Timeout(WalkForward.get_lock_filename(walkforward_conf))
        )
        patched_configuration_load_config_file(mocker, walkforward_conf)
        mocker.patch(
            "freqtrade.optimize.walk_forward.WalkForward.__init__",
            hyperopt_mock,
        )
        patch_exchange(mocker)

        args = [
            "walk-forward",
            "--config",
            "config.json",
            "--strategy",
            "HyperoptableStrategy",
            "--hyperopt-loss",
            "SharpeHyperOptLossDaily",
            "--epochs",
            "5",
            "--timerange",
            "20180101-20200101",
        ]
        pargs = get_args(args)
        start_walk_forward(pargs)
        assert log_has(
            "Another running instance of freqtrade Walk-Forward detected.",
            caplog,
        )

    def test_walk_forward_cli_args_parsed(self):
        args = get_args(
            [
                "walk-forward",
                "--config",
                "config.json",
                "--strategy",
                "TestStrategy",
                "--hyperopt-loss",
                "SharpeHyperOptLossDaily",
                "--timerange",
                "20180101-20200101",
                "--wf-windows",
                "7",
                "--wf-train-ratio",
                "0.8",
                "--wf-embargo-days",
                "14",
                "--wf-holdout-months",
                "3",
                "--wf-min-test-trades",
                "50",
            ]
        )
        assert args["wf_windows"] == 7
        assert args["wf_train_ratio"] == 0.8
        assert args["wf_embargo_days"] == 14
        assert args["wf_holdout_months"] == 3
        assert args["wf_min_test_trades"] == 50

    def test_walk_forward_cli_defaults(self):
        args = get_args(
            [
                "walk-forward",
                "--config",
                "config.json",
                "--strategy",
                "TestStrategy",
                "--hyperopt-loss",
                "SharpeHyperOptLossDaily",
                "--timerange",
                "20180101-20200101",
            ]
        )
        assert args["wf_windows"] == 5
        assert args["wf_train_ratio"] == 0.75
        assert args["wf_embargo_days"] == 7
        assert args["wf_holdout_months"] == 0
        assert args["wf_min_test_trades"] == 30


# ------------------------------------------------------------------
# Initialization
# ------------------------------------------------------------------


class TestInit:
    def test_creates_wfa_dir(self, walkforward_conf, tmp_path):
        WalkForward(walkforward_conf)
        assert (tmp_path / "walk_forward").is_dir()

    def test_config_mapping(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        assert wf.n_windows == 3
        assert wf.train_ratio == 0.75
        assert wf.embargo_days == 7
        assert wf.holdout_months == 0
        assert wf.min_test_trades == 30
        assert wf.strategy_name == "HyperoptableStrategy"


# ------------------------------------------------------------------
# WFA Dashboard
# ------------------------------------------------------------------


class TestWFADashboard:
    @staticmethod
    def _make_windows():
        return [
            WalkForwardWindow(
                index=0,
                train_start=datetime(2025, 1, 1, tzinfo=UTC),
                train_end=datetime(2025, 4, 1, tzinfo=UTC),
                test_start=datetime(2025, 4, 8, tzinfo=UTC),
                test_end=datetime(2025, 5, 8, tzinfo=UTC),
            ),
            WalkForwardWindow(
                index=1,
                train_start=datetime(2025, 2, 8, tzinfo=UTC),
                train_end=datetime(2025, 5, 8, tzinfo=UTC),
                test_start=datetime(2025, 5, 15, tzinfo=UTC),
                test_end=datetime(2025, 6, 15, tzinfo=UTC),
            ),
        ]

    def test_dashboard_init(self):
        from freqtrade.optimize.wfa_output import WFADashboard

        windows = self._make_windows()
        dash = WFADashboard(windows, "TestStrat", 200, "USDC")
        assert dash._n_windows == 2
        assert dash._strategy == "TestStrat"
        assert dash._epochs == 200

    def test_set_window_resets_state(self):
        from freqtrade.optimize.wfa_output import WFADashboard

        windows = self._make_windows()
        dash = WFADashboard(windows, "TestStrat", 200, "USDC")
        dash._ho_epoch = 50
        dash._ho_best = {"loss": 0.5}
        dash.set_window(windows[1])
        assert dash._current_idx == 1
        assert dash._ho_epoch == 0
        assert dash._ho_best is None
        assert dash._phase_log[1]["hyperopt"] == "active"

    def test_set_phase_transitions(self):
        from freqtrade.optimize.wfa_output import WFADashboard

        windows = self._make_windows()
        dash = WFADashboard(windows, "TestStrat", 200, "USDC")
        dash.set_window(windows[0])
        dash.set_phase("backtest_optimized")
        assert dash._phase_log[0]["hyperopt"] == "done"
        assert dash._phase_log[0]["backtest_optimized"] == "active"

    def test_on_epoch_updates_best(self):
        from freqtrade.optimize.wfa_output import WFADashboard

        windows = self._make_windows()
        dash = WFADashboard(windows, "TestStrat", 200, "USDC")
        dash.on_epoch({"current_epoch": 10, "is_best": False, "loss": 0.5})
        assert dash._ho_epoch == 10
        assert dash._ho_best is None

        dash.on_epoch({"current_epoch": 15, "is_best": True, "loss": 0.3})
        assert dash._ho_epoch == 15
        assert dash._ho_best["loss"] == 0.3

    def test_complete_window(self):
        from freqtrade.optimize.wfa_output import WFADashboard

        windows = self._make_windows()
        dash = WFADashboard(windows, "TestStrat", 200, "USDC")
        dash.set_window(windows[0])
        result = WindowResult(
            window=windows[0],
            test_metrics={"profit_pct": 12.5, "trades": 100},
        )
        dash.complete_window(result)
        assert len(dash._completed) == 1
        assert all(v == "done" for v in dash._phase_log[0].values())

    def test_build_renders_without_error(self):
        from freqtrade.optimize.wfa_output import WFADashboard

        windows = self._make_windows()
        dash = WFADashboard(windows, "TestStrat", 200, "USDC")
        dash.set_window(windows[0])
        dash.on_epoch(
            {
                "current_epoch": 50,
                "is_best": True,
                "loss": -0.95,
                "results_metrics": {
                    "profit_total": 0.25,
                    "total_trades": 150,
                    "max_drawdown_account": 0.05,
                },
            }
        )
        panel = dash._build()
        assert panel is not None

    def test_build_with_completed_windows(self):
        from freqtrade.optimize.wfa_output import WFADashboard

        windows = self._make_windows()
        dash = WFADashboard(windows, "TestStrat", 200, "USDC")
        dash.set_window(windows[0])
        dash.complete_window(
            WindowResult(
                window=windows[0],
                test_metrics={"profit_pct": 10, "trades": 80, "calmar": 1.5, "max_dd_pct": 5},
                baseline_metrics={"profit_pct": 5},
            )
        )
        dash.set_window(windows[1])
        dash.set_phase("backtest_optimized")
        panel = dash._build()
        assert panel is not None

    def test_build_with_market_context_and_hhi(self):
        from freqtrade.optimize.wfa_output import WFADashboard

        windows = self._make_windows()
        dash = WFADashboard(windows, "TestStrat", 200, "USDC")
        dash.set_window(windows[0])
        dash.complete_window(
            WindowResult(
                window=windows[0],
                test_metrics={
                    "profit_pct": 10,
                    "trades": 80,
                    "calmar": 1.5,
                    "max_dd_pct": 5,
                    "hhi": 0.12,
                    "top1_pct": 30,
                },
                baseline_metrics={"profit_pct": 5},
                market_context={
                    "btc_change_pct": 15.2,
                    "atr_pct": 3.1,
                    "volatility_ann_pct": 60,
                    "regime": "bull",
                },
            )
        )
        panel = dash._build()
        assert panel is not None

    def test_insights_panel_renders(self):
        from freqtrade.optimize.wfa_output import WFADashboard

        windows = self._make_windows()
        dash = WFADashboard(windows, "TestStrat", 200, "USDC")
        for i, w in enumerate(windows):
            dash.set_window(w)
            dash.complete_window(
                WindowResult(
                    window=w,
                    test_metrics={
                        "profit_pct": 5 + i * 3,
                        "trades": 60 + i * 10,
                        "calmar": 1.0 + i * 0.5,
                        "max_dd_pct": 8 - i,
                        "hhi": 0.05,
                        "top1_pct": 15,
                    },
                    baseline_metrics={"profit_pct": 2},
                    market_context={"regime": "range"},
                    params={"buy": {"rsi_low": 25 + i, "vol_mult": 1.5 + i * 0.1}},
                )
            )
        panel = dash._build()
        assert panel is not None
        insights = dash._build_insights()
        assert insights is not None

    def test_insights_with_one_window(self):
        from freqtrade.optimize.wfa_output import WFADashboard

        windows = self._make_windows()
        dash = WFADashboard(windows, "TestStrat", 200, "USDC")
        dash.set_window(windows[0])
        dash.complete_window(
            WindowResult(
                window=windows[0],
                test_metrics={
                    "profit_pct": -5,
                    "trades": 30,
                    "calmar": -0.5,
                    "max_dd_pct": 15,
                    "hhi": 0.25,
                },
                baseline_metrics={"profit_pct": 2},
                params={"buy": {"rsi_low": 30}},
            )
        )
        insights = dash._build_insights()
        assert insights is not None


# ------------------------------------------------------------------
# Anchored window mode (Improvement #1)
# ------------------------------------------------------------------


class TestAnchoredWindows:
    def test_anchored_windows_basic(self, walkforward_conf):
        walkforward_conf["wf_mode"] = "anchored"
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()

        assert len(windows) == 3
        for w in windows:
            assert w.train_start == windows[0].train_start
        for w in windows:
            assert w.train_start < w.train_end
            assert w.test_start < w.test_end

    def test_anchored_train_grows(self, walkforward_conf):
        walkforward_conf["wf_mode"] = "anchored"
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()

        for i in range(len(windows) - 1):
            days_i = (windows[i].train_end - windows[i].train_start).days
            days_j = (windows[i + 1].train_end - windows[i + 1].train_start).days
            assert days_j > days_i

    def test_anchored_disjoint_tests(self, walkforward_conf):
        walkforward_conf["wf_mode"] = "anchored"
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()

        for i in range(len(windows) - 1):
            assert windows[i].test_end <= windows[i + 1].test_start

    def test_anchored_embargo(self, walkforward_conf):
        walkforward_conf["wf_mode"] = "anchored"
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()

        for w in windows:
            gap = (w.test_start - w.train_end).days
            assert gap == walkforward_conf["wf_embargo_days"]

    def test_anchored_too_many_windows_raises(self, walkforward_conf):
        walkforward_conf["wf_mode"] = "anchored"
        walkforward_conf["wf_windows"] = 50
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match=r"window too short"):
            wf._compute_windows()

    def test_rolling_mode_is_default(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        assert wf.wf_mode == "rolling"


# ------------------------------------------------------------------
# Concentrated profit check (Improvement #3)
# ------------------------------------------------------------------


class TestConcentration:
    def test_compute_concentration_basic(self):
        profits = [100, 50, 30, 20]
        result = WalkForward._compute_concentration(profits)
        assert "hhi" in result
        assert "top1_pct" in result
        assert result["hhi"] > 0
        assert result["top1_pct"] == 50.0

    def test_compute_concentration_single_trade(self):
        result = WalkForward._compute_concentration([100.0])
        assert result["hhi"] == 1.0
        assert result["top1_pct"] == 100.0

    def test_compute_concentration_empty(self):
        result = WalkForward._compute_concentration([])
        assert result["hhi"] == 0.0
        assert result["top1_pct"] == 0.0

    def test_compute_concentration_mixed_signs(self):
        profits = [100, -50, 30, -20]
        result = WalkForward._compute_concentration(profits)
        assert result["hhi"] > 0
        assert result["top1_pct"] > 0

    def test_hhi_perfectly_diversified(self):
        profits = [10.0] * 100
        result = WalkForward._compute_concentration(profits)
        assert result["hhi"] == pytest.approx(0.01, abs=0.001)


# ------------------------------------------------------------------
# Weighted consensus (Improvement #4)
# ------------------------------------------------------------------


class TestWeightedConsensus:
    def test_unweighted_consensus(self):
        params = [
            {"buy": {"a": 10, "b": 1.0}},
            {"buy": {"a": 20, "b": 2.0}},
            {"buy": {"a": 30, "b": 3.0}},
        ]
        c = WalkForward._compute_consensus_params(params)
        assert c["buy"]["a"] == 20
        assert c["buy"]["b"] == 2.0

    def test_weighted_consensus_skews(self):
        params = [
            {"buy": {"a": 10}},
            {"buy": {"a": 20}},
            {"buy": {"a": 30}},
        ]
        weights = [0.1, 0.1, 10.0]
        c = WalkForward._compute_consensus_params(params, weights=weights)
        assert c["buy"]["a"] == 30

    def test_weighted_consensus_equal_weights(self):
        params = [
            {"buy": {"a": 10.0}},
            {"buy": {"a": 20.0}},
            {"buy": {"a": 30.0}},
        ]
        weights = [1.0, 1.0, 1.0]
        c = WalkForward._compute_consensus_params(params, weights=weights)
        assert c["buy"]["a"] == 20.0

    def test_weighted_median_basic(self):
        med = WalkForward._weighted_median([1.0, 2.0, 3.0], [1.0, 1.0, 1.0])
        assert med == 2.0

    def test_weighted_median_heavy_last(self):
        med = WalkForward._weighted_median([1.0, 2.0, 3.0], [0.1, 0.1, 10.0])
        assert med == 3.0


# ------------------------------------------------------------------
# Deflated Sharpe Ratio (Improvement #5)
# ------------------------------------------------------------------


class TestDeflatedSharpe:
    def test_dsr_high_sr_few_trials(self):
        dsr = WalkForward._deflated_sharpe_ratio(sr_observed=3.0, n_trials=10, n_obs=252)
        assert 0 < dsr <= 1

    def test_dsr_low_sr_many_trials(self):
        dsr = WalkForward._deflated_sharpe_ratio(sr_observed=0.5, n_trials=1000, n_obs=100)
        assert dsr < 0.5

    def test_dsr_zero_sr(self):
        dsr = WalkForward._deflated_sharpe_ratio(sr_observed=0.0, n_trials=100, n_obs=252)
        assert dsr < 0.5

    def test_dsr_edge_cases(self):
        assert WalkForward._deflated_sharpe_ratio(1.0, 1, 100) == 0.0
        assert WalkForward._deflated_sharpe_ratio(1.0, 10, 1) == 0.0

    def test_dsr_increases_with_sr(self):
        dsr_low = WalkForward._deflated_sharpe_ratio(0.5, 100, 252)
        dsr_high = WalkForward._deflated_sharpe_ratio(3.0, 100, 252)
        assert dsr_high > dsr_low

    def test_dsr_decreases_with_more_trials(self):
        dsr_few = WalkForward._deflated_sharpe_ratio(2.0, 10, 252)
        dsr_many = WalkForward._deflated_sharpe_ratio(2.0, 10000, 252)
        assert dsr_few > dsr_many

    def test_norm_cdf_basic(self):
        assert WalkForward._norm_cdf(0) == pytest.approx(0.5, abs=0.001)
        assert WalkForward._norm_cdf(10) > 0.99
        assert WalkForward._norm_cdf(-10) < 0.01


# ------------------------------------------------------------------
# Market context (Improvement #2)
# ------------------------------------------------------------------


class TestMarketContext:
    def test_market_context_graceful_failure(self, walkforward_conf):
        """Should return empty dict if BTC data not available."""
        wf = WalkForward(walkforward_conf)
        ctx = wf._compute_market_context(
            datetime(2019, 1, 1, tzinfo=UTC),
            datetime(2019, 3, 1, tzinfo=UTC),
        )
        assert isinstance(ctx, dict)

    def test_concentration_in_warning_flags(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = [
            WindowResult(
                window=WalkForwardWindow(
                    index=0,
                    train_start=datetime(2018, 1, 1, tzinfo=UTC),
                    train_end=datetime(2018, 6, 1, tzinfo=UTC),
                    test_start=datetime(2018, 6, 8, tzinfo=UTC),
                    test_end=datetime(2018, 8, 1, tzinfo=UTC),
                ),
                test_metrics={
                    "trades": 50,
                    "top1_pct": 60,
                    "hhi": 0.2,
                },
                test_trade_count=50,
            ),
        ]
        warnings = wf._generate_warning_flags(results, {}, 1)
        assert any("top-1" in w for w in warnings)
        assert any("HHI" in w for w in warnings)


# ------------------------------------------------------------------
# Phase 2: Monte Carlo trade shuffle
# ------------------------------------------------------------------


class TestMonteCarlo:
    def test_mc_basic_distribution(self):
        import numpy as np

        rng = np.random.RandomState(123)
        profits = list(rng.normal(0.5, 2.0, 200))
        mc = WalkForward._mc_trade_shuffle(profits, starting_balance=1000, n_simulations=500)
        assert mc.n_simulations == 500
        assert mc.max_dd_p5 < mc.max_dd_p95
        assert mc.return_dd_p5 < mc.return_dd_p95
        assert mc.total_return_pct > 0

    def test_mc_empty_profits(self):
        mc = WalkForward._mc_trade_shuffle([], starting_balance=1000)
        assert mc.n_simulations == 0
        assert mc.total_return_pct == 0.0

    def test_mc_few_trades(self):
        mc = WalkForward._mc_trade_shuffle([1.0, 2.0, 3.0], starting_balance=1000)
        assert mc.n_simulations == 0

    def test_mc_all_positive(self):
        profits = [1.0] * 50
        mc = WalkForward._mc_trade_shuffle(profits, starting_balance=1000, n_simulations=100)
        assert mc.total_return_pct > 0
        assert mc.max_consec_loss_p50 == 0
        assert mc.return_dd_p50 > 0

    def test_mc_all_negative(self):
        profits = [-1.0] * 50
        mc = WalkForward._mc_trade_shuffle(profits, starting_balance=1000, n_simulations=100)
        assert mc.total_return_pct < 0
        assert mc.max_consec_loss_p50 == 50

    def test_mc_deterministic_with_seed(self):
        import numpy as np

        rng = np.random.RandomState(99)
        profits = list(rng.normal(0.3, 1.0, 100))
        mc1 = WalkForward._mc_trade_shuffle(profits, seed=42, n_simulations=200)
        mc2 = WalkForward._mc_trade_shuffle(profits, seed=42, n_simulations=200)
        assert mc1.max_dd_p50 == mc2.max_dd_p50
        assert mc1.return_dd_p50 == mc2.return_dd_p50

    def test_mc_dd_varies_with_order(self):
        import numpy as np

        rng = np.random.RandomState(77)
        profits = list(rng.normal(0.2, 3.0, 300))
        mc = WalkForward._mc_trade_shuffle(profits, starting_balance=1000, n_simulations=500)
        assert mc.max_dd_p5 < mc.max_dd_p95


# ------------------------------------------------------------------
# Phase 2: OOS equity curve concatenation
# ------------------------------------------------------------------


class TestOOSEquityCurve:
    def test_concat_basic(self):
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=w,
                test_trade_profits=[10.0, 5.0, -3.0, 8.0, 2.0],
            ),
            WindowResult(
                window=w,
                test_trade_profits=[4.0, -2.0, 6.0, 1.0],
            ),
        ]
        eq = WalkForward._concat_oos_equity(results, starting_balance=1000)
        assert eq.n_trades == 9
        assert eq.total_return_pct > 0
        assert eq.max_dd_pct >= 0
        assert eq.k_ratio > 0

    def test_concat_empty(self):
        eq = WalkForward._concat_oos_equity([], starting_balance=1000)
        assert eq.n_trades == 0
        assert eq.total_return_pct == 0.0

    def test_concat_negative(self):
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(window=w, test_trade_profits=[-10.0, -5.0, -3.0]),
        ]
        eq = WalkForward._concat_oos_equity(results, starting_balance=1000)
        assert eq.total_return_pct < 0
        assert eq.max_dd_pct > 0

    def test_k_ratio_equity_basic(self):
        import numpy as np

        equity = np.array([1000.0, 1010.0, 1020.0, 1030.0, 1040.0])
        k = WalkForward._k_ratio_equity(equity)
        assert k > 5.0

    def test_k_ratio_equity_short(self):
        import numpy as np

        equity = np.array([1000.0, 1010.0])
        k = WalkForward._k_ratio_equity(equity)
        assert k == 0.0


# ------------------------------------------------------------------
# Phase 2: Carver discount factor
# ------------------------------------------------------------------


class TestCarverDiscount:
    def test_discount_basic(self):
        mc = MCResult(return_dd_p5=1.0, return_dd_p50=2.0, n_simulations=1000)
        d = WalkForward._carver_discount(mc)
        assert d == pytest.approx(0.5, abs=0.01)

    def test_discount_low_p5(self):
        mc = MCResult(return_dd_p5=0.1, return_dd_p50=2.0, n_simulations=1000)
        d = WalkForward._carver_discount(mc)
        assert d < 0.1

    def test_discount_zero_p50(self):
        mc = MCResult(return_dd_p5=1.0, return_dd_p50=0.0, n_simulations=1000)
        d = WalkForward._carver_discount(mc)
        assert d == 0.0

    def test_discount_robust(self):
        mc = MCResult(return_dd_p5=1.6, return_dd_p50=2.0, n_simulations=1000)
        d = WalkForward._carver_discount(mc)
        assert d == pytest.approx(0.8, abs=0.01)


# ------------------------------------------------------------------
# Phase 2: MC warnings
# ------------------------------------------------------------------


class TestMCWarnings:
    def test_mc_return_dd_low_warning(self):
        mc = MCResult(
            return_dd_p5=0.2,
            return_dd_p50=1.5,
            max_dd_p50=8.0,
            max_dd_p95=12.0,
            n_simulations=1000,
        )
        warnings = WalkForward._mc_warnings(mc)
        assert any("return/DD" in w for w in warnings)

    def test_mc_tail_dd_warning(self):
        mc = MCResult(max_dd_p50=5.0, max_dd_p95=12.0, n_simulations=1000)
        warnings = WalkForward._mc_warnings(mc)
        assert any("tail DD" in w for w in warnings)

    def test_mc_consec_loss_warning(self):
        mc = MCResult(max_consec_loss_p95=12, n_simulations=1000)
        warnings = WalkForward._mc_warnings(mc)
        assert any("consecutive losses" in w for w in warnings)

    def test_mc_no_warnings_clean(self):
        mc = MCResult(
            return_dd_p5=1.5,
            return_dd_p50=2.5,
            return_dd_p95=4.0,
            max_dd_p5=2.0,
            max_dd_p50=5.0,
            max_dd_p95=8.0,
            max_consec_loss_p50=3,
            max_consec_loss_p95=6,
            n_simulations=1000,
        )
        warnings = WalkForward._mc_warnings(mc)
        assert len(warnings) == 0


# ------------------------------------------------------------------
# Phase 2: MC verdict check
# ------------------------------------------------------------------


class TestMCVerdict:
    @staticmethod
    def _realistic_profits(n, mean=0.1, std=0.5):
        import numpy as np

        rng = np.random.RandomState(42)
        return list(rng.normal(mean, std, n))

    def _make_results(self, n_windows=5, trades=60, profit=5.0):
        results = []
        for i in range(n_windows):
            w = WalkForwardWindow(
                index=i,
                train_start=datetime(2018, 1, 1, tzinfo=UTC),
                train_end=datetime(2018, 6, 1, tzinfo=UTC),
                test_start=datetime(2018, 6, 8, tzinfo=UTC),
                test_end=datetime(2018, 9, 1, tzinfo=UTC),
            )
            tp = self._realistic_profits(trades)
            results.append(
                WindowResult(
                    window=w,
                    train_metrics={
                        "profit_pct": 20,
                        "calmar": 5,
                        "max_dd_pct": 5,
                        "trades": 200,
                    },
                    test_metrics={
                        "profit_pct": profit,
                        "calmar": 2,
                        "max_dd_pct": 4,
                        "trades": trades,
                        "profit_factor": 1.5,
                        "sqn": 2.5,
                        "expectancy": 0.01,
                    },
                    test_trade_count=trades,
                    test_trade_profits=tp,
                    wfe=0.6,
                )
            )
        return results

    def test_mc_check_in_verdict_pass(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = self._make_results()
        stability = {f"p{i}": {"stable": True, "unstable": False} for i in range(10)}
        all_profits = self._realistic_profits(300, mean=0.1, std=0.5)
        mc = MCResult(return_dd_p5=1.5, return_dd_p50=2.5, n_simulations=1000)
        grade, checks = wf._compute_verdict(results, stability, 0.97, 10, all_profits, mc=mc)
        mc_check = [c for c in checks if c[0] == "mc_robust"]
        assert len(mc_check) == 1
        assert mc_check[0][1] is True
        assert grade == "A"

    def test_mc_check_in_verdict_fail(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = self._make_results()
        stability = {f"p{i}": {"stable": True, "unstable": False} for i in range(10)}
        all_profits = self._realistic_profits(300, mean=0.1, std=0.5)
        mc = MCResult(return_dd_p5=0.1, return_dd_p50=2.0, n_simulations=1000)
        grade, checks = wf._compute_verdict(results, stability, 0.97, 10, all_profits, mc=mc)
        mc_check = [c for c in checks if c[0] == "mc_robust"]
        assert len(mc_check) == 1
        assert mc_check[0][1] is False
        assert grade in ("C", "D")

    def test_no_mc_means_no_mc_check(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = self._make_results()
        stability = {f"p{i}": {"stable": True, "unstable": False} for i in range(10)}
        all_profits = self._realistic_profits(300, mean=0.1, std=0.5)
        _grade, checks = wf._compute_verdict(results, stability, 0.97, 10, all_profits)
        mc_check = [c for c in checks if c[0] == "mc_robust"]
        assert len(mc_check) == 0


# ------------------------------------------------------------------
# Phase 2: Report MC and equity logging
# ------------------------------------------------------------------


class TestReportMCAndEquity:
    def test_format_report_with_mc(self, walkforward_conf, caplog):
        wf = WalkForward(walkforward_conf)
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=w,
                train_metrics={"profit_pct": 10, "calmar": 2, "max_dd_pct": 5, "trades": 50},
                test_metrics={"profit_pct": 5, "calmar": 1, "max_dd_pct": 8, "trades": 30},
            )
        ]
        mc = MCResult(
            total_return_pct=12.5,
            max_dd_p5=3.0,
            max_dd_p50=6.0,
            max_dd_p95=10.0,
            return_dd_p5=1.2,
            return_dd_p50=2.1,
            return_dd_p95=4.0,
            max_consec_loss_p50=3,
            max_consec_loss_p95=7,
            n_simulations=1000,
        )
        oos_eq = OOSEquityCurve(
            total_return_pct=12.5,
            max_dd_pct=8.1,
            k_ratio=1.82,
            n_trades=200,
        )
        with caplog.at_level("INFO"):
            wf._format_report(
                results,
                {},
                {"buy": {"rsi": 30}},
                [],
                mc=mc,
                oos_equity=oos_eq,
            )
        assert log_has_re(r".*MC Shuffle.*1000 sims.*", caplog)
        assert log_has_re(r".*Carver discount.*", caplog)
        assert log_has_re(r".*OOS Equity.*12\.5.*", caplog)

    def test_log_mc_and_equity_no_mc(self, caplog):
        with caplog.at_level("INFO"):
            WalkForward._log_mc_and_equity(None, None)
        assert not any("MC Shuffle" in r.message for r in caplog.records)


# ------------------------------------------------------------------
# Phase 2: Export JSON with MC and equity
# ------------------------------------------------------------------


class TestExportPhase2:
    def test_export_includes_mc_and_equity(self, walkforward_conf, tmp_path):
        wf = WalkForward(walkforward_conf)
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=w,
                train_metrics={"profit_pct": 10},
                test_metrics={"profit_pct": 5},
            )
        ]
        mc = MCResult(
            total_return_pct=12.5,
            max_dd_p5=3.0,
            max_dd_p50=6.0,
            max_dd_p95=10.0,
            return_dd_p5=1.2,
            return_dd_p50=2.1,
            return_dd_p95=4.0,
            max_consec_loss_p50=3,
            max_consec_loss_p95=7,
            n_simulations=1000,
        )
        oos_eq = OOSEquityCurve(
            total_return_pct=12.5,
            max_dd_pct=8.1,
            k_ratio=1.82,
            n_trades=200,
        )
        build_data = wf._build_results_data(
            results,
            {},
            {"buy": {"rsi": 30}},
            mc=mc,
            oos_equity=oos_eq,
        )
        path = wf._export_results_json(build_data)
        with path.open("r") as f:
            data = rapidjson.load(f)

        assert data["monte_carlo"] is not None
        assert data["monte_carlo"]["n_simulations"] == 1000
        assert data["monte_carlo"]["return_dd_p5"] == 1.2
        assert data["monte_carlo"]["carver_discount"] is not None
        assert data["monte_carlo"]["max_consec_loss_p95"] == 7
        assert data["oos_equity"] is not None
        assert data["oos_equity"]["total_return_pct"] == 12.5
        assert data["oos_equity"]["k_ratio"] == 1.82

    def test_export_no_mc(self, walkforward_conf, tmp_path):
        wf = WalkForward(walkforward_conf)
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=w,
                train_metrics={"profit_pct": 10},
                test_metrics={"profit_pct": 5},
            )
        ]
        build_data = wf._build_results_data(results, {}, {"buy": {"rsi": 30}})
        path = wf._export_results_json(build_data)
        with path.open("r") as f:
            data = rapidjson.load(f)
        assert data["monte_carlo"] is None
        assert data["oos_equity"] is None


# ------------------------------------------------------------------
# Phase 3: Regime analysis
# ------------------------------------------------------------------


class TestRegimeAnalysis:
    @staticmethod
    def _make_result(regime: str, profit: float, dd: float = 5.0) -> WindowResult:
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2020, 1, 1, tzinfo=UTC),
            train_end=datetime(2020, 6, 1, tzinfo=UTC),
            test_start=datetime(2020, 6, 8, tzinfo=UTC),
            test_end=datetime(2020, 9, 1, tzinfo=UTC),
        )
        return WindowResult(
            window=w,
            test_metrics={"profit_pct": profit, "max_dd_pct": dd},
            market_context={"regime": regime},
        )

    def test_regime_basic(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = [
            self._make_result("bull", 10.0),
            self._make_result("bull", 8.0),
            self._make_result("bear", -5.0),
            self._make_result("range", 2.0),
        ]
        regime = wf._analyze_regimes(results)
        assert "bull" in regime.regime_stats
        assert "bear" in regime.regime_stats
        assert "range" in regime.regime_stats
        assert regime.regime_stats["bull"]["windows"] == 2
        assert regime.regime_stats["bull"]["avg_profit"] > 0
        assert regime.worst_regime == "bear"
        assert regime.regime_dependent is True

    def test_regime_all_profitable(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = [
            self._make_result("bull", 10.0),
            self._make_result("bear", 3.0),
            self._make_result("range", 5.0),
        ]
        regime = wf._analyze_regimes(results)
        assert regime.regime_dependent is False

    def test_regime_empty(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        regime = wf._analyze_regimes([])
        assert regime.regime_stats == {}
        assert regime.regime_dependent is False

    def test_regime_no_context(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2020, 1, 1, tzinfo=UTC),
            train_end=datetime(2020, 6, 1, tzinfo=UTC),
            test_start=datetime(2020, 6, 8, tzinfo=UTC),
            test_end=datetime(2020, 9, 1, tzinfo=UTC),
        )
        results = [WindowResult(window=w, test_metrics={"profit_pct": 5.0})]
        regime = wf._analyze_regimes(results)
        assert regime.regime_stats == {}


# ------------------------------------------------------------------
# Phase 3: Parameter perturbation
# ------------------------------------------------------------------


class TestPerturbParams:
    def test_perturb_basic(self):
        consensus = {"buy": {"rsi": 30, "vol": 2.5}}
        search_ranges = {"rsi": (10.0, 50.0), "vol": (1.0, 5.0)}
        variants = WalkForward._perturb_params(consensus, search_ranges, 0.05, 10, seed=42)
        assert len(variants) == 10
        for v in variants:
            assert "buy" in v
            assert "rsi" in v["buy"]
            assert "vol" in v["buy"]
            assert 10 <= v["buy"]["rsi"] <= 50
            assert 1.0 <= v["buy"]["vol"] <= 5.0

    def test_perturb_deterministic(self):
        consensus = {"buy": {"rsi": 30}}
        ranges = {"rsi": (10.0, 50.0)}
        v1 = WalkForward._perturb_params(consensus, ranges, 0.05, 5, seed=99)
        v2 = WalkForward._perturb_params(consensus, ranges, 0.05, 5, seed=99)
        assert v1 == v2

    def test_perturb_int_stays_int(self):
        consensus = {"buy": {"rsi": 30}}
        ranges = {"rsi": (10.0, 50.0)}
        variants = WalkForward._perturb_params(consensus, ranges, 0.10, 20, seed=42)
        for v in variants:
            assert isinstance(v["buy"]["rsi"], int)

    def test_perturb_clamps_to_range(self):
        consensus = {"buy": {"rsi": 49}}
        ranges = {"rsi": (10.0, 50.0)}
        variants = WalkForward._perturb_params(consensus, ranges, 0.50, 100, seed=42)
        for v in variants:
            assert 10 <= v["buy"]["rsi"] <= 50

    def test_perturb_non_numeric_passthrough(self):
        consensus = {"buy": {"rsi": 30, "method": "ema"}}
        ranges = {"rsi": (10.0, 50.0)}
        variants = WalkForward._perturb_params(consensus, ranges, 0.05, 5, seed=42)
        for v in variants:
            assert v["buy"]["method"] == "ema"


# ------------------------------------------------------------------
# Phase 3: Regime warnings
# ------------------------------------------------------------------


class TestRegimeWarnings:
    def test_regime_dependent_warning(self):
        regime = RegimeAnalysis(
            regime_stats={
                "bull": {"windows": 3, "avg_profit": 8.0, "pct_profitable": 1.0},
                "bear": {"windows": 2, "avg_profit": -4.0, "pct_profitable": 0.0},
            },
            worst_regime="bear",
            regime_dependent=True,
        )
        warnings = WalkForward._regime_warnings(regime)
        assert any("bear" in w for w in warnings)
        assert any("tip #69" in w for w in warnings)

    def test_single_regime_warning(self):
        regime = RegimeAnalysis(
            regime_stats={"bull": {"windows": 5, "avg_profit": 10.0}},
            worst_regime="bull",
            regime_dependent=False,
        )
        warnings = WalkForward._regime_warnings(regime)
        assert any("Only bull" in w for w in warnings)

    def test_no_regime_warnings(self):
        regime = RegimeAnalysis(
            regime_stats={
                "bull": {"windows": 2, "avg_profit": 5.0},
                "bear": {"windows": 2, "avg_profit": 3.0},
            },
            worst_regime="bear",
            regime_dependent=False,
        )
        warnings = WalkForward._regime_warnings(regime)
        assert len(warnings) == 0


# ------------------------------------------------------------------
# Phase 3: Perturbation warnings
# ------------------------------------------------------------------


class TestPerturbWarnings:
    def test_low_profitable_warning(self):
        perturb = PerturbResult(
            n_perturbations=60,
            pct_profitable=0.50,
            sensitivity=1.0,
            profit_p5=-2.0,
            profit_p50=1.0,
            profit_p95=5.0,
        )
        warnings = WalkForward._perturb_warnings(perturb)
        assert any("50%" in w for w in warnings)
        assert any("tip #81" in w for w in warnings)

    def test_high_sensitivity_warning(self):
        perturb = PerturbResult(
            n_perturbations=60,
            pct_profitable=0.90,
            sensitivity=3.5,
            profit_p5=0.5,
            profit_p50=2.0,
            profit_p95=4.0,
        )
        warnings = WalkForward._perturb_warnings(perturb)
        assert any("sensitivity" in w for w in warnings)

    def test_clean_perturb(self):
        perturb = PerturbResult(
            n_perturbations=60,
            pct_profitable=0.85,
            sensitivity=0.8,
            profit_p5=1.0,
            profit_p50=3.0,
            profit_p95=6.0,
        )
        warnings = WalkForward._perturb_warnings(perturb)
        assert len(warnings) == 0


# ------------------------------------------------------------------
# Phase 3: Multi-seed warnings
# ------------------------------------------------------------------


class TestMultiSeedWarnings:
    def test_low_convergence_warning(self):
        ms = MultiSeedResult(n_seeds=5, convergence_pct=0.40, seed_params=[])
        warnings = WalkForward._multi_seed_warnings(ms)
        assert any("40%" in w for w in warnings)
        assert any("tip #76" in w for w in warnings)

    def test_good_convergence(self):
        ms = MultiSeedResult(n_seeds=5, convergence_pct=0.80, seed_params=[])
        warnings = WalkForward._multi_seed_warnings(ms)
        assert len(warnings) == 0


# ------------------------------------------------------------------
# Phase 3: Verdict checks #11 and #12
# ------------------------------------------------------------------


class TestPhase3Verdict:
    @staticmethod
    def _make_base_results():
        windows = []
        for i in range(5):
            w = WalkForwardWindow(
                index=i,
                train_start=datetime(2020, 1, 1, tzinfo=UTC),
                train_end=datetime(2020, 6, 1, tzinfo=UTC),
                test_start=datetime(2020, 6, 8, tzinfo=UTC),
                test_end=datetime(2020, 9, 1, tzinfo=UTC),
            )
            windows.append(
                WindowResult(
                    window=w,
                    train_metrics={"profit_pct": 10, "trades": 100, "max_dd_pct": 5},
                    test_metrics={
                        "profit_pct": 5,
                        "calmar": 1.5,
                        "max_dd_pct": 4,
                        "trades": 60,
                        "sharpe": 1.2,
                        "profit_factor": 1.5,
                    },
                    wfe=0.6,
                    test_trade_count=60,
                )
            )
        return windows

    def test_verdict_with_perturb_pass(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = self._make_base_results()
        stability = {"rsi": {"stable": True}, "vol": {"stable": True}}
        perturb = PerturbResult(
            n_perturbations=60,
            pct_profitable=0.85,
            sensitivity=0.5,
            profit_p5=1.0,
            profit_p50=3.0,
            profit_p95=6.0,
        )
        _, checks = wf._compute_verdict(
            results,
            stability,
            0.98,
            2,
            [0.01] * 300,
            perturb=perturb,
        )
        check_dict = {name: ok for name, ok, _ in checks}
        assert "param_robust" in check_dict
        assert check_dict["param_robust"] is True

    def test_verdict_with_perturb_fail(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = self._make_base_results()
        stability = {"rsi": {"stable": True}}
        perturb = PerturbResult(
            n_perturbations=60,
            pct_profitable=0.50,
            sensitivity=2.5,
            profit_p5=-3.0,
            profit_p50=0.5,
            profit_p95=4.0,
        )
        _, checks = wf._compute_verdict(
            results,
            stability,
            0.98,
            1,
            [0.01] * 300,
            perturb=perturb,
        )
        check_dict = {name: ok for name, ok, _ in checks}
        assert check_dict["param_robust"] is False

    def test_verdict_with_multi_seed_pass(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = self._make_base_results()
        stability = {"rsi": {"stable": True}}
        ms = MultiSeedResult(n_seeds=5, convergence_pct=0.80, seed_params=[])
        _, checks = wf._compute_verdict(
            results,
            stability,
            0.98,
            1,
            [0.01] * 300,
            multi_seed=ms,
        )
        check_dict = {name: ok for name, ok, _ in checks}
        assert "seed_convergence" in check_dict
        assert check_dict["seed_convergence"] is True

    def test_verdict_with_multi_seed_fail(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = self._make_base_results()
        stability = {"rsi": {"stable": True}}
        ms = MultiSeedResult(n_seeds=5, convergence_pct=0.40, seed_params=[])
        _, checks = wf._compute_verdict(
            results,
            stability,
            0.98,
            1,
            [0.01] * 300,
            multi_seed=ms,
        )
        check_dict = {name: ok for name, ok, _ in checks}
        assert check_dict["seed_convergence"] is False

    def test_verdict_no_phase3(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        results = self._make_base_results()
        stability = {"rsi": {"stable": True}}
        _, checks = wf._compute_verdict(
            results,
            stability,
            0.98,
            1,
            [0.01] * 300,
        )
        check_names = {name for name, _, _ in checks}
        assert "param_robust" not in check_names
        assert "seed_convergence" not in check_names


# ------------------------------------------------------------------
# Phase 3: Export JSON with Phase 3 fields
# ------------------------------------------------------------------


class TestExportPhase3:
    def test_export_with_phase3(self, walkforward_conf, tmp_path):
        wf = WalkForward(walkforward_conf)
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=w,
                train_metrics={"profit_pct": 10},
                test_metrics={"profit_pct": 5},
            )
        ]
        regime = RegimeAnalysis(
            regime_stats={
                "bull": {"windows": 3, "avg_profit": 8.0, "avg_dd": 4.0, "pct_profitable": 1.0}
            },
            worst_regime="bull",
            regime_dependent=False,
        )
        perturb = PerturbResult(
            n_perturbations=60,
            profit_p5=1.0,
            profit_p50=3.5,
            profit_p95=7.0,
            pct_profitable=0.85,
            sensitivity=0.9,
        )
        ms = MultiSeedResult(n_seeds=5, convergence_pct=0.80, seed_params=[])

        build_data = wf._build_results_data(
            results,
            {},
            {"buy": {"rsi": 30}},
            regime=regime,
            perturb=perturb,
            multi_seed=ms,
        )
        path = wf._export_results_json(build_data)
        with path.open("r") as f:
            data = rapidjson.load(f)

        assert data["regime_analysis"] is not None
        assert data["regime_analysis"]["worst_regime"] == "bull"
        assert data["perturbation"] is not None
        assert data["perturbation"]["n_perturbations"] == 60
        assert data["perturbation"]["pct_profitable"] == 0.85
        assert data["multi_seed"] is not None
        assert data["multi_seed"]["n_seeds"] == 5
        assert data["multi_seed"]["convergence_pct"] == 0.80

    def test_export_no_phase3(self, walkforward_conf, tmp_path):
        wf = WalkForward(walkforward_conf)
        w = WalkForwardWindow(
            index=0,
            train_start=datetime(2018, 1, 1, tzinfo=UTC),
            train_end=datetime(2018, 6, 1, tzinfo=UTC),
            test_start=datetime(2018, 6, 8, tzinfo=UTC),
            test_end=datetime(2018, 9, 1, tzinfo=UTC),
        )
        results = [
            WindowResult(
                window=w,
                train_metrics={"profit_pct": 10},
                test_metrics={"profit_pct": 5},
            )
        ]
        build_data = wf._build_results_data(results, {}, {"buy": {"rsi": 30}})
        path = wf._export_results_json(build_data)
        with path.open("r") as f:
            data = rapidjson.load(f)
        assert data["regime_analysis"] is None
        assert data["perturbation"] is None
        assert data["multi_seed"] is None


# ------------------------------------------------------------------
# Phase 3: Grade extraction
# ------------------------------------------------------------------


class TestGradeFromChecks:
    def test_all_pass(self):
        checks = [("profitable_windows", True, ""), ("dsr", True, ""), ("sqn", True, "")]
        assert WalkForward._grade_from_checks(checks) == "A"

    def test_critical_fail(self):
        checks = [
            ("profitable_windows", False, ""),
            ("dsr", True, ""),
            ("sqn", True, ""),
            ("wfe", True, ""),
        ]
        assert WalkForward._grade_from_checks(checks) == "D"

    def test_critical_fail_with_low_pass_rate(self):
        checks = [
            ("profitable_windows", False, ""),
            ("dsr", False, ""),
            ("sqn", False, ""),
            ("wfe", False, ""),
        ]
        assert WalkForward._grade_from_checks(checks) == "F"

    def test_most_pass(self):
        checks = [
            ("profitable_windows", True, ""),
            ("dsr", True, ""),
            ("sqn", True, ""),
            ("wfe", False, ""),
        ]
        assert WalkForward._grade_from_checks(checks) == "B"


# ------------------------------------------------------------------
# Phase 3: Log phase3
# ------------------------------------------------------------------


class TestLogPhase3:
    def test_log_regime(self, caplog):
        regime = RegimeAnalysis(
            regime_stats={
                "bull": {"windows": 3, "avg_profit": 8.0},
                "bear": {"windows": 2, "avg_profit": -2.0},
            },
            worst_regime="bear",
            regime_dependent=True,
        )
        WalkForward._log_phase3(regime, None, None)
        assert "Regime breakdown" in caplog.text

    def test_log_perturb(self, caplog):
        perturb = PerturbResult(
            n_perturbations=60,
            profit_p5=1.0,
            profit_p50=3.0,
            profit_p95=6.0,
            pct_profitable=0.85,
            sensitivity=0.9,
        )
        WalkForward._log_phase3(None, perturb, None)
        assert "Perturbation" in caplog.text
        assert "85%" in caplog.text

    def test_log_multi_seed(self, caplog):
        ms = MultiSeedResult(n_seeds=5, convergence_pct=0.80, seed_params=[])
        WalkForward._log_phase3(None, None, ms)
        assert "Multi-seed" in caplog.text
        assert "80%" in caplog.text

    def test_log_all_none(self, caplog):
        WalkForward._log_phase3(None, None, None)
        assert "Regime" not in caplog.text
        assert "Perturbation" not in caplog.text
        assert "Multi-seed" not in caplog.text


# ------------------------------------------------------------------
# Phase 4: CPCV
# ------------------------------------------------------------------


class TestCPCVGroups:
    def test_compute_groups_basic(self, walkforward_conf):
        walkforward_conf["wf_cpcv_groups"] = 6
        wf = WalkForward(walkforward_conf)
        groups = wf._compute_cpcv_groups()
        assert len(groups) == 6
        for start, end in groups:
            assert end > start

    def test_compute_groups_cover_full_range(self, walkforward_conf):
        walkforward_conf["wf_cpcv_groups"] = 4
        wf = WalkForward(walkforward_conf)
        groups = wf._compute_cpcv_groups()
        full_start = datetime(2018, 1, 1, tzinfo=UTC)
        full_end = datetime(2020, 1, 1, tzinfo=UTC)
        assert groups[0][0] == full_start
        assert groups[-1][1] == full_end

    def test_compute_groups_contiguous(self, walkforward_conf):
        walkforward_conf["wf_cpcv_groups"] = 5
        wf = WalkForward(walkforward_conf)
        groups = wf._compute_cpcv_groups()
        for i in range(len(groups) - 1):
            gap = abs((groups[i + 1][0] - groups[i][1]).total_seconds())
            assert gap < 86400 * 2


class TestCPCVCombinations:
    def test_basic_combinations(self):
        combos = WalkForward._compute_cpcv_combinations(6, 2)
        from math import comb

        assert len(combos) == comb(6, 2)

    def test_all_indices_present(self):
        combos = WalkForward._compute_cpcv_combinations(4, 1)
        for train_idx, test_idx in combos:
            all_idx = set(train_idx) | set(test_idx)
            assert all_idx == {0, 1, 2, 3}
            assert len(test_idx) == 1
            assert len(train_idx) == 3

    def test_train_test_disjoint(self):
        combos = WalkForward._compute_cpcv_combinations(6, 2)
        for train_idx, test_idx in combos:
            assert set(train_idx) & set(test_idx) == set()


class TestCPCVEmbargo:
    def test_embargo_shrinks_adjacent_train(self, walkforward_conf):
        walkforward_conf["wf_embargo_days"] = 7
        wf = WalkForward(walkforward_conf)
        groups = [
            (datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 2, 1, tzinfo=UTC)),
            (datetime(2024, 2, 1, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC)),
            (datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 4, 1, tzinfo=UTC)),
            (datetime(2024, 4, 1, tzinfo=UTC), datetime(2024, 5, 1, tzinfo=UTC)),
        ]
        train_idx = (0, 2, 3)
        test_idx = (1,)
        purged_train, test_ranges = wf._apply_cpcv_embargo(groups, train_idx, test_idx)
        assert len(test_ranges) == 1
        train_g0 = purged_train[0]
        assert train_g0[1] < groups[0][1]

    def test_embargo_no_adjacent(self, walkforward_conf):
        walkforward_conf["wf_embargo_days"] = 7
        wf = WalkForward(walkforward_conf)
        groups = [
            (datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 2, 1, tzinfo=UTC)),
            (datetime(2024, 2, 1, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC)),
            (datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 4, 1, tzinfo=UTC)),
            (datetime(2024, 4, 1, tzinfo=UTC), datetime(2024, 5, 1, tzinfo=UTC)),
        ]
        train_idx = (0, 1)
        test_idx = (2, 3)
        purged_train, test_ranges = wf._apply_cpcv_embargo(groups, train_idx, test_idx)
        assert len(test_ranges) == 2
        assert purged_train[0][0] == groups[0][0]
        assert purged_train[1][1] < groups[1][1]


class TestCPCVAggregate:
    def test_aggregate_basic(self):
        combo_results = [
            {"combo_idx": 0, "profit_pct": 5.0, "trades": 100, "max_dd_pct": 3.0},
            {"combo_idx": 1, "profit_pct": -2.0, "trades": 80, "max_dd_pct": 5.0},
            {"combo_idx": 2, "profit_pct": 8.0, "trades": 120, "max_dd_pct": 2.0},
        ]
        result = WalkForward._aggregate_cpcv(combo_results, 4, 1)
        assert isinstance(result, CPCVResult)
        assert result.n_combinations == 3
        assert result.n_groups == 4
        assert result.n_test_groups == 1
        assert len(result.path_returns) == 3
        assert result.prob_of_loss == pytest.approx(1 / 3, abs=0.01)

    def test_aggregate_empty(self):
        result = WalkForward._aggregate_cpcv([], 6, 2)
        assert result.n_combinations == 0
        assert result.path_returns == []

    def test_aggregate_all_positive(self):
        combo_results = [
            {"combo_idx": i, "profit_pct": 3.0 + i, "trades": 50, "max_dd_pct": 1.0}
            for i in range(5)
        ]
        result = WalkForward._aggregate_cpcv(combo_results, 5, 1)
        assert result.prob_of_loss == 0.0
        assert result.avg_return > 0

    def test_aggregate_sharpe_positive(self):
        combo_results = [
            {"combo_idx": i, "profit_pct": 10.0 + i, "trades": 50, "max_dd_pct": 1.0}
            for i in range(6)
        ]
        result = WalkForward._aggregate_cpcv(combo_results, 6, 2)
        assert result.sharpe_of_paths > 0


class TestCPCVValidation:
    def test_validate_too_few_groups(self, walkforward_conf):
        walkforward_conf["wf_cpcv_groups"] = 3
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match="must be >= 4"):
            wf._validate_cpcv()

    def test_validate_k_ge_n(self, walkforward_conf):
        walkforward_conf["wf_cpcv_groups"] = 4
        walkforward_conf["wf_cpcv_test_groups"] = 4
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match="must be <"):
            wf._validate_cpcv()

    def test_validate_k_zero(self, walkforward_conf):
        walkforward_conf["wf_cpcv_groups"] = 4
        walkforward_conf["wf_cpcv_test_groups"] = 0
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match="must be >= 1"):
            wf._validate_cpcv()

    def test_validate_too_many_combos(self, walkforward_conf):
        walkforward_conf["wf_cpcv_groups"] = 20
        walkforward_conf["wf_cpcv_test_groups"] = 10
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match="too many"):
            wf._validate_cpcv()

    def test_validate_short_groups(self, walkforward_conf):
        walkforward_conf["timerange"] = "20240101-20240210"
        walkforward_conf["wf_cpcv_groups"] = 10
        walkforward_conf["wf_cpcv_test_groups"] = 2
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match="too short"):
            wf._validate_cpcv()

    def test_validate_passes(self, walkforward_conf):
        walkforward_conf["wf_cpcv_groups"] = 6
        walkforward_conf["wf_cpcv_test_groups"] = 2
        wf = WalkForward(walkforward_conf)
        wf._validate_cpcv()


class TestCPCVWarnings:
    def test_high_prob_loss_warning(self):
        cpcv = CPCVResult(
            n_groups=6,
            n_test_groups=2,
            n_combinations=15,
            prob_of_loss=0.50,
            sharpe_of_paths=1.0,
            path_returns=[1.0, -2.0, 3.0],
        )
        warnings = WalkForward._cpcv_warnings(cpcv)
        assert any("50%" in w for w in warnings)

    def test_low_sharpe_warning(self):
        cpcv = CPCVResult(
            n_groups=6,
            n_test_groups=2,
            n_combinations=15,
            prob_of_loss=0.10,
            sharpe_of_paths=0.3,
            path_returns=[1.0, 2.0, 3.0],
        )
        warnings = WalkForward._cpcv_warnings(cpcv)
        assert any("Sharpe" in w for w in warnings)

    def test_clean_cpcv_no_warnings(self):
        cpcv = CPCVResult(
            n_groups=6,
            n_test_groups=2,
            n_combinations=15,
            prob_of_loss=0.10,
            sharpe_of_paths=1.5,
            path_returns=[5.0, 3.0, 7.0],
        )
        warnings = WalkForward._cpcv_warnings(cpcv)
        assert len(warnings) == 0


class TestCPCVVerdict:
    def test_cpcv_verdict_pass(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        cpcv = CPCVResult(
            n_groups=6,
            n_test_groups=2,
            n_combinations=15,
            prob_of_loss=0.10,
            sharpe_of_paths=1.2,
            path_returns=[5.0, 3.0, 7.0],
        )
        _, checks = wf._compute_verdict([], {}, None, 0, [], cpcv=cpcv)
        cpcv_checks = [c for c in checks if c[0] == "cpcv_prob_loss"]
        assert len(cpcv_checks) == 1
        assert cpcv_checks[0][1] is True

    def test_cpcv_verdict_fail(self, walkforward_conf):
        wf = WalkForward(walkforward_conf)
        cpcv = CPCVResult(
            n_groups=6,
            n_test_groups=2,
            n_combinations=15,
            prob_of_loss=0.50,
            sharpe_of_paths=0.3,
            path_returns=[1.0, -2.0, -3.0],
        )
        _, checks = wf._compute_verdict([], {}, None, 0, [], cpcv=cpcv)
        cpcv_checks = [c for c in checks if c[0] == "cpcv_prob_loss"]
        assert len(cpcv_checks) == 1
        assert cpcv_checks[0][1] is False


class TestLoadLatestConsensus:
    def test_load_from_consensus_file(self, walkforward_conf, tmp_path):
        wf = WalkForward(walkforward_conf)
        consensus_file = tmp_path / "walk_forward" / "HyperoptableStrategy_consensus_2024.json"
        consensus_file.parent.mkdir(parents=True, exist_ok=True)
        data = {"params": {"buy": {"rsi": 25}}, "strategy_name": "HyperoptableStrategy"}
        consensus_file.write_text(rapidjson.dumps(data))
        result = wf._load_latest_consensus()
        assert result == {"buy": {"rsi": 25}}

    def test_load_no_consensus_raises(self, walkforward_conf, tmp_path):
        walkforward_conf["strategy"] = "NoSuchStrategy"
        wf = WalkForward(walkforward_conf)
        with pytest.raises(OperationalException, match="No consensus params"):
            wf._load_latest_consensus()


class TestLogCPCV:
    def test_log_cpcv_basic(self, caplog):
        cpcv = CPCVResult(
            n_groups=6,
            n_test_groups=2,
            n_combinations=15,
            n_paths=5,
            avg_return=3.5,
            sharpe_of_paths=1.2,
            prob_of_loss=0.13,
            path_returns=[1.0, 2.0, 5.0, 7.0, 3.0],
        )
        WalkForward._log_cpcv(cpcv)
        assert "CPCV" in caplog.text
        assert "15 combos" in caplog.text
        assert "p5/p50/p95" in caplog.text

    def test_log_cpcv_none(self, caplog):
        WalkForward._log_cpcv(None)
        assert "CPCV" not in caplog.text


class TestExportCPCV:
    def test_export_with_cpcv(self, walkforward_conf, tmp_path):
        wf = WalkForward(walkforward_conf)
        results = [
            WindowResult(
                window=WalkForwardWindow(
                    0,
                    datetime(2018, 1, 1),
                    datetime(2018, 6, 1),
                    datetime(2018, 6, 8),
                    datetime(2018, 8, 1),
                ),
                train_metrics={"profit_pct": 10, "trades": 100, "calmar": 2.0},
                test_metrics={"profit_pct": 5, "trades": 50, "calmar": 1.0},
            )
        ]
        cpcv = CPCVResult(
            n_groups=6,
            n_test_groups=2,
            n_combinations=15,
            n_paths=5,
            avg_return=3.5,
            sharpe_of_paths=1.2,
            prob_of_loss=0.13,
            path_returns=[1.0, 2.0, 5.0, 7.0, 3.0],
        )
        data = wf._build_results_data(results, {}, {"buy": {"rsi": 30}}, cpcv=cpcv)
        assert data["cpcv"] is not None
        assert data["cpcv"]["n_groups"] == 6
        assert data["cpcv"]["prob_of_loss"] == 0.13
        path = wf._export_results_json(data)
        assert path.exists()
        content = rapidjson.loads(path.read_text())
        assert "cpcv" in content
        assert content["cpcv"]["n_combinations"] == 15


# ------------------------------------------------------------------
# Phase 4: HTML report
# ------------------------------------------------------------------


class TestHTMLReport:
    def _make_data(self, **overrides):
        data = {
            "strategy": "TestStrategy",
            "wf_mode": "rolling",
            "n_windows": 3,
            "epochs_per_window": 100,
            "hyperopt_loss": "CalmarHyperOptLoss",
            "timestamp": "2024-01-01T00:00:00",
            "verdict": {
                "grade": "B",
                "checks": [
                    ("profitable_windows", True, "3/3 profitable (100%)"),
                    ("wfe", False, "WFE median 40%"),
                    ("dsr", True, "DSR 0.970"),
                ],
            },
            "warnings": ["Test warning 1", "Test warning 2"],
            "oos_trade_profits": [10.0, -5.0, 15.0, -3.0, 8.0, 12.0, -2.0, 7.0],
            "windows": [
                {
                    "index": 1,
                    "test_range": "20180601-20180801",
                    "test_metrics": {
                        "profit_pct": 5.0,
                        "trades": 40,
                        "calmar": 1.5,
                        "max_dd_pct": 3.0,
                    },
                    "wfe": 0.65,
                    "market_context": {"regime": "bull"},
                }
            ],
            "param_stability": {
                "rsi": {
                    "values": [25, 30, 28],
                    "median": 28,
                    "std": 2.5,
                    "std_over_range": 0.05,
                    "stable": True,
                    "unstable": False,
                },
            },
            "consensus_params": {"buy": {"rsi": 28}},
            "monte_carlo": None,
            "regime_analysis": None,
            "perturbation": None,
            "multi_seed": None,
            "cpcv": None,
        }
        data.update(overrides)
        return data

    def test_generate_basic(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        result = generate_wfa_html_report(data, out)
        assert result == out
        assert out.exists()
        content = out.read_text()
        assert "<!DOCTYPE html>" in content
        assert "</html>" in content

    def test_contains_verdict(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "Verdict" in content
        assert "grade" in content.lower() or "B" in content

    def test_contains_windows_table(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "Windows" in content
        assert "20180601" in content

    def test_equity_chart_svg(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "<svg" in content
        assert "polyline" in content

    def test_no_equity_with_empty_profits(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data(oos_trade_profits=[])
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "<svg" not in content

    def test_self_contained_no_external(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        stripped = content.replace("http://www.w3.org/2000/svg", "")
        assert "http://" not in stripped
        assert "https://" not in stripped
        assert "<script" not in content

    def test_html_escaping(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data(strategy="<script>alert('xss')</script>")
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "<script>alert" not in content
        assert "&lt;script&gt;" in content

    def test_with_monte_carlo(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        mc = {
            "n_simulations": 1000,
            "total_return_pct": 15.0,
            "max_dd_p5": 2.0,
            "max_dd_p50": 5.0,
            "max_dd_p95": 12.0,
            "return_dd_p5": 1.2,
            "return_dd_p50": 3.0,
            "return_dd_p95": 7.0,
            "max_consec_loss_p50": 4,
            "max_consec_loss_p95": 8,
            "carver_discount": 0.40,
        }
        data = self._make_data(monte_carlo=mc)
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "Monte Carlo" in content
        assert "1000 sims" in content

    def test_with_cpcv(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        cpcv_data = {
            "n_groups": 6,
            "n_test_groups": 2,
            "n_combinations": 15,
            "n_paths": 5,
            "avg_return": 3.5,
            "sharpe_of_paths": 1.2,
            "prob_of_loss": 0.13,
            "path_returns": [1.0, 2.0, 5.0, 7.0, 3.0],
        }
        data = self._make_data(cpcv=cpcv_data)
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "CPCV" in content
        assert "N=6" in content
        assert "P(loss)" in content

    def test_with_phase3(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        regime = {
            "regime_stats": {
                "bull": {"windows": 3, "avg_profit": 5.0, "avg_dd": 2.0},
                "bear": {"windows": 2, "avg_profit": -1.0, "avg_dd": 4.0},
            },
            "worst_regime": "bear",
            "regime_dependent": True,
        }
        perturb = {
            "n_perturbations": 60,
            "profit_p5": 1.0,
            "profit_p50": 3.0,
            "profit_p95": 6.0,
            "pct_profitable": 0.85,
            "sensitivity": 0.9,
        }
        ms = {"n_seeds": 5, "convergence_pct": 0.80}
        data = self._make_data(regime_analysis=regime, perturbation=perturb, multi_seed=ms)
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "Robustness" in content
        assert "Regime" in content
        assert "Perturbation" in content
        assert "Multi-Seed" in content

    def test_warnings_section(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data(warnings=["Watch out!", "Be careful!"])
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "Watch out!" in content
        assert "Be careful!" in content

    def test_no_warnings_section_when_empty(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data(warnings=[])
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "Warnings" not in content

    def test_consensus_section(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data(consensus_params={"buy": {"rsi": 28, "volume_pct": 0.3}})
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "Consensus" in content
        assert "rsi" in content
        assert "28" in content

    def test_param_stability_badges(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        stability = {
            "rsi": {
                "values": [25, 30, 28],
                "median": 28,
                "std": 2.5,
                "std_over_range": 0.05,
                "stable": True,
                "unstable": False,
            },
            "volume": {
                "values": [0.1, 0.9, 0.5],
                "median": 0.5,
                "std": 0.4,
                "std_over_range": 0.50,
                "stable": False,
                "unstable": True,
            },
        }
        data = self._make_data(param_stability=stability)
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "stable" in content
        assert "unstable" in content


class TestExportHTMLReport:
    def test_export_html_report_method(self, walkforward_conf, tmp_path):
        wf = WalkForward(walkforward_conf)
        results = [
            WindowResult(
                window=WalkForwardWindow(
                    0,
                    datetime(2018, 1, 1),
                    datetime(2018, 6, 1),
                    datetime(2018, 6, 8),
                    datetime(2018, 8, 1),
                ),
                train_metrics={"profit_pct": 10, "trades": 100, "calmar": 2.0},
                test_metrics={"profit_pct": 5, "trades": 50, "calmar": 1.0},
            )
        ]
        data = wf._build_results_data(results, {}, {"buy": {"rsi": 30}})
        path = wf._export_html_report(data)
        assert path is not None
        assert path.exists()
        assert path.suffix == ".html"
        content = path.read_text()
        assert "<!DOCTYPE html>" in content


# ------------------------------------------------------------------
# Block 7 — Beginner-friendly overhaul tests
# ------------------------------------------------------------------


class TestGlossary:
    def test_glossary_completeness(self):
        from freqtrade.optimize.wfa_glossary import METRIC_GLOSSARY

        expected_slugs = {
            "wfe",
            "sqn",
            "dsr",
            "calmar",
            "dd",
            "hhi",
            "pf",
            "mc",
            "carver_discount",
            "k_ratio",
            "expectancy",
            "embargo",
            "cpcv",
            "sensitivity",
            "convergence",
            "prob_of_loss",
            "sharpe_of_paths",
            "sharpe",
            "sortino",
            "win_rate",
            "payoff_ratio",
            "cagr",
            "skewness",
            "kurtosis",
            "profit_concentration",
            "expected_max_sharpe",
        }
        assert expected_slugs == set(METRIC_GLOSSARY.keys())

    def test_glossary_structure(self):
        from freqtrade.optimize.wfa_glossary import METRIC_GLOSSARY

        required_keys = {"name", "abbrev", "one_liner", "explanation", "thresholds", "source"}
        for slug, entry in METRIC_GLOSSARY.items():
            missing = required_keys - set(entry.keys())
            assert not missing, f"{slug} missing keys: {missing}"
            assert isinstance(entry["thresholds"], list)
            assert isinstance(entry["name"], str)
            assert len(entry["one_liner"]) > 0

    def test_verdict_guide_grades(self):
        from freqtrade.optimize.wfa_glossary import VERDICT_GUIDE

        assert set(VERDICT_GUIDE.keys()) == {"A", "B", "C", "D", "F"}
        for grade, text in VERDICT_GUIDE.items():
            assert len(text) > 20, f"Grade {grade} guide too short"


class TestBeginnerConsole:
    def test_plan_includes_intro(self, walkforward_conf, caplog):
        wf = WalkForward(walkforward_conf)
        windows = wf._compute_windows()
        with caplog.at_level("INFO"):
            wf._print_plan(windows)
        low = caplog.text.lower()
        assert "optimize on past data" in low or "train on past" in low
        assert "embargo" in caplog.text.lower()

    def test_cpcv_log_spells_out_acronym(self, caplog):
        cpcv = CPCVResult(
            n_groups=6,
            n_test_groups=2,
            n_combinations=15,
            n_paths=5,
            avg_return=3.5,
            sharpe_of_paths=1.2,
            prob_of_loss=0.13,
            path_returns=[1.0, 2.0, 5.0, 7.0, 3.0],
        )
        with caplog.at_level("INFO"):
            WalkForward._log_cpcv(cpcv)
        assert "Combinatorial Purged" in caplog.text

    def test_report_includes_next_steps(self, walkforward_conf, caplog):
        wf = WalkForward(walkforward_conf)
        with caplog.at_level("INFO"):
            wf._log_next_steps("A")
        assert "NEXT STEPS" in caplog.text
        assert "consensus" in caplog.text.lower()

    def test_threshold_label_returns_label(self):
        label = WalkForward._threshold_label("sqn", 2.0)
        assert "good" in label.lower()

    def test_threshold_label_unknown_slug(self):
        label = WalkForward._threshold_label("nonexistent", 1.0)
        assert label == "" or label == "N/A"


class TestBeginnerHTML:
    def _make_data(self, **overrides):
        data = {
            "strategy": "TestStrategy",
            "wf_mode": "rolling",
            "n_windows": 3,
            "epochs_per_window": 100,
            "hyperopt_loss": "CalmarHyperOptLoss",
            "timestamp": "2024-01-01T00:00:00",
            "verdict": {
                "grade": "B",
                "checks": [
                    ("profitable_windows", True, "3/3 profitable"),
                    ("wfe", False, "WFE median 40%"),
                    ("dsr", True, "DSR 0.970"),
                ],
            },
            "warnings": ["Test warning"],
            "oos_trade_profits": [10.0, -5.0, 15.0, -3.0, 8.0],
            "windows": [
                {
                    "index": 1,
                    "test_range": "20180601-20180801",
                    "test_metrics": {
                        "profit_pct": 5.0,
                        "trades": 40,
                        "calmar": 1.5,
                        "max_dd_pct": 3.0,
                    },
                    "wfe": 0.65,
                    "market_context": {"regime": "bull"},
                }
            ],
            "param_stability": {
                "rsi": {
                    "values": [25, 30, 28],
                    "median": 28,
                    "std": 2.5,
                    "std_over_range": 0.05,
                    "stable": True,
                    "unstable": False,
                },
            },
            "consensus_params": {"buy": {"rsi": 28}},
            "monte_carlo": {
                "n_simulations": 1000,
                "total_return_pct": 15.0,
                "max_dd_p5": 2.0,
                "max_dd_p50": 5.0,
                "max_dd_p95": 12.0,
                "return_dd_p5": 1.2,
                "return_dd_p50": 3.0,
                "return_dd_p95": 7.0,
                "max_consec_loss_p50": 4,
                "max_consec_loss_p95": 8,
                "carver_discount": 0.40,
            },
            "regime_analysis": {
                "regime_stats": {
                    "bull": {"windows": 2, "avg_profit": 5.0, "avg_dd": 2.0},
                },
                "worst_regime": "bull",
                "regime_dependent": False,
            },
            "perturbation": {
                "n_perturbations": 60,
                "profit_p5": 1.0,
                "profit_p50": 3.0,
                "profit_p95": 6.0,
                "pct_profitable": 0.85,
                "sensitivity": 0.9,
            },
            "multi_seed": {"n_seeds": 5, "convergence_pct": 0.80},
            "cpcv": {
                "n_groups": 6,
                "n_test_groups": 2,
                "n_combinations": 15,
                "n_paths": 5,
                "avg_return": 3.5,
                "sharpe_of_paths": 1.2,
                "prob_of_loss": 0.13,
                "path_returns": [1.0, 2.0, 5.0, 7.0, 3.0],
            },
        }
        data.update(overrides)
        return data

    def test_contains_intro_section(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "walk-forward" in content.lower() or "Walk-Forward" in content

    def test_contains_tooltips(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert 'class="tooltip"' in content

    def test_contains_next_steps(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "What To Do Next" in content or "Next Steps" in content or "NEXT" in content

    def test_contains_glossary(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "Glossary" in content

    def test_equity_chart_labels(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "Trade" in content
        assert "Equity" in content

    def test_section_explainers(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "<details" in content

    def test_threshold_badges_present(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_wfa_html_report(data, out)
        content = out.read_text()
        assert "badge" in content.lower() or "color:" in content
