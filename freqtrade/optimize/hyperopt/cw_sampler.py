"""
PlateauSampler — Coordinate-Wise Sampler for Freqtrade Hyperopt

Inspired by Eric Lefort's approach to strategy parameter tuning, refined with
techniques from:
  - Hansen & Ostermeier 2001 "Completely Derandomized Self-Adaptation in
    Evolution Strategies" (CMA-ES) — adaptive step size based on observed
    sensitivity
  - Bailey & Lopez de Prado "The Deflated Sharpe Ratio" — minimize the number
    of trials in regions that don't matter (reduces PBO)
  - Rob Carver "Systematic Trading" — preserve trading activity as a hard
    constraint alongside loss optimization

Core principle: optimize parameters one at a time while keeping all others at
their default (baseline) values. For each parameter, scan values around the
default (adaptive, not uniform grid), identify a "plateau" — a region of stable
performance — and classify the parameter into one of three categories. Then
explore the joint space restricted to the plateaus using bayesian sampling.

Phases:
  1. BASELINE   — 1 trial with all params at defaults. Captures loss_baseline
                  and n_trades_baseline. Used to anchor adaptive tolerance and
                  the activity floor.

  2. SCAN       — For each parameter, test values around its default,
                  symmetrically expanding (default ± 1×step, ± 2×step, ...) up
                  to a per-direction limit. EARLY STOP when the first value
                  outside the plateau is hit (boundary detected). Per-param
                  tolerance is computed adaptively from observed loss
                  volatility (see _compute_tolerance).

  3. CLASSIFY   — Each parameter is classified as:
                    ACTIVE_PLATEAU      — has a plateau around the default;
                                          explore in [low, high] during assembly
                    FROZEN_BOWL         — default is a local minimum (all
                                          neighbors worse); fix at default
                    FROZEN_CATEGORICAL  — for categorical params with no plateau
                                          ordering; pick best loss observed

  4. ASSEMBLY   — Uniform random sampling within the plateau bounds for
                  ACTIVE_PLATEAU params; FROZEN_* params are hardcoded at
                  their fixed values. Random (not TPE) because TPE converges
                  to peaks within the plateau, which still overfits.

  5. EXPORT     — Occam-regularized selection: from the top-K trials by loss
                  (within EXPORT_TOP_K_TOLERANCE of the absolute best), pick
                  the one with the fewest params changed from baseline. If
                  the baseline is the best (no improvement found), v1 is
                  exported unchanged — the baseline trial is always in the
                  candidate pool.

A "plateau" requires BOTH:
  - |loss(n) - loss(baseline)| < adaptive_tolerance
  - n_trades(n) >= activity_floor (70% × n_trades_baseline by default)

The activity floor prevents the sampler from picking restrictive parameter
values that fire far fewer trades than the baseline — a known over-fitting
failure mode where the "plateau" is in fact a region of inactivity.

Usage:
  freqtrade hyperopt --sampler PlateauSampler [--epochs N] ...

Empirical limits this design addresses:
  - When v1 is near a Pareto front, no single-param perturbation helps.
    ALL params can classify as FROZEN_BOWL → v1 is exported unchanged
    (the baseline trial is always in the candidate pool).
  - The activity floor enforces trade-count preservation on TRAIN. OOS
    regime drift can still break this; deployers should keep the manual
    "v2 trade count >= 70% v1 trade count on holdout" check (see
    docs/hyperopt-cwsampler.md).
  - Loss-function alignment between v1 and v2 hyperopt remains critical:
    the plateau is anchored on the loss function the user passed. Using a
    different loss than what selected v1 produces plateaus optimized for a
    different objective and rarely carries over. Pairing PlateauSampler
    with WalkForwardLoss is the recommended default to mitigate this.
"""

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import optuna
from optuna.distributions import (
    BaseDistribution,
    CategoricalDistribution,
    FloatDistribution,
    IntDistribution,
)
from optuna.samplers import BaseSampler, TPESampler
from optuna.study import Study
from optuna.trial import TrialState


logger = logging.getLogger(__name__)


# ── Plateau detection — adaptive tolerance ────────────────────────────────
# A neighbor value n is "in plateau" if |loss(n) - loss(baseline)| < tolerance.
# Tolerance is computed per-param from the observed loss volatility:
#   tolerance = clip(
#     PLATEAU_FLOOR    × |loss_baseline|,             # absolute minimum
#     PLATEAU_FRACTION × max_observed_loss_change,    # relative to sensitivity
#     PLATEAU_CEILING  × |loss_baseline|              # absolute maximum
#   )
# Rationale: a stiff param (small loss change everywhere) should declare plateau
# easily (floor kicks in); a volatile param needs more leeway (fraction kicks
# in); a peaked param (huge loss change) gets clipped to the ceiling so we
# don't absorb clearly-worse values into a "plateau".
PLATEAU_FLOOR = 0.01     # 1% of |loss_baseline|
PLATEAU_FRACTION = 0.30  # 30% of max observed loss change
PLATEAU_CEILING = 0.15   # 15% of |loss_baseline|
# Number of scan trials per param to consider before first tolerance
# calibration (default ±1, ±2 = 4 trials). Tolerance is then recomputed on
# every subsequent scan trial for that param (continuous calibration).
PLATEAU_INITIAL_SCAN_K = 4

# ── Activity floor — anti-over-conservatism ───────────────────────────────
# A scan value is excluded from the plateau if its trial fired fewer than
# max(min_active_trades_abs, min_trades_ratio × n_trades_baseline) trades.
# Same floor is applied to filter assembly trials before export selection.
DEFAULT_MIN_ACTIVE_TRADES = 10
DEFAULT_MIN_TRADES_RATIO = 0.7

# ── Adaptive scan ─────────────────────────────────────────────────────────
# sampling_step = max(dist.step, range / SCAN_RANGE_DIVISOR)
# For coarse-step params, sampling_step = dist.step (unchanged).
# For fine-step params with wide range, sampling_step is broader so 10
# steps/direction covers a meaningful fraction of the range.
SCAN_RANGE_DIVISOR = 30
# Hard cap on scan trials per direction per param. With adaptive early stop,
# typical use is 2-6 trials per direction; this is a safety upper bound.
MAX_SCAN_STEPS_PER_DIR = 10
# Minimum scan trials per param to allow plateau detection at all.
# Below this, the sampler refuses to start (caught by the budget check in
# hyperopt_optimizer.py via the MIN_POINTS_PER_PARAM export).
MIN_POINTS_PER_PARAM = 4

# ── Assembly — random uniform sampling in plateau regions ────────────────
# Assembly uses uniform random sampling within plateau bounds. Rationale:
# TPE's convergence behavior is harmful in finance because the "best loss
# in train" is almost always a peak that does not generalize. Random
# sampling produces a spread of trials in the plateau; the export logic
# (below) then picks the best loss AMONG TRIALS WITH FEW PARAMS CHANGED
# (Occam regularization).
ASSEMBLY_RANDOM_WARMUP = 30  # kept for compatibility, unused with RandomSampler
# Default fraction of total epochs allocated to assembly (rest = baseline + scan).
DEFAULT_SCAN_BUDGET_RATIO = 0.6  # → assembly gets 1 - this = 40%

# ── Export Policy — Occam-regularized top-K selection ─────────────────
# Standard "best-loss" export overfits in finance: the trial with the lowest
# training loss is almost always a peak that doesn't generalize. The fix:
# from the top K trials (lowest loss), pick the one with the FEWEST params
# changed from baseline. This is Occam's razor: simple changes generalize
# better than complex ones.
#
# A param is "changed" if |trial.param - baseline.param| / range > EPSILON.
# The Top-K window is loss-relative: take all trials with loss within
# TOP_K_LOSS_TOLERANCE × |baseline_loss| of the absolute best.
EXPORT_TOP_K_TOLERANCE = 0.20  # 20% of |baseline_loss| of slack from best
EXPORT_CHANGE_EPSILON = 0.01   # 1% of param range = "no change"


@dataclass
class ScanMeasure:
    """A single scan trial's outcome for one parameter."""
    value: Any
    loss: float
    n_trades: int
    trial_number: int
    in_plateau: bool = False  # filled in by _classify_one_param


@dataclass
class ParamProfile:
    """Result of the scan phase for one parameter."""
    name: str
    kind: str  # "ACTIVE_PLATEAU" | "FROZEN_BOWL" | "FROZEN_MONOTONIC" | "FROZEN_CATEGORICAL"
    default: Any
    # For ACTIVE_PLATEAU: low, high = plateau bounds; best_value = best loss in plateau
    # For FROZEN_*: low=high=best_value = the fixed value
    low: Any
    high: Any
    best_value: Any
    best_loss: float
    tolerance_used: float = 0.0
    scan_measures: list[ScanMeasure] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.kind == "ACTIVE_PLATEAU"

    @property
    def is_frozen(self) -> bool:
        return self.kind.startswith("FROZEN_")


class PlateauSampler(BaseSampler):
    """Coordinate-Wise Sampler — adaptive scan + restricted bayesian assembly.

    See module docstring for the full algorithm. Key external API:
      - record_trial_metrics(trial_number, n_trades): called by freqtrade after
        each trial so the sampler knows the trade count (used by activity floor
        and plateau classification).
      - get_robust_optima(): returns the final params dict (computed at export
        time = best trial across baseline + scan + assembly that passes floor).
      - get_phase(): returns current phase string for logging/UI.
      - get_param_profiles(): returns dict[param_name -> ParamProfile] (populated
        after scan completes).

    Args:
        seed: RNG seed for reproducibility (passed to internal TPE as well).
        total_epochs: total epoch budget. Used to compute scan vs assembly
            allocation. Required for proper budget planning.
        defaults: hand-tuned defaults dict {param_name: value}. Used as the
            baseline anchor for the scan. Strongly recommended — without this,
            the sampler falls back to midpoint of each param range.
        output_dir: directory for auto-dumping the robust_optima JSON archive
            (in addition to the per-strategy .json export).
        strategy_name: name of the strategy class (used in archive filename).
        min_active_trades: absolute floor for n_trades (default 10).
        min_trades_ratio: relative floor as fraction of n_trades_baseline
            (default 0.7 = 70%).
        plateau_floor, plateau_fraction, plateau_ceiling: tolerance constants
            (see module docstring).
        plateau_initial_scan: K initial scan trials per param before first
            tolerance calibration (default 4).
        scan_budget_ratio: fraction of total_epochs to allocate to scan phase
            (rest goes to assembly; default 0.6).
        max_scan_steps_per_dir: hard cap on scan trials per direction per param.
        assembly_random_warmup: number of random trials at start of assembly
            before TPE takes over.
    """

    def __init__(
        self,
        seed: int = 42,
        total_epochs: int | None = None,
        defaults: dict[str, Any] | None = None,
        output_dir: Path | str | None = None,
        strategy_name: str | None = None,
        min_active_trades: int = DEFAULT_MIN_ACTIVE_TRADES,
        min_trades_ratio: float = DEFAULT_MIN_TRADES_RATIO,
        plateau_floor: float = PLATEAU_FLOOR,
        plateau_fraction: float = PLATEAU_FRACTION,
        plateau_ceiling: float = PLATEAU_CEILING,
        plateau_initial_scan: int = PLATEAU_INITIAL_SCAN_K,
        scan_budget_ratio: float = DEFAULT_SCAN_BUDGET_RATIO,
        max_scan_steps_per_dir: int = MAX_SCAN_STEPS_PER_DIR,
        assembly_random_warmup: int = ASSEMBLY_RANDOM_WARMUP,
        **kwargs,  # tolerate legacy kwargs (points_per_param, assembly_mode, etc.) silently
    ):
        # ── Reproducibility ────────────────────────────────────────────
        self._seed = seed
        self._rng = np.random.RandomState(seed)

        # ── Budget ─────────────────────────────────────────────────────
        self._total_epochs = total_epochs
        self._scan_budget_ratio = scan_budget_ratio
        self._assembly_random_warmup = max(1, assembly_random_warmup)
        if not 0.0 <= min_trades_ratio <= 1.5:
            raise ValueError(
                f"min_trades_ratio must be in [0, 1.5] (typical: 0.7), got {min_trades_ratio}"
            )

        # ── Tolerance constants ────────────────────────────────────────
        self._plateau_floor = plateau_floor
        self._plateau_fraction = plateau_fraction
        self._plateau_ceiling = plateau_ceiling
        self._plateau_initial_scan = max(2, plateau_initial_scan)

        # ── Activity floor ─────────────────────────────────────────────
        self._min_active_trades_abs = max(1, min_active_trades)
        self._min_trades_ratio = min_trades_ratio
        # Effective floor resolved after baseline completes. Starts at abs only.
        self._min_active_trades = self._min_active_trades_abs

        # ── Export auto-dump ───────────────────────────────────────────
        self._output_dir = Path(output_dir) if output_dir else None
        self._strategy_name = strategy_name

        # ── User-provided defaults (baseline anchor) ──────────────────
        self._user_defaults = defaults or {}

        # ── Scan config ────────────────────────────────────────────────
        self._max_scan_steps_per_dir = max(2, max_scan_steps_per_dir)

        # ── State ──────────────────────────────────────────────────────
        # Discovered params (in order of first sample_independent call).
        self._param_names: list[str] = []
        self._param_distributions: dict[str, BaseDistribution] = {}
        # Baseline values (defaults if provided else midpoint).
        self._baseline: dict[str, Any] = {}
        # Per-param sampling step (computed in _finalize_init).
        self._sampling_steps: dict[str, float] = {}
        # Per-param scan slots: list of (offset_index, value). Pre-built for the
        # full possible scan range; trials past the boundary are no-ops.
        self._scan_slots: dict[str, list[tuple[int, Any]]] = {}
        # Flattened scan schedule: list of (param_name, offset_index, value).
        # Order: param-by-param, alternating +1, -1, +2, -2, ..., +K, -K.
        self._scan_schedule: list[tuple[str, int, Any]] = []
        # Completed scan trials by param (filled in via after_trial + record_trial_metrics).
        self._scan_completed: dict[str, list[ScanMeasure]] = defaultdict(list)
        # Per-param classified profile (filled in after scan completes).
        self._param_profiles: dict[str, ParamProfile] = {}
        # ACTIVE param sub-distributions for assembly TPE.
        self._tpe_sub_distributions: dict[str, BaseDistribution] = {}
        # FROZEN param values (used in assembly to fill missing TPE params).
        self._frozen_values: dict[str, Any] = {}
        # Internal TPE study for assembly (created in _init_assembly).
        self._tpe_sampler: TPESampler | None = None
        self._tpe_study: Study | None = None
        # Map external trial.number → internal TPE trial (so we can tell results back).
        self._assembly_sub_trials: dict[int, Any] = {}
        # Per-external-trial cached spec (param values for the trial).
        self._current_trial_spec: dict[str, Any] = {}
        # Baseline metrics captured after trial 0 completes.
        self._baseline_loss: float | None = None
        self._baseline_n_trades: int | None = None
        # Trial-number → n_trades, populated by record_trial_metrics().
        self._trial_n_trades: dict[int, int] = {}
        # Phase machine state.
        self._phase = "init"
        self._last_seen_trial: int = -1
        self._initialized = False
        # Logging gates.
        self._scan_completion_logged = False
        self._assembly_entered_logged = False

    # ── External hook ─────────────────────────────────────────────────

    def record_trial_metrics(self, trial_number: int, n_trades: int) -> None:
        """Called by freqtrade after each trial completes.

        Records n_trades for use by activity floor and plateau classification.
        Without this, the sampler falls back to "all trials count as active"
        (no floor enforcement).
        """
        self._trial_n_trades[trial_number] = int(n_trades)

    # ── Optuna interface ─────────────────────────────────────────────

    def infer_relative_search_space(self, study, trial):
        return {}

    def sample_relative(self, study, trial, search_space):
        return {}

    def sample_independent(self, study, trial, param_name, param_distribution):
        self._discover_param(param_name, param_distribution)

        # Per-trial state update (only once, on first param call of the trial)
        if trial.number != self._last_seen_trial:
            self._last_seen_trial = trial.number
            self._update_phase(study, trial.number)
            self._decide_trial_spec(study, trial.number)

        # Return the value cached for this param in the current trial spec
        return self._current_trial_spec.get(
            param_name, self._baseline.get(param_name, self._get_midpoint(param_distribution))
        )

    def after_trial(self, study, trial, state, values):
        # Tell internal TPE about completed assembly trials (so it can update its model)
        if self._phase == "assembly" and trial.number in self._assembly_sub_trials:
            sub_trial = self._assembly_sub_trials[trial.number]
            try:
                if state == TrialState.COMPLETE and values is not None:
                    self._tpe_study.tell(sub_trial, float(values[0]))
                else:
                    self._tpe_study.tell(sub_trial, state=TrialState.FAIL)
            except Exception as exc:  # pragma: no cover
                logger.debug(f"PlateauSampler: TPE.tell for trial {trial.number} failed: {exc}")

    def reseed_rng(self) -> None:
        self._rng = np.random.RandomState(self._rng.randint(0, 2**31))

    # ── Phase management ──────────────────────────────────────────────

    def _update_phase(self, study: Study, trial_number: int) -> None:
        if self._phase == "init":
            self._phase = "baseline"
            return

        if self._phase == "baseline" and trial_number >= 1:
            self._finalize_baseline(study)
            self._init_scan()
            self._phase = "scan"
            return

        if self._phase == "scan":
            # Pull all completed scan trials' losses into _scan_completed entries.
            self._record_completed_scans(study)
            # Scan is done when trial_number > 1 + len(scan_schedule) - 1 = len(scan_schedule)
            # (trial 0 = baseline, trials 1..len = scan)
            if trial_number > len(self._scan_schedule):
                self._classify_params()
                self._init_assembly()
                self._phase = "assembly"
                if not self._assembly_entered_logged:
                    self._log_scan_completion()
                    self._assembly_entered_logged = True

    def _decide_trial_spec(self, study: Study, trial_number: int) -> None:
        if self._phase == "baseline":
            self._current_trial_spec = dict(self._baseline)
        elif self._phase == "scan":
            self._current_trial_spec = self._spec_scan(trial_number)
        elif self._phase == "assembly":
            self._current_trial_spec = self._spec_assembly(trial_number)
        else:
            self._current_trial_spec = dict(self._baseline)

    # ── BASELINE ──────────────────────────────────────────────────────

    def _finalize_baseline(self, study: Study) -> None:
        # Capture baseline loss and trade count from trial 0
        for t in study.get_trials(deepcopy=False, states=[TrialState.COMPLETE]):
            if t.number == 0 and t.values is not None:
                self._baseline_loss = float(t.values[0])
                break

        self._baseline_n_trades = self._trial_n_trades.get(0)
        if self._baseline_n_trades and self._baseline_n_trades > 0:
            relative_floor = int(self._baseline_n_trades * self._min_trades_ratio)
            self._min_active_trades = max(self._min_active_trades_abs, relative_floor)
            logger.info(
                f"PlateauSampler: baseline complete. loss={self._baseline_loss}, "
                f"n_trades={self._baseline_n_trades}. Activity floor set to "
                f"{self._min_active_trades} trades (= max({self._min_active_trades_abs} abs, "
                f"{relative_floor} = {self._min_trades_ratio:.0%} of baseline))"
            )
        else:
            logger.warning(
                "PlateauSampler: baseline trial completed but n_trades not recorded — "
                "activity floor will use absolute minimum only "
                f"({self._min_active_trades_abs} trades). "
                "Plateau detection will be less reliable."
            )

        if self._baseline_n_trades and self._baseline_n_trades < 30:
            logger.warning(
                f"PlateauSampler: baseline has only {self._baseline_n_trades} trades. "
                "Plateau detection is statistically unreliable with so few trades — "
                "consider a longer training timerange or a more permissive strategy."
            )

    # ── SCAN init + schedule ──────────────────────────────────────────

    def _init_scan(self) -> None:
        """Pre-build the scan schedule. Per-param: ±1, ±2, ..., ±K (alternating)."""
        if self._initialized:
            return

        # Compute sampling step per param
        for name, dist in self._param_distributions.items():
            self._sampling_steps[name] = self._compute_sampling_step(dist)

        # Validate categoricals: warn if numeric-looking values
        self._validate_categoricals()

        # Build per-param scan slots and flatten into schedule
        for name in self._param_names:
            dist = self._param_distributions[name]
            default = self._baseline[name]
            step = self._sampling_steps[name]
            slots = self._build_scan_slots(name, dist, default, step)
            self._scan_slots[name] = slots
            for offset, value in slots:
                self._scan_schedule.append((name, offset, value))

        n_scan = len(self._scan_schedule)
        budget_msg = ""
        if self._total_epochs:
            assembly_target = self._total_epochs - 1 - n_scan  # 1 = baseline
            budget_msg = (
                f" (max scan slots = {n_scan}, assembly target = "
                f"{max(0, assembly_target)} trials)"
            )
            if assembly_target < self._assembly_random_warmup:
                logger.warning(
                    f"PlateauSampler: assembly budget ({max(0, assembly_target)}) is below "
                    f"the random warmup target ({self._assembly_random_warmup}). "
                    "TPE will not have time to converge — increase --epochs."
                )

        logger.info(
            f"PlateauSampler: scan initialized for {len(self._param_names)} params{budget_msg}. "
            f"Adaptive early-stop will skip trials past detected plateau boundaries."
        )
        self._initialized = True

    def _build_scan_slots(
        self,
        name: str,
        dist: BaseDistribution,
        default: Any,
        step: float,
    ) -> list[tuple[int, Any]]:
        """Build the per-param scan slot list: (offset_index, value) pairs.

        For categorical params with N choices, returns one slot per non-default
        choice (no concept of "step" — exhaustive scan of alternatives).

        For numeric params, alternates +1, -1, +2, -2, ..., +K, -K. Slots whose
        value falls outside the distribution range are dropped at build time.
        """
        slots: list[tuple[int, Any]] = []

        if isinstance(dist, CategoricalDistribution):
            for i, choice in enumerate(dist.choices):
                if choice != default:
                    slots.append((i + 1, choice))
            return slots

        # Numeric: alternate +k, -k for k = 1..K
        K = self._max_scan_steps_per_dir
        for k in range(1, K + 1):
            for sign in (1, -1):
                offset = sign * k
                value = self._scan_value(dist, default, step, offset)
                if value is not None and value != default:
                    slots.append((offset, value))

        return slots

    def _scan_value(
        self, dist: BaseDistribution, default: Any, step: float, offset: int
    ) -> Any:
        """Compute the scan value for the given offset; None if out of range."""
        if isinstance(dist, IntDistribution):
            value = int(default) + round(offset * step)
            if value < dist.low or value > dist.high:
                return None
            return value
        if isinstance(dist, FloatDistribution):
            value = float(default) + offset * step
            if value < dist.low - 1e-12 or value > dist.high + 1e-12:
                return None
            # Snap to native step if defined
            if dist.step and dist.step > 0:
                value = round(value / dist.step) * dist.step
                if value < dist.low or value > dist.high:
                    return None
            return value
        return None

    def _compute_sampling_step(self, dist: BaseDistribution) -> float:
        """Per-param sampling step = max(native_step, range / SCAN_RANGE_DIVISOR).

        For Int: native_step = 1. For Float with explicit step: that step. Else 0.
        Result is rounded to a multiple of native_step so all scan values are
        snappable to the distribution's grid.
        """
        if isinstance(dist, IntDistribution):
            native = max(1, dist.step or 1)
            range_w = dist.high - dist.low
            adaptive = max(native, range_w // SCAN_RANGE_DIVISOR)
            return float(round(adaptive / native) * native)
        if isinstance(dist, FloatDistribution):
            native = dist.step if (dist.step and dist.step > 0) else (dist.high - dist.low) / 1000
            range_w = dist.high - dist.low
            adaptive = max(native, range_w / SCAN_RANGE_DIVISOR)
            return round(adaptive / native) * native
        return 1.0

    # ── SCAN sampling ─────────────────────────────────────────────────

    def _spec_scan(self, trial_number: int) -> dict[str, Any]:
        """Return the per-trial param dict for a scan trial.

        Scan trial index is trial_number - 1 (since trial 0 = baseline).
        Out-of-range or adaptive-skip slots return all-baseline (= duplicate of
        baseline, dedup'd by freqtrade — no wasted evaluation).
        """
        scan_idx = trial_number - 1
        if scan_idx < 0 or scan_idx >= len(self._scan_schedule):
            return dict(self._baseline)

        param, offset, value = self._scan_schedule[scan_idx]

        # Adaptive early-stop: check if the boundary in this direction has
        # already been found for this param. If yes, skip this trial.
        if self._should_skip_scan_slot(param, offset):
            return dict(self._baseline)

        spec = dict(self._baseline)
        spec[param] = value
        return spec

    def _should_skip_scan_slot(self, param: str, offset: int) -> bool:
        """Adaptive early-stop logic.

        Skip a scan slot if the plateau boundary in its direction has been
        detected: there is at least one completed trial AT or PAST the same
        sign with smaller |offset| that is OUTSIDE the plateau.

        Requires at least PLATEAU_INITIAL_SCAN_K completed trials for this
        param (regardless of direction) before any skipping is allowed.
        """
        completed = self._scan_completed.get(param, [])
        if len(completed) < self._plateau_initial_scan:
            return False  # not enough data to decide yet

        # Compute current tolerance for this param
        tolerance = self._compute_tolerance(param)

        # Look at completed trials in the same direction (same sign of offset)
        sign = 1 if offset > 0 else -1
        same_direction = [
            m for m in completed
            if self._offset_for_value(param, m.value) is not None
            and (self._offset_for_value(param, m.value) or 0) * sign > 0
        ]
        if not same_direction:
            return False

        # Sort by |offset| ascending. Find the first one that is OUTSIDE plateau.
        same_direction.sort(key=lambda m: abs(self._offset_for_value(param, m.value) or 0))
        for m in same_direction:
            m_offset = self._offset_for_value(param, m.value) or 0
            in_plateau = (
                self._is_in_plateau_loss(m.loss, tolerance)
                and self._passes_activity_floor(m.n_trades)
            )
            if not in_plateau and abs(m_offset) < abs(offset):
                # Boundary found at m_offset, further trials in this direction are useless
                return True
        return False

    def _offset_for_value(self, param: str, value: Any) -> int | None:
        """Reverse-lookup: given a value tested for param, return its offset_index."""
        slots = self._scan_slots.get(param, [])
        for offset, v in slots:
            if v == value or (isinstance(v, float) and abs(v - value) < 1e-9):
                return offset
        return None

    def _record_completed_scans(self, study: Study) -> None:
        """Refresh _scan_completed from study's completed trials.

        Idempotent — only adds trials not yet recorded.
        """
        already_recorded = {m.trial_number for measures in self._scan_completed.values()
                            for m in measures}
        for t in study.get_trials(deepcopy=False, states=[TrialState.COMPLETE]):
            if t.number == 0 or t.values is None:
                continue
            if t.number in already_recorded:
                continue
            scan_idx = t.number - 1
            if scan_idx < 0 or scan_idx >= len(self._scan_schedule):
                continue
            param, _offset, expected_value = self._scan_schedule[scan_idx]
            # Skip duplicates (= baseline dedup'd or adaptive-skip slots)
            if t.params.get(param) != expected_value:
                continue
            measure = ScanMeasure(
                value=expected_value,
                loss=float(t.values[0]),
                n_trades=self._trial_n_trades.get(t.number, 0),
                trial_number=t.number,
            )
            self._scan_completed[param].append(measure)

    # ── Tolerance + plateau membership ────────────────────────────────

    def _compute_tolerance(self, param: str) -> float:
        """Adaptive tolerance for plateau membership.

        tolerance = clip(
          PLATEAU_FLOOR    × |loss_baseline|,
          PLATEAU_FRACTION × max(|loss(n) - loss_baseline|) over scan,
          PLATEAU_CEILING  × |loss_baseline|,
        )

        Only floor-passing trials contribute to max_change. Trials that fail
        the activity floor are excluded entirely (their losses are unreliable
        anyway — collapsed trades = high variance per-trade).

        Recomputed on every call → tolerance widens as we observe more
        sensitive values, narrows otherwise. Continuous calibration ensures
        correct classification regardless of scan order.
        """
        baseline = self._baseline_loss
        if baseline is None:
            return 0.0  # not yet calibrated
        baseline_abs = max(abs(baseline), 1e-9)
        floor = self._plateau_floor * baseline_abs
        ceiling = self._plateau_ceiling * baseline_abs

        # Filter: only trials passing the activity floor contribute to tolerance
        # (low-trade trials have unreliable losses and can artificially constrain
        # the tolerance, leading to over-tight plateau detection).
        eligible = [m for m in self._scan_completed.get(param, [])
                    if self._passes_activity_floor(m.n_trades)]
        if not eligible:
            return floor  # nothing usable observed yet → use minimum

        max_change = max(abs(m.loss - baseline) for m in eligible)
        adaptive = self._plateau_fraction * max_change
        return float(np.clip(adaptive, floor, ceiling))

    def _is_in_plateau_loss(self, loss: float, tolerance: float) -> bool:
        """Loss component of plateau membership."""
        if self._baseline_loss is None:
            return False
        return abs(loss - self._baseline_loss) <= tolerance

    def _passes_activity_floor(self, n_trades: int) -> bool:
        """Trade-count component of plateau membership."""
        if n_trades is None:
            return True  # no info = assume OK (degrades gracefully)
        return n_trades >= self._min_active_trades

    # ── Classification ────────────────────────────────────────────────

    def _classify_params(self) -> None:
        """After scan, classify each param as ACTIVE_PLATEAU / FROZEN_BOWL /
        FROZEN_MONOTONIC / FROZEN_CATEGORICAL.
        """
        for name in self._param_names:
            dist = self._param_distributions[name]
            measures = self._scan_completed.get(name, [])
            self._param_profiles[name] = self._classify_one_param(name, dist, measures)

    def _classify_one_param(
        self,
        name: str,
        dist: BaseDistribution,
        measures: list[ScanMeasure],
    ) -> ParamProfile:
        default = self._baseline[name]
        baseline_loss = self._baseline_loss or 0.0

        # Categorical: special handling (no notion of plateau / boundary)
        if isinstance(dist, CategoricalDistribution):
            return self._classify_categorical(name, default, baseline_loss, measures)

        if not measures:
            # No scan data — keep default
            return ParamProfile(
                name=name, kind="FROZEN_BOWL", default=default,
                low=default, high=default, best_value=default,
                best_loss=baseline_loss,
            )

        tolerance = self._compute_tolerance(name)

        # Mark in_plateau on each measure
        for m in measures:
            m.in_plateau = (
                self._is_in_plateau_loss(m.loss, tolerance)
                and self._passes_activity_floor(m.n_trades)
            )

        # Baseline is by definition in its own plateau
        in_plateau_values: list[Any] = [default]
        for m in measures:
            if m.in_plateau:
                in_plateau_values.append(m.value)

        # ACTIVE_PLATEAU: at least one non-default value in plateau
        if len(in_plateau_values) >= 2:
            low = min(in_plateau_values)
            high = max(in_plateau_values)
            # Best value = lowest loss within plateau (default's loss = baseline_loss)
            candidates = [(default, baseline_loss)] + [
                (m.value, m.loss) for m in measures if m.in_plateau
            ]
            best_value, best_loss = min(candidates, key=lambda x: x[1])
            return ParamProfile(
                name=name, kind="ACTIVE_PLATEAU", default=default,
                low=low, high=high, best_value=best_value, best_loss=best_loss,
                tolerance_used=tolerance, scan_measures=measures,
            )

        # FROZEN_BOWL: all non-default values worse than baseline → keep default
        # MONOTONIC case: at least one value better than baseline (outside plateau)
        # → reclassify as ACTIVE_PLATEAU with bounds [min(default, best), max(default, best)].
        # Rationale: FREEZING at best_value combines individual improvements into a
        # never-tested-together config that overfits dramatically on OOS (verified
        # empirically: the naive combined-frozen export overfit dramatically on OOS).
        # Treating these as ACTIVE with default-to-best range lets TPE explore the
        # interpolation cube; baseline + scan + assembly trials all enter the
        # candidate pool and the best-loss WITH activity floor wins.
        better_neighbors = [m for m in measures if m.loss < baseline_loss
                            and self._passes_activity_floor(m.n_trades)]
        if better_neighbors:
            best = min(better_neighbors, key=lambda m: m.loss)
            low = min(default, best.value) if isinstance(default, (int, float)) else default
            high = max(default, best.value) if isinstance(default, (int, float)) else default
            return ParamProfile(
                name=name, kind="ACTIVE_PLATEAU", default=default,
                low=low, high=high, best_value=best.value, best_loss=best.loss,
                tolerance_used=tolerance, scan_measures=measures,
            )

        # FROZEN_BOWL
        return ParamProfile(
            name=name, kind="FROZEN_BOWL", default=default,
            low=default, high=default, best_value=default,
            best_loss=baseline_loss, tolerance_used=tolerance, scan_measures=measures,
        )

    def _classify_categorical(
        self,
        name: str,
        default: Any,
        baseline_loss: float,
        measures: list[ScanMeasure],
    ) -> ParamProfile:
        # For categoricals: best choice = whichever has lowest loss (incl. baseline)
        candidates = [(default, baseline_loss)] + [
            (m.value, m.loss) for m in measures
            if self._passes_activity_floor(m.n_trades)
        ]
        if not candidates:
            return ParamProfile(
                name=name, kind="FROZEN_CATEGORICAL", default=default,
                low=default, high=default, best_value=default, best_loss=baseline_loss,
                scan_measures=measures,
            )
        best_value, best_loss = min(candidates, key=lambda x: x[1])
        return ParamProfile(
            name=name, kind="FROZEN_CATEGORICAL", default=default,
            low=best_value, high=best_value, best_value=best_value, best_loss=best_loss,
            scan_measures=measures,
        )

    def _validate_categoricals(self) -> None:
        """Warn if any CategoricalParameter has numeric-only choices.

        Numeric categoricals are a known anti-pattern for plateau detection:
        the sampler can't measure neighbor stability without an ordering.
        Better to use IntParameter or FloatParameter.
        """
        for name, dist in self._param_distributions.items():
            if not isinstance(dist, CategoricalDistribution):
                continue
            choices = list(dist.choices)
            all_numeric = all(isinstance(c, (int, float)) and not isinstance(c, bool)
                              for c in choices)
            if all_numeric:
                logger.warning(
                    f"PlateauSampler: parameter '{name}' is a CategoricalParameter with "
                    f"numeric choices {choices}. This is sub-optimal — PlateauSampler "
                    f"cannot detect plateaus on categoricals (no neighbour ordering). "
                    f"Recommended: convert to IntParameter or FloatParameter to enable "
                    f"plateau detection. PlateauSampler will continue but will treat this "
                    f"param as FROZEN_CATEGORICAL (best choice by loss, no plateau)."
                )

    # ── ASSEMBLY ──────────────────────────────────────────────────────

    def _init_assembly(self) -> None:
        """Build the restricted sub-distribution for random sampling.

        Uses RandomSampler instead of TPESampler with uniform random sampling within
        plateau bounds. See ASSEMBLY_RANDOM_WARMUP constant docstring for
        rationale (TPE converges to overfit peaks, random sampling produces
        a diverse pool that the Occam-regularized export picks from).
        """
        self._tpe_sub_distributions = {}
        self._frozen_values = {}

        for name, profile in self._param_profiles.items():
            dist = self._param_distributions[name]
            if profile.is_active:
                # Build restricted sub-distribution within plateau bounds
                sub_dist = self._make_sub_distribution(dist, profile.low, profile.high)
                if sub_dist is not None:
                    self._tpe_sub_distributions[name] = sub_dist
                else:
                    # Fallback: freeze at best_value
                    self._frozen_values[name] = profile.best_value
            else:
                self._frozen_values[name] = profile.best_value

        # Use Optuna's RandomSampler instead of TPESampler. Random produces
        # a diverse pool that doesn't converge to a peak; the export logic
        # then applies Occam regularization to pick a robust trial.
        from optuna.samplers import RandomSampler
        self._tpe_sampler = RandomSampler(seed=self._seed)
        self._tpe_study = optuna.create_study(sampler=self._tpe_sampler, direction="minimize")

    def _make_sub_distribution(
        self,
        dist: BaseDistribution,
        low: Any,
        high: Any,
    ) -> BaseDistribution | None:
        """Create a distribution restricted to [low, high]."""
        if isinstance(dist, IntDistribution):
            new_low = max(dist.low, int(low))
            new_high = min(dist.high, int(high))
            if new_low > new_high:
                return None
            if new_low == new_high:
                return None  # degenerate — caller will freeze
            return IntDistribution(low=new_low, high=new_high, step=dist.step or 1)
        if isinstance(dist, FloatDistribution):
            new_low = max(dist.low, float(low))
            new_high = min(dist.high, float(high))
            if new_low >= new_high:
                return None
            return FloatDistribution(
                low=new_low, high=new_high, step=dist.step, log=dist.log,
            )
        return None  # categoricals are frozen, never in sub_distributions

    def _spec_assembly(self, trial_number: int) -> dict[str, Any]:
        """Return per-trial spec for assembly. Combines random-sampled active
        params with frozen values.

        When ALL params are frozen (no ACTIVE_PLATEAU detected), every assembly
        trial would produce the same spec. To avoid wasting epochs on N-1
        dedups (which still cost overhead in freqtrade's loop), we return a
        slightly varied spec by re-using the baseline values on alternating
        trials. This ensures at most 2 unique trials are evaluated, the rest
        being legitimate dedups that freqtrade skips fast.
        """
        spec = dict(self._frozen_values)
        if self._tpe_sub_distributions and self._tpe_study is not None:
            sub_trial = self._tpe_study.ask(self._tpe_sub_distributions)
            self._assembly_sub_trials[trial_number] = sub_trial
            for name, value in sub_trial.params.items():
                spec[name] = value
        # If no active subspace and we've already evaluated the combined-frozen
        # spec, return baseline on every other trial to force a different dedup
        # target (avoids 300 identical trials)
        elif trial_number % 2 == 0:
            spec = dict(self._baseline)
        return spec

    # ── Logging ───────────────────────────────────────────────────────

    def _log_scan_completion(self) -> None:
        n_active = sum(1 for p in self._param_profiles.values() if p.is_active)
        n_bowl = sum(1 for p in self._param_profiles.values() if p.kind == "FROZEN_BOWL")
        n_mono = sum(1 for p in self._param_profiles.values() if p.kind == "FROZEN_MONOTONIC")
        n_cat = sum(1 for p in self._param_profiles.values() if p.kind == "FROZEN_CATEGORICAL")

        logger.info("=" * 70)
        logger.info("PlateauSampler: SCAN complete → entering ASSEMBLY phase")
        logger.info(
            f"  Param classification: {n_active} ACTIVE_PLATEAU, "
            f"{n_bowl} FROZEN_BOWL, {n_mono} FROZEN_MONOTONIC, "
            f"{n_cat} FROZEN_CATEGORICAL"
        )
        for name, profile in self._param_profiles.items():
            if profile.is_active:
                logger.info(
                    f"    {name}: ACTIVE_PLATEAU [{profile.low}..{profile.high}] "
                    f"best={profile.best_value} (loss={profile.best_loss:.4f}, "
                    f"tolerance={profile.tolerance_used:.4f})"
                )
            else:
                logger.info(
                    f"    {name}: {profile.kind} = {profile.best_value} "
                    f"(loss={profile.best_loss:.4f})"
                )
        logger.info("=" * 70)

    # ── EXPORT (final selection) ──────────────────────────────────────

    def select_best_export(self, study: Study) -> tuple[dict[str, Any], int, float, int]:
        """Pick the best params dict to export — Occam-regularized policy.

        Returns (params, source_trial_number, loss, n_trades).

        Selection logic:
          1. Filter trials by activity floor (trade count ≥ floor).
          2. From the top-K trials by loss (within EXPORT_TOP_K_TOLERANCE × |loss_baseline|
             of the absolute best), pick the one with FEWEST params changed from
             baseline. Tie-break by lower loss.
          3. A param is "changed" if |value - baseline| / range > EXPORT_CHANGE_EPSILON.

        Rationale (Occam regularization):
          The standard "lowest-loss trial wins" rule overfits in finance — the
          best-loss train trial is almost always a peak that doesn't generalize.
          By preferring trials that change FEWER params (within a tolerance of
          the absolute best loss), we apply Occam's razor: simple changes are
          more likely to generalize than complex ones. The baseline (trial 0
          with 0 changes) is always in the candidate pool, so if no v2 candidate
          materially beats it, v1 is exported naturally.

        Literature reference: Bailey & Lopez de Prado "The Deflated Sharpe
        Ratio" — many trials = harsh penalty. Picking the simplest among the
        best is a proxy for deflating the in-sample peak.
        """
        completed = study.get_trials(deepcopy=False, states=[TrialState.COMPLETE])
        candidates = []
        for t in completed:
            if t.values is None:
                continue
            n_trades = self._trial_n_trades.get(t.number, 0)
            if not self._passes_activity_floor(n_trades):
                continue
            params = dict(self._baseline)
            params.update(t.params)
            loss = float(t.values[0])
            n_changes = self._count_param_changes(params)
            candidates.append({
                "loss": loss, "n_changes": n_changes, "trial_number": t.number,
                "n_trades": n_trades, "params": params,
            })

        if not candidates:
            logger.warning(
                "PlateauSampler: no trial passed the activity floor — exporting baseline (v1)"
            )
            return dict(self._baseline), 0, self._baseline_loss or 0.0, self._baseline_n_trades or 0

        # Find the absolute best loss
        best_loss_abs = min(c["loss"] for c in candidates)

        # Define the top-K window: trials within EXPORT_TOP_K_TOLERANCE × |baseline_loss|
        # of the best. If baseline_loss not known, fall back to relative tolerance.
        if self._baseline_loss is not None and self._baseline_loss != 0:
            slack = abs(self._baseline_loss) * EXPORT_TOP_K_TOLERANCE
        else:
            slack = abs(best_loss_abs) * EXPORT_TOP_K_TOLERANCE
        top_k = [c for c in candidates if c["loss"] <= best_loss_abs + slack]

        # Sort top_k by (n_changes ascending, loss ascending)
        # → prefers fewer changes, ties broken by lower loss
        top_k.sort(key=lambda c: (c["n_changes"], c["loss"]))
        chosen = top_k[0]

        # Logging: explain the selection
        if chosen["loss"] != best_loss_abs:
            logger.info(
                f"PlateauSampler: Occam regularization selected trial #{chosen['trial_number']} "
                f"(loss={chosen['loss']:.4f}, n_changes={chosen['n_changes']}/{len(self._baseline)}) "
                f"over absolute-best (loss={best_loss_abs:.4f}). "
                f"Reason: fewer params changed = less overfitting risk."
            )
        elif chosen["n_changes"] == 0:
            logger.info(
                f"PlateauSampler: best trial is baseline (#0) — v1 is preserved. "
                f"No v2 candidate beat baseline within {EXPORT_TOP_K_TOLERANCE:.0%} loss tolerance."
            )
        else:
            logger.info(
                f"PlateauSampler: best trial = #{chosen['trial_number']} "
                f"(loss={chosen['loss']:.4f}, n_changes={chosen['n_changes']}/{len(self._baseline)})"
            )

        return chosen["params"], chosen["trial_number"], chosen["loss"], chosen["n_trades"]

    def _count_param_changes(self, params: dict[str, Any]) -> int:
        """Count params that differ materially from baseline (Occam metric).

        A param is "changed" if its normalized distance from baseline exceeds
        EXPORT_CHANGE_EPSILON. Categorical params count as changed if their
        value differs from baseline. Used by select_best_export to apply
        Occam regularization on the choice of exported trial.
        """
        n = 0
        for name, value in params.items():
            baseline_value = self._baseline.get(name)
            if baseline_value is None:
                continue
            dist = self._param_distributions.get(name)
            if isinstance(dist, CategoricalDistribution):
                if value != baseline_value:
                    n += 1
                continue
            if isinstance(dist, (IntDistribution, FloatDistribution)):
                range_w = max(float(dist.high - dist.low), 1e-9)
                normalized_change = abs(float(value) - float(baseline_value)) / range_w
                if normalized_change > EXPORT_CHANGE_EPSILON:
                    n += 1
        return n

    # ── Public accessors ──────────────────────────────────────────────

    def get_robust_optima(self) -> dict[str, Any]:
        """Best-trial params dict, or the cached value if select_best_export
        was already called externally with a study.

        For backward compat: returns a snapshot of the current best known
        params. If never called select_best_export, returns the best from the
        last classification (or baseline if scan not complete).
        """
        # If we have profiles (scan complete), best_value per param is at least
        # the per-param scan optimum. Real best comes from select_best_export.
        if self._param_profiles:
            return {p.name: p.best_value for p in self._param_profiles.values()}
        return dict(self._baseline)

    def get_phase(self) -> str:
        return self._phase

    def get_param_profiles(self) -> dict[str, ParamProfile]:
        return dict(self._param_profiles)

    def get_n_baseline_fallback(self) -> int:
        """Number of params that ended up frozen at default (BOWL or categorical
        with default winning). For backward compat with the export pipeline.
        """
        return sum(
            1 for p in self._param_profiles.values()
            if p.is_frozen and p.best_value == p.default
        )

    # ── Export helpers (called by hyperopt.py) ────────────────────────

    def _build_robust_params_dict(self, n_fallback: int) -> dict:
        """Build the freqtrade-loadable JSON params dict.

        Compatible with the old API: hyperopt.py:_export_cwsampler_robust calls
        this to write the strategy .json file. The actual params come from
        select_best_export (called by hyperopt.py before this).
        """
        # The caller (hyperopt.py) sets self._best_robust before calling this.
        # For backward compat, _best_robust is a synonym for the export params.
        best = getattr(self, "_best_robust_export", None) or self.get_robust_optima()
        n_active = sum(1 for p in self._param_profiles.values() if p.is_active)
        n_bowl = sum(1 for p in self._param_profiles.values() if p.kind == "FROZEN_BOWL")
        n_mono = sum(1 for p in self._param_profiles.values() if p.kind == "FROZEN_MONOTONIC")
        n_cat = sum(1 for p in self._param_profiles.values() if p.kind == "FROZEN_CATEGORICAL")

        return {
            "strategy_name": self._strategy_name or "unknown",
            "params": {
                "buy": {k: self._normalize_value(v) for k, v in best.items()},
                "sell": {},
                "protection": {},
                "roi": {},
                "stoploss": {},
                "max_open_trades": {},
                "trailing": {},
            },
            "ft_stratparam_v": 1,
            "export_time": datetime.now(UTC).isoformat(),
            "cwsampler_meta": {
                "version": 6,
                "n_params": len(self._param_profiles),
                "n_active_plateau": n_active,
                "n_frozen_bowl": n_bowl,
                "n_frozen_monotonic": n_mono,
                "n_frozen_categorical": n_cat,
                "param_profiles": {
                    name: {
                        "kind": p.kind,
                        "default": self._normalize_value(p.default),
                        "low": self._normalize_value(p.low),
                        "high": self._normalize_value(p.high),
                        "best_value": self._normalize_value(p.best_value),
                        "best_loss": p.best_loss,
                        "tolerance_used": p.tolerance_used,
                        "n_scan_trials": len(p.scan_measures),
                        "n_in_plateau": sum(1 for m in p.scan_measures if m.in_plateau),
                    }
                    for name, p in self._param_profiles.items()
                },
                "n_baseline_fallback": n_fallback,  # backward compat field
            },
        }

    def _dump_robust_optima_to_json(self, n_fallback: int) -> None:
        """Archive the params JSON to output_dir (in addition to the strategy
        co-located dump done by hyperopt.py). Useful for comparison across
        runs."""
        if not self._output_dir or not self._strategy_name:
            return
        try:
            data = self._build_robust_params_dict(n_fallback)
            self._output_dir.mkdir(parents=True, exist_ok=True)
            archive_path = self._output_dir / f"cwsampler_robust_{self._strategy_name}.json"
            archive_path.write_text(json.dumps(data, indent=2, default=str))
            logger.info(f"PlateauSampler: robust_optima archived to {archive_path}")
        except Exception as exc:  # pragma: no cover
            logger.warning(f"PlateauSampler: failed to dump robust_optima JSON: {exc}")

    # ── Utility methods ───────────────────────────────────────────────

    def _discover_param(self, name: str, dist: BaseDistribution) -> None:
        if name not in self._param_distributions:
            self._param_distributions[name] = dist
            self._param_names.append(name)
            # Lazily set baseline now: user default if provided, else midpoint
            if name in self._user_defaults:
                self._baseline[name] = self._clip_to_dist(self._user_defaults[name], dist)
            else:
                self._baseline[name] = self._get_midpoint(dist)

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
            return dist.choices[len(dist.choices) // 2]
        return value

    @staticmethod
    def _normalize_value(v: Any) -> Any:
        """Convert numpy types to native Python for JSON serialisation."""
        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            return float(v)
        return v


# ── Backward-compat alias ─────────────────────────────────────────────
# The previous name CWSampler remains as an alias so existing
# `--sampler CWSampler` invocations and `from ... import CWSampler` calls
# keep working. Internally both names refer to the same class.
CWSampler = PlateauSampler
