import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

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


@router.get("/stratdev/runs", response_model=AllRunsResponse)
def api_list_all_runs(
    strategy: str | None = None,
    run_type: str | None = None,
    config: dict = Depends(get_config),
) -> AllRunsResponse:
    from freqtrade.optimize.stratdev_readers import (
        convert_backtest_entries,
        get_hyperopt_resultlist,
        get_wfa_resultlist,
    )

    backtests: list[dict[str, Any]] = []
    hyperopts: list[dict[str, Any]] = []
    wfa_runs: list[dict[str, Any]] = []

    if run_type is None or run_type == "backtest":
        bd = _bt_dir(config)
        if bd.exists():
            backtests = convert_backtest_entries(bd)

    if run_type is None or run_type == "hyperopt":
        hd = _ho_dir(config)
        if hd.exists():
            hyperopts = get_hyperopt_resultlist(hd)

    if run_type is None or run_type == "wfa":
        wd = _wfa_dir(config)
        if wd.exists():
            wfa_runs = get_wfa_resultlist(wd)

    if strategy:
        backtests = [e for e in backtests if e.get("strategy") == strategy]
        hyperopts = [e for e in hyperopts if e.get("strategy") == strategy]
        wfa_runs = [e for e in wfa_runs if e.get("strategy") == strategy]

    return AllRunsResponse(
        backtests=[RunListEntry(**e) for e in backtests],
        hyperopts=[RunListEntry(**e) for e in hyperopts],
        wfa_runs=[RunListEntry(**e) for e in wfa_runs],
    )


@router.get("/stratdev/hyperopt/{filename}")
def api_hyperopt_detail(
    filename: str,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import get_hyperopt_run_detail

    return get_hyperopt_run_detail(_ho_dir(config), filename)


@router.get("/stratdev/hyperopt/{filename}/analysis")
def api_hyperopt_analysis(
    filename: str,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import compute_hyperopt_analysis

    return compute_hyperopt_analysis(_ho_dir(config), filename)


@router.delete("/stratdev/hyperopt/{filename}")
def api_delete_hyperopt(
    filename: str,
    config: dict = Depends(get_config),
) -> AllRunsResponse:
    from freqtrade.optimize.stratdev_readers import (
        delete_hyperopt_result,
        get_hyperopt_resultlist,
    )

    delete_hyperopt_result(_ho_dir(config), filename)
    remaining = get_hyperopt_resultlist(_ho_dir(config))
    return AllRunsResponse(
        backtests=[],
        hyperopts=[RunListEntry(**e) for e in remaining],
        wfa_runs=[],
    )


@router.patch("/stratdev/hyperopt/{filename}")
def api_update_hyperopt_meta(
    filename: str,
    body: MetadataUpdateRequest,
    config: dict = Depends(get_config),
) -> dict[str, str]:
    return _update_meta(_ho_dir(config) / f"{filename}.meta.json", body)


@router.get("/stratdev/wfa/{filename}")
def api_wfa_detail(
    filename: str,
    config: dict = Depends(get_config),
) -> dict[str, Any]:
    from freqtrade.optimize.stratdev_readers import get_wfa_run_detail

    return get_wfa_run_detail(_wfa_dir(config), filename)


@router.delete("/stratdev/wfa/{filename}")
def api_delete_wfa(
    filename: str,
    config: dict = Depends(get_config),
) -> AllRunsResponse:
    from freqtrade.optimize.stratdev_readers import (
        delete_wfa_result,
        get_wfa_resultlist,
    )

    delete_wfa_result(_wfa_dir(config), filename)
    remaining = get_wfa_resultlist(_wfa_dir(config))
    return AllRunsResponse(
        backtests=[],
        hyperopts=[],
        wfa_runs=[RunListEntry(**e) for e in remaining],
    )


@router.patch("/stratdev/wfa/{filename}")
def api_update_wfa_meta(
    filename: str,
    body: MetadataUpdateRequest,
    config: dict = Depends(get_config),
) -> dict[str, str]:
    import rapidjson

    from freqtrade.misc import file_dump_json

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


@router.get(
    "/stratdev/backtest/{filename}/snapshot",
    response_model=BacktestSnapshotResponse,
)
def api_backtest_snapshot(
    filename: str,
    strategy: str,
    config: dict = Depends(get_config),
) -> BacktestSnapshotResponse:
    from freqtrade.optimize.stratdev_readers import get_backtest_snapshot

    result = get_backtest_snapshot(_bt_dir(config), filename, strategy)
    return BacktestSnapshotResponse(**result)


@router.post("/stratdev/diff", response_model=SnapshotDiffResponse)
def api_snapshot_diff(
    body: SnapshotDiffRequest,
    config: dict = Depends(get_config),
) -> SnapshotDiffResponse:
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
            body,
            config,
            get_hyperopt_run_detail,
            get_wfa_run_detail,
            get_backtest_snapshot,
        )
    elif body.diff_type == "config":
        saved, current_path = _get_config_snapshot(body, config)

    result = compute_snapshot_diff(saved, current_path)
    return SnapshotDiffResponse(**result)


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
