# KAC Index v1 — Analysis

> Original 2021–2025 backtest, walk-forward verdict, drawdown profile.
> This is a **living document** — update it whenever you rerun the analysis on fresh data.

## Published headline metrics (2021–2025)

| Metric | Value |
|---|---|
| Total return | +972 % |
| Win rate | 48.2 % |
| Max drawdown | 31.35 % |
| Avg trade duration | ~8 days |

These figures come from the original publication on Freqtrade France. **Backtest ≠ live** — see "Honest assessment" below.

## Backtest reproduction

**Command**:
```bash
freqtrade backtesting \
  --strategy kac_index_v1 \
  --config backtest_configs/futures_binance_fix.json \
  --timerange 20210101-20251231 \
  --timeframe 1h \
  --max-open-trades -1
```

## Walk-forward analysis (CPCV)

**Command**:
```bash
freqtrade walk-forward \
  --strategy kac_index_v1 \
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

## Honest assessment

**Strengths**:
- The TOTAL3-indexation insight is real and non-obvious. Most strategies miss this normalization.
- 5-year backtest covers multiple regimes (2021 bull, 2022 bear, 2023 chop, 2024 bull, 2025 mixed).

**Weaknesses**:
- The -90 % stoploss masks tail risk. If you actually deploy v1 with this stoploss and the regime turns, you wear it.
- TradingView dependency is a single point of failure.
- Position concentration on outperformers means a few bad picks dominate PnL.

**Why v2 exists**:
- v1 has overfit risk on the entry filter complexity. v2 simplified to `ATR > 2.75` only — fewer parameters, simpler logic, tighter stop (-40 %).

## Live PnL

Live PnL screenshots and reproducible parameters are shared with [Freqtrade France](https://buymeacoffee.com/freqtrade_france) members.

— Mouton 🐑 \| Freqtrade France
