# =============================================================
#  ➤ Stratégie KAC-Index : ATR-CCI + Indexation sur Total3
#
#  Fichier rédigé par Titouannwtt pour Freqtrade France 🇫🇷
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
import talib.abstract as ta
import ta as clean_ta
from freqtrade.strategy import IStrategy
import logging

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
tv = TvDatafeed()


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

#command = freqtrade backtesting --strategy kac_index_v1 --config futures_binance_fix.json --timerange 20210101- --timeframe 1h --max-open-trades -1 --cache none --dry-run-wallet 1000

class kac_index_v1(IStrategy):

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

    # Takeprofit
    minimal_roi = {
        # Si à n'importe quel moment dès l'ouverture de la position : la position atteint 143% de profit, alors on ferme la position
        '0': 1.45,
        
        # Si 4 jours (5760 minutes) après l'ouverture de la position : la position atteint 133% de profit ou est déjà au dessus, alors on ferme la position
        '5760': 1.331,
        
        # Si 5 jours et demi (131 heures soit 7860 minutes) après l'ouverture de la position : la position atteint 31,5% de profit ou est déjà au dessus, alors on ferme la position
        '7860': 0.314,
        
        # Si 7 jours (169 heures soit 10140) après l'ouverture de la position : la position est au moins supérieur à -28%, alors on ferme la position.
        # Si la position est à -30%, alors elle restera ouverte jusqu'à atteindre -28% ou recevoir un signal de fermeture.
        '10140': -0.282
    }

    stoploss = -0.9

    trailing_stop = False

    order_types = {'entry': 'limit','exit': 'limit','stoploss': 'market','stoploss_on_exchange': False}
    order_time_in_force = {'entry': 'GTC', 'exit': 'GTC'}


    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        in_backtest=True if self.dp.runmode.value in ("backtest", "hyperopt") else False
        
        #Indexation sur le TOTAL3 : On récupère les valeurs du TOTAL3 sur tradingview
        # (comme l'historique Trading view ne permet pas de récupérer la donnée en timeframe 1h depuis 2021, on récupère la donnée en timeframe 1d)
        dataframe = add_tv_graph(dataframe, backtest=in_backtest)
        
        # Ensuite, on lisse la donnée récupérée au format 1d, pour la transformer sur 24 heures grâce à une moyenne sur un rolling
        # (on convertit ainsi notre donnée en tf 1d en une donnée lisée en tf 1h)
        dataframe['TOTAL3'] = dataframe['1d_TOTAL3'].rolling(24).mean()
        
        #Indexation sur le TOTAL3 : On divise toutes les colonnes ohlc (open, high, low, close) par notre TOTAL3 lissée
        # Nos valeurs ohlc de la paire seront donc indexées par rapport au TOTAL3 et plus par rapport à l'USDC ou l'USDT
        for column in ['close', 'high', 'low', 'open']:
            column_old=column+'_old'
            dataframe[column_old]=dataframe[column]
            dataframe[column]=dataframe[column]/dataframe['TOTAL3']
            
        # Indicateur ATR (mais sur une indexation au TOTAL3 puisque ohlc avec une indexation sur le TOTAL3 sont utilisés)
        dataframe["ATR"] = (ta.ATR(dataframe, timeperiod=16) / dataframe["close"]) * 100
        
        # Indicateur CCI (avec indexation sur le TOTAL3 également)
        dataframe['CCI'] = clean_ta.trend.CCIIndicator(high=dataframe['high'], low=dataframe['low'], close=dataframe['close'], window=10, constant=0.0175).cci()
        
        
        #Indexation sur le TOTAL3 : On remet les valeurs ohlc à leurs valeurs d'origines (sinon FreqUI, backtest, etc. ne comprennent pas)
        for column in ['close', 'high', 'low', 'open']:
            column_old=column+'_old'
            dataframe[column]=dataframe[column_old]
        
        
        # Plot configs - Permet de réprésenter les zones où les indicateurs respectent les conditions
        # Points d'entrée
        dataframe['I-ATR'] = np.where((dataframe['ATR'] > 1.5) & (dataframe['ATR'] > dataframe['ATR'].shift(1)), dataframe['ATR'], np.nan)
        dataframe['I-CCI'] = np.where((dataframe['CCI'] < 0), dataframe['CCI'], np.nan)

        # Points de sortie
        dataframe['C-ATR'] = np.where((dataframe['ATR'] < -2) & (dataframe['ATR'] < dataframe['ATR'].shift(1)), dataframe['ATR'], np.nan)
        dataframe['C-CCI'] = np.where((dataframe['CCI'] > 200) & (dataframe['CCI'] > dataframe['CCI'].shift(1)), dataframe['CCI'], np.nan)

        return dataframe


    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # On applique les conditions d'entrée de notre stratégie
        dataframe.loc[reduce(lambda x, y: x & y, [
            # ATR croissant et supérieur à 1.5
            (dataframe['ATR'] > 1.5),
            (dataframe['ATR'] > dataframe['ATR'].shift(1)),
            
            # CCI en dessous de 0
            (dataframe['CCI'] < 0),
        ]), 'enter_long'] = 1
        return dataframe


    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # On applique les conditions de sortie de notre stratégie
        dataframe.loc[reduce(lambda x, y: x & y, [
            # ATR décroissant et en dessous de -2
            (dataframe['ATR'] < -2),
            (dataframe['ATR'] < dataframe['ATR'].shift(1)),
            
            # CCI croissant et au dessus de 200
            (dataframe['CCI'] > 200),
            (dataframe['CCI'] > dataframe['CCI'].shift(1))
        ]), 'exit_long'] = 1
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
