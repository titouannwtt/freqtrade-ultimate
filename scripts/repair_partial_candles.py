#!/usr/bin/env python3
"""
Find (and optionally rebuild) OHLCV candles that were stored while still forming.

Background: between 2026-04 and the fix of the historic download path,
`freqtrade download-data` stored candles served by the ftcache daemon while they
were still forming (see CachedExchangeMixin.get_historic_ohlcv). Such a candle
has a volume far below the real one and is never rewritten by the incremental
download. A forming candle is a snapshot: its volume only grows, so the value
rebuilt from complete finer candles is an upper bound that equals the truth
whenever the finer candles are themselves complete.

For each pair and timeframe the script compares every stored candle with the
aggregate of the finer timeframes that fully cover it (1m, 5m, 15m, 30m, 1h,
2h, 4h), in that order, and keeps the aggregate with the highest volume.
A candle is "bad" when stored volume < aggregate volume * (1 - tol). Finer
timeframes are processed first, and repaired in memory before they are used as a
source for the coarser ones, so a partial 5m candle rebuilt from 1m also
corrects the 15m/30m/1h... built on it.

When the stored volume is ABOVE the aggregate (by more than tol) the finer
candles are the incomplete side: reported as "finer_suspect", never "fixed"
(there is no finer source to rebuild them from; 5m is only repairable from 1m).

Dry-run by default: nothing is written. `--apply` rewrites the touched files
atomically (temp file + os.replace) in the freqtrade feather layout.

Do NOT --apply while a hyperopt, backtest or the strategy generator reads the
data directory: stop them first (the script refuses if it sees one, see --force).

Usage:
  python scripts/repair_partial_candles.py -d user_data/data/hyperliquid
  python scripts/repair_partial_candles.py -d user_data/data/hyperliquid \
      --since 2026-04-01 --pairs BTC SOL --csv /tmp/repair.csv
  python scripts/repair_partial_candles.py -d user_data/data/hyperliquid --apply \
      --backup-dir /path/with/space
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd


TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}
ORDER = list(TF_MS)  # finest first
COLUMNS = ["date", "open", "high", "low", "close", "volume"]
FNAME_RE = re.compile(r"^(?P<pair>.+)-(?P<tf>\d+[mhdw])-(?P<ct>futures)\.feather$")


@dataclass
class TfStats:
    files: int = 0
    scanned: int = 0  # stored candles that have at least one complete finer source
    bad: int = 0
    finer_suspect: int = 0
    no_source: int = 0
    by_source: Counter = field(default_factory=Counter)
    pairs_bad: Counter = field(default_factory=Counter)


def _ts_ms(df: pd.DataFrame) -> np.ndarray:
    d = df["date"]
    if getattr(d.dt, "tz", None) is not None:
        d = d.dt.tz_convert("UTC").dt.tz_localize(None)
    return d.to_numpy().astype("datetime64[ms]").astype("int64")


def aggregate(target_ts: np.ndarray, tf_ms: int, src: pd.DataFrame, src_ms: int) -> pd.DataFrame:
    """Aggregate `src` candles into the buckets [t, t + tf_ms) of `target_ts`.

    Returns a frame indexed like target_ts (NaN where the bucket is not fully
    covered by `src`: every one of the tf_ms // src_ms sub-candles is required).
    """
    n_expected = tf_ms // src_ms
    out = pd.DataFrame(
        np.nan, index=range(len(target_ts)), columns=["open", "high", "low", "close", "volume"]
    )
    if src.empty or len(target_ts) == 0:
        return out
    s_ts = _ts_ms(src)
    idx = np.searchsorted(target_ts, s_ts, side="right") - 1
    valid = (idx >= 0) & (s_ts < target_ts[np.clip(idx, 0, None)] + tf_ms)
    if not valid.any():
        return out
    sub = src.loc[valid, ["open", "high", "low", "close", "volume"]].copy()
    sub["b"] = idx[valid]
    g = sub.groupby("b", sort=True)
    agg = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "n": g["volume"].size(),
        }
    )
    agg = agg[agg["n"] == n_expected].drop(columns="n")
    out.loc[agg.index, agg.columns] = agg.to_numpy()
    return out


def analyse_pair(
    frames: dict[str, pd.DataFrame],
    since_ms: int,
    now_ms: int,
    tol: float,
    stats: dict[str, TfStats],
    pair: str,
) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    """Return (repaired frames, list of proposed fixes). `frames` is not modified."""
    repaired = {tf: df.copy() for tf, df in frames.items()}
    fixes: list[dict] = []
    for tf in ORDER:
        if tf not in repaired:
            continue
        df = repaired[tf]
        st = stats[tf]
        tf_ms = TF_MS[tf]
        t = _ts_ms(df)
        sources = [s for s in ORDER if s in repaired and TF_MS[s] < tf_ms and tf_ms % TF_MS[s] == 0]
        if not sources:
            continue
        aggs = {s: aggregate(t, tf_ms, repaired[s], TF_MS[s]) for s in sources}
        vol = np.stack([aggs[s]["volume"].to_numpy() for s in sources])  # (S, N)
        has = ~np.isnan(vol)
        any_src = has.any(axis=0)
        # best source = highest volume (a forming candle only ever under-counts);
        # np.argmax returns the first max, i.e. the finest source on ties.
        best_i = np.argmax(np.where(has, vol, -1.0), axis=0)
        best_vol = vol[best_i, np.arange(len(t))]
        in_window = (t >= since_ms) & (t + tf_ms <= now_ms)
        st.no_source += int((in_window & ~any_src).sum())
        cand = in_window & any_src
        st.scanned += int(cand.sum())
        stored = df["volume"].to_numpy()
        bad = cand & (stored < best_vol * (1.0 - tol))
        over = cand & (stored > best_vol * (1.0 + tol))
        st.finer_suspect += int(over.sum())
        for i in np.flatnonzero(bad):
            s = sources[int(best_i[i])]
            a = aggs[s].iloc[i]
            fixes.append(
                {
                    "pair": pair,
                    "timeframe": tf,
                    "date": pd.Timestamp(int(t[i]), unit="ms", tz="UTC").isoformat(),
                    "stored_volume": float(stored[i]),
                    "proposed_volume": float(a["volume"]),
                    "ratio": float(stored[i] / a["volume"]) if a["volume"] else float("nan"),
                    "source": s,
                    "stored_close": float(df["close"].iat[i]),
                    "proposed_close": float(a["close"]),
                }
            )
            for col in ("open", "high", "low", "close", "volume"):
                df.iat[i, df.columns.get_loc(col)] = a[col]
            st.bad += 1
            st.by_source[s] += 1
            st.pairs_bad[pair] += 1
    return repaired, fixes


def list_files(datadir: Path, pairs: list[str] | None):
    fdir = datadir / "futures"
    out: dict[str, dict[str, Path]] = defaultdict(dict)
    for f in sorted(fdir.iterdir()):
        m = FNAME_RE.match(f.name)
        if not m or m["tf"] not in TF_MS:
            continue
        pair = m["pair"]
        if pairs and not any(pair == p or pair.startswith(p + "_") for p in pairs):
            continue
        out[pair][m["tf"]] = f
    return out


def write_atomic(path: Path, df: pd.DataFrame) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        df.reset_index(drop=True).loc[:, COLUMNS].to_feather(
            tmp, compression_level=9, compression="lz4"
        )
        shutil.copymode(path, tmp)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def readers_running() -> list[str]:
    try:
        res = subprocess.run(
            ["pgrep", "-af", r"freqtrade (hyperopt|backtesting|download-data)|run_continuous"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    me = str(os.getpid())
    return [
        line
        for line in res.stdout.splitlines()
        if line.split(" ", 1)[0] != me and "pgrep" not in line
    ]


CSV_FIELDS = [
    "pair",
    "timeframe",
    "date",
    "stored_volume",
    "proposed_volume",
    "ratio",
    "source",
    "stored_close",
    "proposed_close",
]


def write_csv(path: Path, fixes: list[dict]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(fixes)


def print_report(args: argparse.Namespace, stats: dict[str, TfStats], targets: set[str]) -> None:
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"datadir={args.datadir} since={args.since} tol={args.tol} mode={mode}")
    print(
        f"{'tf':<4} {'files':>6} {'scanned':>9} {'bad':>7} {'bad%':>6} "
        f"{'finer_suspect':>14} {'no_source':>10}  sources"
    )
    for tf in ORDER:
        st = stats[tf]
        if tf not in targets or st.files == 0:
            continue
        pct = 100.0 * st.bad / st.scanned if st.scanned else 0.0
        src = ", ".join(
            f"{k}:{v}" for k, v in sorted(st.by_source.items(), key=lambda kv: TF_MS[kv[0]])
        )
        print(
            f"{tf:<4} {st.files:>6} {st.scanned:>9} {st.bad:>7} {pct:>5.1f}% "
            f"{st.finer_suspect:>14} {st.no_source:>10}  {src}"
        )
    print(f"total bad candles: {sum(s.bad for tf, s in stats.items() if tf in targets)}")
    top: Counter = Counter()
    for tf, st in stats.items():
        if tf in targets:
            top.update(st.pairs_bad)
    if top:
        print("top pairs: " + ", ".join(f"{p}:{n}" for p, n in top.most_common(10)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("-d", "--datadir", type=Path, required=True)
    ap.add_argument("--pairs", nargs="+", help="Pair prefixes, e.g. BTC SOL (file names)")
    ap.add_argument("-t", "--timeframes", nargs="+", choices=ORDER, help="Targets to check")
    ap.add_argument("--since", default="2026-04-01", help="Check candles from this date (UTC)")
    ap.add_argument("--tol", type=float, default=0.01, help="Relative volume tolerance")
    ap.add_argument("--csv", type=Path, help="Write every proposed fix to this CSV")
    ap.add_argument("--apply", action="store_true", help="Rewrite the files (default: dry-run)")
    ap.add_argument("--backup-dir", type=Path, help="With --apply: copy originals here first")
    ap.add_argument("--force", action="store_true", help="--apply even if a reader is running")
    args = ap.parse_args(argv)

    since_ms = int(datetime.fromisoformat(args.since).replace(tzinfo=UTC).timestamp() * 1000)
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    targets = set(args.timeframes) if args.timeframes else set(ORDER)

    if args.apply and not args.force:
        running = readers_running()
        if running:
            print("Refusing --apply: data readers are running (use --force to override):")
            print("\n".join(running[:10]))
            return 2

    files = list_files(args.datadir, args.pairs)
    stats = {tf: TfStats() for tf in ORDER}
    all_fixes: list[dict] = []
    written = 0
    for pair, tfmap in files.items():
        frames = {tf: pd.read_feather(p)[COLUMNS] for tf, p in tfmap.items()}
        for tf in tfmap:
            stats[tf].files += 1
        repaired, fixes = analyse_pair(frames, since_ms, now_ms, args.tol, stats, pair)
        # Only targets requested are reported/written; the rest still served as sources.
        fixes = [f for f in fixes if f["timeframe"] in targets]
        all_fixes.extend(fixes)
        if args.apply and fixes:
            for tf in {f["timeframe"] for f in fixes}:
                path = tfmap[tf]
                if args.backup_dir:
                    args.backup_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, args.backup_dir / path.name)
                write_atomic(path, repaired[tf])
                written += 1

    print_report(args, stats, targets)
    if args.csv:
        write_csv(args.csv, all_fixes)
        print(f"fixes written to {args.csv}")
    if args.apply:
        print(f"files rewritten: {written}")
    else:
        print("dry-run: nothing written (use --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
