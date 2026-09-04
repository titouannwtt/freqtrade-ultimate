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
from copy import deepcopy
from re import Pattern
from typing import Any

from freqtrade.constants import Config
from freqtrade.exceptions import OperationalException
from freqtrade.exchange.exchange_types import Tickers
from freqtrade.plugins.pairlist.IPairList import IPairList, PairlistParameter, SupportsBacktesting
from freqtrade.plugins.pairlistmanager import PairListManager


logger = logging.getLogger(__name__)


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
            return dex.lower() if isinstance(dex, str) else dex
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
    the pairlist manager.

    Resolution order:
    1. A pair present in a market's own (static) `pair_whitelist` belongs to that
       market. The first market (in configuration order) that lists it wins.
    2. Otherwise, the first market (in configuration order) whose `scope` (and
       optional `pair_regex`) matches the pair wins.
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
        if pair_regex and not re.search(pair_regex, pair):
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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        markets_config: list[dict[str, Any]] = self._pairlistconfig.get("markets", [])
        if not markets_config:
            raise OperationalException(
                "MultiMarketPairList requires a non-empty 'markets' list in its configuration."
            )

        self._market_names: list[str] = []
        self._market_scopes: dict[str, str] = {}
        self._market_regex: dict[str, Pattern[str] | None] = {}
        self._sub_managers: dict[str, PairListManager] = {}

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
            self._market_regex[name] = re.compile(pair_regex) if pair_regex else None
            self._sub_managers[name] = sub_manager

        # Populated on each gen_pairlist() call.
        self._market_of: dict[str, str] = {}
        self._markets_summary: dict[str, int] = {}

    def _build_sub_manager(self, name: str, market_cfg: dict[str, Any]) -> PairListManager:
        sub_config = deepcopy(self._config)
        sub_config["pairlists"] = deepcopy(market_cfg["pairlists"])
        exchange_cfg = sub_config.setdefault("exchange", {})
        if "pair_whitelist" in market_cfg:
            exchange_cfg["pair_whitelist"] = deepcopy(market_cfg["pair_whitelist"])
        if "pair_blacklist" in market_cfg:
            exchange_cfg["pair_blacklist"] = deepcopy(market_cfg["pair_blacklist"])

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
                    "('all' (default), 'main' or 'hip3:<dex>'), 'pair_regex' (applied "
                    "to the pair symbol), 'pair_whitelist'/'pair_blacklist' (override "
                    "exchange.pair_whitelist/pair_blacklist for this market only)."
                ),
            },
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

    def gen_pairlist(self, tickers: Tickers) -> list[str]:
        """
        Generate the pairlist: refresh each market's own pairlist chain (restricted
        to that market's scope right after its generator runs), then build the
        ordered union - a pair already seen in an earlier market is kept there and
        dropped from later markets.
        :param tickers: Tickers (from exchange.get_tickers). May be cached.
        :return: List of pairs
        """
        combined: list[str] = []
        seen: set[str] = set()
        market_of: dict[str, str] = {}
        counts: dict[str, int] = {}

        for name in self._market_names:
            sub_manager = self._sub_managers[name]
            scope_pairs = self._scope_pairs(self._market_scopes[name], self._market_regex[name])

            # Restrict the sub-chain to this market's scope right after its
            # generator runs (refresh_pairlist() intersects with `pairs` before
            # running the rest of the filter chain).
            sub_manager.refresh_pairlist(pairs=scope_pairs)

            added = 0
            for pair in sub_manager.whitelist:
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
        self.log_once(
            f"MultiMarketPairList - {summary_str} ({len(combined)} pairs total)", logger.info
        )

        return combined

    def market_of(self, pair: str) -> str | None:
        """
        Name of the market the given pair came from in the last generated whitelist,
        or None if the pair is not currently in the whitelist.
        """
        return self._market_of.get(pair)

    def markets_summary(self) -> dict[str, int]:
        """Per-market pair counts from the last gen_pairlist() run."""
        return dict(self._markets_summary)
