#!/usr/bin/env python3
"""
Estimate how many candles the ftcache daemon's PERSISTED cache holds in a frozen,
partial state (read-only report).

Context: before the partial-candle tracking (store.CandleSeries.partial_ts), the
daemon cached candles it received while they were still forming and never refetched
them once a newer candle was appended. The persisted series
(~/.freqtrade/ftcache/<exchange>/<mode>/*.feather, 5000 candles each) still hold them.

The fixed daemon flags and heals them ITSELF: on the first access of a series
persisted before the fix, it compares it with the finer series of its own store
(freqtrade/ohlcv_cache/frozen.py) and refetches the frozen candles once, through its
rate limiter. No offline step, hence no race with the daemon (which bots relaunch on
demand). This script only reports what that scan will find, with the same method as
scripts/repair_partial_candles.py. Run it on a COPY of the persistence directory; it
never writes and never opens the daemon socket.

Coverage limits (same as the daemon scan): a candle is only checked against finer
series held in the cache, and each holds 5000 candles (5m ~17 days, 15m ~52 days,
30m ~104 days, 1h ~208 days). The finest timeframe (5m) and anything older than the
finer series cannot be checked: the figure is a lower bound and the rest ages out
through trim_to().

Usage:
  cp -r ~/.freqtrade/ftcache/hyperliquid /some/copy/
  python scripts/ftcache_flag_partials.py --root /some/copy --exchange hyperliquid
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd


_SPEC = importlib.util.spec_from_file_location(
    "repair_partial_candles", Path(__file__).with_name("repair_partial_candles.py")
)
repair = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = repair
_SPEC.loader.exec_module(repair)

# {pair_safe}-{tf}.feather ; candle types other than futures/spot get a suffix.
FNAME_RE = re.compile(r"^(?P<pair>.+)-(?P<tf>\d+[mhdw])\.feather$")


def list_series(root: Path, exchange: str, mode: str) -> dict[str, dict[str, Path]]:
    out: dict[str, dict[str, Path]] = defaultdict(dict)
    for f in sorted((root / exchange / mode).glob("*.feather")):
        m = FNAME_RE.match(f.name)
        if m and m["tf"] in repair.TF_MS:  # funding_rate etc. never match: never partial
            out[m["pair"]][m["tf"]] = f
    return out


def load_frames(tfmap: dict[str, Path]) -> dict[str, pd.DataFrame]:
    frames = {}
    for tf, p in tfmap.items():
        df = pd.read_feather(p)[repair.COLUMNS]
        df["date"] = df["date"].dt.as_unit("ms")
        frames[tf] = df
    return frames


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--root", type=Path, required=True, help="ftcache persistence root")
    ap.add_argument("--exchange", default="hyperliquid")
    ap.add_argument("--mode", default="futures", help="trading mode directory")
    ap.add_argument("--since", default="2026-04-01", help="Check candles from this date (UTC)")
    ap.add_argument("--tol", type=float, default=0.01)
    ap.add_argument("--csv", type=Path)
    args = ap.parse_args(argv)

    since_ms = int(datetime.fromisoformat(args.since).replace(tzinfo=UTC).timestamp() * 1000)
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    stats = {tf: repair.TfStats() for tf in repair.ORDER}
    flagged: dict[Path, set[int]] = defaultdict(set)  # feather path -> ts
    rows: list[dict] = []
    n_series = 0
    for pair, tfmap in list_series(args.root, args.exchange, args.mode).items():
        frames = load_frames(tfmap)
        for tf in tfmap:
            stats[tf].files += 1
            n_series += 1
        _, fixes = repair.analyse_pair(frames, since_ms, now_ms, args.tol, stats, pair)
        for f in fixes:
            ts = int(pd.Timestamp(f["date"]).timestamp() * 1000)
            flagged[tfmap[f["timeframe"]]].add(ts)
            rows.append(f)

    print(f"root={args.root} exchange={args.exchange}/{args.mode} since={args.since} (read-only)")
    print(f"series scanned: {n_series}; series holding frozen candles: {len(flagged)}")
    print(
        f"{'tf':<4} {'series':>7} {'scanned':>9} {'frozen':>8} {'frozen%':>8} {'finer_suspect':>14}"
    )
    for tf in repair.ORDER:
        st = stats[tf]
        if st.files == 0:
            continue
        pct = 100.0 * st.bad / st.scanned if st.scanned else 0.0
        print(
            f"{tf:<4} {st.files:>7} {st.scanned:>9} {st.bad:>8} {pct:>7.1f}% {st.finer_suspect:>14}"
        )
    print(f"total frozen candles: {sum(s.bad for s in stats.values())}")
    if args.csv:
        repair.write_csv(args.csv, rows)
        print(f"details written to {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
