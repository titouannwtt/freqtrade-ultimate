"""
ProfitDrawDownHyperOptLoss

This module defines the alternative HyperOptLoss class based on Profit &
Drawdown objective which can be used for Hyperoptimization.

Possible to change `DRAWDOWN_MULT` to penalize drawdown objective for
individual needs.
"""

from datetime import datetime

from pandas import DataFrame

from freqtrade.data.metrics import calculate_max_drawdown, calculate_underwater
from freqtrade.optimize.hyperopt import IHyperOptLoss


# smaller numbers penalize drawdowns more severely
DRAWDOWN_MULT = 0.0025


class MyProfitDrawDownHyperOptLoss(IHyperOptLoss):
    @staticmethod
    def hyperopt_loss_function(
        results: DataFrame,
        starting_balance: float,
        min_date: datetime,
        max_date: datetime,
        *args,
        **kwargs,
    ) -> float:
        total_profit = results["profit_abs"].sum()

        # Profit Drawdown
        try:
            drawdown_df = calculate_underwater(
                results, value_col="profit_abs", starting_balance=starting_balance
            )
            max_drawdown = abs(min(drawdown_df["drawdown"]))
            relative_drawdown = max(drawdown_df["drawdown_relative"])

            drawdown = calculate_max_drawdown(
                results, starting_balance=starting_balance, value_col="profit_abs"
            )
            relative_account_drawdown = drawdown.relative_account_drawdown
        except ValueError:
            relative_drawdown = 0
            relative_account_drawdown = 0
        if relative_drawdown > -45 and relative_account_drawdown > -45:
            return -1 * (
                total_profit - (relative_account_drawdown * total_profit) * (1 - DRAWDOWN_MULT)
            )
        else:
            return 1
