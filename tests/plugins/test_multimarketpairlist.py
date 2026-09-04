# pragma pylint: disable=missing-docstring,C0103,protected-access

from copy import deepcopy
from unittest.mock import MagicMock, PropertyMock

import pytest
import time_machine

from freqtrade.configuration.config_validation import validate_config_schema
from freqtrade.constants import AVAILABLE_PAIRLISTS
from freqtrade.exceptions import OperationalException
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.plugins.pairlist.MultiMarketPairList import market_of_pair
from tests.conftest import EXMS, get_patched_freqtradebot


def _market(base: str, quote: str = "USDC", dex: str | None = None) -> dict:
    return {
        "id": f"{base}{quote}".lower(),
        "symbol": f"{base}/{quote}:{quote}",
        "base": base,
        "quote": quote,
        "active": True,
        "spot": True,
        "swap": False,
        "linear": None,
        "type": "spot",
        "precision": {"price": 8, "amount": 8, "cost": 8},
        "limits": {
            "amount": {"min": 0.01, "max": 100000000},
            "price": {"min": None, "max": 500000},
            "cost": {"min": 0.0001, "max": 500000},
            "leverage": {"min": 1.0, "max": 2.0},
        },
        "info": {"hip3": True, "dex": dex} if dex else {},
    }


def mm_markets() -> dict:
    markets = {}
    for base in ["BTC", "ETH", "SOL", "DOGE"]:
        m = _market(base)
        markets[m["symbol"]] = m
    for base in ["XYZ-AAPL", "XYZ-GOOGL", "XYZ-NVDA", "XYZ-TSLA", "XYZ-GOLD"]:
        m = _market(base, dex="xyz")
        markets[m["symbol"]] = m
    # A HIP-3 DEX not referenced by any configured market - must never leak in.
    m = _market("VNTL-SPACEX", dex="vntl")
    markets[m["symbol"]] = m
    return markets


@pytest.fixture(scope="function")
def markets_mm():
    return mm_markets()


def _base_conf(default_conf):
    conf = deepcopy(default_conf)
    conf["runmode"] = "dry_run"
    conf["stake_currency"] = "USDC"
    conf["exchange"]["pair_whitelist"] = []
    conf["exchange"]["pair_blacklist"] = []
    return conf


def _example_markets_config() -> list[dict]:
    return [
        {
            "name": "crypto",
            "scope": "main",
            "pairlists": [{"method": "StaticPairList"}],
            "pair_whitelist": ["BTC/USDC:USDC", "ETH/USDC:USDC", "SOL/USDC:USDC"],
        },
        {
            "name": "stocks",
            "scope": "hip3:xyz",
            "pair_whitelist": ["XYZ-NVDA/USDC:USDC", "XYZ-GOOGL/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
        {
            "name": "mat",
            "scope": "hip3:xyz",
            "pair_whitelist": ["XYZ-GOLD/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]


def _build_freqtrade(mocker, conf, markets, tickers=None) -> FreqtradeBot:
    """
    Build a patched FreqtradeBot with the given custom markets/tickers in effect.

    get_patched_freqtradebot() internally re-patches EXMS.markets with its own
    generic default (overriding any 'markets' mock applied before it), but leaves
    exchange_has/get_option/get_tickers alone. So: patch the construction-time
    checks (exchange_has/get_option/get_tickers) before building the bot - some
    Pairlist Handlers (e.g. VolumePairList) validate them in __init__ - then
    re-apply the custom 'markets' afterwards before refreshing the pairlist.
    """
    patches = {
        "exchange_has": MagicMock(return_value=True),
        "get_option": MagicMock(return_value=True),
    }
    if tickers is not None:
        patches["get_tickers"] = tickers
    mocker.patch.multiple(EXMS, **patches)

    freqtrade = get_patched_freqtradebot(mocker, conf)

    mocker.patch.multiple(EXMS, markets=PropertyMock(return_value=markets))
    freqtrade.pairlists.refresh_pairlist()
    return freqtrade


def _patch_construction_only(mocker, markets):
    """Patch the exchange before constructing a bot expected to raise at construction time."""
    mocker.patch.multiple(
        EXMS,
        markets=PropertyMock(return_value=markets),
        exchange_has=MagicMock(return_value=True),
    )


def test_multimarket_in_available_pairlists():
    assert "MultiMarketPairList" in AVAILABLE_PAIRLISTS


def test_multimarket_union_order_and_dedup(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": _example_markets_config()}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)

    assert freqtrade.pairlists.whitelist == [
        "BTC/USDC:USDC",
        "ETH/USDC:USDC",
        "SOL/USDC:USDC",
        "XYZ-NVDA/USDC:USDC",
        "XYZ-GOOGL/USDC:USDC",
        "XYZ-GOLD/USDC:USDC",
    ]

    mmp = freqtrade.pairlists._pairlist_handlers[0]
    assert mmp.markets_summary() == {"crypto": 3, "stocks": 2, "mat": 1}
    assert mmp.market_of("BTC/USDC:USDC") == "crypto"
    assert mmp.market_of("XYZ-NVDA/USDC:USDC") == "stocks"
    assert mmp.market_of("XYZ-GOLD/USDC:USDC") == "mat"


def test_multimarket_duplicate_pair_kept_in_first_market(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "first",
            "scope": "hip3:xyz",
            "pair_whitelist": ["XYZ-NVDA/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
        {
            "name": "second",
            "scope": "hip3:xyz",
            "pair_whitelist": ["XYZ-NVDA/USDC:USDC", "XYZ-GOOGL/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)

    assert freqtrade.pairlists.whitelist == ["XYZ-NVDA/USDC:USDC", "XYZ-GOOGL/USDC:USDC"]
    mmp = freqtrade.pairlists._pairlist_handlers[0]
    # The pair is kept in the first market that produced it.
    assert mmp.market_of("XYZ-NVDA/USDC:USDC") == "first"
    assert mmp.market_of("XYZ-GOOGL/USDC:USDC") == "second"
    assert mmp.markets_summary() == {"first": 1, "second": 1}


def test_multimarket_scope_filters_generator_leak(mocker, default_conf, markets_mm):
    """
    A market scoped to 'main' must never let a HIP-3 pair through, even if its own
    generator (here simulated via a polluted static pair_whitelist) returns one.
    A market scoped to a HIP-3 dex must not be polluted by other dexes either.
    """
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            # Simulates a generator that (incorrectly) also matched a HIP-3 pair.
            "pair_whitelist": ["BTC/USDC:USDC", "XYZ-AAPL/USDC:USDC", "VNTL-SPACEX/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
        {
            "name": "stocks",
            "scope": "hip3:xyz",
            # Simulates a generator that also matched a pair from another dex.
            "pair_whitelist": ["XYZ-NVDA/USDC:USDC", "VNTL-SPACEX/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)

    # VNTL-SPACEX (dex 'vntl') is not in scope for either market and must be dropped.
    # XYZ-AAPL (dex 'xyz') is not in scope for 'main' and must be dropped there.
    assert freqtrade.pairlists.whitelist == ["BTC/USDC:USDC", "XYZ-NVDA/USDC:USDC"]


def test_multimarket_pair_regex(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "stocks",
            "scope": "hip3:xyz",
            "pair_regex": r"^XYZ-(NVDA|GOOGL)/",
            "pair_whitelist": ["XYZ-NVDA/USDC:USDC", "XYZ-GOOGL/USDC:USDC", "XYZ-TSLA/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)

    assert freqtrade.pairlists.whitelist == ["XYZ-NVDA/USDC:USDC", "XYZ-GOOGL/USDC:USDC"]


def test_multimarket_local_and_global_blacklist(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    conf["exchange"]["pair_blacklist"] = ["ETH/USDC:USDC"]
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pair_whitelist": ["BTC/USDC:USDC", "ETH/USDC:USDC", "SOL/USDC:USDC"],
            # Local blacklist overrides the market's own view.
            "pair_blacklist": ["SOL/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)

    # SOL removed by the market's local blacklist, ETH removed by the global blacklist.
    assert freqtrade.pairlists.whitelist == ["BTC/USDC:USDC"]


def test_multimarket_subchain_generator_plus_filter(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pair_whitelist": [
                "BTC/USDC:USDC",
                "ETH/USDC:USDC",
                "SOL/USDC:USDC",
                "DOGE/USDC:USDC",
            ],
            "pairlists": [
                {"method": "StaticPairList"},
                {"method": "OffsetFilter", "offset": 1},
            ],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)

    # OffsetFilter with offset=1 drops the first pair of the sub-chain.
    assert freqtrade.pairlists.whitelist == ["ETH/USDC:USDC", "SOL/USDC:USDC", "DOGE/USDC:USDC"]


def test_multimarket_empty_markets_raises(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": []}]
    _patch_construction_only(mocker, markets_mm)

    with pytest.raises(OperationalException, match=r".*non-empty 'markets'.*"):
        get_patched_freqtradebot(mocker, conf)


def test_multimarket_duplicate_name_raises(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "pair_whitelist": ["BTC/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
        {
            "name": "crypto",
            "pair_whitelist": ["ETH/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    _patch_construction_only(mocker, markets_mm)

    with pytest.raises(OperationalException, match=r".*[Dd]uplicate market name.*"):
        get_patched_freqtradebot(mocker, conf)


def test_multimarket_non_generator_first_handler_raises(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "pair_whitelist": ["BTC/USDC:USDC"],
            "pairlists": [{"method": "OffsetFilter", "offset": 0}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    _patch_construction_only(mocker, markets_mm)

    with pytest.raises(OperationalException, match=r".*not a Pairlist generator.*"):
        get_patched_freqtradebot(mocker, conf)


def test_multimarket_missing_name_raises(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [{"pairlists": [{"method": "StaticPairList"}]}]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    _patch_construction_only(mocker, markets_mm)

    with pytest.raises(OperationalException, match=r".*requires a 'name'.*"):
        get_patched_freqtradebot(mocker, conf)


def test_multimarket_empty_pairlists_raises(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [{"name": "crypto", "pairlists": []}]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    _patch_construction_only(mocker, markets_mm)

    with pytest.raises(OperationalException, match=r".*non-empty 'pairlists'.*"):
        get_patched_freqtradebot(mocker, conf)


def test_multimarket_invalid_scope_raises(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "bogus",
            "pair_whitelist": ["BTC/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    _patch_construction_only(mocker, markets_mm)

    with pytest.raises(OperationalException, match=r".*invalid scope.*"):
        get_patched_freqtradebot(mocker, conf)


def test_multimarket_refresh_period_independent_per_market(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    tickers_mock = MagicMock(
        return_value={
            "BTC/USDC:USDC": {"symbol": "BTC/USDC:USDC", "quoteVolume": 100},
            "ETH/USDC:USDC": {"symbol": "ETH/USDC:USDC", "quoteVolume": 50},
        }
    )
    markets_cfg = [
        {
            "name": "fast",
            "scope": "main",
            "pairlists": [{"method": "VolumePairList", "number_assets": 1, "refresh_period": 1}],
        },
        {
            "name": "slow",
            "scope": "main",
            "pairlists": [
                {"method": "VolumePairList", "number_assets": 1, "refresh_period": 86400}
            ],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]

    with time_machine.travel("2024-01-01 00:00:00 +00:00", tick=False) as traveller:
        freqtrade = _build_freqtrade(mocker, conf, markets_mm, tickers=tickers_mock)
        mmp = freqtrade.pairlists._pairlist_handlers[0]
        fast_handler = mmp._sub_managers["fast"].pairlist_handlers[0]
        slow_handler = mmp._sub_managers["slow"].pairlist_handlers[0]

        assert len(fast_handler._pair_cache) == 1
        assert len(slow_handler._pair_cache) == 1

        # Move well past the fast market's refresh_period, but not the slow one's.
        traveller.shift(10)
        fast_handler._pair_cache.clear()
        slow_cache_before = dict(slow_handler._pair_cache)

        freqtrade.pairlists.refresh_pairlist()
        # Fast market regenerated (cache repopulated after being cleared).
        assert len(fast_handler._pair_cache) == 1
        # Slow market's cache entry is untouched (still cached, same content).
        assert dict(slow_handler._pair_cache) == slow_cache_before


def test_multimarket_needstickers_union(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pair_whitelist": ["BTC/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
        {
            "name": "volume",
            "scope": "main",
            "pairlists": [{"method": "VolumePairList", "number_assets": 1}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    tickers_mock = MagicMock(
        return_value={"BTC/USDC:USDC": {"symbol": "BTC/USDC:USDC", "quoteVolume": 1}}
    )
    freqtrade = _build_freqtrade(mocker, conf, markets_mm, tickers=tickers_mock)

    mmp = freqtrade.pairlists._pairlist_handlers[0]
    assert mmp.needstickers is True


def test_multimarket_schema_validation_accepted(default_conf):
    conf = deepcopy(default_conf)
    conf["stake_currency"] = "USDC"
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": _example_markets_config()}]

    validated = validate_config_schema(conf)
    assert validated["pairlists"][0]["method"] == "MultiMarketPairList"
    assert len(validated["pairlists"][0]["markets"]) == 3


# --- market_of_pair() module function (pure config, no exchange access) ---


def _config_with_multimarket() -> dict:
    return {"pairlists": [{"method": "MultiMarketPairList", "markets": _example_markets_config()}]}


def test_market_of_pair_static_whitelist_membership():
    conf = _config_with_multimarket()
    assert market_of_pair(conf, "XYZ-NVDA/USDC:USDC") == "stocks"
    assert market_of_pair(conf, "XYZ-GOLD/USDC:USDC") == "mat"
    assert market_of_pair(conf, "BTC/USDC:USDC") == "crypto"


def test_market_of_pair_scope_fallback():
    conf = _config_with_multimarket()
    # ADA/USDC:USDC is not in any static pair_whitelist, but matches the 'crypto'
    # market's 'main' scope via the naming-convention fallback (no exchange access).
    assert market_of_pair(conf, "ADA/USDC:USDC") == "crypto"
    # XYZ-MSFT is not declared anywhere, but matches the 'stocks' market's
    # 'hip3:xyz' scope (first market with that scope wins over 'mat').
    assert market_of_pair(conf, "XYZ-MSFT/USDC:USDC") == "stocks"


def test_market_of_pair_no_match_returns_none():
    conf = _config_with_multimarket()
    assert market_of_pair(conf, "VNTL-SPACEX/USDC:USDC") is None


def test_market_of_pair_no_multimarket_configured():
    assert market_of_pair({"pairlists": [{"method": "StaticPairList"}]}, "BTC/USDC:USDC") is None
    assert market_of_pair({}, "BTC/USDC:USDC") is None
