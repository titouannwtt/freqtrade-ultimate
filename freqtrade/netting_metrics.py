"""
Netting impact instrumentation (fork extension).

Why
---
On Hyperliquid the wallet is NETTED per coin: several bots share one account and the
exchange only ever carries the algebraic sum of their legs. A bot that opens a long
while a sibling holds a short of the same size therefore books a trade whose position
never existed on chain. Both books then report a P&L for a position the account never
carried, and the two legs typically end their life as ``external_close`` when either
side moves first.

Nothing in the trading loop was recording that. A trade blurred by the shared wallet
looked exactly like a clean one in the database, so per-bot statistics silently mixed
the two. This module is the missing record: it does not change any decision, it only
counts and stamps, so that afterwards a trade can be classified as

* **clean**      — no sibling held the coin when it opened;
* **shadowed**   — a sibling held the OPPOSITE side, so part (or all) of this position
                   existed only in this bot's book;
* **stacked**    — a sibling held the SAME side (no netting, but shared leverage and a
                   shared liquidation price on the coin).

Everything here is failure-swallowing on purpose: an observer that can break the
trading loop is a bug, and this one runs on the entry path.
"""

from __future__ import annotations

import logging
import time
from typing import Any


logger = logging.getLogger(__name__)

# How often the periodic summary is emitted (seconds).
SUMMARY_INTERVAL_S = 3600.0

CLEAN = "clean"
SHADOWED = "shadowed"
STACKED = "stacked"


def classify(is_short: bool, siblings: list[dict[str, Any]]) -> str:
    """Classify an entry against what the fleet already holds on the coin."""
    if not siblings:
        return CLEAN
    my_side = "short" if is_short else "long"
    if any(s.get("side") != my_side for s in siblings):
        return SHADOWED
    return STACKED


class NettingMetrics:
    """Per-bot counters + the per-trade context stamped into ``trade.custom_data``.

    One instance per FreqtradeBot. Not thread-safe by design: every call site is on
    the single trading-loop thread (entry path, exit path, end of cycle).
    """

    CUSTOM_DATA_KEY = "netting"

    def __init__(self, bot_name: str = "") -> None:
        self._bot_name = bot_name
        self._pending: dict[str, dict[str, Any]] = {}
        self._started = time.monotonic()
        self._last_summary = time.monotonic()
        self.counters: dict[str, int] = {
            "entries": 0,
            "entries_clean": 0,
            "entries_shadowed": 0,
            "entries_stacked": 0,
            "entries_overridden": 0,
            "exits_external_close": 0,
            "exits_capped": 0,
        }

    # ----- entry side ---------------------------------------------------------

    def record_entry_attempt(
        self,
        pair: str,
        is_short: bool,
        siblings: list[dict[str, Any]],
        *,
        leverage: float,
        overridden: bool = False,
        blocked_reason: str = "",
    ) -> dict[str, Any]:
        """Stash the fleet context of an entry about to be sent, and log it.

        The context is kept keyed by pair until the trade row exists (the Trade object
        is only created once the order is acknowledged) — see ``take_entry_context``.
        """
        state = classify(is_short, siblings)
        ctx: dict[str, Any] = {
            "state": state,
            "side": "short" if is_short else "long",
            "leverage": float(leverage),
            "siblings": siblings,
            "opened_at": time.time(),
        }
        if overridden:
            ctx["coordination_override"] = blocked_reason or True
        self._pending[pair] = ctx
        self.counters["entries"] += 1
        self.counters[f"entries_{state}"] += 1
        if overridden:
            self.counters["entries_overridden"] += 1
        if state == CLEAN:
            logger.info("Netting: %s entry on a coin no sibling holds (clean).", pair)
        else:
            logger.warning(
                "Netting: %s %s entry on a coin already held by the fleet (%s) — %s. %s",
                pair,
                ctx["side"],
                state,
                ", ".join(f"{s['bot']} {s['side']} {s['lev']:g}x" for s in siblings),
                (
                    f"Coordination would have refused it ({blocked_reason}); "
                    "never_block_entries is on, so it was allowed and marked."
                    if overridden
                    else "Coordination allowed it."
                ),
            )
        return ctx

    def take_entry_context(self, pair: str) -> dict[str, Any] | None:
        """Pop the context recorded for ``pair`` (None when there is none)."""
        return self._pending.pop(pair, None)

    def stamp_trade(self, trade: Any, pair: str) -> None:
        """Persist the entry context on the trade (``trade_custom_data`` table).

        Chosen over an ``enter_tag`` suffix on purpose: this bot's enter_tag carries
        the market name and is what FreqUI groups per-market performance by, so
        polluting it would corrupt the very statistics this instrumentation exists to
        keep readable. Custom data survives in the DB and is served by the API
        (``/list_custom_data``), which is what the analysis script reads.
        """
        ctx = self.take_entry_context(pair)
        if ctx is None:
            return
        try:
            trade.set_custom_data(key=self.CUSTOM_DATA_KEY, value=ctx)
        except Exception:
            logger.debug("Netting: could not stamp trade %s", pair, exc_info=True)

    # ----- exit side ----------------------------------------------------------

    def record_external_close(self, pair: str) -> None:
        self.counters["exits_external_close"] += 1
        logger.info("Netting: %s closed as external_close (fleet counter).", pair)

    def record_capped_exit(self, pair: str) -> None:
        self.counters["exits_capped"] += 1

    # ----- reporting ----------------------------------------------------------

    def summary_line(self) -> str:
        c = self.counters
        hours = max((time.monotonic() - self._started) / 3600.0, 1e-9)
        return (
            f"Netting summary ({self._bot_name}, {hours:.1f}h of uptime): "
            f"{c['entries']} entries — {c['entries_clean']} clean, "
            f"{c['entries_shadowed']} shadowed (opposite side already on the coin), "
            f"{c['entries_stacked']} stacked (same side), "
            f"{c['entries_overridden']} allowed by never_block_entries; "
            f"exits: {c['exits_external_close']} external_close, "
            f"{c['exits_capped']} capped to the wallet position."
        )

    def maybe_log_summary(self, interval_s: float = SUMMARY_INTERVAL_S) -> bool:
        """Emit the periodic summary at most once per ``interval_s``. Never raises."""
        try:
            now = time.monotonic()
            if now - self._last_summary < interval_s:
                return False
            self._last_summary = now
            if not any(self.counters.values()):
                return False
            logger.info(self.summary_line())
            return True
        except Exception:
            logger.debug("Netting: summary failed", exc_info=True)
            return False
