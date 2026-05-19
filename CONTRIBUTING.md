# Contributing to Freqtrade Ultimate

Thanks for your interest. This document covers what we accept, what we don't, and how to make your PR mergeable on the first review.

## What we accept

- **Bug fixes** on fork-specific code (anything in `freqtrade/ohlcv_cache/`, `freqtrade/pairlist_cache/`, fork-touched files marked in [`docs/FEATURES.md`](docs/FEATURES.md)). For upstream bugs, please open them on [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) — they'll land in this fork on the next upstream merge.
- **Infrastructure features**: caching, hyperopt samplers, walk-forward modes, observability, REST API endpoints.
- **Hyperliquid-specific improvements**: new exchange edge cases, liquidation/ADL handling, funding-fee corrections.
- **Documentation improvements** in `docs/FEATURES.md`, `CLAUDE.md`, or per-strategy `_readme.md` / `_analysis.md` files.
- **New showcase strategies** in `user_data/strategies/` — must follow the naming convention and ship with the four files (`.py`, `_readme.md`, `_analysis.md`, `.json`). Strategy must include a real walk-forward analysis section, not just a backtest equity curve.

## What we don't accept

- Strategy contributions that rely on lookahead bias, future-looking indicators, or curve-fitted parameters. We reject these on principle — see [`docs/FEATURES.md`](docs/FEATURES.md) for the bar.
- Cosmetic refactors of upstream code that increase merge conflicts.
- Adding dependencies we can avoid. Discuss in an issue first.
- Features that are clearly upstream-suitable. Send those to [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade).

## Workflow

1. **Open an issue first** for any non-trivial change. We may already be working on it or have a reason not to merge it.
2. **Fork → branch → PR**. Name your branch descriptively: `feat/<topic>`, `fix/<topic>`, `docs/<topic>`.
3. **One topic per PR**. Don't mix a bug fix and a feature.
4. **Lint before pushing**:
   ```bash
   ruff check freqtrade/
   ruff format freqtrade/
   ```
5. **Test what you touched**:
   ```bash
   pytest tests/<your_area>/ --random-order -n auto
   ```
6. **Update [`docs/FEATURES.md`](docs/FEATURES.md)** if your change adds or modifies a feature.

## Commit messages

- Imperative mood, lowercase prefix (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `ci:`).
- Reference the issue: `feat(ftcache): support feather compression (#42)`.
- No trailer blocks, no AI-generated co-author lines.

## Code style

- Python 3.11+.
- Ruff config in `pyproject.toml`. Line length 100, max complexity 12.
- Type hints on public functions in fork-specific code.
- No `print()` for diagnostics — use `logger`.
- Threading: use the helpers from `freqtrade/exchange/exchange_metrics.py` rather than rolling your own.

## Strategy-specific contributions

For new strategies in `user_data/strategies/`, the analysis must include:
- **Backtest** on at least 2 years of data, with realistic slippage and fees for Hyperliquid.
- **Walk-forward verdict** using `freqtrade walk-forward` with CPCV mode.
- **PBO score** (Probability of Backtest Overfitting). Strategies above PBO 0.5 will not be merged.
- **Drawdown profile** — max DD, time-under-water, recovery time.
- **Honest assessment** of failure modes (no "100% win rate" cherry-picking).

Discussion of advanced strategies (live-tested, ready-to-deploy) happens in the [Freqtrade France](https://buymeacoffee.com/freqtrade_france) community — paid member strategies are not contributed to this public fork.

## Questions

Open a [GitHub Discussion](https://github.com/titouannwtt/freqtrade-ultimate/discussions) or join the [Freqtrade France](https://buymeacoffee.com/freqtrade_france) community.

— Mouton 🐑 \| Freqtrade France
