#!/usr/bin/env python3
"""
bench_dashboard.py — measure what a dashboard page load actually costs the fleet.

Why this exists
---------------
"The dashboard is slow" is not actionable, and optimisation guided by intuition has
already failed once on this codebase: a previous pass cut polling frequencies fourfold
and changed nothing, because the bottleneck was elsewhere. So every change in this
workstream is justified by a number produced here, before and after.

What it measures
----------------
The realistic shape of the load: every bot answering the same endpoint *concurrently*,
which is what the browser does. Two figures matter and they say different things:

* **wall** — how long the fleet takes to answer at all. This is what the user feels as
  a freeze, and it is dominated by the slowest bot, not the average one.
* **p95 / max** — the tail. A median hides an endpoint that occasionally blocks for a
  minute because it went to the exchange inside the HTTP handler.

Read-only: it issues GETs only. It never writes, and it never asks a bot to trade.

Usage
-----
    python user_data/bench_dashboard.py                    # full run
    python user_data/bench_dashboard.py --endpoints status,balance
    python user_data/bench_dashboard.py --json out.json    # to diff before/after
    python user_data/bench_dashboard.py --compare before.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The set a dashboard page load actually hits, in rough order of cost.
DEFAULT_ENDPOINTS = [
    "status",
    "balance",
    "profit",
    "trades?limit=500",
    "daily",
    "performance",
    "locks",
    "count",
    "show_config",
    "stats",
]


def discover_bots() -> list[dict]:
    """Running bots with their API coordinates, resolved the way freqtrade resolves them."""
    import reconcile_positions as rp

    running = subprocess.run(["pgrep", "-af", "freqtrade"], capture_output=True, text=True).stdout
    bots = []
    for line in running.splitlines():
        if " trade " not in line:
            continue
        m = re.search(r"--config\s+(\S+\.json)", line)
        if not m:
            continue
        path = m.group(1)
        pid = line.split()[0]
        # Resolve against the *bot's* working directory, read from /proc, not this
        # script's location: the config path on a command line is relative to wherever
        # the bot was launched, and the benchmark may well be run from elsewhere (a
        # worktree, a checkout, another user's clone).
        full = path
        if not path.startswith("/"):
            try:
                cwd = Path(f"/proc/{pid}/cwd").readlink()
            except OSError:
                cwd = REPO
            full = str(Path(cwd) / path)
        if not Path(full).exists():
            continue
        try:
            cfg = rp._merged_config([full])
        except Exception as exc:
            print(f"  (config illisible, bot ignoré: {Path(full).name} — {exc})")
            continue
        api = cfg.get("api_server") or {}
        port = api.get("listen_port")
        if not port:
            continue
        bots.append(
            {
                "name": cfg.get("bot_name") or Path(path).name,
                "port": int(port),
                "user": api.get("username", ""),
                "password": api.get("password", ""),
                "dry_run": bool(cfg.get("dry_run", True)),
            }
        )
    return sorted(bots, key=lambda b: b["port"])


def fetch(bot: dict, endpoint: str, timeout: float) -> tuple[float, int, int]:
    """(elapsed_ms, bytes, http_status).

    A failure is timed too: a slow error still freezes a page just as much as a slow success.
    """
    url = f"http://127.0.0.1:{bot['port']}/api/v1/{endpoint}"
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError(f"refusing a non-local URL: {url}")
    req = urllib.request.Request(url)  # noqa: S310 — scheme checked on the line above
    if bot["user"]:
        token = b64encode(f"{bot['user']}:{bot['password']}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read()
            return (time.perf_counter() - t0) * 1000, len(body), resp.status
    except urllib.error.HTTPError as exc:
        return (time.perf_counter() - t0) * 1000, 0, exc.code
    except Exception:
        return (time.perf_counter() - t0) * 1000, 0, 0


def bench_endpoint(bots: list[dict], endpoint: str, timeout: float) -> dict:
    """Every bot, concurrently — the browser's access pattern, not a sequential loop."""
    t0 = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=min(32, len(bots) or 1)) as pool:
        futures = {pool.submit(fetch, b, endpoint, timeout): b for b in bots}
        for fut in as_completed(futures):
            results.append(fut.result())
    wall = (time.perf_counter() - t0) * 1000
    times = sorted(r[0] for r in results)
    sizes = [r[1] for r in results]
    errors = [r[2] for r in results if r[2] >= 400 or r[2] == 0]
    return {
        "endpoint": endpoint,
        "bots": len(results),
        "wall_ms": round(wall, 1),
        "sum_ms": round(sum(times), 1),
        "median_ms": round(statistics.median(times), 1) if times else 0,
        "p95_ms": round(times[int(len(times) * 0.95) - 1], 1) if times else 0,
        "max_ms": round(max(times), 1) if times else 0,
        "bytes": sum(sizes),
        "errors": len(errors),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoints", default=",".join(DEFAULT_ENDPOINTS))
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--json", help="write results here (to diff a later run against)")
    ap.add_argument("--compare", help="an earlier --json file to compare against")
    args = ap.parse_args()

    bots = discover_bots()
    if not bots:
        print("Aucun bot en marche — rien à mesurer.")
        return 1
    live = sum(1 for b in bots if not b["dry_run"])
    print(f"{len(bots)} bots interrogés ({live} live, {len(bots) - live} dry)\n")

    rows = [bench_endpoint(bots, ep, args.timeout) for ep in args.endpoints.split(",")]
    rows.sort(key=lambda r: -r["wall_ms"])

    print(
        f"{'ENDPOINT':<22}{'WALL':>9}{'SOMME':>10}{'MÉD':>9}{'P95':>10}{'MAX':>10}{'OCTETS':>12}{'ERR':>5}"
    )
    for r in rows:
        print(
            f"{r['endpoint']:<22}{r['wall_ms']:>8.0f}ms{r['sum_ms']:>9.0f}ms"
            f"{r['median_ms']:>8.0f}ms{r['p95_ms']:>9.0f}ms{r['max_ms']:>9.0f}ms"
            f"{r['bytes']:>12,}{r['errors']:>5}"
        )
    total_wall = sum(r["wall_ms"] for r in rows)
    total_bytes = sum(r["bytes"] for r in rows)
    print(f"\n  Chargement complet : {total_wall / 1000:.1f}s de mur, {total_bytes / 1e6:.1f} Mo")

    if args.compare:
        try:
            with Path(args.compare).open(encoding="utf-8") as fh:
                before = {r["endpoint"]: r for r in json.load(fh)["rows"]}
        except Exception as exc:
            print(f"  (comparaison impossible : {exc})")
            before = {}
        if before:
            print(f"\n  {'ENDPOINT':<22}{'AVANT':>10}{'APRÈS':>10}{'GAIN':>10}")
            for r in rows:
                b = before.get(r["endpoint"])
                if not b:
                    continue
                delta = r["wall_ms"] - b["wall_ms"]
                pct = (delta / b["wall_ms"] * 100) if b["wall_ms"] else 0
                print(
                    f"  {r['endpoint']:<22}{b['wall_ms']:>9.0f}ms{r['wall_ms']:>9.0f}ms{pct:>9.0f}%"
                )
            tb = sum(v["wall_ms"] for v in before.values())
            print(
                f"  {'TOTAL':<22}{tb:>9.0f}ms{total_wall:>9.0f}ms"
                f"{((total_wall - tb) / tb * 100) if tb else 0:>9.0f}%"
            )

    if args.json:
        Path(args.json).write_text(
            json.dumps({"bots": len(bots), "rows": rows}, indent=2), encoding="utf-8"
        )
        print(f"\n  écrit dans {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
