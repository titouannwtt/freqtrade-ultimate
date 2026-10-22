"""Per-thread marker telling the ftcache client WHY a REST call is being made.

A bot flagged ``shared_ohlcv_cache.priority_bot`` sends its trading-critical calls in
the PRIME band (above CRITICAL). "Trading-critical" is not a property of the REST
endpoint: the same order-book read prices an exit (critical) and a notification
(not), and ``fetch_order`` follows a stop (critical) as well as a FreqUI refresh
(not). The caller knows, so the caller says so with a context manager and the
mixin reads it when it picks a priority.

The state is thread-local on purpose. The RPC / FreqUI threads call into the same
exchange object while the trading loop is mid-order and must not inherit its urgency
(same reasoning as the entry-pricing flag in ``mixin.py``).

Two modes, nestable, innermost wins:

* ``trading_call()``: the calls made inside are trading-critical.
* ``notification_call()``: the calls made inside only feed a message. They are never
  PRIME nor escalated to CRITICAL, even when a trading call encloses them (a
  notification is sent from inside ``execute_entry``).

Exchange-agnostic and import-light: ``freqtradebot`` uses it without caring whether
the exchange object is a ftcache mixin.
"""

from __future__ import annotations

import contextlib
import functools
import threading
from collections.abc import Callable, Iterator
from typing import Any, TypeVar


_TRADING = "trading"
_QUIET = "quiet"

_state = threading.local()

F = TypeVar("F", bound=Callable[..., Any])


def _stack() -> list[str]:
    stack = getattr(_state, "stack", None)
    if stack is None:
        stack = []
        _state.stack = stack
    return stack


@contextlib.contextmanager
def _mode(name: str) -> Iterator[None]:
    stack = _stack()
    stack.append(name)
    try:
        yield
    finally:
        stack.pop()


def trading_call() -> contextlib.AbstractContextManager[None]:
    """Mark the calls made inside the block (this thread) as trading-critical."""
    return _mode(_TRADING)


def notification_call() -> contextlib.AbstractContextManager[None]:
    """Mark the calls made inside the block (this thread) as notification-only."""
    return _mode(_QUIET)


def is_trading_call() -> bool:
    """True when the innermost marker of this thread is ``trading_call``."""
    stack = _stack()
    return bool(stack) and stack[-1] == _TRADING


def is_notification_call() -> bool:
    """True when the innermost marker of this thread is ``notification_call``."""
    stack = _stack()
    return bool(stack) and stack[-1] == _QUIET


def trading(fn: F) -> F:
    """Decorator form of :func:`trading_call` for bot methods."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with trading_call():
            return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
