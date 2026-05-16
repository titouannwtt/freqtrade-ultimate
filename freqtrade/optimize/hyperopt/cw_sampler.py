"""
CWSampler — Coordinate-Wise Sampler for Freqtrade Hyperopt

Optimization method inspired by Eric Lefort's approach to strategy parameter tuning.
Reference: https://youtu.be/GaXngWITLSg?si=Fc1PQQtBW6VmtA0j&t=1991

Core principle: optimize parameters one at a time while keeping all others at their
default (baseline) values. For each parameter, scan its full range and look for
"plateaus" — regions where performance is stable across neighboring values — rather
than isolated peaks which indicate overfitting. After scanning all parameters
individually, assemble the best robust values and validate the combination.

Phases:
  1. BASELINE — evaluate the strategy with all parameters at their defaults (1 trial)
  2. SCAN — for each parameter, vary it across its range (grid) while fixing
     all others at baseline. Collect enough samples to measure landscape shape.
  3. PLATEAU DETECTION — for each parameter, identify the most robust optimum:
     prefer values where neighbors also perform well (plateau) over isolated peaks.
     Parameters with no stable plateau fall back to baseline.
  4. ASSEMBLY — combine the best robust value for each parameter and run
     a focused refinement (jitter ±10%) around this combined point.

Usage:
  freqtrade hyperopt --hyperopt-sampler CWSampler [--epochs N] ...

The sampler uses a deterministic grid scan: 1 baseline trial + (points_per_param × n_params)
scan trials, then all remaining epochs are assembly refinement.
"""

import logging
from collections import defaultdict
from typing import Any, Sequence

import numpy as np
from optuna.distributions import (
    BaseDistribution,
    CategoricalDistribution,
    FloatDistribution,
    IntDistribution,
)
from optuna.samplers import BaseSampler
from optuna.study import Study
from optuna.trial import FrozenTrial, TrialState


logger = logging.getLogger(__name__)

PLATEAU_NEIGHBOR_RADIUS = 0.15
PLATEAU_MIN_RATIO = 0.5

# Default scan/total ratio: how much of the epoch budget to spend in scan vs assembly.
# 0.6 = 60% scan, 40% assembly. Empirically: assembly needs ≥30% for plateau refinement
# to be meaningful, and scan needs ≥50% for plateau detection to have enough points
# per param. 0.6 is the sweet spot.
DEFAULT_SCAN_BUDGET_RATIO = 0.6
# Minimum points per param to make plateau detection meaningful (15% neighbour radius
# requires at least ~7 points to have neighbours, ≥5 is the absolute floor).
MIN_POINTS_PER_PARAM = 5
# Hard cap on points per param — above 20, the marginal cost outweighs grid density gain
# (the plateau radius is 15% of range so >20 points gives <1.5% precision per point).
MAX_POINTS_PER_PARAM = 20


class CWSampler(BaseSampler):
    """Coordinate-Wise Sampler: optimizes one parameter at a time, selects plateaus.

    Args:
        seed: RNG seed for reproducibility.
        points_per_param: scan grid points per parameter. If `total_epochs` is given,
            this is auto-adjusted to fit within the scan budget (recommended).
            Defaults to 20 if total_epochs is not provided.
        total_epochs: Total epoch budget for the hyperopt. If provided, the sampler
            self-adjusts `points_per_param` so that scan + assembly fits within this
            budget (scan = total_epochs × 0.6, assembly = remaining ~40%). When using
            CWSampler, freqtrade auto-passes this from --epochs.
        defaults: Optional dict {param_name: default_value}. Used as the baseline
            (anchor point for the scan). If not provided, falls back to the midpoint
            of each parameter's range — but this often gives suboptimal results since
            the midpoint may be far from the hand-tuned default. Strongly recommended
            to provide hand-tuned defaults when known.
    """

    def __init__(
        self,
        seed: int = 42,
        points_per_param: int = 20,
        total_epochs: int | None = None,
        defaults: dict[str, Any] | None = None,
        scan_budget_ratio: float = DEFAULT_SCAN_BUDGET_RATIO,
        **kwargs,
    ):
        self._seed = seed
        self._rng = np.random.RandomState(seed)
        self._points_per_param_requested = points_per_param
        self._total_epochs = total_epochs
        self._scan_budget_ratio = scan_budget_ratio
        self._user_defaults = defaults or {}

        # Final points_per_param is computed in _finalize_init based on total_epochs
        # and n_params discovered. Start with the user request.
        self._points_per_param = points_per_param

        self._param_names: list[str] = []
        self._param_distributions: dict[str, BaseDistribution] = {}
        self._baseline: dict[str, Any] = {}
        self._scan_grid: dict[str, list[Any]] = {}
        self._best_robust: dict[str, Any] = {}

        self._phase = "init"
        self._schedule: list[dict[str, Any]] = []
        self._initialized = False
        self._last_seen_trial: int = -1
        self._assembly_entered_logged = False

    # ── Optuna interface ──────────────────────────────────────────────

    def infer_relative_search_space(
        self, study: Study, trial: FrozenTrial
    ) -> dict[str, BaseDistribution]:
        return {}

    def sample_relative(
        self, study: Study, trial: FrozenTrial, search_space: dict[str, BaseDistribution]
    ) -> dict[str, Any]:
        return {}

    def sample_independent(
        self, study: Study, trial: FrozenTrial, param_name: str,
        param_distribution: BaseDistribution,
    ) -> Any:
        self._discover_param(param_name, param_distribution)

        # Only update state once per trial (first param call)
        if trial.number != self._last_seen_trial:
            self._last_seen_trial = trial.number
            self._update_phase(study)

        if self._phase in ("init", "baseline"):
            return self._baseline.get(param_name, self._get_midpoint(param_distribution))
        if self._phase == "scan":
            return self._sample_scan(trial.number, param_name, param_distribution)
        return self._sample_assembly(param_name, param_distribution)

    def after_trial(
        self, study: Study, trial: FrozenTrial,
        state: TrialState, values: Sequence[float] | None,
    ) -> None:
        pass

    def reseed_rng(self) -> None:
        self._rng = np.random.RandomState(self._rng.randint(0, 2**31))

    # ── Param discovery ───────────────────────────────────────────────

    def _discover_param(self, name: str, dist: BaseDistribution) -> None:
        if name not in self._param_distributions:
            self._param_distributions[name] = dist
            self._param_names.append(name)

    # ── Phase management ──────────────────────────────────────────────

    def _update_phase(self, study: Study) -> None:
        # 2026-05-16 fix — count COMPLETE + PRUNED trials, not just COMPLETE.
        # Freqtrade prunes duplicate-parameter trials (sets state=PRUNED) and
        # those weren't counted, which blocked the scan→assembly transition
        # forever when duplicates occurred (which happens systematically with
        # CWSampler scan, since each scan trial = baseline + 1 varied param,
        # producing dup with baseline when grid value == baseline). Now we
        # count ALL attempted trials regardless of dedup outcome.
        all_attempted = study.get_trials(
            deepcopy=False,
            states=[TrialState.COMPLETE, TrialState.PRUNED, TrialState.FAIL],
        )
        completed = [t for t in all_attempted if t.state == TrialState.COMPLETE]
        n_attempted = len(all_attempted)

        if self._phase == "init":
            self._phase = "baseline"
            return

        if self._phase == "baseline" and n_attempted >= 1:
            self._finalize_init()
            return

        if self._phase == "scan" and self._initialized:
            # Check if scan schedule is exhausted (count ALL attempted, not just complete)
            scan_trial_idx = n_attempted - 1
            if scan_trial_idx >= len(self._schedule):
                self._transition_to_assembly(completed)

    def _finalize_init(self) -> None:
        n_params = len(self._param_distributions)

        # Self-budget: if total_epochs is known, adjust points_per_param to fit budget
        if self._total_epochs is not None and n_params > 0:
            scan_budget = max(n_params * MIN_POINTS_PER_PARAM,
                              int(self._total_epochs * self._scan_budget_ratio))
            auto_ppp = max(MIN_POINTS_PER_PARAM,
                           min(MAX_POINTS_PER_PARAM, scan_budget // n_params))
            if auto_ppp != self._points_per_param_requested:
                logger.info(
                    f"CWSampler: auto-adjusted points_per_param "
                    f"{self._points_per_param_requested} → {auto_ppp} "
                    f"(budget={self._total_epochs} epochs, {n_params} params, "
                    f"target scan_ratio={self._scan_budget_ratio:.0%})"
                )
            self._points_per_param = auto_ppp

        # Build baseline (defaults if provided, else midpoint) and scan grids
        baseline_source: dict[str, str] = {}
        for name, dist in self._param_distributions.items():
            if name in self._user_defaults:
                # Clip the user default to the dist range to avoid grid-build errors
                self._baseline[name] = self._clip_to_dist(self._user_defaults[name], dist)
                baseline_source[name] = "default"
            else:
                self._baseline[name] = self._get_midpoint(dist)
                baseline_source[name] = "midpoint"
            self._scan_grid[name] = self._make_grid(dist, self._points_per_param)

        self._build_schedule()
        self._initialized = True
        self._phase = "scan"

        # Diagnostic logging
        n_from_default = sum(1 for v in baseline_source.values() if v == "default")
        n_from_midpoint = n_params - n_from_default
        budget_msg = ""
        if self._total_epochs is not None:
            assembly_target = self._total_epochs - len(self._schedule)
            budget_msg = (
                f", budget={self._total_epochs} → "
                f"scan_pct={len(self._schedule)/max(1,self._total_epochs):.0%}, "
                f"assembly={max(0, assembly_target)} trials"
            )
            if assembly_target < n_params:
                logger.error(
                    f"CWSampler: assembly budget too low ({assembly_target} trials < "
                    f"{n_params} params). Increase --epochs or accept that the assembly "
                    f"phase will be skipped (results will be midpoint-anchored)."
                )
        logger.info(
            f"CWSampler: {n_params} params (baselines: {n_from_default} from default, "
            f"{n_from_midpoint} from midpoint), scan={len(self._schedule)} trials"
            f"{budget_msg}"
        )

    # ── Schedule ──────────────────────────────────────────────────────

    def _build_schedule(self) -> None:
        self._schedule = []
        for pname in self._param_names:
            grid = self._scan_grid[pname]
            for grid_val in grid:
                trial_params = dict(self._baseline)
                trial_params[pname] = grid_val
                self._schedule.append(trial_params)

    def _sample_scan(
        self, trial_number: int, param_name: str, param_distribution: BaseDistribution
    ) -> Any:
        # trial 0 = baseline (already handled), scan starts at trial 1
        scan_idx = trial_number - 1
        if 0 <= scan_idx < len(self._schedule):
            return self._schedule[scan_idx].get(
                param_name, self._baseline.get(param_name)
            )
        # Past schedule — transition happened between update_phase and here
        return self._sample_assembly(param_name, param_distribution)

    # ── Scan → Assembly transition ────────────────────────────────────

    def _transition_to_assembly(self, completed_trials: list[FrozenTrial]) -> None:
        if self._phase == "assembly":
            return

        self._compute_robust_optima(completed_trials)
        self._phase = "assembly"
        if not self._assembly_entered_logged:
            self._assembly_entered_logged = True
            logger.info("=" * 70)
            logger.info("CWSampler: SCAN complete → entering ASSEMBLY phase")
            logger.info(f"CWSampler: robust optima = {dict(self._best_robust)}")
            # Per-param summary: was the chosen value default or baseline-fallback?
            n_fallback = 0
            for pname, chosen in self._best_robust.items():
                if chosen == self._baseline.get(pname):
                    n_fallback += 1
            logger.info(
                f"CWSampler: {len(self._best_robust) - n_fallback} params found stable "
                f"plateaus, {n_fallback} fell back to baseline (no plateau detected)"
            )
            logger.info("=" * 70)

    # ── Plateau detection ─────────────────────────────────────────────

    def _compute_robust_optima(self, completed_trials: list[FrozenTrial]) -> None:
        logger.info("CWSampler: computing robust optima (plateau detection)...")

        # Build per-parameter results from scan trials (skip baseline = trial 0)
        scan_results: dict[str, list[tuple[Any, float]]] = defaultdict(list)
        for t in completed_trials:
            if t.number == 0 or t.values is None:
                continue
            scan_idx = t.number - 1
            if scan_idx < 0 or scan_idx >= len(self._schedule):
                continue
            # Find which param was varied in this scheduled trial
            scheduled = self._schedule[scan_idx]
            for pname in self._param_names:
                if scheduled[pname] != self._baseline.get(pname):
                    scan_results[pname].append((scheduled[pname], t.values[0]))
                    break
            else:
                # All at baseline — record for all params
                for pname in self._param_names:
                    val = t.params.get(pname, self._baseline.get(pname))
                    scan_results[pname].append((val, t.values[0]))

        for pname in self._param_names:
            results = scan_results.get(pname, [])
            if not results:
                self._best_robust[pname] = self._baseline.get(pname, 0)
                continue

            dist = self._param_distributions.get(pname)
            if isinstance(dist, CategoricalDistribution):
                self._best_robust[pname] = self._best_categorical(results)
            else:
                self._best_robust[pname] = self._best_plateau(pname, results)

    def _best_categorical(self, results: list[tuple[Any, float]]) -> Any:
        by_val: dict[Any, list[float]] = defaultdict(list)
        for val, loss in results:
            by_val[val].append(loss)
        avg = {v: float(np.mean(losses)) for v, losses in by_val.items()}
        return min(avg, key=avg.get)

    def _best_plateau(self, pname: str, results: list[tuple[Any, float]]) -> Any:
        if len(results) < 3:
            best_idx = int(np.argmin([r[1] for r in results]))
            return results[best_idx][0]

        results_sorted = sorted(results, key=lambda x: float(x[0]))
        values = [float(r[0]) for r in results_sorted]
        losses = [float(r[1]) for r in results_sorted]

        param_range = values[-1] - values[0]
        if param_range == 0:
            return results_sorted[0][0]

        radius = param_range * PLATEAU_NEIGHBOR_RADIUS
        robustness_scores = []
        for i in range(len(values)):
            neighbor_losses = [
                losses[j] for j in range(len(values))
                if j != i and abs(values[j] - values[i]) <= radius
            ]
            if not neighbor_losses:
                robustness_scores.append(0.0)
                continue
            neighbor_mean = float(np.mean(neighbor_losses))
            denom = abs(losses[i]) + 1e-10
            ratio = 1.0 - abs(neighbor_mean - losses[i]) / denom
            robustness_scores.append(max(0.0, min(1.0, ratio)))

        best_loss = min(losses)
        worst_loss = max(losses)
        loss_range = worst_loss - best_loss if worst_loss != best_loss else 1.0

        best_combined = -1.0
        best_idx = 0
        for i in range(len(values)):
            performance = 1.0 - (losses[i] - best_loss) / loss_range
            combined = 0.5 * performance + 0.5 * robustness_scores[i]
            if combined > best_combined:
                best_combined = combined
                best_idx = i

        chosen_val = results_sorted[best_idx][0]
        chosen_robustness = robustness_scores[best_idx]

        logger.info(
            f"  {pname}: chosen={chosen_val} (loss={losses[best_idx]:.4f}, "
            f"robustness={chosen_robustness:.2f}, baseline={self._baseline.get(pname)})"
        )

        if chosen_robustness < PLATEAU_MIN_RATIO:
            logger.warning(
                f"  {pname}: no stable plateau found "
                f"(robustness={chosen_robustness:.2f} < {PLATEAU_MIN_RATIO}), "
                f"keeping baseline={self._baseline.get(pname)}"
            )
            return self._baseline.get(pname, chosen_val)

        return chosen_val

    # ── Assembly sampling ─────────────────────────────────────────────

    def _sample_assembly(self, param_name: str, param_distribution: BaseDistribution) -> Any:
        center = self._best_robust.get(param_name)
        if center is None:
            return self._get_midpoint(param_distribution)

        if isinstance(param_distribution, CategoricalDistribution):
            if self._rng.random() < 0.3:
                idx = self._rng.randint(0, len(param_distribution.choices))
                return param_distribution.choices[idx]
            return center

        if isinstance(param_distribution, IntDistribution):
            low, high = param_distribution.low, param_distribution.high
            spread = max(1, int((high - low) * 0.1))
            jitter_low = max(low, int(center) - spread)
            jitter_high = min(high, int(center) + spread)
            return int(self._rng.randint(jitter_low, jitter_high + 1))

        if isinstance(param_distribution, FloatDistribution):
            low, high = param_distribution.low, param_distribution.high
            spread = (high - low) * 0.1
            jitter_low = max(low, float(center) - spread)
            jitter_high = min(high, float(center) + spread)
            val = self._rng.uniform(jitter_low, jitter_high)
            if param_distribution.step and param_distribution.step > 0:
                val = round(val / param_distribution.step) * param_distribution.step
                val = max(low, min(high, val))
            return val

        return center

    # ── Utilities ─────────────────────────────────────────────────────

    @staticmethod
    def _get_midpoint(dist: BaseDistribution) -> Any:
        if isinstance(dist, IntDistribution):
            return (dist.low + dist.high) // 2
        if isinstance(dist, FloatDistribution):
            mid = (dist.low + dist.high) / 2
            if dist.step and dist.step > 0:
                return round(mid / dist.step) * dist.step
            return mid
        if isinstance(dist, CategoricalDistribution):
            return dist.choices[len(dist.choices) // 2]
        return 0

    @staticmethod
    def _clip_to_dist(value: Any, dist: BaseDistribution) -> Any:
        """Clip a user-provided default to the distribution's valid range.

        If the value is outside [low, high] for numeric dists, clamp it.
        For categoricals, fall back to midpoint if value not in choices.
        For float dists with step, snap to nearest grid point.
        """
        if isinstance(dist, IntDistribution):
            v = int(value)
            return max(dist.low, min(dist.high, v))
        if isinstance(dist, FloatDistribution):
            v = float(value)
            v = max(dist.low, min(dist.high, v))
            if dist.step and dist.step > 0:
                v = round(v / dist.step) * dist.step
                v = max(dist.low, min(dist.high, v))
            return v
        if isinstance(dist, CategoricalDistribution):
            if value in dist.choices:
                return value
            logger.warning(
                f"CWSampler: default value {value!r} not in categorical choices "
                f"{list(dist.choices)}, falling back to midpoint"
            )
            return dist.choices[len(dist.choices) // 2]
        return value

    @staticmethod
    def _make_grid(dist: BaseDistribution, n_points: int) -> list[Any]:
        if isinstance(dist, CategoricalDistribution):
            return list(dist.choices)

        if isinstance(dist, IntDistribution):
            low, high = dist.low, dist.high
            step = dist.step if dist.step else 1
            all_vals = list(range(low, high + 1, step))
            if len(all_vals) <= n_points:
                return all_vals
            indices = np.linspace(0, len(all_vals) - 1, n_points, dtype=int)
            return sorted(set(all_vals[i] for i in indices))

        if isinstance(dist, FloatDistribution):
            low, high = dist.low, dist.high
            if dist.step and dist.step > 0:
                all_vals = []
                v = low
                while v <= high + 1e-10:
                    all_vals.append(round(v, 10))
                    v += dist.step
                if len(all_vals) <= n_points:
                    return all_vals
                indices = np.linspace(0, len(all_vals) - 1, n_points, dtype=int)
                return sorted(set(all_vals[i] for i in indices))
            return [round(low + (high - low) * i / max(1, n_points - 1), 10)
                    for i in range(n_points)]

        return [CWSampler._get_midpoint(dist)]
