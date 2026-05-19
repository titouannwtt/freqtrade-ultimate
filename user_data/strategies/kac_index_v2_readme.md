# KAC Index v2

> The optimized successor to [KAC Index v1](kac_index_v1_readme.md). Same TOTAL3-indexation philosophy, but simplified entry logic, more trades, tighter risk.

## What changed vs v1

| Aspect | v1 | v2 |
|---|---|---|
| Entry logic | ATR + CCI multi-filter | **ATR > 2.75 only** |
| Stoploss | -90 % | **-40 %** |
| Equity curve | Stepwise | **Smoother (higher Sharpe)** |
| Trade frequency | Lower | **Higher** |

The core insight is the same — divide each price by TOTAL3 market cap, compute indicators on the indexed values. The change is operational: fewer parameters means less overfitting surface area, tighter stop means less tail exposure, simpler signal means more frequent trade opportunities.

## Setup at a glance

| Parameter | Value |
|---|---|
| Timeframe | 1h |
| Reference index | TOTAL3 (smoothed 24-28h) |
| Entry | Indexed ATR > 2.75 |
| Stoploss | -40 % |
| Trade frequency | Higher than v1 |

## Indicators used

- **TOTAL3 market cap data** (fetched via TradingView; smoothed)
- **ATR** on indexed prices (the single entry filter)

## Dependencies

Same as v1 — requires `tvdatafeed`:

```bash
pip install requests websockets
git clone https://github.com/rongardF/tvdatafeed
cd tvdatafeed
pip install .
```

## Quick start

```bash
freqtrade backtesting \
  --strategy kac_index_v2 \
  --config backtest_configs/futures_binance_fix.json \
  --timerange 20210101- \
  --timeframe 1h \
  --max-open-trades -1
```

Hyperopt-optimized parameters live in `kac_index_v2.json` — they will silently override any defaults baked into the strategy code.

## Limits & honest assessment

- **TradingView dependency** remains a single point of failure. Plan a fallback if `tvdatafeed` ever breaks.
- **-40 % stoploss** is more reasonable than v1 but still wide. Sized position is critical.
- **Single-filter entry** is more robust to overfitting than v1's multi-filter, but also less selective — expect more whipsaw in low-volatility regimes.

See [`kac_index_v2_analysis.md`](kac_index_v2_analysis.md) for backtest, walk-forward, and PBO score.

## Want more strategies?

Detailed walkthrough on the post: [KAC Index v2 — Stratégie ATR + CCI indexée TOTAL3 optimisée](https://buymeacoffee.com/freqtrade_france/kac-index-v2-stratgie-atr-cci-indexe-total3-optimise). Live-tested strategies with PnL reports are reserved for [Freqtrade France](https://buymeacoffee.com/freqtrade_france) members.

— Mouton 🐑 \| Freqtrade France
