"""Tests for reset_db / first-real-trade cap / open-trade reconciliation + DB integrity."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freqtrade.replay import runner


def _make_db(path: Path, trades: list[tuple]) -> None:
    """trades: list of (id, open_date_iso, enter_tag, is_open)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, open_date TEXT, enter_tag TEXT, is_open INT)"
    )
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, ft_trade_id INT)")
    conn.execute("CREATE TABLE KeyValueStore (key TEXT, string_value TEXT)")
    conn.executemany("INSERT INTO trades VALUES (?,?,?,?)", trades)
    conn.execute("INSERT INTO orders VALUES (1, 1)")
    conn.execute("INSERT INTO KeyValueStore VALUES ('ft_replay_seed', '{}')")
    conn.commit()
    conn.close()


class TestSqliteHelpers:
    def test_sqlite_file_parsing(self):
        assert runner._sqlite_file("sqlite:///a/b.sqlite") == Path("a/b.sqlite")
        assert runner._sqlite_file("postgresql://x") is None
        assert runner._sqlite_file(None) is None

    def test_earliest_real_trade_ignores_replay(self, tmp_path):
        db = tmp_path / "t.sqlite"
        _make_db(db, [
            (1, "2026-01-05 00:00:00.000000", "[replay] x", 0),  # replay → ignored
            (2, "2026-03-10 00:00:00.000000", "mytag", 1),        # real
            (3, "2026-04-01 00:00:00.000000", None, 1),           # real (null tag)
        ])
        earliest = runner._earliest_real_trade_open(db)
        assert earliest == datetime(2026, 3, 10, tzinfo=UTC)

    def test_earliest_real_trade_all_replay(self, tmp_path):
        db = tmp_path / "t.sqlite"
        _make_db(db, [(1, "2026-01-05 00:00:00.000000", "[replay] x", 0)])
        assert runner._earliest_real_trade_open(db) is None

    def test_earliest_real_trade_missing_db(self, tmp_path):
        assert runner._earliest_real_trade_open(tmp_path / "nope.sqlite") is None
        assert runner._earliest_real_trade_open(None) is None

    def test_existing_ids(self, tmp_path):
        db = tmp_path / "t.sqlite"
        _make_db(db, [(1, "2026-01-01 00:00:00.000000", "a", 0),
                      (2, "2026-01-02 00:00:00.000000", "b", 1)])
        assert runner._existing_trade_ids(db) == {1, 2}
        assert runner._existing_trade_ids(tmp_path / "nope.sqlite") == set()

    def test_truncate_clears_rows_not_file(self, tmp_path):
        db = tmp_path / "t.sqlite"
        _make_db(db, [(1, "2026-01-01 00:00:00.000000", "a", 0)])
        runner._truncate_trades(db)
        assert db.exists()  # file kept (open connection stays valid)
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        # seed marker also cleared
        assert conn.execute(
            "SELECT COUNT(*) FROM KeyValueStore WHERE key='ft_replay_seed'"
        ).fetchone()[0] == 0
        conn.close()

    def test_truncate_missing_db_noop(self, tmp_path):
        runner._truncate_trades(tmp_path / "nope.sqlite")  # must not raise

    def test_quick_check_ok_and_missing(self, tmp_path):
        db = tmp_path / "t.sqlite"
        _make_db(db, [(1, "2026-01-01 00:00:00.000000", "a", 0)])
        assert runner._db_quick_check(db) is True
        assert runner._db_quick_check(tmp_path / "nope.sqlite") is True  # nothing to corrupt

    def test_prepare_seed_db_caps_end(self, tmp_path):
        db = tmp_path / "t.sqlite"
        _make_db(db, [(1, "2026-03-01 00:00:00.000000", "real", 1)])
        end, ids = runner._prepare_seed_db(
            db, reset_db=False,
            start_dt=datetime(2026, 1, 1, tzinfo=UTC), end_dt=datetime(2026, 6, 1, tzinfo=UTC),
        )
        assert end == datetime(2026, 3, 1, tzinfo=UTC)  # capped at the real trade
        assert ids == {1}

    def test_prepare_seed_db_reset_wipes(self, tmp_path):
        db = tmp_path / "t.sqlite"
        _make_db(db, [(1, "2026-03-01 00:00:00.000000", "real", 1)])
        end, ids = runner._prepare_seed_db(
            db, reset_db=True,
            start_dt=datetime(2026, 1, 1, tzinfo=UTC), end_dt=datetime(2026, 6, 1, tzinfo=UTC),
        )
        assert end == datetime(2026, 6, 1, tzinfo=UTC)  # not capped
        assert ids == set()
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
        conn.close()

    def test_prepare_seed_db_rejects_when_no_room(self, tmp_path):
        db = tmp_path / "t.sqlite"
        _make_db(db, [(1, "2026-01-01 00:00:00.000000", "real", 1)])  # real trade at start
        # Empty window is a graceful no-op signal (NothingToReplay), not a hard RuntimeError —
        # the caller marks the bot seeded and resumes instead of erroring + reload-looping.
        with pytest.raises(runner.NothingToReplay, match="at/before"):
            runner._prepare_seed_db(
                db, reset_db=False,
                start_dt=datetime(2026, 1, 1, tzinfo=UTC), end_dt=datetime(2026, 6, 1, tzinfo=UTC),
            )


class _FakeBot:
    """Minimal stand-in exposing ``exchange.fetch_ticker(pair)['last']`` for flatten."""

    def __init__(self, prices: dict | None = None):
        self._prices = prices or {}

        class _Ex:
            def fetch_ticker(_self, pair):
                if pair not in self._prices:
                    raise KeyError(pair)
                return {"last": self._prices[pair]}

        self.exchange = _Ex()


class TestFlattenReplayOpenTrades:
    """_flatten_replay_open_trades via the real Trade ORM (in-memory DB).

    A seed must hand the live bot a FLAT book: every replay open trade is closed at the
    last simulated price; real (pre-existing) trades are untouched.
    """

    def _setup_trades(self, tmp_path, specs):
        """specs: list of (pair, enter_tag, open_min) → returns created Trade list."""
        from freqtrade.persistence import Trade, init_db

        init_db(f"sqlite:///{tmp_path / 'recon.sqlite'}")
        created = []
        for pair, tag, minute in specs:
            t = Trade(
                pair=pair, stake_amount=10.0, amount=1.0, open_rate=100.0,
                open_date=datetime(2026, 1, 1, 0, minute, tzinfo=UTC),
                is_open=True, enter_tag=tag, exchange="hyperliquid", fee_open=0.0, fee_close=0.0,
            )
            Trade.session.add(t)
            created.append(t)
        Trade.commit()
        return created

    def test_flattens_all_replay_keeps_real(self, tmp_path):
        # real BTC (id1) + 2 replay (ETH, SOL) — all replay opens must close, real stays open
        t = self._setup_trades(tmp_path, [
            ("BTC/USDC:USDC", "real", 0),
            ("ETH/USDC:USDC", None, 5),
            ("SOL/USDC:USDC", None, 6),
        ])
        bot = _FakeBot({"ETH/USDC:USDC": 110.0, "SOL/USDC:USDC": 90.0})
        closed = runner._flatten_replay_open_trades(bot, {t[0].id})
        assert closed == 2
        assert t[0].is_open is True            # real untouched
        assert t[1].is_open is False and t[1].exit_reason == "replay_seed_end"
        assert t[2].is_open is False and t[2].exit_reason == "replay_seed_end"

    def test_closes_at_last_simulated_price(self, tmp_path):
        t = self._setup_trades(tmp_path, [("ETH/USDC:USDC", None, 5)])
        bot = _FakeBot({"ETH/USDC:USDC": 110.0})
        runner._flatten_replay_open_trades(bot, set())
        assert t[0].close_rate == 110.0       # last sim price, not entry

    def test_price_unavailable_falls_back_to_open_rate(self, tmp_path):
        t = self._setup_trades(tmp_path, [("XRP/USDC:USDC", None, 5)])
        bot = _FakeBot({})                    # fetch_ticker raises → neutral close
        closed = runner._flatten_replay_open_trades(bot, set())
        assert closed == 1
        assert t[0].is_open is False
        assert t[0].close_rate == t[0].open_rate

    def test_no_replay_trades_returns_zero(self, tmp_path):
        t = self._setup_trades(tmp_path, [("BTC/USDC:USDC", "real", 0)])
        closed = runner._flatten_replay_open_trades(_FakeBot(), {t[0].id})
        assert closed == 0
        assert t[0].is_open is True
