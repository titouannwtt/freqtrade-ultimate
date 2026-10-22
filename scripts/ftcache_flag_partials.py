#!/usr/bin/env python3
"""
Flag candles stored while forming in the ftcache daemon's PERSISTED cache.

Context: before the fix of the partial-candle tracking (store.CandleSeries.partial_ts),
the daemon cached candles it received while they were still forming and never
refetched them once a newer candle was appended. The persisted series
(~/.freqtrade/ftcache/<exchange>/<mode>/*.feather, 5000 candles each) still hold
them, and live bots receive them (informatives, startup candles).

The fixed daemon refetches every candle listed in `partial_ts` of a series' meta
file once it is closed (one contiguous call per series). This tool finds the
frozen candles of the persisted cache, by comparing each series with the
aggregate of its finer timeframes held in the same cache (same method as
scripts/repair_partial_candles.py), and writes their timestamps to the meta files.
After the next daemon start, the daemon heals itself, lazily, as bots ask for the
series, through its own rate limiter.

Dry-run by default (reads only). Run it on a COPY of the persistence directory to
estimate, or on the real one with `--apply` ONLY WHILE THE DAEMON IS STOPPED: the
daemon rewrites the meta files of dirty series at every flush and would overwrite
the flags. This tool never opens the daemon socket.

Usage:
  python scripts/ftcache_flag_partials.py --root /copy/of/ftcache --exchange hyperliquid
  python scripts/ftcache_flag_partials.py --root ~/.freqtrade/ftcache --exchange hyperliquid \
      --apply --backup-dir /path/with/space      # daemon stopped!
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


def daemon_running() -> list[str]:
    try:
        res = subprocess.run(
            ["pgrep", "-af", r"freqtrade\.ohlcv_cache\.daemon|ohlcv_cache/daemon\.py"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    me = str(os.getpid())
    return [ln for ln in res.stdout.splitlines() if ln.split(" ", 1)[0] != me and "pgrep" not in ln]


def write_meta(path: Path, meta: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(meta))
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def apply_flags(args: argparse.Namespace, flagged: dict[Path, set[int]]) -> None:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_root = args.backup_dir / f"ftcache_meta_{stamp}"
    backup_root.mkdir(parents=True, exist_ok=False)
    written = 0
    for feather, ts_set in flagged.items():
        meta_path = feather.with_suffix(".meta.json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        dest = backup_root / meta_path.relative_to(args.root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if meta_path.exists():
            shutil.copy2(meta_path, dest)
        meta["partial_ts"] = sorted(set(meta.get("partial_ts", [])) | ts_set)
        write_meta(meta_path, meta)
        written += 1
    print(f"meta files updated: {written} (backups in {backup_root})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--root", type=Path, required=True, help="ftcache persistence root")
    ap.add_argument("--exchange", default="hyperliquid")
    ap.add_argument("--mode", default="futures", help="trading mode directory")
    ap.add_argument("--since", default="2026-04-01", help="Check candles from this date (UTC)")
    ap.add_argument("--tol", type=float, default=0.01)
    ap.add_argument("--csv", type=Path)
    ap.add_argument("--apply", action="store_true", help="Write partial_ts into the meta files")
    ap.add_argument("--backup-dir", type=Path, help="Required with --apply (meta files copied)")
    ap.add_argument("--force", action="store_true", help="--apply even if a daemon process is seen")
    args = ap.parse_args(argv)

    if args.apply and not args.backup_dir:
        ap.error("--apply requires --backup-dir")
    if args.apply and not args.force:
        running = daemon_running()
        if running:
            print("Refusing --apply: the ftcache daemon is running. Stop it first:")
            print("\n".join(running[:5]))
            return 2

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

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"root={args.root} exchange={args.exchange}/{args.mode} since={args.since} mode={mode}")
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

    if not args.apply:
        print("dry-run: nothing written (stop the daemon, then --apply --backup-dir ...)")
        return 0

    apply_flags(args, flagged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
