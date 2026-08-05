# Changelog — Freqtrade Ultimate

All notable fork-specific changes are documented here. Upstream Freqtrade changes are tracked in [freqtrade/freqtrade releases](https://github.com/freqtrade/freqtrade/releases).

Versioning convention: `v<upstream_version>-fork.<n>` — e.g. `v2026.3-fork.5` means "fork iteration 5 based on upstream 2026.3".

## [Unreleased]

### Upstream sync tracking — freqtrade 2026.7 (2026-07-31)

Upstream [freqtrade 2026.7](https://github.com/freqtrade/freqtrade/releases/tag/2026.7) was published 2026-07-31. This section documents the delta `2026.6..2026.7` and its impact on this fork. **No application code is changed in this tracking PR** — the sync itself must be applied as its own follow-up PR (same pattern as the previous `Merge upstream freqtrade 2026.6 (#30)`).

**Delta size:** 277 commits, 89 files, +11 355 / −4 448.

**Upstream highlights (from the 2026.7 release notes):**
- Backtesting performance improvements (when used with `--export signals`).
- Timerange filtering for trades in parquet format; Arrow-based OHLCV filtering (feather/parquet).
- OHLCV storage recommendation now favors **feather only** (drops parquet).
- OKX / MyOKX: support both stop-market and stop-limit orders.
- FreqAI: backtest pairs with partial data availability (later listing date).
- Deprecation: `from freqtrade.vendor.qtpylib` → `from technical import qtpylib`.
- Fix: `get_conversation_rate` when rate is `None`.
- Fix: don't persist 0 balances to wallet history.
- Fix: Hyperliquid properly sets `lastTradeTimestamp` (fills the `filled_date` for orders returned by `_add_missing_trades_from_trades`).
- Fix: `handle_on_exchange_orders` no longer breaks when no order is found.
- Feat: include funding fees in the wallet-history migration.
- Chore: refreshed Binance leverage tiers; ta_lib armhf wheel bump to 0.7.1.

**Fork-sensitive files touched by upstream (impact scan):**

| File | Upstream delta | Impact on fork | Effort |
|------|----------------|----------------|--------|
| `freqtrade/freqtradebot.py` | 113 lines | 🔴 **Conflict** — `enter_positions()` signature changes to `enter_positions(free_trade_slots: int)`; call site in `process()` becomes `if …((free_trade_slots := self.get_free_open_trades()) > 0): self.enter_positions(free_trade_slots)`. The fork has a `_cp("entries")` probe on the line right after `self.enter_positions()`, so the merge won't auto-resolve. Also, upstream guards `order_close_notify` with `if order_obj:` in `update_trade_state()`. Custom handlers `_handle_external_close`, `_handle_liquidation`, `_coordinator.opposite_side_sibling` guard block are **unaffected** — they live in a separate branch of `handle_onexchange_order()` untouched by upstream. Manual 3-way needed on the two `enter_positions` sites and on the `order_obj` guard block. | ~20 min |
| `freqtrade/exchange/hyperliquid.py` | +2 lines | 🟢 **Trivial** — one added line: `order["lastTradeTimestamp"] = max(t.get("timestamp") or 0 for t in trades)` inside `_add_missing_trades_from_trades`. Fork's `fetch_liquidation_fills()` is a separate method, unaffected. Cherry-pick clean. | ~2 min |
| `freqtrade/persistence/trade_model.py` | 101 lines | 🟡 **Portable but bulky** — refactors `LocalTrade.to_json()` to compute `filled_entry_orders`, `filled_exit_orders`, `open_orders_wo_sl`, `open_sl_orders`, `date_entry_fill_utc`, `stoploss_last_update_utc`, `trade_duration_s` once instead of via repeated property calls. Fork does not add fields inside `to_json` — merge should be clean but the diff is large enough to warrant a visual review. | ~10 min |
| `freqtrade/wallets.py` | 3 lines | 🟢 **Trivial** — `if wallet.total == 0: continue` inside the `get_all_balances()` loop (paired with the "don't persist 0 balances" fix). Cherry-pick clean. | ~1 min |
| `freqtrade/rpc/rpc.py` | +6 lines | 🟢 **Trivial** — adds `if prev_len > 0 and len(df_analyzed) == 0: raise RPCException(...)` after `trim_dataframe` in `pair_analysis` (guards against startup_candle_count trimming to empty). No fork override on this path. | ~1 min |
| `freqtrade/rpc/telegram.py` | 1 line | 🟢 **Trivial** — help text change `/balance total` → `/balance full`. Fork's telegram customizations, if any, are elsewhere. | ~1 min |
| `freqtrade/plugins/pairlist/AgeFilter.py`, `IPairList.py`, `StaticPairList.py` | 3 lines each | 🟢 **Trivial** — signature harmonization for the new pairlist API. Fork's `TrendRegularityFilter.py` follows the same `IPairList` shape; check it inherits the updated signature without warning after the sync. | ~5 min |

**Past-conflict files (from PR #30) — NOT touched by 2026.7 → free ride:**
- `freqtrade/data/metrics.py` (both `calculate_pvalue` and `calculate_p_value` remain)
- `freqtrade/commands/deploy_ui.py` (fork's `.tar.gz` FreqUI installer preserved)
- `freqtrade/rpc/api_server/api_v1.py` (`API_VERSION` renumbering)
- `freqtrade/rpc/api_server/webserver.py` (stratdev routers)
- `freqtrade/rpc/api_server/api_replay.py` (replay routers)

**Estimated total conflict-resolution effort:** ~40 minutes (dominated by the `enter_positions` signature change and the `trade_model.to_json` refactor visual review), vs. the ~4 hours the 2026.6 sync took.

**Structural note:** the fork history is squash-style vs. upstream (`git merge-base HEAD 2026.7` returns commit `4139b0b0c` — the ancient 2017 `add signal handler for SIGINT, SIGTERM and SIGABRT` commit). `git merge 2026.7 --allow-unrelated-histories` produces ~50 add/add conflicts because every file added since 2017 registers as add/add, so **the previous sync approach must be reused**: apply the upstream diff (`git diff 2026.6..2026.7 | git apply --3way`) rather than a git-merge, then hand-resolve the ~5 real conflict sites listed above.

**Follow-up plan:**
1. Merge this tracking PR (zero-risk documentation).
2. Open a follow-up PR that applies the `2026.6..2026.7` diff via `git apply --3way`, hand-resolves the freqtradebot.py sites, runs `pytest --random-order -n auto` and the replay determinism harness, bumps the version marker to `2026.7`.
3. Follow up on the `qtpylib` deprecation warning across the fork's strategies (`grep -r "freqtrade.vendor.qtpylib" user_data/` — expect several strategy files to need `from technical import qtpylib`).
4. Let Dependabot flush the dep bumps (ccxt 4.5.61→4.5.68, sqlalchemy, tqdm 4.68.3→4.69.0, fastapi 0.139.0→0.139.2, ruff 0.15.18→0.15.22, mypy 2.2→2.3, filelock 3.29.7→3.31.1, ta-lib 0.6.8→0.7.1, lightgbm 4.6→4.7, numexpr 2.14.1→2.14.2, websockets 16.1→16.1.1) — six of these already have open Dependabot PRs.

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
