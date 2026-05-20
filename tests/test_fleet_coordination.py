"""Tests for the fleet position coordination extension."""

import json
import sqlite3
import threading
import time

import pytest

from freqtrade.fleet_coordination import (
    FleetRegistry,
    PositionCoordinator,
    SiblingPositionReader,
)


def _make_db(path, rows):
    """rows: iterable of (pair, is_short, leverage, is_open)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE trades (pair TEXT, is_short INTEGER, leverage REAL, is_open INTEGER)"
    )
    conn.executemany(
        "INSERT INTO trades (pair, is_short, leverage, is_open) VALUES (?, ?, ?, ?)",
        list(rows),
    )
    conn.commit()
    conn.close()


def _cfg(
    tmp_path,
    *,
    mode,
    sibling_dbs=(),
    leverage_policy="keep",
    trading_mode="futures",
    dry_run=False,
    exchange_check="warn",
    bot_name="me",
    exchange="hyperliquid",
):
    return {
        "bot_name": bot_name,
        "trading_mode": trading_mode,
        "dry_run": dry_run,
        "exchange": {"name": exchange},
        "user_data_dir": str(tmp_path / "ud"),
        "position_coordination": {
            "mode": mode,
            "leverage_policy": leverage_policy,
            "exchange_check": exchange_check,
            "registry": [str(p) for p in sibling_dbs],
        },
    }


PAIR = "SOL/USDC:USDC"


# --------------------------------------------------------------------------- #
# SiblingPositionReader
# --------------------------------------------------------------------------- #
def test_reader_returns_only_open_on_pair(tmp_path):
    db = tmp_path / "sib.sqlite"
    _make_db(
        db,
        [
            (PAIR, 1, 3.0, 1),       # open short 3x on our pair
            (PAIR, 0, 2.0, 0),       # closed -> ignored
            ("ETH/USDC:USDC", 1, 5.0, 1),  # other pair -> ignored
        ],
    )
    rows = SiblingPositionReader.read_open_on_pair(db, PAIR)
    assert rows == [(True, 3.0)]


def test_reader_missing_db_is_safe(tmp_path):
    assert SiblingPositionReader.read_open_on_pair(tmp_path / "nope.sqlite", PAIR) == []


# --------------------------------------------------------------------------- #
# Decision matrix
# --------------------------------------------------------------------------- #
def test_off_mode_always_allows(tmp_path):
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 0, 3.0, 1)])
    coord = PositionCoordinator(_cfg(tmp_path, mode="off", sibling_dbs=[db]))
    assert coord.evaluate(PAIR, is_short=False, my_leverage=3.0).allow


def test_no_siblings_allows(tmp_path):
    coord = PositionCoordinator(_cfg(tmp_path, mode="strict"))
    d = coord.evaluate(PAIR, is_short=False, my_leverage=3.0)
    assert d.allow and d.leverage == 3.0


def test_strict_blocks_when_sibling_holds_pair(tmp_path):
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 0, 3.0, 1)])
    coord = PositionCoordinator(_cfg(tmp_path, mode="strict", sibling_dbs=[db]))
    d = coord.evaluate(PAIR, is_short=False, my_leverage=3.0)
    assert not d.allow
    assert "strict" in d.reason


def test_compat_same_side_same_leverage_allows(tmp_path):
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 1, 3.0, 1)])
    coord = PositionCoordinator(
        _cfg(tmp_path, mode="compat", leverage_policy="keep", sibling_dbs=[db])
    )
    d = coord.evaluate(PAIR, is_short=True, my_leverage=3.0)
    assert d.allow and d.leverage == 3.0


def test_compat_opposite_side_blocks(tmp_path):
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 0, 3.0, 1)])  # sibling LONG
    coord = PositionCoordinator(_cfg(tmp_path, mode="compat", sibling_dbs=[db]))
    d = coord.evaluate(PAIR, is_short=True, my_leverage=3.0)  # we want SHORT
    assert not d.allow
    assert "OPPOSITE" in d.reason


@pytest.mark.parametrize(
    "policy,sibling_lev,my_lev,expected_lev,expected_changed",
    [
        ("lowest", 3.0, 2.0, 2.0, True),
        ("lowest", 2.0, 3.0, 2.0, False),
        ("highest", 3.0, 2.0, 3.0, False),
        ("highest", 2.0, 3.0, 3.0, True),
        ("keep", 5.0, 2.0, 5.0, False),
    ],
)
def test_compat_leverage_policies(
    tmp_path, policy, sibling_lev, my_lev, expected_lev, expected_changed
):
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 0, sibling_lev, 1)])
    coord = PositionCoordinator(
        _cfg(tmp_path, mode="compat", leverage_policy=policy, sibling_dbs=[db])
    )
    d = coord.evaluate(PAIR, is_short=False, my_leverage=my_lev)
    assert d.allow
    assert d.leverage == expected_lev
    assert d.leverage_changed is expected_changed


def test_compat_block_policy_blocks_on_mismatch(tmp_path):
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 0, 5.0, 1)])
    coord = PositionCoordinator(
        _cfg(tmp_path, mode="compat", leverage_policy="block", sibling_dbs=[db])
    )
    assert not coord.evaluate(PAIR, is_short=False, my_leverage=2.0).allow


def test_compat_block_policy_allows_on_match(tmp_path):
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 0, 3.0, 1)])
    coord = PositionCoordinator(
        _cfg(tmp_path, mode="compat", leverage_policy="block", sibling_dbs=[db])
    )
    assert coord.evaluate(PAIR, is_short=False, my_leverage=3.0).allow


# --------------------------------------------------------------------------- #
# Spot
# --------------------------------------------------------------------------- #
def test_spot_strict_blocks(tmp_path):
    db = tmp_path / "sib.sqlite"
    _make_db(db, [("BTC/USDC", 0, 1.0, 1)])
    coord = PositionCoordinator(
        _cfg(tmp_path, mode="strict", trading_mode="spot", sibling_dbs=[db])
    )
    assert not coord.evaluate("BTC/USDC", is_short=False, my_leverage=1.0).allow


def test_spot_compat_allows(tmp_path):
    db = tmp_path / "sib.sqlite"
    _make_db(db, [("BTC/USDC", 0, 1.0, 1)])
    coord = PositionCoordinator(
        _cfg(tmp_path, mode="compat", trading_mode="spot", sibling_dbs=[db])
    )
    assert coord.evaluate("BTC/USDC", is_short=False, my_leverage=1.0).allow


# --------------------------------------------------------------------------- #
# Intent markers (pre-commit visibility)
# --------------------------------------------------------------------------- #
def test_intent_marker_blocks_other_bot(tmp_path):
    # Bot A reserves the pair (writes an intent marker) before its trade hits the DB.
    coord_a = PositionCoordinator(_cfg(tmp_path, mode="strict", bot_name="A"))
    coord_a.mark_intent(PAIR, is_short=False, leverage=3.0)

    # Bot B (same env, same coordination dir) must see A's intent and block.
    coord_b = PositionCoordinator(_cfg(tmp_path, mode="strict", bot_name="B"))
    assert not coord_b.evaluate(PAIR, is_short=False, my_leverage=3.0).allow


def test_intent_marker_self_is_ignored(tmp_path):
    coord_a = PositionCoordinator(_cfg(tmp_path, mode="strict", bot_name="A"))
    coord_a.mark_intent(PAIR, is_short=False, leverage=3.0)
    # A reading its own marker must not block itself.
    assert coord_a.evaluate(PAIR, is_short=False, my_leverage=3.0).allow


def test_intent_marker_expired_ignored(tmp_path, monkeypatch):
    import freqtrade.fleet_coordination as fc

    coord_a = PositionCoordinator(_cfg(tmp_path, mode="strict", bot_name="A"))
    coord_a.mark_intent(PAIR, is_short=False, leverage=3.0)

    monkeypatch.setattr(fc, "INTENT_TTL_S", -1.0)  # everything is stale now
    coord_b = PositionCoordinator(_cfg(tmp_path, mode="strict", bot_name="B"))
    assert coord_b.evaluate(PAIR, is_short=False, my_leverage=3.0).allow


def test_clear_intent_unblocks(tmp_path):
    coord_a = PositionCoordinator(_cfg(tmp_path, mode="strict", bot_name="A"))
    coord_a.mark_intent(PAIR, is_short=False, leverage=3.0)
    coord_a.clear_intent(PAIR)
    coord_b = PositionCoordinator(_cfg(tmp_path, mode="strict", bot_name="B"))
    assert coord_b.evaluate(PAIR, is_short=False, my_leverage=3.0).allow


# --------------------------------------------------------------------------- #
# Anti-race lock
# --------------------------------------------------------------------------- #
def test_entry_lock_serializes_across_holders(tmp_path):
    coord = PositionCoordinator(_cfg(tmp_path, mode="strict"))
    events = []
    started = threading.Event()

    def first():
        with coord.entry_lock(PAIR):
            events.append("A-in")
            started.set()
            time.sleep(0.3)
            events.append("A-out")

    def second():
        started.wait()
        with coord.entry_lock(PAIR):
            events.append("B-in")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # B must only enter after A has fully released the lock.
    assert events == ["A-in", "A-out", "B-in"]


def test_entry_lock_noop_when_disabled(tmp_path):
    coord = PositionCoordinator(_cfg(tmp_path, mode="off"))
    with coord.entry_lock(PAIR):
        pass  # must not raise even though coordination is disabled


# --------------------------------------------------------------------------- #
# FleetRegistry directory auto-discovery
# --------------------------------------------------------------------------- #
def _write_cfg(directory, name, *, bot_name, exchange, dry_run, db):
    (directory / name).write_text(
        json.dumps(
            {
                "bot_name": bot_name,
                "exchange": {"name": exchange},
                "dry_run": dry_run,
                "db_url": f"sqlite:///{db}",
            }
        )
    )


def test_registry_filters_by_environment(tmp_path):
    cfgdir = tmp_path / "live_configs"
    cfgdir.mkdir()
    # self
    _write_cfg(cfgdir, "me.json", bot_name="me", exchange="hyperliquid", dry_run=False, db="me.sq")
    # same env (hyperliquid live) -> sibling
    _write_cfg(cfgdir, "peer.json", bot_name="peer", exchange="hyperliquid", dry_run=False,
               db="peer.sq")
    # dry on same exchange -> excluded
    _write_cfg(cfgdir, "drypeer.json", bot_name="drypeer", exchange="hyperliquid", dry_run=True,
               db="dry.sq")
    # other exchange -> excluded
    _write_cfg(cfgdir, "bn.json", bot_name="bn", exchange="binance", dry_run=False, db="bn.sq")
    # underscore-prefixed base -> skipped
    _write_cfg(cfgdir, "_default.json", bot_name="base", exchange="hyperliquid", dry_run=False,
               db="base.sq")

    config = {
        "bot_name": "me",
        "exchange": {"name": "hyperliquid"},
        "dry_run": False,
        "config_files": [str(cfgdir / "me.json")],
        "position_coordination": {},
    }
    reg = FleetRegistry(config)
    names = {bot for bot, _ in reg.siblings()}
    assert names == {"peer"}
