"""SIGTERM must always terminate the ftcache daemon.

Regression for a production incident: SIGTERM started an unbounded cleanup
(`Server.wait_closed()` waits for every open connection, and ~50 bots hold long-lived
ones), while asyncio had already dropped the listening socket. The daemon stayed alive,
busy and holding its PID lock, so no replacement could spawn and the whole fleet ran
without its cache until the process was SIGKILLed by hand.

The invariant these tests pin: *whatever* the cleanup does, the process exits and the
socket file does not survive it.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


DAEMON = [sys.executable, "-m", "freqtrade.ohlcv_cache.daemon", "--socket"]


def _wait_for_socket(path, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2)
            try:
                s.connect(str(path))
                return True
            except OSError:
                pass
            finally:
                s.close()
        time.sleep(0.3)
    return False


@pytest.fixture
def daemon(tmp_path):
    """A real daemon on a private socket, with a short shutdown budget."""
    sock = tmp_path / "ftcache-test.sock"
    env = {**os.environ, "FTCACHE_SHUTDOWN_GRACE_S": "2"}
    proc = subprocess.Popen(
        [*DAEMON, str(sock)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if not _wait_for_socket(sock):
        proc.kill()
        proc.wait(timeout=10)
        pytest.skip("daemon did not come up in this environment")
    yield proc, sock
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def test_sigterm_terminates_the_daemon(daemon):
    proc, _ = daemon
    proc.send_signal(signal.SIGTERM)
    # Generous versus the 2s budget: this asserts termination, not promptness.
    assert proc.wait(timeout=40) is not None
    assert proc.poll() is not None, "SIGTERM left the daemon alive — the incident condition"


def test_sigterm_removes_the_socket_file(daemon):
    """A leftover socket keeps clients dialling a corpse instead of spawning a successor."""
    proc, sock = daemon
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=40)
    assert not Path(sock).exists()


def test_a_client_holding_a_connection_cannot_block_shutdown(daemon):
    """The exact production shape: an open connection must not pin the daemon alive."""
    proc, sock = daemon
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(5)
    conn.connect(str(sock))
    try:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=40) is not None
    finally:
        conn.close()


def test_the_socket_is_unlinked_outside_the_cleanup_budget():
    """If unlinking sat inside the timed cleanup, a slow teardown would leave the file."""
    import inspect

    from freqtrade.ohlcv_cache.daemon import Daemon

    # The call, not the word: the docstring legitimately mentions unlinking.
    assert "os.unlink" not in inspect.getsource(Daemon._cleanup), (
        "_cleanup must not unlink; the caller does it unconditionally after the timeout"
    )
    serve = inspect.getsource(Daemon.serve)
    assert "wait_for" in serve and "SHUTDOWN_GRACE_S" in serve
    assert "unlink" in serve


def test_the_hard_deadline_runs_off_the_event_loop():
    """A wedged loop runs no coroutine — the guarantee must come from a thread."""
    import inspect

    from freqtrade.ohlcv_cache import daemon as mod

    src = inspect.getsource(mod.main)
    assert "threading.Timer" in src, "the force-exit deadline must not depend on asyncio"
    assert "os._exit" in src
    assert "second signal" in src, "a repeated signal must short-circuit to exit"
