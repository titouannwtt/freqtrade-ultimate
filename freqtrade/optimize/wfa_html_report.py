from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

import numpy as np

from freqtrade.optimize.wfa_glossary import (
    METRIC_GLOSSARY,
    PERCENTILE_HINT,
    PERCENTILE_HINT_FR,
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
        L = self._L
        parts = [
            self._html_header(),
            self._section_intro(),
            # -- Verdict --
            self._group_open("verdict", L("Verdict", "Verdict")),
            self._section_verdict(),
            self._section_scorecard(),
            self._group_close(),
            # -- Windows --
            self._group_open(
                "perf",
                L("Window Results", "Résultats par fenêtre"),
            ),
            self._section_window_table(),
            self._section_wfe_chart(),
            self._section_degradation(),
            self._section_holdout(),
            self._group_close(),
            # -- OOS Analysis --
            self._group_open(
                "oos",
                L(
                    "Out-of-Sample Analysis",
                    "Analyse hors-échantillon",
                ),
            ),
            self._section_oos_aggregate(),
            self._section_equity_chart(),
            self._section_concentration(),
            self._section_regime(),
            self._group_close(),
            # -- Robustness --
            self._group_open(
                "robust",
                L(
                    "Robustness Analysis",
                    "Analyse de robustesse",
                ),
            ),
            self._section_warnings(),
            self._section_monte_carlo(),
            self._section_perturbation(),
            self._section_multi_seed(),
            self._section_cpcv(),
            self._group_close(),
            # -- Parameters --
            self._group_open(
                "params",
                L(
                    "Parameter Analysis",
                    "Analyse des paramètres",
                ),
            ),
            self._section_param_stability(),
            self._section_consensus(),
            self._group_close(),
            # -- Reference --
            self._group_open(
                "ref",
                L("Reference", "Référence"),
            ),
            self._section_next_steps(),
            self._section_glossary(),
            self._group_close(),
            self._html_footer(),
        ]
        return "\n".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _group_open(cls: str, title: str) -> str:
        return f'<div class="group group-{cls}"><div class="group-title">{title}</div>'

    @staticmethod
    def _group_close() -> str:
        return "</div>"

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
        return (
            f'<span class="tooltip">{safe}'
            f'<span class="tip-text">{tip_body}</span></span>'
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
        return f"<details><summary>{summary}</summary><div>{body}</div></details>"

    @staticmethod
    def _L(en: str, fr: str) -> str:
        return f'<span lang="en">{en}</span><span lang="fr">{fr}</span>'

    def _desc(self, en: str, fr: str) -> str:
        return f'<p class="section-desc">{self._L(en, fr)}</p>'

    def _advisory(self, level: str, en: str, fr: str) -> str:
        cls = {"good": "advisory-good", "warn": "advisory-warn", "bad": "advisory-bad"}.get(
            level, "advisory-warn"
        )
        return f'<div class="advisory {cls}">{self._L(en, fr)}</div>'

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

    # ------------------------------------------------------------------
    # Structure
    # ------------------------------------------------------------------

    def _html_header(self) -> str:
        strategy = self._esc(self.d.get("strategy", ""))
        mode = self._esc(self.d.get("wf_mode", ""))
        n_win = self.d.get("n_windows", 0)
        epochs = self.d.get("epochs_per_window", 0)
        loss = self._esc(self.d.get("hyperopt_loss", ""))
        ts = self._esc(self.d.get("timestamp", ""))
        L = self._L
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>WFA Report — {strategy}</title>
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
<h1>{L("Walk-Forward Analysis", "Analyse Walk-Forward")} \
— {strategy}</h1>
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
h1 { color: #00d4ff; border-bottom: 2px solid #00d4ff;
     padding-bottom: 8px; }
h2 { color: #00d4ff; margin-top: 30px; }
h3 { color: #84cc16; margin-top: 20px; }
.meta { color: #888; font-size: 0.9em; }
table {
  width: 100%; border-collapse: collapse; margin: 10px 0;
  font-size: 0.85em;
}
th { background: #16213e; color: #00d4ff; padding: 8px;
     text-align: left; }
td { padding: 6px 8px; border-bottom: 1px solid #2a2a4a; }
tr:hover { background: #16213e; }
.grade {
  font-size: 3em; font-weight: bold; display: inline-block;
  width: 80px; height: 80px; line-height: 80px;
  text-align: center; border-radius: 12px; margin-right: 16px;
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
.badge-stable { color: #22c55e; font-size: 0.82em; }
.badge-marginal { color: #eab308; font-size: 0.82em; }
.badge-unstable { color: #ef4444; font-size: 0.82em; }
svg { background: #0f0f23; border-radius: 8px; margin: 10px 0; }
.section { margin-bottom: 24px; }
.kv { display: inline-block; margin-right: 20px;
      margin-bottom: 10px; }
.kv-label { color: #888; font-size: 0.85em; }
.kv-value { font-size: 1.1em; font-weight: bold;
            font-family: monospace; }
.intro {
  background: #16213e; padding: 16px; border-radius: 8px;
  margin: 16px 0; line-height: 1.6; font-size: 0.9em;
}
.next-steps {
  background: #1a2744; padding: 16px; border-radius: 8px;
  border-left: 4px solid #00d4ff; margin: 16px 0;
  line-height: 1.5;
}
.explain-box {
  background: #16213e; padding: 12px 16px; border-radius: 6px;
  margin: 10px 0; font-size: 0.88em; line-height: 1.6;
  color: #bbb;
}
.tooltip { position: relative; cursor: help;
           border-bottom: 1px dotted #00d4ff; }
.tooltip .tip-text {
  visibility: hidden; position: absolute; bottom: 125%;
  left: 50%; transform: translateX(-50%);
  background: #16213e; color: #e0e0e0;
  padding: 8px 12px; border-radius: 6px; font-size: 0.8em;
  width: 280px; z-index: 10; border: 1px solid #2a2a4a;
  white-space: normal; line-height: 1.4;
}
.tooltip:hover .tip-text { visibility: visible; }
details { margin: 8px 0; }
details summary { cursor: pointer; color: #00d4ff;
                  font-size: 0.9em; }
details summary:hover { text-decoration: underline; }
details > div {
  padding: 8px 0 8px 16px; color: #bbb;
  font-size: 0.85em; line-height: 1.6;
}
.section-desc {
  color: #888; font-size: 0.82em; margin: 2px 0 10px 0;
  font-style: italic; line-height: 1.4;
}
span[lang="fr"] { display: none; }
html:lang(fr) span[lang="en"] { display: none; }
html:lang(fr) span[lang="fr"] { display: inline; }
.lang-bar {
  position: fixed; top: 10px; right: 20px; z-index: 100;
  background: #16213e; border: 1px solid #2a2a4a;
  border-radius: 6px; padding: 4px 8px; font-size: 0.85em;
}
.lang-bar select {
  background: #0f0f23; color: #e0e0e0;
  border: 1px solid #2a2a4a; border-radius: 4px;
  padding: 2px 6px; font-size: 0.9em; cursor: pointer;
}
.mini-section {
  background: #16213e; padding: 12px 16px; border-radius: 6px;
  margin: 10px 0; font-size: 0.88em; line-height: 1.6;
}
.badge-inline { font-size: 0.78em; margin-left: 6px; }
.warn-box {
  background: #1a2744; padding: 10px 14px; border-radius: 6px;
  margin: 8px 0; line-height: 1.5;
}
.group {
  border-left: 4px solid var(--gc, #2a2a4a);
  margin: 32px 0; padding: 0 0 0 16px;
}
.group-title {
  font-size: 1.15em; font-weight: bold; margin: 0 0 4px 0;
  letter-spacing: 0.5px;
}
.group-verdict { --gc: #00d4ff; }
.group-verdict .group-title { color: #00d4ff; }
.group-perf { --gc: #22c55e; }
.group-perf .group-title { color: #22c55e; }
.group-oos { --gc: #84cc16; }
.group-oos .group-title { color: #84cc16; }
.group-robust { --gc: #eab308; }
.group-robust .group-title { color: #eab308; }
.group-params { --gc: #a855f7; }
.group-params .group-title { color: #a855f7; }
.group-diag { --gc: #f97316; }
.group-diag .group-title { color: #f97316; }
.group-ref { --gc: #888; }
.group-ref .group-title { color: #888; }
.metric-card {
  display: inline-block; background: #16213e;
  border-radius: 8px; padding: 12px 16px;
  margin: 4px; min-width: 140px; text-align: center;
}
.metric-card .mc-label { color: #888; font-size: 0.8em; }
.metric-card .mc-value {
  font-size: 1.3em; font-weight: bold; margin: 4px 0;
  font-family: monospace;
}
.metric-card .mc-badge { font-size: 0.75em; }
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
    # Intro
    # ------------------------------------------------------------------

    def _section_intro(self) -> str:
        L = self._L
        what_en = (
            "Walk-forward analysis is the gold standard for "
            "validating trading strategies. Instead of optimizing "
            "on all data and then testing on the same data "
            "(overfitting), it splits history into N sequential "
            "windows. For each window: optimize on training data, "
            "then test on the unseen test period that follows. "
            "This mimics real trading — you always trade with "
            "parameters optimized on past data."
        )
        what_fr = (
            "L'analyse walk-forward est le standard pour valider "
            "les stratégies de trading. Au lieu d'optimiser sur "
            "toutes les données puis tester sur les mêmes "
            "(surapprentissage), elle découpe l'historique en N "
            "fenêtres séquentielles. Pour chaque fenêtre : "
            "optimiser sur les données d'entraînement, puis "
            "tester sur la période non vue qui suit. Cela simule "
            "le trading réel — on trade toujours avec des "
            "paramètres optimisés sur le passé."
        )
        how_en = (
            "<strong>Verdict</strong> = overall grade (A-F) with "
            "pass/fail checklist based on Pardo, Lopez de Prado, "
            "Carver, Chan, Davey, Van Tharp. "
            "<strong>Windows</strong> = per-period results — look "
            "for consistency, not one lucky window. "
            "<strong>OOS Analysis</strong> = concatenated "
            "out-of-sample metrics. "
            "<strong>Robustness</strong> = Monte Carlo, regime, "
            "perturbation, multi-seed, CPCV tests. "
            "<strong>Parameters</strong> = stability and consensus "
            "across windows. "
            "Hover over <u>underlined terms</u> for definitions."
        )
        how_fr = (
            "<strong>Verdict</strong> = note globale (A-F) avec "
            "checklist basée sur Pardo, Lopez de Prado, Carver, "
            "Chan, Davey, Van Tharp. "
            "<strong>Fenêtres</strong> = résultats par période — "
            "chercher la consistance, pas une fenêtre chanceuse. "
            "<strong>Analyse OOS</strong> = métriques "
            "hors-échantillon concaténées. "
            "<strong>Robustesse</strong> = Monte Carlo, régimes, "
            "perturbation, multi-seed, CPCV. "
            "<strong>Paramètres</strong> = stabilité et consensus "
            "entre fenêtres. "
            "Survolez les <u>termes soulignés</u> pour les "
            "définitions."
        )
        embargo = self.d.get("embargo_days", 0)
        ratio = self.d.get("train_ratio", 0.75)
        return f"""
<div class="intro">
<strong>{L("What is this?", "Qu'est-ce que c'est ?")}</strong>
{L(what_en, what_fr)}
<br><br>
<strong>{L("How to read:", "Comment lire :")}</strong>
{L(how_en, how_fr)}
<br><br>
<em style="color:#888">{
            L(
                f"Train/test ratio: {ratio:.0%}/{1 - ratio:.0%}. "
                f"Embargo: {embargo} days between train and test "
                f"to prevent information leakage (Lopez de Prado).",
                f"Ratio train/test : {ratio:.0%}/{1 - ratio:.0%}. "
                f"Embargo : {embargo} jours entre train et test "
                f"pour empêcher les fuites d'information "
                f"(Lopez de Prado).",
            )
        }</em>
</div>"""

    # ------------------------------------------------------------------
    # Verdict
    # ------------------------------------------------------------------

    def _section_verdict(self) -> str:
        L = self._L
        verdict = self.d.get("verdict", {})
        grade = verdict.get("grade", "?")
        checks = verdict.get("checks", [])
        color = GRADE_COLORS.get(grade, "#888")
        label_en, label_fr = WFAHTMLReport._verdict_label(grade)
        label = self._L(label_en, label_fr)
        rows = ""
        for item in checks:
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                _, ok, desc = item[0], item[1], item[2]
            else:
                continue
            cls = "check-pass" if ok else "check-fail"
            mark = "&#10003;" if ok else "&#10007;"
            rows += f'<tr><td class="{cls}">{mark}</td><td>{self._esc(str(desc))}</td></tr>\n'
        guide_pair = VERDICT_GUIDE.get(grade, ("", ""))
        guide_html = ""
        if guide_pair[0]:
            guide_html = (
                f'<p style="color:#888;font-size:0.85em;margin-top:10px">'
                f"{self._L(self._esc(guide_pair[0]), self._esc(guide_pair[1]))}</p>"
            )
        return f"""
<div class="section" id="sec-verdict">
<h2>{L("Overall Verdict", "Verdict global")}</h2>
{
            self._desc(
                "Grade based on Pardo (WFE, profitable windows), "
                "Lopez de Prado (DSR, CPCV), "
                "Van Tharp (SQN), Chan (trades/params), "
                "Davey (DD ratio, profit factor), "
                "Carver (Monte Carlo robustness).",
                "Note basée sur Pardo (WFE, fenêtres profitables), "
                "Lopez de Prado (DSR, CPCV), "
                "Van Tharp (SQN), Chan (trades/params), "
                "Davey (ratio DD, profit factor), "
                "Carver (robustesse Monte Carlo).",
            )
        }
<div class="verdict-box">
  <div class="grade" style="background:{color}20;color:{color}">
    {self._esc(grade)}
  </div>
  <div style="flex:1">
    <div style="font-size:1.1em;font-weight:bold;color:{color};\
margin-bottom:6px">{label}</div>
    <table>{rows}</table>
    {guide_html}
  </div>
</div>
</div>"""

    @staticmethod
    def _verdict_label(grade: str) -> tuple[str, str]:
        labels = {
            "A": ("Deploy — all criteria met", "Déployer — tous les critères validés"),
            "B": ("Proceed to dry-run", "Passer en dry-run"),
            "C": ("Investigate — mixed signals", "Investiguer — signaux mitigés"),
            "D": ("Rework — most criteria failed", "Retravailler — majorité échouée"),
            "F": ("Reject — critical failure", "Rejeter — échec critique"),
        }
        return labels.get(grade, ("Unknown", "Inconnu"))

    # ------------------------------------------------------------------
    # Scorecard
    # ------------------------------------------------------------------

    _SC_ANCHORS: dict[str, str] = {
        "Profitable Windows": "sec-windows",
        "WFE": "sec-wfe",
        "DSR": "sec-verdict",
        "OOS Trades": "sec-oos-agg",
        "SQN": "sec-oos-agg",
        "Param Stability": "sec-param-stability",
        "DD Ratio": "sec-degradation",
        "Profit Factor": "sec-windows",
        "Trades/Params": "sec-windows",
        "MC Robustness": "sec-monte-carlo",
        "Perturbation": "sec-perturbation",
        "Seed Convergence": "sec-multi-seed",
        "CPCV P(loss)": "sec-cpcv",
        "OOS Equity": "sec-equity",
        "Concentration": "sec-concentration",
        "Regime": "sec-regime",
        "Holdout": "sec-holdout",
        "OOS Profit": "sec-oos-agg",
        "Max DD": "sec-oos-agg",
    }

    def _section_scorecard(self) -> str:
        L = self._L
        criteria = self._build_scorecard()
        if not criteria:
            return ""
        n_green = sum(1 for c in criteria if c["lvl"] == "g")
        n_yellow = sum(1 for c in criteria if c["lvl"] == "y")
        n_red = sum(1 for c in criteria if c["lvl"] == "r")
        if n_red > 0:
            vc = "#ef4444"
            ve = "Critical issues — fix red criteria first."
            vf = "Problèmes critiques — corriger les rouges."
        elif n_yellow > 2:
            vc = "#eab308"
            ve = "Marginal — several yellow flags."
            vf = "Marginal — plusieurs alertes jaunes."
        elif n_yellow > 0:
            vc = "#eab308"
            ve = "Acceptable with caveats."
            vf = "Acceptable avec réserves."
        else:
            vc = "#22c55e"
            ve = "All criteria passed."
            vf = "Tous les critères sont validés."
        verdict_html = (
            f'<div style="border:2px solid {vc};'
            f"border-radius:8px;padding:10px 16px;"
            f'margin:8px 0;text-align:center">'
            f'<strong style="color:{vc};font-size:1.1em">'
            f"{n_green} "
            + L("passed", "validés")
            + f" / {n_yellow} "
            + L("warnings", "alertes")
            + f" / {n_red} "
            + L("failed", "échoués")
            + f"</strong><br>"
            f'<span style="color:{vc}">'
            f"{L(ve, vf)}</span></div>"
        )
        card_html = self._render_scorecard(criteria)
        recs = self._build_recommendations(criteria)
        recs_html = ""
        if recs:
            items = "".join(f"<li>{L(e, f)}</li>" for e, f in recs)
            recs_html = (
                '<div style="margin-top:12px"><strong>'
                + L("Recommendations", "Recommandations")
                + ":</strong><ul style='margin:6px 0;"
                f"padding-left:20px'>{items}</ul></div>"
            )
        return (
            '<div class="section" id="sec-scorecard"><h2>'
            + L("Strategy Scorecard", "Bilan de la stratégie")
            + "</h2>"
            + self._desc(
                "Each criterion is graded against thresholds "
                "from quantitative trading literature. "
                "Red = blocking, Yellow = needs attention, "
                "Green = passed. Click criterion names "
                "to jump to the relevant section.",
                "Chaque critère est évalué selon des seuils "
                "issus de la littérature quantitative. "
                "Rouge = bloquant, Jaune = à surveiller, "
                "Vert = validé. Cliquez sur les noms pour "
                "aller à la section concernée.",
            )
            + verdict_html
            + card_html
            + recs_html
            + "</div>"
        )

    def _build_scorecard(self) -> list[dict]:
        verdict = self.d.get("verdict", {})
        checks = verdict.get("checks", [])
        windows = self.d.get("windows", [])
        mc = self.d.get("monte_carlo")
        perturb = self.d.get("perturbation")
        ms = self.d.get("multi_seed")
        cpcv = self.d.get("cpcv")
        oos_eq = self.d.get("oos_equity")
        oos_agg = self.d.get("oos_aggregate", {})
        regime = self.d.get("regime_analysis")
        holdout = self.d.get("holdout")
        c: list[dict] = []
        c.extend(self._sc_from_checks(checks))
        if mc:
            c.append(self._sc_mc(mc))
        if perturb:
            c.append(self._sc_perturb(perturb))
        if ms:
            c.append(self._sc_seed(ms))
        if cpcv:
            c.append(self._sc_cpcv(cpcv))
        if oos_eq:
            c.append(self._sc_oos_equity(oos_eq))
        if oos_agg:
            c.append(self._sc_oos_profit(oos_agg, windows))
        c.append(self._sc_max_dd(windows))
        if regime:
            c.append(self._sc_regime(regime))
        if holdout:
            c.append(self._sc_holdout(holdout, windows))
        self._sc_concentration(windows, c)
        for cr in c:
            anchor = self._SC_ANCHORS.get(cr["name"][0], "")
            if anchor:
                cr["anchor"] = anchor
        return c

    @staticmethod
    def _sc_from_checks(
        checks: list,
    ) -> list[dict]:
        result: list[dict] = []
        name_map = {
            "profitable_windows": (
                "Profitable Windows",
                "Fenêtres profitables",
            ),
            "wfe": ("WFE", "WFE"),
            "dsr": ("DSR", "DSR"),
            "oos_trades": ("OOS Trades", "Trades OOS"),
            "sqn": ("SQN", "SQN"),
            "param_stability": (
                "Param Stability",
                "Stabilité params",
            ),
            "dd_ratio": ("DD Ratio", "Ratio DD"),
            "profit_factor": (
                "Profit Factor",
                "Profit Factor",
            ),
            "trades_params": (
                "Trades/Params",
                "Trades/Params",
            ),
        }
        for item in checks:
            if not isinstance(item, (list, tuple)):
                continue
            if len(item) < 3:
                continue
            slug, ok, desc = item[0], item[1], str(item[2])
            names = name_map.get(slug)
            if not names:
                continue
            result.append(
                {
                    "name": names,
                    "val": "✓" if ok else "✗",
                    "lvl": "g" if ok else "r",
                    "pct": 100 if ok else 10,
                    "en": desc,
                    "fr": desc,
                    "ref": "",
                }
            )
        return result

    @staticmethod
    def _sc_mc(mc: dict) -> dict:
        rd_p5 = mc.get("return_dd_p5", 0)
        ok = rd_p5 > 0.5
        return {
            "name": ("MC Robustness", "Robustesse MC"),
            "val": f"{rd_p5:.2f}",
            "lvl": "g" if ok else "r",
            "pct": int(min(rd_p5 / 1.0 * 100, 100)),
            "en": (
                f"Return/DD p5={rd_p5:.2f} — "
                f"{'edge survives reordering' if ok else 'fragile under reordering'} "
                f"(Carver #111)."
            ),
            "fr": (
                f"Return/DD p5={rd_p5:.2f} — "
                f"{'edge survit au réordonnancement' if ok else 'fragile au réordonnancement'} "
                f"(Carver #111)."
            ),
            "ref": "Carver #111",
        }

    @staticmethod
    def _sc_perturb(p: dict) -> dict:
        pct = p.get("pct_profitable", 0)
        ok = pct >= 0.70
        return {
            "name": ("Perturbation", "Perturbation"),
            "val": f"{pct:.0%}",
            "lvl": "g" if ok else ("y" if pct >= 0.50 else "r"),
            "pct": int(pct * 100),
            "en": (
                f"{pct:.0%} profitable under param noise — "
                f"{'robust' if ok else 'narrow optimum'} "
                f"(tip #81)."
            ),
            "fr": (
                f"{pct:.0%} profitable avec bruit params — "
                f"{'robuste' if ok else 'optimum étroit'} "
                f"(tip #81)."
            ),
            "ref": "tip #81",
        }

    @staticmethod
    def _sc_seed(ms: dict) -> dict:
        conv = ms.get("convergence_pct", 0)
        ok = conv >= 0.60
        return {
            "name": ("Seed Convergence", "Convergence seeds"),
            "val": f"{conv:.0%}",
            "lvl": "g" if ok else "y",
            "pct": int(conv * 100),
            "en": (
                f"{conv:.0%} params converge — "
                f"{'stable surface' if ok else 'noisy surface'} "
                f"(tip #76)."
            ),
            "fr": (
                f"{conv:.0%} params convergent — "
                f"{'surface stable' if ok else 'surface bruitée'} "
                f"(tip #76)."
            ),
            "ref": "tip #76",
        }

    @staticmethod
    def _sc_cpcv(cpcv: dict) -> dict:
        prob = cpcv.get("prob_of_loss", 1.0)
        ok = prob < 0.30
        return {
            "name": ("CPCV P(loss)", "CPCV P(perte)"),
            "val": f"{prob:.0%}",
            "lvl": "g" if ok else ("y" if prob < 0.40 else "r"),
            "pct": int((1 - prob) * 100),
            "en": (
                f"{prob:.0%} of paths lose money — "
                f"{'acceptable' if ok else 'fragile edge'} "
                f"(Lopez de Prado)."
            ),
            "fr": (
                f"{prob:.0%} des chemins perdent — "
                f"{'acceptable' if ok else 'edge fragile'} "
                f"(Lopez de Prado)."
            ),
            "ref": "Lopez de Prado",
        }

    @staticmethod
    def _sc_oos_equity(eq: dict) -> dict:
        k = eq.get("k_ratio", 0)
        ok = k > 0.5
        return {
            "name": ("OOS Equity", "Equity OOS"),
            "val": f"K={k:.2f}",
            "lvl": "g" if ok else ("y" if k > 0 else "r"),
            "pct": int(min(max(k / 2, 0), 1) * 100),
            "en": (f"K-ratio {k:.2f} — {'smooth growth' if ok else 'choppy curve'}."),
            "fr": (f"K-ratio {k:.2f} — {'croissance régulière' if ok else 'courbe chaotique'}."),
            "ref": "",
        }

    @staticmethod
    def _sc_oos_profit(agg: dict, windows: list) -> dict:
        total = sum(w.get("test_metrics", {}).get("profit_pct", 0) for w in windows)
        ok = total > 0
        return {
            "name": ("OOS Profit", "Profit OOS"),
            "val": f"{total:+.1f}%",
            "lvl": "g" if ok else "r",
            "pct": min(100, int(max(total, 0) * 5)),
            "en": (
                f"Aggregate OOS profit {total:+.1f}% — {'positive' if ok else 'negative edge'}."
            ),
            "fr": (f"Profit OOS agrégé {total:+.1f}% — {'positif' if ok else 'edge négatif'}."),
            "ref": "",
        }

    @staticmethod
    def _sc_max_dd(windows: list) -> dict:
        dds = [w.get("test_metrics", {}).get("max_dd_pct", 0) for w in windows]
        worst = max(dds) if dds else 0
        if worst > 40:
            lvl = "r"
        elif worst > 25:
            lvl = "y"
        else:
            lvl = "g"
        return {
            "name": ("Max DD", "DD max"),
            "val": f"{worst:.1f}%",
            "lvl": lvl,
            "pct": int(min(worst * 2, 100)),
            "en": (
                f"Worst OOS DD {worst:.1f}% — "
                f"{'extreme' if worst > 40 else 'moderate' if worst > 25 else 'acceptable'}"
                f" (tip #16)."
            ),
            "fr": (
                f"Pire DD OOS {worst:.1f}% — "
                f"{'extrême' if worst > 40 else 'modéré' if worst > 25 else 'acceptable'}"
                f" (tip #16)."
            ),
            "ref": "tip #16",
        }

    @staticmethod
    def _sc_regime(regime: dict) -> dict:
        dep = regime.get("regime_dependent", False)
        return {
            "name": ("Regime", "Régime"),
            "val": "dep" if dep else "ok",
            "lvl": "y" if dep else "g",
            "pct": 30 if dep else 80,
            "en": (
                "Regime dependent — profitable only in some conditions (tip #69)."
                if dep
                else "Works across regimes."
            ),
            "fr": (
                "Dépendant du régime — profitable uniquement dans certaines conditions (tip #69)."
                if dep
                else "Fonctionne dans tous les régimes."
            ),
            "ref": "tip #69" if dep else "",
        }

    @staticmethod
    def _sc_holdout(ho: dict, windows: list) -> dict:
        ho_profit = ho.get("test_metrics", {}).get("profit_pct", 0)
        avg_oos = 0.0
        if windows:
            avg_oos = sum(w.get("test_metrics", {}).get("profit_pct", 0) for w in windows) / len(
                windows
            )
        ok = ho_profit > 0
        return {
            "name": ("Holdout", "Holdout"),
            "val": f"{ho_profit:+.1f}%",
            "lvl": "g" if ok else "r",
            "pct": min(100, int(max(ho_profit, 0) * 5)),
            "en": (
                f"Holdout profit {ho_profit:+.1f}% "
                f"(avg OOS: {avg_oos:+.1f}%) — "
                f"{'passes' if ok else 'fails on unseen data'}."
            ),
            "fr": (
                f"Profit holdout {ho_profit:+.1f}% "
                f"(moy OOS : {avg_oos:+.1f}%) — "
                f"{'validé' if ok else 'échoue sur données non vues'}."
            ),
            "ref": "",
        }

    @staticmethod
    def _sc_concentration(windows: list, c: list) -> None:
        for w in windows:
            top1 = w.get("test_metrics", {}).get("top1_pct", 0)
            if top1 > 50:
                wi = w.get("index", 0)
                c.append(
                    {
                        "name": (
                            "Concentration",
                            "Concentration",
                        ),
                        "val": f"W{wi}: {top1:.0f}%",
                        "lvl": "y",
                        "pct": int(top1),
                        "en": (f"Window {wi}: top-1 trade = {top1:.0f}% of profit — concentrated."),
                        "fr": (f"Fenêtre {wi} : top-1 trade = {top1:.0f}% du profit — concentré."),
                        "ref": "",
                    }
                )
                return

    _SC_SLUGS: dict[str, str] = {
        "Profitable Windows": "wfe",
        "WFE": "wfe",
        "DSR": "dsr",
        "OOS Trades": "sqn",
        "SQN": "sqn",
        "Param Stability": "sensitivity",
        "DD Ratio": "dd",
        "Profit Factor": "pf",
        "Trades/Params": "dof",
        "MC Robustness": "mc",
        "Perturbation": "sensitivity",
        "Seed Convergence": "convergence",
        "CPCV P(loss)": "cpcv",
        "OOS Equity": "k_ratio",
        "Concentration": "hhi",
        "Regime": "embargo",
        "Holdout": "embargo",
        "OOS Profit": "expectancy",
        "Max DD": "dd",
    }

    def _render_scorecard(self, criteria: list[dict]) -> str:
        L = self._L
        colors = {"g": "#22c55e", "y": "#eab308", "r": "#ef4444"}
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
                "grid-template-columns:160px 70px 1fr;"
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
            "Profitable Windows",
            "r_only",
            "Less than 60% of windows are profitable. "
            "Simplify the strategy or increase train period.",
            "Moins de 60% des fenêtres profitables. "
            "Simplifier la stratégie ou augmenter la "
            "période d'entraînement.",
        ),
        (
            "WFE",
            "r_only",
            "Walk-Forward Efficiency below 50% — most "
            "in-sample edge disappears out-of-sample. "
            "Reduce parameter count.",
            "Efficacité WF en dessous de 50% — l'essentiel "
            "de l'edge in-sample disparaît hors-échantillon. "
            "Réduire le nombre de paramètres.",
        ),
        (
            "DSR",
            "r_only",
            "Deflated Sharpe below 0.95 — edge may be a "
            "statistical artifact from multiple testing. "
            "Reduce epochs or parameter spaces.",
            "DSR en dessous de 0.95 — l'edge est peut-être "
            "un artefact statistique du test multiple. "
            "Réduire les epochs ou les espaces de paramètres.",
        ),
        (
            "SQN",
            "r_only",
            "SQN outside healthy range. Below 0: losing. Above 5: too good to be true (Van Tharp).",
            "SQN hors zone saine. < 0 : perdant. > 5 : trop beau pour être vrai (Van Tharp).",
        ),
        (
            "DD Ratio",
            "r_only",
            "OOS drawdown much worse than training — the "
            "optimizer found fragile parameters (Davey).",
            "DD hors-échantillon bien pire qu'en training — "
            "l'optimiseur a trouvé des paramètres fragiles "
            "(Davey).",
        ),
        (
            "MC Robustness",
            "r_only",
            "Return/DD ratio collapses under trade "
            "reordering. The edge depends on trade "
            "sequence, not signal quality (Carver #111).",
            "Le ratio rendement/DD s'effondre sous "
            "réordonnancement. L'edge dépend de la "
            "séquence, pas du signal (Carver #111).",
        ),
        (
            "Perturbation",
            "both",
            "Less than 70% of parameter perturbations "
            "remain profitable — narrow optimum (tip #81).",
            "Moins de 70% des perturbations restent profitables — optimum étroit (tip #81).",
        ),
        (
            "OOS Profit",
            "r_only",
            "Negative aggregate OOS profit. The strategy does not generalize out-of-sample.",
            "Profit OOS agrégé négatif. La stratégie ne généralise pas hors-échantillon.",
        ),
    ]

    @staticmethod
    def _build_recommendations(
        criteria: list[dict],
    ) -> list[tuple[str, str]]:
        recs: list[tuple[str, str]] = []
        bad = {c["name"][0] for c in criteria if c["lvl"] in ("r", "y")}
        for name, mode, en, fr in WFAHTMLReport._REC_TABLE:
            if name not in bad:
                continue
            lvl = next(
                (c["lvl"] for c in criteria if c["name"][0] == name),
                "",
            )
            if mode == "r_only" and lvl != "r":
                continue
            recs.append((en, fr))
        return recs

    # ------------------------------------------------------------------
    # Windows
    # ------------------------------------------------------------------

    def _section_window_table(self) -> str:
        L = self._L
        windows = self.d.get("windows", [])
        if not windows:
            return ""
        rows = ""
        for w in windows:
            idx = w.get("index", 0)
            test_range = self._fmt_range(str(w.get("test_range", "")))
            train_range = self._fmt_range(str(w.get("train_range", "")))
            tm = w.get("test_metrics", {})
            trm = w.get("train_metrics", {})
            profit = tm.get("profit_pct", 0)
            trades = tm.get("trades", 0)
            calmar = tm.get("calmar", 0)
            dd = tm.get("max_dd_pct", 0)
            wfe = w.get("wfe", 0)
            ctx = w.get("market_context", {})
            regime = self._esc(str(ctx.get("regime", "")))
            p_cls = "pos" if profit > 0 else "neg"
            train_profit = trm.get("profit_pct", 0)
            tp_cls = "pos" if train_profit > 0 else "neg"
            rows += (
                f"<tr><td>{idx}</td>"
                f"<td>{train_range}</td>"
                f"<td>{test_range}</td>"
                f'<td class="{tp_cls}">'
                f"{train_profit:+.1f}%</td>"
                f'<td class="{p_cls}">'
                f"{profit:+.1f}%</td>"
                f"<td>{trades}</td>"
                f"<td>{calmar:.2f}</td>"
                f"<td>{dd:.1f}%</td>"
                f"<td>{wfe:.0%}</td>"
                f"<td>{regime}</td></tr>\n"
            )
        tip_calmar = self._tip("calmar", "Calmar")
        tip_dd = self._tip("dd", "Max DD")
        tip_wfe = self._tip("wfe", "WFE")
        n_profit = sum(
            1
            for w in windows
            if w.get("test_metrics", {}).get("profit_pct", 0) > 0
        )
        pct_profit = n_profit / len(windows) if windows else 0
        if pct_profit >= 0.8:
            adv = self._advisory(
                "good",
                f"{n_profit}/{len(windows)} windows profitable "
                f"({pct_profit:.0%}) — strong consistency "
                f"across market conditions.",
                f"{n_profit}/{len(windows)} fenêtres profitables "
                f"({pct_profit:.0%}) — bonne consistance "
                f"dans toutes les conditions de marché.",
            )
        elif pct_profit >= 0.5:
            adv = self._advisory(
                "warn",
                f"Only {n_profit}/{len(windows)} windows profitable "
                f"({pct_profit:.0%}). Check losing windows — "
                f"if they cluster in one regime, the strategy "
                f"may be regime-dependent.",
                f"Seulement {n_profit}/{len(windows)} fenêtres "
                f"profitables ({pct_profit:.0%}). Vérifier les "
                f"fenêtres perdantes — si elles se concentrent "
                f"dans un régime, la stratégie est peut-être "
                f"dépendante du régime.",
            )
        else:
            adv = self._advisory(
                "bad",
                f"Only {n_profit}/{len(windows)} windows profitable "
                f"({pct_profit:.0%}) — the strategy fails to "
                f"generalize. Simplify the logic or reduce "
                f"parameter count.",
                f"Seulement {n_profit}/{len(windows)} fenêtres "
                f"profitables ({pct_profit:.0%}) — la stratégie "
                f"ne généralise pas. Simplifier la logique ou "
                f"réduire le nombre de paramètres.",
            )
        return f"""
<div class="section" id="sec-windows">
<h2>{L("Window Results", "Résultats par fenêtre")}</h2>
{
            self._desc(
                "Each row is one walk-forward cycle: train on past data, "
                "test on unseen future. Consistency across windows matters "
                "more than any single result (tip #161 Chan: best system "
                "in backtest = often the luckiest).",
                "Chaque ligne est un cycle walk-forward : entraîner sur "
                "le passé, tester sur le futur non vu. La consistance "
                "entre fenêtres compte plus qu'un seul résultat "
                "(tip #161 Chan : meilleur système en backtest = souvent "
                "le plus chanceux).",
            )
        }
<table>
<tr><th>#</th>
<th>{L("Train", "Entraîn.")}</th>
<th>{L("Test", "Test")}</th>
<th>{L("Train Profit", "Profit Train")}</th>
<th>{L("Test Profit", "Profit Test")}</th>
<th>{L("Trades", "Trades")}</th>
<th>{tip_calmar}</th><th>{tip_dd}</th>
<th>{tip_wfe}</th>
<th>{L("Regime", "Régime")}</th></tr>
{rows}
</table>
{adv}
</div>"""

    def _section_wfe_chart(self) -> str:
        L = self._L
        windows = self.d.get("windows", [])
        if len(windows) < 2:
            return ""
        wfes = [w.get("wfe", 0) for w in windows]
        median_wfe = sorted(wfes)[len(wfes) // 2]
        low_count = sum(1 for v in wfes if v < 0.3)
        if median_wfe >= 0.5 and low_count == 0:
            adv = self._advisory(
                "good",
                f"Median WFE {median_wfe:.0%} — the in-sample "
                f"edge transfers well to live trading (Pardo).",
                f"WFE médian {median_wfe:.0%} — l'edge "
                f"in-sample se transfère bien en live (Pardo).",
            )
        elif median_wfe >= 0.3:
            adv = self._advisory(
                "warn",
                f"Median WFE {median_wfe:.0%} — moderate "
                f"transfer. {low_count} window(s) below 30%. "
                f"Consider reducing parameters.",
                f"WFE médian {median_wfe:.0%} — transfert "
                f"modéré. {low_count} fenêtre(s) sous 30%. "
                f"Réduire le nombre de paramètres.",
            )
        else:
            adv = self._advisory(
                "bad",
                f"Median WFE {median_wfe:.0%} — most of the "
                f"in-sample edge vanishes OOS. Strong "
                f"overfitting signal (Pardo).",
                f"WFE médian {median_wfe:.0%} — l'essentiel "
                f"de l'edge in-sample disparaît OOS. Signal "
                f"fort de surapprentissage (Pardo).",
            )
        return f"""
<div class="section" id="sec-wfe">
<h2>{L("WFE Evolution", "Évolution du WFE")}</h2>
{
            self._desc(
                "Walk-Forward Efficiency per window. Above 50% = the "
                "in-sample edge persists out-of-sample (Pardo). "
                "Declining WFE suggests the strategy's edge is "
                "decaying over time.",
                "Efficacité Walk-Forward par fenêtre. Au-dessus de "
                "50% = l'edge in-sample persiste hors-échantillon "
                "(Pardo). Un WFE en déclin suggère que l'edge de la "
                "stratégie se dégrade avec le temps.",
            )
        }
{self._svg_wfe_bars(windows)}
{adv}
</div>"""

    @staticmethod
    def _svg_wfe_bars(windows: list) -> str:
        w_svg, h_svg = 900, 250
        pad_l, pad_r, pad_t, pad_b = 60, 20, 20, 40
        n = len(windows)
        bar_w = min(60, (w_svg - pad_l - pad_r) // n - 8)
        wfe_vals = [wi.get("wfe", 0) for wi in windows]
        y_min = min(0, min(wfe_vals) - 0.1)
        y_max = max(1.0, max(wfe_vals) + 0.1)

        def sx(i: int) -> float:
            return pad_l + (w_svg - pad_l - pad_r) * (i + 0.5) / n

        def sy(v: float) -> float:
            return pad_t + (h_svg - pad_t - pad_b) * (1 - (v - y_min) / (y_max - y_min))

        bars = ""
        zero_y = sy(0)
        half_y = sy(0.5)
        for i, wfe in enumerate(wfe_vals):
            cx = sx(i)
            vy = sy(wfe)
            clr = "#22c55e" if wfe >= 0.5 else ("#eab308" if wfe >= 0.3 else "#ef4444")
            if wfe >= 0:
                bars += (
                    f'<rect x="{cx - bar_w / 2:.1f}" '
                    f'y="{vy:.1f}" '
                    f'width="{bar_w}" '
                    f'height="{zero_y - vy:.1f}" '
                    f'fill="{clr}" opacity="0.8"/>\n'
                )
            else:
                bars += (
                    f'<rect x="{cx - bar_w / 2:.1f}" '
                    f'y="{zero_y:.1f}" '
                    f'width="{bar_w}" '
                    f'height="{vy - zero_y:.1f}" '
                    f'fill="{clr}" opacity="0.8"/>\n'
                )
            bars += (
                f'<text x="{cx:.1f}" y="{vy - 5:.1f}" '
                f'text-anchor="middle" fill="#e0e0e0" '
                f'font-size="10">{wfe:.0%}</text>\n'
            )
            bars += (
                f'<text x="{cx:.1f}" y="{h_svg - 8:.1f}" '
                f'text-anchor="middle" fill="#888" '
                f'font-size="10">W{windows[i].get("index", i + 1)}'
                f"</text>\n"
            )
        threshold_line = (
            f'<line x1="{pad_l}" y1="{half_y:.1f}" '
            f'x2="{w_svg - pad_r}" y2="{half_y:.1f}" '
            f'stroke="#22c55e" stroke-dasharray="6" '
            f'stroke-width="1" opacity="0.5"/>\n'
            f'<text x="{w_svg - pad_r + 2}" y="{half_y:.1f}" '
            f'fill="#22c55e" font-size="9" '
            f'dominant-baseline="middle">50%</text>\n'
        )
        zero_line = (
            f'<line x1="{pad_l}" y1="{zero_y:.1f}" '
            f'x2="{w_svg - pad_r}" y2="{zero_y:.1f}" '
            f'stroke="#555" stroke-width="1"/>\n'
        )
        return (
            f'<svg width="{w_svg}" height="{h_svg}" '
            f'xmlns="http://www.w3.org/2000/svg">\n'
            f"{zero_line}{threshold_line}{bars}"
            f"</svg>"
        )

    def _section_degradation(self) -> str:
        L = self._L
        windows = self.d.get("windows", [])
        if not windows:
            return ""
        rows = ""
        for w in windows:
            idx = w.get("index", 0)
            deg = w.get("degradation", {})
            if not deg:
                continue
            cells = f"<td>W{idx}</td>"
            for key in (
                "profit_pct",
                "calmar",
                "sharpe",
                "profit_factor",
            ):
                val = deg.get(key, 0)
                cls = "pos" if val >= 0 else "neg"
                cells += f'<td class="{cls}">{val:+.0%}</td>'
            rows += f"<tr>{cells}</tr>\n"
        if not rows:
            return ""
        return f"""
<div class="section" id="sec-degradation">
<h2>{L("Train vs Test Degradation", "Dégradation Train vs Test")}</h2>
{
            self._desc(
                "How much each metric degraded from training to testing. "
                "Negative = OOS worse than IS. DD ratio > -50% is "
                "normal; > -80% suggests severe overfitting "
                "(Davey: expect 30-50% degradation).",
                "Combien chaque métrique s'est dégradée entre "
                "l'entraînement et le test. Négatif = OOS pire que IS. "
                "Ratio DD > -50% est normal ; > -80% suggère un "
                "surapprentissage sévère "
                "(Davey : s'attendre à 30-50% de dégradation).",
            )
        }
<table>
<tr><th>#</th>
<th>{L("Profit", "Profit")}</th>
<th>{self._tip("calmar", "Calmar")}</th>
<th>{self._tip("sharpe", "Sharpe")}</th>
<th>{self._tip("pf", "PF")}</th></tr>
{rows}
</table>
{self._degradation_advisory(windows)}
</div>"""

    def _degradation_advisory(self, windows: list) -> str:
        degs = []
        for w in windows:
            d = w.get("degradation", {})
            if d:
                degs.append(d.get("profit_pct", 0))
        if not degs:
            return ""
        avg = sum(degs) / len(degs)
        severe = sum(1 for d in degs if d < -0.8)
        if avg > -0.5 and severe == 0:
            return self._advisory(
                "good",
                f"Average profit degradation {avg:+.0%} — "
                f"within Davey's expected 30-50% range. "
                f"The optimizer found robust parameters.",
                f"Dégradation profit moyenne {avg:+.0%} — "
                f"dans la plage attendue de 30-50% (Davey). "
                f"L'optimiseur a trouvé des paramètres robustes.",
            )
        if severe > 0:
            return self._advisory(
                "bad",
                f"{severe} window(s) with >80% degradation — "
                f"severe overfitting. Reduce parameter count "
                f"or increase training period.",
                f"{severe} fenêtre(s) avec >80% de dégradation "
                f"— surapprentissage sévère. Réduire le "
                f"nombre de paramètres ou allonger la "
                f"période d'entraînement.",
            )
        return self._advisory(
            "warn",
            f"Average profit degradation {avg:+.0%} — "
            f"moderate. Some in-sample edge is lost OOS.",
            f"Dégradation profit moyenne {avg:+.0%} — "
            f"modérée. Une partie de l'edge in-sample "
            f"est perdue OOS.",
        )

    def _section_holdout(self) -> str:
        L = self._L
        ho = self.d.get("holdout")
        if not ho:
            return ""
        tm = ho.get("test_metrics", {})
        bm = ho.get("baseline_metrics", {})
        profit = tm.get("profit_pct", 0)
        trades = tm.get("trades", 0)
        dd = tm.get("max_dd_pct", 0)
        calmar = tm.get("calmar", 0)
        p_cls = "pos" if profit > 0 else "neg"
        test_range = self._fmt_range(str(ho.get("test_range", "")))
        baseline_html = ""
        if bm:
            bp = bm.get("profit_pct", 0)
            bt = bm.get("trades", 0)
            bp_cls = "pos" if bp > 0 else "neg"
            baseline_html = (
                f'<div class="mini-section">'
                f"<strong>{L('Baseline (default params)', 'Baseline (params défaut)')}:</strong> "
                f'<span class="{bp_cls}">{bp:+.1f}%</span> | '
                f"{bt} trades"
                f"</div>"
            )
        return f"""
<div class="section" id="sec-holdout">
<h2>{L("Holdout Validation", "Validation holdout")}</h2>
{
            self._desc(
                "The holdout period was never used during training or "
                "window testing — it is the ultimate unseen test. "
                "If profit collapses here, the strategy is overfitted "
                "(tip #85 Lopez de Prado: holdout final = never touched).",
                "La période holdout n'a jamais été utilisée pendant "
                "l'entraînement ou le test — c'est l'ultime test non vu. "
                "Si le profit s'effondre ici, la stratégie est "
                "sur-optimisée (tip #85 Lopez de Prado : holdout final "
                "= jamais touché).",
            )
        }
<div class="mini-section">
<strong>{L("Period", "Période")}:</strong> {test_range}<br>
<span class="kv"><span class="kv-label">\
{L("Profit", "Profit")}</span><br>
  <span class="kv-value {p_cls}">{profit:+.1f}%</span></span>
<span class="kv"><span class="kv-label">\
{L("Trades", "Trades")}</span><br>
  <span class="kv-value">{trades}</span></span>
<span class="kv"><span class="kv-label">\
{self._tip("calmar", "Calmar")}</span><br>
  <span class="kv-value">{calmar:.2f}\
{self._threshold_badge("calmar", calmar)}</span></span>
<span class="kv"><span class="kv-label">\
{self._tip("dd", "Max DD")}</span><br>
  <span class="kv-value">{dd:.1f}%\
{self._threshold_badge("dd", dd)}</span></span>
</div>
{baseline_html}
{self._holdout_advisory(profit, bm)}
</div>"""

    def _holdout_advisory(
        self, profit: float, bm: dict
    ) -> str:
        bp = bm.get("profit_pct", 0) if bm else 0
        if profit > 0 and profit > bp:
            return self._advisory(
                "good",
                "Holdout profitable and beats baseline — "
                "strong out-of-sample confirmation. The "
                "optimized parameters add genuine value.",
                "Holdout profitable et bat la baseline — "
                "confirmation hors-échantillon forte. Les "
                "paramètres optimisés ajoutent une valeur "
                "réelle.",
            )
        if profit > 0:
            return self._advisory(
                "warn",
                "Holdout profitable but does not beat "
                "baseline. The optimization may not add "
                "value beyond default parameters.",
                "Holdout profitable mais ne bat pas la "
                "baseline. L'optimisation n'apporte "
                "peut-être pas de valeur au-delà des "
                "paramètres par défaut.",
            )
        return self._advisory(
            "bad",
            "Holdout loses money — the strategy does not "
            "generalize to unseen data. Likely overfitted "
            "(Lopez de Prado tip #85).",
            "Le holdout perd de l'argent — la stratégie ne "
            "généralise pas sur les données non vues. "
            "Surapprentissage probable (Lopez de Prado "
            "tip #85).",
        )

    # ------------------------------------------------------------------
    # OOS Analysis
    # ------------------------------------------------------------------

    def _section_oos_aggregate(self) -> str:
        L = self._L
        agg = self.d.get("oos_aggregate", {})
        dsr = self.d.get("deflated_sharpe_ratio")
        windows = self.d.get("windows", [])
        total_trades = agg.get("total_trades", 0)
        sqn = agg.get("sqn", 0)
        expectancy = agg.get("expectancy", 0)
        total_profit = sum(w.get("test_metrics", {}).get("profit_pct", 0) for w in windows)
        avg_sharpe = 0.0
        avg_dd = 0.0
        avg_pf = 0.0
        if windows:
            sharpes = [w.get("test_metrics", {}).get("sharpe", 0) for w in windows]
            avg_sharpe = float(np.mean(sharpes))
            dds = [w.get("test_metrics", {}).get("max_dd_pct", 0) for w in windows]
            avg_dd = float(np.mean(dds))
            pfs = [w.get("test_metrics", {}).get("profit_factor", 0) for w in windows]
            avg_pf = float(np.mean(pfs))
        p_cls = "pos" if total_profit > 0 else "neg"
        dsr_html = ""
        if dsr is not None:
            dsr_clr = "#22c55e" if dsr > 0.95 else ("#eab308" if dsr > 0.5 else "#ef4444")
            dsr_html = (
                f'<span class="kv">'
                f'<span class="kv-label">'
                f"{self._tip('dsr', 'DSR')}</span><br>"
                f'<span class="kv-value" style="color:{dsr_clr}">'
                f"{dsr:.3f}"
                f"{self._threshold_badge('dsr', dsr)}"
                f"</span></span>"
            )
        return f"""
<div class="section" id="sec-oos-agg">
<h2>{L("OOS Aggregate Metrics", "Métriques OOS agrégées")}</h2>
{
            self._desc(
                "Metrics computed across ALL out-of-sample windows "
                "combined. These represent the strategy's real-world "
                "performance on data the optimizer never saw.",
                "Métriques calculées sur TOUTES les fenêtres "
                "hors-échantillon combinées. Elles représentent la "
                "performance réelle de la stratégie sur des données "
                "que l'optimiseur n'a jamais vues.",
            )
        }
<div>
<span class="kv"><span class="kv-label">\
{L("Total OOS Profit", "Profit OOS total")}</span><br>
  <span class="kv-value {p_cls}">{total_profit:+.1f}%\
</span></span>
<span class="kv"><span class="kv-label">\
{L("Trades", "Trades")}</span><br>
  <span class="kv-value">{total_trades}</span></span>
<span class="kv"><span class="kv-label">\
{self._tip("sqn", "SQN")}</span><br>
  <span class="kv-value">{sqn:.1f}\
{self._threshold_badge("sqn", sqn)}</span></span>
<span class="kv"><span class="kv-label">\
{self._tip("expectancy", "Expectancy")}</span><br>
  <span class="kv-value">{expectancy:+.4f}</span></span>
<span class="kv"><span class="kv-label">\
{L("Avg Sharpe", "Sharpe moy.")}</span><br>
  <span class="kv-value">{avg_sharpe:.2f}\
{self._threshold_badge("sharpe", avg_sharpe)}</span></span>
<span class="kv"><span class="kv-label">\
{L("Avg DD", "DD moy.")}</span><br>
  <span class="kv-value">{avg_dd:.1f}%</span></span>
<span class="kv"><span class="kv-label">\
{self._tip("pf", "Avg PF")}</span><br>
  <span class="kv-value">{avg_pf:.2f}\
{self._threshold_badge("pf", avg_pf)}</span></span>
{dsr_html}
</div>
{self._oos_aggregate_advisory(sqn, total_trades)}
</div>"""

    def _oos_aggregate_advisory(
        self, sqn: float, total_trades: int
    ) -> str:
        if total_trades < 50:
            return self._advisory(
                "bad",
                f"Only {total_trades} OOS trades — insufficient "
                f"for statistical significance. Need 50+ "
                f"(Van Tharp: SQN needs sqrt(N) trades).",
                f"Seulement {total_trades} trades OOS — "
                f"insuffisant pour la significativite. "
                f"50+ necessaires (Van Tharp : SQN = "
                f"sqrt(N) trades).",
            )
        if sqn < 0:
            return self._advisory(
                "bad",
                f"SQN {sqn:.1f} — negative expectancy OOS. "
                f"The strategy loses money on unseen data "
                f"(Van Tharp).",
                f"SQN {sqn:.1f} — esperance negative OOS. "
                f"La strategie perd sur les donnees non "
                f"vues (Van Tharp).",
            )
        if total_trades < 100 or sqn < 2:
            parts_en = []
            parts_fr = []
            if total_trades < 100:
                parts_en.append(
                    f"{total_trades} trades (ideally 100+)"
                )
                parts_fr.append(
                    f"{total_trades} trades (ideal 100+)"
                )
            if sqn < 2:
                parts_en.append(f"SQN {sqn:.1f} (good > 2)")
                parts_fr.append(f"SQN {sqn:.1f} (bon > 2)")
            return self._advisory(
                "warn",
                "Marginal: " + ", ".join(parts_en)
                + " (Van Tharp).",
                "Marginal : " + ", ".join(parts_fr)
                + " (Van Tharp).",
            )
        if sqn > 5:
            return self._advisory(
                "warn",
                f"SQN {sqn:.1f} > 5 — suspiciously high. "
                f"May indicate data issues or overfitting "
                f"(Van Tharp: SQN > 5 = too good to be true).",
                f"SQN {sqn:.1f} > 5 — suspicieusement eleve. "
                f"Possible probleme de donnees ou "
                f"surapprentissage (Van Tharp : SQN > 5 = "
                f"trop beau pour etre vrai).",
            )
        return self._advisory(
            "good",
            f"SQN {sqn:.1f} with {total_trades} trades — "
            f"healthy edge on unseen data (Van Tharp: "
            f"2-3 = good, 3+ = excellent).",
            f"SQN {sqn:.1f} avec {total_trades} trades — "
            f"edge sain sur donnees non vues (Van Tharp : "
            f"2-3 = bon, 3+ = excellent).",
        )

    def _section_equity_chart(self) -> str:
        L = self._L
        profits = self.d.get("oos_trade_profits", [])
        if not profits or len(profits) < 2:
            return ""
        oos_eq = self.d.get("oos_equity")
        eq_cards = ""
        if oos_eq:
            k = oos_eq.get("k_ratio", 0)
            ret = oos_eq.get("total_return_pct", 0)
            dd = oos_eq.get("max_dd_pct", 0)
            nt = oos_eq.get("n_trades", 0)
            eq_cards = (
                '<div style="margin:8px 0">'
                f'<span class="kv">'
                f'<span class="kv-label">'
                f"{L('Return', 'Rendement')}</span><br>"
                f'<span class="kv-value">{ret:+.1f}%'
                f"</span></span>"
                f'<span class="kv">'
                f'<span class="kv-label">'
                f"{self._tip('dd', 'Max DD')}</span><br>"
                f'<span class="kv-value">{dd:.1f}%'
                f"{self._threshold_badge('dd', dd)}"
                f"</span></span>"
                f'<span class="kv">'
                f'<span class="kv-label">'
                f"{self._tip('k_ratio', 'K-ratio')}</span><br>"
                f'<span class="kv-value">{k:.2f}'
                f"{self._threshold_badge('k_ratio', k)}"
                f"</span></span>"
                f'<span class="kv">'
                f'<span class="kv-label">'
                f"{L('Trades', 'Trades')}</span><br>"
                f'<span class="kv-value">{nt}'
                f"</span></span>"
                f"</div>"
            )
        return f"""
<div class="section" id="sec-equity">
<h2>{L("OOS Equity Curve", "Courbe d'equity OOS")}</h2>
{
            self._desc(
                "Concatenated equity curve from all out-of-sample trades "
                "in chronological order. A smooth upward curve (high "
                "K-ratio) means consistent growth; erratic jumps mean "
                "the strategy depends on a few lucky trades.",
                "Courbe d'equity concaténée de tous les trades "
                "hors-échantillon en ordre chronologique. Une courbe "
                "ascendante régulière (K-ratio élevé) = croissance "
                "consistante ; des sauts erratiques = la stratégie "
                "dépend de quelques trades chanceux.",
            )
        }
{eq_cards}
{self._svg_equity_curve(profits)}
{self._equity_advisory(oos_eq)}
</div>"""

    def _equity_advisory(self, oos_eq: dict | None) -> str:
        if not oos_eq:
            return ""
        k = oos_eq.get("k_ratio", 0)
        dd = oos_eq.get("max_dd_pct", 0)
        if k > 0.5 and dd < 25:
            return self._advisory(
                "good",
                f"K-ratio {k:.2f}, max DD {dd:.1f}% — smooth "
                f"equity growth with controlled drawdowns.",
                f"K-ratio {k:.2f}, DD max {dd:.1f}% — "
                f"croissance reguliere avec drawdowns "
                f"controles.",
            )
        if k < 0:
            return self._advisory(
                "bad",
                f"K-ratio {k:.2f} — equity curve trends "
                f"downward. The strategy loses money OOS.",
                f"K-ratio {k:.2f} — la courbe d'equity "
                f"tend a la baisse. La strategie perd OOS.",
            )
        return self._advisory(
            "warn",
            f"K-ratio {k:.2f}, max DD {dd:.1f}% — choppy "
            f"equity curve. Growth is inconsistent across "
            f"OOS windows.",
            f"K-ratio {k:.2f}, DD max {dd:.1f}% — courbe "
            f"d'equity chaotique. Croissance inconsistante "
            f"entre les fenetres OOS.",
        )

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
                f'text-anchor="end" fill="#888" '
                f'font-size="10">{val:.0f}</text>\n'
            )
        y_label = (
            f'<text x="15" y="{(pad_t + h - pad_b) / 2:.0f}" '
            f'text-anchor="middle" fill="#888" font-size="11" '
            f'transform="rotate(-90,15,'
            f'{(pad_t + h - pad_b) / 2:.0f})">Equity</text>\n'
        )
        x_label = (
            f'<text x="{(pad_l + w - pad_r) / 2:.0f}" '
            f'y="{h - 5}" text-anchor="middle" fill="#888" '
            f'font-size="11">Trade #</text>\n'
        )
        legend_x = w - pad_r - 130
        legend = (
            f'<line x1="{legend_x}" y1="12" '
            f'x2="{legend_x + 20}" y2="12" '
            f'stroke="#555" stroke-dasharray="4" '
            f'stroke-width="1"/>\n'
            f'<text x="{legend_x + 25}" y="16" fill="#888" '
            f'font-size="10">Starting balance</text>\n'
        )
        return (
            f'<svg width="{w}" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">\n'
            f"{grid}{y_label}{x_label}{legend}"
            f'<line x1="{pad_l}" y1="{start_y:.1f}" '
            f'x2="{w - pad_r}" y2="{start_y:.1f}" '
            f'stroke="#555" stroke-dasharray="4" '
            f'stroke-width="1"/>\n'
            f'<polyline points="{points}" fill="none" '
            f'stroke="{color}" stroke-width="2"/>\n'
            f"</svg>"
        )

    def _section_concentration(self) -> str:
        L = self._L
        windows = self.d.get("windows", [])
        conc_rows = ""
        has_data = False
        for w in windows:
            tm = w.get("test_metrics", {})
            hhi = tm.get("hhi", 0)
            top1 = tm.get("top1_pct", 0)
            if hhi > 0 or top1 > 0:
                has_data = True
                idx = w.get("index", 0)
                hhi_badge = self._threshold_badge("hhi", hhi)
                conc_rows += (
                    f"<tr><td>W{idx}</td><td>{hhi:.3f}{hhi_badge}</td><td>{top1:.0f}%</td></tr>\n"
                )
        if not has_data:
            return ""
        return f"""
<div class="section" id="sec-concentration">
<h2>{L("Profit Concentration", "Concentration du profit")}</h2>
{
            self._desc(
                "If one trade dominates the profit, the strategy's "
                "success is luck, not a repeatable edge. HHI measures "
                "concentration (0 = diversified, 1 = one trade). "
                "Top-1 > 50% = red flag.",
                "Si un trade domine le profit, le succès de la "
                "stratégie est de la chance, pas un edge reproductible. "
                "HHI mesure la concentration (0 = diversifié, 1 = un "
                "trade). Top-1 > 50% = signal d'alarme.",
            )
        }
<table>
<tr><th>#</th>
<th>{self._tip("hhi", "HHI")}</th>
<th>{L("Top-1 Trade %", "Top-1 Trade %")}</th></tr>
{conc_rows}
</table>
{self._concentration_advisory(windows)}
</div>"""

    def _concentration_advisory(self, windows: list) -> str:
        hhis = [
            w.get("test_metrics", {}).get("hhi", 0)
            for w in windows
            if w.get("test_metrics", {}).get("hhi", 0) > 0
        ]
        if not hhis:
            return ""
        max_hhi = max(hhis)
        avg_hhi = sum(hhis) / len(hhis)
        if max_hhi > 0.2:
            return self._advisory(
                "bad",
                f"HHI up to {max_hhi:.3f} — profit depends "
                f"on a few trades. Remove them and the edge "
                f"may vanish.",
                f"HHI jusqu'a {max_hhi:.3f} — le profit "
                f"depend de quelques trades. Sans eux, "
                f"l'edge disparait peut-etre.",
            )
        if avg_hhi > 0.1:
            return self._advisory(
                "warn",
                f"Average HHI {avg_hhi:.3f} — moderate "
                f"concentration. Verify the edge persists "
                f"without top trades.",
                f"HHI moyen {avg_hhi:.3f} — concentration "
                f"moderee. Verifier que l'edge persiste "
                f"sans les meilleurs trades.",
            )
        return self._advisory(
            "good",
            f"Average HHI {avg_hhi:.3f} — profit is well "
            f"diversified across trades. No single trade "
            f"dominates.",
            f"HHI moyen {avg_hhi:.3f} — le profit est bien "
            f"diversifie entre les trades. Aucun trade ne "
            f"domine.",
        )

    # ------------------------------------------------------------------
    # Robustness
    # ------------------------------------------------------------------

    def _section_monte_carlo(self) -> str:
        L = self._L
        mc = self.d.get("monte_carlo")
        if not mc:
            return ""
        tip_carver = self._tip("carver_discount", "Carver discount")
        carver = mc.get("carver_discount", 0)
        return f"""
<div class="section" id="sec-monte-carlo">
<h2>{self._tip("mc", L("Monte Carlo Shuffle", "Monte Carlo — Réordonnancement"))}\
 ({mc["n_simulations"]} sims)</h2>
{
            self._desc(
                "Your trades happened in a specific order, but they could "
                "have occurred differently. Monte Carlo reshuffles the "
                "trade sequence 1,000 times. Return stays the same (sum "
                "doesn't change), but max drawdown varies with ordering. "
                "If p5 DD is much worse than p50, your equity curve was "
                "lucky (tip #111 Carver). " + PERCENTILE_HINT,
                "Vos trades ont eu lieu dans un ordre précis, mais "
                "auraient pu survenir différemment. Monte Carlo mélange "
                "la séquence 1000 fois. Le rendement ne change pas "
                "(la somme est invariante), mais le DD max varie avec "
                "l'ordre. Si DD p5 >> p50, votre courbe d'equity était "
                "chanceuse (tip #111 Carver). " + PERCENTILE_HINT_FR,
            )
        }
<div>
  <span class="kv"><span class="kv-label">\
{L("Return", "Rendement")}</span><br>
    <span class="kv-value">\
{mc["total_return_pct"]:+.1f}%</span></span>
  <span class="kv"><span class="kv-label">\
{self._tip("dd", L("Max DD", "DD max"))} p5/p50/p95\
</span><br>
    <span class="kv-value">{mc["max_dd_p5"]:.1f}% / \
{mc["max_dd_p50"]:.1f}% / {mc["max_dd_p95"]:.1f}%\
</span></span>
  <span class="kv"><span class="kv-label">\
{L("Return/DD", "Rend./DD")} p5/p50/p95</span><br>
    <span class="kv-value">{mc["return_dd_p5"]:.2f} / \
{mc["return_dd_p50"]:.2f} / {mc["return_dd_p95"]:.2f}\
</span></span>
  <span class="kv"><span class="kv-label">\
{tip_carver}</span><br>
    <span class="kv-value">{carver:.2f}\
{self._threshold_badge("carver_discount", carver)}\
</span></span>
  <span class="kv"><span class="kv-label">\
{L("Consec loss p50/p95", "Pertes consec. p50/p95")}\
</span><br>
    <span class="kv-value">\
{mc.get("max_consec_loss_p50", 0)} / \
{mc["max_consec_loss_p95"]}</span></span>
</div>
{self._svg_mc_dd_distribution(mc)}
{self._mc_advisory(mc)}
</div>"""

    def _mc_advisory(self, mc: dict) -> str:
        rd_p5 = mc.get("return_dd_p5", 0)
        carver = mc.get("carver_discount", 0)
        if rd_p5 > 1.0 and carver > 0.5:
            return self._advisory(
                "good",
                f"Return/DD p5={rd_p5:.2f}, Carver "
                f"discount={carver:.2f} — edge survives "
                f"trade reordering (Carver #111).",
                f"Return/DD p5={rd_p5:.2f}, discount "
                f"Carver={carver:.2f} — l'edge survit au "
                f"reordonnancement (Carver #111).",
            )
        if rd_p5 < 0.5:
            return self._advisory(
                "bad",
                f"Return/DD p5={rd_p5:.2f} — edge "
                f"collapses under reordering. The profit "
                f"depends on lucky trade sequence, not "
                f"signal quality (Carver #111).",
                f"Return/DD p5={rd_p5:.2f} — l'edge "
                f"s'effondre au reordonnancement. Le "
                f"profit depend de la sequence chanceuse, "
                f"pas de la qualite du signal "
                f"(Carver #111).",
            )
        return self._advisory(
            "warn",
            f"Return/DD p5={rd_p5:.2f}, Carver "
            f"discount={carver:.2f} — marginal. Some "
            f"edge survives reordering but drawdowns "
            f"worsen significantly (Carver #111).",
            f"Return/DD p5={rd_p5:.2f}, discount "
            f"Carver={carver:.2f} — marginal. Une partie "
            f"de l'edge survit mais les drawdowns "
            f"s'aggravent significativement (Carver #111).",
        )

    @staticmethod
    def _svg_mc_dd_distribution(mc: dict) -> str:
        p5 = mc.get("max_dd_p5", 0)
        p50 = mc.get("max_dd_p50", 0)
        p95 = mc.get("max_dd_p95", 0)
        w, h = 400, 50
        pad_l, pad_r = 10, 10
        track_w = w - pad_l - pad_r
        dd_min = max(0, p5 * 0.8)
        dd_max = p95 * 1.2 if p95 > 0 else 10

        def sx(v: float) -> float:
            if dd_max - dd_min < 0.01:
                return pad_l + track_w / 2
            return pad_l + track_w * (v - dd_min) / (dd_max - dd_min)

        x5 = sx(p5)
        x50 = sx(p50)
        x95 = sx(p95)
        return (
            f'<svg width="{w}" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">\n'
            f'<rect x="{pad_l}" y="20" width="{track_w}" '
            f'height="10" fill="#2a2a4a" rx="3"/>\n'
            f'<rect x="{x5:.1f}" y="20" '
            f'width="{max(x95 - x5, 1):.1f}" height="10" '
            f'fill="#eab308" opacity="0.5" rx="3"/>\n'
            f'<circle cx="{x50:.1f}" cy="25" r="5" '
            f'fill="#00d4ff"/>\n'
            f'<text x="{x5:.1f}" y="15" text-anchor="middle" '
            f'fill="#22c55e" font-size="9">p5: {p5:.1f}%</text>\n'
            f'<text x="{x50:.1f}" y="45" text-anchor="middle" '
            f'fill="#00d4ff" font-size="9">p50: {p50:.1f}%</text>\n'
            f'<text x="{x95:.1f}" y="15" text-anchor="middle" '
            f'fill="#ef4444" font-size="9">p95: {p95:.1f}%</text>\n'
            f"</svg>"
        )

    def _section_regime(self) -> str:
        L = self._L
        regime = self.d.get("regime_analysis")
        if not regime:
            return ""
        stats = regime.get("regime_stats", {})
        if not stats:
            return ""
        dep = regime.get("regime_dependent", False)
        worst = regime.get("worst_regime", "")
        rows = ""
        for r, s in sorted(stats.items()):
            avg_p = s.get("avg_profit", 0)
            p_cls = "pos" if avg_p > 0 else "neg"
            rows += (
                f"<tr><td>{self._esc(r)}</td>"
                f"<td>{s.get('windows', 0)}</td>"
                f'<td class="{p_cls}">{avg_p:+.1f}%</td>'
                f"<td>{s.get('avg_dd', 0):.1f}%</td>"
                f"<td>{s.get('pct_profitable', 0):.0%}</td>"
                f"</tr>\n"
            )
        dep_html = ""
        if dep:
            dep_html = (
                '<p class="warn-item">⚠ '
                + L(
                    f"Regime dependent: strategy loses in "
                    f"{self._esc(worst)}. It only works in "
                    f"certain market conditions (tip #69).",
                    f"Dépendant du régime : la stratégie perd "
                    f"en {self._esc(worst)}. Elle ne fonctionne "
                    f"que dans certaines conditions de marché "
                    f"(tip #69).",
                )
                + "</p>"
            )
        return f"""
<div class="section" id="sec-regime">
<h2>{L("Regime Analysis", "Analyse de régime")}</h2>
{
            self._desc(
                "OOS performance broken down by market regime "
                "(bull/bear/range based on BTC price movement). "
                "A robust strategy works across regimes — not just "
                "in favorable conditions (tip #69).",
                "Performance OOS ventilée par régime de marché "
                "(bull/bear/range basé sur le mouvement BTC). "
                "Une stratégie robuste fonctionne dans tous les "
                "régimes — pas seulement en conditions favorables "
                "(tip #69).",
            )
        }
<table>
<tr><th>{L("Regime", "Régime")}</th>
<th>{L("Windows", "Fenêtres")}</th>
<th>{L("Avg Profit", "Profit moy.")}</th>
<th>{L("Avg DD", "DD moy.")}</th>
<th>{L("% Profitable", "% Profitable")}</th></tr>
{rows}
</table>
{dep_html}
{self._regime_advisory(dep, worst)}
</div>"""

    def _regime_advisory(
        self, dep: bool, worst: str
    ) -> str:
        if not dep:
            return self._advisory(
                "good",
                "Strategy works across all market regimes "
                "— no regime dependency detected (tip #69).",
                "La strategie fonctionne dans tous les "
                "regimes de marche — aucune dependance "
                "detectee (tip #69).",
            )
        return self._advisory(
            "bad",
            f"Regime dependent — loses in "
            f"{self._esc(worst)}. Deploy only with "
            f"a regime filter or accept drawdowns in "
            f"unfavorable conditions (tip #69).",
            f"Dependant du regime — perd en "
            f"{self._esc(worst)}. Deployer uniquement "
            f"avec un filtre de regime ou accepter les "
            f"drawdowns en conditions defavorables "
            f"(tip #69).",
        )

    def _section_perturbation(self) -> str:
        L = self._L
        perturb = self.d.get("perturbation")
        if not perturb:
            return ""
        sens = perturb.get("sensitivity", 0)
        return f"""
<div class="section" id="sec-perturbation">
<h2>{L("Parameter Perturbation", "Perturbation des paramètres")}</h2>
{
            self._desc(
                "Consensus params nudged +/-5% and +/-10% randomly, "
                "then backtested on the full OOS period. If most "
                "perturbations stay profitable, the optimum is broad "
                "and robust (tip #81: stability of optimal params — "
                "if changing slightly kills performance, it's "
                "overfitting).",
                "Les params consensus sont perturbés de +/-5% et "
                "+/-10% aléatoirement, puis backtestés sur la "
                "période OOS complète. Si la plupart restent "
                "profitables, l'optimum est large et robuste "
                "(tip #81 : stabilité des params optimaux — si un "
                "léger changement tue la performance, c'est du "
                "surapprentissage).",
            )
        }
<div>
  <span class="kv"><span class="kv-label">\
{L("Variants tested", "Variantes testées")}</span><br>
    <span class="kv-value">\
{perturb["n_perturbations"]}</span></span>
  <span class="kv"><span class="kv-label">\
{L("Profit p5/p50/p95", "Profit p5/p50/p95")}</span><br>
    <span class="kv-value">\
{perturb["profit_p5"]:+.1f}% / \
{perturb["profit_p50"]:+.1f}% / \
{perturb["profit_p95"]:+.1f}%</span></span>
  <span class="kv"><span class="kv-label">\
{L("% Profitable", "% Profitable")}</span><br>
    <span class="kv-value">\
{perturb["pct_profitable"]:.0%}</span></span>
  <span class="kv"><span class="kv-label">\
{self._tip("sensitivity", L("Sensitivity", "Sensibilité"))}\
</span><br>
    <span class="kv-value">{sens:.2f}\
{self._threshold_badge("sensitivity", sens)}\
</span></span>
</div>
{self._perturbation_advisory(perturb)}
</div>"""

    def _perturbation_advisory(self, p: dict) -> str:
        pct = p.get("pct_profitable", 0)
        sens = p.get("sensitivity", 0)
        if pct >= 0.8:
            return self._advisory(
                "good",
                f"{pct:.0%} profitable under noise, "
                f"sensitivity={sens:.2f} — broad optimum, "
                f"robust to param changes (tip #81).",
                f"{pct:.0%} profitable avec bruit, "
                f"sensibilite={sens:.2f} — optimum large, "
                f"robuste aux variations de params "
                f"(tip #81).",
            )
        if pct < 0.5:
            return self._advisory(
                "bad",
                f"Only {pct:.0%} profitable under noise — "
                f"narrow optimum. Small param changes kill "
                f"the edge (tip #81: overfitting).",
                f"Seulement {pct:.0%} profitable avec "
                f"bruit — optimum etroit. De petits "
                f"changements tuent l'edge "
                f"(tip #81 : surapprentissage).",
            )
        return self._advisory(
            "warn",
            f"{pct:.0%} profitable under noise, "
            f"sensitivity={sens:.2f} — moderate "
            f"robustness. Some param combinations fail "
            f"(tip #81).",
            f"{pct:.0%} profitable avec bruit, "
            f"sensibilite={sens:.2f} — robustesse "
            f"moderee. Certaines combinaisons echouent "
            f"(tip #81).",
        )

    def _section_multi_seed(self) -> str:
        L = self._L
        ms = self.d.get("multi_seed")
        if not ms:
            return ""
        conv = ms.get("convergence_pct", 0)
        return f"""
<div class="section" id="sec-multi-seed">
<h2>{L("Multi-Seed Convergence", "Convergence multi-seed")}</h2>
{
            self._desc(
                "The same hyperopt run repeated with different random "
                "seeds. If different seeds find very different params, "
                "the optimization landscape is noisy and the 'best' "
                "params are not meaningfully better than alternatives "
                "(tip #76: the number of trials attempted is the most "
                "important information).",
                "Le même hyperopt répété avec différentes seeds. "
                "Si différentes seeds trouvent des params très "
                "différents, le paysage d'optimisation est bruité et "
                "les 'meilleurs' params ne sont pas significativement "
                "meilleurs que les alternatives (tip #76 : le nombre "
                "d'essais tentés est l'info la plus importante).",
            )
        }
<div>
  <span class="kv"><span class="kv-label">\
{L("Seeds tested", "Seeds testées")}</span><br>
    <span class="kv-value">{ms["n_seeds"]}</span></span>
  <span class="kv"><span class="kv-label">\
{self._tip("convergence", L("Convergence", "Convergence"))}\
</span><br>
    <span class="kv-value">{conv:.0%}\
{self._threshold_badge("convergence", conv)}\
</span></span>
</div>
{self._seed_advisory(conv)}
</div>"""

    def _seed_advisory(self, conv: float) -> str:
        if conv >= 0.7:
            return self._advisory(
                "good",
                f"Convergence {conv:.0%} — different seeds "
                f"find similar params. Stable optimization "
                f"landscape (tip #76).",
                f"Convergence {conv:.0%} — differentes "
                f"seeds trouvent des params similaires. "
                f"Paysage d'optimisation stable (tip #76).",
            )
        if conv < 0.5:
            return self._advisory(
                "bad",
                f"Convergence {conv:.0%} — seeds disagree "
                f"strongly. The 'best' params are random "
                f"noise, not signal (tip #76).",
                f"Convergence {conv:.0%} — les seeds "
                f"divergent fortement. Les 'meilleurs' "
                f"params sont du bruit, pas du signal "
                f"(tip #76).",
            )
        return self._advisory(
            "warn",
            f"Convergence {conv:.0%} — moderate agreement "
            f"between seeds. Consider increasing epochs "
            f"or reducing parameter space (tip #76).",
            f"Convergence {conv:.0%} — accord modere "
            f"entre seeds. Augmenter les epochs ou "
            f"reduire l'espace de params (tip #76).",
        )

    def _section_cpcv(self) -> str:
        L = self._L
        cpcv = self.d.get("cpcv")
        if not cpcv:
            return ""
        arr = np.array(cpcv.get("path_returns", []))
        p5 = f"{float(np.percentile(arr, 5)):+.1f}%" if len(arr) > 0 else "N/A"
        p50 = f"{float(np.percentile(arr, 50)):+.1f}%" if len(arr) > 0 else "N/A"
        p95 = f"{float(np.percentile(arr, 95)):+.1f}%" if len(arr) > 0 else "N/A"
        prob = cpcv.get("prob_of_loss", 0)
        sharpe = cpcv.get("sharpe_of_paths", 0)
        title = self._tip("cpcv", "CPCV")
        return f"""
<div class="section" id="sec-cpcv">
<h2>{title} — {
            L("Combinatorial Purged Cross-Validation", "Validation croisée combinatoire purgée")
        } (N={cpcv["n_groups"]}, K={cpcv["n_test_groups"]})</h2>
{
            self._desc(
                "Unlike rolling walk-forward which tests N sequential "
                "windows, CPCV tests every possible combination of "
                "train/test splits. This gives a distribution of returns "
                "and a probability of loss — a much stronger test "
                "(Lopez de Prado, Advances in Financial ML).",
                "Contrairement au walk-forward glissant qui teste N "
                "fenêtres séquentielles, CPCV teste toutes les "
                "combinaisons possibles de splits train/test. Donne "
                "une distribution de rendements et une probabilité de "
                "perte — un test bien plus solide (Lopez de Prado, "
                "Advances in Financial ML).",
            )
        }
<div>
  <span class="kv"><span class="kv-label">\
{L("Combinations", "Combinaisons")}</span><br>
    <span class="kv-value">{cpcv["n_combinations"]}\
</span></span>
  <span class="kv"><span class="kv-label">\
{L("Paths", "Chemins")}</span><br>
    <span class="kv-value">{cpcv["n_paths"]}\
</span></span>
  <span class="kv"><span class="kv-label">\
{L("Avg Return", "Rendement moy.")}</span><br>
    <span class="kv-value">{cpcv["avg_return"]:+.1f}%\
</span></span>
  <span class="kv"><span class="kv-label">\
{self._tip("sharpe_of_paths", L("Sharpe of Paths", "Sharpe des chemins"))}\
</span><br>
    <span class="kv-value">{sharpe:.2f}\
{self._threshold_badge("sharpe_of_paths", sharpe)}\
</span></span>
  <span class="kv"><span class="kv-label">\
{self._tip("prob_of_loss", "P(loss)")}</span><br>
    <span class="kv-value">{prob:.0%}\
{self._threshold_badge("prob_of_loss", prob)}\
</span></span>
  <span class="kv"><span class="kv-label">\
p5/p50/p95</span><br>
    <span class="kv-value">{p5} / {p50} / {p95}\
</span></span>
</div>
{self._svg_cpcv_returns(cpcv.get("path_returns", []))}
{self._cpcv_advisory(prob, sharpe)}
</div>"""

    def _cpcv_advisory(
        self, prob: float, sharpe: float
    ) -> str:
        if prob < 0.2 and sharpe > 0.5:
            return self._advisory(
                "good",
                f"P(loss)={prob:.0%}, Sharpe of "
                f"paths={sharpe:.2f} — strong edge across "
                f"all combinatorial splits "
                f"(Lopez de Prado).",
                f"P(perte)={prob:.0%}, Sharpe des "
                f"chemins={sharpe:.2f} — edge solide sur "
                f"toutes les combinaisons de splits "
                f"(Lopez de Prado).",
            )
        if prob > 0.4:
            return self._advisory(
                "bad",
                f"P(loss)={prob:.0%} — more than 40% of "
                f"paths lose money. The edge is fragile and "
                f"may be a statistical artifact "
                f"(Lopez de Prado).",
                f"P(perte)={prob:.0%} — plus de 40% des "
                f"chemins perdent. L'edge est fragile et "
                f"peut-etre un artefact statistique "
                f"(Lopez de Prado).",
            )
        return self._advisory(
            "warn",
            f"P(loss)={prob:.0%}, Sharpe of "
            f"paths={sharpe:.2f} — moderate. Some paths "
            f"lose money; edge exists but is not strong "
            f"(Lopez de Prado).",
            f"P(perte)={prob:.0%}, Sharpe des "
            f"chemins={sharpe:.2f} — modere. Certains "
            f"chemins perdent ; l'edge existe mais n'est "
            f"pas fort (Lopez de Prado).",
        )

    @staticmethod
    def _svg_cpcv_returns(returns: list[float]) -> str:
        if len(returns) < 2:
            return ""
        w, h = 600, 180
        pad_l, pad_r, pad_t, pad_b = 50, 20, 15, 30
        arr = np.array(returns)
        n_bins = min(15, max(5, len(returns) // 3))
        counts, edges = np.histogram(arr, bins=n_bins)
        max_count = int(np.max(counts)) if len(counts) else 1

        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        bar_w = plot_w / n_bins
        bars = ""
        for i in range(n_bins):
            bh = counts[i] / max(max_count, 1) * plot_h
            x = pad_l + i * bar_w
            y = pad_t + plot_h - bh
            mid = (edges[i] + edges[i + 1]) / 2
            clr = "#22c55e" if mid > 0 else "#ef4444"
            bars += (
                f'<rect x="{x:.1f}" y="{y:.1f}" '
                f'width="{bar_w - 2:.1f}" height="{bh:.1f}" '
                f'fill="{clr}" opacity="0.7"/>\n'
            )
        zero_x = pad_l + plot_w * (0 - float(arr.min())) / (
            float(arr.max()) - float(arr.min()) if float(arr.max()) != float(arr.min()) else 1
        )
        zero_line = (
            f'<line x1="{zero_x:.1f}" y1="{pad_t}" '
            f'x2="{zero_x:.1f}" y2="{pad_t + plot_h}" '
            f'stroke="#888" stroke-dasharray="4" '
            f'stroke-width="1"/>\n'
        )
        return (
            f'<svg width="{w}" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">\n'
            f"{bars}{zero_line}"
            f'<text x="{(pad_l + w - pad_r) / 2}" '
            f'y="{h - 5}" text-anchor="middle" fill="#888" '
            f'font-size="10">Return %</text>\n'
            f"</svg>"
        )

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def _section_param_stability(self) -> str:
        L = self._L
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
                f"<tr><td>{self._esc(param)}</td>"
                f"<td>{median:.4f}</td><td>{std:.4f}</td>"
                f"<td>{sor:.4f}</td><td>{badge}</td>"
                f"<td style='color:#888;font-size:0.8em'>"
                f"{vals_str}</td></tr>\n"
            )
        return f"""
<div class="section" id="sec-param-stability">
<h2>{L("Parameter Stability", "Stabilité des paramètres")}</h2>
{
            self._desc(
                "Parameters that change wildly between windows are "
                "'unstable' — the optimizer is fitting noise, not "
                "signal (tip #81: if changing a param slightly kills "
                "performance, it's overfitting). Stable params "
                "(std/range < 15%) suggest a real pattern. Consider "
                "freezing unstable params at sensible defaults.",
                "Les paramètres qui changent beaucoup entre fenêtres "
                "sont 'instables' — l'optimiseur fitte le bruit, pas "
                "le signal (tip #81 : si changer légèrement un param "
                "tue la performance, c'est du surapprentissage). "
                "Params stables (std/range < 15%) = vrai pattern. "
                "Envisager de figer les params instables.",
            )
        }
<table>
<tr><th>{L("Param", "Param")}</th>
<th>{L("Median", "Médiane")}</th>
<th>{L("Std", "Écart-type")}</th>
<th>{L("Std/Range", "Std/Range")}</th>
<th>{L("Status", "Statut")}</th>
<th>{L("Values per window", "Valeurs par fenêtre")}</th></tr>
{rows}
</table>
{self._svg_param_sparklines(stability)}
{self._param_stability_advisory(stability)}
</div>"""

    def _param_stability_advisory(
        self, stability: dict
    ) -> str:
        if not stability:
            return ""
        n_total = len(stability)
        n_stable = sum(
            1 for v in stability.values() if v.get("stable")
        )
        n_unstable = sum(
            1 for v in stability.values()
            if v.get("unstable")
        )
        ratio = n_stable / n_total if n_total else 0
        if ratio >= 0.7 and n_unstable == 0:
            return self._advisory(
                "good",
                f"{n_stable}/{n_total} params stable — "
                f"optimizer found consistent values across "
                f"windows (tip #81).",
                f"{n_stable}/{n_total} params stables — "
                f"l'optimiseur a trouve des valeurs "
                f"consistantes entre fenetres (tip #81).",
            )
        if n_unstable > n_total / 2:
            return self._advisory(
                "bad",
                f"{n_unstable}/{n_total} params unstable — "
                f"optimizer is fitting noise. Consider "
                f"freezing unstable params at defaults "
                f"(tip #81).",
                f"{n_unstable}/{n_total} params instables — "
                f"l'optimiseur fitte le bruit. Envisager "
                f"de figer les params instables aux "
                f"valeurs par defaut (tip #81).",
            )
        return self._advisory(
            "warn",
            f"{n_stable}/{n_total} stable, "
            f"{n_unstable} unstable — mixed. Freeze "
            f"unstable params and re-run to improve "
            f"robustness (tip #81).",
            f"{n_stable}/{n_total} stables, "
            f"{n_unstable} instables — mitige. Figer les "
            f"params instables et relancer pour "
            f"ameliorer la robustesse (tip #81).",
        )

    @staticmethod
    def _svg_param_sparklines(
        stability: dict[str, dict],
    ) -> str:
        params = [(k, v) for k, v in sorted(stability.items()) if len(v.get("values", [])) >= 2]
        if not params:
            return ""
        n_params = len(params)
        row_h = 30
        w = 600
        h = n_params * row_h + 20
        pad_l = 120
        pad_r = 20
        plot_w = w - pad_l - pad_r
        lines = ""
        for pi, (name, info) in enumerate(params):
            vals = info.get("values", [])
            y_base = 10 + pi * row_h + row_h // 2
            lines += (
                f'<text x="{pad_l - 5}" y="{y_base + 4}" '
                f'text-anchor="end" fill="#888" '
                f'font-size="10">{html.escape(name)}</text>\n'
            )
            v_min = min(vals)
            v_max = max(vals)
            v_range = v_max - v_min if v_max != v_min else 1
            pts = []
            for i, v in enumerate(vals):
                x = pad_l + plot_w * i / max(len(vals) - 1, 1)
                y = y_base + 8 - 16 * (v - v_min) / v_range
                pts.append(f"{x:.1f},{y:.1f}")
            clr = (
                "#22c55e"
                if info.get("stable")
                else ("#ef4444" if info.get("unstable") else "#eab308")
            )
            lines += (
                f'<polyline points="{" ".join(pts)}" '
                f'fill="none" stroke="{clr}" '
                f'stroke-width="1.5"/>\n'
            )
            for pt in pts:
                lines += (
                    f'<circle cx="{pt.split(",")[0]}" '
                    f'cy="{pt.split(",")[1]}" r="2.5" '
                    f'fill="{clr}"/>\n'
                )
        return f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">\n{lines}</svg>'

    def _section_consensus(self) -> str:
        L = self._L
        consensus = self.d.get("consensus_params", {})
        if not consensus:
            return ""
        rows = ""
        for space, params in sorted(consensus.items()):
            if not isinstance(params, dict):
                continue
            for k, v in sorted(params.items()):
                rows += f"<tr><td>{self._esc(space)}</td><td>{self._esc(k)}</td><td>{v}</td></tr>\n"
        strategy = self._esc(self.d.get("strategy", ""))
        return f"""
<div class="section" id="sec-consensus">
<h2>{L("Consensus Parameters", "Paramètres consensus")}</h2>
{
            self._desc(
                "Weighted median of each parameter across all windows, "
                "weighted by test Calmar ratio. These are the params to "
                "deploy — they represent the robust center of the "
                "optimization landscape, not any single window's 'best'.",
                "Médiane pondérée de chaque paramètre sur toutes les "
                "fenêtres, pondérée par le Calmar test. Ce sont les "
                "params à déployer — ils représentent le centre "
                "robuste du paysage d'optimisation, pas le 'meilleur' "
                "d'une seule fenêtre.",
            )
        }
<table>
<tr><th>{L("Space", "Espace")}</th>
<th>{L("Parameter", "Paramètre")}</th>
<th>{L("Value", "Valeur")}</th></tr>
{rows}
</table>
{
            self._details(
                L("How to apply", "Comment appliquer"),
                L(
                    f"Copy the consensus JSON from "
                    f"<code>user_data/walk_forward/{strategy}_consensus_*.json</code> "
                    f"to <code>user_data/strategies/{strategy}.json</code>. "
                    f"Freqtrade loads this at startup and overrides the strategy's "
                    f"default parameters.",
                    f"Copier le JSON consensus depuis "
                    f"<code>user_data/walk_forward/{strategy}_consensus_*.json</code> "
                    f"vers <code>user_data/strategies/{strategy}.json</code>. "
                    f"Freqtrade le charge au démarrage et écrase les paramètres "
                    f"par défaut de la stratégie.",
                ),
            )
        }
{
            self._advisory(
                "good",
                "Consensus params are the weighted median across "
                "all windows — the robust center of the optimization "
                "landscape, not any single window's best.",
                "Les params consensus sont la mediane ponderee de "
                "toutes les fenetres — le centre robuste du paysage "
                "d'optimisation, pas le meilleur d'une seule fenetre.",
            )
        }
</div>"""

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _section_warnings(self) -> str:
        L = self._L
        warnings = self.d.get("warnings", [])
        if not warnings:
            return ""
        items = ""
        for w in warnings:
            items += f'<p class="warn-item">⚠ {self._esc(w)}</p>'
        return f"""
<div class="section" id="sec-warnings">
<h2>{L("Warning Flags", "Alertes")}</h2>
{
            self._desc(
                "Automatic flags based on per-window and aggregate "
                "analysis. Each references a specific rule from "
                "quantitative trading literature.",
                "Alertes automatiques basées sur l'analyse par fenêtre "
                "et agrégée. Chacune référence une règle spécifique "
                "de la littérature quantitative.",
            )
        }
{items}
{
            self._advisory(
                "warn",
                f"{len(warnings)} warning(s) detected — review "
                f"each flag above. Warnings indicate specific "
                f"weaknesses that may impact live performance.",
                f"{len(warnings)} alerte(s) detectee(s) — "
                f"examiner chaque alerte ci-dessus. Les alertes "
                f"indiquent des faiblesses specifiques pouvant "
                f"impacter la performance en live.",
            )
        }
</div>"""

    # ------------------------------------------------------------------
    # Reference
    # ------------------------------------------------------------------

    def _section_next_steps(self) -> str:
        L = self._L
        verdict = self.d.get("verdict", {})
        grade = verdict.get("grade", "?")
        guide_pair = VERDICT_GUIDE.get(grade, ("", ""))
        if not guide_pair[0]:
            return ""
        strategy = self._esc(self.d.get("strategy", ""))
        return f"""
<div class="section" id="sec-next-steps">
<h2>{L("What To Do Next", "Prochaines étapes")}</h2>
<div class="next-steps">
  <p><strong>{L("Grade", "Note")} {self._esc(grade)}:\
</strong> {L(self._esc(guide_pair[0]), self._esc(guide_pair[1]))}</p>
</div>
{
            self._details(
                L("Output files from this run", "Fichiers générés par cette exécution"),
                L(
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
                    "<p>Tous les fichiers sont dans <code>user_data/walk_forward/</code> :</p>"
                    "<ul>"
                    "<li><strong>Params consensus</strong> (JSON) : "
                    "copier dans le fichier stratégie</li>"
                    "<li><strong>Résultats complets</strong> (JSON) : données machine</li>"
                    "<li><strong>Ce rapport</strong> (HTML) : résumé partageable</li>"
                    "</ul>"
                    "<p>Pour appliquer les params consensus en dry-run :</p>"
                    f"<code>cp user_data/walk_forward/{strategy}_consensus_*.json "
                    f"user_data/strategies/{strategy}.json</code>",
                ),
            )
        }
</div>"""

    def _section_glossary(self) -> str:
        L = self._L
        rows = ""
        for slug, entry in sorted(METRIC_GLOSSARY.items()):
            abbrev = self._esc(entry.get("abbrev", slug))
            name = self._esc(entry.get("name", ""))
            one_liner_en = self._esc(entry.get("one_liner", ""))
            one_liner_fr = self._esc(entry.get("explanation_fr", one_liner_en)[:80])
            source = self._esc(entry.get("source", ""))
            source_html = f' <span style="color:#555">({source})</span>' if source else ""
            rows += (
                f"<tr><td><strong>{abbrev}</strong></td>"
                f"<td>{name}</td>"
                f"<td>{L(one_liner_en, one_liner_fr)}"
                f"{source_html}</td></tr>\n"
            )
        return f"""
<div class="section" id="sec-glossary">
<details><summary><h2 style="display:inline">\
{L("Glossary", "Glossaire")}</h2></summary><div>
<table><tr><th>{L("Abbrev", "Abrév.")}</th>
<th>{L("Full Name", "Nom complet")}</th>
<th>{L("Description", "Description")}</th></tr>
{rows}</table>
</div></details></div>"""

    @staticmethod
    def _html_footer() -> str:
        return """
</div>
</body>
</html>"""
