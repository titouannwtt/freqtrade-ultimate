"""Tests for the shared-wallet (netting) instrumentation and the
``never_block_entries`` coordination override.

Two guarantees are under test:

1. a bot that sets ``position_coordination.never_block_entries`` is never refused an
   entry by coordination — in any mode, under any leverage policy, and even when
   sibling discovery fails — while the refusal it would have received stays visible
   on the Decision;
2. the override never changes the coin's leverage, so a sibling's open position is
   never disturbed by it.

Plus the recording side: classification, counters, and the per-trade stamp.
"""

import sqlite3

import pytest

from freqtrade.fleet_coordination import PositionCoordinator
from freqtrade.netting_metrics import CLEAN, SHADOWED, STACKED, NettingMetrics, classify


PAIR = "XYZ-META/USDC:USDC"


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


def _cfg(tmp_path, *, mode, sibling_dbs=(), leverage_policy="keep", never_block=False):
    return {
        "bot_name": "me",
        "trading_mode": "futures",
        "dry_run": False,
        "exchange": {"name": "hyperliquid"},
        "user_data_dir": str(tmp_path / "ud"),
        "position_coordination": {
            "mode": mode,
            "leverage_policy": leverage_policy,
            "never_block_entries": never_block,
            "registry": [str(p) for p in sibling_dbs],
        },
    }


# --------------------------------------------------------------------------- #
# never_block_entries
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["strict", "compat"])
def test_never_block_allows_opposite_side_entry(tmp_path, mode):
    """A sibling short on the coin must not stop our long once the flag is on."""
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 1, 2.0, 1)])

    blocking = PositionCoordinator(_cfg(tmp_path, mode=mode, sibling_dbs=[db]))
    refused = blocking.evaluate(PAIR, is_short=False, my_leverage=2.0)
    assert refused.allow is False
    assert refused.overridden is False

    permissive = PositionCoordinator(
        _cfg(tmp_path, mode=mode, sibling_dbs=[db], never_block=True)
    )
    allowed = permissive.evaluate(PAIR, is_short=False, my_leverage=2.0)
    assert allowed.allow is True
    assert allowed.overridden is True
    assert allowed.blocked_reason == refused.reason
    # The coin is never touched: no exchange leverage change is requested.
    assert allowed.leverage_changed is False


def test_never_block_adopts_the_coin_leverage_without_changing_it(tmp_path):
    """cap refuses a coin sitting above us; the override opens at the coin's leverage."""
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 1, 4.0, 1)])

    capped = PositionCoordinator(
        _cfg(tmp_path, mode="compat", sibling_dbs=[db], leverage_policy="cap")
    )
    assert capped.evaluate(PAIR, is_short=True, my_leverage=2.0).allow is False

    permissive = PositionCoordinator(
        _cfg(
            tmp_path,
            mode="compat",
            sibling_dbs=[db],
            leverage_policy="cap",
            never_block=True,
        )
    )
    decision = permissive.evaluate(PAIR, is_short=True, my_leverage=2.0)
    assert decision.allow is True
    assert decision.overridden is True
    # 4x is what the venue will actually execute at on a wallet-level per-coin leverage.
    assert decision.leverage == 4.0
    assert decision.leverage_changed is False


def test_keep_policy_never_refuses_and_never_moves_the_coin(tmp_path):
    """`keep` is the only policy that can neither refuse nor disturb a sibling."""
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 1, 4.0, 1)])
    coord = PositionCoordinator(
        _cfg(tmp_path, mode="compat", sibling_dbs=[db], leverage_policy="keep")
    )
    decision = coord.evaluate(PAIR, is_short=True, my_leverage=2.0)
    assert decision.allow is True
    assert decision.leverage == 4.0
    assert decision.leverage_changed is False


def test_lowest_policy_would_move_the_coin(tmp_path):
    """Counter-example that justifies `keep`: `lowest` asks to change the coin."""
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 1, 4.0, 1)])
    coord = PositionCoordinator(
        _cfg(tmp_path, mode="compat", sibling_dbs=[db], leverage_policy="lowest")
    )
    decision = coord.evaluate(PAIR, is_short=True, my_leverage=2.0)
    assert decision.allow is True
    assert decision.leverage == 2.0
    assert decision.leverage_changed is True  # would re-leverage the sibling's position


def test_never_block_survives_a_failed_discovery(tmp_path, mocker):
    """Fail-closed on a broken registry must also be overridden, or rare entries die."""
    db = tmp_path / "sib.sqlite"
    _make_db(db, [])
    coord = PositionCoordinator(
        _cfg(tmp_path, mode="compat", sibling_dbs=[db], never_block=True)
    )
    # Discovery swallows its own errors and hands back an empty list; only the flag
    # tells "empty fleet" apart from "could not look".
    mocker.patch.object(coord._registry, "siblings", return_value=[])
    coord._registry.last_discovery_failed = True
    decision = coord.evaluate(PAIR, is_short=False, my_leverage=2.0)
    assert decision.allow is True
    assert decision.overridden is True
    assert "fail-closed" in decision.blocked_reason
    assert decision.leverage == 2.0  # nothing readable -> keep what we asked for


def test_never_block_does_not_change_a_clean_decision(tmp_path):
    db = tmp_path / "sib.sqlite"
    _make_db(db, [])
    coord = PositionCoordinator(
        _cfg(tmp_path, mode="strict", sibling_dbs=[db], never_block=True)
    )
    decision = coord.evaluate(PAIR, is_short=False, my_leverage=2.0)
    assert decision.allow is True
    assert decision.overridden is False
    assert decision.blocked_reason == ""


# --------------------------------------------------------------------------- #
# sibling_snapshot
# --------------------------------------------------------------------------- #
def test_sibling_snapshot_reads_even_with_coordination_off(tmp_path):
    """Measurement must not depend on the decision engine being switched on."""
    db = tmp_path / "sib.sqlite"
    _make_db(db, [(PAIR, 1, 4.0, 1)])
    coord = PositionCoordinator(_cfg(tmp_path, mode="off", sibling_dbs=[db]))
    assert coord.evaluate(PAIR, is_short=False, my_leverage=2.0).allow is True
    snapshot = coord.sibling_snapshot(PAIR)
    assert len(snapshot) == 1
    assert snapshot[0]["side"] == "short"
    assert snapshot[0]["lev"] == 4.0


def test_sibling_snapshot_never_raises(tmp_path, mocker):
    coord = PositionCoordinator(_cfg(tmp_path, mode="compat"))
    mocker.patch.object(coord, "_sibling_positions", side_effect=RuntimeError("nope"))
    assert coord.sibling_snapshot(PAIR) == []


# --------------------------------------------------------------------------- #
# NettingMetrics
# --------------------------------------------------------------------------- #
def test_classify():
    assert classify(False, []) == CLEAN
    assert classify(False, [{"bot": "b", "side": "short", "lev": 2.0}]) == SHADOWED
    assert classify(False, [{"bot": "b", "side": "long", "lev": 2.0}]) == STACKED
    assert classify(True, [{"bot": "b", "side": "short", "lev": 2.0}]) == STACKED


def test_metrics_counts_and_context():
    m = NettingMetrics("hippo")
    m.record_entry_attempt(PAIR, False, [], leverage=2.0)
    ctx = m.record_entry_attempt(
        "BTC/USDC:USDC",
        False,
        [{"bot": "sib", "side": "short", "lev": 4.0}],
        leverage=4.0,
        overridden=True,
        blocked_reason="netting risk",
    )
    assert ctx["state"] == SHADOWED
    assert ctx["coordination_override"] == "netting risk"
    assert m.counters["entries"] == 2
    assert m.counters["entries_clean"] == 1
    assert m.counters["entries_shadowed"] == 1
    assert m.counters["entries_overridden"] == 1

    m.record_external_close(PAIR)
    m.record_capped_exit(PAIR)
    assert m.counters["exits_external_close"] == 1
    assert m.counters["exits_capped"] == 1
    assert "1 clean" in m.summary_line()


def test_metrics_stamps_trade_once():
    class FakeTrade:
        def __init__(self):
            self.data = {}

        def set_custom_data(self, key, value):
            self.data[key] = value

    m = NettingMetrics("hippo")
    m.record_entry_attempt(PAIR, True, [{"bot": "sib", "side": "long", "lev": 2.0}], leverage=2.0)
    trade = FakeTrade()
    m.stamp_trade(trade, PAIR)
    assert trade.data["netting"]["state"] == SHADOWED
    # Context is consumed: a second stamp (e.g. a DCA order) must not re-write it.
    other = FakeTrade()
    m.stamp_trade(other, PAIR)
    assert other.data == {}


def test_metrics_stamp_swallows_errors():
    class Exploding:
        def set_custom_data(self, key, value):
            raise RuntimeError("db down")

    m = NettingMetrics("hippo")
    m.record_entry_attempt(PAIR, False, [], leverage=1.0)
    m.stamp_trade(Exploding(), PAIR)  # must not raise


def test_summary_is_throttled_and_silent_when_empty():
    m = NettingMetrics("hippo")
    assert m.maybe_log_summary(interval_s=0) is False  # nothing happened yet
    m.record_entry_attempt(PAIR, False, [], leverage=1.0)
    assert m.maybe_log_summary(interval_s=0) is True
    assert m.maybe_log_summary(interval_s=3600) is False
