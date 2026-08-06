"""
Provable order ownership on a shared exchange account.

The problem
-----------
Several bots can trade one exchange account — the normal setup on Hyperliquid, where
positions are netted per coin account-wide. Account-scoped endpoints (``fetch_orders``,
``fetch_positions``) then return the *fleet's* activity, not this bot's, and every
attempt to answer "is this order mine?" from the data alone is an inference:

* "it is on my pair"          → false as soon as a sibling trades the same pair;
* "it is in my DB"            → useless precisely when the DB lost it, which is the
                                case the recovery path exists for;
* "it carries a strategy tag" → tags are set after the fact and can be empty.

Each of those has been observed failing in production, twice silently claiming a
sibling's fill and inflating two books with one on-chain position.

The approach
------------
Stamp ownership *into the order at creation*, where it is unambiguous, using the
exchange's own client-order-id field. Every order this bot places carries an id whose
first bytes are a stable fingerprint of the bot; ownership is then a string comparison
against data the exchange echoes back, not a guess.

    0x  a1b2c3d4  9f8e7d6c5b4a39281706f5e4
        ^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^^^
        bot        per-order randomness
        (8 hex)    (24 hex)

Why this shape: Hyperliquid requires a 128-bit hex string, so the budget is exactly 32
hex characters. Eight go to the bot fingerprint — 4 billion values, far beyond any
fleet size, and enough that an accidental collision with a foreign order is not a
practical concern. The remaining 24 (96 bits of randomness) make ids unique.

The fingerprint is derived from the bot's identity, not stored, so it survives a
restart, a DB restore, or a machine move without any state to keep in sync. Two bots
sharing a name *and* a database are the same bot as far as this module is concerned —
which is the correct answer.

Nothing here is Hyperliquid-specific except the length constraint, which is the most
restrictive of the exchanges we support; the same ids are valid elsewhere.
"""

from __future__ import annotations

import hashlib
import logging
import secrets


logger = logging.getLogger(__name__)

# Hyperliquid mandates a 128-bit hex string (0x + 32 hex chars). Other exchanges accept
# free-form client ids, so the tightest constraint sets the format for everyone.
_TOTAL_HEX = 32
_PREFIX_HEX = 8
_RANDOM_HEX = _TOTAL_HEX - _PREFIX_HEX


def bot_fingerprint(config: dict) -> str:
    """Stable 8-hex-char identity for this bot, derived (never stored).

    Built from the bot name and its database URL: two bots are "the same bot" when they
    would write to the same book, which is exactly the granularity ownership needs.
    Falls back to whatever is available so a minimal config still gets a usable — if
    less discriminating — identity rather than an exception.
    """
    parts = [
        str(config.get("bot_name") or ""),
        str(config.get("db_url") or ""),
    ]
    seed = "|".join(p for p in parts if p)
    if not seed:
        # Nothing identifying in the config. A constant fingerprint is still better
        # than none: it distinguishes "placed by a freqtrade bot" from "placed by
        # something else entirely", and the caller is warned once.
        logger.warning(
            "No bot_name or db_url in config — order ownership will not distinguish "
            "this bot from other freqtrade bots on the same account."
        )
        seed = "freqtrade-anonymous"
    return hashlib.sha256(seed.encode()).hexdigest()[:_PREFIX_HEX]


def new_client_order_id(fingerprint: str) -> str:
    """Mint a client order id carrying ``fingerprint``."""
    return "0x" + fingerprint + secrets.token_hex(_RANDOM_HEX // 2)


def is_ours(client_order_id: str | None, fingerprint: str) -> bool:
    """True when ``client_order_id`` was minted by the bot owning ``fingerprint``.

    Deliberately strict: anything malformed, absent, or foreign answers False. The
    caller's safe branch is "not mine" — refusing to claim an order costs a retry,
    while wrongly claiming one corrupts two books at once.
    """
    if not client_order_id or not fingerprint:
        return False
    cid = client_order_id.lower()
    if cid.startswith("0x"):
        cid = cid[2:]
    return len(cid) == _TOTAL_HEX and cid.startswith(fingerprint.lower())


def is_freqtrade_order(client_order_id: str | None) -> bool:
    """True when the id has the shape this module mints (any bot).

    Distinguishes "a sibling's order" from "an order placed outside freqtrade
    entirely" (exchange UI, another tool) — the two call for different handling: a
    sibling's order is somebody's responsibility, an unmanaged one is nobody's.
    """
    if not client_order_id:
        return False
    cid = client_order_id.lower()
    if cid.startswith("0x"):
        cid = cid[2:]
    if len(cid) != _TOTAL_HEX:
        return False
    try:
        int(cid, 16)
    except ValueError:
        return False
    return True
