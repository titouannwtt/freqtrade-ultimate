import logging
import re
from pathlib import Path
from typing import Any

import rapidjson

from freqtrade.data.btanalysis.bt_fileutils import (
    get_backtest_resultlist,
    load_file_from_zip,
)
from freqtrade.optimize.hyperopt_tools import HyperoptTools


logger = logging.getLogger(__name__)

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
    return {
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


def _entry_from_filename(fthypt: Path) -> dict[str, Any]:
    m = _FTHYPT_NAME_RE.match(fthypt.name)
    strategy = m.group(1) if m else fthypt.stem
    return {
        "run_type": "hyperopt",
        "filename": fthypt.stem,
        "strategy": strategy,
        "timestamp": int(fthypt.stat().st_mtime),
        "has_metadata": False,
    }


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

    all_losses = [e.get("loss", 1e6) for e in all_epochs]
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
            }
            for i, e in enumerate(top_10)
        ],
        "total_epochs": total_epochs,
        "convergence": _subsample(all_losses, 500),
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
        return {
            "trades": len(tl),
            "profit_pct": round(sum(pr) * 100, 2),
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


def convert_backtest_entries(
    dirname: Path,
) -> list[dict[str, Any]]:
    raw = get_backtest_resultlist(dirname)
    results = []
    for entry in raw:
        results.append(
            {
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
        )
    return results
