"""
Shared pairlist result cache daemon.

Lightweight TTL key-value store over a Unix socket. Bots submit per-pair
filter results (GET/PUT) and other bots with identical filter params reuse
them. No master/slave — every bot is equal.

Spawned on-demand by PairlistCacheClient; shuts down after idle timeout.

Protocol: newline-delimited JSON, same convention as ftcache.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path


logger = logging.getLogger("ftpairlist.daemon")

_PROTOCOL_VERSION = 1


class _Entry:
    __slots__ = ("expires_at", "value")

    def __init__(self, value: dict, ttl: float) -> None:
        self.value = value
        self.expires_at = time.monotonic() + ttl

    def alive(self) -> bool:
        return time.monotonic() < self.expires_at


_DEFAULT_PERSISTENCE_PATH = Path.home() / ".freqtrade" / "ftpairlist" / "cache.json"


class PairlistCacheDaemon:
    def __init__(
        self,
        socket_path: str,
        idle_shutdown_s: float = 900,
        persistence_path: Path | None = None,
    ) -> None:
        self.socket_path = socket_path
        self._idle_shutdown_s = idle_shutdown_s
        self._persistence_path = persistence_path or _DEFAULT_PERSISTENCE_PATH
        # cache[method][params_hash][pair] = _Entry
        self._cache: dict[str, dict[str, dict[str, _Entry]]] = {}
        self._active_clients = 0
        self._last_disconnect = time.monotonic()
        self._shutdown = asyncio.Event()
        self._stats = {
            "started": time.monotonic(),
            "gets": 0,
            "hits": 0,
            "puts": 0,
        }
        self._method_stats: dict[str, dict[str, int]] = {}

    def _get(self, method: str, ph: str, pair: str) -> dict | None:
        bucket = self._cache.get(method, {}).get(ph, {})
        entry = bucket.get(pair)
        if entry and entry.alive():
            return entry.value
        return None

    def _bump_method(self, method: str, key: str, count: int = 1) -> None:
        m = self._method_stats.get(method)
        if m is None:
            m = {"gets": 0, "hits": 0, "puts": 0}
            self._method_stats[method] = m
        m[key] = m.get(key, 0) + count

    def _put(self, method: str, ph: str, pair: str, value: dict, ttl: float) -> None:
        self._cache.setdefault(method, {}).setdefault(ph, {})[pair] = _Entry(value, ttl)

    def _save_to_disk(self) -> None:
        """Persist live cache entries to JSON so they survive a daemon restart."""
        now = time.monotonic()
        data: dict[str, dict[str, dict[str, dict]]] = {}
        count = 0
        for method, ph_buckets in self._cache.items():
            for ph, pair_dict in ph_buckets.items():
                for pair, entry in pair_dict.items():
                    ttl_remaining = entry.expires_at - now
                    if ttl_remaining <= 0:
                        continue
                    data.setdefault(method, {}).setdefault(ph, {})[pair] = {
                        "value": entry.value,
                        "ttl_remaining": ttl_remaining,
                    }
                    count += 1

        if count == 0:
            logger.info("no live cache entries to persist")
            return

        try:
            self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._persistence_path.with_suffix(".tmp")
            payload = {"version": 1, "saved_at": time.time(), "data": data}
            tmp_path.write_text(json.dumps(payload, separators=(",", ":")))
            tmp_path.rename(self._persistence_path)
            logger.info("saved %d cache entries to %s", count, self._persistence_path)
        except Exception:
            logger.exception("failed to save cache to disk")

    def _load_from_disk(self) -> None:
        """Reload cache entries from a previous daemon's JSON dump."""
        if not self._persistence_path.exists():
            return

        try:
            raw = json.loads(self._persistence_path.read_text())
        except Exception:
            logger.exception("failed to read cache file %s", self._persistence_path)
            return

        if raw.get("version") != 1:
            logger.warning("unknown cache file version %s — skipping", raw.get("version"))
            return

        count = 0
        for method, ph_buckets in raw.get("data", {}).items():
            for ph, pair_dict in ph_buckets.items():
                for pair, entry_data in pair_dict.items():
                    ttl = entry_data.get("ttl_remaining", 0)
                    if ttl <= 0:
                        continue
                    self._put(method, ph, pair, entry_data["value"], ttl)
                    count += 1

        logger.info("loaded %d cache entries from %s", count, self._persistence_path)

        try:
            self._persistence_path.unlink()
        except OSError:
            pass

    def _dispatch(self, req: dict) -> dict:
        op = req.get("op", "")
        rid = req.get("req_id", "")

        if op == "ping":
            return {
                "req_id": rid,
                "ok": True,
                "version": _PROTOCOL_VERSION,
                "uptime_s": time.monotonic() - self._stats["started"],
            }

        if op == "get":
            method = req["method"]
            self._stats["gets"] += 1
            self._bump_method(method, "gets")
            val = self._get(method, req["params_hash"], req["pair"])
            if val is not None:
                self._stats["hits"] += 1
                self._bump_method(method, "hits")
                return {"req_id": rid, "ok": True, "hit": True, "value": val}
            return {"req_id": rid, "ok": True, "hit": False}

        if op == "put":
            method = req["method"]
            self._stats["puts"] += 1
            self._bump_method(method, "puts")
            self._put(
                method, req["params_hash"], req["pair"], req["value"], req.get("ttl", 3600)
            )
            return {"req_id": rid, "ok": True}

        if op == "mget":
            method = req["method"]
            pairs = req.get("pairs", [])
            self._stats["gets"] += len(pairs)
            self._bump_method(method, "gets", len(pairs))
            results = {}
            for pair in pairs:
                val = self._get(method, req["params_hash"], pair)
                if val is not None:
                    self._stats["hits"] += 1
                    self._bump_method(method, "hits")
                    results[pair] = {"hit": True, "value": val}
                else:
                    results[pair] = {"hit": False}
            return {"req_id": rid, "ok": True, "results": results}

        if op == "mput":
            method = req["method"]
            entries = req.get("entries", {})
            self._stats["puts"] += len(entries)
            self._bump_method(method, "puts", len(entries))
            ttl = req.get("ttl", 3600)
            for pair, val in entries.items():
                self._put(method, req["params_hash"], pair, val, ttl)
            return {"req_id": rid, "ok": True}

        if op == "stats":
            total_entries = sum(
                sum(len(pairs) for pairs in ph.values()) for ph in self._cache.values()
            )
            return {
                "req_id": rid,
                "ok": True,
                "clients": self._active_clients,
                **self._stats,
                "entries": total_entries,
                "by_method": dict(self._method_stats),
            }

        return {"req_id": rid, "ok": False, "error": f"unknown op: {op}"}

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._active_clients += 1
        logger.info("client connected — active=%d", self._active_clients)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    continue
                resp = self._dispatch(req)
                writer.write((json.dumps(resp, separators=(",", ":")) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._active_clients -= 1
            self._last_disconnect = time.monotonic()
            logger.info("client disconnected — active=%d", self._active_clients)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                logger.debug("writer close failed", exc_info=True)

    async def _idle_watchdog(self) -> None:
        while not self._shutdown.is_set():
            await asyncio.sleep(10)
            if self._active_clients > 0:
                continue
            idle = time.monotonic() - self._last_disconnect
            if idle >= self._idle_shutdown_s:
                logger.info("idle %.0fs — shutting down", idle)
                self._shutdown.set()

    async def _periodic_stats(self) -> None:
        """Log summary stats every 60s."""
        last_gets = 0
        last_hits = 0
        last_puts = 0
        while not self._shutdown.is_set():
            await asyncio.sleep(60)
            gets = self._stats["gets"]
            hits = self._stats["hits"]
            puts = self._stats["puts"]
            d_gets = gets - last_gets
            d_hits = hits - last_hits
            d_puts = puts - last_puts
            last_gets, last_hits, last_puts = gets, hits, puts
            total_entries = sum(
                sum(len(pairs) for pairs in ph.values()) for ph in self._cache.values()
            )
            uptime = time.monotonic() - self._stats["started"]
            hit_pct = round(hits / gets * 100, 1) if gets > 0 else 0
            logger.info(
                "STATS uptime=%ds clients=%d entries=%d "
                "gets=%d hits=%d(%s%%) puts=%d "
                "delta(gets=%d hits=%d puts=%d)",
                uptime, self._active_clients, total_entries,
                gets, hits, hit_pct, puts,
                d_gets, d_hits, d_puts,
            )

    async def _gc(self) -> None:
        """Periodic sweep of expired entries."""
        while not self._shutdown.is_set():
            await asyncio.sleep(120)
            swept = 0
            for method_buckets in self._cache.values():
                for pair_dict in method_buckets.values():
                    expired = [k for k, v in pair_dict.items() if not v.alive()]
                    for k in expired:
                        del pair_dict[k]
                        swept += 1
            if swept:
                logger.debug("gc swept %d expired entries", swept)

    async def serve(self) -> None:
        self._load_from_disk()

        sock = Path(self.socket_path)
        if sock.exists():
            try:
                sock.unlink()
            except OSError:
                pass

        server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
        )
        sock.chmod(0o600)
        logger.info("pairlist cache daemon listening on %s (pid=%d)", self.socket_path, os.getpid())

        watchdog = asyncio.create_task(self._idle_watchdog())
        gc = asyncio.create_task(self._gc())
        stats_task = asyncio.create_task(self._periodic_stats())
        try:
            await self._shutdown.wait()
        finally:
            watchdog.cancel()
            gc.cancel()
            stats_task.cancel()
            server.close()
            await server.wait_closed()
            self._save_to_disk()
            try:
                sock.unlink()
            except OSError:
                pass
            logger.info("daemon stopped")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=False)
    parser.add_argument("--idle-shutdown", type=int, default=900)
    parser.add_argument("--persistence-path", type=Path, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    log_fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    root = logging.getLogger()
    root.setLevel(log_level)

    console = logging.StreamHandler()
    console.setFormatter(log_fmt)
    root.addHandler(console)

    log_dir = Path.home() / ".freqtrade" / "ftpairlist" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "daemon.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(log_fmt)
    root.addHandler(file_handler)

    from freqtrade.pairlist_cache.defaults import default_socket_path

    sock = args.socket or default_socket_path()

    daemon = PairlistCacheDaemon(
        sock,
        idle_shutdown_s=args.idle_shutdown,
        persistence_path=args.persistence_path,
    )

    def _sig(*_):
        daemon._shutdown.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    try:
        asyncio.run(daemon.serve())
    except Exception:
        logger.exception("daemon crashed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
