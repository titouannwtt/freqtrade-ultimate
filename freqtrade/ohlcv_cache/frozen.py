"""
Detect candles that a cached series holds in a frozen, partial state.

Series persisted before `CandleSeries.partial_ts` existed carry no markers. A candle
that was cached while forming has a volume below the sum of the finer candles that
cover it, so it can be recognised from the daemon's own store: the same series at a
finer timeframe (5m under 15m under 1h ...). Pure functions, no I/O.

Coverage is bounded by the depth of the finer series (5000 candles each: 5m ~17 days,
15m ~52 days, 30m ~104 days, 1h ~208 days). Candles older than that, and the finest
timeframe itself, cannot be checked; they age out through trim_to().
"""

from __future__ import annotations

import numpy as np

from freqtrade.ohlcv_cache.store import PARTIAL_GRACE_MS, CandleSeries


def detect_frozen(
    series: CandleSeries,
    finer: list[CandleSeries],
    now_ms: int,
    tol: float = 0.01,
) -> set[int]:
    """Open timestamps of closed candles of `series` below every finer aggregate by > tol."""
    if len(series.candles) == 0 or not finer:
        return set()
    ts = series.candles[:, 0].astype(np.int64)
    vol = series.candles[:, 5]
    n = len(ts)
    best = np.full(n, -1.0)
    for f in finer:
        if len(f.candles) == 0 or series.tf_ms % f.tf_ms or f.tf_ms >= series.tf_ms:
            continue
        f_ts = f.candles[:, 0].astype(np.int64)
        idx = np.searchsorted(ts, f_ts, side="right") - 1
        valid = (idx >= 0) & (f_ts < ts[np.clip(idx, 0, None)] + series.tf_ms)
        if not valid.any():
            continue
        counts = np.bincount(idx[valid], minlength=n)
        sums = np.bincount(idx[valid], weights=f.candles[valid, 5], minlength=n)
        full = counts == series.tf_ms // f.tf_ms
        best = np.where(full & (sums > best), sums, best)
    closed = ts + series.tf_ms + PARTIAL_GRACE_MS <= now_ms
    bad = closed & (best >= 0) & (vol < best * (1.0 - tol))
    return {int(t) for t in ts[bad]}
