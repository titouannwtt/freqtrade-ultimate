"""The replay must be able to liquidate — and at realistic prices.

Regression for the dynv1 audit (2026-08-19): dry-run freqtrade never liquidates an open
position (a real venue's exchange does it, the bot only detects the aftermath), and the
fake exchange holds no positions. A replayed loser could therefore ride forever: 15
legitimate liquidations in the equivalent backtest against 0 *possible* in replay. The
tool built to keep the backtester honest was silently the optimistic one.

Second half of the same bug: the synthetic markets advertised 50x max leverage for every
pair, while Hyperliquid derives maintenance margin from the real cap (mm = 1/(2*max_lev)).
136 of 493 real pairs are capped at 3x (16.7% maintenance, not 1%) — so even the
informational liquidation prices were far too generous.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from freqtrade.enums import ExitType
from freqtrade.replay.runner import _enforce_liquidations


def _trade(is_short, liq, lev=3.0, pair="ACE/USDC:USDC"):
    return SimpleNamespace(
        pair=pair, is_short=is_short, liquidation_price=liq, leverage=lev
    )


def _harness(trades, candle):
    bot = MagicMock()
    store = MagicMock()
    store.get_candle_ohlc.return_value = candle
    clock = MagicMock()
    clock.now.return_value = "2026-08-14 08:22:00"
    return bot, store, clock, patch(
        "freqtrade.replay.runner.Trade.get_open_trades", return_value=trades
    )


def test_a_short_crossing_its_liquidation_price_is_liquidated():
    t = _trade(is_short=True, liq=0.1507)
    bot, store, clock, ctx = _harness([t], {"low": 0.148, "high": 0.1534})
    with ctx:
        _enforce_liquidations(bot, store, clock, "futures")
    bot.execute_trade_exit.assert_called_once()
    args = bot.execute_trade_exit.call_args
    assert args.args[1] == 0.1507, "fills AT the liquidation price, not the candle close"
    assert args.args[2].exit_type == ExitType.LIQUIDATION


def test_a_long_is_liquidated_on_the_low_side():
    t = _trade(is_short=False, liq=95.0)
    bot, store, clock, ctx = _harness([t], {"low": 94.2, "high": 101.0})
    with ctx:
        _enforce_liquidations(bot, store, clock, "futures")
    bot.execute_trade_exit.assert_called_once()


def test_an_untouched_liquidation_price_does_nothing():
    t = _trade(is_short=True, liq=0.20)
    bot, store, clock, ctx = _harness([t], {"low": 0.14, "high": 0.16})
    with ctx:
        _enforce_liquidations(bot, store, clock, "futures")
    bot.execute_trade_exit.assert_not_called()


def test_a_trade_without_liquidation_price_is_skipped_not_crashed():
    t = _trade(is_short=True, liq=None)
    bot, store, clock, ctx = _harness([t], {"low": 0.1, "high": 99.0})
    with ctx:
        _enforce_liquidations(bot, store, clock, "futures")
    bot.execute_trade_exit.assert_not_called()


def test_a_failing_exit_does_not_abort_the_sweep():
    """One broken trade must not shield the others from liquidation."""
    t1 = _trade(is_short=True, liq=0.15, pair="AAA/USDC:USDC")
    t2 = _trade(is_short=True, liq=0.15, pair="BBB/USDC:USDC")
    bot, store, clock, ctx = _harness([t1, t2], {"low": 0.14, "high": 0.16})
    bot.execute_trade_exit.side_effect = [RuntimeError("boom"), MagicMock()]
    with ctx:
        _enforce_liquidations(bot, store, clock, "futures")
    assert bot.execute_trade_exit.call_count == 2


def test_the_hook_runs_after_the_stoploss_hook_in_the_drive_loop():
    """For a short the stop sits below the liquidation price: a candle crossing both is
    a stop exit in reality. Ordering is load-bearing, so pin it."""
    import inspect

    from freqtrade.replay import runner

    src = inspect.getsource(runner._drive_loop)
    assert src.index("_enforce_intracandle_sl") < src.index("_enforce_liquidations")


def test_synthetic_markets_carry_the_real_leverage_cap(tmp_path):
    import json

    from freqtrade.replay.exchange import ReplayExchangeMixin

    caps = {"ACE/USDC:USDC": 3, "BTC/USDC:USDC": 40}
    (tmp_path / "replay_leverage_caps.json").write_text(json.dumps(caps))
    mixin = ReplayExchangeMixin.__new__(ReplayExchangeMixin)
    mixin._replay_lev_caps = mixin._load_leverage_caps({"user_data_dir": str(tmp_path)})
    mixin._replay_lev_caps_missing = set()
    mixin._replay_min_notional = 10.0

    assert mixin._leverage_cap("ACE/USDC:USDC") == 3
    assert mixin._leverage_cap("BTC/USDC:USDC") == 40
    # Unknown pair: the documented default, not a flat 50.
    assert mixin._leverage_cap("NEW/USDC:USDC") == mixin._DEFAULT_LEVERAGE_CAP
    market = mixin._make_market("ACE/USDC:USDC")
    assert market["limits"]["leverage"]["max"] == 3
