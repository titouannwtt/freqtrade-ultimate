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

## Echelle DCA : composer sur le cumul au lieu de la mise initiale (famille hippo, corrige 2026-10-07)

Pattern herite du template DCA "safety order" (NostalgiaForInfinity / Elliot-like). Dans la
branche de compensation de remplissage partiel, l'echelle multipliait `already_bought`, le
cumul deja engage, au lieu de la mise initiale :

```python
# FAUX
current_actual_stake = already_bought * volume_scale ** (count_of_buys - 1)
```

Juste pour le 1er renfort (`volume_scale ** 0 == 1`), faux ensuite. Avec
`safety_order_volume_scale = 1.8` et `max_so_multiplier_orig = 3`, l'echelle complete
demandait **23,74 x la mise initiale** au lieu des **7,04 x** pour lesquels
`custom_stake_amount` dimensionne la premiere entree (`proposed_stake / max_so_multiplier`).

Trois symptomes, tous observes en live sur `hippo_original_multi` :

1. le dernier palier etait TOUJOURS refuse par l'enveloppe de capital, donc une echelle
   annoncee a 3 crans n'en avait que 2 ;
2. une position pouvait depasser 100 % de son propre slot de capital en empruntant au
   second slot (mesure : 103 % en backtest, et `SKY/USDC:USDC` a 2491 USDC de marge pour
   une mise initiale de 445, soit 51 % du capital deployable du bot) ;
3. le resultat dependait d'un redemarrage du bot : `cust_proposed_initial_stakes` est un
   dictionnaire en memoire, et la branche de repli utilisee quand il est vide calculait,
   elle, la bonne valeur. Meme trade, deux echelles selon qu'un restart a eu lieu.

Formulation correcte, qui preserve l'intention de compensation de remplissage partiel :

```python
nominal  = proposed_initial_stake * volume_scale ** (count_of_buys - 1)
expected = proposed_initial_stake * ladder_cumulative(count_of_buys - 1, volume_scale)
stake    = lerp(nominal, nominal + max(0.0, expected - already_bought), compensation_scale)
stake    = min(stake, max(0.0, proposed_initial_stake * max_so_multiplier - already_bought))
```

ou `ladder_cumulative(k, vs) = 1 + (vs**k - 1) / (vs - 1)` est le cumul nominal apres `k`
renforts, en multiples de la mise initiale. Le `min` final est un plafond dur : l'echelle
ne depasse jamais `max_so_multiplier x mise initiale`, et il rattrape proprement une
position deja en surchauffe (sur `SKY`, il ramene le dernier palier de 8082 a 641 USDC).

**Reflexe de review** : sur toute echelle DCA, verifier que la somme des paliers vaut
exactement le diviseur utilise par `custom_stake_amount`. Si elle vaut davantage, le
dernier palier est decoratif.

### MAIS : corriger cette echelle naivement COUTE de la performance (mesure 2026-10-07)

Resultat contre-intuitif, a connaitre avant de "corriger" ce pattern ailleurs. Composer sur
le cumul n'est pas qu'une erreur de calcul : c'est une **martingale nettement plus
agressive** (paliers 1 / 3.6 au lieu de 1 / 1.8), qui tire la moyenne d'entree vers le bas
beaucoup plus vite, donc qui **sort plus tot et plus souvent en profit**.

Backtest 6 mois (2026-04-07 -> 2026-10-05), 72 paires, hippo_original_multi, drawdown
recalcule positions ouvertes incluses :

  version                                     trades   profit   drawdown open-inclusif
  echelle d'origine (compose sur le cumul)       142   +601.60                  -5.01 %
  echelle bornee, stop -35 % inchange            136   -332.37                 -54.63 %
  echelle bornee + stop resserre a -25 %         136   -127.65                 -40.44 %

Tout l'ecart vient d'UN trade : ZEC/USDC du 2026-06-04, qui a chute de 52 % en dix heures.
L'echelle d'origine, en sur-finançant son 2e palier (1214 USDC au lieu de 607), avait une
moyenne d'entree assez basse pour sortir au ROI **huit heures avant le point bas**
(+9.40). L'echelle bornee garde la moyenne plus haute, ne recupere pas, et realise -660.

Simulation exhaustive (113 229 bougies d'entree candidates, meme fenetre), l'echelle
d'origine correctement modelisee :

  queue de risque (pire excursion)   origine   bornee   bornee + stop -25 %
  centile 1                           -7.28 %  -8.21 %              -8.21 %
  centile 0,1                        -32.78 % -28.98 %             -25.12 %
  pire                               -37.92 % -38.95 %             -32.06 %
  stop touche, crypto                 0.132 %  0.109 %              0.216 %
  liquidation                         0.000 %  0.000 %              0.000 %

L'echelle d'origine est donc MEILLEURE dans le corps de la distribution et son pire cas
absolu est equivalent ; l'echelle bornee ne gagne qu'au centile 0,1. Aucune des deux ne
liquide jamais, le levier 1 sur crypto rendant la liquidation structurellement impossible
et les instruments xyz ne descendant pas assez bas a levier 2.

**Ce qui reste un vrai defaut de l'echelle d'origine**, independamment du P&L :
  - la position n'est bornee que par l'enveloppe de capital, pas par la strategie : elle a
    atteint 103 % de son propre slot en backtest, donc elle emprunte au second slot ;
  - le dernier palier est decoratif (toujours refuse) ;
  - le resultat depend d'un redemarrage du bot (branche de repli vs dictionnaire en
    memoire), donc deux executions du meme trade ne donnent pas la meme echelle.

**Conclusion operationnelle** : ne pas corriger l'echelle sans recalibrer en meme temps
`safety_order_volume_scale`. Une echelle bornee qui reproduit l'agressivite d'origine
demande vs ~= 3.6 (paliers 1 / 3.6 / 12.96, max_so_multiplier 18.56), donc une mise
initiale 2,6 fois plus petite : c'est un changement de sizing complet, a tester, pas un
correctif de calcul. Le patch de correction est conserve sur la branche
`fix/dca-ladder-capital-safety`, NON deploye.

## Enveloppe de capital et sizing qui mesurent deux capitaux differents

`max_position_stake_ratio` se jugeait sur `available_capital` brut, alors que le sizing
divise `available_capital - capital_withdrawal + profit realise` (x
`tradable_balance_ratio`) par `max_open_trades`. Sur un bot avec un retrait important les
deux bases n'ont aucun rapport, et l'echelle DCA tient ou ne tient pas par accident, sans
qu'aucun reglage ne le dise.

Cle ajoutee : `max_position_stake_basis` (`"allocated"` par defaut, comportement historique,
ou `"deployable"`). Sur la base deployable la condition devient invariante d'echelle :

    max_position_stake_ratio >= overbuy_factor / max_open_trades

Le garde-fou vit desormais dans `freqtrade/util/capital_envelope.py` et le **backtester
l'applique aussi**. Il etait live-only : un backtest finançait des echelles que le live
refuse, donc tout reglage de `max_position_stake_ratio` valide par backtest etait faux par
construction.
