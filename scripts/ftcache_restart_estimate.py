#!/usr/bin/env python3
"""
Estimate the refetch weight a daemon restart will trigger (read-only, on a COPY).

Since the persistence loader read every millisecond-unit feather as 1970 (pandas 3
writes ms, the old loader assumed ns), most persisted series hold only the few real
rows written since the last restart. The fixed loader drops the corrupt rows, so the
next live request of such a series finds a prefix gap and refetches the history
(compute_gaps), at the request's priority, through the token bucket.

This script replays the fixed loader on a copy of the persistence directory and sums
the weight of the prefix + suffix gaps for a request of `--limit` candles (what bots ask
for: 5000), with Hyperliquid's weight model (20 per call + 1 per 60 candles, 5000
candles per call). A series counts as "active" when a live request touched it within
`--active-hours` (last_live_refresh_wall_ms in its meta file): those are the ones bots
will actually ask for, the others are only refetched if a bot starts reading them.

It never opens the daemon socket and never writes.

Usage:
  cp -r ~/.freqtrade/ftcache/hyperliquid /some/copy/
  python scripts/ftcache_restart_estimate.py --root /some/copy
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


MIN_VALID_TS_MS = 1_483_228_800_000
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
         "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}  # fmt: skip
FNAME_RE = re.compile(r"^(?P<pair>.+?)-(?P<tf>\d+[smhdw])(?:-(?P<ct>[a-zA-Z_]+))?\.feather$")


def call_weight(n_candles: int, funding: bool = False) -> float:
    if funding:
        return 20.0 + math.ceil(min(n_candles, 500) / 20)
    return 20.0 + math.ceil(min(n_candles, 5000) / 60)


def gap_weight(n_candles: int, funding: bool = False) -> float:
    per_call = 500 if funding else 5000
    full, rest = divmod(n_candles, per_call)
    return full * call_weight(per_call, funding) + (call_weight(rest, funding) if rest else 0.0)


def to_ms(dates: pd.Series) -> np.ndarray:
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_convert("UTC").dt.tz_localize(None)
    return dates.to_numpy().astype("datetime64[ms]").astype("int64")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--root", type=Path, required=True, help="copy of <persistence>/<exchange>")
    ap.add_argument("--limit", type=int, default=5000, help="candles a bot requests per series")
    ap.add_argument("--active-hours", type=float, default=24.0)
    args = ap.parse_args()

    now_ms = int(time.time() * 1000)
    # (tf, active) -> [series, polluted, weight]
    acc: dict[tuple[str, bool], list[float]] = defaultdict(lambda: [0, 0, 0.0])
    n_files = n_polluted = n_dropped_rows = 0
    for f in sorted(args.root.rglob("*.feather")):
        m = FNAME_RE.match(f.name)
        if not m or m["tf"] not in TF_MS:
            continue
        funding = m["ct"] == "funding_rate"
        tf_ms = TF_MS[m["tf"]]
        meta_f = f.with_suffix(".meta.json")
        meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
        active = (now_ms - int(meta.get("last_live_refresh_wall_ms", 0) or 0)) < (
            args.active_hours * 3_600_000
        )
        ts = to_ms(pd.read_feather(f, columns=["date"])["date"])
        good = ts[ts >= MIN_VALID_TS_MS]
        n_files += 1
        polluted = len(good) < len(ts)
        n_polluted += polluted
        n_dropped_rows += len(ts) - len(good)
        end = (now_ms // tf_ms + 1) * tf_ms
        start = end - args.limit * tf_ms
        if len(good) == 0:
            missing = args.limit
        else:
            first = (int(good.min()) // tf_ms) * tf_ms
            last = (int(good.max()) // tf_ms) * tf_ms
            prefix = max(0, (min(first, end) - start) // tf_ms)
            suffix = max(0, (end - (last + tf_ms)) // tf_ms)
            missing = prefix + suffix
        key = (m["tf"] if not funding else "funding", active)
        a = acc[key]
        a[0] += 1
        a[1] += polluted
        a[2] += gap_weight(int(missing), funding) if missing else 0.0

    print(f"series: {n_files}, polluted (1970 rows): {n_polluted}")
    print(f"corrupt rows dropped: {n_dropped_rows}")
    print(
        f"request size: {args.limit} candles; active = live-refreshed within {args.active_hours}h"
    )
    print(f"{'tf':<8} {'active':>6} {'series':>7} {'polluted':>9} {'weight':>10}")
    tot = {True: 0.0, False: 0.0}
    for (tf, active), (n, p, w) in sorted(acc.items()):
        print(f"{tf:<8} {str(active):>6} {int(n):>7} {int(p):>9} {w:>10.0f}")
        tot[active] += w
    print(f"TOTAL weight, active series: {tot[True]:.0f}; inactive series: {tot[False]:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
