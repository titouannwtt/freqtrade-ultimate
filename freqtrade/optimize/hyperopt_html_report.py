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
        L = self._L
        parts = [
            self._html_header(),
            self._section_run_summary(),
            self._section_intro(),
            # -- Performance --
            self._group_open(
                "perf",
                L("Performance", "Performance"),
            ),
            self._section_best_epoch(),
            self._section_sans_top_trade(),
            self._section_pair_distribution(),
            self._section_regime_analysis(),
            self._group_close(),
            # -- Robustness --
            self._group_open(
                "robust",
                L(
                    "Robustness Analysis",
                    "Analyse de robustesse",
                ),
            ),
            self._section_overfit_warnings(),
            self._section_dof_traffic_light(),
            self._section_monte_carlo(),
            self._section_best_vs_median(),
            self._section_dispersion_bands(),
            self._section_return_vs_dd(),
            self._group_close(),
            # -- Parameters --
            self._group_open(
                "params",
                L(
                    "Parameter Analysis",
                    "Analyse des paramètres",
                ),
            ),
            self._section_best_params(),
            self._section_param_deep_dive(),
            self._section_param_agreement(),
            self._section_param_correlation(),
            self._section_sensitivity_grid(),
            self._section_parallel_coords(),
            self._group_close(),
            # -- Convergence --
            self._group_open(
                "conv",
                L("Convergence", "Convergence"),
            ),
            self._section_convergence_chart(),
            self._section_loss_histogram(),
            self._section_top10_table(),
            self._group_close(),
            # -- Reference --
            self._group_open(
                "ref",
                L("Reference", "Référence"),
            ),
            self._section_sampler_explanation(),
            self._section_next_steps(),
            self._section_glossary(),
            self._group_close(),
            self._html_footer(),
        ]
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _group_open(cls: str, title: str) -> str:
        return f'<div class="group group-{cls}"><div class="group-title">{title}</div>'

    @staticmethod
    def _group_close() -> str:
        return "</div>"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _esc(text: str) -> str:
        return html.escape(str(text))

    @staticmethod
    def _tip(slug: str, display: str) -> str:
        entry = METRIC_GLOSSARY.get(slug, {})
        tip_en = html.escape(entry.get("explanation", entry.get("one_liner", "")))
        if not tip_en:
            if "<" not in display:
                return html.escape(display)
            return display
        tip_fr = html.escape(entry.get("explanation_fr", ""))
        tip_body = f'<span lang="en">{tip_en}</span>'
        if tip_fr:
            tip_body += f'<span lang="fr">{tip_fr}</span>'
        safe = display if "<" in display else html.escape(display)
        return f'<span class="tooltip">{safe}<span class="tip-text">{tip_body}</span></span>'

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
    def _L(en: str, fr: str) -> str:
        return f'<span lang="en">{en}</span><span lang="fr">{fr}</span>'

    def _desc(self, en: str, fr: str) -> str:
        return f'<p class="section-desc">{self._L(en, fr)}</p>'

    @staticmethod
    def _fmt_range(raw: str) -> str:
        parts = raw.split("-")
        out = []
        for p in parts:
            p = p.strip()
            if len(p) == 8 and p.isdigit():
                out.append(f"{p[:4]}-{p[4:6]}-{p[6:]}")
            else:
                out.append(p)
        return " / ".join(out)

    @staticmethod
    def _fmt(value: float, decimals: int = 2) -> str:
        try:
            return f"{value:.{decimals}f}"
        except (TypeError, ValueError):
            return "N/A"

    def _advisory(self, level: str, en: str, fr: str) -> str:
        cls = {
            "good": "advisory-good",
            "warn": "advisory-warn",
            "bad": "advisory-bad",
        }.get(level, "advisory-warn")
        return f'<div class="advisory {cls}">{self._L(en, fr)}</div>'

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
        L = self._L
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Hyperopt Report — {strategy}</title>
<style>{self._css()}</style>
</head>
<body>
<div class="lang-bar">
  {L("Language", "Langue")}:
  <select onchange="document.documentElement.lang=this.value">
    <option value="en">English</option>
    <option value="fr">Français</option>
  </select>
</div>
<div class="container">
<h1>{L("Hyperopt Report", "Rapport Hyperopt")} — {strategy}</h1>
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
span[lang="fr"] { display: none; }
html:lang(fr) span[lang="en"] { display: none; }
html:lang(fr) span[lang="fr"] { display: inline; }
.lang-bar {
  position: fixed; top: 10px; right: 20px; z-index: 100;
  background: #16213e; border: 1px solid #2a2a4a; border-radius: 6px;
  padding: 4px 8px; font-size: 0.85em;
}
.lang-bar select {
  background: #0f0f23; color: #e0e0e0; border: 1px solid #2a2a4a;
  border-radius: 4px; padding: 2px 6px; font-size: 0.9em; cursor: pointer;
}
.mini-section {
  background: #16213e; padding: 12px 16px; border-radius: 6px;
  margin: 10px 0; font-size: 0.88em; line-height: 1.6;
}
.badge-inline { font-size: 0.78em; margin-left: 6px; }
.section-desc {
  color: #888; font-size: 0.82em; margin: 2px 0 10px 0;
  font-style: italic; line-height: 1.4;
}
.band-track {
  fill: #2a2a4a; rx: 3; ry: 3;
}
.warn-box {
  background: #1a2744; padding: 10px 14px; border-radius: 6px;
  margin: 8px 0; line-height: 1.5;
}
.param-detail {
  background: #16213e; padding: 10px 14px; border-radius: 6px;
  margin: 6px 0; font-size: 0.88em; line-height: 1.5;
}
.param-detail .pd-row {
  display: inline-block; margin-right: 16px; margin-bottom: 4px;
}
.pd-label { color: #888; font-size: 0.85em; }
.pd-value { font-weight: bold; font-family: monospace; }
.badge-conv { color: #22c55e; font-size: 0.82em; }
.badge-spread { color: #eab308; font-size: 0.82em; }
.badge-sens-high { color: #ef4444; font-size: 0.82em; }
.badge-sens-med { color: #eab308; font-size: 0.82em; }
.badge-sens-low { color: #22c55e; font-size: 0.82em; }
.badge-boundary { color: #ef4444; font-size: 0.78em; }
code.filepath {
  background: #0f0f23; padding: 1px 6px; border-radius: 3px;
  border: 1px solid #2a2a4a; font-size: 0.9em;
}
pre { white-space: pre-wrap; word-break: break-all; }
.run-summary {
  background: linear-gradient(135deg, #0f1a2e 0%, #162040 100%);
  border: 1px solid #2a3a5a; border-radius: 10px;
  padding: 20px; margin: 16px 0;
}
.run-card {
  background: #0f0f23; border-radius: 8px;
  padding: 14px 18px; margin: 8px 0;
  border: 1px solid #2a2a4a;
}
.run-card h3 { margin: 0 0 8px 0; font-size: 0.95em; }
.run-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.run-json {
  background: #0a0a1a; border-radius: 6px;
  padding: 12px; font-family: monospace;
  font-size: 0.82em; color: #84cc16;
  border: 1px solid #1a2a3a;
  max-height: 300px; overflow-y: auto;
}
.run-cmd {
  background: #0a0a1a; border-radius: 6px;
  padding: 10px 14px; font-family: monospace;
  font-size: 0.82em; color: #00d4ff;
  border: 1px solid #1a2a3a;
  word-break: break-all;
}
.warn-actions summary {
  cursor: pointer; color: #00d4ff; font-size: 0.85em;
  margin-top: 6px;
}
.warn-actions summary:hover { text-decoration: underline; }
.group {
  border-left: 4px solid var(--gc, #2a2a4a);
  margin: 32px 0; padding: 0 0 0 16px;
}
.group-title {
  font-size: 1.15em; font-weight: bold; margin: 0 0 4px 0;
  letter-spacing: 0.5px;
}
.group-perf { --gc: #22c55e; }
.group-perf .group-title { color: #22c55e; }
.group-robust { --gc: #eab308; }
.group-robust .group-title { color: #eab308; }
.group-params { --gc: #a855f7; }
.group-params .group-title { color: #a855f7; }
.group-conv { --gc: #00d4ff; }
.group-conv .group-title { color: #00d4ff; }
.group-ref { --gc: #888; }
.group-ref .group-title { color: #888; }
.advisory {
  margin: 10px 0 0 0; padding: 8px 12px; border-radius: 6px;
  font-size: 0.82em; line-height: 1.4; border-left: 3px solid;
}
.advisory-good { background: #0d2818; border-color: #22c55e;
                 color: #86efac; }
.advisory-warn { background: #1a1a00; border-color: #eab308;
                 color: #fde68a; }
.advisory-bad { background: #1a0d0d; border-color: #ef4444;
                color: #fca5a5; }
"""

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    @staticmethod
    def _section_intro() -> str:
        L = HyperoptHTMLReport._L
        what_en = (
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
        what_fr = (
            "Hyperopt lance votre stratégie des centaines ou milliers de fois avec "
            "différentes combinaisons de paramètres, cherchant celle qui optimise une "
            "fonction de loss (ex. ratio Calmar, Sharpe). Le résultat est le meilleur "
            "epoch — le jeu de paramètres qui a minimisé la loss sur vos données."
            "<br><br>"
            "<strong>Attention :</strong> Les résultats hyperopt sont in-sample — "
            "l'optimiseur a vu les mêmes données sur lesquelles il est évalué. Un bon "
            "résultat ne garantit pas la rentabilité en live. Validez toujours avec une "
            "analyse walk-forward ou un dry-run avant d'engager du capital réel."
            "<br><br>"
            "<strong>Lire ce rapport :</strong> "
            "<strong>Meilleur Epoch</strong> = les métriques des paramètres optimisés. "
            "<strong>Top 10</strong> = les 10 meilleurs epochs par loss — vérifiez la cohérence. "
            "<strong>Convergence</strong> = comment l'optimiseur s'est amélioré. "
            "<strong>Accord des paramètres</strong> = les paramètres stables sont plus fiables. "
            "Survolez les termes soulignés pour les définitions."
        )
        body = L(what_en, what_fr)
        summary_en = "What is hyperopt and how to read this report?"
        summary_fr = "Qu'est-ce que l'hyperopt et comment lire ce rapport ?"
        return (
            '<div class="intro"><details><summary>'
            + L(html.escape(summary_en), html.escape(summary_fr))
            + f"</summary><div>{body}</div></details></div>"
        )

    @staticmethod
    def _format_duration(secs: float) -> str:
        s = int(secs)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h {m}m"

    def _run_summary_cfg_rows(self) -> list[tuple]:
        L = self._L
        cfg = self.d.get("config_summary") or {}
        strategy = self._esc(self.d.get("strategy", ""))
        loss_name = self._esc(self.d.get("hyperopt_loss", ""))
        sampler = self._esc(self.d.get("sampler") or "TPESampler")
        loss_entry = LOSS_GLOSSARY.get(self.d.get("hyperopt_loss", ""), {})
        loss_desc = self._esc(loss_entry.get("one_liner", ""))
        sc = self._esc(self.d.get("stake_currency", "USDT"))
        spaces = ", ".join(cfg.get("spaces", []))
        jobs = cfg.get("jobs", -1)
        rng = cfg.get("random_state")
        ape = cfg.get("analyze_per_epoch", False)
        p_all = cfg.get("print_all", False)
        p_json = cfg.get("print_json", False)
        elapsed = self.d.get("elapsed_secs")
        dur = self._format_duration(elapsed) if elapsed else "—"
        up = set(self.d.get("user_params") or [])

        return self._cfg_rows_core(
            L,
            strategy,
            loss_name,
            loss_desc,
            sampler,
            cfg,
            spaces,
            sc,
            jobs,
            rng,
            ape,
            p_all,
            p_json,
            dur,
            up,
        )

    @staticmethod
    def _cfg_rows_core(
        L,
        strategy,
        loss_name,
        loss_desc,
        sampler,
        cfg,
        spaces,
        sc,
        jobs,
        rng,
        ape,
        p_all,
        p_json,
        dur,
        up,
    ) -> list[tuple]:
        # (label, value, desc, cli_arg, is_user, tooltip)
        return [
            (
                L("Strategy", "Stratégie"),
                f"<code>{strategy}</code>",
                L(
                    "Trading strategy optimized",
                    "Stratégie de trading optimisée",
                ),
                "--strategy",
                "strategy" in up,
                L(
                    "The strategy class containing your "
                    "buy/sell logic and the parameter spaces "
                    "to optimize. Must be in "
                    "user_data/strategies/. The strategy "
                    "defines which indicators to compute, "
                    "entry/exit signals, and the "
                    "DecimalParameter / IntParameter / "
                    "CategoricalParameter ranges that "
                    "hyperopt will search.",
                    "La classe de stratégie contenant votre "
                    "logique achat/vente et les espaces de "
                    "paramètres à optimiser. Doit être dans "
                    "user_data/strategies/. La stratégie "
                    "définit les indicateurs, les signaux "
                    "d'entrée/sortie, et les plages "
                    "DecimalParameter / IntParameter / "
                    "CategoricalParameter que l'hyperopt "
                    "va explorer.",
                ),
            ),
            (
                L("Loss function", "Fonction de loss"),
                f"<code>{loss_name}</code>",
                L(loss_desc, loss_desc) if loss_desc else "—",
                "--hyperopt-loss",
                "hyperopt_loss" in up,
                L(
                    "The objective function that hyperopt "
                    "minimizes. Each epoch produces a loss "
                    "value; the epoch with the lowest loss "
                    "wins. Different loss functions optimize "
                    "for different goals: SharpeHyperOptLoss "
                    "maximizes risk-adjusted returns, "
                    "CalmarHyperOptLoss maximizes return/"
                    "drawdown ratio, OnlyProfitHyperOptLoss "
                    "focuses on raw profit. Choice of loss "
                    "function fundamentally changes which "
                    "parameters are selected.",
                    "La fonction objectif que l'hyperopt "
                    "minimise. Chaque epoch produit une "
                    "valeur de loss ; l'epoch avec la "
                    "loss la plus basse gagne. Différentes "
                    "fonctions optimisent différents "
                    "objectifs : SharpeHyperOptLoss pour "
                    "le rendement ajusté au risque, "
                    "CalmarHyperOptLoss pour le ratio "
                    "rendement/drawdown, "
                    "OnlyProfitHyperOptLoss pour le profit "
                    "brut. Le choix change fondamentalement "
                    "les paramètres sélectionnés.",
                ),
            ),
            (
                L("Sampler", "Échantillonneur"),
                f"<code>{sampler}</code>",
                L(
                    "Search algorithm",
                    "Algorithme de recherche",
                ),
                "--hyperopt-sampler",
                "hyperopt_sampler" in up,
                L(
                    "The optimization algorithm that decides "
                    "which parameter combinations to try "
                    "next. TPESampler (Tree-structured "
                    "Parzen Estimator) builds a probabilistic "
                    "model and focuses on promising regions "
                    "— best for most cases. "
                    "NSGAIIISampler uses evolutionary "
                    "multi-objective optimization — useful "
                    "when optimizing multiple conflicting "
                    "objectives. RandomSampler explores "
                    "uniformly — good baseline but "
                    "inefficient with many parameters.",
                    "L'algorithme d'optimisation qui décide "
                    "quelles combinaisons de paramètres "
                    "essayer. TPESampler construit un modèle "
                    "probabiliste et se concentre sur les "
                    "régions prometteuses — le meilleur "
                    "choix en général. NSGAIIISampler "
                    "utilise l'évolution multi-objectif — "
                    "utile pour optimiser plusieurs objectifs "
                    "contradictoires. RandomSampler explore "
                    "uniformément — bon baseline mais "
                    "inefficace avec beaucoup de paramètres.",
                ),
            ),
            (
                L("Epochs", "Epochs"),
                f"<code>{cfg.get('epochs', 0)}</code>",
                L(
                    "Total iterations",
                    "Itérations totales",
                ),
                "--epochs",
                "epochs" in up,
                L(
                    "Number of parameter combinations to "
                    "evaluate. More epochs = better chance "
                    "of finding optimal parameters, but "
                    "longer runtime and higher risk of "
                    "overfitting (the optimizer memorizes "
                    "noise in the data). Rule of thumb: "
                    "100-500 for few parameters, 500-2000 "
                    "for many. Watch the convergence chart "
                    "— if the best loss hasn't improved "
                    "in the last 30% of epochs, you have "
                    "enough.",
                    "Nombre de combinaisons de paramètres "
                    "à évaluer. Plus d'epochs = meilleure "
                    "chance de trouver l'optimum, mais plus "
                    "long et risque accru de surapprentissage "
                    "(l'optimiseur mémorise le bruit). Règle "
                    "empirique : 100-500 pour peu de "
                    "paramètres, 500-2000 pour beaucoup. "
                    "Surveillez la convergence — si la "
                    "meilleure loss n'a pas bougé dans "
                    "les derniers 30%, c'est suffisant.",
                ),
            ),
            (
                L("Spaces", "Espaces"),
                f"<code>{spaces or 'default'}</code>",
                L(
                    "Parameter spaces optimized",
                    "Espaces de paramètres optimisés",
                ),
                "--spaces",
                "spaces" in up,
                L(
                    "Which parameter groups to optimize. "
                    "'buy' = entry signal parameters, "
                    "'sell' = exit signal parameters, "
                    "'roi' = minimal ROI table, "
                    "'stoploss' = stoploss value, "
                    "'trailing' = trailing stop parameters, "
                    "'protection' = trade protections. "
                    "'default' includes all spaces defined "
                    "in the strategy. Optimizing fewer "
                    "spaces reduces search complexity and "
                    "overfitting risk.",
                    "Quels groupes de paramètres optimiser. "
                    "'buy' = paramètres d'entrée, "
                    "'sell' = paramètres de sortie, "
                    "'roi' = table ROI minimum, "
                    "'stoploss' = valeur du stoploss, "
                    "'trailing' = trailing stop, "
                    "'protection' = protections. "
                    "'default' inclut tous les espaces "
                    "définis dans la stratégie. Moins "
                    "d'espaces = moins de complexité et "
                    "moins de surapprentissage.",
                ),
            ),
            (
                L("Min trades", "Min trades"),
                f"<code>{cfg.get('min_trades', 1)}</code>",
                L(
                    "Min trades for valid epoch",
                    "Trades min pour epoch valide",
                ),
                "--min-trades",
                "hyperopt_min_trades" in up,
                L(
                    "Minimum number of trades an epoch must "
                    "produce to be considered valid. Epochs "
                    "with fewer trades are penalized with "
                    "a high loss value. Prevents the "
                    "optimizer from finding parameters that "
                    "trade very rarely (which may look "
                    "profitable by luck). Recommended: "
                    "at least 30-50 for statistical "
                    "significance.",
                    "Nombre minimum de trades qu'un epoch "
                    "doit produire pour être valide. Les "
                    "epochs avec moins de trades reçoivent "
                    "une pénalité élevée. Empêche "
                    "l'optimiseur de trouver des paramètres "
                    "qui tradent très rarement (qui peuvent "
                    "sembler rentables par chance). "
                    "Recommandé : au moins 30-50 pour la "
                    "significativité statistique.",
                ),
            ),
            (
                L("Timerange", "Période"),
                f"<code>{HyperoptHTMLReport._fmt_range(cfg.get('timerange') or '—')}</code>",
                L(
                    "Data period",
                    "Période de données",
                ),
                "--timerange",
                "timerange" in up,
                L(
                    "The date range of historical data used "
                    "for optimization. Format: YYYYMMDD-"
                    "YYYYMMDD. Using recent data (3-6 "
                    "months) is crucial — market regimes "
                    "change and old patterns may not hold. "
                    "The data must match the target exchange "
                    "(different exchanges have different "
                    "price action). Longer ranges give more "
                    "trades but dilute recent regime "
                    "relevance.",
                    "La période de données historiques "
                    "utilisée pour l'optimisation. Format : "
                    "AAAAMMJJ-AAAAMMJJ. Utiliser des données "
                    "récentes (3-6 mois) est crucial — les "
                    "régimes de marché changent et les "
                    "anciens patterns ne tiennent pas. Les "
                    "données doivent correspondre à "
                    "l'exchange cible. Des plages plus "
                    "longues donnent plus de trades mais "
                    "diluent la pertinence du régime actuel.",
                ),
            ),
            (
                L("Timeframe", "Timeframe"),
                f"<code>{cfg.get('timeframe') or '—'}</code>",
                L(
                    "Candle timeframe",
                    "Timeframe des bougies",
                ),
                "--timeframe",
                "timeframe" in up,
                L(
                    "The candlestick interval used by the "
                    "strategy. Common values: 1m, 5m, 15m, "
                    "1h, 4h, 1d. Shorter timeframes generate "
                    "more signals but are noisier and more "
                    "sensitive to slippage/fees. Must match "
                    "the timeframe your strategy is designed "
                    "for. Informative pairs may use "
                    "different timeframes via "
                    "informative_pairs().",
                    "L'intervalle des bougies utilisé par "
                    "la stratégie. Valeurs courantes : 1m, "
                    "5m, 15m, 1h, 4h, 1d. Les timeframes "
                    "courts génèrent plus de signaux mais "
                    "sont plus bruités et sensibles au "
                    "slippage/frais. Doit correspondre au "
                    "timeframe pour lequel votre stratégie "
                    "est conçue.",
                ),
            ),
            (
                L("Currency", "Devise"),
                f"<code>{sc}</code>",
                L("Stake currency", "Devise de stake"),
                "--stake-currency",
                "stake_currency" in up,
                L(
                    "The currency used for trading (your "
                    "base capital). For spot: typically USDT "
                    "or BTC. For futures: USDT or USDC. "
                    "Affects pair selection (only pairs "
                    "quoted in this currency are traded), "
                    "profit calculations, and fee "
                    "accounting.",
                    "La devise utilisée pour le trading "
                    "(votre capital de base). Spot : "
                    "typiquement USDT ou BTC. Futures : "
                    "USDT ou USDC. Affecte la sélection "
                    "des paires (seules les paires cotées "
                    "dans cette devise sont tradées), le "
                    "calcul des profits et la comptabilité "
                    "des frais.",
                ),
            ),
            (
                L("Jobs", "Jobs"),
                f"<code>{jobs}</code>",
                L(
                    "Parallel workers (-1 = all CPUs)",
                    "Workers parallèles (-1 = tous CPUs)",
                ),
                "-j / --job-workers",
                "hyperopt_jobs" in up,
                L(
                    "Number of parallel workers for epoch "
                    "evaluation. -1 uses all available CPU "
                    "cores. More workers = faster but uses "
                    "more RAM (each worker loads the full "
                    "dataset). If you hit memory limits, "
                    "reduce this value. On a 16-core machine "
                    "with 32GB RAM, -1 works well. On "
                    "limited hardware, try 2-4.",
                    "Nombre de workers parallèles pour "
                    "l'évaluation des epochs. -1 utilise "
                    "tous les coeurs CPU. Plus de workers = "
                    "plus rapide mais plus de RAM (chaque "
                    "worker charge le dataset complet). Si "
                    "vous manquez de mémoire, réduisez. "
                    "Sur une machine 16 coeurs / 32GB RAM, "
                    "-1 fonctionne bien. Sur du matériel "
                    "limité, essayez 2-4.",
                ),
            ),
            (
                L("Random state", "Graine aléatoire"),
                f"<code>{rng if rng is not None else 'None'}</code>",
                L(
                    "RNG seed for reproducibility",
                    "Graine RNG pour reproductibilité",
                ),
                "--random-state",
                "hyperopt_random_state" in up,
                L(
                    "Seed for the random number generator. "
                    "Setting a fixed value (e.g. 42) makes "
                    "results reproducible — running the "
                    "same hyperopt twice gives identical "
                    "results. None = random seed each run. "
                    "Useful for debugging or comparing "
                    "loss functions on the exact same "
                    "parameter sequences.",
                    "Graine pour le générateur de nombres "
                    "aléatoires. Une valeur fixe (ex. 42) "
                    "rend les résultats reproductibles — "
                    "relancer le même hyperopt donne des "
                    "résultats identiques. None = graine "
                    "aléatoire à chaque run. Utile pour "
                    "le debug ou comparer des fonctions de "
                    "loss sur les mêmes séquences.",
                ),
            ),
            (
                L("Analyze/epoch", "Analyse/epoch"),
                f"<code>{ape}</code>",
                L(
                    "Run analysis after each epoch",
                    "Analyser après chaque epoch",
                ),
                "--analyze-per-epoch",
                "analyze_per_epoch" in up,
                L(
                    "When enabled, runs the full strategy "
                    "analysis (indicators, signals) after "
                    "each epoch instead of batch-processing. "
                    "Slower but required for strategies that "
                    "use FreqAI or dynamic informative "
                    "pairs. The first epoch runs in "
                    "single-threaded mode to populate the "
                    "data cache.",
                    "Quand activé, lance l'analyse complète "
                    "de la stratégie (indicateurs, signaux) "
                    "après chaque epoch au lieu du batch. "
                    "Plus lent mais nécessaire pour les "
                    "stratégies utilisant FreqAI ou des "
                    "paires informatives dynamiques. Le "
                    "premier epoch tourne en mono-thread "
                    "pour remplir le cache.",
                ),
            ),
            (
                L("Print all", "Afficher tout"),
                f"<code>{p_all}</code>",
                L(
                    "Print all epoch results",
                    "Afficher tous les résultats",
                ),
                "--print-all",
                "print_all" in up,
                L(
                    "Print results for every epoch to the "
                    "console, not just improvements. Useful "
                    "for monitoring progress and spotting "
                    "patterns in bad epochs. Generates a "
                    "lot of output — best combined with "
                    "logging to a file. Default: only "
                    "epochs that improve the best loss are "
                    "printed.",
                    "Affiche les résultats de chaque epoch "
                    "dans la console, pas seulement les "
                    "améliorations. Utile pour surveiller "
                    "la progression et repérer des patterns "
                    "dans les mauvais epochs. Génère "
                    "beaucoup de sortie — à combiner avec "
                    "un fichier de log. Par défaut : seuls "
                    "les epochs qui améliorent la loss "
                    "sont affichés.",
                ),
            ),
            (
                L("Print JSON", "JSON console"),
                f"<code>{p_json}</code>",
                L(
                    "Output results as JSON",
                    "Résultats en JSON",
                ),
                "--print-json",
                "print_json" in up,
                L(
                    "Output the best epoch results in JSON "
                    "format at the end of the run. The JSON "
                    "contains the optimal parameter values "
                    "ready to paste into your strategy's "
                    "buy_params/sell_params dict or into a "
                    "co-located .json file. Combines with "
                    "--print-all to get JSON for every "
                    "epoch.",
                    "Affiche les résultats du meilleur "
                    "epoch en JSON à la fin du run. Le JSON "
                    "contient les valeurs optimales prêtes "
                    "à coller dans le buy_params/sell_params "
                    "de votre stratégie ou dans un fichier "
                    ".json co-localisé. Se combine avec "
                    "--print-all pour avoir le JSON de "
                    "chaque epoch.",
                ),
            ),
            (
                L("Duration", "Durée"),
                f"<code>{dur}</code>",
                L(
                    "Total optimization time",
                    "Temps total d'optimisation",
                ),
                "",
                False,
                L(
                    "Wall-clock time from start to finish "
                    "of the hyperopt run, including data "
                    "loading, indicator computation, and "
                    "all epoch evaluations.",
                    "Temps réel du début à la fin du run "
                    "hyperopt, incluant le chargement des "
                    "données, le calcul des indicateurs "
                    "et l'évaluation de tous les epochs.",
                ),
            ),
        ]

    def _section_run_summary(self) -> str:
        import json as _json

        L = self._L
        strategy = self._esc(self.d.get("strategy", ""))
        loss_name = self._esc(self.d.get("hyperopt_loss", ""))
        cfg = self.d.get("config_summary") or {}
        best_loss = self.d.get("best_loss", 0)

        loss_entry = LOSS_GLOSSARY.get(self.d.get("hyperopt_loss", ""), {})
        loss_for = self._esc(loss_entry.get("best_for", ""))
        spaces = ", ".join(cfg.get("spaces", []))

        # Build full command with all params
        cmd_parts = self._run_summary_cmd(strategy, loss_name, cfg, spaces)
        cmd_multi = " \\\n    ".join(cmd_parts)
        cmd_single = " ".join(cmd_parts)

        # Best params JSON
        bp_raw = self.d.get("best_params_raw") or {}
        bp_details = self.d.get("best_params") or {}
        json_display = bp_details if bp_details else bp_raw
        json_str = _json.dumps(json_display, indent=2)
        copy_js = html.escape(_json.dumps(json_display), quote=True)

        # Config table rows
        rows_html = ""
        for row in self._run_summary_cfg_rows():
            param, val, desc, cli_arg, is_user, tip = row
            src_badge = (
                '<span style="color:#00d4ff">user</span>'
                if is_user
                else '<span style="color:#555">default</span>'
            )
            cli_cell = (
                f"<code style='color:#888;white-space:nowrap'>{cli_arg}</code>" if cli_arg else "—"
            )
            desc_html = (
                f'<span class="tooltip">{desc}<span class="tip-text">{tip}</span></span>'
                if tip
                else desc
            )
            rows_html += (
                f"<tr><td>{param}</td>"
                f"<td>{val}</td>"
                f'<td style="white-space:nowrap">'
                f"{cli_cell}</td>"
                f'<td style="font-size:0.8em">'
                f"{src_badge}</td>"
                f'<td style="color:#888;font-size:0.85em">'
                f"{desc_html}</td></tr>"
            )

        loss_detail = ""
        if loss_for:
            loss_detail = (
                f'<div style="margin:6px 0;font-size:0.85em;'
                f'color:#888">'
                f"<strong>{L('Best for', 'Idéal pour')}"
                f":</strong> {loss_for}</div>"
            )

        best_loss_html = ""
        if best_loss:
            best_loss_html = (
                f'<div style="text-align:center;margin:8px 0">'
                f'<span style="background:#16213e;'
                f"border:2px solid #00d4ff;"
                f"border-radius:8px;padding:6px 16px;"
                f'font-size:0.95em">'
                f"{L('Best loss', 'Meilleure loss')}: "
                f'<strong style="color:#00d4ff;'
                f'font-size:1.2em">{best_loss:.6f}'
                f"</strong></span></div>"
            )

        copy_btn = (
            f'<button onclick="navigator.clipboard.writeText('
            f"'{copy_js}'"
            f").then(function(){{this.textContent="
            f"'Copied!'}}.bind(this))\""
            f' style="background:#16213e;color:#00d4ff;'
            f"border:1px solid #2a2a4a;border-radius:4px;"
            f"padding:4px 10px;cursor:pointer;"
            f'font-size:0.8em;margin:6px 0">' + L("Copy JSON", "Copier JSON") + "</button>"
        )

        return (
            '<div class="run-summary">'
            "<h2 style='margin-top:0;color:#00d4ff'>"
            + L("Run Summary", "Résumé de l'exécution")
            + "</h2>"
            + best_loss_html
            + '<div class="run-grid">'
            '<div class="run-card">'
            "<h3>" + L("Configuration", "Configuration") + "</h3>"
            '<table style="width:100%;font-size:0.85em">'
            f"<tr><th>{L('Parameter', 'Paramètre')}</th>"
            f"<th>{L('Value', 'Valeur')}</th>"
            f"<th>{L('CLI Argument', 'Argument CLI')}</th>"
            f"<th>{L('Source', 'Source')}</th>"
            f"<th>{L('Description', 'Description')}</th>"
            f"</tr>{rows_html}</table>" + loss_detail + "</div>"
            '<div class="run-card">'
            "<h3>"
            + L(
                "Best Result — JSON",
                "Meilleur résultat — JSON",
            )
            + "</h3>"
            f'<div class="run-json">'
            f"<pre>{html.escape(json_str)}</pre></div>"
            f"{copy_btn}"
            "</div></div>" + self._run_summary_cmd_block(cmd_multi, cmd_single) + "</div>"
        )

    @staticmethod
    def _run_summary_cmd(
        strategy: str,
        loss_name: str,
        cfg: dict,
        spaces: str,
    ) -> list[str]:
        parts = [
            "freqtrade hyperopt",
            f"--strategy {strategy}",
            f"--hyperopt-loss {loss_name}",
        ]
        epochs = cfg.get("epochs", 0)
        if epochs:
            parts.append(f"--epochs {epochs}")
        if spaces:
            parts.append(f"--spaces {spaces}")
        min_t = cfg.get("min_trades", 1)
        if min_t and min_t != 1:
            parts.append(f"--min-trades {min_t}")
        tr = cfg.get("timerange", "")
        if tr:
            parts.append(f"--timerange {tr}")
        tf = cfg.get("timeframe", "")
        if tf:
            parts.append(f"--timeframe {tf}")
        jobs = cfg.get("jobs", -1)
        if jobs != -1:
            parts.append(f"-j {jobs}")
        rng = cfg.get("random_state")
        if rng is not None:
            parts.append(f"--random-state {rng}")
        if cfg.get("analyze_per_epoch"):
            parts.append("--analyze-per-epoch")
        if cfg.get("print_all"):
            parts.append("--print-all")
        if cfg.get("print_json"):
            parts.append("--print-json")
        return parts

    def _run_summary_cmd_block(self, cmd_multi: str, cmd_single: str) -> str:
        L = self._L
        cmd_copy = html.escape(cmd_single, quote=True)
        btn_s = (
            "background:#0a0a1a;color:#888;"
            "border:1px solid #2a2a4a;border-radius:3px;"
            "padding:2px 8px;cursor:pointer;"
            "font-size:0.75em;margin-left:8px"
        )
        copy_btn = (
            f'<button onclick="navigator.clipboard'
            f".writeText('{cmd_copy}')"
            f".then(function(){{this.textContent="
            f"'Copied!'}}.bind(this))\""
            f' style="{btn_s}">' + L("Copy", "Copier") + "</button>"
        )
        toggle_btn = (
            ' <button onclick="'
            "var p=this.parentElement.parentElement;"
            "var a=p.querySelector('.cmd-multi');"
            "var b=p.querySelector('.cmd-single');"
            "if(a.style.display==='none'){"
            "a.style.display='block';"
            "b.style.display='none';"
            "this.textContent='single line'}"
            "else{a.style.display='none';"
            "b.style.display='block';"
            "this.textContent='multi line'}"
            f'" style="{btn_s}">'
            "single line</button>"
        )
        return (
            '<div class="run-card" style="margin-top:8px">'
            "<h3>" + L("Command", "Commande") + toggle_btn + copy_btn + "</h3>"
            '<div class="run-cmd cmd-multi">'
            f"<pre>{cmd_multi}</pre></div>"
            '<div class="run-cmd cmd-single" '
            'style="display:none">'
            f"<pre>{cmd_single}</pre></div>"
            "</div>"
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

        L = self._L

        def _row(label: str, value: str, badge: str = "") -> str:
            return f"<tr><td>{label}{badge}</td><td><strong>{value}</strong></td></tr>"

        trades_rows = (
            _row("Trades", str(total_trades))
            + _row(
                L("W / D / L", "V / N / D"),
                f'<span class="pos">{wins}</span> / {draws} / <span class="neg">{losses}</span>',
            )
            + _row(
                L(
                    self._tip("expectancy", "Win rate"),
                    self._tip("expectancy", "Taux de gain"),
                ),
                f'<span class="{p_cls}">{winrate:.1%}</span>',
            )
            + _row(L("Avg Holding", "Durée moyenne"), holding)
        )

        profit_rows = (
            _row(
                L("Total Profit", "Profit total"),
                f'<span class="{p_cls}">'
                f"{sign}{self._fmt(profit_pct)}% "
                f"({self._fmt(profit_abs)} {sc})</span>",
            )
            + _row(
                L("Avg / Trade", "Moy. / Trade"),
                f'<span class="{p_cls}">{sign}{self._fmt(profit_mean_pct, 3)}%</span>',
            )
            + _row(
                L(
                    self._tip("pf", "Profit Factor"),
                    self._tip("pf", "Facteur de profit"),
                ),
                self._fmt(pf),
                self._threshold_badge("pf", pf),
            )
            + _row(
                L(
                    self._tip("expectancy", "Expectancy"),
                    self._tip("expectancy", "Espérance"),
                ),
                f"{self._fmt(expectancy)} {sc}",
            )
            + _row(
                L("Exp. Ratio", "Ratio esp."),
                self._fmt(expectancy_ratio),
            )
        )

        risk_rows = (
            _row(
                self._tip("calmar", "Calmar"),
                self._fmt(calmar),
                self._threshold_badge("calmar", calmar),
            )
            + _row(
                self._tip("sqn", "SQN"),
                self._fmt(sqn),
                self._threshold_badge("sqn", sqn),
            )
            + _row(
                "Sharpe",
                self._fmt(sharpe) + self._benchmark_tag("sharpe"),
                self._dsr_badge(),
            )
            + _row("Sortino", self._fmt(sortino))
            + _row(
                L(
                    self._tip("dd", "Max DD"),
                    self._tip("dd", "DD max."),
                ),
                f"{self._fmt(dd_pct)}% ({self._fmt(dd_abs)} {sc})" + self._benchmark_tag("dd"),
            )
        )

        skew_html = self._skew_kurtosis_badges()
        skew_section = ""
        if skew_html:
            skew_section = f'<div style="margin-top:8px">{skew_html}</div>'

        tbl_style = "width:100%;font-size:0.88em;margin:0"
        return (
            '<div class="section" id="sec-best-epoch"><h2>'
            + L(
                "Best Epoch — Summary",
                "Meilleur Epoch — Résumé",
            )
            + "</h2>"
            + self._desc(
                "Key performance metrics from the best parameter set found during optimization.",
                "Métriques clés du meilleur jeu de paramètres trouvé.",
            )
            + '<div style="display:grid;'
            "grid-template-columns:1fr 1fr 1fr;"
            'gap:12px">'
            '<div class="run-card">'
            "<h3>" + L("Trades", "Trades") + "</h3>"
            f'<table style="{tbl_style}">'
            f"{trades_rows}</table></div>"
            '<div class="run-card">'
            "<h3>" + L("Profitability", "Rentabilité") + "</h3>"
            f'<table style="{tbl_style}">'
            f"{profit_rows}</table></div>"
            '<div class="run-card">'
            "<h3>" + L("Risk / Quality", "Risque / Qualité") + "</h3>"
            f'<table style="{tbl_style}">'
            f"{risk_rows}</table></div>"
            f"</div>{skew_section}"
            + self._advisory_best_epoch(profit_pct, dd_pct, sharpe)
            + "</div>"
        )

    def _advisory_best_epoch(self, profit_pct: float, dd_pct: float, sharpe: float) -> str:
        if profit_pct <= 0:
            return self._advisory(
                "bad",
                "Negative profit in-sample: these parameters lose money even on training data.",
                "Profit negatif in-sample : ces parametres "
                "perdent de l'argent meme sur les donnees "
                "d'entrainement.",
            )
        if dd_pct > 30:
            return self._advisory(
                "bad",
                f"Max drawdown {dd_pct:.1f}% exceeds 30%. "
                "High DD in-sample usually means worse "
                "out-of-sample (Bailey & Lopez de Prado).",
                f"Drawdown max {dd_pct:.1f}% depasse 30%. "
                "Un DD eleve in-sample est generalement "
                "pire hors echantillon.",
            )
        if sharpe < 0.5:
            return self._advisory(
                "warn",
                f"Sharpe {sharpe:.2f} is below 0.5 — weak "
                "risk-adjusted return. Consider whether the "
                "edge is real.",
                f"Sharpe {sharpe:.2f} sous 0.5 — rendement "
                "ajuste au risque faible. L'edge est-il "
                "reel ?",
            )
        return self._advisory(
            "good",
            f"Profit +{profit_pct:.1f}%, DD {dd_pct:.1f}%, "
            f"Sharpe {sharpe:.2f} — solid in-sample metrics. "
            "Validate with walk-forward before trusting.",
            f"Profit +{profit_pct:.1f}%, DD {dd_pct:.1f}%, "
            f"Sharpe {sharpe:.2f} — metriques in-sample "
            "solides. Validez en walk-forward.",
        )

    def _section_best_params(self) -> str:
        best_params = self.d.get("best_params", {})
        if not best_params:
            return ""

        ps = self.d.get("param_stats", {})
        L = self._L

        rows = ""
        for space, params in sorted(best_params.items()):
            if not isinstance(params, dict):
                rows += (
                    f"<tr><td>{self._esc(space)}</td>"
                    f"<td>—</td>"
                    f"<td><code>"
                    f"{self._esc(str(params))}</code></td>" + "<td></td>" * 7 + "</tr>\n"
                )
                continue
            for k, v in sorted(params.items()):
                st = ps.get(k, {})
                med3 = st.get("median_top3", "")
                med5 = st.get("median_top5", "")
                med10 = st.get("median", "")
                maj5 = st.get("majority_top5", "")
                maj10 = st.get("majority_top10", "")
                mean5 = st.get("mean_top5", "")
                mean10 = st.get("mean", "")
                rec = st.get("recommended", "")

                def _cell(val: object) -> str:
                    if val == "" or val is None:
                        return '<td style="color:#555">—</td>'
                    return f"<td><code>{self._esc(str(val))}</code></td>"

                rec_c = "#00d4ff" if rec != "" else "#555"
                rec_cell = (
                    f'<td style="color:{rec_c};'
                    f'font-weight:bold">'
                    f"<code>{self._esc(str(rec))}</code>"
                    f"</td>"
                    if rec != ""
                    else '<td style="color:#555">—</td>'
                )

                rows += (
                    f"<tr><td>{self._esc(space)}</td>"
                    f"<td>{self._esc(k)}</td>"
                    f"<td><code><strong>"
                    f"{self._esc(str(v))}"
                    f"</strong></code></td>"
                    + _cell(med3)
                    + _cell(med5)
                    + _cell(med10)
                    + _cell(maj5)
                    + _cell(maj10)
                    + _cell(mean5)
                    + _cell(mean10)
                    + rec_cell
                    + "</tr>\n"
                )

        hdr = (
            "<tr>"
            f"<th>{L('Space', 'Espace')}</th>"
            f"<th>{L('Param', 'Param')}</th>"
            f"<th>{L('Best', 'Best')}</th>"
            f"<th>{L('Med. T3', 'Méd. T3')}</th>"
            f"<th>{L('Med. T5', 'Méd. T5')}</th>"
            f"<th>{L('Med. T10', 'Méd. T10')}</th>"
            f"<th>{L('Maj. T5', 'Maj. T5')}</th>"
            f"<th>{L('Maj. T10', 'Maj. T10')}</th>"
            f"<th>{L('Mean T5', 'Moy. T5')}</th>"
            f"<th>{L('Mean T10', 'Moy. T10')}</th>"
            f"<th style='color:#00d4ff'>"
            f"{L('Recommended', 'Recommandé')}</th>"
            "</tr>"
        )

        return (
            '<div class="section"><h2>'
            + L("Best Parameters", "Meilleurs paramètres")
            + "</h2>"
            + self._desc(
                "Optimized values compared across top epochs. "
                "The recommended value is the median of the "
                "top-5 — more robust than the single best.",
                "Valeurs optimisées comparées entre les top "
                "epochs. La valeur recommandée est la médiane "
                "du top-5 — plus robuste que le seul meilleur.",
            )
            + '<div style="overflow-x:auto">'
            f'<table style="font-size:0.82em">'
            f"{hdr}{rows}</table></div></div>"
        )

    def _section_top10_table(self) -> str:
        top_epochs = self.d.get("top_epochs", [])
        if not top_epochs:
            return ""
        param_stats = self.d.get("param_stats", {})
        strategy = self._esc(self.d.get("strategy", "Strategy"))
        L = self._L

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
            rank_cls = ' class="best-rank"' if rank == 1 else ""
            rows += (
                f"<tr><td{rank_cls}>{rank}</td>"
                f"<td><code>{self._fmt(loss, 4)}</code></td>"
                f"<td>{trades}</td>"
                f'<td class="{p_cls}">'
                f"{profit_pct:+.1f}%</td>"
                f"<td>{self._fmt(dd_pct, 1)}%</td>"
                f"<td>{self._fmt(calmar)}</td>"
                f"<td>{self._fmt(sharpe)}</td>"
                f"<td>{self._fmt(pf)}</td></tr>\n"
            )
            pd = epoch.get("params_dict", {})
            if pd:
                rows += self._top10_detail_row(rank, pd, m, param_stats, strategy)

        return (
            '<div class="section"><h2>Top 10 Epochs</h2>'
            + self._desc(
                "The 10 best epochs ranked by loss. Consistent "
                "metrics across epochs indicate a robust result. "
                "Expand each row for full parameters and JSON "
                "export.",
                "Les 10 meilleurs epochs classés par loss. Des "
                "métriques cohérentes entre epochs indiquent un "
                "résultat robuste. Dépliez chaque ligne pour les "
                "paramètres et l'export JSON.",
            )
            + "<table>"
            f"<tr><th>{L('Rank', 'Rang')}</th>"
            f"<th>{L('Loss', 'Perte')}</th>"
            f"<th>Trades</th><th>Profit%</th>"
            f"<th>{self._tip('dd', 'Max DD%')}</th>"
            f"<th>{self._tip('calmar', 'Calmar')}</th>"
            f"<th>Sharpe</th>"
            f"<th>{self._tip('pf', 'PF')}</th></tr>"
            f"{rows}"
            "</table></div>"
        )

    def _top10_detail_row(
        self,
        rank: int,
        pd: dict,
        metrics: dict,
        param_stats: dict,
        strategy: str,
    ) -> str:
        import json as _json

        L = self._L
        # Param table with median/mean comparison
        param_rows = ""
        for k in sorted(pd.keys()):
            v = pd[k]
            v_esc = self._esc(str(v))
            stat = param_stats.get(k, {})
            med = stat.get("median")
            mean = stat.get("mean")
            comp = ""
            if med is not None and isinstance(v, (int, float)):
                delta_med = v - med
                sign = "+" if delta_med >= 0 else ""
                c = "#22c55e" if abs(delta_med) < abs(med) * 0.1 else "#eab308"
                comp = (
                    f'<span style="color:{c};'
                    f'font-size:0.85em"> '
                    f"(med: {med}, mean: {mean}, "
                    f"Δ: {sign}{delta_med:.4g})"
                    f"</span>"
                )
            param_rows += (
                f"<tr><td><code>{self._esc(k)}</code></td>"
                f"<td><code><strong>{v_esc}</strong></code>"
                f"{comp}</td></tr>"
            )

        # Extra metrics
        wr = metrics.get("winrate", 0)
        sortino = metrics.get("sortino", 0)
        trades = metrics.get("total_trades", 0)
        extra = (
            '<div style="margin:6px 0">'
            f'<span class="kv" style="margin-right:14px">'
            f'<span class="kv-label">'
            f"{L('Win rate', 'Taux de gain')}"
            f'</span><br><span class="kv-value">'
            f"{wr:.1%}</span></span>"
            f'<span class="kv" style="margin-right:14px">'
            f'<span class="kv-label">Sortino</span><br>'
            f'<span class="kv-value">'
            f"{self._fmt(sortino)}</span></span>"
            f'<span class="kv" style="margin-right:14px">'
            f'<span class="kv-label">Trades</span><br>'
            f'<span class="kv-value">{trades}</span></span>'
            "</div>"
        )

        # JSON for copy button
        json_str = _json.dumps(pd, indent=2)
        json_escaped = html.escape(json_str, quote=True)
        copy_btn = (
            f'<button onclick="navigator.clipboard.writeText('
            f"'{html.escape(_json.dumps(pd), quote=True)}'"
            f").then(function(){{this.textContent="
            f"'Copied!'}}.bind(this))\""
            f' style="background:#16213e;color:#00d4ff;'
            f"border:1px solid #2a2a4a;border-radius:4px;"
            f"padding:4px 10px;cursor:pointer;"
            f'font-size:0.8em;margin:4px 0">' + L("Copy JSON", "Copier JSON") + "</button>"
        )

        lbl_params = L("Parameters", "Paramètres")
        lbl_json = L("JSON for strategy file", "JSON pour le fichier stratégie")

        return (
            f'<tr><td colspan="8"><details>'
            f'<summary style="font-size:0.82em">'
            f"#{rank} — {lbl_params}"
            f"</summary>"
            f'<div style="padding:6px 0">'
            f"{extra}"
            f'<table style="width:auto;font-size:0.85em">'
            f"<tr><th>{L('Param', 'Param')}</th>"
            f"<th>{L('Value (vs top-10)', 'Valeur (vs top-10)')}"
            f"</th></tr>"
            f"{param_rows}"
            f"</table>"
            f"<details style='margin:6px 0'>"
            f"<summary style='font-size:0.8em'>"
            f"{lbl_json}</summary>"
            f'<pre style="background:#0f0f23;padding:8px;'
            f"border-radius:4px;font-size:0.8em;"
            f'overflow-x:auto;margin:4px 0">'
            f"{json_escaped}</pre>"
            f"{copy_btn}"
            f"</details>"
            f"</div></details></td></tr>\n"
        )

    def _section_convergence_chart(self) -> str:
        all_losses = self.d.get("all_losses") or []
        if len(all_losses) < 2:
            return ""
        dd_data = self.d.get("epoch_dd_data") or []
        L = self._L

        # Build loss+sampler explanation
        sampler = self.d.get("sampler") or "TPESampler"
        loss_name = self.d.get("hyperopt_loss", "")
        loss_entry = LOSS_GLOSSARY.get(loss_name, {})
        loss_one = loss_entry.get("one_liner", "")

        combo_en = f"<strong>{sampler}</strong> explores the parameter space"
        combo_fr = f"<strong>{sampler}</strong> explore l'espace des paramètres"
        if "TPE" in sampler:
            combo_en += (
                " by building a probabilistic model — "
                "it focuses on promising regions, so the "
                "green line should drop quickly then "
                "flatten."
            )
            combo_fr += (
                " en construisant un modèle probabiliste "
                "— il se concentre sur les régions "
                "prometteuses, donc la ligne verte devrait "
                "baisser vite puis se stabiliser."
            )
        elif "NSGA" in sampler:
            combo_en += (
                " using multi-objective evolution — "
                "convergence may be slower but covers "
                "more trade-offs. Expect a gradual "
                "descent."
            )
            combo_fr += (
                " par évolution multi-objectif — la "
                "convergence peut être plus lente mais "
                "couvre plus de compromis. Attendez-vous "
                "à une descente graduelle."
            )
        elif "Random" in sampler or "random" in sampler:
            combo_en += (
                " randomly — the green line may be "
                "erratic. Random search needs many epochs "
                "to find good regions."
            )
            combo_fr += (
                " aléatoirement — la ligne verte peut "
                "être erratique. La recherche aléatoire "
                "nécessite beaucoup d'epochs."
            )
        else:
            combo_en += "."
            combo_fr += "."

        if loss_one:
            combo_en += f" The loss function <code>{loss_name}</code> optimizes for: {loss_one}."
            combo_fr += f" La fonction de loss <code>{loss_name}</code> optimise : {loss_one}."

        combo_box = (
            '<div class="mini-section" style="font-size:0.85em">' + L(combo_en, combo_fr) + "</div>"
        )

        guide = (
            '<details style="margin:6px 0">'
            "<summary style='font-size:0.82em'>" + L("How to read", "Comment lire") + "</summary>"
            '<div style="font-size:0.82em;color:#bbb;'
            'padding:4px 0 4px 16px">'
            "<ul style='margin:4px 0;padding-left:16px'>"
            "<li>"
            + L(
                "Each dot = one epoch's loss value",
                "Chaque point = la loss d'un epoch",
            )
            + "</li><li>"
            + L(
                "Green line = best loss found so far (should decrease and flatten)",
                "Ligne verte = meilleure loss trouvée jusque-là (doit baisser et se stabiliser)",
            )
            + "</li><li>"
            + L(
                "Cyan dot = the overall best epoch",
                "Point cyan = le meilleur epoch global",
            )
            + "</li><li><strong>"
            + L(
                "Flat curve at the end",
                "Courbe plate à la fin",
            )
            + "</strong>: "
            + L(
                "the optimizer converged — good",
                "l'optimiseur a convergé — bien",
            )
            + "</li><li><strong>"
            + L("Still dropping", "Encore en baisse")
            + "</strong>: "
            + L(
                "may need more epochs",
                "peut-être besoin de plus d'epochs",
            )
            + "</li></ul></div></details>"
        )
        return (
            '<div class="section" id="sec-convergence"><h2>'
            + L("Convergence Chart", "Graphique de convergence")
            + "</h2>"
            + self._desc(
                "How the optimizer's best loss evolved over "
                "epochs. A curve that flattens means the "
                "optimizer found its optimum.",
                "Évolution de la meilleure loss au fil des "
                "epochs. Une courbe qui se stabilise signifie "
                "que l'optimiseur a trouvé son optimum.",
            )
            + combo_box
            + self._svg_convergence(all_losses, dd_data)
            + guide
            + self._advisory_convergence(all_losses)
            + "</div>"
        )

    def _advisory_convergence(self, all_losses: list[float]) -> str:
        finite = [v for v in all_losses if v == v and abs(v) < 1e15]
        if len(finite) < 10:
            return ""
        n = len(finite)
        cutoff = int(n * 0.7)
        first_70 = finite[:cutoff]
        last_30 = finite[cutoff:]
        still_improving = min(last_30) < min(first_70)
        if still_improving:
            return self._advisory(
                "warn",
                "Loss is still improving in the last 30% "
                "of epochs — the optimizer has not fully "
                "converged. Consider running more epochs.",
                "La loss continue a s'ameliorer dans les "
                "derniers 30% des epochs — l'optimiseur "
                "n'a pas converge. Lancez plus d'epochs.",
            )
        return self._advisory(
            "good",
            "Loss plateau reached — the optimizer has "
            "converged. More epochs are unlikely to "
            "improve results significantly.",
            "Plateau de loss atteint — l'optimiseur a "
            "converge. Plus d'epochs n'amelioreront "
            "probablement pas les resultats.",
        )

    @staticmethod
    def _dd_to_color(dd: float) -> str:
        if dd < 0.15:
            return "#22c55e"
        if dd < 0.30:
            return "#eab308"
        return "#ef4444"

    @staticmethod
    def _conv_grid(
        pad_l: int,
        pad_r: int,
        pad_t: int,
        pad_b: int,
        w: int,
        h: int,
        y_min: float,
        y_max: float,
        sy: Any,
    ) -> str:
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
                f'text-anchor="end" fill="#888" '
                f'font-size="10">{val:.4f}</text>\n'
            )
        return grid

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

        # X-axis epoch tick labels
        x_ticks = ""
        n_xticks = min(n, 6)
        for j in range(n_xticks):
            idx = int(j * (n - 1) / max(n_xticks - 1, 1))
            xx = sx(idx)
            x_ticks += (
                f'<text x="{xx:.1f}" y="{h - pad_b + 14}" '
                f'fill="#888" font-size="9" '
                f'text-anchor="middle">{idx + 1}</text>\n'
            )

        # Best epoch marker
        best_idx = all_losses.index(min(finite))
        best_val = all_losses[best_idx]
        bx = sx(best_idx)
        by = sy(best_val)
        best_dot = (
            f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="6" '
            f'fill="#00d4ff" stroke="#fff" stroke-width="2"/>\n'
            f'<text x="{bx:.1f}" y="{by - 10:.1f}" '
            f'fill="#00d4ff" font-size="9" '
            f'text-anchor="middle" font-weight="bold">'
            f"#{best_idx + 1}: {best_val:.4f}</text>\n"
        )

        grid = HyperoptHTMLReport._conv_grid(
            pad_l,
            pad_r,
            pad_t,
            pad_b,
            w,
            h,
            y_min,
            y_max,
            sy,
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
            f"{grid}{y_label}{x_label}{x_ticks}{legend}"
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

        L = self._L
        explainer_en = (
            "Parameters that stay consistent across the top-10 epochs are 'stable' "
            "(std/range &lt; 15%) — the optimizer found a real pattern. "
            "'Unstable' params (std/range &gt; 30%) vary wildly, suggesting the "
            "optimizer is fitting noise. Consider freezing unstable params at "
            "sensible defaults."
        )
        explainer_fr = (
            "Les paramètres cohérents dans le top-10 sont 'stables' "
            "(éc.-type/étendue &lt; 15%) — l'optimiseur a trouvé un vrai pattern. "
            "Les paramètres 'instables' (éc.-type/étendue &gt; 30%) varient beaucoup, "
            "signe que l'optimiseur fit du bruit. Figez les paramètres instables "
            "à des valeurs par défaut raisonnables."
        )
        explainer = (
            "<details><summary>"
            + L("How to read this section", "Comment lire cette section")
            + "</summary><div>"
            + L(explainer_en, explainer_fr)
            + "</div></details>"
        )
        return (
            '<div class="section" id="sec-param-stability"><h2>'
            + L("Top-10 Parameter Agreement", "Accord des paramètres Top-10")
            + "</h2>"
            f"{explainer}"
            "<table>"
            f"<tr><th>{L('Parameter', 'Paramètre')}</th>"
            f"<th>{L('Mean', 'Moyenne')}</th>"
            f"<th>{L('Std', 'Éc.-type')}</th>"
            f"<th>{L('Std/Range', 'Éc.-type/Étendue')}</th>"
            f"<th>{L('Status', 'Statut')}</th>"
            f"<th>{L('Values', 'Valeurs')}</th></tr>"
            f"{rows}"
            "</table>" + self._advisory_agreement(all_params) + "</div>"
        )

    def _advisory_agreement(self, all_params: dict[str, list]) -> str:
        n_stable = 0
        n_total = 0
        for values in all_params.values():
            nums = [v for v in values if isinstance(v, (int, float))]
            n_total += 1
            if len(nums) >= 2:
                rng = max(nums) - min(nums)
                mean = sum(nums) / len(nums)
                var = sum((x - mean) ** 2 for x in nums) / len(nums)
                sor = (var**0.5 / rng) if rng > 0 else 0
                if sor < 0.15:
                    n_stable += 1
            else:
                uniq = set(str(v) for v in values)
                if len(uniq) == 1:
                    n_stable += 1
        if n_total == 0:
            return ""
        ratio = n_stable / n_total
        if ratio >= 0.7:
            return self._advisory(
                "good",
                f"{n_stable}/{n_total} parameters are stable — strong agreement across top-10.",
                f"{n_stable}/{n_total} parametres stables — forte convergence dans le top-10.",
            )
        if ratio >= 0.4:
            return self._advisory(
                "warn",
                f"Only {n_stable}/{n_total} parameters are "
                "stable. Freeze unstable ones at sensible "
                "defaults to reduce overfitting.",
                f"Seulement {n_stable}/{n_total} parametres "
                "stables. Figez les instables a des valeurs "
                "par defaut raisonnables.",
            )
        return self._advisory(
            "bad",
            f"Only {n_stable}/{n_total} parameters are "
            "stable — the optimizer is mostly fitting "
            "noise. Consider reducing parameter count.",
            f"Seulement {n_stable}/{n_total} parametres "
            "stables — l'optimiseur fit principalement "
            "du bruit. Reduisez le nombre de parametres.",
        )

    # ------------------------------------------------------------------
    # New metric sections (A3, A4, B1-B3, C1, C3, C4)
    # ------------------------------------------------------------------

    def _section_sans_top_trade(self) -> str:
        st = self.d.get("sans_top_trade")
        if not st:
            return ""
        sc = self._esc(self.d.get("stake_currency", "USDC"))
        L = self._L
        fragile_badge = ""
        if st["fragile"]:
            fragile_badge = (
                ' <span style="color:#ef4444;font-weight:bold">'
                + L(
                    "FRAGILE — luck, not edge",
                    "FRAGILE — chance, pas un edge",
                )
                + "</span>"
            )
        tip_en = self._tip("profit_concentration", "Concentration Risk")
        tip_fr = self._tip("profit_concentration", "Risque de concentration")

        total = st["total_profit"]
        w1 = st["without_top1"]
        w2 = st["without_top2"]
        w1_pct = st["without_top1_pct"]
        w2_pct = st["without_top2_pct"]

        cls_w1 = "pos" if w1 >= 0 else "neg"
        cls_w2 = "pos" if w2 >= 0 else "neg"

        return (
            '<div class="section" id="sec-concentration">'
            f"<h2>{L(tip_en, tip_fr)}"
            f" — {L('Sans Top Trade Test', 'Test sans meilleur trade')}</h2>"
            + self._desc(
                "Tests whether profitability survives removing the "
                "best trades — reveals if profit depends on a few "
                "lucky hits.",
                "Vérifie si la rentabilité survit sans les meilleurs "
                "trades — révèle si le profit dépend de quelques "
                "coups de chance.",
            )
            + self._svg_concentration_gauge(st, sc)
            + '<table style="width:auto;margin:8px 0">'
            f"<tr><th></th><th>{L('Profit', 'Profit')}</th>"
            f"<th>{L('% of total', '% du total')}</th></tr>"
            f"<tr><td>{L('Total', 'Total')}</td>"
            f"<td><strong>{self._fmt(total)} {sc}</strong></td>"
            f"<td>100%</td></tr>"
            f"<tr><td>{L('Without best trade', 'Sans meilleur trade')}</td>"
            f'<td class="{cls_w1}">{self._fmt(w1)} {sc}</td>'
            f'<td class="{cls_w1}">{w1_pct:.1f}%</td></tr>'
            f"<tr><td>{L('Without top 2', 'Sans top 2')}</td>"
            f'<td class="{cls_w2}">{self._fmt(w2)} {sc}</td>'
            f'<td class="{cls_w2}">{w2_pct:.1f}%</td></tr>'
            f"</table>"
            f"{fragile_badge}" + self._advisory_sans_top(st) + "</div>"
        )

    def _advisory_sans_top(self, st: dict) -> str:
        if st["fragile"]:
            return self._advisory(
                "bad",
                "Profit depends on 1-2 lucky trades. This is not a reliable edge.",
                "Le profit depend de 1-2 trades chanceux. Ce n'est pas un edge fiable.",
            )
        w2_pct = st["without_top2_pct"]
        if w2_pct >= 70:
            return self._advisory(
                "good",
                f"{w2_pct:.0f}% of profit survives removing top 2 trades — well-distributed edge.",
                f"{w2_pct:.0f}% du profit survit sans les "
                "2 meilleurs trades — edge bien distribue.",
            )
        return self._advisory(
            "warn",
            f"Only {w2_pct:.0f}% of profit survives "
            "without top 2 trades. Monitor concentration "
            "risk in live trading.",
            f"Seulement {w2_pct:.0f}% du profit survit "
            "sans les 2 meilleurs trades. Surveillez le "
            "risque de concentration en live.",
        )

    @staticmethod
    def _svg_concentration_gauge(st: dict, sc: str) -> str:
        total = st["total_profit"]
        w1 = st["without_top1"]
        w2 = st["without_top2"]
        if abs(total) < 0.001:
            return ""
        w, h = 600, 110
        pad_l, pad_r = 110, 130
        bar_w = w - pad_l - pad_r
        bar_h = 18
        sc_esc = html.escape(sc)

        def _bar(
            y: int,
            val: float,
            label: str,
            c: str,
        ) -> str:
            ratio = max(0, val / total) if total > 0 else 0
            bw = max(2, ratio * bar_w)
            pct = val / total * 100 if total else 0
            return (
                f'<text x="{pad_l - 10}" y="{y + 13}" '
                f'fill="#bbb" font-size="11" '
                f'text-anchor="end" font-weight="bold">'
                f"{label}</text>"
                f'<rect x="{pad_l}" y="{y}" '
                f'width="{bw:.0f}" height="{bar_h}" '
                f'fill="{c}" rx="4" opacity="0.75">'
                f"<title>{val:.2f} {sc_esc} "
                f"({pct:.0f}%)</title></rect>"
                f'<text x="{pad_l + bw + 8:.0f}" '
                f'y="{y + 13}" fill="{c}" '
                f'font-size="11" font-weight="bold">'
                f"{val:+.2f} {sc_esc} "
                f'<tspan fill="#888">({pct:.0f}%)'
                f"</tspan></text>"
            )

        c_total = "#22c55e" if total >= 0 else "#ef4444"
        c_w1 = "#84cc16" if w1 >= 0 else "#ef4444"
        c_w2 = "#22c55e" if w2 >= 0 else "#ef4444"
        if w2 < 0:
            c_w2 = "#ef4444"
        elif w2 < total * 0.3:
            c_w2 = "#eab308"

        bars = (
            _bar(12, total, "Total", c_total)
            + _bar(40, w1, "- Top 1", c_w1)
            + _bar(68, w2, "- Top 1+2", c_w2)
        )
        bars += f'<line x1="{pad_l}" y1="8" x2="{pad_l}" y2="92" stroke="#555" stroke-width="1"/>'
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:#0f0f23;border-radius:6px;'
            f'margin:8px 0">{bars}</svg>'
        )

    def _section_pair_distribution(self) -> str:
        pairs = self.d.get("pair_profit_distribution", [])
        if not pairs:
            return ""
        L = self._L
        total_pairs = self.d.get("total_pairs", 0)
        n_traded = len(pairs)
        n_profitable = sum(1 for p in pairs if p.get("profit_abs", 0) > 0)
        summary = ""
        if total_pairs > 0:
            summary = (
                '<div class="mini-section">'
                + L(
                    f"<strong>{n_profitable}</strong> profitable"
                    f" / <strong>{n_traded}</strong> traded"
                    f" / <strong>{total_pairs}</strong> in"
                    f" pairlist",
                    f"<strong>{n_profitable}</strong> rentables"
                    f" / <strong>{n_traded}</strong> tradées"
                    f" / <strong>{total_pairs}</strong> dans"
                    f" la pairlist",
                )
                + "</div>"
            )
        elif n_traded > 0:
            summary = (
                '<div class="mini-section">'
                + L(
                    f"<strong>{n_profitable}</strong> profitable"
                    f" / <strong>{n_traded}</strong> pairs "
                    f"traded",
                    f"<strong>{n_profitable}</strong> rentables"
                    f" / <strong>{n_traded}</strong> paires "
                    f"tradées",
                )
                + "</div>"
            )
        return (
            '<div class="section" id="sec-pair-dist"><h2>'
            + L("Profit by Pair", "Profit par paire")
            + "</h2>"
            + self._desc(
                "Profit contribution by trading pair. Check if "
                "performance is driven by one or two pairs only.",
                "Contribution au profit par paire. Vérifiez si la "
                "performance repose sur une ou deux paires "
                "seulement.",
            )
            + summary
            + self._svg_pair_bars(pairs)
            + self._advisory_pair_dist(pairs, n_profitable)
            + "</div>"
        )

    def _advisory_pair_dist(self, pairs: list, n_profitable: int) -> str:
        n = len(pairs)
        if n == 0:
            return ""
        total_abs = sum(abs(p.get("profit_abs", 0)) for p in pairs)
        if total_abs < 1e-9:
            return ""
        top_share = abs(pairs[0].get("profit_abs", 0)) / total_abs
        if n_profitable <= 1 and n > 3:
            return self._advisory(
                "bad",
                "Only 1 profitable pair — the strategy may only work on a single instrument.",
                "Une seule paire rentable — la strategie "
                "ne fonctionne peut-etre que sur un seul "
                "instrument.",
            )
        if top_share > 0.5:
            return self._advisory(
                "warn",
                f"Top pair contributes {top_share:.0%} of "
                "total profit. High pair concentration "
                "increases fragility.",
                f"La paire principale contribue "
                f"{top_share:.0%} du profit total. "
                "Forte concentration = fragilite accrue.",
            )
        return self._advisory(
            "good",
            f"{n_profitable}/{n} pairs profitable — well-diversified across instruments.",
            f"{n_profitable}/{n} paires rentables — bonne diversification.",
        )

    @staticmethod
    def _svg_pair_bars(pairs: list[dict]) -> str:
        n = len(pairs)
        row_h = 28
        pad_l, pad_r, pad_t, pad_b = 140, 180, 10, 10
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
            count = p.get("trade_count", 0)
            wr = p.get("win_rate", 0)
            avg = p.get("avg_profit", 0)
            label = f"{pair_name} ({count}t)"
            lines.append(
                f'<text x="{pad_l - 8}" y="{y + 4}" '
                f'text-anchor="end" fill="#bbb" '
                f'font-size="10">{label}</text>'
            )
            lines.append(
                f'<rect x="{pad_l}" y="{y - 8}" '
                f'width="{bw:.1f}" height="16" '
                f'fill="{color}" rx="2"/>'
            )
            detail = f"{val:+.2f}"
            if count:
                detail += f"  WR:{wr:.0%}  avg:{avg:+.4f}"
            lines.append(
                f'<text x="{pad_l + bw + 6:.1f}" y="{y + 4}" '
                f'fill="{color}" font-size="9">'
                f"{detail}</text>"
            )
        body = "\n".join(lines)
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{body}</svg>'

    def _section_best_vs_median(self) -> str:
        bvm = self.d.get("best_vs_median_gap")
        if not bvm:
            return ""
        L = self._L
        best_p = bvm["best_profit"]
        med_p = bvm["median_profit"]
        gap = bvm["gap_ratio"]
        if gap <= 1.5:
            verdict_c = "#22c55e"
            verdict_en = "Consistent — best epoch close to median"
            verdict_fr = "Cohérent — meilleur epoch proche de la médiane"
        elif gap <= 2.0:
            verdict_c = "#eab308"
            verdict_en = "Moderate gap — best may be slightly lucky"
            verdict_fr = "Écart modéré — le meilleur est peut-être chanceux"
        else:
            verdict_c = "#ef4444"
            verdict_en = "Outlier — best epoch far from others, likely luck"
            verdict_fr = "Outlier — meilleur epoch loin des autres, probablement chanceux"

        svg = self._svg_best_vs_median(best_p, med_p, gap)

        return (
            '<div class="section" id="sec-best-vs-median"><h2>'
            + L(
                "Best vs. Median Top-10",
                "Meilleur vs. Médiane Top-10",
            )
            + "</h2>"
            + self._desc(
                "Compares the best epoch to the median "
                "of the top-10. A large gap (>2x) suggests "
                "the best epoch is an outlier — luck, not skill.",
                "Compare le meilleur epoch à la médiane du "
                "top-10. Un écart important (>2x) suggère "
                "que le meilleur est un outlier — chance, "
                "pas compétence.",
            )
            + svg
            + f'<div class="mini-section" '
            f'style="border-left:3px solid {verdict_c}">'
            f'<strong style="color:{verdict_c}">'
            f"{L('Gap', 'Écart')}: {gap:.2f}x"
            f"</strong> — "
            f"{L(verdict_en, verdict_fr)}"
            "</div>" + self._advisory_bvm(gap) + "</div>"
        )

    def _advisory_bvm(self, gap: float) -> str:
        if gap > 2.0:
            return self._advisory(
                "bad",
                f"Gap {gap:.1f}x — the best epoch is a "
                "statistical outlier. Use the median "
                "top-10 result for realistic expectations.",
                f"Ecart {gap:.1f}x — le meilleur epoch est "
                "un outlier statistique. Utilisez la mediane "
                "du top-10 pour des attentes realistes.",
            )
        if gap > 1.5:
            return self._advisory(
                "warn",
                f"Gap {gap:.1f}x — moderate divergence "
                "between best and median. Check if the "
                "median result is still acceptable.",
                f"Ecart {gap:.1f}x — divergence moderee. "
                "Verifiez si le resultat median reste "
                "acceptable.",
            )
        return self._advisory(
            "good",
            f"Gap {gap:.1f}x — best epoch is close to median, suggesting reproducible results.",
            f"Ecart {gap:.1f}x — le meilleur epoch est "
            "proche de la mediane, resultats "
            "reproductibles.",
        )

    @staticmethod
    def _svg_best_vs_median(
        best: float,
        median: float,
        gap: float,
    ) -> str:
        w, h = 600, 70
        pad_l, pad_r = 120, 80
        bar_w = w - pad_l - pad_r
        max_val = max(abs(best), abs(median), 0.01)

        def _row(
            y: int,
            val: float,
            label: str,
            c: str,
        ) -> str:
            bw = max(2, abs(val) / max_val * bar_w)
            return (
                f'<text x="{pad_l - 10}" y="{y + 13}" '
                f'fill="#bbb" font-size="11" '
                f'text-anchor="end" font-weight="bold">'
                f"{label}</text>"
                f'<rect x="{pad_l}" y="{y}" '
                f'width="{bw:.0f}" height="18" '
                f'fill="{c}" rx="4" opacity="0.75"/>'
                f'<text x="{pad_l + bw + 8:.0f}" '
                f'y="{y + 13}" fill="{c}" '
                f'font-size="11" font-weight="bold">'
                f"{val:+.2f}%</text>"
            )

        c_best = "#00d4ff"
        c_med = "#888"
        body = _row(10, best, "Best", c_best) + _row(38, median, "Median Top-10", c_med)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:#0f0f23;border-radius:6px;'
            f'margin:8px 0">{body}</svg>'
        )

    def _section_dispersion_bands(self) -> str:
        bands = self.d.get("dispersion_bands") or {}
        if not bands:
            return ""
        L = self._L
        labels = {
            "profit": ("Profit %", "#22c55e"),
            "drawdown": ("Max DD %", "#ef4444"),
            "sharpe": ("Sharpe", "#00d4ff"),
        }
        band_svgs = []
        for key, (label, color) in labels.items():
            b = bands.get(key)
            if not b:
                continue
            band_svgs.append(self._svg_band(label, b, color))

        guide = (
            '<details style="margin:6px 0">'
            "<summary style='font-size:0.82em'>" + L("How to read", "Comment lire") + "</summary>"
            '<div style="font-size:0.82em;color:#bbb;'
            'padding:4px 0 4px 16px">'
            "<ul style='margin:4px 0;padding-left:16px'>"
            "<li>"
            + L(
                "Each bar shows the spread (min to max) of a metric across the top-10 epochs",
                "Chaque barre montre l'étendue (min à max) d'une métrique dans le top-10",
            )
            + "</li>"
            "<li>"
            + L(
                "Cyan marker = median value",
                "Marqueur cyan = valeur médiane",
            )
            + "</li>"
            "<li><strong>"
            + L("Narrow band", "Bande étroite")
            + "</strong>: "
            + L(
                "metrics are consistent across top epochs — robust optimization",
                "métriques cohérentes entre les top epochs — optimisation robuste",
            )
            + "</li>"
            "<li><strong>"
            + L("Wide band", "Bande large")
            + "</strong>: "
            + L(
                "high variance — results depend heavily on which epoch you pick",
                "forte variance — les résultats dépendent beaucoup de l'epoch choisi",
            )
            + "</li></ul></div></details>"
        )

        return (
            '<div class="section" id="sec-dispersion"><h2>'
            + L("Top-10 Dispersion", "Dispersion Top-10")
            + "</h2>"
            + self._desc(
                "How much key metrics vary across the top-10 "
                "epochs. Narrow bands = the optimizer "
                "consistently found similar results.",
                "Variation des métriques clés dans le top-10. "
                "Bandes étroites = l'optimiseur a trouvé des "
                "résultats similaires de façon constante.",
            )
            + "".join(band_svgs)
            + guide
            + self._advisory_dispersion(bands)
            + "</div>"
        )

    def _advisory_dispersion(self, bands: dict) -> str:
        pb = bands.get("profit")
        if not pb:
            return ""
        spread = pb["max"] - pb["min"]
        med = abs(pb["median"])
        med = med if med > 0.01 else 1.0
        ratio = spread / med
        if ratio > 0.6:
            return self._advisory(
                "bad",
                "Profit spread across top-10 is wide — "
                "results are highly sensitive to which "
                "epoch you pick.",
                "L'ecart de profit dans le top-10 est "
                "large — les resultats dependent fortement "
                "de l'epoch choisi.",
            )
        if ratio > 0.3:
            return self._advisory(
                "warn",
                "Moderate profit spread in top-10. The "
                "median result may differ noticeably from "
                "the best epoch.",
                "Ecart de profit modere dans le top-10. "
                "Le resultat median peut differer du "
                "meilleur epoch.",
            )
        return self._advisory(
            "good",
            "Narrow profit spread — top-10 epochs "
            "produce similar results, indicating "
            "stable optimization.",
            "Ecart de profit etroit — les top-10 "
            "epochs donnent des resultats similaires, "
            "signe d'une optimisation stable.",
        )

    @staticmethod
    def _svg_band(
        label: str,
        b: dict,
        accent: str,
    ) -> str:
        w, h = 600, 50
        pad_l = 100
        pad_r = 40
        track_w = w - pad_l - pad_r
        lo, med, hi = b["min"], b["median"], b["max"]
        span = hi - lo if hi > lo else 1.0
        x_med = pad_l + (med - lo) / span * track_w
        spread = hi - lo
        spread_c = (
            (
                "#22c55e"
                if spread < abs(med) * 0.3
                else "#eab308"
                if spread < abs(med) * 0.6
                else "#ef4444"
            )
            if abs(med) > 0.01
            else "#888"
        )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:transparent;margin:2px 0">'
            f'<text x="{pad_l - 10}" y="22" '
            f'text-anchor="end" fill="{accent}" '
            f'font-size="11" font-weight="bold">'
            f"{label}</text>"
            f'<rect x="{pad_l}" y="14" '
            f'width="{track_w}" height="14" '
            f'fill="#1a1a2e" rx="4" '
            f'stroke="#2a2a4a" stroke-width="1"/>'
            f'<rect x="{pad_l}" y="14" '
            f'width="{track_w}" height="14" '
            f'fill="{accent}" opacity="0.15" rx="4"/>'
            f'<line x1="{x_med:.1f}" y1="10" '
            f'x2="{x_med:.1f}" y2="32" '
            f'stroke="#00d4ff" stroke-width="3"/>'
            f'<circle cx="{x_med:.1f}" cy="21" r="4" '
            f'fill="#00d4ff"/>'
            f'<text x="{pad_l}" y="42" fill="#888" '
            f'font-size="9">min: {lo:.2f}</text>'
            f'<text x="{x_med:.1f}" y="42" fill="#00d4ff" '
            f'font-size="9" text-anchor="middle">'
            f"med: {med:.2f}</text>"
            f'<text x="{pad_l + track_w}" y="42" '
            f'fill="#888" font-size="9" '
            f'text-anchor="end">max: {hi:.2f}</text>'
            f'<text x="{pad_l + track_w + 4}" y="22" '
            f'fill="{spread_c}" font-size="9">'
            f"±{spread:.2f}</text>"
            "</svg>"
        )

    def _section_loss_histogram(self) -> str:
        hist = self.d.get("loss_histogram")
        if not hist or not hist.get("bins"):
            return ""
        L = self._L
        pct = hist.get("best_percentile", 0)
        pct_html = ""
        if pct > 0:
            c = "#22c55e" if pct > 80 else "#eab308" if pct > 50 else "#ef4444"
            pct_html = (
                f'<div class="mini-section" '
                f'style="border-left:3px solid {c}">'
                f'<strong style="color:{c}">'
                + L("Percentile", "Percentile")
                + f": {pct:.0f}%</strong> — "
                + L(
                    f"Best loss beats {pct:.0f}% of all epochs.",
                    f"La meilleure loss bat {pct:.0f}% de tous les epochs.",
                )
                + "</div>"
            )
        guide = (
            '<details style="margin:6px 0">'
            "<summary style='font-size:0.82em'>" + L("How to read", "Comment lire") + "</summary>"
            '<div style="font-size:0.82em;color:#bbb;'
            'padding:4px 0 4px 16px">'
            "<ul style='margin:4px 0;padding-left:16px'>"
            "<li>"
            + L(
                "Bars show how many epochs got each loss range",
                "Les barres montrent combien d'epochs ont chaque plage de loss",
            )
            + "</li>"
            "<li>"
            + L(
                "Dashed cyan line = your best loss",
                "Ligne cyan pointillée = votre meilleure loss",
            )
            + "</li>"
            "<li style='color:#22c55e'>"
            + L(
                "Green zone = top 25% — strong results",
                "Zone verte = top 25% — résultats solides",
            )
            + "</li>"
            "<li style='color:#eab308'>"
            + L(
                "Yellow zone = middle 50%",
                "Zone jaune = 50% médian",
            )
            + "</li>"
            "<li style='color:#ef4444'>"
            + L(
                "Red zone = bottom 25% — weak results",
                "Zone rouge = bottom 25% — résultats faibles",
            )
            + "</li>"
            "<li><strong>"
            + L("Goal", "Objectif")
            + "</strong>: "
            + L(
                "best loss deep in the green zone, most mass concentrated left",
                "meilleure loss dans la zone verte, masse concentrée à gauche",
            )
            + "</li>"
            "</ul></div></details>"
        )
        return (
            '<div class="section" id="sec-loss-hist"><h2>'
            + L("Loss Distribution", "Distribution des loss")
            + "</h2>"
            + self._desc(
                "Distribution of loss values across all epochs.",
                "Distribution des loss sur tous les epochs.",
            )
            + self._svg_loss_histogram(hist)
            + pct_html
            + guide
            + self._advisory_loss_hist(pct)
            + "</div>"
        )

    def _advisory_loss_hist(self, pct: float) -> str:
        if pct >= 90:
            return self._advisory(
                "good",
                f"Best loss in the top {100 - pct:.0f}% "
                "— the optimizer found an exceptional "
                "parameter set relative to all tries.",
                f"Meilleure loss dans le top "
                f"{100 - pct:.0f}% — l'optimiseur a "
                "trouve un jeu de parametres exceptionnel.",
            )
        if pct >= 50:
            return self._advisory(
                "warn",
                f"Best loss at percentile {pct:.0f}% — "
                "decent but many epochs achieved similar "
                "results. The loss surface may be flat.",
                f"Meilleure loss au percentile {pct:.0f}% "
                "— correct mais beaucoup d'epochs ont des "
                "resultats similaires.",
            )
        if pct > 0:
            return self._advisory(
                "bad",
                f"Best loss only at percentile {pct:.0f}% "
                "— the optimizer struggled to find good "
                "regions. Check if spaces are too large.",
                f"Meilleure loss seulement au percentile "
                f"{pct:.0f}% — l'optimiseur a eu du mal. "
                "Verifiez si les espaces sont trop larges.",
            )
        return ""

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
        plot_h = h - pad_t - pad_b

        elems = []
        # Background color zones (quartiles)
        q1 = n // 4
        q3 = n * 3 // 4
        zones = [
            (0, q1, "#22c55e08"),
            (q1, q3, "#eab30806"),
            (q3, n, "#ef444408"),
        ]
        for z_start, z_end, col in zones:
            zx = pad_l + z_start * bar_w
            zw = (z_end - z_start) * bar_w
            elems.append(
                f'<rect x="{zx:.0f}" y="{pad_t}" width="{zw:.0f}" height="{plot_h}" fill="{col}"/>'
            )

        # Y-axis tick labels
        n_yticks = 5
        for j in range(n_yticks + 1):
            val = int(max_count * j / n_yticks)
            yy = h - pad_b - val / max_count * plot_h
            elems.append(
                f'<text x="{pad_l - 6}" y="{yy + 3:.1f}" '
                f'fill="#888" font-size="9" '
                f'text-anchor="end">{val}</text>'
            )
            elems.append(
                f'<line x1="{pad_l}" y1="{yy:.1f}" '
                f'x2="{w - pad_r}" y2="{yy:.1f}" '
                f'stroke="#2a2a4a" stroke-width="0.5"/>'
            )

        for i, b in enumerate(bins):
            bh = b["count"] / max_count * plot_h
            x = pad_l + i * bar_w
            y = h - pad_b - bh
            if i <= q1:
                bar_c = "#22c55e"
            elif i >= q3:
                bar_c = "#ef4444"
            else:
                bar_c = "#eab308"
            elems.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" '
                f'width="{bar_w * 0.85:.1f}" '
                f'height="{bh:.1f}" fill="{bar_c}" '
                f'opacity="0.6" rx="2">'
                f"<title>Loss {b['lo']:.4f} - "
                f"{b['hi']:.4f}\n"
                f"Count: {b['count']}</title></rect>"
            )
            if b["count"] > 0 and bh > 14:
                elems.append(
                    f'<text x="{x + bar_w * 0.42:.1f}" '
                    f'y="{y + bh / 2 + 4:.1f}" '
                    f'fill="#fff" font-size="9" '
                    f'text-anchor="middle" opacity="0.8">'
                    f"{b['count']}</text>"
                )
            if i % max(1, n // 5) == 0:
                elems.append(
                    f'<text x="{x:.1f}" y="{h - 8}" fill="#888" font-size="9">{b["lo"]:.3f}</text>'
                )

        # Best loss marker
        if bins:
            lo_val = bins[0]["lo"]
            hi_val = bins[-1]["hi"]
            rng = hi_val - lo_val
            if rng > 0:
                bx = pad_l + (best_loss - lo_val) / rng * (w - pad_l - pad_r)
                elems.append(
                    f'<line x1="{bx:.1f}" y1="{pad_t}" '
                    f'x2="{bx:.1f}" y2="{h - pad_b}" '
                    f'stroke="#00d4ff" stroke-width="2" '
                    f'stroke-dasharray="4"/>'
                )
                elems.append(
                    f'<text x="{bx:.1f}" '
                    f'y="{pad_t - 4}" '
                    f'fill="#00d4ff" font-size="10" '
                    f'text-anchor="middle" '
                    f'font-weight="bold">'
                    f"best = {best_loss:.4f}</text>"
                )

        elems.append(
            f'<text x="{w // 2}" y="{h - 2}" fill="#888" '
            f'font-size="11" text-anchor="middle">Loss</text>'
        )
        elems.append(
            f'<text x="12" y="{h // 2}" fill="#888" '
            f'font-size="11" text-anchor="middle" '
            f'transform="rotate(-90 12 {h // 2})">'
            f"Count</text>"
        )

        body = "\n".join(elems)
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{body}</svg>'

    def _section_param_correlation(self) -> str:
        corr = self.d.get("param_correlation", [])
        if not corr:
            return ""
        params = sorted({c["param_a"] for c in corr} | {c["param_b"] for c in corr})
        if len(params) < 2:
            return ""
        L = self._L
        guide = (
            '<details style="margin:6px 0">'
            "<summary style='font-size:0.82em'>" + L("How to read", "Comment lire") + "</summary>"
            '<div style="font-size:0.82em;color:#bbb;'
            'padding:4px 0 4px 16px">'
            "<ul style='margin:4px 0;padding-left:16px'>"
            "<li style='color:#6060f0'>"
            + L(
                "Blue (negative) = when one goes up, the other goes down",
                "Bleu (négatif) = quand l un monte, l autre descend",
            )
            + "</li>"
            "<li style='color:#888'>"
            + L(
                "Gray (near 0) = independent parameters (ideal)",
                "Gris (proche de 0) = paramètres indépendants (idéal)",
            )
            + "</li>"
            "<li style='color:#f06060'>"
            + L(
                "Red (positive) = parameters move together (coupled)",
                "Rouge (positif) = paramètres bougent ensemble (couplés)",
            )
            + "</li>"
            "<li><strong>"
            + L("Goal", "Objectif")
            + "</strong>: "
            + L(
                "most cells gray — parameters should be "
                "independent. Strong colors (|r| > 0.7) "
                "suggest redundant dimensions.",
                "la majorité grise — les paramètres doivent "
                "être indépendants. Couleurs fortes "
                "(|r| > 0.7) = dimensions redondantes.",
            )
            + "</li>"
            "</ul></div></details>"
        )
        # Color legend bar
        legend = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'width="400" height="30" '
            'style="background:transparent;margin:4px 0">'
            "<defs>"
            '<linearGradient id="corr-leg" '
            'x1="0" y1="0" x2="1" y2="0">'
            '<stop offset="0%" stop-color="#3c3cf0"/>'
            '<stop offset="50%" stop-color="#555"/>'
            '<stop offset="100%" stop-color="#f03c3c"/>'
            "</linearGradient></defs>"
            '<rect x="50" y="4" width="300" height="12" '
            'fill="url(#corr-leg)" rx="3"/>'
            '<text x="50" y="26" fill="#6060f0" '
            'font-size="8">-1 (inverse)</text>'
            '<text x="200" y="26" fill="#888" '
            'font-size="8" text-anchor="middle">'
            "0 (independent)</text>"
            '<text x="350" y="26" fill="#f06060" '
            'font-size="8" text-anchor="end">'
            "+1 (coupled)</text>"
            "</svg>"
        )
        return (
            '<div class="section" id="sec-param-corr"><h2>'
            + L(
                "Parameter Correlation (Top-10)",
                "Corrélation des paramètres (Top-10)",
            )
            + "</h2>"
            + self._desc(
                "Pearson correlation between parameter values in the top-10.",
                "Corrélation de Pearson entre les paramètres du top-10.",
            )
            + self._svg_correlation_heatmap(corr, params)
            + legend
            + guide
            + self._advisory_param_corr(corr)
            + "</div>"
        )

    def _advisory_param_corr(self, corr: list) -> str:
        strong = [c for c in corr if abs(c.get("correlation", 0)) > 0.7]
        if strong:
            pairs = ", ".join(f"{c['param_a']}/{c['param_b']}" for c in strong[:3])
            return self._advisory(
                "warn",
                f"{len(strong)} strongly correlated pair(s) "
                f"(|r|>0.7): {pairs}. Consider merging or "
                "removing redundant parameters.",
                f"{len(strong)} paire(s) fortement "
                f"correlee(s) (|r|>0.7) : {pairs}. "
                "Fusionnez ou supprimez les parametres "
                "redondants.",
            )
        return self._advisory(
            "good",
            "No strong correlations between parameters — each parameter contributes independently.",
            "Pas de forte correlation entre parametres "
            "— chaque parametre contribue "
            "independamment.",
        )

    @staticmethod
    def _svg_correlation_heatmap(corr: list[dict], params: list[str]) -> str:
        n = len(params)
        cell = 50
        pad_l, pad_t = 110, 150
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
            lbl = html.escape(p[:16])
            # Column label: anchor at grid top edge, rotate -90
            cx = pad_l + i * cell + cell // 2
            elems.append(
                f'<text x="{cx}" y="{pad_t - 8}" fill="#bbb" '
                f'font-size="10" text-anchor="start" '
                f'transform="rotate(-90 {cx} {pad_t - 8})">'
                f"{lbl}</text>"
            )
            # Row label
            y = pad_t + i * cell + cell // 2 + 4
            elems.append(
                f'<text x="{pad_l - 8}" y="{y}" fill="#bbb" '
                f'font-size="10" text-anchor="end">{lbl}</text>'
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
        L = self._L
        guide = (
            '<details style="margin:6px 0">'
            "<summary style='font-size:0.82em'>" + L("How to read", "Comment lire") + "</summary>"
            '<div style="font-size:0.82em;color:#bbb;'
            'padding:4px 0 4px 16px">'
            "<ul style='margin:4px 0;padding-left:16px'>"
            "<li>"
            + L(
                "Each vertical axis = one parameter (0=min, 1=max of search range)",
                "Chaque axe vertical = un paramètre (0=min, 1=max du range)",
            )
            + "</li>"
            "<li>"
            + L(
                "Each line connects one epoch across all parameters",
                "Chaque ligne relie un epoch sur tous les paramètres",
            )
            + "</li>"
            "<li style='color:#22c55e'>"
            + L(
                "Green lines = low loss (good epochs)",
                "Lignes vertes = loss faible (bons epochs)",
            )
            + "</li>"
            "<li style='color:#ef4444'>"
            + L(
                "Red lines = high loss (bad epochs)",
                "Lignes rouges = loss élevée (mauvais epochs)",
            )
            + "</li>"
            "<li><strong>"
            + L("Good sign", "Bon signe")
            + "</strong>: "
            + L(
                "green lines cluster in a narrow band "
                "on each axis = optimizer found a "
                "consistent sweet spot",
                "les lignes vertes se groupent dans une "
                "bande étroite sur chaque axe = "
                "l optimiseur a trouvé un sweet spot",
            )
            + "</li>"
            "<li><strong>"
            + L("Bad sign", "Mauvais signe")
            + "</strong>: "
            + L(
                "green and red lines overlap everywhere "
                "= the parameter has little impact "
                "(consider freezing it)",
                "les lignes vertes et rouges se mélangent "
                "partout = le paramètre a peu d impact "
                "(envisagez de le figer)",
            )
            + "</li>"
            "</ul></div></details>"
        )
        return (
            '<div class="section" id="sec-parallel"><h2>'
            + L(
                "Parallel Coordinates (Top-10)",
                "Coordonnées parallèles (Top-10)",
            )
            + "</h2>"
            + self._desc(
                "Each line traces one epoch's parameter values "
                "across all dimensions. Color = loss value.",
                "Chaque ligne trace les paramètres d'un epoch. Couleur = valeur de loss.",
            )
            + self._svg_parallel_coords(pc)
            + guide
            + "</div>"
        )

    @staticmethod
    def _svg_parallel_coords(pc: dict) -> str:
        params = pc["params"]
        lines = pc["lines"]
        n_params = len(params)
        w, h = 900, 380
        pad_l, pad_r, pad_t, pad_b = 40, 40, 55, 50
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b

        losses = [ln["loss"] for ln in lines]
        lo_min = min(losses) if losses else 0
        lo_max = max(losses) if losses else 1
        lo_rng = lo_max - lo_min if lo_max > lo_min else 1

        def _loss_color(loss: float) -> str:
            t = (loss - lo_min) / lo_rng
            t = max(0.0, min(1.0, t))
            r = int(34 + t * (239 - 34))
            g = int(197 + t * (68 - 197))
            b_ch = int(94 + t * (68 - 94))
            return f"rgb({r},{g},{b_ch})"

        elems = []
        elems.append(
            "<defs>"
            '<linearGradient id="pc-bg" x1="0" y1="0" '
            'x2="0" y2="1">'
            '<stop offset="0%" stop-color="#22c55e" '
            'stop-opacity="0.06"/>'
            '<stop offset="50%" stop-color="#1a1a2e" '
            'stop-opacity="0"/>'
            '<stop offset="100%" stop-color="#ef4444" '
            'stop-opacity="0.06"/>'
            "</linearGradient></defs>"
        )
        elems.append(
            f'<rect x="{pad_l}" y="{pad_t}" '
            f'width="{plot_w}" height="{plot_h}" '
            f'fill="url(#pc-bg)" rx="4"/>'
        )

        # Axes + labels + value ticks (0/1 markers)
        for i, p in enumerate(params):
            x = pad_l + i * plot_w / max(n_params - 1, 1)
            elems.append(
                f'<line x1="{x:.1f}" y1="{pad_t}" '
                f'x2="{x:.1f}" y2="{h - pad_b}" '
                f'stroke="#2a2a4a" stroke-width="1"/>'
            )
            lbl = html.escape(p[:15])
            elems.append(
                f'<text x="{x:.1f}" y="{pad_t - 14}" '
                f'fill="#bbb" font-size="10" '
                f'font-weight="bold" '
                f'text-anchor="middle">{lbl}</text>'
            )
            # Top=1.0 (max), Bottom=0.0 (min)
            elems.append(
                f'<text x="{x + 4:.1f}" y="{pad_t + 10}" fill="#666" font-size="7">1.0</text>'
            )
            elems.append(
                f'<text x="{x + 4:.1f}" y="{h - pad_b - 3}" fill="#666" font-size="7">0.0</text>'
            )

        # Convergence zone for best epoch (highlight)
        best_line = lines[0] if lines else None
        if best_line:
            for pi, p in enumerate(params):
                x = pad_l + (pi * plot_w / max(n_params - 1, 1))
                v = best_line["values"].get(p, 0.5)
                v = max(0.0, min(1.0, v))
                cy = pad_t + (1.0 - v) * plot_h
                zone_h = plot_h * 0.12
                elems.append(
                    f'<rect x="{x - 8:.1f}" '
                    f'y="{cy - zone_h / 2:.1f}" '
                    f'width="16" height="{zone_h:.0f}" '
                    f'fill="#00d4ff" opacity="0.08" '
                    f'rx="3"/>'
                )

        # Draw worst-to-best
        for li in range(len(lines) - 1, -1, -1):
            line = lines[li]
            vals = line["values"]
            loss = line["loss"]
            color = _loss_color(loss)
            opacity = 0.9 if li == 0 else max(0.2, 0.7 - li * 0.06)
            stroke_w = 3.0 if li == 0 else 1.5
            points = []
            for pi, p in enumerate(params):
                x = pad_l + (pi * plot_w / max(n_params - 1, 1))
                v = max(0.0, min(1.0, vals.get(p, 0.5)))
                y = pad_t + (1.0 - v) * plot_h
                points.append(f"{x:.1f},{y:.1f}")
            if points:
                tooltip = f"Loss: {loss:.4f}"
                elems.append(
                    f"<polyline points="
                    f'"{" ".join(points)}" '
                    f'fill="none" stroke="{color}" '
                    f'stroke-width="{stroke_w}" '
                    f'opacity="{opacity:.2f}">'
                    f"<title>{tooltip}</title>"
                    f"</polyline>"
                )

        # Best epoch dots on axes
        if best_line:
            for pi, p in enumerate(params):
                x = pad_l + (pi * plot_w / max(n_params - 1, 1))
                v = best_line["values"].get(p, 0.5)
                v = max(0.0, min(1.0, v))
                cy = pad_t + (1.0 - v) * plot_h
                elems.append(
                    f'<circle cx="{x:.1f}" cy="{cy:.1f}" '
                    f'r="4" fill="#00d4ff" '
                    f'stroke="#fff" stroke-width="1"/>'
                )
                elems.append(
                    f'<text x="{x:.1f}" y="{cy - 8:.1f}" '
                    f'fill="#00d4ff" font-size="8" '
                    f'text-anchor="middle">'
                    f"{v:.2f}</text>"
                )

        # Color legend bar
        lg_x = w - 160
        lg_y, lg_w, lg_h = pad_t, 12, plot_h
        elems.append(
            "<defs>"
            '<linearGradient id="pc-leg" x1="0" y1="0" '
            'x2="0" y2="1">'
            '<stop offset="0%" stop-color="#22c55e"/>'
            '<stop offset="100%" stop-color="#ef4444"/>'
            "</linearGradient></defs>"
        )
        elems.append(
            f'<rect x="{lg_x}" y="{lg_y}" '
            f'width="{lg_w}" height="{lg_h}" '
            f'fill="url(#pc-leg)" rx="3"/>'
        )
        elems.append(
            f'<text x="{lg_x + lg_w + 4}" y="{lg_y + 10}" '
            f'fill="#22c55e" font-size="8">'
            f"best ({lo_min:.3f})</text>"
        )
        elems.append(
            f'<text x="{lg_x + lg_w + 4}" '
            f'y="{lg_y + lg_h - 2}" '
            f'fill="#ef4444" font-size="8">'
            f"worst ({lo_max:.3f})</text>"
        )

        body = "\n".join(elems)
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{body}</svg>'

    # ------------------------------------------------------------------
    # Overfitting warnings & Parameter deep dive
    # ------------------------------------------------------------------

    def _section_overfit_warnings(self) -> str:
        warnings = self.d.get("overfit_warnings", [])
        if not warnings:
            return ""
        L = self._L
        parts = [
            '<div class="section" id="sec-overfit"><h2>'
            + L("Overfitting Warnings", "Alertes de surapprentissage")
            + "</h2>"
            + self._desc(
                "Automated checks for signs of overfitting or fragility. "
                "Each alert includes the values that triggered it.",
                "Vérifications automatiques des signes de surapprentissage "
                "ou fragilité. Chaque alerte inclut les valeurs déclenchantes.",
            )
        ]
        for w in warnings:
            parts.append(self._render_warning(w))
        n_high = sum(1 for w in warnings if w.get("severity") == "high")
        if n_high > 0:
            parts.append(
                self._advisory(
                    "bad",
                    f"{n_high} high-severity warning(s). "
                    "Address these before deploying live — "
                    "they indicate likely overfitting.",
                    f"{n_high} alerte(s) haute severite. "
                    "Corrigez avant de deployer en live — "
                    "signe probable de surapprentissage.",
                )
            )
        else:
            parts.append(
                self._advisory(
                    "warn",
                    f"{len(warnings)} warning(s) detected. "
                    "Review each and decide if they are "
                    "acceptable for your risk tolerance.",
                    f"{len(warnings)} alerte(s) detectee(s). "
                    "Examinez chacune selon votre tolerance "
                    "au risque.",
                )
            )
        parts.append("</div>")
        return "".join(parts)

    def _render_warning(self, w: dict) -> str:
        sev = w.get("severity", "medium")
        colors = {
            "high": "#ef4444",
            "medium": "#eab308",
            "low": "#888",
        }
        icons = {"high": "!!!", "medium": "!!", "low": "!"}
        color = colors.get(sev, "#888")
        icon = icons.get(sev, "!")
        L = self._L
        title = L(w.get("title_en", ""), w.get("title_fr", ""))
        detail = L(w.get("detail_en", ""), w.get("detail_fr", ""))
        vals = w.get("values", {})
        vals_html = " | ".join(f"{k}: <strong>{v}</strong>" for k, v in vals.items())

        wtype = w.get("warning_type", "")
        svg = self._warning_diagram(wtype, vals)

        actions_en = w.get("actions_en", [])
        actions_fr = w.get("actions_fr", [])
        actions_html = ""
        if actions_en:
            items_en = "".join(f"<li>{html.escape(a)}</li>" for a in actions_en)
            items_fr = "".join(f"<li>{html.escape(a)}</li>" for a in actions_fr)
            lbl = L("What to do", "Que faire")
            actions_html = (
                '<details class="warn-actions">'
                f"<summary>{lbl}</summary>"
                f'<ul style="margin:4px 0 0 0;padding-left:20px;'
                f'font-size:0.85em;color:#bbb">'
                f"{L(items_en, items_fr)}</ul>"
                "</details>"
            )

        # Link technical terms to tooltips
        detail_linked = self._link_terms_in_warning(detail, wtype)

        return (
            f'<div class="warn-box" '
            f'style="border-left:4px solid {color}">'
            f'<span style="color:{color};font-weight:bold">'
            f"{icon} {self._link_title_terms(title, wtype)}"
            f"</span>"
            f'<div style="font-size:0.85em;color:#bbb">'
            f"{detail_linked}</div>"
            f'<div style="font-size:0.82em;color:#888">'
            f"{vals_html}</div>"
            f"{svg}"
            f"{actions_html}"
            f"</div>"
        )

    def _link_title_terms(self, title_html: str, wtype: str) -> str:
        slug_map = {
            "dsr": ("dsr", "DSR"),
            "skew": ("skewness", "Skew"),
            "kurtosis": ("kurtosis", "Kurtosis"),
        }
        if wtype not in slug_map:
            return title_html
        slug, keyword = slug_map[wtype]
        tip = self._tip(slug, keyword)
        return title_html.replace(keyword, tip, 1)

    def _link_terms_in_warning(self, detail_html: str, wtype: str) -> str:
        term_map = {
            "dsr": [("Sharpe", "sharpe")],
            "skew": [("skewness", "skewness")],
            "kurtosis": [("kurtosis", "kurtosis")],
            "dof": [("degrees of freedom", "dof")],
            "concentration": [("profit_concentration", "profit_concentration")],
        }
        terms = term_map.get(wtype, [])
        for keyword, slug in terms:
            if keyword in detail_html:
                tip = self._tip(slug, keyword)
                detail_html = detail_html.replace(keyword, tip, 1)
        return detail_html

    # ------------------------------------------------------------------
    # Warning diagrams — inline SVGs per warning type
    # ------------------------------------------------------------------

    def _warning_diagram(self, wtype: str, vals: dict) -> str:
        diagrams = {
            "dsr": self._svg_warn_dsr,
            "dof": self._svg_warn_dof,
            "concentration": self._svg_warn_concentration,
            "outlier": self._svg_warn_outlier,
            "skew": self._svg_warn_skew,
            "kurtosis": self._svg_warn_kurtosis,
            "clustering": self._svg_warn_clustering,
            "boundary": self._svg_warn_boundary,
        }
        fn = diagrams.get(wtype)
        if not fn:
            return ""
        try:
            return fn(vals)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return ""

    @staticmethod
    def _svg_warn_dsr(vals: dict) -> str:
        obs = float(vals.get("Sharpe", 0))
        emax = float(vals.get("E[max SR]", 0))
        hi = max(obs, emax) * 1.3
        if hi < 0.01:
            return ""
        w, h = 400, 60
        pad_l, pad_r = 20, 20
        track_w = w - pad_l - pad_r
        x_obs = pad_l + (obs / hi) * track_w
        x_emax = pad_l + (emax / hi) * track_w

        zone_good_x = x_emax
        zone_good_w = pad_l + track_w - x_emax
        zone_bad_w = x_emax - pad_l

        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:#0f0f23;border-radius:4px;'
            f'margin:6px 0">'
            # Bad zone (left of threshold)
            f'<rect x="{pad_l}" y="18" '
            f'width="{zone_bad_w:.0f}" height="16" '
            f'fill="#ef444420" rx="3"/>'
            # Good zone (right of threshold)
            f'<rect x="{zone_good_x:.0f}" y="18" '
            f'width="{max(zone_good_w, 0):.0f}" height="16" '
            f'fill="#22c55e20" rx="3"/>'
            # Track
            f'<rect x="{pad_l}" y="24" width="{track_w}" '
            f'height="4" fill="#2a2a4a" rx="2"/>'
            # Threshold line
            f'<line x1="{x_emax:.0f}" y1="14" '
            f'x2="{x_emax:.0f}" y2="38" '
            f'stroke="#eab308" stroke-width="2" '
            f'stroke-dasharray="3"/>'
            f'<text x="{x_emax:.0f}" y="12" fill="#eab308" '
            f'font-size="8" text-anchor="middle">'
            f"E[max]={emax:.2f}</text>"
            # Observed marker
            f'<circle cx="{x_obs:.0f}" cy="26" r="6" '
            f'fill="{"#ef4444" if obs < emax else "#22c55e"}" '
            f'stroke="#fff" stroke-width="1"/>'
            f'<text x="{x_obs:.0f}" y="50" '
            f'fill="#e0e0e0" font-size="8" '
            f'text-anchor="middle">yours={obs:.2f}</text>'
            # Labels
            f'<text x="{pad_l}" y="52" fill="#666" '
            f'font-size="7">0</text>'
            f'<text x="{pad_l + track_w}" y="52" fill="#666" '
            f'font-size="7" text-anchor="end">'
            f"Sharpe</text>"
            "</svg>"
        )

    @staticmethod
    def _svg_warn_dof(vals: dict) -> str:
        ratio = float(vals.get("ratio", 0))
        w, h = 400, 55
        pad_l, pad_r = 20, 20
        track_w = w - pad_l - pad_r
        hi = max(ratio * 1.5, 35)

        zones = [
            (0, 5, "#ef444430", "danger"),
            (5, 10, "#eab30830", "risk"),
            (10, 30, "#22c55e20", "ok"),
            (30, hi, "#22c55e40", "ideal"),
        ]
        elems = []
        for z_lo, z_hi, col, _lbl in zones:
            x0 = pad_l + (z_lo / hi) * track_w
            x1 = pad_l + (min(z_hi, hi) / hi) * track_w
            elems.append(
                f'<rect x="{x0:.0f}" y="16" width="{x1 - x0:.0f}" height="18" fill="{col}" rx="2"/>'
            )

        x_val = pad_l + (min(ratio, hi) / hi) * track_w
        elems.append(
            f'<rect x="{pad_l}" y="22" width="{track_w}" height="4" fill="#2a2a4a" rx="2"/>'
        )
        color = "#ef4444" if ratio < 5 else "#eab308" if ratio < 10 else "#22c55e"
        elems.append(
            f'<circle cx="{x_val:.0f}" cy="24" r="6" '
            f'fill="{color}" stroke="#fff" stroke-width="1"/>'
        )
        elems.append(
            f'<text x="{x_val:.0f}" y="46" fill="#e0e0e0" '
            f'font-size="8" text-anchor="middle">'
            f"yours={ratio:.0f}</text>"
        )
        # Zone labels
        for threshold, label in [(5, "5"), (10, "10"), (30, "30+")]:
            tx = pad_l + (threshold / hi) * track_w
            elems.append(
                f'<line x1="{tx:.0f}" y1="14" '
                f'x2="{tx:.0f}" y2="36" '
                f'stroke="#666" stroke-width="1" '
                f'stroke-dasharray="2"/>'
            )
            elems.append(
                f'<text x="{tx:.0f}" y="12" fill="#888" '
                f'font-size="7" text-anchor="middle">'
                f"{label}</text>"
            )
        elems.append(f'<text x="{pad_l}" y="52" fill="#666" font-size="7">trades / params</text>')
        body = "\n".join(elems)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:#0f0f23;border-radius:4px;'
            f'margin:6px 0">{body}</svg>'
        )

    @staticmethod
    def _svg_warn_concentration(vals: dict) -> str:
        total = float(vals.get("total", 0))
        without = float(vals.get("without_top2", 0))
        if abs(total) < 0.001:
            return ""
        w, h = 400, 55
        pad_l, pad_r = 20, 40
        bar_w = w - pad_l - pad_r
        # Full bar = total profit
        # Segment = what remains without top2
        remain_ratio = without / total if total else 0
        remain_w = max(0, remain_ratio) * bar_w
        total_color = "#22c55e" if total >= 0 else "#ef4444"
        rem_color = "#22c55e" if without >= 0 else "#ef4444"

        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:#0f0f23;border-radius:4px;'
            f'margin:6px 0">'
            # Total profit bar
            f'<rect x="{pad_l}" y="10" '
            f'width="{bar_w}" height="14" '
            f'fill="{total_color}" rx="3" opacity="0.3"/>'
            f'<text x="{pad_l + bar_w + 4}" y="22" '
            f'fill="{total_color}" font-size="8">'
            f"total: {total:.1f}</text>"
            # Without top2 bar
            f'<rect x="{pad_l}" y="30" '
            f'width="{max(remain_w, 0):.0f}" height="14" '
            f'fill="{rem_color}" rx="3" opacity="0.6"/>'
            f'<text x="{pad_l + max(remain_w, 0) + 4:.0f}" '
            f'y="42" fill="{rem_color}" font-size="8">'
            f"sans top-2: {without:.1f}</text>"
            # Zero line
            f'<line x1="{pad_l}" y1="6" '
            f'x2="{pad_l}" y2="50" '
            f'stroke="#666" stroke-width="1"/>'
            "</svg>"
        )

    @staticmethod
    def _svg_warn_outlier(vals: dict) -> str:
        best_s = str(vals.get("best", "0%")).rstrip("%")
        med_s = str(vals.get("median", "0%")).rstrip("%")
        try:
            best = float(best_s)
            med = float(med_s)
        except ValueError:
            return ""
        hi = max(best, med) * 1.3
        if hi < 0.01:
            return ""
        w, h = 400, 50
        pad_l, pad_r = 20, 20
        track_w = w - pad_l - pad_r

        x_med = pad_l + (med / hi) * track_w
        x_best = pad_l + (best / hi) * track_w

        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:#0f0f23;border-radius:4px;'
            f'margin:6px 0">'
            f'<rect x="{pad_l}" y="22" width="{track_w}" '
            f'height="4" fill="#2a2a4a" rx="2"/>'
            # Median marker
            f'<rect x="{x_med - 1:.0f}" y="14" '
            f'width="3" height="20" fill="#888" rx="1"/>'
            f'<text x="{x_med:.0f}" y="12" fill="#888" '
            f'font-size="8" text-anchor="middle">'
            f"median={med:.1f}%</text>"
            # Best marker
            f'<circle cx="{x_best:.0f}" cy="24" r="6" '
            f'fill="#eab308" stroke="#fff" '
            f'stroke-width="1"/>'
            f'<text x="{x_best:.0f}" y="44" fill="#eab308" '
            f'font-size="8" text-anchor="middle">'
            f"best={best:.1f}%</text>"
            # Gap arrow
            f'<line x1="{x_med:.0f}" y1="26" '
            f'x2="{x_best:.0f}" y2="26" '
            f'stroke="#eab30880" stroke-width="1" '
            f'stroke-dasharray="3"/>'
            "</svg>"
        )

    @staticmethod
    def _svg_warn_skew(vals: dict) -> str:
        skew = float(vals.get("skewness", 0))
        w, h = 400, 80
        pad_l, pad_r = 30, 30
        plot_w = w - pad_l - pad_r
        n_pts = 80

        import math

        elems = []
        # Draw normal distribution + skewed overlay
        pts_normal = []
        pts_skewed = []
        for i in range(n_pts):
            x_norm = -3 + 6 * i / (n_pts - 1)
            # Normal
            y_n = math.exp(-0.5 * x_norm**2)
            # Skewed (approximate)
            shift = skew * 0.4
            y_s = math.exp(-0.5 * (x_norm - shift) ** 2) * (
                1 + 0.3 * abs(skew) * (math.exp(-0.8 * (x_norm + 1.5) ** 2) if skew < 0 else 0)
            )
            sx = pad_l + (i / (n_pts - 1)) * plot_w
            sy_n = 65 - y_n * 45
            sy_s = 65 - y_s * 45
            pts_normal.append(f"{sx:.1f},{sy_n:.1f}")
            pts_skewed.append(f"{sx:.1f},{sy_s:.1f}")

        elems.append(
            f'<polyline points="{" ".join(pts_normal)}" '
            f'fill="none" stroke="#666" '
            f'stroke-width="1.5" stroke-dasharray="4"/>'
        )
        elems.append(
            f'<polyline points="{" ".join(pts_skewed)}" '
            f'fill="none" stroke="#eab308" '
            f'stroke-width="2"/>'
        )
        # Legend
        elems.append(
            f'<line x1="{w - 140}" y1="10" '
            f'x2="{w - 120}" y2="10" '
            f'stroke="#666" stroke-width="1.5" '
            f'stroke-dasharray="4"/>'
        )
        elems.append(f'<text x="{w - 116}" y="13" fill="#666" font-size="8">normal</text>')
        elems.append(
            f'<line x1="{w - 140}" y1="22" '
            f'x2="{w - 120}" y2="22" '
            f'stroke="#eab308" stroke-width="2"/>'
        )
        elems.append(
            f'<text x="{w - 116}" y="25" fill="#eab308" '
            f'font-size="8">yours (skew={skew:.1f})</text>'
        )
        # Left tail highlight
        if skew < -0.5:
            tail_x = pad_l
            tail_w = plot_w * 0.15
            elems.append(
                f'<rect x="{tail_x}" y="20" '
                f'width="{tail_w:.0f}" height="48" '
                f'fill="#ef444415" rx="3"/>'
            )
            elems.append(
                f'<text x="{tail_x + tail_w / 2:.0f}" '
                f'y="76" fill="#ef4444" font-size="7" '
                f'text-anchor="middle">tail risk</text>'
            )
        body = "\n".join(elems)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:#0f0f23;border-radius:4px;'
            f'margin:6px 0">{body}</svg>'
        )

    @staticmethod
    def _svg_warn_kurtosis(vals: dict) -> str:
        kurt = float(vals.get("kurtosis", 0))
        w, h = 400, 80
        pad_l, pad_r = 30, 30
        plot_w = w - pad_l - pad_r
        n_pts = 80

        import math

        elems = []
        pts_normal = []
        pts_fat = []
        for i in range(n_pts):
            x_norm = -3.5 + 7 * i / (n_pts - 1)
            y_n = math.exp(-0.5 * x_norm**2)
            # Fat tails: wider distribution, lower peak
            scale = 1 + min(kurt, 10) * 0.08
            y_f = math.exp(-0.5 * (x_norm / scale) ** 2) / scale
            sx = pad_l + (i / (n_pts - 1)) * plot_w
            sy_n = 65 - y_n * 48
            sy_f = 65 - y_f * 48
            pts_normal.append(f"{sx:.1f},{sy_n:.1f}")
            pts_fat.append(f"{sx:.1f},{sy_f:.1f}")

        elems.append(
            f'<polyline points="{" ".join(pts_normal)}" '
            f'fill="none" stroke="#666" '
            f'stroke-width="1.5" stroke-dasharray="4"/>'
        )
        elems.append(
            f'<polyline points="{" ".join(pts_fat)}" '
            f'fill="none" stroke="#eab308" '
            f'stroke-width="2"/>'
        )
        # Tail highlights
        tail_w = plot_w * 0.12
        for tx in [pad_l, pad_l + plot_w - tail_w]:
            elems.append(
                f'<rect x="{tx:.0f}" y="20" '
                f'width="{tail_w:.0f}" height="48" '
                f'fill="#ef444415" rx="3"/>'
            )
        elems.append(
            f'<text x="{pad_l + tail_w / 2:.0f}" y="76" '
            f'fill="#ef4444" font-size="7" '
            f'text-anchor="middle">fat tail</text>'
        )
        elems.append(
            f'<text x="{pad_l + plot_w - tail_w / 2:.0f}" '
            f'y="76" fill="#ef4444" font-size="7" '
            f'text-anchor="middle">fat tail</text>'
        )
        # Legend
        elems.append(
            f'<line x1="{w - 160}" y1="10" '
            f'x2="{w - 140}" y2="10" stroke="#666" '
            f'stroke-width="1.5" stroke-dasharray="4"/>'
        )
        elems.append(f'<text x="{w - 136}" y="13" fill="#666" font-size="8">normal (k=0)</text>')
        elems.append(
            f'<line x1="{w - 160}" y1="22" '
            f'x2="{w - 140}" y2="22" '
            f'stroke="#eab308" stroke-width="2"/>'
        )
        elems.append(
            f'<text x="{w - 136}" y="25" fill="#eab308" font-size="8">yours (k={kurt:.1f})</text>'
        )
        body = "\n".join(elems)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:#0f0f23;border-radius:4px;'
            f'margin:6px 0">{body}</svg>'
        )

    @staticmethod
    def _svg_warn_clustering(vals: dict) -> str:
        parts = str(vals.get("converging", "0/0"))
        try:
            conv, total = parts.split("/")
            conv_n, total_n = int(conv), int(total)
        except (ValueError, AttributeError):
            return ""
        if total_n == 0:
            return ""
        w, h = 400, 45
        pad_l = 20
        bar_w = 300
        seg_w = bar_w / total_n

        elems = []
        for i in range(total_n):
            x = pad_l + i * seg_w
            color = "#ef4444" if i < conv_n else "#22c55e"
            elems.append(
                f'<rect x="{x:.0f}" y="10" '
                f'width="{seg_w - 2:.0f}" height="20" '
                f'fill="{color}" rx="2" opacity="0.6"/>'
            )
        elems.append(
            f'<text x="{pad_l + bar_w + 8}" y="24" '
            f'fill="#e0e0e0" font-size="9">'
            f"{conv_n}/{total_n} converging</text>"
        )
        # Threshold line at 50%
        tx = pad_l + total_n * 0.5 * seg_w
        elems.append(
            f'<line x1="{tx:.0f}" y1="6" x2="{tx:.0f}" '
            f'y2="36" stroke="#eab308" stroke-width="1" '
            f'stroke-dasharray="3"/>'
        )
        elems.append(
            f'<text x="{tx:.0f}" y="42" fill="#eab308" '
            f'font-size="7" text-anchor="middle">50%</text>'
        )
        body = "\n".join(elems)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:#0f0f23;border-radius:4px;'
            f'margin:6px 0">{body}</svg>'
        )

    @staticmethod
    def _svg_warn_boundary(vals: dict) -> str:
        params_str = vals.get("params", "")
        params = [p.strip() for p in params_str.split(",") if p.strip()]
        if not params:
            return ""
        n = len(params)
        row_h = 22
        w, h = 400, 14 + n * row_h
        pad_l = 100
        bar_w = 240

        elems = []
        for i, p in enumerate(params):
            y = 10 + i * row_h
            lbl = html.escape(p[:18])
            elems.append(
                f'<text x="{pad_l - 6}" y="{y + 12}" '
                f'text-anchor="end" fill="#bbb" '
                f'font-size="9">{lbl}</text>'
            )
            # Track
            elems.append(
                f'<rect x="{pad_l}" y="{y + 4}" width="{bar_w}" height="10" fill="#2a2a4a" rx="3"/>'
            )
            # Edge highlights
            edge_w = bar_w * 0.08
            elems.append(
                f'<rect x="{pad_l}" y="{y + 4}" '
                f'width="{edge_w:.0f}" height="10" '
                f'fill="#ef444440" rx="3"/>'
            )
            elems.append(
                f'<rect x="{pad_l + bar_w - edge_w:.0f}" '
                f'y="{y + 4}" '
                f'width="{edge_w:.0f}" height="10" '
                f'fill="#ef444440" rx="3"/>'
            )
            # Marker at edge
            elems.append(
                f'<circle cx="{pad_l + 4}" cy="{y + 9}" '
                f'r="4" fill="#ef4444" '
                f'stroke="#fff" stroke-width="1"/>'
            )
        body = "\n".join(elems)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:#0f0f23;border-radius:4px;'
            f'margin:6px 0">{body}</svg>'
        )

    def _section_param_deep_dive(self) -> str:
        pdd = self.d.get("param_deep_dive")
        if not pdd:
            return ""
        L = self._L
        parts = [
            '<div class="section" id="sec-param-dive"><h2>'
            + L(
                "Parameter Deep Dive",
                "Analyse approfondie des paramètres",
            )
            + "</h2>"
            + self._desc(
                "Per-parameter diagnostics: type, search range, "
                "convergence, distribution, and sensitivity.",
                "Diagnostics par paramètre : type, plage de "
                "recherche, convergence, distribution et sensibilité.",
            )
        ]
        for pname in sorted(pdd.keys()):
            parts.append(self._render_param_detail(pname, pdd[pname]))
        parts.append(self._advisory_param_dive(pdd))
        parts.append("</div>")
        return "".join(parts)

    def _advisory_param_dive(self, pdd: dict) -> str:
        n_boundary = sum(1 for v in pdd.values() if v.get("boundary_cluster"))
        n_high_sens = sum(1 for v in pdd.values() if v.get("sensitivity_label") == "high")
        if n_boundary > 0:
            return self._advisory(
                "bad",
                f"{n_boundary} parameter(s) stuck at range "
                "boundary — widen the search space to "
                "find the true optimum.",
                f"{n_boundary} parametre(s) au bord de la "
                "plage — elargissez l'espace de recherche.",
            )
        if n_high_sens > 0:
            return self._advisory(
                "warn",
                f"{n_high_sens} highly sensitive "
                "parameter(s) — small changes cause large "
                "loss swings. Consider fixing or narrowing.",
                f"{n_high_sens} parametre(s) tres "
                "sensible(s) — de petits changements "
                "causent de grandes variations de loss.",
            )
        return self._advisory(
            "good",
            "No boundary clusters or highly sensitive "
            "parameters — search space is well-configured.",
            "Pas de parametres au bord ni de haute "
            "sensibilite — espace de recherche bien "
            "configure.",
        )

    def _render_param_detail(self, name: str, info: dict) -> str:
        L = self._L
        ptype = info.get("type", "?")
        best = info.get("best_value", "?")
        tendency = info.get("tendency", "")
        sens = info.get("sensitivity_label", "")
        boundary = info.get("boundary_cluster", False)

        badges = ""
        if tendency == "converging":
            badges += ' <span class="badge-conv">converging</span>'
        elif tendency == "spread":
            badges += ' <span class="badge-spread">spread</span>'
        if sens == "high":
            badges += ' <span class="badge-sens-high">' + L("sensitive", "sensible") + "</span>"
        elif sens == "medium":
            badges += (
                ' <span class="badge-sens-med">'
                + L("moderate sensitivity", "sensibilité modérée")
                + "</span>"
            )
        if boundary:
            badges += ' <span class="badge-boundary">' + L("boundary!", "au bord!") + "</span>"

        summary = (
            f"<strong>{self._esc(name)}</strong> = "
            f"<code>{self._esc(str(best))}</code>"
            f" ({ptype}){badges}"
        )

        body_parts = []
        rng_lo = info.get("range_low")
        rng_hi = info.get("range_high")
        cats = info.get("categories")
        if rng_lo is not None and rng_hi is not None:
            body_parts.append(
                f'<span class="pd-row">'
                f'<span class="pd-label">Range:</span> '
                f'<span class="pd-value">{rng_lo} — {rng_hi}'
                f"</span></span>"
            )
        elif cats:
            body_parts.append(
                '<span class="pd-row">'
                '<span class="pd-label">' + L("Categories", "Catégories") + ":</span> "
                f'<span class="pd-value">'
                f"{', '.join(str(c) for c in cats)}"
                f"</span></span>"
            )

        t10_med = info.get("top10_median")
        t10_min = info.get("top10_min")
        t10_max = info.get("top10_max")
        t10_std = info.get("top10_std")
        if t10_med is not None:
            body_parts.append(
                f'<span class="pd-row">'
                f'<span class="pd-label">Top-10:</span> '
                f'<span class="pd-value">'
                f"{t10_min} — {t10_max} "
                f"(med: {t10_med}, std: {t10_std})"
                f"</span></span>"
            )

        sens_val = info.get("sensitivity")
        if sens_val is not None:
            body_parts.append(
                '<span class="pd-row">'
                '<span class="pd-label">'
                + L("Sensitivity (|corr|)", "Sensibilité (|corr|)")
                + ":</span> "
                f'<span class="pd-value">'
                f"{sens_val:.3f}"
                f"</span></span>"
            )

        cat_counts = info.get("category_counts", {})
        if cat_counts:
            cat_str = ", ".join(
                f"{k}: {v}"
                for k, v in sorted(
                    cat_counts.items(),
                    key=lambda x: -x[1],
                )
            )
            body_parts.append(
                '<span class="pd-row">'
                '<span class="pd-label">' + L("Distribution", "Distribution") + f":</span> "
                f'<span class="pd-value">{cat_str}'
                f"</span></span>"
            )

        hist = info.get("histogram")
        svg_hist = ""
        if hist:
            svg_hist = self._svg_mini_histogram(hist, info.get("best_value"))

        body = "<br>".join(body_parts)
        return (
            '<div class="param-detail"><details>'
            f"<summary>{summary}</summary>"
            f'<div style="padding:6px 0">{body}'
            f"{svg_hist}</div>"
            f"</details></div>"
        )

    @staticmethod
    def _svg_mini_histogram(
        bins: list[dict],
        best_value=None,
    ) -> str:
        w, h = 250, 50
        pad_l, pad_b = 5, 12
        n = len(bins)
        if n == 0:
            return ""
        max_count = max((b["count"] for b in bins), default=1)
        if max_count == 0:
            return ""
        bar_w = (w - pad_l * 2) / max(n, 1)
        elems = []
        for i, b in enumerate(bins):
            bh = b["count"] / max_count * (h - pad_b - 2)
            x = pad_l + i * bar_w
            y = h - pad_b - bh
            elems.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" '
                f'width="{bar_w * 0.85:.1f}" '
                f'height="{bh:.1f}" fill="#3a4a6a" rx="1"/>'
            )
        if i == 0:
            lo_txt = f"{bins[0]['lo']:.2f}"
            hi_txt = f"{bins[-1]['hi']:.2f}"
            elems.append(f'<text x="{pad_l}" y="{h - 1}" fill="#666" font-size="7">{lo_txt}</text>')
            elems.append(
                f'<text x="{w - pad_l}" y="{h - 1}" fill="#666" '
                f'font-size="7" text-anchor="end">{hi_txt}</text>'
            )
        elif n > 1:
            lo_txt = f"{bins[0]['lo']:.2f}"
            hi_txt = f"{bins[-1]['hi']:.2f}"
            elems.append(f'<text x="{pad_l}" y="{h - 1}" fill="#666" font-size="7">{lo_txt}</text>')
            elems.append(
                f'<text x="{w - pad_l}" y="{h - 1}" fill="#666" '
                f'font-size="7" text-anchor="end">{hi_txt}</text>'
            )
        if best_value is not None and isinstance(best_value, (int, float)) and bins:
            lo_val = bins[0]["lo"]
            hi_val = bins[-1]["hi"]
            rng = hi_val - lo_val
            if rng > 0:
                bx = pad_l + (best_value - lo_val) / rng * (w - pad_l * 2)
                elems.append(
                    f'<line x1="{bx:.1f}" y1="0" '
                    f'x2="{bx:.1f}" y2="{h - pad_b}" '
                    f'stroke="#00d4ff" stroke-width="1.5" '
                    f'stroke-dasharray="2"/>'
                )
        body = "\n".join(elems)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" '
            f'style="background:#0f0f23;border-radius:4px;'
            f'margin:4px 0">{body}</svg>'
        )

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
        L = self._L
        parts = [
            '<div class="section"><h2>' + L("Loss Function", "Fonction de loss") + "</h2>"
            f'<div class="explain-box">'
            f"<strong>{self._esc(loss_name)}</strong> — {one_liner}",
        ]
        if best_for:
            parts.append(f"<br><strong>{L('Best for', 'Idéal pour')}:</strong> {best_for}")
        if metrics:
            parts.append(f"<br><strong>{L('Metrics', 'Métriques')}:</strong> {metrics}")
        parts.append("</div></div>")
        return "".join(parts)

    def _section_sampler_explanation(self) -> str:
        sampler_name = self.d.get("sampler") or "TPESampler"
        entry = SAMPLER_GLOSSARY.get(sampler_name, {})
        if not entry:
            return ""
        one_liner_en = self._esc(entry.get("one_liner", ""))
        one_liner_fr = self._esc(entry.get("one_liner_fr", one_liner_en))
        expl_en = self._esc(entry.get("explanation", ""))
        expl_fr = self._esc(entry.get("explanation_fr", expl_en))
        wtu_en = self._esc(entry.get("when_to_use", ""))
        wtu_fr = self._esc(entry.get("when_to_use_fr", wtu_en))
        L = self._L
        when_html = ""
        if wtu_en:
            when_html = (
                "<br><br><strong>"
                + L("When to use", "Quand l'utiliser")
                + f":</strong> {L(wtu_en, wtu_fr)}"
            )
        return (
            '<div class="section"><h2>'
            + L("Sampler", "Échantillonneur")
            + "</h2>"
            + self._desc(
                "The sampling algorithm used to explore the parameter space.",
                "L'algorithme d'échantillonnage utilisé pour explorer l'espace des paramètres.",
            )
            + f'<div class="explain-box">'
            f"<strong>{self._esc(sampler_name)}</strong>"
            f" — {L(one_liner_en, one_liner_fr)}"
            f"<br><br>{L(expl_en, expl_fr)}"
            f"{when_html}"
            "</div></div>"
        )

    def _section_next_steps(self) -> str:
        m = self.d.get("best_epoch", {})
        L = self._L
        strategy = self._esc(self.d.get("strategy", "Strategy"))
        strat_file = f'<code class="filepath">user_data/strategies/{strategy}.json</code>'
        criteria = self._build_scorecard(m)
        n_green = sum(1 for c in criteria if c["lvl"] == "g")
        n_yellow = sum(1 for c in criteria if c["lvl"] == "y")
        n_red = sum(1 for c in criteria if c["lvl"] == "r")
        if n_red > 0:
            verdict_c = "#ef4444"
            verdict_en = "Do NOT deploy — fix red criteria first."
            verdict_fr = "NE PAS déployer — corriger les critères rouges."
        elif n_yellow > 2:
            verdict_c = "#eab308"
            verdict_en = "Marginal — several yellow flags need attention."
            verdict_fr = "Marginal — plusieurs alertes jaunes à traiter."
        elif n_yellow > 0:
            verdict_c = "#eab308"
            verdict_en = "Acceptable with caveats — review yellow items."
            verdict_fr = "Acceptable avec réserves — vérifier les points jaunes."
        else:
            verdict_c = "#22c55e"
            verdict_en = "All criteria passed."
            verdict_fr = "Tous les critères sont validés."
        verdict_html = (
            f'<div style="border:2px solid {verdict_c};'
            f"border-radius:8px;padding:10px 16px;"
            f'margin:8px 0;text-align:center">'
            f'<strong style="color:{verdict_c};'
            f'font-size:1.1em">'
            f"{n_green} "
            + L("passed", "validés")
            + f" / {n_yellow} "
            + L("warnings", "alertes")
            + f" / {n_red} "
            + L("failed", "échoués")
            + f"</strong><br>"
            f'<span style="color:{verdict_c}">'
            f"{L(verdict_en, verdict_fr)}</span></div>"
        )
        card_html = self._render_scorecard(criteria)
        recs = self._build_recommendations(criteria, strat_file)
        recs_html = ""
        if recs:
            items = "".join(f"<li>{L(e, f)}</li>" for e, f in recs)
            recs_html = (
                '<div style="margin-top:12px">'
                "<strong>"
                + L("Recommendations", "Recommandations")
                + ":</strong><ul style='margin:6px 0;"
                f"padding-left:20px'>{items}</ul></div>"
            )
        output_html = (
            "<details><summary>"
            + L(
                "Applying the parameters",
                "Appliquer les paramètres",
            )
            + "</summary><div>"
            "<p>" + L("Params saved in", "Paramètres dans") + ' <code class="filepath">'
            "user_data/hyperopt_results/</code></p>"
            "<p>"
            + L("To apply, copy to", "Pour appliquer, copier dans")
            + f" {strat_file}. "
            + L(
                "Freqtrade loads this at startup and "
                "overrides <code>buy_params</code> / "
                "<code>sell_params</code>.",
                "Freqtrade le charge au démarrage et "
                "écrase <code>buy_params</code> / "
                "<code>sell_params</code>.",
            )
            + "</p></div></details>"
        )
        return (
            '<div class="section" id="sec-scorecard"><h2>'
            + L("Strategy Scorecard", "Bilan de la stratégie")
            + "</h2>"
            + self._desc(
                "Each metric is graded against thresholds from "
                "quantitative trading literature (Carver, Clenow, "
                "Chan, Lopez de Prado). Red = blocking, "
                "Yellow = needs attention, Green = passed.",
                "Chaque métrique est évaluée selon des seuils "
                "issus de la littérature quantitative (Carver, "
                "Clenow, Chan, Lopez de Prado). Rouge = bloquant, "
                "Jaune = à surveiller, Vert = validé.",
            )
            + verdict_html
            + card_html
            + recs_html
            + output_html
            + "</div>"
        )

    _SC_ANCHORS: dict[str, str] = {
        "Profit": "sec-best-epoch",
        "Max Drawdown": "sec-best-epoch",
        "Calmar Ratio": "sec-best-epoch",
        "Sharpe Ratio": "sec-best-epoch",
        "Profit Factor": "sec-best-epoch",
        "Trade Count": "sec-best-epoch",
        "SQN": "sec-best-epoch",
        "Win Rate": "sec-best-epoch",
        "Expectancy Ratio": "sec-best-epoch",
        "Sortino / Sharpe": "sec-best-epoch",
        "Epoch Count": "sec-convergence",
        "Deflated Sharpe": "sec-best-epoch",
        "Profit Concentration": "sec-concentration",
        "Param Stability": "sec-param-stability",
        "Monte Carlo": "sec-monte-carlo",
        "Degrees of Freedom": "sec-dof",
        "Regime Consistency": "sec-regime",
        "Best vs Median": "sec-best-vs-median",
        "Return Distribution": "sec-best-epoch",
        "Result Dispersion": "sec-dispersion",
        "Overfitting Alerts": "sec-overfit",
    }

    def _sc_core_metrics(
        self,
        m: dict,
        cfg: dict,
        n_epochs: int,
    ) -> list[dict]:
        return [
            self._sc_profit(m.get("profit_total", 0.0)),
            self._sc_drawdown(m.get("max_drawdown_account", 0.0)),
            self._sc_calmar(m.get("calmar", 0.0)),
            self._sc_sharpe(m.get("sharpe", 0.0), n_epochs),
            self._sc_profit_factor(m.get("profit_factor", 0.0)),
            self._sc_trades(m.get("total_trades", 0), cfg),
            self._sc_sqn(m.get("sqn", 0.0)),
            self._sc_winrate(m.get("winrate", 0.0)),
            self._sc_expectancy(m.get("expectancy_ratio", 0.0)),
            self._sc_sortino_divergence(m.get("sharpe", 0.0), m.get("sortino", 0.0)),
            self._sc_epoch_count(n_epochs),
        ]

    def _build_scorecard(self, m: dict) -> list[dict]:
        cfg = self.d.get("config_summary") or {}
        n_epochs = self.d.get("total_epochs", 0)
        c = self._sc_core_metrics(m, cfg, n_epochs)
        optional = [
            ("dsr_analysis", self._sc_dsr),
            ("sans_top_trade", self._sc_concentration),
            ("monte_carlo", self._sc_monte_carlo),
            ("dof_analysis", self._sc_dof),
            ("regime_analysis", self._sc_regime),
            ("best_vs_median_gap", self._sc_best_vs_median),
            ("distribution_analysis", self._sc_distribution),
        ]
        for key, fn in optional:
            val = self.d.get(key)
            if val:
                c.append(fn(val))
        stab = self.d.get("param_stability") or {}
        if stab:
            c.append(self._sc_param_stability(stab))
        disp = self.d.get("dispersion_bands") or {}
        if disp:
            c.append(self._sc_dispersion(disp))
        warnings = self.d.get("overfit_warnings", [])
        if warnings:
            c.append(self._sc_overfit_warnings(warnings))
        for cr in c:
            anchor = self._SC_ANCHORS.get(cr["name"][0], "")
            if anchor:
                cr["anchor"] = anchor
        return c

    @staticmethod
    def _sc_profit(v: float) -> dict:
        pct = v * 100
        if v <= 0:
            return {
                "name": ("Profit", "Profit"),
                "val": f"{pct:+.1f}%",
                "lvl": "r",
                "pct": 0,
                "en": "Not profitable — no edge detected.",
                "fr": "Non rentable — aucun edge détecté.",
                "ref": "#tips 187: survive before profit",
            }
        if pct < 5:
            return {
                "name": ("Profit", "Profit"),
                "val": f"{pct:+.1f}%",
                "lvl": "y",
                "pct": 30,
                "en": (f"Low profit ({pct:.1f}%). May not cover fees and slippage in live."),
                "fr": (
                    f"Profit faible ({pct:.1f}%). "
                    "Risque de ne pas couvrir frais et "
                    "slippage en live."
                ),
                "ref": "",
            }
        return {
            "name": ("Profit", "Profit"),
            "val": f"{pct:+.1f}%",
            "lvl": "g",
            "pct": min(100, int(pct * 3)),
            "en": f"Profitable ({pct:.1f}%).",
            "fr": f"Rentable ({pct:.1f}%).",
            "ref": "",
        }

    @staticmethod
    def _sc_drawdown(v: float) -> dict:
        pct = v * 100
        if pct > 45:
            return {
                "name": ("Max Drawdown", "Drawdown max"),
                "val": f"{pct:.0f}%",
                "lvl": "r",
                "pct": 95,
                "en": (f"DD {pct:.0f}% — extreme risk. Would blow up at 2x leverage."),
                "fr": (f"DD {pct:.0f}% — risque extrême. Explosion assurée à 2x levier."),
                "ref": "#16: DD max 30-35%",
            }
        if pct > 30:
            return {
                "name": ("Max Drawdown", "Drawdown max"),
                "val": f"{pct:.0f}%",
                "lvl": "y",
                "pct": int(pct * 2),
                "en": (
                    f"DD {pct:.0f}% — above the 30% "
                    "comfort threshold. Reduce stake or "
                    "tighten risk."
                ),
                "fr": (
                    f"DD {pct:.0f}% — au-dessus du seuil "
                    "de confort de 30%. Réduire le stake "
                    "ou resserrer le risque."
                ),
                "ref": "#16: DD max 30-35%",
            }
        return {
            "name": ("Max Drawdown", "Drawdown max"),
            "val": f"{pct:.0f}%",
            "lvl": "g",
            "pct": int(pct * 2),
            "en": f"DD {pct:.0f}% — within acceptable range.",
            "fr": f"DD {pct:.0f}% — dans la plage acceptable.",
            "ref": "",
        }

    @staticmethod
    def _sc_calmar(v: float) -> dict:
        if v < 0.5:
            return {
                "name": ("Calmar Ratio", "Ratio Calmar"),
                "val": f"{v:.2f}",
                "lvl": "r",
                "pct": int(min(v / 2 * 100, 100)),
                "en": (f"Calmar {v:.2f} — return does not justify the drawdown risk."),
                "fr": (f"Calmar {v:.2f} — le rendement ne justifie pas le risque de drawdown."),
                "ref": "#187: Calmar/DD prioritaires",
            }
        if v < 1.0:
            return {
                "name": ("Calmar Ratio", "Ratio Calmar"),
                "val": f"{v:.2f}",
                "lvl": "y",
                "pct": int(min(v / 2 * 100, 100)),
                "en": (f"Calmar {v:.2f} — marginal. Target > 1.0 for confidence."),
                "fr": (f"Calmar {v:.2f} — marginal. Viser > 1.0 pour plus de confiance."),
                "ref": "",
            }
        return {
            "name": ("Calmar Ratio", "Ratio Calmar"),
            "val": f"{v:.2f}",
            "lvl": "g",
            "pct": int(min(v / 2 * 100, 100)),
            "en": f"Calmar {v:.2f} — good return/DD ratio.",
            "fr": f"Calmar {v:.2f} — bon ratio rendement/DD.",
            "ref": "",
        }

    @staticmethod
    def _sc_sharpe(v: float, n_epochs: int) -> dict:
        if v > 5:
            return {
                "name": ("Sharpe Ratio", "Ratio Sharpe"),
                "val": f"{v:.2f}",
                "lvl": "r",
                "pct": 100,
                "en": (f"Sharpe {v:.2f} — fraud/bug/curve-fit. Sharpe > 5 is always a red flag."),
                "fr": (f"Sharpe {v:.2f} — fraude/bug/curve-fit. Sharpe > 5 est toujours suspect."),
                "ref": "#132 Clenow: Sharpe > 5 = fraud",
            }
        if v < 0:
            return {
                "name": ("Sharpe Ratio", "Ratio Sharpe"),
                "val": f"{v:.2f}",
                "lvl": "r",
                "pct": 0,
                "en": "Negative Sharpe — risk-adjusted loss.",
                "fr": "Sharpe négatif — perte ajustée au risque.",
                "ref": "",
            }
        if v < 0.5:
            return {
                "name": ("Sharpe Ratio", "Ratio Sharpe"),
                "val": f"{v:.2f}",
                "lvl": "y",
                "pct": int(v / 2 * 100),
                "en": (f"Sharpe {v:.2f} — below the 0.85 realistic benchmark."),
                "fr": (f"Sharpe {v:.2f} — en dessous du benchmark réaliste de 0.85."),
                "ref": "#140 Clenow: ~0.85 = realistic",
            }
        return {
            "name": ("Sharpe Ratio", "Ratio Sharpe"),
            "val": f"{v:.2f}",
            "lvl": "g",
            "pct": int(min(v / 2 * 100, 100)),
            "en": f"Sharpe {v:.2f}.",
            "fr": f"Sharpe {v:.2f}.",
            "ref": "",
        }

    @staticmethod
    def _sc_profit_factor(v: float) -> dict:
        if v < 1.0:
            return {
                "name": ("Profit Factor", "Profit Factor"),
                "val": f"{v:.2f}",
                "lvl": "r",
                "pct": int(min(v * 50, 100)),
                "en": (f"PF {v:.2f} — losses exceed wins. No edge."),
                "fr": (f"PF {v:.2f} — pertes supérieures aux gains. Pas d'edge."),
                "ref": "",
            }
        if v < 1.3:
            return {
                "name": ("Profit Factor", "Profit Factor"),
                "val": f"{v:.2f}",
                "lvl": "y",
                "pct": int(min(v * 50, 100)),
                "en": (f"PF {v:.2f} — thin edge, vulnerable to fees/slippage in live."),
                "fr": (f"PF {v:.2f} — edge fragile, vulnérable aux frais/slippage en live."),
                "ref": "",
            }
        return {
            "name": ("Profit Factor", "Profit Factor"),
            "val": f"{v:.2f}",
            "lvl": "g",
            "pct": int(min(v * 50, 100)),
            "en": f"PF {v:.2f} — solid edge.",
            "fr": f"PF {v:.2f} — edge solide.",
            "ref": "",
        }

    @staticmethod
    def _sc_trades(n: int, cfg: dict) -> dict:
        min_t = max(cfg.get("min_trades", 0), 30)
        if n < min_t:
            return {
                "name": (
                    "Trade Count",
                    "Nombre de trades",
                ),
                "val": str(n),
                "lvl": "r",
                "pct": int(min(n / 100 * 100, 100)),
                "en": (f"Only {n} trades — not enough for statistical significance."),
                "fr": (f"Seulement {n} trades — insuffisant pour une significativité statistique."),
                "ref": "#76 Lopez de Prado: N trials matters",
            }
        if n < 60:
            return {
                "name": (
                    "Trade Count",
                    "Nombre de trades",
                ),
                "val": str(n),
                "lvl": "y",
                "pct": int(min(n / 100 * 100, 100)),
                "en": (f"{n} trades — marginal confidence. More data would strengthen results."),
                "fr": (
                    f"{n} trades — confiance marginale. "
                    "Plus de données renforceraient les résultats."
                ),
                "ref": "",
            }
        return {
            "name": ("Trade Count", "Nombre de trades"),
            "val": str(n),
            "lvl": "g",
            "pct": int(min(n / 100 * 100, 100)),
            "en": f"{n} trades — sufficient sample.",
            "fr": f"{n} trades — échantillon suffisant.",
            "ref": "",
        }

    @staticmethod
    def _sc_sqn(v: float) -> dict:
        if v < 0:
            return {
                "name": ("SQN", "SQN"),
                "val": f"{v:.2f}",
                "lvl": "r",
                "pct": 0,
                "en": "Negative SQN — losing system.",
                "fr": "SQN négatif — système perdant.",
                "ref": "",
            }
        if v < 1.0:
            return {
                "name": ("SQN", "SQN"),
                "val": f"{v:.2f}",
                "lvl": "y",
                "pct": int(min(v / 3 * 100, 100)),
                "en": f"SQN {v:.2f} — weak edge quality.",
                "fr": f"SQN {v:.2f} — qualité d'edge faible.",
                "ref": "",
            }
        return {
            "name": ("SQN", "SQN"),
            "val": f"{v:.2f}",
            "lvl": "g",
            "pct": int(min(v / 3 * 100, 100)),
            "en": f"SQN {v:.2f}.",
            "fr": f"SQN {v:.2f}.",
            "ref": "",
        }

    @staticmethod
    def _sc_winrate(v: float) -> dict:
        pct = v * 100
        if pct < 40:
            return {
                "name": ("Win Rate", "Taux de gain"),
                "val": f"{pct:.0f}%",
                "lvl": "y",
                "pct": int(pct),
                "en": (
                    f"WR {pct:.0f}% — low, but acceptable "
                    "if payoff ratio compensates. Win rate "
                    "alone is NOT the important metric."
                ),
                "fr": (
                    f"WR {pct:.0f}% — bas, mais acceptable "
                    "si le payoff ratio compense. Le win rate "
                    "seul n'est PAS la métrique importante."
                ),
                "ref": "#4: WR is not the important metric",
            }
        return {
            "name": ("Win Rate", "Taux de gain"),
            "val": f"{pct:.0f}%",
            "lvl": "g",
            "pct": int(pct),
            "en": f"WR {pct:.0f}%.",
            "fr": f"WR {pct:.0f}%.",
            "ref": "#4: check payoff ratio too",
        }

    @staticmethod
    def _sc_dsr(dsr: dict) -> dict:
        genuine = dsr.get("genuine", False)
        obs = dsr.get("observed_sharpe", 0)
        exp = dsr.get("expected_max_sharpe", 0)
        n = dsr.get("n_trials", 0)
        if not genuine:
            return {
                "name": (
                    "Deflated Sharpe",
                    "Sharpe déflaté",
                ),
                "val": f"{obs:.2f} vs E[max]={exp:.2f}",
                "lvl": "y",
                "pct": int(min(obs / max(exp, 0.01) * 100, 100)),
                "en": (
                    f"Sharpe {obs:.2f} < E[max SR] {exp:.2f} from {n} trials — likely overfitted."
                ),
                "fr": (
                    f"Sharpe {obs:.2f} < E[max SR] "
                    f"{exp:.2f} sur {n} essais — "
                    "probablement surajusté."
                ),
                "ref": "#76 Lopez de Prado: N trials",
            }
        return {
            "name": ("Deflated Sharpe", "Sharpe déflaté"),
            "val": f"{obs:.2f} vs E[max]={exp:.2f}",
            "lvl": "g",
            "pct": int(min(obs / max(exp, 0.01) * 100, 100)),
            "en": "Sharpe exceeds expected max — likely genuine.",
            "fr": "Sharpe dépasse le max attendu — probablement réel.",
            "ref": "",
        }

    @staticmethod
    def _sc_concentration(sans: dict) -> dict:
        fragile = sans.get("fragile", False)
        wo2 = sans.get("without_top2_pct", 100)
        if fragile or wo2 < 30:
            return {
                "name": (
                    "Profit Concentration",
                    "Concentration du profit",
                ),
                "val": f"{wo2:.0f}% w/o top 2",
                "lvl": "r" if wo2 < 0 else "y",
                "pct": max(int(wo2), 0),
                "en": ("Profit collapses without top trades — no proven edge, just lucky trades."),
                "fr": (
                    "Le profit s'effondre sans les meilleurs "
                    "trades — pas d'edge prouvé, juste de "
                    "la chance."
                ),
                "ref": ("#hyperopt.md: concentrated profit = red flag"),
            }
        return {
            "name": (
                "Profit Concentration",
                "Concentration du profit",
            ),
            "val": f"{wo2:.0f}% w/o top 2",
            "lvl": "g",
            "pct": int(min(wo2, 100)),
            "en": "Profit is well-distributed across trades.",
            "fr": "Le profit est bien réparti entre les trades.",
            "ref": "",
        }

    @staticmethod
    def _sc_param_stability(stab: dict) -> dict:
        total = len(stab)
        unstable = sum(1 for v in stab.values() if isinstance(v, dict) and v.get("unstable", False))
        if total == 0:
            return {
                "name": (
                    "Param Stability",
                    "Stabilité des params",
                ),
                "val": "—",
                "lvl": "g",
                "pct": 50,
                "en": "No params to assess.",
                "fr": "Aucun paramètre à évaluer.",
                "ref": "",
            }
        ratio = unstable / total
        if ratio > 0.5:
            return {
                "name": (
                    "Param Stability",
                    "Stabilité des params",
                ),
                "val": f"{unstable}/{total}",
                "lvl": "r",
                "pct": int((1 - ratio) * 100),
                "en": (f"{unstable}/{total} params are unstable — small changes crash perf."),
                "fr": (
                    f"{unstable}/{total} params instables "
                    "— un petit changement fait chuter la perf."
                ),
                "ref": "#81 Lopez de Prado: stability",
            }
        if unstable > 0:
            return {
                "name": (
                    "Param Stability",
                    "Stabilité des params",
                ),
                "val": f"{unstable}/{total}",
                "lvl": "y",
                "pct": int((1 - ratio) * 100),
                "en": (f"{unstable}/{total} params show some instability."),
                "fr": (f"{unstable}/{total} params montrent de l'instabilité."),
                "ref": "#81 Lopez de Prado: stability",
            }
        return {
            "name": (
                "Param Stability",
                "Stabilité des params",
            ),
            "val": f"0/{total}",
            "lvl": "g",
            "pct": 100,
            "en": "All params are stable across top epochs.",
            "fr": "Tous les params sont stables dans le top.",
            "ref": "#81 Lopez de Prado: stability",
        }

    @staticmethod
    def _sc_expectancy(v: float) -> dict:
        if v <= 0:
            return {
                "name": ("Expectancy Ratio", "Ratio d'espérance"),
                "val": f"{v:.3f}",
                "lvl": "r",
                "pct": 0,
                "en": "Negative expectancy — losing money per trade on average.",
                "fr": "Espérance négative — perte moyenne par trade.",
                "ref": "#188: asymmetric probabilities",
            }
        if v < 0.03:
            return {
                "name": ("Expectancy Ratio", "Ratio d'espérance"),
                "val": f"{v:.3f}",
                "lvl": "y",
                "pct": int(min(v / 0.1 * 100, 100)),
                "en": (f"Expectancy {v:.3f} — thin edge, fees may eat it in live."),
                "fr": (f"Espérance {v:.3f} — edge mince, les frais peuvent l'absorber en live."),
                "ref": "",
            }
        return {
            "name": ("Expectancy Ratio", "Ratio d'espérance"),
            "val": f"{v:.3f}",
            "lvl": "g",
            "pct": int(min(v / 0.1 * 100, 100)),
            "en": f"Expectancy {v:.3f} — solid per-trade edge.",
            "fr": f"Espérance {v:.3f} — edge solide par trade.",
            "ref": "",
        }

    @staticmethod
    def _sc_sortino_divergence(sharpe: float, sortino: float) -> dict:
        if sharpe <= 0:
            return {
                "name": (
                    "Sortino / Sharpe",
                    "Sortino / Sharpe",
                ),
                "val": "—",
                "lvl": "g",
                "pct": 50,
                "en": "N/A (Sharpe ≤ 0).",
                "fr": "N/A (Sharpe ≤ 0).",
                "ref": "",
            }
        ratio = sortino / sharpe if sharpe > 0 else 0
        if ratio > 3:
            return {
                "name": (
                    "Sortino / Sharpe",
                    "Sortino / Sharpe",
                ),
                "val": f"{ratio:.1f}x",
                "lvl": "y",
                "pct": int(min(ratio * 20, 100)),
                "en": (
                    f"Sortino {ratio:.1f}x Sharpe — "
                    "upside vol masks downside tail risk. "
                    "Clenow: 'those guys usually blow up.'"
                ),
                "fr": (
                    f"Sortino {ratio:.1f}x le Sharpe — "
                    "la vol haussière masque le risque de "
                    "queue. Clenow : 'those guys usually "
                    "blow up.'"
                ),
                "ref": "#98 Clenow: Sortino danger",
            }
        return {
            "name": ("Sortino / Sharpe", "Sortino / Sharpe"),
            "val": f"{ratio:.1f}x",
            "lvl": "g",
            "pct": int(min(ratio * 20, 100)),
            "en": f"Sortino/Sharpe ratio {ratio:.1f}x — normal.",
            "fr": f"Ratio Sortino/Sharpe {ratio:.1f}x — normal.",
            "ref": "",
        }

    @staticmethod
    def _sc_epoch_count(n: int) -> dict:
        if n > 500:
            return {
                "name": ("Epoch Count", "Nombre d'epochs"),
                "val": str(n),
                "lvl": "r",
                "pct": 100,
                "en": (
                    f"{n} epochs — high overfitting risk. Cap at 200-300 or run multiple seeds."
                ),
                "fr": (
                    f"{n} epochs — risque élevé de "
                    "surapprentissage. Limiter à 200-300 "
                    "ou lancer plusieurs seeds."
                ),
                "ref": "#14: cap 200-300 epochs",
            }
        if n > 300:
            return {
                "name": ("Epoch Count", "Nombre d'epochs"),
                "val": str(n),
                "lvl": "y",
                "pct": int(min(n / 5, 100)),
                "en": (
                    f"{n} epochs — above the 200-300 "
                    "sweet spot. Diminishing returns, "
                    "increasing overfitting."
                ),
                "fr": (
                    f"{n} epochs — au-dessus du sweet "
                    "spot 200-300. Rendements décroissants, "
                    "surapprentissage croissant."
                ),
                "ref": "#14: cap 200-300 epochs",
            }
        return {
            "name": ("Epoch Count", "Nombre d'epochs"),
            "val": str(n),
            "lvl": "g",
            "pct": int(min(n / 5, 100)),
            "en": f"{n} epochs — within recommended range.",
            "fr": f"{n} epochs — dans la plage recommandée.",
            "ref": "",
        }

    @staticmethod
    def _sc_monte_carlo(mc: dict) -> dict:
        prob = mc.get("prob_positive", 0)
        p5 = mc.get("p5", 0)
        if prob < 60:
            return {
                "name": ("Monte Carlo", "Monte Carlo"),
                "val": f"{prob:.0f}% positive",
                "lvl": "r",
                "pct": int(prob),
                "en": (
                    f"Only {prob:.0f}% of reshuffled "
                    "simulations are profitable. "
                    "Edge depends on trade ordering luck."
                ),
                "fr": (
                    f"Seulement {prob:.0f}% des simulations "
                    "remixées sont rentables. L'edge dépend "
                    "de l'ordre des trades."
                ),
                "ref": "",
            }
        if prob < 80 or p5 < 0:
            return {
                "name": ("Monte Carlo", "Monte Carlo"),
                "val": f"{prob:.0f}% pos, P5={p5:+.1f}%",
                "lvl": "y",
                "pct": int(prob),
                "en": (
                    f"{prob:.0f}% positive, but P5 = "
                    f"{p5:+.1f}%. Worst-case trade "
                    "ordering is risky."
                ),
                "fr": (
                    f"{prob:.0f}% positif, mais P5 = "
                    f"{p5:+.1f}%. Le pire scénario "
                    "d'ordonnancement est risqué."
                ),
                "ref": "",
            }
        return {
            "name": ("Monte Carlo", "Monte Carlo"),
            "val": f"{prob:.0f}% pos, P5={p5:+.1f}%",
            "lvl": "g",
            "pct": int(prob),
            "en": (f"{prob:.0f}% of reshuffled sequences are profitable (P5={p5:+.1f}%)."),
            "fr": (f"{prob:.0f}% des séquences remixées sont rentables (P5={p5:+.1f}%)."),
            "ref": "",
        }

    @staticmethod
    def _sc_dof(dof: dict) -> dict:
        ratio = dof.get("ratio", 0)
        level = dof.get("level", "red")
        n_t = dof.get("n_trades", 0)
        n_p = dof.get("n_params", 0)
        if level == "red":
            return {
                "name": (
                    "Degrees of Freedom",
                    "Degrés de liberté",
                ),
                "val": f"{ratio:.0f}x ({n_t}/{n_p})",
                "lvl": "r",
                "pct": int(min(ratio / 30 * 100, 100)),
                "en": (
                    f"{ratio:.0f} trades/param — optimizer can perfectly fit noise. Need ≥ 30x."
                ),
                "fr": (
                    f"{ratio:.0f} trades/param — "
                    "l'optimiseur peut parfaitement fitter "
                    "le bruit. Besoin de ≥ 30x."
                ),
                "ref": "",
            }
        if level in ("yellow", "orange"):
            return {
                "name": (
                    "Degrees of Freedom",
                    "Degrés de liberté",
                ),
                "val": f"{ratio:.0f}x ({n_t}/{n_p})",
                "lvl": "y",
                "pct": int(min(ratio / 30 * 100, 100)),
                "en": (f"{ratio:.0f} trades/param — marginal. Results may not generalize."),
                "fr": (
                    f"{ratio:.0f} trades/param — marginal. "
                    "Les résultats risquent de ne pas "
                    "généraliser."
                ),
                "ref": "",
            }
        return {
            "name": (
                "Degrees of Freedom",
                "Degrés de liberté",
            ),
            "val": f"{ratio:.0f}x ({n_t}/{n_p})",
            "lvl": "g",
            "pct": int(min(ratio / 30 * 100, 100)),
            "en": (f"{ratio:.0f} trades/param — sufficient statistical power."),
            "fr": (f"{ratio:.0f} trades/param — puissance statistique suffisante."),
            "ref": "",
        }

    @staticmethod
    def _sc_regime(regime: dict) -> dict:
        f1 = regime.get("first_half", {})
        f2 = regime.get("second_half", {})
        p1 = f1.get("profit_pct", 0)
        p2 = f2.get("profit_pct", 0)
        consistent = regime.get("consistent", True)
        if p2 <= 0 and p1 > 0:
            return {
                "name": (
                    "Regime Consistency",
                    "Consistance temporelle",
                ),
                "val": f"{p1:+.1f}% → {p2:+.1f}%",
                "lvl": "r",
                "pct": 10,
                "en": (
                    "Second half is unprofitable — edge "
                    "may have decayed or was overfitted "
                    "to early data."
                ),
                "fr": (
                    "La 2e moitié est non rentable — "
                    "l'edge a possiblement décliné ou "
                    "était surajusté aux données initiales."
                ),
                "ref": "",
            }
        if not consistent:
            drop = (1 - p2 / p1) * 100 if p1 > 0 else 0
            return {
                "name": (
                    "Regime Consistency",
                    "Consistance temporelle",
                ),
                "val": f"{p1:+.1f}% → {p2:+.1f}%",
                "lvl": "y",
                "pct": int(max(100 - drop, 10)),
                "en": (
                    f"Profit dropped {drop:.0f}% from "
                    "first to second half. May reflect "
                    "regime change or overfitting."
                ),
                "fr": (
                    f"Le profit a chuté de {drop:.0f}% "
                    "entre les deux moitiés. Possible "
                    "changement de régime ou surapprentissage."
                ),
                "ref": "",
            }
        return {
            "name": (
                "Regime Consistency",
                "Consistance temporelle",
            ),
            "val": f"{p1:+.1f}% → {p2:+.1f}%",
            "lvl": "g",
            "pct": 80,
            "en": "Performance is consistent across time halves.",
            "fr": "La performance est consistante entre les deux moitiés.",
            "ref": "",
        }

    @staticmethod
    def _sc_best_vs_median(bvm: dict) -> dict:
        gap = bvm.get("gap_ratio", 1.0)
        outlier = bvm.get("outlier", False)
        best = bvm.get("best_profit", 0)
        med = bvm.get("median_profit", 0)
        if gap > 3:
            return {
                "name": (
                    "Best vs Median",
                    "Meilleur vs Médiane",
                ),
                "val": f"{gap:.1f}x gap",
                "lvl": "r",
                "pct": int(min(100, gap * 20)),
                "en": (
                    f"Best epoch ({best:.1f}%) is {gap:.1f}x "
                    f"the median ({med:.1f}%). Classic "
                    "sign of the luckiest backtest."
                ),
                "fr": (
                    f"Le meilleur epoch ({best:.1f}%) est "
                    f"{gap:.1f}x la médiane ({med:.1f}%). "
                    "Signe classique du backtest le plus "
                    "chanceux."
                ),
                "ref": "#161 Chan: best = luckiest",
            }
        if outlier or gap > 2:
            return {
                "name": (
                    "Best vs Median",
                    "Meilleur vs Médiane",
                ),
                "val": f"{gap:.1f}x gap",
                "lvl": "y",
                "pct": int(min(100, gap * 20)),
                "en": (f"Best epoch {gap:.1f}x median — moderate outlier, verify robustness."),
                "fr": (
                    f"Meilleur epoch {gap:.1f}x la "
                    "médiane — outlier modéré, vérifier "
                    "la robustesse."
                ),
                "ref": "#161 Chan: best = luckiest",
            }
        return {
            "name": ("Best vs Median", "Meilleur vs Médiane"),
            "val": f"{gap:.1f}x gap",
            "lvl": "g",
            "pct": int(min(100, gap * 20)),
            "en": "Best epoch is close to median — consistent.",
            "fr": "Le meilleur epoch est proche de la médiane — consistant.",
            "ref": "",
        }

    @staticmethod
    def _sc_distribution(dist: dict) -> dict:
        skew = dist.get("skewness", 0)
        kurt = dist.get("excess_kurtosis", 0)
        alerts = []
        lvl = "g"
        if skew < -1:
            alerts.append(("strong neg skew", "forte asym. nég."))
            lvl = "y"
        if kurt > 6:
            alerts.append(("extreme fat tails", "queues extrêmes"))
            lvl = "r"
        elif kurt > 3:
            alerts.append(("fat tails", "queues épaisses"))
            if lvl != "r":
                lvl = "y"
        if not alerts:
            return {
                "name": (
                    "Return Distribution",
                    "Distribution des rendements",
                ),
                "val": f"skew={skew:.1f} kurt={kurt:.1f}",
                "lvl": "g",
                "pct": 70,
                "en": "Distribution shape is acceptable.",
                "fr": "La forme de la distribution est acceptable.",
                "ref": "",
            }
        en_a = ", ".join(a[0] for a in alerts)
        fr_a = ", ".join(a[1] for a in alerts)
        return {
            "name": (
                "Return Distribution",
                "Distribution des rendements",
            ),
            "val": f"skew={skew:.1f} kurt={kurt:.1f}",
            "lvl": lvl,
            "pct": 40,
            "en": (f"Warning: {en_a}. Real tail risk is higher than Sharpe suggests."),
            "fr": (
                f"Alerte : {fr_a}. Le risque de queue "
                "réel est plus élevé que le Sharpe "
                "ne le suggère."
            ),
            "ref": "#117 Carver: Sharpe hides blow-ups",
        }

    @staticmethod
    def _sc_dispersion(disp: dict) -> dict:
        p = disp.get("profit", {})
        p_min = p.get("min", 0)
        p_max = p.get("max", 0)
        p_med = p.get("median", 0)
        if not p:
            return {
                "name": (
                    "Result Dispersion",
                    "Dispersion des résultats",
                ),
                "val": "—",
                "lvl": "g",
                "pct": 50,
                "en": "No dispersion data.",
                "fr": "Pas de données de dispersion.",
                "ref": "",
            }
        spread = p_max - p_min
        if p_min < 0 and p_max > 0:
            return {
                "name": (
                    "Result Dispersion",
                    "Dispersion des résultats",
                ),
                "val": f"{p_min:+.1f}% → {p_max:+.1f}%",
                "lvl": "y",
                "pct": 40,
                "en": (
                    f"Top-10 profit ranges from "
                    f"{p_min:+.1f}% to {p_max:+.1f}%. "
                    "Some top epochs are negative — "
                    "unstable edge."
                ),
                "fr": (
                    f"Le profit du top-10 va de "
                    f"{p_min:+.1f}% à {p_max:+.1f}%. "
                    "Certains epochs du top sont négatifs "
                    "— edge instable."
                ),
                "ref": "",
            }
        if p_med > 0 and spread > p_med * 3:
            return {
                "name": (
                    "Result Dispersion",
                    "Dispersion des résultats",
                ),
                "val": f"{p_min:+.1f}% → {p_max:+.1f}%",
                "lvl": "y",
                "pct": 50,
                "en": (
                    f"Wide spread ({spread:.1f}%) across top-10 — best epoch may be an outlier."
                ),
                "fr": (
                    f"Large écart ({spread:.1f}%) dans le "
                    "top-10 — le meilleur epoch est peut-être "
                    "un outlier."
                ),
                "ref": "",
            }
        return {
            "name": (
                "Result Dispersion",
                "Dispersion des résultats",
            ),
            "val": f"{p_min:+.1f}% → {p_max:+.1f}%",
            "lvl": "g",
            "pct": 75,
            "en": "Top-10 results are tightly clustered.",
            "fr": "Les résultats du top-10 sont bien groupés.",
            "ref": "",
        }

    @staticmethod
    def _sc_overfit_warnings(warnings: list) -> dict:
        n = len(warnings)
        if n >= 3:
            return {
                "name": (
                    "Overfitting Alerts",
                    "Alertes surapprentissage",
                ),
                "val": f"{n} alerts",
                "lvl": "r",
                "pct": int(max(100 - n * 15, 5)),
                "en": (f"{n} overfitting warnings triggered. Strong evidence of curve-fitting."),
                "fr": (f"{n} alertes de surapprentissage. Forte suspicion de curve-fitting."),
                "ref": "#113 Carver: implicit fitting",
            }
        if n >= 1:
            return {
                "name": (
                    "Overfitting Alerts",
                    "Alertes surapprentissage",
                ),
                "val": f"{n} alert{'s' if n > 1 else ''}",
                "lvl": "y",
                "pct": int(max(100 - n * 15, 20)),
                "en": (f"{n} overfitting warning(s) — review the details above."),
                "fr": (f"{n} alerte(s) de surapprentissage — vérifier les détails ci-dessus."),
                "ref": "",
            }
        return {
            "name": (
                "Overfitting Alerts",
                "Alertes surapprentissage",
            ),
            "val": "0",
            "lvl": "g",
            "pct": 100,
            "en": "No overfitting warnings triggered.",
            "fr": "Aucune alerte de surapprentissage.",
            "ref": "",
        }

    _SC_SLUGS: dict[str, str] = {
        "Profit": "expectancy",
        "Max Drawdown": "dd",
        "Calmar Ratio": "calmar",
        "Sharpe Ratio": "sharpe",
        "Profit Factor": "pf",
        "Trade Count": "dof",
        "SQN": "sqn",
        "Win Rate": "win_rate",
        "Expectancy Ratio": "expectancy",
        "Sortino / Sharpe": "sortino",
        "Epoch Count": "convergence",
        "Deflated Sharpe": "dsr",
        "Profit Concentration": "profit_concentration",
        "Param Stability": "sensitivity",
        "Monte Carlo": "mc",
        "Degrees of Freedom": "dof",
        "Regime Consistency": "embargo",
        "Best vs Median": "expected_max_sharpe",
        "Return Distribution": "skewness",
        "Result Dispersion": "expected_max_sharpe",
        "Overfitting Alerts": "dsr",
    }

    def _render_scorecard(self, criteria: list[dict]) -> str:
        L = self._L
        colors = {
            "g": "#22c55e",
            "y": "#eab308",
            "r": "#ef4444",
        }
        rows = ""
        for c in criteria:
            clr = colors[c["lvl"]]
            name_en, name_fr = c["name"]
            bar_w = max(c.get("pct", 0), 2)
            ref = c.get("ref", "")
            ref_html = ""
            if ref:
                ref_html = f'<span style="color:#666;font-size:0.75em"> ({ref})</span>'
            anchor = c.get("anchor", "")
            slug = self._SC_SLUGS.get(name_en, "")
            name_txt = self._tip(slug, L(name_en, name_fr)) if slug else L(name_en, name_fr)
            if anchor:
                name_txt = (
                    f'<a href="#{anchor}" style="color:#ccc;'
                    "text-decoration:none;border-bottom:"
                    f'1px dotted #555">{name_txt}</a>'
                )
            rows += (
                '<div style="display:grid;'
                "grid-template-columns:140px 70px 1fr;"
                "gap:8px;align-items:center;padding:5px 0;"
                'border-bottom:1px solid #1a1a2e">'
                f'<div style="font-weight:bold;color:#ccc">'
                f"{name_txt}</div>"
                f'<div style="color:{clr};font-weight:bold;'
                f'text-align:right">{c["val"]}</div>'
                '<div style="display:flex;align-items:center;'
                'gap:8px">'
                '<div style="flex:0 0 120px;height:10px;'
                "background:#1a1a2e;border-radius:5px;"
                'overflow:hidden">'
                f'<div style="width:{bar_w}%;height:100%;'
                f'background:{clr};border-radius:5px">'
                "</div></div>"
                f'<span style="font-size:0.82em;color:#999">'
                f"{L(c['en'], c['fr'])}"
                f"{ref_html}</span>"
                "</div></div>"
            )
        return f'<div style="margin:10px 0">{rows}</div>'

    _REC_TABLE: list[tuple[str, str, str, str]] = [
        (
            "Profit",
            "r_only",
            "Revisit strategy logic, expand timerange, or try a different loss function.",
            "Revoir la logique, élargir le timerange, ou essayer une autre fonction de loss.",
        ),
        (
            "Max Drawdown",
            "both",
            "Switch to <code>CalmarHyperOptLoss</code> or "
            "<code>MaxDrawDownHyperOptLoss</code> to penalize "
            "drawdown. Tighten stoploss space.",
            "Passer à <code>CalmarHyperOptLoss</code> ou "
            "<code>MaxDrawDownHyperOptLoss</code> pour "
            "pénaliser le drawdown. Resserrer le stoploss.",
        ),
        (
            "Sharpe Ratio",
            "r_only",
            "Sharpe > 5 = curve fit. Reduce epochs, add constraints, check for bugs.",
            "Sharpe > 5 = curve fit. Réduire les epochs, "
            "ajouter des contraintes, chercher les bugs.",
        ),
        (
            "Trade Count",
            "both",
            "Increase <code>--hyperopt-min-trades</code> or extend the timerange for more data.",
            "Augmenter <code>--hyperopt-min-trades</code> ou étendre le timerange.",
        ),
        (
            "Profit Concentration",
            "both",
            "Profit depends on a few lucky trades. Diversify pairs or tighten entry logic.",
            "Le profit repose sur quelques trades chanceux."
            " Diversifier les paires ou resserrer les entrées.",
        ),
        (
            "Param Stability",
            "both",
            "Unstable params = overfitting. Reduce param "
            "ranges, use fewer spaces, or cap at 200-300.",
            "Params instables = surapprentissage. Réduire "
            "les ranges, moins d'espaces, ou max 200-300.",
        ),
        (
            "Deflated Sharpe",
            "both",
            "Sharpe may be inflated by multiple trials. "
            "Run fewer epochs or validate with walk-forward.",
            "Le Sharpe est possiblement gonflé par le nombre d'essais. Réduire ou walk-forward.",
        ),
        (
            "Epoch Count",
            "both",
            "Too many epochs. Run 10-20x 200 epochs with different seeds and average.",
            "Trop d'epochs. Lancer 10-20x 200 epochs avec des seeds différentes et moyenner.",
        ),
        (
            "Monte Carlo",
            "both",
            "Profit is fragile to trade ordering. Increase trade count or diversify pairs.",
            "Le profit est fragile à l'ordre des trades. "
            "Augmenter le nombre de trades ou diversifier.",
        ),
        (
            "Regime Consistency",
            "both",
            "Performance degrades in second half. Validate on a holdout or run walk-forward.",
            "La performance se dégrade en 2e moitié. Valider sur un holdout ou walk-forward.",
        ),
        (
            "Best vs Median",
            "both",
            "Best epoch is an outlier — select on robustness (median top-10), not peak.",
            "Le meilleur epoch est un outlier — sélectionner sur la robustesse, pas le pic.",
        ),
        (
            "Return Distribution",
            "both",
            "Negative skew or fat tails — Sharpe understates risk. Size as if Sharpe were half.",
            "Asymétrie nég. ou queues épaisses — le Sharpe sous-estime le risque. Sizer à moitié.",
        ),
        (
            "Sortino / Sharpe",
            "both",
            "Sortino/Sharpe divergence hides tail risk. Don't trust Sortino alone for sizing.",
            "La divergence Sortino/Sharpe masque le risque "
            "de queue. Ne pas se fier au Sortino seul.",
        ),
        (
            "Overfitting Alerts",
            "both",
            "Multiple overfitting signals. Reduce complexity: fewer params, epochs, wider range.",
            "Plusieurs signaux de surapprentissage. Moins "
            "de params, d'epochs, timerange plus large.",
        ),
        (
            "Degrees of Freedom",
            "both",
            "Not enough trades per parameter. Extend "
            "timerange, reduce spaces, or raise min-trades.",
            "Pas assez de trades/param. Étendre le timerange, réduire les espaces, ou min-trades.",
        ),
    ]

    @staticmethod
    def _build_recommendations(criteria: list[dict], strat_file: str) -> list[tuple[str, str]]:
        names_r = {c["name"][0] for c in criteria if c["lvl"] == "r"}
        names_y = {c["name"][0] for c in criteria if c["lvl"] == "y"}
        flagged = names_r | names_y
        recs: list[tuple[str, str]] = []
        for name, mode, en, fr in HyperoptHTMLReport._REC_TABLE:
            if mode == "r_only" and name in names_r:
                recs.append((en, fr))
            elif mode == "both" and name in flagged:
                recs.append((en, fr))
        if not names_r:
            recs.append(
                (
                    f"Copy best params to {strat_file} and "
                    "validate with a <strong>live dry-run"
                    "</strong> at minimal size before deploying.",
                    f"Copier les params dans {strat_file} et "
                    "valider en <strong>dry-run live</strong> à "
                    "taille minimale avant de déployer.",
                )
            )
        return recs

    @staticmethod
    def _section_glossary() -> str:
        rows = ""
        L = HyperoptHTMLReport._L
        for slug, entry in sorted(METRIC_GLOSSARY.items()):
            abbrev = html.escape(entry.get("abbrev", slug))
            name = html.escape(entry.get("name", ""))
            one_liner_en = html.escape(entry.get("one_liner", ""))
            expl_en = html.escape(entry.get("explanation", ""))
            expl_fr = html.escape(entry.get("explanation_fr", ""))
            desc = L(one_liner_en, expl_fr or one_liner_en)
            tooltip_body = ""
            if expl_en or expl_fr:
                tip_en = expl_en or one_liner_en
                tip_fr = expl_fr or tip_en
                tooltip_body = (
                    f'<span class="tip-text">'
                    f'<span lang="en">{tip_en}</span>'
                    f'<span lang="fr">{tip_fr}</span>'
                    f"</span>"
                )
            rows += (
                f"<tr><td><strong>"
                f'<span class="tooltip">{abbrev}'
                f"{tooltip_body}</span>"
                f"</strong></td>"
                f"<td>{name}</td>"
                f"<td>{desc}</td></tr>\n"
            )
        return (
            '<div class="section">'
            "<details><summary><h2 style='display:inline'>"
            + L("Glossary", "Glossaire")
            + "</h2></summary><div>"
            "<table><tr><th>"
            + L("Abbrev", "Abrév.")
            + "</th><th>"
            + L("Full Name", "Nom complet")
            + "</th><th>Description</th></tr>"
            f"{rows}"
            "</table></div></details></div>"
        )

    # ------------------------------------------------------------------
    # New features: Monte Carlo, Sensitivity, Regime, DoF, Return/DD
    # ------------------------------------------------------------------

    def _section_monte_carlo(self) -> str:
        mc = self.d.get("monte_carlo")
        if not mc:
            return ""
        L = self._L
        prob = mc["prob_positive"]
        prob_c = "#22c55e" if prob >= 80 else ("#eab308" if prob >= 60 else "#ef4444")
        return (
            '<div class="section" id="sec-monte-carlo"><h2>'
            + L(
                "Monte Carlo Confidence",
                "Confiance Monte Carlo",
            )
            + "</h2>"
            + self._desc(
                f"{mc['n_simulations']} simulations reshuffling "
                f"{mc['n_trades']} trades to estimate profit "
                "distribution under different trade orderings.",
                f"{mc['n_simulations']} simulations remixant "
                f"{mc['n_trades']} trades pour estimer la "
                "distribution du profit sous différents "
                "ordres de trades.",
            )
            + self._svg_monte_carlo(mc)
            + '<div style="display:grid;'
            "grid-template-columns:repeat(4,1fr);"
            'gap:8px;margin:8px 0">'
            + self._mc_card("P5", f"{mc['p5']:+.1f}%", "#ef4444")
            + self._mc_card("P25", f"{mc['p25']:+.1f}%", "#eab308")
            + self._mc_card("P50", f"{mc['p50']:+.1f}%", "#888")
            + self._mc_card("P95", f"{mc['p95']:+.1f}%", "#22c55e")
            + "</div>"
            + '<div style="text-align:center;margin:8px 0">'
            '<span style="font-size:1.1em">'
            + L("Probability profit > 0", "Probabilité profit > 0")
            + f": <strong style='color:{prob_c}'>"
            f"{prob}%</strong></span></div>" + self._advisory_monte_carlo(mc) + "</div>"
        )

    def _advisory_monte_carlo(self, mc: dict) -> str:
        prob = mc["prob_positive"]
        p5 = mc["p5"]
        if prob < 60:
            return self._advisory(
                "bad",
                f"Only {prob}% probability of profit — the strategy likely has no real edge.",
                f"Seulement {prob}% de probabilite de "
                "profit — la strategie n'a probablement "
                "pas de vrai edge.",
            )
        if p5 < 0:
            return self._advisory(
                "warn",
                f"P5 is {p5:+.1f}% — in 5% of scenarios you would lose money. Size accordingly.",
                f"P5 est {p5:+.1f}% — dans 5% des "
                "scenarios vous perdez. Dimensionnez en "
                "consequence.",
            )
        return self._advisory(
            "good",
            f"{prob}% probability of profit, P5 at {p5:+.1f}% — strong Monte Carlo confidence.",
            f"{prob}% de probabilite de profit, P5 a {p5:+.1f}% — confiance Monte Carlo solide.",
        )

    @staticmethod
    def _mc_card(label: str, value: str, c: str) -> str:
        return (
            f'<div style="background:#0f0f23;'
            f"border-radius:6px;padding:8px;"
            f'text-align:center;border:1px solid #2a2a4a">'
            f'<div style="color:#888;font-size:0.8em">'
            f"{label}</div>"
            f'<div style="color:{c};font-size:1.1em;'
            f'font-weight:bold">{value}</div></div>'
        )

    def _svg_monte_carlo(self, mc: dict) -> str:
        w, h = 900, 200
        pad_l, pad_r, pad_t, pad_b = 50, 20, 20, 30
        pw = w - pad_l - pad_r
        ph = h - pad_t - pad_b
        vals = [mc["p5"], mc["p25"], mc["p50"], mc["p75"], mc["p95"]]
        lo = min(vals + [0])
        hi = max(vals + [0])
        rng = hi - lo if hi > lo else 1

        def _x(v: float) -> float:
            return pad_l + (v - lo) / rng * pw

        zero_x = _x(0)
        body = f'<rect x="{pad_l}" y="{pad_t}" width="{pw}" height="{ph}" fill="#1a1a2e" rx="4"/>'
        body += (
            f'<line x1="{zero_x}" y1="{pad_t}" '
            f'x2="{zero_x}" y2="{pad_t + ph}" '
            f'stroke="#555" stroke-dasharray="4"/>'
        )
        cy = pad_t + ph // 2
        x5, x95 = _x(mc["p5"]), _x(mc["p95"])
        x25, x75 = _x(mc["p25"]), _x(mc["p75"])
        x50 = _x(mc["p50"])
        body += f'<line x1="{x5}" y1="{cy}" x2="{x95}" y2="{cy}" stroke="#555" stroke-width="2"/>'
        body += (
            f'<rect x="{x25}" y="{cy - 20}" '
            f'width="{x75 - x25}" height="40" '
            f'fill="#16213e" stroke="#00d4ff" rx="4"/>'
        )
        body += (
            f'<line x1="{x50}" y1="{cy - 20}" '
            f'x2="{x50}" y2="{cy + 20}" '
            f'stroke="#eab308" stroke-width="2"/>'
        )
        for lbl, xv, v in [
            ("P5", x5, mc["p5"]),
            ("P25", x25, mc["p25"]),
            ("P50", x50, mc["p50"]),
            ("P75", x75, mc["p75"]),
            ("P95", x95, mc["p95"]),
        ]:
            body += (
                f'<circle cx="{xv}" cy="{cy}" r="4" '
                f'fill="#00d4ff"/>'
                f'<text x="{xv}" y="{pad_t + ph + 16}" '
                f'fill="#888" font-size="10" '
                f'text-anchor="middle">'
                f"{lbl}: {v:+.1f}%</text>"
            )
        return (
            f'<svg viewBox="0 0 {w} {h}" '
            f'width="{w}" xmlns="http://www.w3.org/2000/svg">'
            f"{body}</svg>"
        )

    def _section_sensitivity_grid(self) -> str:
        grids = self.d.get("sensitivity_grid") or []
        if not grids:
            return ""
        L = self._L
        parts = (
            '<div class="section" id="sec-sensitivity"><h2>'
            + L(
                "Parameter Sensitivity Heatmaps",
                "Heatmaps de sensibilité des paramètres",
            )
            + "</h2>"
            + self._desc(
                "2D heatmaps showing average loss for each "
                "pair of parameters. Green = low loss (good), "
                "Red = high loss (bad). Flat colors mean "
                "the parameter pair has low interaction.",
                "Heatmaps 2D montrant la loss moyenne pour "
                "chaque paire de paramètres. Vert = loss "
                "basse (bon), Rouge = loss haute (mauvais). "
                "Couleurs uniformes = faible interaction.",
            )
        )
        for g in grids:
            parts += self._svg_sensitivity_heatmap(g)
        parts += self._advisory_sensitivity(grids)
        return parts + "</div>"

    def _advisory_sensitivity(self, grids: list) -> str:
        n_flat = 0
        for g in grids:
            vals = [v for row in g["grid"] for v in row if v is not None]
            if not vals:
                continue
            rng = max(vals) - min(vals)
            mean_v = sum(vals) / len(vals)
            if mean_v and rng / abs(mean_v) < 0.1:
                n_flat += 1
        if n_flat == len(grids) and grids:
            return self._advisory(
                "good",
                "All heatmaps show flat loss surfaces — "
                "the parameters have weak interactions, "
                "which means robust optimization.",
                "Toutes les heatmaps montrent des surfaces "
                "plates — les parametres ont de faibles "
                "interactions, optimisation robuste.",
            )
        if n_flat == 0 and grids:
            return self._advisory(
                "warn",
                "All heatmaps show significant loss "
                "variation — parameters interact strongly. "
                "The optimal region may be narrow.",
                "Toutes les heatmaps montrent des "
                "variations significatives — les parametres "
                "interagissent fortement. La region optimale "
                "peut etre etroite.",
            )
        return self._advisory(
            "good",
            f"{n_flat}/{len(grids)} parameter pairs show "
            "flat surfaces. Mixed interactions are normal.",
            f"{n_flat}/{len(grids)} paires de parametres "
            "ont des surfaces plates. Des interactions "
            "mixtes sont normales.",
        )

    @staticmethod
    def _svg_sensitivity_heatmap(g: dict) -> str:
        n = g["n_bins"]
        cell = 52
        pad_l, pad_t = 90, 40
        a_range = g.get("a_range", [0, 1])
        b_range = g.get("b_range", [0, 1])
        grid = g["grid"]
        all_vals = [v for row in grid for v in row if v is not None]
        if not all_vals:
            return ""
        lo, hi = min(all_vals), max(all_vals)
        rng = hi - lo if hi > lo else 1
        legend_w, legend_pad = 20, 24
        w = pad_l + n * cell + legend_pad + legend_w + 40
        h = pad_t + n * cell + 50

        body = ""
        for ai in range(n):
            for bi in range(n):
                v = grid[ai][bi]
                x = pad_l + bi * cell
                y = pad_t + ai * cell
                if v is None:
                    c = "#2a2a4a"
                else:
                    t = (v - lo) / rng
                    r = int(34 + t * (239 - 34))
                    g_c = int(197 - t * (197 - 68))
                    b_c = int(94 - t * (94 - 68))
                    c = f"rgb({r},{g_c},{b_c})"
                body += (
                    f'<rect x="{x}" y="{y}" '
                    f'width="{cell}" height="{cell}" '
                    f'fill="{c}" stroke="#1a1a2e"/>'
                )
                if v is not None:
                    bright = 0.299 * r + 0.587 * g_c + 0.114 * b_c
                    tc = "#111" if bright > 140 else "#e0e0e0"
                    vt = f"{v:.2f}" if abs(v) < 10 else f"{v:.1f}"
                    body += (
                        f'<text x="{x + cell // 2}" '
                        f'y="{y + cell // 2 + 4}" '
                        f'fill="{tc}" font-size="10" '
                        f'text-anchor="middle">{vt}</text>'
                    )

        pa, pb = g["param_a"], g["param_b"]
        a_lo, a_hi = a_range
        b_lo, b_hi = b_range
        a_bw = (a_hi - a_lo) / n if a_hi != a_lo else 1
        b_bw = (b_hi - b_lo) / n if b_hi != b_lo else 1

        def _fmt(v: float) -> str:
            if abs(v) >= 100:
                return f"{v:.0f}"
            if abs(v) >= 1:
                return f"{v:.1f}"
            return f"{v:.2f}"

        for bi in range(n):
            mid = b_lo + (bi + 0.5) * b_bw
            x = pad_l + bi * cell + cell // 2
            body += (
                f'<text x="{x}" '
                f'y="{pad_t + n * cell + 14}" '
                f'fill="#999" font-size="9" '
                f'text-anchor="middle">'
                f"{_fmt(mid)}</text>"
            )
        for ai in range(n):
            mid = a_lo + (ai + 0.5) * a_bw
            y = pad_t + ai * cell + cell // 2 + 3
            body += (
                f'<text x="{pad_l - 6}" y="{y}" '
                f'fill="#999" font-size="9" '
                f'text-anchor="end">{_fmt(mid)}</text>'
            )

        body += (
            f'<text x="{pad_l + n * cell // 2}" '
            f'y="{pad_t + n * cell + 36}" fill="#bbb" '
            f'font-size="12" font-weight="bold" '
            f'text-anchor="middle">{pb}</text>'
        )
        cy = pad_t + n * cell // 2
        body += (
            f'<text x="14" y="{cy}" fill="#bbb" '
            f'font-size="12" font-weight="bold" '
            f'text-anchor="middle" '
            f'transform="rotate(-90, 14, {cy})">'
            f"{pa}</text>"
        )

        lx = pad_l + n * cell + legend_pad
        gh = n * cell
        for si in range(gh):
            t = si / gh
            r = int(34 + t * (239 - 34))
            gc = int(197 - t * (197 - 68))
            bc = int(94 - t * (94 - 68))
            body += (
                f'<rect x="{lx}" y="{pad_t + si}" '
                f'width="{legend_w}" height="1" '
                f'fill="rgb({r},{gc},{bc})"/>'
            )
        body += (
            f'<text x="{lx + legend_w + 4}" '
            f'y="{pad_t + 8}" fill="#999" '
            f'font-size="9">{_fmt(lo)}</text>'
            f'<text x="{lx + legend_w + 4}" '
            f'y="{pad_t + gh}" fill="#999" '
            f'font-size="9">{_fmt(hi)}</text>'
            f'<text x="{lx + legend_w // 2}" '
            f'y="{pad_t - 6}" fill="#888" '
            f'font-size="9" text-anchor="middle">'
            f"loss</text>"
        )

        return (
            f'<svg viewBox="0 0 {w} {h}" '
            f'width="{w}" '
            f'style="display:inline-block;margin:4px" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f"{body}</svg>"
        )

    def _section_regime_analysis(self) -> str:
        ra = self.d.get("regime_analysis")
        if not ra:
            return ""
        L = self._L
        f = ra["first_half"]
        s = ra["second_half"]
        consistent = ra["consistent"]
        badge_c = "#22c55e" if consistent else "#ef4444"
        badge_t = L("Consistent", "Cohérent") if consistent else L("Inconsistent", "Incohérent")

        def _row(label, d):
            pc = "#22c55e" if d["profit_pct"] > 0 else "#ef4444"
            return (
                f"<tr><td><strong>{label}</strong></td>"
                f"<td>{d['trades']}</td>"
                f'<td style="color:{pc}">'
                f"{d['profit_pct']:+.2f}%</td>"
                f"<td>{d['win_rate']:.1f}%</td>"
                f"<td>{d['avg_profit']:+.2f}%</td></tr>"
            )

        return (
            '<div class="section" id="sec-regime"><h2>'
            + L(
                "Regime Analysis",
                "Analyse par régime",
            )
            + f' <span style="font-size:0.7em;color:{badge_c}">'
            f"{badge_t}</span>"
            + "</h2>"
            + self._desc(
                "Performance split by time period. "
                "Consistent results across both halves "
                "suggest the strategy adapts to different "
                "market conditions.",
                "Performance divisée par période. Des "
                "résultats cohérents entre les deux moitiés "
                "suggèrent que la stratégie s'adapte aux "
                "différentes conditions de marché.",
            )
            + "<table><tr>"
            + f"<th>{L('Period', 'Période')}</th>"
            + f"<th>{L('Trades', 'Trades')}</th>"
            + f"<th>{L('Profit', 'Profit')}</th>"
            + f"<th>{L('Win Rate', 'Win Rate')}</th>"
            + f"<th>{L('Avg Profit', 'Profit moy.')}</th>"
            + "</tr>"
            + _row(L("First half", "Première moitié"), f)
            + _row(L("Second half", "Seconde moitié"), s)
            + "</table>"
            + self._advisory_regime(consistent, f, s)
            + "</div>"
        )

    def _advisory_regime(self, consistent: bool, fh: dict, sh: dict) -> str:
        if not consistent:
            return self._advisory(
                "bad",
                "Halves show inconsistent performance — "
                "the strategy may be curve-fitted to one "
                "market regime.",
                "Les moities montrent des performances "
                "incoherentes — la strategie est peut-etre "
                "sur-ajustee a un seul regime.",
            )
        both_pos = fh["profit_pct"] > 0 and sh["profit_pct"] > 0
        if both_pos:
            return self._advisory(
                "good",
                "Both halves profitable with consistent metrics — the edge appears regime-robust.",
                "Les deux moities rentables avec des "
                "metriques coherentes — l'edge semble "
                "robuste aux regimes.",
            )
        return self._advisory(
            "warn",
            "Consistent direction but one half is negative — limited confidence in the edge.",
            "Direction coherente mais une moitie negative — confiance limitee dans l'edge.",
        )

    def _section_dof_traffic_light(self) -> str:
        dof = self.d.get("dof_analysis")
        if not dof:
            return ""
        L = self._L
        level = dof["level"]
        colors = {
            "green": "#22c55e",
            "yellow": "#eab308",
            "orange": "#f97316",
            "red": "#ef4444",
        }
        c = colors.get(level, "#888")

        return (
            '<div class="section" id="sec-dof"><h2>'
            + L(
                "Degrees of Freedom",
                "Degrés de liberté",
            )
            + "</h2>"
            + self._desc(
                "Ratio of trades to optimized parameters. "
                "More trades per parameter = more "
                "statistically reliable results.",
                "Ratio trades / paramètres optimisés. "
                "Plus de trades par paramètre = résultats "
                "plus fiables statistiquement.",
            )
            + self._svg_dof_gauge(dof, c)
            + self._advisory_dof(level, dof["ratio"])
            + "</div>"
        )

    def _advisory_dof(self, level: str, ratio: float) -> str:
        if level in ("red", "orange"):
            return self._advisory(
                "bad",
                f"Trades/params ratio {ratio:.1f}x is too "
                "low — results are statistically unreliable. "
                "Reduce parameter count or extend data.",
                f"Ratio trades/params {ratio:.1f}x trop "
                "bas — resultats non fiables. Reduisez les "
                "parametres ou allongez les donnees.",
            )
        if level == "yellow":
            return self._advisory(
                "warn",
                f"Ratio {ratio:.1f}x is marginal. Aim for "
                ">15x for reliable conclusions "
                "(Bailey & Lopez de Prado).",
                f"Ratio {ratio:.1f}x marginal. Visez >15x pour des conclusions fiables.",
            )
        return self._advisory(
            "good",
            f"Ratio {ratio:.1f}x — sufficient degrees of freedom for statistical reliability.",
            f"Ratio {ratio:.1f}x — degres de liberte suffisants pour la fiabilite statistique.",
        )

    @staticmethod
    def _svg_dof_gauge(dof: dict, c: str) -> str:
        w, h = 400, 80
        ratio = dof["ratio"]
        max_r = 50
        fill_w = min(ratio / max_r, 1.0) * 340
        thresholds = [
            (5, "#ef4444"),
            (10, "#f97316"),
            (15, "#eab308"),
            (30, "#22c55e"),
        ]
        body = '<rect x="30" y="20" width="340" height="30" fill="#2a2a4a" rx="4"/>'
        body += (
            f'<rect x="30" y="20" width="{fill_w}" height="30" fill="{c}" rx="4" opacity="0.8"/>'
        )
        for thresh, tc in thresholds:
            tx = 30 + min(thresh / max_r, 1.0) * 340
            body += (
                f'<line x1="{tx}" y1="18" '
                f'x2="{tx}" y2="52" '
                f'stroke="{tc}" stroke-width="1" '
                f'opacity="0.5"/>'
                f'<text x="{tx}" y="65" fill="#555" '
                f'font-size="9" text-anchor="middle">'
                f"{thresh}</text>"
            )
        body += (
            f'<text x="200" y="40" fill="#fff" '
            f'font-size="13" font-weight="bold" '
            f'text-anchor="middle">'
            f"{dof['n_trades']} trades / "
            f"{dof['n_params']} params = "
            f"{ratio:.1f}x"
            f" ({dof['label']})</text>"
        )
        return (
            f'<svg viewBox="0 0 {w} {h}" '
            f'width="{w}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f"{body}</svg>"
        )

    def _section_return_vs_dd(self) -> str:
        points = self.d.get("return_vs_dd") or []
        if len(points) < 3:
            return ""
        L = self._L
        return (
            '<div class="section" id="sec-return-dd"><h2>'
            + L(
                "Return vs Drawdown",
                "Rendement vs Drawdown",
            )
            + "</h2>"
            + self._desc(
                "Each dot is an epoch. Top-left (high "
                "return, low drawdown) is ideal. Epochs "
                "clustered in the green zone indicate "
                "robust parameter spaces.",
                "Chaque point est un epoch. Haut-gauche "
                "(rendement élevé, drawdown faible) est "
                "idéal. Des epochs groupés dans la zone "
                "verte indiquent des espaces paramétriques "
                "robustes.",
            )
            + self._svg_return_vs_dd(points)
            + self._advisory_return_dd(points)
            + "</div>"
        )

    def _advisory_return_dd(self, points: list[dict]) -> str:
        good = sum(1 for p in points if p["profit_pct"] > 0 and p["dd_pct"] < 25)
        n = len(points)
        ratio = good / n if n else 0
        if ratio > 0.5:
            return self._advisory(
                "good",
                f"{good}/{n} epochs in the green zone "
                "(profit>0, DD<25%) — the parameter space "
                "contains many viable solutions.",
                f"{good}/{n} epochs en zone verte "
                "(profit>0, DD<25%) — l'espace "
                "parametrique contient de nombreuses "
                "solutions viables.",
            )
        if ratio > 0.2:
            return self._advisory(
                "warn",
                f"Only {good}/{n} epochs in the green zone. The viable parameter region is narrow.",
                f"Seulement {good}/{n} epochs en zone "
                "verte. La region parametrique viable "
                "est etroite.",
            )
        return self._advisory(
            "bad",
            f"Very few epochs ({good}/{n}) in the green zone. Most parameter combinations fail.",
            f"Tres peu d'epochs ({good}/{n}) en zone verte. La plupart des combinaisons echouent.",
        )

    @staticmethod
    def _svg_return_vs_dd(points: list[dict]) -> str:
        w, h = 900, 350
        pad_l, pad_r, pad_t, pad_b = 60, 20, 20, 40
        pw = w - pad_l - pad_r
        ph = h - pad_t - pad_b
        dd_vals = [p["dd_pct"] for p in points]
        pr_vals = [p["profit_pct"] for p in points]
        dd_lo = 0
        dd_hi = max(dd_vals) * 1.1 if dd_vals else 30
        pr_lo = min(min(pr_vals), 0) * 1.1 if pr_vals else -5
        pr_hi = max(pr_vals) * 1.1 if pr_vals else 20
        dd_rng = dd_hi - dd_lo if dd_hi > dd_lo else 1
        pr_rng = pr_hi - pr_lo if pr_hi > pr_lo else 1

        body = f'<rect x="{pad_l}" y="{pad_t}" width="{pw}" height="{ph}" fill="#1a1a2e" rx="4"/>'
        x25 = pad_l + (25 - dd_lo) / dd_rng * pw
        y0 = pad_t + ph - (0 - pr_lo) / pr_rng * ph
        gx = min(x25, pad_l + pw)
        gy = min(y0, pad_t + ph)
        if gx > pad_l and gy > pad_t:
            body += (
                f'<rect x="{pad_l}" y="{pad_t}" '
                f'width="{gx - pad_l}" '
                f'height="{gy - pad_t}" '
                f'fill="#22c55e" opacity="0.08"/>'
            )

        best_loss = min((p["loss"] for p in points), default=0)
        for p in points:
            x = pad_l + (p["dd_pct"] - dd_lo) / dd_rng * pw
            y = pad_t + ph - (p["profit_pct"] - pr_lo) / pr_rng * ph
            is_best = p["loss"] == best_loss
            c = (
                "#00d4ff"
                if is_best
                else ("#22c55e" if p["profit_pct"] > 0 and p["dd_pct"] < 25 else "#ef4444")
            )
            r = 6 if is_best else 4
            body += (
                f'<circle cx="{x}" cy="{y}" r="{r}" '
                f'fill="{c}" opacity="0.7">'
                f"<title>Profit: {p['profit_pct']:+.1f}% "
                f"DD: {p['dd_pct']:.1f}% "
                f"Trades: {p['trades']}</title>"
                f"</circle>"
            )

        for i in range(6):
            val = pr_lo + pr_rng * i / 5
            y = pad_t + ph - (val - pr_lo) / pr_rng * ph
            body += (
                f'<text x="{pad_l - 8}" y="{y + 4}" '
                f'fill="#555" font-size="10" '
                f'text-anchor="end">{val:.0f}%</text>'
                f'<line x1="{pad_l}" y1="{y}" '
                f'x2="{pad_l + pw}" y2="{y}" '
                f'stroke="#2a2a4a" stroke-width="0.5"/>'
            )
        for i in range(6):
            val = dd_lo + dd_rng * i / 5
            x = pad_l + (val - dd_lo) / dd_rng * pw
            body += (
                f'<text x="{x}" y="{pad_t + ph + 16}" '
                f'fill="#555" font-size="10" '
                f'text-anchor="middle">{val:.0f}%</text>'
            )
        body += (
            f'<text x="{pad_l + pw // 2}" '
            f'y="{pad_t + ph + 32}" fill="#888" '
            f'font-size="11" text-anchor="middle">'
            f"Max Drawdown %</text>"
        )
        body += (
            f'<text x="14" y="{pad_t + ph // 2}" '
            f'fill="#888" font-size="11" '
            f'text-anchor="middle" '
            f'transform="rotate(-90, 14, '
            f'{pad_t + ph // 2})">'
            f"Profit %</text>"
        )
        return (
            f'<svg viewBox="0 0 {w} {h}" '
            f'width="{w}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f"{body}</svg>"
        )

    @staticmethod
    def _html_footer() -> str:
        return """
</div>
</body>
</html>"""
