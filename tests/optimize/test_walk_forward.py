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
                test_metrics={"trades": 100},
                test_trade_count=100,
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
        path = wf._export_consensus_json(consensus, None)
        assert path.exists()

        with path.open("r") as f:
            data = rapidjson.load(f)
        assert data["strategy_name"] == "HyperoptableStrategy"
        assert data["params"]["buy"]["rsi"] == 35

    def test_export_consensus_to_strategy_json(self, walkforward_conf, tmp_path):
        strategy_json = tmp_path / "HyperoptableStrategy.json"
        wf = WalkForward(walkforward_conf)
        consensus = {"buy": {"rsi": 35}}
        wf._export_consensus_json(consensus, strategy_json)

        assert strategy_json.exists()
        with strategy_json.open("r") as f:
            data = rapidjson.load(f)
        assert data["params"]["buy"]["rsi"] == 35

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

        assert log_has_re(r".*Walk-Forward Analysis Results.*", caplog)
        assert log_has_re(r".*PARAMETER STABILITY.*", caplog)
        assert log_has_re(r".*CONSENSUS PARAMS.*", caplog)
        assert log_has_re(r".*WARNING FLAGS.*", caplog)
        assert log_has_re(r".*test warning.*", caplog)


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
