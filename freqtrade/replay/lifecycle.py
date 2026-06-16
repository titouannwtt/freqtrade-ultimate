"""
Bot ↔ replay lifecycle.

While a replay seeds a bot's dry DB, the bot must not write to that DB (the replay
subprocess is the sole writer → no concurrency, no StaleDataError / scrambled dates).
This fork's worker exits the process on RUNNING→STOPPED (for auto-restart), so we do NOT
stop the bot. Instead the bot stays RUNNING but ``FreqtradeBot.process()`` short-circuits
(skips the whole trading cycle) while a replay is pending. Flow:

    submit to coordinator (queued/running) → process() no-ops each cycle
    → replay done → RELOAD_CONFIG (re-open the seeded DB and resume)

State is per-process (one bot per process).
"""

from __future__ import annotations

import logging
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from freqtrade.enums import State
from freqtrade.replay import coordinator_client as cc


logger = logging.getLogger(__name__)

# Per-process pending replay (one bot per process). Empty when no replay is in flight.
_pending: dict = {}


def coord_bot_id(config: dict) -> str:
    """Machine-unique id for the coordinator = the dry DB filename stem (one DB per bot)."""
    db = config.get("db_url", "") or ""
    prefix = "sqlite:///"
    if db.startswith(prefix):
        return Path(db[len(prefix) :]).stem
    return config.get("bot_name") or "bot"


def build_cmd(
    config: dict, *, strategy, timerange, pairs, wallet, sub_step, db_url, progress_file, reset_db
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "freqtrade.replay",
        "--config",
        config["config_files"][0],
        "--strategy",
        strategy,
        "--timerange",
        timerange,
        "--pairs",
        *pairs,
        "--wallet",
        str(wallet),
        "--sub-step",
        str(sub_step),
        "--db-url",
        db_url,
        "--seed",
        "--progress-file",
        progress_file,
    ]
    if reset_db:
        cmd.append("--reset-db")
    return cmd


def start_replay(
    freqtrade, *, strategy, timerange, pairs, wallet, sub_step, reset_db=False, priority: int = 0
) -> dict:
    """Stop the bot and submit its replay to the coordinator. Returns the submit result."""
    config = freqtrade.config
    bot_id = coord_bot_id(config)
    db_url = config["db_url"]
    progress_file = tempfile.NamedTemporaryFile(
        prefix="ft_replay_", suffix=".json", delete=False
    ).name
    cmd = build_cmd(
        config,
        strategy=strategy,
        timerange=timerange,
        pairs=pairs,
        wallet=wallet,
        sub_step=sub_step,
        db_url=db_url,
        progress_file=progress_file,
        reset_db=reset_db,
    )

    res = cc.submit(bot_id, cmd, priority, progress_file, db_url)
    if not res.get("ok"):
        logger.warning("[dry-run replay] submit refused: %s", res.get("error"))
        return res
    # Mark the bot as "seeding": process() will no-op until the replay finishes.
    # Keep the full submit context so on_replay_tick can re-submit if the coordinator
    # daemon restarts and loses the job before it ever runs (otherwise the bot would
    # reload without ever seeding — and never retry, _autolaunch_checked being a one-shot).
    _pending.clear()
    _pending.update(
        {
            "bot_id": bot_id,
            "progress_file": progress_file,
            "cmd": cmd,
            "priority": priority,
            "db_url": db_url,
            "seen_active": False,
            "resubmits": 0,
        }
    )
    logger.info(
        "[dry-run replay] seed submitted for %s (state=%s); bot trading paused",
        bot_id,
        res.get("state"),
    )
    return res


def replay_pending() -> bool:
    """True while a replay is seeding this bot (process() should skip trading)."""
    return bool(_pending)


_MAX_RESUBMITS = 3  # tolerate a coordinator restart that loses an in-flight job


def _resume(freqtrade, why: str) -> None:
    """Drop the pending replay and reload the bot to resume trading."""
    logger.info("[dry-run replay] seed %s → reloading bot to resume trading", why)
    _pending.clear()
    freqtrade.state = State.RELOAD_CONFIG


def on_replay_tick(freqtrade) -> None:
    """Called each cycle while a replay is pending: reload the bot once it has finished.

    Robust to a coordinator daemon restart: ``"none"`` (the coordinator has no record of
    the job) is treated as a *finished* replay only once we have actually seen the job
    active; before that it means the freshly-submitted job was lost (e.g. the daemon was
    restarted) and we re-submit rather than silently resume without ever seeding.
    """
    if not _pending:
        return
    state = cc.bot_status(_pending["bot_id"]).get("state")
    if state in ("running", "queued", "paused"):
        _pending["seen_active"] = True  # confirmed the coordinator is tracking it
        return
    if state == "done":
        _resume(freqtrade, "done")
        return
    if state in ("error", "cancelled"):
        _resume(freqtrade, state)
        return
    if state == "none":
        if _pending.get("seen_active"):
            # We saw it run; the coordinator has since reaped it → it finished.
            _resume(freqtrade, "done (reaped)")
            return
        # Never seen active → the submit was lost (coordinator restarted). Re-submit.
        if _pending.get("resubmits", 0) < _MAX_RESUBMITS:
            _pending["resubmits"] += 1
            logger.warning(
                "[dry-run replay] coordinator has no record of %s (lost job?) — "
                "re-submitting (attempt %d/%d)",
                _pending["bot_id"],
                _pending["resubmits"],
                _MAX_RESUBMITS,
            )
            res = cc.submit(
                _pending["bot_id"],
                _pending["cmd"],
                _pending["priority"],
                _pending["progress_file"],
                _pending["db_url"],
            )
            if not res.get("ok"):
                logger.warning(
                    "[dry-run replay] re-submit refused (%s) — resuming trading", res.get("error")
                )
                _resume(freqtrade, "resubmit-refused")
            return
        logger.warning(
            "[dry-run replay] %s never started after %d re-submits — resuming trading",
            _pending["bot_id"],
            _MAX_RESUBMITS,
        )
        _resume(freqtrade, "lost")
        return
    # state is None (coordinator unreachable) or unrecognised → transient, keep waiting.


def cancel_replay(freqtrade) -> dict:
    """Cancel this bot's replay (if any) and resume it."""
    res = cc.cancel(coord_bot_id(freqtrade.config))
    if _pending:
        _pending.clear()
        freqtrade.state = State.RELOAD_CONFIG
    return res


# ── config-driven auto-launch ──────────────────────────────────────────────
_RESOLUTIONS = {"1m": 60, "5m": 300, "15m": 900}
_autolaunch_checked = False  # one-shot per process


def _parse_date(value: str) -> str:
    """Accept DD/MM/YYYY (and a few common forms) → YYYYMMDD."""
    value = str(value).strip()
    for fmt in ("%d/%m/%Y", "%Y%m%d", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    raise ValueError(f"unparseable date '{value}' (expected DD/MM/YYYY)")


def parse_autolaunch_config(cfg: dict) -> dict:
    """Validate + normalise a ``dry_run_replay`` config block. Raises ValueError if invalid."""
    if not cfg.get("automatic_launch"):
        raise ValueError("automatic_launch must be true")
    if not cfg.get("start_date"):
        raise ValueError("start_date is required")
    start = _parse_date(cfg["start_date"])
    end_raw = cfg.get("end_date") or "today"
    end = (
        datetime.now(UTC).strftime("%Y%m%d")
        if str(end_raw).lower() == "today"
        else _parse_date(end_raw)
    )
    resolution = str(cfg.get("resolution", "1m"))
    if resolution not in _RESOLUTIONS:
        raise ValueError(f"resolution must be one of {sorted(_RESOLUTIONS)} (got {resolution!r})")
    if start >= end:
        raise ValueError(f"start_date ({start}) must be before end_date ({end})")
    return {
        "timerange": f"{start}-{end}",
        "resolution_s": _RESOLUTIONS[resolution],
        "reset_db": bool(cfg.get("reset_db", False)),
    }


def _already_seeded() -> bool:
    from freqtrade.persistence import KeyValueStore

    try:
        return bool(KeyValueStore.get_string_value("ft_replay_seed"))
    except Exception:
        return False


def maybe_autolaunch_replay(freqtrade) -> bool:
    """
    One-shot at startup: if the config has ``dry_run_replay.automatic_launch`` and the bot is
    dry-run and not already seeded, launch the seed. Retries (returns False without consuming
    the one-shot) until the bot's pairlist is available. Never runs on a live bot.
    """
    global _autolaunch_checked
    if _autolaunch_checked or _pending:
        return False
    cfg = freqtrade.config.get("dry_run_replay")
    if not isinstance(cfg, dict) or not cfg.get("automatic_launch"):
        _autolaunch_checked = True
        return False
    if not freqtrade.config.get("dry_run"):
        logger.warning("[dry-run replay] dry_run_replay.automatic_launch ignored on a LIVE bot")
        _autolaunch_checked = True
        return False
    if _already_seeded():
        _autolaunch_checked = True
        return False
    try:
        params = parse_autolaunch_config(cfg)
    except ValueError as exc:
        logger.error("[dry-run replay] invalid dry_run_replay config: %s", exc)
        _autolaunch_checked = True
        return False
    pairs = list(
        getattr(freqtrade, "active_pair_whitelist", None)
        or freqtrade.config.get("exchange", {}).get("pair_whitelist")
        or []
    )
    if not pairs:
        return False  # pairlist not ready yet — retry next cycle (one-shot not consumed)
    _autolaunch_checked = True
    logger.info(
        "[dry-run replay] auto-launch: seeding %s @ %ds on %d pairs (reset_db=%s)",
        params["timerange"],
        params["resolution_s"],
        len(pairs),
        params["reset_db"],
    )
    start_replay(
        freqtrade,
        strategy=freqtrade.strategy.get_strategy_name(),
        timerange=params["timerange"],
        pairs=pairs,
        wallet=freqtrade.config.get("dry_run_wallet", 1000.0),
        sub_step=params["resolution_s"],
        reset_db=params["reset_db"],
    )
    return True
