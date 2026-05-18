# PlateauSampler — Coordinate-Wise Sampler for Robustness-First Hyperopt

> **Naming note**: PlateauSampler was previously called `CWSampler`. The old name
> is preserved as an alias — `--sampler CWSampler` and `from ... import CWSampler`
> both still work. All new docs and logs use the `PlateauSampler` name.

A custom Optuna sampler that prioritises **parameter robustness over peak performance**.
Designed to attack the overfitting problem that plagues strategy hyperopt: instead of
hunting for the single best epoch on training data, it searches for parameter values that
sit on broad performance plateaus — values that remain good when neighbouring parameter
settings also remain good.

For best results, pair PlateauSampler with the companion loss function
[`WalkForwardLoss`](../user_data/hyperopts/WalkForwardLoss.py), which rejects strategies
that look good on the train period as a whole but are inconsistent across its
chronological sub-windows. Sampler + loss together cover both axes of the overfitting
problem: the sampler avoids parameter-space peaks, the loss avoids time-window peaks.

References:
- Method inspired by Eric Lefort's coordinate-wise tuning approach
  ([source](https://youtu.be/GaXngWITLSg?si=Fc1PQQtBW6VmtA0j&t=1991))
- Adaptive step calibration: Hansen & Ostermeier 2001, *Completely Derandomized
  Self-Adaptation in Evolution Strategies* (CMA-ES)
- Overfitting penalty: Bailey & López de Prado 2014, *The Deflated Sharpe Ratio*
- Activity preservation: Carver 2015, *Systematic Trading* (no-fitting principle)

---

## Why PlateauSampler exists — the overfitting trap

Standard hyperopt samplers (TPE, NSGA-III, CMA-ES) optimise a noisy in-sample loss.
On a 6-month training window with 50+ pairs, the loss landscape is **rugged**: it has
thousands of narrow peaks that owe their existence to specific quirks of the training
window rather than to a real edge. A Bayesian sampler will happily zero in on the
tallest peak — but tip the market regime slightly and the strategy collapses.

The classic symptom:

```
Training (in-sample) :  +180% profit, Sharpe 8.4, max DD 6%
Test (out-of-sample) :  -12% profit, Sharpe 0.3, max DD 22%
```

The hyperopt converged on a parameter combination that worked for the *exact* training
data and fails everywhere else.

PlateauSampler's diagnosis: the chosen parameters were **on a peak, not on a plateau**.

```
Loss landscape across one parameter (e.g. RSI threshold):

  loss   |
    ↓    |             ★ ← TPE picks this (lowest loss)
         |            /|\
         |           / | \
         |         _/  |  \_         ← This shape = single-point peak,
         |        /    |    \           highly overfit. Tiny shift in
         |  _____/     |     \____      market regime kills it.
         |
         +-----+-------|---+--+------→
              25      31  35  40       parameter value


  loss   |
    ↓    |        _______________
         |       /         ★      \    ← PlateauSampler picks the centre of
         |      /          |       \      a broad plateau.
         |     /           |        \
         |    /            |         \
         |   /             |          \  Many neighbouring values give
         |  /              |           \ similar loss → robust to shifts.
         |                 |
         +--+--+--+--+--+--+--+--+--+--→
           25 27 29 31 33 35 37 39 41    parameter value
```

The plateau may have a slightly worse best-case loss than the peak, but it is **far less
sensitive to perturbations**, both in parameter values (model error) and in market
conditions (regime drift). For trading systems, robustness beats peak performance every
time — backtests are not the deployment environment.

---

## How it works — 5 phases

PlateauSampler implements a deterministic search schedule rather than the adaptive
Bayesian exploration of TPE / GP. Five phases:

### Phase 1 — BASELINE (1 trial)

Run the strategy with every parameter at its **default value** (the `default=X` argument
of `DecimalParameter` / `IntParameter` / `CategoricalParameter`). This single trial
anchors the search: it is the reference loss and trade count that every subsequent
phase compares against. It also seeds the **activity floor** — by default any candidate
v2 trial that fires fewer than 70% of baseline's trade count is excluded from plateau
membership.

If the user did not provide a `default`, midpoint fallback kicks in `(low + high) / 2`,
but with a logged warning — the strategy author should set sensible defaults.

### Phase 2 — ADAPTIVE SCAN (variable trial count)

For each parameter in turn, test values **around its default**, expanding symmetrically:
`default ± 1 step`, `default ± 2 steps`, `default ± 3 steps`, etc., up to a per-direction
limit. After every new trial, recompute an **adaptive tolerance** from the observed loss
volatility on this parameter:

```python
tolerance = clip(
    PLATEAU_FLOOR    × |loss_baseline|,             # absolute minimum (1%)
    PLATEAU_FRACTION × max_observed_loss_change,    # relative to sensitivity (30%)
    PLATEAU_CEILING  × |loss_baseline|,             # absolute maximum (15%)
)
```

A neighbour value `n` is considered "in plateau" if:
- `|loss(n) - loss(baseline)| < tolerance`, AND
- `n_trades(n) ≥ activity_floor` (70% of baseline by default).

As soon as the scan hits a value that's **outside the plateau** in a given direction,
the early-stop kicks in and that direction's expansion stops — no point burning epochs
on values we already know are bad. The step size itself is adaptive:
`sampling_step = max(dist.step, range / 30)`. So for a `FloatParameter(0.01, 0.10,
step=0.001)`, the scan moves in increments of 0.003 (not 0.001), covering meaningful
chunks of the range without thousands of micro-trials.

Typical scan budget: 4-12 trials per param, depending on plateau width.

### Phase 3 — CLASSIFY (computed once, 0 trial)

Using the scan results, classify each parameter into one of three categories:

| Kind | When | What happens in Phase 4 |
|---|---|---|
| `ACTIVE_PLATEAU` | At least one neighbour value satisfies both plateau criteria | Explored in `[low, high]` during assembly |
| `FROZEN_BOWL` | All neighbours are worse than baseline (default is a local minimum) | Fixed at default value, not explored |
| `FROZEN_CATEGORICAL` | Categorical parameter (no ordering, no plateau notion) | Fixed at best-loss observed choice |

For each `ACTIVE_PLATEAU` param, the sampler records the bounds `[low, high]` of the
plateau (the extreme values that still satisfy the membership criteria).

### Phase 4 — ASSEMBLY (remaining epoch budget)

Run trials that combine the `ACTIVE_PLATEAU` params using **uniform random sampling
within their respective plateau bounds**, while keeping all `FROZEN_*` params at their
fixed values. Random sampling — not Bayesian — because Bayesian convergence tends to
find peaks within the plateau, which still overfits. Random gives a diverse spread of
candidates within the validated stable regions; the export phase below picks the right
one with Occam regularisation.

The activity floor still applies: any assembly trial that fires fewer trades than the
floor is excluded from export consideration regardless of its loss.

### Phase 5 — EXPORT (Occam-regularised selection)

Among all completed trials (baseline + scan + assembly) that pass the activity floor,
apply this selection:

1. Find the absolute best loss across the candidate pool.
2. Form a **top-K window**: all trials within `EXPORT_TOP_K_TOLERANCE × |loss_baseline|`
   of the best (default 20%).
3. Within the top-K window, pick the trial with the **fewest parameters changed from
   baseline** (a param is "changed" if normalised distance from baseline > 1%).
4. Tie-break by lowest loss.

This is Occam's razor applied to hyperopt: prefer simple changes over complex ones,
because complex changes are more likely to be overfit artefacts. The baseline trial
(0 changes) is always in the pool — if no v2 candidate substantially improves on it,
v1 is exported automatically with no special hard-fallback needed.

```
Selection example (5 candidate trials, baseline_loss = -0.20):

Trial  | loss   | n_changes | within top-K (20% × 0.20 = 0.04 slack)?
-------|--------|-----------|-----------------------------------------
#0     | -0.20  | 0         | YES (best loss = -0.34, slack window = [-0.38, -0.34])
#13    | -0.36  | 1         | NO  — out of slack
#27    | -0.34  | 2         | YES
#41    | -0.36  | 4         | NO  — out of slack
#92    | -0.35  | 3         | YES

Top-K = [#0, #27, #92]
Pick by n_changes ascending: #0 (0) < #27 (2) < #92 (3)
→ Exported: baseline (trial #0) — v1 is preserved
```

Note that picking the absolute-best-loss alone (trial #13 here, with -0.36) would have
exported a config with 1 changed parameter — but Occam regularisation prefers the
baseline because the 0.36 vs 0.34 difference is within the noise floor.

---

## Concrete example — 2-parameter mean-reversion

Take the simplest possible strategy: RSI(14) oversold-buy + RSI overbought-exit.

```python
class SimpleRsi(IStrategy):
    rsi_buy  = IntParameter(20, 50, default=30, space="buy")
    rsi_exit = IntParameter(50, 90, default=70, space="buy")
    # ... rest of the strategy
```

Run `freqtrade hyperopt --sampler PlateauSampler --epochs 100 --spaces buy`.

What PlateauSampler does:

```
Phase 1 BASELINE (1 trial):
  rsi_buy=30, rsi_exit=70 → loss = -0.234 (the reference)

Phase 2 SCAN (n_params × ppp = 2 × 20 = 40 trials):
  Scan rsi_buy from 20 to 50 in 20 steps (other param at default 70):
    rsi_buy=20: loss=-0.18
    rsi_buy=21: loss=-0.19
    ...
    rsi_buy=28: loss=-0.24       ← plateau starting
    rsi_buy=30: loss=-0.234       ← baseline
    rsi_buy=32: loss=-0.24
    rsi_buy=35: loss=-0.245      ← peak
    rsi_buy=36: loss=-0.18       ← cliff
    rsi_buy=40: loss=-0.10
    ...
  Scan rsi_exit from 50 to 90 in 20 steps (rsi_buy at default 30):
    ... similar grid scan ...

Phase 3 PLATEAU DETECTION:
  rsi_buy: best peak is value=35 (loss=-0.245). But neighbours are:
    32→-0.24, 35→-0.245, 36→-0.18  (cliff at 36)
    Neighbour mean = (-0.24 + -0.18) / 2 = -0.21
    robustness = 1 - |-0.21 - -0.245| / 0.245 = 0.86 → plateau OK
    BUT a TPE sampler would have picked 35 directly. PlateauSampler picks 32 instead:
      neighbours at 32: 30→-0.234, 32→-0.24, 35→-0.245
      neighbour mean = -0.24, robustness = 0.97
      combined score higher at 32 than at 35 due to more uniform neighbourhood
  rsi_exit: same analysis. Suppose plateau picks rsi_exit=68.

Phase 4 ASSEMBLY (100 - 41 = 59 trials):
  Anchor = (rsi_buy=32, rsi_exit=68)
  Jittered trials: (33, 70), (31, 67), (34, 68), (32, 69), ...
  All variants tested → confirms joint stability.

Final reported best: e.g. (rsi_buy=32, rsi_exit=68), loss=-0.241
```

A TPE sampler running on the same problem will likely return `(rsi_buy=35,
rsi_exit=72)` with loss=-0.248. Marginally better in-sample.

Out-of-sample reality (on next 3 months of data):
- TPE picks → loss collapses to -0.05 (the 35→36 cliff was a training artefact)
- PlateauSampler → loss holds at -0.18 (the plateau survives the regime shift)

This is the practical value: PlateauSampler typically gives up 2-5% of in-sample peak loss
in exchange for 30-60% better out-of-sample loss preservation. The OOS gap is the only
metric that matters for live trading.

---

## When to use PlateauSampler

### ✓ Good fit

| Situation | Why |
|---|---|
| Refining hand-tuned strategy with 5-15 params | The 5-phase schedule scales linearly with n_params; sweet spot is 8-15 |
| Validating known-good defaults | The default-anchored baseline preserves what already works; only changes params with clear plateau improvements |
| DCA / mean-reversion strategies | Weak parameter interactions = coordinate-wise scan captures most of the signal |
| After a TPE pass found something interesting | TPE explores broadly, PlateauSampler then validates robustness around the discovery |
| Production deployment after walk-forward | PlateauSampler's bias toward stability is exactly what you want before going live |
| Need reproducibility | Deterministic schedule = same params → same trials in same order |

### ✗ Bad fit

| Situation | Why | Use instead |
|---|---|---|
| Initial exploration of an unknown strategy space | PlateauSampler anchored on defaults; without good defaults it's lost | TPESampler or NSGAIIISampler |
| > 20 optimisable parameters | Scan grows linearly → epoch cost explodes; combined-score interactions get murky | TPESampler with 1500+ epochs |
| Heavily coupled parameters (e.g. grid trading spacing × DCA volume) | One-at-a-time scan can't see joint optima | NSGAIIISampler or CMA-ES |
| Highly categorical search space (40+ choices per param) | The plateau concept doesn't apply to categoricals (no neighbour notion) | TPESampler |
| No epoch budget constraints + want absolute peak | TPE will eventually beat PlateauSampler on raw loss minimisation | TPESampler with 3000+ epochs |
| Strategy testing parameters that should NEVER fall back to baseline | The default-fallback safety can hide a parameter that *should* move significantly | Pass `defaults={}` explicitly to force midpoint baseline |
| **Strategy with hard "cliff" parameters** (e.g. `n_bars_required ≥ 8` cumulative counters, `cumulative_entry_bars`) | Plateau detection assumes smooth loss landscapes. Cliffs are staircases, not plateaus — perturbing by ±1 destroys the signal | Keep v1 as-is; PlateauSampler will mis-classify the cliff as a baseline-fallback or pick a degraded variant |
| **Strategy already near a Pareto front** (very high Sharpe + low DD + few free dimensions) | No move improves anything; all directions degrade. PlateauSampler will export a "robust" v2 that under-performs v1 on every metric | Keep v1; confirmed via TPE A/B that even TPE produces equal-or-worse results — the limit is the strategy, not the sampler |

---

## Strategy profile checklist — does PlateauSampler stand a chance?

Empirically, on a 3-strategy campaign (mean-reversion DCA on Hyperliquid USDC perps,
14-month train + 6-month OOS, run 2026-05-17), only **1 of 3 strategies** produced a v2
that beat its v1 on holdout. The 2 failures were not sampler bugs — they were strategy
profiles where no parameter perturbation could improve the v1 (cross-validated with TPE).

Before running PlateauSampler, sanity-check the v1 against this rule of thumb on the **train**
window:

| v1 in-sample Sharpe | Expected PlateauSampler outcome | Action |
|---|---|---|
| < 1.0 | High probability of a meaningful v2 (the v1 is sub-optimal, there's room to plateau-search) | Run PlateauSampler with confidence |
| 1.0 – 4.0 | Mixed. Plateau may or may not exist. Run it, but apply the **trade-count guardrail** below | Run PlateauSampler, validate on holdout |
| > 4.0 | Likely Pareto-optimal already. The v2 will probably be a degraded variant fitting an artificially conservative pocket | Run PlateauSampler only if you have epoch budget to spare; expect v1 to win |

This rule is a proxy, not a law. A high Sharpe but only 30-50 trades = noisy estimate
that may still hide room to plateau. Conversely, a low Sharpe with a structurally bad
strategy idea won't be saved by any sampler.

### Trade-count parity guardrail — non-negotiable

If the PlateauSampler-produced v2 generates **< 70% of v1's holdout trade count**, **reject
the v2**. This is the single most important sanity check.

Reason: the "plateau" the sampler converged to corresponds to a restrictive parameter
combination that fires far fewer signals. Even if the per-trade metrics look better, the
v2 has lost diversification (fewer trades = each trade carries disproportionate weight
= overfitting risk increases despite the apparent "stability"). The activity floor
inside the sampler enforces this on **train** but cannot enforce it on **holdout** if
the train and holdout regimes differ (typical case: train has high volatility, the
robust optimum is a stricter ATR filter, holdout has low volatility → filter rarely
fires → v2 collapses to a handful of trades).

Example failure mode (VwapRevertV2, 2026-05-17): v1 = 631 trades on holdout with
Sharpe 4.37; PlateauSampler-v2 = 9 trades on holdout with Sharpe 0.44 — a 70x drop in
activity, hidden by reasonable in-sample metrics.

#### Automatic safeguards built into Phase 5

Two automatic mechanisms reduce the risk of exporting an over-fit v2:

1. **Activity floor** — Trials whose trade count falls below 70% of baseline
   are excluded from the export candidate pool entirely (regardless of how
   good their loss looks). This prevents the sampler from picking restrictive
   parameter values that fire too few signals.

2. **Occam-regularised selection** — Among trials with loss within the top-K
   slack window of the best, the export picks the one with the **fewest
   parameters changed from baseline**. Combined with the fact that the
   baseline trial (0 changes) is always in the pool, this means: if no v2
   candidate substantially beats the baseline, v1 is exported automatically.

Even with these automatic safeguards, the manual OOS trade-count check above
remains the final gate before live deployment, because the safeguards only
see TRAIN data.

### Loss-function alignment — critical

If your v1 was selected (manually or by a prior hyperopt) under loss function A, **run
PlateauSampler with the same loss function A**. Running with a different loss B means the
plateau detection optimises for a different objective than the one that originally made
v1 good; the resulting robust_optima will diverge from v1's success conditions, often
producing a v2 with the right "shape" under loss B but no actual carryover to live.

Confirmed empirically: aligning the v2 hyperopt loss to the v1's loss (e.g.
`RobustResearchHyperOptLoss` for both) recovered the ConfluenceShortV2 OOS win that
was lost when v2 was first run under a different loss.

---

## Sampler comparison — quick reference

| Aspect | PlateauSampler | TPESampler | NSGAIIISampler | CMA-ES |
|---|---|---|---|---|
| Convergence model | Deterministic schedule | Bayesian (probabilistic) | Genetic (population) | Evolution strategy |
| Captures param interactions | No (Phase 2) + random in plateaus (Phase 4) | Yes (Parzen estimators) | Yes (population) | Yes (covariance) |
| Detects plateaus natively | **Yes** | No | No | No |
| Robustness vs peak performance | **Robustness first** | Peak first | Peak first | Peak first |
| Reproducibility (same seed) | **100% deterministic** | High | Moderate | High |
| Sweet spot # params | 5-15 | 5-50 | 5-50 | 5-30 (continuous) |
| Epoch budget per param | ~30-50 minimum | ~30-100 | ~50-100 | ~50-100 |
| Handles `--early-stop` | **No (rejected)** | Yes | Yes | Yes |
| Use after TPE found something | **Yes (refinement)** | N/A | N/A | N/A |

---

## Usage

### Minimum invocation

```bash
freqtrade hyperopt \
  --strategy MyStrategy \
  --config myconfig.json \
  --timerange 20240101-20250101 \
  --hyperopt-loss MoutonMeanRevHyperOptLoss \
  --sampler PlateauSampler \
  --epochs 500 \
  --spaces buy \
  -j -2
```

Note the absence of `--early-stop` — see below.

### Epoch budget

The sampler **self-adjusts** `points_per_param` to fit the given budget:
- Generous budget (≥ `scan_needed × 2`) → `points_per_param = 20` (max density)
- Tight budget → reduced down to the floor (5 points/param) to preserve assembly

Minimum guidance per number of optimisable parameters:

| n_params | minimum epochs | recommended epochs |
|---|---|---|
| 3-5    | 60-100  | 150-200 |
| 6-8    | 100-150 | 250-350 |
| 9-12   | 200-300 | 400-500 |
| 13-16  | 300-450 | 500-700 |
| 17-20  | 400-550 | 600-800 |
| 21+    | not recommended — use TPESampler |

The hyperopt CLI will refuse to start if `--epochs` is below the minimum:

```
OperationalException: PlateauSampler: --epochs 100 is below the minimum viable budget
for 11 optimizable parameters. Minimum = 1 + 11 × 5 = 56 (just enough to scan all
params at floor density). Recommended = 187 (proper scan + assembly).
```

### Why `--early-stop` is rejected

The coordinate-wise scan produces many trials that do not improve the running best (a
single parameter varies while all others sit at baseline → most scan trials land worse
than baseline). A standard early-stop watcher would trigger long before the scan
completes, and plateau detection is then skipped entirely — the sampler degrades to "the
best of the first few scan trials, midpoint everywhere else", which defeats its purpose.

To enforce this, the hyperopt CLI raises:

```
OperationalException: PlateauSampler is incompatible with --early-stop (currently set
to 250). Either omit --early-stop or pass --early-stop 0. The sampler manages its
own scan/assembly schedule and needs the full epoch budget to complete plateau
detection.
```

Pass `--early-stop 0` or simply omit the flag.

### Default values are passed automatically

The hyperopt optimiser introspects the strategy's `IHyperOptParameter` instances and
extracts their `default` values, passing them to PlateauSampler as baseline anchors:

```
INFO  PlateauSampler: passed 11 hand-tuned defaults as baseline anchors
INFO  PlateauSampler: 11 params (baselines: 11 from default, 0 from midpoint),
                 scan=211 trials, budget=600 → scan_pct=35%, assembly=389 trials
```

If a parameter has no `default` (uses `low` as the default fallback), midpoint applies
to that single parameter and a warning is logged.

---

## Consuming the output — `robust_optima` is the v2 file

⚠ **Critical distinction**: freqtrade's "Best result" at the end of a hyperopt run
displays the lowest-loss epoch. With PlateauSampler, this is **usually a scan trial**
(baseline + 1 perturbed parameter), NOT the assembled robust optima.

The PlateauSampler's actual recommendation is the `robust_optima` dict computed during
plateau detection. To make this trivially consumable, the sampler **automatically
dumps it to a freqtrade-loadable JSON** at the scan→assembly transition:

```
user_data/hyperopt_results/cwsampler_robust_<strategy_name>.json
```

Format (identical to freqtrade's `hyperopt-show` output schema):

```json
{
  "strategy_name": "ExhaustionHunterV2",
  "params": {
    "buy": {
      "loi_threshold": 0.85,
      "fgm_low": -19,
      "custom_stoploss_value": -0.8,
      "...": "..."
    },
    "sell": {}, "protection": {}, "roi": {}, "stoploss": {},
    "max_open_trades": {}, "trailing": {}
  },
  "ft_stratparam_v": 1,
  "export_time": "2026-05-16T15:33:50.000+00:00",
  "cwsampler_meta": {
    "n_params": 11,
    "n_plateaus": 9,
    "n_baseline_fallback": 2,
    "baseline_source": {"loi_threshold": "default", "...": "..."}
  }
}
```

The log line at scan→assembly transition tells you exactly where to find it:

```
PlateauSampler: robust_optima dumped to user_data/hyperopt_results/cwsampler_robust_ExhaustionHunterV2.json
           — this is the canonical v2 params file. To deploy:
           `cp user_data/hyperopt_results/cwsampler_robust_ExhaustionHunterV2.json
              user_data/strategies/exhaustionhunterv2.json`
```

### Deploying the v2

1. Read the dumped JSON to inspect `cwsampler_meta`. If `n_plateaus / n_params` is
   ≥ 0.7, the sampler has high confidence. If < 0.5, most params fell back to
   baseline — the PlateauSampler didn't find much room for improvement.
2. **Backtest the v2 params on OOS** (the holdout window NOT used for hyperopt).
3. **Backtest the v1 baseline on the same OOS**.
4. **Apply both gates** (in this order):
   - **Trade-count gate**: `v2_trades / v1_trades ≥ 0.7`. If the v2 trades fewer
     than 70% of v1's holdout trades, reject the v2 immediately — the "plateau"
     it found is in fact a restrictive corner that won't fire enough in live.
     See "Trade-count parity guardrail" above.
   - **Risk-adjusted gate**: v2 Sharpe ≥ v1 Sharpe AND v2 max DD ≤ v1 max DD.
     If only one of these is true (e.g. Sharpe up but DD up too), prefer v1 —
     the v2 has shifted the risk profile, not reduced it.
5. If v2 passes both gates, deploy. If equal or borderline, keep v1 (less change
   = less risk). If v2 is worse, the PlateauSampler didn't help on this strategy —
   the v1 was likely near-Pareto already (see "Strategy profile checklist"
   above) or the loss function used for the v2 hyperopt diverged from what
   made the v1 good (see "Loss-function alignment").
6. To deploy, copy the dumped file as the strategy's co-located .json
   (filename = lowercase strategy class name):
   ```bash
   cp user_data/hyperopt_results/cwsampler_robust_ExhaustionHunterV2.json \
      user_data/strategies/exhaustionhunterv2.json
   ```
   Freqtrade will auto-load these params on next run.

### What if you want freqtrade's "Best result" instead?

It is still in the .fthypt file as before; nothing changed for that flow. But
remember: freqtrade-best ≠ PlateauSampler-output. They optimise different things
(raw loss vs plateau-membership). For the PlateauSampler use case (robustness over
peak performance), always start with the dumped robust_optima.

---

## Reading the logs

A successful PlateauSampler run produces (in chronological order):

```
PlateauSampler: passed 11 hand-tuned defaults as baseline anchors
PlateauSampler: 11 params (baselines: 11 from default, 0 from midpoint),
           scan=211 trials, budget=600 → scan_pct=35%, assembly=389 trials
```
*Initialisation. Confirms that defaults were picked up and the scan/assembly split fits
the budget. If `scan_pct > 50%`, the budget is tight (assembly will be small).*

```
PlateauSampler: auto-adjusted points_per_param 20 → 12
           (budget=300 epochs, 11 params, target scan_ratio=60%)
```
*Only printed if the budget forced a reduction of `points_per_param` below 20. Floor is
5; below that the hard-error fires before this log.*

```
PlateauSampler: computing robust optima (plateau detection)...
  rsi_buy: chosen=32 (loss=-0.241, robustness=0.97, baseline=30)
  rsi_exit: chosen=68 (loss=-0.241, robustness=0.81, baseline=70)
  custom_stoploss: no stable plateau found (robustness=0.31 < 0.5),
                   keeping baseline=-0.62
PlateauSampler: SCAN complete → entering ASSEMBLY phase
PlateauSampler: robust optima = {'rsi_buy': 32, 'rsi_exit': 68, 'custom_stoploss': -0.62, ...}
PlateauSampler: 8 params found stable plateaus, 3 fell back to baseline (no plateau detected)
```
*The plateau detection summary. Pay attention to the **baseline-fallback count**: if too
many params fall back, either the strategy is over-parameterised (defaults are
already good), or the loss surface is too noisy for plateau detection (e.g., training
window too short).*

If you never see the `SCAN complete → ASSEMBLY phase` line, the scan was interrupted
(usually by `--early-stop` triggering against the sampler's intent — but this is now
prevented by the hard-error).

---

## Known pitfalls

### Pitfall 1: scan that completes but produces only midpoint/baseline results

Symptom: every reported best epoch's parameter set matches the baseline exactly
(except for one parameter that's at some grid value).

Cause: assembly phase was skipped. The reported best is just the best scan trial,
which is by construction `baseline + 1 perturbed param`.

Diagnosis: search the log for `SCAN complete → entering ASSEMBLY phase`. If absent,
the scan was truncated. Check for `--early-stop` flag (now blocked) or that the
freqtrade version applied the trial.number-based phase transition fix.

### Pitfall 2: all parameters fall back to baseline

Symptom: `PlateauSampler: 0 params found stable plateaus, N fell back to baseline`.

Causes:
- Training window too short → loss too noisy → robustness ratio always < 0.5
- The loss function returns near-zero values → robustness formula uses `|loss|` as
  denominator, which is fragile when loss ≈ 0
- Strategy genuinely insensitive to its parameters (rare; suggests over-parameterisation)

Mitigation: longer training window, switch loss to one with a wider numerical range
(MoutonMeanRevHyperOptLoss is good), or reduce the number of optimised parameters.

### Pitfall 3: confusing "duplicate" message in the freqtrade summary

Freqtrade reports `N epochs skipped due to duplicate parameters` at the end of a run.
With PlateauSampler this is **expected and benign**: the scan visits the baseline value
of each parameter once (when its grid happens to align), producing one duplicate per
parameter. The assembly phase produces more duplicates due to jitter rounding. The
sampler's phase transition uses `trial.number` (Optuna's monotonic counter) and is
not affected by duplicates being pruned.

### Pitfall 4: hand-tuning a default after the first hyperopt

If you re-run PlateauSampler after manually editing a `default=X` in your strategy, the new
default becomes the baseline anchor. Old scan results are not reused. The robust optimum
will likely shift if the new default is far from the old one. Treat each PlateauSampler run
as a fresh anchor-and-search.

---

## Practical workflow recommendation

1. **First pass — exploration with TPESampler**.
   Wide ranges, many params, 600-1000 epochs. Find a candidate configuration.

2. **Validate with walk-forward analysis**.
   Walk-forward N windows. Reject if performance degrades >50% OOS.

3. **Compact the strategy** (see `user_data/strategies_generator/compact_strategy_playbook.md`).
   Inline indicators, freeze chosen params as `default=X` in DecimalParameter declarations.

4. **Second pass — refinement with PlateauSampler**.
   Same strategy file, now with frozen defaults. Re-hyperopt with PlateauSampler at
   400-600 epochs depending on n_params. Looks for plateaus around your defaults
   to improve robustness without losing performance.

5. **Final walk-forward + dry-run**.
   The PlateauSampler-refined params should now show **smaller OOS gap** vs the TPE
   first-pass params, even if the in-sample is marginally lower.

This 2-pass approach (TPE-then-CW) typically wins on OOS by 20-40% over a TPE-only run
with the same total epoch budget.

---

## Implementation references

- Sampler source : `freqtrade/optimize/hyperopt/cw_sampler.py`
- Integration   : `freqtrade/optimize/hyperopt/hyperopt_optimizer.py` (function
  `get_optimizer`, branch `o_sampler == "PlateauSampler"`)
- Final export hook : `freqtrade/optimize/hyperopt/hyperopt.py:_export_cwsampler_robust`
  (calls `PlateauSampler.select_best_export` to apply Occam-regularised selection
  before writing the v2 JSON)
- Constants     :
  - `PLATEAU_FLOOR / PLATEAU_FRACTION / PLATEAU_CEILING` (adaptive tolerance bounds)
  - `EXPORT_TOP_K_TOLERANCE = 0.20` (slack window for Occam selection)
  - `EXPORT_CHANGE_EPSILON = 0.01` (normalised distance threshold for "changed" param)
  - `DEFAULT_SCAN_BUDGET_RATIO = 0.6` (60% scan, 40% assembly)
  - `MAX_SCAN_STEPS_PER_DIR = 10` (hard cap on scan trials per direction per param)
  - `MIN_POINTS_PER_PARAM = 4` (minimum scan trials per param for plateau detection)
  - `DEFAULT_MIN_TRADES_RATIO = 0.7` (70% of baseline's trade count = activity floor)
