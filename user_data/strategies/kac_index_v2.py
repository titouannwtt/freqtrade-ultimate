
# =============================================================
#  ➤ Stratégie KAC-Index : ATR-CCI + Indexation sur Total3
#
#  Fichier généré par Moutonneux pour Freqtrade France 🇫🇷
#  ➤ Soutenez-nous sur : https://coff.ee/freqtrade_france et obtenez davantage de stratégies
#
#  📌 Ce code est fourni à des fins personnelles ou éducatives.
#  ❌ Il ne doit pas être partagé, distribué, ni publié dans un autre cadre.
# =============================================================



import numpy as np
import pandas as pd
from pandas import DataFrame
from functools import reduce
import time
from datetime import datetime, timedelta
from pandas import DataFrame
from typing import Optional, Union, Any, Dict
import talib.abstract as ta
import ta as clean_ta
from freqtrade.strategy import (timeframe_to_minutes, BooleanParameter, IntParameter, CategoricalParameter, DecimalParameter, IStrategy, merge_informative_pair)
import logging

from freqtrade.optimize.space import Categorical, Integer, SKDecimal
from freqtrade.optimize.hyperopt import IHyperOptLoss


# ❌ Cette stratégie nécessite l'utilisation de tvdatafeed, une librairie externe !
# Pour l'installer, utilise : 
        # Installations commands : 
        # pip install requests websockets
        # git clone https://github.com/rongardF/tvdatafeed
        # cd tvdatafeed
        # pip install .

from tvDatafeed import TvDatafeed, Interval
from pytz import UTC

logger = logging.getLogger(__name__)
cache = {}


# Cette fonction permet de récupérer les valeurs du TOTAL3 depuis Trading View (https://fr.tradingview.com/symbols/TOTAL3/) pour les mettre dans un dataframe
# Libre à vous de tester cette fonction avec d'autres symboles disponibles sur Trading View
def add_tv_graph(dataframe: pd.DataFrame, backtest=True):
    symbol = 'TOTAL3'
    exchange = 'CRYPTOCAP'
    interval = Interval.in_daily
    column_name = f'1d_{symbol}'
    
    # Liste des colonnes que l'on veut récupéré sur Trading View
    ohlc_types = ['close', 'open', 'high', 'low', 'volume']
    
    # Lorsque le bot sera en live, il sera important de ne pas retélécharger constamment les données
    # donc on utilisera 'current_hour' pour regarder si on a déjà les données liées à l'heure actuel dans notre cache ou non
    current_hour = datetime.now().replace(minute=0, second=0, microsecond=0)

    # Si les données sont déjà présentes et valides, on sort directement
    if f'{column_name}_close' in dataframe.columns and dataframe[f'{column_name}_close'].max() != -1:
        dataframe[f'{column_name}'] = dataframe[f'{column_name}_close'].ffill()
        return dataframe

    # Clé de cache pour éviter de recharger les mêmes données si on les a déjà chargé
    cache_key = (symbol, exchange, interval.value, 4000) if backtest else (current_hour, symbol, exchange, interval.value, 4000)

    # Chargement des données depuis TradingView, avec fallback si erreur
    if cache_key not in cache:
        tv = TvDatafeed()
        try:
            cache[cache_key] = tv.get_hist(symbol, exchange, interval, n_bars=4000)
        except:
            time.sleep(10)
            try:
                cache[cache_key] = tv.get_hist(symbol, exchange, interval, n_bars=2000)
            except:
                time.sleep(20)
                cache[cache_key] = tv.get_hist(symbol, exchange, interval, n_bars=2000)

    # Nettoyage de cache obsolète si le bot est en live (sinon au bout de plusieurs jours, le cache peut devenir assez conséquent)
    if not backtest:
        cache.pop((current_hour - timedelta(hours=1), symbol, exchange, interval.value, 4000), None)

    # Mise en forme du dataframe téléchargé
    tv_df = pd.DataFrame(cache[cache_key])
    if tv_df.empty:
        print(f"Aucune donnée de tradingview {symbol} récupérée.")
        for ohlc in [''] + ohlc_types:
            dataframe[f'{column_name}_{ohlc}'.rstrip('_')] = dataframe['close'] * 0 - 1
        return dataframe

    tv_df.drop(columns=["symbol"], errors="ignore", inplace=True)
    tv_df.index = pd.to_datetime(tv_df.index).tz_localize(UTC)

    # Décalage des valeurs pour ne pas utiliser la bougie en cours
    for ohlc in ohlc_types:
        if ohlc in tv_df.columns:
            tv_df[ohlc] = tv_df[ohlc].shift(1)
            tv_df.rename(columns={ohlc: f'{column_name}_{ohlc}'}, inplace=True)

    # Préparation du dataframe principal pour le merge
    dataframe = dataframe.copy()
    dataframe['date'] = pd.to_datetime(dataframe['date'])
    if dataframe['date'].dt.tz is None:
        dataframe['date'] = dataframe['date'].dt.tz_localize(UTC)
    dataframe.set_index('date', inplace=True)

    # Merge des données, à partir de la première date disponible
    start_date = dataframe.index.min().normalize()
    tv_df = tv_df[tv_df.index >= start_date]
    dataframe = dataframe.join(tv_df, how='left')

    # Remplissage des trous par propagation
    for ohlc in ohlc_types:
        col = f'{column_name}_{ohlc}'
        if col in dataframe.columns:
            dataframe[col] = dataframe[col].ffill()

    # Ajout d'une colonne "valeur principale" = close
    dataframe[f'{column_name}'] = dataframe.get(f'{column_name}_close', dataframe['close'] * 0 - 1).ffill()

    # Retour au format d'origine
    dataframe.reset_index(inplace=True)
    return dataframe



###############################################
# Stratégie KAC-Index : ATR-CCI + Indexation sur Total3

# La stratégie a été trouvée avec les paramètres suivants :
#   max positions = -1
#   timeframe = 1h
#   config = futures_binance_fix.json
#   timerange = 20210101-

#command = freqtrade backtesting --strategy kac_index_v2 --config futures_binance_fix.json --timerange 20210101- --timeframe 1h --max-open-trades -1 --cache none --dry-run-wallet 1000

class kac_index_v2(IStrategy):

    trailing_stop = False

    minimal_roi = {
        "0": 4.05,
        "6000": 3.42,
        "12000": 2.58,
        "16560": 0.53,
        "19440": -0.02
    }

    use_exit_signal = True
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count: int = 10
    position_adjustment_enable = True

    # Détermine la taille personnalisée du premier ordre d'achat.
    # Dans le cas où on utiliserait Hyperliquid, on doit impérativement avoir une position minimum de 15 usdc
    # donc si la stratégie veut ouvrir en dessous, on fixe à 15 usdc minimum
    # Normalement c'est une condition qui ne nous servira que au début de la stratégie, si elle devient gagnante, on dépassera un certain capital qui rendra cette condition inutile.
    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float, min_stake: float, max_stake: float, **kwargs) -> float:
        # Forcer un minimum de 15 USDC pour hyperliquid
        if proposed_stake < 15.0:
            if self.dp.runmode.value != "hyperopt" and self.dp.runmode.value != "backtest":
                logger.info(f"Stake amount for {pair} is too small ({proposed_stake} < 15.0), adjusting to 15.0.")
            proposed_stake = 15.0
        return proposed_stake
    
    
    INTERFACE_VERSION = 3
    timeframe = '1h'
    can_short: bool = False
    process_only_new_candles = True


    stoploss = -0.40

    trailing_stop = False

    order_types = {'entry': 'limit','exit': 'limit','stoploss': 'market','stoploss_on_exchange': False}
    order_time_in_force = {'entry': 'GTC', 'exit': 'GTC'}
    

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        in_backtest=True if self.dp.runmode.value in ("backtest", "hyperopt") else False
        dataframe = add_tv_graph(dataframe, backtest=in_backtest)
        
        dataframe['TOTAL3'] = dataframe['1d_TOTAL3'].rolling(28).mean()
        
        for column in ['close', 'high', 'low', 'open']:
            column_old=column+'_old'
            dataframe[column_old]=dataframe[column]
            dataframe[column]=dataframe[column]/dataframe['TOTAL3']
        
        dataframe["ATR"] = (ta.ATR(dataframe, timeperiod=8) / dataframe["close"]) * 100
        dataframe['CCI'] = clean_ta.trend.CCIIndicator(high=dataframe['high'], low=dataframe['low'], close=dataframe['close'], window=6, constant=0.01625).cci()
        
        for column in ['close', 'high', 'low', 'open']:
            column_old=column+'_old'
            dataframe[column]=dataframe[column_old]
        
        dataframe['I-ATR'] = np.where((dataframe['ATR'] > 2.75), dataframe['ATR'], np.nan)
        dataframe['C-ATR'] = np.where((dataframe['ATR'] < dataframe['ATR'].shift(1)), dataframe['ATR'], np.nan)
        dataframe['C-CCI'] = np.where((dataframe['CCI'] > dataframe['CCI'].shift(1)), dataframe['CCI'], np.nan)

        return dataframe


    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[reduce(lambda x, y: x & y, [dataframe['ATR'] > 2.75]), 'enter_long'] = 1
        return dataframe


    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        conditions.append((dataframe['ATR'] < dataframe['ATR'].shift(1)))
        conditions.append((dataframe['CCI'] > dataframe['CCI'].shift(1)))
        dataframe.loc[reduce(lambda x, y: x & y, conditions), 'exit_long'] = 1
        return dataframe


    plot_config = {
        "main_plot": {},
        "subplots": {
            "CCI": {
                "CCI": {
                    "color": "#53387a",
                    "type": "line"
                },
                "C-CCI": {
                    "color": "#ff0000",
                    "type": "scatter",
                    "scatterSymbolSize": 6
                },
                "I-CCI": {
                    "color": "#00ff37",
                    "type": "scatter"
                }
            },
            "ATR": {
                "ATR": {
                    "color": "#eba1b1"
                },
                "I-ATR": {
                    "color": "#15ff00",
                    "type": "line"
                },
                "C-ATR": {
                    "color": "#ff0000",
                    "type": "line"
                }
            }
        }
    }
