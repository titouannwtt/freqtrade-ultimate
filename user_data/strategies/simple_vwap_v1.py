# =============================================================
#  Fichier rédigée par Moutonneux pour Freqtrade France 🇫🇷
#  ➤ Soutenez-nous sur : https://coff.ee/freqtrade_france et obtenez davantage de stratégies
#
#  📌 Ce code est fourni à des fins personnelles ou éducatives.
#  ❌ Il ne doit pas être partagé, distribué, ni publié dans un autre cadre.
# =============================================================

"""
Simple VWAP v1 - Stratégie d'exposition constante au marché

Cette stratégie adopte une approche contrariante : au lieu de chercher des entrées 
parfaites et rares, elle vise à être exposée au marché en permanence en ouvrant
jusqu'à 40 positions simultanément avec des conditions d'entrée très permissives.

Philosophie : "Mieux vaut être dans le marché 90% du temps avec de petits gains 
et petites pertes que de rater les grandes opportunités"

Timeframe : 4h
Positions max : 40
"""

import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.persistence import Trade
import math
from functools import reduce
from datetime import datetime
from typing import Optional, Dict

import logging
logger = logging.getLogger(__name__)

from freqtrade.strategy import (
    CategoricalParameter, 
    DecimalParameter, 
    IntParameter, 
    IStrategy
)

import ta as clean_ta
from technical import qtpylib


def VWAPB(dataframe, window_size=20, num_of_std=1):
    """
    Calcule les bandes VWAP (Volume Weighted Average Price)
    
    Les bandes VWAP sont similaires aux bandes de Bollinger mais utilisent
    le prix pondéré par le volume au lieu d'une simple moyenne mobile.
    
    Args:
        dataframe: DataFrame contenant les données OHLCV
        window_size: Période de calcul (par défaut 20)
        num_of_std: Nombre d'écarts-types pour les bandes (par défaut 1)
    
    Returns:
        Tuple contenant (bande_basse, vwap, bande_haute)
    """
    df = dataframe.copy()
    df['vwap'] = qtpylib.rolling_vwap(df, window=window_size)
    rolling_std = df['vwap'].rolling(window=window_size).std()
    df['vwap_low'] = df['vwap'] - (rolling_std * num_of_std)
    df['vwap_high'] = df['vwap'] + (rolling_std * num_of_std)
    return df['vwap_low'], df['vwap'], df['vwap_high']


def lerp(a: float, b: float, t: float) -> float:
    """
    Interpolation linéaire entre deux valeurs
    
    Utilisée pour ajuster progressivement les montants de DCA
    en cas de remplissage partiel des ordres.
    
    Args:
        a: Valeur de départ
        b: Valeur d'arrivée  
        t: Facteur d'interpolation (0 = a, 1 = b)
    
    Returns:
        Valeur interpolée entre a et b
    """
    return (1 - t) * a + t * b


class simple_vwap_v1(IStrategy):
    """
    Simple VWAP v1 - Stratégie d'exposition maximale au marché
    
    Cette stratégie utilise une approche unique avec :
    - Des conditions d'entrée ultra-permissives (VWAP touch)
    - Un filtre de confirmation sur 8 bougies consécutives
    - Des sorties fréquentes basées sur EMA et CCI
    - Un système de DCA intelligent avec jusqu'à 4 rechargements
    - Une gestion dynamique de la taille des positions selon la volatilité
    """
    
    # =================================================================
    # Configuration de base de la stratégie
    # =================================================================
    
    INTERFACE_VERSION = 3
    timeframe = '4h'
    can_short = False
    
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count = 300  # Besoin d'historique pour l'EMA 180
    position_adjustment_enable = True
    
    # Configuration des ordres
    order_types = {
        'entry': 'limit',
        'exit': 'limit', 
        'stoploss': 'market',
        'stoploss_on_exchange': False
    }
    order_time_in_force = {'entry': 'GTC', 'exit': 'GTC'}
    
    # ROI et stoploss par défaut (désactivés car gérés manuellement)
    minimal_roi = {"0": 100.0}
    stoploss = -1
    trailing_stop = False
    
    # =================================================================
    # Configuration du graphique pour l'interface
    # =================================================================
    
    plot_config = {
        "main_plot": {
            # Indicateurs principaux sur le graphique des prix
            "EMA": {"color": "#ffffff", "type": "line"},
            "C-EMA": {"color": "#ff0000", "type": "scatter", "scatterSymbolSize": 10},
            
            "VWAP_low": {"color": "#53c3eb", "type": "line"},
            "I-VWAP": {"color": "#00ff00", "type": "scatter", "scatterSymbolSize": 10},
        },
        "subplots": {
            # CCI dans un sous-graphique séparé
            "CCI": {
                "CCI": {"color": "#ffffff", "type": "line"},
                "C-CCI": {"color": "#ff0000", "type": "scatter", "scatterSymbolSize": 6},
            }
        }
    }
    
    # =================================================================
    # Paramètres de gestion du risque (optimisables)
    # =================================================================
    
    # Stoploss personnalisé : détermine la perte maximale acceptée
    # Valeurs : -0.45 à -0.2 (de -45% à -20%)
    # Par défaut : -0.37 (-37%)
    my_custom_stoploss = DecimalParameter(
        -0.45, -0.2, 
        decimals=2, 
        default=-0.37, 
        space="sell", 
        optimize=True
    )
    
    # Ratio du capital total à utiliser pour le trading
    # Permet de garder une réserve de sécurité
    # Valeurs : 0.25 à 1.0 (25% à 100% du capital)
    # Par défaut : 0.35 (35%)
    tradable_balance_ratio = CategoricalParameter(
        [0.25, 0.35, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0], 
        default=0.35, 
        space="buy", 
        optimize=True
    )
    
    # =================================================================
    # Paramètres DCA (Dollar Cost Averaging)
    # =================================================================
    
    # Déclencheur du premier safety order
    # Si la position perd plus que cette valeur, on peut recharger
    # Valeurs : -0.03 à 0.0 (-3% à 0%)
    # Par défaut : -0.012 (-1.2%)
    initial_safety_order_trigger = DecimalParameter(
        -0.03, 0.0, 
        decimals=3, 
        default=-0.012, 
        space="buy", 
        optimize=True
    )
    
    # Nombre maximum de safety orders (rechargements)
    # Plus ce nombre est élevé, plus on peut moyenner à la baisse
    # Valeurs : 3 à 12
    # Par défaut : 4
    max_so_multiplier_orig = IntParameter(
        3, 12, 
        default=4, 
        space="buy", 
        optimize=True
    )
    
    # Espacement entre les safety orders
    # Plus la valeur est élevée, plus les ordres sont espacés
    # Valeurs : 1 à 10  
    # Par défaut : 3
    safety_order_step_scale = IntParameter(
        1, 10, 
        default=3, 
        space="buy", 
        optimize=True
    )
    
    # Multiplicateur de volume pour chaque safety order
    # Détermine de combien augmente la taille de chaque ordre suivant
    # Valeurs : 1.3 à 2.3
    # Par défaut : 1.8
    safety_order_volume_scale = DecimalParameter(
        1.3, 2.3, 
        decimals=1, 
        default=1.8, 
        space="buy", 
        optimize=True
    )
    
    # =================================================================
    # Variables internes
    # =================================================================
    
    cust_proposed_initial_stakes = {}
    max_so_multiplier = 4  # Sera recalculé dynamiquement
    
    # =================================================================
    # Méthodes de gestion des positions
    # =================================================================
    
    def custom_exit(self, pair: str, trade: 'Trade', current_time: 'datetime', 
                   current_rate: float, current_profit: float, **kwargs):
        """
        Gestion personnalisée des sorties avec stoploss ajustable
        
        Cette méthode est appelée à chaque tick pour vérifier si une position
        doit être fermée. Elle implémente un stoploss personnalisé qui peut
        être optimisé indépendamment du stoploss par défaut de Freqtrade.
        
        Args:
            pair: Paire de trading (ex: BTC/USDT)
            trade: Objet Trade contenant les infos de la position
            current_time: Timestamp actuel
            current_rate: Prix actuel
            current_profit: Profit/perte actuel en ratio (0.1 = +10%)
            
        Returns:
            'stop_loss' si le stoploss est atteint, None sinon
        """
        # Vérifier d'abord les conditions de sortie par défaut
        tag = super().custom_sell(pair, trade, current_time, current_rate, 
                                 current_profit, **kwargs)
        if tag:
            return tag
            
        # Appliquer notre stoploss personnalisé
        if current_profit <= self.my_custom_stoploss.value:
            return 'stop_loss'
            
        return None
    
    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, 
                          amount: float, rate: float, time_in_force: str, 
                          exit_reason: str, current_time: datetime, **kwargs) -> bool:
        """
        Confirmation finale avant fermeture d'une position
        
        Nettoie les données internes quand une position est complètement fermée.
        
        Returns:
            True pour confirmer la sortie
        """
        # Nettoyer les données si on ferme complètement la position
        if trade.amount == amount and pair in self.cust_proposed_initial_stakes:
            del self.cust_proposed_initial_stakes[pair]
            
            # Nettoyage spécifique pour backtest/hyperopt
            if self.dp.runmode.value in ("backtest", "hyperopt"):
                for t in self._open_trades:
                    if t.get('pair') == pair:
                        self._open_trades.remove(t)
                        break
        return True
    
    def custom_stake_amount(self, pair: str, current_time: datetime, 
                           current_rate: float, proposed_stake: float, 
                           min_stake: float, max_stake: float, **kwargs) -> float:
        """
        Calcul intelligent de la taille de position
        
        Cette méthode ajuste la taille de chaque position en fonction de :
        1. La volatilité actuelle (ATR)
        2. Le nombre maximum de positions
        3. Le ratio de capital alloué au trading
        4. Les minimums requis par l'exchange
        
        La réduction basée sur la volatilité protège le capital lors
        des périodes de forte volatilité.
        
        Args:
            pair: Paire de trading
            current_time: Timestamp actuel
            current_rate: Prix actuel
            proposed_stake: Montant proposé par Freqtrade
            min_stake: Montant minimum de l'exchange
            max_stake: Montant maximum autorisé
            
        Returns:
            Montant ajusté pour la position
        """
        base_stake = proposed_stake
        
        # =============================================================
        # Ajustement basé sur la volatilité
        # =============================================================
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is not None and not dataframe.empty:
            # Calculer l'ATR sur les 15 dernières bougies
            volatility_window = 15
            recent_high = dataframe['high'].iloc[-volatility_window:]
            recent_low = dataframe['low'].iloc[-volatility_window:]
            recent_close = dataframe['close'].iloc[-1]
            
            # ATR en pourcentage du prix
            recent_atr_pct = (recent_high - recent_low).mean() / recent_close
            
            # Réduire la taille si volatilité élevée
            volatility_threshold = 0.102  # 10.2%
            
            if recent_atr_pct > volatility_threshold:
                # Volatilité très élevée : réduire de 50%
                volatility_adjustment = 0.5
            elif recent_atr_pct > volatility_threshold * 0.6:
                # Volatilité modérée : réduire de 25%
                volatility_adjustment = 0.75
            else:
                # Volatilité normale : taille complète
                volatility_adjustment = 1.0
                
            base_stake *= volatility_adjustment
        
        # =============================================================
        # Calcul du quota par position
        # =============================================================
        ratio = self.tradable_balance_ratio.value
        max_trades = self.config.get("max_open_trades", 40)
        
        # Capital disponible total
        available_wallet = self.wallets.get_total_stake_amount()
        
        # Quota = (Capital * Ratio) / Nombre de positions max
        quota = (available_wallet * ratio) / max_trades
        
        # Ne pas dépasser le quota calculé
        base_stake = min(base_stake, quota)
        
        # =============================================================
        # Application des limites
        # =============================================================
        # Minimum requis par Hyperliquid
        stake_final = max(15.0, base_stake)
        
        # Ne pas dépasser le maximum autorisé
        stake_final = min(stake_final, max_stake)
        
        return stake_final
    
    def adjust_trade_position(self, trade: Trade, current_time: datetime, 
                            current_rate: float, current_profit: float, 
                            min_stake: float, max_stake: float, **kwargs) -> Optional[float]:
        """
        Système de DCA (Dollar Cost Averaging) intelligent
        
        Cette méthode implémente un système de rechargement progressif
        des positions perdantes. Les rechargements sont déclenchés à des
        seuils de perte croissants et avec des montants croissants.
        
        Exemple avec les paramètres par défaut :
        - 1er DCA à -1.2% avec 1.8x le montant initial
        - 2e DCA à -3.6% avec 3.24x le montant initial  
        - 3e DCA à -10.8% avec 5.83x le montant initial
        - 4e DCA à -32.4% avec 10.5x le montant initial
        
        Args:
            trade: Position actuelle
            current_time: Timestamp
            current_rate: Prix actuel
            current_profit: Profit/perte actuel
            min_stake: Montant minimum
            max_stake: Montant maximum
            
        Returns:
            Montant à ajouter à la position ou None
        """
        # Ne rien faire si on n'est pas en perte suffisante
        if current_profit > self.initial_safety_order_trigger.value:
            return None
        
        # Récupérer l'historique des ordres
        filled_buys = trade.select_filled_orders(trade.entry_side)
        count_of_buys = len(filled_buys)
        
        # Vérifier qu'on n'a pas atteint la limite de DCA
        if not (1 <= count_of_buys <= self.max_so_multiplier_orig.value):
            return None
            
        # =============================================================
        # Calcul du seuil de déclenchement pour ce DCA
        # =============================================================
        if self.safety_order_step_scale.value == 1:
            # Espacement linéaire
            safety_order_trigger = abs(self.initial_safety_order_trigger.value) * count_of_buys
        else:
            # Espacement exponentiel (recommandé)
            initial_trigger = abs(self.initial_safety_order_trigger.value)
            scale = self.safety_order_step_scale.value
            
            if scale > 1:
                # Formule pour progression géométrique croissante
                numerator = scale * (pow(scale, count_of_buys - 1) - 1)
                denominator = scale - 1
                safety_order_trigger = initial_trigger + (initial_trigger * scale * numerator / denominator)
            else:
                # Formule pour progression géométrique décroissante
                numerator = 1 - pow(scale, count_of_buys - 1)
                denominator = 1 - scale
                safety_order_trigger = initial_trigger + (initial_trigger * scale * numerator / denominator)
        
        # =============================================================
        # Vérifier si on a atteint le seuil de DCA
        # =============================================================
        if current_profit > (-1 * abs(safety_order_trigger)):
            return None
            
        # =============================================================
        # Calcul du montant du DCA
        # =============================================================
        try:
            # Montant du premier ordre
            actual_initial_stake = filled_buys[0].cost
            
            # Calculer le montant pour ce DCA
            stake_amount = actual_initial_stake * pow(
                self.safety_order_volume_scale.value, 
                count_of_buys - 1
            )
            
            # Compensation pour les ordres partiellement remplis
            if trade.pair in self.cust_proposed_initial_stakes:
                proposed_initial = self.cust_proposed_initial_stakes[trade.pair]
                if proposed_initial > 0:
                    # Calculer ce qui aurait dû être investi
                    already_bought = sum(buy.cost for buy in filled_buys)
                    theoretical_bought = proposed_initial * sum(
                        pow(self.safety_order_volume_scale.value, i) 
                        for i in range(count_of_buys)
                    )
                    
                    # Ajuster si nécessaire
                    if theoretical_bought > already_bought:
                        compensation = theoretical_bought - already_bought
                        stake_amount += compensation * 0.4  # 40% de compensation
            
            return stake_amount
            
        except Exception as e:
            logger.error(f'Erreur DCA pour {trade.pair}: {str(e)}')
            return None
    
    def get_max_so_multiplier(self):
        """
        Calcule le multiplicateur total nécessaire pour tous les DCA
        
        Cette valeur est utilisée pour réserver suffisamment de capital
        dès le premier ordre afin de pouvoir effectuer tous les DCA prévus.
        
        Exemple : avec 4 DCA et un multiplicateur de 1.8 :
        - Total = 1 + 1.8 + 3.24 + 5.83 + 10.5 = 22.37
        - Le premier ordre ne représentera que 1/22.37 du capital alloué
        
        Returns:
            Multiplicateur total pour la répartition du capital
        """
        max_orders = self.max_so_multiplier_orig.value
        scale = self.safety_order_volume_scale.value
        
        if max_orders <= 0:
            return 1
            
        if scale == 1:
            # Cas simple : tous les ordres ont la même taille
            return max_orders + 1
            
        # Calcul de la somme d'une série géométrique
        # 1 + scale + scale² + ... + scale^(n-1)
        if scale > 1:
            total = 1 + scale * (pow(scale, max_orders) - 1) / (scale - 1)
        else:
            total = 1 + scale * (1 - pow(scale, max_orders)) / (1 - scale)
            
        return total
    
    # =================================================================
    # Indicateurs techniques
    # =================================================================
    
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Calcul des indicateurs techniques
        
        Cette méthode calcule tous les indicateurs nécessaires :
        1. EMA 180 : Tendance de fond sur ~30 jours
        2. CCI 160 : Momentum et surachat/survente
        3. VWAP : Prix moyen pondéré par volume
        
        Les indicateurs préfixés par 'I-' sont pour les entrées (vert)
        Les indicateurs préfixés par 'C-' sont pour les sorties (rouge)
        """
        # =============================================================
        # Indicateurs principaux
        # =============================================================
        
        # EMA 180 périodes (30 jours en 4h)
        dataframe['EMA'] = clean_ta.trend.ema_indicator(
            close=dataframe['close'], 
            window=180
        )
        
        # CCI (Commodity Channel Index) 160 périodes
        dataframe['CCI'] = clean_ta.trend.CCIIndicator(
            high=dataframe['high'], 
            low=dataframe['low'], 
            close=dataframe['close'], 
            window=160, 
            constant=0.0175  # Constante personnalisée
        ).cci()
        
        # VWAP avec bandes (5 périodes, 1.05 écart-type)
        dataframe["VWAP_low"], _, _ = VWAPB(
            dataframe, 
            window_size=5, 
            num_of_std=1.05
        )
        
        # =============================================================
        # Indicateurs pour l'affichage graphique
        # =============================================================
        
        # Signal d'entrée : VWAP touché
        dataframe['I-VWAP'] = np.where(
            (dataframe['VWAP_low'] < dataframe['high']), 
            dataframe['VWAP_low'], 
            np.nan
        )
        
        # Signal de sortie : EMA en baisse
        dataframe['C-EMA'] = np.where(
            (dataframe['EMA'] < dataframe['EMA'].shift(50)), 
            dataframe['close'], 
            np.nan
        )
        
        # Signal de sortie : CCI négatif et en baisse
        dataframe['C-CCI'] = np.where(
            ((dataframe['CCI'] < 0) & (dataframe['CCI'] < dataframe['CCI'].shift(1))), 
            0,  # Ligne zéro pour la visibilité
            np.nan
        )
        
        return dataframe
    
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Conditions d'entrée en position
        
        La stratégie n'a qu'une seule condition très simple :
        - Le prix doit toucher la bande basse du VWAP
        
        MAIS avec une subtilité importante : le signal doit persister
        pendant 8 bougies consécutives (32 heures) avant d'ouvrir.
        
        Cette confirmation filtre les faux signaux tout en gardant
        une approche très ouverte aux entrées.
        """
        conditions = []
        
        # Condition unique : prix touche la bande basse VWAP
        conditions.append((dataframe['VWAP_low'] < dataframe['high']))
        
        # Marquer les bougies qui remplissent la condition
        dataframe.loc[
            reduce(lambda x, y: x & y, conditions), 
            'enter_long'
        ] = 1
        
        # =============================================================
        # Filtre de confirmation : 8 bougies consécutives
        # =============================================================
        # Le signal n'est validé que s'il persiste pendant 8 bougies
        # Cela évite les entrées sur des pics isolés
        cumulative_window = 8
        dataframe['enter_long'] = dataframe['enter_long'].rolling(
            window=cumulative_window
        ).min()
        
        return dataframe
    
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Conditions de sortie de position
        
        La stratégie utilise deux conditions de sortie (en OU) :
        1. EMA en baisse sur 50 périodes (tendance qui s'essouffle)
        2. CCI négatif ET en baisse (momentum baissier)
        
        Ces conditions se déclenchent fréquemment, ce qui fait que
        les positions sont rarement gardées très longtemps.
        """
        conditions = []
        
        # Condition 1 : EMA décroît sur 50 périodes
        ema_shift = 50
        conditions.append(
            (dataframe['EMA'] < dataframe['EMA'].shift(ema_shift))
        )
        
        # Condition 2 : CCI négatif ET en baisse
        conditions.append(
            ((dataframe['CCI'] < 0) & 
             (dataframe['CCI'] < dataframe['CCI'].shift(1)))
        )
        
        # Sortir si AU MOINS UNE condition est remplie (OU logique)
        if conditions:
            dataframe.loc[
                reduce(lambda x, y: x | y, conditions), 
                'exit_long'
            ] = 1
            
        return dataframe
