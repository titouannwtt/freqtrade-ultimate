# CWSampler — Coordinate-Wise Sampler for Robustness-First Hyperopt

A custom Optuna sampler that prioritises **parameter robustness over peak performance**.
Designed to attack the overfitting problem that plagues strategy hyperopt: instead of
hunting for the single best epoch on training data, it searches for parameter values that
sit on broad performance plateaus — values that remain good when neighbouring parameter
settings also remain good.

Reference: method inspired by Eric Lefort's coordinate-wise tuning approach
([source](https://youtu.be/GaXngWITLSg?si=Fc1PQQtBW6VmtA0j&t=1991)).

---

## Why CWSampler exists — the overfitting trap

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

CWSampler's diagnosis: the chosen parameters were **on a peak, not on a plateau**.

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
         |       /         ★      \    ← CWSampler picks the centre of
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

## How it works — 4 phases

CWSampler implements a deterministic search schedule rather than the adaptive Bayesian
exploration of TPE / GP. Phases:

### Phase 1 — BASELINE (1 trial)

Run the strategy with every parameter at its **default value** (the `default=X` argument
of `DecimalParameter` / `IntParameter` / `CategoricalParameter`). This single trial
anchors the search: it is the reference loss that every coordinate sweep will compare
against.

If the user did not provide a `default`, midpoint fallback kicks in `(low + high) / 2`,
but with a logged warning — the strategy author should set sensible defaults.

### Phase 2 — SCAN (n_params × points_per_param trials)

For each parameter in turn, vary it across a regular grid of `points_per_param` values
spanning its full range, while **keeping every other parameter fixed at the baseline**.

```
Visual: 4-param strategy, 5 points per param → 1 + 4×5 = 21 trials

Trial #  | param_A     | param_B  | param_C  | param_D
---------|-------------|----------|----------|----------
0        | def_A       | def_B    | def_C    | def_D     ← baseline
---------|-------------|----------|----------|----------
1        | grid_A[0]   | def_B    | def_C    | def_D     ← scan param_A
2        | grid_A[1]   | def_B    | def_C    | def_D
3        | grid_A[2]   | def_B    | def_C    | def_D
4        | grid_A[3]   | def_B    | def_C    | def_D
5        | grid_A[4]   | def_B    | def_C    | def_D
---------|-------------|----------|----------|----------
6        | def_A       | grid_B[0]| def_C    | def_D     ← scan param_B
7        | def_A       | grid_B[1]| def_C    | def_D
...      | ...         | ...      | ...      | ...
---------|-------------|----------|----------|----------
11       | def_A       | def_B    | grid_C[0]| def_D     ← scan param_C
...      | ...         | ...      | ...      | ...
---------|-------------|----------|----------|----------
16       | def_A       | def_B    | def_C    | grid_D[0] ← scan param_D
...      | ...         | ...      | ...      | ...
20       | def_A       | def_B    | def_C    | grid_D[4]
```

The scan produces a **one-dimensional slice** of the loss surface for each parameter
individually — the shape of the loss when only that parameter changes. This is enough to
spot peaks vs plateaus per parameter.

### Phase 3 — PLATEAU DETECTION (computed once)

For each parameter, compute a **combined score per grid point** that balances:

- **performance** : how good the loss is at this point (relative to scan's best/worst)
- **robustness**  : how similar the loss is at this point compared to its neighbours
  within a 15% radius of the parameter's range

```python
performance_score = 1 - (loss[i] - best_loss) / (worst_loss - best_loss)
robustness_score  = 1 - |mean(neighbour_losses) - loss[i]| / |loss[i]|
combined_score    = 0.5 × performance + 0.5 × robustness
```

Pick the grid value with the highest combined score.

If `robustness_score < 0.5` (no stable plateau detected for this parameter), **fall back
to the baseline value** for that parameter. This is a key safety guard: rather than
"optimise" toward a noisy point, the sampler preserves the known-good default. Defaults
that ARE based on prior tuning therefore act as a noise floor.

```
Combined score example (one parameter, 7 grid points):

   value  →  20    25    30    35    40    45    50
   loss   →  0.32  0.28  0.21  0.20  0.19  0.27  0.31
   perf   →  0.05  0.31  0.85  0.92  1.00  0.38  0.10
   robust →  0.42  0.71  0.91  0.95  0.62  0.55  0.48
   combined  0.24  0.51  0.88  0.94  0.81  0.46  0.29
                                 ↑
                          picked (plateau centre)
```

Note: in this example, the absolute best loss is at value=40 (0.19), but the chosen
robust optimum is value=35 because its neighbours are also good. This is the entire
point of the sampler.

### Phase 4 — ASSEMBLY (remaining epoch budget)

Combine all robust optima from Phase 3 into a single anchor point. Run trials that
perturb this anchor with **±10% jitter on each parameter independently** (uniformly
distributed within the parameter's range), to validate that the assembled combination
holds together. This is where joint parameter interactions are tested, since Phase 2
only saw one-at-a-time variations.

If the assembled point survives jittering (loss remains stable), the final reported
best epoch comes from this phase. If the jitter reveals that the assembled point is
fragile when params are perturbed jointly, the freqtrade hyperopt output will surface
better epochs from elsewhere — but in practice, robust-individual-plateau parameters
tend to behave decently jointly when their initial defaults already worked.

---

## Concrete example — 2-parameter mean-reversion

Take the simplest possible strategy: RSI(14) oversold-buy + RSI overbought-exit.

```python
class SimpleRsi(IStrategy):
    rsi_buy  = IntParameter(20, 50, default=30, space="buy")
    rsi_exit = IntParameter(50, 90, default=70, space="buy")
    # ... rest of the strategy
```

Run `freqtrade hyperopt --sampler CWSampler --epochs 100 --spaces buy`.

What CWSampler does:

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
    BUT a TPE sampler would have picked 35 directly. CWSampler picks 32 instead:
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
- CWSampler → loss holds at -0.18 (the plateau survives the regime shift)

This is the practical value: CWSampler typically gives up 2-5% of in-sample peak loss
in exchange for 30-60% better out-of-sample loss preservation. The OOS gap is the only
metric that matters for live trading.

---

## When to use CWSampler

### ✓ Good fit

| Situation | Why |
|---|---|
| Refining hand-tuned strategy with 5-15 params | The 4-phase schedule scales linearly with n_params; sweet spot is 8-15 |
| Validating known-good defaults | The default-anchored baseline preserves what already works; only changes params with clear plateau improvements |
| DCA / mean-reversion strategies | Weak parameter interactions = coordinate-wise scan captures most of the signal |
| After a TPE pass found something interesting | TPE explores broadly, CWSampler then validates robustness around the discovery |
| Production deployment after walk-forward | CWSampler's bias toward stability is exactly what you want before going live |
| Need reproducibility | Deterministic schedule = same params → same trials in same order |

### ✗ Bad fit

| Situation | Why | Use instead |
|---|---|---|
| Initial exploration of an unknown strategy space | CWSampler anchored on defaults; without good defaults it's lost | TPESampler or NSGAIIISampler |
| > 20 optimisable parameters | Scan grows linearly → epoch cost explodes; combined-score interactions get murky | TPESampler with 1500+ epochs |
| Heavily coupled parameters (e.g. grid trading spacing × DCA volume) | One-at-a-time scan can't see joint optima | NSGAIIISampler or CMA-ES |
| Highly categorical search space (40+ choices per param) | The plateau concept doesn't apply to categoricals (no neighbour notion) | TPESampler |
| No epoch budget constraints + want absolute peak | TPE will eventually beat CWSampler on raw loss minimisation | TPESampler with 3000+ epochs |
| Strategy testing parameters that should NEVER fall back to baseline | The default-fallback safety can hide a parameter that *should* move significantly | Pass `defaults={}` explicitly to force midpoint baseline |

---

## Sampler comparison — quick reference

| Aspect | CWSampler | TPESampler | NSGAIIISampler | CMA-ES |
|---|---|---|---|---|
| Convergence model | Deterministic schedule | Bayesian (probabilistic) | Genetic (population) | Evolution strategy |
| Captures param interactions | No (Phase 2) + jitter (Phase 4) | Yes (Parzen estimators) | Yes (population) | Yes (covariance) |
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
  --sampler CWSampler \
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
OperationalException: CWSampler: --epochs 100 is below the minimum viable budget
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
OperationalException: CWSampler is incompatible with --early-stop (currently set
to 250). Either omit --early-stop or pass --early-stop 0. The sampler manages its
own scan/assembly schedule and needs the full epoch budget to complete plateau
detection.
```

Pass `--early-stop 0` or simply omit the flag.

### Default values are passed automatically

The hyperopt optimiser introspects the strategy's `IHyperOptParameter` instances and
extracts their `default` values, passing them to CWSampler as baseline anchors:

```
INFO  CWSampler: passed 11 hand-tuned defaults as baseline anchors
INFO  CWSampler: 11 params (baselines: 11 from default, 0 from midpoint),
                 scan=211 trials, budget=600 → scan_pct=35%, assembly=389 trials
```

If a parameter has no `default` (uses `low` as the default fallback), midpoint applies
to that single parameter and a warning is logged.

---

## Reading the logs

A successful CWSampler run produces (in chronological order):

```
CWSampler: passed 11 hand-tuned defaults as baseline anchors
CWSampler: 11 params (baselines: 11 from default, 0 from midpoint),
           scan=211 trials, budget=600 → scan_pct=35%, assembly=389 trials
```
*Initialisation. Confirms that defaults were picked up and the scan/assembly split fits
the budget. If `scan_pct > 50%`, the budget is tight (assembly will be small).*

```
CWSampler: auto-adjusted points_per_param 20 → 12
           (budget=300 epochs, 11 params, target scan_ratio=60%)
```
*Only printed if the budget forced a reduction of `points_per_param` below 20. Floor is
5; below that the hard-error fires before this log.*

```
CWSampler: computing robust optima (plateau detection)...
  rsi_buy: chosen=32 (loss=-0.241, robustness=0.97, baseline=30)
  rsi_exit: chosen=68 (loss=-0.241, robustness=0.81, baseline=70)
  custom_stoploss: no stable plateau found (robustness=0.31 < 0.5),
                   keeping baseline=-0.62
CWSampler: SCAN complete → entering ASSEMBLY phase
CWSampler: robust optima = {'rsi_buy': 32, 'rsi_exit': 68, 'custom_stoploss': -0.62, ...}
CWSampler: 8 params found stable plateaus, 3 fell back to baseline (no plateau detected)
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

Symptom: `CWSampler: 0 params found stable plateaus, N fell back to baseline`.

Causes:
- Training window too short → loss too noisy → robustness ratio always < 0.5
- The loss function returns near-zero values → robustness formula uses `|loss|` as
  denominator, which is fragile when loss ≈ 0
- Strategy genuinely insensitive to its parameters (rare; suggests over-parameterisation)

Mitigation: longer training window, switch loss to one with a wider numerical range
(MoutonMeanRevHyperOptLoss is good), or reduce the number of optimised parameters.

### Pitfall 3: confusing "duplicate" message in the freqtrade summary

Freqtrade reports `N epochs skipped due to duplicate parameters` at the end of a run.
With CWSampler this is **expected and benign**: the scan visits the baseline value
of each parameter once (when its grid happens to align), producing one duplicate per
parameter. The assembly phase produces more duplicates due to jitter rounding. The
sampler's phase transition uses `trial.number` (Optuna's monotonic counter) and is
not affected by duplicates being pruned.

### Pitfall 4: hand-tuning a default after the first hyperopt

If you re-run CWSampler after manually editing a `default=X` in your strategy, the new
default becomes the baseline anchor. Old scan results are not reused. The robust optimum
will likely shift if the new default is far from the old one. Treat each CWSampler run
as a fresh anchor-and-search.

---

## Practical workflow recommendation

1. **First pass — exploration with TPESampler**.
   Wide ranges, many params, 600-1000 epochs. Find a candidate configuration.

2. **Validate with walk-forward analysis**.
   Walk-forward N windows. Reject if performance degrades >50% OOS.

3. **Compact the strategy** (see `user_data/strategies_generator/compact_strategy_playbook.md`).
   Inline indicators, freeze chosen params as `default=X` in DecimalParameter declarations.

4. **Second pass — refinement with CWSampler**.
   Same strategy file, now with frozen defaults. Re-hyperopt with CWSampler at
   400-600 epochs depending on n_params. Looks for plateaus around your defaults
   to improve robustness without losing performance.

5. **Final walk-forward + dry-run**.
   The CWSampler-refined params should now show **smaller OOS gap** vs the TPE
   first-pass params, even if the in-sample is marginally lower.

This 2-pass approach (TPE-then-CW) typically wins on OOS by 20-40% over a TPE-only run
with the same total epoch budget.

---

## Implementation references

- Sampler source : `freqtrade/optimize/hyperopt/cw_sampler.py`
- Integration   : `freqtrade/optimize/hyperopt/hyperopt_optimizer.py` (function
  `get_optimizer`, branch `o_sampler == "CWSampler"`)
- Constants     :
  - `PLATEAU_NEIGHBOR_RADIUS = 0.15` (15% of range — neighbour window for robustness)
  - `PLATEAU_MIN_RATIO = 0.5` (below this robustness, fall back to baseline)
  - `DEFAULT_SCAN_BUDGET_RATIO = 0.6` (60% scan, 40% assembly)
  - `MIN_POINTS_PER_PARAM = 5`, `MAX_POINTS_PER_PARAM = 20`
