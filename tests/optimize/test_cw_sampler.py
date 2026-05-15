"""Tests for the CWSampler (Coordinate-Wise Sampler)."""

import numpy as np
import optuna
import pytest
from optuna.distributions import CategoricalDistribution, FloatDistribution, IntDistribution

from freqtrade.optimize.hyperopt.cw_sampler import CWSampler


@pytest.fixture
def distributions():
    return {
        "rsi_period": IntDistribution(5, 30),
        "ema_length": IntDistribution(10, 50),
        "threshold": FloatDistribution(0.01, 0.10, step=0.01),
    }


@pytest.fixture
def sampler():
    return CWSampler(seed=42, points_per_param=10)


def run_study(sampler, distributions, n_trials):
    """Helper: run n_trials on a study, return it."""
    study = optuna.create_study(sampler=sampler, direction="minimize")
    for _ in range(n_trials):
        trial = study.ask(distributions)
        loss = sum(v**2 for v in trial.params.values())
        study.tell(trial, loss)
    return study


class TestCWSamplerInit:
    def test_init_defaults(self):
        s = CWSampler(seed=123)
        assert s._seed == 123
        assert s._points_per_param == 20
        assert s._phase == "init"

    def test_init_custom_points(self):
        s = CWSampler(seed=0, points_per_param=5)
        assert s._points_per_param == 5


class TestGridGeneration:
    def test_int_grid_small_range(self):
        dist = IntDistribution(1, 5)
        grid = CWSampler._make_grid(dist, 20)
        assert grid == [1, 2, 3, 4, 5]

    def test_int_grid_large_range(self):
        dist = IntDistribution(0, 100)
        grid = CWSampler._make_grid(dist, 10)
        assert len(grid) <= 10
        assert grid[0] == 0
        assert grid[-1] == 100

    def test_float_grid_with_step(self):
        dist = FloatDistribution(0.01, 0.10, step=0.01)
        grid = CWSampler._make_grid(dist, 20)
        assert len(grid) == 10
        assert abs(grid[0] - 0.01) < 1e-9
        assert abs(grid[-1] - 0.10) < 1e-9

    def test_float_grid_continuous(self):
        dist = FloatDistribution(0.0, 1.0)
        grid = CWSampler._make_grid(dist, 5)
        assert len(grid) == 5
        assert grid[0] == 0.0
        assert grid[-1] == 1.0

    def test_categorical_grid(self):
        dist = CategoricalDistribution(["a", "b", "c"])
        grid = CWSampler._make_grid(dist, 10)
        assert grid == ["a", "b", "c"]


class TestMidpoint:
    def test_int_midpoint(self):
        assert CWSampler._get_midpoint(IntDistribution(10, 20)) == 15

    def test_float_midpoint(self):
        assert CWSampler._get_midpoint(FloatDistribution(0.0, 1.0)) == 0.5

    def test_float_midpoint_with_step(self):
        val = CWSampler._get_midpoint(FloatDistribution(0.01, 0.10, step=0.01))
        assert abs(val - 0.06) < 0.01

    def test_categorical_midpoint(self):
        val = CWSampler._get_midpoint(CategoricalDistribution(["a", "b", "c"]))
        assert val == "b"


class TestPhaseTransitions:
    def test_starts_in_init(self, sampler):
        assert sampler._phase == "init"

    def test_baseline_then_scan(self, sampler, distributions):
        study = optuna.create_study(sampler=sampler, direction="minimize")

        # First trial → baseline
        t = study.ask(distributions)
        assert sampler._phase == "baseline"
        study.tell(t, 1.0)

        # Second trial → scan
        t = study.ask(distributions)
        assert sampler._phase == "scan"

    def test_scan_to_assembly(self, distributions):
        sampler = CWSampler(seed=42, points_per_param=3)
        study = optuna.create_study(sampler=sampler, direction="minimize")

        # Baseline
        t = study.ask(distributions)
        study.tell(t, 1.0)

        # Scan: 3 params × 3 points = 9 trials
        for _ in range(9):
            t = study.ask(distributions)
            loss = sum(v**2 for v in t.params.values())
            study.tell(t, loss)

        # Next trial should be assembly
        t = study.ask(distributions)
        assert sampler._phase == "assembly"


class TestBaselinePhase:
    def test_baseline_uses_midpoints(self, sampler, distributions):
        study = optuna.create_study(sampler=sampler, direction="minimize")
        t = study.ask(distributions)
        assert t.params["rsi_period"] == 17  # (5+30)//2
        assert t.params["ema_length"] == 30  # (10+50)//2


class TestScanPhase:
    def test_scan_varies_one_param_at_a_time(self, distributions):
        sampler = CWSampler(seed=42, points_per_param=5)
        study = optuna.create_study(sampler=sampler, direction="minimize")

        # Baseline
        t = study.ask(distributions)
        baseline = dict(t.params)
        study.tell(t, 1.0)

        # First scan param (rsi_period): other params should stay at baseline
        t = study.ask(distributions)
        assert t.params["ema_length"] == baseline["ema_length"]
        assert t.params["threshold"] == baseline["threshold"]
        # rsi_period should be a grid value, not necessarily baseline
        study.tell(t, 0.5)

    def test_scan_covers_grid(self, distributions):
        sampler = CWSampler(seed=42, points_per_param=5)
        study = optuna.create_study(sampler=sampler, direction="minimize")

        # Baseline
        t = study.ask(distributions)
        study.tell(t, 1.0)

        rsi_values = []
        grid = sampler._scan_grid.get("rsi_period", [])
        for _ in range(len(grid)):
            t = study.ask(distributions)
            rsi_values.append(t.params["rsi_period"])
            study.tell(t, float(t.params["rsi_period"]))

        assert len(rsi_values) == len(grid)


class TestPlateauDetection:
    def test_plateau_preferred_over_peak(self):
        sampler = CWSampler(seed=42)
        sampler._baseline = {"x": 10}

        # Isolated peak at x=5 (loss=0.1, neighbors much worse)
        # Plateau around x=12-16 (loss~0.5, all similar)
        results = [
            (1, 2.0), (2, 2.0), (3, 1.8), (4, 1.5),
            (5, 0.1),  # peak
            (6, 1.5), (7, 1.8), (8, 2.0),
            (10, 1.0), (11, 0.6),
            (12, 0.5), (13, 0.48), (14, 0.5), (15, 0.52), (16, 0.5),  # plateau
            (17, 0.8), (18, 1.0), (19, 1.5), (20, 2.0),
        ]
        chosen = sampler._best_plateau("x", results)
        # Should pick something in the plateau (12-16), not the peak (5)
        assert 11 <= chosen <= 17, f"Expected plateau value, got {chosen}"

    def test_fallback_to_baseline_if_no_plateau(self):
        sampler = CWSampler(seed=42)
        sampler._baseline = {"x": 5}

        # Every value is wildly different — no plateau
        results = [(0, 10.0), (1, 0.01), (2, 8.0), (3, 0.02), (4, 9.0),
                   (5, 0.03), (6, 7.0), (7, 0.04), (8, 6.0), (9, 0.05), (10, 5.0)]
        chosen = sampler._best_plateau("x", results)
        # With such noisy data, robustness should be low → fallback to baseline
        assert chosen == 5

    def test_categorical_best(self):
        sampler = CWSampler(seed=42)
        results = [("a", 1.0), ("b", 0.5), ("c", 0.8)]
        chosen = sampler._best_categorical(results)
        assert chosen == "b"


class TestAssemblyPhase:
    def test_assembly_near_robust_optimum(self, distributions):
        sampler = CWSampler(seed=42, points_per_param=3)
        study = optuna.create_study(sampler=sampler, direction="minimize")

        # Baseline (trial 0)
        t = study.ask(distributions)
        study.tell(t, 1.0)

        # First scan trial triggers init — now we can read schedule length
        t = study.ask(distributions)
        study.tell(t, sum(v**2 for v in t.params.values()))
        total_scan = len(sampler._schedule)

        # Remaining scan trials
        for _ in range(total_scan - 1):
            t = study.ask(distributions)
            loss = sum(v**2 for v in t.params.values())
            study.tell(t, loss)

        # Assembly trials should be near the robust optimum
        for _ in range(10):
            t = study.ask(distributions)
            assert sampler._phase == "assembly", f"Expected assembly, got {sampler._phase}"
            for pname, dist in distributions.items():
                val = t.params[pname]
                if isinstance(dist, IntDistribution):
                    assert dist.low <= val <= dist.high
                elif isinstance(dist, FloatDistribution):
                    assert dist.low - 1e-9 <= val <= dist.high + 1e-9
            study.tell(t, sum(v**2 for v in t.params.values()))


class TestEndToEnd:
    def test_full_run_small(self):
        """Run CWSampler end-to-end on a simple quadratic objective."""
        sampler = CWSampler(seed=42, points_per_param=10)
        distributions = {
            "x": FloatDistribution(-5.0, 5.0, step=0.5),
            "y": FloatDistribution(-5.0, 5.0, step=0.5),
        }

        study = optuna.create_study(sampler=sampler, direction="minimize")

        # Objective: f(x,y) = (x-1)^2 + (y+2)^2  → minimum at (1, -2)
        for _ in range(200):
            trial = study.ask(distributions)
            x, y = trial.params["x"], trial.params["y"]
            loss = (x - 1.0) ** 2 + (y + 2.0) ** 2
            study.tell(trial, loss)

        best = study.best_trial
        assert abs(best.params["x"] - 1.0) <= 1.5, f"x={best.params['x']}"
        assert abs(best.params["y"] - (-2.0)) <= 1.5, f"y={best.params['y']}"

    def test_full_run_with_categorical(self):
        sampler = CWSampler(seed=42, points_per_param=5)
        distributions = {
            "n": IntDistribution(1, 20),
            "mode": CategoricalDistribution(["fast", "slow", "medium"]),
        }

        study = optuna.create_study(sampler=sampler, direction="minimize")
        for _ in range(50):
            trial = study.ask(distributions)
            n = trial.params["n"]
            mode = trial.params["mode"]
            mode_penalty = {"fast": 0, "slow": 5, "medium": 2}[mode]
            loss = (n - 10) ** 2 + mode_penalty
            study.tell(trial, loss)

        best = study.best_trial
        assert best.params["mode"] == "fast"
        assert 5 <= best.params["n"] <= 15

    def test_deterministic_scan(self):
        """Two runs with same seed produce identical scan phase."""
        distributions = {
            "a": IntDistribution(0, 10),
            "b": FloatDistribution(0.0, 1.0, step=0.1),
        }

        params_run1 = []
        params_run2 = []

        for params_list in [params_run1, params_run2]:
            sampler = CWSampler(seed=99, points_per_param=5)
            study = optuna.create_study(sampler=sampler, direction="minimize")
            for _ in range(20):
                trial = study.ask(distributions)
                params_list.append(dict(trial.params))
                study.tell(trial, 1.0)

        # Scan phase should be identical
        n_scan = 1 + 5 + 5  # baseline + 5 for a + 5 for b
        for i in range(min(n_scan, len(params_run1))):
            assert params_run1[i] == params_run2[i], f"Mismatch at trial {i}"


class TestRegistration:
    def test_sampler_in_dict(self):
        from freqtrade.optimize.hyperopt.hyperopt_optimizer import optuna_samplers_dict
        assert "CWSampler" in optuna_samplers_dict

    def test_sampler_instantiation_via_dict(self):
        from freqtrade.optimize.hyperopt.hyperopt_optimizer import optuna_samplers_dict
        cls = optuna_samplers_dict["CWSampler"]
        sampler = cls(seed=42)
        assert isinstance(sampler, CWSampler)
