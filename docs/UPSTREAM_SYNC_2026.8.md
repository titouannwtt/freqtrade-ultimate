# Sync amont — freqtrade 2026.6 → 2026.8

Ce document consigne le plan de synchronisation vers la version stable
`2026.8` publiée le 31/08/2026 par upstream (`freqtrade/freqtrade`). Le
fork est actuellement figé à `2026.6` (voir `freqtrade/__init__.py`), ce
qui laisse deux releases mensuelles à intégrer : `2026.7` (juillet) et
`2026.8` (août).

Comme la branche `main` du fork est un squash sans ancêtre commun avec
`upstream/stable`, un `git merge upstream/stable` produit un conflit
`add/add` sur ~2 000 fichiers. La synchronisation doit donc être menée
en trois passes ordonnées ; ce fichier documente les zones sensibles à
lire avant chaque passe.

## Résumé des releases amont

### 2026.7 (31/07/2026)
- Améliorations perf backtest (export signals vectorisé).
- Filtrage timerange pour trades en parquet, filtrage Arrow pour OHLCV
  feather/parquet ; feather redevient la reco par défaut.
- OKX / MyOKX : support stop-market + stop-limit ; stop-market étendu
  au spot OKX.
- Mappings d'exchange ajoutés : Bybit EU, Gate EU.
- FreqAI : backtest de paires avec historique partiel (listing tardif),
  meilleure gestion des échecs d'entraînement.
- Dépréciation : `from freqtrade.vendor.qtpylib` → recommander
  `from technical import qtpylib`.

### 2026.8 (31/08/2026)
- **Behavioural change** : `@informative` gagne un cache d'appel activé
  par défaut ; impacte les stratégies qui font des opérations
  non-dataframe dans la fonction décorée.
- `VolatilityFilter` et `RangeStabilityFilter` : nouveaux paramètres
  `lookback_period` / `lookback_timeframe`.
- `VolatilityFilter` : correctif — la première ligne du lookback ne
  valait plus zéro, la moyenne s'aligne, donc les backtests bougent
  légèrement.
- Hyperopt : avertissement quand des `Parameters` sont utilisés dans
  `populate_indicators`.
- `--timerange` accepte les formats heure/minute (`20260101T1500-…`).
- **Suppression** du support Bitmart (l'exchange a fermé fin juillet).

## Zones à impact — fichiers touchés en amont ET modifiés par le fork

Le diff `main ↔ 2026.8` sur `freqtrade/` a 112 fichiers modifiés côté
amont, dont **45 en collision avec les modifications spécifiques au
fork**. Trois catégories sont critiques pour un fork qui trade en
production :

### Bloc Hyperliquid & exécution ordres
- `freqtrade/exchange/hyperliquid.py` — amont : correction du
  `lastTradeTimestamp`. Le fork y a du code spécifique
  (external-close, liquidation, shared-wallet exit guard) ; à merger
  ligne à ligne, ne pas écraser.
- `freqtrade/exchange/exchange.py`, `exchange_types.py`, `exchange_ws.py`,
  `binance.py`, `bybit.py`, `gate.py`, `kraken.py`, `krakenfutures.py`,
  `okx.py`, `bitget.py`, `common.py` — la fenêtre de shutdown WebSocket
  a été retravaillée en `2026.7` ; le fork a sa propre logique de
  résilience à préserver.
- `freqtrade/exchange/bitmart.py` — amont a supprimé le fichier. À
  supprimer côté fork après vérification qu'aucune conf ne le
  référence.

### Bloc portefeuille & persistance
- `freqtrade/wallets.py` — amont : normalisation de la quote balance,
  positionWallet garde l'unrealized PnL, on ne persiste plus les
  balances à 0, funding_fees inclus dans la migration wallet. Le fork
  lit les soldes par compte (fleet), il faut réconcilier ligne à ligne.
- `freqtrade/persistence/trade_model.py` — retouché en amont pour la
  migration wallet ; à vérifier vis-à-vis des additions Hyperliquid
  côté fork.

### Bloc pairlists (impact MultiMarketPairList)
- `freqtrade/plugins/pairlist/VolatilityFilter.py`,
  `VolumePairList.py` — amont : refactor du lookback partagé, ajout
  `lookback_period` sur `VolatilityFilter`. Le fork chaîne des
  pairlists par marché (`MultiMarketPairList`) ; à retester avec les
  nouveaux paramètres.
- `rangestabilityfilter.py`, `IPairList.py`, `StaticPairList.py`,
  `pairlist_helpers.py` — également touchés amont.

### Autres zones à vérifier
- `freqtrade/freqtradebot.py` — la boucle centrale ; amont a des
  ajustements d'informative cache et de wallet migration à intégrer.
- `freqtrade/rpc/api_server/api_v1.py`, `rpc.py`, `telegram.py`,
  `webhook.py` — nouveaux endpoints amont vs. endpoints ajoutés
  par le fork (fleet snapshot, profit_history agrégé).
- `freqtrade/optimize/backtesting.py`, `hyperopt/hyperopt.py`,
  `hyperopt_optimizer.py` — perfs de backtest amont ; le fork a des
  extensions replay/hyperopt à préserver.
- `freqtrade/freqai/data_kitchen.py`, `freqai_interface.py` —
  fix historic predictions dtypes / reprocessing.
- `freqtrade/config_schema/config_schema.py`,
  `configuration/configuration.py`, `configuration/timerange.py` —
  nouveaux champs (`lookback_period`, timerange minute).
- `freqtrade/strategy/interface.py`, `parameters.py`,
  `strategy/informative_decorator.py` — support du cache
  `@informative` (nouveauté principale de 2026.8).
- `freqtrade/util/datetime_helpers.py`, `util/formatters.py` — helpers
  timerange.

## Procédure de synchronisation recommandée

1. **Passe 1 — fichiers non conflictuels** (67 fichiers touchés amont
   mais pas par le fork) : appliquer directement via
   `git checkout 2026.8 -- <path>` pour ces chemins-là. Vérifier
   `pytest tests/test_freqtradebot.py -x` après.
2. **Passe 2 — collisions "cosmétiques"** : docs, tests unitaires
   d'exchanges non-Hyperliquid, templates `.j2`, `qtpylib/indicators.py`.
   Import à un endroit unique, revue rapide.
3. **Passe 3 — zones sensibles trading** (le bloc "Zones à impact"
   ci-dessus) : reprise ligne à ligne, exécution de la suite complète
   `pytest --random-order -n auto`, et **replay** sur une journée de
   données réelles avant tout dry-run (cf. `.claude-tips/replay.md`).

## Points d'attention avant merge

- **Cache `@informative`** activé par défaut : passer en revue les
  stratégies live/dry qui font autre chose que du dataframe dans
  `@informative` (calls réseau, journalisation, mutations d'état).
- **VolatilityFilter** : les backtests peuvent diverger légèrement à
  cause de la moyenne corrigée. Comparer les hyperopt figés.
- **Bitmart** : vérifier `user_data/` et `CLAUDE.local.md` — supprimer
  toute référence.
- **Hyperliquid `lastTradeTimestamp`** : bien préserver le comportement
  du fork (stamp au moment de la détection, pas au fill entry) tout en
  intégrant la correction amont.
- Après merge : `pip install -e .` puis `ruff check freqtrade/` +
  `mypy freqtrade/`.

## Références

- Release 2026.7 : <https://github.com/freqtrade/freqtrade/releases/tag/2026.7>
- Release 2026.8 : <https://github.com/freqtrade/freqtrade/releases/tag/2026.8>
- Diff amont : `git diff 2026.6..2026.8 -- freqtrade/`
- Chevauchement fork : `git diff --name-only main 2026.6 -- freqtrade/`
