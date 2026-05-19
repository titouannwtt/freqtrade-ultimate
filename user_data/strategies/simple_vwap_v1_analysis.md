# Simple VWAP v1 — Analysis

> Backtest, walk-forward verdict, drawdown profile, and overfitting score.
> This is a **living document** — update it whenever you rerun the analysis on fresh data.

## Methodology

We hold to the [anti-overfitting bar](../../docs/FEATURES.md) used throughout this fork: no strategy is published without a walk-forward run and a PBO score. The numbers below come from the commands documented in the [`simple_vwap_v1_readme.md`](simple_vwap_v1_readme.md) plus the walk-forward command shown here.

## Backtest

**Command**:
```bash
freqtrade backtesting \
  --strategy simple_vwap_v1 \
  --config backtest_configs/futures_binance.json \
  --timerange 20210101-20251231 \
  --timeframe 4h \
  --max-open-trades 40 \
  --stake-amount unlimited \
  --dry-run-wallet 1000
```

**Period**: 2021-01-01 → 2025-12-31 (5 years)

| Metric | Value |
|---|---|
| Total return | _TBD — fill from latest backtest_ |
| CAGR | _TBD_ |
| Sharpe ratio | _TBD_ |
| Sortino ratio | _TBD_ |
| Win rate | _TBD_ |
| Total trades | _TBD_ |
| Avg trade duration | _TBD_ |
| Max drawdown | _TBD_ |

## Walk-forward analysis (CPCV)

**Command**:
```bash
freqtrade walk-forward \
  --strategy simple_vwap_v1 \
  --config backtest_configs/futures_binance.json \
  --timerange 20210101-20251231 \
  --wf-mode cpcv \
  --wf-train-period 365 \
  --wf-test-period 90
```

| Metric | Value |
|---|---|
| Walk-forward efficiency (WFE) | _TBD_ |
| Probability of Backtest Overfitting (PBO) | _TBD_ |
| Verdict | _TBD (A–F)_ |

A PBO below 0.5 is required before any strategy can be considered for live use.

## Monte Carlo drawdown simulation

Trade-shuffle Monte Carlo on the backtest's trade sequence:

| Quantile | Drawdown |
|---|---|
| Median | _TBD_ |
| 95th percentile | _TBD_ |
| 99th percentile | _TBD_ |

## Honest assessment

**Strengths**:
- _TBD — list what the data actually shows worked._

**Weaknesses**:
- _TBD — be explicit about regime sensitivity, drawdown periods, etc._

**Failure modes observed**:
- _TBD — flash crashes, low-volatility regimes, etc._

## Live PnL

Live PnL screenshots and reproducible parameters are shared with [Freqtrade France](https://buymeacoffee.com/freqtrade_france) members — backtest ≠ live, and we believe in showing the gap honestly.

— Mouton 🐑 \| Freqtrade France
