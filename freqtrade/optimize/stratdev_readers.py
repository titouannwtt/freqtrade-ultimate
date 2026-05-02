import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import rapidjson

from freqtrade.data.btanalysis.bt_fileutils import (
    get_backtest_resultlist,
    load_backtest_stats,
    load_file_from_zip,
)
from freqtrade.optimize.hyperopt_tools import HyperoptTools


logger = logging.getLogger(__name__)


@lru_cache(maxsize=32)
def _get_sorted_epochs(fthypt_path: str, mtime: float) -> list[dict[str, Any]]:
    fthypt = Path(fthypt_path)
    all_epochs: list[dict[str, Any]] = []
    for batch in HyperoptTools._read_results(fthypt):
        all_epochs.extend(batch)
    all_epochs.sort(key=lambda e: e.get("loss", 1e6))
    return all_epochs

HYPER_PARAMS_FILE_FORMAT = rapidjson.NM_NATIVE | rapidjson.NM_NAN

_FTHYPT_NAME_RE = re.compile(r"^strategy_(.+?)_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.fthypt$")


def get_hyperopt_resultlist(dirname: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for fthypt in sorted(dirname.glob("*.fthypt"), reverse=True):
        meta_path = fthypt.with_suffix(".meta.json")
        if meta_path.exists():
            entry = _entry_from_meta(fthypt, meta_path)
        else:
            entry = _entry_from_filename(fthypt)
        if entry:
            results.append(entry)
    return results


def _entry_from_meta(fthypt: Path, meta_path: Path) -> dict[str, Any]:
    try:
        with meta_path.open() as f:
            meta = rapidjson.load(f)
    except Exception:
        return _entry_from_filename(fthypt)

    entry: dict[str, Any] = {
        "run_type": "hyperopt",
        "filename": fthypt.stem,
        "strategy": meta.get("strategy", ""),
        "timestamp": meta.get("run_start_ts", 0),
        "timeframe": meta.get("timeframe"),
        "timerange": meta.get("timerange"),
        "has_metadata": True,
        "hyperopt_loss": meta.get("hyperopt_loss"),
        "epochs_total": meta.get("epochs_total"),
        "epochs_completed": meta.get("epochs_completed"),
        "best_loss": meta.get("best_loss"),
        "total_profit_pct": meta.get("best_profit_pct"),
        "total_trades": meta.get("best_trades"),
        "best_sharpe": meta.get("best_sharpe"),
    }

    # Override with actual best epoch data from the .fthypt file (metadata can be stale)
    best = _read_best_epoch(fthypt)
    if best:
        rm = best.get("results_metrics", {})
        entry["total_trades"] = rm.get("total_trades", entry.get("total_trades"))
        entry["total_profit_pct"] = round(rm.get("profit_total", 0) * 100, 2)
        entry["best_loss"] = best.get("loss", entry.get("best_loss"))
        if rm.get("sharpe") is not None:
            entry["best_sharpe"] = rm["sharpe"]
        if rm.get("max_drawdown_account") is not None:
            entry["best_max_dd"] = round(rm["max_drawdown_account"] * 100, 2)
        if rm.get("profit_factor") is not None:
            entry["best_profit_factor"] = round(rm["profit_factor"], 4)
        winrate = rm.get("winrate") or rm.get("win_rate")
        if winrate is not None:
            entry["best_winrate"] = round(winrate * 100, 1)
        if rm.get("sqn") is not None:
            entry["best_sqn"] = round(rm["sqn"], 2)

    return entry


def _entry_from_filename(fthypt: Path) -> dict[str, Any]:
    m = _FTHYPT_NAME_RE.match(fthypt.name)
    strategy = m.group(1) if m else fthypt.stem
    entry: dict[str, Any] = {
        "run_type": "hyperopt",
        "filename": fthypt.stem,
        "strategy": strategy,
        "timestamp": int(fthypt.stat().st_mtime),
        "has_metadata": False,
    }
    # Quick scan: read only first line for a fast best_loss estimate
    try:
        with fthypt.open() as f:
            first_line = f.readline().strip()
            if first_line:
                ep = rapidjson.loads(first_line, number_mode=HYPER_PARAMS_FILE_FORMAT)
                rm = ep.get("results_metrics", {})
                entry["best_loss"] = ep.get("loss")
                entry["total_profit_pct"] = round(rm.get("profit_total", 0) * 100, 2)
                entry["total_trades"] = rm.get("total_trades", 0)
                entry["best_sharpe"] = rm.get("sharpe")
    except Exception:
        pass
    return entry


def get_hyperopt_run_detail(dirname: Path, filename: str) -> dict[str, Any]:
    fthypt = dirname / f"{filename}.fthypt"
    meta_path = fthypt.with_suffix(".meta.json")

    detail: dict[str, Any] = {"filename": filename}

    if meta_path.exists():
        with meta_path.open() as f:
            meta = rapidjson.load(f)
        detail.update(meta)
    else:
        m = _FTHYPT_NAME_RE.match(fthypt.name)
        detail["strategy"] = m.group(1) if m else filename
        detail["has_metadata"] = False

    if fthypt.exists():
        best = _read_best_epoch(fthypt)
        if best:
            detail["best_epoch_metrics"] = best.get("results_metrics", {})
            detail["best_params"] = best.get("params_details", {})
            detail["best_params_raw"] = best.get("params_dict", {})
            if "best_loss" not in detail:
                detail["best_loss"] = best.get("loss")
        detail["total_epochs"] = _count_epochs(fthypt)

    return detail


def _read_best_epoch(fthypt: Path) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_loss = float("inf")
    for batch in HyperoptTools._read_results(fthypt):
        for ep in batch:
            loss = ep.get("loss", 1e6)
            if loss < best_loss:
                best_loss = loss
                best = ep
    return best


def get_epoch_detail(dirname: Path, filename: str, rank: int) -> dict[str, Any]:
    fthypt = dirname / f"{filename}.fthypt"
    if not fthypt.exists():
        return {"error": "file_not_found"}

    mtime = fthypt.stat().st_mtime
    all_epochs = _get_sorted_epochs(str(fthypt), mtime)
    idx = rank - 1
    if idx < 0 or idx >= len(all_epochs):
        return {"error": "rank_out_of_range", "total": len(all_epochs)}

    ep = all_epochs[idx]
    rm = ep.get("results_metrics", {})
    return {
        "rank": rank,
        "loss": ep.get("loss"),
        "results_metrics": rm,
        "params_dict": ep.get("params_dict", {}),
        "params_details": ep.get("params_details", {}),
    }


def _compute_analytics_for_epoch(
    epoch: dict[str, Any], rank: int = 1,
) -> dict[str, Any]:
    rm = epoch.get("results_metrics", {})
    result: dict[str, Any] = {
        "epoch_info": {
            "rank": rank,
            "loss": epoch.get("loss"),
            "current_epoch": epoch.get("current_epoch"),
            "total_profit": rm.get("profit_total", 0),
            "total_profit_abs": rm.get("profit_total_abs", 0),
            "total_trades": rm.get("total_trades", 0),
            "max_drawdown": rm.get("max_drawdown", 0),
            "sharpe": rm.get("sharpe", 0),
            "sortino": rm.get("sortino", 0),
            "calmar": rm.get("calmar", 0),
            "profit_factor": rm.get("profit_factor", 0),
            "winrate": rm.get("winrate") or rm.get("win_rate", 0),
            "duration_avg": rm.get("duration_avg"),
            "trade_count_long": rm.get("trade_count_long", 0),
            "trade_count_short": rm.get("trade_count_short", 0),
        },
        "params_dict": epoch.get("params_dict", {}),
        "params_details": epoch.get("params_details", {}),
    }

    daily_profit = rm.get("daily_profit", [])
    starting_balance = rm.get("starting_balance") or rm.get("dry_run_wallet", 1000)
    if daily_profit:
        equity = []
        drawdown_series = []
        balance = starting_balance
        peak = balance
        for date_str, pnl in daily_profit:
            balance += pnl
            equity.append({"date": date_str, "balance": round(balance, 2)})
            if balance > peak:
                peak = balance
            dd_pct = ((peak - balance) / peak * 100) if peak > 0 else 0
            drawdown_series.append({"date": date_str, "dd_pct": round(dd_pct, 2)})
        result["equity_curve"] = equity
        result["drawdown_series"] = drawdown_series
        result["starting_balance"] = starting_balance
        result["top_drawdowns"] = _compute_top_drawdowns(equity, drawdown_series)

    periodic = rm.get("periodic_breakdown", {})
    monthly_data = periodic.get("month", [])
    if monthly_data:
        result["monthly_returns"] = _build_monthly_heatmap(monthly_data)
    elif daily_profit:
        result["monthly_returns"] = _build_monthly_heatmap_from_daily(
            daily_profit, starting_balance
        )

    if daily_profit and len(daily_profit) >= 30:
        result["rolling_metrics"] = _compute_rolling_metrics(daily_profit, starting_balance)

    if daily_profit:
        result["risk_metrics"] = _compute_risk_metrics(daily_profit, starting_balance)
        result["drawdown_calendar"] = _compute_drawdown_calendar(daily_profit, starting_balance)

    trades = rm.get("trades", [])
    if not trades:
        trades = epoch.get("trades", [])
    if trades:
        result["trade_pnl_distribution"] = _compute_trade_pnl_distribution(trades)
        result["trade_durations"] = _compute_trade_durations(trades)
        result["return_distribution_fit"] = _compute_return_distribution_fit(trades)
        result["mae_mfe"] = _compute_mae_mfe(trades)

    result["streaks"] = {
        "max_consecutive_wins": rm.get("max_consecutive_wins", 0),
        "max_consecutive_losses": rm.get("max_consecutive_losses", 0),
        "wins": rm.get("wins", 0),
        "losses": rm.get("losses", 0),
        "draws": rm.get("draws", 0),
    }
    if trades:
        result["streaks"]["distribution"] = _compute_streak_distribution(trades)

    yearly_data = periodic.get("year", [])
    if yearly_data:
        result["yearly_returns"] = [
            {
                "year": str(y.get("date", "")),
                "profit_abs": y.get("profit_abs", 0),
                "trades": y.get("trades", 0),
                "profit_factor": y.get("profit_factor", 0),
            }
            for y in yearly_data
        ]

    exit_reasons = rm.get("exit_reason_summary", [])
    if exit_reasons:
        result["exit_reasons"] = exit_reasons

    results_per_pair = rm.get("results_per_pair", [])
    if results_per_pair:
        result["results_per_pair"] = results_per_pair

    if trades:
        result["duration_scatter"] = _compute_duration_scatter(trades)
        result["exit_reason_detail"] = _compute_exit_reason_detail(trades)
        result["weekday_pattern"] = _compute_weekday_pattern(trades)
        result["cumulative_trades"] = _compute_cumulative_trades(trades, starting_balance)

    if trades and len(trades) >= 20:
        result["rolling_winrate"] = _compute_rolling_winrate(trades, window=50)
        result["rolling_profit_factor"] = _compute_rolling_profit_factor(trades, window=50)

    if trades:
        result["long_short_split"] = _compute_long_short_split(trades)
        result["exposure_timeline"] = _compute_exposure_timeline(trades)
        result["trade_expectancy"] = _compute_trade_expectancy(trades)

    if trades:
        max_open = rm.get("max_open_trades", 0)
        if not max_open:
            max_open = rm.get("max_open_trades_setting", 0) or 5
        result["capital_utilization"] = _compute_capital_utilization(
            trades, starting_balance, int(max_open),
        )

    pair_data = rm.get("results_per_pair", [])
    if pair_data:
        result["pair_profit"] = [
            {
                "pair": p.get("key", ""),
                "profit_abs": p.get("profit_total_abs", p.get("profit_abs", 0)),
                "trade_count": p.get("trades", p.get("trade_count", 0)),
            }
            for p in pair_data
            if isinstance(p, dict) and p.get("key") != "TOTAL"
        ]

    return result


def compute_advanced_analytics(dirname: Path, filename: str) -> dict[str, Any]:
    fthypt = dirname / f"{filename}.fthypt"
    if not fthypt.exists():
        return {"error": "file_not_found"}

    best = _read_best_epoch(fthypt)
    if not best:
        return {"error": "no_epochs"}

    return _compute_analytics_for_epoch(best, rank=1)


def compute_epoch_advanced_analytics(
    dirname: Path, filename: str, rank: int,
) -> dict[str, Any]:
    fthypt = dirname / f"{filename}.fthypt"
    if not fthypt.exists():
        return {"error": "file_not_found"}

    mtime = fthypt.stat().st_mtime
    all_epochs = _get_sorted_epochs(str(fthypt), mtime)

    idx = rank - 1
    if idx < 0 or idx >= len(all_epochs):
        return {"error": "rank_out_of_range", "total": len(all_epochs)}

    return _compute_analytics_for_epoch(all_epochs[idx], rank=rank)


def _compute_top_drawdowns(
    equity: list[dict], dd_series: list[dict]
) -> list[dict[str, Any]]:
    import math

    drawdowns: list[dict[str, Any]] = []
    in_dd = False
    dd_start = ""
    dd_valley = ""
    dd_peak_val = 0.0
    dd_max_depth = 0.0

    for i, pt in enumerate(dd_series):
        dd_pct = pt["dd_pct"]
        bal = equity[i]["balance"]
        date = pt["date"]

        if dd_pct > 0 and not in_dd:
            in_dd = True
            dd_start = date
            dd_peak_val = bal + (bal * dd_pct / (100 - dd_pct)) if dd_pct < 100 else bal
            dd_max_depth = dd_pct
            dd_valley = date
        elif dd_pct > 0 and in_dd:
            if dd_pct > dd_max_depth:
                dd_max_depth = dd_pct
                dd_valley = date
        elif dd_pct == 0 and in_dd:
            in_dd = False
            drawdowns.append({
                "start": dd_start,
                "valley": dd_valley,
                "end": date,
                "depth_pct": round(dd_max_depth, 2),
            })

    if in_dd and dd_start:
        drawdowns.append({
            "start": dd_start,
            "valley": dd_valley,
            "end": dd_series[-1]["date"],
            "depth_pct": round(dd_max_depth, 2),
            "active": True,
        })

    drawdowns.sort(key=lambda d: d["depth_pct"], reverse=True)

    for dd in drawdowns[:5]:
        try:
            from datetime import datetime
            fmt = "%Y-%m-%d"
            s = datetime.strptime(dd["start"], fmt)
            v = datetime.strptime(dd["valley"], fmt)
            e = datetime.strptime(dd["end"], fmt)
            dd["decline_days"] = (v - s).days
            dd["recovery_days"] = (e - v).days
            dd["total_days"] = (e - s).days
        except Exception:
            pass

    return drawdowns[:5]


def _build_monthly_heatmap(monthly_data: list[dict]) -> list[dict]:
    results = []
    for m in monthly_data:
        date_str = str(m.get("date", ""))
        parts = date_str.split("-")
        if len(parts) >= 2:
            results.append({
                "year": int(parts[0]),
                "month": int(parts[1]),
                "profit_abs": m.get("profit_abs", 0),
                "trades": m.get("trades", 0),
            })
    return results


def _build_monthly_heatmap_from_daily(
    daily_profit: list, starting_balance: float,
) -> list[dict]:
    from collections import defaultdict
    monthly: dict[tuple[int, int], dict] = defaultdict(
        lambda: {"profit_abs": 0.0, "trades": 0}
    )
    for date_str, pnl in daily_profit:
        parts = date_str.split("-")
        if len(parts) >= 2:
            key = (int(parts[0]), int(parts[1]))
            monthly[key]["profit_abs"] += pnl
    return [
        {"year": k[0], "month": k[1], "profit_abs": round(v["profit_abs"], 2), "trades": 0}
        for k, v in sorted(monthly.items())
    ]


def _compute_rolling_metrics(
    daily_profit: list, starting_balance: float, window: int = 30,
) -> dict[str, list]:
    import math

    daily_returns: list[float] = []
    balance = starting_balance
    dates: list[str] = []
    for date_str, pnl in daily_profit:
        ret = pnl / balance if balance > 0 else 0
        daily_returns.append(ret)
        balance += pnl
        dates.append(date_str)

    n = len(daily_returns)
    rolling_sharpe = []
    rolling_sortino = []
    rolling_volatility = []

    for i in range(window, n):
        w = daily_returns[i - window: i]
        mean_r = sum(w) / len(w)
        var_r = sum((r - mean_r) ** 2 for r in w) / len(w)
        std_r = math.sqrt(var_r) if var_r > 0 else 1e-10

        ann_factor = math.sqrt(365)
        sharpe = (mean_r / std_r) * ann_factor if std_r > 1e-10 else 0

        downside = [r for r in w if r < 0]
        ds_var = sum(r ** 2 for r in downside) / len(w) if downside else 0
        ds_std = math.sqrt(ds_var) if ds_var > 0 else 1e-10
        sortino = (mean_r / ds_std) * ann_factor if ds_std > 1e-10 else 0

        vol = std_r * ann_factor

        rolling_sharpe.append({"date": dates[i], "value": round(sharpe, 3)})
        rolling_sortino.append({"date": dates[i], "value": round(sortino, 3)})
        rolling_volatility.append({"date": dates[i], "value": round(vol * 100, 2)})

    # Subsample to max 500 points
    for key in ["sharpe", "sortino", "volatility"]:
        data = locals()[f"rolling_{key}"]
        if len(data) > 500:
            step = len(data) / 500
            data[:] = [data[int(i * step)] for i in range(500)]

    return {
        "sharpe": rolling_sharpe,
        "sortino": rolling_sortino,
        "volatility": rolling_volatility,
        "window": window,
    }


def _compute_risk_metrics(
    daily_profit: list, starting_balance: float,
) -> dict[str, Any]:
    import math

    daily_returns: list[float] = []
    balance = starting_balance
    for _, pnl in daily_profit:
        ret = pnl / balance if balance > 0 else 0
        daily_returns.append(ret)
        balance += pnl

    n = len(daily_returns)
    if n < 2:
        return {}

    sorted_returns = sorted(daily_returns)

    # VaR 95% (historical)
    var_idx = int(n * 0.05)
    var_95 = sorted_returns[var_idx] if var_idx < n else 0

    # CVaR 95% (Expected Shortfall)
    tail = sorted_returns[:var_idx + 1] if var_idx > 0 else sorted_returns[:1]
    cvar_95 = sum(tail) / len(tail) if tail else 0

    # Omega ratio (threshold = 0)
    gains = sum(r for r in daily_returns if r > 0)
    losses = abs(sum(r for r in daily_returns if r < 0))
    omega = (gains / losses) if losses > 0 else float("inf")

    # Tail ratio (95th percentile / 5th percentile absolute)
    p95 = sorted_returns[int(n * 0.95)] if n > 20 else 0
    p5 = sorted_returns[int(n * 0.05)] if n > 20 else 0
    tail_ratio = abs(p95 / p5) if p5 != 0 else float("inf")

    # Ulcer Index
    balance = starting_balance
    peak = balance
    sum_sq_dd = 0.0
    for _, pnl in daily_profit:
        balance += pnl
        if balance > peak:
            peak = balance
        dd_pct = ((peak - balance) / peak) if peak > 0 else 0
        sum_sq_dd += dd_pct ** 2
    ulcer_index = math.sqrt(sum_sq_dd / n) * 100 if n > 0 else 0

    # Recovery factor
    total_profit = sum(pnl for _, pnl in daily_profit)
    max_dd_abs = 0
    balance = starting_balance
    peak = balance
    for _, pnl in daily_profit:
        balance += pnl
        if balance > peak:
            peak = balance
        dd = peak - balance
        if dd > max_dd_abs:
            max_dd_abs = dd
    recovery_factor = total_profit / max_dd_abs if max_dd_abs > 0 else float("inf")

    # Gain-to-Pain ratio
    total_loss = sum(abs(pnl) for _, pnl in daily_profit if pnl < 0)
    gain_pain = total_profit / total_loss if total_loss > 0 else float("inf")

    # Kelly criterion (from trade data is better but approximate from daily)
    win_days = [r for r in daily_returns if r > 0]
    loss_days = [r for r in daily_returns if r < 0]
    if win_days and loss_days:
        win_rate = len(win_days) / n
        avg_win = sum(win_days) / len(win_days)
        avg_loss = abs(sum(loss_days) / len(loss_days))
        payoff = avg_win / avg_loss if avg_loss > 0 else 0
        kelly = win_rate - ((1 - win_rate) / payoff) if payoff > 0 else 0
    else:
        kelly = 0

    def _cap(v, lo=-1e6, hi=1e6):
        if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
            return hi if v > 0 else lo
        return round(v, 4)

    return {
        "var_95": _cap(var_95 * 100),
        "cvar_95": _cap(cvar_95 * 100),
        "omega": _cap(omega),
        "tail_ratio": _cap(tail_ratio),
        "ulcer_index": _cap(ulcer_index),
        "recovery_factor": _cap(recovery_factor),
        "gain_pain_ratio": _cap(gain_pain),
        "kelly_criterion": _cap(kelly * 100),
    }


def _compute_trade_pnl_distribution(trades: list) -> dict[str, Any]:
    profits = []
    for t in trades:
        if isinstance(t, dict):
            pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
            if isinstance(pnl, (int, float)):
                profits.append(float(pnl))

    if not profits:
        return {"bins": [], "counts": []}

    import math

    mn, mx = min(profits), max(profits)
    n_bins = 20
    if mx == mn:
        return {"bins": [mn], "counts": [len(profits)]}

    bin_width = (mx - mn) / n_bins
    bins = [round(mn + i * bin_width, 4) for i in range(n_bins + 1)]
    counts = [0] * n_bins

    for p in profits:
        idx = int((p - mn) / bin_width)
        if idx >= n_bins:
            idx = n_bins - 1
        counts[idx] += 1

    win_profits = [p for p in profits if p > 0]
    loss_profits = [p for p in profits if p < 0]

    return {
        "bins": bins,
        "counts": counts,
        "total": len(profits),
        "mean": round(sum(profits) / len(profits), 4),
        "median": round(sorted(profits)[len(profits) // 2], 4),
        "std": round(
            math.sqrt(sum((p - sum(profits) / len(profits)) ** 2 for p in profits) / len(profits)),
            4,
        ),
        "avg_win": round(sum(win_profits) / len(win_profits), 4) if win_profits else 0,
        "avg_loss": round(sum(loss_profits) / len(loss_profits), 4) if loss_profits else 0,
        "best_trade": round(max(profits), 4),
        "worst_trade": round(min(profits), 4),
    }


def _compute_trade_durations(trades: list) -> dict[str, Any]:
    win_durations = []
    loss_durations = []

    for t in trades:
        if not isinstance(t, dict):
            continue
        dur = t.get("trade_duration")
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        if dur is None or not isinstance(pnl, (int, float)):
            continue
        if pnl > 0:
            win_durations.append(dur)
        elif pnl < 0:
            loss_durations.append(dur)

    def _stats(arr):
        if not arr:
            return {"avg": 0, "min": 0, "max": 0, "median": 0, "count": 0}
        s = sorted(arr)
        return {
            "avg": round(sum(s) / len(s), 1),
            "min": s[0],
            "max": s[-1],
            "median": s[len(s) // 2],
            "count": len(s),
        }

    return {
        "winners": _stats(win_durations),
        "losers": _stats(loss_durations),
        "all": _stats(win_durations + loss_durations),
    }


def _compute_streak_distribution(trades: list) -> dict[str, list[int]]:
    win_streaks = []
    loss_streaks = []
    current_streak = 0
    current_type = None

    for t in trades:
        if not isinstance(t, dict):
            continue
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        if not isinstance(pnl, (int, float)):
            continue
        trade_type = "win" if pnl > 0 else "loss"

        if trade_type == current_type:
            current_streak += 1
        else:
            if current_type == "win" and current_streak > 0:
                win_streaks.append(current_streak)
            elif current_type == "loss" and current_streak > 0:
                loss_streaks.append(current_streak)
            current_streak = 1
            current_type = trade_type

    if current_type == "win" and current_streak > 0:
        win_streaks.append(current_streak)
    elif current_type == "loss" and current_streak > 0:
        loss_streaks.append(current_streak)

    return {"win_streaks": win_streaks, "loss_streaks": loss_streaks}


def _compute_duration_scatter(trades: list) -> list[dict]:
    points = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        dur = t.get("trade_duration")
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        pair = t.get("pair", "")
        if dur is None or not isinstance(pnl, (int, float)):
            continue
        points.append({
            "duration": round(float(dur), 1),
            "profit": round(float(pnl), 4),
            "pair": pair,
        })
    if len(points) > 1000:
        step = len(points) / 1000
        points = [points[int(i * step)] for i in range(1000)]
    return points


def _compute_exit_reason_detail(trades: list) -> list[dict]:
    from collections import defaultdict
    reasons: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "profit_sum": 0.0, "wins": 0, "losses": 0}
    )
    for t in trades:
        if not isinstance(t, dict):
            continue
        reason = t.get("exit_reason", "unknown")
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        if not isinstance(pnl, (int, float)):
            continue
        r = reasons[reason]
        r["count"] += 1
        r["profit_sum"] += pnl
        if pnl > 0:
            r["wins"] += 1
        elif pnl < 0:
            r["losses"] += 1
    result = []
    for reason, data in sorted(reasons.items(), key=lambda x: x[1]["count"], reverse=True):
        result.append({
            "reason": reason,
            "count": data["count"],
            "avg_profit": round(data["profit_sum"] / data["count"], 4) if data["count"] else 0,
            "total_profit": round(data["profit_sum"], 4),
            "wins": data["wins"],
            "losses": data["losses"],
            "winrate": round(data["wins"] / data["count"], 4) if data["count"] else 0,
        })
    return result


def _compute_weekday_pattern(trades: list) -> dict:
    from collections import defaultdict
    from datetime import datetime

    by_day: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        if not isinstance(t, dict):
            continue
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        if not isinstance(pnl, (int, float)):
            continue
        close_date = t.get("close_date") or t.get("close_timestamp")
        if not close_date:
            continue
        try:
            if isinstance(close_date, str):
                dt = datetime.fromisoformat(close_date.replace("Z", "+00:00"))
            elif isinstance(close_date, (int, float)):
                dt = datetime.fromtimestamp(close_date / 1000)
            else:
                continue
            by_day[dt.weekday()].append(float(pnl))
        except Exception:
            continue

    result = []
    for i in range(7):
        profits = by_day.get(i, [])
        count = len(profits)
        avg = round(sum(profits) / count, 4) if count else 0
        total = round(sum(profits), 4)
        wins = sum(1 for p in profits if p > 0)
        result.append({
            "day_index": i,
            "trades": count,
            "avg_profit": avg,
            "total_profit": total,
            "winrate": round(wins / count, 4) if count else 0,
        })
    return {"days": result}


def _compute_cumulative_trades(trades: list, starting_balance: float) -> list[dict]:
    points = []
    cumulative = 0.0
    balance = starting_balance
    for i, t in enumerate(trades):
        if not isinstance(t, dict):
            continue
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        pnl_abs = t.get("profit_abs", 0)
        if not isinstance(pnl, (int, float)):
            continue
        cumulative += float(pnl)
        balance += float(pnl_abs) if isinstance(pnl_abs, (int, float)) else 0
        close_date = t.get("close_date", "")
        if isinstance(close_date, str) and len(close_date) >= 10:
            date_label = close_date[:10]
        else:
            date_label = str(i)
        points.append({
            "index": i,
            "date": date_label,
            "cumulative_pct": round(cumulative, 4),
            "balance": round(balance, 2),
            "profit": round(float(pnl), 4),
        })
    if len(points) > 2000:
        step = len(points) / 2000
        points = [points[int(i * step)] for i in range(2000)]
    return points


def _compute_rolling_winrate(trades: list, window: int = 50) -> list[dict]:
    trade_results = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        pnl = t.get("profit_pct")
        if pnl is None:
            pnl = t.get("profit_ratio", 0)
        close_date = t.get("close_date", "")
        if not isinstance(pnl, (int, float)):
            continue
        trade_results.append({
            "win": 1 if pnl > 0 else 0,
            "date": close_date[:10] if isinstance(close_date, str) and len(close_date) >= 10 else "",
        })

    if len(trade_results) < window:
        window = max(10, len(trade_results) // 2)

    points = []
    for i in range(window, len(trade_results)):
        w = trade_results[i - window: i]
        wr = sum(x["win"] for x in w) / len(w)
        points.append({
            "index": i,
            "date": trade_results[i]["date"],
            "winrate": round(wr, 4),
        })

    if len(points) > 500:
        step = len(points) / 500
        points = [points[int(i * step)] for i in range(500)]
    return points


def _compute_rolling_profit_factor(trades: list, window: int = 50) -> list[dict]:
    trade_pnls = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        close_date = t.get("close_date", "")
        if not isinstance(pnl, (int, float)):
            continue
        trade_pnls.append({
            "pnl": float(pnl),
            "date": close_date[:10] if isinstance(close_date, str) and len(close_date) >= 10 else "",
        })

    if len(trade_pnls) < window:
        window = max(10, len(trade_pnls) // 2)

    points = []
    for i in range(window, len(trade_pnls)):
        w = trade_pnls[i - window: i]
        wins = sum(x["pnl"] for x in w if x["pnl"] > 0)
        losses = abs(sum(x["pnl"] for x in w if x["pnl"] < 0))
        pf = wins / losses if losses > 0 else 10.0
        pf = min(pf, 10.0)
        points.append({
            "index": i,
            "date": trade_pnls[i]["date"],
            "profit_factor": round(pf, 3),
        })

    if len(points) > 500:
        step = len(points) / 500
        points = [points[int(i * step)] for i in range(500)]
    return points


def _compute_long_short_split(trades: list) -> dict:
    long_trades = {"count": 0, "profit_sum": 0.0, "wins": 0, "losses": 0, "durations": []}
    short_trades = {"count": 0, "profit_sum": 0.0, "wins": 0, "losses": 0, "durations": []}

    for t in trades:
        if not isinstance(t, dict):
            continue
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        dur = t.get("trade_duration", 0)
        is_short = t.get("is_short", False)
        if not isinstance(pnl, (int, float)):
            continue
        bucket = short_trades if is_short else long_trades
        bucket["count"] += 1
        bucket["profit_sum"] += float(pnl)
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
        if isinstance(dur, (int, float)):
            bucket["durations"].append(float(dur))

    def _summarize(b):
        durs = b["durations"]
        return {
            "count": b["count"],
            "total_profit": round(b["profit_sum"], 4),
            "avg_profit": round(b["profit_sum"] / b["count"], 4) if b["count"] else 0,
            "wins": b["wins"],
            "losses": b["losses"],
            "winrate": round(b["wins"] / b["count"], 4) if b["count"] else 0,
            "avg_duration": round(sum(durs) / len(durs), 1) if durs else 0,
        }

    return {
        "long": _summarize(long_trades),
        "short": _summarize(short_trades),
    }


def _compute_exposure_timeline(trades: list) -> list[dict]:
    from datetime import datetime

    events = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        open_date = t.get("open_date") or t.get("open_timestamp")
        close_date = t.get("close_date") or t.get("close_timestamp")
        if not open_date or not close_date:
            continue
        try:
            if isinstance(open_date, str):
                od = datetime.fromisoformat(open_date.replace("Z", "+00:00"))
            elif isinstance(open_date, (int, float)):
                od = datetime.fromtimestamp(open_date / 1000)
            else:
                continue
            if isinstance(close_date, str):
                cd = datetime.fromisoformat(close_date.replace("Z", "+00:00"))
            elif isinstance(close_date, (int, float)):
                cd = datetime.fromtimestamp(close_date / 1000)
            else:
                continue
            events.append((od, 1))
            events.append((cd, -1))
        except Exception:
            continue

    if not events:
        return []

    events.sort(key=lambda x: x[0])
    timeline = []
    current = 0
    for dt, delta in events:
        current += delta
        timeline.append({
            "date": dt.strftime("%Y-%m-%d"),
            "open_positions": current,
        })

    if len(timeline) > 1000:
        step = len(timeline) / 1000
        timeline = [timeline[int(i * step)] for i in range(1000)]
    return timeline


def _compute_trade_expectancy(trades: list) -> dict:
    import math

    profits = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        if isinstance(pnl, (int, float)):
            profits.append(float(pnl))

    if not profits:
        return {}

    n = len(profits)
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]

    win_rate = len(wins) / n if n else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    payoff = avg_win / avg_loss if avg_loss > 0 else 0
    kelly = (win_rate - ((1 - win_rate) / payoff)) if payoff > 0 else 0

    mean_pnl = sum(profits) / n
    std_pnl = math.sqrt(sum((p - mean_pnl) ** 2 for p in profits) / n) if n > 1 else 0
    sqn = (mean_pnl / std_pnl) * math.sqrt(n) if std_pnl > 0 else 0

    ci_95 = 1.96 * (std_pnl / math.sqrt(n)) if n > 1 else 0

    return {
        "expectancy": round(expectancy, 4),
        "expectancy_per_trade_pct": round(expectancy, 4),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "payoff_ratio": round(payoff, 4),
        "kelly_pct": round(kelly * 100, 2),
        "half_kelly_pct": round(kelly * 50, 2),
        "sqn": round(sqn, 2),
        "ci_95_low": round(expectancy - ci_95, 4),
        "ci_95_high": round(expectancy + ci_95, 4),
        "total_trades": n,
    }


def _compute_return_distribution_fit(trades: list) -> dict[str, Any]:
    import math

    profits = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        if isinstance(pnl, (int, float)):
            profits.append(float(pnl))

    if len(profits) < 10:
        return {}

    n = len(profits)
    mean = sum(profits) / n
    variance = sum((p - mean) ** 2 for p in profits) / n
    std = math.sqrt(variance) if variance > 0 else 1e-10
    skew, kurt = _skew_kurtosis(profits)

    mn, mx = min(profits), max(profits)
    n_bins = min(30, max(10, int(math.sqrt(n))))
    bin_width = (mx - mn) / n_bins if mx > mn else 1.0
    bins = []
    for i in range(n_bins):
        edge_lo = mn + i * bin_width
        edge_hi = mn + (i + 1) * bin_width
        mid = (edge_lo + edge_hi) / 2
        if i == n_bins - 1:
            count = sum(1 for v in profits if edge_lo <= v <= edge_hi)
        else:
            count = sum(1 for v in profits if edge_lo <= v < edge_hi)
        normal_density = (
            math.exp(-0.5 * ((mid - mean) / std) ** 2) / (std * math.sqrt(2 * math.pi))
        )
        expected = normal_density * bin_width * n
        bins.append({
            "lo": round(edge_lo, 4),
            "hi": round(edge_hi, 4),
            "mid": round(mid, 4),
            "count": count,
            "normal_expected": round(expected, 2),
        })

    return {
        "bins": bins,
        "mean": round(mean, 4),
        "std": round(std, 4),
        "skewness": skew,
        "kurtosis": kurt,
        "total": n,
        "is_normal": abs(skew) < 0.5 and abs(kurt) < 1.0,
    }


def _compute_drawdown_calendar(
    daily_profit: list, starting_balance: float,
) -> list[dict]:
    balance = starting_balance
    peak = balance
    result = []
    for date_str, pnl in daily_profit:
        balance += pnl
        if balance > peak:
            peak = balance
        dd_pct = ((peak - balance) / peak * 100) if peak > 0 else 0
        parts = date_str.split("-")
        if len(parts) >= 3:
            result.append({
                "date": date_str,
                "year": int(parts[0]),
                "month": int(parts[1]),
                "day": int(parts[2]),
                "dd_pct": round(dd_pct, 2),
                "pnl": round(pnl, 2),
            })
    return result


def _compute_mae_mfe(trades: list) -> list[dict]:
    points = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        if not isinstance(pnl, (int, float)):
            continue
        open_rate = t.get("open_rate", 0)
        min_rate = t.get("min_rate")
        max_rate = t.get("max_rate")
        is_short = t.get("is_short", False)
        if not open_rate or open_rate == 0:
            continue
        if min_rate is not None and max_rate is not None:
            if is_short:
                mae = (max_rate - open_rate) / open_rate * -100
                mfe = (open_rate - min_rate) / open_rate * 100
            else:
                mae = (min_rate - open_rate) / open_rate * 100
                mfe = (max_rate - open_rate) / open_rate * 100
            points.append({
                "mae": round(float(mae), 4),
                "mfe": round(float(mfe), 4),
                "profit": round(float(pnl), 4),
                "pair": t.get("pair", ""),
            })
    if len(points) > 1000:
        step = len(points) / 1000
        points = [points[int(i * step)] for i in range(1000)]
    return points


def _count_epochs(fthypt: Path) -> int:
    count = 0
    with fthypt.open() as f:
        for _ in f:
            count += 1
    return count


def compute_hyperopt_analysis(dirname: Path, filename: str) -> dict[str, Any]:
    fthypt = dirname / f"{filename}.fthypt"
    if not fthypt.exists():
        return {"error": "file_not_found"}

    cache_path = dirname / f"{filename}.analysis.json"
    if cache_path.exists() and cache_path.stat().st_mtime >= fthypt.stat().st_mtime:
        with cache_path.open() as f:
            return rapidjson.load(f, number_mode=HYPER_PARAMS_FILE_FORMAT)

    all_epochs: list[dict[str, Any]] = []
    for batch in HyperoptTools._read_results(fthypt):
        all_epochs.extend(batch)

    all_epochs.sort(key=lambda e: e.get("loss", 1e6))
    top_10 = all_epochs[:10]
    best = top_10[0] if top_10 else {}
    rm = best.get("results_metrics", {})
    best_trades = rm.get("trades", [])

    all_losses_raw = [e.get("loss", 1e6) for e in all_epochs]
    all_losses = [l for l in all_losses_raw if l < 1e6]
    all_dd = [
        e.get("results_metrics", {}).get("max_drawdown_account", 0) * 100
        for e in all_epochs
    ]

    param_values: dict[str, list] = {}
    for ep in top_10:
        pd = ep.get("params_dict", {})
        for k, v in pd.items():
            param_values.setdefault(k, []).append(v)

    all_param_values: dict[str, list] = {}
    for ep in all_epochs:
        pd = ep.get("params_dict", {})
        for k, v in pd.items():
            all_param_values.setdefault(k, []).append(v)

    param_stability = _compute_param_stability(param_values)
    trade_metrics = _compute_trade_metrics(best_trades)
    param_analytics = _compute_param_analytics(param_values, top_10, rm)
    param_stats = _compute_param_stats(param_values)
    param_deep_dive = _compute_param_deep_dive_inferred(
        best.get("params_dict", {}), param_values, all_param_values, all_losses,
    )

    n_params = len(param_values)
    total_epochs = len(all_epochs)

    import math
    exp_max_sr = math.sqrt(2 * math.log(max(total_epochs, 2)))
    observed_sharpe = rm.get("sharpe", 0.0)
    dsr_analysis = {
        "observed_sharpe": round(observed_sharpe, 4),
        "expected_max_sharpe": round(exp_max_sr, 4),
        "n_trials": total_epochs,
        "genuine": observed_sharpe > exp_max_sr,
    }

    overfit_warnings = _compute_overfit_warnings(
        dsr=dsr_analysis,
        param_deep=param_deep_dive,
        n_params=n_params,
        n_trades=rm.get("total_trades", 0),
        sans_top=trade_metrics.get("sans_top_trade"),
        bvm_gap=param_analytics.get("best_vs_median_gap"),
        dist_analysis=trade_metrics.get("distribution_analysis"),
    )

    loss_histogram = _build_loss_histogram_full(all_losses)

    result: dict[str, Any] = {
        "top_epochs": [
            {
                "rank": i + 1,
                "loss": round(e.get("loss", 0), 6),
                "profit_pct": round(
                    e.get("results_metrics", {}).get("profit_total", 0) * 100, 2
                ),
                "trades": e.get("results_metrics", {}).get("total_trades", 0),
                "sharpe": round(e.get("results_metrics", {}).get("sharpe", 0), 4),
                "dd_pct": round(
                    e.get("results_metrics", {}).get("max_drawdown_account", 0) * 100, 2,
                ),
                "winrate": round(
                    e.get("results_metrics", {}).get("winrate", 0) * 100, 1
                ),
                "params": e.get("params_dict", {}),
                "results_metrics": e.get("results_metrics", {}),
            }
            for i, e in enumerate(top_10)
        ],
        "total_epochs": total_epochs,
        "convergence": _build_convergence(all_losses, 500),
        "epoch_dd_data": _subsample(all_dd, 500),
        "return_vs_dd": _compute_return_vs_dd(all_epochs),
        "loss_histogram": loss_histogram,
        "param_stability": param_stability,
        "param_stats": param_stats,
        "param_deep_dive": param_deep_dive,
        "dsr_analysis": dsr_analysis,
        "overfit_warnings": overfit_warnings,
        "distribution_analysis": trade_metrics.get("distribution_analysis"),
        "sans_top_trade": trade_metrics.get("sans_top_trade"),
        "pair_profit_distribution": trade_metrics.get("pair_profit_distribution", []),
        "best_vs_median_gap": param_analytics.get("best_vs_median_gap"),
        "param_correlation": param_analytics.get("param_correlation", []),
        "parallel_coords": param_analytics.get(
            "parallel_coords", {"params": [], "lines": []}
        ),
        "dispersion_bands": param_analytics.get("dispersion_bands", {}),
        "benchmark_comparison": {
            "sharpe": {
                "value": round(rm.get("sharpe", 0), 4),
                "benchmark": 0.85,
                "above": rm.get("sharpe", 0) > 0.85,
            },
            "dd": {
                "value": round(rm.get("max_drawdown_account", 0) * 100, 2),
                "benchmark": 25.0,
                "above": rm.get("max_drawdown_account", 0) > 0.25,
            },
        },
        "monte_carlo": _compute_monte_carlo(best_trades),
        "sensitivity_grid": _compute_sensitivity_grid(all_epochs, all_param_values),
        "regime_analysis": _compute_regime_analysis(best_trades),
        "dof_analysis": _compute_dof_analysis(rm.get("total_trades", 0), n_params),
    }

    try:
        from freqtrade.misc import file_dump_json

        file_dump_json(cache_path, result)
    except Exception:
        logger.debug("Failed to cache analysis for %s", filename)

    return result


# ---------------------------------------------------------------------------
#  Helper functions for compute_hyperopt_analysis
# ---------------------------------------------------------------------------


def _subsample(data: list, max_points: int) -> list:
    if len(data) <= max_points:
        return data
    step = len(data) / max_points
    return [data[int(i * step)] for i in range(max_points)]


def _build_convergence(losses: list[float], max_points: int) -> list[dict[str, float]]:
    """Build convergence data with raw loss and rolling minimum (best so far)."""
    if not losses:
        return []
    result: list[dict[str, float]] = []
    best_so_far = float("inf")
    for loss in losses:
        if loss < best_so_far:
            best_so_far = loss
        result.append({"loss": loss, "best": best_so_far})
    if len(result) <= max_points:
        return result
    step = len(result) / max_points
    return [result[int(i * step)] for i in range(max_points)]


def _skew_kurtosis(values: list[float]) -> tuple[float, float]:
    n = len(values)
    if n < 3:
        return 0.0, 0.0
    mean = sum(values) / n
    m2 = sum((x - mean) ** 2 for x in values) / n
    m3 = sum((x - mean) ** 3 for x in values) / n
    m4 = sum((x - mean) ** 4 for x in values) / n
    if m2 < 1e-15:
        return 0.0, 0.0
    return round(m3 / (m2**1.5), 4), round(m4 / (m2**2) - 3.0, 4)


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if sx < 1e-15 or sy < 1e-15:
        return 0.0
    return round(cov / (sx * sy), 4)


def _histogram_bins(values: list[float], n_bins: int = 10) -> list[dict]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    n_bins = min(n_bins, max(len(set(values)), 1))
    bw = (hi - lo) / n_bins if n_bins > 0 and hi > lo else 1.0
    bins = []
    for i in range(n_bins):
        edge_lo = lo + i * bw
        edge_hi = lo + (i + 1) * bw
        if i == n_bins - 1:
            count = sum(1 for v in values if edge_lo <= v <= edge_hi)
        else:
            count = sum(1 for v in values if edge_lo <= v < edge_hi)
        bins.append({"lo": round(edge_lo, 4), "hi": round(edge_hi, 4), "count": count})
    return bins


def _build_loss_histogram_full(all_losses: list[float]) -> dict[str, Any] | None:
    if not all_losses:
        return None
    best = min(all_losses)
    return {
        "bins": _histogram_bins(all_losses, 10),
        "best_loss": round(best, 4),
        "best_percentile": round(
            sum(1 for v in all_losses if v > best) / max(len(all_losses), 1) * 100, 1,
        ),
    }


def _compute_param_stability(param_values: dict[str, list]) -> dict[str, dict]:
    import statistics

    result: dict[str, dict] = {}
    for pname, vals in param_values.items():
        nums = [v for v in vals if isinstance(v, (int, float))]
        if len(nums) >= 2:
            std = statistics.stdev(nums)
            rng = max(nums) - min(nums)
            ratio = std / rng if rng > 0 else 0.0
            result[pname] = {
                "values": nums,
                "median": statistics.median(nums),
                "std": round(std, 4),
                "std_over_range": round(ratio, 4),
                "stable": ratio < 0.15,
                "unstable": ratio > 0.30,
            }
    return result


def _compute_trade_metrics(best_trades: list[dict]) -> dict:
    result: dict = {}
    profit_ratios = [
        t.get("profit_ratio", 0.0) for t in best_trades if isinstance(t, dict)
    ]
    if len(profit_ratios) >= 10:
        skew, kurt = _skew_kurtosis(profit_ratios)
        result["distribution_analysis"] = {
            "skewness": skew,
            "excess_kurtosis": kurt,
            "n_trades": len(profit_ratios),
            "skew_alert": skew < -1.0,
            "kurtosis_alert": kurt > 3.0,
        }
    profits_sorted = sorted(
        [t.get("profit_abs", 0.0) for t in best_trades if isinstance(t, dict)],
        reverse=True,
    )
    if profits_sorted:
        total_p = sum(profits_sorted)
        if total_p > 0:
            w1 = total_p - profits_sorted[0]
            w2 = total_p - sum(profits_sorted[:2]) if len(profits_sorted) >= 2 else total_p
            result["sans_top_trade"] = {
                "total_profit": round(total_p, 4),
                "without_top1": round(w1, 4),
                "without_top1_pct": round(w1 / total_p * 100, 1),
                "without_top2": round(w2, 4),
                "without_top2_pct": round(w2 / total_p * 100, 1),
                "fragile": w2 <= 0,
            }
    pair_data: dict[str, dict] = {}
    for t in best_trades:
        if not isinstance(t, dict):
            continue
        pair = t.get("pair", "unknown")
        if pair not in pair_data:
            pair_data[pair] = {"profit": 0.0, "count": 0, "wins": 0}
        pair_data[pair]["profit"] += t.get("profit_abs", 0.0)
        pair_data[pair]["count"] += 1
        if t.get("profit_ratio", 0.0) > 0:
            pair_data[pair]["wins"] += 1
    result["pair_profit_distribution"] = sorted(
        [
            {
                "pair": p,
                "profit_abs": round(d["profit"], 4),
                "trade_count": d["count"],
                "win_rate": round(d["wins"] / d["count"], 3) if d["count"] else 0,
                "avg_profit": round(d["profit"] / d["count"], 4) if d["count"] else 0,
            }
            for p, d in pair_data.items()
        ],
        key=lambda x: abs(x["profit_abs"]),
        reverse=True,
    )
    return result


def _compute_param_analytics(
    param_values: dict[str, list],
    top_10: list[dict],
    rm: dict,
) -> dict:
    import statistics

    result: dict = {}
    num_params = {
        k: v
        for k, v in param_values.items()
        if len(v) >= 3 and all(isinstance(x, (int, float)) for x in v)
    }
    pnames = sorted(num_params.keys())
    corr: list[dict] = []
    for i, pa in enumerate(pnames):
        for pb in pnames[i + 1:]:
            r = _pearson(
                [float(x) for x in num_params[pa]],
                [float(x) for x in num_params[pb]],
            )
            corr.append({"param_a": pa, "param_b": pb, "correlation": r})
    result["param_correlation"] = corr

    pc: dict = {"params": pnames, "lines": []}
    for ep in top_10:
        pd = ep.get("params_dict", {})
        normalized = {}
        for pn in pnames:
            vals = num_params.get(pn, [])
            v = pd.get(pn)
            if isinstance(v, (int, float)) and vals and max(vals) > min(vals):
                normalized[pn] = round(
                    (float(v) - min(vals)) / (max(vals) - min(vals)), 4,
                )
            else:
                normalized[pn] = 0.5
        pc["lines"].append({"values": normalized, "loss": ep.get("loss", 0)})
    pc["actual_ranges"] = {
        pn: {"min": min(num_params[pn]), "max": max(num_params[pn])}
        for pn in pnames
        if num_params.get(pn) and max(num_params[pn]) > min(num_params[pn])
    }
    result["parallel_coords"] = pc

    top10_profits = [
        e.get("results_metrics", {}).get("profit_total", 0.0) for e in top_10
    ]
    if len(top10_profits) >= 2:
        med = statistics.median(top10_profits)
        bp = rm.get("profit_total", 0.0)
        gap = round(bp / med, 2) if med > 0 else 0.0
        result["best_vs_median_gap"] = {
            "best_profit": round(bp * 100, 2),
            "median_profit": round(med * 100, 2),
            "gap_ratio": gap,
            "outlier": med > 0 and gap > 2.0,
        }

    def _band(key: str, mult: float = 1.0):
        vals = [e.get("results_metrics", {}).get(key, 0) * mult for e in top_10]
        if not vals:
            return None
        return {
            "min": round(min(vals), 2),
            "median": round(statistics.median(vals), 2),
            "max": round(max(vals), 2),
        }

    result["dispersion_bands"] = {
        "profit": _band("profit_total", 100),
        "drawdown": _band("max_drawdown_account", 100),
        "sharpe": _band("sharpe"),
    }
    return result


def _compute_param_stats(param_values: dict[str, list]) -> dict[str, dict]:
    import statistics
    from collections import Counter

    param_stats: dict[str, dict] = {}
    for pname, vals in param_values.items():
        n = len(vals)
        if n < 2:
            continue
        nums = [v for v in vals if isinstance(v, (int, float))]
        entry: dict[str, Any] = {}
        if len(nums) >= 2:
            entry["median"] = round(statistics.median(nums), 4)
            entry["mean"] = round(sum(nums) / len(nums), 4)
            for k in (3, 5):
                sl = nums[:k]
                if len(sl) >= 2:
                    entry[f"median_top{k}"] = round(statistics.median(sl), 4)
                    entry[f"mean_top{k}"] = round(sum(sl) / len(sl), 4)
        for k in (5, 10):
            sl = vals[:k]
            if sl:
                c = Counter(sl)
                most = c.most_common(1)[0]
                entry[f"majority_top{k}"] = most[0]
                entry[f"majority_top{k}_count"] = most[1]
        if "median_top5" in entry:
            entry["recommended"] = entry["median_top5"]
        elif "median" in entry:
            entry["recommended"] = entry["median"]
        if entry:
            param_stats[pname] = entry
    return param_stats


def _param_tendency(t10_nums: list, rng_lo, rng_hi) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if len(t10_nums) >= 3 and rng_lo is not None and rng_hi is not None:
        full_range = rng_hi - rng_lo
        if full_range > 0:
            t10_span = max(t10_nums) - min(t10_nums)
            ratio = t10_span / full_range
            result["tendency"] = "converging" if ratio < 0.10 else "spread"
            med = sum(t10_nums) / len(t10_nums)
            near_lo = (med - rng_lo) / full_range < 0.05
            near_hi = (rng_hi - med) / full_range < 0.05
            result["boundary_cluster"] = result.get("tendency") == "converging" and (
                near_lo or near_hi
            )
    return result


def _compute_param_deep_dive_inferred(
    best_params_dict: dict,
    top10_values: dict[str, list],
    all_values: dict[str, list],
    all_losses: list[float],
) -> dict[str, dict]:
    import statistics

    result: dict[str, dict] = {}
    for name in sorted(set(list(top10_values.keys()) + list(all_values.keys()))):
        info: dict[str, Any] = {"name": name}
        all_vals = all_values.get(name, [])
        all_nums = [v for v in all_vals if isinstance(v, (int, float))]

        if not all_nums and all_vals:
            info["type"] = "Categorical"
            info["categories"] = list(set(str(v) for v in all_vals))
            freq: dict[str, int] = {}
            for v in all_vals:
                freq[str(v)] = freq.get(str(v), 0) + 1
            info["category_counts"] = freq
        elif all_nums:
            all_ints = all(isinstance(v, int) for v in all_nums)
            info["type"] = "Int" if all_ints else "Float"
            info["range_low"] = min(all_nums)
            info["range_high"] = max(all_nums)

        info["best_value"] = best_params_dict.get(name)

        t10 = top10_values.get(name, [])
        t10_nums = [v for v in t10 if isinstance(v, (int, float))]
        if len(t10_nums) >= 2:
            info["top10_min"] = min(t10_nums)
            info["top10_max"] = max(t10_nums)
            info["top10_median"] = round(statistics.median(t10_nums), 4)
            info["top10_std"] = round(statistics.stdev(t10_nums), 4)

        rng_lo = info.get("range_low")
        rng_hi = info.get("range_high")
        info.update(_param_tendency(t10_nums, rng_lo, rng_hi))

        if all_nums:
            info["histogram"] = _histogram_bins(all_nums, 8)

        if all_nums and len(all_nums) == len(all_losses):
            corr = abs(_pearson([float(x) for x in all_nums], list(all_losses)))
            info["sensitivity"] = round(corr, 4)
            if corr > 0.5:
                info["sensitivity_label"] = "high"
            elif corr > 0.25:
                info["sensitivity_label"] = "medium"
            else:
                info["sensitivity_label"] = "low"

        result[name] = info
    return result


def _compute_monte_carlo(
    best_trades: list[dict], n_sims: int = 1000,
) -> dict | None:
    profits = [t.get("profit_ratio", 0.0) for t in best_trades if isinstance(t, dict)]
    if len(profits) < 10:
        return None
    import random as _rng

    state = _rng.getstate()
    _rng.seed(42)
    finals = []
    for _ in range(n_sims):
        shuffled = profits[:]
        _rng.shuffle(shuffled)
        cum = 1.0
        for p in shuffled:
            cum *= 1 + p
        finals.append((cum - 1) * 100)
    _rng.setstate(state)
    finals.sort()

    def _pct(p: float) -> float:
        idx = int(len(finals) * p / 100)
        return round(finals[min(idx, len(finals) - 1)], 2)

    return {
        "p5": _pct(5), "p25": _pct(25), "p50": _pct(50),
        "p75": _pct(75), "p95": _pct(95),
        "mean": round(sum(finals) / len(finals), 2),
        "n_simulations": n_sims,
        "n_trades": len(profits),
        "prob_positive": round(
            sum(1 for f in finals if f > 0) / len(finals) * 100, 1,
        ),
    }


def _compute_sensitivity_grid(
    top_epochs: list[dict], param_values: dict[str, list],
) -> list[dict]:
    num_params = {
        k: v
        for k, v in param_values.items()
        if len(v) >= 3 and all(isinstance(x, (int, float)) for x in v)
    }
    pnames = sorted(num_params.keys())
    grids = []
    for i, pa in enumerate(pnames):
        for pb in pnames[i + 1:]:
            cells: dict[tuple, list] = {}
            n_bins = 5
            a_vals = [float(x) for x in num_params[pa]]
            b_vals = [float(x) for x in num_params[pb]]
            a_lo, a_hi = min(a_vals), max(a_vals)
            b_lo, b_hi = min(b_vals), max(b_vals)
            a_bw = (a_hi - a_lo) / n_bins if a_hi > a_lo else 1
            b_bw = (b_hi - b_lo) / n_bins if b_hi > b_lo else 1
            for ep in top_epochs:
                pd = ep.get("params_dict", {})
                va, vb = pd.get(pa), pd.get(pb)
                if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
                    continue
                ai = min(int((float(va) - a_lo) / a_bw), n_bins - 1)
                bi = min(int((float(vb) - b_lo) / b_bw), n_bins - 1)
                cells.setdefault((ai, bi), []).append(ep.get("loss", 0))
            grid = []
            for ai in range(n_bins):
                row = []
                for bi in range(n_bins):
                    vs = cells.get((ai, bi), [])
                    row.append(round(sum(vs) / len(vs), 4) if vs else None)
                grid.append(row)
            grids.append({
                "param_a": pa, "param_b": pb, "grid": grid,
                "a_range": [round(a_lo, 4), round(a_hi, 4)],
                "b_range": [round(b_lo, 4), round(b_hi, 4)],
                "n_bins": n_bins,
            })
    return grids[:6]


def _compute_regime_analysis(best_trades: list[dict]) -> dict | None:
    trades = [t for t in best_trades if isinstance(t, dict)]
    if len(trades) < 10:
        return None
    dated = []
    for t in trades:
        ts = t.get("open_timestamp")
        if ts:
            dated.append((ts, t))
    if len(dated) < 6:
        return None
    dated.sort(key=lambda x: x[0])
    mid = len(dated) // 2
    first = [d[1] for d in dated[:mid]]
    second = [d[1] for d in dated[mid:]]

    def _stats(tl: list) -> dict:
        pr = [t.get("profit_ratio", 0) for t in tl]
        pa = [t.get("profit_abs", 0) for t in tl]
        wins = sum(1 for p in pr if p > 0)
        # Compound return: multiply (1 + r) for each trade
        compound = 1.0
        for r in pr:
            compound *= (1 + r)
        return {
            "trades": len(tl),
            "profit_pct": round((compound - 1) * 100, 2),
            "profit_abs": round(sum(pa), 2),
            "win_rate": round(wins / len(tl) * 100, 1) if tl else 0,
            "avg_profit": round(sum(pr) / len(pr) * 100, 2) if pr else 0,
        }

    s1, s2 = _stats(first), _stats(second)
    return {
        "first_half": s1, "second_half": s2,
        "first_label": "First half", "second_label": "Second half",
        "consistent": abs(s1["profit_pct"] - s2["profit_pct"])
        < max(abs(s1["profit_pct"]), abs(s2["profit_pct"]), 1) * 0.5,
    }


def _compute_return_vs_dd(top_epochs: list[dict]) -> list[dict]:
    points = []
    for ep in top_epochs:
        rm = ep.get("results_metrics", {})
        profit = rm.get("profit_total", 0)
        dd = rm.get("max_drawdown_account", 0)
        trades = rm.get("total_trades", 0)
        if trades > 0:
            points.append({
                "profit_pct": round(profit * 100, 2),
                "dd_pct": round(dd * 100, 2),
                "trades": trades,
                "loss": ep.get("loss", 0),
            })
    return points


def _compute_dof_analysis(n_trades: int, n_params: int) -> dict:
    ratio = n_trades / n_params if n_params > 0 else 0
    if ratio >= 30:
        level, label = "green", "Excellent"
    elif ratio >= 15:
        level, label = "green", "Good"
    elif ratio >= 10:
        level, label = "yellow", "Marginal"
    elif ratio >= 5:
        level, label = "orange", "Low"
    else:
        level, label = "red", "Critical"
    return {
        "n_trades": n_trades, "n_params": n_params,
        "ratio": round(ratio, 1), "level": level, "label": label,
    }


def _compute_overfit_warnings(
    dsr: dict | None, param_deep: dict, n_params: int, n_trades: int,
    sans_top: dict | None, bvm_gap: dict | None, dist_analysis: dict | None,
) -> list[dict]:
    warnings: list[dict] = []

    if dsr and not dsr.get("genuine"):
        warnings.append({
            "severity": "high", "warning_type": "dsr",
            "title_en": "DSR: Sharpe likely overfitted",
            "title_fr": "DSR : Sharpe probablement surajusté",
            "detail_en": (
                "Observed Sharpe is below the expected maximum "
                "from pure noise given the number of trials."
            ),
            "detail_fr": (
                "Le Sharpe observé est inférieur au maximum "
                "attendu du bruit pur vu le nombre d'essais."
            ),
            "actions_en": [
                "Reduce total epochs to lower E[max SR]",
                "Increase min-trades to require more statistical evidence",
                "Switch to CalmarHyperOptLoss (penalizes drawdown, harder to overfit)",
                "Use walk-forward validation to confirm out-of-sample performance",
            ],
            "actions_fr": [
                "Réduire le nombre d'epochs pour baisser E[max SR]",
                "Augmenter min-trades pour exiger plus de preuves statistiques",
                "Passer à CalmarHyperOptLoss (pénalise le drawdown, plus dur à surajuster)",
                "Utiliser la validation walk-forward pour "
                "confirmer la performance hors-échantillon",
            ],
            "values": {
                "Sharpe": dsr.get("observed_sharpe"),
                "E[max SR]": dsr.get("expected_max_sharpe"),
                "N trials": dsr.get("n_trials"),
            },
        })

    conv = sum(1 for p in param_deep.values() if p.get("tendency") == "converging")
    if n_params >= 3 and conv / max(n_params, 1) > 0.5:
        warnings.append({
            "severity": "medium", "warning_type": "clustering",
            "title_en": "Excessive parameter clustering",
            "title_fr": "Clustering excessif des paramètres",
            "detail_en": (
                "Most parameters converge to a narrow range "
                "— possible curve-fitting to training data."
            ),
            "detail_fr": (
                "La plupart des paramètres convergent vers "
                "une plage étroite — possible surapprentissage."
            ),
            "actions_en": [
                "Widen parameter search ranges",
                "Reduce epochs to avoid over-exploration",
                "Check if converging params are truly independent",
            ],
            "actions_fr": [
                "Élargir les plages de recherche",
                "Réduire le nombre d'epochs pour éviter la sur-exploration",
                "Vérifier si les paramètres convergents sont vraiment indépendants",
            ],
            "values": {"converging": f"{conv}/{n_params}"},
        })

    if n_params > 0 and n_trades > 0:
        dof = n_trades / n_params
        if dof < 10:
            sev = "high" if dof < 5 else "medium"
            warnings.append({
                "severity": sev, "warning_type": "dof",
                "title_en": "Low degrees of freedom",
                "title_fr": "Peu de degrés de liberté",
                "detail_en": (
                    "Too few trades relative to optimized "
                    "parameters — results lack statistical confidence."
                ),
                "detail_fr": (
                    "Trop peu de trades par rapport aux "
                    "paramètres optimisés — résultats peu fiables statistiquement."
                ),
                "actions_en": [
                    "Increase min-trades (aim for 30+ per parameter)",
                    "Reduce the number of optimized parameters",
                    "Extend the training timerange",
                ],
                "actions_fr": [
                    "Augmenter min-trades (viser 30+ par paramètre)",
                    "Réduire le nombre de paramètres optimisés",
                    "Étendre le timerange d'entraînement",
                ],
                "values": {
                    "trades/params": f"{n_trades}/{n_params}",
                    "ratio": round(dof, 1),
                },
            })

    if sans_top and sans_top.get("fragile"):
        warnings.append({
            "severity": "high", "warning_type": "concentration",
            "title_en": "Profit concentration: fragile",
            "title_fr": "Concentration du profit : fragile",
            "detail_en": (
                "Removing the top 2 trades makes profit "
                "negative — the edge depends on lucky hits."
            ),
            "detail_fr": (
                "Sans les 2 meilleurs trades, le profit "
                "devient négatif — l'edge dépend de coups de chance."
            ),
            "actions_en": [
                "Increase min-trades to force more diversified results",
                "Add more pairs to spread risk",
                "Check if the top trade is a lucky outlier or a repeatable pattern",
            ],
            "actions_fr": [
                "Augmenter min-trades pour forcer des résultats plus diversifiés",
                "Ajouter plus de paires pour répartir le risque",
                "Vérifier si le meilleur trade est un outlier chanceux ou un pattern répétable",
            ],
            "values": {
                "total": sans_top.get("total_profit"),
                "without_top2": sans_top.get("without_top2"),
            },
        })

    if bvm_gap and bvm_gap.get("outlier"):
        warnings.append({
            "severity": "medium", "warning_type": "outlier",
            "title_en": "Best epoch is an outlier",
            "title_fr": "Le meilleur epoch est un outlier",
            "detail_en": (
                "Best profit > 2x the median top-10 — "
                "the best epoch may be the luckiest, not the best."
            ),
            "detail_fr": (
                "Le profit du meilleur > 2x la médiane — "
                "l'epoch est peut-être le plus chanceux, pas le meilleur."
            ),
            "actions_en": [
                "Consider using 2nd or 3rd best epoch",
                "Compare param values of best vs median to find what differs",
                "Run a shorter timerange to check if the best epoch is robust",
            ],
            "actions_fr": [
                "Envisager le 2e ou 3e meilleur epoch",
                "Comparer les paramètres du meilleur vs la médiane pour identifier les écarts",
                "Lancer sur un timerange plus court pour vérifier la robustesse",
            ],
            "values": {
                "best": f"{bvm_gap.get('best_profit')}%",
                "median": f"{bvm_gap.get('median_profit')}%",
                "gap": f"{bvm_gap.get('gap_ratio')}x",
            },
        })

    if dist_analysis:
        if dist_analysis.get("skew_alert"):
            warnings.append({
                "severity": "medium", "warning_type": "skew",
                "title_en": "Negative skew: tail risk",
                "title_fr": "Skew négatif : risque de queue",
                "detail_en": "Return distribution has heavy left tail — occasional large losses.",
                "detail_fr": (
                    "La distribution a une queue gauche lourde — grosses pertes occasionnelles."
                ),
                "actions_en": [
                    "Add or tighten stoploss to cap downside",
                    "Check if a few pairs dominate the left tail",
                    "Consider CalmarHyperOptLoss to penalize drawdown-heavy results",
                ],
                "actions_fr": [
                    "Ajouter ou resserrer le stoploss pour limiter les pertes",
                    "Vérifier si quelques paires dominent la queue gauche",
                    "Envisager CalmarHyperOptLoss pour pénaliser les résultats à fort drawdown",
                ],
                "values": {"skewness": dist_analysis.get("skewness")},
            })
        if dist_analysis.get("kurtosis_alert"):
            warnings.append({
                "severity": "medium", "warning_type": "kurtosis",
                "title_en": "Fat tails: extreme events",
                "title_fr": "Queues épaisses : événements extrêmes",
                "detail_en": (
                    "Excess kurtosis > 3 — extreme gains "
                    "and losses more frequent than normal."
                ),
                "detail_fr": (
                    "Kurtosis > 3 — les gains et pertes "
                    "extrêmes sont plus fréquents que la normale."
                ),
                "actions_en": [
                    "Expect occasional extreme P&L days",
                    "Size positions conservatively",
                    "Use a loss function that accounts for tail risk (Calmar, Sortino)",
                ],
                "actions_fr": [
                    "S'attendre à des jours de P&L extrêmes occasionnels",
                    "Dimensionner les positions prudemment",
                    "Utiliser une loss function qui tient compte du risque de queue "
                    "(Calmar, Sortino)",
                ],
                "values": {"kurtosis": dist_analysis.get("excess_kurtosis")},
            })

    boundary = [n for n, p in param_deep.items() if p.get("boundary_cluster")]
    if boundary:
        warnings.append({
            "severity": "medium", "warning_type": "boundary",
            "title_en": "Boundary clustering",
            "title_fr": "Clustering aux bornes",
            "detail_en": (
                "Some params cluster at the edge of the "
                "search range — the optimum may lie outside."
            ),
            "detail_fr": (
                "Certains paramètres se concentrent au "
                "bord du range — l'optimum est peut-être hors de l'espace de recherche."
            ),
            "actions_en": [
                "Extend the search range for flagged parameters",
                "Re-run hyperopt with wider bounds to check if loss improves",
            ],
            "actions_fr": [
                "Étendre la plage de recherche des paramètres signalés",
                "Relancer l'hyperopt avec des bornes plus larges pour voir si la loss s'améliore",
            ],
            "values": {"params": ", ".join(boundary)},
        })

    return warnings


def get_wfa_resultlist(dirname: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for json_file in sorted(dirname.glob("*_wfa_results_*.json"), reverse=True):
        try:
            with json_file.open() as f:
                data = rapidjson.load(f, number_mode=HYPER_PARAMS_FILE_FORMAT)
            results.append(
                {
                    "run_type": "wfa",
                    "filename": json_file.stem,
                    "strategy": data.get("strategy", ""),
                    "timestamp": (data.get("run_start_ts") or int(json_file.stat().st_mtime)),
                    "timeframe": data.get("timeframe"),
                    "timerange": data.get("timerange"),
                    "has_metadata": True,
                    "hyperopt_loss": data.get("hyperopt_loss"),
                    "verdict_grade": (data.get("verdict", {}).get("grade")),
                    "n_windows": data.get("n_windows"),
                }
            )
        except Exception:
            logger.warning(f"Failed to read WFA result: {json_file}")
    return results


def get_wfa_run_detail(dirname: Path, filename: str) -> dict[str, Any]:
    json_file = dirname / f"{filename}.json"
    with json_file.open() as f:
        return rapidjson.load(f, number_mode=HYPER_PARAMS_FILE_FORMAT)


def get_backtest_snapshot(dirname: Path, filename: str, strategy: str) -> dict[str, Any]:
    zip_path = dirname / f"{filename}.zip"
    result: dict[str, Any] = {}
    try:
        raw = load_file_from_zip(zip_path, f"{filename}_{strategy}.py")
        result["strategy_source"] = raw.decode("utf-8")
    except Exception:
        result["strategy_source"] = None
    try:
        raw = load_file_from_zip(zip_path, f"{filename}_config.json")
        result["config"] = rapidjson.loads(raw)
    except Exception:
        result["config"] = None
    try:
        raw = load_file_from_zip(zip_path, f"{filename}_{strategy}.json")
        result["strategy_params"] = rapidjson.loads(raw)
    except Exception:
        result["strategy_params"] = None
    return result


def compute_snapshot_diff(
    saved_content: str,
    current_path: Path,
) -> dict[str, Any]:
    if not current_path.exists():
        return {
            "snapshot": saved_content,
            "current": None,
            "has_changes": True,
        }
    current = current_path.read_text(encoding="utf-8")
    return {
        "snapshot": saved_content,
        "current": current,
        "has_changes": saved_content != current,
    }


def delete_hyperopt_result(dirname: Path, filename: str) -> None:
    for ext in (".fthypt", ".meta.json", ".fthypt.pkl", ".analysis.json"):
        p = dirname / f"{filename}{ext}"
        if p.exists():
            logger.info(f"Deleting {p.name}")
            p.unlink()


def delete_wfa_result(dirname: Path, filename: str) -> None:
    json_file = dirname / f"{filename}.json"
    if json_file.exists():
        logger.info(f"Deleting {json_file.name}")
        json_file.unlink()
    stem = filename.replace("_wfa_results_", "_")
    parts = stem.rsplit("_", 1)
    if len(parts) == 2:
        prefix, ts = parts[0], parts[1]
        consensus = dirname / f"{prefix}_consensus_{ts}.json"
        if consensus.exists():
            logger.info(f"Deleting {consensus.name}")
            consensus.unlink()


def compute_backtest_analytics(
    dirname: Path, filename: str, strategy: str,
) -> dict[str, Any]:
    zip_path = dirname / f"{filename}.zip"
    json_path = dirname / f"{filename}.json"
    load_path = zip_path if zip_path.exists() else json_path
    if not load_path.exists():
        return {"error": "file_not_found"}

    try:
        stats = load_backtest_stats(load_path)
    except Exception as e:
        logger.warning("Failed to load backtest stats: %s", e)
        return {"error": "load_failed"}

    strat_data = stats.get("strategy", {}).get(strategy, {})
    if not strat_data:
        all_strats = list(stats.get("strategy", {}).keys())
        if all_strats:
            strat_data = stats["strategy"][all_strats[0]]
        else:
            return {"error": "strategy_not_found"}

    trades = strat_data.get("trades", [])
    wins_count = sum(1 for t in trades if isinstance(t, dict) and t.get("profit_ratio", 0) > 0)
    losses_count = sum(
        1 for t in trades if isinstance(t, dict) and t.get("profit_ratio", 0) < 0
    )
    draws_count = len(trades) - wins_count - losses_count

    epoch: dict[str, Any] = {
        "results_metrics": {
            "daily_profit": strat_data.get("daily_profit", []),
            "trades": trades,
            "periodic_breakdown": strat_data.get("periodic_breakdown", {}),
            "starting_balance": strat_data.get("starting_balance", 1000),
            "dry_run_wallet": strat_data.get("starting_balance", 1000),
            "total_trades": strat_data.get("total_trades", 0),
            "profit_total": strat_data.get("profit_total", 0),
            "profit_total_abs": strat_data.get("profit_total_abs", 0),
            "max_drawdown": strat_data.get("max_drawdown", 0),
            "max_drawdown_account": strat_data.get("max_drawdown_account", 0),
            "sharpe": strat_data.get("sharpe", 0),
            "sortino": strat_data.get("sortino", 0),
            "calmar": strat_data.get("calmar", 0),
            "profit_factor": strat_data.get("profit_factor", 0),
            "winrate": strat_data.get("winrate", 0),
            "duration_avg": strat_data.get("duration_avg"),
            "trade_count_long": strat_data.get("trade_count_long", 0),
            "trade_count_short": strat_data.get("trade_count_short", 0),
            "max_consecutive_wins": strat_data.get("max_consecutive_wins", 0),
            "max_consecutive_losses": strat_data.get("max_consecutive_losses", 0),
            "wins": wins_count,
            "losses": losses_count,
            "draws": draws_count,
            "results_per_pair": strat_data.get("results_per_pair", []),
            "exit_reason_summary": strat_data.get("exit_reason_summary", []),
        },
    }

    result = _compute_analytics_for_epoch(epoch, rank=1)

    starting_balance = strat_data.get("starting_balance", 1000)
    daily_profit = strat_data.get("daily_profit", [])
    max_open_trades = strat_data.get("max_open_trades", 1)
    market_change = strat_data.get("market_change", 0)

    if trades:
        result["hourly_pattern"] = _compute_hourly_pattern(trades)
    if trades:
        result["capital_utilization"] = _compute_capital_utilization(
            trades, starting_balance, max_open_trades,
        )
    rpp = strat_data.get("results_per_pair", [])
    if rpp:
        result["pair_heatmap"] = _compute_pair_heatmap(rpp)
    if daily_profit:
        result["benchmark"] = _compute_benchmark_comparison(
            daily_profit, starting_balance, market_change,
        )
    result["order_stats"] = _compute_order_stats(strat_data)

    result["backtest_summary"] = {
        "best_pair": strat_data.get("best_pair"),
        "worst_pair": strat_data.get("worst_pair"),
        "final_balance": strat_data.get("final_balance"),
        "market_change": market_change,
        "backtest_days": strat_data.get("backtest_days"),
        "backtest_start": strat_data.get("backtest_start"),
        "backtest_end": strat_data.get("backtest_end"),
        "timeframe": strat_data.get("timeframe"),
        "timerange": strat_data.get("timerange"),
        "trades_per_day": strat_data.get("trades_per_day"),
        "winning_days": strat_data.get("winning_days"),
        "losing_days": strat_data.get("losing_days"),
        "draw_days": strat_data.get("draw_days"),
        "stoploss": strat_data.get("stoploss"),
        "trailing_stop": strat_data.get("trailing_stop"),
        "trailing_stop_positive": strat_data.get("trailing_stop_positive"),
        "trailing_stop_positive_offset": strat_data.get("trailing_stop_positive_offset"),
        "trailing_only_offset_is_reached": strat_data.get("trailing_only_offset_is_reached"),
        "minimal_roi": strat_data.get("minimal_roi"),
        "max_open_trades": max_open_trades,
        "holding_avg": strat_data.get("holding_avg"),
        "winner_holding_avg": strat_data.get("winner_holding_avg"),
        "loser_holding_avg": strat_data.get("loser_holding_avg"),
        "sqn": strat_data.get("sqn"),
        "cagr": strat_data.get("cagr"),
        "expectancy": strat_data.get("expectancy"),
        "expectancy_ratio": strat_data.get("expectancy_ratio"),
        "rejected_signals": strat_data.get("rejected_signals", 0),
        "timedout_entry_orders": strat_data.get("timedout_entry_orders", 0),
        "timedout_exit_orders": strat_data.get("timedout_exit_orders", 0),
        "canceled_trade_entries": strat_data.get("canceled_trade_entries", 0),
    }

    return result


def _compute_hourly_pattern(trades: list) -> dict:
    from collections import defaultdict
    from datetime import datetime

    by_hour: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        if not isinstance(t, dict):
            continue
        pnl = t.get("profit_pct") or t.get("profit_ratio", 0)
        if not isinstance(pnl, (int, float)):
            continue
        close_date = t.get("close_date") or t.get("close_timestamp")
        if not close_date:
            continue
        try:
            if isinstance(close_date, str):
                dt = datetime.fromisoformat(close_date.replace("Z", "+00:00"))
            elif isinstance(close_date, (int, float)):
                dt = datetime.fromtimestamp(close_date / 1000)
            else:
                continue
            by_hour[dt.hour].append(float(pnl))
        except Exception:
            continue

    hours = []
    for h in range(24):
        profits = by_hour.get(h, [])
        count = len(profits)
        avg = round(sum(profits) / count, 4) if count else 0
        total = round(sum(profits), 4)
        wins = sum(1 for p in profits if p > 0)
        hours.append({
            "hour": h,
            "trades": count,
            "avg_profit": avg,
            "total_profit": total,
            "winrate": round(wins / count, 4) if count else 0,
        })
    return {"hours": hours}


def _compute_capital_utilization(
    trades: list, starting_balance: float, max_open_trades: int,
) -> list[dict]:
    from collections import defaultdict
    from datetime import datetime

    daily_stake: dict[str, float] = defaultdict(float)
    daily_count: dict[str, int] = defaultdict(int)

    for t in trades:
        if not isinstance(t, dict):
            continue
        open_date = t.get("open_date", "")
        stake = t.get("stake_amount", 0)
        if not isinstance(open_date, str) or len(open_date) < 10:
            continue
        day = open_date[:10]
        daily_stake[day] += float(stake) if isinstance(stake, (int, float)) else 0
        daily_count[day] += 1

    if not daily_stake:
        return []

    max_possible = starting_balance * max(max_open_trades, 1)
    result = []
    for day in sorted(daily_stake.keys()):
        util = min(daily_stake[day] / max_possible * 100, 100) if max_possible > 0 else 0
        result.append({
            "date": day,
            "utilization_pct": round(util, 2),
            "deployed": round(daily_stake[day], 2),
            "trades": daily_count[day],
        })

    if len(result) > 1000:
        step = len(result) / 1000
        result = [result[int(i * step)] for i in range(1000)]
    return result


def _compute_pair_heatmap(results_per_pair: list) -> list[dict]:
    result = []
    for p in results_per_pair:
        if not isinstance(p, dict):
            continue
        key = p.get("key", "")
        if key == "TOTAL":
            continue
        result.append({
            "pair": key,
            "trades": p.get("trades", 0),
            "winrate": round(p.get("winrate", 0) * 100, 1)
            if isinstance(p.get("winrate"), (int, float)) and p.get("winrate", 0) <= 1
            else round(p.get("winrate", 0), 1),
            "avg_profit": round(p.get("profit_mean_pct", 0), 2),
            "total_profit": round(p.get("profit_total_abs", 0), 2),
            "profit_factor": round(p.get("profit_factor", 0), 2),
            "sqn": round(p.get("sqn", 0), 2),
            "sharpe": round(p.get("sharpe", 0), 2),
            "max_drawdown": round(p.get("max_drawdown_account", 0) * 100, 2)
            if isinstance(p.get("max_drawdown_account"), (int, float))
            else 0,
            "duration_avg": p.get("duration_avg"),
        })
    result.sort(key=lambda x: abs(x["total_profit"]), reverse=True)
    return result


def _compute_benchmark_comparison(
    daily_profit: list, starting_balance: float, market_change: float,
) -> dict:
    if not daily_profit:
        return {}

    strat_equity = []
    balance = starting_balance
    for date_str, pnl in daily_profit:
        balance += pnl
        strat_equity.append({"date": date_str, "balance": round(balance, 2)})

    n_days = len(daily_profit)
    bh_equity = []
    if n_days > 0 and market_change != 0:
        daily_bh_return = (1 + market_change) ** (1 / n_days) - 1
        bh_balance = starting_balance
        for date_str, _ in daily_profit:
            bh_balance *= 1 + daily_bh_return
            bh_equity.append({"date": date_str, "balance": round(bh_balance, 2)})

    strat_total = (balance / starting_balance - 1) if starting_balance > 0 else 0

    return {
        "strategy_equity": strat_equity if len(strat_equity) <= 500
        else [strat_equity[int(i * len(strat_equity) / 500)] for i in range(500)],
        "buyhold_equity": bh_equity if len(bh_equity) <= 500
        else [bh_equity[int(i * len(bh_equity) / 500)] for i in range(500)],
        "strategy_return": round(strat_total * 100, 2),
        "buyhold_return": round(market_change * 100, 2),
        "alpha": round((strat_total - market_change) * 100, 2),
    }


def _compute_order_stats(strat_data: dict) -> dict:
    return {
        "rejected_signals": strat_data.get("rejected_signals", 0),
        "timedout_entry_orders": strat_data.get("timedout_entry_orders", 0),
        "timedout_exit_orders": strat_data.get("timedout_exit_orders", 0),
        "canceled_trade_entries": strat_data.get("canceled_trade_entries", 0),
        "canceled_entry_orders": strat_data.get("canceled_entry_orders", 0),
        "replaced_entry_orders": strat_data.get("replaced_entry_orders", 0),
    }


def convert_backtest_entries(
    dirname: Path,
) -> list[dict[str, Any]]:
    raw = get_backtest_resultlist(dirname)
    results = []
    for entry in raw:
        result: dict[str, Any] = {
            "run_type": "backtest",
            "filename": entry["filename"],
            "strategy": entry["strategy"],
            "timestamp": entry.get("backtest_start_time", 0),
            "timeframe": entry.get("timeframe"),
            "timerange": None,
            "has_metadata": True,
            "run_id": entry.get("run_id"),
            "notes": entry.get("notes"),
        }
        try:
            raw = load_file_from_zip(
                dirname / f"{entry['filename']}.zip", entry["strategy"]
            )
            if raw:
                strat_data = rapidjson.loads(raw)
                result["total_profit_pct"] = round(
                    strat_data.get("profit_total", 0) * 100, 2
                )
                result["total_trades"] = strat_data.get("total_trades", 0)
                result["best_sharpe"] = strat_data.get("sharpe")
                result["best_loss"] = None
        except Exception:
            pass
        results.append(result)
    return results
