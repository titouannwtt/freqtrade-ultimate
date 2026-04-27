"""
MoutonMeanRevHyperOptLoss — Hyperopt loss for DCA / mean-reversion strategies.

Design principles (from tips.txt):
  - NOT Sharpe: inactivity is discipline, not dysfunction (#2, #70)
  - High win-rate alone is meaningless — check payoff ratio (#4, #188)
  - Frequent small profits > rare big ones — reward consistency (#6)
  - Concentrated profit = red flag — smooth sigmoid gate (#161)
  - No cherry-picking pairs — require diversity with daily correlation adj (#106)
  - Quarterly consistency: binary profitable + magnitude regularity
  - Max drawdown hard cap + exponential gate — catastrophe protection (#40, #67)
  - K-ratio of equity curve — slope/SE(slope), discriminates better than R²
  - DCA win rate floor — WR < 55% is pathological for mean-reversion

Additive metrics (weighted, sum = 1.0 — weights reflect actual influence):
  Annualized return ......... 0.22  (profit/time without DD)
  K-ratio equity curve ...... 0.22  (slope / SE — smooth growth)
  Profit factor ............. 0.12  (gross wins / gross losses)
  Payoff ratio .............. 0.08  (avg_win / avg_loss — asymmetry check)
  Quarterly consistency ..... 0.14  (profitable quarters + magnitude regularity)
  Pair diversity ............ 0.08  (daily-bucketed correlation-adjusted spread)
  TUW health ................ 0.08  (time underwater score)
  Confidence ................ 0.06  (sqrt-based sample size factor)

Multiplicative gates (intentionally dominant — documented as such):
  Concentration penalty ..... smooth sigmoid on top-2-trade profit share (#161)
  Drawdown penalty .......... exponential: score / exp(8 * max_dd)

Hard filters (return REJECT with gradient for TPE navigation):
  - Total profit <= 0           (gradient: proportional to loss magnitude)
  - Trade count < MIN_TRADES    (gradient: proportional to shortfall)
  - Win rate < MIN_WIN_RATE     (gradient: proportional to shortfall)
  - Max drawdown > MAX_DD       (gradient: proportional to excess DD)
  - Pairs < MIN_PAIRS           (gradient: proportional to shortfall)
  - Training period < 30 days
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


# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------
MIN_TRADES = 60
MIN_PAIRS = 5
MIN_WIN_RATE = 0.55
MAX_DD_ALLOWED = 0.45
QUARTERLY_DECAY = 0.85

W_RETURN = 0.22
W_K_RATIO = 0.22
W_PF = 0.12
W_PAYOFF = 0.08
W_QUARTERLY = 0.14
W_DIVERSITY = 0.08
W_TUW = 0.08
W_CONFIDENCE = 0.06

REJECT = 1e6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _annualized_return(total_profit: float, starting_balance: float, days: int) -> float:
    if days < 30 or total_profit <= 0 or starting_balance <= 0:
        return 0.0
    total_return = total_profit / starting_balance
    return (1.0 + total_return) ** (365.0 / days) - 1.0


def _k_ratio(arr: np.ndarray) -> float:
    """Kestner K-ratio: slope / SE(slope)."""
    n = len(arr)
    if n < 3:
        return 0.0
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, arr, 1)
    if slope <= 0:
        return 0.0
    residuals = arr - (slope * x + intercept)
    ss_res = np.sum(residuals ** 2)
    se_slope = np.sqrt(ss_res / (n - 2)) / np.sqrt(np.sum((x - x.mean()) ** 2))
    if se_slope < 1e-10:
        return 20.0
    return slope / se_slope


def _profit_factor(results: DataFrame) -> float:
    wins = results.loc[results["profit_abs"] > 0, "profit_abs"].sum()
    losses = abs(results.loc[results["profit_abs"] < 0, "profit_abs"].sum())
    if losses < 1e-8:
        return min(wins, 20.0)
    return wins / losses


def _payoff_ratio(results: DataFrame) -> float:
    wins = results.loc[results["profit_abs"] > 0, "profit_abs"]
    losers = results.loc[results["profit_abs"] < 0, "profit_abs"]
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = abs(losers.mean()) if len(losers) else 1e-8
    if avg_loss < 1e-8:
        return min(avg_win, 20.0)
    return avg_win / avg_loss


def _concentration_penalty(results: DataFrame) -> float:
    """Smooth sigmoid gate: top-2 trades carrying >40% profit → score crushed."""
    total = results["profit_abs"].sum()
    if total <= 0:
        return 0.0
    top2_ratio = results["profit_abs"].nlargest(2).sum() / total
    return float(np.clip(1.0 / (1.0 + np.exp(15.0 * (top2_ratio - 0.45))), 0.05, 1.0))


def _max_time_underwater(results: DataFrame, starting_balance: float) -> float:
    sorted_results = results.sort_values("close_date")
    equity = starting_balance + sorted_results["profit_abs"].cumsum()
    dates = pd.to_datetime(sorted_results["close_date"], utc=True)

    peak = equity.cummax()
    underwater = equity < peak

    if not underwater.any():
        return 0.0

    groups = (~underwater).cumsum()
    max_days = 0.0
    for _, group_dates in dates[underwater].groupby(groups[underwater]):
        if len(group_dates) >= 2:
            duration = (group_dates.iloc[-1] - group_dates.iloc[0]).total_seconds() / 86400
            max_days = max(max_days, duration)

    return max_days


def _quarterly_consistency(results: DataFrame, max_date: datetime) -> float:
    """60% weighted binary (profitable or not) + 40% magnitude regularity."""
    dates = pd.to_datetime(results["close_date"], utc=True)
    results = results.copy()
    results["_q"] = dates.dt.to_period("Q")
    ts = pd.Timestamp(max_date)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    now_q = ts.to_period("Q")

    quarters = results.groupby("_q")["profit_abs"].sum()
    if len(quarters) < 2:
        return 0.5

    weighted_profitable = 0.0
    total_weight = 0.0
    for q, profit in quarters.items():
        dist = (now_q.year - q.year) * 4 + (now_q.quarter - q.quarter)
        w = QUARTERLY_DECAY ** max(0, dist)
        total_weight += w
        if profit > 0:
            weighted_profitable += w
    pct_profitable = weighted_profitable / total_weight if total_weight > 0 else 0.0

    avg_q = quarters.mean()
    worst_q = quarters.min()
    regularity = float(np.clip(1.0 + worst_q / abs(avg_q), 0.0, 1.0)) if avg_q > 0 else 0.0

    return 0.6 * pct_profitable + 0.4 * regularity


def _pair_diversity_score(results: DataFrame) -> float:
    """Profit spread across pairs, discounted by daily-bucketed correlation."""
    if "pair" not in results.columns:
        return 0.5

    pair_profits = results.groupby("pair")["profit_abs"].sum()
    n_pairs = len(pair_profits)
    if n_pairs < MIN_PAIRS:
        return 0.0

    profitable = (pair_profits > 0).sum()
    frac_profitable = profitable / n_pairs

    total = pair_profits.sum()
    if total <= 0:
        return 0.0

    diversity = 1.0 - pair_profits.max() / total

    corr_discount = 1.0
    try:
        daily = results.assign(
            _day=pd.to_datetime(results["close_date"], utc=True).dt.date
        )
        pivot = daily.pivot_table(
            values="profit_abs", index="_day", columns="pair", aggfunc="sum"
        ).fillna(0)
        if pivot.shape[1] >= 2 and pivot.shape[0] >= 10:
            corr_matrix = pivot.corr()
            n = len(corr_matrix)
            avg_corr = (corr_matrix.values.sum() - n) / (n * n - n)
            corr_discount = 1.0 - max(0.0, avg_corr) * 0.5
    except (ValueError, KeyError):
        pass

    base = frac_profitable * 0.5 + diversity * 0.5
    return float(np.clip(base * corr_discount, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------

def _norm_return(v: float) -> float:
    if v <= 0:
        return 0.0
    return float(np.clip(np.log1p(v) / np.log1p(1.5), 0.0, 1.0))


def _norm_k_ratio(v: float) -> float:
    if v <= 0:
        return 0.0
    return float(np.clip(v / 6.0, 0.0, 1.0))


def _norm_pf(v: float) -> float:
    if v <= 1.0:
        return 0.0
    return float(np.clip((v - 1.0) / 2.0, 0.0, 1.0))


def _norm_payoff(v: float) -> float:
    if v < 0.2:
        return 0.0
    return float(np.clip((v - 0.2) / 1.3, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class MoutonMeanRevHyperOptLoss(IHyperOptLoss):
    """
    Hyperopt loss tailored for DCA / mean-reversion strategies.
    8 additive metrics with honest weights + 2 multiplicative gates (concentration, DD).
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
        starting_balance: float = 1000,
        **kwargs: Any,
    ) -> float:

        # --- Hard filters (with gradient for TPE) ---
        if trade_count < MIN_TRADES:
            return REJECT + (MIN_TRADES - trade_count) * 10

        total_profit = results["profit_abs"].sum()
        if total_profit <= 0:
            return REJECT + abs(total_profit) * 10

        days = (max_date - min_date).days
        if days < 30:
            return REJECT

        wr = (results["profit_abs"] > 0).sum() / len(results)
        if wr < MIN_WIN_RATE:
            return REJECT + (MIN_WIN_RATE - wr) * 1000

        try:
            dd_result = calculate_max_drawdown(
                results, value_col="profit_abs", starting_balance=starting_balance
            )
            max_dd = dd_result.relative_account_drawdown
        except ValueError:
            max_dd = 0.0

        if max_dd > MAX_DD_ALLOWED:
            return REJECT + max_dd * 100

        n_pairs = results["pair"].nunique() if "pair" in results.columns else 0
        if n_pairs < MIN_PAIRS:
            return REJECT + (MIN_PAIRS - n_pairs) * 100

        # --- Compute all 8 additive metrics ---
        ann_return = _annualized_return(total_profit, starting_balance, days)

        equity = (
            starting_balance + results.sort_values("close_date")["profit_abs"].cumsum()
        ).values
        k_ratio = _k_ratio(equity)

        pf = _profit_factor(results)
        payoff = _payoff_ratio(results)
        q_consistency = _quarterly_consistency(results, max_date)
        diversity = _pair_diversity_score(results)

        tuw = _max_time_underwater(results, starting_balance)
        tuw_score = float(np.clip(1.0 - max(0.0, tuw - 45) / 135, 0.0, 1.0))

        confidence = float(np.clip(
            1.0 - 1.0 / np.sqrt(trade_count / MIN_TRADES), 0.0, 1.0
        ))

        # --- Weighted additive composite (honest weights) ---
        composite = (
            _norm_return(ann_return) * W_RETURN
            + _norm_k_ratio(k_ratio) * W_K_RATIO
            + _norm_pf(pf) * W_PF
            + _norm_payoff(payoff) * W_PAYOFF
            + q_consistency * W_QUARTERLY
            + diversity * W_DIVERSITY
            + tuw_score * W_TUW
            + confidence * W_CONFIDENCE
        )

        # --- Multiplicative gates (intentionally dominant) ---
        composite *= _concentration_penalty(results)
        final = composite / np.exp(8.0 * max_dd)

        if not np.isfinite(final):
            return REJECT

        return -float(final)
