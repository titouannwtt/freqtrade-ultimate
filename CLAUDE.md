# CLAUDE.md

## What is this repo

Fork of [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) (v2026.3) focused on **Hyperliquid futures trading** with aggressive DCA short strategies. The fork adds liquidation/ADL detection, external position close handling, and a custom pairlist filter.

**Owner:** titouannwtt (private repo)
**Upstream:** `upstream` remote → `https://github.com/freqtrade/freqtrade.git`

## Trading guardrails — co-pilot posture

Claude agit comme un **co-pilote critique**, pas un exécutant passif. Argent réel et mois de travail en jeu.

**Avant toute action trading** (stratégie, hyperopt, config, sizing, pairlist, deployment) :
1. Lire `.claude-tips/README.md` → identifier les fichiers pertinents → les lire
2. Vérifier que la demande ne contredit aucune règle stricte (🚫). Si conflit : bloquer, citer le tip, proposer une alternative
3. Donner des avis tranchés, pas des listes de pour/contre. Aller à contre-courant si justifié
4. Accepter d'avoir tort si l'utilisateur argumente solidement. Proposer d'enrichir les tips si idée originale crédible
5. Prendre en compte le contexte réel : infrastructure (ftcache, bots en live), config (sizing, MOT, throttle), cohérence portfolio (exposition, corrélation)

**Contexte par défaut** : DCA mean-reversion sur Hyperliquid USDC perps en 15m. Prioriser `mean_reversion.md` et `risk_management.md`.

**Source de verite** : `tips.txt` a la racine (199 tips). Les fichiers `.claude-tips/*.md` sont des index actionnables. En cas de divergence, `tips.txt` prime.

## Common commands

```bash
# Install / update after merge
.venv/bin/pip install -e .

# Run a bot (inside a screen session)
./launch_bot.sh hyperliquid_hippo_dynv1_short_sharpe.json

# Run tests
pytest --random-order -n auto

# Run a single test
pytest tests/test_freqtradebot.py::test_function_name -x

# Lint (ruff, line-length=100, max-complexity=12)
ruff check freqtrade/
ruff format freqtrade/

# Type check
mypy freqtrade/

# Query a trade database (no sqlite3 binary, use python)
python3 -c "import sqlite3; c=sqlite3.connect('database/xxx.sqlite').cursor(); ..."
```

## Architecture overview

### Core trading loop
`freqtradebot.py` → `process()` is called each cycle:
1. `create_trades()` → entry signals → `custom_stake_amount()` → place orders
2. `manage_open_orders()` → `update_trade_state()` for pending orders
3. `exit_positions()` → check wallet sync → exit signals → `execute_trade_exit()`

When wallet mismatch detected (position gone from exchange):
- `handle_onexchange_order()` → `_handle_liquidation()` → `_handle_external_close()`

### Strategy parameter loading (important gotcha)
Priority: **JSON file > buy_params dict > DecimalParameter default**

Each strategy `Foo.py` can have a co-located `Foo.json` (hyperopt output) that **silently overrides** `buy_params`/`sell_params` in the Python code. Always check the JSON file for actual live values.

### Position sizing flow (DCA strategies)
```
wallets.py: available_amount = available_capital - capital_withdrawal + total_closed_profit (DB)
wallets.py: proposed_stake = available_amount / max_open_trades
strategy:   custom_stake = (proposed_stake / max_so_multiplier * overbuy_factor) * tradable_balance_ratio
strategy:   DCA orders = custom_stake * safety_order_volume_scale^(n-1)
```

**Critical:** `total_closed_profit` is cumulative from the DB — stake grows with profits (silent compounding). See `.claude-tips/live_trading.md` § "Capital & sizing" for the full analysis.

## Custom modifications (fork-specific)

1. **External close handler** (`freqtradebot.py:_handle_external_close`) — Detects positions closed externally (Hyperliquid ADL, manual close). Closes trade at market with `exit_reason="external_close"`.
2. **Liquidation detection** (`exchange/hyperliquid.py:fetch_liquidation_fills`) — Checks `liquidationMarkPx` field in user trades.
3. **TrendRegularityFilter** (`plugins/pairlist/TrendRegularityFilter.py`) — Excludes pairs with strong linear uptrends (high R²). For short-only strategies. Registered in `constants.py`.

## File layout

| Path | Purpose |
|------|---------|
| `live_configs/` | **Live-only** bot JSON configs (one per bot instance) — never use for hyperopt/backtest |
| `backtest_configs/` | Configs for hyperopt and backtesting (pair lists, MOT variants) |
| `live_configs/_hyperliquid_freqtrade_access.json` | API keys (**gitignored, never commit**) |
| `user_data/strategies/` | All custom strategies (.py) + hyperopt params (.json) |
| `database/` | SQLite trade databases (one per bot) |
| `launch_bot.sh` | Bot launcher with auto-restart loop (60s grace period) |
| `freqtrade/freqtradebot.py` | Core bot logic (custom: `_handle_external_close`) |
| `freqtrade/exchange/hyperliquid.py` | Hyperliquid adapter (custom: liquidation detection) |
| `freqtrade/plugins/pairlist/TrendRegularityFilter.py` | Custom pairlist filter |

## Bots currently running

Bots run in `screen` sessions. List with `screen -ls`. Each bot has a config in `live_configs/`, a database in `database/`, a strategy in `user_data/strategies/`.

To check a bot's logs: `screen -S <session> -X hardcopy -h /tmp/out.txt && tail -50 /tmp/out.txt`

## Updating from upstream

```bash
git fetch upstream --tags
git merge upstream/stable --no-edit
# Resolve conflicts (usually .github/ CI files → accept theirs)
# Verify custom code preserved: grep _handle_external_close freqtrade/freqtradebot.py
.venv/bin/pip install -e .
```

## Hard constraints

- **Never suggest backtests.** Strategy testing is done live with small capital.
- **API keys** are in `_hyperliquid_freqtrade_access.json` — gitignored. If missing, bot won't start.
- **Never use Hyperliquid sub-accounts.** Single wallet `0xC234...` for all volume. Rate-limit fix = VPN/proxy, never wallet-level.
- When fixing trade DB issues, use `python3` with `sqlite3` module (no `sqlite3` CLI).
- Strategies use `"stake_amount": "unlimited"` — sizing is in `custom_stake_amount()`.
- Bot restart: `kill <pid>` then `launch_bot.sh` auto-restarts after 60s, or via `screen -S <session> -X stuff './launch_bot.sh ...\n'`.
- **Never use `pkill`/`kill -9` on hyperopt** — leaves orphaned workers. Use `screen -S <session> -X stuff $'\003'` (Ctrl+C).

## Detailed guides (loaded on demand)

These `.claude-tips/` files contain in-depth reference for specific topics. **Do not read them all upfront** — load only the relevant ones per `.claude-tips/README.md` routing table.

| Guide | When to load |
|-------|-------------|
| `hyperopt.md` | Hyperopt launch, loss function choice, walk-forward, common traps |
| `live_trading.md` | Config tuning: throttle, pricing, pairlist, timeout, sizing |
| `portfolio.md` | Multi-bot setup: ftcache, tournaments, correlation risk |
| `strategy_evaluation.md` | Reviewing strategy metrics, function checklist |
| `mean_reversion.md` | DCA strategies, stoploss philosophy, safety orders |
| `risk_management.md` | Position sizing, drawdown limits, leverage, diversification |
| `strategy_development.md` | Building new strategies, indicator selection |
| `backtesting.md` | Backtest methodology, holdout validation |
| `psychology.md` | Behavioral biases, emotional discipline |
| `market_analysis.md` | Regime detection, S1-S4 stages, macro filters |
| `data_quality.md` | Feature selection, causal inference |
| `machine_learning.md` | ML reference (not used on 15m, kept for future) |
| `trend_following.md` | Momentum/trend reference (not our default) |
