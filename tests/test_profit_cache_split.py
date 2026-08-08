"""What may and may not be cached in the profit statistics.

An earlier attempt cached the whole computation and was rejected by the existing suite: a
test that makes get_rate fail expects profit_all_coin to become NaN, and the cache kept
serving the previous good figure. That is the failure mode worth guarding — a dashboard
showing a stale profit precisely when the exchange starts failing.

These tests pin the split rather than the implementation: realised profit is recorded and
may be reused; unrealised profit comes from live rates and must be recomputed every call.
"""

import inspect

from freqtrade.rpc.rpc import RPC


def test_the_open_half_is_recomputed_on_every_call():
    """The property the whole design rests on."""
    src = inspect.getsource(RPC._collect_trade_statistics_data)
    open_call = src.index("_collect_open_trade_profits")
    cache_read = src.index("_closed_stats_cache")
    assert cache_read < open_call, (
        "the cache must be consulted before, and never instead of, live pricing"
    )
    assert "_collect_open_trade_profits(open_trades)" in src


def test_live_rates_are_never_read_inside_the_cached_half():
    """If get_rate appeared here, a cached entry would freeze a price."""
    src = inspect.getsource(RPC._collect_closed_statistics)
    for forbidden in ("get_rate", "calculate_profit", "current_rate"):
        assert forbidden not in src, f"{forbidden} depends on live prices and cannot be cached"


def test_the_cached_half_only_reads_recorded_fields():
    src = inspect.getsource(RPC._collect_closed_statistics)
    assert "close_profit" in src and "close_profit_abs" in src


def test_the_cache_key_is_content_addressed_not_time_based():
    """A TTL would serve figures a client can prove are outdated."""
    src = inspect.getsource(RPC._collect_trade_statistics_data)
    assert "_closed_trades_token()" in src
    assert "TTL" not in src


def test_the_token_moves_when_a_trade_closes():
    """Count and last close date: a close changes both, so the cache cannot survive one."""
    src = inspect.getsource(RPC._closed_trades_token)
    assert "func.count" in src and "func.max" in src and "close_date" in src


def test_an_unreadable_token_forces_recomputation():
    """Assume it changed: recomputing is never wrong, reusing can be."""
    src = inspect.getsource(RPC._closed_trades_token)
    assert "except Exception" in src
    assert "time.monotonic()" in src, "the fallback token must never repeat"


def test_a_pricing_failure_still_yields_nan_rather_than_a_stale_number():
    src = inspect.getsource(RPC._collect_open_trade_profits)
    assert "nan" in src
    assert "PricingError" in src and "ExchangeError" in src
