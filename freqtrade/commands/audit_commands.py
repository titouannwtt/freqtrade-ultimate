"""
``freqtrade position-audit`` — reconcile the exchange against every bot's book.

Runs outside the bots on purpose. An auditor that shares the trader's process, cache,
or credentials path will faithfully reproduce the trader's blind spots; this command
opens its own exchange connection, reads the books read-only, and writes only to the
append-only ledger. It cannot place, cancel or modify an order.
"""

import logging
from pathlib import Path
from typing import Any

from freqtrade.configuration import setup_utils_configuration
from freqtrade.enums import RunMode
from freqtrade.position_audit import (
    AuditLedger,
    BreakRegister,
    build_attestation,
    read_book,
    record_attestation,
    run_probes,
)


logger = logging.getLogger(__name__)

# The probe needs the same exchange object the audit used; stashed rather than
# re-instantiated so we never open a second connection just to inspect capabilities.
_LAST_EXCHANGE: dict[str, Any] = {}


def _ledger_path(config: dict[str, Any]) -> Path:
    return Path(config.get("user_data_dir", "user_data")) / "audit" / "position_ledger.jsonl"


def _discover_books(config: dict[str, Any]) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Every sibling bot's open book, plus the ones we could not read.

    Unreadable books are reported rather than skipped: an audit that quietly ignores a
    book it could not open would declare the fleet balanced while a whole bot's
    positions went uncounted — the failure mode is indistinguishable from success,
    which is the one thing an audit must never allow.
    """
    from freqtrade.fleet_coordination import FleetRegistry

    books: dict[str, dict[str, float]] = {}
    unreadable: list[str] = []
    registry = FleetRegistry(config)
    for bot_name, db_path in registry.siblings():
        try:
            books[bot_name] = read_book(db_path)
        except Exception as exc:
            logger.warning("Cannot read %s (%s)", db_path, exc)
            unreadable.append(bot_name)
    # The bot whose config we were handed is not its own sibling.
    own_db = str(config.get("db_url", "")).replace("sqlite:///", "")
    if own_db:
        own_path = Path(own_db)
        if not own_path.is_absolute():
            own_path = Path.cwd() / own_db
        if own_path.exists():
            try:
                books[str(config.get("bot_name") or own_path.stem)] = read_book(own_path)
            except Exception as exc:
                logger.warning("Cannot read own book %s (%s)", own_path, exc)
                unreadable.append(str(config.get("bot_name") or own_path.stem))
    return books, unreadable


def _exchange_positions(config: dict[str, Any]) -> dict[str, float]:
    """Signed position per coin, read straight from the exchange.

    Uses a plain exchange object rather than a running bot's: the audit must not be
    served from the shared positions cache it exists to check.
    """
    from freqtrade.resolvers import ExchangeResolver

    exchange = ExchangeResolver.load_exchange(config, validate=False)
    fetch = getattr(exchange, "fetch_positions_authoritative", exchange.fetch_positions)
    positions = fetch()
    _LAST_EXCHANGE["obj"] = exchange
    _LAST_EXCHANGE["positions"] = list(positions or [])
    # HIP-3 builder dexes are separate books that only answer when named explicitly;
    # omitting them makes every builder-dex position read as an orphan.
    for dex in sorted(getattr(exchange, "_get_configured_hip3_dexes", lambda: [])() or []):
        try:
            positions = positions + exchange.fetch_positions(None, params={"dex": dex})
        except Exception as exc:
            logger.warning("Cannot read builder dex %s (%s)", dex, exc)
    out: dict[str, float] = {}
    for p in positions or []:
        try:
            contracts = float(p.get("contracts") or 0.0)
        except (TypeError, ValueError):
            continue
        if not contracts:
            continue
        coin = str(p.get("symbol") or "").split("/")[0]
        if not coin:
            continue
        signed = -contracts if str(p.get("side", "")).lower() == "short" else contracts
        out[coin] = out.get(coin, 0.0) + signed
    return out


def _print_attestation(att) -> None:
    """Human-readable statement. Kept separate so the command stays a thin orchestrator."""
    print(f"\nAttestation {att.ts} — {att.exchange_name}")
    print(f"  livres lus : {att.bots_total}   {att.summary()}")
    if att.unreadable_books:
        print(f"  ILLISIBLES : {', '.join(att.unreadable_books)}")
    if not att.breaks:
        return
    print(f"\n  {'COIN':<12}{'EXCHANGE':>14}{'LIVRES':>14}{'ÉCART':>14}  ÉTAT")
    for b in att.breaks:
        print(
            f"  {b.coin:<12}{b.exchange:>14.4f}{b.books:>14.4f}{b.diff:>14.4f}  "
            f"{b.status}  {', '.join(b.owners) or '(non réclamé)'}"
        )


def _print_register(register, att) -> None:
    opened, _still_open, closed = register.reconcile(att)
    if opened:
        print(f"\n  écarts NOUVEAUX : {', '.join(opened)}")
    if closed:
        print(f"  écarts RÉSOLUS  : {', '.join(closed)}")
    aging = register.aging()
    if not aging:
        return
    print("\n  Écarts ouverts (registre) :")
    for a in aging:
        print(
            f"    {a['coin']:<12} {a['status']:<8} écart={a['diff']:<14.4f} "
            f"ouvert depuis {a['age_hours']}h"
        )


def _print_probes(probes) -> None:
    print("\n  Hypothèses vérifiées contre l'exchange :")
    for pr in probes:
        print(f"    {pr}")


def start_position_audit(args: dict[str, Any]) -> None:
    """Entry point for ``freqtrade position-audit``."""
    # set_dry=False on purpose: utility commands normally force dry-run so they never
    # need credentials, but an audit that reads a simulated account audits nothing. It
    # also keeps sibling discovery on the live side of the fleet — a dry bot's book has
    # no counterpart on the exchange and would show up as a phantom on every coin.
    config = setup_utils_configuration(args, RunMode.UTIL_EXCHANGE, set_dry=False)
    ledger = AuditLedger(_ledger_path(config))

    if args.get("verify_only"):
        ok, verdict = ledger.verify()
        print(f"Registre : {verdict}")
        print("Intégrité : OK" if ok else "Intégrité : COMPROMISE")
        if not ok:
            raise SystemExit(2)
        return

    books, unreadable = _discover_books(config)
    positions = _exchange_positions(config)
    att = build_attestation(
        positions,
        books,
        exchange_name=str(config.get("exchange", {}).get("name", "")),
        unreadable=unreadable,
    )
    _print_attestation(att)

    record_attestation(ledger, att)
    _print_register(BreakRegister(ledger), att)

    probes = run_probes(_LAST_EXCHANGE.get("obj"), _LAST_EXCHANGE.get("positions", []))
    failed = [p for p in probes if not p.ok]
    _print_probes(probes)
    if failed:
        ledger.append(
            "contract_probe_failed",
            failures=[{"name": p.name, "detail": p.detail} for p in failed],
        )

    _ok, verdict = ledger.verify()
    print(f"\n  Registre : {verdict}")

    # Non-zero exit so a scheduler can alert without parsing the output.
    if not att.clean or failed:
        raise SystemExit(1)
