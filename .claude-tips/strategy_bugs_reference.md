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

**Resolution, 2026-10-07 : variante "f3", DEPLOYEE.**

    safety_order_volume_scale 3.6   max_so_multiplier_orig 2   overbuy_factor 1.0
    max_position_stake_ratio 0.55 sur max_position_stake_basis "deployable"

Reproduit EXACTEMENT le comportement de l'echelle d'origine (paliers 1 puis 3.6 x la mise
initiale, cumul 5.6 x) mais en le DECLARANT : plus de 3e palier decoratif, plafond dur dans
la strategie, et `overbuy_factor` 1.0 fait que l'echelle complete vaut exactement un slot,
donc `max_open_trades` positions pleines tiennent dans 100 % du capital.

  fenetre    avant      f3        ecart   drawdown avant -> f3
  18 mois  1557.13   1512.78     -2.8 %    -4.00 % -> -3.91 %
   6 mois   601.60    584.79     -2.8 %    -5.01 % -> -4.87 %
   4 mois   433.96    421.77     -2.8 %    -4.94 % -> -4.80 %

L'ecart de 2,8 % est integralement la taille (mise initiale 0.971 x l'ancienne) : memes
trades, meme repartition de paliers, profits dans le meme rapport sur les trois fenetres.
Verifie sur les deux positions ouvertes au moment du deploiement : comportement identique
(XYZ-GOLD palier suivant 1161.82 contre 1161.64 avant ; SKY exactement a son plafond
d'echelle, donc aucun palier, comme avant).

**Variantes evaluees et REJETEES sur 18 mois** (552 jours, 302 trades, 72 paires) :
  - 3 ou 4 paliers (vs 1.4 / 1.8 / 3.6) : profit par point de drawdown 31 a 38 contre ~390
    pour la famille courte. Le 4e palier s'est declenche UNE fois sur 302 trades. La
    couverture profonde assure contre un evenement que le signal d'entree ne rencontre
    pas, et se paie en divisant par trois la mise initiale, qui sert les 73 % de trades
    sans renfort. Mesure exhaustive contraire (22 x moins d'epuisement d'echelle) : elle
    juge chaque entree isolement, sans portefeuille ni occupation du capital, donc elle ne
    voit pas le cout d'un slot bloque par une position profonde.
  - `max_open_trades_per_market` a 1 : -25 % de profit (1557 -> 1168) et drawdown
    INCHANGE, legerement pire (-4.00 % -> -4.45 %). Le drawdown vient d'une position
    profonde isolee, pas d'une correlation entre positions : diversifier entre marches ne
    protege pas d'un risque concentre sur un trade.
  - `stoploss_price` a -0.25 : realise des pertes que l'echelle absorbait.
  - `overbuy_factor` 1.295 conserve (variante f1) : +24 % de profit mais 122,5 % du capital
    engage. C'est de la taille deguisee ; la monter doit passer par `available_capital`.

**Resultat structurel de l'etude** : dans cette famille, le profit par unite de capital
engage est constant a ~1520 et le profit par point de drawdown a ~390, quelle que soit la
forme de l'echelle ou le MOT. Ces parametres ne font que doser l'exposition ; ils ne
produisent pas de rendement ajusté au risque. Le seul reglage qui sort de cette ligne la
casse vers le bas (echelles profondes). Chercher un gain en jouant dessus est une impasse
mesuree.

### Pourquoi aucune echelle profonde ne marche ici : la DCA est un accelerateur de sortie

18 calibrages mesures sur 18 mois (552 jours, 72 paires) : `safety_order_volume_scale` de
0.6 a 3.6 (croissantes, plate, decroissantes), 2 a 4 paliers, `safety_order_step_scale` de
2 a 4, `max_so_multiplier` de 2.7 a 65. **Tout calibrage qui achete plus bas que -7 % du
prix d'entree multiplie le drawdown par 7 a 9**, independamment de la repartition des poids.

  calibrage                          profit   drawdown   profit/drawdown
  f1  vs3.6 n2 overbuy 1.295        1942.11     -4.80 %               405
  f3  vs3.6 n2 overbuy 1.0          1512.78     -3.91 %               387
  g5  vs0.7 n2 step 4               2003.64    -29.33 %                68
  g2  vs0.6 n4                      1610.47    -33.06 %                49
  g4  vs0.8 n3 step 3               1403.98    -32.99 %                43
  g1  vs0.8 n4                      1222.34    -32.75 %                37
  g3  vs1.0 n4                      1159.80    -34.91 %                33
  d1  vs1.8 n4                       787.09    -20.72 %                38

La colonne qui explique tout est la pire excursion sous la moyenne d'entree : **-10.56 %
pour f1/f3, -41 a -46 % pour toutes les autres.** Ce n'est pas que les echelles profondes
resistent mal, c'est que les positions de f3 n'atteignent jamais ces niveaux parce
qu'elles sont **deja fermees**. f3 met 3.6 unites d'un coup a -6.9 %, ce qui ramene son
point mort a -4.9 % et la fait sortir au premier rebond. Une echelle qui etale son capital
jusqu'a -30 % n'a pas cette moyenne basse, donc elle reste ouverte, et une position ouverte
dans un marche qui tombe de 50 % finit a -45 % de sa moyenne quel que soit le nombre de
munitions restantes.

**Dans cette famille la DCA n'est pas une assurance contre les baisses, c'est un
accelerateur de sortie.** Couvrir -30 % exige de rester dans la position jusqu'a -30 %, et
c'est le fait d'y rester qui coute, pas le capital depense.

PIEGE DE MESURE : la simulation exhaustive par bougie d'entree (161 820 points) classe g3
PREMIERE (epuisement d'echelle divise par 12, taux de stop meilleur que f3) alors que le
backtest la classe DERNIERE. Elle juge chaque entree isolement : une position qui survit en
restant ouverte trois semaines compte comme un succes, alors qu'elle a bloque un slot sur
deux, empeche une dizaine d'entrees et creuse le drawdown non realise pendant tout ce
temps. Pour un arbitrage de calibrage, c'est le backtest qui tranche.

### Filtres de pairlist : 8 variantes evaluees, toutes REFUTEES (2026-10-07)

Fenetre 2025-07-01 -> 2026-10-05 (461 j), f3 reevaluee dessus comme reference (278 trades,
1408.56, pire moins-value -192.9).

  filtre (tous ex ante)                     trades   profit   pire MV
  reference f3                                 278  1408.56    -192.9
  atr_max_pct 2.0 -> 1.5                       243  1151.00    -192.9
  univers crypto top 20 capitalisation         184   871.13    -192.9
  univers crypto top 10                        152   709.97    -401.6
  drawdown glissant 60 j > -55 %               105   513.69    -192.9
  distance au plus haut 60 j > -25 %            86   417.32    -192.9
  drawdown glissant 60 j > -40 %                68   302.71    -192.9
  close > ema_200 (calculee, jamais utilisee)   14    74.36     -26.9
  plancher de sortie en prix (0.0025)          278  1393.35    -192.9
  levier xyz 2.0 -> 1.0 (+ plancher)           278  1313.55    -192.9

**La pire moins-value est IDENTIQUE a -192.9 dans presque toutes les variantes**, sur le
meme trade : XMR/USDC, LEVIER 1, rang 9 de capitalisation, excursion -10,52 %, portant une
echelle complete de 1834 USDC. Aucun filtre de paire ne l'atteint (XMR les passe tous) et
le levier n'y change rien (deja a 1). Les filtres ne touchent pas au risque, ils coupent le
profit.

Diagnostic ex ante prealable (au 2025-08-15, donnees anterieures seules, baisse subie
jusqu'au 2025-11-01) : les criteres SONT correles a la baisse suivante (Spearman drawdown
90 j +0,641, rang de capitalisation -0,589, volatilite 30 j -0,537) mais les niveaux sont
tous catastrophiques en crash general : UNI rang 16 a fait -80,5 %, ADA rang 11 -70,9 %,
LINK rang 10 -66,2 %. Seuls BTC, BNB, ETH et SOL ont ete epargnes.

**Conclusion** : le risque de cette strategie ne vient pas des actifs a queue epaisse mais
de la TAILLE de la position. Un actif banal a levier 1 produit le pire moment du
portefeuille des qu'il porte 100 % de son slot. Seul levier demontre : le sizing (f1 -250.8,
f3 -193.6, f5 avec MOT 3 -129.1, proportionnel au slot).

PIEGE DE MESURE : ces 8 filtres affichaient des drawdowns de -3,99 % a -5,13 %, ce qui m'a
fait conclure a tort qu'ils degradaient le risque. Le drawdown open-inclusif se calcule sur
`BASE + realise + non_realise` : a risque absolu CONSTANT, moins de profit encaisse donne un
pic d'equity plus bas donc un drawdown relatif plus grand (923 de profit encaisse avant le
creux pour la reference, 135 pour le filtre le plus severe). Pour comparer des variantes a
nombre de trades different, regarder la pire moins-value en VALEUR ABSOLUE.

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

## Un stop place dans `custom_exit` est SUSPENDU quand un signal de sortie est present

Verifie dans `freqtrade/strategy/interface.py` (~1528) :

```python
if self.use_exit_signal:
    if exit_ and not enter:
        exit_signal = ExitType.EXIT_SIGNAL      # <- custom_exit JAMAIS appele
    else:
        reason_cust = ... self.custom_exit(...)  # <- le stop vivait ici
```

La famille hippo mettait son stop (-35 % de mouvement de prix) dans `custom_exit`, avec
`stoploss = -1` (natif desactive) et `use_custom_stoploss = False`. Des que `rsi > 60` (son
signal de sortie), la branche EXIT_SIGNAL est prise et le seuil n'est jamais evalue. Et ce
signal est lui-meme refuse par `confirm_trade_exit` puisque la position est en perte : donc
aucune sortie du tout pendant ces fenetres. Mesure sur SKY/USDC, 7 jours : RSI > 60 pendant
21,3 % du temps, sequences continues jusqu'a 245 minutes.

Preuve en base, avant correctif : tous les trades portaient `stop_loss_pct = -1` et
`stop_loss = 0.0` (SKY) ou `open_rate x 0.5` (GOLD a levier 2). **Aucun stop reel.**

Second defaut du meme bloc : `confirm_trade_exit` teste `exit_reason == "stop_loss"`, faux
puisque `custom_exit` renvoyait `f"stop_loss ({entry_tag})"`. Seul le test `_is_safety_exit`
(sous-chaine "stop") sauvait la sortie.

**Correctif (deploye le 2026-10-09)** : stop retire de `custom_exit`,
`use_custom_stoploss = True`, et

```python
def custom_stoploss(self, pair, trade, current_time, current_rate, current_profit,
                    after_fill, **kwargs) -> float | None:
    lev = float(getattr(trade, "leverage", None) or 1.0)
    return stoploss_from_open(MARKET_PARAMS[self.market_of(pair)]["stoploss_price"] * lev,
                              current_profit, is_short=trade.is_short, leverage=lev)
```

Trois subtilites indispensables :
  - `stoploss_from_open` divise par le levier en interne : MULTIPLIER par `lev` pour
    exprimer un mouvement de prix. Verifie, stop a `moyenne x 0.65` a levier 1 et 2.
  - `after_fill` DANS LA SIGNATURE fait passer `_ft_stop_uses_after_fill` a True, donc
    freqtrade rappelle la methode apres chaque remplissage avec `allow_refresh=True` et
    autorise le stop a DESCENDRE. Sans lui, le stoploss natif ne monte que et resterait
    bloque au niveau d'avant le renfort alors que le DCA a abaisse la moyenne : il
    declencherait AVANT les -35 % reels. Piege a faux negatif en backtest.
  - en backtest le stop natif est evalue sur le PLUS BAS de la bougie, `custom_exit` sur
    l'ouverture seule : le correctif rend le stop plus reactif, pas seulement plus fiable.

**Cout mesure : zero.** 18 mois, 72 paires, 302 trades, avec et sans : identiques
(1512.78, 0 perte, drawdown -3,91 %). Le stop ne s'est declenche dans aucune des deux
variantes, pire excursion -10,52 % contre un seuil a -35 %. Le backtest ne pouvait donc pas
valider le gain, seulement l'absence d'effet de bord.

**REFLEXE DE CONTROLE sur toute strategie de la flotte** avec `use_exit_signal = True` :
`select pair, stop_loss, stop_loss_pct from trades where is_open=1`. Si `stop_loss_pct`
vaut -1 ou si `stop_loss` vaut 0, aucun stop n'est porte au niveau de freqtrade, quoi que
dise le code de la strategie.
