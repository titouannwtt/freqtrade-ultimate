"""
Replay coordinator daemon (fork-specific).

A single, machine-wide scheduler that caps how many dry-run replays run at once.
Replays are spawned by independent bot processes, so a shared point is needed to
enforce a global limit. This daemon owns the queue and the running set:

    capacity = nproc - CORE_RESERVE - <cores used by running hyperopts>

When over capacity (e.g. a hyperopt starts), the lowest-priority running replays are
*paused* (SIGSTOP — frozen, 0 CPU, state kept in memory) and resumed (SIGCONT) once
cores free up. Queued replays are spawned highest-priority-first. One replay per bot.

It speaks a tiny JSON-line protocol over a Unix socket (same idiom as the ftcache /
pairlist daemons). The scheduling logic (``_reconcile``) is deliberately separated from
process management (``_spawn``/``_pause``/``_resume``/``_alive``) so it is unit-testable
without real subprocesses.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import signal
import socketserver
import subprocess
import threading
import time
from pathlib import Path


logger = logging.getLogger(__name__)

# Always leave this many cores for the live fleet + webserver + OS.
CORE_RESERVE = 2
# Keep finished jobs queryable for this long so the UI can show the final result.
DONE_TTL_S = 3600
SCHED_INTERVAL_S = 1.5


def socket_path() -> str:
    return f"/tmp/ft-replay-coord-{os.getuid()}.sock"  # noqa: S108


def lock_path() -> str:
    return f"/tmp/ft-replay-coord-{os.getuid()}.lock"  # noqa: S108


def daemon_lock_path() -> str:
    """Lifetime lock held by the running daemon (distinct from the client's spawn lock).

    A second daemon that cannot acquire it exits before clobbering the live socket —
    this is the singleton guard that prevents split-brain coordinators.
    """
    return f"/tmp/ft-replay-coord-{os.getuid()}.daemon.lock"  # noqa: S108


def state_path() -> Path:
    return Path.home() / ".freqtrade" / "replay-coordinator-state.json"


def log_path() -> Path:
    return Path.home() / ".freqtrade" / "logs" / "replay-coordinator.log"


def n_cores() -> int:
    return os.cpu_count() or 1


def detect_hyperopt_cores() -> int:
    """
    Cores currently claimed by running ``freqtrade hyperopt`` processes.

    Reads each process's ``-j/--job-workers`` (hyperopt's worker count). A hyperopt with
    no explicit ``-j`` (or ``-j -1``) defaults to all cores, so we count the full machine
    for it — giving hyperopt strict priority, as specified. Linux ``/proc`` based; returns
    0 on platforms without ``/proc``.
    """
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return 0
    total = 0
    ncpu = n_cores()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        args = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
        if not args or "hyperopt" not in args or not any("freqtrade" in a for a in args):
            continue
        total += _parse_job_workers(args, ncpu)
    return total


def _parse_job_workers(args: list[str], ncpu: int) -> int:
    """``-j N`` / ``--job-workers N`` / ``--job-workers=N`` → N; missing or negative → ncpu."""
    jobs: int | None = None
    for i, a in enumerate(args):
        if a in ("-j", "--job-workers") and i + 1 < len(args):
            jobs = _safe_int(args[i + 1])
        elif a.startswith("--job-workers="):
            jobs = _safe_int(a.split("=", 1)[1])
    if jobs is None or jobs < 0:
        return ncpu
    return min(jobs, ncpu)


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def capacity(hyperopt_cores: int | None = None) -> int:
    """Replay slots available right now."""
    hc = detect_hyperopt_cores() if hyperopt_cores is None else hyperopt_cores
    return max(0, n_cores() - CORE_RESERVE - hc)


# States: queued → running ⇄ paused → done | error | cancelled
ACTIVE_STATES = ("queued", "running", "paused")


class Job:
    __slots__ = (
        "bot_id",
        "cmd",
        "db_url",
        "error",
        "finished_at",
        "pid",
        "priority",
        "proc",
        "progress_file",
        "started_at",
        "state",
        "submitted_at",
    )

    def __init__(self, bot_id, cmd, priority, progress_file, db_url):
        self.bot_id: str = bot_id
        self.cmd: list[str] = cmd
        self.priority: int = priority  # higher = more important
        self.progress_file: str = progress_file
        self.db_url: str | None = db_url
        self.state: str = "queued"
        self.pid: int | None = None
        self.proc: subprocess.Popen | None = None
        self.submitted_at: float = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.error: str | None = None

    def to_dict(self) -> dict:
        return {
            "bot_id": self.bot_id,
            "cmd": self.cmd,
            "priority": self.priority,
            "progress_file": self.progress_file,
            "db_url": self.db_url,
            "state": self.state,
            "pid": self.pid,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Job:
        j = cls(d["bot_id"], d["cmd"], d.get("priority", 0), d["progress_file"], d.get("db_url"))
        j.state = d.get("state", "queued")
        j.pid = d.get("pid")
        j.submitted_at = d.get("submitted_at", time.time())
        j.started_at = d.get("started_at")
        j.finished_at = d.get("finished_at")
        j.error = d.get("error")
        return j


class Coordinator:
    """Owns the jobs + scheduling. Process management is in overridable methods."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.start_ts = time.time()
        self._stop = False

    # ── process management (overridden in tests) ─────────────────────────
    def _spawn(self, job: Job) -> None:
        Path(job.progress_file).parent.mkdir(parents=True, exist_ok=True)
        log_file = Path(job.progress_file + ".log").open("w")
        job.proc = subprocess.Popen(
            job.cmd, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True
        )
        job.pid = job.proc.pid

    def _signal(self, job: Job, sig: int) -> None:
        if job.pid is None:
            return
        try:
            os.kill(job.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _alive(self, job: Job) -> bool:
        if job.proc is not None:
            return job.proc.poll() is None
        if job.pid is None:
            return False
        try:
            os.kill(job.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _returncode(self, job: Job) -> int:
        if job.proc is not None and job.proc.returncode is not None:
            return job.proc.returncode
        # Adopted process (no Popen handle): trust the progress file's final status.
        return 0 if _progress(job).get("status") == "done" else 1

    # ── scheduling (pure logic, fully testable) ──────────────────────────
    def _capacity(self) -> int:
        return capacity()

    def _running(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.state == "running"]

    def _waiting(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.state in ("paused", "queued")]

    def _reconcile(self) -> None:
        # 1. Reap finished.
        for job in self.jobs.values():
            if job.state in ("running", "paused") and not self._alive(job):
                rc = self._returncode(job)
                job.state = "done" if rc == 0 else "error"
                job.finished_at = time.time()
                if rc != 0 and not job.error:
                    job.error = f"replay exited with code {rc}"
                logger.info("[replay-coord] %s finished: %s", job.bot_id, job.state)

        cap = self._capacity()

        # 2. Over capacity → pause the lowest-priority running ones.
        runs = sorted(self._running(), key=lambda j: (j.priority, -(j.started_at or 0)))
        for job in runs[: max(0, len(runs) - cap)]:
            self._pause(job)

        # 3. Priority preemption at capacity: a higher-priority waiting job bumps the
        #    lowest-priority running one (this is what "prioritise" does from the UI).
        while cap > 0:
            runs, waits = self._running(), self._waiting()
            if len(runs) < cap or not waits or not runs:
                break
            top = max(waits, key=lambda j: (j.priority, -j.submitted_at))
            low = min(runs, key=lambda j: (j.priority, -(j.started_at or 0)))
            if top.priority <= low.priority:
                break
            self._pause(low)

        # 4. Fill free slots with the highest-priority waiting jobs.
        for job in sorted(self._waiting(), key=lambda j: (-j.priority, j.submitted_at)):
            if len(self._running()) >= cap:
                break
            if job.state == "paused":
                self._resume(job)
            else:
                self._start(job)

        self._gc()

    def _start(self, job: Job) -> None:
        try:
            self._spawn(job)
        except Exception as exc:  # spawn failure must not wedge the scheduler
            job.state = "error"
            job.error = f"spawn failed: {exc}"
            logger.error("[replay-coord] spawn failed for %s: %s", job.bot_id, exc)
            return
        job.state = "running"
        job.started_at = time.time()
        logger.info("[replay-coord] started %s (priority=%d)", job.bot_id, job.priority)

    def _pause(self, job: Job) -> None:
        self._signal(job, signal.SIGSTOP)
        job.state = "paused"
        logger.info("[replay-coord] paused %s (over capacity)", job.bot_id)

    def _resume(self, job: Job) -> None:
        self._signal(job, signal.SIGCONT)
        job.state = "running"
        logger.info("[replay-coord] resumed %s", job.bot_id)

    def _gc(self) -> None:
        now = time.time()
        stale = [
            bid
            for bid, j in self.jobs.items()
            if j.state in ("done", "error", "cancelled")
            and j.finished_at
            and now - j.finished_at > DONE_TTL_S
        ]
        for bid in stale:
            del self.jobs[bid]

    # ── public API (called by the socket handler, holding the lock) ──────
    def submit(self, bot_id, cmd, priority, progress_file, db_url) -> dict:
        with self.lock:
            existing = self.jobs.get(bot_id)
            if existing and existing.state in ACTIVE_STATES:
                return {
                    "ok": False,
                    "error": "A replay is already active for this bot",
                    "state": existing.state,
                }
            self.jobs[bot_id] = Job(bot_id, cmd, priority, progress_file, db_url)
            self._reconcile()
            self._persist()
            return {"ok": True, "state": self.jobs[bot_id].state}

    def cancel(self, bot_id) -> dict:
        with self.lock:
            job = self.jobs.get(bot_id)
            if not job:
                return {"ok": True}
            if job.state in ("running", "paused"):
                self._signal(job, signal.SIGCONT)  # un-freeze so terminate is delivered
                try:
                    if job.proc is not None:
                        job.proc.terminate()
                    elif job.pid:
                        os.kill(job.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            job.state = "cancelled"
            job.error = "cancelled"
            job.finished_at = time.time()
            self._reconcile()
            self._persist()
            return {"ok": True}

    def reprioritize(self, bot_id, priority) -> dict:
        with self.lock:
            job = self.jobs.get(bot_id)
            if not job:
                return {"ok": False, "error": "not found"}
            job.priority = int(priority)
            self._reconcile()
            self._persist()
            return {"ok": True, "state": job.state, "priority": job.priority}

    def bot_status(self, bot_id) -> dict:
        with self.lock:
            job = self.jobs.get(bot_id)
            if not job:
                return {"ok": True, "state": "none"}
            payload = _progress(job)
            return {
                "ok": True,
                "state": job.state,
                "priority": job.priority,
                "progress": payload.get("progress", 0.0),
                "step": payload.get("step", ""),
                "elapsed_s": payload.get("elapsed_s"),
                "eta_s": payload.get("eta_s"),
                "result": payload.get("result"),
                "db_url": job.db_url,
                "error": job.error or payload.get("error"),
            }

    def status(self) -> dict:
        with self.lock:
            hc = detect_hyperopt_cores()
            cap = self._capacity()

            def view(j: Job) -> dict:
                p = _progress(j)
                return {
                    "bot_id": j.bot_id,
                    "state": j.state,
                    "priority": j.priority,
                    "submitted_at": j.submitted_at,
                    "started_at": j.started_at,
                    "progress": p.get("progress", 0.0),
                    "eta_s": p.get("eta_s"),
                }

            return {
                "ok": True,
                "cores": n_cores(),
                "core_reserve": CORE_RESERVE,
                "hyperopt_cores": hc,
                "capacity": cap,
                "uptime_s": time.time() - self.start_ts,
                "running": [view(j) for j in self.jobs.values() if j.state == "running"],
                "paused": [view(j) for j in self.jobs.values() if j.state == "paused"],
                "queued": [view(j) for j in self.jobs.values() if j.state == "queued"],
            }

    def tick(self) -> None:
        with self.lock:
            self._reconcile()
            self._persist()

    # ── persistence (survive a coordinator restart) ──────────────────────
    def _persist(self) -> None:
        try:
            sp = state_path()
            sp.parent.mkdir(parents=True, exist_ok=True)
            data = {"jobs": [j.to_dict() for j in self.jobs.values() if j.state in ACTIVE_STATES]}
            sp.write_text(json.dumps(data))
        except OSError as exc:
            logger.debug("[replay-coord] persist failed: %s", exc)

    def restore(self) -> None:
        """Re-adopt replays still alive after a coordinator restart."""
        try:
            data = json.loads(state_path().read_text())
        except (OSError, ValueError):
            return
        for d in data.get("jobs", []):
            job = Job.from_dict(d)
            if job.state in ("running", "paused") and not self._alive(job):
                continue  # died while we were down
            self.jobs[job.bot_id] = job
        with self.lock:
            self._reconcile()


def _progress(job: Job) -> dict:
    try:
        return json.loads(Path(job.progress_file).read_text())
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------
_COORD = Coordinator()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            line = self.rfile.readline()
            if not line:
                return
            req = json.loads(line.decode("utf-8"))
        except (ValueError, OSError):
            self._send({"ok": False, "error": "bad request"})
            return
        self._send(self._dispatch(req))

    def _dispatch(self, req: dict) -> dict:
        op = req.get("op")
        try:
            if op == "ping":
                return {"ok": True, "uptime_s": time.time() - _COORD.start_ts}
            if op == "submit":
                return _COORD.submit(
                    req["bot_id"],
                    req["cmd"],
                    int(req.get("priority", 0)),
                    req["progress_file"],
                    req.get("db_url"),
                )
            if op == "cancel":
                return _COORD.cancel(req["bot_id"])
            if op == "reprioritize":
                return _COORD.reprioritize(req["bot_id"], req["priority"])
            if op == "bot_status":
                return _COORD.bot_status(req["bot_id"])
            if op == "status":
                return _COORD.status()
            return {"ok": False, "error": f"unknown op: {op}"}
        except KeyError as exc:
            return {"ok": False, "error": f"missing field: {exc}"}
        except Exception as exc:  # never crash the handler
            logger.exception("[replay-coord] handler error")
            return {"ok": False, "error": str(exc)}

    def _send(self, obj: dict) -> None:
        try:
            self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError:
            pass


class _Server(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True


def _scheduler_thread() -> None:
    while True:
        time.sleep(SCHED_INTERVAL_S)
        try:
            _COORD.tick()
        except Exception:
            logger.exception("[replay-coord] scheduler tick failed")


def main() -> int:
    parser = argparse.ArgumentParser("freqtrade replay coordinator")
    parser.add_argument("--socket", default=socket_path())
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [replay-coord] %(levelname)s %(message)s",
    )
    sock = args.socket
    sock_p = Path(sock)
    sock_p.parent.mkdir(parents=True, exist_ok=True)

    # Singleton guard: hold an exclusive lock for our whole lifetime. If another daemon
    # already holds it we exit *before* touching the socket, so we never unlink/clobber a
    # live coordinator's socket (which used to orphan it → split-brain). The OS releases
    # the lock automatically on exit/crash. The fd is intentionally never closed.
    dlock_fd = os.open(daemon_lock_path(), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(dlock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info("another replay coordinator already holds the lock — exiting")
        os.close(dlock_fd)
        return 0

    # Now that we are the sole daemon, a leftover socket file is stale: clear it.
    if sock_p.exists():
        sock_p.unlink()

    _COORD.restore()
    threading.Thread(target=_scheduler_thread, daemon=True).start()

    # Write a pid file for the spawn race-guard (same convention as ftcache).
    try:
        Path(sock + ".pid").write_text(str(os.getpid()))
    except OSError:
        pass

    server = _Server(sock, _Handler)
    logger.info("listening on %s (cores=%d reserve=%d)", sock, n_cores(), CORE_RESERVE)
    # shutdown() must run off the main thread (it blocks until serve_forever returns);
    # calling it directly from the signal handler deadlocks → the daemon would ignore
    # SIGTERM/SIGINT and need SIGKILL. Spawn a thread to drive the graceful shutdown.
    def _handle_signal(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, _handle_signal)
    try:
        server.serve_forever()
    finally:
        with contextlib.suppress(OSError):
            sock_p.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
