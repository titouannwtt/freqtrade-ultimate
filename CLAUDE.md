> **Référence transverse** — appliquer aussi les principes de https://github.com/multica-ai/andrej-karpathy-skills/blob/main/CLAUDE.md

# CLAUDE.md

## What is this repo

Fork of [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) — open-source crypto trading bot. This fork adds **Hyperliquid-specific features** (liquidation/ADL detection, external position close handling) and a **trading co-pilot system** with 199 curated guardrails from Carver, Clenow, Chan, and Lopez de Prado.

If a `CLAUDE.local.md` file exists at the repo root, read it — it contains user-specific config (exchange, strategies, bots, personal constraints).

## Trading guardrails — co-pilot posture

Claude acts as a **critical co-pilot**, not a passive executor. Real money is at stake.

**Before any trading action** (strategy, hyperopt, config, sizing, pairlist, deployment):
1. Read `.claude-tips/README.md` → identify relevant files → read them
2. Check that the request doesn't violate any strict rule (🚫). If conflict: block, cite the tip, propose an alternative
3. Give opinionated advice, not pros/cons lists. Push back when justified
4. Accept being wrong if the user argues with solid reasoning. Propose enriching the tips if a credible original idea comes up
5. Factor in the user's real context: infrastructure, config, portfolio coherence

**Source of truth**: `tips.txt` at the repo root (199 tips). The `.claude-tips/*.md` files are actionable indexes. If divergence, `tips.txt` takes precedence.

## Common commands

```bash
# Install / update after merge
pip install -e .

# Run tests
pytest --random-order -n auto

# Run a single test
pytest tests/test_freqtradebot.py::test_function_name -x

# Lint (ruff, line-length=100, max-complexity=12)
ruff check freqtrade/
ruff format freqtrade/

# Type check
mypy freqtrade/

# Query a trade database (use python, not sqlite3 CLI)
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

**Critical:** `total_closed_profit` is cumulative from the DB — stake grows with profits (silent compounding). See `.claude-tips/live_trading.md` § "Capital & sizing".

## Fork-specific modifications

1. **External close handler** (`freqtradebot.py:_handle_external_close`) — Detects positions closed externally (Hyperliquid ADL, manual close). Closes trade at market with `exit_reason="external_close"`.
2. **Liquidation detection** (`exchange/hyperliquid.py:fetch_liquidation_fills`) — Checks `liquidationMarkPx` field in user trades.
3. **TrendRegularityFilter** (`plugins/pairlist/TrendRegularityFilter.py`) — Excludes pairs with strong linear uptrends (high R²). Useful for short-only strategies. Registered in `constants.py`.
4. **Dry-run replay harness** (`freqtrade/replay/` + `rpc/api_server/api_replay.py`) — Drives the real live bot loop (`FreqtradeBot.process()`) candle-by-candle over historical data via a virtual clock + fake exchange, producing a FreqUI-compatible SQLite DB. Dry-run-only (structurally enforced, quadruple-guarded), Hyperliquid-native, funding-aware. **It is a live-behaviour validation / dry-run-seeding tool, NOT a strategy-selection backtester** — see `.claude-tips/replay.md`. Used from FreqUI as a **per-bot action** ("Simulate dry-run (replay)", dry bots only) that seeds the bot's own dry DB, or via CLI `python -m freqtrade.replay [--seed --sub-step --reset-db …]`, or **auto-launched** by a `dry_run_replay` config block. A **coordinator daemon** (`coordinator.py`, auto-spawned) caps concurrent replays to `nproc-2-hyperopt_cores` with a priority queue + SIGSTOP/SIGCONT pause/resume. DB-integrity is guaranteed (backup + `PRAGMA quick_check` + auto-restore; real trades win over replay trades). Requires `pip install -e ".[replay]"`. Tests: `tests/replay/` (~104). Deploy rule: `runner.py`/`exchange.py`/`coordinator*.py` changes run in a subprocess (no bot restart); `api_replay.py`/`lifecycle.py`/`freqtradebot.py` changes need a dry-bot restart.

## Exporting strategies (`user_data/export_strategies.py`)

Self-contained CLI that scans a folder of bot configs, classifies each as **live** or **dry-run**, and exports per strategy: the **sanitized config** (api keys / wallet keys / passwords / tokens redacted, `add_config_files` dropped), the **strategy `.py`**, its **hyperopt `.json`** (if any), and — optionally — a **6-month backtest**. Output: `live/<Strategy>/…` and `dry/<Strategy>/…` inside one timestamped `.zip`, plus `MANIFEST.json` + `README.txt`.

It is exchange-/market-agnostic (works on any freqtrade fork) and resolves `add_config_files` exactly like freqtrade, so `dry_run` is read correctly even when it lives in an included file. Every step is guarded: a bad config or a failed backtest is logged and recorded, never aborting the run. A final scan **aborts the export** if a suspected secret (e.g. `0x…` key) survived sanitization.

```bash
# all strategies in live_configs/ → user_data/strategies_export_<timestamp>.zip
python user_data/export_strategies.py --config-dir live_configs

# preview what would be exported, write nothing
python user_data/export_strategies.py --config-dir live_configs --list-only

# include a 6-month backtest per strategy (slow; needs local OHLCV data)
python user_data/export_strategies.py --config-dir live_configs --with-backtest
```

| Option | Default | Purpose |
|--------|---------|---------|
| `-d, --config-dir` | *(required)* | Folder of bot config JSON files to scan (e.g. `live_configs`) |
| `-o, --output` | `<userdir>/strategies_export_<ts>.zip` | Output archive path |
| `--output-stem` | timestamped | Name of the top-level folder inside the zip |
| `--userdir` | `user_data` | freqtrade user dir (data lookup + passed as `--userdir` to backtests) |
| `--strategy-dir` | `<userdir>/strategies` | Strategy `.py` lookup dir (repeatable) |
| `--datadir` | `<userdir>/data` | OHLCV data dir for backtests |
| `--config-mode` | `merged` | `merged` = self-contained config (includes resolved); `raw` = literal bot file only |
| `--recursive` | off | Recurse into sub-folders of `--config-dir` |
| `--ignore` | `_*`, `*example*`, `.*` | Filename globs to skip (repeatable; leading-`_` files are includes by convention) |
| `--with-backtest` | off | Run + bundle a backtest per strategy |
| `--backtest-days` | `180` | Backtest lookback window (~6 months) |
| `--backtest-timerange` | — | Explicit timerange (e.g. `20250101-20250701`), overrides `--backtest-days` |
| `--backtest-pairs` | from data | Explicit static pairlist (skips data-based discovery) |
| `--backtest-max-pairs` | `100` | Cap on auto-discovered backtest pairs (runtime control) |
| `--backtest-timeout` | `1800` | Per-backtest timeout (seconds) |
| `--data-format` | `feather` | OHLCV storage format for backtest discovery + loading |
| `--freqtrade-bin` | `freqtrade` | freqtrade executable used for backtests |
| `--list-only` | off | Print the export tree and exit without writing |
| `--allow-suspected-secrets` | off | Do not abort if the final scan flags a suspected secret |
| `-v, --verbose` | off | Debug logging |

**Backtest caveat:** the live pairlist is usually dynamic (`VolumePairList` + filters) and cannot be replayed offline, so backtests run on a **StaticPairList rebuilt from locally downloaded data** (with the config's blacklist applied). They are an *approximate sanity check*, not a faithful replay of live behaviour — and the bundled config is the one that ships, not the dynamic live setup. Generated `.zip` archives are gitignored and must never be committed.

## File layout

| Path | Purpose |
|------|---------|
| `live_configs/` | **Live-only** bot JSON configs (one per bot instance) — never use for hyperopt/backtest |
| `backtest_configs/` | Configs for hyperopt and backtesting (pair lists, MOT variants) |
| `user_data/strategies/` | Strategies (.py) + hyperopt params (.json) |
| `user_data/export_strategies.py` | Strategy export tool — bundles live/dry configs + code + params (+ optional backtest) into a sanitized `.zip`. See "Exporting strategies" below |
| `database/` | SQLite trade databases (one per bot) |
| `launch_bot.sh` | Bot launcher with auto-restart loop |
| `freqtrade/freqtradebot.py` | Core bot logic (+ `_handle_external_close`) |
| `freqtrade/exchange/hyperliquid.py` | Hyperliquid adapter (+ liquidation detection) |
| `freqtrade/plugins/pairlist/TrendRegularityFilter.py` | Custom pairlist filter |
| `tips.txt` | 199 trading tips — source of truth |
| `.claude-tips/` | Actionable tip files by category |

## Hard constraints

- **API keys** must be configured in your bot config and gitignored. Never commit credentials.
- When fixing trade DB issues, use `python3` with `sqlite3` module (no `sqlite3` CLI binary).
- Strategies using `"stake_amount": "unlimited"` handle sizing in `custom_stake_amount()`.
- **Never use `pkill`/`kill -9` on hyperopt** — leaves orphaned workers. Use `screen -S <session> -X stuff $'\003'` (Ctrl+C).

## Updating from upstream

```bash
git fetch upstream --tags
git merge upstream/stable --no-edit
# Resolve conflicts (usually .github/ CI files → accept theirs)
# Verify custom code preserved: grep _handle_external_close freqtrade/freqtradebot.py
pip install -e .
```

## FreqUI fork

If you maintain a custom FreqUI (separate repo), users update it via `freqtrade install-ui` which downloads the latest release artifact (`frequi-dist.tar.gz`) from the FreqUI repo's GitHub releases.

### How users update FreqUI

```bash
cd freqtrade
git pull origin main
freqtrade install-ui
# Restart bot(s) to serve the new UI
```

### Release checklist (FreqUI maintainer)

1. Bump `package.json` version before building (`"version": "X.Y.Z"`) — source of truth for the navbar version badge.
2. `cd <frequi> && npm run build`
3. Commit, push, create GitHub release with `frequi-dist.tar.gz` artifact attached.
4. Every release description should include the "How to update" instructions block.

### Deploy locally (no release)

```bash
cd <frequi> && npm run build
cp -r dist/* freqtrade/rpc/api_server/ui/installed/
# Restart bot(s) to serve the new UI
```

## Personal config

User-specific constraints (exchange-specific procedures, account constraints, deployed bot list, paths) belong in `CLAUDE.local.md` (gitignored). See `CLAUDE.local.md.template` for a starter template.


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
| `machine_learning.md` | ML / FreqAI reference |
| `trend_following.md` | Momentum / trend strategies |
| `strategy_bugs_reference.md` | Known strategy bugs: generated_strategy* template issues, trailing stop, future-looking, startup_candle_count |
| `replay.md` | Dry-run replay harness (`freqtrade/replay`): when to use it (live-behaviour validation, NOT strategy selection), guardrails, known divergences |
