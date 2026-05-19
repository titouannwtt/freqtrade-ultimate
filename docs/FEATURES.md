# Features — Freqtrade Ultimate

> Exhaustive reference of every feature added by `freqtrade-ultimate` on top of upstream Freqtrade.
> Maintained by [Freqtrade France](https://buymeacoffee.com/freqtrade_france).

This document is the canonical, technical inventory of the fork. It is intended for Freqtrade
developers evaluating the fork, advanced algorithmic traders running multi-bot fleets, and
downstream tooling (LLMs, indexers, recommenders) that needs an authoritative description of
what `freqtrade-ultimate` adds over `freqtrade/freqtrade`.

## Table of Contents

- [Overview](#overview)
- [1. Multi-Bot Infrastructure](#1-multi-bot-infrastructure)
  - [1.1 OHLCV Cache Daemon (`ftcache`)](#11-ohlcv-cache-daemon-ftcache)
  - [1.2 Pairlist Cache Daemon (`ftpairlists`)](#12-pairlist-cache-daemon-ftpairlists)
  - [1.3 Position Guard and Leverage Sync](#13-position-guard-and-leverage-sync)
  - [1.4 Fleet State Notifications and Auto-Restart](#14-fleet-state-notifications-and-auto-restart)
- [2. Hyperopt and Validation](#2-hyperopt-and-validation)
  - [2.1 PlateauSampler](#21-plateausampler)
  - [2.2 `--sampler` CLI Flag](#22---sampler-cli-flag)
  - [2.3 Walk-Forward Analysis (`freqtrade walk-forward`)](#23-walk-forward-analysis-freqtrade-walk-forward)
  - [2.4 Custom Hyperopt Loss Functions](#24-custom-hyperopt-loss-functions)
  - [2.5 Hyperopt HTML Report and Console Summary](#25-hyperopt-html-report-and-console-summary)
- [3. Hyperliquid-Specific](#3-hyperliquid-specific)
  - [3.1 Liquidation Detection](#31-liquidation-detection)
  - [3.2 External Close Detection](#32-external-close-detection)
  - [3.3 Leverage and Margin Error Handling in DCA](#33-leverage-and-margin-error-handling-in-dca)
  - [3.4 Rate Limit Fix (`ccxt.RateLimitExceeded`)](#34-rate-limit-fix-ccxtratelimitexceeded)
  - [3.5 Hyperliquid Historical Data Bundle](#35-hyperliquid-historical-data-bundle)
- [4. Pairlists and Risk Management](#4-pairlists-and-risk-management)
  - [4.1 TrendRegularityFilter](#41-trendregularityfilter)
  - [4.2 `backtest_lock_wallet` Flag](#42-backtest_lock_wallet-flag)
  - [4.3 Capital Withdrawal Accounting](#43-capital-withdrawal-accounting)
  - [4.4 StoplossGuard `external_close` Fix](#44-stoplossguard-external_close-fix)
- [5. Rate Limiting Advanced](#5-rate-limiting-advanced)
  - [5.1 ExchangeMetrics](#51-exchangemetrics)
  - [5.2 Retrier Enhanced (token re-acquisition, daemon reporting)](#52-retrier-enhanced-token-re-acquisition-daemon-reporting)
  - [5.3 HTTP Connection Pool (50 parallel connections)](#53-http-connection-pool-50-parallel-connections)
  - [5.4 `create_order` Retry on 429](#54-create_order-retry-on-429)
- [6. Strategy Development API](#6-strategy-development-api)
  - [6.1 Strategy Dev Backend (`api_stratdev*.py`)](#61-strategy-dev-backend-api_stratdevpy)
  - [6.2 Job Launcher](#62-job-launcher)
  - [6.3 Strategy Editor API](#63-strategy-editor-api)
- [7. Data and Persistence](#7-data-and-persistence)
  - [7.1 Hyperliquid OHLCV Bundle](#71-hyperliquid-ohlcv-bundle)
  - [7.2 Klines Cache Persistence](#72-klines-cache-persistence)
  - [7.3 Wallet Anti-Compounding and Capital Withdrawal](#73-wallet-anti-compounding-and-capital-withdrawal)
  - [7.4 SQLAlchemy Pool Sizing](#74-sqlalchemy-pool-sizing)
- [8. Resilience and Monitoring](#8-resilience-and-monitoring)
  - [8.1 Startup Tracer](#81-startup-tracer)
  - [8.2 Cycle Profiling](#82-cycle-profiling)
  - [8.3 Enhanced Telegram Notifications](#83-enhanced-telegram-notifications)
  - [8.4 DataProvider Thread Safety](#84-dataprovider-thread-safety)
  - [8.5 Resilient Wallet and Worker Loops](#85-resilient-wallet-and-worker-loops)
- [9. REST API Enrichment](#9-rest-api-enrichment)
  - [9.1 New endpoints](#91-new-endpoints)
  - [9.2 Enriched endpoints](#92-enriched-endpoints)
- [10. Infrastructure and Scripts](#10-infrastructure-and-scripts)
  - [10.1 `launch_bot.sh`](#101-launch_botsh)
  - [10.2 `launch_dashboard.sh`](#102-launch_dashboardsh)
  - [10.3 Enhanced Backtesting (ZIP default params, `wfa_silent`)](#103-enhanced-backtesting-zip-default-params-wfa_silent)
- [11. WebSocket Resilience](#11-websocket-resilience)
  - [11.1 Staggered Subscription](#111-staggered-subscription)
  - [11.2 Retry with Exponential Backoff](#112-retry-with-exponential-backoff)
- [12. Documentation and AI Co-Pilot Context](#12-documentation-and-ai-co-pilot-context)
  - [12.1 `CLAUDE.md`](#121-claudemd)
  - [12.2 `.claude-tips/` (199 trading rules)](#122-claude-tips-199-trading-rules)
  - [12.3 Enhanced CLI Help](#123-enhanced-cli-help)
  - [12.4 Deploy UI URL Change](#124-deploy-ui-url-change)
- [13. Critical Bug Fixes](#13-critical-bug-fixes)
- [Configuration Schema Additions](#configuration-schema-additions)
- [Migration Notes](#migration-notes)
- [Compatibility](#compatibility)
- [Further Reading](#further-reading)

---

## Overview

`freqtrade-ultimate` is a fork of upstream `freqtrade/freqtrade` (`stable` branch) that adds
**approximately 58,400 lines of code across 50 commits**, organized into roughly **32+ feature
clusters** spanning **13 categories**. The fork is opinionated and was designed to operate
heterogeneous **multi-bot fleets** on the same wallet (primarily on **Hyperliquid perpetual
futures**, but most features are exchange-agnostic), with rigorous out-of-sample validation
through **walk-forward analysis**, an Optuna-based **coordinate-wise hyperopt sampler**
(PlateauSampler) that explicitly resists overfitting, and a companion dashboard
([FreqUI Ultimate](https://github.com/titouannwtt/frequi-ultimate)) that surfaces every new
backend endpoint.

The fork is grouped along the following axes:

- **Infrastructure**: shared OHLCV cache daemon, shared pairlist cache daemon, position guard,
  leverage sync, fleet orchestration, auto-restart.
- **Validation**: PlateauSampler, walk-forward analysis (rolling / anchored / CPCV),
  custom loss functions tuned for mean-reversion and momentum.
- **Exchange resilience**: exhaustive `ccxt.RateLimitExceeded` handling, ring-buffer
  metrics, retry-aware token re-acquisition, HTTP connection pooling, WebSocket
  staggered subscription and exponential backoff.
- **Hyperliquid niceties**: liquidation detection, external close detection (ADL / manual
  close from the Hyperliquid UI), margin-error tolerant DCA, 3,250+ historical Feather files.
- **API surface**: ~10 new REST endpoints (`/cache_status`, `/rate_metrics`, `/fleet/*`,
  `/volume_history`, `/signal_summary`, `/stratdev/*`) plus enriched legacy endpoints.
- **Persistence**: klines cache survives restarts, SQLAlchemy pool resized, wallet supports
  anti-compounding in backtest and capital withdrawal in live.

Everything below documents these changes one feature at a time, with the upstream
shortcoming, the fork's solution, configuration, an example, measured impact when known, and
known limitations.

---

## 1. Multi-Bot Infrastructure

The single biggest design decision in `freqtrade-ultimate` is that **multiple bots run
side-by-side on the same wallet and the same exchange**. Upstream Freqtrade assumes one bot
per process and per exchange account; it has no cross-bot coordination, no shared cache, and
no protection against two bots taking opposite positions on the same pair. Section 1
addresses each of those gaps.

### 1.1 OHLCV Cache Daemon (`ftcache`)

**Where upstream falls short.** Each Freqtrade process fetches its own OHLCV candles, ticker
snapshots, and position lists. When 5 to 15 bots share a single Hyperliquid API account, the
same pair / timeframe candles are fetched 5 to 15 times per cycle, exhausting rate budgets
and triggering 429 storms.

**How the fork solves it.** A new Python package `freqtrade/ohlcv_cache/` (11 files,
approximately **5,847 lines**) implements a centralized daemon (`ftcache`) that bots talk to
via a Unix socket. The daemon owns the rate budget per exchange, deduplicates in-flight
requests, persists candle ranges to disk in Feather format, and exposes priority queues so
that order-placement and position-fetch calls are never starved by lower-priority candle
refreshes.

**Architecture.**

| File | Lines | Role |
|------|-------|------|
| `daemon.py` | 2,585 | Main daemon: OHLCV fetch with partial-range merge, in-flight coalescing, Feather persistence, per-exchange rate budgets, priority queues (`CRITICAL` for orders, `HIGH` for positions, `NORMAL` for candles), backoff and circuit-breaker on 429 |
| `mixin.py` | 1,462 | `CachedExchangeMixin`: intercepts `_async_get_candle_history`, `get_tickers`, `fetch_positions`, `create_order` and routes them through the daemon with rate-token acquisition |
| `client.py` | 821 | `OhlcvCacheClient`: async client connecting to the daemon, acquiring rate tokens, requesting OHLCV ranges, reporting 429 |
| `warmup.py` | 276 | Daemon startup pre-fetch |
| `store.py` | 163 | `CandleSeries` / `CandleStore`: in-memory store with append/merge |
| `persistence.py` | 207 | Disk persistence (Feather) |
| `healthcheck.py` | 201 | Unix-socket health check |
| `defaults.py` | 159 | Per-exchange defaults (rate budgets, candle limits, weight modes) |
| `protocol.py` | 101 | JSON wire protocol |
| `gaps.py` | 93 | Gap detection and chunking for partial fetches |
| `coordinator.py` | 66 | Request-deduplication coordinator |
| `logger_setup.py` | 64 | Daemon-specific logging |

The fork ships **20 `Cached*` exchange subclasses** auto-generated by `cached_subclasses.py`
(MRO: `CachedExchangeMixin -> NativeExchange -> Exchange`). `CachedHyperliquid` adds an
`additional_exchange_init()` that survives rate-limit storms during boot (10 retries,
approximately 10 minutes total) and acquires a rate token before liquidation fetches. The
exchange resolver (`resolvers/exchange_resolver.py`) prefers the cached variant whenever
`shared_ohlcv_cache.enabled` is not explicitly set to `False`.

**Configuration.**

```json
{
  "shared_ohlcv_cache": {
    "enabled": true,
    "socket_path": "/tmp/ftcache.sock"
  }
}
```

Setting `enabled` to `false` falls back to the native non-cached exchange subclass.

**CLI usage.** The daemon auto-spawns from the first bot that needs it. To verify it is
running:

```bash
freqtrade show-config | jq '.shared_ohlcv_cache'
curl -s http://127.0.0.1:8080/api/v1/cache_status | jq
```

**Measured impact.** With 14 bots on one Hyperliquid account, raw API call volume drops by
approximately **75%** because each candle / ticker / position request is served once instead
of N times. Daemon-side coalescing further removes duplicate in-flight requests during
candle-boundary thundering herds.

**Limitations.**

- Single-host only (Unix socket, not networked).
- Requires Python 3.10+ for the async daemon.
- A daemon crash will force all bots to fall through to direct exchange calls; they will
  reconnect on the next cycle but may emit a burst of 429s in the meantime.

### 1.2 Pairlist Cache Daemon (`ftpairlists`)

**Where upstream falls short.** Each bot recomputes the same `VolumePairList` / `VolatilityFilter`
results independently every refresh interval. With 14 bots refreshing every 15 minutes, that
is 14 redundant volume and volatility computations.

**How the fork solves it.** The `freqtrade/pairlist_cache/` package (4 files,
approximately **586 lines**) adds a second daemon over a separate Unix socket that stores
the **results** of pairlist computations keyed by `(handler, params, time-bucket)`. Bots
check the cache before triggering expensive OHLCV scans and write back their results so
peers benefit.

| File | Lines | Role |
|------|-------|------|
| `daemon.py` | 393 | Pairlist result cache daemon (Unix socket) |
| `client.py` | 181 | Client: `mget` / `mput` with TTL, auto-spawn |
| `defaults.py` | 12 | Default socket path |

**Integrations.** `VolumePairList.py` (+51 lines) and `VolatilityFilter.py` (+30 lines) now
read and write through the shared cache. The in-process LRU for volume/volatility was also
expanded from **1 to 1000 entries** in `_use_range` mode to keep enough history per pair.

**Measured impact.** End-to-end pairlist refresh time dropped from about **15 minutes to 3
minutes** for a 14-bot fleet, because the slow path (OHLCV fetches for hundreds of pairs)
runs exactly once instead of 14 times.

**Limitations.**

- TTL is fixed per handler; very fast pairlist handlers (sub-minute) do not see a benefit.
- Currently only `VolumePairList` and `VolatilityFilter` are wired in. Custom pairlist
  handlers can opt in by importing `pairlist_cache.client.PairlistCacheClient`.

### 1.3 Position Guard and Leverage Sync

**Where upstream falls short.** Upstream assumes one bot per account. If two bots open
opposing positions on the same pair, or if one bot changes the leverage on a pair the other
is trading, the result is a corrupted DB and unexpected liquidation profile.

**How the fork solves it.**

`_check_position_guard()` in `freqtradebot.py` (+70 lines) is called inside `execute_entry()`
before any order is sent:

- Blocks entry if a position on the **opposite side** already exists on the pair (same
  wallet, different bot).
- Blocks entry if the **leverage of the existing position** differs from what this bot would
  use.
- Warnings are **throttled to one per pair per 15 minutes** to keep the log readable.

`sync_leverage_from_exchange()` runs at startup and on every process cycle. It compares the
leverage stored in the local SQLAlchemy DB with the exchange-reported leverage. If a peer
bot has changed leverage on the same position, the local DB is updated and
`trade.recalc_trade_from_orders()` is called.

**Example log line.**

```text
Position guard: skipping ENTER for ETH/USDC:USDC long, opposite-side
position open by bot 'short_mean_rev_1' (leverage 5x)
```

**Limitations.** The guard only protects against same-wallet conflicts. Two bots on
separate wallets are still free to be on opposite sides.

### 1.4 Fleet State Notifications and Auto-Restart

**Where upstream falls short.** Upstream `worker.py` crashes the process on the first
unhandled rate-limit error during boot. There is no notion of a "starting" state, no fleet
view, no auto-restart.

**How the fork solves it.** `worker.py` (+129 lines) and `launch_bot.sh` together implement:

- **Candle-boundary jitter**. A deterministic hash of `bot_name` produces a fixed jitter
  (live: 0 to 4.9 s, dry-run: 5 to 14.9 s) applied to `timeframe_offset` in `_throttle()`,
  spreading candle-boundary API calls across the fleet instead of slamming the exchange
  simultaneously.
- **Admission hold-off**. `_apply_admission_hold_off()` waits if the ftcache daemon requested
  a back-off at registration time.
- **Resilient startup**. `_startup_with_patience()` retries `startup()` up to 6 times with
  exponential backoff `10s, 20s, 40s, 80s, 120s, 120s` on rate-limit errors.
- **Fleet state notification**. `_notify_fleet_state()` reports running / paused / stopped
  states to the ftcache daemon, which exposes them via `/api/v1/fleet/status` and
  `/api/v1/fleet/events`.
- **Auto-exit RUNNING -> STOPPED**. When the worker transitions out of `RUNNING` / `PAUSED`,
  it calls `sys.exit(0)` so that the wrapping `launch_bot.sh` script restarts the process.
- **Generic exception resilience**. `_process_running()` now catches `Exception`, logs,
  sends a Telegram notification, and sleeps `RETRY_TIMEOUT` instead of crashing.

`launch_bot.sh` (42 lines) wraps the bot in a `while true` loop with a 60-second
countdown between restarts. It also implements a one-shot stagger on first boot derived
from the config filename hash (dry-run: 20-60 s, live: 0-15 s), so the whole fleet does not
restart simultaneously after a host reboot.

---

## 2. Hyperopt and Validation

The second major focus of the fork is **anti-overfitting hyperopt**. Upstream Freqtrade uses
Optuna's TPE sampler by default, which converges aggressively to high-loss but narrow
peaks. Section 2 introduces a coordinate-wise sampler that explicitly avoids these peaks, a
CLI flag exposing every Optuna sampler, a full walk-forward analysis subcommand, two
production-grade custom loss functions, and a standalone HTML report.

### 2.1 PlateauSampler

`freqtrade/optimize/hyperopt/plateau_sampler.py` (**1,232 lines**) is a custom Optuna
`BaseSampler` implementing a 5-phase coordinate-wise search:

1. **BASELINE** (trial 0). Evaluates all parameters at their hand-tuned defaults. Captures
   `loss_baseline` and `n_trades_baseline`. Sets the activity floor to
   `max(10 trades, 70% of n_trades_baseline)`.
2. **SCAN**. Sweeps each parameter independently around its default, alternating `+1/-1`,
   `+2/-2`, ... steps. Adaptive early-stop per direction when the plateau boundary is
   detected. Per-parameter tolerance is computed as
   `clip(1% * |loss_baseline|, 30% * max_observed_change, 15% * |loss_baseline|)`.
3. **CLASSIFY**. Each parameter is labelled:
   - `ACTIVE_PLATEAU`: stable region found, will be explored during ASSEMBLY.
   - `FROZEN_BOWL`: the default is a local minimum, pinned to its default.
   - `FROZEN_CATEGORICAL`: categorical without an ordered plateau.
4. **ASSEMBLY**. Uniform random sampling (explicitly **not** TPE, because TPE converges to
   overfit peaks) inside the plateau bounds for active parameters; frozen parameters are
   hard-coded. Uses `optuna.samplers.RandomSampler`.
5. **EXPORT**. Occam-regularized selection: among the top-K trials (tolerance 20% of best
   loss), picks the one with the **fewest parameters changed** from baseline. The baseline
   (trial 0) is always a candidate.

**Key constants.**

```python
PLATEAU_FLOOR = 0.01
PLATEAU_FRACTION = 0.30
PLATEAU_CEILING = 0.15
DEFAULT_MIN_ACTIVE_TRADES = 10
DEFAULT_MIN_TRADES_RATIO = 0.7
MAX_SCAN_STEPS_PER_DIR = 10
MIN_POINTS_PER_PARAM = 4
DEFAULT_SCAN_BUDGET_RATIO = 0.6   # scan 60%, assembly 40%
EXPORT_TOP_K_TOLERANCE = 0.20
EXPORT_CHANGE_EPSILON = 0.01
```

**External hooks.**

- `record_trial_metrics(trial_number, n_trades)` — fed by hyperopt after every trial.
- `get_robust_optima()` — returns the final parameter dict.
- `get_phase()` — current phase string for UI.
- `select_best_export(study)` — final Occam-regularized selection.

The hyperopt core (`hyperopt.py`, +1,355 lines) tags every trial with a `plateau_phase`
field, exposes `_epoch_callback` for walk-forward consumers, and enforces a budget guard
that requires `--epochs >= 1 + n_params * MIN_POINTS_PER_PARAM` when PlateauSampler is
active. Early-stop is disabled with PlateauSampler.

The output module (`hyperopt_output.py`, +68 / -50 lines) adds a `Phase` column to the rich
table with per-phase styling (`[dim]BASE[/dim]`, `[cyan]SCAN[/cyan]`, `[green]ASSM[/green]`).

For a deep dive, see [docs/hyperopt-plateausampler.md](hyperopt-plateausampler.md) (659 lines).

### 2.2 `--sampler` CLI Flag

`hyperopt_optimizer.py` registers PlateauSampler in `optuna_samplers_dict` alongside every
sampler shipped with Optuna. The fork adds a `--sampler` CLI flag (propagated from
`cli_options.py` to `config.hyperopt_sampler` in `configuration.py`) that accepts:

- `TPE` — Tree-structured Parzen Estimator (Optuna default).
- `NSGA-II` / `NSGA-III` — multi-objective evolutionary samplers.
- `CMA-ES` — Covariance Matrix Adaptation Evolution Strategy.
- `GP` — Gaussian Process.
- `QMC` — Quasi-Monte Carlo.
- `PlateauSampler` — the coordinate-wise sampler described above.

```bash
freqtrade hyperopt --strategy MyStrat --hyperopt-loss MoutonMomentumLoss \
    --sampler PlateauSampler --epochs 800
```

Each CLI option now ships with **practical guidance** in its help text: e.g. the `--epochs`
help advises "300-500 for initial exploration; 1000+ for final optimization" and the
`--hyperopt-loss` help recommends `SharpeHyperOptLoss` for momentum strategies and
`CalmarHyperOptLoss` for low-drawdown styles.

### 2.3 Walk-Forward Analysis (`freqtrade walk-forward`)

`freqtrade/optimize/walk_forward.py` (**2,652 lines**) plus four support files (
`wfa_html_report.py` 2,777 lines, `wfa_output.py` 414 lines, `wfa_glossary.py` 979 lines,
`walk_forward_commands.py` 47 lines) implement a full walk-forward analysis framework.

**Modes.**

- **Rolling**: fixed-width windows that slide forward.
- **Anchored**: training window grows over time, test window slides forward.
- **CPCV** (Combinatorial Purged Cross-Validation): partitions the timeline into groups
  and runs every combination of test groups, with embargo to prevent lookahead. CPCV
  drives the **Probability of Backtest Overfitting (PBO)** estimate.

**Features.**

- **Embargo** period between train and test to prevent lookahead bias.
- Optional **holdout** period reserved from optimization.
- **Multi-seed** stability test.
- **Monte Carlo** simulation by trade shuffling for drawdown distribution.
- **Walk-Forward Efficiency (WFE)** per window.
- **Parameter stability** tracking across windows.
- Statistical significance tests (t-test on returns, chi-squared on win rates).
- **PBO estimate** via CPCV.

A new run mode `RunMode.WALKFORWARD` is added to `OPTIMIZE_MODES` in `enums/runmode.py`.

**CLI options** (added in `arguments.py` +38 lines, `cli_options.py` +85 lines):

```
--wf-windows          Number of windows
--wf-train-ratio      Train fraction per window
--wf-embargo-days     Embargo period between train and test
--wf-holdout-months   Holdout reserved from optimization
--wf-min-test-trades  Minimum trades per test window
--wf-mode             rolling | anchored | cpcv
--wf-multi-seed       Number of seeds for stability test
--wf-cpcv-groups      Number of CPCV groups
--wf-cpcv-test-groups Number of test groups per CPCV combination
```

**Example.**

```bash
freqtrade walk-forward \
    --strategy MyStrat \
    --hyperopt-loss MoutonMeanRevLoss \
    --sampler PlateauSampler \
    --epochs 600 \
    --wf-mode rolling \
    --wf-windows 6 \
    --wf-train-ratio 0.7 \
    --wf-embargo-days 3 \
    --wf-holdout-months 2 \
    --wf-min-test-trades 40 \
    --timerange 20230101-20251231
```

Output is rendered through `WFADashboard` (`wfa_output.py`) and a standalone HTML report
(`wfa_html_report.py`). The glossary file ships in-context tooltips and definitions for
every WFA concept (PBO, WFE, dispersion bands, regime overlay, ...). See
[docs/walk-forward-analysis.md](walk-forward-analysis.md) (461 lines).

### 2.4 Custom Hyperopt Loss Functions

The fork ships three production-grade custom loss functions in
`freqtrade/optimize/hyperopt_loss/`:

**`hyperopt_loss_mouton_meanrev.py`** (341 lines) — Tuned for **DCA / mean-reversion**.
Additive weighted score over 8 metrics:

| Metric | Weight |
|--------|--------|
| Annualized return | 0.25 |
| K-ratio | 0.18 |
| Profit factor | 0.13 |
| Quarterly consistency | 0.14 |
| Payoff | 0.08 |
| Diversity | 0.08 |
| Time Under Water (TUW) | 0.08 |
| Confidence | 0.06 |

Multiplicative gates: concentration penalty (sigmoid on top-2-trades share), drawdown
penalty (`score / exp(5 * max_dd)`). Hard filters: profit <= 0, trades < 40, win rate < 50%,
drawdown > 50%, pairs < 5, training window < 30 days.

**`hyperopt_loss_mouton_momentum.py`** (334 lines) — Tuned for **trend-following / momentum**.

| Metric | Weight |
|--------|--------|
| Return | 0.22 |
| Payoff | 0.16 |
| Sharpe | 0.14 |
| Tail ratio | 0.12 |
| Profit factor | 0.10 |
| Quarterly consistency | 0.09 |
| Diversity | 0.06 |
| TUW | 0.06 |
| Confidence | 0.05 |

Uses Sharpe (appropriate for momentum, unlike mean-reversion which is penalized by Sharpe).
Includes consecutive-loss penalty and exponential drawdown gate.

**`hyperopt_loss_my_profit_drawdown.py`** (54 lines) — Simple
`-(total_profit - 3 * max_drawdown)`.

```bash
freqtrade hyperopt --hyperopt-loss MoutonMomentumLoss --strategy MyTrend
freqtrade hyperopt --hyperopt-loss MoutonMeanRevLoss   --strategy MyDca
freqtrade hyperopt --hyperopt-loss MyProfitDrawdownLoss
```

See [docs/hyperopt-custom.md](hyperopt-custom.md) (447 lines).

### 2.5 Hyperopt HTML Report and Console Summary

`hyperopt.py` adds `_save_run_metadata()` / `_save_run_end_metadata()` which write a
`.meta.json` per hyperopt run containing strategy source, sanitized config, reconstructed
CLI command, timestamps, best loss, profit and Sharpe. `_reconstruct_command()` derives the
exact CLI invocation from the config to guarantee reproducibility.

`_print_post_run_summary()` prints a rich console summary at the end of each hyperopt run.

`freqtrade/optimize/hyperopt_html_report.py` (**6,212 lines**) generates a standalone HTML
report per run, including convergence curves, per-epoch tables, parameter distribution
charts, and a clear annotation of `plateau_phase` when PlateauSampler is active. The HTML
report is the same artifact that the FreqUI Ultimate strategy-dev panel consumes through the
`/api/v1/stratdev/*` endpoints.

`wfa_silent=True` suppresses progress bars and output when many hyperopt runs are chained
(walk-forward kicks off one hyperopt per window).

---

## 3. Hyperliquid-Specific

`freqtrade-ultimate` is the only Freqtrade fork that ships **first-class Hyperliquid support
for perpetual futures**, including features that upstream simply does not handle because
Hyperliquid behaves differently from CEX exchanges.

### 3.1 Liquidation Detection

**Where upstream falls short.** Upstream has no concept of a liquidation fill; it sees the
position disappear from `fetch_positions` and treats the trade as closed at an arbitrary
market price.

**How the fork solves it.**

`freqtrade/exchange/hyperliquid.py` (+65 lines) introduces
`fetch_liquidation_fills(pair, since)` which calls `fetch_my_trades` and filters by the
`liquidationMarkPx` field in the raw Hyperliquid response. It returns a list of dicts with
`price`, `amount`, `timestamp`, `side`, and `liq_mark_price`, with NaN/zero validation.

The base `exchange.py` adds a stub `fetch_liquidation_fills()` that returns `[]` so other
exchanges do not break.

`freqtradebot.py` adds `_handle_liquidation()`: when a position disappears from
`fetch_positions`, the bot calls `fetch_liquidation_fills`, cancels the stoploss on the
exchange, closes the trade at the liquidation price with `exit_reason="LIQUIDATION"`,
emits a Telegram notification, and triggers protections.

### 3.2 External Close Detection

`_handle_external_close()` in `freqtradebot.py` handles positions closed externally — ADL
events or a manual close through the Hyperliquid UI. It first attempts to obtain the real
fill price through `get_trades_for_order("external", ...)`; on failure it falls back to the
market price. The trade is closed with `exit_reason="external_close"`.

Both `_handle_liquidation()` and `_handle_external_close()` are dispatched from
`handle_onexchange_order()` when a futures position has `total == 0` on the exchange.

The `StoplossGuard` plugin (`plugins/protections/stoploss_guard.py`, +1 line) was updated to
**include `external_close` in its counted exit reasons**, so externally closed trades count
toward the guard threshold.

### 3.3 Leverage and Margin Error Handling in DCA

`hyperliquid.py` updates `_lev_prep` to catch "insufficient margin" and "decrease leverage"
errors during DCA and continue with a warning instead of raising. This is essential because
DCA in a losing position is a deliberate strategy and a margin error there should not crash
the bot.

`_fetch_and_calculate_funding_fees` now returns `float | None` (instead of `float`) in both
`hyperliquid.py` and the base `exchange.py`, so that funding-fee calculation failures can be
distinguished from "zero funding".

### 3.4 Rate Limit Fix (`ccxt.RateLimitExceeded`)

`hyperliquid.py` changes `except ccxt.DDoSProtection` to
`except (ccxt.DDoSProtection, ccxt.RateLimitExceeded)`. The same fix was applied
**systematically across every exchange subclass and the base `exchange.py`** —
approximately **25 exception handlers** in total (binance, bitget, bybit, gate, kraken,
krakenfutures, okx, hyperliquid, ...). Upstream missed `ccxt.RateLimitExceeded` and would
crash on rate-limit responses that did not happen to be DDoS-flagged.

`order["filled"] > 0` was also changed to `(order["filled"] or 0) > 0` for null safety.

### 3.5 Hyperliquid Historical Data Bundle

`user_data/data/hyperliquid/futures/` ships approximately **3,250+ Feather files**
containing OHLCV history for **300+ pairs** across 8 timeframes (`5m`, `15m`, `30m`, `1h`,
`2h`, `4h`, `1d`, plus `1h-funding_rate`).

The fork's `.gitignore` was edited specifically to track these Feather files for supported
timeframes (upstream `.gitignore` excludes all data).

A helper script `download.sh` performs a **10-day rolling candle refresh** to keep the
bundle current; running it weekly is sufficient for most strategies.

---

## 4. Pairlists and Risk Management

### 4.1 TrendRegularityFilter

`freqtrade/plugins/pairlist/TrendRegularityFilter.py` (251 lines) is a new pairlist plugin
designed for **short strategies**: it excludes pairs that exhibit a strong, linear uptrend
(positive slope and high R²), because shorting them is statistically dangerous.

**Algorithm.** Linear regression via numpy on close prices over `lookback_period` candles
of `lookback_timeframe`. A pair is excluded when slope > 0 **and** R² >= `min_r2`.

**Parameters.**

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `lookback_timeframe` | `"1h"` | Timeframe to sample |
| `lookback_period` | `5000` | Number of candles |
| `min_r2` | `0.6` | Minimum R² to trigger exclusion |
| `refresh_period` | `3600` | Cache TTL in seconds |

Registered in `constants.py` as `"TrendRegularityFilter"` in `AVAILABLE_PAIRLISTS`. The
filter uses the shared pairlist cache client (see [section 1.2](#12-pairlist-cache-daemon-ftpairlists))
for cross-bot deduplication.

```json
{
  "pairlists": [
    {"method": "VolumePairList", "number_assets": 100, "sort_key": "quoteVolume"},
    {"method": "TrendRegularityFilter", "lookback_timeframe": "1h",
     "lookback_period": 5000, "min_r2": 0.6, "refresh_period": 3600}
  ]
}
```

### 4.2 `backtest_lock_wallet` Flag

**Where upstream falls short.** Upstream backtests compound profits indefinitely, which
produces unrealistic equity curves for strategies that the trader actually withdraws
profits from.

**How the fork solves it.** `wallets.py` checks a new config flag
`backtest_lock_wallet: true` in `BACKTEST` / `HYPEROPT` mode. When set, the wallet always
reports the **initial wallet value**, no matter how much profit has been accumulated.
Position sizing then operates on the starting capital, which prevents the compounding tail
from dominating the optimization.

### 4.3 Capital Withdrawal Accounting

A new config field `capital_withdrawal` (numeric, >= 0) represents capital withdrawn from
the wallet. The wallet computes:

```text
available_amount = starting_balance - capital_withdrawal + total_profit
```

with a warning when the withdrawal exceeds available capital. The value is surfaced through:

- `GET /api/v1/profit` — adds `capital_withdrawal` and `profit_net_coin`.
- `GET /api/v1/balance` — adds `capital_withdrawal` and an enriched `note` field with a
  capital breakdown.
- Telegram `/profit` — adds withdrawal info and net-of-withdrawals profit when
  `capital_withdrawal > 0`.

`wallets.py` also adds `get_capital_withdrawal()` with validation (numeric, no NaN/Inf, no
negative).

Stake amount tolerance was widened: previously `stake_amount * 1.3 < min_stake_amount`
triggered an adjustment, the new threshold is `stake_amount * 1.8 < min_stake_amount`
(ceiling raised from +30% to +80%).

### 4.4 StoplossGuard `external_close` Fix

A one-line fix in `plugins/protections/stoploss_guard.py` adds `"external_close"` to the
set of exit reasons counted by `StoplossGuard`. Without this, positions closed externally
(ADL, manual close from the exchange UI) were not counted toward the guard's threshold —
a silent under-counting issue specific to Hyperliquid.

---

## 5. Rate Limiting Advanced

The fork rebuilds the exchange-layer rate-limiting story around three primitives:
**metrics**, **token re-acquisition**, and **systematic `RateLimitExceeded` catching**
(already covered in [section 3.4](#34-rate-limit-fix-ccxtratelimitexceeded)).

### 5.1 ExchangeMetrics

`freqtrade/exchange/exchange_metrics.py` (230 lines, new) implements:

- `ApiCall` dataclass: `ts`, `method`, `exchange`, `latency_ms`, `cached`, `success`,
  `error_type`, `pair`.
- `BucketStats`: 10-second time buckets with totals, cached / direct counts, errors,
  429s, latency stats, and per-method breakdown.
- `ExchangeMetrics`:
  - Ring buffer of the last **100,000 API calls**.
  - Bucketed stats.
  - Recent-429 history (last **200**).
  - Methods: `record()`, `get_timeline()`, `get_summary()`, `recent_429s()`,
    `get_top_methods()`, `get_latest_buckets()`.

The data is surfaced through `GET /api/v1/rate_metrics` and consumed by the
`RateBudget`, `RatePulse`, `RequestTimeline`, and `CacheRateMonitor` widgets in
[FreqUI Ultimate](https://github.com/titouannwtt/frequi-ultimate).

### 5.2 Retrier Enhanced (token re-acquisition, daemon reporting)

`freqtrade/exchange/common.py` (+155 lines) extends the `retrier` and `retrier_async`
decorators with:

- `_record_metric()` — records the API call (timestamp, method, latency, success / error)
  into `exchange._metrics`.
- `_report_429_to_daemon()` / `_report_429_to_daemon_async()` — notifies the ftcache
  daemon when a 429 occurs so it can throttle subsequent dispatches.
- `_reacquire_rate_token()` / `_reacquire_rate_token_async()` — re-acquires a rate token
  from the daemon before retrying, so retries do not skip the rate budget.

### 5.3 HTTP Connection Pool (50 parallel connections)

`exchange.py` mounts `HTTPAdapter(pool_connections=50, pool_maxsize=50)` on the API
session. This prevents "Connection pool is full" warnings when many requests are in flight
simultaneously (typical with a 14-bot fleet).

### 5.4 `create_order` Retry on 429

`exchange.py` wraps `create_order` in a 2-attempt retry loop with a 5-second sleep on
`ccxt.RateLimitExceeded`. Order placement is critical and should not fail on transient
rate-limit responses — losing an entry signal to a 429 is much worse than waiting 5
seconds.

---

## 6. Strategy Development API

The fork adds a complete **strategy development backend** that powers the Strategy Dev
panel in [FreqUI Ultimate](https://github.com/titouannwtt/frequi-ultimate).

### 6.1 Strategy Dev Backend (`api_stratdev*.py`)

Four new files in `freqtrade/rpc/api_server/`:

| File | Lines | Role |
|------|-------|------|
| `api_stratdev.py` | 495 | Strategy analysis endpoints: read backtest results, compare params, analyze trades |
| `api_stratdev_jobs.py` | 743 | Background job launcher for hyperopt / backtest from the UI |
| `api_stratdev_editor.py` | 299 | File editor endpoints for strategy `.py` files |
| `api_stratdev_schemas.py` | 63 | Pydantic schemas for the stratdev API |

Two support files:

| File | Lines | Role |
|------|-------|------|
| `freqtrade/optimize/stratdev_readers.py` | 3,442 | Parse backtest ZIPs, analyze trades, extract parameters |
| `freqtrade/optimize/stratdev_dataframe.py` | 210 | DataFrame manipulation for strategy dev |

`webserver.py` registers the three stratdev routers under `/api/v1/stratdev/*` with JWT
auth (the same auth flow as the rest of the REST API).

### 6.2 Job Launcher

`api_stratdev_jobs.py` exposes endpoints to launch hyperopt, backtest, and walk-forward
jobs from the UI:

```text
POST   /api/v1/stratdev/jobs/launch
GET    /api/v1/stratdev/jobs/{job_id}
GET    /api/v1/stratdev/jobs/{job_id}/output
DELETE /api/v1/stratdev/jobs/{job_id}
GET    /api/v1/stratdev/jobs
```

Jobs run as background subprocesses with streamed stdout/stderr, cancellable from the UI.

### 6.3 Strategy Editor API

`api_stratdev_editor.py` exposes file-level read/write endpoints for files under
`user_data/strategies/`. The companion FreqUI Ultimate dashboard renders them in a Monaco
editor with syntax highlighting and validation.

---

## 7. Data and Persistence

### 7.1 Hyperliquid OHLCV Bundle

See [section 3.5](#35-hyperliquid-historical-data-bundle). The bundle ships
**3,250+ Feather files** for 300+ pairs across 8 timeframes.

### 7.2 Klines Cache Persistence

`exchange.py` adds:

- `persist_klines()` — dumps `_klines` and `_pairs_last_refresh_time` to a pickle file at
  shutdown.
- `_load_persisted_klines()` — restores the cached klines at startup if they are not too
  stale (age < `startup_candle_count * timeframe * 2`).

The hooks are wired through `__del__` and `__init__`. This avoids re-downloading hundreds
of candles on every restart, which is critical when 14 bots restart simultaneously after a
host reboot or a `launch_bot.sh` cycle.

### 7.3 Wallet Anti-Compounding and Capital Withdrawal

See [section 4.2](#42-backtest_lock_wallet-flag) and
[section 4.3](#43-capital-withdrawal-accounting).

### 7.4 SQLAlchemy Pool Sizing

`persistence/models.py` (+5 / -1) raises the SQLAlchemy engine pool to
`pool_size=20, max_overflow=40, pool_timeout=120` (up from defaults of 5, 10, 30). This
prevents pool exhaustion under heavy concurrent load (many parallel API requests, multiple
job launchers, RPC clients hitting the same DB).

---

## 8. Resilience and Monitoring

### 8.1 Startup Tracer

`_StartupTracer` in `freqtradebot.py` instruments each initialization phase: exchange
load, strategy load, config validation, DB init, wallet sync, pairlist refresh. Per-phase
timings are logged, slow phases (> 2 s) are highlighted, and a boot summary is printed at
the end. The data also feeds the bot's `/api/v1/ping` "starting" response.

### 8.2 Cycle Profiling

Each phase of `process()` is timed through `_cp()`. If the total cycle exceeds 10 s, the
bot logs a warning with a per-phase breakdown (markets, pairlist, candles, analyze, orders,
exits, entries). This makes performance regressions easy to diagnose without an external
profiler.

Cycles also report open pairs to the ftcache daemon at `CRITICAL` priority and mark
initialization complete for fleet tracking.

### 8.3 Enhanced Telegram Notifications

`telegram.py` (+9 lines) extends `/profit` to display withdrawal info and net-of-withdrawals
profit when `capital_withdrawal > 0`.

Two new exit reasons surface on Telegram:

- `LIQUIDATION` — emitted from `_handle_liquidation()`.
- `external_close` — emitted from `_handle_external_close()` (ADL / manual close).

### 8.4 DataProvider Thread Safety

`dataprovider.py` (+40 / -13 lines) adds a `threading.Lock` (`__cached_pairs_lock`) around
every read / write on `__cached_pairs`. Stale-candle detection emits **one warning per pair
per hour** (instead of per cycle, upstream behaviour). Warnings are suppressed in backtest
/ hyperopt. `clear_cache()` is now lock-protected.

### 8.5 Resilient Wallet and Worker Loops

`wallets.py` (+104 / -25 lines):

- Init is wrapped in `try/except` — the bot continues with empty wallets and retries on the
  next cycle.
- `get_balances()` and `fetch_positions()` are each wrapped individually — stale data is
  kept on failure rather than crashing the bot.
- `_update_live()` logs timing when it exceeds 2 s.
- Per-position parsing is wrapped in `try/except` to skip malformed entries.
- Type check `isinstance(position, dict)` before parsing.

`worker.py` `_reconfigure()` is wrapped in `try/except` so a bad config reload does not
kill the bot. `_schedule.run_pending()` is now executed under `_exit_lock`.

---

## 9. REST API Enrichment

### 9.1 New endpoints

| Endpoint | Role |
|----------|------|
| `GET /api/v1/cache_status` | ftcache and pairlist daemon health and stats |
| `GET /api/v1/rate_metrics` | API call timeline, 429 history, token bucket state |
| `GET /api/v1/fleet/status` | Fleet orchestrator: every registered bot with state, pairs, uptime |
| `GET /api/v1/fleet/events` | Fleet event stream (connects, disconnects, rate limits) |
| `GET /api/v1/volume_history` | Historical volume analysis (exchange vs bot, anomaly detection) |
| `GET /api/v1/signal_summary` | Current signal counts per pair (enter_long, exit_long, ...) |
| `/api/v1/stratdev/*` | All Strategy Dev API endpoints — see [section 6](#6-strategy-development-api) |

All new endpoints are JWT-authenticated.

`api_schemas.py` (+223 lines) introduces matching Pydantic models: `CacheStatus`,
`RateMetricsResponse`, `FleetBotStatus`, `VolumeHistoryResponse`, `SignalSummaryResponse`,
`PipelineStep`, and extensions to `WhitelistResponse`.

### 9.2 Enriched endpoints

| Endpoint | Additions |
|----------|-----------|
| `GET /api/v1/ping` | Returns `{"status": "starting"}` during bot init instead of `"pong"` — clients can distinguish "down" from "still booting" |
| `GET /api/v1/whitelist` | Adds `pipeline` (per-handler pair counts), `handler_configs`, `total_market_pairs`, `added_pairs` |
| `GET /api/v1/profit` | Adds `capital_withdrawal`, `profit_net_coin` |
| `GET /api/v1/balance` | Adds `capital_withdrawal`, enriched `note` field with effective capital breakdown |
| `GET /api/v1/show_config` | Adds `tradable_balance_ratio` |

`pairlistmanager.py` (+38 lines) records `_pipeline_snapshot` (per-handler pair counts and
dropped pairs) after every `refresh_pairlist()` and exposes it via the `pipeline_snapshot`
and `handler_configs` properties.

---

## 10. Infrastructure and Scripts

### 10.1 `launch_bot.sh`

42 lines. Wraps the bot in an auto-restart loop with a 60-second countdown between
restarts. Includes a one-shot thundering-herd stagger derived from a hash of the config
filename:

- **Dry-run**: random 20-60 s delay before the first start.
- **Live**: random 0-15 s delay before the first start.

The stagger applies only to the first start, not to subsequent restarts.

### 10.2 `launch_dashboard.sh`

17 lines. Same auto-restart loop pattern, but for the **FreqUI webserver-only mode** (no
trading, just the UI server).

### 10.3 Enhanced Backtesting (ZIP default params, `wfa_silent`)

`backtesting.py` (+15 lines):

- `_extract_strategy_params()` collects parameter defaults via `detect_all_parameters()`
  and embeds them in the backtest archive.
- `strategy_default_params` is passed through to `store_backtest_results()`.
- When `wfa_silent=True` is set in config, the result-print path is silenced — useful
  because walk-forward runs **many** sub-backtests.

`bt_storage.py` (+10 / -7 lines): when there is no co-located `.json` file but
`strategy_default_params` is available, the defaults are written into the ZIP archive. The
filename construction inside the ZIP was also fixed.

`hyperopt_optimizer.py` adds `cloudpickle.register_pickle_by_value` for the strategy and
loss modules to make multiprocessing serialization deterministic in the presence of the
custom cached exchange subclasses, and uses a picklable `_NoOpLock` instead of
`threading.Lock` in worker processes.

A `download.sh` script (referenced in [section 3.5](#35-hyperliquid-historical-data-bundle))
performs a 10-day rolling refresh of the Feather bundle.

---

## 11. WebSocket Resilience

### 11.1 Staggered Subscription

`exchange_ws.py` (+40 / -12 lines) introduces a **100 ms delay between new pair
subscriptions** to avoid burst subscription that some exchanges (Hyperliquid in particular)
will silently drop.

### 11.2 Retry with Exponential Backoff

`_continuously_async_watch_ohlcv` now has an inner retry loop (max **5 retries**, exponential
backoff up to **30 s**). Upstream gave up on the first `ccxt.BaseError`, which made the
WebSocket unreliable on Hyperliquid. `ExchangeClosedByUser` is distinguished from
`BaseError` so that a clean shutdown does not trigger retries.

---

## 12. Documentation and AI Co-Pilot Context

### 12.1 `CLAUDE.md`

`CLAUDE.md` (161 lines at the repo root) documents the fork's architecture for AI
co-pilots: where the daemons live, how multi-bot coordination works, how to extend the
PlateauSampler, where the FreqUI Ultimate companion lives, and the conventions for
strategy file layout.

### 12.2 `.claude-tips/` (199 trading rules)

The `.claude-tips/` directory (15 files) and the top-level `tips.txt` (199 rules) encode
trading guardrails distilled from canonical sources (**Robert Carver, Andreas Clenow, Ernie
Chan, Marcos Lopez de Prado**). They are intended as in-context priors for any AI used to
write or review strategies in this repo — for example, "do not optimize Sharpe alone for
mean-reverting strategies" or "embargo at least 1% of the timeline for CPCV".

### 12.3 Enhanced CLI Help

`cli_options.py` rewrites every hyperopt-related CLI option's help text with:

- **Practical guidance** ("300-500 for initial exploration; 1000+ for final optimization").
- **Per-strategy-type recommendations** ("SharpeHyperOptLoss for momentum,
  CalmarHyperOptLoss for low-drawdown").
- **Trade-off explanations** ("Each worker loads a full data copy, so RAM scales linearly
  with `--job-workers`").
- A new `--sampler` option with choices and per-sampler explanations.

### 12.4 Deploy UI URL Change

`deploy_ui.py` (+19 / -10 lines) points at `titouannwtt/frequi-ultimate` instead of the
upstream `freqtrade/frequi` repository. The deployer now also supports `.tar.gz` artifacts
in addition to `.zip` (some FreqUI release artifacts ship in the former format).

A new docs file [docs/freq-ui.md](freq-ui.md) updates the upstream FreqUI doc to point at
the fork.

---

## 13. Critical Bug Fixes

Beyond the feature work, the fork includes several **production bug fixes** that affect
correctness or stability. These are upstream regressions or latent bugs that were observed
in live trading.

1. **Order-date comparison inversion** — `freqtradebot.py`:
   `order_date_utc - timedelta(days=5)` was changed to `order_date_utc + timedelta(days=5)`.
   The upstream logic was inverted and considered **every** order as "older than 5 days".

2. **Funding fees `None` guard** — `freqtradebot.py`:
   `funding_fees=funding_fees or 0.0` prevents `None` from being passed into the
   `LocalTrade()` constructor.

3. **Hyperliquid `order["filled"]` null safety** — `hyperliquid.py`:
   `(order["filled"] or 0) > 0` prevents a crash comparing `None > 0`.

4. **Session rollback on `ExchangeError`** — `freqtradebot.py`:
   `handle_onexchange_order()` now calls `Trade.session.rollback()` on `ExchangeError`
   (and generic exceptions) to avoid a corrupted DB state. Without this fix, a transient
   exchange error during order reconciliation would leave the SQLAlchemy session in an
   inconsistent state.

5. **`RLock` instead of `Lock`** — `freqtradebot.py`:
   `_exit_lock` was changed from `Lock()` to `RLock()` to prevent deadlock when nested
   calls (e.g. exit handler triggering protections triggering another exit) re-acquire the
   same lock.

6. **`trade.recalc_trade_from_orders()`** added after position-size drift detection in
   `handle_onexchange_order()`, so the local trade state matches the on-exchange state.

7. **Async loop guard** in `exchange.py` `calculate_funding_fees` — protects against a
   conflicting event loop already running.

8. **RPC error log throttling** — `webserver.py`: the same error is logged at most once
   per 5 minutes instead of once per request.

9. **`ApiServer` singleton guard restored** — `webserver.py`: prevents double-initialization
   during `reload_config`.

---

## Configuration Schema Additions

`freqtrade/config_schema.py` and `freqtrade/configuration.py` introduce the following new
top-level config fields. All are optional; defaults are listed below.

```json
{
  "shared_ohlcv_cache": {
    "enabled": true,
    "socket_path": "/tmp/ftcache.sock"
  },

  "capital_withdrawal": 0,

  "backtest_lock_wallet": false,

  "hyperopt_sampler": "TPE"
}
```

- **`shared_ohlcv_cache.enabled`** (bool, default `true` when key absent) — enables the
  cached exchange subclass. Setting to `false` reverts to upstream behaviour.
- **`capital_withdrawal`** (number >= 0, default `0`) — withdrawn capital subtracted from
  the wallet's effective balance. Surfaced through `/profit`, `/balance`, and Telegram
  `/profit`.
- **`backtest_lock_wallet`** (bool, default `false`) — when `true` in `BACKTEST` /
  `HYPEROPT` mode, the wallet always reports the initial balance (no compounding). Has no
  effect in live trading.
- **`hyperopt_sampler`** (string, default `"TPE"`) — Optuna sampler key. Accepts
  `TPE`, `NSGA-II`, `NSGA-III`, `CMA-ES`, `GP`, `QMC`, `PlateauSampler`. Override on the
  CLI with `--sampler`.

Walk-forward analysis adds **no config schema entries** — every walk-forward parameter is a
CLI flag (`--wf-*`, see [section 2.3](#23-walk-forward-analysis-freqtrade-walk-forward)).

The `available_capital` field gets an enriched description in the schema.

---

## Migration Notes

If you are upgrading from upstream Freqtrade to `freqtrade-ultimate`, here is the
minimum-change path:

1. **Pull the repo** and run `pip install -e .` from the fork root.
2. Optional but recommended: keep `shared_ohlcv_cache.enabled = true` (the default) to
   benefit from the daemon. Set to `false` if you intentionally want upstream behaviour.
3. **Backtest configs**. Set `backtest_lock_wallet: true` if you want anti-compounding
   semantics in your backtests. The default `false` keeps upstream behaviour.
4. **Withdrawals**. Set `capital_withdrawal` to the cumulative amount you have moved out
   of the wallet. The API and Telegram outputs will adjust automatically.
5. **Hyperopt**. Your existing hyperopt commands keep working with the upstream TPE
   sampler. To try PlateauSampler, add `--sampler PlateauSampler` and ensure
   `--epochs >= 1 + n_params * 4`.
6. **Walk-forward**. Replace your manual train/test splits with
   `freqtrade walk-forward ...` — see the example in
   [section 2.3](#23-walk-forward-analysis-freqtrade-walk-forward).
7. **launch scripts**. Replace `freqtrade trade --config ...` with
   `./launch_bot.sh path/to/config.json` to get auto-restart, candle-boundary jitter and
   fleet stagger for free.
8. **FreqUI**. Run `freqtrade deploy-ui` to fetch the [FreqUI Ultimate](https://github.com/titouannwtt/frequi-ultimate)
   build — the upstream FreqUI build does not know about the new endpoints
   (`/cache_status`, `/rate_metrics`, `/fleet/*`, `/volume_history`, `/signal_summary`,
   `/stratdev/*`) and will not render the corresponding widgets.

No DB migration is required; the SQLAlchemy schema is unchanged. The pool sizing change
([section 7.4](#74-sqlalchemy-pool-sizing)) takes effect on next process start.

---

## Compatibility

- **Upstream base.** Tracks `freqtrade/freqtrade` `stable` branch. The fork sits ~58,400
  lines added and ~2,450 lines removed across 50 commits.
- **Python.** Requires Python **3.10 or newer** (the async daemons and `cloudpickle`
  pickle-by-value integration need it).
- **CCXT.** Pinned to a version supporting Hyperliquid spot and perpetuals (see the
  fork's `setup.py` / `pyproject.toml` for the exact pin).
- **Hyperliquid.** First-class support, including liquidation detection
  ([section 3.1](#31-liquidation-detection)), external close
  ([section 3.2](#32-external-close-detection)), and DCA margin-error tolerance
  ([section 3.3](#33-leverage-and-margin-error-handling-in-dca)).
- **Other exchanges.** All cached subclasses are auto-generated for `binance`, `bitget`,
  `bybit`, `gate`, `kraken`, `krakenfutures`, `okx`, and the rest of the upstream list (20
  cached variants in total). Non-Hyperliquid features (PlateauSampler, walk-forward,
  ftcache, ftpairlists, rate metrics, ...) work on any supported exchange.
- **Docker.** The fork's `docker-compose.yml` and `Dockerfile` are based on upstream's; the
  cached daemons run inside the same container. nginx is pinned to `1.29.8-alpine` for the
  FreqUI Ultimate companion image.
- **FreqUI.** [FreqUI Ultimate](https://github.com/titouannwtt/frequi-ultimate) v0.5.0 or
  newer is the companion build that knows how to talk to every new endpoint.

---

## Further Reading

- [Showcase strategies](../user_data/strategies/) — public strategies shipped with this
  fork.
- [PlateauSampler deep dive](hyperopt-plateausampler.md) — 659-line guide on the
  coordinate-wise sampler.
- [Walk-Forward Analysis guide](walk-forward-analysis.md) — 461-line user-facing guide.
- [Custom hyperopt loss reference](hyperopt-custom.md) — 447-line documentation of
  `MoutonMeanRevLoss`, `MoutonMomentumLoss`, and `MyProfitDrawdownLoss`.
- [FreqUI Ultimate](https://github.com/titouannwtt/frequi-ultimate) — companion dashboard
  with 21 draggable widgets, the Strategy Dev panel, multi-currency conversion, and 14
  alert types.
- [Freqtrade France](https://buymeacoffee.com/freqtrade_france) — tutorials, advanced
  courses, paid-member strategies, community support.
