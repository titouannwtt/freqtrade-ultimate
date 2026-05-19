# Simple VWAP v1

> Contrarian Freqtrade strategy that prioritizes **constant market exposure** over perfect entries. Aims to be in the market 90 % of the time, accumulating small consistent gains.

## Philosophy

Most algo strategies wait for "perfect" entries — fewer trades, larger expected edge per trade. Simple VWAP v1 goes the opposite direction: take many small entries, rely on volatility regression to the VWAP mean, and let position management (DCA + exit signals) handle the rest. The hypothesis is that a continuously-deployed bot captures more of the long-tail "boring" market regime than a sniper bot does.

## Setup at a glance

| Parameter | Value |
|---|---|
| Timeframe | 4h |
| Max open positions | 40 |
| Entry | VWAP lower band touch, 8-candle confirmation |
| Exit | EMA trend reversal OR negative CCI momentum |
| Stoploss | -37 % (rarely hit thanks to frequent exit signals) |
| DCA | Up to 4 safety orders, exponential spacing |
| Position sizing | Dynamic, volatility-adjusted |

## Indicators used

- **VWAP** with rolling bands as the mean-reversion anchor.
- **EMA** for trend-reversal exit detection.
- **CCI** for momentum-decay exit detection.

## Recommended config snippet

```json
{
  "max_open_trades": 40,
  "stake_amount": "unlimited",
  "tradable_balance_ratio": 0.99,
  "timeframe": "4h",
  "strategy": "simple_vwap_v1",
  "stoploss": -0.37
}
```

A full backtest config example lives in `backtest_configs/futures_binance.json` (upstream-compatible).

## Quick start

```bash
freqtrade backtesting \
  --strategy simple_vwap_v1 \
  --config backtest_configs/futures_binance.json \
  --timerange 20210101- \
  --timeframe 4h \
  --max-open-trades 40 \
  --stake-amount unlimited \
  --dry-run-wallet 1000
```

Always run `--dry-run` for at least 2 weeks before going live.

## Limits & honest assessment

- **High max-open-trades** (40) means you need real diversification of pairs to avoid concentration risk.
- **-37 % stoploss** is brutal in a flash-crash regime — pair this strategy with adequate capital and DCA budget.
- **Designed for sideways/choppy markets**; will likely underperform during strong unidirectional trends.

See [`simple_vwap_v1_analysis.md`](simple_vwap_v1_analysis.md) for backtest, walk-forward, and PBO score.

## Want more strategies?

The live-tested, hyperopt-tuned strategies (with full PnL reports) are reserved for [Freqtrade France](https://buymeacoffee.com/freqtrade_france) members (9 €/month or 90 €/year). Detailed walkthrough of this strategy on the post: [Simple VWAP v1 — La stratégie du placement constant](https://buymeacoffee.com/freqtrade_france/simple-vwap-v1-la-stratgie-du-placement-constant).

— Mouton 🐑 \| Freqtrade France
