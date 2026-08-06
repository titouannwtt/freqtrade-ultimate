"""
Append-only, hash-chained audit ledger.

Why a separate ledger at all
----------------------------
The trade database is a *working* record: it is rewritten continuously, corrected when
it drifts, and pruned. That is what it is for. It cannot also serve as the *audit*
record, because an audit record must answer questions the working record structurally
cannot:

* what did we hold at 00:00 UTC last Tuesday?
* who changed this position, when, and on what grounds?
* has anything in this history been altered since it was written?

So position-affecting events are additionally written here, once, and never touched
again.

Tamper-evidence
---------------
Each entry stores the hash of the previous one, so the file is a chain::

    entry[n].prev = sha256(canonical(entry[n-1]))

Altering, reordering, or deleting any entry breaks every hash after it, and
``verify()`` reports the first broken link. This does not make tampering *impossible* —
nothing local can — but it makes it *detectable*, which is the property an auditor
actually asks for. Rewriting the file consistently would require rewriting every
subsequent entry, which a partial or accidental edit will never do.

Format
------
One JSON object per line (JSONL): greppable, tailable, appendable without parsing the
file, and readable by anything. Crash-safety comes from opening in append mode and
flushing per line: a torn final line is the only possible damage, and ``verify()``
reports it rather than silently skipping it.

Deliberately not used here: a database. A ledger that can be UPDATEd is not a ledger.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

GENESIS = "0" * 64


def safe_ledger_name(name: str) -> str:
    """Filesystem-safe stem for a per-bot ledger file."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name)) or "freqtrade"


def _canonical(entry: dict[str, Any]) -> bytes:
    """Byte form used for hashing: stable key order, no incidental whitespace."""
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str).encode()


def entry_hash(entry: dict[str, Any]) -> str:
    payload = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(_canonical(payload)).hexdigest()


class AuditLedger:
    """Append-only hash chain on disk."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ----- writing ---------------------------------------------------------------

    def append(self, event: str, **payload: Any) -> dict[str, Any]:
        """Append one event. Returns the written entry.

        Never raises on a write problem: an audit trail that can take the bot down
        turns a bookkeeping outage into a trading outage. Failures are logged loudly
        and the caller carries on — a gap in the ledger is visible at verification
        time, which is the right place to notice it.
        """
        try:
            last = self.last_entry()
            entry = {
                "seq": (last["seq"] + 1) if last else 1,
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "event": event,
                "prev": last["hash"] if last else GENESIS,
                **payload,
            }
            entry["hash"] = entry_hash(entry)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return entry
        except Exception:
            logger.exception("Audit ledger write failed (event=%s) — continuing", event)
            return {}

    # ----- reading ---------------------------------------------------------------

    def entries(self):
        """Yield every well-formed entry, in order."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("%s:%d is not valid JSON — skipped", self.path, line_no)

    def last_entry(self) -> dict[str, Any] | None:
        last = None
        for e in self.entries():
            last = e
        return last

    def verify(self) -> tuple[bool, str]:
        """Walk the chain. Returns (intact, human-readable verdict)."""
        prev_hash = GENESIS
        count = 0
        expected_seq = 1
        for e in self.entries():
            count += 1
            if e.get("seq") != expected_seq:
                return (
                    False,
                    f"séquence rompue à l'entrée {count}: "
                    f"attendu {expected_seq}, trouvé {e.get('seq')}",
                )
            if e.get("prev") != prev_hash:
                return False, f"chaînage rompu à l'entrée seq={e.get('seq')}"
            if entry_hash(e) != e.get("hash"):
                return False, f"contenu altéré à l'entrée seq={e.get('seq')}"
            prev_hash = e["hash"]
            expected_seq += 1
        if count == 0:
            return True, "registre vide"
        return True, f"{count} entrées, chaîne intacte (dernier hash {prev_hash[:12]}…)"

    def replay_positions(self, as_of: str | None = None) -> dict[str, float]:
        """Rebuild the signed position per coin from recorded fills.

        This is what makes the ledger worth keeping: the working database only knows
        *now*, while an auditor asks about a date. ``as_of`` is an ISO timestamp;
        entries after it are ignored.
        """
        book: dict[str, float] = {}
        for e in self.entries():
            if e.get("event") != "fill":
                continue
            if as_of and str(e.get("ts", "")) > as_of:
                break
            coin = str(e.get("coin") or "")
            try:
                signed = float(e.get("signed_amount") or 0.0)
            except (TypeError, ValueError):
                continue
            if coin:
                book[coin] = book.get(coin, 0.0) + signed
        return {c: v for c, v in book.items() if abs(v) > 1e-12}
