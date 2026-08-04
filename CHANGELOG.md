# Changelog — Freqtrade Ultimate

All notable fork-specific changes are documented here. Upstream Freqtrade changes are tracked in [freqtrade/freqtrade releases](https://github.com/freqtrade/freqtrade/releases).

Versioning convention: `v<upstream_version>-fork.<n>` — e.g. `v2026.3-fork.5` means "fork iteration 5 based on upstream 2026.3".

## [Unreleased]

### Branding & documentation
- Renamed repository from `freqtrade-fork` to `freqtrade-ultimate`.
- Full English-only README rewrite, emphasizing Freqtrade France brand and pointing to [`docs/FEATURES.md`](docs/FEATURES.md) for full technical reference.
- Added [`docs/FEATURES.md`](docs/FEATURES.md) — 6 543-word exhaustive technical reference of every feature added (32 features across 13 categories).
- Added [`docs/AI_USAGE.md`](docs/AI_USAGE.md) — guidance for LLMs and AI copilots interacting with the repo.
- Added [`LLMS.txt`](LLMS.txt) at root — `llms.txt` convention for AI indexing.
- Added [`CONTRIBUTING.md`](CONTRIBUTING.md), [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) rewritten for the fork.
- Added GitHub meta: `.github/FUNDING.yml`, `.github/dependabot.yml`, `.github/workflows/lint.yml`, `.github/PULL_REQUEST_TEMPLATE.md`, `.github/ISSUE_TEMPLATE/{config,bug_report,feature_request}.yml`.

## [v2026.3-fork.5] — Production-grade Freqtrade fork for Hyperliquid

This is the consolidated snapshot of all fork-specific features as of the v2026.3-fork.5 tag.

### Multi-bot infrastructure
- **OHLCV Cache Daemon (`ftcache`)** — Shared candle cache across N bots, 75 % API call reduction measured with four bots. Token-bucket rate limiter with priority queue (CRITICAL > HIGH > NORMAL > LOW), feather persistence, async client with in-flight request coalescing, auto-generated `Cached{Exchange}` subclasses for 18+ exchanges.
- **Pairlist Cache Daemon (`ftpairlists`)** — Deduplicates pairlist filter computation between bots. Pairlist refresh time on multi-bot setups: 15 min → 3 min.
- **Position Guard + Leverage Sync** — Prevents conflicting entries when an opposite position exists for the same pair (multi-wallet), detects cross-bot leverage changes and syncs the DB.
- **Fleet State Notifications + Auto-Restart** — Worker reports state changes (running/paused/stopped) to the daemon; startup jitter (0-4.9 s live, 5-14.9 s dry-run) prevents thundering herd; admission hold-off; `launch_bot.sh` auto-restart loop.

### Hyperopt & validation
- **PlateauSampler** — Coordinate-wise Optuna sampler; four-phase pipeline (baseline → scan → assembly → refinement); auto-budgeting `min_epochs = 1 + n_params × MIN_POINTS_PER_PARAM`; exports robust parameters directly to strategy JSON; hard validation block when epochs < budget.
- **`--sampler` CLI flag** — Switch between TPE, NSGA-II/III, CMA-ES, GP, QMC samplers without editing strategy code.
- **Walk-Forward Analysis** (`freqtrade walk-forward`) — 2 652 lines of new code; rolling / anchored / CPCV (Combinatorial Purged Cross-Validation) modes; Monte Carlo drawdown simulation; multi-seed convergence testing; WFE (Walk-Forward Efficiency) and PBO (Probability of Backtest Overfitting); A–F verdict + interactive HTML report.
- **Custom hyperopt loss functions** — `MoutonMeanRevHyperOptLoss` (8 weighted metrics for mean-reversion / DCA), `MoutonMomentumHyperOptLoss` (8 weighted metrics for trend / momentum), `MyProfitDrawDownHyperOptLoss` (baseline).
- **Hyperopt HTML report** — 6 212 lines of new code, auto-generated post-run with parameter agreement analysis and convergence chart.
- **CLI help text** — All hyperopt options rewritten with practical guidance, per-strategy recommendations, and explicit tradeoff explanations.

### Hyperliquid-specific
- **Liquidation detection** — `fetch_liquidation_fills()` interrogates user fills, detects `liquidationMarkPx` field, closes trade at liquidation price with `exit_reason=LIQUIDATION`.
- **External close detection** — `_handle_external_close` recognizes positions closed externally (ADL or manual UI close) via wallet mismatch, closes at market with `exit_reason="external_close"`.
- **Resilient margin / leverage handling** — DCA continues on "insufficient margin" / "decrease leverage" instead of crashing; funding fees return `float | None` for non-calculable cases; rate-limit fix `ccxt.DDoSProtection` → `(ccxt.DDoSProtection, ccxt.RateLimitExceeded)` everywhere.
- **Local Hyperliquid historical data bundle** — 3 250+ feather files (300+ pairs × 8 timeframes including crypto indices and TradFi perpetuals).

### Pairlists & risk
- **`TrendRegularityFilter`** — Excludes pairs with strong regular linear uptrend (high R²). Useful for short strategies. Shared cache across multi-bot.
- **`backtest_lock_wallet`** — Disables compounding in backtests for honest equity curves.
- **Capital withdrawal accounting** — Tracks `available = starting - withdrawal + closed_profit` semantics in REST API, Telegram `/profit`.

### Observability
- **`ExchangeMetrics`** — 230-line module with ring buffer (100K calls), 10 s buckets, top methods, recent 429s.
- **Enriched REST API** — `/cache_status`, `/rate_metrics`, `/fleet/status`, `/fleet/events`, `/volume_history`, `/signal_summary`, `/stratdev/*`; enriched `/ping` (status: starting), `/whitelist`, `/profit`, `/balance`.
- **Enhanced Telegram** — `LIQUIDATION` and `external_close` exit reasons surfaced; withdrawal-aware `/profit`.

### Developer experience
- **Strategy Dev Backend** — 4 new API files (`api_stratdev.py`, `api_stratdev_jobs.py`, `api_stratdev_editor.py`, `api_stratdev_schemas.py`); backtest reader, jobs runner, strategy editor.
- **AI Copilot** — `CLAUDE.md` (161 lines), `.claude-tips/` (14 files, 199 trading rules from Carver, Clenow, Chan, López de Prado, Freqtrade France community), routing table.
- **Enhanced CLI** — Practical guidance and recommendations in every option.
- **Deploy UI URL** — `freqtrade install-ui` now targets `titouannwtt/frequi-ultimate` by default; supports `.tar.gz` artifacts in addition to `.zip`.

### Resilience
- **Startup tracer** — Phase-by-phase boot timing; logs slow phases (> 2 s); summary breakdown.
- **Klines cache persistence** — Saves `_klines` and `_pairs_last_refresh_time` to pickle at shutdown; restores on startup if not stale.
- **DataProvider thread safety** — Lock around `__cached_pairs` read/write; stale-candle detection throttled to once per hour.
- **WebSocket resilience** — Staggered subscription (100 ms delay), retry with exponential backoff (max 5), separate `ExchangeClosedByUser` from `BaseError`.
- **Connection pool** — 50 parallel HTTP connections; `create_order` retry with 5 s sleep on rate limit.
- **SQLAlchemy pool** — `pool_size=20, max_overflow=40, pool_timeout=120`.

### Critical bug fixes
- Order date comparison: `-timedelta(days=5)` → `+timedelta(days=5)` (logic was inverted).
- Funding fees null safety: `funding_fees or 0.0`.
- Order filled check: `order["filled"] or 0`.
- Session rollback on `ExchangeError`.
- `_exit_lock`: `Lock` → `RLock` (deadlock prevention).

### Infrastructure scripts
- `launch_bot.sh` — Auto-restart loop with 60 s countdown, thundering-herd staggering.
- `launch_dashboard.sh` — UI-only mode for the dashboard.
- `download.sh` — Pulls last 10 days of candles for all configs.

---

For the complete, deep technical reference of every feature, see [`docs/FEATURES.md`](docs/FEATURES.md).

[Unreleased]: https://github.com/titouannwtt/freqtrade-ultimate/compare/v2026.3-fork.5...HEAD
[v2026.3-fork.5]: https://github.com/titouannwtt/freqtrade-ultimate/releases/tag/v2026.3-fork.5
