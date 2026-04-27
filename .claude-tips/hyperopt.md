# Hyperopt — Tips et garde-fous

Pour le journal d'optimisation HippoDCA v5, voir [HIPPODCA_V4_STUDY.md](../HIPPODCA_V4_STUDY.md).
Pour la méthodologie staged, voir [HYPEROPT_PLAYBOOK.md](../HYPEROPT_PLAYBOOK.md).

## Règles strictes (ne jamais enfreindre)

- 🚫 **Cap 200-300 epochs maximum**. Au-delà, sur-optimisation quasi garantie. Alternative robuste: 10-20 hyperopts de 200 epochs avec seeds différents et moyenner les JSON résultants. (tips.txt #14)
- 🚫 **Supprimer le `.json` co-localisé AVANT chaque hyperopt**. Il override silencieusement `buy_params` et `DecimalParameter` defaults. Si oublié, l'hyperopt peut varier des params qui n'ont aucun effet réel. (tips.txt #21)
- 🚫 **Jamais `tee` pour piper un hyperopt**. Bufferise les retours chariot et cache la progress bar. Le `.fthypt` persiste déjà toutes les epochs. (tips.txt #25)
- 🚫 **Toujours `--timeframe-detail 1m`**. Sans ça, fills simulés au prix d'ouverture de bougie → "phantom trades" inexistants en live. L'optimizer overfit ces faux fills. Avec 1m detail, DCA triggers, stoploss hits et exits simulés sur le mouvement intra-bougie réel — params moins nombreux mais meilleure qualité (SQN 12.88 vs 6.76 dans nos tests). Si 1m indispo, `--timeframe-detail 5m` acceptable mais **prévenir** que les résultats surestiment trades et profit. (tips.txt #20)
- 🚫 **Toujours entraîner sur l'exchange cible**. Params Binance USDT ne transfèrent PAS à Hyperliquid USDC. Liquidité, spreads, funding, price action différents. Une stratégie profitable Binance peut produire -99% DD sur Hyperliquid. (tips.txt #26)
- 🚫 **Toujours entraîner sur données récentes (12-18 mois max)**. Entraîner sur 6 ans (2020-2026) produit des params overfittés au bullrun 2021. Les gains massifs de cette période dominent toute loss function et produisent des entrées qui acceptent des pertes catastrophiques dans les conditions actuelles. (tips.txt #26)
- 🚫 **Aucun `logger.info` / `logger.debug` / `logger.warning` dans le code stratégie pendant l'hyperopt**. Chaque message × 28 workers × 40 paires × milliers de candles sature la log queue multiprocessing → main thread bloqué sur rich rendering → hyperopt **3× plus lent** ou stalls apparents. Pour backtest et live : tous niveaux OK. Pour hyperopt : seul `logger.error` reste. Pattern : `if logger.isEnabledFor(logging.DEBUG): logger.debug(...)`. Hot paths à auditer : `custom_stake_amount`, `confirm_trade_entry`, `adjust_trade_position`, `custom_exit`, `confirm_trade_exit`. (tips.txt #201)
- 🚫 **Hyperopt = piège d'overfitting**. La vraie edge vient de l'intuition et l'observation. 90% des stratégies rentables se construisent avec 2 indicateurs d'entrée + 1 indicateur de sortie. (tips.txt #38)
- 🚫 **"A very easy way to avoid over-fitting is to do no fitting at all."** Valeurs par défaut raisonnables > optimisation. (tips.txt #54, Carver)
- 🚫 **Zéro optimisation dans les backtests** (Clenow réplique des fonds trend-following avec règles non-optimisées). La volatilité est la variable principale, pas les params d'indicateurs. (tips.txt #72, Clenow)
- 🚫 **Le nombre d'essais tentés est l'info la plus importante et la plus absente**. 1000 epochs = probabilité élevée qu'au moins une soit "bonne" par hasard. (tips.txt #76, Lopez de Prado)
- 🚫 **Stabilité des params optimaux**. Si changer légèrement un param fait chuter la perf, c'est de l'overfitting. La recherche n'est pas une maximisation, c'est une preuve de robustesse. (tips.txt #81, Lopez de Prado)
- 🚫 **Meta-overfitting: holdout final jamais touché**. Même avec CPCV, tester des dizaines d'idées sur le même dataset trouvera un faux positif. (tips.txt #85, Lopez de Prado)
- 🚫 **Tenir le modèle LOIN des données le plus longtemps possible**. "Dès que tu regardes l'equity curve, tu fais de l'implicit fitting." (tips.txt #113, Carver)
- 🚫 **"Best system in backtest = often the luckiest"**. Sélectionner sur ROBUSTESSE, pas sur PEAK performance. (tips.txt #161, Chan)
- 🚫 **Log systématique de TOUS les backtests avec pénalité**. Si tu cherches depuis 6 mois, ajuster le seuil de significance vers le haut. (tips.txt #180, Lopez de Prado)

## Loss function — choix par stratégie

### Loss functions custom du fork

| Comportement stratégie | Loss function | Métriques clés |
|---|---|---|
| **DCA / mean-reversion** | `MoutonMeanRevHyperOptLoss` | 8 métriques additives: CAGR (22%), K-ratio (22%), PF (12%), quarterly (14%), payoff (8%), diversité corr-daily (8%), TUW santé (8%), confiance sqrt (6%). Gates multiplicatives: concentration sigmoid + exp DD (×8). Hard filters avec gradient: profit, trades, WR≥55%, DD≤45%, pairs≥5 |
| **Momentum / trend-following** | `MoutonMomentumHyperOptLoss` | 9 métriques additives: Sharpe (18%), payoff (18%), tail (14%), CAGR (13%), PF (9%), quarterly (9%), diversité corr-daily (7%), TUW santé (7%), confiance sqrt (5%). Gates multiplicatives: consec losses sigmoid + exp DD (×10). Hard filters avec gradient: profit, trades, DD≤40%, payoff≥0.5, pairs≥5 |
| Diversifié multi-pair | `Mouton2HyperOptLoss` | Analyse trimestrielle pondérée + multi-metric + per-pair scoring (75% global / 25% par paire) |

### Loss functions standard freqtrade

| Comportement stratégie | Use | Do NOT use |
|---|---|---|
| Patient (attend jours/semaines) | `CalmarHyperOptLoss` | `SharpeHyperOptLossDaily` (pénalise inactivité) |
| Fréquente (trades chaque jour) | `SharpeHyperOptLossDaily` | `CalmarHyperOptLoss` (ignore consistance) |
| Safety-first (DD = mort) | `MaxDrawDownHyperOptLoss` | Raw profit losses |

### Pourquoi MoutonMeanRev et pas Calmar ?

**Calmar** = profit / max drawdown, point. `MoutonMeanRevHyperOptLoss` utilise 8 métriques additives à poids honnêtes (ce que le poids dit = ce que la métrique pèse réellement) + 2 gates multiplicatives intentionnellement dominantes (concentration, DD exp). Architecture:

- **Additif (discrimine entre bonnes stratégies)**: CAGR 22%, K-ratio Kestner 22%, PF 12%, quarterly consistance+magnitude 14%, payoff 8%, diversité daily-corr 8%, TUW santé 8%, confiance sqrt 6%
- **Gates (élimine les mauvaises)**: concentration sigmoid centrée 0.4, exp(8×DD)
- **Hard filters avec gradient TPE**: WR≥55% (DCA doit reverter), DD≤45%, trades≥60, pairs≥5

Pas d'expectancy (redondant avec PF+payoff — 3 métriques pour la même info). Quarterly inclut maintenant la magnitude (pire trimestre vs moyenne), pas juste binaire profitable/pas. Diversité utilise des buckets journaliers pour la corrélation (le timestamp exact produisait une matrice creuse inutile).

### Pourquoi MoutonMomentum et pas Sharpe ?

**Sharpe** = return / volatilité, point. `MoutonMomentumHyperOptLoss` utilise 9 métriques additives + 2 gates multiplicatives. Architecture:

- **Additif**: Sharpe 18%, payoff 18%, tail ratio 14%, CAGR 13%, PF 9%, quarterly+magnitude 9%, diversité daily-corr 7%, TUW santé 7%, confiance sqrt 5%
- **Gates**: consec losses sigmoid centrée 12, exp(10×DD) — plus strict que MeanRev
- **Hard filters avec gradient TPE**: payoff≥0.5 (let profits run obligatoire), DD≤40%, trades≥40, pairs≥5

TUW et confiance sont additives (poids honnêtes), pas multiplicatives stackées. Seules les gates (consec losses, DD) sont multiplicatives — c'est voulu et documenté.

## Common traps (retour d'expérience)

1. **High win rate ≠ profitable.** DCA à 97% win rate peut perdre -99.7% du capital. Les 3% de trades perdants à haut leverage wipent tout. Un seul trade -100% (liquidation) efface des mois de profits. Toujours vérifier profit total ET max drawdown.

2. **All epochs identical = param sans effet.** 500 epochs avec mêmes trades et profit quel que soit le param = code path mort ou conditions dominantes qui rendent le param irrelevant.

3. **buy_params dict override trap.** Quand on crée Phase N+1 depuis Phase N, on DOIT updater `buy_params` dict avec les valeurs convergées. Le dict override les `DecimalParameter` defaults. Si on freeze un param avec `optimize=False` mais laisse l'ancienne valeur dans `buy_params`, freqtrade utilise la valeur du dict.

4. **Phase separation can backfire.** Freezer les entry params (trainés sur période A) puis optimizer les trading params sur période B ne corrige pas l'overfitting. Quand entry et trading params sont couplés (DCA), optimizer ensemble sur la période cible.

5. **`dry_run_wallet` must be realistic.** 100 USDC avec 5x leverage et DCA crée du sizing absurde. Utiliser un wallet réaliste (1000+ USDC).

6. **`max_open_trades` changes everything for DCA.** `proposed_stake = wallet / MOT`. MOT 5→3 = quasi-doublement du stake par trade. Fixer délibérément.

7. **Concentrated profit = red flag.** Si 1-2 trades portent >50% du profit holdout, la stratégie n'a pas prouvé d'edge — elle a eu de la chance sur un mouvement. Calculer le profit sans le top trade. Si ça collapse à near-zero ou négatif = overfitting à ce price action spécifique.

## Walk-forward validation

Toujours splitter les données :
- **Training** : 70-80% (ex: 2025-03 → 2026-01)
- **Holdout** : 20-30% restants (ex: 2026-01 → 2026-04)

Run un backtest unique (pas hyperopt) sur le holdout avec les meilleurs params. Si performance chute > 50% vs training = overfitting.

### Interpréter une baisse de fréquence en holdout

Une baisse de trade frequency n'est **pas nécessairement de l'overfitting** — le marché a pu offrir moins d'opportunités. Pour les DCA long oversold, moins de trades en marché calme = comportement sain.

Avant de conclure "overfitted", comparer les métriques de volatilité (ATR, daily range, volume) entre training et holdout. Si la vol a baissé proportionnellement au nombre de trades, les params sont OK.

**Mieux vaut être calibré pour haute volatilité que basse** — c'est là que le bot fait son argent. En marché calme il attend, et c'est exactement ce qu'on veut (d'où CalmarHyperOptLoss qui ne pénalise pas l'inactivité).

## Lancer un hyperopt

Toujours en `screen` avec notification Telegram :

```bash
screen -S <session> -X stuff $'.venv/bin/freqtrade hyperopt \
  --strategy <Strategy> \
  --config <config.json> \
  --timerange YYYYMMDD-YYYYMMDD \
  --timeframe 15m \
  --timeframe-detail 1m \
  --hyperopt-loss CalmarHyperOptLoss \
  --epochs 1000 \
  --spaces buy sell \
  --sampler TPESampler \
  -j 28 \
  && /home/moutonneux/.config/claude-notify/send.sh "Hyperopt <label> TERMINE"\n'
```

To check progress: `screen -S <session> -X hardcopy -h /tmp/out.txt && tail -20 /tmp/out.txt`
To stop gracefully: `screen -S <session> -X stuff $'\003'` (sends Ctrl+C)

**Ne jamais utiliser `pkill`/`kill`/`kill -9`** — laisse des workers orphelins en mémoire (joblib/loky). Toujours Ctrl+C via screen pour cleanup propre.

## Bonnes pratiques (toujours suivre sauf justification explicite)

- ✅ **Feature importance > backtesting**. La méthode dominante (rechercher par BT) revient à du data mining brut. (tips.txt #79, Lopez de Prado)
- ✅ **Re-fit uniquement si HF**. Pour un système slow (DCA mean-rev sur 15m), réagir à 2 mois de données récentes sur 5 ans d'historique est une erreur. Carver n'a pas changé ses params en 8 ans. (tips.txt #108, Carver)
