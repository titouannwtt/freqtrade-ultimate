import logging
from copy import deepcopy
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import HTTPException

from freqtrade import __version__
from freqtrade.enums import RunMode, State
from freqtrade.exceptions import OperationalException
from freqtrade.rpc import RPC
from freqtrade.rpc.api_server.api_pairlists import handleExchangePayload
from freqtrade.rpc.api_server.api_schemas import (
    CacheStatus,
    Health,
    Logs,
    MarketRequest,
    MarketResponse,
    Ping,
    PlotConfig,
    RateMetricsResponse,
    ShowConfig,
    StrategyResponse,
    SysInfo,
    Version,
)
from freqtrade.rpc.api_server.deps import (
    get_config,
    get_exchange,
    get_rpc,
    get_rpc_optional,
    verify_strategy,
)
from freqtrade.rpc.rpc import RPCException


logger = logging.getLogger(__name__)

# API version
# Pre-1.1, no version was provided
# Version increments should happen in "small" steps (1.1, 1.12, ...) unless big changes happen.
# 1.11: forcebuy and forcesell accept ordertype
# 1.12: add blacklist delete endpoint
# 1.13: forcebuy supports stake_amount
# versions 2.xx -> futures/short branch
# 2.14: Add entry/exit orders to trade response
# 2.15: Add backtest history endpoints
# 2.16: Additional daily metrics
# 2.17: Forceentry - leverage, partial force_exit
# 2.20: Add websocket endpoints
# 2.21: Add new_candle messagetype
# 2.22: Add FreqAI to backtesting
# 2.23: Allow plot config request in webserver mode
# 2.24: Add cancel_open_order endpoint
# 2.25: Add several profit values to /status endpoint
# 2.26: increase /balance output
# 2.27: Add /trades/<id>/reload endpoint
# 2.28: Switch reload endpoint to Post
# 2.29: Add /exchanges endpoint
# 2.30: new /pairlists endpoint
# 2.31: new /backtest/history/ delete endpoint
# 2.32: new /backtest/history/ patch endpoint
# 2.33: Additional weekly/monthly metrics
# 2.34: new entries/exits/mix_tags endpoints
# 2.35: pair_candles and pair_history endpoints as Post variant
# 2.40: Add hyperopt-loss endpoint
# 2.41: Add download-data endpoint
# 2.42: Add /pair_history endpoint with live data
# 2.43: Add /profit_all endpoint
# 2.44: Add candle_types parameter to download-data endpoint
# 2.45: Add price to forceexit endpoint
# 2.46: Add prepend_data to download-data endpoint
# 2.47: Add Strategy parameters
# 2.48: add /backtest/history/wallets endpoint
API_VERSION = 2.48

# Public API, requires no auth.
router_public = APIRouter()
# Private API, protected by authentication
router = APIRouter()


@router_public.get("/ping", response_model=Ping, tags=["Info"])
@router_public.head("/ping", response_model=Ping, tags=["Info"])
def ping():
    """simple ping to check if API is responsive

    Returns "starting" while the bot is still initializing (exchange,
    pairlists, etc.) and "pong" once fully ready.
    """
    from freqtrade.rpc.api_server.webserver import ApiServer

    if not ApiServer._has_rpc:
        return {"status": "starting"}
    return {"status": "pong"}


@router.get("/version", response_model=Version, tags=["Info"])
def version():
    """Bot Version info"""
    return {"version": __version__}


@router.get("/show_config", response_model=ShowConfig, tags=["Info"])
def show_config(rpc: RPC | None = Depends(get_rpc_optional), config=Depends(get_config)):
    state: State | str = ""
    strategy_version = None
    if rpc:
        state = rpc._freqtrade.state
        strategy_version = rpc._freqtrade.strategy.version()
    resp = RPC._rpc_show_config(config, state, strategy_version)
    resp["api_version"] = API_VERSION
    return resp


@router.get("/logs", response_model=Logs, tags=["Info"])
def logs(limit: int | None = None):
    return RPC._rpc_get_logs(limit)


@router.get("/plot_config", response_model=PlotConfig, tags=["Candle data"])
def plot_config(
    strategy: str | None = None,
    config=Depends(get_config),
    rpc: RPC | None = Depends(get_rpc_optional),
):
    if not strategy:
        if not rpc:
            raise RPCException("Strategy is mandatory in webserver mode.")
        return PlotConfig.model_validate(rpc._rpc_plot_config())
    else:
        config1 = deepcopy(config)
        config1.update({"strategy": strategy})
    try:
        return PlotConfig.model_validate(RPC._rpc_plot_config_with_strategy(config1))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/markets", response_model=MarketResponse, tags=["Candle data"])
def markets(
    query: Annotated[MarketRequest, Query()],
    config=Depends(get_config),
    rpc: RPC | None = Depends(get_rpc_optional),
):
    if not rpc or config["runmode"] == RunMode.WEBSERVER:
        # webserver mode
        config_loc = deepcopy(config)
        handleExchangePayload(query, config_loc)
        exchange = get_exchange(config_loc)
    else:
        exchange = rpc._freqtrade.exchange

    return {
        "markets": exchange.get_markets(
            base_currencies=[query.base] if query.base else None,
            quote_currencies=[query.quote] if query.quote else None,
        ),
        "exchange_id": exchange.id,
    }


@router.get("/strategy/{strategy}", response_model=StrategyResponse, tags=["Strategy"])
def get_strategy(
    strategy: str, config=Depends(get_config), rpc: RPC | None = Depends(get_rpc_optional)
):
    verify_strategy(strategy)

    if not rpc or config["runmode"] == RunMode.WEBSERVER:
        # webserver mode
        config_ = deepcopy(config)
        from freqtrade.resolvers.strategy_resolver import StrategyResolver

        try:
            strategy_obj = StrategyResolver._load_strategy(
                strategy, config_, extra_dir=config_.get("strategy_path")
            )
            strategy_obj.ft_load_hyper_params()
        except OperationalException:
            raise HTTPException(status_code=404, detail="Strategy not found")
        except Exception:
            logger.exception("Unexpected error while loading strategy '%s'.", strategy)
            raise HTTPException(
                status_code=502,
                detail="Unexpected error while loading strategy.",
            )
    else:
        # trade mode
        strategy_obj = rpc._freqtrade.strategy
        if strategy_obj.get_strategy_name() != strategy:
            raise HTTPException(
                status_code=404,
                detail="Only the currently active strategy is available in trade mode",
            )
    return {
        "strategy": strategy_obj.get_strategy_name(),
        "timeframe": getattr(strategy_obj, "timeframe", None),
        "code": strategy_obj.__source__,
        "params": [p for _, p in strategy_obj.enumerate_parameters()],
    }


@router.get("/sysinfo", response_model=SysInfo, tags=["Info"])
def sysinfo():
    return RPC._rpc_sysinfo()


@router.get("/health", response_model=Health, tags=["Info"])
def health(rpc: RPC = Depends(get_rpc)):
    return rpc.health()


@router.get("/cache_status", response_model=CacheStatus, tags=["Info"])
def cache_status():
    return _query_cache_daemons()


@router.get("/rate_metrics", response_model=RateMetricsResponse, tags=["Info"])
def rate_metrics(
    window: int = Query(3600, ge=60, le=86400),
    bucket_s: int = Query(10, ge=5, le=300),
    rpc: RPC = Depends(get_rpc),
):
    return rpc._rpc_rate_metrics(window=window, bucket_s=bucket_s)


def _query_cache_daemons() -> dict:
    from freqtrade.ohlcv_cache.healthcheck import _query_unix

    result: dict = {"ftcache": {}, "pairlist_cache": {}}

    # ftcache daemon
    try:
        from freqtrade.ohlcv_cache.defaults import default_socket_path

        sock = default_socket_path()
        stats = _query_unix(sock, {"op": "stats", "req_id": "api"})
        if stats.get("ok"):
            total = stats.get("requests_total", 0)
            hits = stats.get("cache_hits", 0)
            t_req = stats.get("tickers_requests", 0)
            t_hits = stats.get("tickers_cache_hits", 0)
            p_gets = stats.get("positions_gets", 0)
            p_hits = stats.get("positions_cache_hits", 0)
            result["ftcache"] = {
                "online": True,
                "uptime_s": stats.get("uptime_s", 0),
                "active_clients": stats.get("active_clients", 0),
                "requests_total": total,
                "cache_hits": hits,
                "cache_partial": stats.get("cache_partial", 0),
                "cache_misses": stats.get("cache_misses", 0),
                "hit_rate_pct": round(hits / total * 100, 1) if total > 0 else 0,
                "fetch_errors": stats.get("fetch_errors", 0),
                "series_count": stats.get("series_count", 0),
                "pending_fetches": stats.get("pending_fetches", 0),
                "peak_pending": stats.get("peak_pending", 0),
                "acquire_total": stats.get("acquire_total", 0),
                "tickers_requests": t_req,
                "tickers_cache_hits": t_hits,
                "tickers_hit_rate_pct": round(t_hits / t_req * 100, 1) if t_req > 0 else 0,
                "tickers_fetches": stats.get("tickers_fetches", 0),
                "positions_puts": stats.get("positions_puts", 0),
                "positions_gets": p_gets,
                "positions_cache_hits": p_hits,
                "positions_hit_rate_pct": round(p_hits / p_gets * 100, 1) if p_gets > 0 else 0,
            }
    except Exception:
        logger.debug("ftcache status query failed", exc_info=True)

    # pairlist cache daemon
    try:
        from freqtrade.pairlist_cache.defaults import (
            default_socket_path as pl_socket_path,
        )

        sock = pl_socket_path()
        ping = _query_unix(sock, {"op": "ping", "req_id": "api"})
        stats = _query_unix(sock, {"op": "stats", "req_id": "api"})
        if stats.get("ok"):
            gets = stats.get("gets", 0)
            hits_val = stats.get("hits", 0)
            result["pairlist_cache"] = {
                "online": True,
                "uptime_s": ping.get("uptime_s", 0),
                "active_clients": stats.get("clients", 0),
                "entries": stats.get("entries", 0),
                "gets": gets,
                "hits": hits_val,
                "hit_rate_pct": round(hits_val / gets * 100, 1) if gets > 0 else 0,
                "puts": stats.get("puts", 0),
            }
    except Exception:
        logger.debug("pairlist cache status query failed", exc_info=True)

    return result
