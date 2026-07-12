"""
WalkForwardLoss — multi-window robust loss for trading strategy optimization.

Designed to mitigate over-fitting that occurs when standard "best loss train"
selection picks parameters that fit one regime but fail out-of-sample.

Inspired by:
  - Pardo R. (1992) "Design, Testing, and Optimization of Trading Systems"
    — walk-forward analysis as the gold standard for robustness testing
  - Bailey D.H., Borwein J.M., López de Prado M., Zhu Q.J. (2014) "The
    Probability of Backtest Overfitting" — the multi-fold consistency check
  - López de Prado M. (2018) "Advances in Financial Machine Learning",
    Chapter 7: Cross-Validation in Finance — purging and embargo concepts
  - Bailey D.H., López de Prado M. (2014) "The Deflated Sharpe Ratio" —
    penalize backtests that benefit from multiple-testing artifacts

Algorithm:
  1. Hard rejects (same as RobustResearchHyperOptLoss):
     - too few trades, too few pairs, drawdown too high, profit factor < 1
  2. Split the train period into N=5 contiguous time windows.
  3. For each window, compute the per-window Sharpe and profit.
  4. Coverage check: at least 60% of windows must have ≥ MIN_TRADES_PER_WINDOW trades.
     Insufficient coverage = strategy concentrates in one regime = memorizer → reject.
  5. Window-consistency check:
     - Median Sharpe across windows must be > 0
     - At most N/3 windows can have negative Sharpe
     - Otherwise, the strategy is fragile across regimes → reject
  6. Aggregate loss = -(median_Sharpe - alpha * std_Sharpe).
     This rewards high median (good) and penalizes variance (overfit-prone).

Returned loss is a single scalar (lower = better, freqtrade convention).

This loss does NOT use purging or embargo (those are for ML labels with
temporal overlap, irrelevant for backtest P&L which is independent per trade).
What it DOES capture: the strategy must generate consistent positive Sharpe
across multiple chronological sub-periods of the train data. This is what
makes it robust to single-regime memorization.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.constants import Config
from freqtrade.data.metrics import calculate_max_drawdown
from freqtrade.optimize.hyperopt import IHyperOptLoss


# ── Hard reject thresholds (same spirit as RobustResearchHyperOptLoss) ──
MIN_TRADES_FLOOR = 5
MIN_DISTINCT_PAIRS = 8
MAX_DRAWDOWN = 0.25
MIN_PROFIT_FACTOR = 1.05
MIN_PROFIT_PCT = 0.05  # trades over the full train must yield ≥ 5% total profit

# ── Walk-Forward parameters ────────────────────────────────────────────
# Number of windows to split the train period into. 5 is a good compromise:
# - too few (2-3): not enough chronological diversity, easy to fit all
# - too many (10+): each window too short, noisy per-window Sharpe
WALK_FORWARD_N_WINDOWS = 5
# Minimum trades per window to consider the window "valid" (otherwise it's
# treated as no-data, contributing to the coverage check). 3 = absolute minimum
# for a meaningful Sharpe; 5+ recommended.
MIN_TRADES_PER_WINDOW = 3
# Coverage: at least this fraction of windows must be valid (≥ MIN_TRADES_PER_WINDOW)
# Below this, the strategy concentrates on few sub-periods = memorizer = reject.
MIN_WINDOW_COVERAGE = 0.60  # 60%
# Max fraction of valid windows that can have negative Sharpe before rejecting.
# A robust strategy should be positive in at least 2/3 of windows.
MAX_NEGATIVE_WINDOW_FRACTION = 0.34
# Variance penalty weight in the aggregate score:
#   score = median_sharpe - VARIANCE_WEIGHT x std_sharpe
# Higher = stronger penalty for variance across windows.
VARIANCE_WEIGHT = 0.5


def _per_window_sharpe(window_returns: np.ndarray, n_trades: int) -> float:
    """Compute a Sharpe-like ratio for a single window's returns.

    Uses sqrt(n_trades) annualization proxy (not strict annualization, just a
    scaling factor for trade-count). Capped at 252 to match the convention of
    daily Sharpe annualization.
    """
    if len(window_returns) < 2:
        return 0.0
    mean_r = float(np.mean(window_returns))
    std_r = float(np.std(window_returns, ddof=1))
    if std_r < 1e-12:
        # All same return — degenerate; return 0 to avoid spurious infinities
        return 0.0
    return mean_r / std_r * np.sqrt(min(n_trades, 252))


def _max_drawdown(equity: DataFrame, value_col: str = "equity") -> float:
    try:
        result = calculate_max_drawdown(equity, value_col=value_col)
        for attr in ("relative_account_drawdown", "relative_drawdown"):
            if hasattr(result, attr):
                return float(getattr(result, attr))
        if isinstance(result, tuple | list) and len(result) >= 6:
            return float(result[5])
    except (ValueError, ZeroDivisionError):
        return 0.0
    return 0.0


def _profit_factor(results: DataFrame) -> float:
    wins = float(results.loc[results["profit_abs"] > 0, "profit_abs"].sum())
    losses = abs(float(results.loc[results["profit_abs"] < 0, "profit_abs"].sum()))
    if losses == 0:
        return 0.0 if wins <= 0 else 10.0
    return wins / losses


def _hard_reject_loss(
    df: DataFrame, trade_count: int, config: Config, starting_balance: float
) -> float | None:
    """Run the RobustResearchHyperOptLoss-style hard rejects.

    Returns a loss value (reject) or None if the run passes every check.
    """
    min_trades_required = max(int(config.get("hyperopt_min_trades", 1) or 1), MIN_TRADES_FLOOR)
    if df.empty or trade_count < min_trades_required:
        return 1_000_000.0 + (min_trades_required - trade_count) * 1_000.0

    total_profit = float(df["profit_abs"].sum())
    if total_profit <= 0:
        return 900_000.0 + abs(total_profit)

    profit_pct = total_profit / max(starting_balance, 1.0)
    if profit_pct < MIN_PROFIT_PCT:
        return 850_000.0 + (MIN_PROFIT_PCT - profit_pct) * 100_000.0

    distinct_pairs = int(df["pair"].nunique()) if "pair" in df.columns else 1
    if distinct_pairs < MIN_DISTINCT_PAIRS:
        return 800_000.0 + (MIN_DISTINCT_PAIRS - distinct_pairs) * 10_000.0

    max_dd = _max_drawdown(df)
    if max_dd > MAX_DRAWDOWN:
        return 700_000.0 + max_dd * 100_000.0

    profit_factor = _profit_factor(df)
    if profit_factor < MIN_PROFIT_FACTOR:
        return 600_000.0 + (MIN_PROFIT_FACTOR - profit_factor) * 100_000.0

    return None


def _window_bounds(
    min_date: datetime, max_date: datetime, ts: pd.Series
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Normalize min/max date tz-awareness to match the trade timestamps."""
    if ts.dt.tz is not None and getattr(min_date, "tzinfo", None) is None:
        return pd.Timestamp(min_date).tz_localize("UTC"), pd.Timestamp(max_date).tz_localize("UTC")
    return pd.Timestamp(min_date), pd.Timestamp(max_date)


def _per_window_stats(
    df: DataFrame, min_date: datetime, max_date: datetime, starting_balance: float
) -> tuple[list[float], list[int]]:
    """Split the train period into WALK_FORWARD_N_WINDOWS windows and compute
    the per-window Sharpe-like ratio + trade count for each."""
    ts = pd.to_datetime(df["close_date"])
    wf_min, wf_max = _window_bounds(min_date, max_date, ts)
    wf_step = (wf_max - wf_min) / WALK_FORWARD_N_WINDOWS

    per_window_sharpe: list[float] = []
    per_window_n: list[int] = []
    for i in range(WALK_FORWARD_N_WINDOWS):
        w_start = wf_min + wf_step * i
        w_end = wf_min + wf_step * (i + 1)
        if i == WALK_FORWARD_N_WINDOWS - 1:
            mask = (ts >= w_start) & (ts <= w_end)
        else:
            mask = (ts >= w_start) & (ts < w_end)
        window_trades = df.loc[mask]
        n = len(window_trades)
        per_window_n.append(n)
        if n < MIN_TRADES_PER_WINDOW:
            per_window_sharpe.append(0.0)  # placeholder, marked invalid via per_window_n
            continue
        window_returns = window_trades["profit_abs"].to_numpy(dtype=float) / max(
            starting_balance, 1.0
        )
        per_window_sharpe.append(_per_window_sharpe(window_returns, n))

    return per_window_sharpe, per_window_n


class WalkForwardLoss(IHyperOptLoss):
    """Multi-window robust loss function for hyperopt.

    Returns a scalar loss (lower = better, freqtrade convention) that rewards
    consistent positive Sharpe across N chronological sub-periods of the
    training data. Strongly rejects strategies that concentrate gains in one
    sub-period (regime memorizers).

    Use with any sampler (PlateauSampler, TPESampler, etc.) — the loss
    function carries the robustness logic, the sampler just chooses params.
    """

    @staticmethod
    def hyperopt_loss_function(
        *,
        results: DataFrame,
        trade_count: int,
        min_date: datetime,
        max_date: datetime,
        config: Config,
        processed: dict[str, DataFrame] | None = None,
        backtest_stats: dict[str, Any] | None = None,
        starting_balance: float | None = None,
        **kwargs: Any,
    ) -> float:
        starting_balance = float(starting_balance or config.get("dry_run_wallet", 10_000) or 10_000)
        df = results.sort_values("close_date").copy()

        # ── 1. Hard rejects (same spirit as RobustResearchHyperOptLoss) ──
        df["equity"] = starting_balance + df["profit_abs"].cumsum()
        reject = _hard_reject_loss(df, trade_count, config, starting_balance)
        if reject is not None:
            return reject

        # ── 2-3. Walk-forward window split + per-window Sharpe ──────
        per_window_sharpe, per_window_n = _per_window_stats(
            df, min_date, max_date, starting_balance
        )

        # ── 4. Coverage check ────────────────────────────────────────
        valid_mask = [n >= MIN_TRADES_PER_WINDOW for n in per_window_n]
        n_valid = sum(valid_mask)
        coverage = n_valid / WALK_FORWARD_N_WINDOWS
        if coverage < MIN_WINDOW_COVERAGE:
            return 500_000.0 + (MIN_WINDOW_COVERAGE - coverage) * 100_000.0

        valid_sharpes = np.array(
            [s for s, valid in zip(per_window_sharpe, valid_mask, strict=False) if valid]
        )

        # ── 5. Window-consistency checks ────────────────────────────
        median_sharpe = float(np.median(valid_sharpes))
        if median_sharpe <= 0:
            return 400_000.0 + abs(median_sharpe) * 10_000.0

        negative_sharpes = sum(1 for s in valid_sharpes if s < 0)
        neg_fraction = negative_sharpes / n_valid
        if neg_fraction > MAX_NEGATIVE_WINDOW_FRACTION:
            return 300_000.0 + (neg_fraction - MAX_NEGATIVE_WINDOW_FRACTION) * 100_000.0

        # ── 6. Aggregate robust score ───────────────────────────────
        std_sharpe = float(np.std(valid_sharpes, ddof=0))
        robust_score = median_sharpe - VARIANCE_WEIGHT * std_sharpe

        # Optional bonus: reward higher trade count (within reason) — more
        # statistical evidence. Capped so it doesn't dominate the score.
        trade_count_bonus = min(np.log10(max(trade_count, 1)) / 4.0, 0.5)  # max +0.5

        # Convert to loss (we minimize). Negate to make higher score = lower loss.
        # The trade-count bonus reduces loss further (rewards activity).
        loss = -(robust_score + trade_count_bonus)
        return float(loss)
