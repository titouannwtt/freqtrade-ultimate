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
    # off, more timeouts.
    # Lowered 900s -> 240s: at 900s a single "daemon busy" spike froze live
    # bots for a full 15 min (no heartbeat, no pricing, no exit evaluation).
    # 240s caps that stall at 4 min before falling back to direct ccxt, which
    # is a much better failure mode for live bots than a 15-min freeze. Trade-off:
    # on a *cold start* (all bots restarting at once, very deep queue) a lower
    # timeout can trigger fallback-to-ccxt cascade — stagger restarts to avoid it.
    "client_timeout_s": 240,
    "client_spawn_timeout_s": 15,
    # Maximum random startup delay (seconds) applied once per client
    # singleton to stagger initial connections and avoid thundering herd
    # when many bots restart simultaneously.  Set to 0 to disable.
    "client_stagger_s": 30,
    # Feather flush cadence (seconds). Only writes dirty series.
    "flush_interval_s": 30,
    # Shared positions cache TTL (seconds). All bots share one wallet, so the
    # daemon coalesces fetch_positions into one real API call per TTL window and
    # serves it to every bot. 15s (vs the old 3s) cuts positions API pressure
    # ~5x while staying well under the bot-side 45s staleness guard
    # (mixin _STALE_POSITIONS_MAX_AGE_S), which still forces a fresh CRITICAL
    # fetch whenever a bot actually holds an open position.
    "positions_cache_ttl_s": 15,
    # --- Mixin-side positions refresher (see docs/dev/positions_refresher_plan_v2.md) ---
    # A per-bot background thread that keeps the local positions cache fresh,
    # decoupled from the OHLCV backoff, so a "daemon busy" spike can't freeze a
    # live bot for the whole client_timeout window. Phase 1 ships the config +
    # the monotonic cache guard; the thread/circuit-breaker land in later phases.
    "positions_refresh_enabled": False,  # master flag — off until the refresher lands + is piloted
    "positions_refresh_interval_s": 10,  # nominal cadence
    "positions_refresh_jitter_pct": 0.3,  # +/-30% to desync the fleet
    "positions_refresh_backoff_max_s": 120,  # cap on the adaptive backoff after consecutive failures
    "positions_soft_stale_s": 45,  # cache older than this -> best-effort direct fetch
    "positions_hard_stale_s": 90,  # circuit breaker: older than this -> refuse risky actions
    "positions_equiv_check_interval_s": 3600,  # HL public-vs-signed field cross-check cadence
    "positions_report_to_daemon": True,  # push refreshed positions to the shared cache (non-blocking)
}


# Hyperliquid API weight map.  The HL API charges a "weight" per request
# type.  Total budget is 1200 weight / minute / IP.  We convert to a
# weight-per-second TokenBucket so the daemon never exceeds the limit.
#
# Info requests cost 20 unless whitelisted at 2 (clearinghouseState, allMids,
# l2Book, ...).  candleSnapshot and fundingHistory additionally charge weight
# per items returned (1 per 60 candles / 1 per 20 funding entries) — the
# "*_per_items" entries below feed that surcharge (computed on the requested
# limit).  A 5000-candle warmup chunk therefore really costs ~104, not 4;
# underestimating this kept the daemon in permanent 429 backoff.
#
# Source: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
HL_WEIGHT_MAP: dict[str, float] = {
    "fetch": 20.0,  # candleSnapshot (base)
    "fetch_per_items": 60.0,  # +1 weight per 60 candles returned
    "fetch_max_items": 5000.0,  # candleSnapshot returns at most 5000 candles
    "funding_history": 20.0,  # fundingHistory (base)
    "funding_history_per_items": 20.0,  # +1 weight per 20 items returned
    # fundingHistory returns at most 500 entries per call, no matter the
    # requested limit.  Without this cap a limit=5000 warmup request was
    # charged 270 weight — above burst (150), deadlocking the token bucket.
    "funding_history_max_items": 500.0,
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
        # 1140 weight/min ÷ 60 = 19 weight/sec steady.  Burst must exceed the
        # largest single request cost (5000-candle chunk ≈ 104 weight) or the
        # token bucket can never grant it (tokens are capped at burst).
        "rate_per_s": HL_EFFECTIVE_BUDGET_PER_MIN / 60.0,
        "burst": 150.0,
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
