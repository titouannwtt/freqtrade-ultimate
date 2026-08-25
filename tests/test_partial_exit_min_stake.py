"""A partial exit must be measured in the same unit as the minimum it is compared to.

Regression for KNEIRO, 2026-08-23. `min_exit_stake` comes from
get_min_pair_stake_amount(), which divides the exchange's notional floor by the
leverage — so it is a STAKE (margin) figure. The old check compared it against
`amount * rate`, a NOTIONAL. On a 3x trade that is too permissive by 3x:

    remaining notional  9.60  >=  min_exit_stake 4.12   -> passed
    real margin value   3.20   <  min_exit_stake 4.12   -> should have blocked

Hyperliquid then refused the order ("Order must have minimum value of $10"), the
strategy's trigger was still true, and it re-sent the same order every cycle:
32 rejections in 3 minutes.

The slice itself was never checked at all — only `amount == 0` and the remainder.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from freqtrade.freqtradebot import FreqtradeBot


def _bot(min_exit_stake):
    bot = FreqtradeBot.__new__(FreqtradeBot)
    bot.exchange = MagicMock()
    bot.exchange.amount_to_contract_precision.side_effect = lambda pair, amt: amt
    bot.exchange.get_rates.return_value = (0.0909, 0.0909)
    bot.exchange.get_min_pair_stake_amount.side_effect = lambda pair, price, sl, lev=1.0: (
        min_exit_stake
    )
    bot.exchange.get_max_pair_stake_amount.return_value = 1e9
    bot.wallets = MagicMock()
    bot.wallets.get_available_stake_amount.return_value = 1e9
    bot.strategy = MagicMock()
    bot.strategy.stoploss = -0.15
    bot.execute_trade_exit = MagicMock()
    bot.execute_entry = MagicMock()
    return bot


def _trade(amount, stake, leverage):
    t = SimpleNamespace(
        pair="KNEIRO/USDC:USDC",
        amount=amount,
        stake_amount=stake,
        leverage=leverage,
        is_short=True,
        nr_of_successful_entries=1,
    )
    t.calc_profit_ratio = lambda rate: 0.05
    return t


def _ask(bot, trade, slice_stake):
    """Drive the decrease branch with the strategy asking for -slice_stake."""
    bot.strategy._adjust_trade_position_internal.return_value = (-slice_stake, "ladder")
    bot.check_and_call_adjust_trade_position(trade)


# The incident, to the digit: 211.2 @ 0.0909 at 3x, halving the position.
INCIDENT = dict(amount=211.2, stake=6.4, leverage=3.0)


def test_the_kneiro_slice_is_now_refused():
    """9.60 notional / 3x = 3.20 margin, under the 4.12 minimum."""
    bot = _bot(min_exit_stake=4.1176)
    trade = _trade(**INCIDENT)
    _ask(bot, trade, trade.stake_amount * 0.5)
    bot.execute_trade_exit.assert_not_called()


def test_a_slice_comfortably_above_the_minimum_still_exits():
    bot = _bot(min_exit_stake=4.1176)
    # 2000 @ 0.0909 at 3x -> slice 1000*0.0909/3 = 30.3 margin, remainder likewise.
    trade = _trade(amount=2000.0, stake=60.6, leverage=3.0)
    _ask(bot, trade, trade.stake_amount * 0.5)
    bot.execute_trade_exit.assert_called_once()


def test_unleveraged_behaviour_is_unchanged():
    """Dividing by leverage is a no-op at 1x — spot users see exactly the old result."""
    bot = _bot(min_exit_stake=4.1176)
    trade = _trade(amount=1000.0, stake=90.9, leverage=1.0)
    _ask(bot, trade, trade.stake_amount * 0.5)
    bot.execute_trade_exit.assert_called_once()


def test_no_minimum_reported_does_not_block():
    """get_min_pair_stake_amount may return None; that must not stop an exit."""
    bot = _bot(min_exit_stake=None)
    trade = _trade(**INCIDENT)
    _ask(bot, trade, trade.stake_amount * 0.5)
    bot.execute_trade_exit.assert_called_once()


def test_a_slice_that_rounds_to_zero_is_still_refused():
    bot = _bot(min_exit_stake=4.1176)
    bot.exchange.amount_to_contract_precision.side_effect = lambda pair, amt: 0.0
    trade = _trade(**INCIDENT)
    _ask(bot, trade, trade.stake_amount * 0.5)
    bot.execute_trade_exit.assert_not_called()


@pytest.mark.parametrize("leverage", [1.0, 2.0, 3.0, 10.0])
def test_the_threshold_scales_with_leverage(leverage):
    """A slice worth exactly the minimum in MARGIN terms must pass at any leverage."""
    bot = _bot(min_exit_stake=4.1176)
    rate = 0.0909
    # Choose the position so that half of it is worth exactly 2x the minimum in margin,
    # keeping both the slice and the remainder clear of the floor.
    slice_margin = 4.1176 * 2
    half_amount = slice_margin * leverage / rate
    trade = _trade(amount=half_amount * 2, stake=slice_margin * 2, leverage=leverage)
    _ask(bot, trade, trade.stake_amount * 0.5)
    bot.execute_trade_exit.assert_called_once()
