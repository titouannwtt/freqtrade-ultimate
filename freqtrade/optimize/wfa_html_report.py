from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

import numpy as np

from freqtrade.optimize.wfa_glossary import (
    METRIC_GLOSSARY,
    PERCENTILE_HINT,
    VERDICT_GUIDE,
)


logger = logging.getLogger(__name__)

GRADE_COLORS = {
    "A": "#22c55e",
    "B": "#84cc16",
    "C": "#eab308",
    "D": "#f97316",
    "F": "#ef4444",
}


def generate_wfa_html_report(data: dict[str, Any], output_path: Path) -> Path:
    report = WFAHTMLReport(data)
    output_path.write_text(report.generate(), encoding="utf-8")
    return output_path


class WFAHTMLReport:
    def __init__(self, data: dict[str, Any]) -> None:
        self.d = data

    def generate(self) -> str:
        parts = [
            self._html_header(),
            self._section_intro(),
            self._section_verdict(),
            self._section_window_table(),
            self._section_equity_chart(),
            self._section_monte_carlo(),
            self._section_phase3(),
            self._section_cpcv(),
            self._section_warnings(),
            self._section_param_stability(),
            self._section_consensus(),
            self._section_next_steps(),
            self._section_glossary(),
            self._html_footer(),
        ]
        return "\n".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tip(slug: str, display: str) -> str:
        entry = METRIC_GLOSSARY.get(slug, {})
        tip = html.escape(entry.get("explanation", entry.get("one_liner", "")))
        if not tip:
            return html.escape(display)
        return (
            f'<span class="tooltip">{html.escape(display)}'
            f'<span class="tip-text">{tip}</span></span>'
        )

    @staticmethod
    def _threshold_badge(slug: str, value: float) -> str:
        entry = METRIC_GLOSSARY.get(slug, {})
        label = ""
        color = "#888"
        for thresh_val, thresh_label, thresh_color in entry.get("thresholds", []):
            if value >= thresh_val:
                label = thresh_label
                color = thresh_color
        if not label:
            return ""
        return f' <span style="color:{color};font-size:0.8em">({label})</span>'

    @staticmethod
    def _details(summary: str, body: str) -> str:
        return f"<details><summary>{html.escape(summary)}</summary><div>{body}</div></details>"

    # ------------------------------------------------------------------
    # Structure
    # ------------------------------------------------------------------

    def _html_header(self) -> str:
        strategy = html.escape(self.d.get("strategy", ""))
        mode = html.escape(self.d.get("wf_mode", ""))
        n_win = self.d.get("n_windows", 0)
        epochs = self.d.get("epochs_per_window", 0)
        loss = html.escape(self.d.get("hyperopt_loss", ""))
        ts = html.escape(self.d.get("timestamp", ""))
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>WFA Report — {strategy}</title>
<style>{self._css()}</style>
</head>
<body>
<div class="container">
<h1>Walk-Forward Analysis — {strategy}</h1>
<p class="meta">
  {loss} | {n_win} windows ({mode}) | {epochs} epochs/win | {ts}
</p>"""

    @staticmethod
    def _css() -> str:
        return """
body {
  font-family: -apple-system, 'Segoe UI', Roboto, monospace;
  background: #1a1a2e; color: #e0e0e0; margin: 0; padding: 20px;
}
.container { max-width: 1000px; margin: 0 auto; }
h1 { color: #00d4ff; border-bottom: 2px solid #00d4ff; padding-bottom: 8px; }
h2 { color: #00d4ff; margin-top: 30px; }
.meta { color: #888; font-size: 0.9em; }
table {
  width: 100%; border-collapse: collapse; margin: 10px 0;
  font-size: 0.85em;
}
th { background: #16213e; color: #00d4ff; padding: 8px; text-align: left; }
td { padding: 6px 8px; border-bottom: 1px solid #2a2a4a; }
tr:hover { background: #16213e; }
.grade {
  font-size: 3em; font-weight: bold; display: inline-block;
  width: 80px; height: 80px; line-height: 80px; text-align: center;
  border-radius: 12px; margin-right: 16px;
}
.check-pass { color: #22c55e; }
.check-fail { color: #ef4444; }
.verdict-box {
  display: flex; align-items: center; margin: 16px 0;
  background: #16213e; padding: 16px; border-radius: 8px;
}
.pos { color: #22c55e; }
.neg { color: #ef4444; }
.warn-item { color: #eab308; margin: 4px 0; }
.badge-stable { color: #22c55e; }
.badge-marginal { color: #eab308; }
.badge-unstable { color: #ef4444; }
svg { background: #0f0f23; border-radius: 8px; margin: 10px 0; }
.section { margin-bottom: 24px; }
.kv { display: inline-block; margin-right: 20px; }
.kv-label { color: #888; font-size: 0.85em; }
.kv-value { font-size: 1.1em; font-weight: bold; }
.intro {
  background: #16213e; padding: 16px; border-radius: 8px;
  margin: 16px 0; line-height: 1.6; font-size: 0.9em;
}
.next-steps {
  background: #1a2744; padding: 16px; border-radius: 8px;
  border-left: 4px solid #00d4ff; margin: 16px 0; line-height: 1.5;
}
.tooltip { position: relative; cursor: help; border-bottom: 1px dotted #00d4ff; }
.tooltip .tip-text {
  visibility: hidden; position: absolute; bottom: 125%; left: 50%;
  transform: translateX(-50%); background: #16213e; color: #e0e0e0;
  padding: 8px 12px; border-radius: 6px; font-size: 0.8em;
  width: 280px; z-index: 10; border: 1px solid #2a2a4a;
  white-space: normal; line-height: 1.4;
}
.tooltip:hover .tip-text { visibility: visible; }
details { margin: 8px 0; }
details summary { cursor: pointer; color: #00d4ff; font-size: 0.9em; }
details summary:hover { text-decoration: underline; }
details > div {
  padding: 8px 0 8px 16px; color: #bbb; font-size: 0.85em; line-height: 1.6;
}
"""

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    @staticmethod
    def _section_intro() -> str:
        return """
<div class="intro">
<strong>What is this?</strong> Walk-forward analysis tests whether your
strategy's optimized parameters work on data the optimizer never saw.
The data is split into N windows &mdash; for each, freqtrade optimizes on
the training period, then measures performance on the test period that follows.
<br><br>
<strong>How to read this report:</strong>
<strong>Verdict</strong> = overall grade (A-F) with pass/fail checklist.
<strong>Windows</strong> = per-period results &mdash; look for consistency,
not one lucky window.
<strong>Equity curve</strong> = concatenated out-of-sample performance.
<strong>Monte Carlo</strong> = stress test &mdash; what if trade order was different?
<strong>Robustness</strong> = does profit survive regime changes, parameter noise,
different seeds?
<strong>CPCV</strong> = advanced cross-validation across all possible data splits.
Hover over underlined terms for definitions.
</div>"""

    def _section_verdict(self) -> str:
        verdict = self.d.get("verdict", {})
        grade = verdict.get("grade", "?")
        checks = verdict.get("checks", [])
        color = GRADE_COLORS.get(grade, "#888")

        rows = ""
        for item in checks:
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                _, ok, desc = item[0], item[1], item[2]
            else:
                continue
            cls = "check-pass" if ok else "check-fail"
            mark = "&#10003;" if ok else "&#10007;"
            rows += f'<tr><td class="{cls}">{mark}</td><td>{html.escape(str(desc))}</td></tr>\n'

        return f"""
<div class="section">
<h2>Verdict</h2>
<div class="verdict-box">
  <div class="grade" style="background:{color}20;color:{color}">
    {html.escape(grade)}
  </div>
  <table style="flex:1">{rows}</table>
</div>
</div>"""

    def _section_window_table(self) -> str:
        windows = self.d.get("windows", [])
        if not windows:
            return ""

        rows = ""
        for w in windows:
            idx = w.get("index", 0)
            test_range = html.escape(str(w.get("test_range", "")))
            tm = w.get("test_metrics", {})
            profit = tm.get("profit_pct", 0)
            trades = tm.get("trades", 0)
            calmar = tm.get("calmar", 0)
            dd = tm.get("max_dd_pct", 0)
            wfe = w.get("wfe", 0)
            ctx = w.get("market_context", {})
            regime = html.escape(str(ctx.get("regime", "")))
            p_cls = "pos" if profit > 0 else "neg"
            rows += (
                f"<tr><td>{idx}</td><td>{test_range}</td>"
                f'<td class="{p_cls}">{profit:+.1f}%</td>'
                f"<td>{trades}</td><td>{calmar:.2f}</td>"
                f"<td>{dd:.1f}%</td><td>{wfe:.0%}</td>"
                f"<td>{regime}</td></tr>\n"
            )

        tip_calmar = self._tip("calmar", "Calmar")
        tip_dd = self._tip("dd", "Max DD")
        tip_wfe = self._tip("wfe", "WFE")

        return f"""
<div class="section">
<h2>Windows</h2>
<table>
<tr><th>#</th><th>Test Period</th><th>Profit</th><th>Trades</th>
<th>{tip_calmar}</th><th>{tip_dd}</th><th>{tip_wfe}</th><th>Regime</th></tr>
{rows}
</table>
</div>"""

    def _section_equity_chart(self) -> str:
        profits = self.d.get("oos_trade_profits", [])
        if not profits or len(profits) < 2:
            return ""
        return f"""
<div class="section">
<h2>Out-of-Sample Equity Curve</h2>
{self._svg_equity_curve(profits)}
</div>"""

    @staticmethod
    def _svg_equity_curve(profits: list[float]) -> str:
        w, h = 900, 300
        pad_l, pad_r, pad_t, pad_b = 70, 20, 20, 40

        starting = 1000.0
        equity = starting + np.cumsum(profits)
        n = len(equity)
        y_min = float(min(starting, np.min(equity))) * 0.99
        y_max = float(np.max(equity)) * 1.01
        if y_max - y_min < 1:
            y_max = y_min + 1

        def sx(i: int) -> float:
            return pad_l + (w - pad_l - pad_r) * i / max(n - 1, 1)

        def sy(v: float) -> float:
            return pad_t + (h - pad_t - pad_b) * (1 - (v - y_min) / (y_max - y_min))

        points = " ".join(f"{sx(i):.1f},{sy(float(equity[i])):.1f}" for i in range(n))
        start_y = sy(starting)
        color = "#22c55e" if equity[-1] >= starting else "#ef4444"

        grid = ""
        n_lines = 5
        for j in range(n_lines + 1):
            val = y_min + (y_max - y_min) * j / n_lines
            yy = sy(val)
            grid += (
                f'<line x1="{pad_l}" y1="{yy:.1f}" '
                f'x2="{w - pad_r}" y2="{yy:.1f}" '
                f'stroke="#2a2a4a" stroke-width="1"/>\n'
            )
            grid += (
                f'<text x="{pad_l - 5}" y="{yy:.1f}" '
                f'text-anchor="end" fill="#888" font-size="10">'
                f"{val:.0f}</text>\n"
            )

        y_label = (
            f'<text x="15" y="{(pad_t + h - pad_b) / 2:.0f}" '
            f'text-anchor="middle" fill="#888" font-size="11" '
            f'transform="rotate(-90,15,{(pad_t + h - pad_b) / 2:.0f})">'
            f"Equity</text>\n"
        )
        x_label = (
            f'<text x="{(pad_l + w - pad_r) / 2:.0f}" y="{h - 5}" '
            f'text-anchor="middle" fill="#888" font-size="11">'
            f"Trade #</text>\n"
        )
        legend_x = w - pad_r - 130
        legend = (
            f'<line x1="{legend_x}" y1="12" x2="{legend_x + 20}" y2="12" '
            f'stroke="#555" stroke-dasharray="4" stroke-width="1"/>\n'
            f'<text x="{legend_x + 25}" y="16" fill="#888" '
            f'font-size="10">Starting balance</text>\n'
        )

        return (
            f'<svg width="{w}" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">\n'
            f"{grid}{y_label}{x_label}{legend}"
            f'<line x1="{pad_l}" y1="{start_y:.1f}" '
            f'x2="{w - pad_r}" y2="{start_y:.1f}" '
            f'stroke="#555" stroke-dasharray="4" stroke-width="1"/>\n'
            f'<polyline points="{points}" fill="none" '
            f'stroke="{color}" stroke-width="2"/>\n'
            f"</svg>"
        )

    def _section_monte_carlo(self) -> str:
        mc = self.d.get("monte_carlo")
        if not mc:
            return ""
        tip_carver = self._tip("carver_discount", "Carver discount")
        explainer = self._details(
            "How to read this section",
            f"Trades are reshuffled {mc['n_simulations']:,} times to see how "
            f"drawdown varies with ordering alone. "
            f"{html.escape(PERCENTILE_HINT)} "
            f"If p5 drawdown is much worse than p50, your equity curve "
            f"depends on lucky trade ordering.",
        )
        return f"""
<div class="section">
<h2>{self._tip("mc", "Monte Carlo")} Shuffle ({mc["n_simulations"]} sims)</h2>
{explainer}
<div>
  <span class="kv"><span class="kv-label">Return</span><br>
    <span class="kv-value">{mc["total_return_pct"]:+.1f}%</span></span>
  <span class="kv"><span class="kv-label">\
{self._tip("dd", "Max DD")} p5/p50/p95</span><br>
    <span class="kv-value">{mc["max_dd_p5"]:.1f}% / {mc["max_dd_p50"]:.1f}% / \
{mc["max_dd_p95"]:.1f}%</span></span>
  <span class="kv"><span class="kv-label">Return/DD p5/p50/p95</span><br>
    <span class="kv-value">{mc["return_dd_p5"]:.2f} / {mc["return_dd_p50"]:.2f} / \
{mc["return_dd_p95"]:.2f}</span></span>
  <span class="kv"><span class="kv-label">{tip_carver}</span><br>
    <span class="kv-value">{mc["carver_discount"]:.2f}\
{self._threshold_badge("carver_discount", mc["carver_discount"])}</span></span>
  <span class="kv"><span class="kv-label">Consec loss p95</span><br>
    <span class="kv-value">{mc["max_consec_loss_p95"]}</span></span>
</div>
</div>"""

    def _section_phase3(self) -> str:
        parts: list[str] = []

        regime = self.d.get("regime_analysis")
        if regime:
            rows = ""
            for r, s in regime.get("regime_stats", {}).items():
                rows += (
                    f"<tr><td>{html.escape(r)}</td>"
                    f"<td>{s.get('windows', 0)}</td>"
                    f"<td>{s.get('avg_profit', 0):+.1f}%</td>"
                    f"<td>{s.get('avg_dd', 0):.1f}%</td></tr>\n"
                )
            dep = "Yes" if regime.get("regime_dependent") else "No"
            parts.append(
                f"<h3>Regime Analysis</h3>"
                f"<table><tr><th>Regime</th><th>Windows</th>"
                f"<th>Avg Profit</th><th>Avg DD</th></tr>{rows}</table>"
                f"<p>Regime dependent: {dep}</p>"
            )

        perturb = self.d.get("perturbation")
        if perturb:
            sens = perturb["sensitivity"]
            parts.append(
                f"<h3>Parameter Perturbation "
                f"({perturb['n_perturbations']} variants)</h3>"
                f"<div>"
                f"<span class='kv'><span class='kv-label'>"
                f"Profit p5/p50/p95</span><br>"
                f"<span class='kv-value'>{perturb['profit_p5']:+.1f}% / "
                f"{perturb['profit_p50']:+.1f}% / "
                f"{perturb['profit_p95']:+.1f}%</span></span>"
                f"<span class='kv'><span class='kv-label'>"
                f"% Profitable</span><br>"
                f"<span class='kv-value'>"
                f"{perturb['pct_profitable']:.0%}</span></span>"
                f"<span class='kv'><span class='kv-label'>"
                f"{self._tip('sensitivity', 'Sensitivity')}</span><br>"
                f"<span class='kv-value'>{sens:.2f}"
                f"{self._threshold_badge('sensitivity', sens)}"
                f"</span></span>"
                f"</div>"
            )

        ms = self.d.get("multi_seed")
        if ms:
            conv = ms["convergence_pct"]
            parts.append(
                f"<h3>Multi-Seed ({ms['n_seeds']} seeds)</h3>"
                f"<p>{self._tip('convergence', 'Convergence')}: "
                f"{conv:.0%}"
                f"{self._threshold_badge('convergence', conv)}</p>"
            )

        if not parts:
            return ""

        explainer = self._details(
            "How to read this section",
            "These tests stress the strategy: does it survive different "
            "market regimes? What if parameters are slightly wrong? "
            "Do different random seeds find the same solution?",
        )
        return '<div class="section"><h2>Robustness</h2>' + explainer + "\n".join(parts) + "</div>"

    def _section_cpcv(self) -> str:
        cpcv = self.d.get("cpcv")
        if not cpcv:
            return ""
        arr = np.array(cpcv.get("path_returns", []))
        p5 = f"{float(np.percentile(arr, 5)):+.1f}%" if len(arr) > 0 else "N/A"
        p50 = f"{float(np.percentile(arr, 50)):+.1f}%" if len(arr) > 0 else "N/A"
        p95 = f"{float(np.percentile(arr, 95)):+.1f}%" if len(arr) > 0 else "N/A"
        prob = cpcv["prob_of_loss"]
        explainer = self._details(
            "How to read this section",
            "Unlike rolling walk-forward which tests N sequential windows, "
            "CPCV tests every possible combination of train/test splits. "
            "This gives a distribution of returns and a probability of loss "
            "— a stronger test of whether the strategy is genuinely profitable.",
        )
        title = self._tip("cpcv", "CPCV")
        return f"""
<div class="section">
<h2>{title} &mdash; Combinatorial Purged Cross-Validation \
(N={cpcv["n_groups"]}, K={cpcv["n_test_groups"]})</h2>
{explainer}
<div>
  <span class="kv"><span class="kv-label">Combinations</span><br>
    <span class="kv-value">{cpcv["n_combinations"]}</span></span>
  <span class="kv"><span class="kv-label">Paths</span><br>
    <span class="kv-value">{cpcv["n_paths"]}</span></span>
  <span class="kv"><span class="kv-label">Avg Return</span><br>
    <span class="kv-value">{cpcv["avg_return"]:+.1f}%</span></span>
  <span class="kv"><span class="kv-label">\
{self._tip("sharpe_of_paths", "Sharpe of Paths")}</span><br>
    <span class="kv-value">{cpcv["sharpe_of_paths"]:.2f}\
{self._threshold_badge("sharpe_of_paths", cpcv["sharpe_of_paths"])}\
</span></span>
  <span class="kv"><span class="kv-label">\
{self._tip("prob_of_loss", "P(loss)")}</span><br>
    <span class="kv-value">{prob:.0%}\
{self._threshold_badge("prob_of_loss", prob)}</span></span>
  <span class="kv"><span class="kv-label">p5/p50/p95</span><br>
    <span class="kv-value">{p5} / {p50} / {p95}</span></span>
</div>
</div>"""

    def _section_warnings(self) -> str:
        warnings = self.d.get("warnings", [])
        if not warnings:
            return ""
        items = "\n".join(f'<p class="warn-item">! {html.escape(w)}</p>' for w in warnings)
        return f"""
<div class="section">
<h2>Warnings</h2>
{items}
</div>"""

    def _section_param_stability(self) -> str:
        stability = self.d.get("param_stability", {})
        if not stability:
            return ""
        rows = ""
        for param, info in sorted(stability.items()):
            vals = info.get("values", [])
            vals_str = ", ".join(f"{v}" for v in vals)
            median = info.get("median", 0)
            std = info.get("std", 0)
            sor = info.get("std_over_range", 0)
            if info.get("stable"):
                badge = '<span class="badge-stable">stable</span>'
            elif info.get("unstable"):
                badge = '<span class="badge-unstable">unstable</span>'
            else:
                badge = '<span class="badge-marginal">marginal</span>'
            rows += (
                f"<tr><td>{html.escape(param)}</td>"
                f"<td>{median:.4f}</td><td>{std:.4f}</td>"
                f"<td>{sor:.4f}</td><td>{badge}</td>"
                f"<td style='color:#888;font-size:0.8em'>{vals_str}</td></tr>\n"
            )
        explainer = self._details(
            "How to read this section",
            "Parameters that change wildly between windows are 'unstable' "
            "— the optimizer is fitting noise, not signal. Stable parameters "
            "(std/range &lt; 15%) suggest a real pattern. Consider freezing "
            "unstable parameters at sensible defaults.",
        )
        return f"""
<div class="section">
<h2>Parameter Stability</h2>
{explainer}
<table>
<tr><th>Param</th><th>Median</th><th>Std</th>
<th>Std/Range</th><th>Status</th><th>Values</th></tr>
{rows}
</table>
</div>"""

    def _section_consensus(self) -> str:
        consensus = self.d.get("consensus_params", {})
        if not consensus:
            return ""
        rows = ""
        for space, params in sorted(consensus.items()):
            if not isinstance(params, dict):
                continue
            for k, v in sorted(params.items()):
                rows += (
                    f"<tr><td>{html.escape(space)}</td><td>{html.escape(k)}</td><td>{v}</td></tr>\n"
                )
        return f"""
<div class="section">
<h2>Consensus Parameters</h2>
<table>
<tr><th>Space</th><th>Parameter</th><th>Value</th></tr>
{rows}
</table>
</div>"""

    def _section_next_steps(self) -> str:
        verdict = self.d.get("verdict", {})
        grade = verdict.get("grade", "?")
        guide = VERDICT_GUIDE.get(grade, "")
        if not guide:
            return ""
        strategy = html.escape(self.d.get("strategy", "Strategy"))
        return f"""
<div class="section">
<h2>What To Do Next</h2>
<div class="next-steps">
  <p><strong>Grade {html.escape(grade)}:</strong> {html.escape(guide)}</p>
</div>
{
            self._details(
                "Output files from this run",
                "<p>All files are in <code>user_data/walk_forward/</code>:</p>"
                "<ul>"
                "<li><strong>Consensus params</strong> (JSON): copy to your strategy "
                "file to use these optimized values</li>"
                "<li><strong>Full results</strong> (JSON): machine-readable data</li>"
                "<li><strong>This report</strong> (HTML): shareable summary</li>"
                "</ul>"
                "<p>To apply consensus params for dry-run:</p>"
                f"<code>cp user_data/walk_forward/{strategy}_consensus_*.json "
                f"user_data/strategies/{strategy}.json</code>",
            )
        }
</div>"""

    @staticmethod
    def _section_glossary() -> str:
        rows = ""
        for slug, entry in sorted(METRIC_GLOSSARY.items()):
            abbrev = html.escape(entry.get("abbrev", slug))
            name = html.escape(entry.get("name", ""))
            one_liner = html.escape(entry.get("one_liner", ""))
            rows += f"<tr><td><strong>{abbrev}</strong></td>"
            rows += f"<td>{name}</td><td>{one_liner}</td></tr>\n"

        return (
            '<div class="section">'
            "<details><summary><h2 style='display:inline'>"
            "Glossary</h2></summary><div>"
            "<table><tr><th>Abbrev</th><th>Full Name</th>"
            f"<th>Description</th></tr>{rows}</table>"
            "</div></details></div>"
        )

    @staticmethod
    def _html_footer() -> str:
        return """
</div>
</body>
</html>"""
