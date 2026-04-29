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
            self._section_best_params(),
            self._section_top10_table(),
            self._section_convergence_chart(),
            self._section_param_agreement(),
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
            kv("Sharpe", self._fmt(sharpe)),
            kv("Sortino", self._fmt(sortino)),
            kv(
                self._tip("pf", "Profit Factor") + self._threshold_badge("pf", pf),
                self._fmt(pf),
            ),
            kv(
                self._tip("dd", "Max Drawdown"),
                f"{self._fmt(dd_pct)}% ({self._fmt(dd_abs)} {sc})",
            ),
            kv(
                self._tip("expectancy", "Expectancy"),
                f"{self._fmt(expectancy)} {sc}",
            ),
            kv("Expectancy Ratio", self._fmt(expectancy_ratio)),
            kv("Avg Holding", holding),
        ]

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
        all_losses = self.d.get("all_losses", [])
        if len(all_losses) < 2:
            return ""
        return (
            '<div class="section"><h2>Convergence Chart</h2>'
            + self._svg_convergence(all_losses)
            + "</div>"
        )

    @staticmethod
    def _svg_convergence(all_losses: list[float]) -> str:
        w, h = 900, 300
        pad_l, pad_r, pad_t, pad_b = 70, 20, 20, 40
        n = len(all_losses)

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

        # All epochs scatter (thin dots)
        dots = ""
        for i, v in enumerate(all_losses):
            if v != v or abs(v) >= 1e15:
                continue
            cx = sx(i)
            cy = sy(v)
            dots += f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="1.5" fill="#3a4a6a" opacity="0.7"/>\n'

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
        cfg = self.d.get("config_summary", {})
        min_trades = cfg.get("min_trades", 0)
        strategy = self._esc(self.d.get("strategy", "Strategy"))

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
                f"(user_data/strategies/{strategy}.json). Validate with a live dry-run "
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
