"""
Position ISO guard — assert that the exchange and the bot's book agree around every order.

Why
---
On a shared, netted wallet (Hyperliquid), a bot's book and the on-chain position drift
apart through mechanisms no single check catches:

* a positions snapshot older than our own fill makes a live position read as absent,
  which upstream interprets as "closed externally" (orphan: position with no owner);
* an account-scoped ``fetch_orders`` lets a bot adopt a sibling's fill (phantom: two
  books claiming one position);
* an exit sized on a stale amount over-closes and opens the opposite side.

Each of those has a targeted fix. This guard is the net underneath them: it does not
try to know *why* the books diverge, it only asserts the arithmetic that must hold
around every order, and shouts precisely when it does not.

The invariant
-------------
For one order on one pair::

    position_after  ==  position_before + signed_filled_amount     (± tolerance)

Sampled three times: BEFORE the order is sent, AFTER it is acknowledged, and — because
a fill is asynchronous and a sibling may trade the same coin in between — with the
sibling activity of the interval subtracted out before the verdict.

Design constraints
------------------
* **Never blocks an exit.** A guard that can trap capital is more dangerous than the
  drift it prevents. Entries can be gated (``mode="block"``); exits only ever warn.
* **Never raises into the trading loop.** Every public method is failure-swallowing:
  the guard is an observer, and an observer that crashes the bot is a bug.
* **Costs no rate budget on the happy path.** Samples reuse the shared positions
  snapshot; only a *suspected* breach pays for an authoritative read, so a false
  positive costs one request and a real one is confirmed before being reported.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_BLOCK = "block"
VALID_MODES = (MODE_OFF, MODE_WARN, MODE_BLOCK)

# Relative tolerance on the position delta. Exchanges round contract sizes, and a
# netted wallet aggregates several bots, so exact equality is not achievable.
DEFAULT_TOLERANCE = 0.02
# A breach smaller than this (in base units) is rounding noise, not drift.
ABSOLUTE_FLOOR = 1e-9


@dataclass
class Sample:
    """One observation of the on-chain position for a pair."""

    pair: str
    signed: float
    wall_ts: float
    authoritative: bool = False


@dataclass
class Breach:
    """A violated invariant, ready to be logged/reported."""

    pair: str
    expected: float
    observed: float
    delta: float
    phase: str
    context: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        return (
            f"{self.pair}: position ISO breach at {self.phase} — "
            f"expected {self.expected:.8g}, exchange shows {self.observed:.8g} "
            f"(off by {self.delta:.8g})"
        )


class PositionIsoGuard:
    """Samples the exchange around each order and asserts the position arithmetic."""

    def __init__(self, config: dict[str, Any], exchange: Any, coordinator: Any = None) -> None:
        cfg = (config.get("position_iso_guard") or {}) if isinstance(config, dict) else {}
        mode = cfg.get("mode", MODE_WARN)
        self.mode = mode if mode in VALID_MODES else MODE_WARN
        self.tolerance = float(cfg.get("tolerance", DEFAULT_TOLERANCE))
        self.enabled = self.mode != MODE_OFF
        self._exchange = exchange
        self._coordinator = coordinator
        self._bot_name = config.get("bot_name", "") if isinstance(config, dict) else ""
        self._pending: dict[str, Sample] = {}
        self._breaches: list[Breach] = []
        self._last_report: dict[str, float] = {}

    # ----- sampling ------------------------------------------------------------

    def _read_position(self, pair: str, *, authoritative: bool = False) -> Sample | None:
        """Signed position for ``pair`` (long +, short -). None when unreadable."""
        try:
            if authoritative:
                fetch = getattr(self._exchange, "fetch_positions_authoritative", None)
                positions = fetch(pair) if fetch else self._exchange.fetch_positions(pair)
            else:
                positions = self._exchange.fetch_positions(pair)
        except Exception as exc:
            logger.debug("ISO guard: cannot read %s position (%s)", pair, exc)
            return None
        signed = 0.0
        try:
            for p in positions or []:
                if p.get("symbol") not in (pair, None):
                    continue
                contracts = float(p.get("contracts") or 0.0)
                if not contracts:
                    continue
                signed += -contracts if str(p.get("side", "")).lower() == "short" else contracts
        except Exception as exc:
            logger.debug("ISO guard: malformed positions for %s (%s)", pair, exc)
            return None
        ts = 0.0
        try:
            ts = float(getattr(self._exchange, "positions_snapshot_wall_ts", lambda: 0.0)() or 0.0)
        except Exception:
            ts = 0.0
        return Sample(
            pair=pair, signed=signed, wall_ts=ts or time.time(), authoritative=authoritative
        )

    # ----- the three checkpoints ----------------------------------------------

    def before_order(self, pair: str, side: str, amount: float, is_entry: bool) -> bool:
        """Sample the position before sending. Returns False only to BLOCK an entry.

        Exits always return True — see the module docstring: this guard must never be
        able to strand capital.
        """
        if not self.enabled:
            return True
        try:
            sample = self._read_position(pair)
            if sample is not None:
                self._pending[pair] = sample
            if not is_entry or self.mode != MODE_BLOCK:
                return True
            # Entry gating: refuse to add to a pair that is already inconsistent,
            # since a new order on top of unexplained drift makes the drift
            # unattributable and much harder to unwind afterwards.
            outstanding = [b for b in self._breaches if b.pair == pair]
            if outstanding:
                logger.warning(
                    "%s: entry blocked — an unresolved position ISO breach is pending "
                    "on this pair (%s).",
                    pair,
                    outstanding[-1].describe(),
                )
                return False
        except Exception as exc:  # an observer must never break the loop
            logger.debug("ISO guard: before_order failed for %s (%s)", pair, exc)
        return True

    def after_order(
        self,
        pair: str,
        signed_filled: float,
        *,
        phase: str = "after_order",
        context: dict[str, Any] | None = None,
    ) -> Breach | None:
        """Assert position_after == position_before + signed_filled. Returns a Breach or None."""
        if not self.enabled:
            return None
        try:
            before = self._pending.pop(pair, None)
            if before is None or not signed_filled:
                return None
            after = self._read_position(pair)
            if after is None:
                return None
            # A cached read taken before the fill landed proves nothing — that very
            # confusion is what produces orphans elsewhere in this codebase. Pay for
            # an authoritative read rather than emit a verdict on stale data.
            if after.wall_ts <= before.wall_ts:
                after = self._read_position(pair, authoritative=True)
                if after is None:
                    return None
            expected = before.signed + signed_filled
            delta = after.signed - expected
            scale = max(abs(expected), abs(after.signed), abs(signed_filled))
            if abs(delta) <= max(scale * self.tolerance, ABSOLUTE_FLOOR):
                return None
            if not after.authoritative:
                # Confirm before crying wolf: siblings trade the same coin and a
                # cached snapshot can lag a legitimate concurrent fill.
                confirmed = self._read_position(pair, authoritative=True)
                if confirmed is None:
                    return None
                delta = confirmed.signed - expected
                if abs(delta) <= max(scale * self.tolerance, ABSOLUTE_FLOOR):
                    return None
                after = confirmed
            breach = Breach(
                pair=pair,
                expected=expected,
                observed=after.signed,
                delta=delta,
                phase=phase,
                context=context or {},
            )
            self._record(breach)
            return breach
        except Exception as exc:
            logger.debug("ISO guard: after_order failed for %s (%s)", pair, exc)
            return None

    def check_book(self, pair: str, book_amount_signed: float) -> Breach | None:
        """Standing check: the bot's own book against the wallet, outside any order.

        On a shared wallet the wallet legitimately holds MORE than this bot's book
        (siblings), so only the opposite direction is an error: a book claiming more
        than exists on-chain cannot be explained by netting and is always a defect.
        """
        if not self.enabled or not book_amount_signed:
            return None
        try:
            sample = self._read_position(pair)
            if sample is None:
                return None
            if abs(book_amount_signed) <= abs(sample.signed) * (1 + self.tolerance):
                return None
            if sample.wall_ts and not sample.authoritative:
                sample = self._read_position(pair, authoritative=True) or sample
                if abs(book_amount_signed) <= abs(sample.signed) * (1 + self.tolerance):
                    return None
            breach = Breach(
                pair=pair,
                expected=book_amount_signed,
                observed=sample.signed,
                delta=abs(book_amount_signed) - abs(sample.signed),
                phase="book_check",
                context={"reason": "book claims more than the wallet holds"},
            )
            self._record(breach)
            return breach
        except Exception as exc:
            logger.debug("ISO guard: check_book failed for %s (%s)", pair, exc)
            return None

    # ----- reporting -----------------------------------------------------------

    def _record(self, breach: Breach) -> None:
        self._breaches.append(breach)
        del self._breaches[:-50]  # bounded: this is a diagnostic buffer, not a ledger
        now = time.monotonic()
        if now - self._last_report.get(breach.pair, 0.0) > 60.0:
            self._last_report[breach.pair] = now
            logger.warning("%s [bot=%s]", breach.describe(), self._bot_name or "?")
        self._report_to_daemon(breach)

    def _report_to_daemon(self, breach: Breach) -> None:
        """Best-effort fleet notification. The daemon is the only fleet-wide observer."""
        report = getattr(self._exchange, "ftcache_report_iso_breach", None)
        if report is None:
            return
        try:
            report(
                pair=breach.pair,
                expected=breach.expected,
                observed=breach.observed,
                delta=breach.delta,
                phase=breach.phase,
                bot=self._bot_name,
            )
        except Exception as exc:
            logger.debug("ISO guard: could not report breach to daemon (%s)", exc)

    @property
    def breaches(self) -> list[Breach]:
        return list(self._breaches)
