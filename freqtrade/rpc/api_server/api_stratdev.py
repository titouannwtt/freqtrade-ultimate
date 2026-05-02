import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from freqtrade.rpc.api_server.api_stratdev_schemas import (
    AllRunsResponse,
    BacktestSnapshotResponse,
    MetadataUpdateRequest,
    RunListEntry,
    SnapshotDiffRequest,
    SnapshotDiffResponse,
)
from freqtrade.rpc.api_server.deps import get_config


logger = logging.getLogger(__name__)
router = APIRouter()


async def _safe_to_thread(func, *args):
    try:
        return await asyncio.to_thread(func, *args)
    except RuntimeError as e:
        if "cannot schedule new futures" in str(e) or "interpreter shutdown" in str(e):
            logger.debug("Ignoring thread pool submission during shutdown")
            raise HTTPException(status_code=503, detail="Server shutting down")
        raise

_runs_cache: AllRunsResponse | None = None
_runs_cache_ts: float = 0.0
_RUNS_CACHE_TTL_S = 300.0


def _bt_dir(config: dict) -> Path:
    return Path(config["user_data_dir"]) / "backtest_results"


def _ho_dir(config: dict) -> Path:
    return Path(config["user_data_dir"]) / "hyperopt_results"


def _wfa_dir(config: dict) -> Path:
    d = Path(config["user_data_dir"]) / "walk_forward_results"
    if not d.exists():
        old = Path(config["user_data_dir"]) / "walk_forward"
        if old.exists():
            return old
    return d


def _build_all_runs(
    config: dict,
    strategy: str | None = None,
    run_type: str | None = None,
    ho_offset: int = 0,
    ho_limit: int | None = None,
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import (
        convert_backtest_entries,
        get_hyperopt_resultlist,
        get_wfa_resultlist,
    )

    backtests: list[dict[str, Any]] = []
    hyperopts: list[dict[str, Any]] = []
    wfa_runs: list[dict[str, Any]] = []
    ho_total: int = 0

    if run_type is None or run_type == "backtest":
        bd = _bt_dir(config)
        if bd.exists():
            try:
                backtests = convert_backtest_entries(bd)
            except Exception as e:
                logger.error(f"Failed to read backtests: {e}")

    if run_type is None or run_type == "hyperopt":
        hd = _ho_dir(config)
        if hd.exists():
            try:
                ho_result = get_hyperopt_resultlist(hd, ho_offset, ho_limit)
                hyperopts = ho_result["items"]
                ho_total = ho_result["total"]
            except Exception as e:
                logger.error(f"Failed to read hyperopts: {e}")

    if run_type is None or run_type == "wfa":
        wd = _wfa_dir(config)
        if wd.exists():
            try:
                wfa_runs = get_wfa_resultlist(wd)
            except Exception as e:
                logger.error(f"Failed to read WFA runs: {e}")

    if strategy:
        backtests = [e for e in backtests if e.get("strategy") == strategy]
        hyperopts = [e for e in hyperopts if e.get("strategy") == strategy]
        wfa_runs = [e for e in wfa_runs if e.get("strategy") == strategy]

    return {
        "backtests": [RunListEntry(**e) for e in backtests],
        "hyperopts": [RunListEntry(**e) for e in hyperopts],
        "wfa_runs": [RunListEntry(**e) for e in wfa_runs],
        "hyperopts_total": ho_total,
    }


@router.get("/stratdev/runs", response_model=AllRunsResponse)
async def api_list_all_runs(
    strategy: str | None = None,
    run_type: str | None = None,
    ho_offset: int = 0,
    ho_limit: int | None = None,
    config: dict = Depends(get_config),
) -> AllRunsResponse:
    global _runs_cache, _runs_cache_ts
    now = time.monotonic()
    is_paginated = ho_offset > 0 or ho_limit is not None
    if (
        _runs_cache is not None
        and not strategy
        and not run_type
        and not is_paginated
        and (now - _runs_cache_ts) < _RUNS_CACHE_TTL_S
    ):
        return _runs_cache

    result = await _safe_to_thread(
        _build_all_runs, config, strategy, run_type, ho_offset, ho_limit,
    )
    if not strategy and not run_type and not is_paginated:
        _runs_cache = AllRunsResponse(**result)
        _runs_cache_ts = now
    return AllRunsResponse(**result)


@router.get("/stratdev/hyperopt/{filename}")
async def api_hyperopt_detail(
    filename: str,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import get_hyperopt_run_detail

    return await _safe_to_thread(get_hyperopt_run_detail, _ho_dir(config), filename)


@router.get("/stratdev/hyperopt/{filename}/analysis")
async def api_hyperopt_analysis(
    filename: str,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import compute_hyperopt_analysis

    return await _safe_to_thread(compute_hyperopt_analysis, _ho_dir(config), filename)


@router.get("/stratdev/hyperopt/{filename}/advanced")
async def api_hyperopt_advanced(
    filename: str,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import compute_advanced_analytics

    return await _safe_to_thread(compute_advanced_analytics, _ho_dir(config), filename)


@router.get("/stratdev/hyperopt/{filename}/epoch/{rank}/advanced")
async def api_hyperopt_epoch_advanced(
    filename: str,
    rank: int,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import compute_epoch_advanced_analytics

    return await _safe_to_thread(
        compute_epoch_advanced_analytics, _ho_dir(config), filename, rank,
    )


@router.get("/stratdev/hyperopt/{filename}/epoch/{rank}")
async def api_hyperopt_epoch_detail(
    filename: str,
    rank: int,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import get_epoch_detail

    return await _safe_to_thread(get_epoch_detail, _ho_dir(config), filename, rank)


@router.delete("/stratdev/hyperopt/{filename}")
async def api_delete_hyperopt(
    filename: str,
    config: dict = Depends(get_config),
) -> AllRunsResponse:
    from freqtrade.optimize.stratdev_readers import (
        delete_hyperopt_result,
        get_hyperopt_resultlist,
    )

    global _runs_cache, _runs_cache_ts
    await _safe_to_thread(delete_hyperopt_result, _ho_dir(config), filename)
    _runs_cache = None
    _runs_cache_ts = 0.0
    ho_result = await _safe_to_thread(get_hyperopt_resultlist, _ho_dir(config))
    return AllRunsResponse(
        backtests=[],
        hyperopts=[RunListEntry(**e) for e in ho_result["items"]],
        wfa_runs=[],
        hyperopts_total=ho_result["total"],
    )


@router.patch("/stratdev/hyperopt/{filename}")
async def api_update_hyperopt_meta(
    filename: str,
    body: MetadataUpdateRequest,
    config: dict = Depends(get_config),
) -> dict[str, str]:
    return await _safe_to_thread(
        _update_meta, _ho_dir(config) / f"{filename}.meta.json", body,
    )


@router.get("/stratdev/wfa/{filename}")
async def api_wfa_detail(
    filename: str,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import get_wfa_run_detail

    return await _safe_to_thread(get_wfa_run_detail, _wfa_dir(config), filename)


@router.delete("/stratdev/wfa/{filename}")
async def api_delete_wfa(
    filename: str,
    config: dict = Depends(get_config),
) -> AllRunsResponse:
    from freqtrade.optimize.stratdev_readers import (
        delete_wfa_result,
        get_wfa_resultlist,
    )

    global _runs_cache, _runs_cache_ts
    await _safe_to_thread(delete_wfa_result, _wfa_dir(config), filename)
    _runs_cache = None
    _runs_cache_ts = 0.0
    remaining = await _safe_to_thread(get_wfa_resultlist, _wfa_dir(config))
    return AllRunsResponse(
        backtests=[],
        hyperopts=[],
        wfa_runs=[RunListEntry(**e) for e in remaining],
    )


@router.patch("/stratdev/wfa/{filename}")
async def api_update_wfa_meta(
    filename: str,
    body: MetadataUpdateRequest,
    config: dict = Depends(get_config),
) -> dict[str, str]:
    import rapidjson

    from freqtrade.misc import file_dump_json

    def _do_update() -> dict[str, str]:
        wfa_file = _wfa_dir(config) / f"{filename}.json"
        if not wfa_file.exists():
            return {"status": "not_found"}
        with wfa_file.open() as f:
            data = rapidjson.load(f)
        if body.notes is not None:
            data["notes"] = body.notes
        if body.tags is not None:
            data["tags"] = body.tags
        if body.favorite is not None:
            data["favorite"] = body.favorite
        file_dump_json(wfa_file, data)
        return {"status": "ok"}

    return await _safe_to_thread(_do_update)


@router.get("/stratdev/backtest/{filename}/analysis")
async def api_backtest_analysis(
    filename: str,
    strategy: str,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import compute_backtest_analytics

    return await _safe_to_thread(
        compute_backtest_analytics, _bt_dir(config), filename, strategy,
    )


@router.get(
    "/stratdev/backtest/{filename}/snapshot",
    response_model=BacktestSnapshotResponse,
)
async def api_backtest_snapshot(
    filename: str,
    strategy: str,
    config: dict = Depends(get_config),
) -> BacktestSnapshotResponse:
    from freqtrade.optimize.stratdev_readers import get_backtest_snapshot

    result = await _safe_to_thread(
        get_backtest_snapshot, _bt_dir(config), filename, strategy,
    )
    return BacktestSnapshotResponse(**result)


@router.post("/stratdev/diff", response_model=SnapshotDiffResponse)
async def api_snapshot_diff(
    body: SnapshotDiffRequest,
    config: dict = Depends(get_config),
) -> SnapshotDiffResponse:
    def _do_diff() -> dict:
        from freqtrade.optimize.stratdev_readers import (
            compute_snapshot_diff,
            get_backtest_snapshot,
            get_hyperopt_run_detail,
            get_wfa_run_detail,
        )

        saved = ""
        current_path = Path("/dev/null")

        if body.diff_type == "strategy":
            saved, current_path = _get_strategy_snapshot(
                body, config,
                get_hyperopt_run_detail, get_wfa_run_detail, get_backtest_snapshot,
            )
        elif body.diff_type == "config":
            saved, current_path = _get_config_snapshot(body, config)

        return compute_snapshot_diff(saved, current_path)

    result = await _safe_to_thread(_do_diff)
    return SnapshotDiffResponse(**result)


@router.get("/stratdev/backtest/{filename}/plot-profit")
async def api_backtest_plot_profit(
    filename: str,
    strategy: str,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import compute_plot_profit_data

    return await _safe_to_thread(
        compute_plot_profit_data,
        _bt_dir(config),
        filename,
        strategy,
    )


@router.get("/stratdev/backtest/{filename}/plot-dataframe")
async def api_backtest_plot_dataframe(
    filename: str,
    strategy: str,
    pair: str,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_dataframe import compute_plot_dataframe

    return await _safe_to_thread(
        compute_plot_dataframe,
        config,
        _bt_dir(config),
        filename,
        strategy,
        pair,
    )


@router.get("/stratdev/backtest/{filename}/pairs")
async def api_backtest_pairs(
    filename: str,
    strategy: str,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_dataframe import get_backtest_pairs

    return await _safe_to_thread(
        get_backtest_pairs,
        _bt_dir(config),
        filename,
        strategy,
    )


@router.get("/stratdev/glossary")
def api_glossary() -> dict[str, Any]:
    from freqtrade.optimize.wfa_glossary import (
        LOSS_GLOSSARY,
        METRIC_GLOSSARY,
        SAMPLER_GLOSSARY,
    )

    return {
        "metrics": METRIC_GLOSSARY,
        "samplers": SAMPLER_GLOSSARY,
        "losses": LOSS_GLOSSARY,
    }


def _update_meta(meta_path: Path, body: MetadataUpdateRequest) -> dict[str, str]:
    import rapidjson

    from freqtrade.misc import file_dump_json

    if not meta_path.exists():
        return {"status": "not_found"}
    with meta_path.open() as f:
        meta = rapidjson.load(f)
    if body.notes is not None:
        meta["notes"] = body.notes
    if body.tags is not None:
        meta["tags"] = body.tags
    if body.favorite is not None:
        meta["favorite"] = body.favorite
    file_dump_json(meta_path, meta)
    return {"status": "ok"}


def _get_strategy_snapshot(
    body: SnapshotDiffRequest,
    config: dict,
    get_ho_detail,
    get_wfa_detail,
    get_bt_snapshot,
) -> tuple[str, Path]:
    from freqtrade.optimize.hyperopt_tools import HyperoptTools

    saved = ""
    strategy_name = ""

    if body.run_type == "hyperopt":
        detail = get_ho_detail(_ho_dir(config), body.filename)
        saved = detail.get("strategy_source", "")
        strategy_name = detail.get("strategy", "")
    elif body.run_type == "wfa":
        detail = get_wfa_detail(_wfa_dir(config), body.filename)
        saved = detail.get("strategy_source", "") or ""
        strategy_name = detail.get("strategy", "")
    elif body.run_type == "backtest":
        parts = body.filename.rsplit("_", 1)
        strat = parts[0] if len(parts) > 1 else body.filename
        snap = get_bt_snapshot(_bt_dir(config), body.filename, strat)
        saved = snap.get("strategy_source") or ""
        strategy_name = strat

    current_path = Path("/dev/null")
    if strategy_name:
        fn = HyperoptTools.get_strategy_filename(config, strategy_name)
        if fn:
            current_path = fn
    return saved or "", current_path


def _get_config_snapshot(
    body: SnapshotDiffRequest,
    config: dict,
) -> tuple[str, Path]:
    import rapidjson

    saved = ""
    if body.run_type == "hyperopt":
        meta_path = _ho_dir(config) / f"{body.filename}.meta.json"
        if meta_path.exists():
            with meta_path.open() as f:
                meta = rapidjson.load(f)
            cfg = meta.get("config", {})
            saved = rapidjson.dumps(cfg, indent=2)
    elif body.run_type == "wfa":
        from freqtrade.optimize.stratdev_readers import get_wfa_run_detail

        detail = get_wfa_run_detail(_wfa_dir(config), body.filename)
        cfg = detail.get("config", {})
        saved = rapidjson.dumps(cfg, indent=2) if cfg else ""

    config_files = config.get("config", [])
    if isinstance(config_files, list) and config_files:
        current_path = Path(config_files[0])
    else:
        current_path = Path("/dev/null")
    return saved, current_path
