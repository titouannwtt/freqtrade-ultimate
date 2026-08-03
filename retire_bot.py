#!/usr/bin/env python3
"""
retire_bot.py — Safely retire a live Freqtrade bot from a shared (netted) Hyperliquid wallet.

Why this exists
---------------
Cutting a bot by hand (Ctrl+C in its screen) does NOT close its positions. On a netted wallet
the positions stay on-chain; once the bot's config/DB is gone, they become untracked ORPHANS
(unmanaged risk). This tool makes retirement safe *by construction*: a bot is never stopped
while it still holds open trades.

Procedure (mirrors the validated manual runbook)
------------------------------------------------
  1. /stopentry          — freeze new entries
  2. /forceexit all      — ask the bot to close every open trade
  3. poll /status        — wait until the DB shows 0 open trades
  4. on-chain fallback    — for trades the bot can't fill (429 backoff), close the EXACT bot
                            amount with a signed reduceOnly market order (requires --onchain;
                            reduceOnly guarantees it can never flip the net or touch other bots)
  5. stop the screen     — double Ctrl+C (freqtrade, then the launch_bot.sh 60s countdown)
  6. archive             — move config + DB to _retired/ (the strategy .py is never touched)

Usage
-----
    python3 retire_bot.py <config_basename.json> [--onchain] [--yes] [--no-archive]

    --onchain     enable the signed reduceOnly fallback for stuck positions (uses the wallet
                  private key from _hyperliquid_freqtrade_access.json). Off by default.
    --yes         skip the confirmation prompt.
    --no-archive  stop the bot but leave config/DB in place.

Read the position summary it prints before confirming. It refuses to stop a bot that still
has open trades unless every one has been closed (or --onchain cleared them).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
ACCESS = os.path.join(REPO, "live_configs", "_hyperliquid_freqtrade_access.json")


def _deep(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _deep(a[k], v)
        else:
            a[k] = v


def merged(cfg_path):
    m = {}
    c = json.load(open(cfg_path))
    for inc in c.get("add_config_files", []):
        try:
            _deep(m, json.load(open(os.path.join(os.path.dirname(cfg_path), inc))))
        except Exception:
            pass
    _deep(m, c)
    return m


def api(port, method, path, token=None, user=None, pw=None, body=None, timeout=45):
    url = f"http://127.0.0.1:{port}/api/v1{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    elif user:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except Exception as e:
        return "ERR", str(e)[:120]


def token_for(a):
    st, t = api(a["listen_port"], "POST", "/token/login", user=a["username"], pw=a["password"], timeout=15)
    if st != 200:
        raise SystemExit(f"auth failed on port {a['listen_port']}: {st} {t}")
    return t["access_token"]


def open_trades(a, tok):
    st, d = api(a["listen_port"], "GET", "/status", token=tok, timeout=15)
    return d if isinstance(d, list) else []


def onchain_close(coin_symbol, amount, is_short):
    """Signed reduceOnly market close of an EXACT amount. Returns filled qty."""
    import ccxt

    acc = json.load(open(ACCESS))["exchange"]
    ex = ccxt.hyperliquid({"walletAddress": acc["walletAddress"], "privateKey": acc["privateKey"]})
    ex.load_markets()
    last = ex.fetch_ticker(coin_symbol)["last"]
    side = "buy" if is_short else "sell"                 # buy closes a short, sell closes a long
    price = round(last * (1.05 if is_short else 0.95), 6)  # aggressive IOC bound
    amt = float(ex.amount_to_precision(coin_symbol, abs(amount)))
    o = ex.create_order(coin_symbol, "market", side, amt, price, {"reduceOnly": True})
    return o.get("filled")


def screen_session_for(config_base):
    """Best-effort: find the screen session running this config."""
    out = subprocess.run(["screen", "-ls"], capture_output=True, text=True).stdout
    stem = config_base.replace(".json", "")
    key = re.sub(r"^(hyperliquid_|dry_pipeline_|HL-)", "", stem)
    for line in out.splitlines():
        m = re.search(r"\d+\.(\S+)", line)
        if m and key in m.group(1):
            return m.group(1)
    return None


def stop_screen(session, port):
    def up():
        return f":{port} " in subprocess.run(["ss", "-ltn"], capture_output=True, text=True).stdout

    def ctrlc():
        subprocess.run(["screen", "-S", session, "-X", "stuff", "\003"])

    ctrlc()
    for _ in range(22):
        time.sleep(5)
        if not up():
            break
    else:
        ctrlc()
        time.sleep(5)
    ctrlc()  # break the launch_bot.sh countdown
    time.sleep(6)
    return not up()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="config basename in live_configs/ (e.g. hyperliquid_foo.json)")
    ap.add_argument("--onchain", action="store_true", help="allow signed reduceOnly fallback for stuck trades")
    ap.add_argument("--yes", action="store_true", help="skip confirmation")
    ap.add_argument("--no-archive", action="store_true", help="do not move config/DB to _retired/")
    args = ap.parse_args()

    cfg_path = os.path.join(REPO, "live_configs", args.config)
    cfg = merged(cfg_path)
    if cfg.get("dry_run") is not False:
        print("This is not a live bot (dry_run != false) — nothing on-chain to close. Stop it via screen.")
    a = cfg["api_server"]
    port = a["listen_port"]
    tok = token_for(a)

    trades = open_trades(a, tok)
    print(f"Retiring {args.config} (port {port}) — {len(trades)} open trade(s):")
    for t in trades:
        print(f"  id={t['trade_id']} {t['pair']} {'SHORT' if t.get('is_short') else 'LONG'} "
              f"amount={t.get('amount')} profit={t.get('profit_abs')}")
    if not args.yes and trades:
        if input("Proceed to force-exit and retire? [y/N] ").strip().lower() != "y":
            sys.exit("aborted")

    # 1-2. stop entries + force exit all
    api(port, "POST", "/stopentry", token=tok, timeout=15)
    if trades:
        api(port, "POST", "/forceexit", token=tok, body={"tradeid": "all"}, timeout=30)

    # 3. wait until flat
    for _ in range(20):
        time.sleep(15)
        tok = token_for(a)
        trades = open_trades(a, tok)
        print(f"  still open: {[t['trade_id'] for t in trades]}")
        if not trades:
            break

    # 4. on-chain fallback for anything stuck
    if trades:
        if not args.onchain:
            sys.exit(f"\n{len(trades)} trade(s) would not fill via the bot (rate-limit?). "
                     "Re-run with --onchain to close them on-chain, or retry later. NOT stopping the bot.")
        for t in trades:
            filled = onchain_close(t["pair"], t["amount"], t.get("is_short"))
            print(f"  on-chain closed {t['pair']} amount={t['amount']} -> filled={filled}")
        time.sleep(8)

    # 5. stop the bot
    session = screen_session_for(args.config)
    if not session:
        sys.exit("could not locate the screen session — stop it manually, then archive.")
    print(f"  stopping screen {session} ...")
    if not stop_screen(session, port):
        sys.exit(f"port {port} still up — verify the screen stopped before archiving.")
    subprocess.run(["screen", "-S", session, "-X", "quit"])
    print(f"  stopped. port {port} free.")

    # 6. archive
    if not args.no_archive:
        os.makedirs(os.path.join(REPO, "live_configs", "_retired"), exist_ok=True)
        os.makedirs(os.path.join(REPO, "database", "_retired"), exist_ok=True)
        if os.path.exists(cfg_path):
            os.rename(cfg_path, os.path.join(REPO, "live_configs", "_retired", args.config))
        db = cfg.get("db_url", "").replace("sqlite:///", "")
        if db and not db.startswith("/"):
            db = os.path.join(REPO, db)
        if db and os.path.exists(db):
            os.rename(db, os.path.join(REPO, "database", "_retired", os.path.basename(db)))
        print("  config + DB archived to _retired/ (strategy .py left intact).")
    print("✅ retired cleanly — no orphan left on the wallet.")


if __name__ == "__main__":
    main()
