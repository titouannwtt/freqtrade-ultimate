"""
MoutonMomentumHyperOptLoss — Hyperopt loss for momentum / trend-following strategies.

Design principles (from tips.txt):
  - "Cut losses early, let profits run" — reward high payoff ratio (#124)
  - Momentum works because of persistent behavioral bias (#75, #94)
  - Risk management > buy/sell rules (#73, #55) — heavy drawdown penalty
  - Sharpe IS appropriate here: momentum should trade regularly (#98)
  - Asymmetric rules for asymmetric phenomena (#141) — reward right-tail skew
  - Volatility targeting matters (#56) — penalize excessive volatility
  - Need large universe for momentum (#104) — pair diversity required
  - Low win-rate is OK if payoff compensates (#188) — don't over-weight WR
  - Robustness > peak performance (#161) — quarterly stability matters
  - Don't use Sortino (#98) — use Sharpe or Calmar

Additive metrics (weighted, sum = 1.0 — weights reflect actual influence):
  Annualized return ......... 0.22  (profit/time — primary objective)
  Payoff ratio .............. 0.16  (avg_win / avg_loss — "let profits run")
  Sharpe ratio .............. 0.14  (risk-adjusted return, normalized on 2.0 for crypto)
  Tail ratio ................ 0.12  (right tail vs left tail — skew quality)
  Profit factor ............. 0.10  (gross wins / gross losses)
  Quarterly consistency ..... 0.09  (profitable quarters + magnitude regularity)
  Pair diversity ............ 0.06  (daily-bucketed correlation-adjusted spread)
  TUW health ................ 0.06  (time underwater score)
  Confidence ................ 0.05  (sqrt-based sample size factor)

Multiplicative gates (intentionally dominant — documented as such):
  Consecutive loss penalty .. smooth sigmoid centered at 12
  Drawdown penalty .......... exponential: score / exp(5 * max_dd)

Hard filters (return REJECT with gradient for TPE navigation):
  - Total profit <= 0           (gradient: proportional to loss magnitude)
  - Trade count < MIN_TRADES    (gradient: proportional to shortfall)
  - Max drawdown > MAX_DD       (gradient: proportional to excess DD)
  - Training period < 30 days
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.constants import Config
from freqtrade.data.metrics import calculate_max_drawdown, calculate_sharpe
from freqtrade.optimize.hyperopt import IHyperOptLoss


# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------
MIN_TRADES = 30
MIN_PAIRS = 5
MAX_DD_ALLOWED = 0.45
MIN_PAYOFF_RATIO = 0.3
QUARTERLY_DECAY = 0.85

W_RETURN = 0.22
W_PAYOFF = 0.16
W_SHARPE = 0.14
W_TAIL = 0.12
W_PF = 0.10
W_QUARTERLY = 0.09
W_DIVERSITY = 0.06
W_TUW = 0.06
W_CONFIDENCE = 0.05

REJECT = 1e6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _annualized_return(total_profit: float, starting_balance: float, days: int) -> float:
    if days < 30 or total_profit <= 0 or starting_balance <= 0:
        return 0.0
    total_return = total_profit / starting_balance
    return (1.0 + total_return) ** (365.0 / days) - 1.0


def _payoff_ratio(results: DataFrame) -> float:
    wins = results.loc[results["profit_abs"] > 0, "profit_abs"]
    losers = results.loc[results["profit_abs"] < 0, "profit_abs"]
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = abs(losers.mean()) if len(losers) else 1e-8
    if avg_loss < 1e-8:
        return min(avg_win, 20.0)
    return avg_win / avg_loss


def _profit_factor(results: DataFrame) -> float:
    wins = results.loc[results["profit_abs"] > 0, "profit_abs"].sum()
    losses = abs(results.loc[results["profit_abs"] < 0, "profit_abs"].sum())
    if losses < 1e-8:
        return min(wins, 20.0)
    return wins / losses


def _tail_ratio(results: DataFrame) -> float:
    """p95 / |p5| — momentum MUST have fatter right tail than left."""
    profits = results["profit_abs"]
    if len(profits) < 20:
        return 1.0
    p95 = np.percentile(profits, 95)
    p5 = np.percentile(profits, 5)
    if abs(p5) < 1e-8:
        return min(abs(p95), 10.0) if p95 > 0 else 0.0
    return abs(p95 / p5)


def _consecutive_losses(results: DataFrame) -> int:
    is_loss = (results.sort_values("close_date")["profit_abs"] < 0).astype(int)
    if len(is_loss) == 0:
        return 0
    groups = (is_loss != is_loss.shift()).cumsum()
    streaks = is_loss.groupby(groups).sum()
    return int(streaks.max()) if len(streaks) > 0 else 0


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

def _norm_sharpe(v: float) -> float:
    if v <= 0:
        return 0.0
    return float(np.clip(v / 2.0, 0.0, 1.0))


def _norm_payoff(v: float) -> float:
    if v < 0.5:
        return 0.0
    return float(np.clip((v - 0.5) / 2.0, 0.0, 1.0))


def _norm_tail(v: float) -> float:
    if v <= 0.8:
        return 0.0
    return float(np.clip((v - 0.8) / 2.5, 0.0, 1.0))


def _norm_return(v: float) -> float:
    if v <= 0:
        return 0.0
    return float(np.clip(np.log1p(v) / np.log1p(2.0), 0.0, 1.0))


def _norm_pf(v: float) -> float:
    if v <= 1.0:
        return 0.0
    return float(np.clip((v - 1.0) / 2.0, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class MoutonMomentumHyperOptLoss(IHyperOptLoss):
    """
    Hyperopt loss tailored for momentum / trend-following strategies.
    9 additive metrics with honest weights + 2 multiplicative gates (consec losses, DD).
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

        try:
            dd_result = calculate_max_drawdown(
                results, value_col="profit_abs", starting_balance=starting_balance
            )
            max_dd = dd_result.relative_account_drawdown
        except ValueError:
            max_dd = 0.0

        if max_dd > MAX_DD_ALLOWED:
            return REJECT + max_dd * 100

        # --- Compute all 9 additive metrics ---
        sharpe = calculate_sharpe(results, min_date, max_date, starting_balance)
        payoff = _payoff_ratio(results)
        tail = _tail_ratio(results)
        ann_return = _annualized_return(total_profit, starting_balance, days)
        pf = _profit_factor(results)
        q_consistency = _quarterly_consistency(results, max_date)
        diversity = _pair_diversity_score(results)

        tuw = _max_time_underwater(results, starting_balance)
        tuw_score = float(np.clip(1.0 - max(0.0, tuw - 30) / 120, 0.0, 1.0))

        confidence = float(np.clip(
            1.0 - 1.0 / np.sqrt(trade_count / MIN_TRADES), 0.0, 1.0
        ))

        # --- Weighted additive composite (honest weights) ---
        composite = (
            _norm_return(ann_return) * W_RETURN
            + _norm_payoff(payoff) * W_PAYOFF
            + _norm_sharpe(sharpe) * W_SHARPE
            + _norm_tail(tail) * W_TAIL
            + _norm_pf(pf) * W_PF
            + q_consistency * W_QUARTERLY
            + diversity * W_DIVERSITY
            + tuw_score * W_TUW
            + confidence * W_CONFIDENCE
        )

        # --- Multiplicative gates (intentionally dominant) ---
        max_consec = _consecutive_losses(results)
        consec_penalty = 0.3 + 0.7 / (1.0 + np.exp(0.4 * (max_consec - 12)))
        composite *= consec_penalty
        final = composite / np.exp(5.0 * max_dd)

        if not np.isfinite(final):
            return REJECT

        return -float(final)
