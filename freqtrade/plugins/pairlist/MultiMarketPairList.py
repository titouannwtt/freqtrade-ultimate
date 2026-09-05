"""
Multi Market PairList provider

Combines several independent pairlist chains ("markets"), each with its own
generator + filters and its own scope (main exchange, or a specific HIP-3 DEX on
Hyperliquid), into a single ordered union whitelist. This lets a single bot trade
several unrelated markets (e.g. crypto perps on the main DEX and synthetic
stocks/commodities on a HIP-3 DEX) with each market keeping its own pairlist logic.
"""

import logging
import re
import time
from copy import deepcopy
from re import Pattern
from typing import Any

from freqtrade.constants import Config
from freqtrade.exceptions import OperationalException
from freqtrade.exchange.exchange_types import Tickers
from freqtrade.plugins.pairlist.IPairList import IPairList, PairlistParameter, SupportsBacktesting
from freqtrade.plugins.pairlistmanager import PairListManager


logger = logging.getLogger(__name__)

# Sentinel returned by `_dex_of_pair()` for a market that is confirmed HIP-3
# (`info.hip3` is truthy) but whose DEX name is not exposed by the exchange
# (`info.dex` missing/empty) and whose symbol has no `<DEX>-` prefix either.
# It can never match `_scope_matches("main", ...)` (only `None` does) nor any
# `hip3:<dex>` scope - unlike `None`, it never silently leaks into the main
# exchange's scope (see audit #4).
_UNKNOWN_HIP3_DEX = "\x00unknown-hip3-dex"


def _dex_of_pair(pair: str, market: dict[str, Any] | None) -> str | None:
    """
    Return the (lowercase) HIP-3 DEX name a pair belongs to, or None if it belongs
    to the main exchange.

    If `market` (the ccxt market dict, as found in `exchange.markets`) is available,
    it is used - this is the authoritative source (`info.hip3` / `info.dex`).
    Otherwise (e.g. when classifying a pair from configuration alone, with no
    exchange access), fall back to the `<DEX>-` symbol prefix convention used by
    Hyperliquid for HIP-3 base currencies (e.g. `XYZ-AAPL/USDC:USDC`).
    """
    if market is not None:
        info = market.get("info") or {}
        if info.get("hip3"):
            dex = info.get("dex")
            if isinstance(dex, str) and dex:
                return dex.lower()
            # HIP-3 confirmed, but the DEX name itself is missing: never treat
            # this as the main exchange (None). Fall back to the symbol prefix,
            # or a sentinel that cannot match any scope if there is no prefix.
            base = pair.split("/", 1)[0]
            return base.split("-", 1)[0].lower() if "-" in base else _UNKNOWN_HIP3_DEX
        return None

    base = pair.split("/", 1)[0]
    if "-" in base:
        return base.split("-", 1)[0].lower()
    return None


def _scope_matches(scope: str, dex: str | None) -> bool:
    """
    Check whether a pair belonging to DEX `dex` (None == main exchange) is part of
    `scope` ("all", "main" or "hip3:<dex>").
    """
    if scope == "all":
        return True
    if scope == "main":
        return dex is None
    if scope.startswith("hip3:"):
        return dex == scope.removeprefix("hip3:").lower()
    return False


def _validate_scope(scope: str, market_name: str) -> None:
    if scope != "all" and scope != "main" and not scope.startswith("hip3:"):
        raise OperationalException(
            f"MultiMarketPairList: invalid scope '{scope}' for market '{market_name}'. "
            "Use 'all', 'main' or 'hip3:<dex>'."
        )


def _find_multimarket_config(config: Config) -> dict[str, Any] | None:
    for pl in config.get("pairlists", []):
        if isinstance(pl, dict) and pl.get("method") == "MultiMarketPairList":
            return pl
    return None


def market_of_pair(config: Config, pair: str) -> str | None:
    """
    Classify `pair` into the name of the market it belongs to, purely from the
    `MultiMarketPairList` configuration (no exchange access required). This allows a
    strategy to know which market a pair belongs to without holding a reference to
    the pairlist manager - notably for pairs currently held in an open position but
    no longer part of the live whitelist (see `MultiMarketPairList.market_of()`).

    Resolution order:
    1. A pair present in a market's own (static) `pair_whitelist` belongs to that
       market. The first market (in configuration order) that lists it wins.
    2. Otherwise, the first market (in configuration order) whose `scope` (and
       optional `pair_regex`, matched case-insensitively) matches the pair wins.
    3. If nothing matches, returns None.

    :param config: Full bot configuration (or a strategy's `self.config`).
    :param pair: Pair to classify, e.g. "BTC/USDC:USDC" or "XYZ-AAPL/USDC:USDC".
    :return: Market name, or None if no configured market matches.
    """
    handler_cfg = _find_multimarket_config(config)
    if handler_cfg is None:
        return None
    markets_cfg = handler_cfg.get("markets", [])

    for market_cfg in markets_cfg:
        if pair in market_cfg.get("pair_whitelist", []):
            return market_cfg.get("name")

    for market_cfg in markets_cfg:
        scope = market_cfg.get("scope", "all")
        pair_regex = market_cfg.get("pair_regex")
        if pair_regex and not re.search(pair_regex, pair, re.IGNORECASE):
            continue
        dex = _dex_of_pair(pair, None)
        if _scope_matches(scope, dex):
            return market_cfg.get("name")

    return None


class MultiMarketPairList(IPairList):
    is_pairlist_generator = True
    # Sub-markets may combine generators with very different backtesting support
    # (e.g. MarketCapPairList is BIASED); rather than silently picking the worst
    # case, backtesting this handler is not supported.
    supports_backtesting = SupportsBacktesting.NO

    # Default "keep the last known non-empty whitelist" grace period (seconds)
    # used when a market has no `stale_grace_period` of its own and its
    # generator has no `refresh_period` to derive a default from (see audit #2).
    _DEFAULT_STALE_GRACE_PERIOD = 3600

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        markets_config: list[dict[str, Any]] = self._pairlistconfig.get("markets", [])
        if not markets_config:
            raise OperationalException(
                "MultiMarketPairList requires a non-empty 'markets' list in its configuration."
            )

        declared_dexes = {d.lower() for d in self._config.get("exchange", {}).get("hip3_dexes", [])}

        self._market_names: list[str] = []
        self._market_scopes: dict[str, str] = {}
        self._market_regex: dict[str, Pattern[str] | None] = {}
        self._sub_managers: dict[str, PairListManager] = {}
        self._stale_grace_period: dict[str, float] = {}

        seen_names: set[str] = set()
        for market_cfg in markets_config:
            name = market_cfg.get("name")
            if not name:
                raise OperationalException(
                    "MultiMarketPairList: each entry in 'markets' requires a 'name'."
                )
            if name in seen_names:
                raise OperationalException(
                    f"MultiMarketPairList: duplicate market name '{name}'. "
                    "Market names must be unique."
                )
            seen_names.add(name)

            scope = market_cfg.get("scope", "all")
            _validate_scope(scope, name)
            if declared_dexes and scope.startswith("hip3:"):
                dex_name = scope.removeprefix("hip3:").lower()
                if dex_name not in declared_dexes:
                    raise OperationalException(
                        f"MultiMarketPairList: market '{name}' uses scope '{scope}' "
                        f"but DEX '{dex_name}' is not listed in exchange.hip3_dexes "
                        f"({sorted(declared_dexes)}). Add it there, or fix the scope: "
                        "otherwise this market will silently generate an empty "
                        "pairlist forever (Hyperliquid.market_is_tradable() rejects "
                        "any HIP-3 DEX absent from hip3_dexes)."
                    )

            market_pairlists = market_cfg.get("pairlists")
            if not market_pairlists:
                raise OperationalException(
                    f"MultiMarketPairList: market '{name}' requires a non-empty 'pairlists' list."
                )

            sub_manager = self._build_sub_manager(name, market_cfg)
            first_handler = sub_manager.pairlist_handlers[0]
            if not first_handler.is_pairlist_generator:
                raise OperationalException(
                    f"MultiMarketPairList: market '{name}' - the first Pairlist Handler "
                    f"({first_handler.name}) is not a Pairlist generator. Please add a "
                    "generator (e.g. StaticPairList, VolumePairList, MarketCapPairList) "
                    "at the first position of this market's 'pairlists'."
                )

            self._market_names.append(name)
            self._market_scopes[name] = scope
            pair_regex = market_cfg.get("pair_regex")
            self._market_regex[name] = re.compile(pair_regex, re.IGNORECASE) if pair_regex else None
            self._sub_managers[name] = sub_manager
            self._stale_grace_period[name] = self._resolve_stale_grace_period(market_cfg)

        # Populated on each gen_pairlist() call.
        self._market_of: dict[str, str] = {}
        self._markets_summary: dict[str, int] = {}
        self._last_logged_summary: dict[str, int] | None = None

        # Failure/empty-market isolation state (audit #1, #2): last known
        # non-empty whitelist per market, when it started being empty, and a
        # failure counter used to throttle error logs without dropping the count.
        self._last_nonempty: dict[str, list[str]] = {}
        self._empty_since: dict[str, float] = {}
        self._market_failure_counts: dict[str, int] = {}

    def _resolve_stale_grace_period(self, market_cfg: dict[str, Any]) -> float:
        """
        Per-market "keep the last non-empty whitelist" grace period. Defaults to
        3x the market's own generator `refresh_period` when set, else a flat
        `_DEFAULT_STALE_GRACE_PERIOD` (3600s).
        """
        if "stale_grace_period" in market_cfg:
            return float(market_cfg["stale_grace_period"])
        generator_cfg = market_cfg.get("pairlists", [{}])[0]
        refresh_period = generator_cfg.get("refresh_period")
        if refresh_period is not None:
            return 3 * float(refresh_period)
        return float(self._DEFAULT_STALE_GRACE_PERIOD)

    def _build_sub_manager(self, name: str, market_cfg: dict[str, Any]) -> PairListManager:
        sub_config = deepcopy(self._config)
        sub_config["pairlists"] = deepcopy(market_cfg["pairlists"])
        sub_config["_pairlist_label"] = f"market:{name}"
        exchange_cfg = sub_config.setdefault("exchange", {})

        has_own_whitelist = "pair_whitelist" in market_cfg
        inherit_global = bool(market_cfg.get("inherit_global_whitelist", False))
        if has_own_whitelist:
            exchange_cfg["pair_whitelist"] = deepcopy(market_cfg["pair_whitelist"])
        elif not inherit_global:
            # Never silently inherit the bot-level exchange.pair_whitelist: a
            # market without its own list gets an empty one unless it explicitly
            # opts in via 'inherit_global_whitelist' (audit #6).
            exchange_cfg["pair_whitelist"] = []
        if "pair_blacklist" in market_cfg:
            exchange_cfg["pair_blacklist"] = deepcopy(market_cfg["pair_blacklist"])

        first_method = market_cfg["pairlists"][0].get("method")
        if first_method == "StaticPairList" and not has_own_whitelist and not inherit_global:
            raise OperationalException(
                f"MultiMarketPairList: market '{name}' uses StaticPairList as its "
                "generator but has no 'pair_whitelist' of its own. Add one, or set "
                "'inherit_global_whitelist: true' to explicitly use the bot-level "
                "exchange.pair_whitelist for this market (not recommended: today it "
                "silently inherited it, which is exactly the pitfall being fixed)."
            )

        dataprovider = getattr(self._pairlistmanager, "_dataprovider", None)
        try:
            return PairListManager(self._exchange, sub_config, dataprovider)
        except OperationalException as err:
            raise OperationalException(f"MultiMarketPairList: market '{name}' - {err}") from err

    def short_desc(self) -> str:
        """
        Short whitelist method description - used for startup-messages
        """
        return f"{self.name} - {len(self._market_names)} markets ({', '.join(self._market_names)})"

    @staticmethod
    def description() -> str:
        return (
            'Combine several independent pairlist chains ("markets"), each with its '
            "own generator/filters and its own scope (main exchange or a HIP-3 DEX), "
            "into a single ordered union whitelist."
        )

    @staticmethod
    def available_parameters() -> dict[str, PairlistParameter]:
        return {
            "markets": {
                "type": "list",
                "default": [],
                "description": "List of markets, each with its own pairlist chain",
                "help": (
                    "Each entry requires a unique 'name' and a non-empty 'pairlists' "
                    "sub-chain (starting with a generator). Optional keys: 'scope' "
                    "('all' (default), 'main' or 'hip3:<dex>' - 'hip3:<dex>' requires "
                    "the DEX to be listed in exchange.hip3_dexes when that key is "
                    "set), 'pair_regex' (applied to the pair symbol, case-insensitive, "
                    "unanchored re.search), 'pair_whitelist' (overrides "
                    "exchange.pair_whitelist for this market only; a StaticPairList "
                    "generator requires one, unless 'inherit_global_whitelist' is set), "
                    "'pair_blacklist' (adds to - never widens - "
                    "exchange.pair_blacklist, which always applies to the final "
                    "union), 'inherit_global_whitelist' (bool, default false: opt-in "
                    "to explicitly reuse exchange.pair_whitelist), 'stale_grace_period' "
                    "(seconds a market may return an empty pairlist before the empty "
                    "result is accepted; default 3x its generator's refresh_period, "
                    "or 3600s). This structure is not editable through the generic "
                    "'list' editor - edit it as raw JSON."
                ),
            },
            **IPairList.refresh_period_parameter(),
        }

    @property
    def needstickers(self) -> bool:
        return any(mgr.tickers_needed for mgr in self._sub_managers.values())

    def _scope_pairs(self, scope: str, pair_regex: Pattern[str] | None) -> list[str]:
        """All exchange pairs currently matching `scope` (and `pair_regex`)."""
        result = []
        for pair, market in self._exchange.markets.items():
            dex = _dex_of_pair(pair, market)
            if not _scope_matches(scope, dex):
                continue
            if pair_regex and not pair_regex.search(pair):
                continue
            result.append(pair)
        return result

    def _log_market_failure(self, name: str, err: Exception) -> None:
        """Log (throttled) that a market's sub-chain failed this cycle, and that
        its last known pairlist is being reused instead (audit #1)."""
        self._market_failure_counts[name] = self._market_failure_counts.get(name, 0) + 1
        kept = len(self._last_nonempty.get(name, []))
        self.log_once(
            f"[market:{name}] refresh failed ({err.__class__.__name__}: {err}); "
            f"reusing its last known pairlist ({kept} pairs) - this market is "
            f"degraded until it recovers ({self._market_failure_counts[name]} "
            "failures so far).",
            logger.warning,
        )

    def _refresh_market(self, name: str, tickers: Tickers) -> list[str]:
        """
        Refresh one market's own pairlist chain and return the pairs to use this
        cycle, isolating failures and applying the "keep last non-empty
        pairlist" guard. Never raises: a broken or empty market degrades to its
        last known good pairlist (or an empty one if it never had pairs, or once
        its grace period is exhausted), while the other markets keep working
        (audit #1, #2).
        """
        sub_manager = self._sub_managers[name]
        scope_pairs = self._scope_pairs(self._market_scopes[name], self._market_regex[name])

        try:
            # Restrict the sub-chain to this market's scope right after its
            # generator runs (refresh_pairlist() intersects with `pairs` before
            # running the rest of the filter chain). Reuse the tickers already
            # fetched for this cycle instead of letting every market fetch its
            # own (audit #3).
            sub_manager.refresh_pairlist(pairs=scope_pairs, tickers=tickers)
            market_pairs = list(sub_manager.whitelist)
        except Exception as err:  # one market must never take down the others
            self._log_market_failure(name, err)
            return list(self._last_nonempty.get(name, []))

        if market_pairs:
            self._last_nonempty[name] = list(market_pairs)
            self._empty_since.pop(name, None)
            return market_pairs

        previous = self._last_nonempty.get(name)
        if not previous:
            # This market never had any pairs: a genuinely empty market, not
            # something that "disappeared" - nothing to guard against.
            return market_pairs

        now = time.time()
        since = self._empty_since.setdefault(name, now)
        grace = self._stale_grace_period[name]
        stale_for = now - since
        if stale_for < grace:
            logger.warning(
                "[market:%s] generator returned no pairs; keeping %d previous "
                "pairs for now (empty for %.0fs, grace period %.0fs).",
                name,
                len(previous),
                stale_for,
                grace,
            )
            return list(previous)

        logger.warning(
            "[market:%s] generator empty for %.0fs (grace period %.0fs exhausted); "
            "accepting the empty pairlist - treating this market as genuinely "
            "having no pairs right now.",
            name,
            stale_for,
            grace,
        )
        self._last_nonempty.pop(name, None)
        self._empty_since.pop(name, None)
        return market_pairs

    def gen_pairlist(self, tickers: Tickers) -> list[str]:
        """
        Generate the pairlist: refresh each market's own pairlist chain (restricted
        to that market's scope right after its generator runs), then build the
        ordered union - a pair already seen in an earlier market is kept there and
        dropped from later markets. A market whose refresh fails, or which comes
        back empty, never takes the whole whitelist down with it - see
        `_refresh_market()`.
        :param tickers: Tickers (from exchange.get_tickers). May be cached. Shared
            as-is with every sub-manager that needs them, fetched once per cycle
            by the top-level PairListManager.
        :return: List of pairs
        """
        combined: list[str] = []
        seen: set[str] = set()
        market_of: dict[str, str] = {}
        counts: dict[str, int] = {}

        for name in self._market_names:
            market_pairs = self._refresh_market(name, tickers)

            added = 0
            for pair in market_pairs:
                if pair in seen:
                    continue
                seen.add(pair)
                combined.append(pair)
                market_of[pair] = name
                added += 1
            counts[name] = added

        # Global blacklist applies to the union, regardless of any per-market override.
        combined = self.verify_blacklist(combined, logger.info)
        market_of = {pair: market_of[pair] for pair in combined}

        self._market_of = market_of
        self._markets_summary = counts

        summary_str = ", ".join(f"{name}: {count} pairs" for name, count in counts.items())
        message = f"MultiMarketPairList - {summary_str} ({len(combined)} pairs total)"
        if counts != self._last_logged_summary:
            # A market appearing/disappearing/changing size is always worth
            # logging immediately - don't wait for the refresh_period throttle
            # (audit #9).
            logger.info(message)
            self._last_logged_summary = dict(counts)
        else:
            self.log_once(message, logger.info)

        return combined

    def market_of(self, pair: str) -> str | None:
        """
        Name of the market the given pair came from in the last generated
        whitelist. Falls back to the static configuration-based classification
        (`market_of_pair`) for a pair not currently in the whitelist - notably an
        open position's pair re-injected by the bot after the pairlist was
        generated (audit #7). Returns None only if neither resolves it.
        """
        market = self._market_of.get(pair)
        if market is not None:
            return market
        return market_of_pair(self._config, pair)

    def markets_summary(self) -> dict[str, int]:
        """Per-market pair counts from the last gen_pairlist() run."""
        return dict(self._markets_summary)
