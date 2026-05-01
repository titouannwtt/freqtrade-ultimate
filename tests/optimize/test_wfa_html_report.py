from __future__ import annotations

from typing import Any

from freqtrade.optimize.wfa_html_report import WFAHTMLReport


def _make_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "strategy": "TestStrat",
        "wf_mode": "rolling",
        "n_windows": 3,
        "epochs_per_window": 150,
        "hyperopt_loss": "CalmarHyperOptLoss",
        "train_ratio": 0.75,
        "embargo_days": 7,
        "timestamp": "2026-04-30T12:00:00",
        "deflated_sharpe_ratio": 0.98,
        "oos_aggregate": {
            "total_trades": 250,
            "sqn": 2.1,
            "expectancy": 0.003,
        },
        "verdict": {
            "grade": "B",
            "checks": [
                ("profitable_windows", True, "3/3 (100%)"),
                ("wfe", True, "WFE median 65%"),
                ("dsr", True, "DSR 0.980"),
                ("oos_trades", True, "OOS trades: 250"),
                ("sqn", True, "SQN 2.1"),
                ("param_stability", True, "5/6 stable (83%)"),
                ("dd_ratio", True, "Max DD ratio 1.2x"),
                ("profit_factor", True, "Avg PF 1.45"),
                ("trades_params", True, "Min trades/params 50:1"),
            ],
        },
        "warnings": [
            "No --timeframe-detail: fills overestimated (tip #20)",
        ],
        "oos_trade_profits": [0.5, -0.2, 0.3, 0.1, -0.1, 0.4],
        "monte_carlo": {
            "n_simulations": 1000,
            "total_return_pct": 5.2,
            "max_dd_p5": 2.1,
            "max_dd_p50": 3.5,
            "max_dd_p95": 6.8,
            "return_dd_p5": 0.76,
            "return_dd_p50": 1.49,
            "return_dd_p95": 2.48,
            "max_consec_loss_p50": 3,
            "max_consec_loss_p95": 5,
            "carver_discount": 0.51,
        },
        "oos_equity": {
            "total_return_pct": 5.2,
            "max_dd_pct": 3.5,
            "k_ratio": 1.23,
            "n_trades": 250,
        },
        "regime_analysis": {
            "regime_stats": {
                "bull": {
                    "windows": 2,
                    "avg_profit": 3.5,
                    "avg_dd": 2.1,
                    "pct_profitable": 1.0,
                },
                "bear": {
                    "windows": 1,
                    "avg_profit": -1.2,
                    "avg_dd": 5.3,
                    "pct_profitable": 0.0,
                },
            },
            "worst_regime": "bear",
            "regime_dependent": True,
        },
        "perturbation": {
            "n_perturbations": 60,
            "profit_p5": -2.1,
            "profit_p50": 3.2,
            "profit_p95": 8.5,
            "pct_profitable": 0.78,
            "sensitivity": 0.85,
        },
        "multi_seed": {
            "n_seeds": 5,
            "convergence_pct": 0.72,
        },
        "cpcv": {
            "n_groups": 6,
            "n_test_groups": 2,
            "n_combinations": 15,
            "n_paths": 5,
            "avg_return": 3.2,
            "sharpe_of_paths": 1.1,
            "prob_of_loss": 0.20,
            "path_returns": [2.1, 3.5, -0.5, 4.2, 5.1],
        },
        "windows": [
            {
                "index": 1,
                "train_range": "20250301-20250901",
                "test_range": "20250908-20251108",
                "wfe": 0.65,
                "train_metrics": {
                    "profit_pct": 12.5,
                    "trades": 150,
                    "calmar": 3.2,
                    "sharpe": 1.8,
                    "max_dd_pct": 4.1,
                    "profit_factor": 1.6,
                },
                "test_metrics": {
                    "profit_pct": 5.2,
                    "trades": 80,
                    "calmar": 1.8,
                    "sharpe": 1.1,
                    "max_dd_pct": 3.2,
                    "profit_factor": 1.35,
                    "hhi": 0.08,
                    "top1_pct": 25,
                },
                "baseline_metrics": {},
                "market_context": {"regime": "bull"},
                "params": {},
                "degradation": {
                    "profit_pct": -0.584,
                    "calmar": -0.4375,
                    "sharpe": -0.389,
                    "profit_factor": -0.156,
                },
            },
            {
                "index": 2,
                "train_range": "20250501-20251101",
                "test_range": "20251108-20260108",
                "wfe": 0.55,
                "train_metrics": {
                    "profit_pct": 10.1,
                    "trades": 120,
                    "calmar": 2.5,
                    "sharpe": 1.5,
                    "max_dd_pct": 5.2,
                    "profit_factor": 1.4,
                },
                "test_metrics": {
                    "profit_pct": 3.1,
                    "trades": 90,
                    "calmar": 1.2,
                    "sharpe": 0.9,
                    "max_dd_pct": 4.5,
                    "profit_factor": 1.25,
                    "hhi": 0.12,
                    "top1_pct": 35,
                },
                "baseline_metrics": {},
                "market_context": {"regime": "bull"},
                "params": {},
                "degradation": {
                    "profit_pct": -0.693,
                    "calmar": -0.52,
                    "sharpe": -0.40,
                    "profit_factor": -0.107,
                },
            },
            {
                "index": 3,
                "train_range": "20250701-20260101",
                "test_range": "20260108-20260308",
                "wfe": 0.45,
                "train_metrics": {
                    "profit_pct": 8.0,
                    "trades": 100,
                    "calmar": 2.0,
                    "sharpe": 1.2,
                    "max_dd_pct": 6.0,
                    "profit_factor": 1.3,
                },
                "test_metrics": {
                    "profit_pct": -1.2,
                    "trades": 70,
                    "calmar": -0.5,
                    "sharpe": -0.3,
                    "max_dd_pct": 8.0,
                    "profit_factor": 0.85,
                    "hhi": 0.05,
                    "top1_pct": 15,
                },
                "baseline_metrics": {},
                "market_context": {"regime": "bear"},
                "params": {},
                "degradation": {
                    "profit_pct": -1.15,
                    "calmar": -1.25,
                    "sharpe": -1.25,
                    "profit_factor": -0.346,
                },
            },
        ],
        "holdout": {
            "test_range": "20260308-20260430",
            "test_metrics": {
                "profit_pct": 2.1,
                "trades": 40,
                "calmar": 1.5,
                "max_dd_pct": 2.8,
            },
            "baseline_metrics": {
                "profit_pct": 0.5,
                "trades": 35,
            },
        },
        "param_stability": {
            "buy_rsi_low": {
                "values": [28, 30, 29],
                "mean": 29.0,
                "std": 0.82,
                "median": 29.0,
                "std_over_range": 0.08,
                "stable": True,
                "unstable": False,
            },
            "buy_volume_factor": {
                "values": [1.2, 2.5, 0.8],
                "mean": 1.5,
                "std": 0.72,
                "median": 1.2,
                "std_over_range": 0.36,
                "stable": False,
                "unstable": True,
            },
        },
        "consensus_params": {
            "buy": {"rsi_low": 29, "volume_factor": 1.2},
            "sell": {"rsi_high": 72},
        },
    }
    data.update(overrides)
    return data


class TestWFAHTMLReport:
    def test_generate_basic(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "<!DOCTYPE html>" in html
        assert "Walk-Forward Analysis" in html
        assert "TestStrat" in html

    def test_bilingual_toggle(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert 'lang="en"' in html
        assert 'lang="fr"' in html
        assert "Français" in html
        assert "lang-bar" in html

    def test_bilingual_css(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert 'span[lang="fr"] { display: none; }' in html
        assert 'html:lang(fr) span[lang="en"] { display: none; }' in html

    def test_verdict_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-verdict" in html
        assert "&#10003;" in html
        assert "grade" in html.lower()

    def test_verdict_grade_displayed(self):
        html = WFAHTMLReport(
            _make_data(
                verdict={
                    "grade": "A",
                    "checks": [("wfe", True, "WFE 70%")],
                }
            )
        ).generate()
        assert "Deploy" in html

    def test_scorecard_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-scorecard" in html
        assert "Strategy Scorecard" in html

    def test_scorecard_criteria_from_checks(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "Profitable Windows" in html
        assert "WFE" in html
        assert "DSR" in html

    def test_scorecard_clickable_links(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert 'href="#sec-' in html

    def test_scorecard_gauge_bars(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "border-radius:5px" in html

    def test_scorecard_mc_criterion(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "MC Robustness" in html
        assert "Carver #111" in html

    def test_scorecard_perturbation_criterion(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "Perturbation" in html
        assert "tip #81" in html

    def test_scorecard_recommendations(self):
        data = _make_data(
            verdict={
                "grade": "D",
                "checks": [
                    ("profitable_windows", False, "1/3 (33%)"),
                    ("wfe", False, "WFE 20%"),
                ],
            }
        )
        html = WFAHTMLReport(data).generate()
        assert "Recommendations" in html

    def test_scorecard_no_mc_when_absent(self):
        html = WFAHTMLReport(_make_data(monte_carlo=None)).generate()
        assert "MC Robustness" not in html

    def test_scorecard_all_green(self):
        data = _make_data()
        data["regime_analysis"] = {
            "regime_stats": {
                "range": {
                    "windows": 3,
                    "avg_profit": 2.0,
                    "avg_dd": 3.0,
                    "pct_profitable": 1.0,
                }
            },
            "worst_regime": "range",
            "regime_dependent": False,
        }
        html = WFAHTMLReport(data).generate()
        assert "All criteria passed" in html or "Tous les" in html

    def test_window_table(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-windows" in html
        assert "2025-09-08" in html
        assert "W1" in html or ">1<" in html

    def test_window_table_train_column(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "Train Profit" in html
        assert "2025-03-01" in html

    def test_wfe_chart(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-wfe" in html
        assert "<svg" in html
        assert "50%" in html

    def test_wfe_chart_colors(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "#22c55e" in html
        assert "#eab308" in html or "#ef4444" in html

    def test_degradation_table(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-degradation" in html
        assert "Degradation" in html or "Dégradation" in html

    def test_holdout_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-holdout" in html
        assert "Holdout" in html
        assert "+2.1%" in html

    def test_holdout_baseline(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "Baseline" in html
        assert "+0.5%" in html

    def test_no_holdout_when_absent(self):
        html = WFAHTMLReport(_make_data(holdout=None)).generate()
        assert "sec-holdout" not in html

    def test_oos_aggregate(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-oos-agg" in html
        assert "SQN" in html
        assert "2.1" in html

    def test_oos_dsr_displayed(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "0.980" in html
        assert "DSR" in html

    def test_equity_chart(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-equity" in html
        assert "<polyline" in html
        assert "K-ratio" in html

    def test_equity_no_chart_few_trades(self):
        html = WFAHTMLReport(_make_data(oos_trade_profits=[0.1])).generate()
        assert "OOS Equity Curve" not in html

    def test_concentration_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-concentration" in html
        assert "HHI" in html

    def test_monte_carlo_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-monte-carlo" in html
        assert "Monte Carlo" in html
        assert "1000" in html
        assert "Carver discount" in html

    def test_monte_carlo_dd_viz(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "p5:" in html
        assert "p50:" in html

    def test_no_monte_carlo_when_absent(self):
        html = WFAHTMLReport(_make_data(monte_carlo=None)).generate()
        assert "sec-monte-carlo" not in html

    def test_regime_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-regime" in html
        assert "bull" in html
        assert "bear" in html
        assert "Regime dependent" in html or "Dépendant" in html

    def test_no_regime_when_absent(self):
        html = WFAHTMLReport(_make_data(regime_analysis=None)).generate()
        assert "sec-regime" not in html

    def test_perturbation_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-perturbation" in html
        assert "60" in html
        assert "tip #81" in html

    def test_multi_seed_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-multi-seed" in html
        assert "72%" in html

    def test_cpcv_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-cpcv" in html
        assert "CPCV" in html
        assert "Lopez de Prado" in html

    def test_cpcv_histogram(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "Return %" in html

    def test_no_cpcv_when_absent(self):
        html = WFAHTMLReport(_make_data(cpcv=None)).generate()
        assert "sec-cpcv" not in html

    def test_param_stability_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-param-stability" in html
        assert "buy_rsi_low" in html
        assert "stable" in html.lower()
        assert "unstable" in html.lower()

    def test_param_sparklines(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "<polyline" in html

    def test_consensus_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-consensus" in html
        assert "rsi_low" in html
        assert "29" in html

    def test_warnings_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-warnings" in html
        assert "tip #20" in html

    def test_no_warnings_when_empty(self):
        html = WFAHTMLReport(_make_data(warnings=[])).generate()
        assert "sec-warnings" not in html

    def test_next_steps_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-next-steps" in html
        assert "What To Do Next" in html

    def test_glossary_section(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "sec-glossary" in html
        assert "Glossary" in html or "Glossaire" in html

    def test_glossary_bilingual(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "Walk-Forward Efficiency" in html

    def test_tooltips_present(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert 'class="tooltip"' in html
        assert "tip-text" in html

    def test_groups_present(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "group-verdict" in html
        assert "group-perf" in html
        assert "group-oos" in html
        assert "group-robust" in html
        assert "group-params" in html
        assert "group-ref" in html

    def test_section_ids(self):
        html = WFAHTMLReport(_make_data()).generate()
        for sid in [
            "sec-verdict",
            "sec-scorecard",
            "sec-windows",
            "sec-wfe",
            "sec-degradation",
            "sec-holdout",
            "sec-oos-agg",
            "sec-equity",
            "sec-concentration",
            "sec-monte-carlo",
            "sec-regime",
            "sec-perturbation",
            "sec-multi-seed",
            "sec-cpcv",
            "sec-param-stability",
            "sec-consensus",
            "sec-warnings",
            "sec-next-steps",
            "sec-glossary",
        ]:
            assert sid in html, f"Missing section id: {sid}"

    def test_no_external_urls(self):
        html = WFAHTMLReport(_make_data()).generate()
        lines = html.split("\n")
        for line in lines:
            if "http://" in line or "https://" in line:
                assert "xmlns" in line, f"External URL found: {line.strip()[:100]}"

    def test_no_script_tags(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "<script" not in html.lower().replace("onchange", "")

    def test_graceful_empty_data(self):
        data = _make_data(
            windows=[],
            monte_carlo=None,
            regime_analysis=None,
            perturbation=None,
            multi_seed=None,
            cpcv=None,
            holdout=None,
            oos_trade_profits=[],
            oos_equity=None,
            param_stability={},
            consensus_params={},
            warnings=[],
        )
        html = WFAHTMLReport(data).generate()
        assert "<!DOCTYPE html>" in html
        assert "Walk-Forward Analysis" in html

    def test_generate_wfa_html_report_function(self, tmp_path):
        from freqtrade.optimize.wfa_html_report import (
            generate_wfa_html_report,
        )

        data = _make_data()
        out = tmp_path / "test_report.html"
        result = generate_wfa_html_report(data, out)
        assert result == out
        assert out.exists()
        content = out.read_text()
        assert "<!DOCTYPE html>" in content

    def test_threshold_badge(self):
        badge = WFAHTMLReport._threshold_badge("sqn", 2.5)
        assert "good" in badge
        assert "#84cc16" in badge

    def test_threshold_badge_empty(self):
        badge = WFAHTMLReport._threshold_badge("nonexistent", 1.0)
        assert badge == ""

    def test_tip_helper(self):
        tip = WFAHTMLReport._tip("sqn", "SQN")
        assert "tooltip" in tip
        assert "tip-text" in tip
        assert "SQN" in tip

    def test_tip_unknown_slug(self):
        tip = WFAHTMLReport._tip("nonexistent_xyz", "Something")
        assert tip == "Something"
        assert "tooltip" not in tip

    def test_fmt_helper(self):
        assert WFAHTMLReport._fmt(3.14159, 2) == "3.14"
        assert WFAHTMLReport._fmt(0.0, 0) == "0"

    def test_L_helper(self):
        result = WFAHTMLReport._L("Hello", "Bonjour")
        assert 'lang="en"' in result
        assert 'lang="fr"' in result
        assert "Hello" in result
        assert "Bonjour" in result

    def test_desc_helper(self):
        report = WFAHTMLReport(_make_data())
        result = report._desc("English desc", "French desc")
        assert "section-desc" in result
        assert "English desc" in result
        assert "French desc" in result

    def test_verdict_label_all_grades(self):
        for grade in ("A", "B", "C", "D", "F"):
            label_en, label_fr = WFAHTMLReport._verdict_label(grade)
            assert label_en != "Unknown"
            assert len(label_en) > 5
            assert label_fr != "Inconnu"
            assert len(label_fr) > 5

    def test_scorecard_max_dd_red(self):
        data = _make_data()
        data["windows"][0]["test_metrics"]["max_dd_pct"] = 55.0
        html = WFAHTMLReport(data).generate()
        assert "extreme" in html.lower()

    def test_scorecard_holdout_fail(self):
        data = _make_data()
        data["holdout"]["test_metrics"]["profit_pct"] = -5.0
        html = WFAHTMLReport(data).generate()
        assert "Holdout" in html
        assert "-5.0%" in html

    def test_scorecard_concentration_flag(self):
        data = _make_data()
        data["windows"][0]["test_metrics"]["top1_pct"] = 65.0
        html = WFAHTMLReport(data).generate()
        assert "Concentration" in html
        assert "65%" in html

    def test_intro_embargo_mention(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "7 days" in html or "7 jours" in html

    def test_intro_train_ratio(self):
        html = WFAHTMLReport(_make_data()).generate()
        assert "75%" in html

    def test_regime_not_dependent(self):
        data = _make_data()
        data["regime_analysis"] = {
            "regime_stats": {
                "range": {
                    "windows": 3,
                    "avg_profit": 2.0,
                    "avg_dd": 3.0,
                    "pct_profitable": 1.0,
                }
            },
            "worst_regime": "range",
            "regime_dependent": False,
        }
        html = WFAHTMLReport(data).generate()
        assert "sec-regime" in html
        assert "Regime dependent" not in html

    def test_cpcv_no_histogram_few_returns(self):
        data = _make_data()
        data["cpcv"]["path_returns"] = [1.0]
        html = WFAHTMLReport(data).generate()
        assert "sec-cpcv" in html
