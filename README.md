<p align="center">
  <img src=".readme_illustrations/logo_freqtrade_ultimate.jpg" width="180" alt="Freqtrade Ultimate logo">
</p>

<h1 align="center">Freqtrade Ultimate</h1>

<p align="center">
  <b>The production-grade Freqtrade fork for algorithmic trading on Hyperliquid.</b><br>
  Maintained by <a href="https://buymeacoffee.com/freqtrade_france">Freqtrade France</a>.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPLv3-blue"></a>
  <a href="https://github.com/titouannwtt/freqtrade-ultimate/stargazers"><img src="https://img.shields.io/github/stars/titouannwtt/freqtrade-ultimate?style=social"></a>
  <a href="https://github.com/titouannwtt/freqtrade-ultimate/network/members"><img src="https://img.shields.io/github/forks/titouannwtt/freqtrade-ultimate?style=social"></a>
  <a href="https://buymeacoffee.com/freqtrade_france"><img src="https://img.shields.io/badge/community-Freqtrade%20France-orange"></a>
</p>

<p align="center">
  <a href="#-already-running-freqtrade-switch-to-ultimate-without-losing-anything">Migrate from upstream</a> ·
  <a href="#-new-to-freqtrade-start-here">Start fresh</a> ·
  <a href="#-what-is-freqtrade-ultimate">About</a> ·
  <a href="#-feature-highlights">Features</a> ·
  <a href="#-showcase-strategies">Strategies</a> ·
  <a href="#-learn-algorithmic-trading">Learn</a> ·
  <a href="docs/FEATURES.md">Full feature list</a>
</p>

---

## 🔄 Already running freqtrade? Switch to Ultimate without losing anything

**Freqtrade Ultimate is a drop-in superset of upstream freqtrade.** Same `freqtrade` command, same configuration schema, same database format — it just adds features. Your bot *is* its `user_data/` folder (strategies, configs, hyperopt and backtest results, downloaded data) plus its trade database. Switching forks means pointing a Freqtrade Ultimate install at those same files. **Nothing about a running bot changes**: same open trades, same config, same strategy — it simply resumes with extra capabilities available.

<details>
<summary><b>Before you start (30-second checklist)</b></summary>

- **Stop the bot cleanly first** — let it finish its cycle (`Ctrl+C` in its screen/tmux, or `systemctl stop`). Don't migrate a bot mid-trade.
- **Back up your trade database and config** — `cp tradesv3.sqlite tradesv3.sqlite.bak`. Cheap insurance.
- **Database migrations are forward-only and automatic.** Ultimate tracks the current upstream version, so your DB upgrades itself on first start. (Only relevant edge case: don't point Ultimate at a DB written by a *newer* upstream `develop` than this fork — downgrading a DB is unsupported by freqtrade itself.)
- You don't need to uninstall your current freqtrade. The cleanest migrations below keep your old setup intact as an instant rollback.

</details>

### 🖥️ Native install (source / venv — no Docker)

The safest path is **non-destructive**: install the fork in a *new* folder and run your existing bot from it. Your old install is never touched, so rollback is "just start the old one again".

```bash
# 1. Stop your current bot cleanly (Ctrl+C in its screen/tmux, or `systemctl stop`).

# 2. Back up your trade database (cheap insurance).
cp /path/to/tradesv3.sqlite /path/to/tradesv3.sqlite.bak

# 3. Clone Freqtrade Ultimate into a NEW folder — your old install stays as rollback.
git clone https://github.com/titouannwtt/freqtrade-ultimate.git
cd freqtrade-ultimate

# 4. Install it (creates its own .venv; your old environment is untouched).
./setup.sh -i

# 5. Activate and confirm you're on the fork.
source .venv/bin/activate
freqtrade --version

# 6. Run YOUR bot with YOUR existing files — nothing copied, nothing lost.
freqtrade trade \
  --config   /path/to/your/config.json \
  --strategy YourStrategy \
  --userdir  /path/to/your/user_data \
  --db-url   sqlite:////absolute/path/to/your/tradesv3.sqlite
```

> **Prefer one tidy folder?** Instead of pointing at old paths, copy your data into the clone:
> `cp -a /path/to/your/user_data/. ./user_data/` then place your `config.json` and `*.sqlite` where you normally keep them, and launch as usual.

### 🐳 Docker

There is **no public Ultimate image — you build it yourself from source** (the repo's `.dockerignore` excludes the bundled data, so the build stays small and fast). Recommended path: keep your working `docker-compose.yml`, volumes and DB exactly as they are, and **swap only the image**.

```bash
# 1. Stop your current stack.
docker compose down

# 2. Build the Freqtrade Ultimate image from source.
git clone https://github.com/titouannwtt/freqtrade-ultimate.git
cd freqtrade-ultimate
docker build -t freqtrade-ultimate:latest .
cd -

# 3. In your EXISTING docker-compose.yml, change the image line:
#       image: freqtradeorg/freqtrade:stable
#    to:
#       image: freqtrade-ultimate:latest

# 4. Bring the bot back up — same mounted user_data, same DB, same trades.
docker compose up -d
```

> Your `user_data/` volume and database are never rewritten by this — only the code inside the container changes. **Rollback:** set the image line back to `freqtradeorg/freqtrade:stable` and `docker compose up -d`.
> **Update the fork later:** `git pull` in the clone → `docker build -t freqtrade-ultimate:latest .` → `docker compose up -d`.

<details>
<summary><b>Windows users</b></summary>

`setup.sh` is for macOS/Linux. On Windows, use the **Docker** path above, or install inside **WSL2** and follow the native path there.

</details>

<details>
<summary><b>Staying in sync with upstream</b></summary>

This fork keeps the official project as its `upstream` remote and merges upstream releases in, so you keep getting upstream fixes on top of the Ultimate features. To pull the latest fork code: `git pull` (native) or `git pull` + rebuild (Docker), then restart your bot.

</details>

## 🌱 New to freqtrade? Start here

You don't need to install upstream freqtrade first — Freqtrade Ultimate is a complete, ready-to-run installation on its own.

> 🇫🇷 **Francophone ?** La communauté **Freqtrade France** propose un guide écrit pas-à-pas (installation, configuration, premier bot) : **[Freqtrade — le guide complet du bot de trading open source](https://buymeacoffee.com/freqtrade_france/freqtrade-le-guide-complet-du-bot-de-trading-open-source-installation-et-config)**. *(French only / en français uniquement.)*

### 🖥️ Native install (source / venv)

```bash
git clone https://github.com/titouannwtt/freqtrade-ultimate.git
cd freqtrade-ultimate
./setup.sh -i

source .venv/bin/activate
freqtrade create-userdir --userdir user_data
freqtrade new-config --config user_data/config.json
```

### 🐳 Docker

The bundled `docker-compose.yml` builds the Ultimate image for you (tagged `freqtrade-ultimate:latest`):

```bash
git clone https://github.com/titouannwtt/freqtrade-ultimate.git
cd freqtrade-ultimate

# Build the image from source.
docker compose build

# Create your user_data folder and a starter config:
docker compose run --rm freqtrade create-userdir --userdir user_data
docker compose run --rm freqtrade new-config --config user_data/config.json

# Edit user_data/config.json (exchange, stake, pairs), then launch:
docker compose up -d
```

**Next steps:** edit `user_data/config.json` (exchange, stake, pairs), drop a strategy into `user_data/strategies/`, then launch with `freqtrade trade` (native) or `docker compose up -d`. The full [Freqtrade documentation](https://www.freqtrade.io/) applies unchanged; see the [Learn](#-learn-algorithmic-trading) section below and [docs/FEATURES.md](docs/FEATURES.md) for what this fork adds.

## 🎯 What is Freqtrade Ultimate?

A maintained, opinionated fork of [Freqtrade](https://github.com/freqtrade/freqtrade) optimized for serious algorithmic trading on **Hyperliquid** perpetual futures, with **35+ features** not present upstream.

**Why this fork exists.** Running multiple Freqtrade bots in production on Hyperliquid surfaces real-world problems upstream wasn't designed for — rate-limit cascades when four bots refresh OHLCV simultaneously, ADL and liquidation handling on a DEX without traditional liquidation events, multi-bot pairlist deduplication, and statistically valid hyperopt without curve-fitting. This fork solves those.

**Editorial principle: no curve-fitted strategies.** Every showcase strategy in this repo ships with its walk-forward analysis and real drawdowns. We do not promote backtest-pretty strategies that fail live — many popular Freqtrade strategies embed subtle lookahead biases that make backtests look magical and live results disappointing. We document why, and we publish the methodology that avoids it.

## ⚡ Feature highlights

A quick tour. Full inventory with implementation details lives in [**docs/FEATURES.md**](docs/FEATURES.md).

### Multi-bot infrastructure
- **OHLCV Cache Daemon (`ftcache`)** — Shared candle cache across N bots, **75 % API call reduction** measured in production with four bots.
- **Pairlist Cache Daemon (`ftpairlists`)** — Deduplicates pairlist filter computation between bots (pairlist refresh: 15 min → 3 min).
- **Position Guard + Leverage Sync** — Prevents conflicting entries and unintended hedges on shared wallets.
- **Fleet State Notifications + Auto-Restart** — Thundering-herd prevention with startup jitter, `launch_bot.sh` auto-restart loop.

### Hyperopt & validation
- **PlateauSampler** — Coordinate-wise Optuna sampler for robust hyperparameter optimization (four-phase: baseline → scan → assembly → refinement).
- **`--sampler` CLI flag** — Switch between TPE, NSGA-II/III, CMA-ES, GP, QMC samplers without editing your strategy code.
- **Walk-Forward Analysis** (`freqtrade walk-forward`) — Rolling, anchored, and **CPCV (Combinatorial Purged Cross-Validation)** modes, plus Monte Carlo drawdown simulation, **PBO (Probability of Backtest Overfitting)** score, verdict A–F, and an interactive HTML report.
- **Custom hyperopt losses** — `MoutonMeanRev` (mean-reversion / DCA), `MoutonMomentum` (trend / momentum), `MyProfitDrawdown` (simple baseline).

### Hyperliquid-specific
- **Liquidation detection** via user-fills monitoring (`liquidationMarkPx`).
- **External close detection** (ADL or manual UI close) with `exit_reason="external_close"`.
- **Resilient leverage / margin error handling** in DCA continues operation instead of crashing.
- **Local Hyperliquid historical data bundle** — 3 250+ Feather files (300+ pairs × 8 timeframes including crypto indices and TradFi perpetuals).

### Pairlists & risk
- **`TrendRegularityFilter`** — Excludes pairs with a regular linear uptrend (essential for short strategies).
- **`backtest_lock_wallet`** — Disables compounding in backtests for honest equity curves.
- **Capital withdrawal accounting** — Tracks net profit after capital removal in REST API, Telegram and `/profit`.

### Observability
- **`ExchangeMetrics`** — Ring-buffered API-call metrics, 429 tracking, live token-bucket state.
- **REST API enriched** — `/cache_status`, `/rate_metrics`, `/fleet/status`, `/fleet/events`, `/volume_history`, `/signal_summary`, `/stratdev/*` (consumed by [frequi-ultimate](https://github.com/titouannwtt/frequi-ultimate)).
- **Enhanced Telegram** — `LIQUIDATION` and `external_close` exit reasons; withdrawal-aware `/profit`.

### Dry-run replay
- **Replay engine** — Drives `FreqtradeBot.process()` (the real live code path) candle-by-candle over historical data via a virtual clock and fake exchange overlay. **Validate a strategy in hours instead of months.**
- **1-minute resolution, real funding** — Stoploss/ROI/signals checked every virtual minute; funding computed from local Feather files. Equivalent to permanent `--timeframe-detail 1m` on the real engine.
- **Config auto-launch** — Add a `dry_run_replay` block to your bot config and the bot auto-seeds a 5-month replay on first start, then transitions to normal dry-run. Zero manual action.
- **Coordinator daemon** — Caps concurrent replays to `nproc - 2 - hyperopt_cores` with SIGSTOP/SIGCONT priority queue. Auto-spawned.

### Developer experience
- **Strategy Dev Backend** — Reader, jobs runner, strategy editor APIs (consumed by [frequi-ultimate](https://github.com/titouannwtt/frequi-ultimate)).
- **AI Copilot** — Repository ships with [`CLAUDE.md`](CLAUDE.md) plus 14 tip files containing 199 trading rules curated from Carver, Clenow, Chan, López de Prado, and the Freqtrade France community.
- **Enhanced CLI help** — Practical guidance, recommendations, and tradeoffs documented in every option.

➡️ **Full feature list with deep technical details:** [docs/FEATURES.md](docs/FEATURES.md)

## 📊 Showcase strategies

This repository ships with public showcase strategies directly inside [`user_data/strategies/`](user_data/strategies/). Each strategy follows a strict naming convention:

| File | Purpose |
|---|---|
| `<strategy>.py` | Strategy code (production-grade) |
| `<strategy>_readme.md` | Philosophy, indicators, entry/exit logic, recommended config snippet |
| `<strategy>_analysis.md` | Backtest results, walk-forward verdict, drawdown analysis, PBO score |
| `<strategy>.json` | Optimized hyperopt parameters |

These strategies are intentionally simple and **honest about their limits**. They demonstrate the methodology (anti-overfitting, walk-forward, real drawdowns), not maximum profitability. **More advanced and live-tested strategies are reserved for [Freqtrade France](https://buymeacoffee.com/freqtrade_france) members** along with full live PnL screenshots and reproducible parameters.

## 🎛️ Companion dashboard (FreqUI)

Installation is covered above — [migrate from upstream](#-already-running-freqtrade-switch-to-ultimate-without-losing-anything) or [start fresh](#-new-to-freqtrade-start-here). This fork is a drop-in replacement: all upstream commands work unchanged, and the full [Freqtrade documentation](https://www.freqtrade.io/) applies. Fork-specific commands and flags are documented in [docs/FEATURES.md](docs/FEATURES.md).

For the multi-bot UI optimized for this fork (50+ enhanced components, fleet comparison, market context):

```bash
freqtrade install-ui --ui-version github://titouannwtt/frequi-ultimate
```

Or visit [titouannwtt/frequi-ultimate](https://github.com/titouannwtt/frequi-ultimate).

## 🎓 Learn algorithmic trading

**[Freqtrade France](https://buymeacoffee.com/freqtrade_france)** is the French-speaking community where Mouton (this fork's maintainer) publishes:

- 📚 **Free tutorials** — Freqtrade basics, Hyperliquid setup, hyperopt, backtesting, walk-forward analysis (80 % of the content is free).
- 💎 **Member tutorials** (9 € / month or 90 € / year) — PlateauSampler internals, custom hyperopt loss design, walk-forward CPCV deep dives, anti-overfitting playbook.
- 🤖 **Ready-to-deploy strategies for members** — Live-tested, with reproducible backtests and live PnL.
- 🎥 **Long-form YouTube** — [@freqtrade_france](https://www.youtube.com/@freqtrade_france).
- 🐦 **Twitter** — [@MoutonCrypto](https://x.com/MoutonCrypto).

If you don't want to subscribe but want to support the fork, the simplest free way is to use the [Hyperliquid referral link](https://app.hyperliquid.xyz/join/MOUTON) when creating your account.

## 🤝 Contributing

PRs are welcome on infrastructure features (caching, hyperopt samplers, walk-forward, observability). See [CONTRIBUTING.md](CONTRIBUTING.md). Strategy-specific contributions and discussions happen in the [Freqtrade France](https://buymeacoffee.com/freqtrade_france) community.

## ⚠️ Disclaimer

This is **educational software**. Past performance does not guarantee future results. You are responsible for your own trading and any losses incurred. This project does not provide investment advice. Trading crypto futures is high risk and can lead to total loss of capital.

## 📄 License

GPL-3.0 — same as upstream Freqtrade.

---

<p align="center">
  Built and maintained by <b>Mouton 🐑</b> · <a href="https://buymeacoffee.com/freqtrade_france"><b>Freqtrade France</b></a>
</p>
