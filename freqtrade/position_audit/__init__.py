"""
Position audit: an evidence trail for what the bots hold, independent of the bots.

Three pieces, each answering a question the trade database cannot:

* :mod:`~freqtrade.position_audit.ledger` — an append-only, hash-chained record of
  position-affecting events. Answers "what happened, and has this record been
  altered?", and can rebuild the book as of any past instant.
* :mod:`~freqtrade.position_audit.attestation` — a dated statement comparing the
  exchange against every bot's book, read independently of the cache the bots use.
  Answers "do our books match reality, right now?".
* ``BreakRegister`` — follows each discrepancy from detection to resolution with an
  age, so nothing stays open unnoticed. Answers "and when they did not match, what
  did you do about it?".

Nothing here can place, cancel or modify an order.
"""

from freqtrade.position_audit.attestation import (
    Attestation,
    BreakRegister,
    CoinLine,
    build_attestation,
    read_book,
    record_attestation,
)
from freqtrade.position_audit.contract_probe import ProbeResult, run_probes
from freqtrade.position_audit.ledger import AuditLedger, safe_ledger_name


__all__ = [
    "Attestation",
    "AuditLedger",
    "BreakRegister",
    "CoinLine",
    "build_attestation",
    "read_book",
    "record_attestation",
    "ProbeResult",
    "run_probes",
    "safe_ledger_name",
]
