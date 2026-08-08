"""The local rate limiter must never be able to outlive the bot it serves."""

import threading
import time

import pytest

from freqtrade.ohlcv_cache.mixin import _LocalRateLimiter


def test_acquire_returns_within_the_ceiling_when_the_budget_never_frees():
    """Regression: this loop had no exit condition.

    It runs on a ThreadPoolExecutor worker, and concurrent.futures joins those at
    interpreter exit — so an unbounded wait here kept a fully shut-down bot alive
    forever, and its supervisor could never relaunch it. A live bot was silently down
    for 15 minutes this way.
    """
    lim = _LocalRateLimiter(exchange_budget_per_min=1.0, assumed_bots=1)
    lim._MAX_ACQUIRE_WAIT_S = 1.0
    lim.acquire(cost=1.0)  # fills the budget

    started = time.monotonic()
    lim.acquire(cost=1.0)  # budget is full; must give up rather than block forever
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, "acquire must respect its ceiling"


def test_giving_up_still_records_the_cost():
    """The call goes out either way; not recording it would corrupt the next window."""
    lim = _LocalRateLimiter(exchange_budget_per_min=1.0, assumed_bots=1)
    lim._MAX_ACQUIRE_WAIT_S = 0.5
    lim.acquire(cost=1.0)
    before = len(lim._window)
    lim.acquire(cost=1.0)
    assert len(lim._window) == before + 1


def test_critical_priority_is_never_delayed():
    """Orders must go through regardless of the local budget."""
    from freqtrade.ohlcv_cache.client import OhlcvCacheClient

    lim = _LocalRateLimiter(exchange_budget_per_min=1.0, assumed_bots=1)
    lim.acquire(cost=1.0)
    started = time.monotonic()
    lim.acquire(cost=5.0, priority=OhlcvCacheClient.CRITICAL)
    assert time.monotonic() - started < 0.5


def test_a_blocked_acquire_cannot_hold_a_thread_indefinitely():
    """The property that actually matters: the thread finishes on its own."""
    lim = _LocalRateLimiter(exchange_budget_per_min=1.0, assumed_bots=1)
    lim._MAX_ACQUIRE_WAIT_S = 1.0
    lim.acquire(cost=1.0)

    done = threading.Event()

    def worker():
        lim.acquire(cost=1.0)
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    assert done.wait(timeout=10), "the worker never returned — interpreter exit would hang"
    t.join(timeout=5)
    assert not t.is_alive()


@pytest.mark.parametrize("budget", [1.0, 10.0])
def test_acquire_succeeds_normally_when_there_is_room(budget):
    lim = _LocalRateLimiter(exchange_budget_per_min=budget, assumed_bots=1)
    started = time.monotonic()
    lim.acquire(cost=1.0)
    assert time.monotonic() - started < 0.5


# ------------------------------------------------- interpreter exit must not be held hostage


def test_python_exit_joins_workers_regardless_of_daemon_status():
    """Pins the fact that shaped the fix — and refutes the obvious remedy.

    "Make the worker thread a daemon" is the intuitive fix for a hanging interpreter exit,
    and it does not work here: concurrent.futures joins its workers explicitly at exit.
    Daemon status only matters for threads the interpreter abandons, never for ones it
    waits on by name. The fix therefore has to be bounding the work and releasing the
    executor, not relabelling the thread.
    """
    import concurrent.futures.thread as cft
    import inspect

    assert "join()" in inspect.getsource(cft._python_exit)


def test_exchange_close_releases_the_loop_executor():
    """A shut-down bot must not leave workers for the interpreter to wait on."""
    import inspect

    from freqtrade.exchange.exchange import Exchange

    src = inspect.getsource(Exchange.close)
    assert "cancel_futures=True" in src
    assert "wait=False" in src, "close() must not itself block on the workers it is releasing"
