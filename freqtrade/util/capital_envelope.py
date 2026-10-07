"""Enveloppe de capital par position (garde-fou DCA).

Partage entre la boucle live (``FreqtradeBot._position_within_capital_envelope``) et le
backtest (``Backtesting._check_adjust_trade_for_candle``). Avant cette factorisation le
garde-fou n'existait QUE dans la boucle live : un backtest finançait donc des echelles DCA
que le live refuse, et toute validation d'un reglage de ``max_position_stake_ratio`` par
backtest etait structurellement fausse.
"""

import logging
from datetime import UTC, datetime
from typing import Any


logger = logging.getLogger(__name__)

VALID_BASES = ("allocated", "deployable")


def resolve_envelope_basis(config: dict[str, Any], wallets: Any) -> tuple[float, str] | None:
    """Return ``(allocated_amount, basis_name)``, or None when the guard does not apply."""
    basis = config.get("max_position_stake_basis", "allocated")
    if basis not in VALID_BASES:
        logger.warning(
            "max_position_stake_basis=%r is not one of %s; falling back to 'allocated'.",
            basis,
            " / ".join(VALID_BASES),
        )
        basis = "allocated"

    allocated: float | None
    if basis == "deployable":
        try:
            allocated = wallets.get_total_stake_amount()
        except Exception:
            return None
    else:
        allocated = config.get("available_capital")
        if not allocated:
            # No explicit allocation: fall back to what the bot may actually deploy.
            try:
                allocated = wallets.get_total_stake_amount()
            except Exception:
                return None
    if not allocated or allocated <= 0:
        return None
    return float(allocated), basis


def position_within_capital_envelope(
    config: dict[str, Any],
    wallets: Any,
    trade: Any,
    added_stake: float,
    warned: set | None = None,
) -> bool:
    """True when ``added_stake`` keeps this single position inside the bot's envelope.

    ``warned`` is an optional set used to rate-limit the refusal log to once per trade per
    hour: the guard is re-evaluated every cycle while the DCA trigger stays true, and one
    fleet bot logged the same line 7848 times in 36 h before the limiter existed.
    """
    ratio = config.get("max_position_stake_ratio", 1.0)
    if not ratio or ratio <= 0:
        return True
    resolved = resolve_envelope_basis(config, wallets)
    if resolved is None:
        return True
    allocated, basis = resolved

    projected = (getattr(trade, "stake_amount", 0.0) or 0.0) + added_stake
    if projected <= allocated * ratio:
        return True

    if warned is not None:
        key = (getattr(trade, "id", 0) or 0, int(datetime.now(UTC).timestamp() // 3600))
        if key in warned:
            return False
        warned.add(key)
    logger.warning(
        "%s: safety order of %.2f refused - it would take this single position to "
        "%.2f of margin, past %.0f%% of the bot's %.2f %s capital. "
        "Raise max_position_stake_ratio to allow it (further identical refusals "
        "for this trade are logged at most hourly).",
        getattr(trade, "pair", "?"),
        added_stake,
        projected,
        ratio * 100,
        allocated,
        basis,
    )
    return False
