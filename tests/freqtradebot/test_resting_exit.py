"""Resting take-profit (`roi_tp`, freqtrade/resting_exit.py): lifecycle on a fake order book."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from freqtrade.enums import ExitCheckTuple, ExitType, TradingMode
from freqtrade.persistence import Order, Trade
from freqtrade.resting_exit import ROI_TP_TAG, RestingExitManager, resting_exit_refusal
from freqtrade.strategy.interface import IStrategy
from freqtrade.util.datetime_helpers import dt_now
from tests.conftest import EXMS, get_patched_freqtradebot, patch_wallet


PAIR = "ETH/USDT"


class FakeBook:
    """In-memory exchange: orders rest until the test fills or cancels them."""

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.created: list[dict] = []
        self.fetches = 0
        self._n = 0

    def _new(self, **fields) -> dict:
        self._n += 1
        oid = f"o{self._n}"
        now = dt_now()
        order = {
            "id": oid,
            "symbol": PAIR,
            "status": "open",
            "filled": 0.0,
            "average": None,
            "cost": 0.0,
            "fee": None,
            "timestamp": int(now.timestamp() * 1000),
            "datetime": now.isoformat(),
            "info": {},
            **fields,
        }
        order["remaining"] = order["amount"]
        self.orders[oid] = order
        self.created.append(deepcopy(order))
        return deepcopy(order)

    # exchange API --------------------------------------------------------------
    def create_order(self, *, pair, ordertype, side, amount, rate, leverage, **kwargs):
        return self._new(
            type=ordertype,
            side=side,
            amount=amount,
            price=rate,
            reduceOnly=kwargs.get("reduceOnly"),
        )

    def create_stoploss(self, pair, amount, stop_price, order_types, side, leverage):
        return self._new(
            type="stop_loss_limit",
            side=side,
            amount=amount,
            price=stop_price * 0.95,
            stopPrice=stop_price,
            ft_order_type="stoploss",
        )

    def fetch_order(self, order_id, pair, params=None):
        self.fetches += 1
        return deepcopy(self.orders[order_id])

    def cancel(self, order_id, pair, amount=None):
        order = self.orders[order_id]
        if order["status"] == "open":
            order["status"] = "canceled"
        return deepcopy(order)

    # test helpers --------------------------------------------------------------
    def fill(self, order_id, qty=None, price=None):
        order = self.orders[order_id]
        qty = order["amount"] - order["filled"] if qty is None else qty
        price = price or order["price"]
        order["filled"] += qty
        order["remaining"] = order["amount"] - order["filled"]
        order["average"] = price
        order["cost"] = order["filled"] * price
        order["fee"] = {"cost": order["cost"] * 0.001, "currency": "USDT", "rate": 0.001}
        if order["remaining"] <= 1e-12:
            order["status"] = "closed"

    def open_ids(self) -> list[str]:
        return [o["id"] for o in self.orders.values() if o["status"] == "open"]


@pytest.fixture
def book(mocker):
    return install_book(mocker)


def install_book(mocker) -> FakeBook:
    book = FakeBook()
    mocker.patch.multiple(
        EXMS,
        fetch_ticker=MagicMock(return_value={"bid": 2.0, "ask": 2.02, "last": 2.0}),
        create_order=MagicMock(side_effect=book.create_order),
        create_stoploss=MagicMock(side_effect=book.create_stoploss),
        fetch_order=MagicMock(side_effect=book.fetch_order),
        fetch_stoploss_order=MagicMock(side_effect=book.fetch_order),
        cancel_order_with_result=MagicMock(side_effect=book.cancel),
        cancel_stoploss_order_with_result=MagicMock(side_effect=book.cancel),
        get_min_pair_stake_amount=MagicMock(return_value=0.0),
        get_max_pair_stake_amount=MagicMock(return_value=1e9),
        get_trades_for_order=MagicMock(return_value=[]),
        get_funding_fees=MagicMock(return_value=0.0),
    )
    patch_wallet(mocker, free=1e6)
    return book


def _bot(mocker, conf, target: dict | None, *, stop_on_exchange=False, blocks_assets=False):
    conf = deepcopy(conf)
    conf["exchange"]["shared_wallet"] = False
    if stop_on_exchange:
        conf["order_types"] = {
            "entry": "limit",
            "exit": "limit",
            "stoploss": "limit",
            "stoploss_on_exchange": True,
            "stoploss_on_exchange_interval": 60,
        }
    bot = get_patched_freqtradebot(mocker, conf)
    bot.exchange._ft_has["stoploss_blocks_assets"] = blocks_assets
    if target is not None:
        mocker.patch.object(
            type(bot.strategy),
            "resting_exit_price",
            new=lambda self, trade, current_time, **kw: target["price"],
        )
    return bot


def _open_trade(amount=10.0, rate=2.0) -> Trade:
    trade = Trade(
        pair=PAIR,
        stake_amount=amount * rate,
        amount=amount,
        amount_requested=amount,
        open_rate=rate,
        fee_open=0.001,
        fee_close=0.001,
        exchange="binance",
        is_open=True,
        open_date=dt_now() - timedelta(minutes=15),
        leverage=1.0,
        trading_mode=TradingMode.SPOT,
        strategy="StrategyTestV3",
        timeframe=5,
        stop_loss=rate * 0.65,
        initial_stop_loss=rate * 0.65,
        stop_loss_pct=-0.35,
        initial_stop_loss_pct=-0.35,
    )
    trade.orders.append(
        Order(
            ft_order_side="buy",
            ft_pair=PAIR,
            ft_is_open=False,
            ft_amount=amount,
            ft_price=rate,
            order_id="entry-1",
            status="closed",
            symbol=PAIR,
            order_type="limit",
            side="buy",
            price=rate,
            average=rate,
            amount=amount,
            filled=amount,
            remaining=0.0,
            cost=amount * rate,
            order_date=dt_now() - timedelta(minutes=15),
            order_filled_date=dt_now() - timedelta(minutes=15),
        )
    )
    trade.recalc_open_trade_value()
    Trade.session.add(trade)
    Trade.commit()
    return trade


def _tp(trade: Trade) -> Order | None:
    tps = [o for o in trade.open_orders if o.ft_order_tag == ROI_TP_TAG]
    assert len(tps) <= 1
    return tps[0] if tps else None


# --------------------------------------------------------------------------------------
def test_default_hook_is_none_and_manager_inert(mocker, default_conf_usdt, book):
    assert IStrategy.resting_exit_price(MagicMock(), MagicMock(), dt_now()) is None
    bot = _bot(mocker, default_conf_usdt, target=None)
    trade = _open_trade()
    assert not bot._resting_exit.strategy_opted_in
    bot._resting_exit.process()
    assert book.created == []
    assert book.fetches == 0
    assert not trade.has_open_orders


def test_place_and_keep(mocker, default_conf_usdt, book):
    target = {"price": 2.1000000049}
    bot = _bot(mocker, default_conf_usdt, target)
    trade = _open_trade()
    bot._resting_exit.process()
    tp = _tp(trade)
    assert tp is not None
    # rounded towards profit (up for a long exit), full position, limit exit
    assert tp.price == pytest.approx(2.10000001)
    assert tp.amount == trade.amount
    assert tp.ft_order_side == "sell" and tp.order_type == "limit"
    assert trade.exit_reason == ROI_TP_TAG
    # stable target: nothing sent on the next loops
    bot._resting_exit.process()
    bot._resting_exit.process()
    assert len(book.created) == 1


def test_reprice_at_roi_step_only_from_one_tick(mocker, default_conf_usdt, book):
    target = {"price": 2.1}
    bot = _bot(mocker, default_conf_usdt, target)
    trade = _open_trade()
    bot._resting_exit.process()
    first = _tp(trade)
    # less than one tick: rounded up to the same price, kept
    target["price"] = 2.1 - 4e-9
    bot._resting_exit.process()
    assert len(book.created) == 1
    # ROI step: lower target -> old order cancelled, new one at the new price
    target["price"] = 2.05
    bot._resting_exit.process()
    assert book.orders[first.order_id]["status"] == "canceled"
    tp = _tp(trade)
    assert tp.order_id != first.order_id
    assert tp.price == pytest.approx(2.05)
    assert book.open_ids() == [tp.order_id]
    assert trade.is_open


def test_target_none_withdraws(mocker, default_conf_usdt, book):
    target = {"price": 2.1}
    bot = _bot(mocker, default_conf_usdt, target)
    trade = _open_trade()
    bot._resting_exit.process()
    target["price"] = None
    bot._resting_exit.process()
    assert _tp(trade) is None
    assert book.open_ids() == []


def test_dca_cancels_tp_then_replaced_on_new_average(mocker, default_conf_usdt, book):
    target = {"price": None}
    bot = _bot(mocker, default_conf_usdt, target)
    trade = _open_trade()
    mocker.patch.object(
        type(bot.strategy),
        "resting_exit_price",
        new=lambda self, trade, current_time, **kw: round(trade.open_rate * 1.05, 8),
    )
    bot._resting_exit.process()
    tp1 = _tp(trade)
    assert tp1.price == pytest.approx(2.1)

    # Safety order: the native single-open-order rule cancels the TP.
    assert bot.execute_entry(PAIR, 18.0, price=1.8, trade=trade, mode="pos_adjust")
    assert book.orders[tp1.order_id]["status"] == "canceled"
    entry = [o for o in trade.open_orders if o.ft_order_side == "buy"]
    assert len(entry) == 1 and _tp(trade) is None
    # DCA in flight: the manager does not touch the trade
    bot._resting_exit.process()
    assert _tp(trade) is None

    book.fill(entry[0].order_id)
    bot.manage_open_orders()
    assert not trade.has_open_orders
    assert trade.amount == pytest.approx(20.0)
    assert trade.open_rate == pytest.approx(1.9)

    bot._resting_exit.process()
    tp2 = _tp(trade)
    assert tp2.amount == pytest.approx(20.0)
    assert tp2.price == pytest.approx(1.995)
    assert book.open_ids() == [tp2.order_id]


@pytest.mark.parametrize(
    "exit_check,tag",
    [
        (ExitCheckTuple(exit_type=ExitType.EXIT_SIGNAL), "exit_signal"),
        (ExitCheckTuple(exit_type=ExitType.STOP_LOSS), "stop_loss"),
    ],
)
def test_loop_exit_replaces_tp(mocker, default_conf_usdt, book, exit_check, tag):
    bot = _bot(mocker, default_conf_usdt, {"price": 2.1})
    trade = _open_trade()
    bot._resting_exit.process()
    tp = _tp(trade)

    assert bot.execute_trade_exit(trade, 1.99, exit_check=exit_check)
    assert book.orders[tp.order_id]["status"] == "canceled"
    assert _tp(trade) is None
    others = trade.open_orders
    assert len(others) == 1 and others[0].ft_order_tag == tag
    # the manager leaves a native exit alone (no new TP on top of it)
    bot._resting_exit.process()
    assert len(trade.open_orders) == 1 and _tp(trade) is None
    assert book.open_ids() == [others[0].order_id]


def test_tp_fill_closes_trade_and_leaves_no_open_order(mocker, default_conf_usdt, book):
    bot = _bot(mocker, default_conf_usdt, {"price": 2.1}, stop_on_exchange=True)
    trade = _open_trade()
    bot.handle_stoploss_on_exchange(trade)
    sl = trade.open_sl_orders[0]
    bot._resting_exit.process()
    tp = _tp(trade)

    book.fill(tp.order_id)
    bot.manage_open_orders()
    assert not trade.is_open
    assert trade.exit_reason == ROI_TP_TAG
    assert book.orders[sl.order_id]["status"] == "canceled"
    assert book.open_ids() == []
    assert not trade.has_open_orders and not trade.has_open_sl_orders


def test_stop_fill_cancels_resting_tp(mocker, default_conf_usdt, book):
    bot = _bot(mocker, default_conf_usdt, {"price": 2.1}, stop_on_exchange=True)
    trade = _open_trade()
    bot.handle_stoploss_on_exchange(trade)
    bot._resting_exit.process()
    sl = trade.open_sl_orders[0]
    tp = _tp(trade)

    book.fill(sl.order_id, price=1.3)
    assert bot.handle_stoploss_on_exchange(trade) is True
    assert not trade.is_open
    assert book.orders[tp.order_id]["status"] == "canceled"
    assert book.open_ids() == []
    assert not trade.has_open_orders


def test_liquidation_cancels_resting_tp(mocker, default_conf_usdt, book):
    bot = _bot(mocker, default_conf_usdt, {"price": 2.1})
    trade = _open_trade()
    bot._resting_exit.process()
    tp = _tp(trade)
    mocker.patch(
        f"{EXMS}.fetch_liquidation_fills",
        return_value=[{"liq_mark_price": 1.0, "timestamp": int(dt_now().timestamp() * 1000)}],
    )
    assert bot._handle_liquidation(trade)
    assert not trade.is_open
    assert book.orders[tp.order_id]["status"] == "canceled"
    assert not trade.has_open_orders


def test_partial_fill(mocker, default_conf_usdt, book):
    target = {"price": 2.1}
    bot = _bot(mocker, default_conf_usdt, target)
    trade = _open_trade()
    bot._resting_exit.process()
    tp = _tp(trade)

    book.fill(tp.order_id, qty=4.0)
    bot.manage_open_orders()  # still open: no timeout, no replace
    bot._resting_exit.process()
    assert _tp(trade).order_id == tp.order_id
    assert len(book.created) == 1

    # ROI step: the partially filled TP is cancelled -> native partial exit, then a new TP
    # for what is left of the position.
    target["price"] = 2.05
    bot._resting_exit.process()
    assert book.orders[tp.order_id]["status"] == "canceled"
    assert trade.is_open
    assert trade.amount == pytest.approx(6.0)
    tp2 = _tp(trade)
    assert tp2.amount == pytest.approx(6.0)
    assert tp2.price == pytest.approx(2.05)

    book.fill(tp2.order_id)
    bot.manage_open_orders()
    assert not trade.is_open
    assert book.open_ids() == []


def test_stop_on_exchange_kept_while_tp_rests(mocker, default_conf_usdt, book):
    target = {"price": 2.1}
    bot = _bot(mocker, default_conf_usdt, target, stop_on_exchange=True, blocks_assets=False)
    trade = _open_trade()
    bot.handle_stoploss_on_exchange(trade)
    sl = trade.open_sl_orders[0]
    bot._resting_exit.process()
    target["price"] = 2.05  # re-price as well
    bot._resting_exit.process()
    for _ in range(3):
        bot.handle_stoploss_on_exchange(trade)
        bot._resting_exit.process()
    assert book.orders[sl.order_id]["status"] == "open"
    assert [o.order_id for o in trade.open_sl_orders] == [sl.order_id]
    assert bot.exchange.cancel_stoploss_order_with_result.call_count == 0
    assert sum(1 for o in book.created if o.get("ft_order_type") == "stoploss") == 1
    assert sorted(book.open_ids()) == sorted([sl.order_id, _tp(trade).order_id])


def test_guard_stop_blocking_assets_refuses_tp(mocker, default_conf_usdt, book):
    bot = _bot(mocker, default_conf_usdt, {"price": 2.1}, stop_on_exchange=True, blocks_assets=True)
    trade = _open_trade()
    bot.handle_stoploss_on_exchange(trade)
    bot._resting_exit.process()
    assert _tp(trade) is None
    assert len(trade.open_sl_orders) == 1


def test_guard_shared_wallet_refuses_and_withdraws(mocker, default_conf_usdt, book, caplog):
    target = {"price": 2.1}
    bot = _bot(mocker, default_conf_usdt, target)
    trade = _open_trade()
    bot._resting_exit.process()
    tp = _tp(trade)
    assert tp is not None

    bot.config["exchange"]["shared_wallet"] = True
    bot._resting_exit.process()
    assert book.orders[tp.order_id]["status"] == "canceled"
    bot._resting_exit.process()
    assert _tp(trade) is None
    assert len(book.created) == 1
    assert "shared_wallet is true" in caplog.text
    assert resting_exit_refusal({"exchange": {"shared_wallet": True}}, bot.exchange)
    assert resting_exit_refusal({"exchange": {"shared_wallet": False}}, bot.exchange) is None


def test_roi_tp_exempt_from_unfilledtimeout(mocker, default_conf_usdt, book):
    conf = deepcopy(default_conf_usdt)
    conf["unfilledtimeout"] = {"entry": 10, "exit": 10, "unit": "minutes"}
    bot = _bot(mocker, conf, {"price": 2.1})
    trade = _open_trade()
    bot._resting_exit.process()
    tp = _tp(trade)
    old = dt_now() - timedelta(hours=3)
    tp.order_date = old
    book.orders[tp.order_id]["timestamp"] = int(old.timestamp() * 1000)
    book.orders[tp.order_id]["datetime"] = old.isoformat()
    Trade.commit()
    assert bot.strategy.ft_check_timed_out(trade, tp, dt_now())  # it would time out
    replace = mocker.spy(bot, "replace_order")
    bot.manage_open_orders()
    assert book.orders[tp.order_id]["status"] == "open"
    assert replace.call_count == 0

    # control: any other exit order still times out
    tp.ft_order_tag = "exit_signal"
    Trade.commit()
    bot.manage_open_orders()
    assert book.orders[tp.order_id]["status"] == "canceled"


@pytest.mark.parametrize("cancel_on_exit", [False, True])
def test_cancel_open_orders_on_exit_honoured(mocker, default_conf_usdt, book, cancel_on_exit):
    conf = deepcopy(default_conf_usdt)
    conf["cancel_open_orders_on_exit"] = cancel_on_exit
    bot = _bot(mocker, conf, {"price": 2.1})
    trade = _open_trade()
    bot._resting_exit.process()
    tp = _tp(trade)
    bot.process_stopped()
    assert book.orders[tp.order_id]["status"] == ("canceled" if cancel_on_exit else "open")


def test_manager_skips_closed_trades(mocker, default_conf_usdt, book):
    bot = _bot(mocker, default_conf_usdt, {"price": 2.1})
    trade = _open_trade()
    trade.is_open = False
    Trade.commit()
    RestingExitManager(bot).process([trade])
    assert book.created == []


def test_tp_bookkeeping_not_notified_only_fill(mocker, default_conf_usdt, book):
    from freqtrade.enums import RPCMessageType

    target = {"price": 2.1}
    bot = _bot(mocker, default_conf_usdt, target)
    send = mocker.patch.object(bot.rpc, "send_msg")
    trade = _open_trade()
    bot._resting_exit.process()
    target["price"] = 2.05
    bot._resting_exit.process()
    sent = [c.args[0]["type"] for c in send.call_args_list]
    assert RPCMessageType.EXIT not in sent
    assert RPCMessageType.EXIT_CANCEL not in sent

    book.fill(_tp(trade).order_id)
    bot.manage_open_orders()
    assert not trade.is_open
    sent = [c.args[0]["type"] for c in send.call_args_list]
    assert RPCMessageType.EXIT_FILL in sent
