"""Tests for the position audit trail: ledger, attestation, break register, identity."""

import json

import pytest

from freqtrade.order_identity import (
    bot_fingerprint,
    is_freqtrade_order,
    is_ours,
    new_client_order_id,
)
from freqtrade.position_audit import (
    AuditLedger,
    BreakRegister,
    build_attestation,
    read_book,
    record_attestation,
    safe_ledger_name,
)


# ------------------------------------------------------------------ order identity


def test_fingerprint_is_stable_across_calls():
    cfg = {"bot_name": "alpha", "db_url": "sqlite:///a.sqlite"}
    assert bot_fingerprint(cfg) == bot_fingerprint(dict(cfg))


def test_distinct_bots_get_distinct_fingerprints():
    a = bot_fingerprint({"bot_name": "alpha", "db_url": "sqlite:///a.sqlite"})
    b = bot_fingerprint({"bot_name": "beta", "db_url": "sqlite:///b.sqlite"})
    assert a != b


def test_minted_id_matches_hyperliquid_format():
    cid = new_client_order_id(bot_fingerprint({"bot_name": "x", "db_url": "y"}))
    assert cid.startswith("0x")
    assert len(cid) == 34  # 0x + 128 bits of hex
    int(cid, 16)  # must parse as hex


def test_our_order_is_recognised_and_a_siblings_is_not():
    mine = bot_fingerprint({"bot_name": "alpha", "db_url": "sqlite:///a.sqlite"})
    theirs = bot_fingerprint({"bot_name": "beta", "db_url": "sqlite:///b.sqlite"})
    cid = new_client_order_id(mine)
    assert is_ours(cid, mine) is True
    assert is_ours(cid, theirs) is False


@pytest.mark.parametrize("bad", [None, "", "0x", "not-hex", "0x1234"])
def test_malformed_ids_are_never_ours(bad):
    """Anything we cannot parse must read as 'not mine' — claiming is the costly error."""
    assert is_ours(bad, "deadbeef") is False


def test_foreign_order_is_not_mistaken_for_a_freqtrade_one():
    assert is_freqtrade_order(new_client_order_id("abcd1234")) is True
    assert is_freqtrade_order("some-exchange-ui-order") is False


# ------------------------------------------------------------------ ledger


def test_ledger_chains_entries(tmp_path):
    led = AuditLedger(tmp_path / "l.jsonl")
    led.append("fill", coin="ETH", signed_amount=-1.0)
    led.append("fill", coin="ETH", signed_amount=-0.5)
    entries = list(led.entries())
    assert [e["seq"] for e in entries] == [1, 2]
    assert entries[1]["prev"] == entries[0]["hash"]
    assert led.verify()[0] is True


def test_ledger_detects_edited_content(tmp_path):
    path = tmp_path / "l.jsonl"
    led = AuditLedger(path)
    led.append("fill", coin="ETH", signed_amount=-1.0)
    led.append("fill", coin="ETH", signed_amount=-0.5)
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[0])
    tampered["signed_amount"] = -99.0
    lines[0] = json.dumps(tampered, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    ok, verdict = AuditLedger(path).verify()
    assert ok is False and "altéré" in verdict


def test_ledger_detects_a_deleted_entry(tmp_path):
    path = tmp_path / "l.jsonl"
    led = AuditLedger(path)
    for i in range(3):
        led.append("fill", coin="ETH", signed_amount=-float(i + 1))
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert AuditLedger(path).verify()[0] is False


def test_ledger_rebuilds_the_book_as_of_a_past_instant(tmp_path):
    led = AuditLedger(tmp_path / "l.jsonl")
    first = led.append("fill", coin="ETH", signed_amount=-1.0)
    led.append("fill", coin="ETH", signed_amount=-0.5)
    assert led.replay_positions() == pytest.approx({"ETH": -1.5})
    assert led.replay_positions(as_of=first["ts"]) == pytest.approx({"ETH": -1.0})


def test_a_write_failure_never_raises(tmp_path):
    led = AuditLedger(tmp_path / "l.jsonl")
    led.path = tmp_path / "nope" / "deep" / "l.jsonl"  # parent never created
    led.path.parent.parent.mkdir(parents=True, exist_ok=True)
    led.path.parent.touch()  # a FILE where a directory is needed
    assert led.append("fill", coin="ETH", signed_amount=-1.0) == {}


def test_safe_ledger_name_strips_path_separators():
    assert "/" not in safe_ledger_name("a/b")
    assert safe_ledger_name("") == "freqtrade"


# ------------------------------------------------------------------ attestation


def test_matching_books_are_iso():
    att = build_attestation({"ETH": -1.0}, {"bot": {"ETH": -1.0}})
    assert att.clean is True
    assert att.breaks == []


def test_position_nobody_claims_is_an_orphan():
    att = build_attestation({"ETH": -1.0}, {"bot": {}})
    assert [b.status for b in att.breaks] == ["ORPHAN"]


def test_book_without_counterpart_is_a_phantom():
    att = build_attestation({}, {"bot": {"ETH": -1.0}})
    assert [b.status for b in att.breaks] == ["PHANTOM"]


def test_several_books_are_summed_before_comparing():
    """The wallet is netted: two bots holding half each is not a discrepancy."""
    att = build_attestation({"ETH": -1.0}, {"a": {"ETH": -0.5}, "b": {"ETH": -0.5}})
    assert att.clean is True


def test_unreadable_book_makes_the_attestation_unclean():
    att = build_attestation({"ETH": -1.0}, {"bot": {"ETH": -1.0}}, unreadable=["other"])
    assert att.clean is False, "an audit that skipped a book cannot declare itself clean"


def test_rounding_is_not_a_break():
    att = build_attestation({"ETH": -1.0}, {"bot": {"ETH": -1.005}})
    assert att.clean is True


def test_reads_a_real_book(tmp_path):
    import sqlite3

    db = tmp_path / "t.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE trades (pair TEXT, amount REAL, is_short INT, is_open INT)")
    conn.executemany(
        "INSERT INTO trades VALUES (?,?,?,?)",
        [("ETH/USDC:USDC", 1.5, 1, 1), ("BTC/USDC:USDC", 0.5, 0, 1), ("SOL/USDC:USDC", 9, 1, 0)],
    )
    conn.commit()
    conn.close()
    assert read_book(db) == pytest.approx({"ETH": -1.5, "BTC": 0.5})


# ------------------------------------------------------------------ break register


def test_break_is_opened_once_and_closed_when_it_disappears(tmp_path):
    led = AuditLedger(tmp_path / "l.jsonl")
    reg = BreakRegister(led)

    drifted = build_attestation({"ETH": -1.0}, {"bot": {}})
    assert reg.reconcile(drifted)[0] == ["ETH"]
    # still there on the next run: not re-opened, just still open
    opened, still_open, closed = reg.reconcile(drifted)
    assert opened == [] and still_open == ["ETH"] and closed == []
    # resolved
    assert reg.reconcile(build_attestation({}, {}))[2] == ["ETH"]
    assert reg.aging() == []


def test_resolved_breaks_stay_in_the_history(tmp_path):
    """'It was fixed' and 'it never happened' must not look alike."""
    led = AuditLedger(tmp_path / "l.jsonl")
    reg = BreakRegister(led)
    reg.reconcile(build_attestation({"ETH": -1.0}, {"bot": {}}))
    reg.reconcile(build_attestation({}, {}))
    events = [e["event"] for e in led.entries()]
    assert "break_opened" in events and "break_closed" in events


def test_open_breaks_are_aged(tmp_path):
    led = AuditLedger(tmp_path / "l.jsonl")
    reg = BreakRegister(led)
    reg.reconcile(build_attestation({"ETH": -1.0}, {"bot": {}}))
    aging = reg.aging()
    assert len(aging) == 1 and aging[0]["coin"] == "ETH" and aging[0]["age_hours"] >= 0


def test_attestation_is_itself_recorded(tmp_path):
    led = AuditLedger(tmp_path / "l.jsonl")
    record_attestation(led, build_attestation({"ETH": -1.0}, {"bot": {"ETH": -1.0}}))
    assert [e["event"] for e in led.entries()] == ["attestation"]
    assert led.verify()[0] is True


# ------------------------------------------------------------------ contract probes


def test_probe_passes_when_the_exchange_still_echoes_client_order_ids():
    from freqtrade.position_audit.contract_probe import probe_client_order_id_support

    class Api:
        @staticmethod
        def parse_order(_raw):
            return {"clientOrderId": "0x" + "a" * 32}

    class Ex:
        _api = Api()

        @staticmethod
        def get_option(name, default=None):
            return True if name == "supports_client_order_id" else default

    assert probe_client_order_id_support(Ex()).ok is True


def test_probe_fails_when_the_echo_disappears():
    """The regression this exists to catch: ccxt silently stops exposing the field."""
    from freqtrade.position_audit.contract_probe import probe_client_order_id_support

    class Api:
        @staticmethod
        def parse_order(_raw):
            return {}  # field gone

    class Ex:
        _api = Api()

        @staticmethod
        def get_option(name, default=None):
            return True if name == "supports_client_order_id" else default

    res = probe_client_order_id_support(Ex())
    assert res.ok is False and "clientOrderId" in res.detail


def test_probe_is_not_applicable_on_exchanges_without_the_capability():
    from freqtrade.position_audit.contract_probe import probe_client_order_id_support

    class Ex:
        @staticmethod
        def get_option(_name, default=None):
            return False

    assert probe_client_order_id_support(Ex()).ok is True


def test_probe_flags_positions_missing_the_fields_reconciliation_reads():
    from freqtrade.position_audit.contract_probe import probe_position_shape

    assert probe_position_shape([{"symbol": "ETH", "contracts": 1, "side": "short"}]).ok is True
    bad = probe_position_shape([{"symbol": "ETH", "contracts": 1}])
    assert bad.ok is False and "side" in bad.detail


def test_probe_flags_a_snapshot_timestamped_in_the_future():
    import time

    from freqtrade.position_audit.contract_probe import probe_snapshot_freshness

    class Ex:
        @staticmethod
        def positions_snapshot_wall_ts():
            return time.time() + 3600

    assert probe_snapshot_freshness(Ex()).ok is False


def test_a_probe_that_itself_explodes_is_reported_not_raised():
    from freqtrade.position_audit import run_probes

    class Ex:
        def get_option(self, *_a, **_k):
            raise RuntimeError("boom")

    results = run_probes(Ex(), [])
    assert any(not r.ok for r in results)
