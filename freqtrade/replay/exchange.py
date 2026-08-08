"""
ReplayExchange — a thin behavioural overlay on the *real* resolved exchange
class (e.g. ``Hyperliquid``) that swaps live network I/O for the local data
store, while leaving everything else — dry-run order creation/filling, DCA,
fee model, **funding-fee calculation**, the virtual wallet — inherited and
running exactly as in production.

Why subclass the real exchange instead of the generic ``Exchange``?
Hyperliquid carries fork-specific behaviour (funding, precision, liquidation
handling). By mixing in over the resolved class we inherit all of it. Crucially
we do **not** override ``get_funding_fees`` or ``get_fee``: in dry-run
Hyperliquid computes funding from mark + funding-rate candles via
``_fetch_and_calculate_funding_fees`` → ``refresh_latest_ohlcv``. We just serve
those candles from disk, so funding fees come out right — unlike the original
``Freqtrade_reply`` which hard-zeroed them.

What we override (network surface only):
  _init_ccxt           -> MagicMock, no network client exists
  reload_markets       -> no-op (called every process() loop)
  validate_config      -> no-op (data validated by the data store up front)
  exchange_has         -> True only for capabilities we implement
  get_markets          -> static market map built from the data store
  get_positions        -> {} (futures position polling not needed in dry-run)
  refresh_latest_ohlcv -> serve OHLCV / mark / funding from the store, clock-gated
  fetch_ticker         -> synthesise from last close +/- half-spread
  fetch_l2_order_book  -> synthesise the book consumed by dry-run fills
  check_dry_limit_..   -> use candle high/low for intra-candle stop/limit fills
  set_leverage / set_margin_mode / get_max_leverage -> no-op / passthrough
"""

from __future__ import annotations

import logging
from inspect import isclass
from unittest.mock import MagicMock

import freqtrade.exchange as exchanges
from freqtrade.enums import CandleType
from freqtrade.exchange import MAP_EXCHANGE_CHILDCLASS, Exchange
from freqtrade.replay.clock import VirtualClock
from freqtrade.replay.data_store import ReplayDataStore


logger = logging.getLogger(__name__)

_SUPPORTED_CAPABILITIES = {
    "fetchL2OrderBook",
    "fetchOHLCV",
    "fetchTicker",
    "createOrder",
    "cancelOrder",
    "fetchOrder",
    "fetchOrders",
    "fetchClosedOrders",
    "fetchBalance",
    "setLeverage",
    "setMarginMode",
}

# Minimum order notional (in stake currency) advertised by the synthetic markets.
#
# Real venues refuse dust orders — Hyperliquid rejects anything below 10 USDC of
# notional. The synthetic market used to advertise 0.0, and that omission was NOT
# cosmetic: when a DCA strategy asks for a safety order while the wallet is fully
# committed, `Wallets._check_available_stake_amount` clamps the requested stake to
# whatever is free (routinely ~1e-06 USDC). With a real minimum, `validate_stake_amount`
# returns 0 and `execute_entry()` bails out immediately. With a minimum of 0 the dust
# order sails through to `handle_similar_open_order()`, which cancels *every* open order
# of the trade to make room for it — including a pending EXIT. In dry-run that "cancel"
# first fetches the order (filling it), books the exit and closes the trade, and then the
# dust entry reopens the very same trade. Next candle the exit signal fires again on a
# position that was already sold: the same P&L is realised two or three times over.
# Observed on XYZ-MSTR (UltoscBbsqueezeLongV1, 2026-06-22): a -9.0 % trade recorded as
# -28.5 % because its full size was sold three times.
_MIN_NOTIONAL_BY_EXCHANGE = {
    "hyperliquid": 10.0,
}
_DEFAULT_MIN_NOTIONAL = 10.0


def resolve_base_exchange_class(config: dict) -> type[Exchange]:
    """
    Return the *behavioural* exchange class for this config (e.g. ``Hyperliquid``).

    Deliberately ignores the fork's ``Cached*`` variants: the shared-OHLCV-cache
    layer would only get in the way of the replay's own data feed.
    """
    name = config["exchange"]["name"]
    name = MAP_EXCHANGE_CHILDCLASS.get(name, name).title()
    cls = getattr(exchanges, name, None)
    if cls is not None and isclass(cls) and issubclass(cls, Exchange):
        return cls
    logger.warning("No dedicated class for exchange %r; using generic Exchange.", name)
    return Exchange


def build_replay_exchange(
    config: dict,
    store: ReplayDataStore,
    clock: VirtualClock,
    pairs: list[str],
    slippage_pct: float = 0.0005,
) -> Exchange:
    """Construct a replay exchange that subclasses the real resolved exchange class."""
    base_cls = resolve_base_exchange_class(config)
    replay_cls = type(f"Replay{base_cls.__name__}", (ReplayExchangeMixin, base_cls), {})
    logger.info("Replay exchange: %s (over %s)", replay_cls.__name__, base_cls.__name__)
    return replay_cls(config, store=store, clock=clock, pairs=pairs, slippage_pct=slippage_pct)


class ReplayExchangeMixin:
    """Mixin overriding only the live-network surface of the inherited exchange."""

    def __init__(
        self,
        config: dict,
        *,
        store: ReplayDataStore,
        clock: VirtualClock,
        pairs: list[str],
        slippage_pct: float = 0.0005,
    ) -> None:
        # Must exist before super().__init__ — _init_ccxt is called from there.
        self._replay_store = store
        self._replay_clock = clock
        self._slippage_pct = slippage_pct
        # Minimum order notional the synthetic markets advertise (see the module-level
        # note on _MIN_NOTIONAL_BY_EXCHANGE — 0.0 lets dust orders cancel pending exits).
        exch_name = str(config.get("exchange", {}).get("name", "")).lower()
        self._replay_min_notional = float(
            config.get(
                "replay_min_notional",
                _MIN_NOTIONAL_BY_EXCHANGE.get(exch_name, _DEFAULT_MIN_NOTIONAL),
            )
        )
        super().__init__(config, validate=False)
        self._markets = {p: self._make_market(p) for p in pairs}

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    def _make_market(self, pair: str) -> dict:
        base = pair.split("/")[0]
        quote = pair.split("/")[1].split(":")[0]
        return {
            "id": pair.replace("/", "").replace(":", ""),
            "symbol": pair,
            "base": base,
            "quote": quote,
            "settle": quote,
            "active": True,
            "spot": False,
            "swap": True,
            "future": True,
            "linear": True,
            "type": "swap",
            "contract": True,
            "contractSize": 1.0,
            # Hyperliquid perp fees (override per-run via config["fee"] if desired).
            "taker": 0.00045,
            "maker": 0.00015,
            "precision": {"amount": 8, "price": 5},
            "limits": {
                "amount": {"min": 0.0, "max": None},
                # Real minimum notional — without it, dust orders become legal and can
                # cancel a pending exit (see _MIN_NOTIONAL_BY_EXCHANGE above).
                "cost": {"min": self._replay_min_notional, "max": None},
                "price": {"min": None, "max": None},
                "market": {"min": 0, "max": None},
                "leverage": {"min": 1, "max": 50},
            },
            "info": {},
        }

    def add_pair_markets(self, pairs: list[str]) -> None:
        """Register extra (e.g. informative) pairs so they aren't rejected as unknown."""
        for pair in pairs:
            if pair not in self._markets:
                self._markets[pair] = self._make_market(pair)

    def get_markets(self, *args, **kwargs) -> dict:
        return self._markets

    def get_positions(self, *args, **kwargs) -> dict:
        return {}

    # ------------------------------------------------------------------
    # No real ccxt client
    # ------------------------------------------------------------------

    def _init_ccxt(self, exchange_conf, is_sync=True, ccxt_config=None):
        name = exchange_conf.get("name", "hyperliquid")
        mock = MagicMock()
        mock.options = {}
        mock.markets = {}
        mock.name = name
        mock.id = name.lower()
        mock.timeframes = {tf: tf for tf in ("1m", "5m", "15m", "1h", "4h", "1d")}
        mock.precisionMode = 2  # DECIMAL_PLACES — read directly by Exchange.__init__
        mock.precisionModePrice = 2
        mock.describe.return_value = {
            "has": {cap: True for cap in _SUPPORTED_CAPABILITIES},
            "timeframes": mock.timeframes,
            "precisionMode": 2,
        }
        return mock

    def reload_markets(self, force: bool = False, **kwargs) -> None:
        pass

    def validate_config(self, config: dict) -> None:
        pass

    def exchange_has(self, endpoint: str) -> bool:
        return endpoint in _SUPPORTED_CAPABILITIES

    # ------------------------------------------------------------------
    # Data feed — OHLCV, mark and funding-rate candles
    # ------------------------------------------------------------------

    def refresh_latest_ohlcv(
        self,
        pair_list: list,
        *,
        since_ms: int | None = None,
        cache: bool = True,
        drop_incomplete: bool | None = None,
    ) -> dict:
        now = self._replay_clock.now()
        drop = True if drop_incomplete is None else drop_incomplete
        results: dict = {}
        for pair, tf, c_type in pair_list:
            if cache:
                # Strategy feed: capped window, hide the forming candle, cached.
                df = self._replay_store.get_candles(
                    pair,
                    tf,
                    c_type,
                    up_to=now,
                    drop_incomplete=drop,
                    max_candles=self._replay_store.max_candles,
                )
                if not df.empty:
                    self._klines[(pair, tf, c_type)] = df
                    # Strategies requesting via get_pair_dataframe() without a
                    # candle_type resolve to SPOT; mirror so informative pairs
                    # are found regardless of how they were requested.
                    if c_type != CandleType.SPOT:
                        self._klines[(pair, tf, CandleType.SPOT)] = df
            else:
                # Funding/mark feed: honour since_ms, keep the full window
                # (no cap), don't drop the forming candle, don't cache.
                df = self._replay_store.get_candles(
                    pair,
                    tf,
                    c_type,
                    up_to=now,
                    since_ms=since_ms,
                    drop_incomplete=drop,
                    max_candles=None,
                )
            results[(pair, tf, c_type)] = df
        return results

    # ------------------------------------------------------------------
    # Price feeds — entry/exit pricing and dry-run order fills
    # ------------------------------------------------------------------

    def _price_ctype(self) -> CandleType:
        return CandleType.get_default(self.trading_mode.value)

    def fetch_ticker(self, pair: str) -> dict:
        price = self._replay_store.get_last_price(
            pair, self._replay_clock.now(), self._price_ctype()
        )
        half = price * self._slippage_pct / 2
        ts = int(self._replay_clock.now().timestamp() * 1000)
        return {
            "symbol": pair,
            "last": price,
            "bid": price - half,
            "ask": price + half,
            "high": price,
            "low": price,
            "open": price,
            "close": price,
            "baseVolume": 0.0,
            "quoteVolume": 0.0,
            "timestamp": ts,
            "datetime": self._replay_clock.now().isoformat(),
            "info": {},
        }

    def fetch_l2_order_book(self, pair: str, limit: int = 1) -> dict:
        """Synthetic book consumed by _dry_is_price_crossed() for limit-order fills."""
        price = self._replay_store.get_last_price(
            pair, self._replay_clock.now(), self._price_ctype()
        )
        half = price * self._slippage_pct / 2
        ts = int(self._replay_clock.now().timestamp() * 1000)
        return {
            "asks": [[price + half, 999_999.0]],
            "bids": [[price - half, 999_999.0]],
            "timestamp": ts,
            "datetime": self._replay_clock.now().isoformat(),
            "nonce": ts,
        }

    # ------------------------------------------------------------------
    # Intra-candle fills (stop-loss / take-profit accuracy)
    # ------------------------------------------------------------------

    def check_dry_limit_order_filled(
        self, order: dict, immediate: bool = False, orderbook: dict | None = None
    ) -> dict:
        """
        For deferred checks on open orders, replace the close+/-spread synthetic
        book with the last completed candle's high/low so stops and limit exits
        trigger whenever the candle range touched the order price.

        Caveat (documented divergence): like backtesting, we don't know whether
        the high or the low came first within the candle.
        """
        if immediate or order.get("status") == "closed":
            return super().check_dry_limit_order_filled(
                order, immediate=immediate, orderbook=orderbook
            )

        pair = order["symbol"]
        c_type = self._price_ctype()
        now = self._replay_clock.now()
        candle = None
        for tf in ("1m", "5m", "15m", self._config.get("timeframe", "1h")):
            candle = self._replay_store.get_candle_ohlc(pair, tf, c_type, now)
            if candle is not None:
                break
        if candle is None:
            return super().check_dry_limit_order_filled(order, immediate=False, orderbook=orderbook)

        low, high = candle["low"], candle["high"]
        half = low * self._slippage_pct / 2
        candle_book = {
            "asks": [
                [low + half, 999_999.0]
            ],  # market fell this low  -> fills buy limits / sell stops
            "bids": [
                [high - half, 999_999.0]
            ],  # market rose this high -> fills sell limits / buy stops
            "timestamp": int(now.timestamp() * 1000),
            "datetime": now.isoformat(),
            "nonce": 0,
        }

        # Stoploss orders: fill at the trigger price degraded by realistic slippage.
        # order["price"] is the stop-limit safety price (stoploss_on_exchange_limit_ratio,
        # ~1% beyond the trigger) — live fills only reach it on violent gaps, so using it
        # as the fill price inflated every stopped loss systematically. It remains the
        # worst-case cap: a limit order can never fill beyond its own limit.
        if order.get("ft_order_type") == "stoploss" and order.get("status") != "closed":
            from freqtrade.misc import safe_value_fallback

            stop_trigger = safe_value_fallback(
                order, self._ft_has.get("stop_price_prop", "stopPrice"), "price"
            )
            if self._dry_is_price_crossed(
                pair, order["side"], stop_trigger, candle_book, is_stop=True
            ):
                adverse = (
                    1 + self._slippage_pct if order["side"] == "buy" else 1 - self._slippage_pct
                )
                fill_price = stop_trigger * adverse
                limit_price = order.get("price")
                if limit_price:
                    fill_price = (
                        min(fill_price, limit_price)
                        if order["side"] == "buy"
                        else max(fill_price, limit_price)
                    )
                order.update(
                    {
                        "status": "closed",
                        "filled": order["amount"],
                        "remaining": 0,
                        "average": fill_price,
                        "cost": order["amount"] * fill_price,
                    }
                )
                self.add_dry_order_fee(pair, order, "maker")
            return order

        return super().check_dry_limit_order_filled(order, immediate=False, orderbook=candle_book)

    # ------------------------------------------------------------------
    # Leverage / margin — no live calls
    # ------------------------------------------------------------------

    def get_max_leverage(self, pair: str, stake_amount: float | None) -> float:
        mkt = self._markets.get(pair, {})
        return float(mkt.get("limits", {}).get("leverage", {}).get("max") or 50.0)

    def get_fee(
        self,
        symbol: str,
        order_type: str = "",
        side: str = "",
        amount: float = 1,
        price: float = 1,
        taker_or_maker: str = "maker",
    ) -> float:
        """
        Trading fee. The inherited implementation would call the (mocked) ccxt
        client and return a MagicMock; here we honour an explicit ``config["fee"]``
        override, else the market's maker/taker rate for the requested side.
        """
        if self._config.get("fee") is not None:
            return self._config["fee"]
        market = self._markets.get(symbol, {})
        return float(market.get(taker_or_maker, market.get("taker", 0.0005)))

    def set_leverage(self, leverage, pair=None, accept_fail=False) -> None:
        pass

    def set_margin_mode(self, pair, marginMode, accept_fail=False, params=None) -> None:
        pass

    # Fork-specific Hyperliquid network probes that the live loop may call:
    # neutralise them so they never touch the mocked ccxt client.
    def fetch_liquidation_fills(self, *args, **kwargs) -> list:
        return []

    # The fork persists the OHLCV cache to disk for any TRADE_MODE (incl. dry-run),
    # which a replay would otherwise overwrite — clobbering the live bots' cache.
    # We feed candles ourselves, so disable both sides entirely.
    def persist_klines(self) -> None:
        pass

    def _load_persisted_klines(self) -> None:
        pass
