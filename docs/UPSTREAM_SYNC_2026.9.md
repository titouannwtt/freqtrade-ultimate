# Sync amont — freqtrade 2026.8 → 2026.9

Ce document consigne le suivi de la release stable `2026.9` publiée le
`29/09/2026` par upstream (`freqtrade/freqtrade`,
[release notes](https://github.com/freqtrade/freqtrade/releases/tag/2026.9)).

**Le fork est toujours figé à `2026.6`** (`freqtrade/__init__.py`). La
dette amont accumulée est donc désormais `2026.7 + 2026.8 + 2026.9` —
lire d'abord [`UPSTREAM_SYNC_2026.8.md`](./UPSTREAM_SYNC_2026.8.md)
(couvre 2026.7 et 2026.8) avant d'intégrer cet incrément 2026.9.

Comme pour les versions précédentes, `main` du fork est un squash sans
ancêtre commun avec `upstream/stable` : un `git merge upstream/stable`
brut produit des milliers de conflits `add/add`. La synchro reste donc
menée en passes ordonnées via cherry-picks sélectifs.

## Résumé — 2026.9 (29/09/2026)

**252 commits, 77 fichiers, +5 360 / -1 591 lignes.**

### 🔒 Sécurité (à porter en priorité si l'API REST ou FreqAI est exposée)

- **`fix: don't allow freqAI model identifiers outside of model directory`**
  (`9cf942017`) — path traversal via `identifier` de modèle FreqAI ;
  ajoute un helper `is_path_in_dir` et l'appelle dans `data_kitchen.py`.
  Impact direct sur `freqtrade/freqai/data_kitchen.py`, `misc.py` et
  `freqai/utils.py`.
- **`feat: use Pydantic custom validator to ensure non-base64 strategies`**
  (`4927aa117`) — durcit la validation d'entrée sur `api_analysis.py`,
  `api_backtest.py`, `api_pair_history.py`, `api_schemas.py`, `api_v1.py`,
  `deps.py`. Aligne les endpoints REST sur la validation Pydantic plutôt
  qu'un check ad hoc.
- **`Add base64 verification to plot_config`** (`b9a35b602`) —
  vérification supplémentaire pour éviter l'injection via `plot_config`.

### ⚠️ Comportement trading — à valider en replay avant dry-run

- **`feat: implement liquidation warning to freqtradebot`** (`32fd5a79c`)
  + **`feat: telegram liquidation message`** (`fdeabf6d5`)
  + **`feat: add LiquidationWarningMessage base types`** (`d9ab920d7`) —
  nouveau canal de notification liquidation. **À réconcilier avec la
  détection de liquidation Hyperliquid déjà implémentée par le fork**
  (voir `freqtrade/exchange/hyperliquid.py` + `enums/rpcmessagetype.py`).
- **`fix: check contracts to verify for position openness`** (`5284e4d27`)
  — critère de position ouverte modifié ; **impacte l'external-close et
  le shared-wallet exit guard du fork sur Hyperliquid**.
- **`fix: don't overwrite an order's funding fee when the order is
  reprocessed`** (`cab0ec50d`) — protection funding fee sur reprocess.
- **`fix: consider existing trade-amount for entry cancels`** (`6f2d9f219`)
  — impacte la logique d'annulation d'entrée partielle.
- **`fix: exclude orders with open exit orders`** (`bbe0a0804`) — filtrage
  d'ordres pour éviter double-exit.
- **`fix: improved behavior for cross margin`** (`1297a5de6`) — cross
  margin mieux géré.
- **`fix: proactively protect from session errors on shutdown`**
  (`30c00ed63`) — hardening arrêt WebSocket. **Interagit avec la refonte
  shutdown déjà entamée dans 2026.7 (voir doc 2026.8) et avec la logique
  de résilience du fork sur `exchange_ws.py` + `common.py`**.
- **`fix: valid-pair combination should respect trade mode`**
  (`3774521e7`) — pair combinations respectent futures/spot.

### 📊 Données OHLCV & backtest — quelques divergences possibles

- **`fix: don't drop funding-fee candle in data-download`** (`fd0e573ac`)
  — évite la perte d'une bougie funding lors du téléchargement.
- **`fix: always drop last candle for historic parallel download`**
  (`14909b500`) + **`fix: improved handling of last candle drops`**
  (`ab63965cd`) — la dernière bougie est systématiquement retirée du
  download parallèle historique.
- **`fix: clamp grace_period to half a candle length`** (`448415cf1`) +
  **`fix: add max-poll time to avoid very outdated longer candles (1h+)`**
  (`a0668815d`) + **`Fix: improve refresh-logic for another edge-case`**
  (`e5e335174`) + **`fix: improved refresh logic for edgecase`**
  (`2f7a730d0`) + **`fix: improved refresh logic for some edge cases`**
  (`674dfb80c`) — durcissement complet du refresh de bougies live avec
  nouvelle logique de grace period et max-poll ; **impacte directement
  `MultiMarketPairList` / `ftcache` du fork qui construit sa propre
  fenêtre de refresh**.

### 🔌 Exchanges

- **`chore: delist bitmex`** (`bff1e31a1`) — pendant du delist bitmart de
  2026.8. Le fork ne l'utilise pas mais vérifier les configs d'exemple.
- **`fix: remove coinex from online tests`** (`0d7510da3`) — coinex hors
  ligne côté tests.
- **`chore: update binance leverage tiers`** (`548cd47b1`) — mise à jour
  du JSON binance leverage tiers.
- **`chore: bump ccxt to 4.5.83`** (`6637d6212`) puis 4.5.84 — nouvelle
  version ccxt (impact global sur exchanges).

### 🧠 FreqAI

- **`fix: improve date resilience for freqAI`** (`d6a0721d5`) — moins de
  crashes sur des dates edge-case.
- Path traversal fix (voir Sécurité).

### 📢 RPC / Telegram / Webhook / API

- **`feat: add delete_custom_data`** (`2a3bca6cb`) — nouvel endpoint pour
  supprimer `custom_data`. Impact `api_v1.py`, `rpc.py`, `persistence/custom_data.py`.
- **`feat: add proxy_coin to show_config`** (`6fac8ddb2`) — nouveau champ
  exposé.
- **`feat: warn about time offsets between hosts and exchange`**
  (`ded215b47`) — alerte drift NTP côté freqtradebot / rpc / telegram.
- **`fix: redact coingecko api_keys`** (`29186a9a0`) — logs plus safe.

### 🛠️ Divers

- **`fix: ensure fee is never None`** (`09a4362ef`).
- **`fix: Improved error handling for scheduled jobs`** (`81a2cd162`) +
  ajout `freqtrade/util/ft_scheduler.py` (nouveau helper).
- **`fix: set pricing_side alongside order_type`** (`0fe47b351`).
- **`fix: move strategy_safe_wrapper to analyze_ticker_internal`**
  (`e28f7e26a`).
- **`feat: add "is_path_in_dir" helper`** (`33e32a52b`) — helper sécurité
  généraliste (voir Sécurité FreqAI).
- **`feat: update config schema`** (`5df94820a`) — nouveaux champs
  ajoutés au schéma.
- Grosse vague de dep bumps : pandas 3.0.6, sqlalchemy 2.0.54,
  uvicorn 0.53.0, plotly 7.1.0, cachetools 7.2.0, filelock 4.0.1,
  pymdown-extensions 12.0.1, ruff 0.16.8, ccxt 4.5.84.

## Zones à impact — fichiers touchés en amont ET modifiés par le fork

Sur 77 fichiers modifiés amont, **~60 sont aussi modifiés par le fork**
(overlap calculé via `comm -12` entre `git diff --name-only 2026.8..2026.9`
et `git diff --name-only 2026.6 main`).

Les fichiers sensibles pour un fork qui trade en production Hyperliquid :

### Bloc Hyperliquid & exécution ordres
- `freqtrade/exchange/exchange.py`, `exchange_ws.py`, `exchange_types.py`,
  `exchange_utils.py`, `common.py`, `binance.py`, `bybit.py`, `bitget.py`
  — hardening shutdown session (30c00ed63) + contracts/position check
  (5284e4d27) + cross margin (1297a5de6). **À réconcilier avec les
  patches du fork sur ces fichiers.**

### Bloc portefeuille & persistance
- `freqtrade/wallets.py` — retouché à nouveau amont. Le fork lit les
  soldes par compte (fleet), pas par exchange.
- `freqtrade/persistence/trade_model.py` — impact liquidation warning +
  funding fee reprocess.
- `freqtrade/persistence/custom_data.py` — nouveau endpoint delete.

### Bloc pairlists (impact MultiMarketPairList)
- `freqtrade/plugins/pairlist/VolumePairList.py`,
  `AgeFilter.py`, `PercentChangePairList.py`, `IPairList.py`,
  `rangestabilityfilter.py` — cascading changes de la vague 2026.8.

### Bloc RPC / API / Telegram
- `freqtrade/rpc/api_server/api_v1.py`, `api_backtest.py`,
  `api_analysis.py`, `api_pair_history.py`, `api_schemas.py`, `deps.py`,
  `webserver.py`, `ws/proxy.py` — refonte validation base64 + nouveaux
  endpoints (delete_custom_data, proxy_coin).
- `freqtrade/rpc/rpc.py`, `rpc_types.py`, `telegram.py`, `webhook.py`,
  `fiat_convert.py` — liquidation warning + time offset warning +
  coingecko redact.
- `freqtrade/enums/rpcmessagetype.py` — nouveau type liquidation warning.

### Bloc FreqAI
- `freqtrade/freqai/data_kitchen.py`, `data_drawer.py`, `utils.py`,
  `RL/BaseEnvironment.py`, `RL/Base3ActionRLEnv.py`,
  `RL/Base4ActionRLEnv.py`, `RL/Base5ActionRLEnv.py` — path traversal
  fix + date resilience.

### Bloc backtest / hyperopt / lookahead
- `freqtrade/optimize/backtesting.py`, `hyperopt/hyperopt.py`,
  `hyperopt/hyperopt_optimizer.py`, `hyperopt_tools.py`,
  `optimize/analysis/lookahead.py`, `analysis/lookahead_helpers.py`,
  `optimize_reports/bt_output.py` — retouches diverses ; à préserver
  vs les extensions replay/walk-forward/PlateauSampler du fork.

### Bloc data / candle refresh
- `freqtrade/data/dataprovider.py`, `data/entryexitanalysis.py`,
  `data/history/history_utils.py`, `data/history/datahandlers/idatahandler.py`,
  `data/converter/converter.py`, `data/converter/trade_converter.py`,
  `data/converter/orderflow.py` — grosse vague grace period + last
  candle drop. **Interagit avec le cache OHLCV du fork (`ftcache`).**

### Bloc config / stratégie
- `freqtrade/config_schema/config_schema.py`,
  `configuration/deploy_config.py`, `constants.py`,
  `strategy/interface.py`, `strategy/hyper.py`,
  `strategy/strategyupdater.py` — nouveaux champs schema, config
  validation.

### Fichiers non-conflictuels (à porter direct)
- `freqtrade/util/ft_scheduler.py` (nouveau), `util/__init__.py`,
  `enums/candletype.py`, `misc.py` (partiel), `commands/data_commands.py`,
  `commands/list_commands.py`, `commands/optimize_commands.py`,
  `configuration/config_secrets.py`, `configuration/config_validation.py`.

## Procédure de synchronisation recommandée

1. **Terminer d'abord la dette 2026.7 + 2026.8** (voir
   [`UPSTREAM_SYNC_2026.8.md`](./UPSTREAM_SYNC_2026.8.md) et PR #42).
2. **Passe sécurité 2026.9** — porter en priorité les 3 fixes sécurité
   (path traversal FreqAI + base64 strategies + plot_config) même si
   la dette 2026.8 n'est pas soldée.
3. **Passe candle refresh** — la nouvelle logique grace/max-poll doit
   être portée en synchronisation avec l'implémentation `ftcache` du
   fork, sans quoi les fenêtres divergent.
4. **Passe liquidation warning** — arbitrer entre le nouveau canal amont
   et la détection Hyperliquid existante ; probablement les fusionner
   (topic Telegram commun, source Hyperliquid uniquement).
5. **Reste** : cherry-picks sélectifs comme les vagues précédentes, avec
   suite complète `pytest --random-order -n auto` + replay sur données
   réelles avant tout dry-run (cf. `.claude-tips/replay.md`).

## Points d'attention avant merge

- **FreqAI path traversal** : porter d'abord même si le reste de la
  vague FreqAI 2026.8 attend.
- **Base64 strategies + plot_config** : impact API REST — tester les
  clients FreqUI-fork avant déploiement.
- **Liquidation warning** : ne pas doubler les alertes Hyperliquid déjà
  émises par le fork ; unifier le canal Telegram.
- **Contract-based position openness check** : rejouer les tests
  Hyperliquid `test_exchange.py` + `test_hyperliquid.py` (fork) avant
  merge.
- **Candle refresh grace / max-poll** : replay 24h de données réelles ;
  comparer le nombre de bougies retenues avec/sans le fix.
- **Delist Bitmex** : vérifier `user_data/` et `CLAUDE.local.md` fork.
- **ccxt 4.5.84** : `pip install -e .` puis `pytest tests/exchange/` +
  `mypy freqtrade/`.

## Références

- Release 2026.9 :
  <https://github.com/freqtrade/freqtrade/releases/tag/2026.9>
- Diff amont ciblé :
  `git diff 2026.8..2026.9 -- freqtrade/`
- Chevauchement fork ↔ 2026.9 :
  `comm -12 <(git diff --name-only 2026.8..2026.9 | sort)
   <(git diff --name-only 2026.6 main | sort)`
- Dette amont accumulée : `2026.7 + 2026.8 + 2026.9`
  (voir aussi PR #42 `chore(sync): tracer la dette amont freqtrade
  2026.7 + 2026.8`).
