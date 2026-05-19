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
  <a href="#-what-is-freqtrade-ultimate">About</a> ·
  <a href="#-feature-highlights">Features</a> ·
  <a href="#-showcase-strategies">Strategies</a> ·
  <a href="#-installation">Install</a> ·
  <a href="#-learn-algorithmic-trading">Learn</a> ·
  <a href="docs/FEATURES.md">Full feature list</a>
</p>

---

## 🎯 What is Freqtrade Ultimate?

A maintained, opinionated fork of [Freqtrade](https://github.com/freqtrade/freqtrade) optimized for serious algorithmic trading on **Hyperliquid** perpetual futures, with **32+ features** not present upstream.

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

## 🚀 Installation

```bash
git clone https://github.com/titouannwtt/freqtrade-ultimate.git
cd freqtrade-ultimate
./setup.sh -i
```

Then follow the standard [Freqtrade documentation](https://www.freqtrade.io/) — this fork is a drop-in replacement and all upstream commands work unchanged. Fork-specific commands and flags are documented in [docs/FEATURES.md](docs/FEATURES.md).

### Companion: the dashboard

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
