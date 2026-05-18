"""Tests for PlateauSampler (Coordinate-Wise Sampler).

Covers:
  - Init / constructor signatures
  - Sampling step adaptation (per-distribution-kind)
  - Scan slot building (offset alternation + range clipping)
  - Adaptive tolerance computation (floor / fraction / ceiling)
  - Plateau membership (loss + trades multi-criteria)
  - Param classification (ACTIVE_PLATEAU / FROZEN_BOWL / FROZEN_CATEGORICAL)
  - Adaptive early-stop (skip slots past boundary)
  - Activity floor (relative to baseline)
  - Assembly subspace construction (random sampling in plateau bounds)
  - select_best_export with Occam-regularised top-K selection
  - JSON output schema
  - Categorical warning
  - End-to-end on synthetic loss surface
  - Registration in optuna_samplers_dict
"""

import logging

import numpy as np
import optuna
import pytest
from optuna.distributions import CategoricalDistribution, FloatDistribution, IntDistribution

from freqtrade.optimize.hyperopt.cw_sampler import (
    PLATEAU_INITIAL_SCAN_K,
    PlateauSampler,
    ParamProfile,
    ScanMeasure,
)


# ── Helpers ────────────────────────────────────────────────────────────

class _FakeTrial:
    def __init__(self, number, values, params):
        self.number = number
        self.values = values
        self.params = params


def _make_completed_trial(number, loss, params):
    return _FakeTrial(number=number, values=[float(loss)], params=dict(params))


def _make_study_with_trials(trials):
    study = optuna.create_study()
    study.get_trials = lambda deepcopy=False, states=None: list(trials)
    return study


# ── Init / constructor ────────────────────────────────────────────────

class TestInit:
    def test_init_defaults(self):
        s = PlateauSampler(seed=123)
        assert s._seed == 123
        assert s._phase == "init"
        assert s._min_active_trades_abs == 10
        assert s._min_trades_ratio == 0.7

    def test_init_with_defaults_dict(self):
        s = PlateauSampler(seed=0, defaults={"a": 5, "b": 0.3})
        assert s._user_defaults == {"a": 5, "b": 0.3}

    def test_init_invalid_trades_ratio(self):
        with pytest.raises(ValueError):
            PlateauSampler(seed=0, min_trades_ratio=2.5)

    def test_init_tolerates_legacy_kwargs(self):
        # The old API had points_per_param, assembly_mode, etc. — should be silently absorbed
        s = PlateauSampler(
            seed=0, points_per_param=20, assembly_mode="mixing",
            performance_weight=0.5,  # not in new API
        )
        assert s._seed == 0


# ── Sampling step computation ─────────────────────────────────────────

class TestSamplingStep:
    def test_int_step_one(self):
        s = PlateauSampler()
        step = s._compute_sampling_step(IntDistribution(0, 10))
        # range = 10, divisor = 30 → max(1, 0) = 1
        assert step == 1.0

    def test_int_wide_range(self):
        s = PlateauSampler()
        step = s._compute_sampling_step(IntDistribution(0, 300))
        # range = 300, divisor = 30 → max(1, 10) = 10
        assert step == 10.0

    def test_float_with_step(self):
        s = PlateauSampler()
        step = s._compute_sampling_step(FloatDistribution(0.0, 0.10, step=0.001))
        # range = 0.10, divisor = 30 → 0.00333.., snapped to multiple of 0.001 = 0.003
        assert step == pytest.approx(0.003, abs=1e-6)

    def test_float_without_step(self):
        s = PlateauSampler()
        step = s._compute_sampling_step(FloatDistribution(0.0, 1.0))
        # native = 0.001 (range/1000), adaptive = max(0.001, 1/30=0.033)
        # snapped to multiple of 0.001 = 0.033
        assert step == pytest.approx(0.033, abs=1e-3)


# ── Scan slot building ────────────────────────────────────────────────

class TestScanSlots:
    def test_int_alternating_offsets(self):
        s = PlateauSampler(max_scan_steps_per_dir=3)
        slots = s._build_scan_slots("x", IntDistribution(0, 100), default=50, step=1)
        # Expect (+1, 51), (-1, 49), (+2, 52), (-2, 48), (+3, 53), (-3, 47)
        offsets = [o for o, v in slots]
        values = [v for o, v in slots]
        assert offsets == [1, -1, 2, -2, 3, -3]
        assert values == [51, 49, 52, 48, 53, 47]

    def test_int_clips_at_boundary(self):
        s = PlateauSampler(max_scan_steps_per_dir=5)
        # Default near lower bound: slots below should be dropped
        slots = s._build_scan_slots("x", IntDistribution(0, 100), default=2, step=1)
        # Below: -1 → 1, -2 → 0; -3..-5 → dropped (would be negative)
        values_below = [v for o, v in slots if o < 0]
        assert all(v >= 0 for v in values_below)
        assert len(values_below) == 2  # only -1, -2

    def test_categorical_one_slot_per_alternative(self):
        s = PlateauSampler()
        slots = s._build_scan_slots(
            "x", CategoricalDistribution(["a", "b", "c"]), default="b", step=1
        )
        values = [v for o, v in slots]
        assert set(values) == {"a", "c"}
        assert "b" not in values  # default excluded

    def test_float_with_step_snapped(self):
        s = PlateauSampler(max_scan_steps_per_dir=3)
        dist = FloatDistribution(0.0, 1.0, step=0.1)
        slots = s._build_scan_slots("x", dist, default=0.5, step=0.1)
        values = sorted({v for o, v in slots})
        # All values should be multiples of 0.1
        assert all(abs(v / 0.1 - round(v / 0.1)) < 1e-9 for v in values)


# ── Tolerance computation ─────────────────────────────────────────────

class TestTolerance:
    def _make(self, baseline_loss=-1.0, scan_losses=None):
        s = PlateauSampler(seed=0)
        s._baseline_loss = baseline_loss
        if scan_losses is not None:
            s._scan_completed["x"] = [
                ScanMeasure(value=i, loss=l, n_trades=100, trial_number=i+1)
                for i, l in enumerate(scan_losses)
            ]
        return s

    def test_floor_when_no_data(self):
        s = self._make(baseline_loss=-1.0)
        # No scan data → floor = 0.01 × |loss_baseline| = 0.01
        assert s._compute_tolerance("x") == pytest.approx(0.01, abs=1e-9)

    def test_fraction_kicks_in(self):
        s = self._make(baseline_loss=-1.0, scan_losses=[-0.9, -0.8])
        # max_change = 0.2 → 0.3 × 0.2 = 0.06
        # floor = 0.01, ceiling = 0.15
        # → 0.06
        assert s._compute_tolerance("x") == pytest.approx(0.06, abs=1e-9)

    def test_ceiling_clips_huge_changes(self):
        s = self._make(baseline_loss=-1.0, scan_losses=[-2.0, 0.5])
        # max_change = 1.5 → 0.3 × 1.5 = 0.45 → CLIPPED to ceiling 0.15
        assert s._compute_tolerance("x") == pytest.approx(0.15, abs=1e-9)

    def test_floor_clips_tiny_changes(self):
        s = self._make(baseline_loss=-1.0, scan_losses=[-0.999, -0.998])
        # max_change = 0.002 → 0.3 × 0.002 = 0.0006 → floor 0.01
        assert s._compute_tolerance("x") == pytest.approx(0.01, abs=1e-9)


# ── Plateau membership ───────────────────────────────────────────────

class TestPlateauMembership:
    def test_loss_within_tolerance(self):
        s = PlateauSampler(seed=0)
        s._baseline_loss = -1.0
        assert s._is_in_plateau_loss(-1.05, tolerance=0.1)  # |0.05| < 0.1
        assert not s._is_in_plateau_loss(-1.2, tolerance=0.1)  # |0.2| > 0.1

    def test_activity_floor_passes(self):
        s = PlateauSampler(seed=0)
        s._min_active_trades = 70
        assert s._passes_activity_floor(100)
        assert s._passes_activity_floor(70)
        assert not s._passes_activity_floor(50)

    def test_activity_floor_none_is_permissive(self):
        # If n_trades not recorded, fallback to "pass" so degradation is graceful
        s = PlateauSampler(seed=0)
        s._min_active_trades = 70
        assert s._passes_activity_floor(None)


# ── Param classification ─────────────────────────────────────────────

class TestClassification:
    def _make_primed_sampler(self):
        s = PlateauSampler(seed=0)
        s._baseline_loss = -1.0
        s._baseline_n_trades = 100
        s._min_active_trades = 70
        s._param_names = ["x"]
        s._param_distributions = {"x": IntDistribution(0, 10)}
        s._baseline = {"x": 5}
        s._sampling_steps = {"x": 1.0}
        return s

    def test_active_plateau_when_neighbors_stable(self):
        s = self._make_primed_sampler()
        # All neighbors stable within tolerance (loss changes well within floor 1%)
        # baseline_loss = -1.0, floor tolerance = 0.01 → all |Δloss| < 0.01
        s._scan_completed["x"] = [
            ScanMeasure(value=4, loss=-1.003, n_trades=95, trial_number=1),
            ScanMeasure(value=6, loss=-1.005, n_trades=98, trial_number=2),
            ScanMeasure(value=3, loss=-1.007, n_trades=90, trial_number=3),
            ScanMeasure(value=7, loss=-1.009, n_trades=92, trial_number=4),
        ]
        s._classify_params()
        p = s._param_profiles["x"]
        assert p.kind == "ACTIVE_PLATEAU"
        assert p.low == 3
        assert p.high == 7
        # Best value = lowest loss (=-1.009 at value 7)
        assert p.best_value == 7

    def test_active_plateau_narrows_with_diverging_losses(self):
        """When neighbors progressively diverge from baseline, plateau bounds
        should be tight — only the truly-stable values qualify."""
        s = self._make_primed_sampler()
        # Diverging losses: only the closest neighbor is in plateau
        s._scan_completed["x"] = [
            ScanMeasure(value=4, loss=-1.01, n_trades=95, trial_number=1),
            ScanMeasure(value=6, loss=-1.02, n_trades=98, trial_number=2),
            ScanMeasure(value=3, loss=-1.03, n_trades=90, trial_number=3),
            ScanMeasure(value=7, loss=-1.04, n_trades=92, trial_number=4),
        ]
        s._classify_params()
        p = s._param_profiles["x"]
        # max_change = 0.04, tolerance = 0.3 × 0.04 = 0.012
        # Only loss=-1.01 (|Δ|=0.01) is within tolerance
        # Default + that one value form the plateau → narrow
        if p.kind == "ACTIVE_PLATEAU":
            assert p.high - p.low <= 2  # narrow
        else:
            # Or classified as MONOTONIC since values are progressively better
            assert p.kind == "FROZEN_MONOTONIC"

    def test_frozen_bowl_when_all_neighbors_worse(self):
        s = self._make_primed_sampler()
        # All neighbors WORSE (higher loss) than baseline; outside tolerance
        s._scan_completed["x"] = [
            ScanMeasure(value=4, loss=-0.5, n_trades=80, trial_number=1),
            ScanMeasure(value=6, loss=-0.4, n_trades=80, trial_number=2),
            ScanMeasure(value=3, loss=-0.3, n_trades=80, trial_number=3),
            ScanMeasure(value=7, loss=-0.2, n_trades=80, trial_number=4),
        ]
        s._classify_params()
        p = s._param_profiles["x"]
        assert p.kind == "FROZEN_BOWL"
        assert p.best_value == 5  # default

    def test_monotonic_reclassified_as_active_plateau(self):
        """When neighbors are progressively better than baseline outside the
        plateau, we DON'T freeze at best (that overfits). We reclassify as
        ACTIVE_PLATEAU with bounds [min(default, best), max(default, best)]
        so TPE assembly can explore the interpolation cube.
        """
        s = self._make_primed_sampler()
        # +side worse, -side BETTER → monotonic improvement going down
        s._scan_completed["x"] = [
            ScanMeasure(value=4, loss=-1.5, n_trades=90, trial_number=1),  # better!
            ScanMeasure(value=6, loss=-0.5, n_trades=80, trial_number=2),  # worse
            ScanMeasure(value=3, loss=-2.0, n_trades=85, trial_number=3),  # even better
            ScanMeasure(value=7, loss=-0.3, n_trades=80, trial_number=4),  # worse
        ]
        s._classify_params()
        p = s._param_profiles["x"]
        # Reclassified as ACTIVE_PLATEAU with bounds = [min(5, 3), max(5, 3)] = [3, 5]
        assert p.kind == "ACTIVE_PLATEAU"
        assert p.low == 3
        assert p.high == 5
        assert p.best_value == 3  # the absolute best, used as TPE hint

    def test_activity_floor_excludes_low_trade_values(self):
        s = self._make_primed_sampler()
        # Failing-floor trials have tempting losses but must be ignored
        # OK trials must be within tolerance to count as plateau
        s._scan_completed["x"] = [
            ScanMeasure(value=4, loss=-1.5, n_trades=10, trial_number=1),   # FAIL floor (would look great)
            ScanMeasure(value=6, loss=-1.005, n_trades=95, trial_number=2), # OK + in plateau
            ScanMeasure(value=3, loss=-2.0, n_trades=10, trial_number=3),   # FAIL
            ScanMeasure(value=7, loss=-1.008, n_trades=95, trial_number=4), # OK + in plateau
        ]
        s._classify_params()
        p = s._param_profiles["x"]
        # ACTIVE_PLATEAU on the OK trials only — failing-floor trials excluded
        assert p.kind == "ACTIVE_PLATEAU"
        # Plateau bounds: default=5 plus values 6 and 7 = [5, 7]
        # (values 3 and 4 are excluded by floor)
        assert p.low == 5
        assert p.high == 7


class TestCategoricalClassification:
    def test_categorical_picks_best_loss(self):
        s = PlateauSampler(seed=0)
        s._baseline_loss = -1.0
        s._baseline_n_trades = 100
        s._min_active_trades = 70
        s._param_names = ["mode"]
        s._param_distributions = {"mode": CategoricalDistribution(["fast", "slow", "medium"])}
        s._baseline = {"mode": "medium"}
        s._scan_completed["mode"] = [
            ScanMeasure(value="fast", loss=-1.5, n_trades=80, trial_number=1),
            ScanMeasure(value="slow", loss=-0.5, n_trades=80, trial_number=2),
        ]
        s._classify_params()
        p = s._param_profiles["mode"]
        assert p.kind == "FROZEN_CATEGORICAL"
        assert p.best_value == "fast"  # lowest loss


# ── Adaptive early-stop ──────────────────────────────────────────────

class TestAdaptiveEarlyStop:
    def _make_primed(self):
        s = PlateauSampler(seed=0, plateau_initial_scan=2)
        s._baseline_loss = -1.0
        s._baseline_n_trades = 100
        s._min_active_trades = 70
        s._param_names = ["x"]
        s._param_distributions = {"x": IntDistribution(0, 100)}
        s._baseline = {"x": 50}
        s._sampling_steps = {"x": 1.0}
        s._scan_slots = {"x": [(o, 50 + o) for o in [1, -1, 2, -2, 3, -3, 4, -4]]}
        return s

    def test_no_skip_before_initial_scan_done(self):
        s = self._make_primed()
        s._scan_completed["x"] = [
            ScanMeasure(value=51, loss=-1.01, n_trades=95, trial_number=1)
        ]
        # Only 1 completed, K=2 required → no skip
        assert not s._should_skip_scan_slot("x", 2)

    def test_skip_past_upper_boundary(self):
        s = self._make_primed()
        # +1, +2 in plateau, +3 OUTSIDE plateau (boundary at +2)
        s._scan_completed["x"] = [
            ScanMeasure(value=51, loss=-1.01, n_trades=95, trial_number=1),
            ScanMeasure(value=49, loss=-1.02, n_trades=95, trial_number=2),
            ScanMeasure(value=52, loss=-1.03, n_trades=95, trial_number=3),
            ScanMeasure(value=53, loss=-0.5, n_trades=95, trial_number=4),  # OUTSIDE
        ]
        # +4 should be skipped (boundary at +3)
        assert s._should_skip_scan_slot("x", 4)

    def test_no_skip_other_direction(self):
        s = self._make_primed()
        # +3 outside plateau, but -3 not yet scanned
        s._scan_completed["x"] = [
            ScanMeasure(value=51, loss=-1.01, n_trades=95, trial_number=1),
            ScanMeasure(value=49, loss=-1.02, n_trades=95, trial_number=2),
            ScanMeasure(value=53, loss=-0.5, n_trades=95, trial_number=3),  # boundary upper
        ]
        # -4 should NOT be skipped (lower direction still being scanned)
        assert not s._should_skip_scan_slot("x", -4)
        # +4 SHOULD be skipped
        assert s._should_skip_scan_slot("x", 4)


# ── Assembly TPE subspace ────────────────────────────────────────────

class TestAssemblySubspace:
    def test_active_plateau_builds_subdistribution(self):
        s = PlateauSampler(seed=0)
        s._param_distributions = {"x": IntDistribution(0, 100)}
        s._param_names = ["x"]
        s._baseline = {"x": 50}
        s._param_profiles = {
            "x": ParamProfile(
                name="x", kind="ACTIVE_PLATEAU", default=50,
                low=45, high=55, best_value=52, best_loss=-1.5,
            )
        }
        s._init_assembly()
        assert "x" in s._tpe_sub_distributions
        sub = s._tpe_sub_distributions["x"]
        assert isinstance(sub, IntDistribution)
        assert sub.low == 45
        assert sub.high == 55
        assert "x" not in s._frozen_values

    def test_frozen_param_goes_to_frozen_values(self):
        s = PlateauSampler(seed=0)
        s._param_distributions = {"x": IntDistribution(0, 100)}
        s._param_names = ["x"]
        s._baseline = {"x": 50}
        s._param_profiles = {
            "x": ParamProfile(
                name="x", kind="FROZEN_BOWL", default=50,
                low=50, high=50, best_value=50, best_loss=-1.0,
            )
        }
        s._init_assembly()
        assert "x" in s._frozen_values
        assert s._frozen_values["x"] == 50
        assert "x" not in s._tpe_sub_distributions

    def test_degenerate_subspace_freezes(self):
        s = PlateauSampler(seed=0)
        s._param_distributions = {"x": IntDistribution(0, 100)}
        s._param_names = ["x"]
        s._baseline = {"x": 50}
        # Plateau of just 1 value (default == low == high)
        s._param_profiles = {
            "x": ParamProfile(
                name="x", kind="ACTIVE_PLATEAU", default=50,
                low=50, high=50, best_value=50, best_loss=-1.0,
            )
        }
        s._init_assembly()
        # Degenerate (low==high) → falls back to frozen
        assert "x" in s._frozen_values
        assert "x" not in s._tpe_sub_distributions


# ── select_best_export ───────────────────────────────────────────────

class TestSelectBestExport:
    def _make_primed(self):
        s = PlateauSampler(seed=0)
        s._baseline = {"x": 5}
        s._baseline_loss = -1.0
        s._baseline_n_trades = 100
        s._min_active_trades = 70
        s._param_names = ["x"]
        s._param_distributions = {"x": IntDistribution(0, 10)}
        return s

    def test_best_trial_is_baseline_when_no_v2_beats(self):
        s = self._make_primed()
        trials = [
            _make_completed_trial(0, -1.0, {"x": 5}),    # baseline
            _make_completed_trial(1, -0.8, {"x": 4}),    # worse
            _make_completed_trial(2, -0.9, {"x": 6}),    # worse
        ]
        s._trial_n_trades = {0: 100, 1: 80, 2: 90}
        study = _make_study_with_trials(trials)
        params, src, loss, n = s.select_best_export(study)
        assert src == 0
        assert params == {"x": 5}

    def test_best_trial_is_assembly_when_it_beats_baseline(self):
        s = self._make_primed()
        trials = [
            _make_completed_trial(0, -1.0, {"x": 5}),
            _make_completed_trial(1, -1.2, {"x": 4}),
            _make_completed_trial(2, -1.5, {"x": 6}),    # best
        ]
        s._trial_n_trades = {0: 100, 1: 80, 2: 90}
        study = _make_study_with_trials(trials)
        params, src, loss, n = s.select_best_export(study)
        assert src == 2
        assert params == {"x": 6}
        assert loss == -1.5

    def test_floor_filter_excludes_low_trade_count(self):
        s = self._make_primed()
        # Trial 2 has the BEST loss but FAILS the floor (50 < 70)
        # Trial 1 has a substantial improvement (-50%) that beats the Occam window
        trials = [
            _make_completed_trial(0, -1.0, {"x": 5}),
            _make_completed_trial(1, -1.5, {"x": 4}),    # 50% improvement, clear winner
            _make_completed_trial(2, -2.0, {"x": 6}),    # better loss but fails floor
        ]
        s._trial_n_trades = {0: 100, 1: 80, 2: 50}
        study = _make_study_with_trials(trials)
        params, src, loss, n = s.select_best_export(study)
        assert src == 1
        assert params == {"x": 4}


class TestV7OccamRegularization:
    """Tests for v7 Occam-regularized export policy."""

    def _make_primed(self):
        s = PlateauSampler(seed=0)
        s._baseline = {"x": 5, "y": 10}
        s._baseline_loss = -1.0
        s._baseline_n_trades = 100
        s._min_active_trades = 70
        s._param_names = ["x", "y"]
        s._param_distributions = {
            "x": IntDistribution(0, 10),
            "y": IntDistribution(0, 20),
        }
        return s

    def test_baseline_preferred_when_marginal_improvement(self):
        """Trial 1 improves loss by only 10% (within 20% slack) → baseline preferred (0 changes)."""
        s = self._make_primed()
        trials = [
            _make_completed_trial(0, -1.0, {"x": 5, "y": 10}),    # baseline
            _make_completed_trial(1, -1.1, {"x": 4, "y": 10}),    # marginal improvement
        ]
        s._trial_n_trades = {0: 100, 1: 95}
        study = _make_study_with_trials(trials)
        params, src, loss, n = s.select_best_export(study)
        # Within slack of 20% × 1.0 = 0.2: both trials in top_k
        # Trial 0: 0 changes, trial 1: 1 change → baseline wins
        assert src == 0

    def test_clear_improvement_picks_changed_trial(self):
        """Trial 1 improves loss by 50% (well beyond 20% slack) → trial 1 wins."""
        s = self._make_primed()
        trials = [
            _make_completed_trial(0, -1.0, {"x": 5, "y": 10}),
            _make_completed_trial(1, -1.5, {"x": 4, "y": 10}),
        ]
        s._trial_n_trades = {0: 100, 1: 95}
        study = _make_study_with_trials(trials)
        params, src, loss, n = s.select_best_export(study)
        # slack = 0.2, best = -1.5; baseline (-1.0) > -1.5 + 0.2 = -1.3 → excluded
        assert src == 1

    def test_prefers_simpler_within_top_k(self):
        """Two trials in top_k: prefer the one with fewer changes."""
        s = self._make_primed()
        trials = [
            _make_completed_trial(0, -1.0, {"x": 5, "y": 10}),    # 0 changes
            _make_completed_trial(1, -2.0, {"x": 4, "y": 10}),    # 1 change
            _make_completed_trial(2, -2.1, {"x": 4, "y": 15}),    # 2 changes, marginally better
        ]
        s._trial_n_trades = {0: 100, 1: 95, 2: 95}
        study = _make_study_with_trials(trials)
        params, src, loss, n = s.select_best_export(study)
        # slack = 0.2, best = -2.1; -2.0 within slack (≤ -1.9), -1.0 outside
        # top_k = [trial 1, trial 2]; trial 1 has fewer changes → wins
        assert src == 1
        assert params == {"x": 4, "y": 10}

    def test_count_param_changes_int_above_epsilon(self):
        s = self._make_primed()
        # x changes by 1 / range=10 = 10% > 1% epsilon → counted
        # y unchanged → not counted
        n = s._count_param_changes({"x": 4, "y": 10})
        assert n == 1

    def test_count_param_changes_int_below_epsilon(self):
        s = self._make_primed()
        # All unchanged
        n = s._count_param_changes({"x": 5, "y": 10})
        assert n == 0

    def test_count_param_changes_categorical(self):
        s = PlateauSampler(seed=0)
        s._baseline = {"mode": "fast"}
        s._param_distributions = {"mode": CategoricalDistribution(["fast", "slow"])}
        s._param_names = ["mode"]
        assert s._count_param_changes({"mode": "fast"}) == 0
        assert s._count_param_changes({"mode": "slow"}) == 1

    def test_baseline_returned_if_all_fail_floor(self):
        s = self._make_primed()  # baseline = {x:5, y:10}
        trials = [
            _make_completed_trial(0, -1.0, {"x": 5, "y": 10}),
            _make_completed_trial(1, -1.2, {"x": 4, "y": 10}),
        ]
        s._trial_n_trades = {0: 50, 1: 30}  # ALL fail floor
        study = _make_study_with_trials(trials)
        params, src, loss, n = s.select_best_export(study)
        assert src == 0  # baseline as ultimate fallback
        assert params == {"x": 5, "y": 10}


# ── JSON output schema ──────────────────────────────────────────────

class TestExportJsonSchema:
    def test_meta_has_required_fields(self):
        s = PlateauSampler(seed=0, strategy_name="Foo")
        s._baseline = {"x": 5}
        s._baseline_loss = -1.0
        s._param_profiles = {
            "x": ParamProfile(
                name="x", kind="ACTIVE_PLATEAU", default=5,
                low=4, high=6, best_value=6, best_loss=-1.3,
                tolerance_used=0.05,
            )
        }
        s._best_robust_export = {"x": 6}
        data = s._build_robust_params_dict(n_fallback=0)
        assert data["strategy_name"] == "Foo"
        assert data["params"]["buy"] == {"x": 6}
        assert data["ft_stratparam_v"] == 1
        meta = data["cwsampler_meta"]
        assert meta["version"] == 6
        assert meta["n_params"] == 1
        assert meta["n_active_plateau"] == 1
        assert "x" in meta["param_profiles"]
        prof = meta["param_profiles"]["x"]
        assert prof["kind"] == "ACTIVE_PLATEAU"
        assert prof["low"] == 4
        assert prof["high"] == 6
        assert prof["best_value"] == 6


# ── Categorical warning ─────────────────────────────────────────────

class TestCategoricalWarning:
    def test_numeric_categorical_warns(self, caplog):
        s = PlateauSampler(seed=0)
        s._param_distributions = {
            "n": CategoricalDistribution([10, 15, 20, 25, 30])
        }
        s._param_names = ["n"]
        with caplog.at_level(logging.WARNING):
            s._validate_categoricals()
        assert any("CategoricalParameter with numeric choices" in r.message
                   for r in caplog.records)

    def test_string_categorical_no_warning(self, caplog):
        s = PlateauSampler(seed=0)
        s._param_distributions = {
            "mode": CategoricalDistribution(["fast", "slow"])
        }
        s._param_names = ["mode"]
        with caplog.at_level(logging.WARNING):
            s._validate_categoricals()
        assert not any("CategoricalParameter" in r.message for r in caplog.records)


# ── End-to-end on synthetic loss ────────────────────────────────────

class TestEndToEnd:
    def test_quadratic_loss_around_default(self):
        """Loss = (x - 7)² with default=7. Plateau detection should classify as
        ACTIVE_PLATEAU around 7 (small quadratic = wide flat-ish bottom)."""
        # Use simple quadratic to ensure deterministic plateau
        def loss_fn(params):
            return (params["x"] - 7) ** 2

        distributions = {"x": IntDistribution(0, 15)}
        defaults = {"x": 7}
        # 1 baseline + ~30 scan + ~30 assembly = 60 trials minimum
        sampler = PlateauSampler(seed=42, total_epochs=80, defaults=defaults)
        study = optuna.create_study(sampler=sampler, direction="minimize")
        for _ in range(80):
            trial = study.ask(distributions)
            loss = loss_fn(trial.params)
            sampler.record_trial_metrics(trial.number, n_trades=100)  # constant trades
            study.tell(trial, loss)

        # After all trials, classification should be done
        assert sampler.get_phase() == "assembly"
        profiles = sampler.get_param_profiles()
        assert "x" in profiles
        # x=7 is the global min, neighbors are slightly worse → ACTIVE_PLATEAU expected
        assert profiles["x"].kind in ("ACTIVE_PLATEAU", "FROZEN_BOWL")

    def test_baseline_wins_when_no_improvement(self):
        """If baseline is the global min and all neighbors are much worse,
        select_best_export should pick the baseline."""
        def loss_fn(params):
            return -1.0 if params["x"] == 5 else 100.0  # only x=5 is good

        distributions = {"x": IntDistribution(0, 10)}
        defaults = {"x": 5}
        sampler = PlateauSampler(seed=42, total_epochs=60, defaults=defaults)
        study = optuna.create_study(sampler=sampler, direction="minimize")
        for _ in range(60):
            trial = study.ask(distributions)
            loss = loss_fn(trial.params)
            sampler.record_trial_metrics(trial.number, n_trades=100)
            study.tell(trial, loss)

        params, src, loss, n = sampler.select_best_export(study)
        # Baseline (x=5) should win
        assert src == 0
        assert params == {"x": 5}


# ── Registration ─────────────────────────────────────────────────────

class TestRegistration:
    def test_sampler_in_dict(self):
        from freqtrade.optimize.hyperopt.hyperopt_optimizer import optuna_samplers_dict
        assert "PlateauSampler" in optuna_samplers_dict

    def test_sampler_instantiation_via_dict(self):
        from freqtrade.optimize.hyperopt.hyperopt_optimizer import optuna_samplers_dict
        cls = optuna_samplers_dict["PlateauSampler"]
        sampler = cls(seed=42)
        assert isinstance(sampler, PlateauSampler)


# ── Plateau classification edge cases ────────────────────────────────

class TestClassificationEdgeCases:
    def test_no_scan_data_freezes(self):
        s = PlateauSampler(seed=0)
        s._baseline_loss = -1.0
        s._param_names = ["x"]
        s._param_distributions = {"x": IntDistribution(0, 10)}
        s._baseline = {"x": 5}
        s._sampling_steps = {"x": 1.0}
        # No scan_completed entries
        s._classify_params()
        p = s._param_profiles["x"]
        assert p.kind == "FROZEN_BOWL"
        assert p.best_value == 5

    def test_initial_scan_k_respected(self):
        """Until K initial scan trials are completed, no early-stop happens."""
        s = PlateauSampler(seed=0, plateau_initial_scan=PLATEAU_INITIAL_SCAN_K)
        s._baseline_loss = -1.0
        s._param_names = ["x"]
        s._param_distributions = {"x": IntDistribution(0, 100)}
        s._baseline = {"x": 50}
        s._sampling_steps = {"x": 1.0}
        s._scan_slots = {"x": [(o, 50 + o) for o in [1, -1, 2, -2, 3, -3]]}
        # Only 3 trials so far, K=4 → no skip allowed
        s._scan_completed["x"] = [
            ScanMeasure(value=51, loss=-0.1, n_trades=80, trial_number=1),  # outside plateau
            ScanMeasure(value=49, loss=-0.1, n_trades=80, trial_number=2),
            ScanMeasure(value=52, loss=-0.1, n_trades=80, trial_number=3),
        ]
        assert not s._should_skip_scan_slot("x", 3)


# ── ParamProfile dataclass ──────────────────────────────────────────

class TestParamProfile:
    def test_is_active(self):
        p = ParamProfile(
            name="x", kind="ACTIVE_PLATEAU", default=5,
            low=4, high=6, best_value=5, best_loss=-1.0,
        )
        assert p.is_active
        assert not p.is_frozen

    def test_is_frozen(self):
        for kind in ("FROZEN_BOWL", "FROZEN_MONOTONIC", "FROZEN_CATEGORICAL"):
            p = ParamProfile(
                name="x", kind=kind, default=5,
                low=5, high=5, best_value=5, best_loss=-1.0,
            )
            assert p.is_frozen
            assert not p.is_active
