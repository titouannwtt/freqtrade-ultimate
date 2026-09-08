#!/usr/bin/env python3
"""Netting reconciler — the cleanup half of fleet position coordination.

WHY THIS EXISTS
On a shared Hyperliquid wallet, N freqtrade bots each assume 1-bot-1-wallet,
but the exchange nets positions per coin. Entry-time coordination
(fleet_coordination.py, compat/strict) blocks opposite-side opens, but across
62 independently-running processes there are unavoidable race windows
(discovery cache staleness, un-restarted bots). When two bots end up on
opposite sides of a coin, the minority/absent side becomes STUCK: its exit is a
reduce-only order that would *increase* the netted position, so the exchange
rejects it forever ("Reduce only order would increase position"). The trade rots
in the DB, spams warnings, and can feed the external_close fabrication path.

WHAT THIS DOES (safe by construction)
Once per run it reads every live bot's open trades, sums the per-coin DB slices,
and fetches the real on-chain netted position (ONE exchange call, backoff — so
it is rate-limit friendly). For each coin it looks for a TRUE PHANTOM: a single
open trade whose removal makes the collective DB sum EXACTLY match the on-chain
net, and whose own side is absent from the on-chain net. Such a trade provably
does not exist on the exchange, so deleting it (via the owning bot's API — no
exchange order, only cancels stuck reduce-only orders) can never strand a real
position. Anything that does NOT meet that exact criterion (real minority
positions, multi-trade or ambiguous discrepancies) is only REPORTED, never
auto-touched.

MULTI-ADDRESS
The fleet is not one address. Every bot is reconciled against the on-chain net of
the address in ITS OWN merged config (exchange.walletAddress), grouped by
(wallet, coin). A bot whose address cannot be resolved is skipped, never compared.
The persisted phantom state is keyed (wallet, coin, bot, trade_id).

  preview (default): python user_data/netting_reconciler.py
  apply           : python user_data/netting_reconciler.py --apply

Not a strategy/leverage change; does not touch the live bot hot path."""
import base64
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

# NO hardcoded access file. Each bot's on-chain counterpart is resolved from ITS OWN
# merged config (exchange.walletAddress), because the fleet is no longer guaranteed to
# sit on one address: a bot moved to a Hyperliquid sub-account has a net of its own, and
# comparing it to the master wallet's net makes every one of its positions read as an
# absent phantom — which is how this script would delete its live trades. Only the public
# address is read; the private key is never touched (reads use the public /info endpoint).
WALLET_KEYS = ("walletAddress", "wallet_address")

# Relative tolerance when comparing collective DB sum to the on-chain net.
REL_TOL = 0.02
ABS_TOL = 1e-6

# Persist which phantoms were seen last run, keyed (wallet, coin, bot, trade_id). A phantom is only auto-deleted when
# it was ALSO flagged on the previous run, so a transient exchange-API hiccup
# (a real position momentarily missing from fetch_positions -> looks "absent")
# can never trigger a wrongful deletion.
STATE_FILE = "user_data/.netting_reconciler_state.json"

# Never auto-delete a trade opened less than this many seconds ago (2h): freshly
# filled entries can lag in fetch_positions and must never be mistaken for phantoms.
MIN_DELETE_AGE_S = 7200


def _read_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return [a.decode(errors="replace") for a in fh.read().split(b"\x00") if a]
    except Exception:
        return []


def _deep_merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _merge_config(path, seen=None):
    if seen is None:
        seen = set()
    ap = os.path.abspath(path)
    if ap in seen or not os.path.exists(ap):
        return {}
    seen.add(ap)
    try:
        d = json.load(open(ap))
    except Exception:
        return {}
    merged = {}
    for inc in d.get("add_config_files", []):
        ip = inc if os.path.isabs(inc) else os.path.join(os.path.dirname(ap), inc)
        _deep_merge(merged, _merge_config(ip, seen))
    _deep_merge(merged, d)
    return merged


def bot_wallet(conf):
    """Public address this bot trades on, normalised, or None if undeterminable.

    None is NOT "the master wallet": a bot whose address cannot be resolved is
    excluded from reconciliation entirely (see main()). Guessing here is exactly the
    failure that deleted live trades on 2026-08-02.
    """
    ex = conf.get("exchange", {}) or {}
    for k in WALLET_KEYS:
        v = ex.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def live_bots():
    """Process-driven discovery of live (dry_run=False) bots, with their address."""
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        argv = _read_cmdline(pid)
        if not argv or "trade" not in argv or not any("freqtrade" in a for a in argv):
            continue
        cfg = None
        for i, a in enumerate(argv):
            if a in ("-c", "--config") and i + 1 < len(argv):
                cfg = argv[i + 1]
                break
        if not cfg:
            continue
        conf = _merge_config(cfg)
        if not conf or conf.get("dry_run") is True:
            continue
        url = conf.get("db_url", "")
        if not url.startswith("sqlite:///"):
            continue
        api = conf.get("api_server", {})
        out[conf.get("bot_name") or os.path.basename(cfg)] = {
            "db": url[len("sqlite:///"):],
            "port": api.get("listen_port"),
            "user": api.get("username"),
            "pw": api.get("password"),
            # HIP-3 builder dexes this bot trades (e.g. ["xyz"]). The on-chain net
            # MUST be fetched per-dex too, or every builder-dex trade reads as
            # "absent on-chain" and gets wrongly deleted as a phantom.
            "hip3": list(conf.get("exchange", {}).get("hip3_dexes", []) or []),
            # Public address only. The private key is never read or stored here.
            "wallet": bot_wallet(conf),
        }
    return out


def open_slices(db):
    """[(trade_id, coin, signed_amount, is_short, pair)] for open trades in a DB."""
    if not os.path.exists(db):
        return []
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3.0)
        cur = c.cursor()
        cur.execute("SELECT id, pair, amount, is_short, open_date FROM trades WHERE is_open=1")
        rows = cur.fetchall()
        c.close()
    except Exception:
        return []
    out = []
    for tid, pair, amt, is_short, open_date in rows:
        coin = pair.split("/")[0]
        signed = -amt if is_short else amt
        age_s = None
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(str(open_date).split("+")[0])
            age_s = (datetime.datetime.utcnow() - dt).total_seconds()
        except Exception:
            pass
        out.append((tid, coin, signed, bool(is_short), pair, age_s))
    return out


def exchange_net(wallet, hip3_dexes=()):
    """Signed on-chain net per coin for ONE address: MAIN dex + its HIP-3 dexes.

    HIP-3 positions live on separate dexes and are ONLY returned when
    fetch_positions is called with params={"dex": <name>}. Being blind to them
    made every booked builder-dex trade (e.g. XYZ-KR200) look like a phantom,
    so this script deleted them from the bots' DBs 30-60min after each fill and
    stranded the real position on-chain (70 orphaned fills in 2 days).
    If ANY fetch (main or dex) fails, we return an error and take NO action *for
    that address* — the other addresses are still reconciled independently.

    The client is address-only: fetch_positions on Hyperliquid is the public /info
    endpoint, so no private key is needed and none is read.
    """
    import ccxt

    cli = ccxt.hyperliquid({"walletAddress": wallet, "enableRateLimit": True})
    last = None
    for attempt in range(4):
        try:
            out = {}
            batches = [cli.fetch_positions()]
            for dex in sorted(set(hip3_dexes)):
                batches.append(cli.fetch_positions(None, params={"dex": dex}))
            for batch in batches:
                for p in batch:
                    coin = p.get("symbol", "").split("/")[0]
                    contracts = p.get("contracts") or 0
                    if contracts:
                        out[coin] = out.get(coin, 0.0) + (
                            -contracts if p.get("side") == "short" else contracts
                        )
            return out, None
        except Exception as e:
            last = repr(e)[:120]
            time.sleep(5 * (attempt + 1))
    return None, last


def _api(port, user, pw):
    base = f"http://127.0.0.1:{port}/api/v1"
    tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req = urllib.request.Request(
        base + "/token/login", headers={"Authorization": f"Basic {tok}"}, data=b""
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        bearer = json.loads(r.read().decode())["access_token"]
    return base, bearer


def delete_trade(bot, tid):
    base, bearer = _api(bot["port"], bot["user"], bot["pw"])
    req = urllib.request.Request(
        base + f"/trades/{tid}", headers={"Authorization": f"Bearer {bearer}"}, method="DELETE"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return True, json.loads(r.read().decode()).get("result_msg", "ok")
    except urllib.error.HTTPError as e:
        # The bot may be mid-cycle; the delete often lands server-side anyway.
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, repr(e)[:80]


def _match(a, b):
    return abs(a - b) <= max(abs(a), abs(b)) * REL_TOL + ABS_TOL


def collect_slices(bots):
    """Group every live bot's open trades by (wallet, coin).

    Returns ``(per_key, skipped)``. ``skipped`` lists the bots whose address could
    not be resolved: their trades are NOT reconciled against anything. Silence beats
    a wrong deletion.
    """
    per_key = {}
    skipped = []
    for name, b in bots.items():
        wallet = b.get("wallet")
        if not wallet:
            skipped.append(name)
            continue
        for tid, coin, signed, is_short, pair, age_s in open_slices(b["db"]):
            per_key.setdefault((wallet, coin), []).append(
                (name, tid, signed, is_short, pair, age_s)
            )
    return per_key, skipped


def wallet_dexes(bots):
    """{wallet: sorted HIP-3 dexes used by the bots on THAT wallet}."""
    out = {}
    for b in bots.values():
        w = b.get("wallet")
        if not w:
            continue
        out.setdefault(w, set()).update(b.get("hip3", []))
    return {w: sorted(d) for w, d in out.items()}


def fetch_nets(bots, fetcher=exchange_net):
    """{wallet: coin->net} for every distinct address, plus {wallet: error}.

    One fetch per distinct address. A failure isolates that address only.
    """
    nets, errors = {}, {}
    for wallet, dexes in sorted(wallet_dexes(bots).items()):
        net, err = fetcher(wallet, dexes)
        if err:
            errors[wallet] = err
        else:
            nets[wallet] = net
    return nets, errors


def analyze(per_key, nets):
    """Classify every (wallet, coin) against the net of THAT wallet.

    A wallet absent from ``nets`` (fetch failed) is skipped entirely: no phantom, no
    minority, no action.
    """
    auto, minority, ambiguous = [], [], []
    for (wallet, coin), slices in sorted(per_key.items()):
        if wallet not in nets:
            continue  # address unknown / fetch failed -> never act
        db_sum = sum(s[2] for s in slices)
        onchain = nets[wallet].get(coin, 0.0)
        if _match(db_sum, onchain):
            # Collective DBs agree with reality. A minority-side slice here is a
            # REAL netted position (removing it would create an orphan).
            for name, tid, signed, is_short, pair, _age in slices:
                if onchain != 0 and (signed > 0) != (onchain > 0) and abs(signed) > ABS_TOL:
                    minority.append((wallet, coin, name, tid, signed, onchain, pair))
            continue
        # Discrepancy: look for exactly one trade whose removal makes DB == on-chain
        # AND whose own side is absent from the on-chain net -> provable phantom.
        candidates = []
        for name, tid, signed, is_short, pair, age_s in slices:
            # Never touch a freshly-opened trade: a just-filled entry may not be
            # reflected in fetch_positions yet (exchange/API lag), and a wrongful
            # delete here is exactly how real positions get orphaned.
            if age_s is None or age_s < MIN_DELETE_AGE_S:
                continue
            side_absent = onchain == 0 or (signed > 0) != (onchain > 0)
            if side_absent and _match(db_sum - signed, onchain):
                candidates.append((name, tid, signed, is_short, pair))
        if len(candidates) == 1:
            name, tid, signed, is_short, pair = candidates[0]
            auto.append((wallet, coin, name, tid, signed, onchain, pair))
        else:
            ambiguous.append((wallet, coin, db_sum, onchain, slices))
    return auto, minority, ambiguous


def _short(wallet):
    return f"{wallet[:6]}…{wallet[-4:]}" if wallet and len(wallet) > 12 else str(wallet)


def main():
    apply = "--apply" in sys.argv
    print("=" * 68)
    print("NETTING RECONCILER", time.strftime("%Y-%m-%d %H:%M:%S"),
          "APPLY" if apply else "PREVIEW")
    print("=" * 68)

    bots = live_bots()
    per_key, skipped = collect_slices(bots)
    dexes = wallet_dexes(bots)
    wallets = sorted(dexes)
    print(f"live bots: {len(bots)}   addresses: {len(wallets)}   "
          f"(wallet,coin) pairs with open trades: {len(per_key)}")
    for w in wallets:
        holders = sorted(n for n, b in bots.items() if b.get("wallet") == w)
        print(f"  {_short(w)}  bots={len(holders)}  hip3={dexes[w] or 'none'}")
    if skipped:
        print(f"  SKIPPED (no resolvable exchange.walletAddress, never reconciled): "
              f"{', '.join(sorted(skipped))}")

    nets, errors = fetch_nets(bots)
    for w, err in errors.items():
        print(f"\nEXCHANGE FETCH FAILED for {_short(w)} ({err}) — no action for this address")
    if not nets:
        print("\nNo address could be read — read-only, no action taken")
        return

    try:
        prev_seen = set(tuple(x) for x in json.load(open(STATE_FILE)))
    except Exception:
        prev_seen = set()

    auto, minority, ambiguous = analyze(per_key, nets)

    cur_seen = [[wallet, coin, name, tid] for wallet, coin, name, tid, *_ in auto]
    print(f"\n[AUTO-DELETABLE PHANTOMS: {len(auto)}]  "
          "(single trade, absent on-chain for ITS address, removal makes DB match)")
    for wallet, coin, name, tid, signed, onchain, pair in auto:
        confirmed = (wallet, coin, name, tid) in prev_seen
        tag = "confirmed 2x" if confirmed else "seen 1x (waits for next run)"
        print(f"  {_short(wallet)} {coin:8} {name:38} #{tid} slice={signed:+.4f} "
              f"on-chain={onchain:+.4f}  {pair}  [{tag}]")
        if apply and confirmed:
            ok, msg = delete_trade(bots[name], tid)
            print(f"      -> {'DELETED' if ok else 'DELETE (verify)'}: {msg}")

    try:
        json.dump(cur_seen, open(STATE_FILE, "w"))
    except Exception:
        pass

    print(f"\n[REAL MINORITY POSITIONS: {len(minority)}]  (netted, NOT deleted — manual call)")
    for wallet, coin, name, tid, signed, onchain, pair in minority:
        print(f"  {_short(wallet)} {coin:8} {name:38} #{tid} slice={signed:+.4f} "
              f"on-chain={onchain:+.4f}  {pair}")

    print(f"\n[AMBIGUOUS DISCREPANCIES: {len(ambiguous)}]  (multi-trade — review manually)")
    for wallet, coin, db_sum, onchain, slices in ambiguous:
        print(f"  {_short(wallet)} {coin:8} db_sum={db_sum:+.4f} on-chain={onchain:+.4f}")
        for name, tid, signed, is_short, pair, _age in slices:
            print(f"      {name:38} #{tid} {signed:+.4f}")

    if apply and auto:
        print("\n(re-run in preview to confirm phantoms cleared)")


if __name__ == "__main__":
    main()
