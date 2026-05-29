"""
Hard safety gate for the replay harness.

The replay engine drives the *real* live bot loop (``FreqtradeBot.process()``).
The only thing standing between "simulation" and "places real orders on
Hyperliquid" is configuration. This module makes that boundary structural
rather than incidental: a replay simply cannot be constructed against a live
configuration.

Defense in depth — three independent layers, any one of which is sufficient:

1. ``dry_run`` is forced to ``True`` and re-asserted. Freqtrade only ever
   touches the real order endpoints when ``dry_run`` is ``False``.
2. Every credential field on the exchange config is blanked. With no API key /
   private key, no authenticated request can be signed even if (1) failed.
3. The replay exchange replaces ccxt with a mock (see ``exchange.py``), so there
   is no network client to call at all.

This module owns layers (1) and (2) and refuses to proceed if anything looks
like it could reach a live account.
"""

from __future__ import annotations

import logging

from freqtrade.enums import RunMode


logger = logging.getLogger(__name__)

# Substring every replay database URL must contain. Keeps replay results in
# their own namespace so a replay can never open (and overwrite) a live or
# normal-dry-run trade database.
REPLAY_DB_MARKER = ".replay."

# Anything that could authenticate a request to a real exchange. Hyperliquid
# signs with ``walletAddress`` + ``privateKey``; ccxt exchanges generally use
# ``key`` / ``secret`` / ``password`` / ``uid``. We blank the union.
CREDENTIAL_FIELDS = (
    "key",
    "secret",
    "password",
    "uid",
    "token",
    "walletAddress",
    "wallet_address",
    "privateKey",
    "private_key",
)


class ReplaySafetyError(RuntimeError):
    """Raised when a replay would run against anything other than a clean dry-run."""


def enforce_replay_safety(config: dict, *, namespace_db: bool = True) -> dict:
    """
    Mutate ``config`` in place so it is structurally incapable of live trading,
    raising :class:`ReplaySafetyError` if that cannot be guaranteed.

    Returns the same ``config`` for convenience.
    """
    # ── Layer 1: force dry-run ────────────────────────────────────────────
    config["dry_run"] = True
    config["runmode"] = RunMode.DRY_RUN

    # ── Layer 2: blank every credential, on every exchange config section ──
    exchange_cfg = config.get("exchange", {})
    _blank_credentials(exchange_cfg)
    # ccxt config blocks can also carry secrets
    for sub in ("ccxt_config", "ccxt_sync_config", "ccxt_async_config"):
        if isinstance(exchange_cfg.get(sub), dict):
            _blank_credentials(exchange_cfg[sub])

    # Disable outbound channels for the replay *process* itself. Viewing
    # results in FreqUI is a separate, normal dry-run process pointed at the
    # replay DB — it does not run here.
    config.pop("telegram", None)
    config.pop("api_server", None)
    config.pop("discord", None)
    config.pop("webhook", None)

    # ── Database ──────────────────────────────────────────────────────────
    # Default: namespace into *.replay.sqlite so we can never touch a live/normal
    # DB. Seed mode (namespace_db=False) deliberately targets the caller-supplied
    # dry-run DB to populate it — the dry-run guard lives in the caller.
    if namespace_db:
        config["db_url"] = _namespace_db_url(config.get("db_url"))
    elif not str(config.get("db_url", "")).startswith("sqlite:///"):
        raise ReplaySafetyError(
            f"Replay only writes to sqlite databases (got {config.get('db_url')!r})."
        )

    # ── Final gate: re-verify the invariants we just established ──────────
    _assert_invariants(config, require_marker=namespace_db)
    logger.info(
        "Replay safety enforced: dry_run=True, credentials blanked, db_url=%s",
        config["db_url"],
    )
    return config


def _blank_credentials(section: dict) -> None:
    for field in CREDENTIAL_FIELDS:
        if section.get(field):
            section[field] = ""


def _namespace_db_url(db_url: str | None) -> str:
    """
    Force the trade DB into the replay namespace and refuse non-sqlite URLs.

    A replay must never be able to open a production database engine
    (Postgres/MySQL), so only sqlite is allowed here.
    """
    default = f"sqlite:///user_data/tradesv3{REPLAY_DB_MARKER}sqlite"
    if not db_url:
        return default

    if not db_url.startswith("sqlite:///"):
        raise ReplaySafetyError(
            f"Replay only writes to sqlite databases (got {db_url!r}). "
            "Refusing to run against a production database engine."
        )

    if REPLAY_DB_MARKER in db_url:
        return db_url

    # Inject the marker before the final extension: foo.sqlite -> foo.replay.sqlite
    if db_url.endswith(".sqlite"):
        return db_url[: -len(".sqlite")] + REPLAY_DB_MARKER + "sqlite"
    return db_url + REPLAY_DB_MARKER + "sqlite"


def _assert_invariants(config: dict, *, require_marker: bool = True) -> None:
    if config.get("dry_run") is not True:
        raise ReplaySafetyError("dry_run must be True for a replay — refusing to start.")

    if config.get("runmode") != RunMode.DRY_RUN:
        raise ReplaySafetyError("runmode must be DRY_RUN for a replay — refusing to start.")

    exchange_cfg = config.get("exchange", {})
    leftover = [f for f in CREDENTIAL_FIELDS if exchange_cfg.get(f)]
    for sub in ("ccxt_config", "ccxt_sync_config", "ccxt_async_config"):
        if isinstance(exchange_cfg.get(sub), dict):
            leftover += [f"{sub}.{f}" for f in CREDENTIAL_FIELDS if exchange_cfg[sub].get(f)]
    if leftover:
        raise ReplaySafetyError(
            f"Exchange credentials still present after blanking: {leftover}. "
            "Refusing to start a replay that could reach a live account."
        )

    db_url = config.get("db_url", "")
    if require_marker and REPLAY_DB_MARKER not in db_url:
        raise ReplaySafetyError(
            f"Replay db_url must contain {REPLAY_DB_MARKER!r} (got {db_url!r})."
        )
