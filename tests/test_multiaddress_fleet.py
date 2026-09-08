"""Fleet tooling must survive a bot moving to a different Hyperliquid address.

Every helper below assumed "one wallet for the whole fleet". Once one bot runs on a
sub-account, that assumption turns each of its live positions into an *absent*
position for the master wallet — and the netting reconciler deletes absent positions
from the owning bot's DB. Same class of bug in the daemon's positions cache (one bot
teaching the fleet the wrong address), in the exposure guardrail (an account silently
out of scope) and in FreqUI (an order signed for the wrong account).

Each fix is covered twice: two bots on two addresses (the new case) AND every bot on
one address (non-regression — behaviour must be byte-for-byte what it was).
"""

import asyncio
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
# Adresses factices construites a l execution : une adresse en clair dans le
# depot declenche le garde-fou anti-secret, meme quand elle est inventee.
WALLET_A = "0x" + "A" * 36 + "1111"
WALLET_B = "0x" + "B" * 36 + "2222"


def _load(name: str, relpath: str):
    """Import a repo script that is not part of the freqtrade package."""
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# 1. user_data/netting_reconciler.py — the one that DELETES trades
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def nr():
    return _load("_nr_under_test", "user_data/netting_reconciler.py")


def _bot(wallet, hip3=()):
    return {"db": "", "port": 1, "user": "u", "pw": "p", "hip3": list(hip3), "wallet": wallet}


def _slice(name, tid, signed, pair, age_s=99999):
    return (name, tid, signed, signed < 0, pair, age_s)


def test_reconciler_two_addresses_never_phantoms_the_other_account(nr):
    """A bot alone on address B holding BTC is NOT a phantom of address A."""
    per_key = {
        (WALLET_A.lower(), "ETH"): [_slice("bot_a", 1, 5.0, "ETH/USDC:USDC")],
        (WALLET_B.lower(), "BTC"): [_slice("bot_b", 7, 2.0, "BTC/USDC:USDC")],
    }
    nets = {
        WALLET_A.lower(): {"ETH": 5.0},  # A holds ETH only — no BTC anywhere on A
        WALLET_B.lower(): {"BTC": 2.0},  # B holds its own BTC
    }
    auto, minority, ambiguous = nr.analyze(per_key, nets)
    assert auto == [], "a position living on another address must never be deleted"
    assert minority == []
    assert ambiguous == []


def test_reconciler_single_wallet_still_detects_the_phantom(nr):
    """Non-regression: on one address the provable phantom is still found."""
    # bot_b is short a coin the wallet is net LONG on, and removing its slice makes
    # the collective sum equal the on-chain net: a provable phantom.
    per_key = {
        (WALLET_A.lower(), "BTC"): [
            _slice("bot_a", 1, 2.0, "BTC/USDC:USDC"),
            _slice("bot_b", 2, -3.0, "BTC/USDC:USDC"),
        ]
    }
    nets = {WALLET_A.lower(): {"BTC": 2.0}}
    auto, minority, ambiguous = nr.analyze(per_key, nets)
    assert [(a[1], a[2], a[3]) for a in auto] == [("BTC", "bot_b", 2)]
    assert ambiguous == []


def test_reconciler_would_have_deleted_before_the_fix(nr):
    """The 2026-08-02 shape: same trade, compared to the WRONG address, is a phantom.

    Proves the two tests above are not vacuous — the classifier does fire when the
    net it is handed genuinely lacks the position.
    """
    per_key = {(WALLET_B.lower(), "BTC"): [_slice("bot_b", 7, 2.0, "BTC/USDC:USDC")]}
    nets = {WALLET_B.lower(): {}}  # as if we had read the master wallet: BTC absent
    auto, _minority, _amb = nr.analyze(per_key, nets)
    assert [(a[0], a[3]) for a in auto] == [(WALLET_B.lower(), 7)]


def test_reconciler_unknown_address_is_skipped_not_phantomed(nr, tmp_path):
    """A bot whose walletAddress cannot be resolved is excluded, never compared."""
    db = tmp_path / "no_addr.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trades (id INTEGER, pair TEXT, amount REAL, is_short INTEGER,"
        " open_date TEXT, is_open INTEGER)"
    )
    conn.execute("INSERT INTO trades VALUES (1, 'BTC/USDC:USDC', 2.0, 0, '2020-01-01 00:00:00', 1)")
    conn.commit()
    conn.close()
    bots = {
        "ghost": {"db": str(db), "port": 1, "user": "u", "pw": "p", "hip3": [], "wallet": None},
    }
    per_key, skipped = nr.collect_slices(bots)
    assert per_key == {}
    assert skipped == ["ghost"]
    assert nr.analyze(per_key, {}) == ([], [], [])


def test_reconciler_failed_fetch_isolates_that_address_only(nr):
    """One address unreadable must not freeze — nor act on — the others."""
    per_key = {
        (WALLET_A.lower(), "BTC"): [
            _slice("bot_a", 1, 2.0, "BTC/USDC:USDC"),
            _slice("bot_b", 2, -3.0, "BTC/USDC:USDC"),
        ],
        (WALLET_B.lower(), "ETH"): [_slice("bot_c", 9, 4.0, "ETH/USDC:USDC")],
    }
    nets = {WALLET_A.lower(): {"BTC": 2.0}}  # B's fetch failed -> absent from nets
    auto, _minority, _amb = nr.analyze(per_key, nets)
    assert [(a[0], a[3]) for a in auto] == [(WALLET_A.lower(), 2)]


def test_reconciler_fetches_one_net_per_address_with_its_own_dexes(nr):
    calls = []

    def fake_fetch(wallet, dexes):
        calls.append((wallet, tuple(dexes)))
        return {"BTC": 1.0}, None

    bots = {
        "a1": _bot(WALLET_A.lower(), ["xyz"]),
        "a2": _bot(WALLET_A.lower()),
        "b1": _bot(WALLET_B.lower()),
    }
    nets, errors = nr.fetch_nets(bots, fetcher=fake_fetch)
    assert errors == {}
    assert sorted(nets) == sorted([WALLET_A.lower(), WALLET_B.lower()])
    assert calls == [(WALLET_A.lower(), ("xyz",)), (WALLET_B.lower(), ())]


def test_reconciler_min_age_guard_is_preserved(nr):
    """A freshly-opened trade is never a deletion candidate, address or not."""
    per_key = {
        (WALLET_A.lower(), "BTC"): [
            _slice("bot_a", 1, 2.0, "BTC/USDC:USDC"),
            _slice("bot_b", 2, -3.0, "BTC/USDC:USDC", age_s=nr.MIN_DELETE_AGE_S - 1),
        ]
    }
    nets = {WALLET_A.lower(): {"BTC": 2.0}}
    auto, _minority, ambiguous = nr.analyze(per_key, nets)
    assert auto == []
    assert len(ambiguous) == 1


def test_reconciler_never_reads_a_private_key(nr):
    """The reconciler must not carry a hardcoded access file nor read privateKey."""
    src = (REPO / "user_data" / "netting_reconciler.py").read_text()
    assert "privateKey" not in src
    assert "_hyperliquid_freqtrade_access" not in src


def test_reconciler_bot_wallet_normalises_and_accepts_both_spellings(nr):
    assert nr.bot_wallet({"exchange": {"walletAddress": WALLET_A}}) == WALLET_A.lower()
    assert nr.bot_wallet({"exchange": {"wallet_address": WALLET_B}}) == WALLET_B.lower()
    assert nr.bot_wallet({"exchange": {}}) is None
    assert nr.bot_wallet({}) is None


# --------------------------------------------------------------------------- #
# 2. freqtrade/ohlcv_cache — the daemon's central positions cache
# --------------------------------------------------------------------------- #
def _daemon(tmp_path):
    from freqtrade.ohlcv_cache.daemon import Daemon

    return Daemon(str(tmp_path / "s.sock"), {"positions_cache_ttl_s": 999.0})


def _run(coro):
    return asyncio.run(coro)


def test_daemon_positions_cache_is_keyed_by_address(tmp_path):
    """Two addresses, two cache entries — neither is served to the other."""
    d = _daemon(tmp_path)
    pos_a = [{"symbol": "BTC/USDC:USDC", "contracts": 1.0, "side": "long"}]
    pos_b = [{"symbol": "ETH/USDC:USDC", "contracts": 9.0, "side": "short"}]
    _run(
        d._handle_positions_put(
            {"exchange": "hyperliquid", "wallet_address": WALLET_A, "data": pos_a}
        )
    )
    _run(
        d._handle_positions_put(
            {"exchange": "hyperliquid", "wallet_address": WALLET_B, "data": pos_b}
        )
    )

    ra = _run(d._handle_positions_get({"exchange": "hyperliquid", "wallet_address": WALLET_A}))
    rb = _run(d._handle_positions_get({"exchange": "hyperliquid", "wallet_address": WALLET_B}))
    assert ra["hit"] and ra["data"] == pos_a
    assert rb["hit"] and rb["data"] == pos_b


def test_daemon_second_address_does_not_overwrite_the_first_target(tmp_path):
    """The central fetcher target must ACCUMULATE, not be replaced by the last caller."""
    d = _daemon(tmp_path)
    _run(d._handle_positions_get({"exchange": "hyperliquid", "wallet_address": WALLET_A}))
    _run(d._handle_positions_get({"exchange": "hyperliquid", "wallet_address": WALLET_B}))
    assert set(d._positions_fetch_targets) == {
        ("hyperliquid", WALLET_A.lower()),
        ("hyperliquid", WALLET_B.lower()),
    }


def test_daemon_single_address_behaviour_unchanged(tmp_path):
    """Non-regression: one address = one target, one cache entry, cache hits as before."""
    d = _daemon(tmp_path)
    pos = [{"symbol": "BTC/USDC:USDC", "contracts": 1.0, "side": "long"}]
    for _ in range(3):
        _run(d._handle_positions_get({"exchange": "hyperliquid", "wallet_address": WALLET_A}))
    _run(
        d._handle_positions_put(
            {"exchange": "hyperliquid", "wallet_address": WALLET_A, "data": pos}
        )
    )
    r = _run(d._handle_positions_get({"exchange": "hyperliquid", "wallet_address": WALLET_A}))
    assert r["hit"] and r["data"] == pos
    assert list(d._positions_fetch_targets) == [("hyperliquid", WALLET_A.lower())]
    assert len(d._positions_cache) == 1


def test_daemon_address_case_does_not_split_the_cache(tmp_path):
    """Checksummed and lowercase spellings are the same account."""
    d = _daemon(tmp_path)
    pos = [{"symbol": "BTC/USDC:USDC", "contracts": 1.0, "side": "long"}]
    _run(
        d._handle_positions_put(
            {"exchange": "hyperliquid", "wallet_address": WALLET_A, "data": pos}
        )
    )
    r = _run(
        d._handle_positions_get({"exchange": "hyperliquid", "wallet_address": WALLET_A.lower()})
    )
    assert r["hit"] and r["data"] == pos


def test_daemon_unidentified_push_is_never_served_to_an_identified_reader(tmp_path):
    """An old client that pushes without an address gets its own bucket.

    Serving it to an address-aware reader would be exactly the cross-account leak
    this change exists to prevent; a miss just costs one /info call.
    """
    d = _daemon(tmp_path)
    _run(d._handle_positions_put({"exchange": "hyperliquid", "data": [{"symbol": "X"}]}))
    r = _run(d._handle_positions_get({"exchange": "hyperliquid", "wallet_address": WALLET_A}))
    assert r["hit"] is False


def test_daemon_open_position_symbols_unions_every_address(tmp_path):
    """OHLCV fetch priority must cover every account the fleet holds."""
    d = _daemon(tmp_path)
    _run(
        d._handle_positions_put(
            {
                "exchange": "hyperliquid",
                "wallet_address": WALLET_A,
                "data": [{"symbol": "BTC/USDC:USDC", "contracts": 1.0}],
            }
        )
    )
    _run(
        d._handle_positions_put(
            {
                "exchange": "hyperliquid",
                "wallet_address": WALLET_B,
                "data": [{"symbol": "ETH/USDC:USDC", "contracts": 2.0}],
            }
        )
    )
    assert d._open_position_symbols("hyperliquid") == {"BTC/USDC:USDC", "ETH/USDC:USDC"}


def test_daemon_central_fetcher_serves_each_address_its_own_positions(tmp_path):
    """The periodic fetcher writes one cache entry per address, not one per exchange."""
    d = _daemon(tmp_path)
    d._positions_daemon_fetch_interval_s = 0.01
    _run(d._handle_positions_get({"exchange": "hyperliquid", "wallet_address": WALLET_A}))
    _run(d._handle_positions_get({"exchange": "hyperliquid", "wallet_address": WALLET_B}))

    by_wallet = {
        WALLET_A.lower(): [{"symbol": "BTC/USDC:USDC", "contracts": 1.0, "side": "long"}],
        WALLET_B.lower(): [{"symbol": "ETH/USDC:USDC", "contracts": 2.0, "side": "short"}],
    }

    class _FakeClient:
        def __init__(self, wallet):
            self.wallet = wallet

        async def fetch_positions(self):
            return by_wallet[self.wallet.lower()]

    async def fake_client(exchange, wallet):
        return _FakeClient(wallet)

    async def drive():
        d._positions_fetch_client = fake_client
        task = asyncio.create_task(d._periodic_positions_fetch())
        await asyncio.sleep(0.05)
        d._shutdown_event.set()
        await asyncio.wait_for(task, timeout=2)

    _run(drive())
    from freqtrade.ohlcv_cache.daemon import positions_cache_key

    assert (
        d._positions_cache[positions_cache_key("hyperliquid", WALLET_A)].data
        == (by_wallet[WALLET_A.lower()])
    )
    assert (
        d._positions_cache[positions_cache_key("hyperliquid", WALLET_B)].data
        == (by_wallet[WALLET_B.lower()])
    )


def test_client_push_positions_carries_the_wallet():
    """The push must identify its account, or the daemon cannot key it."""
    from freqtrade.ohlcv_cache.client import OhlcvCacheClient

    sent = {}

    class _C(OhlcvCacheClient):
        async def _send_and_receive(self, req):
            sent.update(req)
            return {"ok": True}

    c = _C(socket_path="/nonexistent.sock", exchange_id="hyperliquid", trading_mode="futures")
    _run(c.push_positions([{"symbol": "BTC"}], wallet_address=WALLET_A))
    assert sent["wallet_address"] == WALLET_A
    assert sent["op"] == "positions_put"


# --------------------------------------------------------------------------- #
# 3. user_data/exposure_guardrail.py
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def eg():
    return _load("_eg_under_test", "user_data/exposure_guardrail.py")


def _metrics(eg_mod, equity, notional, margin_used, withdrawable, n=1):
    return {
        "equity": equity,
        "notional": notional,
        "margin_used": margin_used,
        "withdrawable": withdrawable,
        "free_margin_pct": withdrawable / equity,
        "leverage": notional / equity,
        "margin_util": margin_used / equity,
        "n_positions": n,
    }


def test_guardrail_aggregates_every_address(eg):
    per = [
        {"wallet": WALLET_A, "metrics": _metrics(eg, 1000, 1000, 400, 500), "severity": "OK"},
        {"wallet": WALLET_B, "metrics": _metrics(eg, 500, 1500, 400, 50), "severity": "CRITICAL"},
    ]
    agg = eg.aggregate_metrics(per)
    assert agg["equity"] == 1500
    assert agg["notional"] == 2500
    # Ratios are derived from the totals, never averaged.
    assert agg["leverage"] == pytest.approx(2500 / 1500)
    assert agg["n_positions"] == 2


def test_guardrail_single_address_matches_that_address(eg):
    """Non-regression: with one account the aggregate IS that account's metrics."""
    m = _metrics(eg, 1000, 2000, 800, 300)
    agg = eg.aggregate_metrics([{"wallet": WALLET_A, "metrics": m, "severity": "WARN"}])
    for k, v in m.items():
        assert agg[k] == pytest.approx(v)


def test_guardrail_worst_of_never_dilutes_a_bad_account(eg):
    """A small over-levered sub-account must not be hidden by a large calm wallet."""
    calm = _metrics(eg, 100000, 10000, 1000, 60000)
    hot = _metrics(eg, 1000, 2900, 950, 20)
    per = [
        {"wallet": WALLET_A, "metrics": calm, "severity": eg.severity(calm)[0]},
        {"wallet": WALLET_B, "metrics": hot, "severity": eg.severity(hot)[0]},
    ]
    agg = eg.aggregate_metrics(per)
    agg_sev = eg.severity(agg)[0]
    assert agg_sev == "OK", "the aggregate alone would say everything is fine"
    assert eg.worst([agg_sev] + [p["severity"] for p in per]) == "CRITICAL"


def test_guardrail_scope_includes_master_and_every_live_bot_address(eg, tmp_path, monkeypatch):
    cfg_dir = tmp_path / "live_configs"
    cfg_dir.mkdir()
    (cfg_dir / "bot_a.json").write_text(
        json.dumps({"dry_run": False, "exchange": {"walletAddress": WALLET_A}})
    )
    (cfg_dir / "bot_b.json").write_text(
        json.dumps({"dry_run": False, "exchange": {"walletAddress": WALLET_B}})
    )
    (cfg_dir / "bot_dry.json").write_text(
        json.dumps({"dry_run": True, "exchange": {"walletAddress": "0xdead"}})
    )
    (cfg_dir / "bot_noaddr.json").write_text(json.dumps({"dry_run": False, "exchange": {}}))

    monkeypatch.setattr(eg, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(eg, "_master_wallet", lambda: WALLET_A)
    monkeypatch.setattr(
        eg,
        "_running_configs",
        lambda: {"bot_a.json", "bot_b.json", "bot_dry.json", "bot_noaddr.json"},
    )
    wallets, unresolved = eg.live_wallets()
    assert {w.lower() for w in wallets} == {WALLET_A.lower(), WALLET_B.lower()}
    assert unresolved == ["bot_noaddr"]


def test_guardrail_scope_falls_back_to_master_when_no_bot_runs(eg, tmp_path, monkeypatch):
    """Non-regression: today's fleet resolves to exactly the master wallet."""
    monkeypatch.setattr(eg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(eg, "_master_wallet", lambda: WALLET_A)
    monkeypatch.setattr(eg, "_running_configs", set)
    wallets, unresolved = eg.live_wallets()
    assert wallets == [WALLET_A]
    assert unresolved == []


# --------------------------------------------------------------------------- #
# 4. FreqUI server side — signing on the right account
# --------------------------------------------------------------------------- #
def _fv_bot(name, wallet, key="k", vault=None, dry=False):
    return {
        "bot_name": name,
        "dry_run": dry,
        "wallet": wallet,
        "private_key": key,
        "vault_address": vault,
        "hip3_dexes": [],
        "db_path": f"/tmp/{name}.sqlite",
        "port": 9000,
        "api_user": "u",
        "api_pw": "p",
    }


def test_fleetview_creds_refuse_to_guess_across_addresses():
    from fastapi import HTTPException

    from freqtrade.rpc.api_server.api_fleetview import _live_wallet_creds

    bots = [_fv_bot("a", WALLET_A), _fv_bot("b", WALLET_B)]
    with pytest.raises(HTTPException) as exc:
        _live_wallet_creds(bots)
    assert exc.value.status_code == 409


def test_fleetview_creds_pick_the_bot_of_the_requested_address():
    from freqtrade.rpc.api_server.api_fleetview import _live_wallet_creds

    bots = [_fv_bot("a", WALLET_A, key="ka"), _fv_bot("b", WALLET_B, key="kb", vault=WALLET_B)]
    assert _live_wallet_creds(bots, WALLET_B) == (WALLET_B, "kb", WALLET_B)
    # Case-insensitive, as addresses are.
    assert _live_wallet_creds(bots, WALLET_B.lower())[1] == "kb"
    # Unknown address: no credentials rather than someone else's.
    assert _live_wallet_creds(bots, "0xcafe") == (None, None, None)


def test_fleetview_creds_single_address_unchanged():
    """Non-regression: one address = the same creds the old code returned."""
    from freqtrade.rpc.api_server.api_fleetview import _live_wallet_creds

    bots = [
        _fv_bot("a", WALLET_A, key="ka"),
        _fv_bot("b", WALLET_A, key="kb"),
        _fv_bot("d", WALLET_A, key="kd", dry=True),
    ]
    assert _live_wallet_creds(bots) == (WALLET_A, "ka", None)


def test_fleetview_vault_address_read_from_ccxt_options():
    from freqtrade.rpc.api_server.api_fleetview import _vault_address

    assert (
        _vault_address({"exchange": {"ccxt_config": {"options": {"vaultAddress": WALLET_B}}}})
        == WALLET_B
    )
    assert (
        _vault_address(
            {"exchange": {"ccxt_async_config": {"options": {"subAccountAddress": WALLET_B}}}}
        )
        == WALLET_B
    )
    assert _vault_address({"exchange": {}}) is None


def test_fleetview_market_order_targets_the_vault(monkeypatch):
    """Without options.vaultAddress a signed order executes on the MASTER account."""
    import freqtrade.rpc.api_server.api_fleetview as fv

    seen = {}

    class _Ex:
        def __init__(self, cfg):
            seen["cfg"] = cfg

        def load_markets(self):
            return {}

        def fetch_ticker(self, symbol):
            return {"bid": 100.0, "ask": 100.0}

        def create_order(self, *a, **kw):
            seen["params"] = a[-1]
            return {"id": "1", "filled": 1.0, "average": 100.0}

    fake_ccxt = type("m", (), {"hyperliquid": _Ex})
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)

    fv._place_market_order(
        WALLET_B, "kb", "BTC/USDC:USDC", "sell", 0.01, True, vault_address=WALLET_B
    )
    assert seen["cfg"]["options"]["vaultAddress"] == WALLET_B
    assert seen["cfg"]["walletAddress"] == WALLET_B

    # Non-regression: a master-wallet bot has no vault and must send no such option.
    fv._place_market_order(WALLET_A, "ka", "BTC/USDC:USDC", "sell", 0.01, True)
    assert "options" not in seen["cfg"]


def test_fleetview_select_entry_refuses_an_ambiguous_coin():
    from fastapi import HTTPException

    from freqtrade.rpc.api_server.api_fleetview import _select_coin_entry

    state = {
        "coins": [
            {"coin": "BTC", "wallet": WALLET_A, "wallet_short": "0xAAAA...1111"},
            {"coin": "BTC", "wallet": WALLET_B, "wallet_short": "0xBBBB...2222"},
        ]
    }
    with pytest.raises(HTTPException) as exc:
        _select_coin_entry(state, "BTC", None)
    assert exc.value.status_code == 409
    assert _select_coin_entry(state, "BTC", WALLET_B)["wallet"] == WALLET_B


def test_fleetview_select_entry_single_address_unchanged():
    """Non-regression: one address, no wallet given, the entry is found as before."""
    from fastapi import HTTPException

    from freqtrade.rpc.api_server.api_fleetview import _select_coin_entry

    state = {"coins": [{"coin": "BTC", "wallet": WALLET_A, "wallet_short": "0xAAAA...1111"}]}
    assert _select_coin_entry(state, "BTC", None)["coin"] == "BTC"
    with pytest.raises(HTTPException) as exc:
        _select_coin_entry(state, "ETH", None)
    assert exc.value.status_code == 404


def test_fleetview_reconciliation_compares_each_address_to_its_own_net(monkeypatch):
    """Two bots, two addresses: neither claims the other's coin as a phantom."""
    import freqtrade.rpc.api_server.api_fleetview as fv

    bots = [_fv_bot("bot_a", WALLET_A), _fv_bot("bot_b", WALLET_B, vault=WALLET_B)]
    monkeypatch.setattr(fv, "discover_bots", lambda: bots)

    def fake_metrics(db_path):
        pair = "ETH/USDC:USDC" if "bot_a" in db_path else "BTC/USDC:USDC"
        amount = 5.0 if "bot_a" in db_path else 2.0
        return {
            "open_trades": [
                {
                    "trade_id": 1,
                    "pair": pair,
                    "amount": amount,
                    "is_short": False,
                    "open_rate": 1.0,
                    "stake_amount": 1.0,
                }
            ]
        }

    monkeypatch.setattr(fv, "_bot_db_metrics", fake_metrics)

    def fake_positions(wallet, key=None, hip3_dexes=()):
        if wallet.lower() == WALLET_A.lower():
            return [{"symbol": "ETH/USDC:USDC", "contracts": 5.0, "side": "long", "markPrice": 1.0}]
        return [{"symbol": "BTC/USDC:USDC", "contracts": 2.0, "side": "long", "markPrice": 1.0}]

    monkeypatch.setattr(fv, "_fetch_positions_raw", fake_positions)

    state = fv._build_reconciliation()
    assert state["multi_address"] is True
    assert {c["status"] for c in state["coins"]} == {"ok"}
    by_coin = {c["coin"]: c for c in state["coins"]}
    assert by_coin["BTC"]["wallet"] == WALLET_B
    assert by_coin["ETH"]["wallet"] == WALLET_A


def test_fleetview_reconciliation_single_address_unchanged(monkeypatch):
    """Non-regression: one address, two bots, the netted view is the collective sum."""
    import freqtrade.rpc.api_server.api_fleetview as fv

    bots = [_fv_bot("bot_a", WALLET_A), _fv_bot("bot_b", WALLET_A)]
    monkeypatch.setattr(fv, "discover_bots", lambda: bots)
    monkeypatch.setattr(
        fv,
        "_bot_db_metrics",
        lambda db: {
            "open_trades": [
                {
                    "trade_id": 1,
                    "pair": "BTC/USDC:USDC",
                    "amount": 1.0,
                    "is_short": False,
                    "open_rate": 1.0,
                    "stake_amount": 1.0,
                }
            ]
        },
    )
    monkeypatch.setattr(
        fv,
        "_fetch_positions_raw",
        lambda w, key=None, hip3_dexes=(): [
            {"symbol": "BTC/USDC:USDC", "contracts": 2.0, "side": "long", "markPrice": 1.0}
        ],
    )

    state = fv._build_reconciliation()
    assert state["multi_address"] is False
    assert len(state["coins"]) == 1
    entry = state["coins"][0]
    assert entry["db_sum"] == 2.0 and entry["on_chain"] == 2.0
    assert entry["status"] == "ok"


# --------------------------------------------------------------------------- #
# 5. reconcile_positions.py / monitor_429_orphans.py — alert noise only
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def rp():
    return _load("_rp_under_test", "reconcile_positions.py")


def _rp_db(tmp_path, name, rows):
    db = tmp_path / f"{name}.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE trades (pair TEXT, amount REAL, is_short INTEGER, is_open INTEGER)")
    conn.executemany("INSERT INTO trades VALUES (?, ?, ?, 1)", rows)
    conn.commit()
    conn.close()
    return db


def test_reconcile_positions_two_addresses_report_no_drift(rp, tmp_path, monkeypatch):
    cfgs = [
        {
            "dry_run": False,
            "bot_name": "a",
            "exchange": {"walletAddress": WALLET_A},
            "db_url": "sqlite:///" + str(_rp_db(tmp_path, "a", [("ETH/USDC:USDC", 5.0, 0)])),
        },
        {
            "dry_run": False,
            "bot_name": "b",
            "exchange": {"walletAddress": WALLET_B},
            "db_url": "sqlite:///" + str(_rp_db(tmp_path, "b", [("BTC/USDC:USDC", 2.0, 0)])),
        },
    ]
    monkeypatch.setattr(rp, "_running_bot_configs", lambda: cfgs)
    monkeypatch.setattr(
        rp,
        "_hl_positions",
        lambda wd=None: (
            {(WALLET_A.lower(), "ETH"): 5.0, (WALLET_B.lower(), "BTC"): 2.0},
            {"ETH": 100.0, "BTC": 100.0},
        ),
    )
    r = rp._reconcile_once()
    assert r["orphans"] == [] and r["phantoms"] == []
    assert r["ok"] == 2
    assert len(r["wallets"]) == 2


def test_reconcile_positions_single_address_still_flags_the_orphan(rp, tmp_path, monkeypatch):
    cfgs = [
        {
            "dry_run": False,
            "bot_name": "a",
            "exchange": {"walletAddress": WALLET_A},
            "db_url": "sqlite:///" + str(_rp_db(tmp_path, "s", [("ETH/USDC:USDC", 5.0, 0)])),
        },
    ]
    monkeypatch.setattr(rp, "_running_bot_configs", lambda: cfgs)
    # Mark prices matter: the dust floor is expressed in stake currency.
    monkeypatch.setattr(
        rp,
        "_hl_positions",
        lambda wd=None: (
            {(WALLET_A.lower(), "ETH"): 5.0, (WALLET_A.lower(), "BTC"): 3.0},
            {"ETH": 100.0, "BTC": 100.0},
        ),
    )
    r = rp._reconcile_once()
    assert [(c, w) for c, _d, w in r["orphans"]] == [("BTC", WALLET_A.lower())]
    assert r["phantoms"] == []


def test_reconcile_positions_unresolved_bot_is_skipped(rp, tmp_path, monkeypatch):
    cfgs = [
        {
            "dry_run": False,
            "bot_name": "ghost",
            "exchange": {},
            "db_url": "sqlite:///" + str(_rp_db(tmp_path, "g", [("BTC/USDC:USDC", 2.0, 0)])),
        },
    ]
    monkeypatch.setattr(rp, "_running_bot_configs", lambda: cfgs)
    monkeypatch.setattr(rp, "_hl_positions", lambda wd=None: ({}, {}))
    r = rp._reconcile_once()
    assert r["unresolved_bots"] == ["ghost"]
    assert r["orphans"] == [] and r["phantoms"] == []


def test_reconcile_positions_never_reads_a_private_key():
    src = (REPO / "reconcile_positions.py").read_text()
    assert "privateKey" not in src
    assert "_hyperliquid_freqtrade_access" not in src


@pytest.fixture(scope="module")
def mon():
    return _load("_mon_under_test", "user_data/monitor_429_orphans.py")


def test_monitor_429_db_net_is_keyed_by_address(mon, tmp_path):
    db_a = _rp_db(tmp_path, "ma", [("ETH/USDC:USDC", 5.0, 0)])
    db_b = _rp_db(tmp_path, "mb", [("BTC/USDC:USDC", 2.0, 0)])
    dbmap = {
        "a": {"db": str(db_a), "wallet": WALLET_A.lower(), "hip3": []},
        "b": {"db": str(db_b), "wallet": WALLET_B.lower(), "hip3": ["xyz"]},
        "ghost": {"db": str(db_a), "wallet": None, "hip3": []},
    }
    net, detail, skipped = mon.db_net_positions(dbmap)
    assert net == {(WALLET_A.lower(), "ETH"): 5.0, (WALLET_B.lower(), "BTC"): 2.0}
    assert skipped == ["ghost"]
    assert mon.wallet_dexes(dbmap) == {WALLET_A.lower(): [], WALLET_B.lower(): ["xyz"]}


def test_monitor_429_single_address_unchanged(mon, tmp_path):
    """Non-regression: one address, the net is the same collective sum as before."""
    db_a = _rp_db(tmp_path, "sa", [("ETH/USDC:USDC", 5.0, 0)])
    db_b = _rp_db(tmp_path, "sb", [("ETH/USDC:USDC", 3.0, 1)])
    dbmap = {
        "a": {"db": str(db_a), "wallet": WALLET_A.lower(), "hip3": []},
        "b": {"db": str(db_b), "wallet": WALLET_A.lower(), "hip3": []},
    }
    net, detail, skipped = mon.db_net_positions(dbmap)
    assert net == {(WALLET_A.lower(), "ETH"): 2.0}
    assert skipped == []
    assert len(detail[(WALLET_A.lower(), "ETH")]) == 2


def test_monitor_429_never_reads_a_private_key():
    src = (REPO / "user_data" / "monitor_429_orphans.py").read_text()
    assert "privateKey" not in src
    assert "_hyperliquid_freqtrade_access" not in src
