"""Resting take-profit (exit tag ``roi_tp``) kept on the exchange book.

A strategy opts in by implementing ``IStrategy.resting_exit_price(trade, current_time)``.
On every bot loop, after ``exit_positions``, ``RestingExitManager.process`` keeps exactly one
reduce-only limit exit order tagged ``roi_tp`` per open trade at the price returned by the
hook: it places it, re-prices it when the target moved by at least one tick, or cancels it
when the hook returns None.

Ownership rules (the native exit logic keeps priority):
- a trade with any other open order (entry/DCA, or an exit with another tag) is left alone:
  a DCA, an exit signal or a stop replaces the TP through ``handle_similar_open_order``;
  the TP is placed again at the first loop where the trade has no other open order;
- ``manage_open_orders`` neither times out nor replaces a ``roi_tp`` order (this module
  owns its price and lifetime);
- the order is only ever placed when it is reduce-only: ``exchange.shared_wallet`` must be
  false, and with ``stoploss_on_exchange`` the exchange must not lock the assets of the
  stop (``stoploss_blocks_assets``), otherwise every TP placement would cancel the stop.

The default hook returns None and the manager returns before touching anything, so the
feature is inert for every strategy that does not implement it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from math import isclose, isfinite
from typing import TYPE_CHECKING

from ccxt import ROUND_DOWN, ROUND_UP

from freqtrade.enums import ExitCheckTuple, ExitType
from freqtrade.exceptions import DependencyException, ExchangeError, InvalidOrderException
from freqtrade.persistence import Order, Trade
from freqtrade.strategy.interface import IStrategy
from freqtrade.strategy.strategy_wrapper import strategy_safe_wrapper


if TYPE_CHECKING:
    from freqtrade.freqtradebot import FreqtradeBot


logger = logging.getLogger(__name__)

ROI_TP_TAG = "roi_tp"
CANCEL_REASON_ROI_TP = "roi_tp withdrawn"


def is_roi_tp(order: Order | None) -> bool:
    return (
        order is not None and order.ft_order_side != "stoploss" and order.ft_order_tag == ROI_TP_TAG
    )


def resting_exit_refusal(config: dict, exchange) -> str | None:
    """Reason why a resting TP must not be placed with this config, None when it is safe."""
    if config.get("exchange", {}).get("shared_wallet", False):
        return "exchange.shared_wallet is true: the exit would not be reduce-only"
    order_types = config.get("order_types") or {}
    if order_types.get("stoploss_on_exchange") and exchange.get_option(
        "stoploss_blocks_assets", True
    ):
        return "stoploss_on_exchange on an exchange where the stop blocks the assets"
    return None


class RestingExitManager:
    def __init__(self, bot: FreqtradeBot) -> None:
        self._bot = bot
        self._refusal_logged = False

    @property
    def strategy_opted_in(self) -> bool:
        return type(self._bot.strategy).resting_exit_price is not IStrategy.resting_exit_price

    def process(self, trades: list[Trade] | None = None) -> None:
        if not self.strategy_opted_in:
            return
        for trade in trades if trades is not None else Trade.get_open_trades():
            try:
                self._process_trade(trade)
            except (DependencyException, ExchangeError, InvalidOrderException) as e:
                logger.warning(f"Resting exit for {trade.pair} not managed this loop: {e}")

    # ------------------------------------------------------------------------------------
    def _process_trade(self, trade: Trade) -> None:
        if not trade.is_open or not trade.has_open_position:
            return
        open_orders = trade.open_orders
        tps = [o for o in open_orders if is_roi_tp(o) and o.ft_order_side == trade.exit_side]
        if len(tps) != len(open_orders):
            # DCA, exit signal, stop or a manual order in flight: native logic owns the trade.
            return
        tp = tps[0] if tps else None

        target = strategy_safe_wrapper(self._bot.strategy.resting_exit_price, default_retval=None)(
            trade=trade, current_time=datetime.now(UTC)
        )
        refusal = resting_exit_refusal(self._bot.config, self._bot.exchange)
        if refusal and target is not None and not self._refusal_logged:
            logger.warning(f"Resting take-profit disabled: {refusal}.")
            self._refusal_logged = True

        if target is None or refusal or not isfinite(target) or target <= 0:
            if tp is not None:
                self._cancel(trade, tp)
            return

        price = self._bot.exchange.price_to_precision(
            trade.pair,
            target,
            # never below the target for a long exit, never above it for a short exit
            rounding_mode=ROUND_DOWN if trade.is_short else ROUND_UP,
        )
        if tp is not None:
            tick = self._bot.exchange.price_get_one_pip(trade.pair, price)
            same_price = abs(tp.safe_placement_price - price) < tick / 2
            same_amount = isclose(tp.safe_amount, trade.amount, rel_tol=1e-9)
            if same_price and same_amount:
                return
            logger.info(f"Re-pricing roi_tp of {trade.pair}: {tp.safe_placement_price} -> {price}")
            if not self._cancel(trade, tp):
                return
            if not trade.is_open or trade.has_open_orders:
                return
        self._place(trade, price)

    def _cancel(self, trade: Trade, tp: Order) -> bool:
        order = self._bot.exchange.fetch_order(tp.order_id, trade.pair)
        cancelled = self._bot.handle_cancel_exit(trade, order, tp, CANCEL_REASON_ROI_TP)
        Trade.commit()
        return cancelled

    def _place(self, trade: Trade, price: float) -> bool:
        placed = self._bot.execute_trade_exit(
            trade,
            price,
            exit_check=ExitCheckTuple(exit_type=ExitType.ROI, exit_reason=ROI_TP_TAG),
            ordertype="limit",
            skip_custom_exit_price=True,
        )
        Trade.commit()
        return placed
