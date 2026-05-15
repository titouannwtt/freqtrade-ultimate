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


class CWSampler(BaseSampler):
    """Coordinate-Wise Sampler: optimizes one parameter at a time, selects plateaus."""

    def __init__(self, seed: int = 42, points_per_param: int = 20, **kwargs):
        self._seed = seed
        self._rng = np.random.RandomState(seed)
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
        completed = study.get_trials(deepcopy=False, states=[TrialState.COMPLETE])
        n_completed = len(completed)

        if self._phase == "init":
            self._phase = "baseline"
            return

        if self._phase == "baseline" and n_completed >= 1:
            self._finalize_init()
            return

        if self._phase == "scan" and self._initialized:
            # Check if scan schedule is exhausted
            # trial_number is 0-based; trial 0 = baseline, trials 1..N = scan
            scan_trial_idx = n_completed - 1  # how many scan trials completed
            if scan_trial_idx >= len(self._schedule):
                self._transition_to_assembly(completed)

    def _finalize_init(self) -> None:
        for name, dist in self._param_distributions.items():
            self._baseline[name] = self._get_midpoint(dist)
            self._scan_grid[name] = self._make_grid(dist, self._points_per_param)

        self._build_schedule()
        self._initialized = True
        self._phase = "scan"
        logger.info(
            f"CWSampler: {len(self._param_names)} params, "
            f"scan={len(self._schedule)} trials, then assembly refinement"
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
        logger.info("CWSampler: scan complete → entering ASSEMBLY phase")
        logger.info(f"CWSampler: robust optima = {dict(self._best_robust)}")

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
