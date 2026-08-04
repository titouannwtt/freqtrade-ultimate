"""
Strategy Dev Job Manager — subprocess-based backtest/hyperopt/WFA launcher.

Runs optimization jobs as subprocesses so they work alongside trading-mode bots.
Results land in the standard backtest_results/hyperopt_results/walk_forward_results
directories, which the existing stratdev reader already scans.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from freqtrade.rpc.api_server.deps import get_config


logger = logging.getLogger(__name__)
router = APIRouter()

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_\-\./]+$")
_SAFE_TIMERANGE_RE = re.compile(r"^(\d{8})-(\d{8})?$|^(\d{8})-$|^-(\d{8})$")
_MAX_LOG_LINES = 500
_JOB_HISTORY_LIMIT = 20

_CONFIG_SEARCH_DIRS = [
    "backtest_configs",
    "custom_configs",
    "config_examples",
    "live_configs",
    "user_data",
]


class JobType(str, Enum):
    backtest = "backtest"
    hyperopt = "hyperopt"
    wfa = "walk-forward"


_JOBTYPE_TO_CLI = {
    JobType.backtest: "backtesting",
    JobType.hyperopt: "hyperopt",
    JobType.wfa: "walk-forward",
}


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    aborted = "aborted"


class JobStartRequest(BaseModel):
    job_type: JobType
    config_file: str
    strategy: str
    timerange: str | None = None
    timeframe: str | None = None
    max_open_trades: int | None = None
    stake_amount: str | float | None = None
    enable_protections: bool = False
    dry_run_wallet: float | None = None

    # Common advanced
    data_format_ohlcv: str | None = None
    fee: float | None = None
    pairs: list[str] | None = None
    enable_position_stacking: bool = False
    timeframe_detail: str | None = None

    # Backtest-only
    export_type: str | None = None
    breakdown: list[str] | None = None
    cache: str | None = None
    enable_dynamic_pairlist: bool = False
    notes: str | None = None

    # Hyperopt / WFA
    epochs: int | None = None
    spaces: list[str] | None = None
    hyperopt_loss: str | None = None
    sampler: str | None = None
    early_stop: int | None = None
    min_trades: int | None = None
    job_workers: int | None = None
    random_state: int | None = None

    # Hyperopt-only flags
    print_all: bool = False
    disable_param_export: bool = False
    ignore_missing_spaces: bool = False
    analyze_per_epoch: bool = False

    # WFA only
    wf_windows: int | None = None
    wf_train_ratio: float | None = None
    wf_embargo_days: int | None = None
    wf_holdout_months: int | None = None
    wf_min_test_trades: int | None = None
    wf_mode: str | None = None
    wf_multi_seed: int | None = None
    wf_cpcv_groups: int | None = None
    wf_cpcv_test_groups: int | None = None

    @field_validator("strategy")
    @classmethod
    def validate_safe_name(cls, v: str) -> str:
        if not _SAFE_NAME_RE.match(v):
            raise ValueError(f"Invalid name: {v!r}. Only alphanumeric, _, -, . allowed.")
        return v

    @field_validator("config_file")
    @classmethod
    def validate_safe_path(cls, v: str) -> str:
        if not _SAFE_PATH_RE.match(v) or ".." in v:
            raise ValueError(f"Invalid config path: {v!r}")
        return v

    @field_validator("timerange")
    @classmethod
    def validate_timerange(cls, v: str | None) -> str | None:
        if v is not None and not _SAFE_TIMERANGE_RE.match(v):
            raise ValueError(f"Invalid timerange: {v!r}. Expected format: YYYYMMDD-YYYYMMDD")
        return v

    @field_validator("spaces")
    @classmethod
    def validate_spaces(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            valid = {
                "default",
                "all",
                "buy",
                "sell",
                "enter",
                "exit",
                "roi",
                "stoploss",
                "trailing",
                "protection",
                "trades",
            }
            for s in v:
                if s not in valid:
                    raise ValueError(f"Invalid space: {s!r}")
        return v

    @field_validator("sampler")
    @classmethod
    def validate_sampler(cls, v: str | None) -> str | None:
        if v is not None:
            valid = {
                "NSGAIIISampler",
                "NSGAIISampler",
                "TPESampler",
                "CmaEsSampler",
                "GPSampler",
                "QMCSampler",
            }
            if v not in valid:
                raise ValueError(f"Invalid sampler: {v!r}")
        return v

    @field_validator("wf_mode")
    @classmethod
    def validate_wf_mode(cls, v: str | None) -> str | None:
        if v is not None and v not in ("rolling", "anchored", "cpcv"):
            raise ValueError(f"Invalid wf_mode: {v!r}")
        return v

    @field_validator("data_format_ohlcv")
    @classmethod
    def validate_data_format(cls, v: str | None) -> str | None:
        if v is not None and v not in ("json", "jsongz", "feather", "parquet"):
            raise ValueError(f"Invalid data format: {v!r}")
        return v

    @field_validator("export_type")
    @classmethod
    def validate_export_type(cls, v: str | None) -> str | None:
        if v is not None and v not in ("none", "trades", "signals"):
            raise ValueError(f"Invalid export type: {v!r}")
        return v

    @field_validator("breakdown")
    @classmethod
    def validate_breakdown(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            valid = {"day", "week", "month", "year", "weekday"}
            for b in v:
                if b not in valid:
                    raise ValueError(f"Invalid breakdown: {b!r}")
        return v

    @field_validator("cache")
    @classmethod
    def validate_cache(cls, v: str | None) -> str | None:
        if v is not None and v not in ("none", "day", "week", "month"):
            raise ValueError(f"Invalid cache: {v!r}")
        return v


class JobStatusResponse(BaseModel):
    job_id: str
    job_type: str
    strategy: str
    config_file: str
    status: str
    pid: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    error: str | None = None
    log_lines: list[str] = []
    command: str = ""


class JobListResponse(BaseModel):
    active: JobStatusResponse | None = None
    history: list[JobStatusResponse] = []


class ConfigListResponse(BaseModel):
    configs: list[str]
    strategies: list[str]


class _JobState:
    def __init__(
        self,
        job_id: str,
        job_type: JobType,
        strategy: str,
        config_file: str,
        command: list[str],
    ):
        self.job_id = job_id
        self.job_type = job_type
        self.strategy = strategy
        self.config_file = config_file
        self.command = command
        self.status = JobStatus.pending
        self.pid: int | None = None
        self.process: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.exit_code: int | None = None
        self.error: str | None = None
        self.log_buffer: deque[str] = deque(maxlen=_MAX_LOG_LINES)
        self._reader_thread: threading.Thread | None = None

    def to_response(self, include_logs: bool = False) -> JobStatusResponse:
        return JobStatusResponse(
            job_id=self.job_id,
            job_type=self.job_type.value,
            strategy=self.strategy,
            config_file=self.config_file,
            status=self.status.value,
            pid=self.pid,
            started_at=self.started_at,
            finished_at=self.finished_at,
            exit_code=self.exit_code,
            error=self.error,
            log_lines=list(self.log_buffer) if include_logs else [],
            command=" ".join(self.command),
        )


class _JobManager:
    def __init__(self):
        self._active: _JobState | None = None
        self._history: deque[_JobState] = deque(maxlen=_JOB_HISTORY_LIMIT)
        self._lock = threading.Lock()

    @property
    def active_job(self) -> _JobState | None:
        if self._active and self._active.status in (JobStatus.pending, JobStatus.running):
            return self._active
        return None

    def start_job(
        self,
        request: JobStartRequest,
        config: dict[str, Any],
    ) -> _JobState:
        with self._lock:
            if self.active_job is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"A job is already running ({self.active_job.job_type.value}: "
                    f"{self.active_job.strategy}). Abort it first or wait.",
                )

            project_root = Path(config.get("user_data_dir", "")).parent

            config_path = (project_root / request.config_file).resolve()
            if not config_path.is_file():
                raise HTTPException(
                    status_code=404,
                    detail=f"Config file not found: {request.config_file}",
                )
            if not str(config_path).startswith(str(project_root.resolve())):
                raise HTTPException(
                    status_code=403,
                    detail="Config file must be within the project directory",
                )

            strategy_dir = Path(config.get("user_data_dir", "")) / "strategies"
            strategy_files = list(strategy_dir.glob(f"{request.strategy}.py"))
            if not strategy_files:
                raise HTTPException(
                    status_code=404,
                    detail=f"Strategy not found: {request.strategy}",
                )

            disk_free = shutil.disk_usage(str(project_root)).free
            if disk_free < 1_000_000_000:
                raise HTTPException(
                    status_code=507,
                    detail=f"Insufficient disk space ({disk_free // 1_000_000}MB). "
                    f"Need at least 1GB free.",
                )

            cmd = self._build_command(request, config_path, config)
            job_id = str(uuid4())[:8]
            job = _JobState(
                job_id=job_id,
                job_type=request.job_type,
                strategy=request.strategy,
                config_file=request.config_file,
                command=cmd,
            )

            self._spawn(job, project_root)

            if self._active and self._active.status not in (JobStatus.pending, JobStatus.running):
                self._history.appendleft(self._active)
            self._active = job
            return job

    def get_active(self) -> _JobState | None:
        job = self._active
        if job and job.process:
            ret = job.process.poll()
            if ret is not None:
                self._finalize(job, ret)
        return job

    def get_job(self, job_id: str) -> _JobState | None:
        if self._active and self._active.job_id == job_id:
            return self.get_active()
        for j in self._history:
            if j.job_id == job_id:
                return j
        return None

    def abort_job(self, job_id: str) -> _JobState:
        with self._lock:
            job = self._active
            if not job or job.job_id != job_id:
                raise HTTPException(status_code=404, detail="Job not found or not active")
            if job.status not in (JobStatus.pending, JobStatus.running):
                raise HTTPException(status_code=409, detail="Job is not running")

            if job.process and job.process.poll() is None:
                try:
                    os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    job.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(job.process.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

            job.status = JobStatus.aborted
            job.finished_at = time.time()
            job.error = "Aborted by user"
            return job

    def list_all(self) -> JobListResponse:
        active = self.get_active()
        active_resp = active.to_response(include_logs=True) if active else None
        return JobListResponse(
            active=active_resp,
            history=[j.to_response() for j in self._history],
        )

    def _build_command(
        self,
        req: JobStartRequest,
        config_path: Path,
        config: dict[str, Any],
    ) -> list[str]:
        import sys

        venv_ft = Path(sys.executable).parent / "freqtrade"
        if venv_ft.is_file():
            ft_bin = str(venv_ft)
        else:
            ft_bin = shutil.which("freqtrade")
        if not ft_bin:
            raise HTTPException(status_code=500, detail="freqtrade binary not found in PATH")

        cmd = [ft_bin, _JOBTYPE_TO_CLI[req.job_type], "-c", str(config_path)]
        cmd.extend(["-s", req.strategy])

        user_data = config.get("user_data_dir", "")
        if user_data:
            cmd.extend(["--userdir", str(user_data)])

        if req.timerange:
            cmd.extend(["--timerange", req.timerange])
        if req.timeframe:
            cmd.extend(["--timeframe", req.timeframe])
        if req.max_open_trades is not None:
            cmd.extend(["--max-open-trades", str(req.max_open_trades)])
        if req.stake_amount is not None:
            cmd.extend(["--stake-amount", str(req.stake_amount)])
        if req.enable_protections:
            cmd.append("--enable-protections")
        if req.dry_run_wallet is not None:
            cmd.extend(["--dry-run-wallet", str(req.dry_run_wallet)])

        # Common advanced params
        if req.data_format_ohlcv:
            cmd.extend(["--data-format-ohlcv", req.data_format_ohlcv])
        if req.fee is not None:
            cmd.extend(["--fee", str(req.fee)])
        if req.pairs:
            cmd.extend(["--pairs"] + req.pairs)
        if req.enable_position_stacking:
            cmd.append("--eps")
        if req.timeframe_detail:
            cmd.extend(["--timeframe-detail", req.timeframe_detail])

        # Backtest-only params
        if req.job_type == JobType.backtest:
            export_type = req.export_type or "trades"
            cmd.extend(["--export", export_type])
            if req.breakdown:
                cmd.extend(["--breakdown"] + req.breakdown)
            if req.cache:
                cmd.extend(["--cache", req.cache])
            if req.enable_dynamic_pairlist:
                cmd.append("--enable-dynamic-pairlist")
            if req.notes:
                cmd.extend(["--notes", req.notes])

        # Hyperopt / WFA params
        if req.job_type in (JobType.hyperopt, JobType.wfa):
            cmd.extend(["--export", "trades"])
            if req.epochs:
                cmd.extend(["--epochs", str(req.epochs)])
            if req.spaces:
                cmd.extend(["--spaces"] + req.spaces)
            if req.hyperopt_loss:
                if not _SAFE_NAME_RE.match(req.hyperopt_loss):
                    raise HTTPException(
                        status_code=422, detail=f"Invalid loss name: {req.hyperopt_loss}"
                    )
                cmd.extend(["--hyperopt-loss", req.hyperopt_loss])
            if req.sampler:
                cmd.extend(["--sampler", req.sampler])
            if req.early_stop:
                cmd.extend(["--early-stop", str(req.early_stop)])
            if req.min_trades:
                cmd.extend(["--min-trades", str(req.min_trades)])
            workers = req.job_workers if req.job_workers is not None else -2
            cmd.extend(["-j", str(workers)])
            if req.random_state is not None:
                cmd.extend(["--random-state", str(req.random_state)])

        # Hyperopt-only flags
        if req.job_type == JobType.hyperopt:
            if req.print_all:
                cmd.append("--print-all")
            if req.disable_param_export:
                cmd.append("--disable-param-export")
            if req.ignore_missing_spaces:
                cmd.append("--ignore-missing-spaces")
            if req.analyze_per_epoch:
                cmd.append("--analyze-per-epoch")

        # WFA params
        if req.job_type == JobType.wfa:
            if req.wf_windows is not None:
                cmd.extend(["--wf-windows", str(req.wf_windows)])
            if req.wf_train_ratio is not None:
                cmd.extend(["--wf-train-ratio", str(req.wf_train_ratio)])
            if req.wf_embargo_days is not None:
                cmd.extend(["--wf-embargo-days", str(req.wf_embargo_days)])
            if req.wf_holdout_months is not None:
                cmd.extend(["--wf-holdout-months", str(req.wf_holdout_months)])
            if req.wf_min_test_trades is not None:
                cmd.extend(["--wf-min-test-trades", str(req.wf_min_test_trades)])
            if req.wf_mode:
                cmd.extend(["--wf-mode", req.wf_mode])
            if req.wf_multi_seed is not None:
                cmd.extend(["--wf-multi-seed", str(req.wf_multi_seed)])
            if req.wf_cpcv_groups is not None:
                cmd.extend(["--wf-cpcv-groups", str(req.wf_cpcv_groups)])
            if req.wf_cpcv_test_groups is not None:
                cmd.extend(["--wf-cpcv-test-groups", str(req.wf_cpcv_test_groups)])

        return cmd

    def _spawn(self, job: _JobState, cwd: Path) -> None:
        try:
            proc = subprocess.Popen(
                job.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
                start_new_session=True,
                text=True,
                bufsize=1,
            )
            job.process = proc
            job.pid = proc.pid
            job.status = JobStatus.running
            job.started_at = time.time()

            reader = threading.Thread(target=self._read_output, args=(job,), daemon=True)
            reader.start()
            job._reader_thread = reader

            logger.info(
                f"Started {job.job_type.value} job {job.job_id} (PID {job.pid}): "
                f"{' '.join(job.command)}"
            )
        except Exception as e:
            job.status = JobStatus.failed
            job.error = str(e)
            job.finished_at = time.time()
            logger.error(f"Failed to start job {job.job_id}: {e}")

    def _read_output(self, job: _JobState) -> None:
        try:
            assert job.process and job.process.stdout
            for line in job.process.stdout:
                job.log_buffer.append(line.rstrip("\n"))
        except Exception:
            pass
        finally:
            if job.process:
                ret = job.process.wait()
                self._finalize(job, ret)

    _ERROR_PATTERNS = re.compile(
        r"(?:^|\n).*(Error|Exception|Traceback|CRITICAL|"
        r"No data found|Cannot find|not found|ImportError|"
        r"invalid choice|Insufficient|OperationalError)",
        re.IGNORECASE,
    )
    _SUCCESS_PATTERNS = re.compile(
        r"(?:Result for strategy|Backtesting done|hyperopt done|"
        r"walk.forward.*complete|Best result|"
        r"\d+ trades|TOTAL\s)",
        re.IGNORECASE,
    )

    def _finalize(self, job: _JobState, exit_code: int) -> None:
        if job.status in (JobStatus.completed, JobStatus.failed, JobStatus.aborted):
            return
        job.exit_code = exit_code
        job.finished_at = time.time()

        log_text = "\n".join(job.log_buffer)
        has_error = bool(self._ERROR_PATTERNS.search(log_text))
        has_success = bool(self._SUCCESS_PATTERNS.search(log_text))
        ran_too_fast = (
            job.started_at
            and job.finished_at
            and (job.finished_at - job.started_at) < 3.0
            and not has_success
        )

        if exit_code != 0:
            job.status = JobStatus.failed
            last_lines = list(job.log_buffer)[-10:]
            job.error = "\n".join(last_lines) if last_lines else f"Exit code {exit_code}"
            logger.warning(f"Job {job.job_id} failed (exit {exit_code})")
        elif has_success:
            job.status = JobStatus.completed
            logger.info(f"Job {job.job_id} completed successfully")
        elif ran_too_fast:
            job.status = JobStatus.failed
            last_lines = list(job.log_buffer)[-10:]
            job.error = (
                "\n".join(last_lines)
                if last_lines
                else "Process exited too quickly (< 3s) with no output"
            )
            logger.warning(f"Job {job.job_id} failed (too fast, exit {exit_code})")
        elif has_error:
            job.status = JobStatus.failed
            last_lines = list(job.log_buffer)[-10:]
            job.error = "\n".join(last_lines) if last_lines else "Errors detected in output"
            logger.warning(f"Job {job.job_id} failed (errors in output)")
        else:
            job.status = JobStatus.completed
            logger.info(f"Job {job.job_id} completed (exit 0, no errors)")


_manager = _JobManager()


@router.get("/stratdev/jobs", response_model=JobListResponse)
def list_jobs():
    return _manager.list_all()


@router.get("/stratdev/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str):
    job = _manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_response(include_logs=True)


@router.post("/stratdev/jobs/start", response_model=JobStatusResponse)
def start_job(request: JobStartRequest, config=Depends(get_config)):
    if request.job_type in (JobType.hyperopt, JobType.wfa) and not request.epochs:
        raise HTTPException(
            status_code=422,
            detail="epochs is required for hyperopt and walk-forward jobs",
        )
    job = _manager.start_job(request, config)
    return job.to_response(include_logs=True)


@router.post("/stratdev/jobs/{job_id}/abort", response_model=JobStatusResponse)
def abort_job(job_id: str):
    job = _manager.abort_job(job_id)
    return job.to_response(include_logs=True)


@router.delete("/stratdev/jobs/{job_id}")
def delete_job(job_id: str):
    job = _manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in (JobStatus.pending, JobStatus.running):
        raise HTTPException(status_code=409, detail="Cannot delete a running job")
    with _manager._lock:
        _manager._history = deque(
            (j for j in _manager._history if j.job_id != job_id),
            maxlen=_JOB_HISTORY_LIMIT,
        )
        if _manager._active and _manager._active.job_id == job_id:
            _manager._active = None
    return {"detail": "Job deleted"}


@router.get("/stratdev/configs", response_model=ConfigListResponse)
def list_configs(config=Depends(get_config)):
    project_root = Path(config.get("user_data_dir", "")).parent

    configs: list[str] = []
    for f in sorted(project_root.glob("*.json")):
        if not f.name.startswith(".") and not f.name.startswith("_"):
            configs.append(f.name)

    for subdir in _CONFIG_SEARCH_DIRS:
        d = project_root / subdir
        if d.is_dir():
            for f in sorted(d.glob("*.json")):
                if not f.name.startswith(".") and not f.name.startswith("_"):
                    configs.append(f"{subdir}/{f.name}")

    strategy_dir = Path(config.get("user_data_dir", "")) / "strategies"
    strategies = sorted(
        f.stem
        for f in strategy_dir.glob("*.py")
        if not f.name.startswith("_") and f.name != "__init__.py"
    )

    return ConfigListResponse(configs=configs, strategies=strategies)


class ConfigDefaults(BaseModel):
    max_open_trades: int | None = None
    stake_amount: str | None = None
    dry_run_wallet: float | None = None
    stake_currency: str | None = None
    trading_mode: str | None = None
    timeframe: str | None = None
    exchange: str | None = None
    pairs_count: int | None = None


@router.get("/stratdev/configs/defaults", response_model=ConfigDefaults)
def get_config_defaults(config_file: str, config=Depends(get_config)):
    import json as _json

    if not _SAFE_PATH_RE.match(config_file) or ".." in config_file:
        raise HTTPException(status_code=422, detail="Invalid config path")

    project_root = Path(config.get("user_data_dir", "")).parent
    config_path = (project_root / config_file).resolve()
    if not str(config_path).startswith(str(project_root.resolve())):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    if not config_path.is_file():
        raise HTTPException(status_code=404, detail="Config file not found")

    try:
        merged: dict = {}
        with open(config_path) as f:
            base = _json.load(f)

        for extra in base.get("add_config_files", []):
            extra_path = (config_path.parent / extra).resolve()
            if extra_path.is_file() and str(extra_path).startswith(str(project_root.resolve())):
                try:
                    with open(extra_path) as ef:
                        extra_cfg = _json.load(ef)
                    merged.update(extra_cfg)
                except Exception:
                    pass
        merged.update(base)

        exchange_name = merged.get("exchange", {}).get("name")
        pairs = merged.get("exchange", {}).get("pair_whitelist", [])

        return ConfigDefaults(
            max_open_trades=merged.get("max_open_trades"),
            stake_amount=str(merged.get("stake_amount")) if "stake_amount" in merged else None,
            dry_run_wallet=merged.get("dry_run_wallet"),
            stake_currency=merged.get("stake_currency"),
            trading_mode=merged.get("trading_mode"),
            timeframe=merged.get("timeframe"),
            exchange=exchange_name,
            pairs_count=len(pairs) if pairs else None,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read config: {e}")
