# pragma pylint: disable=missing-docstring,W0212,C0103
from __future__ import annotations


class TestHyperoptGlossary:
    def test_new_metric_entries_exist(self):
        from freqtrade.optimize.wfa_glossary import METRIC_GLOSSARY

        new_slugs = {"sharpe", "sortino", "win_rate", "payoff_ratio", "cagr"}
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

        data = self._make_data(all_losses=[])
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


class TestHyperoptConsoleSummary:
    def test_threshold_label(self):
        from freqtrade.optimize.hyperopt.hyperopt import Hyperopt

        label = Hyperopt._threshold_label("calmar", 3.0)
        assert "good" in label.lower()

    def test_threshold_label_unknown(self):
        from freqtrade.optimize.hyperopt.hyperopt import Hyperopt

        label = Hyperopt._threshold_label("nonexistent", 1.0)
        assert label == ""
