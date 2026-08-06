"""
Fleet position attestation and break register.

What this produces
------------------
A dated, tamper-evident statement of the form: *at this instant, the exchange held
these positions, the bots' books claimed these, and here is every discrepancy* — plus a
register that follows each discrepancy from detection to resolution.

That pairing is the point. Detection alone is what we already had: drift was found when
somebody thought to look, which in practice meant days of silent accumulation. A break
register turns "we noticed and fixed it" into "nothing goes unnoticed, and nothing
stays open without being visible", which is the claim an auditor is actually testing.

Independence
------------
The attestation reads the exchange **directly**, never through the shared positions
cache the bots use. That is deliberate and worth preserving: an auditor that consumes
the same possibly-stale view as the audited system will faithfully confirm its errors.
Every incident in this codebase's history traces back to a cached position reading, so
the one process whose job is to detect them must not share that path.

Books are read from the bots' databases in read-only mode, including bots that are
currently stopped — a stopped bot still owns its positions, and treating its holdings
as unowned is how a healthy position gets closed by mistake.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from freqtrade.position_audit.ledger import AuditLedger


logger = logging.getLogger(__name__)

# Relative tolerance below which a difference is rounding, not a break.
DEFAULT_TOLERANCE = 0.01


@dataclass
class CoinLine:
    coin: str
    exchange: float
    books: float
    diff: float
    status: str  # ISO | ORPHAN | PHANTOM
    owners: list[str] = field(default_factory=list)


@dataclass
class Attestation:
    ts: str
    exchange_name: str
    bots_total: int
    bots_running: int
    unreadable_books: list[str]
    lines: list[CoinLine]

    @property
    def breaks(self) -> list[CoinLine]:
        return [b for b in self.lines if b.status != "ISO"]

    @property
    def clean(self) -> bool:
        return not self.breaks and not self.unreadable_books

    def summary(self) -> str:
        iso = len(self.lines) - len(self.breaks)
        head = f"{iso} coins ISO, {len(self.breaks)} écart(s)"
        if self.unreadable_books:
            head += f", {len(self.unreadable_books)} livre(s) illisible(s)"
        return head


def read_book(db_path: Path) -> dict[str, float]:
    """Signed open amount per coin from one bot database (read-only)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT pair, amount, is_short FROM trades WHERE is_open = 1"
        ).fetchall()
    finally:
        conn.close()
    book: dict[str, float] = {}
    for pair, amount, is_short in rows:
        coin = str(pair).split("/")[0]
        signed = -abs(float(amount or 0)) if is_short else abs(float(amount or 0))
        book[coin] = book.get(coin, 0.0) + signed
    return book


def build_attestation(
    exchange_positions: dict[str, float],
    books: dict[str, dict[str, float]],
    *,
    exchange_name: str = "",
    bots_running: int = 0,
    unreadable: list[str] | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> Attestation:
    """Compare one exchange snapshot against every book. Pure function: easy to test."""
    merged: dict[str, float] = {}
    owners: dict[str, list[str]] = {}
    for bot, book in books.items():
        for coin, signed in book.items():
            merged[coin] = merged.get(coin, 0.0) + signed
            owners.setdefault(coin, []).append(bot)

    lines: list[CoinLine] = []
    for coin in sorted(set(exchange_positions) | set(merged)):
        ex = exchange_positions.get(coin, 0.0)
        bk = merged.get(coin, 0.0)
        diff = ex - bk
        scale = max(abs(ex), abs(bk))
        if abs(diff) <= scale * tolerance:
            status = "ISO"
        elif abs(ex) > abs(bk):
            # More on the exchange than anyone claims: a position nobody pilots.
            status = "ORPHAN"
        else:
            # A book claims more than exists: no counterpart to close against.
            status = "PHANTOM"
        lines.append(CoinLine(coin, ex, bk, diff, status, sorted(owners.get(coin, []))))
    return Attestation(
        ts=datetime.now(UTC).isoformat(timespec="seconds"),
        exchange_name=exchange_name,
        bots_total=len(books),
        bots_running=bots_running,
        unreadable_books=list(unreadable or []),
        lines=lines,
    )


class BreakRegister:
    """Follows each discrepancy from first detection to resolution.

    Backed by the same append-only ledger: a break is opened once, and closed by a
    later entry rather than by editing the first. The current state is the replay of
    those events, so the history of a break survives its resolution — "it was fixed"
    and "it never happened" must not look alike.
    """

    def __init__(self, ledger: AuditLedger) -> None:
        self.ledger = ledger

    def _open_breaks(self) -> dict[str, dict[str, Any]]:
        open_now: dict[str, dict[str, Any]] = {}
        for e in self.ledger.entries():
            if e.get("event") == "break_opened":
                open_now[str(e.get("coin"))] = e
            elif e.get("event") == "break_closed":
                open_now.pop(str(e.get("coin")), None)
        return open_now

    def reconcile(self, att: Attestation) -> tuple[list[str], list[str], list[str]]:
        """Record the attestation's breaks. Returns (opened, still_open, closed)."""
        previously_open = self._open_breaks()
        current = {b.coin: b for b in att.breaks}

        opened, closed = [], []
        for coin, b in current.items():
            if coin not in previously_open:
                self.ledger.append(
                    "break_opened",
                    coin=coin,
                    status=b.status,
                    exchange=b.exchange,
                    books=b.books,
                    diff=b.diff,
                    owners=b.owners,
                )
                opened.append(coin)
        for coin in previously_open:
            if coin not in current:
                self.ledger.append("break_closed", coin=coin)
                closed.append(coin)
        still_open = [c for c in current if c in previously_open]
        return opened, still_open, closed

    def aging(self) -> list[dict[str, Any]]:
        """Open breaks with their age in hours — what escalation should key off."""
        now = datetime.now(UTC)
        out = []
        for coin, e in self._open_breaks().items():
            try:
                opened_at = datetime.fromisoformat(str(e.get("ts")))
            except ValueError:
                continue
            out.append(
                {
                    "coin": coin,
                    "status": e.get("status"),
                    "diff": e.get("diff"),
                    "opened_at": e.get("ts"),
                    "age_hours": round((now - opened_at).total_seconds() / 3600, 1),
                    "owners": e.get("owners", []),
                }
            )
        return sorted(out, key=lambda x: -x["age_hours"])


def record_attestation(ledger: AuditLedger, att: Attestation) -> dict[str, Any]:
    """Write the attestation itself, so the statement is as auditable as the events."""
    return ledger.append(
        "attestation",
        exchange=att.exchange_name,
        bots_total=att.bots_total,
        bots_running=att.bots_running,
        unreadable_books=att.unreadable_books,
        coins=len(att.lines),
        breaks=[asdict(b) for b in att.breaks],
        clean=att.clean,
    )
