import asyncio
import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar, cast, overload

from freqtrade.exceptions import DDosProtection, RetryableOrderError, TemporaryError
from freqtrade.mixins import LoggingMixin


logger = logging.getLogger(__name__)


def _record_metric(
    args: tuple, method_name: str, elapsed_s: float, *,
    success: bool, error_type: str | None = None,
) -> None:
    if not args or not method_name:
        return
    exchange_obj = args[0]
    metrics = getattr(exchange_obj, "_metrics", None)
    if metrics is None:
        return
    try:
        from freqtrade.exchange.exchange_metrics import ApiCall

        metrics.record(ApiCall(
            ts=time.time(),
            method=method_name,
            exchange=getattr(exchange_obj, "name", "unknown"),
            latency_ms=elapsed_s * 1000,
            cached=False,
            success=success,
            error_type=error_type,
        ))
    except Exception:  # noqa: S110
        pass


__logging_mixin = None


def _reset_logging_mixin():
    """
    Reset global logging mixin - used in tests only.
    """
    global __logging_mixin
    __logging_mixin = LoggingMixin(logger)


def _get_logging_mixin():
    # Logging-mixin to cache kucoin responses
    # Only to be used in retrier
    global __logging_mixin
    if not __logging_mixin:
        __logging_mixin = LoggingMixin(logger)
    return __logging_mixin


# Maximum default retry count.
# Functions are always called RETRY_COUNT + 1 times (for the original call)
API_RETRY_COUNT = 4
API_FETCH_ORDER_RETRY_COUNT = 5

BAD_EXCHANGES = {
    "bitmex": "Various reasons",
    "probit": "Requires additional, regular calls to `signIn()`",
    "poloniex": "Does not provide fetch_order endpoint to fetch both open and closed orders",
    "kucoinfutures": "Unsupported futures exchange",
    "poloniexfutures": "Unsupported futures exchange",
    "binancecoinm": "Unsupported futures exchange",
}

MAP_EXCHANGE_CHILDCLASS = {
    "okex": "okx",
    "gateio": "gate",
    "huboi": "htx",
}

SUPPORTED_EXCHANGES = [
    "binance",
    "binanceus",
    "binanceusdm",
    "bingx",
    "bitmart",
    "bitget",
    "bybit",
    "gate",
    "htx",
    "hyperliquid",
    "kraken",
    "krakenfutures",
    "okx",
    "myokx",
]

# either the main, or replacement methods (array) is required
EXCHANGE_HAS_REQUIRED: dict[str, list[str]] = {
    # Required / private
    "fetchOrder": ["fetchOpenOrder", "fetchClosedOrder"],
    "fetchL2OrderBook": ["fetchTicker"],
    "cancelOrder": [],
    "createOrder": [],
    "fetchBalance": [],
    # Public endpoints
    "fetchOHLCV": [],
}

EXCHANGE_HAS_OPTIONAL: dict[str, list[str]] = {
    # Private
    "fetchMyTrades": [],  # Trades for order - fee detection
    "createLimitOrder": [],
    "createMarketOrder": [],  # Either OR for orders
    # Public
    "fetchOrderBook": [],
    "fetchL2OrderBook": [],
    "fetchTicker": [],  # OR for pricing
    "fetchTickers": [],  # For volumepairlist?
    "fetchTrades": [],  # Downloading trades data
    "fetchOrders": ["fetchOpenOrders", "fetchClosedOrders"],  # ,  # Refinding balance...
    # ccxt.pro
    "watchOHLCV": [],
}

EXCHANGE_HAS_OPTIONAL_FUTURES: dict[str, list[str]] = {
    # private
    "setLeverage": [],  # Margin/Futures trading
    "setMarginMode": [],  # Margin/Futures trading
    "fetchFundingHistory": [],  # Futures trading
    # Public
    "fetchFundingRateHistory": [],  # Futures trading
    "fetchPositions": [],  # Futures trading
    "fetchLeverageTiers": ["fetchMarketLeverageTiers"],  # Futures initialization
    "fetchMarkOHLCV": [],
    "fetchIndexOHLCV": [],  # Futures additional data
    "fetchPremiumIndexOHLCV": [],
}


def calculate_backoff(retrycount, max_retries):
    """
    Calculate backoff
    """
    return (max_retries - retrycount) ** 2 + 1


def retrier_async(f):
    _fname = getattr(f, "__name__", "unknown")

    async def wrapper(*args, **kwargs):
        count = kwargs.pop("count", API_RETRY_COUNT)
        kucoin = args[0].name == "KuCoin"  # Check if the exchange is KuCoin.
        t0 = time.monotonic()
        try:
            result = await f(*args, **kwargs)
            _record_metric(args, _fname, time.monotonic() - t0, success=True)
            return result
        except TemporaryError as ex:
            error_type = "429" if isinstance(ex, DDosProtection) else "error"
            _record_metric(
                args, _fname, time.monotonic() - t0,
                success=False, error_type=error_type,
            )
            msg = f'{f.__name__}() returned exception: "{ex}". '
            if count > 0:
                msg += f"Retrying still for {count} times."
                count -= 1
                kwargs["count"] = count
                if isinstance(ex, DDosProtection):
                    if kucoin and "429000" in str(ex):
                        # Temporary fix for 429000 error on kucoin
                        # see https://github.com/freqtrade/freqtrade/issues/5700 for details.
                        _get_logging_mixin().log_once(
                            f"Kucoin 429 error, avoid triggering DDosProtection backoff delay. "
                            f"{count} tries left before giving up",
                            logmethod=logger.warning,
                        )
                        # Reset msg to avoid logging too many times.
                        msg = ""
                    else:
                        backoff_delay = calculate_backoff(count + 1, API_RETRY_COUNT)
                        logger.info(f"Applying DDosProtection backoff delay: {backoff_delay}")
                        await asyncio.sleep(backoff_delay)
                if msg:
                    logger.warning(msg)
                return await wrapper(*args, **kwargs)
            else:
                logger.warning(msg + "Giving up.")
                raise ex

    return wrapper


F = TypeVar("F", bound=Callable[..., Any])


# Type shenanigans
@overload
def retrier(_func: F) -> F: ...


@overload
def retrier(_func: F, *, retries=API_RETRY_COUNT) -> F: ...


@overload
def retrier(*, retries=API_RETRY_COUNT) -> Callable[[F], F]: ...


def retrier(_func: F | None = None, *, retries=API_RETRY_COUNT):
    def decorator(f: F) -> F:
        _fname = getattr(f, "__name__", "unknown")

        @wraps(f)
        def wrapper(*args, **kwargs):
            count = kwargs.pop("count", retries)
            t0 = time.monotonic()
            try:
                result = f(*args, **kwargs)
                _record_metric(
                    args, _fname, time.monotonic() - t0, success=True,
                )
                return result
            except (TemporaryError, RetryableOrderError) as ex:
                error_type = "429" if isinstance(ex, DDosProtection) else "error"
                _record_metric(
                    args, _fname, time.monotonic() - t0,
                    success=False, error_type=error_type,
                )
                msg = f'{f.__name__}() returned exception: "{ex}". '
                if count > 0:
                    logger.warning(msg + f"Retrying still for {count} times.")
                    count -= 1
                    kwargs.update({"count": count})
                    if isinstance(ex, DDosProtection | RetryableOrderError):
                        # increasing backoff
                        backoff_delay = calculate_backoff(count + 1, retries)
                        logger.info(f"Applying DDosProtection backoff delay: {backoff_delay}")
                        time.sleep(backoff_delay)
                    return wrapper(*args, **kwargs)
                else:
                    logger.warning(msg + "Giving up.")
                    raise ex

        return cast(F, wrapper)

    # Support both @retrier and @retrier(retries=2) syntax
    if _func is None:
        return decorator
    else:
        return decorator(_func)
