# KAC Index v1

> The original "TOTAL3 indexation" strategy. Evaluates each asset's price **relative to the entire altcoin market** rather than in absolute USDT terms.

## Philosophy

Traditional strategies analyze prices in USDT or USDC, which can be misleading when the entire market moves together. If everything pumps 10 %, your strategy may falsely register strength on every asset. KAC Index v1 fixes this by **dividing each price by the TOTAL3 market cap** (smoothed over 24–28 h) before computing any indicator.

The signal becomes "this asset is outperforming altcoins as a whole", not "this asset went up".

## Setup at a glance

| Parameter | Value |
|---|---|
| Timeframe | 1h |
| Reference index | TOTAL3 (smoothed 24-28h) |
| Entry | Indexed price reaches oversold zone (CCI + ATR confirmation) |
| Position style | Conservative — longer holds, fewer trades |
| Avg trade duration | ~8 days |
| Stoploss | -90 % (designed for long holds; tightened in v2) |

## Indicators used

- **TOTAL3 market cap data** (fetched via TradingView; smoothed)
- **ATR** on indexed prices (volatility relative to altcoin universe)
- **CCI** on indexed prices (overbought / oversold relative to altcoin universe)

## Dependencies

This strategy fetches TOTAL3 data via [`tvdatafeed`](https://github.com/rongardF/tvdatafeed):

```bash
pip install requests websockets
git clone https://github.com/rongardF/tvdatafeed
cd tvdatafeed
pip install .
```

## Quick start

```bash
# Download data
freqtrade download-data --config config.json --timerange 20210101- --timeframe 1h

# Backtest
freqtrade backtesting \
  --strategy kac_index_v1 \
  --config backtest_configs/futures_binance_fix.json \
  --timerange 20210101- \
  --timeframe 1h \
  --max-open-trades -1
```

## Limits & honest assessment

- **TradingView dependency** — `tvdatafeed` can break if TradingView changes their websocket protocol. Have a fallback plan.
- **-90 % stoploss** is intentional for the long-hold style but means a bad regime can wipe a meaningful slice of capital before any exit triggers. Read the analysis carefully before deploying.
- **v2 supersedes v1** for most use cases — simpler logic, tighter risk. v1 is shipped here as a teaching reference for the original discovery.

See [`kac_index_v1_analysis.md`](kac_index_v1_analysis.md) for the published 2021-2025 backtest and the walk-forward verdict.

## Want more strategies?

The live-tested, hyperopt-tuned strategies are reserved for [Freqtrade France](https://buymeacoffee.com/freqtrade_france) members (9 €/month or 90 €/year). Detailed walkthrough on the post: [Stratégie KAC Index — Utilisation d'indexation sur le TOTAL3](https://buymeacoffee.com/freqtrade_france/stratgie-kac-index-utilisation-d-indexation-sur-le-total3-keltner-atr-et-cci).

— Mouton 🐑 \| Freqtrade France
