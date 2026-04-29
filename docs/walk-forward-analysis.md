# Walk-Forward Analysis

Walk-Forward Analysis (WFA) is a rigorous validation method that tests whether the parameters
your hyperopt found actually work on data the optimizer never saw — not once, but across multiple
sequential windows. This page explains how to run it, how to read the report, and what to do
with the results.

## What is Walk-Forward Analysis?

### The overfitting problem

Every time you run hyperopt, the optimizer searches thousands of parameter combinations and picks
the one that performed best on your training data. The problem: it will always find something that
looks great in-sample. That "best" result may be entirely due to chance — parameters that happened
to fit the noise in that specific historical window. When you deploy the strategy, performance
collapses.

This is **overfitting**, and it is the single most common reason strategies fail in live trading.

A single backtest cannot detect overfitting. The in-sample curve can look spectacular while the
strategy has zero real edge.

### What WFA solves

WFA splits your data into sequential train/test pairs:

1. Optimize parameters on the **train** period (in-sample)
2. Run a backtest on the **test** period (out-of-sample, never touched by the optimizer)
3. Repeat across multiple windows, rolling forward in time

If the strategy has a real edge, it should perform reasonably on every test window. If it only
worked because the optimizer memorized historical noise, performance will degrade — and WFA will
catch it before you deploy.

### Why it is better than a single backtest

- Tests generalization across multiple independent periods, not just one
- Computes Walk-Forward Efficiency (WFE): how much in-sample profit survives out-of-sample
- Runs Monte Carlo simulation to stress-test drawdown
- Tests robustness to parameter perturbation
- Optional CPCV gives a probability of loss across all possible data splits

!!! note
    WFA does not guarantee live performance. It is a filter, not a crystal ball. A strategy
    that passes WFA still needs to be monitored in dry-run before committing real capital.

---

## Quick Start

```bash
freqtrade walk-forward \
  --strategy MyStrategy \
  --timerange 20230101-20250101 \
  --wf-windows 5 \
  --wf-train-ratio 0.75 \
  --wf-embargo-days 7 \
  --wf-mode rolling \
  --wf-min-test-trades 30 \
  --config config.json
```

| Flag | Value | What it does |
|---|---|---|
| `--strategy` | `MyStrategy` | Strategy class to optimize and test |
| `--timerange` | `20230101-20250101` | Full date range (both ends required) |
| `--wf-windows` | `5` | Number of train/test windows |
| `--wf-train-ratio` | `0.75` | 75% of each window for training, 25% for testing |
| `--wf-embargo-days` | `7` | Gap between train end and test start |
| `--wf-mode` | `rolling` | Fixed-size windows sliding forward |
| `--wf-min-test-trades` | `30` | Reject windows with fewer trades than this |
| `--config` | `config.json` | Standard freqtrade config (exchange, pairs, timeframe) |

!!! tip
    For 2 years of data on a 15m timeframe, the defaults (`--wf-windows 5 --wf-train-ratio 0.75`)
    produce ~110-day train windows and ~36-day test windows. That is generally sufficient for
    mean-reversion strategies that trade frequently.

---

## How It Works

WFA runs in four phases.

### Phase 1: Sequential windows

The total date range is divided into N windows. In each window:

1. **Hyperopt** runs on the train period to find optimal parameters
2. An **embargo gap** is skipped (prevents indicator lookahead leakage)
3. A **backtest** runs on the test period using the parameters from step 1

The window then slides forward, and the process repeats.

```
Rolling mode (--wf-mode rolling):

|<---------- Total range ----------------------------------------->|
|  Train 1  |emb| Test 1 |  Train 2  |emb| Test 2 |  Train 3  |...
|-----------|   |--------|-----------|   |--------|-----------|
^                        ^
fixed window size        window slides forward by (test + embargo)
```

```
Anchored mode (--wf-mode anchored):

|<---------- Total range ----------------------------------------->|
|    Train 1    |emb| Test 1 |
|       Train 2       |emb| Test 2 |
|           Train 3           |emb| Test 3 |
^
train always starts at the beginning; it grows with each window
```

After all windows complete, WFA stitches together the out-of-sample test periods into a
continuous equity curve. That concatenated curve is what gets analyzed in phases 2-4.

### Phase 2: Monte Carlo simulation and equity curve analysis

The concatenated out-of-sample trade list is passed to a Monte Carlo simulation. The trade
order is reshuffled 1,000 times, and for each permutation the simulator computes:

- Maximum drawdown
- Return/drawdown ratio

This produces distributions for worst-case (p5), typical (p50), and best-case (p95) scenarios.
The **Carver Discount** (p5 / p50 ratio) measures how robust the strategy is to unlucky
trade ordering.

Alongside MC, the equity curve is analyzed for:

- Overall return, maximum drawdown
- K-ratio (equity curve smoothness)
- SQN, DSR, profit factor, HHI, expectancy

### Phase 3: Robustness checks

Two additional checks run after the main windows:

**Parameter perturbation.** The consensus parameters (most common values across windows) are
randomly nudged by ±5–10% and backtested many times. If profit drops sharply with small
perturbations, the strategy only works at exact parameter values — a sign of overfitting.
A sensitivity score near 0 is good; above 2.0 is fragile.

**Multi-seed convergence.** If `--wf-multi-seed N` is set, the optimizer runs N additional
times on the last window with different random seeds. If different seeds find very different
parameters, the optimization landscape is noisy and the chosen parameters are not reliably
optimal. Convergence above 60% means seeds agree.

### Phase 4: CPCV (optional)

Activated with `--wf-mode cpcv`.

Instead of N sequential windows, CPCV divides the data into N groups and tests every possible
combination of those groups as train/test splits. With N=6 groups and K=2 test groups,
C(6,2)=15 combinations are tested, producing multiple independent backtest paths.

This gives a **Probability of Loss** — the fraction of paths that ended with a negative return.
Below 15% is strong evidence the strategy has a real edge.

!!! warning
    CPCV is significantly slower than rolling mode because it runs C(N,K) hyperopt+backtest
    cycles instead of N. Use it for final validation of a promising strategy, not for every
    iteration.

---

## Understanding the Report

### Verdict (A-F)

The report opens with a single letter grade summarizing all checks.

| Grade | Meaning | Action |
|---|---|---|
| **A** | All checks passed | Deploy to dry-run at full size. Monitor 2-4 weeks |
| **B** | Most checks passed | Dry-run at reduced size (50%). Review failed checks |
| **C** | Mixed signals | Do not deploy. Investigate failed metrics first |
| **D** | Most criteria failed | Strategy likely overfits. Simplify and re-run |
| **F** | Critical failure — lost money out-of-sample | Go back to strategy design |

### Window table

Each row in the window table represents one train/test cycle.

| Column | What it shows |
|---|---|
| Window | Window index and date ranges |
| Train return | Annualized return on the training (in-sample) period |
| Test return | Annualized return on the test (out-of-sample) period |
| WFE | Walk-Forward Efficiency: test return / train return |
| Calmar | Test return / max drawdown on the test period |
| Max DD | Maximum drawdown on the test period |
| Trades | Number of trades during the test period |
| Regime | Market regime during this window (trending / mean-reverting / volatile) |

!!! tip
    Look for windows where WFE collapsed. If one specific date range consistently fails, check
    whether a macro event or regime change explains it. A single bad window is acceptable;
    consistent degradation is not.

### Key metrics reference

| Metric | Full name | What it measures | Good values | Source |
|---|---|---|---|---|
| WFE | Walk-Forward Efficiency | Test return / train return | > 50% | Pardo |
| SQN | System Quality Number | Edge quality: mean/std * sqrt(N) | 1.5–3 (> 5 = suspicious) | Van Tharp |
| DSR | Deflated Sharpe Ratio | Sharpe adjusted for number of trials | > 0.95 | Bailey & Lopez de Prado |
| Calmar | Calmar Ratio | Annual return / max drawdown | > 2 | Young |
| Max DD | Maximum Drawdown | Worst peak-to-trough loss | < 25% | — |
| HHI | Herfindahl-Hirschman Index | Profit concentration across trades | < 0.15 | — |
| PF | Profit Factor | Gross profit / gross loss | > 1.2 | Davey |
| Exp | Expectancy | Mean profit per trade | > 0 | Van Tharp |
| K-ratio | K-Ratio | Equity curve linearity / smoothness | > 0.5 | Zephyr |
| Carver | Carver Discount | p5 / p50 MC return/DD ratio | > 0.6 | Carver |
| Sensitivity | Parameter Sensitivity | Profit change under ±10% param nudge | < 1.0 | — |
| Convergence | Seed Convergence | Agreement between different random seeds | > 60% | — |
| P(loss) | Probability of Loss | CPCV paths with negative return | < 15% | Lopez de Prado |
| Sharpe of Paths | Sharpe of CPCV Paths | Consistency of returns across splits | > 1.0 | Lopez de Prado |

### Monte Carlo

The Monte Carlo section answers: "How bad could things have gotten if the same trades had
occurred in a different order?"

- **p5**: the worst 5% of scenarios — planning for bad luck
- **p50**: the median scenario — typical expected experience
- **p95**: the best 5% of scenarios — what good luck looks like

Max drawdown is path-dependent (trade order matters), while total return is not (it is just
the sum). The key number to watch is the **p5 max drawdown** — this is your stress-test
drawdown estimate.

The **Carver Discount** (p5 / p50 return/DD ratio) measures fragility:

- Near 1.0: worst-case performance is close to typical — robust
- Below 0.3: unlucky ordering wrecks the risk profile — fragile

!!! warning
    If the p5 max drawdown exceeds your risk tolerance, reduce position size before deploying —
    even if the grade is A. The MC simulation often reveals drawdown risk that the mean curve hides.

### Robustness checks

**Parameter perturbation** (sensitivity):
Parameters are nudged randomly ±5–10% across many trials. The report shows:

- `profit_p5 / p50 / p95`: spread of results under perturbation
- `pct_profitable`: fraction of perturbations that remained profitable
- `sensitivity`: normalized instability score (lower is better)

If `pct_profitable` is below 70%, the strategy depends on finding exact parameter values.

**Multi-seed convergence**:
Only shown when `--wf-multi-seed N > 0`. Reports what fraction of seeds found parameters
within 15% of each other. Below 60% convergence means the optimizer is finding noise.

### CPCV

Only present when `--wf-mode cpcv`.

CPCV runs C(N,K) hyperopt+backtest combinations. The key outputs:

- **n_combinations**: how many train/test splits were tested
- **n_paths**: independent backtest paths reconstructed from the combinations
- **prob_of_loss**: fraction of paths with negative return (< 15% is strong, > 30% is fragile)
- **sharpe_of_paths**: Sharpe across path returns (> 1.0 is good)
- **avg_return**: average return across all paths

!!! note
    A probability of loss below 15% across 15 independent combinations is stronger evidence
    than a single backtest with a great Sharpe. This is the most rigorous test available.

---

## Choosing Parameters

### How many windows for how much data

| Data range | Recommended `--wf-windows` |
|---|---|
| 6–12 months | 3 |
| 1–2 years | 4–5 |
| 2–3 years | 5–6 |
| 3+ years | 6–8 |

More windows means shorter individual test periods. Below ~30 trades per test window, results
are statistically unreliable. If `--wf-min-test-trades 30` triggers warnings, reduce the
number of windows or expand the date range.

### Train ratio tradeoffs

| `--wf-train-ratio` | Effect |
|---|---|
| 0.5 | Longer test periods, less training data. Riskier for complex strategies |
| 0.75 (default) | Balanced. Recommended for most strategies |
| 0.85 | More training data, shorter tests. Better for strategies with few parameters |
| 0.90 | Very short test periods. Only with 3+ years of data |

Lower ratios give more out-of-sample data per window but may starve the optimizer. For
strategies with many hyperopt parameters, stay at 0.75 or higher.

### Rolling vs anchored vs CPCV

| Mode | When to use |
|---|---|
| `rolling` | Default. Best for most strategies. Assumes market regimes change over time |
| `anchored` | When you believe earlier data is still relevant and do not want to discard it. The optimizer sees more data each window, but stationarity assumptions weaken |
| `cpcv` | Final validation of a promising strategy. Slowest but gives probability of loss |

!!! tip
    Start with `rolling`. Only switch to `anchored` if you have reason to believe the earliest
    data is still representative of current market behavior.

### Embargo days by timeframe

| Timeframe | Recommended `--wf-embargo-days` |
|---|---|
| 1m–5m | 1 |
| 15m–1h | 2–3 |
| 4h | 5 |
| 1d | 7–14 |

The embargo exists to prevent lagged indicators (e.g., a 200-period MA computed at the end of
training) from overlapping with the test period. For daily bars with long-period indicators,
use at least 14 days.

### When to enable holdout

`--wf-holdout-months N` reserves the final N months as a completely untouched final test.
The optimizer and all walk-forward windows never see this data.

Use holdout when:

- You plan to iterate on the strategy design (the holdout stays clean across iterations)
- You want a final independent check before deploying to live

2–3 months is recommended. With less than 18 months of total data, holdout shrinks the
usable range significantly and may not be worth it.

---

## What To Do After

### Grade A or B — deploy to dry-run

```bash
# Copy the WFA consensus parameters to your strategy JSON
cp user_data/walk_forward/MyStrategy_consensus.json user_data/strategies/MyStrategy.json
```

Then start a dry-run:

```bash
freqtrade trade --strategy MyStrategy --config config.json --dry-run
```

Monitor for 2–4 weeks (grade A) or 4+ weeks (grade B). Compare live fill prices to the
backtest assumptions. Watch for slippage, spread, and execution differences that were not
modeled.

!!! warning
    Even with grade A, only deploy with position sizes you can tolerate losing. Start at 50%
    of intended size and scale up after confirming live behavior matches expectations.

### Grade C — investigate before proceeding

Common fixes for grade C:

1. Add more data history (expand `--timerange`)
2. Reduce the number of hyperopt parameters (fewer parameters = less overfitting risk)
3. Freeze parameters that are unstable across windows (look at the window table)
4. Switch loss function — try `CalmarHyperOptLoss` instead of Sharpe-based losses
5. Check if the failed windows cluster around a specific date — regime change may explain them

Do not deploy a grade C strategy. Fix the underlying issue first and re-run WFA.

### Grade D or F — go back to strategy design

Grade D/F means the strategy does not have a durable edge, or the edge is too small to
survive parameter variation and different market regimes.

Diagnostics:

- If WFE is consistently below 30%, the strategy memorizes training data
- If the equity curve degrades window-by-window, the edge is decaying over time
- If sensitivity is above 2.0, parameters are over-specified
- If SQN is above 5 in training but near 0 in testing, the strategy overfits severely

The correct response is to simplify entry/exit logic, not to tune more aggressively.

---

## Command Reference

| Flag | Type | Default | Description |
|---|---|---|---|
| `--wf-windows` | int | 5 | Number of walk-forward windows |
| `--wf-train-ratio` | float | 0.75 | Fraction of each window used for training |
| `--wf-embargo-days` | int | 7 | Gap in days between train end and test start |
| `--wf-holdout-months` | int | 0 | Months to reserve as final holdout (0 = disabled) |
| `--wf-min-test-trades` | int | 30 | Minimum trades per test window |
| `--wf-mode` | str | rolling | Window mode: `rolling`, `anchored`, or `cpcv` |
| `--wf-multi-seed` | int | 0 | Extra hyperopt runs with different seeds (0 = disabled) |
| `--wf-cpcv-groups` | int | 6 | Number of time-blocks for CPCV mode (N) |
| `--wf-cpcv-test-groups` | int | 2 | Test blocks held out per CPCV combination (K) |

---

## FAQ

### How long does it take?

Roughly: `--wf-windows * hyperopt_time_per_window`. With 5 windows and a 15-minute hyperopt
per window, the full run takes about 75 minutes. CPCV with `N=6, K=2` runs 15 combinations
and takes roughly 3–5× longer than a 5-window rolling run.

For faster iteration, reduce `-e` (hyperopt epochs) in your config or use `--wf-windows 3`
while designing the strategy, then do a full 5-window run before deployment.

### Can I use it with FreqAI?

No. FreqAI strategies use their own training pipeline. Walk-Forward Analysis calls the
standard freqtrade hyperopt and backtest commands and is not compatible with FreqAI models.

### What if I have less than 1 year of data?

Use `--wf-windows 3` and `--wf-train-ratio 0.75`. With 6 months of data this gives roughly
60-day train windows and 20-day test windows. Results will be noisier and the minimum
meaningful `--wf-min-test-trades` threshold becomes harder to meet. Below 4 months of data,
WFA results are not reliable — the test windows are too short to distinguish edge from noise.

### Should I use CPCV or rolling?

Start with `rolling`. It is faster and the results are easier to interpret. Use `cpcv` as a
final check on a strategy that already passed rolling mode. The Probability of Loss metric
from CPCV is more rigorous than the WFE from rolling, but it costs significantly more
computation time.

### Why did my strategy get an F?

The most common reasons:

1. **Train return >> Test return in every window** — classic overfitting, too many parameters
2. **All windows profitable in training, most negative in testing** — the optimizer found
   curve-fitting patterns, not real edges
3. **WFE consistently below 20%** — the edge does not generalize at all
4. **High sensitivity score** — the strategy only works at exact parameter values

Try removing half your hyperopt parameters and re-running. Simpler strategies generalize better.

### Can I run it on futures or spot?

Yes. WFA calls the standard freqtrade hyperopt and backtest internally. Any exchange and
market type supported by freqtrade works. Just make sure your config sets the correct
`trading_mode`, `collateral`, and `exchange` settings, as you would for a normal backtest.

!!! note
    For futures strategies, ensure your data download includes the correct settlement type.
    Funding rates are not modeled in backtests or WFA.
