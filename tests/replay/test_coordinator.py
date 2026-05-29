"""Unit tests for the replay coordinator scheduling logic (no real subprocesses)."""

import signal

import pytest

import freqtrade.replay.coordinator as coord


# ── hyperopt core detection ────────────────────────────────────────────────
class TestHyperoptParsing:
    @pytest.mark.parametrize(
        "args, ncpu, expected",
        [
            (["freqtrade", "hyperopt", "-j", "20"], 32, 20),
            (["freqtrade", "hyperopt", "--job-workers", "8"], 32, 8),
            (["freqtrade", "hyperopt", "--job-workers=4"], 32, 4),
            (["freqtrade", "hyperopt"], 32, 32),  # default → all cores
            (["freqtrade", "hyperopt", "-j", "-1"], 32, 32),  # -1 → all cores
            (["freqtrade", "hyperopt", "-j", "999"], 32, 32),  # clamp to ncpu
            (["freqtrade", "hyperopt", "-j", "garbage"], 32, 32),  # non-int → default
            (["freqtrade", "hyperopt", "-j"], 32, 32),  # dangling flag
            (["freqtrade", "hyperopt", "-j", "0"], 32, 0),  # explicit 0
        ],
    )
    def test_parse_job_workers(self, args, ncpu, expected):
        assert coord._parse_job_workers(args, ncpu) == expected

    def test_capacity_math(self):
        assert coord.capacity(hyperopt_cores=0) == coord.n_cores() - coord.CORE_RESERVE
        assert coord.capacity(hyperopt_cores=10_000) == 0  # never negative

    def test_safe_int(self):
        assert coord._safe_int("5") == 5
        assert coord._safe_int("x") is None
        assert coord._safe_int("") is None


# ── scheduling ──────────────────────────────────────────────────────────────
class FakeCoordinator(coord.Coordinator):
    """Coordinator with simulated processes — no real spawning/signals."""

    def __init__(self, cap):
        super().__init__()
        self.cap = cap
        self.alive: dict[str, bool] = {}
        self.rc: dict[str, int] = {}
        self.signals: list[tuple[str, int]] = []
        self.spawned: list[str] = []

    def _capacity(self) -> int:
        return self.cap

    def _spawn(self, job):
        job.pid = 100000 + len(self.spawned)
        self.spawned.append(job.bot_id)
        self.alive[job.bot_id] = True

    def _signal(self, job, sig):
        self.signals.append((job.bot_id, sig))

    def _alive(self, job):
        return self.alive.get(job.bot_id, False)

    def _returncode(self, job):
        return self.rc.get(job.bot_id, 0)

    def _persist(self):  # no disk in tests
        pass

    # test helper
    def finish(self, bot_id, rc=0):
        self.alive[bot_id] = False
        self.rc[bot_id] = rc

    def add(self, bot_id, priority=0):
        return self.submit(bot_id, ["echo", bot_id], priority, f"{bot_id}.progress.json", None)

    def states(self):
        return {b: j.state for b, j in self.jobs.items()}


def test_fills_up_to_capacity_then_queues():
    c = FakeCoordinator(cap=2)
    for i in range(5):
        c.add(f"b{i}")
    st = c.states()
    assert sum(v == "running" for v in st.values()) == 2
    assert sum(v == "queued" for v in st.values()) == 3


def test_highest_priority_runs_first():
    c = FakeCoordinator(cap=1)
    c.add("low", priority=1)
    c.add("high", priority=10)
    # A strictly-higher-priority submit preempts the running lower one.
    assert c.jobs["high"].state == "running"
    assert c.jobs["low"].state in ("queued", "paused")


def test_equal_priority_does_not_preempt():
    c = FakeCoordinator(cap=1)
    c.add("first", priority=0)
    c.add("second", priority=0)
    # Equal priority → FIFO, no preemption of the running one.
    assert c.jobs["first"].state == "running"
    assert c.jobs["second"].state == "queued"


def test_one_replay_per_bot():
    c = FakeCoordinator(cap=5)
    assert c.add("b")["ok"] is True
    res = c.add("b")
    assert res["ok"] is False
    assert "already active" in res["error"]


def test_finished_job_frees_a_slot():
    c = FakeCoordinator(cap=1)
    c.add("a")
    c.add("b")
    assert c.jobs["a"].state == "running"
    assert c.jobs["b"].state == "queued"
    c.finish("a", rc=0)
    c.tick()
    assert c.jobs["a"].state == "done"
    assert c.jobs["b"].state == "running"


def test_failed_job_marked_error():
    c = FakeCoordinator(cap=1)
    c.add("a")
    c.finish("a", rc=1)
    c.tick()
    assert c.jobs["a"].state == "error"
    assert "code 1" in (c.jobs["a"].error or "")


def test_capacity_drop_pauses_lowest_priority():
    c = FakeCoordinator(cap=3)
    c.add("p1", priority=1)
    c.add("p2", priority=2)
    c.add("p3", priority=3)
    assert all(c.jobs[b].state == "running" for b in ("p1", "p2", "p3"))
    # Hyperopt starts → capacity drops to 1.
    c.cap = 1
    c.tick()
    assert c.jobs["p3"].state == "running"  # highest priority kept
    assert c.jobs["p1"].state == "paused"
    assert c.jobs["p2"].state == "paused"
    assert ("p1", signal.SIGSTOP) in c.signals


def test_capacity_recovery_resumes_paused():
    c = FakeCoordinator(cap=1)
    c.add("p1", priority=1)
    c.add("p2", priority=2)  # preempts p1 (2 > 1)
    assert c.jobs["p2"].state == "running"
    assert c.jobs["p1"].state == "paused"
    c.cap = 2
    c.tick()
    assert c.jobs["p1"].state == "running"


def test_reprioritize_preempts_running():
    c = FakeCoordinator(cap=1)
    c.add("running_eq", priority=5)
    c.add("waiting", priority=5)  # equal → queues behind
    assert c.jobs["running_eq"].state == "running"
    assert c.jobs["waiting"].state == "queued"
    # Bump the waiting one above the running one → it preempts.
    c.reprioritize("waiting", 99)
    assert c.jobs["waiting"].state == "running"
    assert c.jobs["running_eq"].state in ("queued", "paused")


def test_cancel_running_frees_slot():
    c = FakeCoordinator(cap=1)
    c.add("a")
    c.add("b")
    c.cancel("a")
    c.tick()
    assert c.jobs["a"].state == "cancelled"
    assert c.jobs["b"].state == "running"


def test_cancel_unknown_is_noop():
    c = FakeCoordinator(cap=1)
    assert c.cancel("ghost")["ok"] is True


def test_zero_capacity_queues_everything():
    c = FakeCoordinator(cap=0)
    c.add("a")
    c.add("b")
    assert all(j.state == "queued" for j in c.jobs.values())
    assert c.spawned == []


def test_status_shape():
    c = FakeCoordinator(cap=2)
    c.add("a", priority=5)
    c.add("b", priority=1)
    c.add("d", priority=0)
    st = c.status()
    assert st["ok"] is True
    assert len(st["running"]) == 2
    assert len(st["queued"]) == 1
    assert {"bot_id", "state", "priority", "progress"} <= set(st["running"][0])


def test_bot_status_none_for_unknown():
    c = FakeCoordinator(cap=1)
    assert c.bot_status("nope") == {"ok": True, "state": "none"}


def test_reprioritize_unknown():
    c = FakeCoordinator(cap=1)
    assert c.reprioritize("nope", 5)["ok"] is False


def test_resubmit_after_done_is_allowed():
    c = FakeCoordinator(cap=1)
    c.add("a")
    c.finish("a", rc=0)
    c.tick()
    assert c.jobs["a"].state == "done"
    # A finished bot can be replayed again.
    assert c.add("a")["ok"] is True
    assert c.jobs["a"].state == "running"
