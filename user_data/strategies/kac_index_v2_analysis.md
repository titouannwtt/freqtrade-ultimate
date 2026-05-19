# KAC Index v2 — Analysis

> Backtest, walk-forward verdict, drawdown profile.
> This is a **living document** — update it whenever you rerun the analysis on fresh data.

## Backtest

**Command**:
```bash
freqtrade backtesting \
  --strategy kac_index_v2 \
  --config backtest_configs/futures_binance_fix.json \
  --timerange 20210101-20251231 \
  --timeframe 1h \
  --max-open-trades -1
```

| Metric | Value |
|---|---|
| Total return | _TBD — rerun on latest data_ |
| CAGR | _TBD_ |
| Sharpe ratio | _TBD — should be **higher than v1** per design_ |
| Sortino ratio | _TBD_ |
| Win rate | _TBD_ |
| Total trades | _TBD — should be **higher than v1**_ |
| Avg trade duration | _TBD_ |
| Max drawdown | _TBD — expected around -40 % at worst given stoploss_ |

## Walk-forward analysis (CPCV)

**Command**:
```bash
freqtrade walk-forward \
  --strategy kac_index_v2 \
  --config backtest_configs/futures_binance_fix.json \
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

## Comparison with v1

| Metric | v1 | v2 |
|---|---|---|
| Total return | +972 % (2021-2025) | _TBD_ |
| Win rate | 48.2 % | _TBD_ |
| Max DD | 31.35 % | _TBD_ |
| Trade count | Lower | Higher |
| Sharpe | Lower | Higher (by design) |

## Honest assessment

**Strengths**:
- Simpler signal (single ATR filter) → less overfitting surface area.
- Tighter stop (-40 % vs -90 %) → bounded tail risk.
- Higher Sharpe ratio expected from smoother equity curve.

**Weaknesses**:
- TradingView `tvdatafeed` dependency persists.
- Low-volatility regimes will trigger more whipsaw vs v1.
- Single-filter entry is less selective on quality vs v1's multi-filter.

## Live PnL

Live PnL screenshots and reproducible parameters are shared with [Freqtrade France](https://buymeacoffee.com/freqtrade_france) members.

— Mouton 🐑 \| Freqtrade France
