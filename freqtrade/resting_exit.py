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
import time as time_module
from datetime import UTC, datetime
from math import isclose, isfinite
from typing import TYPE_CHECKING

from ccxt import ROUND_DOWN, ROUND_UP

from freqtrade.enums import ExitCheckTuple, ExitType
from freqtrade.exceptions import DependencyException, ExchangeError, InvalidOrderException
from freqtrade.ohlcv_cache.trading_context import trading
from freqtrade.persistence import Order, Trade
from freqtrade.strategy.interface import IStrategy
from freqtrade.strategy.strategy_wrapper import strategy_safe_wrapper


if TYPE_CHECKING:
    from freqtrade.freqtradebot import FreqtradeBot


logger = logging.getLogger(__name__)

ROI_TP_TAG = "roi_tp"
CANCEL_REASON_ROI_TP = "roi_tp withdrawn"
# A re-place driven only by an amount change (target unchanged) is allowed at most once per
# trade every AMOUNT_REPLACE_EVERY_LOOPS loops: a persistent mismatch must not churn orders.
AMOUNT_REPLACE_EVERY_LOOPS = 20
# Placement failures are logged at WARNING at most once per trade per this many seconds.
FAILURE_WARN_INTERVAL_S = 3600


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
        self._loop = 0
        self._last_amount_replace: dict[int, int] = {}
        self._failure_warned_at: dict[int, float] = {}

    @property
    def strategy_opted_in(self) -> bool:
        return type(self._bot.strategy).resting_exit_price is not IStrategy.resting_exit_price

    @trading
    def process(self, trades: list[Trade] | None = None) -> None:
        if not self.strategy_opted_in:
            return
        self._loop += 1
        for trade in trades if trades is not None else Trade.get_open_trades():
            try:
                self._process_trade(trade)
            except (DependencyException, ExchangeError, InvalidOrderException) as e:
                self._warn_failure(trade, f"Resting exit for {trade.pair} not managed: {e}")

    def _warn_failure(self, trade: Trade, msg: str) -> None:
        now = time_module.monotonic()
        last = self._failure_warned_at.get(trade.id)
        if last is None or now - last >= FAILURE_WARN_INTERVAL_S:
            self._failure_warned_at[trade.id] = now
            logger.warning(f"{msg} (repeats logged at debug level for the next hour)")
        else:
            logger.debug(msg)

    def _target_price(self, trade: Trade) -> float | None:
        target = strategy_safe_wrapper(self._bot.strategy.resting_exit_price, default_retval=None)(
            trade=trade, current_time=datetime.now(UTC)
        )
        if target is None or not isfinite(target) or target <= 0:
            return None
        return self._bot.exchange.price_to_precision(
            trade.pair,
            target,
            # never below the target for a long exit, never above it for a short exit
            rounding_mode=ROUND_DOWN if trade.is_short else ROUND_UP,
        )

    def _allowed_target(self, trade: Trade) -> float | None:
        price = self._target_price(trade)
        refusal = resting_exit_refusal(self._bot.config, self._bot.exchange)
        if refusal and price is not None and not self._refusal_logged:
            logger.warning(f"Resting take-profit disabled: {refusal}.")
            self._refusal_logged = True
        return None if refusal else price

    def _refresh_funding(self, trade: Trade) -> None:
        """Same refresh as execute_trade_exit, done BEFORE the hook so the target computed
        now is the one the next loop computes again (no re-place caused by the refresh)."""
        trade.set_funding_fees(
            self._bot.exchange.get_funding_fees(
                pair=trade.pair,
                amount=trade.amount,
                is_short=trade.is_short,
                open_date=trade.date_last_filled_utc,
            )
        )

    def _amount_replace_allowed(self, trade: Trade) -> bool:
        """Amount-only change: bounded, a persistent mismatch must not churn orders."""
        last = self._last_amount_replace.get(trade.id)
        if last is not None and self._loop - last < AMOUNT_REPLACE_EVERY_LOOPS:
            return False
        self._last_amount_replace[trade.id] = self._loop
        return True

    def _needs_action(self, trade: Trade, tp: Order | None, price: float | None) -> bool:
        if price is None:
            return tp is not None
        if tp is None:
            return True
        return not (self._same_price(trade, tp, price) and self._same_amount(trade, tp))

    def _same_price(self, trade: Trade, tp: Order, price: float) -> bool:
        tick = self._bot.exchange.price_get_one_pip(trade.pair, price)
        return abs(tp.safe_placement_price - price) < tick / 2

    def _exit_amount_now(self, trade: Trade) -> float:
        """The amount an exit placed now would carry: trade.amount, capped (quietly) to the
        wallet position on account-scoped exchanges, as `_clamp_exit_to_wallet_position`."""
        amount = trade.amount
        try:
            if not self._bot.exchange.get_option("orders_are_account_scoped", False):
                return amount
            owned = abs(float(self._bot.wallets.get_owned(trade.pair, trade.base_currency) or 0))
        except Exception:
            return amount
        return amount if owned <= 0 or owned >= amount else owned

    def _same_amount(self, trade: Trade, tp: Order) -> bool:
        # Compare the order with what an exit would carry NOW. Right after a DCA fill the
        # wallet position can still be the old one (positions are cached), so the TP is first
        # placed for the old amount; comparing with trade.amount at placement would then
        # keep that half-size TP forever. A capped amount that stays capped does not churn.
        return isclose(tp.safe_amount, self._exit_amount_now(trade), rel_tol=1e-6)

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

        price = self._allowed_target(trade)
        if not self._needs_action(trade, tp, price):
            return
        if price is not None:
            # Something will be sent: refresh funding first and recompute the target with it.
            self._refresh_funding(trade)
            price = self._allowed_target(trade)
            if not self._needs_action(trade, tp, price):
                return

        if price is None:
            if tp is not None:
                self._cancel(trade, tp)
            return

        if tp is not None:
            if self._same_price(trade, tp, price) and not self._amount_replace_allowed(trade):
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
