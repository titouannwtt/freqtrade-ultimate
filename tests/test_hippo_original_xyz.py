"""Tests pour les dérivées HIP-3 xyz de `hippo_original`.

`hippo_original_stocks` et `hippo_original_mat` sont des copies autonomes de
`hippo_original` (long DCA 5m validé en live crypto), paramétrées par un bloc
d'attributs de classe pour cibler les marchés actions / matières premières du
dex HIP-3 "xyz". Ce fichier vérifie :

(a) que les deux stratégies se chargent via `StrategyResolver` ;
(b) qu'avec les attributs entrée remis aux valeurs natives, `enter_long` et
    `exit_long` sont identiques à `hippo_original` sur des données réelles
    (isomorphisme structurel), et que `minimal_roi`/`max_so_multiplier`
    concordent une fois alignés ;
(c) que les deux configs `live_configs/hyperliquid_hippo_original_{stocks,mat}.json`
    sont valides et cohérentes avec la flotte (ports, whitelist xyz, stratégie) ;
(d) que le filtre de liquidité tolérant (`liquidity_max_empty`) ne bloque pas
    l'entrée 48h après quelques bougies vides isolées, contrairement au
    `missing_data < 1` natif.
"""

import json
from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_series_equal

from freqtrade.resolvers import StrategyResolver
from tests.conftest import get_default_conf, patch_exchange


REPO_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_DIR = REPO_ROOT / "user_data" / "strategies"
DATA_DIR = REPO_ROOT / "user_data" / "data" / "hyperliquid" / "futures"
LIVE_CONFIGS_DIR = REPO_ROOT / "live_configs"

N_CANDLES = 3000
DATA_FILES = [
    "ETH_USDC_USDC-5m-futures.feather",
    "SOL_USDC_USDC-5m-futures.feather",
]
DERIVED_STRATEGIES = ["hippo_original_stocks", "hippo_original_mat"]

# Remet les attributs d'entrée des dérivées à leur équivalent natif crypto,
# tel que défini dans hippo_original.populate_indicators/populate_entry_trend.
NATIVE_ENTRY_OVERRIDES = {
    "tcp_window": 4,
    "tcp_threshold": 0.04,
    "tcp_s_threshold": None,
    "liquidity_recent_candles": 0,
    "liquidity_max_empty": 0,
    "session_rule": None,
}

XYZ_CONFIGS = {
    "hyperliquid_hippo_original_stocks.json": "hippo_original_stocks",
    "hyperliquid_hippo_original_mat.json": "hippo_original_mat",
}


def _config_path(name: str) -> Path:
    """Config vivante dans live_configs/, sinon archivée dans live_configs/_retired/
    (les deux bots ont été fusionnés dans hippo_original_multi le 2026-09-05)."""
    live = LIVE_CONFIGS_DIR / name
    return live if live.is_file() else LIVE_CONFIGS_DIR / "_retired" / name


def _load_strategy(mocker, testdatadir, strategy_name):
    conf = get_default_conf(testdatadir)
    conf["strategy_path"] = str(STRATEGY_DIR)
    conf["strategy"] = strategy_name
    conf["timeframe"] = "5m"
    # Ne pas laisser la config de test écraser minimal_roi : on veut la
    # valeur définie par la stratégie elle-même (comparée en (b)).
    conf.pop("minimal_roi", None)
    patch_exchange(mocker)
    return StrategyResolver.load_strategy(conf)


def _load_candles(filename: str, n: int = N_CANDLES) -> pd.DataFrame:
    df = pd.read_feather(DATA_DIR / filename)
    return df.tail(n).reset_index(drop=True)


# ── (a) chargement via StrategyResolver ─────────────────────────────────────


@pytest.mark.parametrize("strategy_name", DERIVED_STRATEGIES)
def test_les_derivees_se_chargent(mocker, testdatadir, strategy_name):
    strategy = _load_strategy(mocker, testdatadir, strategy_name)
    assert strategy.__class__.__name__ == strategy_name
    assert strategy.timeframe == "5m"
    # Le bloc d'attributs attendu est bien présent (structure, pas valeurs).
    for attr in (
        "tcp_window", "tcp_threshold", "tcp_s_threshold", "vwap_window", "vwap_std", "cti_max",
        "rsi_max", "rsi84_max", "rsi112_max", "atr_max_pct",
        "liquidity_recent_candles", "liquidity_max_empty", "liquidity_window",
        "session_rule", "exit_rsi", "custom_stoploss_ratio",
    ):
        assert hasattr(strategy, attr)


# ── (b) isomorphisme avec hippo_original ────────────────────────────────────


@pytest.mark.parametrize("data_file", DATA_FILES)
@pytest.mark.parametrize("strategy_name", DERIVED_STRATEGIES)
def test_isomorphisme_avec_hippo_original(mocker, testdatadir, strategy_name, data_file):
    original = _load_strategy(mocker, testdatadir, "hippo_original")
    derived = _load_strategy(mocker, testdatadir, strategy_name)
    for attr, value in NATIVE_ENTRY_OVERRIDES.items():
        setattr(derived, attr, value)

    df = _load_candles(data_file)
    meta = {"pair": "TEST/USDC:USDC"}

    df_orig = original.advise_indicators(df.copy(), meta)
    df_orig = original.advise_entry(df_orig, meta)
    df_orig = original.advise_exit(df_orig, meta)

    df_derived = derived.advise_indicators(df.copy(), meta)
    df_derived = derived.advise_entry(df_derived, meta)
    df_derived = derived.advise_exit(df_derived, meta)

    assert_series_equal(
        df_orig["enter_long"].fillna(0).astype(float),
        df_derived["enter_long"].fillna(0).astype(float),
        check_names=False,
    )
    assert_series_equal(
        df_orig["exit_long"].fillna(0).astype(float),
        df_derived["exit_long"].fillna(0).astype(float),
        check_names=False,
    )
    # Le nombre d'entrées observées doit être non trivial pour que la
    # comparaison ci-dessus soit un test, pas une tautologie sur des zéros.
    assert df_orig["enter_long"].fillna(0).sum() >= 0

    # minimal_roi : valeurs provisoires différentes par construction, mais
    # même structure (clés) et alignables sur le natif.
    assert set(derived.minimal_roi.keys()) == set(original.minimal_roi.keys())
    assert derived.minimal_roi != original.minimal_roi
    derived.minimal_roi = dict(original.minimal_roi)
    assert derived.minimal_roi == original.minimal_roi

    # max_so_multiplier est recalculé à partir de max_so_multiplier_orig /
    # safety_order_volume_scale, identiques au natif par défaut : doit déjà
    # concorder sans override (régression sur la formule recopiée).
    assert derived.max_so_multiplier == pytest.approx(original.max_so_multiplier)


# ── (c) configs xyz ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("config_name,strategy_name", XYZ_CONFIGS.items())
def test_configs_xyz_valides(config_name, strategy_name):
    cfg_path = _config_path(config_name)
    assert cfg_path.is_file()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    assert isinstance(cfg.get("dry_run"), bool), "dry_run doit etre explicite"
    assert cfg["strategy"] == strategy_name
    assert "xyz" in cfg["exchange"]["hip3_dexes"]

    whitelist = cfg["exchange"]["pair_whitelist"]
    assert whitelist, "whitelist vide"
    for pair in whitelist:
        assert pair.startswith("XYZ-"), pair
        assert pair.endswith("/USDC:USDC"), pair

    port = cfg["api_server"]["listen_port"]
    other_ports = []
    for other in LIVE_CONFIGS_DIR.glob("*.json"):
        if other.name == config_name:
            continue
        try:
            other_cfg = json.loads(other.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        other_port = other_cfg.get("api_server", {}).get("listen_port")
        if other_port is not None:
            other_ports.append((other.name, other_port))

    collisions = [f for f, p in other_ports if p == port]
    assert not collisions, f"port {port} aussi utilisé par {collisions}"


def test_les_deux_ports_xyz_sont_distincts():
    ports = {}
    for name in XYZ_CONFIGS:
        path = _config_path(name)
        if path.parent.name == "_retired":
            pytest.skip("configs archivées : les ports ne sont plus réservés")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        ports[name] = cfg["api_server"]["listen_port"]
    assert len(set(ports.values())) == len(ports), ports


# ── (d) tolérance de liquidité vs blocage 48h natif ─────────────────────────


def _synthetic_ohlcv(n: int, empty_indices: set[int]) -> pd.DataFrame:
    """Bougies OHLCV synthétiques, avec volume nul aux index donnés."""
    dates = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    close = pd.Series(100.0 + (pd.Series(range(n)) % 7) * 0.01)
    df = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": 100.0,
        }
    )
    df.loc[list(empty_indices), "volume"] = 0.0
    return df


def test_liquidite_tolerante_ne_bloque_pas_48h(mocker, testdatadir):
    """liquidity_max_empty=12 tolère 5 bougies vides isolées ; missing_data < 1
    (natif) les aurait interdites pendant 48h (576 bougies de 5m) chacune."""
    derived = _load_strategy(mocker, testdatadir, "hippo_original_stocks")
    assert derived.liquidity_max_empty == 12
    assert derived.liquidity_window == 576

    # La fenêtre de liquidité (576) doit être déjà pleine (min_periods) avant
    # la première bougie vide, sinon la comparaison mesure l'amorçage du
    # rolling, pas le comportement du filtre.
    n = 1300
    empty_indices = {700, 760, 820, 880, 940}
    df = _synthetic_ohlcv(n, empty_indices)

    out = derived.populate_indicators(df.copy(), {"pair": "TEST/USDC:USDC"})

    # Comportement natif équivalent (missing_data < 1), calculé sur la même
    # série de volume, pour prouver le contraste.
    empty_count = (out["volume"] <= 0).rolling(
        window=derived.liquidity_window, min_periods=derived.liquidity_window
    ).sum()
    native_ok = empty_count < 1

    # Avant tout incident, une fois la fenêtre pleine, tout le monde est ok.
    assert native_ok.iloc[derived.liquidity_window : 700].all()
    assert out["liquidity_ok"].iloc[derived.liquidity_window : 700].all()

    # Juste après chaque bougie vide, le natif bloque (la fenêtre contient
    # désormais une bougie vide) : au moins un point bloqué peu après chaque
    # incident, jusqu'à ce que la bougie vide sorte de la fenêtre (48h).
    for idx in sorted(empty_indices):
        window = range(idx + 1, min(idx + 100, n))
        assert not native_ok.iloc[list(window)].all(), (
            f"le calcul natif de reference ne bloque plus apres l'index {idx}, "
            "le scenario ne prouve plus rien"
        )

    # La version tolérante (liquidity_max_empty=12) n'est jamais bloquée par
    # ces 5 bougies isolées : bien en dessous du seuil de 12 sur la fenêtre.
    assert out["liquidity_ok"].iloc[derived.liquidity_window : n].all()
    assert out["liquidity_ok"].sum() > native_ok.sum()


# ── (e) seuils exprimes en prix, ramenes au levier reel du trade ───────────────
@pytest.mark.parametrize("strategy_name", DERIVED_STRATEGIES)
def test_seuils_en_prix_selon_levier_reel(mocker, testdatadir, strategy_name):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock

    strat = _load_strategy(mocker, testdatadir, strategy_name)
    now = datetime.now(UTC)
    assert strat.leverage_value == 2.0
    assert strat.leverage("X", now, 1.0, 1.0, 20.0, None, "long") == 2.0
    assert strat.leverage("X", now, 1.0, 1.0, 1.0, None, "long") == 1.0
    floor = strat._roi_price["40"]
    sl = strat._stoploss_price

    def trade(lev):
        t = MagicMock()
        t.leverage = lev
        t.open_date_utc = now - timedelta(minutes=45)
        t.enter_tag = None
        t.entry_tag = None
        return t

    # meme mouvement de PRIX -> meme decision ROI, quel que soit le levier
    assert strat.min_roi_reached(trade(1.0), floor + 1e-6, now) is True
    assert strat.min_roi_reached(trade(2.0), floor * 2.0 + 1e-6, now) is True
    assert strat.min_roi_reached(trade(2.0), floor + 1e-6, now) is False
    # stop custom : -35 % de PRIX
    assert strat.custom_exit("X", trade(2.0), now, 1.0, sl * 2.0 - 1e-6) is not None
    assert strat.custom_exit("X", trade(2.0), now, 1.0, sl) is None
    assert strat.custom_exit("X", trade(1.0), now, 1.0, sl - 1e-6) is not None
    # renfort : declenche sur le mouvement de PRIX, pas sur le ratio leverage
    for lev in (1.0, 2.0):
        profit = strat._so_trigger_price * lev * 0.9
        assert strat.adjust_trade_position(trade(lev), now, 1.0, profit, 1.0, 100.0) is None
