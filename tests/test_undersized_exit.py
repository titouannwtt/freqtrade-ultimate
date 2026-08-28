"""Never send an exit the venue will certainly refuse for being too small.

A winning short shrinks in notional as it wins. Once `amount * rate` drops under the
exchange's minimum order value, the position can no longer be closed — and freqtrade
re-sent the doomed order every cycle. Measured over 36h on a live fleet: 5002 rejected
orders, ~139/h, against an API already returning 429s. Worst offender: KAITO, amount
20.0 at 0.31992 = $6.40 against Hyperliquid's $10 floor, retried 2746 times by one bot.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from freqtrade.freqtradebot import FreqtradeBot


def _bot(min_stake):
    bot = FreqtradeBot.__new__(FreqtradeBot)
    bot.exchange = MagicMock()
    bot.exchange.get_min_pair_stake_amount.return_value = min_stake
    bot.strategy = MagicMock()
    bot.strategy.stoploss = -0.15
    bot._undersized_exit_warned = {}
    return bot


def _trade(tid=9, leverage=3.0):
    return SimpleNamespace(id=tid, pair="KAITO/USDC:USDC", leverage=leverage)


# The incident, to the digit.
KAITO_AMOUNT, KAITO_RATE = 20.0, 0.31992


def test_the_kaito_exit_is_refused_locally():
    """6.40 notional / 3x = 2.13 in stake terms, under a 4.12 minimum."""
    bot = _bot(min_stake=4.1176)
    assert bot._exit_meets_exchange_minimum(_trade(), KAITO_AMOUNT, KAITO_RATE) is False


def test_a_normal_exit_is_untouched():
    bot = _bot(min_stake=4.1176)
    assert bot._exit_meets_exchange_minimum(_trade(), 20.0, 5.0) is True


def test_no_minimum_reported_fails_open():
    """An exchange that declares no minimum must never have its exits blocked."""
    bot = _bot(min_stake=None)
    assert bot._exit_meets_exchange_minimum(_trade(), KAITO_AMOUNT, KAITO_RATE) is True


def test_an_exchange_error_fails_open():
    bot = _bot(min_stake=4.1176)
    bot.exchange.get_min_pair_stake_amount.side_effect = RuntimeError("boom")
    assert bot._exit_meets_exchange_minimum(_trade(), KAITO_AMOUNT, KAITO_RATE) is True


def test_leverage_is_taken_into_account():
    """min_pair_stake_amount is a margin figure; the exit must be compared in margin too.
    At 1x the same notional is 6.40 of stake, comfortably over the 4.12 floor."""
    bot = _bot(min_stake=4.1176)
    assert bot._exit_meets_exchange_minimum(_trade(leverage=1.0), KAITO_AMOUNT, KAITO_RATE) is True


def test_the_warning_is_throttled_to_hourly(caplog):
    """The guard runs every cycle; the log must not."""
    bot = _bot(min_stake=4.1176)
    trade = _trade()
    for _ in range(50):
        bot._exit_meets_exchange_minimum(trade, KAITO_AMOUNT, KAITO_RATE)
    assert sum("under the exchange minimum" in r.message for r in caplog.records) == 1


def test_each_trade_warns_on_its_own():
    bot = _bot(min_stake=4.1176)
    bot._exit_meets_exchange_minimum(_trade(tid=1), KAITO_AMOUNT, KAITO_RATE)
    bot._exit_meets_exchange_minimum(_trade(tid=2), KAITO_AMOUNT, KAITO_RATE)
    assert set(bot._undersized_exit_warned) == {1, 2}


def test_a_zero_amount_is_not_judged():
    """Regression: blocking a degenerate zero-amount exit turned a previously-successful
    path into a refusal and broke handle_trade's contract (10 upstream tests)."""
    bot = _bot(min_stake=4.1176)
    assert bot._exit_meets_exchange_minimum(_trade(), 0.0, KAITO_RATE) is True


def test_a_zero_rate_is_not_judged():
    bot = _bot(min_stake=4.1176)
    assert bot._exit_meets_exchange_minimum(_trade(), KAITO_AMOUNT, 0.0) is True
