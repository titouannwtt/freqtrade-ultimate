"""
Client for the replay coordinator daemon.

Auto-spawns the daemon on first use (flock-guarded, like the ftcache client) and speaks
the JSON-line protocol. Every call degrades gracefully: if the daemon can't be reached it
returns ``{"ok": False, "error": ...}`` rather than raising, so a coordinator outage never
takes a bot down.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from freqtrade.replay.coordinator import lock_path, log_path, socket_path


logger = logging.getLogger(__name__)

_SPAWN_TIMEOUT_S = 10.0


def _recv_line(sock: socket.socket) -> bytes:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf


def _responsive(path: str) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(path)
    except OSError:
        return False
    try:
        s.sendall(b'{"op": "ping"}\n')
        line = _recv_line(s)
        return bool(line) and json.loads(line.split(b"\n", 1)[0]).get("ok") is True
    except (OSError, ValueError):
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _ensure_daemon(path: str) -> bool:
    if Path(path).exists() and _responsive(path):
        return True
    lp = lock_path()
    Path(lp).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lp, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another process is spawning — wait for the socket to come up.
            deadline = time.monotonic() + _SPAWN_TIMEOUT_S
            while time.monotonic() < deadline:
                if Path(path).exists() and _responsive(path):
                    return True
                time.sleep(0.3)
            return False
        if Path(path).exists() and _responsive(path):
            return True
        lg = log_path()
        lg.parent.mkdir(parents=True, exist_ok=True)
        logf = lg.open("a")
        subprocess.Popen(
            [sys.executable, "-m", "freqtrade.replay.coordinator", "--socket", path],
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + _SPAWN_TIMEOUT_S
        while time.monotonic() < deadline:
            if Path(path).exists() and _responsive(path):
                return True
            time.sleep(0.2)
        logger.warning("[replay-coord] daemon did not come up within %.0fs", _SPAWN_TIMEOUT_S)
        return False
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


def _request(payload: dict, *, autospawn: bool = True, timeout: float = 5.0) -> dict:
    path = socket_path()
    if autospawn and not _ensure_daemon(path):
        return {"ok": False, "error": "replay coordinator unavailable"}
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(path)
    except OSError as exc:
        return {"ok": False, "error": f"replay coordinator unavailable: {exc}"}
    try:
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        line = _recv_line(s)
        if not line:
            return {"ok": False, "error": "no response from coordinator"}
        return json.loads(line.split(b"\n", 1)[0])
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            s.close()
        except OSError:
            pass


# ── public API ───────────────────────────────────────────────────────────
def submit(bot_id: str, cmd: list[str], priority: int, progress_file: str, db_url: str) -> dict:
    return _request(
        {
            "op": "submit",
            "bot_id": bot_id,
            "cmd": cmd,
            "priority": priority,
            "progress_file": progress_file,
            "db_url": db_url,
        }
    )


def cancel(bot_id: str) -> dict:
    return _request({"op": "cancel", "bot_id": bot_id})


def reprioritize(bot_id: str, priority: int) -> dict:
    return _request({"op": "reprioritize", "bot_id": bot_id, "priority": priority})


def bot_status(bot_id: str) -> dict:
    return _request({"op": "bot_status", "bot_id": bot_id})


def status() -> dict:
    return _request({"op": "status"})
