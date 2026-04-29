# Custom Hyperopt Losses and Samplers

This fork extends freqtrade's hyperopt with three custom loss functions, a `--sampler` flag
for choosing the Optuna optimization algorithm, and an auto-generated HTML report after each
hyperopt run.

## Overview

| Addition | Purpose |
|---|---|
| `MoutonMeanRevHyperOptLoss` | 8-metric composite loss for DCA / mean-reversion |
| `MoutonMomentumHyperOptLoss` | 9-metric composite loss for momentum / trend-following |
| `MyProfitDrawDownHyperOptLoss` | Simple profit-minus-drawdown baseline |
| `--sampler NAME` | Override Optuna sampler (default: `NSGAIIISampler`) |
| HTML report | Auto-generated report at `user_data/hyperopt_results/<strategy>.html` |

All three custom losses use **hard filters** that reject configurations below minimum quality
thresholds, and **multiplicative gates** that apply exponential drawdown penalties on top of
the weighted metric score.

---

## Loss Functions

### MoutonMeanRevHyperOptLoss

**Best for:** DCA strategies with safety orders, mean-reversion on Hyperliquid USDC perps.

**File:** `freqtrade/optimize/hyperopt_loss/hyperopt_loss_mouton_meanrev.py`

#### Metric weights

| Metric | Weight | Description |
|---|---|---|
| Annualized return (CAGR) | 0.25 | Primary objective — profit per unit time |
| K-ratio (equity curve slope) | 0.18 | Slope / SE(slope) — rewards smooth, steady growth |
| Quarterly consistency | 0.14 | Fraction of profitable quarters + magnitude regularity |
| Profit factor | 0.13 | Gross wins / gross losses |
| Payoff ratio | 0.08 | Avg win / avg loss — asymmetry check |
| Pair diversity | 0.08 | Daily-bucketed, correlation-adjusted spread across pairs |
| TUW health | 0.08 | Time-underwater score — penalizes prolonged drawdowns |
| Confidence | 0.06 | sqrt-based sample size factor |

Weights sum to 1.0. The score is then multiplied by two gates:

- **Concentration gate:** smooth sigmoid on the profit share of the top 2 trades.
  If one or two trades dominate total profit, the score is heavily penalized.
- **Drawdown gate:** `score / exp(5 * max_dd)`. At 30% drawdown this halves the score;
  at 50% it reduces it to ~8% of face value.

#### Hard filters

| Filter | Threshold | Gradient on rejection |
|---|---|---|
| Total profit | > 0 | Proportional to loss magnitude |
| Trade count | >= 40 | Proportional to shortfall |
| Win rate | >= 50% | Proportional to shortfall |
| Max drawdown | <= 50% | Proportional to excess |
| Pair count | >= 5 | Proportional to shortfall |
| Training period | >= 30 days | Binary |

Hard-filter rejections return a large loss value (`1e6`) with a gradient signal so
TPE and NSGA-III can navigate away from rejected regions rather than treating them as
a flat wall.

#### When to use

- DCA strategies with multiple safety orders (HippoDCA family and similar).
- Any mean-reversion strategy where inactivity is intentional, not a flaw.
- When you want the optimizer to reward consistent, diversified profits rather than
  a handful of large wins.

#### When NOT to use

- Momentum or trend-following strategies. The high weight on K-ratio and pair diversity
  will mischaracterize strategies that legitimately concentrate on trending pairs.
- Strategies with fewer than 40 trades in the training period — the hard filter will
  reject every epoch.
- Quick baseline exploration. Use `CalmarHyperOptLoss` or `MyProfitDrawDownHyperOptLoss`
  for initial landscape mapping.

---

### MoutonMomentumHyperOptLoss

**Best for:** Momentum and trend-following strategies, long or short.

**File:** `freqtrade/optimize/hyperopt_loss/hyperopt_loss_mouton_momentum.py`

#### Metric weights

| Metric | Weight | Description |
|---|---|---|
| Annualized return (CAGR) | 0.22 | Primary objective |
| Payoff ratio | 0.16 | Avg win / avg loss — "let profits run" |
| Sharpe ratio | 0.14 | Risk-adjusted return, normalized on 2.0 for crypto |
| Tail ratio | 0.12 | Right-tail size vs left-tail size — rewards positive skew |
| Profit factor | 0.10 | Gross wins / gross losses |
| Quarterly consistency | 0.09 | Profitable quarters + magnitude regularity |
| Pair diversity | 0.06 | Daily-bucketed correlation-adjusted spread |
| TUW health | 0.06 | Time-underwater score |
| Confidence | 0.05 | sqrt-based sample size factor |

Gates:

- **Consecutive loss gate:** smooth sigmoid centered at 12 consecutive losses. Prevents
  the optimizer from selecting strategies with catastrophic losing streaks.
- **Drawdown gate:** `score / exp(5 * max_dd)` — same formula as MeanRev.

#### Hard filters

| Filter | Threshold | Gradient on rejection |
|---|---|---|
| Total profit | > 0 | Proportional to loss magnitude |
| Trade count | >= 30 | Proportional to shortfall |
| Max drawdown | <= 45% | Proportional to excess |
| Payoff ratio | >= 0.3 | Binary (prevents degenerate configs) |
| Training period | >= 30 days | Binary |

#### When to use

- Momentum or breakout strategies where winners should be larger than losers.
- When Sharpe is a meaningful signal (regular, frequent trades).
- When tail skew matters — strategies designed to capture large asymmetric moves.

#### When NOT to use

- DCA strategies where payoff ratio is typically below 1 by design.
- Strategies with fewer than 30 trades in the training period.
- Situations where you need a fast baseline. Use `SharpeHyperOptLossDaily` + TPE instead.

---

### MyProfitDrawDownHyperOptLoss

**Best for:** General-purpose baseline. Quick first pass on any strategy type.

**File:** `freqtrade/optimize/hyperopt_loss/hyperopt_loss_my_profit_drawdown.py`

#### How it works

Simple formula: `loss = -(profit - drawdown * profit * DRAWDOWN_MULT)`

The drawdown multiplier penalizes high-drawdown configurations without the complexity of
multi-metric weighting. There are no gates or per-metric weights.

#### Hard filters

| Filter | Threshold |
|---|---|
| Relative drawdown | <= 45% |
| Account drawdown | <= 45% |

#### When to use

- Initial exploration before committing to a composite loss.
- Strategies where you want human-interpretable results without black-box weighting.
- When training data is short and the more opinionated losses would reject too many epochs.

#### When NOT to use

- Final hyperopt runs for live deployment — the composite losses (`MoutonMeanRev`,
  `CalmarHyperOptLoss`) produce more robust parameter selections.
- When drawdown control is critical but profit is low — the formula ties penalty magnitude
  to profit, so low-profit epochs with high drawdown may slip through.

---

## Built-in Losses Quick Reference

These are the upstream freqtrade losses, unchanged in this fork.

| Loss function | One-liner |
|---|---|
| `SharpeHyperOptLoss` | Maximize annualized Sharpe ratio |
| `SharpeHyperOptLossDaily` | Sharpe on daily-bucketed returns — more stable with many trades |
| `SortinoHyperOptLoss` | Like Sharpe but only penalizes downside volatility |
| `SortinoHyperOptLossDaily` | Daily Sortino — downside-only, aggregated per day |
| `CalmarHyperOptLoss` | Maximize annual return / max drawdown — recommended for patient DCA |
| `MaxDrawDownHyperOptLoss` | Minimize absolute max drawdown |
| `MaxDrawDownRelativeHyperOptLoss` | Minimize drawdown as a fraction of peak equity |
| `MaxDrawDownPerPairHyperOptLoss` | Control per-pair drawdown extremes |
| `ProfitDrawDownHyperOptLoss` | Upstream profit vs drawdown balance (fixed weighting) |
| `ShortTradeDurHyperOptLoss` | Maximize profit, minimize trade holding time |
| `OnlyProfitHyperOptLoss` | Pure profit maximization — no risk adjustment |
| `MultiMetricHyperOptLoss` | Configurable weighted combination of multiple metrics |

!!! note "CalmarHyperOptLoss for DCA"
    When the composite losses are overkill, `CalmarHyperOptLoss` is the best single-metric
    alternative for DCA mean-reversion strategies. It penalizes deep drawdowns without
    over-penalizing flat periods the way Sharpe does.

---

## Sampler Comparison

Passed via `--sampler NAME` on any `hyperopt` or `walk-forward` command.
When omitted, the fork defaults to `NSGAIIISampler`.

| Sampler | Type | Best for | Convergence speed | Exploration | Recommended epochs |
|---|---|---|---|---|---|
| `NSGAIIISampler` | Genetic, multi-objective | All composite losses, default | Moderate | High | 500-2000 |
| `NSGAIISampler` | Genetic, multi-objective | Fallback if NSGA-III unsatisfying | Moderate | Moderate | 500-2000 |
| `TPESampler` | Bayesian (Parzen estimator) | Simple single-metric losses | Fast | Moderate | 300-500 |
| `CmaEsSampler` | Evolution strategy | Continuous parameter spaces | Moderate | Moderate | 500-1000 |
| `GPSampler` | Gaussian process surrogate | Few parameters (<8), each epoch expensive | Slow | High | 100-200 |
| `QMCSampler` | Quasi-Monte Carlo | Initial landscape mapping | N/A (no learning) | Very high | 200-500 |

#### Practical guidance

- **NSGAIIISampler** is the default and works well with all loss functions. Its genetic
  diversity maintenance is especially valuable for composite losses with implicit
  trade-offs between metrics.
- **TPESampler** converges faster on single-objective losses (Sharpe, Calmar,
  MyProfitDrawDown). If you are running 300-500 epochs with a simple loss, TPE will
  usually find a better result than NSGA-III in the same budget.
- **CmaEsSampler** is worth trying when most of your parameters are continuous
  (`DecimalParameter`, `FloatRange`) and they interact with each other. It needs
  ~50 warm-up epochs before it outperforms TPE.
- **GPSampler** is only practical for strategies with fewer than 8 parameters and a
  small epoch budget (under 200). It fits a surrogate model per trial, which is slow
  at scale.
- **QMCSampler** does not learn from previous results. Use it for an initial mapping
  pass — then rerun with TPE or NSGA-III initialized from the best QMC points.

---

## Decision Tree

Use this to choose a loss + sampler combination before launching hyperopt.

```
What type of strategy?
|
+-- DCA / mean-reversion (safety orders, high win rate expected)
|   |
|   +-- Full validation run
|   |   Loss: MoutonMeanRevHyperOptLoss
|   |   Sampler: NSGAIIISampler
|   |   Epochs: 1000+
|   |
|   +-- Quick baseline / short training data
|       Loss: CalmarHyperOptLoss
|       Sampler: TPESampler
|       Epochs: 300-500
|
+-- Momentum / trend-following (cut losses early, let profits run)
|   |
|   +-- Full validation run
|   |   Loss: MoutonMomentumHyperOptLoss
|   |   Sampler: NSGAIIISampler
|   |   Epochs: 1000+
|   |
|   +-- Quick baseline
|       Loss: SharpeHyperOptLossDaily
|       Sampler: TPESampler
|       Epochs: 300-500
|
+-- Unknown / exploring a new strategy
|   Loss: MyProfitDrawDownHyperOptLoss or CalmarHyperOptLoss
|   Sampler: TPESampler or QMCSampler (map first, then refine)
|   Epochs: 300-500
|
+-- Multi-objective (explicit trade-off between 3+ metrics)
    Loss: MultiMetricHyperOptLoss
    Sampler: NSGAIIISampler
    Epochs: 1000+
```

---

## Integration with Walk-Forward Analysis

Hyperopt finds the parameter set that minimizes the loss on training data. Walk-forward
analysis tests whether those parameters generalize to unseen data by running hyperopt on
rolling windows and evaluating each window's parameters on the next period.

After a hyperopt run, run walk-forward to validate:

```bash
# Rolling walk-forward: 6 windows, 75% train / 25% test, 2-day embargo
freqtrade walk-forward \
  --strategy MyStrategy \
  --hyperopt-loss MoutonMeanRevHyperOptLoss \
  --sampler NSGAIIISampler \
  --wf-windows 6 \
  --wf-train-ratio 0.75 \
  --wf-embargo-days 2 \
  --wf-min-test-trades 30 \
  --epochs 500 \
  --timerange 20240101-20250101 \
  -i 15m \
  -c live_configs/_default_spot_usdc.json

# CPCV mode: tests all possible train/test splits
freqtrade walk-forward \
  --strategy MyStrategy \
  --hyperopt-loss CalmarHyperOptLoss \
  --wf-mode cpcv \
  --wf-cpcv-groups 6 \
  --wf-cpcv-test-groups 2 \
  --wf-embargo-days 2 \
  --epochs 300 \
  --timerange 20240101-20250101 \
  -i 15m \
  -c live_configs/_default_spot_usdc.json
```

Walk-forward produces its own HTML report and a structured JSON output with metrics such
as Walk-Forward Efficiency (WFE), System Quality Number (SQN), Monte Carlo drawdown
distributions, and a Carver discount factor. See `freqtrade/optimize/wfa_glossary.py` for
all metric definitions and thresholds.

!!! warning "Hyperopt does not replace walk-forward"
    A strong hyperopt result (high Calmar, low drawdown) on training data is necessary but
    not sufficient. Always follow hyperopt with walk-forward or at minimum a live dry-run
    before committing real capital.

---

## HTML Report

Every hyperopt run automatically generates an HTML report alongside the results JSON file:

```
user_data/hyperopt_results/<strategy>_<timestamp>.html
```

The report is self-contained (no external dependencies) and includes:

### Sections

| Section | Contents |
|---|---|
| Best Epoch — Summary | Trades, win rate, total profit, Calmar, SQN, Sharpe, Sortino, profit factor, max drawdown, expectancy, average holding time |
| Best Parameters | All optimized parameters grouped by space (buy, sell, roi, stoploss, etc.) |
| Top 10 Epochs | Table of the 10 best epochs by loss value — check for consistency across ranks |
| Convergence Chart | SVG chart showing every epoch's loss (scatter) and best-so-far (line). A flat best-so-far line after a certain point indicates the optimizer has converged |
| Parameter Agreement | For each parameter in the top 10, shows spread and badges it as **stable** (std/range < 0.15), **marginal**, or **unstable** (std/range > 0.30). Stable parameters are more trustworthy |
| Loss Explanation | Description of the loss function used and what it optimizes |
| Sampler Explanation | Description of the sampler used and its trade-offs |
| Next Steps | Guidance based on the result (profitable, high drawdown, too few trades, etc.) |
| Glossary | Definitions of all metrics shown in the report with color-coded quality thresholds |

### Reading parameter stability

The Parameter Agreement section is the most actionable diagnostic. If multiple parameters
are flagged **unstable**, the optimizer is finding different parameter values that all score
similarly — the loss surface is flat or noisy in those dimensions. In that case:

1. Consider freezing unstable parameters at sensible defaults.
2. Run more epochs to give the sampler more signal.
3. Switch to `CmaEsSampler` or `GPSampler` for better exploration of correlated parameters.
4. Consider whether the parameter is genuinely important for the strategy.

Hovering over underlined metric names in the report displays their full definitions.

---

## Examples

### DCA mean-reversion, full run

```bash
freqtrade hyperopt \
  --strategy HippoDCA_hyp_dynv1_short_sharpe \
  --hyperopt-loss MoutonMeanRevHyperOptLoss \
  --sampler NSGAIIISampler \
  --epochs 1000 \
  --spaces buy sell \
  --timerange 20240701-20250101 \
  -i 15m \
  -j 4 \
  -c live_configs/_default_spot_usdc.json
# HTML report written to user_data/hyperopt_results/HippoDCA_hyp_dynv1_short_sharpe_*.html
```

### Quick Calmar baseline with fast convergence

```bash
freqtrade hyperopt \
  --strategy HippoDCA_hyp_dynv1_short_sharpe \
  --hyperopt-loss CalmarHyperOptLoss \
  --sampler TPESampler \
  --epochs 400 \
  --spaces buy \
  --timerange 20240901-20250101 \
  -i 15m \
  -j 4 \
  -c live_configs/_default_spot_usdc.json
```

### Momentum strategy with payoff focus

```bash
freqtrade hyperopt \
  --strategy MyMomentumStrategy \
  --hyperopt-loss MoutonMomentumHyperOptLoss \
  --sampler NSGAIIISampler \
  --epochs 1000 \
  --spaces buy sell stoploss \
  --timerange 20240101-20250101 \
  -i 15m \
  -j 4 \
  -c live_configs/_default_spot_usdc.json
```

### Initial landscape exploration with QMC then refine with TPE

```bash
# Step 1: map the space evenly
freqtrade hyperopt \
  --strategy MyStrategy \
  --hyperopt-loss MyProfitDrawDownHyperOptLoss \
  --sampler QMCSampler \
  --epochs 300 \
  --spaces buy \
  --timerange 20240901-20250101 \
  -i 15m \
  -c live_configs/_default_spot_usdc.json

# Step 2: refine from the best QMC result
freqtrade hyperopt \
  --strategy MyStrategy \
  --hyperopt-loss CalmarHyperOptLoss \
  --sampler TPESampler \
  --epochs 400 \
  --spaces buy \
  --timerange 20240901-20250101 \
  -i 15m \
  -c live_configs/_default_spot_usdc.json
```

!!! note "Timerange guidance"
    Train on the exchange you intend to trade on (Hyperliquid), using recent data. A
    6-month window ending close to today reduces the risk of optimizing on a market regime
    that no longer applies. Keep at least 30 days in the training period — all custom losses
    hard-filter shorter periods.
