"""Tests pour la fusion `hippo_original_multi`.

`hippo_original_multi` reunit dans une seule classe / un seul bot les trois
strategies `hippo_original` (crypto), `hippo_original_stocks` (actions HIP-3
xyz) et `hippo_original_mat` (matieres premieres HIP-3 xyz), chacune gardant
EXACTEMENT ses signaux et parametres sur ses paires, avec un capital commun.

Ce fichier verifie :
(a) le chargement via StrategyResolver ;
(b) l'ISOMORPHISME par marche : sur des donnees reelles, `enter_long` /
    `exit_long` produits par la fusion pour une paire d'un marche donne sont
    identiques a ceux de la strategie source de ce marche ;
(c) `market_of` : dispatch via une config MultiMarketPairList, et fallback
    (STOCKS_PAIRS / MAT_PAIRS / prefixe XYZ-) quand elle est absente ;
(d) par marche : `leverage()`, `min_roi_reached` (meme mouvement de prix,
    meme decision quel que soit le levier ; alignement avec
    `hippo_original.min_roi_reached` a levier 1 sur une grille), `custom_exit`
    et `adjust_trade_position` avec un trade MagicMock ;
(e) `custom_stake_amount` par marche, egale a celle de la source
    correspondante pour un meme proposed_stake.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest
from pandas.testing import assert_series_equal

from freqtrade.resolvers import StrategyResolver
from tests.conftest import get_default_conf, patch_exchange


REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
STRATEGY_DIR = REPO_ROOT / "user_data" / "strategies"
DATA_DIR = REPO_ROOT / "user_data" / "data" / "hyperliquid" / "futures"

N_CANDLES = 3000

# marche -> (strategie source, [(fichier de donnees, paire correspondante)])
MARKET_CASES = {
    "crypto": (
        "hippo_original",
        [
            ("ETH_USDC_USDC-5m-futures.feather", "ETH/USDC:USDC"),
            ("SOL_USDC_USDC-5m-futures.feather", "SOL/USDC:USDC"),
        ],
    ),
    "stocks": (
        "hippo_original_stocks",
        [
            ("XYZ-NVDA_USDC_USDC-5m-futures.feather", "XYZ-NVDA/USDC:USDC"),
            ("XYZ-AAPL_USDC_USDC-5m-futures.feather", "XYZ-AAPL/USDC:USDC"),
        ],
    ),
    "mat": (
        "hippo_original_mat",
        [
            ("XYZ-GOLD_USDC_USDC-5m-futures.feather", "XYZ-GOLD/USDC:USDC"),
            ("XYZ-CL_USDC_USDC-5m-futures.feather", "XYZ-CL/USDC:USDC"),
        ],
    ),
}

PAIR_BY_MARKET = {
    "crypto": "ETH/USDC:USDC",
    "stocks": "XYZ-NVDA/USDC:USDC",
    "mat": "XYZ-GOLD/USDC:USDC",
}


def _load_strategy(mocker, testdatadir, strategy_name):
    conf = get_default_conf(testdatadir)
    conf["strategy_path"] = str(STRATEGY_DIR)
    conf["strategy"] = strategy_name
    conf["timeframe"] = "5m"
    conf.pop("minimal_roi", None)
    patch_exchange(mocker)
    return StrategyResolver.load_strategy(conf)


def _load_candles(filename: str, n: int = N_CANDLES) -> pd.DataFrame:
    df = pd.read_feather(DATA_DIR / filename)
    return df.tail(n).reset_index(drop=True)


# ── (a) chargement ───────────────────────────────────────────────────────────


def test_la_fusion_se_charge(mocker, testdatadir):
    strategy = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    assert strategy.__class__.__name__ == "hippo_original_multi"
    assert strategy.timeframe == "5m"
    assert strategy.market_of("XYZ-NVDA/USDC:USDC") == "stocks"
    assert strategy.market_of("XYZ-GOLD/USDC:USDC") == "mat"
    assert strategy.market_of("ETH/USDC:USDC") == "crypto"


# ── (b) isomorphisme par marche ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "market,data_file,pair",
    [
        (market, data_file, pair)
        for market, (_src, cases) in MARKET_CASES.items()
        for data_file, pair in cases
    ],
)
def test_isomorphisme_par_marche(mocker, testdatadir, market, data_file, pair):
    src_name = MARKET_CASES[market][0]
    source = _load_strategy(mocker, testdatadir, src_name)
    multi = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    assert multi.market_of(pair) == market

    df = _load_candles(data_file)
    meta = {"pair": pair}

    df_src = source.advise_indicators(df.copy(), meta)
    df_src = source.advise_entry(df_src, meta)
    df_src = source.advise_exit(df_src, meta)

    df_multi = multi.advise_indicators(df.copy(), meta)
    df_multi = multi.advise_entry(df_multi, meta)
    df_multi = multi.advise_exit(df_multi, meta)

    assert_series_equal(
        df_src["enter_long"].fillna(0).astype(float),
        df_multi["enter_long"].fillna(0).astype(float),
        check_names=False,
    )
    assert_series_equal(
        df_src["exit_long"].fillna(0).astype(float),
        df_multi["exit_long"].fillna(0).astype(float),
        check_names=False,
    )
    # enter_tag de la fusion doit porter le nom du marche sur chaque entree.
    entered = df_multi["enter_long"].fillna(0).astype(float) > 0
    if entered.any():
        assert (df_multi.loc[entered, "enter_tag"] == market).all()


# ── (c) market_of : config MultiMarketPairList et fallback ─────────────────


def test_market_of_avec_config_multimarket(mocker, testdatadir):
    conf = get_default_conf(testdatadir)
    conf["strategy_path"] = str(STRATEGY_DIR)
    conf["strategy"] = "hippo_original_multi"
    conf["timeframe"] = "5m"
    conf.pop("minimal_roi", None)
    conf["pairlists"] = [
        {
            "method": "MultiMarketPairList",
            "markets": [
                {
                    "name": "crypto",
                    "scope": "main",
                    "pairlists": [{"method": "MarketCapPairList"}],
                },
                {
                    "name": "stocks",
                    "scope": "hip3:xyz",
                    "pair_whitelist": ["XYZ-NVDA/USDC:USDC", "XYZ-AAPL/USDC:USDC"],
                    "pairlists": [{"method": "StaticPairList"}],
                },
                {
                    "name": "mat",
                    "scope": "hip3:xyz",
                    "pair_whitelist": ["XYZ-GOLD/USDC:USDC", "XYZ-CL/USDC:USDC"],
                    "pairlists": [{"method": "StaticPairList"}],
                },
            ],
        }
    ]
    patch_exchange(mocker)
    strategy = StrategyResolver.load_strategy(conf)

    assert strategy.market_of("XYZ-NVDA/USDC:USDC") == "stocks"
    assert strategy.market_of("XYZ-GOLD/USDC:USDC") == "mat"
    # Paire absente de toute liste statique -> marche par defaut (sans liste
    # statique = "crypto", pilote par MarketCapPairList).
    assert strategy.market_of("BTC/USDC:USDC") == "crypto"
    assert strategy.market_of("SOME/OTHER:PAIR") == "crypto"


def test_market_of_fallback_sans_multimarket(mocker, testdatadir):
    # get_default_conf ne pose PAS de MultiMarketPairList -> fallback sur
    # STOCKS_PAIRS / MAT_PAIRS.
    strategy = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    for pair in strategy.STOCKS_PAIRS:
        assert strategy.market_of(pair) == "stocks"
    for pair in strategy.MAT_PAIRS:
        assert strategy.market_of(pair) == "mat"
    assert strategy.market_of("ETH/USDC:USDC") == "crypto"
    # Paire au prefixe HIP-3 absente des deux listes : NE DOIT JAMAIS recevoir
    # les parametres crypto (bug corrige suite a l'audit du 2026-09-04). Deux
    # candidats HIP-3 connus (stocks/mat) -> ambigu -> premier candidat par
    # defaut pour ne pas planter une position deja ouverte, mais jamais
    # "crypto", et confirm_trade_entry refuse toute NOUVELLE entree dessus
    # (voir test_confirm_trade_entry_refuse_paire_hip3_ambigue).
    assert strategy.market_of("XYZ-UNKNOWN/USDC:USDC") != "crypto"
    assert strategy.market_of("XYZ-UNKNOWN/USDC:USDC") == "stocks"


def test_market_of_hip3_pair_absente_config_multimarket(mocker, testdatadir):
    """Meme garde que ci-dessus, mais avec une config MultiMarketPairList :
    market_of_pair() resout deja les paires HIP-3 sans pair_whitelist statique
    via le scope (le premier marche 'hip3:xyz' declare), donc jamais
    "crypto" - _resolve_unclassified_hip3_pair n'est alors utile que si aucun
    marche ne declare un scope HIP-3 du tout."""
    conf = get_default_conf(testdatadir)
    conf["strategy_path"] = str(STRATEGY_DIR)
    conf["strategy"] = "hippo_original_multi"
    conf["timeframe"] = "5m"
    conf.pop("minimal_roi", None)
    conf["pairlists"] = [
        {
            "method": "MultiMarketPairList",
            "markets": [
                {
                    "name": "crypto",
                    "scope": "main",
                    "pairlists": [{"method": "MarketCapPairList"}],
                },
                {
                    "name": "stocks",
                    "scope": "hip3:xyz",
                    "pair_whitelist": ["XYZ-NVDA/USDC:USDC"],
                    "pairlists": [{"method": "StaticPairList"}],
                },
            ],
        }
    ]
    patch_exchange(mocker)
    strategy = StrategyResolver.load_strategy(conf)

    # Absente de la pair_whitelist statique de "stocks", mais son scope
    # (hip3:xyz) matche quand meme -> classee "stocks" par market_of_pair,
    # jamais "crypto".
    assert strategy.market_of("XYZ-UNKNOWN/USDC:USDC") == "stocks"


def test_confirm_trade_entry_refuse_paire_hip3_ambigue(mocker, testdatadir):
    """Une NOUVELLE entree sur une paire HIP-3 dont le marche est ambigu
    (aucune pair_whitelist statique ne la couvre, et plusieurs marches HIP-3
    sont declares) doit etre refusee par confirm_trade_entry, jamais
    silencieusement acceptee avec des parametres crypto."""
    strategy = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    assert (
        strategy.confirm_trade_entry(
            pair="XYZ-UNKNOWN/USDC:USDC",
            order_type="limit",
            amount=1.0,
            rate=1.0,
            time_in_force="GTC",
            current_time=datetime.now(UTC),
            entry_tag=None,
            side="long",
        )
        is False
    )
    # Une paire correctement classee (crypto, ou HIP-3 couverte par une
    # pair_whitelist statique) n'est pas concernee par cette garde.
    assert (
        strategy.confirm_trade_entry(
            pair="ETH/USDC:USDC",
            order_type="limit",
            amount=1.0,
            rate=1.0,
            time_in_force="GTC",
            current_time=datetime.now(UTC),
            entry_tag=None,
            side="long",
        )
        is True
    )
    assert (
        strategy.confirm_trade_entry(
            pair=strategy.STOCKS_PAIRS[0],
            order_type="limit",
            amount=1.0,
            rate=1.0,
            time_in_force="GTC",
            current_time=datetime.now(UTC),
            entry_tag=None,
            side="long",
        )
        is True
    )


# ── (d) callbacks par trade ──────────────────────────────────────────────────


def _mock_trade(pair, lev, minutes_open=45, filled_costs=None):
    t = MagicMock()
    t.pair = pair
    t.leverage = lev
    t.open_date_utc = datetime.now(UTC) - timedelta(minutes=minutes_open)
    t.entry_tag = None
    t.entry_side = "buy"
    if filled_costs is not None:
        orders = []
        for cost in filled_costs:
            o = MagicMock()
            o.cost = cost
            orders.append(o)
        t.select_filled_orders.return_value = orders
    return t


@pytest.mark.parametrize("market", ["crypto", "stocks", "mat"])
def test_leverage_par_marche(mocker, testdatadir, market):
    from user_data.strategies.hippo_original_multi import MARKET_PARAMS

    strategy = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    now = datetime.now(UTC)
    pair = PAIR_BY_MARKET[market]
    expected = MARKET_PARAMS[market]["leverage_value"]
    assert strategy.leverage(pair, now, 1.0, 1.0, 20.0, None, "long") == expected
    # Plafonne par max_leverage si celui-ci est plus bas.
    assert strategy.leverage(pair, now, 1.0, 1.0, 0.5, None, "long") == 0.5


def test_min_roi_reached_crypto_aligne_sur_hippo_original(mocker, testdatadir):
    """A levier 1 sur le marche crypto, la fusion doit rendre EXACTEMENT les
    memes decisions que `hippo_original.min_roi_reached` sur une grille de
    durees/profits (paliers ROI natifs)."""
    original = _load_strategy(mocker, testdatadir, "hippo_original")
    multi = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    now = datetime.now(UTC)

    durations = [0, 5, 9, 10, 15, 29, 30, 35, 39, 40, 41, 120]
    profits = [
        -0.01,
        0.0,
        0.004,
        0.0049,
        0.005,
        0.006,
        0.009,
        0.0179,
        0.018,
        0.019,
        0.0099,
        0.01,
        0.011,
        0.0499,
        0.05,
        0.06,
    ]

    for dur in durations:
        for profit in profits:
            t_orig = _mock_trade("ETH/USDC:USDC", 1.0, minutes_open=dur)
            t_multi = _mock_trade("ETH/USDC:USDC", 1.0, minutes_open=dur)
            expected = original.min_roi_reached(t_orig, profit, now)
            actual = multi.min_roi_reached(t_multi, profit, now)
            assert actual == expected, (dur, profit)


@pytest.mark.parametrize("market", ["stocks", "mat"])
def test_min_roi_reached_prix_independant_du_levier(mocker, testdatadir, market):
    from user_data.strategies.hippo_original_multi import MARKET_PARAMS

    multi = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    now = datetime.now(UTC)
    pair = "XYZ-NVDA/USDC:USDC" if market == "stocks" else "XYZ-GOLD/USDC:USDC"
    floor = MARKET_PARAMS[market]["roi_price"][40]

    t1 = _mock_trade(pair, 1.0, minutes_open=45)
    t2 = _mock_trade(pair, 2.0, minutes_open=45)
    # meme mouvement de PRIX -> meme decision, quel que soit le levier
    assert multi.min_roi_reached(t1, floor + 1e-6, now) is True
    assert multi.min_roi_reached(t2, floor * 2.0 + 1e-6, now) is True
    assert multi.min_roi_reached(t2, floor + 1e-6, now) is False


@pytest.mark.parametrize("market", ["crypto", "stocks", "mat"])
def test_custom_exit_stop_prix(mocker, testdatadir, market):
    from user_data.strategies.hippo_original_multi import MARKET_PARAMS

    multi = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    now = datetime.now(UTC)
    pair = PAIR_BY_MARKET[market]
    sl = MARKET_PARAMS[market]["stoploss_price"]

    t1 = _mock_trade(pair, 1.0)
    assert multi.custom_exit(pair, t1, now, 1.0, sl - 1e-6) is not None
    assert multi.custom_exit(pair, t1, now, 1.0, sl + 1e-6) is None

    t2 = _mock_trade(pair, 2.0)
    assert multi.custom_exit(pair, t2, now, 1.0, sl * 2.0 - 1e-6) is not None
    assert multi.custom_exit(pair, t2, now, 1.0, sl) is None


@pytest.mark.parametrize("market", ["crypto", "stocks", "mat"])
def test_adjust_trade_position_premier_renfort_meme_mise_selon_levier(mocker, testdatadir, market):
    """La mise du 1er renfort ne doit pas dependre du levier : le correctif
    `cost / trade.leverage` doit annuler l'effet du levier sur `filled_buys[0].cost`."""
    from user_data.strategies.hippo_original_multi import MARKET_PARAMS

    multi = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    now = datetime.now(UTC)
    pair = PAIR_BY_MARKET[market]
    so_trigger = MARKET_PARAMS[market]["so_trigger_price"]
    # profit de PRIX largement au-dela du trigger du premier renfort
    price_profit = so_trigger * 1.5

    results = {}
    for lev in (1.0, 2.0):
        initial_stake = 100.0
        # cost = notionnel = levier x mise -> mise = initial_stake constante
        t = _mock_trade(pair, lev, filled_costs=[initial_stake * lev])
        multi.cust_proposed_initial_stakes[pair] = 0.0  # force le chemin "fallback"
        result = multi.adjust_trade_position(t, now, 1.0, price_profit * lev, 1.0, 100000.0)
        assert result is not None, (market, lev)
        results[lev] = result

    assert results[1.0] == pytest.approx(results[2.0], rel=1e-9)


# ── (e) custom_stake_amount par marche ──────────────────────────────────────


@pytest.mark.parametrize(
    "market,src_name,pair",
    [
        ("crypto", "hippo_original", "ETH/USDC:USDC"),
        ("stocks", "hippo_original_stocks", "XYZ-NVDA/USDC:USDC"),
        ("mat", "hippo_original_mat", "XYZ-GOLD/USDC:USDC"),
    ],
)
def test_custom_stake_amount_egale_a_la_source(mocker, testdatadir, market, src_name, pair):
    source = _load_strategy(mocker, testdatadir, src_name)
    multi = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    now = datetime.now(UTC)
    proposed_stake = 137.5

    expected = source.custom_stake_amount(pair, now, 1.0, proposed_stake, 1.0, 100000.0)
    actual = multi.custom_stake_amount(pair, now, 1.0, proposed_stake, 1.0, 100000.0)
    assert actual == pytest.approx(expected)


# ── (f) plafond de trades ouverts par marche ────────────────────────────────


def _confirm_kwargs(pair, entry_tag="crypto"):
    return dict(
        pair=pair,
        order_type="limit",
        amount=1.0,
        rate=1.0,
        time_in_force="GTC",
        current_time=datetime.now(UTC),
        entry_tag=entry_tag,
        side="long",
    )


def test_confirm_trade_entry_sans_plafond_tout_passe(mocker, testdatadir):
    multi = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    assert multi.max_open_trades_per_market is None
    mocker.patch(
        "freqtrade.persistence.Trade.get_trades_proxy",
        return_value=[_mock_trade("XYZ-NVDA/USDC:USDC", 1.0)],
    )
    assert multi.confirm_trade_entry(**_confirm_kwargs("XYZ-AAPL/USDC:USDC")) is True


def test_confirm_trade_entry_plafond_refuse_meme_marche(mocker, testdatadir):
    multi = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    multi.max_open_trades_per_market = {"stocks": 1}
    mocker.patch(
        "freqtrade.persistence.Trade.get_trades_proxy",
        return_value=[_mock_trade("XYZ-NVDA/USDC:USDC", 1.0)],
    )
    # Le marche "stocks" est deja au plafond (1 trade ouvert sur XYZ-NVDA) :
    # une nouvelle entree stocks sur une autre paire est refusee.
    assert multi.confirm_trade_entry(**_confirm_kwargs("XYZ-AAPL/USDC:USDC")) is False


def test_confirm_trade_entry_plafond_autres_marches_passent(mocker, testdatadir):
    multi = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    multi.max_open_trades_per_market = {"stocks": 1}
    mocker.patch(
        "freqtrade.persistence.Trade.get_trades_proxy",
        return_value=[_mock_trade("XYZ-NVDA/USDC:USDC", 1.0)],
    )
    # Le plafond ne s'applique qu'au marche "stocks" : mat et crypto passent.
    assert multi.confirm_trade_entry(**_confirm_kwargs("XYZ-GOLD/USDC:USDC")) is True
    assert multi.confirm_trade_entry(**_confirm_kwargs("ETH/USDC:USDC")) is True


def test_confirm_trade_entry_renfort_meme_paire_toujours_autorise(mocker, testdatadir):
    """Un renfort (DCA) sur une paire deja ouverte n'est pas une nouvelle
    entree : la garde explicite le laisse passer meme au plafond. En
    pratique freqtrade n'appelle meme pas ce callback pour un renfort (voir
    `freqtradebot.py::execute_entry`, `mode == "initial"` uniquement)."""
    multi = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    multi.max_open_trades_per_market = {"stocks": 1}
    mocker.patch(
        "freqtrade.persistence.Trade.get_trades_proxy",
        return_value=[_mock_trade("XYZ-NVDA/USDC:USDC", 1.0)],
    )
    assert multi.confirm_trade_entry(**_confirm_kwargs("XYZ-NVDA/USDC:USDC")) is True


def test_confirm_trade_entry_config_prime_sur_attribut_de_classe(mocker, testdatadir):
    conf = get_default_conf(testdatadir)
    conf["strategy_path"] = str(STRATEGY_DIR)
    conf["strategy"] = "hippo_original_multi"
    conf["timeframe"] = "5m"
    conf.pop("minimal_roi", None)
    conf["hippo_multi"] = {"max_open_trades_per_market": {"stocks": 5}}
    patch_exchange(mocker)
    multi = StrategyResolver.load_strategy(conf)

    assert multi.max_open_trades_per_market == {"stocks": 5}
    mocker.patch(
        "freqtrade.persistence.Trade.get_trades_proxy",
        return_value=[_mock_trade("XYZ-NVDA/USDC:USDC", 1.0)],
    )
    # Plafond de la config (5) tres au-dessus du seul trade ouvert -> passe,
    # alors qu'un attribut de classe eventuel a 1 aurait refuse.
    assert multi.confirm_trade_entry(**_confirm_kwargs("XYZ-AAPL/USDC:USDC")) is True


def test_confirm_trade_entry_independant_de_max_open_trades_global(mocker, testdatadir):
    """Le plafond par marche s'applique independamment de `max_open_trades`
    global : meme si le bot a encore des slots globaux libres, un marche au
    plafond refuse quand meme la nouvelle entree."""
    multi = _load_strategy(mocker, testdatadir, "hippo_original_multi")
    multi.max_open_trades_per_market = {"stocks": 1}
    assert multi.config.get("max_open_trades", 3) >= 1
    mocker.patch(
        "freqtrade.persistence.Trade.get_trades_proxy",
        return_value=[_mock_trade("XYZ-NVDA/USDC:USDC", 1.0)],
    )
    assert multi.confirm_trade_entry(**_confirm_kwargs("XYZ-AAPL/USDC:USDC")) is False
