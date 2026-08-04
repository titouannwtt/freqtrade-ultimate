"""
Client for the shared pairlist cache daemon.

Provides sync API (pairlist filters run in sync context).
Auto-spawns the daemon on first use, same pattern as ftcache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from freqtrade.pairlist_cache.defaults import default_lock_path, default_socket_path


logger = logging.getLogger("ftpairlist.client")

_SINGLETON: PairlistCacheClient | None = None


class PairlistCacheClient:
    def __init__(self, socket_path: str, timeout: float = 5.0) -> None:
        self._socket_path = socket_path
        self._timeout = timeout
        self._sock: socket.socket | None = None

    def _connect(self) -> None:
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        sock.connect(self._socket_path)
        self._sock = sock

    def _close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                logger.debug("socket close failed", exc_info=True)
            self._sock = None

    def _request(self, req: dict) -> dict:
        req.setdefault("req_id", uuid.uuid4().hex[:8])
        payload = (json.dumps(req, separators=(",", ":")) + "\n").encode()
        try:
            self._connect()
            self._sock.sendall(payload)  # type: ignore
            buf = b""
            while b"\n" not in buf:
                chunk = self._sock.recv(1024 * 1024)  # type: ignore
                if not chunk:
                    raise ConnectionError("daemon closed connection")
                buf += chunk
            return json.loads(buf.split(b"\n", 1)[0])
        except Exception:
            self._close()
            raise

    def mget(self, method: str, params_hash: str, pairs: list[str]) -> dict[str, dict | None]:
        """Batch get. Returns {pair: value_dict_or_None}."""
        try:
            resp = self._request(
                {
                    "op": "mget",
                    "method": method,
                    "params_hash": params_hash,
                    "pairs": pairs,
                }
            )
        except Exception as e:
            logger.debug("mget failed (%s), returning all misses", e)
            return {p: None for p in pairs}

        if not resp.get("ok"):
            return {p: None for p in pairs}

        out: dict[str, dict | None] = {}
        for pair in pairs:
            entry = resp.get("results", {}).get(pair, {})
            out[pair] = entry["value"] if entry.get("hit") else None
        return out

    def mput(
        self,
        method: str,
        params_hash: str,
        entries: dict[str, dict],
        ttl: int,
    ) -> None:
        """Batch put. entries = {pair: value_dict}."""
        try:
            self._request(
                {
                    "op": "mput",
                    "method": method,
                    "params_hash": params_hash,
                    "entries": entries,
                    "ttl": ttl,
                }
            )
        except Exception as e:
            logger.debug("mput failed (%s), ignoring", e)

    @staticmethod
    def compute_params_hash(pairlistconfig: dict) -> str:
        """Hash ALL config params (excluding 'method') so any param change
        invalidates the cache automatically."""
        filtered = {k: v for k, v in sorted(pairlistconfig.items()) if k != "method"}
        raw = json.dumps(filtered, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def get_or_spawn(socket_path: str | None = None) -> PairlistCacheClient:
        global _SINGLETON
        if _SINGLETON is not None:
            return _SINGLETON

        sock_path = socket_path or default_socket_path()

        if not Path(sock_path).exists():
            _spawn_daemon(sock_path)

        client = PairlistCacheClient(sock_path)
        try:
            resp = client._request({"op": "ping"})
            if resp.get("ok"):
                logger.info("pairlist cache daemon connected at %s", sock_path)
                _SINGLETON = client
                return client
        except Exception:
            logger.debug("ping failed, spawning daemon", exc_info=True)

        _spawn_daemon(sock_path)
        client = PairlistCacheClient(sock_path)
        _SINGLETON = client
        logger.info("pairlist cache daemon spawned at %s", sock_path)
        return client


def _spawn_daemon(socket_path: str) -> None:
    lock_path = default_lock_path()
    import fcntl

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        os.close(lock_fd)
        _wait_for_socket(socket_path)
        return

    try:
        subprocess.Popen(
            [sys.executable, "-m", "freqtrade.pairlist_cache.daemon", "--socket", socket_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _wait_for_socket(socket_path)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:
            logger.debug("lock release failed", exc_info=True)
        os.close(lock_fd)


def _wait_for_socket(path: str, timeout: float = 10.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if Path(path).exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(path)
                s.close()
                return
            except Exception:
                logger.debug("socket not ready yet", exc_info=True)
        time.sleep(0.2)
    logger.warning("pairlist cache daemon did not start within %.0fs", timeout)
