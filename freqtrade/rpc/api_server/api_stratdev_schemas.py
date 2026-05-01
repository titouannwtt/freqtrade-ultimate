from enum import Enum
from typing import Any

from pydantic import BaseModel


class RunType(str, Enum):
    backtest = "backtest"
    hyperopt = "hyperopt"
    wfa = "wfa"


class RunListEntry(BaseModel):
    run_type: RunType
    filename: str
    strategy: str
    timestamp: int = 0
    timeframe: str | None = None
    timerange: str | None = None
    notes: str | None = None
    has_metadata: bool = True
    total_profit_pct: float | None = None
    total_trades: int | None = None
    best_sharpe: float | None = None
    hyperopt_loss: str | None = None
    epochs_total: int | None = None
    epochs_completed: int | None = None
    best_loss: float | None = None
    verdict_grade: str | None = None
    n_windows: int | None = None
    run_id: str | None = None


class AllRunsResponse(BaseModel):
    backtests: list[RunListEntry]
    hyperopts: list[RunListEntry]
    wfa_runs: list[RunListEntry]


class SnapshotDiffRequest(BaseModel):
    run_type: RunType
    filename: str
    diff_type: str


class SnapshotDiffResponse(BaseModel):
    snapshot: str
    current: str | None = None
    has_changes: bool


class BacktestSnapshotResponse(BaseModel):
    strategy_source: str | None = None
    config: dict[str, Any] | None = None
    strategy_params: dict[str, Any] | None = None


class MetadataUpdateRequest(BaseModel):
    notes: str | None = None
    tags: list[str] | None = None
    favorite: bool | None = None
