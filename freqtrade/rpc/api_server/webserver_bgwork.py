from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, NotRequired
from uuid import uuid4

from typing_extensions import TypedDict

from freqtrade.exchange.exchange import Exchange


if TYPE_CHECKING:
    from pandas import DataFrame

    from freqtrade.configuration import TimeRange
    from freqtrade.constants import Config
    from freqtrade.optimize.backtesting import Backtesting


class ProgressTask(TypedDict):
    progress: float
    total: float
    description: str


JOB_CATEGORIES = Literal[
    "pairlist", "download_data", "backtest", "lookahead_analysis", "recursive_analysis"
]


class JobsContainer(TypedDict):
    category: JOB_CATEGORIES
    is_running: bool
    status: str
    progress: float | None
    progress_tasks: NotRequired[dict[str, ProgressTask]]
    result: Any
    error: str | None


class BtContainer(TypedDict):
    bt: Backtesting | None
    data: dict[str, DataFrame]
    timerange: TimeRange | None
    last_config: Config
    job_id: str | None


class ApiBG:
    # Backtesting type: Backtesting
    # Holds the backtesting instance and its cached data.
    # job_id links to the job container.
    bt: BtContainer = {
        "bt": None,
        "data": {},
        "timerange": None,
        "last_config": {},
        "job_id": None,
    }
    # Exchange - only available in webserver mode.
    exchanges: dict[str, Exchange] = {}

    # Dry-run replay (fork-specific). The replay runs in an isolated subprocess
    # because it freezes the process-wide clock (freezegun) — which must not
    # affect the always-on webserver. State here just tracks that subprocess.
    replay: dict[str, Any] = {
        "proc": None,  # subprocess.Popen | None
        "progress_file": None,  # path to the JSON progress/result file
        "db_url": None,  # the *.replay.sqlite produced
        "error": None,
    }
    replay_running: bool = False

    # Generic background jobs

    # TODO: Change this to FtTTLCache -> must be more intelligent than FtTTLCache - as we can't
    # evict still running jobs.
    jobs: dict[str, JobsContainer] = {}
    # Pairlist evaluate things
    pairlist_running: bool = False
    download_data_running: bool = False
    # Lookahead / recursive analysis
    analysis_running: bool = False

    @staticmethod
    def get_job_id() -> str:
        return str(uuid4())
