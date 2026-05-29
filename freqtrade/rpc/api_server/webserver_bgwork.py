from typing import Any, Literal, NotRequired
from uuid import uuid4

from typing_extensions import TypedDict

from freqtrade.exchange.exchange import Exchange


class ProgressTask(TypedDict):
    progress: float
    total: float
    description: str


class JobsContainer(TypedDict):
    category: Literal["pairlist", "download_data"]
    is_running: bool
    status: str
    progress: float | None
    progress_tasks: NotRequired[dict[str, ProgressTask]]
    result: Any
    error: str | None


class ApiBG:
    # Backtesting type: Backtesting
    bt: dict[str, Any] = {
        "bt": None,
        "data": None,
        "timerange": None,
        "last_config": {},
        "bt_error": None,
    }
    bgtask_running: bool = False
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

    @staticmethod
    def get_job_id() -> str:
        return str(uuid4())
