#!/usr/bin/env python3
"""Fleet monitor: scans HL bot screens for RECENT 429/rate-limit errors and
reconciles live bot DB open positions against the real (netted) exchange
positions to spot orphans. Read-only. Safe to run repeatedly. Not for commit.

Live-bot discovery is PROCESS-DRIVEN (enumerates running freqtrade 'trade'
processes and reads their merged config), so live bots whose config filename
carries a cosmetic '_dry' suffix are still counted correctly."""
import glob
import json, os, re, subprocess, sqlite3, time, tempfile, datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

RECENT_MIN = int(os.environ.get("MON_RECENT_MIN", "60"))  # 429 window (minutes)


def _read_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().split(b"\x00")
    except Exception:
        return []


def _merge_config(path, _seen=None):
    """Load a freqtrade config, resolving add_config_files like freqtrade does."""
    if _seen is None:
        _seen = set()
    ap = os.path.abspath(path)
    if ap in _seen or not os.path.exists(ap):
        return {}
    _seen.add(ap)
    try:
        d = json.load(open(ap))
    except Exception:
        return {}
    merged = {}
    for inc in d.get("add_config_files", []):
        ip = inc if os.path.isabs(inc) else os.path.join(os.path.dirname(ap), inc)
        merged.update(_merge_config(ip, _seen))
    merged.update(d)
    return merged


def live_bots():
    """Enumerate running freqtrade 'trade' processes -> {bot_name: (db_path, dry)}.

    Returns only dry_run=False bots (the ones on the shared live wallet)."""
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        argv = [a.decode(errors="replace") for a in _read_cmdline(pid) if a]
        if not argv:
            continue
        joined = " ".join(argv)
        if "freqtrade" not in joined or "trade" not in argv:
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
        name = conf.get("bot_name") or os.path.basename(cfg)
        out[name] = url[len("sqlite:///"):]
    return out


def _screen_sessions():
    ls = subprocess.run(["screen", "-ls"], capture_output=True, text=True).stdout
    return re.findall(r"\d+\.(HL-[^\s]+)", ls)


_TS = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_RL = re.compile(r"429|RateLimitExceeded|Too Many Requests", re.I)


def scan_429_recent():
    """Per live screen, count 429 lines whose timestamp is within RECENT_MIN.
    Lines without a parseable timestamp are ignored (avoids counting stale
    scrollback that survives restarts)."""
    cutoff = dt.datetime.now() - dt.timedelta(minutes=RECENT_MIN)
    hits, total = {}, {}
    for s in _screen_sessions():
        if s.startswith("HL-dry"):
            continue
        tf = tempfile.mktemp(suffix=".txt")
        subprocess.run(["screen", "-S", s, "-X", "hardcopy", "-h", tf])
        time.sleep(0.15)
        try:
            txt = open(tf, errors="replace").read()
        except Exception:
            txt = ""
        finally:
            try:
                os.unlink(tf)
            except Exception:
                pass
        recent = tot = 0
        for line in txt.splitlines():
            if not _RL.search(line):
                continue
            tot += 1
            m = _TS.search(line)
            if not m:
                continue
            try:
                t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if t >= cutoff:
                recent += 1
        if tot:
            total[s] = tot
        if recent:
            hits[s] = recent
    return hits, total


def db_net_positions(dbmap):
    """coin -> signed net amount summed across all live bot DBs (short = -)."""
    net, detail = {}, {}
    for name, path in dbmap.items():
        if not os.path.exists(path):
            continue
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True).cursor()
            c.execute("SELECT pair, amount, is_short FROM trades WHERE is_open=1")
            rows = c.fetchall()
        except Exception:
            continue
        for pair, amount, is_short in rows:
            coin = pair.split("/")[0]
            signed = -amount if is_short else amount
            net[coin] = net.get(coin, 0.0) + signed
            detail.setdefault(coin, []).append((name, round(signed, 4)))
    return net, detail


def exchange_net_positions():
    """coin -> signed net contracts on the shared wallet (one call, backoff)."""
    import ccxt
    acc = json.load(open("live_configs/_hyperliquid_freqtrade_access.json"))
    ex = acc.get("exchange", {})
    wallet = ex.get("walletAddress") or ex.get("wallet_address")
    sec = ex.get("secret") or ex.get("privateKey")
    cli = ccxt.hyperliquid({"walletAddress": wallet, "privateKey": sec, "enableRateLimit": True})
    # HIP-3 builder dexes (e.g. "xyz") hold positions invisible to the plain
    # fetch_positions call — include every dex configured by a live bot, or
    # builder-dex trades read as absent and get falsely reported as drift.
    dexes = set()
    try:
        for cfgp in glob.glob("live_configs/*.json"):
            if os.path.basename(cfgp).startswith("_"):
                continue
            try:
                conf = _merge_config(cfgp)
            except Exception:
                continue
            if conf.get("dry_run") is False:
                dexes.update(conf.get("exchange", {}).get("hip3_dexes", []) or [])
    except Exception:
        pass
    last = None
    for attempt in range(4):
        try:
            poss = list(cli.fetch_positions())
            for dex in sorted(dexes):
                poss.extend(cli.fetch_positions(None, params={"dex": dex}))
            out = {}
            for p in poss:
                sym = p.get("symbol", "")
                coin = sym.split("/")[0]
                contracts = p.get("contracts") or 0
                side = p.get("side")
                signed = -contracts if side == "short" else contracts
                if contracts:
                    out[coin] = out.get(coin, 0.0) + signed
            return out, None
        except Exception as e:
            last = repr(e)
            time.sleep(5 * (attempt + 1))
    return None, last


def main():
    print("=" * 64)
    print("FLEET MONITOR", time.strftime("%Y-%m-%d %H:%M:%S"),
          f"(429 window: {RECENT_MIN}min)")
    print("=" * 64)

    hits, total = scan_429_recent()
    print(f"\n[429 / rate-limit -- RECENT (<{RECENT_MIN}min)]")
    if not hits:
        print("  none recent")
    else:
        for s, n in sorted(hits.items(), key=lambda x: -x[1]):
            print(f"  {s:44} {n:4}  (scrollback total {total.get(s, 0)})")

    dbmap = live_bots()
    dbnet, detail = db_net_positions(dbmap)
    exnet, err = exchange_net_positions()
    print(f"\n[live bots discovered: {len(dbmap)}]")

    print("\n[orphan reconciliation: DB net vs exchange net]")
    if err:
        print("  exchange fetch FAILED:", err)
        print("  (DB open coins:", sorted(k for k, v in dbnet.items() if abs(v) > 1e-9), ")")
        return
    coins = sorted(set(dbnet) | set(exnet))
    flagged = 0
    for coin in coins:
        d = dbnet.get(coin, 0.0)
        e = exnet.get(coin, 0.0)
        tol = max(abs(d), abs(e)) * 0.02 + 1e-6
        if abs(d - e) > tol:
            flagged += 1
            who = detail.get(coin, [])
            kind = "ORPHAN?" if not who else "netting"
            print(f"  MISMATCH {coin:8} db_net={d:+.4f} exch_net={e:+.4f} "
                  f"[{kind}] bots={who}")
    if not flagged:
        print(f"  clean ({len(coins)} coins reconciled)")
    else:
        print(f"  {flagged} coin(s) flagged -> ORPHAN? = on exchange, no live bot")


if __name__ == "__main__":
    main()
