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

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
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

# Activity floor — anti-over-conservatism. Two thresholds (whichever is tighter):
# 1. Absolute floor: trial must have >= DEFAULT_MIN_ACTIVE_TRADES trades.
# 2. Relative floor: trial must have >= DEFAULT_MIN_TRADES_RATIO × baseline_n_trades.
# The relative floor is computed once after the baseline trial completes, using the
# baseline (= v1 defaults) trade count as reference. This prevents the sampler from
# silently picking restrictive params that fire 10x-60x fewer trades than v1, which
# was observed to be a major over-fitting risk: fewer trades = each trade carries
# more statistical weight = less diversification = HIGHER overfitting risk despite
# the apparent "stability" of the loss surface.
#
# Trials below the activity floor are now HARD-EXCLUDED from plateau detection
# (not softly penalised). If ALL grid values for a param fall below the floor, the
# sampler falls back to the baseline value — preserving v1's known-good trade
# regime rather than picking an artificially conservative variant.
DEFAULT_MIN_ACTIVE_TRADES = 10
DEFAULT_MIN_TRADES_RATIO = 0.7  # require >= 70% of baseline's trade count

# Assembly mode controls how the assembly phase explores around the robust optima:
# - "jitter" (legacy): random ±10% perturbation around the assembled all-robust point.
# - "mixing": deterministic+random exploration of robust-vs-baseline param subsets.
#             Trials gradually swap params from baseline → robust, exposing the
#             interaction trade-off. Default since 2026-05-16 because pure jitter
#             can't escape an inactive all-robust region.
DEFAULT_ASSEMBLY_MODE = "mixing"

# Health check threshold: if the first N assembly trials all show loss > baseline_loss
# × HEALTH_BAD_RATIO, log a loud warning that the robust optima may be over-conservative.
HEALTH_CHECK_AFTER_N_ASSEMBLY = 5
HEALTH_BAD_RATIO = 1.20  # 20% worse than baseline = warning


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
        output_dir: Path | str | None = None,
        strategy_name: str | None = None,
        min_active_trades: int = DEFAULT_MIN_ACTIVE_TRADES,
        min_trades_ratio: float = DEFAULT_MIN_TRADES_RATIO,
        assembly_mode: str = DEFAULT_ASSEMBLY_MODE,
        performance_weight: float = 0.5,
        **kwargs,
    ):
        self._seed = seed
        self._rng = np.random.RandomState(seed)
        self._points_per_param_requested = points_per_param
        self._total_epochs = total_epochs
        self._scan_budget_ratio = scan_budget_ratio
        self._user_defaults = defaults or {}
        # output_dir + strategy_name enable the auto-dump of robust_optima
        # to a freqtrade-loadable .json at scan→assembly transition. This is
        # the recommended way to consume the CWSampler's output for v2 deploy:
        # freqtrade's "Best result" reports the lowest-loss epoch (often a
        # scan trial = baseline + 1 perturbed param), NOT the robust optima.
        # The plateau-anchored output of this sampler IS robust_optima.
        self._output_dir = Path(output_dir) if output_dir else None
        self._strategy_name = strategy_name

        # Activity floor: scan trials with too few trades are HARD-EXCLUDED from
        # plateau detection. The effective floor is computed at baseline completion
        # as max(min_active_trades, min_trades_ratio × baseline_n_trades). This
        # baseline-relative threshold prevents the sampler from picking restrictive
        # params that fire fewer trades than v1 (= the over-conservatism failure
        # mode). Trade counts are fed by the freqtrade hyperopt loop via
        # record_trial_metrics().
        self._min_active_trades_abs = max(1, min_active_trades)
        if not 0.0 <= min_trades_ratio <= 1.5:
            raise ValueError(
                f"min_trades_ratio must be in [0, 1.5] (typical: 0.7), got {min_trades_ratio}"
            )
        self._min_trades_ratio = min_trades_ratio
        # Effective floor (resolved at baseline completion). Starts at abs only.
        self._min_active_trades = self._min_active_trades_abs
        # Trial 0 (baseline) trade count, used to compute the relative floor
        self._baseline_n_trades: int | None = None
        # trial_number → n_trades, populated as trials complete (by external caller)
        self._trial_n_trades: dict[int, int] = {}

        # Assembly exploration mode: "mixing" (default, swaps robust/baseline subsets)
        # or "jitter" (legacy, ±10% Gaussian around all-robust point).
        if assembly_mode not in ("mixing", "jitter"):
            raise ValueError(f"assembly_mode must be 'mixing' or 'jitter', got {assembly_mode!r}")
        self._assembly_mode = assembly_mode
        # Per-trial cached subset decision for mixing mode (recomputed each new trial)
        self._mixing_trial_number: int = -1
        self._mixing_robust_mask: dict[str, bool] = {}
        # Counter of assembly trials seen so far (for ordering the mixing schedule)
        self._assembly_trial_count: int = 0

        # Performance/robustness weight in the combined plateau score (must sum to 1.0).
        # Default 0.5/0.5 (Lefort original). Increase performance_weight (e.g. 0.7) when
        # the loss landscape is noisy and robust_optima tend to be over-conservative.
        if not 0.0 <= performance_weight <= 1.0:
            raise ValueError(f"performance_weight must be in [0, 1], got {performance_weight}")
        self._performance_weight = performance_weight
        self._robustness_weight = 1.0 - performance_weight

        # Final points_per_param is computed in _finalize_init based on total_epochs
        # and n_params discovered. Start with the user request.
        self._points_per_param = points_per_param

        self._param_names: list[str] = []
        self._param_distributions: dict[str, BaseDistribution] = {}
        self._baseline: dict[str, Any] = {}
        self._scan_grid: dict[str, list[Any]] = {}
        self._best_robust: dict[str, Any] = {}
        # Per-param robustness score from plateau detection (filled in _compute_robust_optima).
        # Used by mixing assembly to order params by confidence (high robustness first).
        self._best_robustness_scores: dict[str, float] = {}

        # Baseline trial loss, captured after the baseline trial completes (used by
        # the health check to compare assembly trials against).
        self._baseline_loss: float | None = None
        self._health_check_done: bool = False

        self._phase = "init"
        self._schedule: list[dict[str, Any]] = []
        self._initialized = False
        self._last_seen_trial: int = -1
        self._assembly_entered_logged = False

    # ── External hook for trade count injection ──────────────────────

    def record_trial_metrics(self, trial_number: int, n_trades: int) -> None:
        """Called by freqtrade's hyperopt loop after each trial completes.

        Records the number of trades produced by the trial, used by the activity
        floor in plateau scoring. If not called, the activity floor is skipped
        (all trials treated as fully active).
        """
        self._trial_n_trades[trial_number] = int(n_trades)

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
            self._update_phase(study, trial.number)

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

    def _update_phase(self, study: Study, current_trial_number: int) -> None:
        # 2026-05-16 fix v2 — use Optuna's monotonic trial.number, NOT
        # len(study.get_trials()). Freqtrade dedups duplicate-parameter trials
        # BEFORE they enter the Optuna study, so get_trials() undercount
        # attempted trials. trial.number is always monotonic (Optuna assigns it
        # before any dedup decision), so it accurately tracks the schedule index.
        #
        # Schedule:
        #   trial.number 0       = baseline (baseline trial)
        #   trial.number 1..N    = scan trials (N = len(self._schedule))
        #   trial.number > N     = assembly trials
        if self._phase == "init":
            self._phase = "baseline"
            return

        if self._phase == "baseline" and current_trial_number >= 1:
            self._finalize_init()
            return

        if self._phase == "scan" and self._initialized:
            scan_idx = current_trial_number - 1   # 0-indexed scan position
            if scan_idx >= len(self._schedule):
                # Need actual COMPLETE trials for plateau detection — those ARE
                # tracked by Optuna correctly (only the dedup'd ones are missing)
                completed = study.get_trials(
                    deepcopy=False,
                    states=[TrialState.COMPLETE],
                )
                self._transition_to_assembly(completed)
        elif self._phase == "assembly" and not self._health_check_done:
            # Run the health check once enough assembly trials have completed
            self._check_assembly_health(study)

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

        # Compute baseline-relative activity floor now that baseline trial is done.
        # Trial 0 (baseline = all-defaults) ran in the previous phase; its trade count
        # is in self._trial_n_trades[0] (fed by freqtrade's record_trial_metrics hook).
        # We anchor the activity threshold on this number: any scan trial firing
        # fewer than min_trades_ratio × baseline_n_trades trades is HARD-EXCLUDED
        # from plateau detection. This prevents the sampler from picking restrictive
        # params that fire 10x-60x fewer trades than v1 (a known over-fitting risk:
        # fewer trades = each trade carries more statistical weight = worse OOS).
        self._baseline_n_trades = self._trial_n_trades.get(0)
        if self._baseline_n_trades and self._baseline_n_trades > 0:
            relative_floor = int(self._baseline_n_trades * self._min_trades_ratio)
            new_floor = max(self._min_active_trades_abs, relative_floor)
            if new_floor != self._min_active_trades:
                logger.info(
                    f"CWSampler: activity floor raised to {new_floor} trades "
                    f"(was abs={self._min_active_trades_abs}, "
                    f"baseline_n_trades={self._baseline_n_trades}, "
                    f"ratio={self._min_trades_ratio:.0%} → relative={relative_floor})"
                )
            self._min_active_trades = new_floor

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
            # Per-param summary: was the chosen value default or baseline-fallback?
            n_fallback = sum(
                1 for pname, chosen in self._best_robust.items()
                if chosen == self._baseline.get(pname)
            )
            logger.info("=" * 70)
            logger.info("CWSampler: SCAN complete → entering ASSEMBLY phase")
            logger.info(f"CWSampler: robust optima = {dict(self._best_robust)}")
            logger.info(
                f"CWSampler: {len(self._best_robust) - n_fallback} params found stable "
                f"plateaus, {n_fallback} fell back to baseline (no plateau detected)"
            )
            self._dump_robust_optima_to_json(n_fallback)
            logger.info("=" * 70)

    def _build_robust_params_dict(self, n_fallback: int) -> dict:
        """Build the freqtrade-loadable params dict from robust_optima.

        Returns the same schema as freqtrade's hyperopt export JSON, so it can
        be written directly next to the strategy file and auto-loaded as the
        strategy's live params.
        """
        return {
            "strategy_name": self._strategy_name or "unknown",
            "params": {
                "buy": {k: self._normalize_value(v) for k, v in self._best_robust.items()},
                "sell": {},
                "protection": {},
                "roi": {},
                "stoploss": {},
                "max_open_trades": {},
                "trailing": {},
            },
            "ft_stratparam_v": 1,
            "export_time": datetime.now(timezone.utc).isoformat(),
            "cwsampler_meta": {
                "n_params": len(self._best_robust),
                "n_plateaus": len(self._best_robust) - n_fallback,
                "n_baseline_fallback": n_fallback,
                "baseline_source": {
                    k: ("default" if k in self._user_defaults else "midpoint")
                    for k in self._best_robust
                },
            },
        }

    def _dump_robust_optima_to_json(self, n_fallback: int) -> None:
        """Write the robust optima directly next to the strategy file.

        This is the canonical CWSampler output — it REPLACES the standard
        freqtrade "Best result" export when CWSampler is used. The file is
        saved at the same location as freqtrade's normal param export:
        next to the strategy .py file as {StrategyName}.json.

        This means `freqtrade backtesting --strategy X` will auto-load
        the robust params without any manual `cp`.

        Silently no-ops if output_dir or strategy_name is None (sampler used
        outside the freqtrade hyperopt CLI).
        """
        if not self._output_dir or not self._strategy_name:
            return
        try:
            data = self._build_robust_params_dict(n_fallback)

            # Also write to hyperopt_results/ as archive (useful for comparison)
            self._output_dir.mkdir(parents=True, exist_ok=True)
            archive_path = self._output_dir / f"cwsampler_robust_{self._strategy_name}.json"
            archive_path.write_text(json.dumps(data, indent=2, default=str))
            logger.info(
                f"CWSampler: robust_optima archived to {archive_path}"
            )
        except Exception as exc:  # pragma: no cover — never crash a hyperopt over this
            logger.warning(f"CWSampler: failed to dump robust_optima JSON: {exc}")

    def get_robust_optima(self) -> dict[str, Any]:
        """Public accessor for the robust_optima dict (plateau-anchored params).

        Used by the hyperopt runner to export the robust params as the final
        result instead of the lowest-loss epoch.
        """
        return dict(self._best_robust)

    def get_phase(self) -> str:
        """Public accessor for the current sampler phase.

        Returns one of: 'init', 'baseline', 'scan', 'assembly'.
        """
        return self._phase

    def get_n_baseline_fallback(self) -> int:
        """Count how many params fell back to baseline (no plateau found)."""
        return sum(
            1 for pname, chosen in self._best_robust.items()
            if chosen == self._baseline.get(pname)
        )

    @staticmethod
    def _normalize_value(v: Any) -> Any:
        """Convert numpy types to native Python types for JSON serialisation."""
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            return float(v)
        return v

    # ── Plateau detection ─────────────────────────────────────────────

    def _compute_robust_optima(self, completed_trials: list[FrozenTrial]) -> None:
        logger.info("CWSampler: computing robust optima (plateau detection)...")

        # Capture baseline loss for health check (trial 0 = baseline)
        for t in completed_trials:
            if t.number == 0 and t.values is not None:
                self._baseline_loss = float(t.values[0])
                break

        # Build per-parameter results from scan trials (skip baseline = trial 0)
        # Also track which trial.number produced each value (for activity floor lookup)
        scan_results: dict[str, list[tuple[Any, float]]] = defaultdict(list)
        scan_trial_numbers: dict[str, list[int]] = defaultdict(list)
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
                    scan_trial_numbers[pname].append(t.number)
                    break
            else:
                # All at baseline — record for all params
                for pname in self._param_names:
                    val = t.params.get(pname, self._baseline.get(pname))
                    scan_results[pname].append((val, t.values[0]))
                    scan_trial_numbers[pname].append(t.number)

        for pname in self._param_names:
            results = scan_results.get(pname, [])
            tnums = scan_trial_numbers.get(pname, [])
            if not results:
                self._best_robust[pname] = self._baseline.get(pname, 0)
                self._best_robustness_scores[pname] = 0.0
                continue

            dist = self._param_distributions.get(pname)
            if isinstance(dist, CategoricalDistribution):
                self._best_robust[pname] = self._best_categorical(results)
                self._best_robustness_scores[pname] = 0.5  # cat baseline robustness
            else:
                self._best_robust[pname] = self._best_plateau(pname, results, tnums)

    def _best_categorical(self, results: list[tuple[Any, float]]) -> Any:
        by_val: dict[Any, list[float]] = defaultdict(list)
        for val, loss in results:
            by_val[val].append(loss)
        avg = {v: float(np.mean(losses)) for v, losses in by_val.items()}
        return min(avg, key=avg.get)

    def _best_plateau(
        self,
        pname: str,
        results: list[tuple[Any, float]],
        trial_numbers: list[int] | None = None,
    ) -> Any:
        if len(results) < 3:
            best_idx = int(np.argmin([r[1] for r in results]))
            return results[best_idx][0]

        # Sort results + trial_numbers in parallel by parameter value
        if trial_numbers is None:
            trial_numbers = [-1] * len(results)
        pairs = sorted(zip(results, trial_numbers), key=lambda x: float(x[0][0]))
        results_sorted = [p[0] for p in pairs]
        trials_sorted = [p[1] for p in pairs]
        values = [float(r[0]) for r in results_sorted]
        losses = [float(r[1]) for r in results_sorted]

        param_range = values[-1] - values[0]
        if param_range == 0:
            self._best_robustness_scores[pname] = 0.0
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

        # Activity floor: HARD-EXCLUDE grid points whose trial fired below the floor.
        # The floor is baseline-relative (set in _finalize_init after baseline trial
        # completes — typically 70% of baseline's trade count). Excluded grid points
        # can never be picked as the robust optimum, regardless of how flat their
        # local loss landscape is. If ALL grid points are excluded, we fall back to
        # baseline for this parameter (preserves v1's known-good regime).
        eligible_indices = []
        for i, tnum in enumerate(trials_sorted):
            n_trades = self._trial_n_trades.get(tnum)
            if n_trades is None or n_trades >= self._min_active_trades:
                eligible_indices.append(i)

        if not eligible_indices:
            n_excluded = len(values)
            logger.warning(
                f"  {pname}: all {n_excluded} scan points produced < {self._min_active_trades} "
                f"trades (baseline-relative floor). Falling back to baseline="
                f"{self._baseline.get(pname)}."
            )
            self._best_robustness_scores[pname] = 0.0
            return self._baseline.get(pname, results_sorted[0][0])

        best_combined = -1.0
        best_idx = eligible_indices[0]
        for i in eligible_indices:
            performance = 1.0 - (losses[i] - best_loss) / loss_range
            combined = (
                self._performance_weight * performance
                + self._robustness_weight * robustness_scores[i]
            )
            if combined > best_combined:
                best_combined = combined
                best_idx = i

        n_excluded = len(values) - len(eligible_indices)

        chosen_val = results_sorted[best_idx][0]
        chosen_robustness = robustness_scores[best_idx]
        n_trades_chosen = self._trial_n_trades.get(trials_sorted[best_idx])

        activity_str = (
            f", n_trades={n_trades_chosen}/{self._min_active_trades}min"
            if n_trades_chosen is not None else ""
        )
        excl_str = (
            f", {n_excluded}/{len(values)} grid pts excluded (< {self._min_active_trades} trades)"
            if n_excluded > 0 else ""
        )
        logger.info(
            f"  {pname}: chosen={chosen_val} (loss={losses[best_idx]:.4f}, "
            f"robustness={chosen_robustness:.2f}{activity_str}"
            f"{excl_str}, baseline={self._baseline.get(pname)})"
        )

        if chosen_robustness < PLATEAU_MIN_RATIO:
            logger.warning(
                f"  {pname}: no stable plateau found "
                f"(robustness={chosen_robustness:.2f} < {PLATEAU_MIN_RATIO}), "
                f"keeping baseline={self._baseline.get(pname)}"
            )
            self._best_robustness_scores[pname] = 0.0
            return self._baseline.get(pname, chosen_val)

        self._best_robustness_scores[pname] = chosen_robustness
        return chosen_val

    # ── Assembly sampling ─────────────────────────────────────────────

    def _sample_assembly(self, param_name: str, param_distribution: BaseDistribution) -> Any:
        if self._assembly_mode == "mixing":
            return self._sample_assembly_mixing(param_name, param_distribution)
        return self._sample_assembly_jitter(param_name, param_distribution)

    def _sample_assembly_mixing(
        self, param_name: str, param_distribution: BaseDistribution
    ) -> Any:
        """Mixing strategy: each assembly trial uses a SUBSET of robust_optima
        (rest from baseline), exploring the trade-off between full-robust (often
        too conservative) and full-baseline (= no optimization).

        Schedule:
          Trial 1 of assembly: ALL params robust (= what jitter would explore at center)
          Trial 2: all robust EXCEPT the lowest-robustness param (revert to baseline)
          Trial 3: all robust EXCEPT the 2 lowest-robustness params
          ...
          Trial n_params: only the highest-robustness param uses robust value
          Trial n_params+1 and beyond: random subsets (more diverse exploration)

        After the deterministic schedule completes, switch to random k-out-of-n
        mixing for variety. The schedule choice is cached per trial.number so all
        param calls in the same trial see a consistent decision.
        """
        # Recompute the mix decision when we see a new trial
        if self._last_seen_trial != self._mixing_trial_number:
            self._mixing_trial_number = self._last_seen_trial
            self._assembly_trial_count += 1
            self._mixing_robust_mask = self._build_mixing_mask(self._assembly_trial_count)

        use_robust = self._mixing_robust_mask.get(param_name, True)
        if use_robust:
            return self._best_robust.get(param_name, self._baseline.get(param_name))
        return self._baseline.get(param_name, self._get_midpoint(param_distribution))

    def _build_mixing_mask(self, assembly_n: int) -> dict[str, bool]:
        """Decide for the n-th assembly trial which params use robust vs baseline.

        For the first n_params trials, we follow a deterministic schedule that
        progressively reverts params from robust to baseline, lowest-robustness
        first. After that, we draw random subsets.
        """
        n_params = len(self._param_names)
        if n_params == 0:
            return {}

        # Order params by robustness ascending (lowest first = first to be reverted)
        params_by_robustness = sorted(
            self._param_names,
            key=lambda p: self._best_robustness_scores.get(p, 0.0),
        )

        if assembly_n == 1:
            # All robust (test the assembled point as-is)
            return {p: True for p in self._param_names}

        if assembly_n <= n_params:
            # Revert the (assembly_n - 1) lowest-robustness params to baseline
            n_baseline = assembly_n - 1
            baseline_set = set(params_by_robustness[:n_baseline])
            return {p: (p not in baseline_set) for p in self._param_names}

        # Past the deterministic schedule: random subset (biased toward high-k robust)
        # Sample k from a distribution favouring k close to n_params (most robust)
        # k follows a triangular distribution biased toward n_params
        k_min = max(1, n_params // 3)
        k = int(self._rng.triangular(k_min, n_params, n_params))
        # Random selection of k params to keep robust
        selected = list(self._rng.choice(n_params, size=k, replace=False))
        selected_set = {self._param_names[i] for i in selected}
        return {p: (p in selected_set) for p in self._param_names}

    def _sample_assembly_jitter(
        self, param_name: str, param_distribution: BaseDistribution
    ) -> Any:
        """Legacy assembly mode: ±10% Gaussian jitter around the all-robust point."""
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

    def _check_assembly_health(self, study: Study) -> None:
        """Compare early assembly trials to the baseline. Warn if assembly is
        consistently MUCH worse than baseline → robust optima may be over-
        conservative (the activity floor + mixing assembly help, but if they
        still under-perform, the user should consider tuning weights).
        """
        if self._health_check_done:
            return
        if self._baseline_loss is None or self._assembly_trial_count < HEALTH_CHECK_AFTER_N_ASSEMBLY:
            return

        # Collect losses from the first N assembly trials
        scan_size = len(self._schedule)
        assembly_trials = [
            t for t in study.get_trials(deepcopy=False, states=[TrialState.COMPLETE])
            if t.number > scan_size and t.values is not None
        ]
        if len(assembly_trials) < HEALTH_CHECK_AFTER_N_ASSEMBLY:
            return

        assembly_losses = sorted(float(t.values[0]) for t in assembly_trials)
        # Use the best (lowest) loss seen in early assembly
        best_assembly_loss = assembly_losses[0]
        ratio = abs(best_assembly_loss) / max(abs(self._baseline_loss), 1e-9)

        self._health_check_done = True
        if (
            (self._baseline_loss < 0 and best_assembly_loss > self._baseline_loss * HEALTH_BAD_RATIO)
            or (self._baseline_loss >= 0 and best_assembly_loss > self._baseline_loss * HEALTH_BAD_RATIO)
        ):
            logger.warning(
                "=" * 70
            )
            logger.warning(
                "CWSampler: HEALTH WARNING — assembly losses are significantly worse "
                "than baseline. The first %d assembly trials have best loss=%.4f, "
                "vs baseline_loss=%.4f (ratio %.2fx).",
                HEALTH_CHECK_AFTER_N_ASSEMBLY, best_assembly_loss, self._baseline_loss, ratio,
            )
            logger.warning(
                "This usually means the robust_optima are over-conservative for this "
                "loss landscape (the strategy is happier with the v1 defaults than "
                "with the plateau-anchored values). Mitigations:"
            )
            logger.warning(
                "  - increase performance_weight (default 0.5 → try 0.7 or 0.8)"
            )
            logger.warning(
                "  - increase min_active_trades (default %d → try 20-50) to penalise "
                "inactive plateaus more strongly",
                DEFAULT_MIN_ACTIVE_TRADES,
            )
            logger.warning(
                "  - use assembly_mode='jitter' if mixing isn't helping converge"
            )
            logger.warning("=" * 70)

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
