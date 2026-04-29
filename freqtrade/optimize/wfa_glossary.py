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
        "thresholds": [
            (-99, "losing", "#ef4444"),
            (0, "weak", "#f97316"),
            (0.5, "moderate", "#eab308"),
            (1.0, "good", "#22c55e"),
        ],
        "source": "Lopez de Prado",
    },
}


VERDICT_GUIDE: dict[str, str] = {
    "A": (
        "All checks passed. Deploy to dry-run with your intended position "
        "size. Monitor for 2-4 weeks, comparing live fills to backtest "
        "assumptions. Watch for slippage and execution differences."
    ),
    "B": (
        "Most checks passed. Safe to dry-run at reduced size (50%). "
        "Review any failed checks — they may not be dealbreakers. "
        "Run for 4+ weeks before considering live capital."
    ),
    "C": (
        "Mixed signals. Do NOT deploy yet. Common fixes: add more data "
        "history, reduce parameter count, freeze unstable params, or "
        "switch loss function (try CalmarHyperOptLoss)."
    ),
    "D": (
        "Most criteria failed. The strategy likely overfits or has no "
        "durable edge. Try: simplify entry/exit logic, increase "
        "--wf-min-test-trades, check if market regime changed."
    ),
    "F": (
        "Critical failure — the strategy lost money out-of-sample or "
        "shows clear overfitting. Go back to strategy design. If the "
        "equity curve degrades window-by-window, the edge is decaying."
    ),
}


PERCENTILE_HINT = "p5 = worst 5% of scenarios, p50 = median (typical), p95 = best 5% of scenarios."


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
        "one_liner": ("Genetic multi-objective, good diversity. Default."),
        "explanation": (
            "Evolutionary algorithm that maintains a diverse "
            "population across the Pareto front. Best when the "
            "loss function has multiple implicit objectives."
        ),
        "when_to_use": (
            "Default choice. Works well with all loss functions, "
            "especially multi-metric ones like MoutonMeanRev."
        ),
    },
    "NSGAIISampler": {
        "name": "NSGA-II (older variant)",
        "one_liner": ("Genetic multi-objective, less diverse than III."),
        "explanation": (
            "Predecessor of NSGA-III with less sophisticated diversity maintenance. Rarely better."
        ),
        "when_to_use": ("Try if NSGA-III results are unsatisfying."),
    },
    "TPESampler": {
        "name": "TPE (Tree-structured Parzen Estimator)",
        "one_liner": ("Bayesian, fast convergence on single-objective."),
        "explanation": (
            "Models the search space as a probability "
            "distribution and focuses sampling on promising "
            "regions. Converges faster than genetic algorithms "
            "but explores less diversity."
        ),
        "when_to_use": (
            "Best for simple losses (Sharpe, Calmar) where "
            "you want fast convergence. Use with 300-500 epochs."
        ),
    },
    "CmaEsSampler": {
        "name": "CMA-ES",
        "one_liner": ("Gradient-free, for continuous spaces. Powerful."),
        "explanation": (
            "Adapts a covariance matrix to model correlations "
            "between parameters. Excellent for continuous "
            "parameter spaces where parameters interact."
        ),
        "when_to_use": (
            "Best when most parameters are continuous "
            "(FloatRange, DecimalParameter). Less effective "
            "with many categorical or integer parameters."
        ),
    },
    "GPSampler": {
        "name": "GP (Gaussian Process)",
        "one_liner": ("Gaussian process model. Expensive, thorough."),
        "explanation": (
            "Fits a Gaussian process to model the loss "
            "surface. Very sample-efficient but slow with "
            "many parameters (>10)."
        ),
        "when_to_use": (
            "Best for strategies with few parameters (<8) where each epoch is expensive."
        ),
    },
    "QMCSampler": {
        "name": "QMC (Quasi-Monte Carlo)",
        "one_liner": ("Uniform exploration. Pure random, no learning."),
        "explanation": (
            "Low-discrepancy sequence that covers the search "
            "space more evenly than pure random. Does not "
            "learn from previous results."
        ),
        "when_to_use": (
            "Use for initial landscape mapping before switching to a learning sampler."
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
