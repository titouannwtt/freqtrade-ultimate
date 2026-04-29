from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

from freqtrade.optimize.wfa_glossary import LOSS_GLOSSARY, METRIC_GLOSSARY, SAMPLER_GLOSSARY


logger = logging.getLogger(__name__)


def generate_hyperopt_html_report(data: dict[str, Any], output_path: Path) -> Path:
    report = HyperoptHTMLReport(data)
    output_path.write_text(report.generate(), encoding="utf-8")
    return output_path


class HyperoptHTMLReport:
    def __init__(self, data: dict[str, Any]) -> None:
        self.d = data

    def generate(self) -> str:
        parts = [
            self._html_header(),
            self._section_intro(),
            self._section_best_epoch(),
            self._section_sans_top_trade(),
            self._section_pair_distribution(),
            self._section_best_params(),
            self._section_top10_table(),
            self._section_best_vs_median(),
            self._section_dispersion_bands(),
            self._section_convergence_chart(),
            self._section_loss_histogram(),
            self._section_param_agreement(),
            self._section_param_correlation(),
            self._section_parallel_coords(),
            self._section_loss_explanation(),
            self._section_sampler_explanation(),
            self._section_next_steps(),
            self._section_glossary(),
            self._html_footer(),
        ]
        return "\n".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _esc(text: str) -> str:
        return html.escape(str(text))

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

    @staticmethod
    def _fmt(value: float, decimals: int = 2) -> str:
        try:
            return f"{value:.{decimals}f}"
        except (TypeError, ValueError):
            return "N/A"

    def _dsr_badge(self) -> str:
        dsr = self.d.get("dsr_analysis")
        if not dsr:
            return ""
        if dsr["genuine"]:
            c, t = "#22c55e", "DSR: likely genuine"
        else:
            c = "#ef4444"
            t = (
                f"DSR: likely overfitted — E[max SR] "
                f"from {dsr['n_trials']} trials "
                f"= {dsr['expected_max_sharpe']:.2f}"
            )
        return f' <span class="badge-inline" style="color:{c}">({t})</span>'

    def _skew_kurtosis_badges(self) -> str:
        da = self.d.get("distribution_analysis")
        if not da or da.get("n_trades", 0) < 10:
            return ""
        skew = da["skewness"]
        kurt = da["excess_kurtosis"]
        parts = []
        skew_c = "#22c55e" if skew >= 0 else ("#ef4444" if skew < -1 else "#eab308")
        skew_lbl = "tail risk" if skew < -1 else ""
        parts.append(
            f'<span class="kv"><span class="kv-label">'
            f"{self._tip('skewness', 'Skewness')}</span><br>"
            f'<span class="kv-value" style="color:{skew_c}">'
            f"{skew:.2f}" + (f" — {skew_lbl}" if skew_lbl else "") + "</span></span>"
        )
        kurt_c = "#ef4444" if kurt > 6 else ("#eab308" if kurt > 3 else "#22c55e")
        kurt_lbl = "fat tails" if kurt > 3 else ""
        parts.append(
            f'<span class="kv"><span class="kv-label">'
            f"{self._tip('kurtosis', 'Kurtosis')}</span><br>"
            f'<span class="kv-value" style="color:{kurt_c}">'
            f"{kurt:.2f}" + (f" — {kurt_lbl}" if kurt_lbl else "") + "</span></span>"
        )
        return "".join(parts)

    def _benchmark_tag(self, metric: str) -> str:
        bm = (self.d.get("benchmark_comparison") or {}).get(metric)
        if not bm:
            return ""
        if metric == "dd":
            if bm["above"]:
                return (
                    ' <span class="badge-inline" style="color:#eab308">'
                    f"(&gt; {bm['benchmark']:.0f}% benchmark)</span>"
                )
            return ""
        if bm["above"]:
            return (
                ' <span class="badge-inline" style="color:#22c55e">'
                f"(&gt; {bm['benchmark']} benchmark)</span>"
            )
        return ""

    # ------------------------------------------------------------------
    # Structure
    # ------------------------------------------------------------------

    def _html_header(self) -> str:
        strategy = self._esc(self.d.get("strategy", ""))
        loss = self._esc(self.d.get("hyperopt_loss", ""))
        sampler = self._esc(self.d.get("sampler") or "TPESampler")
        ts = self._esc(self.d.get("timestamp", ""))
        total_epochs = self.d.get("total_epochs", 0)
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Hyperopt Report — {strategy}</title>
<style>{self._css()}</style>
</head>
<body>
<div class="container">
<h1>Hyperopt Report — {strategy}</h1>
<p class="meta">
  {loss} | {sampler} | {total_epochs} epochs | {ts}
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
h3 { color: #84cc16; margin-top: 20px; }
.meta { color: #888; font-size: 0.9em; }
table {
  width: 100%; border-collapse: collapse; margin: 10px 0;
  font-size: 0.85em;
}
th { background: #16213e; color: #00d4ff; padding: 8px; text-align: left; }
td { padding: 6px 8px; border-bottom: 1px solid #2a2a4a; }
tr:hover { background: #16213e; }
.pos { color: #22c55e; }
.neg { color: #ef4444; }
.badge-stable { color: #22c55e; font-size: 0.82em; }
.badge-marginal { color: #eab308; font-size: 0.82em; }
.badge-unstable { color: #ef4444; font-size: 0.82em; }
svg { background: #0f0f23; border-radius: 8px; margin: 10px 0; }
.section { margin-bottom: 24px; }
.kv { display: inline-block; margin-right: 20px; margin-bottom: 10px; }
.kv-label { color: #888; font-size: 0.85em; }
.kv-value { font-size: 1.1em; font-weight: bold; font-family: monospace; }
.intro {
  background: #16213e; padding: 16px; border-radius: 8px;
  margin: 16px 0; line-height: 1.6; font-size: 0.9em;
}
.next-steps {
  background: #1a2744; padding: 16px; border-radius: 8px;
  border-left: 4px solid #00d4ff; margin: 16px 0; line-height: 1.5;
}
.explain-box {
  background: #16213e; padding: 12px 16px; border-radius: 6px;
  margin: 10px 0; font-size: 0.88em; line-height: 1.6; color: #bbb;
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
.best-rank { color: #00d4ff; font-weight: bold; }
.mini-section {
  background: #16213e; padding: 12px 16px; border-radius: 6px;
  margin: 10px 0; font-size: 0.88em; line-height: 1.6;
}
.badge-inline { font-size: 0.78em; margin-left: 6px; }
.band-track {
  fill: #2a2a4a; rx: 3; ry: 3;
}
"""

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    @staticmethod
    def _section_intro() -> str:
        what_is = (
            "Hyperopt runs your strategy hundreds or thousands of times with different "
            "parameter combinations, looking for the set that optimizes a loss function "
            "(e.g. Calmar ratio, Sharpe). The result is the single best epoch — the "
            "parameter set that minimized the loss on your training data."
            "<br><br>"
            "<strong>Important caveats:</strong> Hyperopt results are in-sample — the "
            "optimizer saw the same data it is evaluated on. A good hyperopt result does "
            "not guarantee live profitability. Always validate with walk-forward analysis "
            "or a live dry-run before committing real capital."
            "<br><br>"
            "<strong>Reading this report:</strong> "
            "<strong>Best Epoch</strong> = the optimized parameter metrics. "
            "<strong>Top 10</strong> = the best 10 epochs by loss — check for consistency. "
            "<strong>Convergence chart</strong> = how the optimizer improved over time. "
            "<strong>Parameter Agreement</strong> = stable params are more trustworthy. "
            "Hover over underlined terms for definitions."
        )
        return (
            '<div class="intro">'
            + HyperoptHTMLReport._details("What is hyperopt and how to read this report?", what_is)
            + "</div>"
        )

    def _section_best_epoch(self) -> str:
        m = self.d.get("best_epoch", {})
        if not m:
            return ""
        sc = self._esc(self.d.get("stake_currency", "USDC"))
        total_trades = m.get("total_trades", 0)
        wins = m.get("wins", 0)
        draws = m.get("draws", 0)
        losses = m.get("losses", 0)
        winrate = m.get("winrate", 0.0)
        profit_pct = m.get("profit_total", 0.0) * 100
        profit_abs = m.get("profit_total_abs", 0.0)
        profit_mean_pct = m.get("profit_mean", 0.0) * 100
        calmar = m.get("calmar", 0.0)
        sqn = m.get("sqn", 0.0)
        sharpe = m.get("sharpe", 0.0)
        sortino = m.get("sortino", 0.0)
        pf = m.get("profit_factor", 0.0)
        dd_pct = m.get("max_drawdown_account", 0.0) * 100
        dd_abs = m.get("max_drawdown_abs", 0.0)
        expectancy = m.get("expectancy", 0.0)
        expectancy_ratio = m.get("expectancy_ratio", 0.0)
        holding = self._esc(str(m.get("holding_avg", "N/A")))

        p_cls = "pos" if profit_pct >= 0 else "neg"
        sign = "+" if profit_pct >= 0 else ""

        def kv(label: str, value: str) -> str:
            return (
                f'<span class="kv"><span class="kv-label">{label}</span><br>'
                f'<span class="kv-value">{value}</span></span>'
            )

        rows = [
            kv("Trades", str(total_trades)),
            kv("W / D / L", f"{wins} / {draws} / {losses}"),
            kv(
                self._tip("expectancy", "Win rate"),
                f'<span class="{p_cls}">{winrate:.1%}</span>',
            ),
            kv(
                "Total Profit",
                f'<span class="{p_cls}">{sign}{self._fmt(profit_pct)}%'
                f" ({self._fmt(profit_abs)} {sc})</span>",
            ),
            kv(
                "Avg Profit / Trade",
                f'<span class="{p_cls}">{sign}{self._fmt(profit_mean_pct, 3)}%</span>',
            ),
            kv(
                self._tip("calmar", "Calmar") + self._threshold_badge("calmar", calmar),
                self._fmt(calmar),
            ),
            kv(
                self._tip("sqn", "SQN") + self._threshold_badge("sqn", sqn),
                self._fmt(sqn),
            ),
            kv(
                "Sharpe" + self._dsr_badge(),
                self._fmt(sharpe) + self._benchmark_tag("sharpe"),
            ),
            kv("Sortino", self._fmt(sortino)),
            kv(
                self._tip("pf", "Profit Factor") + self._threshold_badge("pf", pf),
                self._fmt(pf),
            ),
            kv(
                self._tip("dd", "Max Drawdown"),
                f"{self._fmt(dd_pct)}% ({self._fmt(dd_abs)} {sc})" + self._benchmark_tag("dd"),
            ),
            kv(
                self._tip("expectancy", "Expectancy"),
                f"{self._fmt(expectancy)} {sc}",
            ),
            kv("Expectancy Ratio", self._fmt(expectancy_ratio)),
            kv("Avg Holding", holding),
        ]
        skew_html = self._skew_kurtosis_badges()
        if skew_html:
            rows.append(skew_html)

        return '<div class="section"><h2>Best Epoch — Summary</h2>' + "".join(rows) + "</div>"

    def _section_best_params(self) -> str:
        best_params = self.d.get("best_params", {})
        if not best_params:
            return ""

        rows = ""
        for space, params in sorted(best_params.items()):
            if not isinstance(params, dict):
                rows += (
                    f"<tr><td>{self._esc(space)}</td>"
                    f"<td style='color:#888'>—</td>"
                    f"<td><code>{self._esc(str(params))}</code></td></tr>\n"
                )
                continue
            for k, v in sorted(params.items()):
                rows += (
                    f"<tr><td>{self._esc(space)}</td>"
                    f"<td>{self._esc(k)}</td>"
                    f"<td><code>{self._esc(str(v))}</code></td></tr>\n"
                )

        return (
            '<div class="section"><h2>Best Parameters</h2>'
            "<table>"
            "<tr><th>Space</th><th>Parameter</th><th>Value</th></tr>"
            f"{rows}"
            "</table></div>"
        )

    def _section_top10_table(self) -> str:
        top_epochs = self.d.get("top_epochs", [])
        if not top_epochs:
            return ""

        rows = ""
        for rank, epoch in enumerate(top_epochs[:10], start=1):
            loss = epoch.get("loss", 0.0)
            m = epoch.get("results_metrics", {})
            trades = m.get("total_trades", 0)
            profit_pct = m.get("profit_total", 0.0) * 100
            dd_pct = m.get("max_drawdown_account", 0.0) * 100
            calmar = m.get("calmar", 0.0)
            sharpe = m.get("sharpe", 0.0)
            pf = m.get("profit_factor", 0.0)
            p_cls = "pos" if profit_pct >= 0 else "neg"
            rank_style = ' class="best-rank"' if rank == 1 else ""
            rows += (
                f"<tr><td{rank_style}>{rank}</td>"
                f"<td><code>{self._fmt(loss, 4)}</code></td>"
                f"<td>{trades}</td>"
                f'<td class="{p_cls}">{profit_pct:+.1f}%</td>'
                f"<td>{self._fmt(dd_pct, 1)}%</td>"
                f"<td>{self._fmt(calmar)}</td>"
                f"<td>{self._fmt(sharpe)}</td>"
                f"<td>{self._fmt(pf)}</td></tr>\n"
            )

        return (
            '<div class="section"><h2>Top 10 Epochs</h2>'
            "<table>"
            f"<tr><th>Rank</th><th>Loss</th><th>Trades</th><th>Profit%</th>"
            f"<th>{self._tip('dd', 'Max DD%')}</th>"
            f"<th>{self._tip('calmar', 'Calmar')}</th>"
            f"<th>Sharpe</th>"
            f"<th>{self._tip('pf', 'PF')}</th></tr>"
            f"{rows}"
            "</table></div>"
        )

    def _section_convergence_chart(self) -> str:
        all_losses = self.d.get("all_losses") or []
        if len(all_losses) < 2:
            return ""
        dd_data = self.d.get("epoch_dd_data") or []
        return (
            '<div class="section"><h2>Convergence Chart</h2>'
            + self._svg_convergence(all_losses, dd_data)
            + "</div>"
        )

    @staticmethod
    def _dd_to_color(dd: float) -> str:
        if dd < 0.15:
            return "#22c55e"
        if dd < 0.30:
            return "#eab308"
        return "#ef4444"

    @staticmethod
    def _svg_convergence(
        all_losses: list[float],
        dd_data: list[float] | None = None,
    ) -> str:
        w, h = 900, 300
        pad_l, pad_r, pad_t, pad_b = 70, 20, 20, 40
        n = len(all_losses)
        has_dd = dd_data and len(dd_data) == n

        finite = [v for v in all_losses if v == v and abs(v) < 1e15]
        if not finite:
            return ""
        y_min = min(finite)
        y_max = max(finite)
        if y_max - y_min < 1e-9:
            y_max = y_min + 1e-9
        margin = (y_max - y_min) * 0.05
        y_min -= margin
        y_max += margin

        def sx(i: int) -> float:
            return pad_l + (w - pad_l - pad_r) * i / max(n - 1, 1)

        def sy(v: float) -> float:
            return pad_t + (h - pad_t - pad_b) * (1 - (v - y_min) / (y_max - y_min))

        dots = ""
        for i, v in enumerate(all_losses):
            if v != v or abs(v) >= 1e15:
                continue
            cx = sx(i)
            cy = sy(v)
            if has_dd:
                color = HyperoptHTMLReport._dd_to_color(dd_data[i])
            else:
                color = "#3a4a6a"
            dots += f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="1.5" fill="{color}" opacity="0.7"/>\n'

        # Best-so-far line
        best_so_far: list[float] = []
        current_best = float("inf")
        for v in all_losses:
            if v == v and abs(v) < 1e15 and v < current_best:
                current_best = v
            best_so_far.append(current_best if current_best < float("inf") else float("nan"))

        bsf_points = " ".join(
            f"{sx(i):.1f},{sy(v):.1f}"
            for i, v in enumerate(best_so_far)
            if v == v and abs(v) < 1e15
        )

        # Best epoch marker
        best_idx = all_losses.index(min(finite))
        best_val = all_losses[best_idx]
        bx = sx(best_idx)
        by = sy(best_val)
        best_dot = (
            f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="5" '
            f'fill="#00d4ff" stroke="#fff" stroke-width="1.5"/>\n'
        )

        # Grid lines
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
                f'<text x="{pad_l - 5}" y="{yy + 4:.1f}" '
                f'text-anchor="end" fill="#888" font-size="10">'
                f"{val:.4f}</text>\n"
            )

        y_label = (
            f'<text x="14" y="{(pad_t + h - pad_b) / 2:.0f}" '
            f'text-anchor="middle" fill="#888" font-size="11" '
            f'transform="rotate(-90,14,{(pad_t + h - pad_b) / 2:.0f})">Loss</text>\n'
        )
        x_label = (
            f'<text x="{(pad_l + w - pad_r) / 2:.0f}" y="{h - 5}" '
            f'text-anchor="middle" fill="#888" font-size="11">Epoch</text>\n'
        )

        legend_x = w - pad_r - 220
        legend = (
            f'<line x1="{legend_x}" y1="14" x2="{legend_x + 20}" y2="14" '
            f'stroke="#22c55e" stroke-width="2"/>\n'
            f'<text x="{legend_x + 25}" y="18" fill="#888" font-size="10">'
            f"Best so far</text>\n"
            f'<circle cx="{legend_x + 130}" cy="14" r="5" fill="#00d4ff" '
            f'stroke="#fff" stroke-width="1.5"/>\n'
            f'<text x="{legend_x + 140}" y="18" fill="#888" font-size="10">'
            f"Best epoch</text>\n"
        )
        if has_dd:
            dd_lx = pad_l
            legend += (
                f'<circle cx="{dd_lx}" cy="{h - 5}" r="4" fill="#22c55e"/>'
                f'<text x="{dd_lx + 8}" y="{h - 2}" fill="#888" '
                f'font-size="8">DD&lt;15%</text>'
                f'<circle cx="{dd_lx + 60}" cy="{h - 5}" r="4" fill="#eab308"/>'
                f'<text x="{dd_lx + 68}" y="{h - 2}" fill="#888" '
                f'font-size="8">DD 15-30%</text>'
                f'<circle cx="{dd_lx + 140}" cy="{h - 5}" r="4" fill="#ef4444"/>'
                f'<text x="{dd_lx + 148}" y="{h - 2}" fill="#888" '
                f'font-size="8">DD&gt;30%</text>\n'
            )

        return (
            f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">\n'
            f"{grid}{y_label}{x_label}{legend}"
            f"{dots}"
            f'<polyline points="{bsf_points}" fill="none" '
            f'stroke="#22c55e" stroke-width="2"/>\n'
            f"{best_dot}"
            f"</svg>"
        )

    def _section_param_agreement(self) -> str:
        top_epochs = self.d.get("top_epochs", [])
        if len(top_epochs) < 2:
            return ""

        # Collect all param values across top epochs
        all_params: dict[str, list[Any]] = {}
        for epoch in top_epochs[:10]:
            params_dict = epoch.get("params_dict", {})
            for space, params in params_dict.items():
                if not isinstance(params, dict):
                    continue
                for k, v in params.items():
                    key = f"{space}.{k}"
                    all_params.setdefault(key, []).append(v)

        if not all_params:
            return ""

        rows = ""
        for param, values in sorted(all_params.items()):
            numeric = [v for v in values if isinstance(v, (int, float))]
            vals_str = ", ".join(self._esc(str(v)) for v in values)

            if len(numeric) >= 2:
                v_min = min(numeric)
                v_max = max(numeric)
                v_range = v_max - v_min
                mean = sum(numeric) / len(numeric)
                variance = sum((x - mean) ** 2 for x in numeric) / len(numeric)
                std = variance**0.5
                sor = (std / v_range) if v_range > 0 else 0.0
                if sor < 0.15:
                    badge = '<span class="badge-stable">stable</span>'
                elif sor > 0.30:
                    badge = '<span class="badge-unstable">unstable</span>'
                else:
                    badge = '<span class="badge-marginal">marginal</span>'
                rows += (
                    f"<tr><td>{self._esc(param)}</td>"
                    f"<td>{self._fmt(mean, 4)}</td>"
                    f"<td>{self._fmt(std, 4)}</td>"
                    f"<td>{self._fmt(sor, 3)}</td>"
                    f"<td>{badge}</td>"
                    f"<td style='color:#888;font-size:0.8em'>{vals_str}</td></tr>\n"
                )
            else:
                # Categorical or single-value
                unique = list(dict.fromkeys(str(v) for v in values))
                if len(unique) == 1:
                    badge = '<span class="badge-stable">stable</span>'
                else:
                    badge = '<span class="badge-marginal">varies</span>'
                rows += (
                    f"<tr><td>{self._esc(param)}</td>"
                    f"<td colspan='3' style='color:#888'>categorical</td>"
                    f"<td>{badge}</td>"
                    f"<td style='color:#888;font-size:0.8em'>{vals_str}</td></tr>\n"
                )

        explainer = self._details(
            "How to read this section",
            "Parameters that stay consistent across the top-10 epochs are 'stable' "
            "(std/range &lt; 15%) — the optimizer found a real pattern. "
            "'Unstable' params (std/range &gt; 30%) vary wildly, suggesting the "
            "optimizer is fitting noise. Consider freezing unstable params at "
            "sensible defaults.",
        )
        return (
            '<div class="section"><h2>Top-10 Parameter Agreement</h2>'
            f"{explainer}"
            "<table>"
            "<tr><th>Parameter</th><th>Mean</th><th>Std</th>"
            "<th>Std/Range</th><th>Status</th><th>Values</th></tr>"
            f"{rows}"
            "</table></div>"
        )

    # ------------------------------------------------------------------
    # New metric sections (A3, A4, B1-B3, C1, C3, C4)
    # ------------------------------------------------------------------

    def _section_sans_top_trade(self) -> str:
        st = self.d.get("sans_top_trade")
        if not st:
            return ""
        sc = self._esc(self.d.get("stake_currency", "USDC"))
        fragile_badge = (
            ' <span style="color:#ef4444;font-weight:bold">FRAGILE — luck, not edge</span>'
            if st["fragile"]
            else ""
        )
        return (
            '<div class="section">'
            f"<h2>{self._tip('profit_concentration', 'Concentration Risk')}"
            " — Sans Top Trade Test</h2>"
            '<div class="mini-section">'
            f"Total profit: <strong>{self._fmt(st['total_profit'])}"
            f" {sc}</strong><br>"
            f"Without best trade: {self._fmt(st['without_top1'])}"
            f" {sc} ({st['without_top1_pct']:.1f}%)<br>"
            f"Without top 2 trades: {self._fmt(st['without_top2'])}"
            f" {sc} ({st['without_top2_pct']:.1f}%)"
            f"{fragile_badge}"
            "</div></div>"
        )

    def _section_pair_distribution(self) -> str:
        pairs = self.d.get("pair_profit_distribution", [])
        if not pairs:
            return ""
        return (
            '<div class="section"><h2>Profit by Pair</h2>' + self._svg_pair_bars(pairs) + "</div>"
        )

    @staticmethod
    def _svg_pair_bars(pairs: list[dict]) -> str:
        n = len(pairs)
        row_h = 28
        pad_l, pad_r, pad_t, pad_b = 120, 60, 10, 10
        h = pad_t + n * row_h + pad_b
        w = 900
        bar_w = w - pad_l - pad_r
        max_abs = max((abs(p["profit_abs"]) for p in pairs), default=1)
        if max_abs < 1e-9:
            max_abs = 1.0
        lines = []
        for i, p in enumerate(pairs):
            y = pad_t + i * row_h + row_h // 2
            val = p["profit_abs"]
            bw = abs(val) / max_abs * bar_w * 0.8
            color = "#22c55e" if val >= 0 else "#ef4444"
            pair_name = html.escape(str(p["pair"])[:20])
            lines.append(
                f'<text x="{pad_l - 8}" y="{y + 4}" '
                f'text-anchor="end" fill="#bbb" '
                f'font-size="10">{pair_name}</text>'
            )
            lines.append(
                f'<rect x="{pad_l}" y="{y - 8}" '
                f'width="{bw:.1f}" height="16" '
                f'fill="{color}" rx="2"/>'
            )
            lines.append(
                f'<text x="{pad_l + bw + 6:.1f}" y="{y + 4}" '
                f'fill="{color}" font-size="10">'
                f"{val:+.2f}</text>"
            )
        body = "\n".join(lines)
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{body}</svg>'

    def _section_best_vs_median(self) -> str:
        bvm = self.d.get("best_vs_median_gap")
        if not bvm:
            return ""
        badge = ""
        if bvm["outlier"]:
            badge = (
                ' <span style="color:#eab308;font-size:0.85em">'
                "(outlier — may be the luckiest, not the best)</span>"
            )
        return (
            '<div class="section">'
            "<h2>Best vs. Median Top-10</h2>"
            '<div class="mini-section">'
            f"Best epoch profit: <strong>{bvm['best_profit']:.2f}%"
            f"</strong> | Median top-10: "
            f"<strong>{bvm['median_profit']:.2f}%</strong>"
            f" | Gap: <strong>{bvm['gap_ratio']:.2f}x</strong>"
            f"{badge}"
            "</div></div>"
        )

    def _section_dispersion_bands(self) -> str:
        bands = self.d.get("dispersion_bands") or {}
        if not bands:
            return ""
        parts = ['<div class="section"><h2>Top-10 Dispersion</h2>']
        labels = {
            "profit": "Profit %",
            "drawdown": "Max DD %",
            "sharpe": "Sharpe",
        }
        for key, label in labels.items():
            b = bands.get(key)
            if not b:
                continue
            parts.append(self._svg_band(label, b))
        parts.append("</div>")
        return "".join(parts)

    @staticmethod
    def _svg_band(label: str, b: dict) -> str:
        w, h = 500, 30
        pad_l = 80
        track_w = w - pad_l - 20
        lo, med, hi = b["min"], b["median"], b["max"]
        span = hi - lo if hi > lo else 1.0
        x_lo = pad_l
        x_med = pad_l + (med - lo) / span * track_w
        x_hi = pad_l + track_w
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:transparent;margin:2px 0">'
            f'<text x="{pad_l - 8}" y="20" text-anchor="end" '
            f'fill="#888" font-size="10">{html.escape(label)}</text>'
            f'<rect x="{x_lo}" y="10" width="{track_w}" '
            f'height="10" class="band-track"/>'
            f'<rect x="{x_lo}" y="10" '
            f'width="{x_hi - x_lo:.1f}" height="10" '
            f'fill="#3a4a6a" rx="3"/>'
            f'<line x1="{x_med:.1f}" y1="6" '
            f'x2="{x_med:.1f}" y2="24" '
            f'stroke="#00d4ff" stroke-width="2"/>'
            f'<text x="{x_lo}" y="28" fill="#666" '
            f'font-size="8">{lo:.2f}</text>'
            f'<text x="{x_med:.1f}" y="28" fill="#00d4ff" '
            f'font-size="8" text-anchor="middle">{med:.2f}</text>'
            f'<text x="{x_hi}" y="28" fill="#666" '
            f'font-size="8" text-anchor="end">{hi:.2f}</text>'
            "</svg>"
        )

    def _section_loss_histogram(self) -> str:
        hist = self.d.get("loss_histogram")
        if not hist or not hist.get("bins"):
            return ""
        return (
            '<div class="section"><h2>Loss Distribution</h2>'
            + self._svg_loss_histogram(hist)
            + "</div>"
        )

    @staticmethod
    def _svg_loss_histogram(hist: dict) -> str:
        bins = hist["bins"]
        best_loss = hist["best_loss"]
        w, h = 900, 250
        pad_l, pad_r, pad_t, pad_b = 60, 20, 20, 40
        max_count = max((b["count"] for b in bins), default=1)
        if max_count == 0:
            max_count = 1
        n = len(bins)
        bar_w = (w - pad_l - pad_r) / max(n, 1)

        bars = []
        for i, b in enumerate(bins):
            bh = b["count"] / max_count * (h - pad_t - pad_b)
            x = pad_l + i * bar_w
            y = h - pad_b - bh
            bars.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" '
                f'width="{bar_w * 0.85:.1f}" '
                f'height="{bh:.1f}" fill="#3a4a6a" rx="1"/>'
            )
            if i % max(1, n // 5) == 0:
                bars.append(
                    f'<text x="{x:.1f}" y="{h - 8}" fill="#888" font-size="9">{b["lo"]:.3f}</text>'
                )

        # Best loss marker
        if bins:
            lo_val = bins[0]["lo"]
            hi_val = bins[-1]["hi"]
            rng = hi_val - lo_val
            if rng > 0:
                bx = pad_l + (best_loss - lo_val) / rng * (w - pad_l - pad_r)
                bars.append(
                    f'<line x1="{bx:.1f}" y1="{pad_t}" '
                    f'x2="{bx:.1f}" y2="{h - pad_b}" '
                    f'stroke="#00d4ff" stroke-width="2" '
                    f'stroke-dasharray="4"/>'
                )
                bars.append(
                    f'<text x="{bx:.1f}" y="{pad_t - 4}" '
                    f'fill="#00d4ff" font-size="9" '
                    f'text-anchor="middle">best</text>'
                )

        bars.append(
            f'<text x="{w // 2}" y="{h - 2}" fill="#888" '
            f'font-size="11" text-anchor="middle">Loss</text>'
        )
        bars.append(
            f'<text x="12" y="{h // 2}" fill="#888" '
            f'font-size="11" text-anchor="middle" '
            f'transform="rotate(-90 12 {h // 2})">Count</text>'
        )

        body = "\n".join(bars)
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{body}</svg>'

    def _section_param_correlation(self) -> str:
        corr = self.d.get("param_correlation", [])
        if not corr:
            return ""
        params = sorted({c["param_a"] for c in corr} | {c["param_b"] for c in corr})
        if len(params) < 2:
            return ""
        return (
            '<div class="section">'
            "<h2>Parameter Correlation (Top-10)</h2>"
            + self._svg_correlation_heatmap(corr, params)
            + "</div>"
        )

    @staticmethod
    def _svg_correlation_heatmap(corr: list[dict], params: list[str]) -> str:
        n = len(params)
        cell = 45
        pad_l, pad_t = 100, 80
        w = pad_l + n * cell + 10
        h = pad_t + n * cell + 10
        idx = {p: i for i, p in enumerate(params)}
        corr_map: dict[tuple[int, int], float] = {}
        for c in corr:
            ia = idx.get(c["param_a"], -1)
            ib = idx.get(c["param_b"], -1)
            if ia >= 0 and ib >= 0:
                corr_map[(ia, ib)] = c["correlation"]
                corr_map[(ib, ia)] = c["correlation"]

        def _color(r: float) -> str:
            if r > 0:
                g = int(min(r, 1.0) * 180)
                return f"rgb({g + 60}, {60}, {60})"
            g = int(min(abs(r), 1.0) * 180)
            return f"rgb({60}, {60}, {g + 60})"

        elems = []
        for i, p in enumerate(params):
            lbl = html.escape(p[:12])
            x = pad_l + i * cell + cell // 2
            elems.append(
                f'<text x="{x}" y="{pad_t - 8}" fill="#bbb" '
                f'font-size="9" text-anchor="middle" '
                f'transform="rotate(-45 {x} {pad_t - 8})">'
                f"{lbl}</text>"
            )
            y = pad_t + i * cell + cell // 2 + 3
            elems.append(
                f'<text x="{pad_l - 6}" y="{y}" fill="#bbb" '
                f'font-size="9" text-anchor="end">{lbl}</text>'
            )
        for i in range(n):
            for j in range(n):
                x = pad_l + j * cell
                y = pad_t + i * cell
                if i == j:
                    r = 1.0
                else:
                    r = corr_map.get((i, j), 0.0)
                col = _color(r)
                elems.append(
                    f'<rect x="{x}" y="{y}" '
                    f'width="{cell - 2}" height="{cell - 2}" '
                    f'fill="{col}" rx="3"/>'
                )
                tc = "#e0e0e0" if abs(r) > 0.3 else "#888"
                elems.append(
                    f'<text x="{x + cell // 2}" '
                    f'y="{y + cell // 2 + 3}" fill="{tc}" '
                    f'font-size="9" text-anchor="middle">'
                    f"{r:.2f}</text>"
                )
        body = "\n".join(elems)
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{body}</svg>'

    def _section_parallel_coords(self) -> str:
        pc = self.d.get("parallel_coords") or {}
        params = pc.get("params", [])
        lines = pc.get("lines", [])
        if len(params) < 2 or len(lines) < 3:
            return ""
        return (
            '<div class="section">'
            "<h2>Parallel Coordinates (Top-10)</h2>" + self._svg_parallel_coords(pc) + "</div>"
        )

    @staticmethod
    def _svg_parallel_coords(pc: dict) -> str:
        params = pc["params"]
        lines = pc["lines"]
        n_params = len(params)
        w, h = 900, 320
        pad_l, pad_r, pad_t, pad_b = 40, 40, 50, 30
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b

        elems = []
        # Axes
        for i, p in enumerate(params):
            x = pad_l + i * plot_w / max(n_params - 1, 1)
            elems.append(
                f'<line x1="{x:.1f}" y1="{pad_t}" '
                f'x2="{x:.1f}" y2="{h - pad_b}" '
                f'stroke="#2a2a4a" stroke-width="1"/>'
            )
            lbl = html.escape(p[:15])
            elems.append(
                f'<text x="{x:.1f}" y="{pad_t - 10}" '
                f'fill="#bbb" font-size="9" text-anchor="middle">'
                f"{lbl}</text>"
            )

        for li, line in enumerate(lines):
            vals = line["values"]
            opacity = max(0.15, 1.0 - li * 0.12)
            color = "#00d4ff" if li == 0 else "#888"
            stroke_w = 2.5 if li == 0 else 1.2
            points = []
            for pi, p in enumerate(params):
                x = pad_l + pi * plot_w / max(n_params - 1, 1)
                v = max(0.0, min(1.0, vals.get(p, 0.5)))
                y = pad_t + (1.0 - v) * plot_h
                points.append(f"{x:.1f},{y:.1f}")
            if points:
                elems.append(
                    f'<polyline points="{" ".join(points)}" '
                    f'fill="none" stroke="{color}" '
                    f'stroke-width="{stroke_w}" '
                    f'opacity="{opacity:.2f}"/>'
                )

        # Legend
        elems.append(
            f'<line x1="{w - 120}" y1="12" '
            f'x2="{w - 100}" y2="12" '
            f'stroke="#00d4ff" stroke-width="2.5"/>'
        )
        elems.append(f'<text x="{w - 96}" y="15" fill="#00d4ff" font-size="9">Best epoch</text>')

        body = "\n".join(elems)
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{body}</svg>'

    # ------------------------------------------------------------------
    # Existing explanation sections
    # ------------------------------------------------------------------

    def _section_loss_explanation(self) -> str:
        loss_name = self.d.get("hyperopt_loss", "")
        entry = LOSS_GLOSSARY.get(loss_name, {})
        if not entry:
            return ""
        one_liner = self._esc(entry.get("one_liner", ""))
        best_for = self._esc(entry.get("best_for", ""))
        metrics = self._esc(entry.get("metrics", ""))
        parts = [
            '<div class="section"><h2>Loss Function</h2>'
            f'<div class="explain-box">'
            f"<strong>{self._esc(loss_name)}</strong> — {one_liner}",
        ]
        if best_for:
            parts.append(f"<br><strong>Best for:</strong> {best_for}")
        if metrics:
            parts.append(f"<br><strong>Metrics:</strong> {metrics}")
        parts.append("</div></div>")
        return "".join(parts)

    def _section_sampler_explanation(self) -> str:
        sampler_name = self.d.get("sampler") or "TPESampler"
        entry = SAMPLER_GLOSSARY.get(sampler_name, {})
        if not entry:
            return ""
        one_liner = self._esc(entry.get("one_liner", ""))
        explanation = self._esc(entry.get("explanation", ""))
        return (
            '<div class="section"><h2>Sampler</h2>'
            f'<div class="explain-box">'
            f"<strong>{self._esc(sampler_name)}</strong> — {one_liner}"
            f"<br><br>{explanation}"
            "</div></div>"
        )

    def _section_next_steps(self) -> str:
        m = self.d.get("best_epoch", {})
        profit_total = m.get("profit_total", 0.0)
        calmar = m.get("calmar", 0.0)
        total_trades = m.get("total_trades", 0)
        cfg = self.d.get("config_summary") or {}
        min_trades = cfg.get("min_trades", 0)
        strategy_raw = self.d.get("strategy", "Strategy")
        strategy = self._esc(strategy_raw)

        if profit_total <= 0:
            guidance = (
                "The best epoch is not profitable. This is a clear signal the parameters "
                "found do not produce an edge on this data. Do NOT deploy. Consider: "
                "expanding the timerange, reducing the number of optimized parameters, "
                "switching loss function, or revisiting the strategy logic itself."
            )
        elif calmar < 0.5:
            guidance = (
                "Profit is positive but the Calmar ratio is below 0.5 — the drawdown is "
                "too large relative to the return. Try CalmarHyperOptLoss if not already "
                "used, tighten the stoploss space, or increase min-trades to force the "
                "optimizer to find solutions with more statistical confidence."
            )
        elif total_trades < max(min_trades, 30):
            guidance = (
                f"Only {total_trades} trades — not enough statistical confidence. "
                "Increase --hyperopt-min-trades or shorten the timeframe. Metrics from "
                "fewer than 30 trades can be dominated by a handful of lucky positions."
            )
        else:
            guidance = (
                "Results look reasonable. Copy the best parameters to your strategy JSON "
                f"(user_data/strategies/{strategy_raw}.json). Validate with a live dry-run "
                "at minimal size before scaling capital. Monitor Calmar and win rate "
                "against these in-sample numbers — sustained divergence signals regime change."
            )

        output_detail = (
            "<p>Optimized parameters are saved in "
            f"<code>user_data/hyperopt_results/</code>.</p>"
            "<p>To apply them, copy or symlink the JSON output to "
            f"<code>user_data/strategies/{strategy}.json</code>. "
            "Freqtrade loads this file at startup and overrides buy_params/sell_params.</p>"
        )

        return (
            '<div class="section"><h2>Next Steps</h2>'
            f'<div class="next-steps"><p>{self._esc(guidance)}</p></div>'
            + self._details("Applying the parameters", output_detail)
            + "</div>"
        )

    @staticmethod
    def _section_glossary() -> str:
        rows = ""
        for slug, entry in sorted(METRIC_GLOSSARY.items()):
            abbrev = html.escape(entry.get("abbrev", slug))
            name = html.escape(entry.get("name", ""))
            one_liner = html.escape(entry.get("one_liner", ""))
            rows += (
                f"<tr><td><strong>{abbrev}</strong></td><td>{name}</td><td>{one_liner}</td></tr>\n"
            )
        return (
            '<div class="section">'
            "<details><summary><h2 style='display:inline'>Glossary</h2></summary><div>"
            "<table><tr><th>Abbrev</th><th>Full Name</th><th>Description</th></tr>"
            f"{rows}"
            "</table></div></details></div>"
        )

    @staticmethod
    def _html_footer() -> str:
        return """
</div>
</body>
</html>"""
