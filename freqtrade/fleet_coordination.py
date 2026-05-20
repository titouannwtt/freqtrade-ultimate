"""
Fleet position coordination (fork extension).

Lets several bots that share the same wallet / dry-run group avoid stacking
positions on the same pair, and lets them reconcile leverage when they do.

Coordination source of truth is the *trade database of sibling bots* in the
same environment. The exchange is only used as a non-blocking final cross-check
(a spot balance does not necessarily map to an open trade, so it cannot be
authoritative).

What counts as a sibling is controlled by ``position_coordination.scope``:

* ``wallet``   — (default) only bots trading the *same wallet/account* on the
                 exchange coordinate. Bots split across distinct wallets of the
                 same exchange (separate API keys / wallet addresses,
                 sub-accounts, or several accounts) stay independent. Wallet
                 identity is auto-detected from the credentials already in the
                 config (Hyperliquid wallet address or API key), or set
                 explicitly with ``position_coordination.account``.
* ``exchange`` — every bot on the exchange (same ``dry_run``) coordinates,
                 regardless of wallet (fleet-wide "one position per coin").

Three modes (config key ``position_coordination.mode``):

* ``off``    — no coordination (upstream behaviour).
* ``compat`` — a pair may be shared only if the existing sibling position is on
               the *same side*; leverage is reconciled per ``leverage_policy``.
* ``strict`` — a pair already held by a sibling can never be entered.

An ``flock`` + short-lived *intent marker* serialises concurrent entries on the
same (group, pair) so two bots cannot both pass the check on the same candle
close (first to acquire the lock wins).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import rapidjson

from freqtrade.misc import deep_merge_dicts


logger = logging.getLogger(__name__)

# Modes
MODE_OFF = "off"
MODE_COMPAT = "compat"
MODE_STRICT = "strict"
VALID_MODES = (MODE_OFF, MODE_COMPAT, MODE_STRICT)

# Leverage reconciliation policies (compat mode, futures only)
LEV_LOWEST = "lowest"
LEV_HIGHEST = "highest"
LEV_KEEP = "keep"
LEV_BLOCK = "block"
VALID_LEVERAGE_POLICIES = (LEV_LOWEST, LEV_HIGHEST, LEV_KEEP, LEV_BLOCK)

# Coordination scope — which bots count as siblings on a given exchange.
# ``wallet``   : only bots that trade the *same wallet/account* coordinate. Bots
#                on distinct wallets of the same exchange (different API keys /
#                wallet addresses, sub-accounts, or simply several accounts) are
#                independent and never block one another.
# ``exchange`` : every bot on the exchange coordinates regardless of wallet
#                (fleet-wide "one position per coin" across wallets).
SCOPE_WALLET = "wallet"
SCOPE_EXCHANGE = "exchange"
VALID_SCOPES = (SCOPE_WALLET, SCOPE_EXCHANGE)

# Exchange cross-check behaviour
EXCHANGE_WARN = "warn"
EXCHANGE_BLOCK = "block"

# Tolerant parse mode for sibling configs (freqtrade allows comments / trailing
# commas; access files where the wallet identity lives often use them).
_PARSE_MODE = rapidjson.PM_COMMENTS | rapidjson.PM_TRAILING_COMMAS

# Intent markers older than this (seconds) are ignored / cleaned up. Covers the
# window between "a sibling decided to open" and "its trade is committed to DB",
# and protects against stale markers if a bot crashes mid-entry.
INTENT_TTL_S = 180.0

_WARN_THROTTLE_S = 900.0


@dataclass
class SiblingPosition:
    bot_name: str
    is_short: bool
    leverage: float


@dataclass
class Decision:
    allow: bool
    leverage: float
    reason: str = ""
    # True when the resolved leverage differs from what already sits on the
    # shared coin and a real exchange leverage change is therefore required.
    leverage_changed: bool = False


def _db_url_to_path(db_url: str, config_dir: Path | None) -> Path | None:
    """Resolve a freqtrade sqlite ``db_url`` into a filesystem path."""
    if not db_url or not db_url.startswith("sqlite:///"):
        return None
    rest = db_url[len("sqlite:///") :]
    if rest.startswith("/"):
        # sqlite:////abs/path -> absolute
        return Path(rest)
    # Relative: bots are launched from the repo root, so resolve against cwd
    # first, then fall back to the parent of the config directory.
    candidates = [Path.cwd() / rest]
    if config_dir is not None:
        candidates.append(config_dir.parent / rest)
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def _resolve_config_fields(path: Path, depth: int = 0) -> dict[str, Any]:
    """Resolve a config file and its ``add_config_files`` bases (silently).

    Mirrors freqtrade's merge precedence (the main file overrides its bases) for
    the few fields coordination needs, without the per-file INFO logging that
    ``load_from_files`` emits on every discovery refresh.
    """
    if depth > 5:
        return {}
    try:
        with path.open("r") as fh:
            data = rapidjson.load(fh, parse_mode=_PARSE_MODE)
    except (OSError, ValueError):
        return {}
    merged: dict[str, Any] = {}
    parent = path.resolve().parent
    for sub in data.get("add_config_files", []) or []:
        deep_merge_dicts(_resolve_config_fields(parent / sub, depth + 1), merged)
    deep_merge_dicts(data, merged)
    return merged


def _account_fingerprint(merged: dict[str, Any]) -> str:
    """Stable, non-secret identifier of the wallet/account a bot trades on.

    Two bots trading the *same* wallet share a fingerprint; bots on distinct
    wallets of the same exchange get distinct ones. An explicit
    ``position_coordination.account`` label wins; otherwise it is derived from the
    credentials already in the (merged) config — the Hyperliquid wallet address or
    the exchange API key — hashed so no secret is ever stored, logged, or written
    to a lock/marker path.
    """
    coord = merged.get("position_coordination", {}) or {}
    label = coord.get("account")
    if label:
        return "label:" + _safe_name(str(label))
    ex = merged.get("exchange", {}) or {}
    raw = ex.get("walletAddress") or ex.get("wallet_address") or ex.get("key") or ""
    raw = str(raw).strip().lower()
    if not raw:
        return "default"
    return "acct:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


class FleetRegistry:
    """Discovers sibling bots in the same environment.

    The registry is normally the directory of config files the bot was launched
    with (the configs *are* the registry). ``registry`` may also be an explicit
    list of sqlite paths if the user prefers to pilot it by hand.
    """

    def __init__(self, config: dict[str, Any], cache_ttl: float = 300.0) -> None:
        self._self_bot = config.get("bot_name", "")
        self._self_exchange = str(config.get("exchange", {}).get("name", "")).lower()
        self._self_dry_run = bool(config.get("dry_run", False))
        coord = config.get("position_coordination", {}) or {}
        self._registry_setting = coord.get("registry")
        self._exclude = set(coord.get("exclude", []) or [])
        self._scope = coord.get("scope", SCOPE_WALLET)
        if self._scope not in VALID_SCOPES:
            self._scope = SCOPE_WALLET
        self._self_account = _account_fingerprint(config)

        config_files = config.get("config_files") or []
        self._config_dir: Path | None = (
            Path(config_files[0]).resolve().parent if config_files else None
        )

        self._cache_ttl = cache_ttl
        self._cache_time = 0.0
        self._cached: list[tuple[str, Path]] = []

    def _discover_from_dir(self, directory: Path) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = []
        for cfg_path in sorted(directory.glob("*.json")):
            name = cfg_path.name
            if name.startswith("_") or "example" in name or "template" in name:
                continue
            if name in self._exclude:
                continue
            merged = _resolve_config_fields(cfg_path)
            if not merged:
                continue

            bot_name = merged.get("bot_name", "")
            if not bot_name or bot_name == self._self_bot or bot_name in self._exclude:
                continue
            exchange = str(merged.get("exchange", {}).get("name", "")).lower()
            dry_run = bool(merged.get("dry_run", False))
            if exchange != self._self_exchange or dry_run != self._self_dry_run:
                continue
            # Wallet scope: only bots on the *same* wallet/account are siblings.
            if self._scope == SCOPE_WALLET and _account_fingerprint(merged) != self._self_account:
                continue
            db_path = _db_url_to_path(merged.get("db_url", ""), self._config_dir)
            if db_path is None:
                continue
            out.append((bot_name, db_path))
        return out

    def _discover_from_list(self, paths: list[str]) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = []
        for p in paths:
            path = _db_url_to_path(p, self._config_dir) if p.startswith("sqlite:") else Path(p)
            out.append((path.stem, path))
        return out

    def siblings(self) -> list[tuple[str, Path]]:
        """Return ``[(bot_name, db_path), ...]`` for same-environment siblings."""
        now = time.monotonic()
        if self._cached and now - self._cache_time < self._cache_ttl:
            return self._cached

        try:
            if isinstance(self._registry_setting, list):
                result = self._discover_from_list(self._registry_setting)
            else:
                directory = (
                    Path(self._registry_setting).resolve()
                    if isinstance(self._registry_setting, str)
                    else self._config_dir
                )
                result = self._discover_from_dir(directory) if directory else []
        except Exception as e:
            logger.warning("Coordination: fleet discovery failed (%s) — assuming no siblings", e)
            result = []

        self._cached = result
        self._cache_time = now
        return result


class SiblingPositionReader:
    """Reads open trades on a pair from sibling sqlite DBs (read-only)."""

    @staticmethod
    def read_open_on_pair(db_path: Path, pair: str) -> list[tuple[bool, float]]:
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        except sqlite3.Error as e:
            logger.debug("Coordination: cannot open sibling DB %s (%s)", db_path, e)
            return []
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 2000")
            rows = conn.execute(
                "SELECT is_short, leverage FROM trades WHERE is_open = 1 AND pair = ?",
                (pair,),
            ).fetchall()
        except sqlite3.Error as e:
            logger.debug("Coordination: cannot read sibling DB %s (%s)", db_path, e)
            return []
        finally:
            conn.close()
        return [(bool(r[0]), float(r[1] or 1.0)) for r in rows]


class PositionCoordinator:
    """Decision engine + anti-race locking for fleet position coordination."""

    def __init__(self, config: dict[str, Any]) -> None:
        coord = config.get("position_coordination", {}) or {}
        self.mode = coord.get("mode", MODE_COMPAT)
        if self.mode not in VALID_MODES:
            logger.warning("Coordination: invalid mode '%s', falling back to 'compat'", self.mode)
            self.mode = MODE_COMPAT
        self.leverage_policy = coord.get("leverage_policy", LEV_KEEP)
        if self.leverage_policy not in VALID_LEVERAGE_POLICIES:
            logger.warning(
                "Coordination: invalid leverage_policy '%s', falling back to 'keep'",
                self.leverage_policy,
            )
            self.leverage_policy = LEV_KEEP
        self.scope = coord.get("scope", SCOPE_WALLET)
        if self.scope not in VALID_SCOPES:
            logger.warning("Coordination: invalid scope '%s', falling back to 'wallet'", self.scope)
            self.scope = SCOPE_WALLET
        self.exchange_check = coord.get("exchange_check", EXCHANGE_WARN)

        self.enabled = self.mode != MODE_OFF
        self._self_bot = config.get("bot_name", "")
        self._is_futures = str(config.get("trading_mode", "spot")).lower() == "futures"
        self._env = "dry" if config.get("dry_run", False) else "live"
        self._exchange_name = str(config.get("exchange", {}).get("name", "")).lower()

        self._registry = FleetRegistry(config)

        # Locks and intent markers are scoped to the same group as sibling
        # discovery, so two bots on distinct wallets never share a lock or read
        # each other's intent markers under ``wallet`` scope.
        self._account = _account_fingerprint(config)
        group_token = _safe_name(self._account) if self.scope == SCOPE_WALLET else "_exchange"
        user_dir = Path(config.get("user_data_dir", "user_data"))
        self._coord_dir = user_dir / "coordination" / self._exchange_name / self._env / group_token
        self._intent_dir = self._coord_dir / "intents"
        self._last_warn: dict[str, float] = {}

    # ----- discovery / reading -------------------------------------------------

    def _sibling_positions(self, pair: str) -> list[SiblingPosition]:
        positions: list[SiblingPosition] = []
        for bot_name, db_path in self._registry.siblings():
            for is_short, leverage in SiblingPositionReader.read_open_on_pair(db_path, pair):
                positions.append(SiblingPosition(bot_name, is_short, leverage))
        positions.extend(self._read_intents(pair))
        return positions

    def _read_intents(self, pair: str) -> list[SiblingPosition]:
        """Read other bots' fresh intent markers (covers the pre-commit window)."""
        out: list[SiblingPosition] = []
        if not self._intent_dir.exists():
            return out
        prefix = f"{_safe_name(pair)}__"
        now = time.time()
        for marker in self._intent_dir.glob(f"{prefix}*.json"):
            try:
                data = json.loads(marker.read_text())
            except (OSError, ValueError):
                continue
            if data.get("bot") == self._self_bot:
                continue
            if now - float(data.get("ts", 0)) > INTENT_TTL_S:
                try:
                    marker.unlink()
                except OSError:
                    pass
                continue
            out.append(
                SiblingPosition(
                    data.get("bot", "?"), bool(data.get("is_short", False)),
                    float(data.get("leverage", 1.0)),
                )
            )
        return out

    # ----- decision -----------------------------------------------------------

    def evaluate(self, pair: str, is_short: bool, my_leverage: float) -> Decision:
        """Decide whether this bot may open ``pair`` and at which leverage."""
        if not self.enabled:
            return Decision(True, my_leverage)

        siblings = self._sibling_positions(pair)
        if not siblings:
            return Decision(True, my_leverage)

        # Spot: no side / no leverage — only the strict block is meaningful.
        if not self._is_futures:
            if self.mode == MODE_STRICT:
                return Decision(
                    False, my_leverage,
                    f"{pair} already held by sibling {siblings[0].bot_name} (strict, spot)",
                )
            return Decision(True, my_leverage)

        # Futures
        if self.mode == MODE_STRICT:
            return Decision(
                False, my_leverage,
                f"{pair} already held by sibling {siblings[0].bot_name} (strict mode)",
            )

        # compat: opposite side on a shared wallet would net against the sibling.
        wanted_short = is_short
        for s in siblings:
            if s.is_short != wanted_short:
                return Decision(
                    False, my_leverage,
                    f"{pair} held by sibling {s.bot_name} on the OPPOSITE side "
                    f"({'short' if s.is_short else 'long'}) — netting risk",
                )

        sib_levs = [s.leverage for s in siblings]
        existing = sib_levs[0]
        if self.leverage_policy == LEV_BLOCK:
            if any(int(lv) != int(my_leverage) for lv in sib_levs):
                return Decision(
                    False, my_leverage,
                    f"{pair} leverage mismatch (mine {my_leverage:g}x vs sibling {existing:g}x) "
                    f"and leverage_policy=block",
                )
            return Decision(True, my_leverage)
        if self.leverage_policy == LEV_LOWEST:
            target = min([my_leverage, *sib_levs])
        elif self.leverage_policy == LEV_HIGHEST:
            target = max([my_leverage, *sib_levs])
        else:  # keep — adopt the leverage already on the coin
            target = existing

        return Decision(
            True, float(target), leverage_changed=int(target) != int(existing),
        )

    # ----- intent markers -----------------------------------------------------

    def mark_intent(self, pair: str, is_short: bool, leverage: float) -> None:
        if not self.enabled:
            return
        try:
            self._intent_dir.mkdir(parents=True, exist_ok=True)
            path = self._intent_dir / f"{_safe_name(pair)}__{_safe_name(self._self_bot)}.json"
            path.write_text(
                json.dumps(
                    {
                        "bot": self._self_bot,
                        "is_short": bool(is_short),
                        "leverage": float(leverage),
                        "ts": time.time(),
                    }
                )
            )
        except OSError as e:
            logger.debug("Coordination: could not write intent marker (%s)", e)

    def clear_intent(self, pair: str) -> None:
        if not self.enabled:
            return
        path = self._intent_dir / f"{_safe_name(pair)}__{_safe_name(self._self_bot)}.json"
        try:
            path.unlink()
        except OSError:
            pass

    # ----- anti-race lock -----------------------------------------------------

    @contextmanager
    def entry_lock(self, pair: str):
        """Serialise concurrent entries on the same (group, pair). No-op if disabled."""
        if not self.enabled:
            yield
            return
        try:
            self._coord_dir.mkdir(parents=True, exist_ok=True)
            lock_path = self._coord_dir / f"{_safe_name(pair)}.lock"
            handle = lock_path.open("w")
        except OSError as e:
            logger.debug("Coordination: could not create lock for %s (%s) — proceeding", pair, e)
            yield
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    # ----- logging helper -----------------------------------------------------

    def warn_throttled(self, pair: str, msg: str, *args: object) -> None:
        now = time.monotonic()
        if now - self._last_warn.get(pair, 0.0) >= _WARN_THROTTLE_S:
            logger.warning(msg, *args)
            self._last_warn[pair] = now
