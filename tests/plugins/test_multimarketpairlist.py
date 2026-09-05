# pragma pylint: disable=missing-docstring,C0103,protected-access

from copy import deepcopy
from unittest.mock import MagicMock, PropertyMock

import pytest
import time_machine

from freqtrade.configuration.config_validation import validate_config_schema
from freqtrade.constants import AVAILABLE_PAIRLISTS
from freqtrade.exceptions import ConfigurationError, OperationalException
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.plugins.pairlist.MultiMarketPairList import _dex_of_pair, market_of_pair
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

    # Since the schema fix (audit #8), an empty 'markets' list is now caught by
    # config schema validation - earlier than (but still on top of) the
    # handler's own runtime check.
    with pytest.raises(OperationalException, match=r".*non-empty.*"):
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

    # Since the schema fix (audit #8), a missing 'name' is now caught by config
    # schema validation (generic jsonschema message) before the bot even
    # starts building the handler - earlier than (but still on top of) the
    # handler's own "requires a 'name'" runtime check.
    with pytest.raises(OperationalException, match=r".*'name'.*required.*"):
        get_patched_freqtradebot(mocker, conf)


def test_multimarket_empty_pairlists_raises(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [{"name": "crypto", "pairlists": []}]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    _patch_construction_only(mocker, markets_mm)

    # Since the schema fix (audit #8), an empty sub-'pairlists' is now caught
    # by config schema validation, earlier than (but still on top of) the
    # handler's own "non-empty 'pairlists'" runtime check.
    with pytest.raises(OperationalException, match=r".*non-empty.*"):
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

    # Since the schema fix (audit #8), an invalid scope now fails the schema's
    # 'scope' pattern before the handler's own "invalid scope" runtime check
    # even runs.
    with pytest.raises(OperationalException, match=r".*(invalid scope|does not match).*"):
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


# ---------------------------------------------------------------------------
# Non-regression tests for the audit findings (2026-09-04), see AUDIT.md.
# ---------------------------------------------------------------------------


# --- #1 [BLOQUANTE] a failing sub-market must never take the others down ---


def test_multimarket_submarket_failure_is_isolated(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pair_whitelist": ["BTC/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
        {
            "name": "stocks",
            "scope": "hip3:xyz",
            "pair_whitelist": ["XYZ-NVDA/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)
    assert freqtrade.pairlists.whitelist == ["BTC/USDC:USDC", "XYZ-NVDA/USDC:USDC"]

    mmp = freqtrade.pairlists._pairlist_handlers[0]
    crypto_gen = mmp._sub_managers["crypto"].pairlist_handlers[0]
    mocker.patch.object(
        crypto_gen, "gen_pairlist", MagicMock(side_effect=OperationalException("coingecko down"))
    )

    # Must NOT raise (unlike before the fix), and must not drop the healthy market.
    freqtrade.pairlists.refresh_pairlist()
    assert freqtrade.pairlists.whitelist == ["BTC/USDC:USDC", "XYZ-NVDA/USDC:USDC"]
    assert mmp.markets_summary() == {"crypto": 1, "stocks": 1}


def test_multimarket_submarket_failure_before_any_success_yields_empty(
    mocker, default_conf, markets_mm
):
    """A market that fails on its very first refresh (never had a whitelist yet)
    contributes an empty list this cycle, instead of crashing."""
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pair_whitelist": ["BTC/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    _patch_construction_only(mocker, markets_mm)
    freqtrade = get_patched_freqtradebot(mocker, conf)
    mocker.patch.multiple(EXMS, markets=PropertyMock(return_value=markets_mm))

    mmp = freqtrade.pairlists._pairlist_handlers[0]
    crypto_gen = mmp._sub_managers["crypto"].pairlist_handlers[0]
    mocker.patch.object(
        crypto_gen, "gen_pairlist", MagicMock(side_effect=OperationalException("boom"))
    )

    freqtrade.pairlists.refresh_pairlist()
    assert freqtrade.pairlists.whitelist == []


# --- #2 [BLOQUANTE] keep the last non-empty whitelist within a grace period ---


def test_multimarket_empty_market_keeps_previous_pairlist_within_grace(
    mocker, default_conf, markets_mm
):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pair_whitelist": ["BTC/USDC:USDC", "ETH/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
            "stale_grace_period": 100,
        },
        {
            "name": "stocks",
            "scope": "hip3:xyz",
            "pair_whitelist": ["XYZ-NVDA/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]

    with time_machine.travel("2024-01-01 00:00:00 +00:00", tick=False) as traveller:
        freqtrade = _build_freqtrade(mocker, conf, markets_mm)
        assert len(freqtrade.pairlists.whitelist) == 3

        mmp = freqtrade.pairlists._pairlist_handlers[0]
        crypto_gen = mmp._sub_managers["crypto"].pairlist_handlers[0]
        mocker.patch.object(crypto_gen, "gen_pairlist", MagicMock(return_value=[]))

        # Still within the 100s grace period: the previous 2 crypto pairs are kept.
        traveller.shift(50)
        freqtrade.pairlists.refresh_pairlist()
        assert freqtrade.pairlists.whitelist == [
            "BTC/USDC:USDC",
            "ETH/USDC:USDC",
            "XYZ-NVDA/USDC:USDC",
        ]
        assert mmp.markets_summary()["crypto"] == 2

        # Past the grace period (elapsed since the market first went empty, at
        # the +50s mark, must now exceed the 100s grace): the empty result is
        # finally accepted.
        traveller.shift(110)
        freqtrade.pairlists.refresh_pairlist()
        assert freqtrade.pairlists.whitelist == ["XYZ-NVDA/USDC:USDC"]
        assert mmp.markets_summary()["crypto"] == 0


def test_multimarket_default_stale_grace_period_is_3x_refresh_period(
    mocker, default_conf, markets_mm
):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pair_whitelist": ["BTC/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList", "refresh_period": 30}],
        },
        {
            "name": "stocks",
            "scope": "hip3:xyz",
            "pair_whitelist": ["XYZ-NVDA/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)
    mmp = freqtrade.pairlists._pairlist_handlers[0]
    assert mmp._stale_grace_period["crypto"] == 90  # 3 * 30
    assert mmp._stale_grace_period["stocks"] == 3600  # no refresh_period set -> flat default


# --- #3 [IMPORTANTE] tickers fetched once per cycle, shared with sub-managers ---


def test_multimarket_tickers_fetched_once_per_cycle(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": f"m{i}",
            "scope": "main",
            "pairlists": [{"method": "VolumePairList", "number_assets": 1, "refresh_period": 1}],
        }
        for i in range(3)
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    tickers_mock = MagicMock(
        return_value={
            "BTC/USDC:USDC": {"symbol": "BTC/USDC:USDC", "quoteVolume": 100},
            "ETH/USDC:USDC": {"symbol": "ETH/USDC:USDC", "quoteVolume": 50},
        }
    )
    # _build_freqtrade() performs exactly one refresh_pairlist() cycle, spanning
    # the top-level manager and its 3 tickers-needing sub-managers. Before the
    # fix this called get_tickers() 4 times (1 + 1 per sub-manager, due to a
    # shared maxsize=1 ticker cache evicting itself); it must now be exactly 1.
    _build_freqtrade(mocker, conf, markets_mm, tickers=tickers_mock)
    assert tickers_mock.call_count == 1, (
        f"get_tickers() called {tickers_mock.call_count} times for a single cycle"
    )


# --- #4 [IMPORTANTE] a HIP-3 market without info.dex must never leak into 'main' ---


def test_dex_of_pair_hip3_without_dex_never_matches_main():
    from freqtrade.plugins.pairlist.MultiMarketPairList import _scope_matches

    assert _dex_of_pair("XYZ-AAPL/USDC:USDC", {"info": {"hip3": True}}) == "xyz"
    assert _dex_of_pair("XYZ-AAPL/USDC:USDC", {"info": {"hip3": True, "dex": None}}) == "xyz"
    assert _dex_of_pair("XYZ-AAPL/USDC:USDC", {"info": {"hip3": True, "dex": ""}}) == "xyz"

    # No prefix at all and no dex info: a sentinel that matches no scope, ever.
    dex = _dex_of_pair("WEIRD/USDC:USDC", {"info": {"hip3": True}})
    assert not _scope_matches("main", dex)
    assert not _scope_matches("hip3:xyz", dex)
    assert _scope_matches("all", dex) is True  # 'all' still matches anything


def test_multimarket_hip3_market_without_dex_field_never_leaks_into_main(mocker, default_conf):
    def _broken_market(base: str, quote: str = "USDC") -> dict:
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
            # Confirmed HIP-3, but no 'dex' key at all.
            "info": {"hip3": True},
        }

    markets = mm_markets()
    broken = _broken_market("XYZ-MSFT")
    markets[broken["symbol"]] = broken

    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pair_whitelist": ["BTC/USDC:USDC", "XYZ-MSFT/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets)

    assert freqtrade.pairlists.whitelist == ["BTC/USDC:USDC"]
    assert "XYZ-MSFT/USDC:USDC" not in freqtrade.pairlists.whitelist


# --- #7 [IMPORTANTE] market_of() falls back to market_of_pair() for pairs outside the whitelist ---


def test_multimarket_market_of_falls_back_for_pair_outside_whitelist(
    mocker, default_conf, markets_mm
):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pair_whitelist": ["BTC/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)
    mmp = freqtrade.pairlists._pairlist_handlers[0]

    # VNTL-SPACEX (a HIP-3 pair on a DEX no market declares) belongs to no
    # market's scope/whitelist: legitimately unclassifiable. (A non-hyphenated
    # pair like DOGE would match the lone 'main'-scoped market by design.)
    assert mmp.market_of("VNTL-SPACEX/USDC:USDC") is None

    # Simulate an open position on BTC after it dropped out of the CURRENT
    # whitelist (e.g. freqtradebot._refresh_active_whitelist() re-injecting an
    # open trade's pair): market_of() must still classify it via market_of_pair().
    mmp._market_of.pop("BTC/USDC:USDC", None)
    assert mmp.market_of("BTC/USDC:USDC") == "crypto"


# --- #5 [IMPORTANTE] scope 'hip3:<dex>' must be declared in exchange.hip3_dexes ---


def test_multimarket_scope_hip3_requires_declared_dex(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    conf["exchange"]["hip3_dexes"] = ["xyz"]
    markets_cfg = [
        {
            "name": "stocks",
            "scope": "hip3:cash",  # never declared
            "pair_whitelist": ["XYZ-NVDA/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    _patch_construction_only(mocker, markets_mm)

    with pytest.raises(OperationalException, match=r".*hip3_dexes.*"):
        get_patched_freqtradebot(mocker, conf)


def test_multimarket_scope_hip3_declared_dex_is_accepted(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    conf["exchange"]["hip3_dexes"] = ["xyz"]
    markets_cfg = [
        {
            "name": "stocks",
            "scope": "hip3:xyz",
            "pair_whitelist": ["XYZ-NVDA/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)
    assert freqtrade.pairlists.whitelist == ["XYZ-NVDA/USDC:USDC"]


def test_multimarket_scope_hip3_no_declared_dexes_skips_check(mocker, default_conf, markets_mm):
    """When exchange.hip3_dexes is entirely absent, the check is skipped (the
    real Hyperliquid exchange class still validates it at runtime)."""
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "stocks",
            "scope": "hip3:cash",
            "pair_whitelist": ["XYZ-NVDA/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)  # must not raise
    assert freqtrade.pairlists.whitelist == []


# --- #6 [IMPORTANTE] a StaticPairList sub-market must have its own pair_whitelist ---


def test_multimarket_static_submarket_without_whitelist_raises(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {"name": "crypto", "scope": "main", "pairlists": [{"method": "StaticPairList"}]},
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    _patch_construction_only(mocker, markets_mm)

    with pytest.raises(OperationalException, match=r".*StaticPairList.*pair_whitelist.*"):
        get_patched_freqtradebot(mocker, conf)


def test_multimarket_static_submarket_can_opt_into_inherit_global_whitelist(
    mocker, default_conf, markets_mm
):
    conf = _base_conf(default_conf)
    conf["exchange"]["pair_whitelist"] = ["BTC/USDC:USDC", "ETH/USDC:USDC"]
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "inherit_global_whitelist": True,
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)
    assert freqtrade.pairlists.whitelist == ["BTC/USDC:USDC", "ETH/USDC:USDC"]


def test_multimarket_submarket_does_not_inherit_global_whitelist_by_default(
    mocker, default_conf, markets_mm
):
    """A non-Static generator without its own pair_whitelist no longer
    silently inherits the bot-level exchange.pair_whitelist."""
    conf = _base_conf(default_conf)
    conf["exchange"]["pair_whitelist"] = ["BTC/USDC:USDC", "ETH/USDC:USDC", "SOL/USDC:USDC"]
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pairlists": [{"method": "VolumePairList", "number_assets": 5}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    tickers_mock = MagicMock(
        return_value={"BTC/USDC:USDC": {"symbol": "BTC/USDC:USDC", "quoteVolume": 100}}
    )
    freqtrade = _build_freqtrade(mocker, conf, markets_mm, tickers=tickers_mock)
    mmp = freqtrade.pairlists._pairlist_handlers[0]
    assert mmp._sub_managers["crypto"]._config["exchange"]["pair_whitelist"] == []


# --- #8 [IMPORTANTE] schema validation catches structural garbage in 'markets' ---


def test_multimarket_schema_rejects_non_list_markets(default_conf):
    conf = deepcopy(default_conf)
    conf["stake_currency"] = "USDC"
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": "not-a-list"}]
    with pytest.raises(ConfigurationError):
        validate_config_schema(conf)


def test_multimarket_schema_rejects_markets_without_name(default_conf):
    conf = deepcopy(default_conf)
    conf["stake_currency"] = "USDC"
    conf["pairlists"] = [
        {
            "method": "MultiMarketPairList",
            "markets": [{"pairlists": [{"method": "StaticPairList"}]}],
        }
    ]
    with pytest.raises(ConfigurationError):
        validate_config_schema(conf)


def test_multimarket_schema_rejects_invalid_scope_pattern(default_conf):
    conf = deepcopy(default_conf)
    conf["stake_currency"] = "USDC"
    conf["pairlists"] = [
        {
            "method": "MultiMarketPairList",
            "markets": [
                {
                    "name": "a",
                    "scope": "hip3",  # missing ':<dex>'
                    "pairlists": [{"method": "StaticPairList"}],
                }
            ],
        }
    ]
    with pytest.raises(ConfigurationError):
        validate_config_schema(conf)


def test_multimarket_schema_rejects_non_array_submarket_pairlists(default_conf):
    conf = deepcopy(default_conf)
    conf["stake_currency"] = "USDC"
    conf["pairlists"] = [
        {
            "method": "MultiMarketPairList",
            "markets": [{"name": "a", "pairlists": "nope"}],
        }
    ]
    with pytest.raises(ConfigurationError):
        validate_config_schema(conf)


def test_multimarket_schema_rejects_unknown_submarket_pairlist_method(default_conf):
    conf = deepcopy(default_conf)
    conf["stake_currency"] = "USDC"
    conf["pairlists"] = [
        {
            "method": "MultiMarketPairList",
            "markets": [{"name": "a", "pairlists": [{"method": "Bogus"}]}],
        }
    ]
    with pytest.raises(ConfigurationError):
        validate_config_schema(conf)


def test_multimarket_schema_accepts_valid_markets(default_conf):
    conf = deepcopy(default_conf)
    conf["stake_currency"] = "USDC"
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": _example_markets_config()}]
    validated = validate_config_schema(conf)
    assert len(validated["pairlists"][0]["markets"]) == 3


# --- #11 [MINEURE] pair_regex is case-insensitive ---


def test_multimarket_pair_regex_is_case_insensitive(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pair_regex": "eth/usdc:usdc",  # lowercase
            "pair_whitelist": ["ETH/USDC:USDC", "BTC/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)
    assert freqtrade.pairlists.whitelist == ["ETH/USDC:USDC"]


def test_market_of_pair_pair_regex_is_case_insensitive():
    conf = {
        "pairlists": [
            {
                "method": "MultiMarketPairList",
                "markets": [
                    {
                        "name": "crypto",
                        "scope": "main",
                        "pair_regex": "btc",
                        "pairlists": [{"method": "StaticPairList"}],
                    }
                ],
            }
        ]
    }
    assert market_of_pair(conf, "BTC/USDC:USDC") == "crypto"


# --- #13/#14 [MINEURE] available_parameters: refresh_period + pair_blacklist wording ---


def test_multimarket_available_parameters_documents_refresh_period_and_blacklist(default_conf):
    from freqtrade.plugins.pairlist.MultiMarketPairList import MultiMarketPairList

    params = MultiMarketPairList.available_parameters()
    assert "refresh_period" in params
    assert params["refresh_period"]["type"] == "number"
    # pair_blacklist only ever narrows the union - documented as "adds to",
    # not "overrides" (which would wrongly imply it can widen it).
    assert "pair_blacklist' (adds to" in params["markets"]["help"]


# --- #9 [MINEURE] logs identify the market they come from ---


def test_multimarket_sub_manager_log_label(mocker, default_conf, markets_mm):
    conf = _base_conf(default_conf)
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": _example_markets_config()}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)
    mmp = freqtrade.pairlists._pairlist_handlers[0]
    assert mmp._sub_managers["crypto"]._log_label == "market:crypto"
    assert mmp._sub_managers["stocks"]._log_label == "market:stocks"


def test_multimarket_summary_logged_unthrottled_when_counts_change(
    mocker, default_conf, markets_mm, caplog
):
    import logging as _logging

    conf = _base_conf(default_conf)
    markets_cfg = [
        {
            "name": "crypto",
            "scope": "main",
            "pair_whitelist": ["BTC/USDC:USDC", "ETH/USDC:USDC"],
            "pairlists": [{"method": "StaticPairList"}],
        },
    ]
    conf["pairlists"] = [{"method": "MultiMarketPairList", "markets": markets_cfg}]
    freqtrade = _build_freqtrade(mocker, conf, markets_mm)

    mmp = freqtrade.pairlists._pairlist_handlers[0]
    crypto_gen = mmp._sub_managers["crypto"].pairlist_handlers[0]
    caplog.clear()
    with caplog.at_level(_logging.INFO):
        mocker.patch.object(crypto_gen, "gen_pairlist", MagicMock(return_value=["BTC/USDC:USDC"]))
        freqtrade.pairlists.refresh_pairlist()
    assert any("crypto: 1 pairs" in r.message for r in caplog.records)
