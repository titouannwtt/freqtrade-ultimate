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


def _report_429_to_daemon(exchange_obj, method_name: str = "") -> bool:
    """Notify the daemon that a 429 was received on a direct ccxt call.

    Returns True if the daemon was notified (daemon manages the backoff),
    False if no daemon is available (caller should do its own backoff).
    """
    report_fn = getattr(exchange_obj, "_ftcache_report_429", None)
    if report_fn is None:
        return False
    try:
        report_fn(method=method_name)
        return True
    except Exception:  # noqa: S110
        return False


def _reacquire_rate_token(exchange_obj) -> None:
    """Re-acquire a rate token from ftcache before a retry.

    The @retrier decorator calls f() directly (base Exchange method),
    bypassing the CachedExchangeMixin. Without this, retries hit the
    exchange API without rate limiting.
    """
    acquire_fn = getattr(exchange_obj, "_ftcache_acquire_sync", None)
    if acquire_fn is not None:
        try:
            acquire_fn()
        except Exception:  # noqa: S110
            pass


async def _report_429_to_daemon_async(exchange_obj, method_name: str = "") -> bool:
    """Async version: notify daemon of a 429."""
    get_client = getattr(exchange_obj, "_ftcache_get_client", None)
    if get_client is None:
        return False
    client = get_client()
    if client is None:
        return False
    try:
        await asyncio.wait_for(
            client.report_429(method=method_name),
            timeout=5.0,
        )
        return True
    except Exception:  # noqa: S110
        return False


async def _reacquire_rate_token_async(exchange_obj) -> None:
    """Async version: acquire rate token directly via the client."""
    get_client = getattr(exchange_obj, "_ftcache_get_client", None)
    if get_client is None:
        return
    client = get_client()
    if client is None:
        return
    try:
        await asyncio.wait_for(
            client.acquire_rate_token(priority=None, cost=1.0),
            timeout=10.0,
        )
    except Exception:  # noqa: S110
        pass


def retrier_async(f):
    _fname = getattr(f, "__name__", "unknown")

    async def wrapper(*args, **kwargs):
        count = kwargs.pop("count", API_RETRY_COUNT)
        is_retry = count < API_RETRY_COUNT
        if is_retry and args:
            await _reacquire_rate_token_async(args[0])
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
                        _get_logging_mixin().log_once(
                            f"Kucoin 429 error, avoid triggering DDosProtection backoff delay. "
                            f"{count} tries left before giving up",
                            logmethod=logger.warning,
                        )
                        msg = ""
                    else:
                        daemon_notified = await _report_429_to_daemon_async(
                            args[0], _fname,
                        )
                        if daemon_notified:
                            logger.info(
                                "429 reported to daemon — retry will wait "
                                "for daemon rate token (priority queue)",
                            )
                        else:
                            backoff_delay = calculate_backoff(count + 1, API_RETRY_COUNT)
                            logger.info(
                                "Applying DDosProtection backoff delay: "
                                "%s (no daemon)", backoff_delay,
                            )
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
            is_retry = count < retries
            if is_retry and args:
                _reacquire_rate_token(args[0])
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
                    if isinstance(ex, DDosProtection):
                        daemon_notified = _report_429_to_daemon(args[0], _fname) if args else False
                        if daemon_notified:
                            logger.info(
                                "429 reported to daemon — retry will wait "
                                "for daemon rate token (priority queue)",
                            )
                        else:
                            backoff_delay = calculate_backoff(count + 1, retries)
                            logger.info(
                                "Applying DDosProtection backoff delay: "
                                "%s (no daemon)", backoff_delay,
                            )
                            time.sleep(backoff_delay)
                    elif isinstance(ex, RetryableOrderError):
                        backoff_delay = calculate_backoff(count + 1, retries)
                        logger.info(f"Applying RetryableOrderError backoff delay: {backoff_delay}")
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
