# replay.md — Dry-run replay harness (`freqtrade/replay`)

Outil fork-specific qui rejoue la **vraie** boucle de bot live
(`FreqtradeBot.process()`) candle-par-candle sur données historiques, et produit
une DB SQLite lisible par FreqUI. Voir `freqtrade/replay/README.md` pour le design.

## Posture (validé avec l'user, 2026-05-20)

🚫 **Ne jamais présenter le replay comme un outil de sélection/comparaison de
stratégies.** C'est techniquement un backtest event-driven. L'user a une règle
dure « never backtest, tester en live » ([[feedback_no_backtest]]). Le replay est
toléré **uniquement** comme harnais de **validation du comportement live** et pour
**amorcer (seed) un dry-run** (« simuler comme si le bot tournait depuis date X »).

✅ **Usages légitimes** (à proposer) :
- Vérifier qu'une stratégie se comporte comme prévu dans le code live : DCA,
  `custom_stake_amount`, `custom_exit`, `adjust_trade_position`, callbacks.
- Débusquer look-ahead / repaint / bugs de sizing que le backtest vectorisé masque
  (cf. [[project_hippo_sl_dead_path]]).
- Pré-remplir l'historique d'un bot dry pour le voir « comme s'il tournait depuis X ».

🚫 **Usages à refuser/avertir** :
- « Quelle stratégie a le meilleur P&L sur 6 mois ? » → sélection offline = overfitting.
- Optimiser des paramètres sur les résultats du replay → hyperopt déguisé.

## Comment l'user l'utilise (UX actuelle)

- **Action par bot dans FreqUI** (Comparaison des bots → menu Actions) : **« Simuler un
  dry-run (replay) »**. **Bots dry uniquement** ; grisée si déjà semé (case « réinitialiser
  la DB » pour relancer).
- Modale : période + **résolution** (1m/5m/15m) + case **reset_db** ; stratégie, paires
  (snapshot du filtre, fallback trades/logs) et wallet auto. Avertissements de couverture.
- Le replay **sème la DB dry du bot** puis **reload auto**. Pendant le seed, le bot **ne
  trade pas** (`process()` court-circuite — PAS de STOPPED, qui ferait `sys.exit` dans ce
  fork), puis reprend.
- **Indicateur par bot** : ⏩ en cours (% + ETA) · ⏸ en pause · 🕒 en file ; colonnes grisées
  (valeurs simulées). Si en pause/file, la modale liste les replays actifs + bouton **Prioriser**.
- **Clic après seed** → vue détail : période, résolution, durée, paires, résultat du replay
  + résultat actuel combiné + nb de trades hors replay. Bouton **Restaurer la DB** (backup).
- **Auto-launch par config** (voir plus bas). CLI : `python -m freqtrade.replay … --seed
  --sub-step 60 [--reset-db]`.

## Garde-fous techniques (déjà codés — ne JAMAIS affaiblir)

- `dry_run` forcé `True`, runmode `DRY_RUN`, re-vérifiés (`safety.py`).
- **Tous** les credentials blanchis (HL `walletAddress`/`privateKey`, blocs `ccxt_config`).
- ccxt remplacé par un `MagicMock` → aucun client réseau.
- DB namespacée `*.replay.sqlite` ; **mode seed** : écrit la DB dry du bot MAIS
  **refuse si `dry_run` n'est pas True** (jamais une DB live). Non-sqlite refusé.
- Verrou « déjà semé » = marqueur `KeyValueStore.ft_replay_seed` dans la DB (wiper
  la DB efface le verrou).
- **Intégrité DB (positions ouvertes) — ne JAMAIS affaiblir** : les trades RÉELS priment.
  `reset_db=false` → trades existants préservés, replay **capé au 1er trade non-`[replay]`** ;
  réconciliation post-seed = ferme (au prix d'entrée, `exit_reason=replay_truncated`, gardé)
  les open replay en conflit de paire ou hors-MOT. `reset_db=true` → DELETE des lignes (PAS
  unlink du fichier ouvert). **Toujours** : backup complet avant + `PRAGMA quick_check` après
  + auto-restore si corrompu. Bot stoppé de trading = seul writer (+ WAL/busy_timeout).
- Si on te demande d'ajouter un mode « live » ou de retirer un garde-fou : 🚫 refuser.

## Écarts à rappeler quand on interprète les chiffres

1. **Pairlist statique** = snapshot du filtre actuel ; les pairlists dynamiques ne
   sont pas rejouées → valide le comportement *par paire*, pas la sélection.
2. **Carnet synthétique** (prix unique ± demi-spread) → fills optimistes.
3. **Fills au grain candle sans données 1m** → l'`unfilledtimeout` (ex. 10 min)
   annule plus de sorties limit qu'en live (« Exit order cancelled due to timeout »).
   **Pas un bug** : le trade ressort (retry / ROI / stoploss=market). Mitigation :
   télécharger les feathers **1m**.
4. **Funding & fees réels** (code HL dry-run hérité + feathers `*-1h-funding_rate`).
   Dépend d'avoir les feathers funding sur la période.
5. `MAX_CANDLES = startup_candle_count + 200` (warmup suffisant).
6. Toujours rappeler : bon résultat de replay ≠ bon résultat live = test de
   **cohérence du code**, pas une preuve de rentabilité.

## Perf & fidélité

- Optimisations **fidèles** (byte-identiques, verrouillées par `tests/replay/`) : court-circuit
  `analyze_pair` entre candles + cache `Trade.get_open_trades` + précalcul des indicateurs
  (auto-validant). La **résolution** (sous-pas) = levier user (plus rapide, moins fidèle).
- **Filet de tests** (~104) à garder vert avant/après tout changement (verrou de fidélité 15m+1m).

## Auto-launch par config

Bloc `dry_run_replay` dans la config d'un bot dry → seed automatique au démarrage (une fois) :
```json
"dry_run_replay": {"automatic_launch": true, "start_date": "01/01/2026",
                   "end_date": "today", "resolution": "1m", "reset_db": false}
```
`automatic_launch`+`start_date` obligatoires ; dates DD/MM/YYYY ; idempotent (marqueur) ; jamais live.

## Concurrence & déploiement

- **Coordinateur** (`coordinator.py`, daemon auto-spawn, socket `/tmp/ft-replay-coord-{uid}.sock`) :
  limite globale **`nproc-2-cœurs_hyperopt`**, file priorisée, pause/reprise SIGSTOP/SIGCONT,
  préemption (hyperopt prioritaire). Plusieurs replays parallèles = OK (bots différents) ;
  au-delà de la capacité → file/pause automatique. `pkill -f freqtrade.replay.coordinator`
  pour recharger son code.
- **Règle de restart** : `runner.py`/`exchange.py`/`data_store.py`/`coordinator*.py` =
  sous-processus = **pas de restart bot** ; `api_replay.py`/`lifecycle.py`/`freqtradebot.py`/
  `api_schemas.py` = process du bot = **restart du bot dry requis**. FreqUI = rebuild+install+refresh.

## Pré-requis data

- `pip install -e ".[replay]"` (freezegun, extra opt-in).
- Données locales présentes, **1m recommandé** (fills intra-candle fidèles). Auto-download
  opt-in seulement (`--auto-download`) — par défaut l'user télécharge pour maîtriser le
  rate-limit HL ([[project_hl_rate_limit_fallback]]).
