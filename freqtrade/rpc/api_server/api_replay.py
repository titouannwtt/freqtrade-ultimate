"""
Dry-run replay API (fork-specific).

The replay drives the real live bot loop over historical data. It freezes the
process-wide clock (freezegun), so it must NOT run inside the always-on
webserver process — instead we launch ``python -m freqtrade.replay`` as an
isolated subprocess and track it here:

  POST   /replay  → start a replay (one at a time)
  GET    /replay  → poll progress / fetch the final summary
  DELETE /replay  → stop / reset

Progress and the final summary are exchanged via a small JSON file the replay
CLI writes (``--progress-file``). The replay itself enforces dry-run-only.
"""

import json
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from freqtrade.enums import State
from freqtrade.replay import coordinator_client as cc
from freqtrade.replay import lifecycle
from freqtrade.rpc.api_server.api_schemas import (
    ReplayReprioritizeRequest,
    ReplayRequest,
    ReplayResponse,
)
from freqtrade.rpc.api_server.deps import get_config, get_rpc, verify_strategy


logger = logging.getLogger(__name__)

# Private API, protected by authentication and the webserver_mode dependency.
router = APIRouter()


def _db_file(db_url: str | None) -> Path | None:
    """Filesystem path of a sqlite db_url, else None."""
    prefix = "sqlite:///"
    if db_url and db_url.startswith(prefix):
        return Path(db_url[len(prefix) :])
    return None


def _backup_path(db_url: str | None) -> Path | None:
    p = _db_file(db_url)
    return p.with_suffix(p.suffix + ".pre-replay.bak") if p else None


def _backup_db(db_url: str | None) -> str | None:
    """Snapshot the bot's dry DB before seeding, so a crashed replay can be undone."""
    src = _db_file(db_url)
    if src is None or not src.exists():
        return None
    bak = _backup_path(db_url)
    shutil.copy2(src, bak)
    logger.info("[dry-run replay] backed up %s -> %s", src.name, bak.name)
    return str(bak)


def _seed_marker() -> dict | None:
    """Read the 'already seeded' marker from this bot's (dry-run) database."""
    from freqtrade.persistence import KeyValueStore

    try:
        raw = KeyValueStore.get_string_value("ft_replay_seed")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": raw}


@router.get("/replay/seeded")
def api_replay_seeded(config=Depends(get_config)):
    """Whether this bot is dry-run and whether its DB was already replay-seeded."""
    marker = _seed_marker()
    bak = _backup_path(config.get("db_url"))
    return {
        "dry_run": bool(config.get("dry_run")),
        "seeded": marker is not None,
        "info": marker,
        "backup_available": bool(bak and bak.exists()),
    }


@router.get("/replay/coverage")
def api_replay_coverage(
    timeframe: str = Query("1m"),
    pairs: list[str] = Query(default=[]),
    config=Depends(get_config),
):
    """
    Local data coverage at ``timeframe`` for the given pairs — feeds the modal's
    fidelity warnings (earliest = oldest first-candle across pairs that have the tf;
    latest = newest last-candle). Reads only date columns, cached by mtime → fast.
    """
    from freqtrade.enums import CandleType
    from freqtrade.replay.data_store import ReplayDataStore
    from freqtrade.replay.runner import _default_datadir

    trading_mode = config.get("trading_mode", "futures")
    ct = CandleType.get_default(trading_mode)
    store = ReplayDataStore(_default_datadir(config, trading_mode), trading_mode=trading_mode)

    firsts, lasts, with_data = [], [], 0
    for pair in pairs:
        rng = store.date_range(pair, timeframe, ct)
        if rng is not None:
            with_data += 1
            firsts.append(rng[0])
            lasts.append(rng[1])
    return {
        "timeframe": timeframe,
        "earliest": min(firsts).isoformat() if firsts else None,
        "latest": max(lasts).isoformat() if lasts else None,
        "pairs_with_data": with_data,
        "pairs_total": len(pairs),
    }


_RUNNING_MSG = {
    "running": "Replay running",
    "queued": "Queued — waiting for a free core",
    "paused": "Paused — cores busy (hyperopt or higher-priority replays)",
}


@router.post("/replay", response_model=ReplayResponse)
def api_start_replay(req: ReplayRequest, rpc=Depends(get_rpc), config=Depends(get_config)):
    """
    Seed this bot's dry-run database from a historical replay.

    Stops the bot (so the replay is the sole DB writer), then submits it to the machine-wide
    coordinator, which runs it when a core is free. Refuses for live bots and, unless
    ``reset_db``, if already seeded.
    """
    bot_id = lifecycle.coord_bot_id(config)
    if cc.bot_status(bot_id).get("state") in ("queued", "running", "paused"):
        raise HTTPException(status_code=409, detail="A replay is already active for this bot")

    if not config.get("dry_run"):
        raise HTTPException(
            status_code=403, detail="Dry-run replay is only available for dry-run bots"
        )
    if not req.reset_db and _seed_marker() is not None:
        raise HTTPException(
            status_code=409,
            detail="This dry-run was already seeded by a replay. Tick 'reset DB' to re-run.",
        )
    verify_strategy(req.strategy)
    if not config.get("config_files"):
        raise HTTPException(status_code=400, detail="No config file available to launch a replay")

    # Snapshot the DB before seeding so a crashed/unwanted seed can be restored.
    _backup_db(config["db_url"])
    res = lifecycle.start_replay(
        rpc._freqtrade,
        strategy=req.strategy,
        timerange=req.timerange,
        pairs=req.pairs,
        wallet=req.wallet,
        sub_step=req.sub_step,
        reset_db=req.reset_db,
        priority=req.priority,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "Could not start replay"))
    state = res.get("state", "queued")
    return {
        "status": state,
        "running": state == "running",
        "status_msg": _RUNNING_MSG.get(state, "Replay submitted"),
        "progress": 0.0,
        "step": "startup",
        "db_url": config["db_url"],
    }


@router.get("/replay", response_model=ReplayResponse)
def api_get_replay(config=Depends(get_config)):
    """Poll this bot's replay state from the coordinator."""
    bot_id = lifecycle.coord_bot_id(config)
    bs = cc.bot_status(bot_id)
    state = bs.get("state", "none")

    if state == "none":
        return {"status": "not_started", "running": False,
                "status_msg": "No replay has been started", "progress": 0.0}
    if state in ("running", "queued", "paused"):
        return {
            "status": state, "running": state == "running",
            "status_msg": _RUNNING_MSG[state], "progress": bs.get("progress", 0.0),
            "step": bs.get("step", ""), "db_url": bs.get("db_url"),
            "elapsed_s": bs.get("elapsed_s"), "eta_s": bs.get("eta_s"),
        }
    if state == "error":
        return {"status": "error", "running": False,
                "status_msg": bs.get("error") or "Replay failed",
                "progress": bs.get("progress", 0.0), "db_url": bs.get("db_url")}
    if state == "cancelled":
        return {"status": "not_started", "running": False,
                "status_msg": "Replay cancelled", "progress": 0.0}
    return {  # done
        "status": "done", "running": False, "status_msg": "Replay finished",
        "progress": 1.0, "step": "finished", "db_url": bs.get("db_url"),
        "result": bs.get("result"),
    }


@router.delete("/replay", response_model=ReplayResponse)
def api_stop_replay(rpc=Depends(get_rpc)):
    """Cancel this bot's replay and resume the bot."""
    logger.info("[dry-run replay] stopping seed (user requested)")
    lifecycle.cancel_replay(rpc._freqtrade)
    return {"status": "not_started", "running": False,
            "status_msg": "Replay stopped/reset", "progress": 0.0}


@router.get("/replay/queue")
def api_replay_queue():
    """Machine-wide coordinator view: capacity + running / paused / queued replays."""
    return cc.status()


@router.post("/replay/reprioritize", response_model=ReplayResponse)
def api_replay_reprioritize(req: ReplayReprioritizeRequest):
    """Move a (possibly different) bot's replay up/down the global queue."""
    res = cc.reprioritize(req.bot_id, req.priority)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error", "not found"))
    return {"status": res.get("state", "queued"), "running": res.get("state") == "running",
            "status_msg": "Priority updated", "progress": 0.0}


@router.post("/replay/restore", response_model=ReplayResponse)
def api_restore_replay(rpc=Depends(get_rpc), config=Depends(get_config)):
    """
    Restore the bot's dry DB from the pre-replay snapshot (undo a crashed/unwanted seed),
    then reload the bot so it re-opens the restored database.
    """
    bot_id = lifecycle.coord_bot_id(config)
    if cc.bot_status(bot_id).get("state") in ("queued", "running", "paused"):
        raise HTTPException(status_code=409, detail="Stop the running replay before restoring")
    bak = _backup_path(config.get("db_url"))
    db = _db_file(config.get("db_url"))
    if db is None or bak is None or not bak.exists():
        raise HTTPException(status_code=404, detail="No pre-replay backup found for this bot")

    shutil.copy2(bak, db)
    logger.info("[dry-run replay] restored %s from %s", db.name, bak.name)
    # Re-open the DB on the live bot so it serves the restored history.
    rpc._freqtrade.state = State.RELOAD_CONFIG
    return {
        "status": "not_started",
        "running": False,
        "status_msg": "Database restored from the pre-replay backup; bot reloading.",
        "progress": 0.0,
    }
