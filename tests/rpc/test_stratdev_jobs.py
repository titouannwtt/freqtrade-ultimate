"""Tests for the strategy dev job manager (subprocess-based backtest/hyperopt/WFA launcher)."""
import os
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from freqtrade.rpc.api_server.api_stratdev_jobs import (
    ConfigListResponse,
    JobStartRequest,
    JobStatus,
    JobStatusResponse,
    JobType,
    _JobManager,
    _JobState,
)


@contextmanager
def mock_freqtrade_binary():
    """Patch venv binary resolution so _build_command falls through to shutil.which."""
    original_is_file = Path.is_file

    def patched_is_file(self):
        if self.name == "freqtrade" and "bin" in str(self):
            return False
        return original_is_file(self)

    with patch.object(Path, "is_file", patched_is_file), \
         patch("shutil.which", return_value="/usr/bin/freqtrade"):
        yield


@pytest.fixture
def job_manager():
    return _JobManager()


@pytest.fixture
def mock_config(tmp_path):
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    strat_dir = user_data / "strategies"
    strat_dir.mkdir()
    (strat_dir / "TestStrategy.py").write_text("class TestStrategy: pass")
    (strat_dir / "AnotherStrat.py").write_text("class AnotherStrat: pass")
    (strat_dir / "_private.py").write_text("# hidden")
    (strat_dir / "__init__.py").write_text("")

    config_dir = tmp_path / "backtest_configs"
    config_dir.mkdir()
    (config_dir / "test_config.json").write_text('{"exchange": {}}')
    (config_dir / "another.json").write_text('{"exchange": {}}')
    (config_dir / ".hidden.json").write_text("{}")

    return {"user_data_dir": str(user_data)}


def make_request(**overrides) -> JobStartRequest:
    defaults = {
        "job_type": JobType.backtest,
        "config_file": "backtest_configs/test_config.json",
        "strategy": "TestStrategy",
    }
    defaults.update(overrides)
    return JobStartRequest(**defaults)


class TestJobStartRequest:
    def test_valid_backtest_request(self):
        req = make_request()
        assert req.job_type == JobType.backtest
        assert req.strategy == "TestStrategy"

    def test_valid_hyperopt_request(self):
        req = make_request(
            job_type=JobType.hyperopt,
            epochs=500,
            hyperopt_loss="CalmarHyperOptLoss",
            spaces=["default"],
        )
        assert req.epochs == 500

    def test_valid_wfa_request(self):
        req = make_request(
            job_type=JobType.wfa,
            epochs=500,
            wf_windows=5,
            wf_train_ratio=0.75,
            wf_mode="rolling",
        )
        assert req.wf_mode == "rolling"

    def test_rejects_command_injection_in_strategy(self):
        with pytest.raises(ValidationError, match="Invalid name"):
            make_request(strategy="TestStrategy; rm -rf /")

    def test_rejects_command_injection_in_config(self):
        with pytest.raises(ValidationError, match="Invalid config path"):
            make_request(config_file="../../../etc/passwd")

    def test_rejects_invalid_timerange(self):
        with pytest.raises(ValidationError, match="Invalid timerange"):
            make_request(timerange="2025-01-01 to 2025-04-01")

    def test_valid_timerange(self):
        req = make_request(timerange="20250101-20250401")
        assert req.timerange == "20250101-20250401"

    def test_rejects_invalid_spaces(self):
        with pytest.raises(ValidationError, match="Invalid space"):
            make_request(spaces=["buy", "hacked"])

    def test_rejects_invalid_sampler(self):
        with pytest.raises(ValidationError, match="Invalid sampler"):
            make_request(sampler="MaliciousSampler")

    def test_rejects_invalid_wf_mode(self):
        with pytest.raises(ValidationError, match="Invalid wf_mode"):
            make_request(wf_mode="hacked")

    def test_valid_spaces(self):
        req = make_request(spaces=["buy", "sell", "roi", "stoploss"])
        assert req.spaces == ["buy", "sell", "roi", "stoploss"]

    def test_all_samplers_accepted(self):
        for sampler in ["NSGAIIISampler", "TPESampler", "CmaEsSampler", "GPSampler", "QMCSampler"]:
            req = make_request(sampler=sampler)
            assert req.sampler == sampler

    def test_rejects_invalid_data_format(self):
        with pytest.raises(ValidationError, match="Invalid data format"):
            make_request(data_format_ohlcv="hdf5")

    def test_valid_data_formats(self):
        for fmt in ["json", "jsongz", "feather", "parquet"]:
            req = make_request(data_format_ohlcv=fmt)
            assert req.data_format_ohlcv == fmt

    def test_rejects_invalid_export_type(self):
        with pytest.raises(ValidationError, match="Invalid export type"):
            make_request(export_type="csv")

    def test_valid_export_types(self):
        for et in ["none", "trades", "signals"]:
            req = make_request(export_type=et)
            assert req.export_type == et

    def test_rejects_invalid_breakdown(self):
        with pytest.raises(ValidationError, match="Invalid breakdown"):
            make_request(breakdown=["day", "hour"])

    def test_valid_breakdown(self):
        req = make_request(breakdown=["day", "week", "month"])
        assert req.breakdown == ["day", "week", "month"]

    def test_rejects_invalid_cache(self):
        with pytest.raises(ValidationError, match="Invalid cache"):
            make_request(cache="forever")

    def test_valid_cache(self):
        for c in ["none", "day", "week", "month"]:
            req = make_request(cache=c)
            assert req.cache == c

    def test_new_common_fields(self):
        req = make_request(
            fee=0.001,
            pairs=["BTC/USDT", "ETH/USDT"],
            enable_position_stacking=True,
            timeframe_detail="5m",
        )
        assert req.fee == 0.001
        assert req.pairs == ["BTC/USDT", "ETH/USDT"]
        assert req.enable_position_stacking is True
        assert req.timeframe_detail == "5m"

    def test_new_hyperopt_flags(self):
        req = make_request(
            print_all=True,
            disable_param_export=True,
            ignore_missing_spaces=True,
            analyze_per_epoch=True,
        )
        assert req.print_all is True
        assert req.disable_param_export is True

    def test_new_wfa_fields(self):
        req = make_request(
            wf_multi_seed=3,
            wf_cpcv_groups=8,
            wf_cpcv_test_groups=3,
        )
        assert req.wf_multi_seed == 3
        assert req.wf_cpcv_groups == 8
        assert req.wf_cpcv_test_groups == 3


class TestJobState:
    def test_to_response(self):
        state = _JobState(
            job_id="test123",
            job_type=JobType.backtest,
            strategy="TestStrategy",
            config_file="config.json",
            command=["freqtrade", "backtest"],
        )
        resp = state.to_response()
        assert resp.job_id == "test123"
        assert resp.job_type == "backtest"
        assert resp.log_lines == []

    def test_to_response_with_logs(self):
        state = _JobState(
            job_id="test123",
            job_type=JobType.hyperopt,
            strategy="TestStrategy",
            config_file="config.json",
            command=["freqtrade", "hyperopt"],
        )
        state.log_buffer.append("line 1")
        state.log_buffer.append("line 2")
        resp = state.to_response(include_logs=True)
        assert resp.log_lines == ["line 1", "line 2"]


class TestJobManager:
    def test_no_active_job_initially(self, job_manager):
        assert job_manager.active_job is None

    def test_list_empty(self, job_manager):
        result = job_manager.list_all()
        assert result.active is None
        assert result.history == []

    def test_start_job_validates_config_exists(self, job_manager, mock_config):
        req = make_request(config_file="nonexistent.json")
        with pytest.raises(Exception, match="Config file not found"):
            job_manager.start_job(req, mock_config)

    def test_start_job_validates_strategy_exists(self, job_manager, mock_config):
        req = make_request(strategy="NonExistentStrategy")
        with pytest.raises(Exception, match="Strategy not found"):
            job_manager.start_job(req, mock_config)

    def test_concurrent_job_blocked(self, job_manager, mock_config):
        import threading

        block_event = threading.Event()

        def blocking_iter():
            block_event.wait(timeout=5)
            return
            yield  # make it a generator

        with mock_freqtrade_binary(), \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.poll.return_value = None
            mock_proc.stdout = blocking_iter()
            mock_proc.wait.side_effect = lambda timeout=None: block_event.wait(timeout=timeout or 5) or 0
            mock_popen.return_value = mock_proc

            req = make_request()
            job_manager.start_job(req, mock_config)

            req2 = make_request(strategy="AnotherStrat")
            with pytest.raises(Exception, match="already running"):
                job_manager.start_job(req2, mock_config)

            block_event.set()

    def test_start_backtest_builds_correct_command(self, job_manager, mock_config):
        with mock_freqtrade_binary(), \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 1
            mock_proc.poll.return_value = None
            mock_proc.stdout = iter([])
            mock_popen.return_value = mock_proc

            req = make_request(timerange="20250101-20250401", timeframe="15m")
            job = job_manager.start_job(req, mock_config)

            cmd = job.command
            assert cmd[0] == "/usr/bin/freqtrade"
            assert cmd[1] == "backtesting"
            assert "-s" in cmd
            assert cmd[cmd.index("-s") + 1] == "TestStrategy"
            assert "--timerange" in cmd
            assert cmd[cmd.index("--timerange") + 1] == "20250101-20250401"
            assert "--timeframe" in cmd
            assert "--export" in cmd
            assert "trades" in cmd

    def test_start_hyperopt_builds_correct_command(self, job_manager, mock_config):
        with mock_freqtrade_binary(), \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 2
            mock_proc.poll.return_value = None
            mock_proc.stdout = iter([])
            mock_popen.return_value = mock_proc

            req = make_request(
                job_type=JobType.hyperopt,
                epochs=300,
                hyperopt_loss="CalmarHyperOptLoss",
                spaces=["buy", "sell"],
                sampler="TPESampler",
                job_workers=-2,
            )
            job = job_manager.start_job(req, mock_config)

            cmd = job.command
            assert cmd[1] == "hyperopt"
            assert "--epochs" in cmd
            assert cmd[cmd.index("--epochs") + 1] == "300"
            assert "--hyperopt-loss" in cmd
            assert cmd[cmd.index("--hyperopt-loss") + 1] == "CalmarHyperOptLoss"
            assert "--spaces" in cmd
            space_idx = cmd.index("--spaces")
            assert cmd[space_idx + 1] == "buy"
            assert cmd[space_idx + 2] == "sell"
            assert "--sampler" in cmd
            assert "-j" in cmd
            assert cmd[cmd.index("-j") + 1] == "-2"

    def test_start_wfa_builds_correct_command(self, job_manager, mock_config):
        with mock_freqtrade_binary(), \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 3
            mock_proc.poll.return_value = None
            mock_proc.stdout = iter([])
            mock_popen.return_value = mock_proc

            req = make_request(
                job_type=JobType.wfa,
                epochs=500,
                wf_windows=5,
                wf_train_ratio=0.75,
                wf_embargo_days=7,
                wf_mode="rolling",
            )
            job = job_manager.start_job(req, mock_config)

            cmd = job.command
            assert cmd[1] == "walk-forward"
            assert "--wf-windows" in cmd
            assert cmd[cmd.index("--wf-windows") + 1] == "5"
            assert "--wf-train-ratio" in cmd
            assert "--wf-embargo-days" in cmd
            assert "--wf-mode" in cmd
            assert cmd[cmd.index("--wf-mode") + 1] == "rolling"

    def test_abort_job(self, job_manager, mock_config):
        import threading
        block_event = threading.Event()

        def blocking_iter():
            block_event.wait(timeout=5)
            return
            yield

        with mock_freqtrade_binary(), \
             patch("subprocess.Popen") as mock_popen, \
             patch("os.killpg") as mock_killpg, \
             patch("os.getpgid", return_value=100):
            mock_proc = MagicMock()
            mock_proc.pid = 100
            mock_proc.poll.return_value = None
            mock_proc.stdout = blocking_iter()
            mock_proc.wait.side_effect = lambda timeout=None: (block_event.wait(timeout=timeout or 5), -15)[-1]
            mock_popen.return_value = mock_proc

            req = make_request()
            job = job_manager.start_job(req, mock_config)
            job_id = job.job_id

            result = job_manager.abort_job(job_id)
            assert result.status == JobStatus.aborted
            assert mock_killpg.called
            block_event.set()

    def test_abort_nonexistent_job(self, job_manager):
        with pytest.raises(Exception, match="not found"):
            job_manager.abort_job("nonexistent")

    def test_finalize_on_success(self, job_manager, mock_config):
        with mock_freqtrade_binary(), \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 5
            mock_proc.poll.return_value = 0
            mock_proc.stdout = iter(["Result for strategy TestStrategy\n", "TOTAL  42 trades\n"])
            mock_proc.wait.return_value = 0
            mock_popen.return_value = mock_proc

            req = make_request()
            job = job_manager.start_job(req, mock_config)

            time.sleep(0.1)
            active = job_manager.get_active()
            assert active is not None
            assert active.status == JobStatus.completed
            assert active.exit_code == 0

    def test_finalize_on_failure(self, job_manager, mock_config):
        with mock_freqtrade_binary(), \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 6
            mock_proc.poll.return_value = 1
            mock_proc.stdout = iter(["Error: something broke\n"])
            mock_proc.wait.return_value = 1
            mock_popen.return_value = mock_proc

            req = make_request()
            job = job_manager.start_job(req, mock_config)

            time.sleep(0.1)
            active = job_manager.get_active()
            assert active is not None
            assert active.status == JobStatus.failed
            assert active.exit_code == 1

    def test_get_job_by_id(self, job_manager, mock_config):
        with mock_freqtrade_binary(), \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 7
            mock_proc.poll.return_value = 0
            mock_proc.stdout = iter([])
            mock_proc.wait.return_value = 0
            mock_popen.return_value = mock_proc

            req = make_request()
            job = job_manager.start_job(req, mock_config)

            found = job_manager.get_job(job.job_id)
            assert found is not None
            assert found.job_id == job.job_id

    def test_get_nonexistent_job(self, job_manager):
        assert job_manager.get_job("doesnt_exist") is None

    def test_disk_space_check(self, job_manager, mock_config):
        with patch("shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=500_000_000)
            req = make_request()
            with pytest.raises(Exception, match="Insufficient disk space"):
                job_manager.start_job(req, mock_config)

    def test_freqtrade_binary_not_found(self, job_manager, mock_config):
        original_is_file = Path.is_file

        def patched_is_file(self):
            if self.name == "freqtrade" and "bin" in str(self):
                return False
            return original_is_file(self)

        with patch.object(Path, "is_file", patched_is_file), \
             patch("shutil.which", return_value=None):
            req = make_request()
            with pytest.raises(Exception, match="freqtrade binary not found"):
                job_manager.start_job(req, mock_config)

    def test_hyperopt_loss_injection_blocked(self, job_manager, mock_config):
        """Loss names with special chars are caught by _SAFE_NAME_RE in _build_command."""
        with mock_freqtrade_binary():
            req = make_request(
                job_type=JobType.hyperopt,
                hyperopt_loss="CalmarHyperOptLoss; rm -rf /",
                epochs=500,
            )
            with pytest.raises(Exception, match="Invalid loss name"):
                job_manager.start_job(req, mock_config)
