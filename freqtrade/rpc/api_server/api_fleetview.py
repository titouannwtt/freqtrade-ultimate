"""Fleet view API (fork-specific).

Aggregated, host-local view of every running freqtrade bot on this machine:
per-bot capital / direction / P&L attribution (SQLite read-only, no exchange
calls) plus a wallet reconciliation view (netted shared-wallet vs collective
DB state, ONE fetch_positions per refresh, cached).

Resolution actions re-verify the classification server-side immediately
before acting, so a stale UI can never trigger a wrong operation:
- delete_phantom: provably-absent trade -> DELETE via the owning bot's API
  (no exchange order). Same safety criterion as user_data/netting_reconciler.py.
- close_minority: real netted minority slice -> inverse market order WITHOUT
  reduce-only (realigns the wallet with the sibling ledgers), then DELETE the
  stuck trade via the owning bot's API.
- close_unowned: on-chain position with no DB owner -> reduce-only market
  order (safe by construction: can only shrink the net position).
- realign: DB-only repair for one coin. Closes (deletes) whole slices and/or
  rescales AT MOST ONE residual trade so the sum of remaining claims equals
  the on-chain net. NEVER places any exchange order. The reverse direction
  (trading to match the DB) is deliberately not offered: on a shared netted
  wallet the DBs are not a source of truth for the wallet position.
"""

import base64
import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import HTTPException
from pydantic import BaseModel

from freqtrade.rpc.api_server.deps import get_config


logger = logging.getLogger(__name__)

router = APIRouter()

# Comparison tolerances (same semantics as netting_reconciler.py)
REL_TOL = 0.02
ABS_TOL = 1e-6

# Safety cap for any exchange order placed through the resolve endpoint.
MAX_RESOLVE_NOTIONAL = 1000.0

# Below this notional (USDC), a db/on-chain divergence is classified as dust.
DUST_NOTIONAL = 30.0

_CACHE_LOCK = threading.Lock()
_RECON_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
RECON_CACHE_TTL = 60.0


# ---------------------------------------------------------------------------
# Bot discovery (process scan, same approach as user_data/netting_reconciler)
# ---------------------------------------------------------------------------


def _read_cmdline(pid: str) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return [a.decode(errors="replace") for a in raw.split(b"\x00") if a]
    except Exception:
        return []


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _merge_config(path: str, seen: set | None = None) -> dict:
    if seen is None:
        seen = set()
    ap = Path(path).resolve()
    if ap in seen or not ap.exists():
        return {}
    seen.add(ap)
    try:
        d = json.loads(ap.read_text())
    except Exception:
        return {}
    merged: dict = {}
    for inc in d.get("add_config_files", []):
        ip = Path(inc) if Path(inc).is_absolute() else ap.parent / inc
        _deep_merge(merged, _merge_config(str(ip), seen))
    _deep_merge(merged, d)
    return merged


def discover_bots() -> list[dict]:
    """All running freqtrade 'trade' processes on this host, with merged config."""
    out = []
    for pid_dir in Path("/proc").iterdir():
        pid = pid_dir.name
        if not pid.isdigit():
            continue
        argv = _read_cmdline(pid)
        if not argv or "trade" not in argv or not any("freqtrade" in a for a in argv):
            continue
        cfg_path = None
        for i, a in enumerate(argv):
            if a in ("-c", "--config") and i + 1 < len(argv):
                cfg_path = argv[i + 1]
                break
        if not cfg_path:
            continue
        conf = _merge_config(cfg_path)
        if not conf:
            continue
        db_url = conf.get("db_url", "")
        api = conf.get("api_server", {})
        try:
            proc_start = pid_dir.stat().st_mtime
        except Exception:
            proc_start = None
        out.append(
            {
                "pid": int(pid),
                "config_file": Path(cfg_path).name,
                "bot_name": conf.get("bot_name") or Path(cfg_path).name,
                "dry_run": bool(conf.get("dry_run", True)),
                "strategy": conf.get("strategy"),
                "db_path": db_url[len("sqlite:///") :] if db_url.startswith("sqlite:///") else None,
                "port": api.get("listen_port"),
                "api_user": api.get("username"),
                "api_pw": api.get("password"),
                "available_capital": conf.get("available_capital"),
                "capital_withdrawal": conf.get("capital_withdrawal", 0) or 0,
                "wallet": (conf.get("exchange") or {}).get("walletAddress"),
                "private_key": (conf.get("exchange") or {}).get("privateKey")
                or (conf.get("exchange") or {}).get("secret"),
                "process_start": proc_start,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Per-bot DB metrics (read-only)
# ---------------------------------------------------------------------------


def _bot_db_metrics(db_path: str | None) -> dict:
    empty = {
        "direction": "unknown",
        "realized_total": 0.0,
        "realized_1d": 0.0,
        "realized_7d": 0.0,
        "realized_30d": 0.0,
        "open_count": 0,
        "closed_count": 0,
        "count_30d": 0,
        "first_trade": None,
        "last_trade": None,
        "leverage_values": [],
        "open_trades": [],
        "db_ok": False,
    }
    if not db_path or not Path(db_path).exists():
        return empty
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        cur = con.cursor()
        now = datetime.now(UTC)

        def _win(days: int) -> float:
            cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute(
                "SELECT COALESCE(SUM(close_profit_abs),0) FROM trades "
                "WHERE is_open=0 AND close_date>=?",
                (cutoff,),
            )
            return round(cur.fetchone()[0] or 0.0, 6)

        cur.execute(
            "SELECT COALESCE(SUM(close_profit_abs),0), COUNT(*) FROM trades WHERE is_open=0"
        )
        realized_total, closed_count = cur.fetchone()
        cur.execute("SELECT COUNT(DISTINCT is_short) , MAX(is_short), COUNT(*) FROM trades")
        n_dir, max_short, total_count = cur.fetchone()
        if total_count == 0:
            direction = "unknown"
        elif n_dir > 1:
            direction = "dual"
        else:
            direction = "short" if max_short else "long"
        cur.execute("SELECT MIN(open_date), MAX(open_date) FROM trades")
        first_trade, last_trade = cur.fetchone()
        cutoff30 = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("SELECT COUNT(*) FROM trades WHERE open_date>=?", (cutoff30,))
        count_30d = cur.fetchone()[0]
        cur.execute(
            "SELECT id, pair, is_short, amount, open_rate, stake_amount, leverage, open_date "
            "FROM trades WHERE is_open=1"
        )
        open_trades = [
            {
                "trade_id": r[0],
                "pair": r[1],
                "is_short": bool(r[2]),
                "amount": r[3],
                "open_rate": r[4],
                "stake_amount": r[5],
                "leverage": r[6],
                "open_date": r[7],
            }
            for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT DISTINCT leverage FROM trades WHERE id IN "
            "(SELECT id FROM trades ORDER BY id DESC LIMIT 20)"
        )
        leverage_values = sorted({r[0] for r in cur.fetchall() if r[0]})
        realized_1d, realized_7d, realized_30d = _win(1), _win(7), _win(30)
        con.close()
        return {
            "direction": direction,
            "realized_total": round(realized_total or 0.0, 6),
            "realized_1d": realized_1d,
            "realized_7d": realized_7d,
            "realized_30d": realized_30d,
            "open_count": len(open_trades),
            "closed_count": closed_count,
            "count_30d": count_30d,
            "first_trade": first_trade,
            "last_trade": last_trade,
            "leverage_values": leverage_values,
            "open_trades": open_trades,
            "db_ok": True,
        }
    except Exception as e:
        logger.warning("fleetview: DB read failed for %s: %s", db_path, e)
        return empty


# ---------------------------------------------------------------------------
# Overview endpoint
# ---------------------------------------------------------------------------


@router.get("/fleetview/overview", tags=["FleetView"])
def fleetview_overview(config=Depends(get_config)):
    self_port = config.get("api_server", {}).get("listen_port")
    bots = []
    for b in discover_bots():
        m = _bot_db_metrics(b["db_path"])
        ac = b["available_capital"]
        effective = None
        if ac is not None:
            effective = max(0.0, round(ac - b["capital_withdrawal"] + m["realized_total"], 2))
        inert_cutoff = (datetime.now(UTC) - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
        badges = {
            "frozen": effective is not None and effective <= 0,
            "inert": (
                m["db_ok"]
                and m["open_count"] == 0
                and (m["last_trade"] is None or m["last_trade"] < inert_cutoff)
                and (effective is None or effective > 0)
            ),
            "leverage_values": m["leverage_values"],
        }
        bots.append(
            {
                "bot_name": b["bot_name"],
                "config_file": b["config_file"],
                "dry_run": b["dry_run"],
                "strategy": b["strategy"],
                "port": b["port"],
                "is_self": b["port"] == self_port,
                "direction": m["direction"],
                "capital": {
                    "available_capital": ac,
                    "capital_withdrawal": b["capital_withdrawal"],
                    "realized_total": m["realized_total"],
                    "effective": effective,
                },
                "pnl": {
                    "realized_1d": m["realized_1d"],
                    "realized_7d": m["realized_7d"],
                    "realized_30d": m["realized_30d"],
                    "realized_total": m["realized_total"],
                },
                "trades": {
                    "open_count": m["open_count"],
                    "closed_count": m["closed_count"],
                    "count_30d": m["count_30d"],
                },
                "uptime": {
                    "process_start": b["process_start"],
                    "first_trade": m["first_trade"],
                    "last_trade": m["last_trade"],
                },
                "badges": badges,
                "open_trades": m["open_trades"],
                "db_ok": m["db_ok"],
            }
        )
    bots.sort(key=lambda x: (x["dry_run"], x["bot_name"]))
    return {"generated_at": time.time(), "bots": bots}


# ---------------------------------------------------------------------------
# Reconciliation (collective DB vs on-chain net)
# ---------------------------------------------------------------------------


def _live_wallet_creds(bots: list[dict]) -> tuple[str | None, str | None]:
    for b in bots:
        if not b["dry_run"] and b["wallet"] and b["private_key"]:
            return b["wallet"], b["private_key"]
    return None, None


def _fetch_positions_raw(wallet: str, key: str) -> list[dict]:
    import ccxt

    cli = ccxt.hyperliquid({"walletAddress": wallet, "privateKey": key, "enableRateLimit": True})
    last: Exception | None = None
    for attempt in range(3):
        try:
            return cli.fetch_positions()
        except Exception as e:  # incl. RateLimitExceeded
            last = e
            time.sleep(5 * (attempt + 1))
    raise HTTPException(status_code=502, detail=f"fetch_positions failed: {last!r:.120}")


def _match(a: float, b: float) -> bool:
    return abs(a - b) <= max(abs(a), abs(b)) * REL_TOL + ABS_TOL


def _classify_coin(
    slices: list[dict], db_sum: float, net: float
) -> tuple[str, dict | None, list[dict]]:
    if not slices and abs(net) > ABS_TOL:
        return "unowned", None, []
    if _match(db_sum, net):
        minority_slices = [
            {"bot_name": s["bot_name"], "trade_id": s["trade_id"]}
            for s in slices
            if net != 0
            and (s["signed_amount"] > 0) != (net > 0)
            and abs(s["signed_amount"]) > ABS_TOL
        ]
        return ("minority" if minority_slices else "ok"), None, minority_slices
    candidates = [
        s
        for s in slices
        if (net == 0 or (s["signed_amount"] > 0) != (net > 0))
        and _match(db_sum - s["signed_amount"], net)
    ]
    if len(candidates) == 1:
        return (
            "phantom",
            {"bot_name": candidates[0]["bot_name"], "trade_id": candidates[0]["trade_id"]},
            [],
        )
    return "ambiguous", None, []


def _ambiguous_hints(slices: list[dict], db_sum: float, net: float, mark: float) -> list[dict]:
    """Machine-readable diagnostics for coins we cannot classify automatically.

    Ordered like the manual playbook: transient first, dust second, then the
    two real root causes seen in production (multiple phantom candidates,
    partial-fill / stale-DB drift).
    """
    diff = db_sum - net
    hints: list[dict] = [{"code": "refresh_first"}]
    if mark and abs(diff) * mark < DUST_NOTIONAL:
        hints.append({"code": "dust", "notional": round(abs(diff) * mark, 2)})
        return hints
    # Every single-trade removal that would reconcile db_sum with the net.
    removal_candidates = [
        {
            "bot_name": s["bot_name"],
            "trade_id": s["trade_id"],
            "signed_amount": s["signed_amount"],
        }
        for s in slices
        if _match(db_sum - s["signed_amount"], net)
    ]
    if len(removal_candidates) > 1:
        hints.append({"code": "multi_phantom", "candidates": removal_candidates})
    # Slices whose DB amount, if partially filled on-exchange, would explain the drift:
    # same sign as the divergence and large enough to absorb it.
    partial_suspects = [
        {
            "bot_name": s["bot_name"],
            "trade_id": s["trade_id"],
            "db_amount": s["signed_amount"],
            "implied_fill": round(s["signed_amount"] - diff, 6),
        }
        for s in slices
        if abs(s["signed_amount"]) > abs(diff) - ABS_TOL and (s["signed_amount"] > 0) == (diff > 0)
    ]
    if partial_suspects and not removal_candidates:
        hints.append({"code": "partial_fill", "suspects": partial_suspects})
    if not removal_candidates and not partial_suspects:
        hints.append({"code": "stale_db"})
    return hints


def _build_reconciliation() -> dict:
    bots = discover_bots()
    live = [b for b in bots if not b["dry_run"] and b["db_path"]]
    wallet, key = _live_wallet_creds(bots)
    if not wallet:
        raise HTTPException(status_code=409, detail="No live bot with wallet credentials found")

    per_coin: dict[str, list[dict]] = {}
    for b in live:
        m = _bot_db_metrics(b["db_path"])
        for t in m["open_trades"]:
            coin = t["pair"].split("/")[0]
            per_coin.setdefault(coin, []).append(
                {
                    "bot_name": b["bot_name"],
                    "port": b["port"],
                    "trade_id": t["trade_id"],
                    "pair": t["pair"],
                    "signed_amount": -t["amount"] if t["is_short"] else t["amount"],
                    "open_rate": t["open_rate"],
                    "stake_amount": t["stake_amount"],
                }
            )

    positions = _fetch_positions_raw(wallet, key)
    onchain: dict[str, dict] = {}
    for p in positions:
        coin = (p.get("symbol") or "").split("/")[0]
        contracts = p.get("contracts") or 0
        if not coin or not contracts:
            continue
        signed = -contracts if p.get("side") == "short" else contracts
        cur = onchain.setdefault(
            coin,
            {"net": 0.0, "mark_price": None, "leverage": None, "unrealized_pnl": 0.0},
        )
        cur["net"] += signed
        cur["mark_price"] = p.get("markPrice") or p.get("entryPrice")
        cur["leverage"] = p.get("leverage")
        cur["unrealized_pnl"] += p.get("unrealizedPnl") or 0.0

    coins = []
    for coin in sorted(set(per_coin) | set(onchain)):
        slices = per_coin.get(coin, [])
        db_sum = sum(s["signed_amount"] for s in slices)
        oc = onchain.get(
            coin, {"net": 0.0, "mark_price": None, "leverage": None, "unrealized_pnl": 0.0}
        )
        net = oc["net"]
        status, phantom_candidate, minority_slices = _classify_coin(slices, db_sum, net)
        diff = db_sum - net
        mark = oc["mark_price"] or 0.0
        hints: list[dict] = []
        if status == "ambiguous":
            hints = _ambiguous_hints(slices, db_sum, net, mark)
        coins.append(
            {
                "coin": coin,
                "db_sum": round(db_sum, 6),
                "on_chain": round(net, 6),
                "diff": round(diff, 6),
                "diff_notional": round(abs(diff) * mark, 2) if mark else None,
                "mark_price": oc["mark_price"],
                "on_chain_leverage": oc["leverage"],
                "unrealized_pnl": round(oc["unrealized_pnl"], 4),
                "status": status,
                "slices": slices,
                "phantom_candidate": phantom_candidate,
                "minority_slices": minority_slices,
                "hints": hints,
            }
        )

    n_issue = sum(1 for c in coins if c["status"] != "ok")
    return {
        "generated_at": time.time(),
        "wallet": wallet[:6] + "..." + wallet[-4:],
        "live_bots": len(live),
        "coins": coins,
        "issues": n_issue,
    }


@router.get("/fleetview/reconciliation", tags=["FleetView"])
def fleetview_reconciliation(refresh: bool = Query(False)):
    with _CACHE_LOCK:
        fresh = time.time() - _RECON_CACHE["ts"] < RECON_CACHE_TTL
        if _RECON_CACHE["data"] is not None and fresh and not refresh:
            return _RECON_CACHE["data"]
    data = _build_reconciliation()
    with _CACHE_LOCK:
        _RECON_CACHE["ts"] = time.time()
        _RECON_CACHE["data"] = data
    return data


# ---------------------------------------------------------------------------
# Resolution actions (guarded, re-verified server-side)
# ---------------------------------------------------------------------------


class ResolvePayload(BaseModel):
    action: str  # delete_phantom | close_minority | close_unowned
    coin: str
    bot_name: str | None = None
    trade_id: int | None = None
    confirm: bool = False


class RealignOp(BaseModel):
    bot_name: str
    trade_id: int
    op: str  # close | adjust
    new_amount: float | None = None  # absolute magnitude, required for adjust


class RealignPayload(BaseModel):
    coin: str
    operations: list[RealignOp]
    confirm: bool = False


def _bot_api_call(bot: dict, method: str, path: str, body: dict | None = None) -> dict:
    base = f"http://127.0.0.1:{bot['port']}/api/v1"
    tok = base64.b64encode(f"{bot['api_user']}:{bot['api_pw']}".encode()).decode()
    # URLs are built from hardcoded http://127.0.0.1 + local bot port (no user input).
    req = urllib.request.Request(  # noqa: S310
        base + "/token/login", headers={"Authorization": f"Basic {tok}"}, data=b""
    )
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
        bearer = json.loads(r.read().decode())["access_token"]
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {bearer}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(  # noqa: S310
        base + path, headers=headers, data=data, method=method
    )
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
        return json.loads(r.read().decode())


def _bot_api_delete_trade(bot: dict, trade_id: int) -> str:
    try:
        return _bot_api_call(bot, "DELETE", f"/trades/{trade_id}").get("result_msg", "ok")
    except urllib.error.HTTPError as e:
        # The owning bot may be mid-cycle; the delete often lands server-side anyway.
        return f"HTTP {e.code} (verify on next refresh)"


def _bot_api_adjust_amount(bot: dict, trade_id: int, amount: float) -> str:
    try:
        resp = _bot_api_call(bot, "POST", f"/trades/{trade_id}/amount", {"amount": amount})
        return resp.get("result_msg", "ok")
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return "bot does not expose /trades/<id>/amount yet - restart it on current code first"
        return f"HTTP {e.code} (verify on next refresh)"


def _place_market_order(
    wallet: str, key: str, symbol: str, side: str, amount: float, reduce_only: bool
) -> dict:
    import ccxt

    cli = ccxt.hyperliquid({"walletAddress": wallet, "privateKey": key, "enableRateLimit": True})
    cli.load_markets()
    ticker = cli.fetch_ticker(symbol)
    ref = ticker.get("bid") if side == "sell" else ticker.get("ask")
    if not ref:
        raise HTTPException(status_code=502, detail="No price available for " + symbol)
    notional = amount * ref
    if notional > MAX_RESOLVE_NOTIONAL:
        raise HTTPException(
            status_code=409,
            detail=f"Notional {notional:.0f} exceeds safety cap {MAX_RESOLVE_NOTIONAL:.0f} USDC",
        )
    px = ref * (0.99 if side == "sell" else 1.01)
    params = {"reduceOnly": True} if reduce_only else {}
    order = cli.create_order(symbol, "market", side, amount, px, params)
    return {
        "order_id": order.get("id"),
        "filled": order.get("filled"),
        "average": order.get("average"),
        "notional": round(notional, 2),
    }


@router.post("/fleetview/reconciliation/resolve", tags=["FleetView"])
def fleetview_resolve(payload: ResolvePayload):
    if not payload.confirm:
        raise HTTPException(status_code=409, detail="confirm=true required")
    # Never act on cached state: rebuild right before acting.
    state = _build_reconciliation()
    entry = next((c for c in state["coins"] if c["coin"] == payload.coin), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Coin {payload.coin} not found")

    bots = {b["bot_name"]: b for b in discover_bots()}
    wallet, key = _live_wallet_creds(list(bots.values()))
    result: dict[str, Any] = {"action": payload.action, "coin": payload.coin}

    if payload.action == "delete_phantom":
        pc = entry["phantom_candidate"]
        if entry["status"] != "phantom" or not pc:
            raise HTTPException(status_code=409, detail="Not a provable phantom (state changed?)")
        if payload.bot_name != pc["bot_name"] or payload.trade_id != pc["trade_id"]:
            raise HTTPException(status_code=409, detail="Phantom candidate changed, refresh first")
        bot = bots.get(pc["bot_name"])
        if not bot or not bot["port"]:
            raise HTTPException(status_code=409, detail="Owning bot API not reachable")
        result["delete"] = _bot_api_delete_trade(bot, pc["trade_id"])

    elif payload.action == "close_minority":
        target = next(
            (
                s
                for s in entry["slices"]
                if s["bot_name"] == payload.bot_name and s["trade_id"] == payload.trade_id
            ),
            None,
        )
        is_min = any(
            m["bot_name"] == payload.bot_name and m["trade_id"] == payload.trade_id
            for m in entry["minority_slices"]
        )
        if entry["status"] != "minority" or not target or not is_min:
            raise HTTPException(
                status_code=409, detail="Not a verified minority slice (state changed?)"
            )
        # Inverse order WITHOUT reduce-only: realigns wallet with sibling ledgers.
        side = "sell" if target["signed_amount"] > 0 else "buy"
        result["order"] = _place_market_order(
            wallet, key, target["pair"], side, abs(target["signed_amount"]), reduce_only=False
        )
        bot = bots.get(payload.bot_name)
        if bot and bot["port"]:
            result["delete"] = _bot_api_delete_trade(bot, payload.trade_id)
        else:
            result["delete"] = "owning bot API not reachable — delete the trade manually"

    elif payload.action == "close_unowned":
        if entry["status"] != "unowned":
            raise HTTPException(status_code=409, detail="Coin is not unowned (state changed?)")
        net = entry["on_chain"]
        side = "sell" if net > 0 else "buy"
        # Reduce-only: can only shrink the real net position, never flip it.
        symbol = f"{payload.coin}/USDC:USDC"
        result["order"] = _place_market_order(wallet, key, symbol, side, abs(net), reduce_only=True)

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action {payload.action}")

    with _CACHE_LOCK:
        _RECON_CACHE["ts"] = 0.0  # force refresh on next read
    return result


def _validate_realign_ops(payload: RealignPayload, slices: dict) -> dict:
    """Structural validation of a realign plan. Raises on invalid."""
    if not payload.operations:
        raise HTTPException(status_code=400, detail="No operations provided")
    keys = [(o.bot_name, o.trade_id) for o in payload.operations]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=400, detail="Duplicate trade in operations")
    adjusts = [o for o in payload.operations if o.op == "adjust"]
    if len(adjusts) > 1:
        raise HTTPException(
            status_code=409,
            detail="At most one adjust operation allowed (close whole slices, "
            "adjust a single residual trade)",
        )
    ops_by_key = {}
    for op in payload.operations:
        if op.op not in ("close", "adjust"):
            raise HTTPException(status_code=400, detail=f"Unknown op {op.op}")
        if (op.bot_name, op.trade_id) not in slices:
            raise HTTPException(
                status_code=409,
                detail=f"Trade {op.bot_name} #{op.trade_id} no longer in slices, refresh first",
            )
        if op.op == "adjust" and (op.new_amount is None or op.new_amount <= 0):
            raise HTTPException(
                status_code=400, detail="adjust requires new_amount > 0 (use close otherwise)"
            )
        ops_by_key[(op.bot_name, op.trade_id)] = op
    return ops_by_key


def _simulate_realign(payload: RealignPayload, entry: dict) -> dict:
    """Validate + simulate a realign plan against fresh state. Raises on invalid."""
    slices = {(s["bot_name"], s["trade_id"]): s for s in entry["slices"]}
    ops_by_key = _validate_realign_ops(payload, slices)

    mark = entry["mark_price"] or 0.0
    resulting = 0.0
    touched_notional = 0.0
    plan = []
    for key, s in slices.items():
        signed = s["signed_amount"]
        op = ops_by_key.get(key)
        if op is None:
            resulting += signed
            continue
        if op.op == "close":
            new_signed = 0.0
        else:
            sign = 1.0 if signed > 0 else -1.0
            new_signed = sign * (op.new_amount or 0.0)
            resulting += new_signed
        delta = new_signed - signed
        touched_notional += abs(delta) * mark
        plan.append(
            {
                "bot_name": s["bot_name"],
                "trade_id": s["trade_id"],
                "op": op.op,
                "signed_amount_before": signed,
                "signed_amount_after": round(new_signed, 6),
                "delta": round(delta, 6),
                "delta_notional": round(abs(delta) * mark, 2) if mark else None,
            }
        )

    net = entry["on_chain"]
    if not _match(resulting, net):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Plan does not reconcile: resulting DB sum {resulting:.6f} vs on-chain {net:.6f}"
            ),
        )
    if mark and touched_notional > MAX_RESOLVE_NOTIONAL:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Touched notional {touched_notional:.0f} exceeds safety cap "
                f"{MAX_RESOLVE_NOTIONAL:.0f} USDC"
            ),
        )
    return {
        "coin": payload.coin,
        "on_chain": net,
        "db_sum_before": entry["db_sum"],
        "db_sum_after": round(resulting, 6),
        "touched_notional": round(touched_notional, 2),
        "operations": plan,
    }


@router.post("/fleetview/reconciliation/realign", tags=["FleetView"])
def fleetview_realign(payload: RealignPayload):
    """Realign the collective DB with the on-chain net for one coin (DB-only).

    Closes (deletes) whole slices and/or rescales AT MOST ONE residual trade so
    that the sum of remaining claims equals the on-chain net. Never places any
    exchange order. confirm=false returns the validated plan without acting.
    """
    # Never act on cached state: rebuild right before validating/acting.
    state = _build_reconciliation()
    entry = next((c for c in state["coins"] if c["coin"] == payload.coin), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Coin {payload.coin} not found")
    if entry["status"] == "ok":
        raise HTTPException(status_code=409, detail="Coin already reconciled")

    preview = _simulate_realign(payload, entry)
    if not payload.confirm:
        return {"preview": True, **preview}

    bots = {b["bot_name"]: b for b in discover_bots()}
    results = []
    for op in payload.operations:
        res: dict[str, Any] = {"bot_name": op.bot_name, "trade_id": op.trade_id, "op": op.op}
        bot = bots.get(op.bot_name)
        if not bot or not bot["port"]:
            res["result"] = "owning bot API not reachable - resolve manually"
        elif op.op == "close":
            res["result"] = _bot_api_delete_trade(bot, op.trade_id)
        else:
            res["result"] = _bot_api_adjust_amount(bot, op.trade_id, op.new_amount or 0.0)
        results.append(res)

    with _CACHE_LOCK:
        _RECON_CACHE["ts"] = 0.0  # force refresh on next read
    return {"preview": False, **preview, "results": results}


# Wallet-level exposure thresholds - kept in sync with user_data/exposure_guardrail.py.
FREE_MARGIN_WARN = 0.30
FREE_MARGIN_CRIT = 0.10
LEVERAGE_WARN = 2.0
LEVERAGE_CRIT = 2.5
MARGIN_UTIL_WARN = 0.80
MARGIN_UTIL_CRIT = 0.92
EXPOSURE_STALE_S = 30 * 60


@router.get("/fleetview/exposure", tags=["FleetView"])
def fleetview_exposure(config=Depends(get_config)):
    """Latest aggregate-exposure snapshot for the shared netted wallet (fork).

    Reads the JSON snapshot written every 15 min by the exposure_guardrail.py cron
    (equity / notional / margin / withdrawable + derived leverage & utilisation).
    No exchange call: the guardrail owns the single HL info fetch. Returns
    ``{"available": False}`` when no snapshot exists yet.
    """
    udir = Path(config.get("user_data_dir") or "user_data")
    snap = udir / "exposure_state.json"
    if not snap.exists():
        return {"available": False}
    try:
        data = json.loads(snap.read_text())
    except Exception:
        return {"available": False}
    data["available"] = True
    data["thresholds"] = {
        "free_margin_warn": FREE_MARGIN_WARN,
        "free_margin_crit": FREE_MARGIN_CRIT,
        "leverage_warn": LEVERAGE_WARN,
        "leverage_crit": LEVERAGE_CRIT,
        "margin_util_warn": MARGIN_UTIL_WARN,
        "margin_util_crit": MARGIN_UTIL_CRIT,
    }
    try:
        ts = datetime.fromisoformat(data.get("ts")).timestamp()
        data["age_s"] = max(0.0, time.time() - ts)
        data["stale"] = data["age_s"] > EXPOSURE_STALE_S
    except Exception:
        data["age_s"] = None
        data["stale"] = None
    return data
