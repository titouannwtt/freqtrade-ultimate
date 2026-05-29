"""Tests for config-driven dry-run-replay auto-launch (parsing + gating)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from freqtrade.enums import State
from freqtrade.replay import lifecycle


class TestDateParsing:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("01/01/2026", "20260101"),
            ("25/05/2026", "20260525"),
            ("20260101", "20260101"),
            ("2026-01-01", "20260101"),
            ("01-01-2026", "20260101"),
            ("  01/01/2026  ", "20260101"),  # whitespace tolerated
        ],
    )
    def test_valid_dates(self, value, expected):
        assert lifecycle._parse_date(value) == expected

    @pytest.mark.parametrize("value", ["", "garbage", "32/13/2026", "2026/01/01", "Jan 1 2026"])
    def test_invalid_dates_raise(self, value):
        with pytest.raises(ValueError, match="unparseable"):
            lifecycle._parse_date(value)


class TestParseAutolaunchConfig:
    def test_minimal_valid(self):
        out = lifecycle.parse_autolaunch_config(
            {"automatic_launch": True, "start_date": "01/01/2026", "end_date": "25/05/2026"}
        )
        assert out == {"timerange": "20260101-20260525", "resolution_s": 60, "reset_db": False}

    def test_today_end(self):
        out = lifecycle.parse_autolaunch_config(
            {"automatic_launch": True, "start_date": "01/01/2026", "end_date": "today"}
        )
        assert out["timerange"].endswith(datetime.now(UTC).strftime("%Y%m%d"))

    def test_defaults(self):
        # end_date omitted → today; resolution default 1m; reset_db default false
        out = lifecycle.parse_autolaunch_config(
            {"automatic_launch": True, "start_date": "01/01/2026"}
        )
        assert out["resolution_s"] == 60
        assert out["reset_db"] is False

    def test_resolutions(self):
        for res, secs in (("1m", 60), ("5m", 300), ("15m", 900)):
            out = lifecycle.parse_autolaunch_config(
                {"automatic_launch": True, "start_date": "01/01/2026", "resolution": res}
            )
            assert out["resolution_s"] == secs

    def test_reset_db_true(self):
        out = lifecycle.parse_autolaunch_config(
            {"automatic_launch": True, "start_date": "01/01/2026", "reset_db": True}
        )
        assert out["reset_db"] is True

    def test_missing_automatic_launch(self):
        with pytest.raises(ValueError, match="automatic_launch"):
            lifecycle.parse_autolaunch_config({"start_date": "01/01/2026"})

    def test_missing_start_date(self):
        with pytest.raises(ValueError, match="start_date is required"):
            lifecycle.parse_autolaunch_config({"automatic_launch": True})

    def test_bad_resolution(self):
        with pytest.raises(ValueError, match="resolution must be"):
            lifecycle.parse_autolaunch_config(
                {"automatic_launch": True, "start_date": "01/01/2026", "resolution": "3m"}
            )

    def test_start_after_end(self):
        with pytest.raises(ValueError, match="must be before"):
            lifecycle.parse_autolaunch_config(
                {"automatic_launch": True, "start_date": "01/06/2026", "end_date": "01/01/2026"}
            )


class TestMaybeAutolaunch:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        # one-shot flag + capture start_replay instead of really launching
        lifecycle._autolaunch_checked = False
        lifecycle._pending.clear()
        self.launched = []
        monkeypatch.setattr(
            lifecycle, "start_replay", lambda ft, **kw: self.launched.append(kw) or {"ok": True}
        )
        monkeypatch.setattr(lifecycle, "_already_seeded", lambda: False)
        yield
        lifecycle._autolaunch_checked = False
        lifecycle._pending.clear()

    def _ft(self, **cfg_extra):
        cfg = {
            "dry_run": True,
            "exchange": {"pair_whitelist": ["BTC/USDC:USDC"]},
            **cfg_extra,
        }
        return SimpleNamespace(
            config=cfg,
            active_pair_whitelist=["BTC/USDC:USDC", "ETH/USDC:USDC"],
            strategy=SimpleNamespace(get_strategy_name=lambda: "MyStrat"),
        )

    def test_no_config_is_noop(self):
        assert lifecycle.maybe_autolaunch_replay(self._ft()) is False
        assert self.launched == []
        assert lifecycle._autolaunch_checked is True

    def test_live_bot_ignored(self):
        ft = self._ft(
            dry_run=False, dry_run_replay={"automatic_launch": True, "start_date": "01/01/2026"}
        )
        assert lifecycle.maybe_autolaunch_replay(ft) is False
        assert self.launched == []

    def test_already_seeded_skips(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_already_seeded", lambda: True)
        ft = self._ft(dry_run_replay={"automatic_launch": True, "start_date": "01/01/2026"})
        assert lifecycle.maybe_autolaunch_replay(ft) is False
        assert self.launched == []

    def test_launches_with_active_whitelist(self):
        ft = self._ft(
            dry_run_replay={
                "automatic_launch": True,
                "start_date": "01/01/2026",
                "resolution": "5m",
            }
        )
        assert lifecycle.maybe_autolaunch_replay(ft) is True
        assert len(self.launched) == 1
        kw = self.launched[0]
        assert kw["sub_step"] == 300
        assert kw["pairs"] == ["BTC/USDC:USDC", "ETH/USDC:USDC"]  # active whitelist preferred
        assert kw["strategy"] == "MyStrat"

    def test_defers_when_no_pairs(self):
        ft = self._ft(dry_run_replay={"automatic_launch": True, "start_date": "01/01/2026"})
        ft.active_pair_whitelist = []
        ft.config["exchange"]["pair_whitelist"] = []
        assert lifecycle.maybe_autolaunch_replay(ft) is False
        assert self.launched == []
        assert lifecycle._autolaunch_checked is False  # one-shot NOT consumed → retries

    def test_invalid_config_consumes_oneshot(self):
        ft = self._ft(dry_run_replay={"automatic_launch": True, "start_date": "nope"})
        assert lifecycle.maybe_autolaunch_replay(ft) is False
        assert lifecycle._autolaunch_checked is True  # bad config → don't retry forever

    def test_oneshot_prevents_double_launch(self):
        ft = self._ft(dry_run_replay={"automatic_launch": True, "start_date": "01/01/2026"})
        assert lifecycle.maybe_autolaunch_replay(ft) is True
        assert lifecycle.maybe_autolaunch_replay(ft) is False  # already checked
        assert len(self.launched) == 1


class TestOnReplayTick:
    """on_replay_tick must be robust to a coordinator restart that loses an in-flight job:
    'none' is only a finished replay once the job was seen active; before that it means the
    submit was lost and must be re-submitted (not silently resumed without ever seeding)."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        lifecycle._pending.clear()
        self.submits = []
        monkeypatch.setattr(
            lifecycle.cc,
            "submit",
            lambda *a, **k: self.submits.append((a, k)) or {"ok": True, "state": "queued"},
        )
        yield
        lifecycle._pending.clear()

    def _pending(self, **over):
        base = {
            "bot_id": "bot1",
            "progress_file": "/tmp/p.json",  # noqa: S108 — dummy, never opened (cc is mocked)
            "cmd": ["x"],
            "priority": 0,
            "db_url": "sqlite:///d.sqlite",
            "seen_active": False,
            "resubmits": 0,
        }
        base.update(over)
        lifecycle._pending.clear()
        lifecycle._pending.update(base)

    def _status(self, monkeypatch, state):
        monkeypatch.setattr(lifecycle.cc, "bot_status", lambda bid: {"ok": True, "state": state})

    def test_noop_when_not_pending(self, monkeypatch):
        self._status(monkeypatch, "done")
        ft = SimpleNamespace(state=None)
        lifecycle.on_replay_tick(ft)
        assert ft.state is None

    @pytest.mark.parametrize("state", ["running", "queued", "paused"])
    def test_active_marks_seen_no_reload(self, monkeypatch, state):
        self._pending()
        self._status(monkeypatch, state)
        ft = SimpleNamespace(state=None)
        lifecycle.on_replay_tick(ft)
        assert lifecycle._pending["seen_active"] is True
        assert ft.state is None  # still seeding → keep waiting
        assert lifecycle._pending  # not cleared

    def test_done_reloads(self, monkeypatch):
        self._pending(seen_active=True)
        self._status(monkeypatch, "done")
        ft = SimpleNamespace(state=None)
        lifecycle.on_replay_tick(ft)
        assert ft.state == State.RELOAD_CONFIG
        assert not lifecycle._pending

    @pytest.mark.parametrize("state", ["error", "cancelled"])
    def test_terminal_reloads(self, monkeypatch, state):
        self._pending(seen_active=True)
        self._status(monkeypatch, state)
        ft = SimpleNamespace(state=None)
        lifecycle.on_replay_tick(ft)
        assert ft.state == State.RELOAD_CONFIG
        assert not lifecycle._pending

    def test_none_before_active_resubmits(self, monkeypatch):
        self._pending(seen_active=False, resubmits=0)
        self._status(monkeypatch, "none")
        ft = SimpleNamespace(state=None)
        lifecycle.on_replay_tick(ft)
        assert len(self.submits) == 1  # lost job → re-submitted
        assert ft.state is None  # did NOT reload prematurely
        assert lifecycle._pending["resubmits"] == 1

    def test_none_after_active_reloads(self, monkeypatch):
        self._pending(seen_active=True)
        self._status(monkeypatch, "none")
        ft = SimpleNamespace(state=None)
        lifecycle.on_replay_tick(ft)
        assert self.submits == []  # ran then reaped → no resubmit
        assert ft.state == State.RELOAD_CONFIG

    def test_none_gives_up_after_max_resubmits(self, monkeypatch):
        self._pending(seen_active=False, resubmits=lifecycle._MAX_RESUBMITS)
        self._status(monkeypatch, "none")
        ft = SimpleNamespace(state=None)
        lifecycle.on_replay_tick(ft)
        assert self.submits == []  # exhausted → stop retrying
        assert ft.state == State.RELOAD_CONFIG

    def test_resubmit_refused_resumes(self, monkeypatch):
        self._pending(seen_active=False, resubmits=0)
        self._status(monkeypatch, "none")
        monkeypatch.setattr(lifecycle.cc, "submit", lambda *a, **k: {"ok": False, "error": "x"})
        ft = SimpleNamespace(state=None)
        lifecycle.on_replay_tick(ft)
        assert ft.state == State.RELOAD_CONFIG  # can't re-submit → resume

    def test_coordinator_unreachable_waits(self, monkeypatch):
        self._pending(seen_active=False)
        monkeypatch.setattr(lifecycle.cc, "bot_status", lambda bid: {"ok": False, "error": "down"})
        ft = SimpleNamespace(state=None)
        lifecycle.on_replay_tick(ft)
        assert ft.state is None  # transient outage → keep waiting
        assert self.submits == []
        assert lifecycle._pending
