# pragma pylint: disable=missing-docstring,W0212,C0103
from __future__ import annotations


class TestHyperoptGlossary:
    def test_new_metric_entries_exist(self):
        from freqtrade.optimize.wfa_glossary import METRIC_GLOSSARY

        new_slugs = {
            "sharpe",
            "sortino",
            "win_rate",
            "payoff_ratio",
            "cagr",
            "skewness",
            "kurtosis",
            "profit_concentration",
            "expected_max_sharpe",
        }
        assert new_slugs.issubset(set(METRIC_GLOSSARY.keys()))

    def test_sampler_glossary_completeness(self):
        from freqtrade.optimize.wfa_glossary import SAMPLER_GLOSSARY

        expected = {
            "NSGAIIISampler",
            "NSGAIISampler",
            "TPESampler",
            "CmaEsSampler",
            "GPSampler",
            "QMCSampler",
        }
        assert expected == set(SAMPLER_GLOSSARY.keys())

    def test_sampler_glossary_structure(self):
        from freqtrade.optimize.wfa_glossary import SAMPLER_GLOSSARY

        for name, entry in SAMPLER_GLOSSARY.items():
            assert "name" in entry, f"{name} missing 'name'"
            assert "one_liner" in entry, f"{name} missing 'one_liner'"
            assert "when_to_use" in entry, f"{name} missing 'when_to_use'"

    def test_loss_glossary_completeness(self):
        from freqtrade.optimize.wfa_glossary import LOSS_GLOSSARY

        assert "MoutonMeanRevHyperOptLoss" in LOSS_GLOSSARY
        assert "MoutonMomentumHyperOptLoss" in LOSS_GLOSSARY
        assert "MyProfitDrawDownHyperOptLoss" in LOSS_GLOSSARY
        assert "SharpeHyperOptLoss" in LOSS_GLOSSARY
        assert "CalmarHyperOptLoss" in LOSS_GLOSSARY
        assert len(LOSS_GLOSSARY) >= 15

    def test_loss_glossary_structure(self):
        from freqtrade.optimize.wfa_glossary import LOSS_GLOSSARY

        required = {"name", "one_liner", "best_for", "metrics", "hard_filters"}
        for name, entry in LOSS_GLOSSARY.items():
            missing = required - set(entry.keys())
            assert not missing, f"{name} missing keys: {missing}"

    def test_hyperopt_next_steps(self):
        from freqtrade.optimize.wfa_glossary import HYPEROPT_NEXT_STEPS

        assert "profitable" in HYPEROPT_NEXT_STEPS
        assert "unprofitable" in HYPEROPT_NEXT_STEPS
        assert "high_dd" in HYPEROPT_NEXT_STEPS
        assert "low_trades" in HYPEROPT_NEXT_STEPS


class TestHyperoptHTMLReport:
    def _make_data(self, **overrides):
        data = {
            "strategy": "TestStrategy",
            "hyperopt_loss": "CalmarHyperOptLoss",
            "sampler": "NSGAIIISampler",
            "total_epochs": 100,
            "timestamp": "2024-01-01T00:00:00",
            "stake_currency": "USDT",
            "best_epoch": {
                "total_trades": 50,
                "wins": 30,
                "draws": 5,
                "losses": 15,
                "profit_total": 0.15,
                "profit_total_abs": 150.0,
                "profit_mean": 0.003,
                "max_drawdown_account": 0.12,
                "max_drawdown_abs": 120.0,
                "holding_avg": "2:30:00",
                "calmar": 2.5,
                "sqn": 1.8,
                "sharpe": 1.5,
                "sortino": 2.2,
                "profit_factor": 1.6,
                "winrate": 0.60,
                "expectancy": 3.0,
                "expectancy_ratio": 0.07,
            },
            "best_params": {
                "buy": {"rsi": 28, "volume_pct": 0.3},
                "sell": {"exit_rsi": 75},
            },
            "top_epochs": [
                {
                    "loss": 0.5,
                    "results_metrics": {
                        "total_trades": 50,
                        "profit_total": 0.15,
                        "max_drawdown_account": 0.12,
                        "calmar": 2.5,
                        "sharpe": 1.5,
                        "profit_factor": 1.6,
                    },
                    "params_dict": {"rsi": 28, "volume_pct": 0.3},
                },
                {
                    "loss": 0.6,
                    "results_metrics": {
                        "total_trades": 45,
                        "profit_total": 0.12,
                        "max_drawdown_account": 0.10,
                        "calmar": 2.0,
                        "sharpe": 1.3,
                        "profit_factor": 1.4,
                    },
                    "params_dict": {"rsi": 30, "volume_pct": 0.25},
                },
            ],
            "all_losses": [0.9, 0.8, 0.7, 0.6, 0.5, 0.55, 0.52],
            "param_stability": {
                "rsi": {
                    "values": [28, 30],
                    "median": 29,
                    "std": 1.41,
                    "std_over_range": 0.10,
                    "stable": True,
                    "unstable": False,
                },
            },
            "config_summary": {
                "epochs": 100,
                "spaces": ["buy", "sell"],
                "min_trades": 30,
                "timerange": "20230101-20250101",
                "timeframe": "15m",
                "jobs": -1,
                "random_state": None,
                "analyze_per_epoch": False,
                "print_all": False,
                "print_json": False,
            },
            "elapsed_secs": 3723.5,
            "dsr_analysis": {
                "observed_sharpe": 1.5,
                "expected_max_sharpe": 3.03,
                "n_trials": 100,
                "genuine": False,
            },
            "distribution_analysis": {
                "skewness": -1.2,
                "excess_kurtosis": 4.5,
                "n_trades": 50,
                "skew_alert": True,
                "kurtosis_alert": True,
            },
            "sans_top_trade": {
                "total_profit": 150.0,
                "without_top1": 120.0,
                "without_top1_pct": 80.0,
                "without_top2": 95.0,
                "without_top2_pct": 63.3,
                "fragile": False,
            },
            "pair_profit_distribution": [
                {
                    "pair": "BTC/USDT",
                    "profit_abs": 80.0,
                    "trade_count": 25,
                    "win_rate": 0.72,
                    "avg_profit": 3.2,
                },
                {
                    "pair": "ETH/USDT",
                    "profit_abs": 50.0,
                    "trade_count": 18,
                    "win_rate": 0.61,
                    "avg_profit": 2.78,
                },
                {
                    "pair": "SOL/USDT",
                    "profit_abs": -10.0,
                    "trade_count": 7,
                    "win_rate": 0.29,
                    "avg_profit": -1.43,
                },
            ],
            "best_vs_median_gap": {
                "best_profit": 15.0,
                "median_profit": 6.0,
                "gap_ratio": 2.5,
                "outlier": True,
            },
            "param_correlation": [
                {"param_a": "rsi", "param_b": "volume_pct", "correlation": 0.85},
            ],
            "loss_histogram": {
                "bins": [
                    {"lo": 0.0, "hi": 0.1, "count": 5},
                    {"lo": 0.1, "hi": 0.2, "count": 15},
                    {"lo": 0.2, "hi": 0.3, "count": 25},
                    {"lo": 0.3, "hi": 0.4, "count": 20},
                    {"lo": 0.4, "hi": 0.5, "count": 10},
                ],
                "best_loss": 0.05,
                "best_percentile": 93.0,
            },
            "parallel_coords": {
                "params": ["rsi", "volume_pct", "exit_rsi"],
                "lines": [
                    {"values": {"rsi": 0.3, "volume_pct": 0.6, "exit_rsi": 0.8}, "loss": 0.5},
                    {"values": {"rsi": 0.4, "volume_pct": 0.5, "exit_rsi": 0.7}, "loss": 0.6},
                    {"values": {"rsi": 0.5, "volume_pct": 0.4, "exit_rsi": 0.6}, "loss": 0.7},
                    {"values": {"rsi": 0.6, "volume_pct": 0.3, "exit_rsi": 0.5}, "loss": 0.8},
                ],
            },
            "benchmark_comparison": {
                "sharpe": {"value": 1.5, "benchmark": 0.85, "above": True},
                "dd": {"value": 12.0, "benchmark": 25, "above": False},
                "cagr": {"value": 20.0, "benchmark": 15, "above": True},
            },
            "dispersion_bands": {
                "profit": {"min": 5.0, "median": 12.0, "max": 18.0},
                "drawdown": {"min": 8.0, "median": 15.0, "max": 25.0},
                "sharpe": {"min": 0.8, "median": 1.3, "max": 1.8},
            },
            "epoch_dd_data": [0.1, 0.2, 0.15, 0.25, 0.08, 0.35, 0.12],
            "overfit_warnings": [
                {
                    "severity": "high",
                    "warning_type": "dsr",
                    "title_en": "DSR: Sharpe likely overfitted",
                    "title_fr": "DSR : Sharpe probablement surajusté",
                    "detail_en": "Observed Sharpe below expected max from N trials.",
                    "detail_fr": "Sharpe observé inférieur au max attendu sur N essais.",
                    "actions_en": [
                        "Reduce total epochs",
                        "Use walk-forward validation",
                    ],
                    "actions_fr": [
                        "Réduire le nombre d'epochs",
                        "Utiliser la validation walk-forward",
                    ],
                    "values": {
                        "Sharpe": "1.50",
                        "E[max SR]": "3.03",
                        "N trials": "100",
                    },
                },
                {
                    "severity": "medium",
                    "warning_type": "dof",
                    "title_en": "Low Degrees of Freedom",
                    "title_fr": "Faible degrés de liberté",
                    "detail_en": "Trades/params ratio is low.",
                    "detail_fr": "Le ratio trades/paramètres est faible.",
                    "actions_en": ["Increase min-trades"],
                    "actions_fr": ["Augmenter min-trades"],
                    "values": {
                        "trades/params": "50/10",
                        "ratio": 5.0,
                    },
                },
            ],
            "param_deep_dive": {
                "rsi": {
                    "type": "Int",
                    "best_value": 28,
                    "range_low": 10,
                    "range_high": 50,
                    "categories": None,
                    "top10_min": 25,
                    "top10_max": 33,
                    "top10_median": 29,
                    "top10_std": 2.5,
                    "tendency": "converging",
                    "sensitivity": 0.72,
                    "sensitivity_label": "high",
                    "boundary_cluster": False,
                    "histogram": [
                        {"lo": 10, "hi": 20, "count": 3},
                        {"lo": 20, "hi": 30, "count": 15},
                        {"lo": 30, "hi": 40, "count": 8},
                        {"lo": 40, "hi": 50, "count": 2},
                    ],
                    "category_counts": {},
                },
                "volume_pct": {
                    "type": "Float",
                    "best_value": 0.3,
                    "range_low": 0.1,
                    "range_high": 0.9,
                    "categories": None,
                    "top10_min": 0.2,
                    "top10_max": 0.7,
                    "top10_median": 0.4,
                    "top10_std": 0.18,
                    "tendency": "spread",
                    "sensitivity": 0.15,
                    "sensitivity_label": "low",
                    "boundary_cluster": False,
                    "histogram": [
                        {"lo": 0.1, "hi": 0.3, "count": 8},
                        {"lo": 0.3, "hi": 0.5, "count": 12},
                        {"lo": 0.5, "hi": 0.7, "count": 6},
                        {"lo": 0.7, "hi": 0.9, "count": 2},
                    ],
                    "category_counts": {},
                },
            },
            "total_pairs": 20,
            "param_stats": {
                "rsi": {"median": 29.0, "mean": 29.0},
                "volume_pct": {"median": 0.275, "mean": 0.275},
            },
            "best_params_raw": {"rsi": 28, "volume_pct": 0.3},
            "best_loss": 0.05,
            "monte_carlo": {
                "p5": -2.5,
                "p25": 3.1,
                "p50": 8.4,
                "p75": 13.2,
                "p95": 22.1,
                "mean": 8.8,
                "n_simulations": 1000,
                "n_trades": 50,
                "prob_positive": 82.5,
            },
            "sensitivity_grid": [
                {
                    "param_a": "rsi",
                    "param_b": "volume_pct",
                    "grid": [
                        [0.5, 0.6, None],
                        [0.4, 0.3, 0.5],
                        [None, 0.4, 0.6],
                    ],
                    "a_range": [10, 50],
                    "b_range": [0.1, 0.9],
                    "n_bins": 3,
                }
            ],
            "regime_analysis": {
                "first_half": {
                    "trades": 25,
                    "profit_pct": 9.5,
                    "profit_abs": 95.0,
                    "win_rate": 64.0,
                    "avg_profit": 0.38,
                },
                "second_half": {
                    "trades": 25,
                    "profit_pct": 5.5,
                    "profit_abs": 55.0,
                    "win_rate": 56.0,
                    "avg_profit": 0.22,
                },
                "first_label": "First half",
                "second_label": "Second half",
                "consistent": True,
            },
            "return_vs_dd": [
                {"profit_pct": 15.0, "dd_pct": 12.0, "trades": 50, "loss": 0.5},
                {"profit_pct": 12.0, "dd_pct": 10.0, "trades": 45, "loss": 0.6},
                {"profit_pct": 8.0, "dd_pct": 18.0, "trades": 40, "loss": 0.8},
                {"profit_pct": -2.0, "dd_pct": 25.0, "trades": 35, "loss": 1.2},
            ],
            "dof_analysis": {
                "n_trades": 50,
                "n_params": 5,
                "ratio": 10.0,
                "level": "yellow",
                "label": "Marginal",
            },
        }
        data.update(overrides)
        return data

    def test_generate_basic(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        result = generate_hyperopt_html_report(data, out)
        assert result == out
        assert out.exists()
        content = out.read_text()
        assert "<!DOCTYPE html>" in content
        assert "</html>" in content

    def test_contains_best_epoch(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Best Epoch" in content or "best" in content.lower()
        assert "Calmar" in content
        assert "Sharpe" in content

    def test_convergence_chart_svg(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "<svg" in content
        assert "Epoch" in content

    def test_no_svg_with_empty_losses(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            all_losses=[],
            pair_profit_distribution=[],
            loss_histogram=None,
            param_correlation=[],
            parallel_coords={},
            dispersion_bands={},
            epoch_dd_data=[],
            param_deep_dive=None,
            overfit_warnings=[],
            sans_top_trade=None,
            best_vs_median_gap=None,
            monte_carlo=None,
            dof_analysis=None,
            return_vs_dd=[],
            regime_analysis=None,
            sensitivity_grid=[],
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "<svg" not in content

    def test_self_contained_no_external(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        stripped = content.replace("http://www.w3.org/2000/svg", "")
        assert "http://" not in stripped
        assert "https://" not in stripped
        assert "<script" not in content

    def test_html_escaping(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(strategy="<script>alert('xss')</script>")
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "<script>alert" not in content
        assert "&lt;script&gt;" in content

    def test_loss_explanation_section(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "CalmarHyperOptLoss" in content
        assert "Loss Function" in content or "loss" in content.lower()

    def test_sampler_explanation_section(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "NSGAIIISampler" in content or "NSGA" in content

    def test_glossary_section(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Glossary" in content

    def test_contains_tooltips(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert 'class="tooltip"' in content

    def test_param_stability(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "rsi" in content
        assert "stable" in content.lower()

    def test_next_steps_section(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        low = content.lower()
        assert "next" in low or "walk-forward" in low

    def test_no_sampler_section_when_none(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(sampler=None)
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Sampler" not in content or "sampler" in content.lower()

    # ------------------------------------------------------------------
    # Phase 4 — New metric/chart tests
    # ------------------------------------------------------------------

    def test_dsr_badge_overfit(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            dsr_analysis={
                "observed_sharpe": 1.0,
                "expected_max_sharpe": 3.03,
                "n_trials": 100,
                "genuine": False,
            }
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "likely overfitted" in content
        assert "#ef4444" in content

    def test_dsr_badge_genuine(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            dsr_analysis={
                "observed_sharpe": 4.0,
                "expected_max_sharpe": 3.03,
                "n_trials": 100,
                "genuine": True,
            }
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "likely genuine" in content
        assert "#22c55e" in content

    def test_skew_alert_negative(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            distribution_analysis={
                "skewness": -1.5,
                "excess_kurtosis": 1.0,
                "n_trades": 50,
                "skew_alert": True,
                "kurtosis_alert": False,
            }
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "tail risk" in content
        assert "-1.50" in content

    def test_kurtosis_alert(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            distribution_analysis={
                "skewness": 0.0,
                "excess_kurtosis": 5.0,
                "n_trades": 50,
                "skew_alert": False,
                "kurtosis_alert": True,
            }
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "fat tails" in content
        assert "5.00" in content

    def test_sans_top_trade_fragile(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            sans_top_trade={
                "total_profit": 100.0,
                "without_top1": -5.0,
                "without_top1_pct": -5.0,
                "without_top2": -20.0,
                "without_top2_pct": -20.0,
                "fragile": True,
            }
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "FRAGILE" in content
        assert "luck, not edge" in content

    def test_sans_top_trade_robust(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Sans Top Trade" in content
        assert "FRAGILE" not in content

    def test_pair_distribution_svg(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Profit by Pair" in content
        assert "BTC/USDT" in content
        assert "SOL/USDT" in content

    def test_loss_histogram_svg(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Loss Distribution" in content
        assert "stroke-dasharray" in content

    def test_parallel_coords_svg(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Parallel Coordinates" in content
        assert "polyline" in content

    def test_correlation_heatmap(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Parameter Correlation" in content
        assert "0.85" in content

    def test_no_heatmap_few_params(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(param_correlation=[])
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Parameter Correlation" not in content

    def test_convergence_colored_dd(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            epoch_dd_data=[0.1, 0.2, 0.15, 0.25, 0.08, 0.35, 0.12],
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "#22c55e" in content
        assert "#eab308" in content
        assert "#ef4444" in content
        assert "DD&lt;15%" in content

    def test_dispersion_bands(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Top-10 Dispersion" in content
        assert "Profit %" in content
        assert "Sharpe" in content

    def test_benchmark_annotations(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "benchmark" in content.lower()
        assert "0.85" in content

    def test_best_vs_median_outlier(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Best vs. Median" in content
        assert "outlier" in content
        assert "2.50x" in content

    def test_graceful_empty_data(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            dsr_analysis=None,
            distribution_analysis=None,
            sans_top_trade=None,
            pair_profit_distribution=[],
            best_vs_median_gap=None,
            param_correlation=[],
            loss_histogram=None,
            parallel_coords={},
            benchmark_comparison={},
            dispersion_bands={},
            epoch_dd_data=[],
            all_losses=[],
            overfit_warnings=[],
            param_deep_dive=None,
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "<!DOCTYPE html>" in content
        assert "</html>" in content
        assert "Concentration Risk" not in content
        assert "Profit by Pair" not in content
        assert "Loss Distribution" not in content
        assert "Parallel Coordinates" not in content
        assert "Overfitting Warnings" not in content
        assert "Parameter Deep Dive" not in content

    # ------------------------------------------------------------------
    # New feature tests — overfit warnings, param deep dive, bilingual
    # ------------------------------------------------------------------

    def test_overfit_warnings_panel(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Overfitting Warnings" in content
        assert "Sharpe likely overfitted" in content
        assert "Sharpe" in content
        assert "#ef4444" in content
        assert "warn-box" in content

    def test_overfit_warnings_empty(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(overfit_warnings=[])
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Overfitting Warnings" not in content

    def test_param_deep_dive_section(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Parameter Deep Dive" in content
        assert "param-detail" in content
        assert "rsi" in content
        assert "volume_pct" in content
        assert "converging" in content
        assert "sensitive" in content
        assert "badge-sens-high" in content
        assert "Range:" in content
        assert "10 — 50" in content

    def test_param_deep_dive_empty(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(param_deep_dive=None)
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Parameter Deep Dive" not in content

    def test_param_deep_dive_mini_histogram(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert content.count("svg") >= 4
        assert "stroke-dasharray" in content

    def test_param_deep_dive_boundary_badge(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        pdd = {
            "rsi": {
                "type": "Int",
                "best_value": 10,
                "range_low": 10,
                "range_high": 50,
                "categories": None,
                "top10_min": 10,
                "top10_max": 14,
                "top10_median": 11,
                "top10_std": 1.2,
                "tendency": "converging",
                "sensitivity": 0.5,
                "sensitivity_label": "medium",
                "boundary_cluster": True,
                "histogram": [],
                "category_counts": {},
            },
        }
        data = self._make_data(param_deep_dive=pdd)
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "boundary!" in content
        assert "badge-boundary" in content

    def test_top10_expandable_params(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "<details>" in content
        assert "<code>rsi</code>" in content
        assert "<code><strong>28</strong></code>" in content
        assert "<code>volume_pct</code>" in content

    def test_bilingual_tooltips(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert 'lang="fr"' in content
        assert 'lang="en"' in content
        assert 'class="tip-text"' in content

    def test_bilingual_section_descriptions(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "section-desc" in content
        assert "triques cl" in content
        assert "Key performance metrics" in content

    def test_pair_distribution_enriched(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "25t)" in content
        assert "WR:72%" in content
        assert "avg:" in content

    def test_loss_histogram_percentile(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "93%" in content

    def test_glossary_explanation_fr(self):
        from freqtrade.optimize.wfa_glossary import METRIC_GLOSSARY

        for slug, entry in METRIC_GLOSSARY.items():
            assert "explanation_fr" in entry, f"METRIC_GLOSSARY['{slug}'] missing 'explanation_fr'"
            assert len(entry["explanation_fr"]) > 10, (
                f"METRIC_GLOSSARY['{slug}'].explanation_fr too short"
            )

    def test_warning_diagrams_svg(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "yours=" in content
        assert content.count("warn-box") >= 2

    def test_warning_actions_expandable(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "What to do" in content
        assert "Que faire" in content
        assert "warn-actions" in content

    def test_warning_tooltip_links(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert 'class="tooltip"' in content

    def test_sampler_bilingual(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Échantillonneur" in content
        assert "Quand l'utiliser" in content

    def test_sampler_glossary_bilingual(self):
        from freqtrade.optimize.wfa_glossary import SAMPLER_GLOSSARY

        for name, entry in SAMPLER_GLOSSARY.items():
            assert "one_liner_fr" in entry, f"SAMPLER_GLOSSARY['{name}'] missing 'one_liner_fr'"
            assert "explanation_fr" in entry, f"SAMPLER_GLOSSARY['{name}'] missing 'explanation_fr'"

    def test_glossary_section_bilingual(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Glossaire" in content
        assert "tip-text" in content

    def test_parallel_coords_loss_color(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "pc-bg" in content
        assert "low loss" in content
        assert "high loss" in content

    def test_heatmap_column_labels_not_in_cells(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "rotate(-90" in content

    def test_dof_glossary_entry(self):
        from freqtrade.optimize.wfa_glossary import METRIC_GLOSSARY

        assert "dof" in METRIC_GLOSSARY
        assert "explanation_fr" in METRIC_GLOSSARY["dof"]

    def test_concentration_gauge_svg(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Sans Top Trade" in content or "meilleur trade" in content
        assert "150" in content
        assert "120" in content

    def test_concentration_gauge_empty(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(sans_top_trade=None)
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Sans Top Trade" not in content

    def test_pair_count_summary(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(total_pairs=20)
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "<strong>20</strong>" in content
        assert "profitable" in content or "rentable" in content

    def test_top10_json_copy_button(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "navigator.clipboard.writeText" in content
        assert "Copy JSON" in content or "Copier JSON" in content

    def test_top10_param_median_comparison(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "med:" in content
        assert "mean:" in content

    def test_loss_histogram_color_zones(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "#22c55e" in content
        assert "#eab308" in content
        assert "#ef4444" in content
        assert "How to read" in content or "Comment lire" in content

    def test_correlation_legend_bar(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "corr-leg" in content
        assert "linearGradient" in content

    def test_parallel_coords_guide(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Good sign" in content or "Bon signe" in content
        assert "Bad sign" in content or "Mauvais signe" in content

    def test_top10_extra_metrics(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Win rate" in content or "Taux de gain" in content
        assert "Sortino" in content
        assert "Trades" in content

    def test_run_summary_section(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Run Summary" in content or "Résumé" in content
        assert "run-summary" in content
        assert "freqtrade hyperopt" in content
        assert "CalmarHyperOptLoss" in content
        assert "TestStrategy" in content

    def test_run_summary_best_json(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "run-json" in content
        assert "Copy JSON" in content or "Copier JSON" in content
        assert "rsi" in content
        assert "0.05" in content

    def test_run_summary_config_table(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "15m" in content
        assert "20230101-20250101" in content
        assert "100" in content
        assert "buy, sell" in content

    def test_dispersion_bands_guide(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Narrow band" in content or "Bande étroite" in content
        assert "Wide band" in content or "Bande large" in content
        assert "med:" in content

    def test_convergence_chart_guide(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Flat curve" in content or "Courbe plate" in content
        assert "Still dropping" in content or "Encore en baisse" in content

    def test_best_vs_median_svg(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Best epoch" in content or "Meilleur" in content
        assert "Median top-10" in content or "Médiane" in content
        assert "2.50x" in content

    def test_loss_histogram_y_axis(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "<title>Loss" in content
        assert "Count" in content

    def test_no_loss_explanation_section(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert content.count("Loss Function") <= 1 or (content.count("Fonction de perte") <= 1)

    def test_run_summary_duration(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "1h 2m" in content

    def test_run_summary_all_default_params(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Jobs" in content
        assert "Random state" in content or "Graine" in content
        assert "Analyze" in content or "Analyse" in content
        assert "Print all" in content or "Afficher" in content
        assert "Print JSON" in content or "JSON console" in content

    def test_run_summary_no_duration(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(elapsed_secs=None)
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "run-summary" in content

    def test_convergence_sampler_explanation(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(sampler="NSGAIIISampler")
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "NSGA" in content
        assert "multi-objective" in content or ("multi-objectif" in content)

    def test_convergence_tpe_explanation(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(sampler="TPESampler")
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "TPE" in content
        assert "probabilistic" in content or ("probabiliste" in content)

    def test_format_duration_seconds(self):
        from freqtrade.optimize.hyperopt_html_report import (
            HyperoptHTMLReport,
        )

        assert HyperoptHTMLReport._format_duration(45) == "45s"
        assert HyperoptHTMLReport._format_duration(125) == "2m 5s"
        assert HyperoptHTMLReport._format_duration(3723) == "1h 2m"

    def test_config_table_cli_arg_column(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "CLI Argument" in content or "Argument CLI" in content
        assert "--epochs" in content
        assert "--strategy" in content
        assert "--hyperopt-loss" in content
        assert "--timerange" in content
        assert "--min-trades" in content
        assert "--print-all" in content

    def test_config_table_source_column(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(user_params=["strategy", "hyperopt_loss", "epochs"])
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Source" in content
        assert ">user<" in content
        assert ">default<" in content

    def test_config_description_tooltips(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "DecimalParameter" in content
        assert "objective function" in content or ("fonction objectif" in content)
        assert "probabilistic model" in content or ("modèle probabiliste" in content)
        assert "Wall-clock" in content or ("Temps réel" in content)

    def test_group_structure(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "group-perf" in content
        assert "group-robust" in content
        assert "group-params" in content
        assert "group-conv" in content
        assert "group-ref" in content
        assert "group-title" in content

    def test_command_copy_button(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "navigator.clipboard.writeText" in content
        assert "freqtrade hyperopt" in content
        cmd_section = content[content.index("cmd-multi") :]
        assert "Copy" in cmd_section or "Copier" in cmd_section

    # -- Tests for the 6 new features --

    def test_monte_carlo_section(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Monte Carlo" in content
        assert "P5" in content
        assert "P95" in content
        assert "82.5%" in content
        assert "<svg" in content

    def test_monte_carlo_empty(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(monte_carlo=None)
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Monte Carlo Confidence" not in content
        assert "Confiance Monte Carlo" not in content

    def test_sensitivity_grid_section(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Sensitivity" in content or "sensibilit" in content
        assert "rsi" in content
        assert "volume_pct" in content

    def test_sensitivity_grid_empty(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(sensitivity_grid=[])
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Sensitivity Heatmap" not in content

    def test_regime_analysis_section(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Regime" in content or "gime" in content
        assert "First half" in content or "Premi" in content
        assert "Consistent" in content or "Coh" in content

    def test_regime_analysis_empty(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(regime_analysis=None)
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Regime Analysis" not in content

    def test_dof_traffic_light(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Degrees of Freedom" in content or ("de libert" in content)
        assert "50 trades" in content
        assert "5 params" in content
        assert "10.0x" in content

    def test_dof_empty(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(dof_analysis=None)
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert '<span lang="en">Degrees of Freedom</span>' not in content

    def test_return_vs_dd_scatter(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Return vs Drawdown" in content or ("Rendement vs" in content)
        assert "<circle" in content

    def test_return_vs_dd_too_few(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(return_vs_dd=[])
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Return vs Drawdown" not in content

    def test_monte_carlo_computation(self):
        from freqtrade.optimize.hyperopt.hyperopt import Hyperopt

        trades = [{"profit_ratio": 0.01 * (i % 7 - 2)} for i in range(30)]
        mc = Hyperopt._compute_monte_carlo(trades, 500)
        assert mc is not None
        assert mc["n_simulations"] == 500
        assert mc["n_trades"] == 30
        assert mc["p5"] <= mc["p50"] <= mc["p95"]
        assert 0 <= mc["prob_positive"] <= 100

    def test_dof_computation(self):
        from freqtrade.optimize.hyperopt.hyperopt import Hyperopt

        dof = Hyperopt._compute_dof_analysis(100, 5)
        assert dof["ratio"] == 20.0
        assert dof["level"] == "green"
        dof2 = Hyperopt._compute_dof_analysis(60, 5)
        assert dof2["level"] == "yellow"
        dof3 = Hyperopt._compute_dof_analysis(3, 5)
        assert dof3["level"] == "red"

    def test_return_vs_dd_computation(self):
        from freqtrade.optimize.hyperopt.hyperopt import Hyperopt

        epochs = [
            {
                "results_metrics": {
                    "profit_total": 0.15,
                    "max_drawdown_account": 0.10,
                    "total_trades": 50,
                },
                "loss": 0.5,
            },
        ]
        pts = Hyperopt._compute_return_vs_dd(epochs)
        assert len(pts) == 1
        assert pts[0]["profit_pct"] == 15.0
        assert pts[0]["dd_pct"] == 10.0

    def test_scorecard_section_present(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data()
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Strategy Scorecard" in content
        assert "Bilan de la strat" in content
        assert "passed" in content
        assert "Profit" in content
        assert "Calmar" in content
        assert "Sharpe" in content
        assert "Drawdown" in content
        assert "Profit Factor" in content

    def test_scorecard_red_verdict(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            best_epoch={
                "total_trades": 5,
                "wins": 2,
                "draws": 0,
                "losses": 3,
                "profit_total": -0.05,
                "profit_total_abs": -50.0,
                "profit_mean": -0.01,
                "max_drawdown_account": 0.55,
                "max_drawdown_abs": 550.0,
                "holding_avg": "5:00:00",
                "calmar": 0.1,
                "sqn": -0.5,
                "sharpe": -0.3,
                "sortino": -0.1,
                "profit_factor": 0.7,
                "winrate": 0.40,
                "expectancy": -1.0,
                "expectancy_ratio": -0.02,
            }
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "Do NOT deploy" in content or ("NE PAS" in content)
        assert "#ef4444" in content

    def test_scorecard_all_green(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            best_epoch={
                "total_trades": 200,
                "wins": 140,
                "draws": 10,
                "losses": 50,
                "profit_total": 0.25,
                "profit_total_abs": 250.0,
                "profit_mean": 0.005,
                "max_drawdown_account": 0.15,
                "max_drawdown_abs": 150.0,
                "holding_avg": "2:00:00",
                "calmar": 3.0,
                "sqn": 2.5,
                "sharpe": 1.5,
                "sortino": 2.0,
                "profit_factor": 2.0,
                "winrate": 0.70,
                "expectancy": 5.0,
                "expectancy_ratio": 0.1,
            },
            dsr_analysis={
                "observed_sharpe": 2.0,
                "expected_max_sharpe": 1.5,
                "n_trials": 100,
                "genuine": True,
            },
            sans_top_trade={
                "total_profit": 250.0,
                "without_top1": 220.0,
                "without_top1_pct": 88.0,
                "without_top2": 195.0,
                "without_top2_pct": 78.0,
                "fragile": False,
            },
            monte_carlo={
                "p5": 5.0,
                "p25": 15.0,
                "p50": 20.0,
                "p75": 25.0,
                "p95": 30.0,
                "mean": 20.0,
                "n_simulations": 1000,
                "n_trades": 200,
                "prob_positive": 95,
            },
            dof_analysis={
                "n_trades": 200,
                "n_params": 5,
                "ratio": 40.0,
                "level": "green",
                "label": "Excellent",
            },
            best_vs_median_gap={
                "best_profit": 15.0,
                "median_profit": 12.0,
                "gap_ratio": 1.25,
                "outlier": False,
            },
            distribution_analysis={
                "skewness": 0.2,
                "excess_kurtosis": 1.5,
                "n_trades": 200,
                "skew_alert": False,
                "kurtosis_alert": False,
            },
            overfit_warnings=[],
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "All criteria passed" in content or ("Tous les crit" in content)

    def test_scorecard_recommendations(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            best_epoch={
                "total_trades": 50,
                "wins": 30,
                "draws": 5,
                "losses": 15,
                "profit_total": 0.15,
                "profit_total_abs": 150.0,
                "profit_mean": 0.003,
                "max_drawdown_account": 0.40,
                "max_drawdown_abs": 400.0,
                "holding_avg": "2:30:00",
                "calmar": 0.4,
                "sqn": 1.8,
                "sharpe": 1.5,
                "sortino": 2.2,
                "profit_factor": 1.6,
                "winrate": 0.60,
                "expectancy": 3.0,
                "expectancy_ratio": 0.07,
            }
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "CalmarHyperOptLoss" in content
        assert "Recommend" in content or "Recommand" in content

    def test_scorecard_sharpe_fraud(self, tmp_path):
        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        data = self._make_data(
            best_epoch={
                "total_trades": 50,
                "wins": 30,
                "draws": 5,
                "losses": 15,
                "profit_total": 0.15,
                "profit_total_abs": 150.0,
                "profit_mean": 0.003,
                "max_drawdown_account": 0.12,
                "max_drawdown_abs": 120.0,
                "holding_avg": "2:30:00",
                "calmar": 2.5,
                "sqn": 1.8,
                "sharpe": 7.0,
                "sortino": 2.2,
                "profit_factor": 1.6,
                "winrate": 0.60,
                "expectancy": 3.0,
                "expectancy_ratio": 0.07,
            }
        )
        out = tmp_path / "report.html"
        generate_hyperopt_html_report(data, out)
        content = out.read_text()
        assert "fraud" in content.lower() or ("curve-fit" in content.lower())


class TestHyperoptConsoleSummary:
    def test_threshold_label(self):
        from freqtrade.optimize.hyperopt.hyperopt import Hyperopt

        label = Hyperopt._threshold_label("calmar", 3.0)
        assert "good" in label.lower()

    def test_threshold_label_unknown(self):
        from freqtrade.optimize.hyperopt.hyperopt import Hyperopt

        label = Hyperopt._threshold_label("nonexistent", 1.0)
        assert label == ""
