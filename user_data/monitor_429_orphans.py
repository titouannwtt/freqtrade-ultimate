#!/usr/bin/env python3
"""Fleet monitor: scans HL bot screens for RECENT 429/rate-limit errors and
reconciles live bot DB open positions against the real (netted) exchange
positions to spot orphans. Read-only. Safe to run repeatedly. Not for commit.

Live-bot discovery is PROCESS-DRIVEN (enumerates running freqtrade 'trade'
processes and reads their merged config), so live bots whose config filename
carries a cosmetic '_dry' suffix are still counted correctly.

Multi-address: each bot is reconciled against the on-chain net of the address in its
OWN config (exchange.walletAddress). A bot on a Hyperliquid sub-account therefore no
longer shows every one of its positions as an ORPHAN?. A bot whose address cannot be
resolved is skipped, never flagged. Reads are address-only (public /info)."""
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


def bot_wallet(conf):
    """Normalised public address of a bot, or None when undeterminable."""
    ex = conf.get("exchange", {}) or {}
    for k in ("walletAddress", "wallet_address"):
        v = ex.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def live_bots():
    """Enumerate running freqtrade 'trade' processes -> {bot_name: {db, wallet, hip3}}.

    Returns only dry_run=False bots."""
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
        out[name] = {
            "db": url[len("sqlite:///"):],
            "wallet": bot_wallet(conf),
            "hip3": list(conf.get("exchange", {}).get("hip3_dexes", []) or []),
        }
    return out


def _short(wallet):
    return f"{wallet[:6]}…{wallet[-4:]}" if wallet and len(wallet) > 12 else str(wallet)


def _screen_sessions():
    ls = subprocess.run(["screen", "-ls"], capture_output=True, text=True).stdout
    return re.findall(r"\d+\.(HL-[^\s]+)", ls)


_TS = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_RL = re.compile(r"429|RateLimitExceeded|Too Many Requests", re.I)


def scan_429_recent():
    """Per live screen, count 429 lines whose timestamp is within RECENT_MIN.
    Lines without a parseable timestamp are ignored (avoids counting stale
    scrollback that survives restarts)."""
    # Bot log timestamps are UTC (freqtrade/loggers pins Formatter.converter to
    # gmtime), while this script may run from cron under the server's local
    # timezone -- Europe/Paris since 2026-09-08. Comparing a local "now" against a
    # UTC log stamp would put the cutoff 2h ahead of every line and silently report
    # zero recent 429s forever. Build the cutoff in UTC, naive, to match the stamps.
    cutoff = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(minutes=RECENT_MIN)
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
    """(wallet, coin) -> signed net across live bot DBs. Returns (net, detail, skipped)."""
    net, detail, skipped = {}, {}, []
    for name, b in dbmap.items():
        path, wallet = b["db"], b["wallet"]
        if not wallet:
            skipped.append(name)
            continue
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
            key = (wallet, coin)
            net[key] = net.get(key, 0.0) + signed
            detail.setdefault(key, []).append((name, round(signed, 4)))
    return net, detail, sorted(set(skipped))


def wallet_dexes(dbmap):
    """{wallet: sorted HIP-3 dexes} for the running live bots, per address.

    HIP-3 builder dexes (e.g. "xyz") hold positions invisible to the plain
    fetch_positions call — a dex must be queried on the address that uses it, or
    builder-dex trades read as absent and get falsely reported as drift.
    """
    out = {}
    for b in dbmap.values():
        if not b["wallet"]:
            continue
        out.setdefault(b["wallet"], set()).update(b.get("hip3", []))
    return {w: sorted(d) for w, d in out.items()}


def exchange_net_positions(dbmap):
    """(wallet, coin) -> signed net contracts, one address-only client per address.

    Returns ``(nets, errors)``; a failed address is reported and simply not compared.
    """
    import ccxt
    nets, errors = {}, {}
    for wallet, dexes in sorted(wallet_dexes(dbmap).items()):
        # Address-only: fetch_positions is the public /info endpoint (no key read).
        cli = ccxt.hyperliquid({"walletAddress": wallet, "enableRateLimit": True})
        last = None
        for attempt in range(4):
            try:
                poss = list(cli.fetch_positions())
                for dex in dexes:
                    poss.extend(cli.fetch_positions(None, params={"dex": dex}))
                for p in poss:
                    sym = p.get("symbol", "")
                    coin = sym.split("/")[0]
                    contracts = p.get("contracts") or 0
                    side = p.get("side")
                    signed = -contracts if side == "short" else contracts
                    if contracts:
                        key = (wallet, coin)
                        nets[key] = nets.get(key, 0.0) + signed
                last = None
                break
            except Exception as e:
                last = repr(e)
                time.sleep(5 * (attempt + 1))
        if last:
            errors[wallet] = last
    return nets, errors


def main():
    print("=" * 64)
    print("FLEET MONITOR", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC",
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
    dbnet, detail, skipped = db_net_positions(dbmap)
    exnet, errors = exchange_net_positions(dbmap)
    addrs = sorted(wallet_dexes(dbmap))
    print(f"\n[live bots discovered: {len(dbmap)}  addresses: {len(addrs)}]")
    for w in addrs:
        holders = sorted(n for n, b in dbmap.items() if b["wallet"] == w)
        print(f"  {_short(w)}  bots={len(holders)}")
    if skipped:
        print("  SKIPPED (no resolvable exchange.walletAddress): " + ", ".join(skipped))

    print("\n[orphan reconciliation: DB net vs exchange net, per address]")
    for w, err in errors.items():
        print(f"  exchange fetch FAILED for {_short(w)}: {err} — address not compared")
    readable = {w for w in addrs if w not in errors}
    keys = sorted(k for k in (set(dbnet) | set(exnet)) if k[0] in readable)
    flagged = 0
    for wallet, coin in keys:
        d = dbnet.get((wallet, coin), 0.0)
        e = exnet.get((wallet, coin), 0.0)
        tol = max(abs(d), abs(e)) * 0.02 + 1e-6
        if abs(d - e) > tol:
            flagged += 1
            who = detail.get((wallet, coin), [])
            kind = "ORPHAN?" if not who else "netting"
            print(f"  MISMATCH {_short(wallet)} {coin:8} db_net={d:+.4f} exch_net={e:+.4f} "
                  f"[{kind}] bots={who}")
    if not flagged:
        print(f"  clean ({len(keys)} (address,coin) pairs reconciled)")
    else:
        print(f"  {flagged} pair(s) flagged -> ORPHAN? = on exchange, no live bot")


if __name__ == "__main__":
    main()
