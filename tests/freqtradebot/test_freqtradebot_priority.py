"""FreqtradeBot marks its trading calls (priority bots) and keeps notifications out of them."""

import inspect
from unittest.mock import MagicMock

import pytest

from freqtrade.exceptions import ExchangeError
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.ohlcv_cache import trading_context
from freqtrade.persistence import Trade
from tests.conftest import create_mock_trades_usdt, get_patched_freqtradebot


# The loop methods whose REST calls are trading-critical (DESIGN.md, 3.2).
TRADING_METHODS = [
    "handle_trade",
    "check_and_call_adjust_trade_position",
    "execute_entry",
    "execute_trade_exit",
    "manage_open_orders",
    "handle_stoploss_on_exchange",
    "_handle_external_close",
    "update_trade_state",
]


@pytest.mark.parametrize("name", TRADING_METHODS)
def test_trading_methods_are_wrapped_by_the_trading_decorator(name):
    method = getattr(FreqtradeBot, name)
    assert hasattr(method, "__wrapped__"), f"{name} is not wrapped"
    # The wrapper is ours, not some other decorator: it enters the marker.
    assert method.__closure__ and any(
        cell.cell_contents is inspect.unwrap(method) for cell in method.__closure__
    )


def test_manage_open_orders_runs_inside_a_trading_call(mocker, default_conf_usdt):
    freqtrade = get_patched_freqtradebot(mocker, default_conf_usdt)
    seen = []

    def fetch_order(order_id, pair, params=None):
        seen.append((trading_context.is_trading_call(), trading_context.is_notification_call()))
        raise ExchangeError("stop here")

    trade = MagicMock()
    trade.pair = "ETH/USDT"
    trade.open_orders = [MagicMock(order_id="o1")]
    mocker.patch("freqtrade.freqtradebot.Trade.get_open_trades", return_value=[trade])
    freqtrade.exchange.fetch_order = fetch_order

    assert not trading_context.is_trading_call()
    freqtrade.manage_open_orders()
    assert seen == [(True, False)]
    assert not trading_context.is_trading_call(), "the marker is cleared afterwards"


def test_the_marker_is_cleared_when_a_trading_method_raises(mocker, default_conf_usdt):
    freqtrade = get_patched_freqtradebot(mocker, default_conf_usdt)
    mocker.patch("freqtrade.freqtradebot.Trade.get_open_trades", side_effect=RuntimeError("db"))
    with pytest.raises(RuntimeError):
        freqtrade.manage_open_orders()
    assert not trading_context.is_trading_call()


@pytest.mark.usefixtures("init_persistence")
@pytest.mark.parametrize("fill", [False, True])
def test_entry_notifications_read_their_price_outside_the_trading_band(
    mocker, default_conf_usdt, fee, fill
):
    """Review point 4: ``_notify_enter`` and ``_notify_enter_cancel`` are sent from inside
    ``execute_entry``; their ``get_rate(side='entry')`` must not use a top-priority token."""
    freqtrade = get_patched_freqtradebot(mocker, default_conf_usdt)
    create_mock_trades_usdt(fee)
    trade = Trade.get_trades().all()[0]
    order = trade.orders[0]
    seen = []

    def get_rate(pair, refresh, side, is_short, order_book=None, ticker=None):
        seen.append(
            (side, trading_context.is_trading_call(), trading_context.is_notification_call())
        )
        return 1.0

    freqtrade.exchange.get_rate = get_rate
    freqtrade.rpc.send_msg = MagicMock()
    with trading_context.trading_call():  # as when called from execute_entry
        freqtrade._notify_enter(trade, order, "limit", fill=fill)
        if not fill:
            freqtrade._notify_enter_cancel(trade, "limit", "timeout")
    assert seen
    assert all(side == "entry" and not trading and quiet for side, trading, quiet in seen)
    # and the caller is back in its trading call afterwards
    with trading_context.trading_call():
        assert trading_context.is_trading_call()


@pytest.mark.usefixtures("init_persistence")
def test_exit_notifications_read_their_price_outside_the_trading_band(
    mocker, default_conf_usdt, fee
):
    freqtrade = get_patched_freqtradebot(mocker, default_conf_usdt)
    create_mock_trades_usdt(fee)
    trade = Trade.get_trades().all()[0]
    seen = []

    def get_rate(pair, refresh, side, is_short, order_book=None, ticker=None):
        seen.append(
            (side, trading_context.is_trading_call(), trading_context.is_notification_call())
        )
        return 1.0

    freqtrade.exchange.get_rate = get_rate
    freqtrade.rpc.send_msg = MagicMock()
    with trading_context.trading_call():
        freqtrade._notify_exit(trade, "limit", fill=False)
    assert seen == [("exit", False, True)]


@pytest.mark.usefixtures("init_persistence")
def test_the_slow_cycle_telemetry_line_is_logged_only_for_a_slow_cycle(
    mocker, default_conf_usdt, caplog
):
    freqtrade = get_patched_freqtradebot(mocker, default_conf_usdt)
    summary = {
        "PRIME": {
            "n": 3.0,
            "lock_p50_s": 0.0,
            "lock_max_s": 0.4,
            "token_p50_s": 0.05,
            "token_max_s": 1.25,
        }
    }
    freqtrade.exchange.ftcache_wait_summary = MagicMock(return_value=summary)
    with caplog.at_level("INFO"):
        freqtrade._log_ftcache_waits(slow=False)
        assert "ftcache waits" not in caplog.text
        freqtrade._log_ftcache_waits(slow=True)
    assert (
        "ftcache waits: PRIME n=3 lock p50/max=0.00/0.40s token p50/max=0.05/1.25s" in caplog.text
    )
    # the counters were read (and reset) in both cases
    assert freqtrade.exchange.ftcache_wait_summary.call_count == 2


def test_the_telemetry_never_breaks_the_loop(mocker, default_conf_usdt):
    freqtrade = get_patched_freqtradebot(mocker, default_conf_usdt)
    freqtrade.exchange.ftcache_wait_summary = MagicMock(side_effect=RuntimeError("x"))
    freqtrade._log_ftcache_waits(slow=True)  # must not raise
