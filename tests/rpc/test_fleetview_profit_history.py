"""Fork-specific: fleet-wide, decimated profit_history aggregate.

Covers the three properties that matter operationally: the payload never carries
a secret, one broken ledger never kills the response, and the decimation keeps
the curve's ends exact.
"""

import gzip
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from freqtrade.rpc.api_server import api_fleetview as fv


SECRET_KEY_RE = re.compile(r"password|passwd|api_pw|private_key|privatekey|secret|wallet|token")


def _make_db(path, n=1000, start=None, closed_base=0.0):
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE profit_history (id INTEGER PRIMARY KEY, timestamp DATETIME NOT NULL, "
        "profit_closed_abs FLOAT NOT NULL, profit_open_abs FLOAT NOT NULL, "
        "open_trades INTEGER NOT NULL)"
    )
    rows = [
        (
            (start + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S.%f"),
            closed_base + i,
            -i / 2,
            i % 3,
        )
        for i in range(n)
    ]
    con.executemany(
        "INSERT INTO profit_history (timestamp, profit_closed_abs, profit_open_abs, open_trades) "
        "VALUES (?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    return rows


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Two running bots + one stopped ledger on disk, no real processes."""
    dbdir = tmp_path / "database"
    dbdir.mkdir()
    rows_a = _make_db(dbdir / "alpha.sqlite", n=1000)
    _make_db(dbdir / "beta.sqlite", n=7)
    _make_db(dbdir / "gamma.sqlite", n=40)  # stopped: no process claims it
    (dbdir / "alpha.sqlite.backup").write_bytes(b"junk")
    _make_db(dbdir / "alpha-backup.sqlite", n=5)  # must be skipped by name

    bots = [
        {
            "bot_name": "alpha",
            "db_path": str(dbdir / "alpha.sqlite"),
            "port": 9400,
            "dry_run": False,
            "strategy": "StratA",
            "api_user": "u",
            "api_pw": "sup3rs3cret",
            "wallet": "0xC234deadbeef",
            "private_key": "0xdeadbeefcafe",
            "available_capital": 150,
            "capital_withdrawal": 0,
            "config_file": "a.json",
            "pid": 1,
            "process_start": 0,
        },
        {
            "bot_name": "beta",
            "db_path": str(dbdir / "beta.sqlite"),
            "port": 9401,
            "dry_run": True,
            "strategy": "StratB",
            "api_user": "u",
            "api_pw": "otherpw",
            "wallet": None,
            "private_key": None,
            "available_capital": 50,
            "capital_withdrawal": 0,
            "config_file": "b.json",
            "pid": 2,
            "process_start": 0,
        },
    ]
    monkeypatch.setattr(fv, "discover_bots", lambda: bots)
    return {"dbdir": dbdir, "bots": bots, "rows_a": rows_a}


class TestPayload:
    def test_all_bots_present_running_and_stopped(self, fleet):
        p = fv._ph_fleet_payload(0, 300)
        names = {b["bot_name"]: b for b in p["bots"]}
        assert set(names) == {"alpha", "beta", "gamma"}
        assert names["alpha"]["state"] == "running"
        assert names["alpha"]["port"] == 9400
        assert names["gamma"]["state"] == "stopped"
        assert names["gamma"]["port"] is None
        assert names["gamma"]["length"] == 40
        assert p["errors"] == {}
        assert p["truncated"] is False

    def test_backup_named_ledgers_skipped(self, fleet):
        p = fv._ph_fleet_payload(0, 300)
        assert "alpha-backup" not in {b["bot_name"] for b in p["bots"]}
        assert "alpha-backup" not in p["errors"]

    def test_decimation_targets_point_budget(self, fleet):
        p = fv._ph_fleet_payload(0, 100)
        alpha = next(b for b in p["bots"] if b["bot_name"] == "alpha")
        assert alpha["total"] == 1000
        assert 90 <= alpha["length"] <= 115, alpha["length"]

    def test_decimation_keeps_both_ends_exact(self, fleet):
        rows = fleet["rows_a"]
        p = fv._ph_fleet_payload(0, 50)
        alpha = next(b for b in p["bots"] if b["bot_name"] == "alpha")
        assert alpha["data"][0][1] == rows[0][1]
        assert alpha["data"][-1][1] == rows[-1][1]
        assert alpha["last_ts"] == alpha["data"][-1][0]

    def test_points_sorted_and_shaped(self, fleet):
        p = fv._ph_fleet_payload(0, 50)
        for bot in p["bots"]:
            ts = [row[0] for row in bot["data"]]
            assert ts == sorted(ts)
            assert all(len(row) == 4 for row in bot["data"])

    def test_short_series_returned_whole(self, fleet):
        p = fv._ph_fleet_payload(0, 300)
        beta = next(b for b in p["bots"] if b["bot_name"] == "beta")
        assert beta["length"] == 7 == beta["total"]

    def test_since_is_strict_and_incremental(self, fleet):
        full = fv._ph_fleet_payload(0, 5000)
        alpha = next(b for b in full["bots"] if b["bot_name"] == "alpha")
        cut = alpha["data"][-3][0]
        inc = fv._ph_fleet_payload(cut, 5000)
        alpha_inc = next(b for b in inc["bots"] if b["bot_name"] == "alpha")
        assert alpha_inc["total"] == 2  # strictly after: the cut sample is excluded
        assert all(row[0] > cut for row in alpha_inc["data"])

    def test_since_beyond_end_yields_empty_series(self, fleet):
        p = fv._ph_fleet_payload(4_000_000_000_000, 300)
        assert all(b["length"] == 0 for b in p["bots"])
        assert p["errors"] == {}


class TestResilience:
    def test_corrupt_db_is_isolated(self, fleet):
        (fleet["dbdir"] / "beta.sqlite").write_bytes(b"this is not a database at all")
        p = fv._ph_fleet_payload(0, 300)
        assert {b["bot_name"] for b in p["bots"]} == {"alpha", "gamma"}
        assert p["errors"]["beta"] in ("db_corrupt", "db_error")

    def test_missing_table_is_isolated(self, fleet):
        path = fleet["dbdir"] / "beta.sqlite"
        con = sqlite3.connect(path)
        con.execute("DROP TABLE profit_history")
        con.commit()
        con.close()
        p = fv._ph_fleet_payload(0, 300)
        assert p["errors"]["beta"] == "no_history_table"
        assert any(b["bot_name"] == "alpha" for b in p["bots"])

    def test_missing_file_is_isolated(self, fleet):
        (fleet["dbdir"] / "beta.sqlite").unlink()
        p = fv._ph_fleet_payload(0, 300)
        assert p["errors"]["beta"] == "db_missing"

    def test_budget_exhaustion_is_reported_not_raised(self, fleet, monkeypatch):
        monkeypatch.setattr(fv, "PH_BUDGET_S", -1.0)
        p = fv._ph_fleet_payload(0, 300)
        assert p["bots"] == []
        assert p["truncated"] is True
        assert set(p["errors"].values()) == {"budget_exhausted"}

    @pytest.mark.parametrize(
        "exc,code",
        [
            (sqlite3.OperationalError("database is locked"), "db_locked"),
            (sqlite3.OperationalError("database table is busy"), "db_locked"),
            (sqlite3.OperationalError("no such table: profit_history"), "no_history_table"),
            (sqlite3.OperationalError("unable to open database file"), "db_corrupt"),
            (sqlite3.OperationalError("file is not a database"), "db_corrupt"),
            (sqlite3.OperationalError("syntax error"), "db_error"),
            (sqlite3.DatabaseError("malformed"), "db_corrupt"),
            (TimeoutError("slow"), "timeout"),
            (RuntimeError("boom"), "db_error"),
        ],
    )
    def test_error_codes_are_normalised(self, exc, code):
        assert fv._ph_error_code(exc) == code

    def test_error_values_never_leak_paths(self, fleet, monkeypatch):
        secret = "sqlite:////srv/u:p@ss@/database/alpha.sqlite"

        def boom(*_a, **_k):
            raise sqlite3.OperationalError(f"unable to open database file {secret}")

        monkeypatch.setattr(fv, "_ph_read_one", boom)
        p = fv._ph_fleet_payload(0, 300)
        blob = json.dumps(p)
        assert "p@ss" not in blob
        assert "/srv/u" not in blob
        assert set(p["errors"].values()) == {"db_corrupt"}


class TestSecurity:
    def test_no_secret_key_anywhere_in_payload(self, fleet):
        """Whitelist proof: no key of the payload matches a secret-ish name."""
        p = fv._ph_fleet_payload(0, 300)
        seen: list[str] = []

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    seen.append(k)
                    assert not SECRET_KEY_RE.search(k.lower()), f"secret-ish key {k!r} in payload"
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(p)
        assert "bot_name" in seen  # the walk really visited something

    def test_no_secret_value_anywhere_in_payload(self, fleet):
        p = fv._ph_fleet_payload(0, 300)
        blob = json.dumps(p)
        for needle in ("sup3rs3cret", "otherpw", "0xC234deadbeef", "0xdeadbeefcafe", "0x"):
            assert needle not in blob, needle

    def test_endpoint_not_on_the_public_router(self):
        from freqtrade.rpc.api_server.api_v1 import router_public

        paths = {r.path for r in router_public.routes}
        assert paths == {"/ping"}
        assert "/fleetview/profit_history" in {r.path for r in fv.router.routes}


class TestHttpSemantics:
    def _call(self, headers=None, since=0, points=300):
        from starlette.requests import Request

        raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        req = Request({"type": "http", "method": "GET", "headers": raw})
        return fv.fleetview_profit_history(req, since=since, points=points)

    def test_gzip_when_accepted(self, fleet):
        resp = self._call({"accept-encoding": "gzip"})
        assert resp.headers["content-encoding"] == "gzip"
        payload = json.loads(gzip.decompress(resp.body))
        assert payload["bot_count"] == 3

    def test_plain_when_not_accepted(self, fleet):
        resp = self._call()
        assert "content-encoding" not in resp.headers
        assert json.loads(resp.body)["bot_count"] == 3

    def test_gzip_shrinks_the_payload(self, fleet):
        plain = self._call()
        gz = self._call({"accept-encoding": "gzip"})
        assert len(gz.body) < len(plain.body) / 2

    def test_etag_then_304(self, fleet):
        first = self._call()
        etag = first.headers["etag"]
        second = self._call({"if-none-match": etag})
        assert second.status_code == 304
        assert second.body == b""
        assert second.headers["etag"] == etag

    def test_etag_moves_when_a_bot_gets_a_new_sample(self, fleet):
        first = self._call()
        con = sqlite3.connect(fleet["dbdir"] / "beta.sqlite")
        con.execute(
            "INSERT INTO profit_history (timestamp, profit_closed_abs, profit_open_abs, "
            "open_trades) VALUES ('2027-01-01 00:00:00.000000', 42.0, 0.0, 1)"
        )
        con.commit()
        con.close()
        second = self._call()
        assert second.headers["etag"] != first.headers["etag"]

    def test_etag_depends_on_query_params(self, fleet):
        assert self._call(points=300).headers["etag"] != self._call(points=50).headers["etag"]
        assert self._call(since=0).headers["etag"] != self._call(since=1).headers["etag"]
