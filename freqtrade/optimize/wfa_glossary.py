from __future__ import annotations


METRIC_GLOSSARY: dict[str, dict] = {
    "wfe": {
        "name": "Walk-Forward Efficiency",
        "abbrev": "WFE",
        "one_liner": "How much training profit survives on unseen data (>50% good)",
        "explanation": (
            "Annualized test return divided by annualized training return. "
            "A WFE above 50% means more than half the in-sample edge "
            "persists out-of-sample — a sign the strategy generalizes."
        ),
        "explanation_fr": (
            "Rendement test annualisé divisé par le rendement training. "
            "Un WFE > 50% signifie que plus de la moitié de l'edge "
            "in-sample persiste hors-échantillon — signe de généralisation."
        ),
        "thresholds": [
            (-99, "negative", "#ef4444"),
            (0, "poor", "#f97316"),
            (0.3, "marginal", "#eab308"),
            (0.5, "good", "#84cc16"),
            (0.8, "excellent", "#22c55e"),
        ],
        "source": "Pardo",
    },
    "sqn": {
        "name": "System Quality Number",
        "abbrev": "SQN",
        "one_liner": "Edge quality: mean/std * sqrt(N). 1.5-3 good, >5 suspicious",
        "explanation": (
            "Measures trading edge quality by comparing average profit per "
            "trade to its variability. Higher is better, but values above 5 "
            "suggest overfitting — real edges rarely look that clean."
        ),
        "explanation_fr": (
            "Mesure la qualité de l'edge en comparant le profit moyen par "
            "trade à sa variabilité. Plus c'est haut, mieux c'est, mais "
            "au-dessus de 5, c'est suspect — les vrais edges sont rarement aussi nets."
        ),
        "thresholds": [
            (-99, "losing", "#ef4444"),
            (0, "weak", "#f97316"),
            (1.5, "good", "#84cc16"),
            (3, "excellent", "#22c55e"),
            (5, "suspicious", "#eab308"),
        ],
        "source": "Van Tharp",
    },
    "dsr": {
        "name": "Deflated Sharpe Ratio",
        "abbrev": "DSR",
        "one_liner": "Sharpe adjusted for multiple testing (>0.95 significant)",
        "explanation": (
            "The Sharpe ratio deflated for the number of trials run. "
            "When you test hundreds of parameter combinations, some will "
            "look good by chance. DSR corrects for this — above 0.95 means "
            "the edge is likely real, not a statistical fluke."
        ),
        "explanation_fr": (
            "Le ratio de Sharpe corrigé pour le nombre d'essais. "
            "Quand on teste des centaines de combinaisons, certaines "
            "semblent bonnes par chance. Le DSR corrige ce biais — "
            "au-dessus de 0.95, l'edge est probablement réel."
        ),
        "thresholds": [
            (0, "not significant", "#ef4444"),
            (0.5, "weak", "#f97316"),
            (0.95, "significant", "#22c55e"),
        ],
        "source": "Bailey & Lopez de Prado",
    },
    "calmar": {
        "name": "Calmar Ratio",
        "abbrev": "Calmar",
        "one_liner": "Annual return / max drawdown. Higher = better risk-adjusted",
        "explanation": (
            "Annualized return divided by maximum drawdown. A Calmar of 2 "
            "means you earn twice what you risk losing. Below 0.5 suggests "
            "the drawdown pain is not worth the return."
        ),
        "explanation_fr": (
            "Rendement annualisé divisé par le drawdown maximum. Un Calmar "
            "de 2 signifie que vous gagnez 2x ce que vous risquez de perdre. "
            "En dessous de 0.5, la douleur du DD ne vaut pas le rendement."
        ),
        "thresholds": [
            (-99, "losing", "#ef4444"),
            (0, "poor", "#f97316"),
            (0.5, "marginal", "#eab308"),
            (2, "good", "#84cc16"),
            (5, "excellent", "#22c55e"),
        ],
        "source": "Young",
    },
    "dd": {
        "name": "Maximum Drawdown",
        "abbrev": "Max DD",
        "one_liner": "Worst peak-to-trough loss during the period",
        "explanation": (
            "The largest drop from a peak equity value to a subsequent "
            "trough, as a percentage. This is the worst loss you would "
            "have experienced if you entered at the peak."
        ),
        "explanation_fr": (
            "La plus grande chute depuis un pic d'equity jusqu'au creux "
            "suivant, en pourcentage. C'est la pire perte subie si "
            "vous étiez entré au sommet."
        ),
        "thresholds": [
            (0, "none", "#22c55e"),
            (10, "mild", "#84cc16"),
            (25, "moderate", "#eab308"),
            (40, "severe", "#f97316"),
            (60, "extreme", "#ef4444"),
        ],
        "source": "",
    },
    "hhi": {
        "name": "Herfindahl-Hirschman Index",
        "abbrev": "HHI",
        "one_liner": "Profit concentration: 0=even, 1=one trade. Below 0.15 OK",
        "explanation": (
            "Measures how concentrated your profits are across trades. "
            "An HHI near 0 means profits are spread evenly; near 1 means "
            "one trade dominates. Above 0.15 is a warning sign — your "
            "strategy's success depends on too few trades."
        ),
        "explanation_fr": (
            "Mesure la concentration des profits entre les trades. "
            "Un HHI proche de 0 = profits répartis ; proche de 1 = "
            "un trade domine. Au-dessus de 0.15, la stratégie dépend "
            "de trop peu de trades."
        ),
        "thresholds": [
            (0, "diversified", "#22c55e"),
            (0.10, "acceptable", "#84cc16"),
            (0.15, "concentrated", "#eab308"),
            (0.30, "dangerous", "#ef4444"),
        ],
        "source": "",
    },
    "pf": {
        "name": "Profit Factor",
        "abbrev": "PF",
        "one_liner": "Gross profit / gross loss. Above 1.2 = real edge",
        "explanation": (
            "Total gross profit divided by total gross loss. A value of "
            "1.0 is break-even. Above 1.2 suggests a durable edge; below "
            "1.0 means the strategy is losing money overall."
        ),
        "explanation_fr": (
            "Profit brut total divisé par la perte brute totale. 1.0 = "
            "seuil de rentabilité. Au-dessus de 1.2, edge durable ; "
            "en dessous de 1.0, la stratégie perd de l'argent."
        ),
        "thresholds": [
            (0, "losing", "#ef4444"),
            (1.0, "break-even", "#f97316"),
            (1.2, "edge", "#84cc16"),
            (2.0, "strong", "#22c55e"),
        ],
        "source": "Davey",
    },
    "mc": {
        "name": "Monte Carlo Simulation",
        "abbrev": "MC",
        "one_liner": "Reshuffles trade order 1000x to stress-test drawdown",
        "explanation": (
            "Your trades happened in a specific order, but they could have "
            "occurred differently. Monte Carlo reshuffles the trade sequence "
            "1,000 times to see how drawdown varies with ordering alone — "
            "revealing whether your equity curve was lucky or robust."
        ),
        "explanation_fr": (
            "Vos trades ont eu lieu dans un ordre précis, mais auraient pu "
            "survenir différemment. Monte Carlo mélange la séquence 1000 fois "
            "pour voir comment le DD varie — révèle si votre courbe d'equity "
            "était chanceuse ou robuste."
        ),
        "thresholds": [],
        "source": "Carver",
    },
    "carver_discount": {
        "name": "Carver Discount",
        "abbrev": "Carver",
        "one_liner": "Worst-case / median risk ratio. 1.0=robust, low=fragile",
        "explanation": (
            "Ratio of the 5th-percentile to the 50th-percentile return/DD "
            "ratio from Monte Carlo. A value near 1.0 means worst-case "
            "performance is close to typical — the edge is robust. Below "
            "0.3 means unlucky trade ordering wrecks the risk profile."
        ),
        "explanation_fr": (
            "Ratio entre le 5e percentile et la médiane du rendement/DD "
            "Monte Carlo. Proche de 1.0 = robuste. En dessous de 0.3, "
            "un mauvais ordre de trades détruit le profil de risque."
        ),
        "thresholds": [
            (0, "fragile", "#ef4444"),
            (0.3, "moderate", "#eab308"),
            (0.6, "robust", "#22c55e"),
        ],
        "source": "Carver, Systematic Trading",
    },
    "k_ratio": {
        "name": "K-Ratio",
        "abbrev": "K-ratio",
        "one_liner": "Equity curve linearity. >0.5 = smooth growth",
        "explanation": (
            "Slope of the equity curve divided by its standard error. "
            "A high K-ratio means the equity curve grows steadily rather "
            "than in erratic jumps. Above 0.5 is smooth; below 0 means "
            "the equity curve is declining."
        ),
        "explanation_fr": (
            "Pente de la courbe d'equity divisée par son erreur standard. "
            "Un K-ratio élevé = croissance régulière. Au-dessus de 0.5 = "
            "lisse ; en dessous de 0, la courbe décline."
        ),
        "thresholds": [
            (-99, "declining", "#ef4444"),
            (0, "choppy", "#f97316"),
            (0.5, "smooth growth", "#84cc16"),
            (2.0, "very smooth", "#22c55e"),
        ],
        "source": "Zephyr Associates",
    },
    "expectancy": {
        "name": "Expectancy",
        "abbrev": "Exp",
        "one_liner": "Average profit per trade in account currency",
        "explanation": (
            "The mean profit (or loss) per trade. Positive means the "
            "strategy makes money on average; negative means it loses. "
            "This is the most basic measure of whether the strategy works."
        ),
        "explanation_fr": (
            "Le profit (ou perte) moyen par trade. Positif = la stratégie "
            "gagne de l'argent en moyenne. C'est la mesure la plus "
            "fondamentale de l'efficacité d'une stratégie."
        ),
        "thresholds": [
            (-99, "losing", "#ef4444"),
            (0, "profitable", "#22c55e"),
        ],
        "source": "Van Tharp",
    },
    "embargo": {
        "name": "Embargo Period",
        "abbrev": "Embargo",
        "one_liner": "Gap between train/test to prevent information leakage",
        "explanation": (
            "A buffer of days between the training and test periods. "
            "Without it, lagged indicators computed at the end of training "
            "could 'see' into the test period, inflating results."
        ),
        "explanation_fr": (
            "Un tampon de jours entre les périodes train et test. "
            "Sans embargo, les indicateurs retardés calculés en fin "
            "de training pourraient voir dans la période test, gonflant "
            "les résultats."
        ),
        "thresholds": [],
        "source": "Lopez de Prado",
    },
    "cpcv": {
        "name": "Combinatorial Purged Cross-Validation",
        "abbrev": "CPCV",
        "one_liner": "Tests all possible train/test splits for reliability",
        "explanation": (
            "Instead of testing N sequential windows, CPCV tests every "
            "possible combination of data splits. This gives a distribution "
            "of returns and a probability of loss — a much stronger test "
            "of whether the strategy is genuinely profitable."
        ),
        "explanation_fr": (
            "Au lieu de tester N fenêtres séquentielles, CPCV teste toutes "
            "les combinaisons possibles de splits. Donne une distribution de "
            "rendements et une probabilité de perte — un test bien plus solide."
        ),
        "thresholds": [],
        "source": "Lopez de Prado, Advances in Financial ML",
    },
    "sensitivity": {
        "name": "Parameter Sensitivity",
        "abbrev": "Sensitivity",
        "one_liner": "Profit change when params nudged +/-10%. Lower = robust",
        "explanation": (
            "Measures how much profit changes when parameters are randomly "
            "nudged by 5-10%. Low sensitivity means the strategy is robust "
            "to small parameter errors; high sensitivity means it only "
            "works at exact parameter values — a sign of overfitting."
        ),
        "explanation_fr": (
            "Mesure combien le profit change quand les paramètres sont "
            "perturbés de 5-10%. Faible sensibilité = robuste ; haute "
            "sensibilité = ne marche qu'avec des valeurs exactes — "
            "signe de surapprentissage."
        ),
        "thresholds": [
            (0, "robust", "#22c55e"),
            (1.0, "moderate", "#eab308"),
            (2.0, "fragile", "#ef4444"),
        ],
        "source": "",
    },
    "convergence": {
        "name": "Seed Convergence",
        "abbrev": "Convergence",
        "one_liner": "Do different random seeds find the same params? (>60% good)",
        "explanation": (
            "Running the optimizer with different random seeds should find "
            "similar parameter values. If convergence is low, the "
            "optimization landscape is noisy and the 'best' parameters "
            "are not meaningfully better than alternatives."
        ),
        "explanation_fr": (
            "Lancer l'optimiseur avec différentes seeds devrait trouver "
            "des paramètres similaires. Si la convergence est faible, "
            "le paysage d'optimisation est bruité et les 'meilleurs' "
            "paramètres ne sont pas significativement meilleurs."
        ),
        "thresholds": [
            (0, "unstable", "#ef4444"),
            (0.4, "marginal", "#eab308"),
            (0.6, "stable", "#22c55e"),
        ],
        "source": "",
    },
    "prob_of_loss": {
        "name": "Probability of Loss",
        "abbrev": "P(loss)",
        "one_liner": "Fraction of CPCV paths with negative return (<30% OK)",
        "explanation": (
            "The percentage of all CPCV backtest paths that ended with "
            "a negative return. Below 15% is strong evidence the strategy "
            "works; above 30% means it loses money in many data splits."
        ),
        "explanation_fr": (
            "Le pourcentage de chemins CPCV terminant avec un rendement "
            "négatif. En dessous de 15% = preuve solide. Au-dessus de 30% "
            "= la stratégie perd dans beaucoup de splits."
        ),
        "thresholds": [
            (0, "strong", "#22c55e"),
            (0.15, "acceptable", "#84cc16"),
            (0.30, "fragile", "#eab308"),
            (0.50, "unreliable", "#ef4444"),
        ],
        "source": "Lopez de Prado",
    },
    "sharpe_of_paths": {
        "name": "Sharpe of Paths",
        "abbrev": "Sharpe",
        "one_liner": "Risk-adjusted return across CPCV paths (>1.0 good)",
        "explanation": (
            "The Sharpe ratio computed across all CPCV path returns. "
            "Unlike the usual Sharpe (computed within a single backtest), "
            "this measures how consistent the strategy is across different "
            "data splits."
        ),
        "explanation_fr": (
            "Le Sharpe calculé sur tous les chemins CPCV. Contrairement "
            "au Sharpe classique (un seul backtest), il mesure la cohérence "
            "de la stratégie à travers différents splits de données."
        ),
        "thresholds": [
            (-99, "losing", "#ef4444"),
            (0, "weak", "#f97316"),
            (0.5, "moderate", "#eab308"),
            (1.0, "good", "#22c55e"),
        ],
        "source": "Lopez de Prado",
    },
    "skewness": {
        "name": "Skewness",
        "abbrev": "Skew",
        "one_liner": "Return asymmetry. Negative = more large losses than gains",
        "explanation": (
            "Measures how asymmetric the return distribution is. "
            "Negative skew (< -1) means occasional large losses — "
            "typical of DCA without stop-loss. Positive skew means "
            "occasional large wins."
        ),
        "explanation_fr": (
            "Mesure l'asymétrie de la distribution des rendements. "
            "Skew négatif (< -1) = grosses pertes occasionnelles — "
            "typique du DCA sans stop-loss. Skew positif = gros "
            "gains occasionnels."
        ),
        "thresholds": [
            (-99, "extreme neg", "#ef4444"),
            (-1, "negative", "#f97316"),
            (-0.5, "mild neg", "#eab308"),
            (0, "symmetric", "#84cc16"),
            (0.5, "positive", "#22c55e"),
        ],
        "source": "Carver",
    },
    "kurtosis": {
        "name": "Excess Kurtosis",
        "abbrev": "Kurt",
        "one_liner": "Tail heaviness. >3 = fat tails, extreme events more frequent",
        "explanation": (
            "Excess kurtosis above 3 means extreme gains and losses "
            "are more frequent than a normal distribution predicts. "
            "Risk models assuming normality underestimate real risk."
        ),
        "explanation_fr": (
            "Un excès de kurtosis > 3 signifie que les événements "
            "extrêmes sont plus fréquents que prévu par une loi "
            "normale. Les modèles de risque gaussiens sous-estiment "
            "le risque réel."
        ),
        "thresholds": [
            (-99, "thin tails", "#22c55e"),
            (3, "fat tails", "#eab308"),
            (6, "extreme", "#ef4444"),
        ],
        "source": "Carver",
    },
    "profit_concentration": {
        "name": "Profit Concentration",
        "abbrev": "Conc.",
        "one_liner": "How much profit depends on top trades. Low = robust",
        "explanation": (
            "If removing the top 1-2 trades collapses profit to zero, "
            "the edge may be luck, not a repeatable pattern. "
            "Robust strategies remain profitable without their best trades."
        ),
        "explanation_fr": (
            "Si retirer les 1-2 meilleurs trades fait tomber le profit "
            "à zéro, l'edge est peut-être de la chance, pas un pattern "
            "répétable. Les stratégies robustes restent rentables sans "
            "leurs meilleurs trades."
        ),
        "thresholds": [],
        "source": "Community",
    },
    "dof": {
        "name": "Degrees of Freedom",
        "abbrev": "DoF",
        "one_liner": "Trades / params ratio. Higher = more reliable results",
        "explanation": (
            "The ratio of trades to optimized parameters. With too "
            "few trades per parameter, the optimizer can perfectly "
            "fit noise. Aim for at least 30 trades per parameter; "
            "below 10 is a red flag."
        ),
        "explanation_fr": (
            "Le ratio trades / paramètres optimisés. Avec trop "
            "peu de trades par paramètre, l'optimiseur peut "
            "parfaitement fitter le bruit. Visez au moins 30 "
            "trades par paramètre ; en dessous de 10 c'est un "
            "signal d'alarme."
        ),
        "thresholds": [
            (0, "critical", "#ef4444"),
            (5, "low", "#f97316"),
            (10, "acceptable", "#eab308"),
            (30, "good", "#22c55e"),
        ],
        "source": "",
    },
    "expected_max_sharpe": {
        "name": "Expected Maximum Sharpe",
        "abbrev": "E[max SR]",
        "one_liner": "Best Sharpe expected from N random trials: sqrt(2*ln(N))",
        "explanation": (
            "When you test N parameter combinations, the best Sharpe "
            "is biased upward. From pure noise, the expected max is "
            "sqrt(2*ln(N)). If your Sharpe is below this, "
            "the result is likely a statistical artifact."
        ),
        "explanation_fr": (
            "Quand vous testez N combinaisons, le meilleur Sharpe est "
            "biaisé à la hausse. Du bruit pur donne E[max] = sqrt(2*ln(N)). "
            "Si votre Sharpe est en dessous, le résultat est probablement "
            "un artefact statistique."
        ),
        "thresholds": [],
        "source": "Bailey & Lopez de Prado",
    },
}


VERDICT_GUIDE: dict[str, tuple[str, str]] = {
    "A": (
        "All checks passed. Deploy to dry-run with your intended position "
        "size. Monitor for 2-4 weeks, comparing live fills to backtest "
        "assumptions. Watch for slippage and execution differences.",
        "Tous les critères validés. Déployer en dry-run avec la taille "
        "de position prévue. Surveiller 2 à 4 semaines en comparant les "
        "fills live aux hypothèses du backtest. Surveiller le slippage "
        "et les différences d'exécution.",
    ),
    "B": (
        "Most checks passed. Safe to dry-run at reduced size (50%). "
        "Review any failed checks — they may not be dealbreakers. "
        "Run for 4+ weeks before considering live capital.",
        "La plupart des critères validés. Dry-run à taille réduite "
        "(50%) est sûr. Examiner les critères échoués — ils ne sont "
        "pas forcément rédhibitoires. Tourner 4+ semaines avant "
        "d'engager du capital réel.",
    ),
    "C": (
        "Mixed signals. Do NOT deploy yet. Common fixes: add more data "
        "history, reduce parameter count, freeze unstable params, or "
        "switch loss function (try CalmarHyperOptLoss).",
        "Signaux mitigés. NE PAS déployer pour l'instant. Corrections "
        "courantes : ajouter plus d'historique, réduire le nombre de "
        "paramètres, fixer les params instables, ou changer de "
        "fonction de perte (essayer CalmarHyperOptLoss).",
    ),
    "D": (
        "Most criteria failed. The strategy likely overfits or has no "
        "durable edge. Try: simplify entry/exit logic, increase "
        "--wf-min-test-trades, check if market regime changed.",
        "La plupart des critères ont échoué. La stratégie surapprend "
        "probablement ou n'a pas d'edge durable. Essayer : simplifier "
        "la logique d'entrée/sortie, augmenter --wf-min-test-trades, "
        "vérifier si le régime de marché a changé.",
    ),
    "F": (
        "Critical failure — the strategy lost money out-of-sample or "
        "shows clear overfitting. Go back to strategy design. If the "
        "equity curve degrades window-by-window, the edge is decaying.",
        "Échec critique — la stratégie a perdu de l'argent "
        "hors-échantillon ou montre un surapprentissage évident. "
        "Revenir à la conception de la stratégie. Si la courbe "
        "d'equity se dégrade fenêtre par fenêtre, l'edge se dissipe.",
    ),
}


PERCENTILE_HINT = "p5 = worst 5% of scenarios, p50 = median (typical), p95 = best 5% of scenarios."
PERCENTILE_HINT_FR = "p5 = pire 5% des scénarios, p50 = médiane (typique), p95 = meilleur 5%."


# ---------------------------------------------------------------------------
# Hyperopt-specific metrics
# ---------------------------------------------------------------------------

METRIC_GLOSSARY.update(
    {
        "sharpe": {
            "name": "Sharpe Ratio",
            "abbrev": "Sharpe",
            "one_liner": ("Risk-adjusted return. >1 good, >2 excellent"),
            "explanation": (
                "Annualized return divided by annualized volatility. "
                "Measures how much return you get per unit of risk. "
                "Above 1 is good, above 2 is excellent — but "
                "in-sample Sharpe above 3 is almost always "
                "overfitting."
            ),
            "explanation_fr": (
                "Rendement annualisé divisé par la volatilité. "
                "Mesure le rendement par unité de risque. Au-dessus "
                "de 1 = bon, au-dessus de 2 = excellent — mais un "
                "Sharpe in-sample > 3 est presque toujours du "
                "surapprentissage."
            ),
            "thresholds": [
                (-99, "losing", "#ef4444"),
                (0, "flat", "#f97316"),
                (1.0, "good", "#84cc16"),
                (2.0, "excellent", "#22c55e"),
                (3.0, "suspicious", "#eab308"),
            ],
            "source": "Sharpe",
        },
        "sortino": {
            "name": "Sortino Ratio",
            "abbrev": "Sortino",
            "one_liner": ("Like Sharpe but only penalizes downside. >2 good"),
            "explanation": (
                "Annualized return divided by downside deviation "
                "only. Unlike Sharpe, upside volatility is not "
                "penalized. Better for strategies with positively "
                "skewed returns."
            ),
            "explanation_fr": (
                "Rendement annualisé divisé par la déviation baissière "
                "uniquement. Contrairement au Sharpe, la volatilité "
                "haussière n'est pas pénalisée. Mieux adapté aux "
                "stratégies à skew positif."
            ),
            "thresholds": [
                (-99, "losing", "#ef4444"),
                (0, "flat", "#f97316"),
                (1.0, "acceptable", "#eab308"),
                (2.0, "good", "#84cc16"),
                (4.0, "excellent", "#22c55e"),
            ],
            "source": "Sortino & Price",
        },
        "win_rate": {
            "name": "Win Rate",
            "abbrev": "WR",
            "one_liner": "Fraction of trades that are profitable",
            "explanation": (
                "Percentage of trades that closed with a profit. "
                "A high win rate (>60%) feels comfortable but means "
                "nothing without considering payoff ratio. A 40% "
                "win rate with 3:1 payoff beats 80% with 0.3:1."
            ),
            "explanation_fr": (
                "Pourcentage de trades clôturés avec un profit. "
                "Un taux élevé (>60%) rassure mais ne veut rien dire "
                "sans le ratio de payoff. Un taux de 40% avec un "
                "payoff de 3:1 bat 80% avec 0.3:1."
            ),
            "thresholds": [
                (0, "very low", "#ef4444"),
                (0.35, "low", "#f97316"),
                (0.50, "balanced", "#eab308"),
                (0.60, "good", "#84cc16"),
                (0.75, "high", "#22c55e"),
            ],
            "source": "",
        },
        "payoff_ratio": {
            "name": "Payoff Ratio",
            "abbrev": "Payoff",
            "one_liner": ("Avg win / avg loss. >1.5 good for trend, <1 OK for DCA"),
            "explanation": (
                "Average winning trade divided by average losing "
                "trade. High payoff (>2) means winners are much "
                "larger than losers — typical of momentum "
                "strategies. DCA strategies often have payoff "
                "below 1 but compensate with high win rate."
            ),
            "explanation_fr": (
                "Gain moyen divisé par la perte moyenne. Un payoff > 2 "
                "= les gagnants sont bien plus gros que les perdants. "
                "Les stratégies DCA ont souvent un payoff < 1 mais "
                "compensent par un taux de gain élevé."
            ),
            "thresholds": [
                (0, "tiny wins", "#ef4444"),
                (0.5, "low", "#f97316"),
                (1.0, "balanced", "#eab308"),
                (1.5, "good", "#84cc16"),
                (2.5, "excellent", "#22c55e"),
            ],
            "source": "Van Tharp",
        },
        "cagr": {
            "name": "Compound Annual Growth Rate",
            "abbrev": "CAGR",
            "one_liner": ("Annualized return. Comparable across timeranges"),
            "explanation": (
                "The geometric mean annual return. Unlike total "
                "profit, CAGR is comparable across different "
                "backtest durations. A 50% CAGR on 2 years is "
                "more meaningful than 100% total profit on an "
                "unknown period."
            ),
            "explanation_fr": (
                "Le rendement annuel géométrique moyen. Contrairement "
                "au profit total, le CAGR est comparable entre "
                "différentes durées de backtest. Un CAGR de 50% sur "
                "2 ans est plus significatif que 100% de profit total "
                "sur une période inconnue."
            ),
            "thresholds": [
                (-99, "losing", "#ef4444"),
                (0, "flat", "#f97316"),
                (0.20, "moderate", "#eab308"),
                (0.50, "good", "#84cc16"),
                (1.0, "excellent", "#22c55e"),
            ],
            "source": "",
        },
    }
)


# ---------------------------------------------------------------------------
# Sampler glossary
# ---------------------------------------------------------------------------

SAMPLER_GLOSSARY: dict[str, dict] = {
    "NSGAIIISampler": {
        "name": "NSGA-III",
        "one_liner": "Genetic multi-objective, good diversity. Default.",
        "one_liner_fr": "Génétique multi-objectif, bonne diversité. Par défaut.",
        "explanation": (
            "Evolutionary algorithm that maintains a diverse "
            "population across the Pareto front. Best when the "
            "loss function has multiple implicit objectives."
        ),
        "explanation_fr": (
            "Algorithme évolutionnaire qui maintient une population "
            "diversifiée sur le front de Pareto. Idéal quand la "
            "fonction de perte a plusieurs objectifs implicites."
        ),
        "when_to_use": (
            "Default choice. Works well with all loss functions, "
            "especially multi-metric ones like MoutonMeanRev."
        ),
        "when_to_use_fr": (
            "Choix par défaut. Fonctionne bien avec toutes les "
            "fonctions de perte, surtout les multi-métriques "
            "comme MoutonMeanRev."
        ),
    },
    "NSGAIISampler": {
        "name": "NSGA-II (older variant)",
        "one_liner": "Genetic multi-objective, less diverse than III.",
        "one_liner_fr": "Génétique multi-objectif, moins divers que III.",
        "explanation": (
            "Predecessor of NSGA-III with less sophisticated "
            "diversity maintenance. Rarely better."
        ),
        "explanation_fr": (
            "Prédécesseur de NSGA-III avec un maintien de "
            "diversité moins sophistiqué. Rarement meilleur."
        ),
        "when_to_use": "Try if NSGA-III results are unsatisfying.",
        "when_to_use_fr": "Essayez si les résultats NSGA-III ne sont pas satisfaisants.",
    },
    "TPESampler": {
        "name": "TPE (Tree-structured Parzen Estimator)",
        "one_liner": "Bayesian, fast convergence on single-objective.",
        "one_liner_fr": "Bayésien, convergence rapide sur objectif unique.",
        "explanation": (
            "Models the search space as a probability "
            "distribution and focuses sampling on promising "
            "regions. Converges faster than genetic algorithms "
            "but explores less diversity."
        ),
        "explanation_fr": (
            "Modélise l'espace de recherche comme une distribution "
            "de probabilité et concentre l'échantillonnage sur les "
            "régions prometteuses. Converge plus vite que les "
            "algorithmes génétiques mais explore moins."
        ),
        "when_to_use": (
            "Best for simple losses (Sharpe, Calmar) where "
            "you want fast convergence. Use with 300-500 epochs."
        ),
        "when_to_use_fr": (
            "Idéal pour les pertes simples (Sharpe, Calmar) quand "
            "on veut une convergence rapide. 300-500 epochs."
        ),
    },
    "CmaEsSampler": {
        "name": "CMA-ES",
        "one_liner": "Gradient-free, for continuous spaces. Powerful.",
        "one_liner_fr": "Sans gradient, pour espaces continus. Puissant.",
        "explanation": (
            "Adapts a covariance matrix to model correlations "
            "between parameters. Excellent for continuous "
            "parameter spaces where parameters interact."
        ),
        "explanation_fr": (
            "Adapte une matrice de covariance pour modéliser les "
            "corrélations entre paramètres. Excellent pour les "
            "espaces continus avec interactions entre paramètres."
        ),
        "when_to_use": (
            "Best when most parameters are continuous "
            "(FloatRange, DecimalParameter). Less effective "
            "with many categorical or integer parameters."
        ),
        "when_to_use_fr": (
            "Idéal quand la majorité des paramètres sont continus "
            "(FloatRange, DecimalParameter). Moins efficace avec "
            "beaucoup de paramètres catégoriels ou entiers."
        ),
    },
    "GPSampler": {
        "name": "GP (Gaussian Process)",
        "one_liner": "Gaussian process model. Expensive, thorough.",
        "one_liner_fr": "Modèle de processus gaussien. Coûteux, exhaustif.",
        "explanation": (
            "Fits a Gaussian process to model the loss "
            "surface. Very sample-efficient but slow with "
            "many parameters (>10)."
        ),
        "explanation_fr": (
            "Ajuste un processus gaussien pour modéliser la "
            "surface de perte. Très efficace en échantillons "
            "mais lent avec beaucoup de paramètres (>10)."
        ),
        "when_to_use": (
            "Best for strategies with few parameters (<8) "
            "where each epoch is expensive."
        ),
        "when_to_use_fr": (
            "Idéal pour les stratégies avec peu de paramètres "
            "(<8) où chaque epoch est coûteux."
        ),
    },
    "QMCSampler": {
        "name": "QMC (Quasi-Monte Carlo)",
        "one_liner": "Uniform exploration. Pure random, no learning.",
        "one_liner_fr": "Exploration uniforme. Aléatoire pur, pas d'apprentissage.",
        "explanation": (
            "Low-discrepancy sequence that covers the search "
            "space more evenly than pure random. Does not "
            "learn from previous results."
        ),
        "explanation_fr": (
            "Séquence à faible discrépance qui couvre l'espace "
            "de recherche plus uniformément que le pur aléatoire. "
            "N'apprend pas des résultats précédents."
        ),
        "when_to_use": (
            "Use for initial landscape mapping before "
            "switching to a learning sampler."
        ),
        "when_to_use_fr": (
            "Pour cartographier l'espace de recherche "
            "avant de passer à un échantillonneur qui apprend."
        ),
    },
}


# ---------------------------------------------------------------------------
# Loss function glossary
# ---------------------------------------------------------------------------

LOSS_GLOSSARY: dict[str, dict] = {
    "MoutonMeanRevHyperOptLoss": {
        "name": "Mouton Mean-Reversion Loss",
        "one_liner": "8 weighted metrics tuned for DCA strategies",
        "best_for": "DCA / mean-reversion with safety orders",
        "metrics": (
            "CAGR 25%, K-ratio 18%, quarterly consistency 14%, "
            "PF 13%, payoff 8%, diversity 8%, TUW 8%, "
            "confidence 6%. Gates: concentration sigmoid + "
            "exponential drawdown."
        ),
        "hard_filters": ("Trades >= 40, pairs >= 5, WR >= 50%, DD <= 50%, training >= 30d."),
    },
    "MoutonMomentumHyperOptLoss": {
        "name": "Mouton Momentum Loss",
        "one_liner": ("9 weighted metrics tuned for trend-following"),
        "best_for": "Momentum / trend-following strategies",
        "metrics": (
            "CAGR 22%, payoff 16%, Sharpe 14%, tail ratio "
            "12%, PF 10%, quarterly 9%, diversity 6%, TUW 6%, "
            "confidence 5%. Gates: consecutive loss sigmoid + "
            "exponential drawdown."
        ),
        "hard_filters": ("Trades >= 30, pairs >= 5, DD <= 45%, payoff >= 0.3, training >= 30d."),
    },
    "MyProfitDrawDownHyperOptLoss": {
        "name": "Profit-Drawdown Loss",
        "one_liner": "Simple profit minus drawdown penalty",
        "best_for": "General-purpose baseline",
        "metrics": ("Profit minus (drawdown * profit * DRAWDOWN_MULT)."),
        "hard_filters": ("Relative drawdown <= 45%, account DD <= 45%."),
    },
    "SharpeHyperOptLoss": {
        "name": "Sharpe Ratio Loss",
        "one_liner": "Maximize annualized Sharpe ratio",
        "best_for": "Risk-adjusted momentum strategies",
        "metrics": "Annualized Sharpe ratio (daily returns).",
        "hard_filters": "None.",
    },
    "SharpeHyperOptLossDaily": {
        "name": "Sharpe Ratio Loss (Daily)",
        "one_liner": "Sharpe on daily-bucketed returns",
        "best_for": "More stable with many trades",
        "metrics": "Sharpe on daily P&L buckets.",
        "hard_filters": "None.",
    },
    "SortinoHyperOptLoss": {
        "name": "Sortino Ratio Loss",
        "one_liner": "Maximize Sortino — penalizes downside only",
        "best_for": "Positively skewed returns",
        "metrics": "Annualized Sortino ratio.",
        "hard_filters": "None.",
    },
    "SortinoHyperOptLossDaily": {
        "name": "Sortino Ratio Loss (Daily)",
        "one_liner": "Sortino on daily-bucketed returns",
        "best_for": "Same as Sortino, more stable",
        "metrics": "Sortino ratio on daily P&L buckets.",
        "hard_filters": "None.",
    },
    "CalmarHyperOptLoss": {
        "name": "Calmar Ratio Loss",
        "one_liner": "Maximize return / max drawdown",
        "best_for": "Low-drawdown strategies, patient DCA",
        "metrics": "Annualized Calmar ratio.",
        "hard_filters": "None.",
    },
    "MaxDrawDownHyperOptLoss": {
        "name": "Max Drawdown Loss",
        "one_liner": "Minimize maximum drawdown",
        "best_for": "Capital preservation",
        "metrics": "Profit + max drawdown penalty.",
        "hard_filters": "None.",
    },
    "MaxDrawDownRelativeHyperOptLoss": {
        "name": "Max Drawdown Relative Loss",
        "one_liner": "Minimize drawdown relative to profit",
        "best_for": "Profit-drawdown balance",
        "metrics": "Profit weighted by relative account DD.",
        "hard_filters": "None.",
    },
    "MaxDrawDownPerPairHyperOptLoss": {
        "name": "Max Drawdown Per Pair Loss",
        "one_liner": "Control per-pair drawdown extremes",
        "best_for": "Multi-pair with concentration risk",
        "metrics": "Total DD + per-pair DD.",
        "hard_filters": "None.",
    },
    "ProfitDrawDownHyperOptLoss": {
        "name": "Profit-Drawdown Loss (built-in)",
        "one_liner": "Upstream profit vs drawdown balance",
        "best_for": "General-purpose, upstream default",
        "metrics": "Profit penalized by drawdown.",
        "hard_filters": "None.",
    },
    "ShortTradeDurHyperOptLoss": {
        "name": "Short Trade Duration Loss",
        "one_liner": "Maximize profit, minimize trade duration",
        "best_for": "Scalping, fast trades",
        "metrics": "Profit weighted by inverse holding time.",
        "hard_filters": "None.",
    },
    "OnlyProfitHyperOptLoss": {
        "name": "Only Profit Loss",
        "one_liner": "Pure profit maximization, no risk adjust",
        "best_for": "Baseline comparison only",
        "metrics": "Negative total profit.",
        "hard_filters": "None.",
    },
    "MultiMetricHyperOptLoss": {
        "name": "Multi-Metric Loss",
        "one_liner": "Configurable weighted metric combination",
        "best_for": "Custom multi-objective optimization",
        "metrics": "User-defined weighted combination.",
        "hard_filters": "Configurable.",
    },
}


# ---------------------------------------------------------------------------
# Hyperopt next-steps guidance
# ---------------------------------------------------------------------------

HYPEROPT_NEXT_STEPS: dict[str, str] = {
    "profitable": (
        "The best epoch is profitable. Validate these parameters "
        "with walk-forward analysis before deploying: "
        "freqtrade walk-forward --strategy {strategy}. "
        "Hyperopt finds parameters that fit training data — WFA "
        "tests whether they generalize to unseen data."
    ),
    "unprofitable": (
        "No profitable configuration was found. Common causes: "
        "the strategy has no real edge, the search space is too "
        "constrained, or the timerange is too short. Try: expand "
        "the timerange, simplify the strategy, or try a different "
        "loss function."
    ),
    "high_dd": (
        "The best result has high drawdown (>30%). Consider: "
        "switch to CalmarHyperOptLoss to penalize drawdown, "
        "reduce position size, or add a max_open_trades "
        "constraint."
    ),
    "low_trades": (
        "Few trades found (<30). Statistical reliability "
        "requires at least 30-50 trades. Try: expand the "
        "timerange, lower entry thresholds, or increase "
        "--min-trades to push the optimizer."
    ),
}
