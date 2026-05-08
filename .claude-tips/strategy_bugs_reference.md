# Bugs et anomalies connus dans les stratégies

Reference des patterns de bugs identifiés lors du triage mai 2026. A consulter lors de toute analyse/review de stratégie.

## Bugs systémiques des stratégies `generated_strategy*`

Toutes les stratégies `generated_strategy*` proviennent du meme template et partagent ces bugs:

### 1. leverage_value default hors du range CategoricalParameter

```python
leverage_value = CategoricalParameter([1, 2, 3], default=5, space="buy")
```

Le default est **en dehors** de la liste de choix. Freqtrade accepte silencieusement et utilise ce default en backtest. Rendements gonflés de 4-5x. Fix: default doit etre dans la liste.

### 2. startup_candle_count = 10 (toujours insuffisant)

Le template fixe `startup_candle_count = 10` quel que soient les indicateurs. Les lookbacks vont de 42 a 585 candles. Les indicateurs produisent NaN/garbage pendant les premieres centaines de candles. Fix: calculer depuis le max lookback + 20% marge.

### 3. Duplicate confirm_trade_exit

Deux definitions de `confirm_trade_exit` dans le meme fichier. Python utilise silencieusement la derniere. La premiere est du dead code. Fix: supprimer la premiere.

### 4. exit_only_profit gate

```python
exit_only_profit = BooleanParameter(default=True, space="sell")
```

Bloque toutes les sorties tant que le profit est sous le seuil. WR artificiellement gonfle, DD sous-estime. Les trades perdants s'accumulent et creent un drawdown latent massif en live. Fix: default=False.

### 5. stoploss = -1 combine avec leverage > 1x

Quand SL desactive et leverage 3-5x, le custom stoploss ne fire qu'apres une perte de 120-300% du capital. Le compte est liquide bien avant. Fix: `|SL| * leverage < 100%`.

### 6. stoploss = -1 declare deux fois

Doublon inoffensif mais symptomatique du copier-coller.

## Bugs generaux (toutes familles de strategies)

### 7. trailing_stop natif = phantom profits en backtest

Le trailing stop natif de freqtrade est bugue en backtest. Il catch des wicks intra-candle qui n'existeraient pas en live (tip #10). Fix: coder l'equivalent dans `custom_stoploss()` ou `custom_exit()`.

### 8. shift(+N) avec N>0 = future-looking

`dataframe['signal'].shift(+1)` regarde dans le futur. Resultats 100% fictifs. Detecte dans la famille beetcoin_* (tous deplaces vers delete/).

### 9. external_indicators typo reset_indessx

Les templates v9/v10/v11 avaient un typo `reset_indessx` au lieu de `reset_index`. Bloquait le chargement de ~140 strategies. Corrige en session precedente.

### 10. startup_candle_count trop bas (general)

Meme hors generated_strategy*, beaucoup de strategies ont un `startup_candle_count` insuffisant pour leurs indicateurs les plus lents. Verifier systematiquement: trouver le plus long lookback (shift, rolling, EMA, SMA, TRIX, RSI periods) et s'assurer que `startup_candle_count >= max_lookback * 1.2`.

### 11. Penny-picking pattern (tip #133)

WR > 95% et DD < 5% = toujours artificiel. Cause: profit gate + DCA illimite + stoploss=-1. Les pertes ne sont jamais realisees mais s'accumulent en unrealized. Explosion garantie sur le premier mouvement adverse majeur.

## Checklist rapide pour analyser une strategie

1. `startup_candle_count` >= max lookback de tous les indicateurs?
2. Pas de `shift(+N)` avec N>0?
3. Pas de `trailing_stop = True`? (si oui, recoder en custom)
4. `exit_only_profit` desactive ou explicitement voulu?
5. `stoploss * leverage < -100%` = risque de liquidation?
6. `CategoricalParameter` default dans la liste de choix?
7. Pas de duplicate de methodes (confirm_trade_exit, etc.)?
8. Backtest fait avec `--timeframe-detail 1m`?
