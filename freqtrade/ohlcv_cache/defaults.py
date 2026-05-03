"""
Hardcoded defaults for the shared OHLCV cache daemon.

These values are shipped with the fork and used when no override is
provided via ~/.freqtrade/ftcache.yaml. Rate limits are intentionally
set below the exchange-documented thresholds to leave headroom for
order placement traffic.
"""

from pathlib import Path


def default_socket_path() -> str:
    import os

    return f"/tmp/ftcache-{os.getuid()}.sock"


def default_lock_path() -> str:
    import os

    return f"/tmp/ftcache-{os.getuid()}.lock"


def default_persistence_dir() -> Path:
    return Path.home() / ".freqtrade" / "ftcache"


def default_log_dir() -> Path:
    return Path.home() / ".freqtrade" / "ftcache" / "logs"


GLOBAL_DEFAULTS: dict = {
    "socket_path": None,  # resolved lazily via default_socket_path()
    "lock_path": None,
    "persistence_path": None,
    "log_path": None,
    "max_candles_per_series": 5000,
    "idle_series_ttl_hours": 48,
    # Keep the daemon alive comfortably longer than a typical bot cycle
    # (process_throttle_secs can be 30-60s) so it doesn't churn on warmup gaps
    # between pairlist refresh and the first trade cycle.
    "idle_daemon_shutdown_s": 600,
    "healthcheck_interval_s": 30,
    "fallback_on_error": True,
    # Single-request wall-clock budget on the client side. With N bots x
    # 40 pairs x multiple timeframes, the token bucket queue can be very
    # deep on cold start. A short timeout causes cascade failure: client
    # falls back to direct ccxt, adds API pressure, 429, daemon backs
    # off, more timeouts. 900s (15 min) lets the daemon's centralized
    # rate limiter drain the queue even under heavy contention (100+ bots).
    "client_timeout_s": 900,
    "client_spawn_timeout_s": 15,
    # Maximum random startup delay (seconds) applied once per client
    # singleton to stagger initial connections and avoid thundering herd
    # when many bots restart simultaneously.  Set to 0 to disable.
    "client_stagger_s": 30,
    # Feather flush cadence (seconds). Only writes dirty series.
    "flush_interval_s": 30,
}


# Hyperliquid API weight map.  The HL API charges a "weight" per request
# type.  Total budget is 1200 weight / minute / IP.  We convert to a
# weight-per-second TokenBucket so the daemon never exceeds the limit.
#
# Source: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits
HL_WEIGHT_MAP: dict[str, float] = {
    "fetch": 4.0,  # candleSnapshot
    "tickers": 20.0,  # info (allMids + meta)
    "positions_get": 2.0,  # clearinghouseState
    "positions_put": 0.0,  # local cache write, no API call
    "balances_get": 2.0,  # clearinghouseState
    "balances_put": 0.0,  # local cache write, no API call
    "markets": 20.0,  # meta (load_markets)
    "funding_rates": 20.0,  # info
    "leverage_tiers": 20.0,  # meta
    "acquire": 1.0,  # default for bot-side REST (orders)
}

HL_WEIGHT_BUDGET_PER_MIN = 1200
# All order traffic now goes through the daemon (acquire tokens),
# so we can use 95% of the real budget.
HL_EFFECTIVE_BUDGET_PER_MIN = int(HL_WEIGHT_BUDGET_PER_MIN * 0.95)


# Per-exchange defaults. Rate limits are "per second" budgets.
# For Hyperliquid: weight-based (1 unit = 1 weight).
# For others: flat token-based (1 unit = 1 request).
EXCHANGE_DEFAULTS: dict[str, dict] = {
    "hyperliquid": {
        # 1020 weight/min ÷ 60 = 17 weight/sec steady, burst 30
        "rate_per_s": HL_EFFECTIVE_BUDGET_PER_MIN / 60.0,
        "burst": 30.0,
        "weight_mode": True,
        "weight_budget_per_min": HL_EFFECTIVE_BUDGET_PER_MIN,
        "weight_map": HL_WEIGHT_MAP,
        "refresh_overlap_candles": 5,
        "max_candles_per_call": 5000,
        "supports_mark": True,
        "supports_funding": True,
        "skip_cache_for_cdn": False,
    },
    "binance": {
        "rate_per_s": 15,
        "burst": 30,
        "refresh_overlap_candles": 3,
        "max_candles_per_call": 1000,
        "supports_mark": True,
        "supports_funding": True,
        "skip_cache_for_cdn": True,  # binance.vision bypass
    },
    "gate": {
        "rate_per_s": 8,
        "burst": 15,
        "refresh_overlap_candles": 3,
        "max_candles_per_call": 1000,
        "supports_mark": False,
        "supports_funding": True,
    },
    "kucoin": {
        "rate_per_s": 10,
        "burst": 20,
        "refresh_overlap_candles": 3,
        "max_candles_per_call": 1500,
        "supports_mark": False,
        "supports_funding": False,
    },
    "kraken": {
        "rate_per_s": 1,
        "burst": 2,
        "refresh_overlap_candles": 3,
        "max_candles_per_call": 720,
        "supports_mark": False,
        "supports_funding": False,
    },
}


def resolve_global_config(overrides: dict | None = None) -> dict:
    cfg = dict(GLOBAL_DEFAULTS)
    if overrides:
        cfg.update(overrides)
    if cfg["socket_path"] is None:
        cfg["socket_path"] = default_socket_path()
    if cfg["lock_path"] is None:
        cfg["lock_path"] = default_lock_path()
    if cfg["persistence_path"] is None:
        cfg["persistence_path"] = str(default_persistence_dir())
    if cfg["log_path"] is None:
        cfg["log_path"] = str(default_log_dir() / "daemon.log")
    return cfg


def resolve_exchange_config(exchange_id: str, overrides: dict | None = None) -> dict:
    base = dict(EXCHANGE_DEFAULTS.get(exchange_id, {}))
    if overrides:
        base.update(overrides)
    return base
