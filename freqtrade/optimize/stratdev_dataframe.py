import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from freqtrade.constants import Config
from freqtrade.data.btanalysis.bt_fileutils import load_backtest_stats
from freqtrade.enums import CandleType


logger = logging.getLogger(__name__)

_strategy_cache: dict[str, Any] = {}


def _load_strategy_safe(config: Config, strategy_name: str):
    cache_key = strategy_name
    if cache_key in _strategy_cache:
        return _strategy_cache[cache_key]

    try:
        from freqtrade.resolvers import StrategyResolver

        strat_config = dict(config)
        strat_config["strategy"] = strategy_name
        strategy = StrategyResolver.load_strategy(strat_config)
        _strategy_cache[cache_key] = strategy
        return strategy
    except Exception as e:
        logger.warning("Failed to load strategy %s: %s", strategy_name, e)
        return None


def compute_plot_dataframe(
    config: Config,
    bt_dir: Path,
    filename: str,
    strategy_name: str,
    pair: str,
    max_candles: int = 5000,
) -> dict[str, Any]:
    zip_path = bt_dir / f"{filename}.zip"
    json_path = bt_dir / f"{filename}.json"
    load_path = zip_path if zip_path.exists() else json_path
    if not load_path.exists():
        return {"error": "file_not_found"}

    try:
        stats = load_backtest_stats(load_path)
    except Exception as e:
        logger.warning("Failed to load backtest stats: %s", e)
        return {"error": "load_failed"}

    strat_data = stats.get("strategy", {}).get(strategy_name, {})
    if not strat_data:
        all_strats = list(stats.get("strategy", {}).keys())
        if all_strats:
            strat_data = stats["strategy"][all_strats[0]]
        else:
            return {"error": "strategy_not_found"}

    trades = [t for t in strat_data.get("trades", []) if isinstance(t, dict) and t.get("pair") == pair]
    timeframe = strat_data.get("timeframe", "5m")
    timerange_str = strat_data.get("timerange", "")

    datadir = Path(config.get("datadir", config.get("user_data_dir", "") + "/data"))
    if not datadir.exists():
        exchange_name = config.get("exchange", {}).get("name", "binance")
        datadir = Path(config.get("user_data_dir", ".")) / "data" / exchange_name

    candle_type = CandleType.FUTURES if config.get("trading_mode") == "futures" else CandleType.SPOT

    try:
        from freqtrade.configuration import TimeRange
        from freqtrade.data.history import load_pair_history

        timerange = TimeRange.parse_timerange(timerange_str) if timerange_str else None
        ohlcv = load_pair_history(
            pair=pair,
            timeframe=timeframe,
            datadir=datadir,
            timerange=timerange,
            candle_type=candle_type,
        )
    except Exception as e:
        logger.warning("Failed to load OHLCV for %s: %s", pair, e)
        return {"error": "ohlcv_not_found", "pair": pair, "detail": str(e)}

    if ohlcv.empty:
        return {"error": "ohlcv_not_found", "pair": pair, "detail": "Empty dataframe"}

    strategy = _load_strategy_safe(config, strategy_name)

    plot_config: dict[str, Any] = {}
    indicator_columns: list[str] = []

    if strategy:
        try:
            df_analyzed = strategy.analyze_ticker(ohlcv.copy(), {"pair": pair})
            plot_config = getattr(strategy, "plot_config", {}) or {}
        except Exception as e:
            logger.warning("Failed to run analyze_ticker for %s: %s", pair, e)
            df_analyzed = ohlcv
    else:
        df_analyzed = ohlcv

    signal_cols = ["enter_long", "exit_long", "enter_short", "exit_short"]
    base_cols = ["date", "open", "high", "low", "close", "volume"]
    all_indicator_cols = set()

    if plot_config:
        main_plot = plot_config.get("main_plot", {})
        if isinstance(main_plot, dict):
            all_indicator_cols.update(main_plot.keys())
        subplots = plot_config.get("subplots", {})
        if isinstance(subplots, dict):
            for sub_name, sub_conf in subplots.items():
                if isinstance(sub_conf, dict):
                    all_indicator_cols.update(sub_conf.keys())

    for col in list(all_indicator_cols):
        if col not in df_analyzed.columns:
            all_indicator_cols.discard(col)

    indicator_columns = sorted(all_indicator_cols)

    columns = base_cols + [c for c in signal_cols if c in df_analyzed.columns] + indicator_columns

    if len(df_analyzed) > max_candles:
        trade_dates = set()
        for t in trades:
            od = t.get("open_date", "")
            cd = t.get("close_date", "")
            if od:
                trade_dates.add(str(od)[:19])
            if cd:
                trade_dates.add(str(cd)[:19])

        step = len(df_analyzed) / max_candles
        keep_indices = set(int(i * step) for i in range(max_candles))

        if trade_dates:
            date_strs = df_analyzed["date"].astype(str).str[:19]
            for idx, d in enumerate(date_strs):
                if d in trade_dates:
                    keep_indices.add(idx)

        keep_indices = sorted(keep_indices)
        df_analyzed = df_analyzed.iloc[keep_indices].reset_index(drop=True)

    data_rows: list[list] = []
    for _, row in df_analyzed.iterrows():
        row_data = []
        for col in columns:
            val = row.get(col)
            if col == "date":
                row_data.append(str(val) if val is not None else None)
            elif isinstance(val, (np.integer,)):
                row_data.append(int(val))
            elif isinstance(val, (np.floating,)):
                row_data.append(None if np.isnan(val) else round(float(val), 6))
            elif isinstance(val, (int, float)):
                row_data.append(val)
            elif isinstance(val, (np.bool_,)):
                row_data.append(int(val))
            elif isinstance(val, bool):
                row_data.append(int(val))
            else:
                row_data.append(None)
        data_rows.append(row_data)

    return {
        "pair": pair,
        "timeframe": timeframe,
        "columns": columns,
        "data": data_rows,
        "data_length": len(data_rows),
        "trades": trades,
        "plot_config": plot_config,
    }


def get_backtest_pairs(
    bt_dir: Path, filename: str, strategy: str,
) -> dict[str, Any]:
    zip_path = bt_dir / f"{filename}.zip"
    json_path = bt_dir / f"{filename}.json"
    load_path = zip_path if zip_path.exists() else json_path
    if not load_path.exists():
        return {"error": "file_not_found", "pairs": []}

    try:
        stats = load_backtest_stats(load_path)
    except Exception:
        return {"error": "load_failed", "pairs": []}

    strat_data = stats.get("strategy", {}).get(strategy, {})
    if not strat_data:
        all_strats = list(stats.get("strategy", {}).keys())
        if all_strats:
            strat_data = stats["strategy"][all_strats[0]]

    if not strat_data:
        return {"error": "strategy_not_found", "pairs": []}

    trades = strat_data.get("trades", [])
    pairs = sorted({t["pair"] for t in trades if isinstance(t, dict) and "pair" in t})
    return {"pairs": pairs}
