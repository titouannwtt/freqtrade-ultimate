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
        path = wf._export_results_json(results, {}, {"buy": {"rsi": 30}})
        assert path.exists()

        with path.open("r") as f:
            data = rapidjson.load(f)
        assert data["strategy"] == "HyperoptableStrategy"
        assert len(data["windows"]) == 1
        assert data["consensus_params"]["buy"]["rsi"] == 30

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
