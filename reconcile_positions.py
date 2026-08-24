#!/usr/bin/env python3
"""
reconcile_positions.py — Fleet position reconciliation for a shared (netted) Hyperliquid wallet.

Root problem this guards against
--------------------------------
On Hyperliquid the wallet is *netted*: every bot's position on a coin is merged into a
single net position on-chain. When a live bot is stopped/killed (or its DB deleted) WITHOUT
first closing its trades, the position stays on the wallet but no bot tracks it anymore — an
**orphan**. Orphans accumulate silently and become unmanaged risk (they ride until liquidation
or a manual close).

What this does
--------------
For every coin, compares:
  - HL_net   : the net signed position on the wallet (long +, short -)   [via ccxt]
  - bots_net : the signed sum of OPEN trades across all currently-running LIVE bots [via their DBs]
and flags:
  - ORPHAN  : |HL_net| > |bots_net|  → position on the wallet no live bot backs
  - PHANTOM : |bots_net| > |HL_net|  → a bot's DB claims more than exists on-chain (stale trade)

Exit code is non-zero when any drift is found, so it can be cron'd and piped to an alert.
Read-only: it never places or cancels an order.

Usage
-----
    python3 reconcile_positions.py                # human report
    python3 reconcile_positions.py --json         # machine output
    python3 reconcile_positions.py --telegram     # also push a Telegram alert on drift
    python3 reconcile_positions.py --tolerance 0.02
    python3 reconcile_positions.py --dust-usd 25   # coarser dust floor, in stake currency

Requires the wallet read creds in live_configs/_hyperliquid_freqtrade_access.json
(only walletAddress is needed for reads; the private key is never used here).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import time
import subprocess
import sys
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
ACCESS = os.path.join(REPO, "live_configs", "_hyperliquid_freqtrade_access.json")

# Drift below this notional is dust, not a position: Hyperliquid refuses orders under
# 10 USDC, so no bot can have opened anything smaller. Used as the absolute floor of the
# match tolerance, converted to coin units at the mark price of each coin.
DUST_USD = 10.0


def _deep(a: dict, b: dict) -> None:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _deep(a[k], v)
        else:
            a[k] = v


def _merged_config(paths: list[str]) -> dict:
    """Resolve a bot config exactly like freqtrade (main file overrides its add_config_files)."""
    m: dict = {}
    for p in paths:
        if not os.path.isabs(p):
            p = os.path.join(REPO, p)
        try:
            c = json.load(open(p))
        except Exception:
            continue
        for inc in c.get("add_config_files", []):
            try:
                _deep(m, json.load(open(os.path.join(os.path.dirname(p), inc))))
            except Exception:
                pass
        _deep(m, c)
    return m


def _running_bot_configs() -> list[dict]:
    """Every freqtrade process currently listening → its merged config."""
    out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
    cfgs = []
    for pid in set(re.findall(r'"freqtrade",pid=(\d+)', out)):
        try:
            raw = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\x00", b" ").decode()
        except Exception:
            continue
        paths = re.findall(r"(\S+\.json)", raw)
        if paths:
            cfgs.append(_merged_config(paths))
    return cfgs


def _live_bot_positions() -> tuple[dict[str, float], dict[str, list[str]], int]:
    """Signed open-trade sum per coin across running LIVE bots (dry_run is False)."""
    net: dict[str, float] = {}
    detail: dict[str, list[str]] = {}
    n_live = 0
    for cfg in _running_bot_configs():
        if cfg.get("dry_run") is not False:  # keep only genuinely-live bots
            continue
        n_live += 1
        db = cfg.get("db_url", "").replace("sqlite:///", "")
        if db and not db.startswith("/"):
            db = os.path.join(REPO, db)
        name = cfg.get("bot_name") or os.path.basename(db or "?")
        if not db or not os.path.exists(db):
            continue
        try:
            rows = (
                sqlite3.connect(db)
                .execute("select pair, amount, is_short from trades where is_open = 1")
                .fetchall()
            )
        except Exception:
            continue
        for pair, amount, is_short in rows:
            coin = pair.split("/")[0]
            signed = -abs(amount) if is_short else abs(amount)
            net[coin] = net.get(coin, 0.0) + signed
            detail.setdefault(coin, []).append(f"{name}:{'S' if is_short else 'L'}{abs(amount)}")
    return net, detail, n_live


def _hip3_dexes() -> set:
    """Union of HIP-3 builder dexes configured across running live bots."""
    dexes: set = set()
    for cfg in _running_bot_configs():
        if cfg.get("dry_run") is False:
            dexes.update(cfg.get("exchange", {}).get("hip3_dexes", []) or [])
    return dexes


def _hl_positions() -> tuple[dict[str, float], dict[str, float]]:
    """Signed net position per coin, plus its mark price — MAIN dex + HIP-3 builder dexes.

    Builder-dex positions (e.g. XYZ-KR200 on the "xyz" dex) are only returned by
    fetch_positions when called with params={"dex": <name>}; omitting them makes
    every builder-dex trade look like a phantom and its real position an orphan.

    The mark price is returned alongside so the drift tolerance can be expressed in
    stake currency rather than in coin units — see `reconcile()`.
    """
    import ccxt  # imported lazily so the module loads even without ccxt for --help

    acc = json.load(open(ACCESS))["exchange"]
    ex = ccxt.hyperliquid({"walletAddress": acc["walletAddress"], "privateKey": acc["privateKey"]})
    net: dict[str, float] = {}
    px: dict[str, float] = {}
    batches = [ex.fetch_positions()]
    for dex in sorted(_hip3_dexes()):
        batches.append(ex.fetch_positions(None, params={"dex": dex}))
    for batch in batches:
        for p in batch:
            if not p.get("contracts"):
                continue
            coin = p["symbol"].split("/")[0]
            signed = abs(float(p["contracts"]))
            net[coin] = net.get(coin, 0.0) + (signed if p["side"] == "long" else -signed)
            mark = p.get("markPrice") or p.get("entryPrice")
            if mark:
                px[coin] = float(mark)
            elif p.get("notional") and signed:
                px[coin] = abs(float(p["notional"])) / signed
    return net, px


def _push(token: str | None, chat: str | None, msg: str) -> bool:
    """Send one Telegram message. Returns True on success, False on any failure."""
    if not token or not chat:
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10
        )
        return True
    except Exception as e:  # noqa: BLE001 - best-effort notifier, never break the report
        print(f"[telegram] push failed: {e.__class__.__name__}: {e}", file=sys.stderr)
        return False


def _telegram(msg: str) -> None:
    """Best-effort Telegram push, env vars first then bot configs.

    Env first because under cron there is no shell profile, and the bot configs on this
    fleet carry no telegram block at all — a config-only lookup pushed nothing, silently.
    The cron entry sources EDGE_TG_* from ~/.zshrc, as exposure_guardrail.py does.
    """
    if _push(os.environ.get("EDGE_TG_BOT_TOKEN"), os.environ.get("EDGE_TG_CHAT_ID"), msg):
        return
    for cfg in _running_bot_configs():
        tg = cfg.get("telegram", {})
        if _push(tg.get("token"), tg.get("chat_id"), msg):
            return


CONFIRM_DELAY_S = 6.0


def reconcile(tolerance: float = 0.01, dust_usd: float = DUST_USD, confirm: bool = True):
    """Compare wallet vs bot books, confirming any break with a second read.

    The two sides are sampled at different instants and from different systems: the bot
    books come from live SQLite files that their owners are actively writing, the wallet
    from a network call. A trade committed between the two reads therefore shows up as a
    perfect phantom or orphan that never existed. Observed 2026-08-20: a 367-unit SEI
    "orphan" reported while the bot holding it was mid-INSERT; gone on the next run.

    A false alarm is worse here than a missed one. This report exists to be trusted at a
    glance, and an operator who has learned that breaks evaporate on re-run has learned
    to ignore the panel — which is exactly how a real orphan rides to liquidation
    unnoticed. So a break is only reported when it survives a fresh read of BOTH sides,
    a few seconds later: a genuine drift persists, a race does not.

    ``confirm=False`` skips the second pass (used by callers that already hold a lock,
    and by the tests).
    """
    res = _reconcile_once(tolerance, dust_usd)
    if not confirm or not (res["orphans"] or res["phantoms"]):
        return res

    suspect = {c for c, _ in res["orphans"]} | {c for c, _ in res["phantoms"]}
    time.sleep(CONFIRM_DELAY_S)
    again = _reconcile_once(tolerance, dust_usd)
    still = {c for c, _ in again["orphans"]} | {c for c, _ in again["phantoms"]}

    transient = suspect - still
    if transient:
        logger_msg = (
            f"  (ignored {len(transient)} transient break(s) that did not survive "
            f"re-reading: {', '.join(sorted(transient))} — concurrent bot writes)"
        )
        again["transient"] = sorted(transient)
        again["note"] = logger_msg
    return again


def _reconcile_once(tolerance: float = 0.01, dust_usd: float = DUST_USD):
    bots_net, detail, n_live = _live_bot_positions()
    hl, px = _hl_positions()
    coins = sorted(set(hl) | {c for c, v in bots_net.items() if abs(v) > 1e-6})
    rows, orphans, phantoms, ok = [], [], [], 0
    for c in coins:
        h, b = hl.get(c, 0.0), bots_net.get(c, 0.0)
        diff = h - b
        # Absolute floor expressed in stake currency, not in coin units. A flat 0.5-unit
        # floor is sane on BOME (230k units) and catastrophic on XYZ-JP225 (~66k USDC a
        # unit), where it would wave through a 33k USDC orphan as "ISO". Anchored on the
        # venue's minimum order size: below it, no bot could have opened the position.
        unit = px.get(c)
        floor = (dust_usd / unit) if unit else 0.5
        tol = max(abs(h), abs(b)) * tolerance + floor
        if abs(diff) <= tol:
            status = "ISO"
            ok += 1
        elif abs(h) > abs(b):
            status = "ORPHAN"
            orphans.append((c, diff))
        else:
            status = "PHANTOM"
            phantoms.append((c, diff))
        rows.append({"coin": c, "hl": h, "bots": b, "diff": diff, "status": status})
    return {
        "n_live_bots": n_live,
        "rows": rows,
        "orphans": orphans,
        "phantoms": phantoms,
        "ok": ok,
        "total": len(coins),
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--telegram", action="store_true", help="push a Telegram alert if drift is found"
    )
    ap.add_argument(
        "--tolerance", type=float, default=0.01, help="relative match tolerance (default 1%%)"
    )
    ap.add_argument(
        "--dust-usd",
        type=float,
        default=DUST_USD,
        help=f"drift below this notional is dust, not a position (default {DUST_USD})",
    )
    args = ap.parse_args()

    r = reconcile(args.tolerance, args.dust_usd)
    drift = bool(r["orphans"] or r["phantoms"])

    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print(
            f"Live bots: {r['n_live_bots']}  |  coins: {r['total']}  |  ISO: {r['ok']}  "
            f"|  orphans: {len(r['orphans'])}  |  phantoms: {len(r['phantoms'])}\n"
        )
        print(f"{'COIN':<10}{'HL_net':>14}{'BOTS_net':>14}{'DIFF':>12}   status")
        for row in r["rows"]:
            if row["status"] != "ISO":
                print(
                    f"{row['coin']:<10}{row['hl']:>14.3f}{row['bots']:>14.3f}{row['diff']:>12.3f}   {row['status']}"
                )
        if r.get("note"):
            print(r["note"])
        if r["orphans"]:
            print("\n⚠️  ORPHANS (position on the wallet no live bot backs):")
            for c, d in r["orphans"]:
                print(f"    {c}: uncovered net = {d:.3f}")
        if r["phantoms"]:
            print("\n⚠️  PHANTOMS (a bot claims more than exists on-chain):")
            for c, d in r["phantoms"]:
                print(f"    {c}: bot excess = {-d:.3f}")
        if not drift:
            print("\n✅ Fully ISO — every HL position is backed by a live bot.")

    if drift and args.telegram:
        _telegram(
            "⚠️ Position drift detected on the HL wallet\n"
            f"orphans: {', '.join(c for c, _ in r['orphans']) or '—'}\n"
            f"phantoms: {', '.join(c for c, _ in r['phantoms']) or '—'}\n"
            "Run reconcile_positions.py for detail."
        )

    sys.exit(1 if drift else 0)


if __name__ == "__main__":
    main()
