"""Test harness: a private ftcache daemon and a simulated fleet, with no exchange behind it.

Used by the integration and compatibility tests of the priority-bot feature. Everything
here is isolated by construction:

* the daemon listens on a socket under the caller's ``tmp_path``, never on
  ``/tmp/ftcache-<uid>.sock``, and persists nothing (no ``persistence_path``);
* its exchange fetcher is a fake that sleeps instead of calling out, and the real
  ``ExchangeFetcher`` / positions clients are made to raise if anything reaches them, so
  that a test cannot, even by mistake, send a request to an exchange.

The fleet speaks the daemon's wire protocol (newline-delimited JSON) directly. One
connection per bot and one request in flight per connection, like the real client.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import subprocess
import sys
import threading
import time
import types
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from freqtrade.ohlcv_cache import daemon as daemon_mod


REPO_ROOT = Path(__file__).resolve().parents[1]
# The last commit of ``main`` before the priority-bot work: the daemon the fleet runs today.
LEGACY_REF = "0a19796c5"


def load_legacy_daemon_module() -> types.ModuleType | None:
    """The pre-PRIME ``daemon.py``, read from git, as a module of its own (or None)."""
    try:
        src = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"{LEGACY_REF}:freqtrade/ohlcv_cache/daemon.py"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not src.strip():
        return None
    module = types.ModuleType("ftcache_legacy_daemon")
    # dataclasses resolve string annotations through sys.modules[cls.__module__]
    sys.modules["ftcache_legacy_daemon"] = module
    exec(compile(src, f"<{LEGACY_REF}:daemon.py>", "exec"), module.__dict__)  # noqa: S102
    return module


# --------------------------------------------------------------------------- fake exchange


class FakeFetcher:
    """Replaces ``ExchangeFetcher``: bills the bucket like the real one, then just sleeps."""

    def __init__(self, budget, latency_s: tuple[float, float] = (0.05, 0.25), seed: int = 1):
        self.budget = budget
        self.latency_s = latency_s
        self._rng = random.Random(seed)
        self.calls: list[dict] = []

    async def _ensure_client(self):
        raise RuntimeError("no exchange behind the test daemon")

    async def fetch_ohlcv(
        self,
        pair,
        timeframe,
        since_ms,
        limit,
        candle_type,
        priority=2,
        capital=0.0,
        until_ms=None,
        meta=None,
    ):
        weight = 20 + math.ceil((limit or 1) / 60)
        await self.budget.acquire(weight, priority=priority, capital=capital)
        if meta is not None:
            meta["sent_at_ms"] = int(time.time() * 1000)
        self.calls.append({"pair": pair, "priority": priority, "capital": capital})
        await asyncio.sleep(self._rng.uniform(*self.latency_s))
        tf = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}[timeframe]
        start = since_ms if since_ms is not None else int(time.time() * 1000) - limit * tf
        start = (start // tf) * tf
        return [[start + i * tf, 1.0, 1.0, 1.0, 1.0, 1.0] for i in range(limit or 1)]

    async def close(self):
        return None


async def _no_positions_client(exchange, wallet):
    raise RuntimeError("no exchange behind the test daemon")


class PrivateDaemon:
    """A real ``Daemon.serve()`` on a private socket, in a background thread."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        daemon_module: types.ModuleType | None = None,
        global_cfg: dict | None = None,
        latency_s: tuple[float, float] = (0.05, 0.25),
    ):
        mod = daemon_module or daemon_mod
        self.module = mod
        self.socket_path = str(tmp_path / "ftcache-test.sock")
        cfg = {"socket_path": self.socket_path, "idle_daemon_shutdown_s": 3600}
        cfg.update(global_cfg or {})
        # No persistence_path on purpose: nothing is read from or written to ~/.freqtrade.
        self.daemon = mod.Daemon(socket_path=self.socket_path, global_cfg=cfg)
        self.fetcher: FakeFetcher | None = None
        self._latency_s = latency_s
        self.daemon._get_fetcher = self._get_fetcher
        self.daemon._positions_fetch_client = _no_positions_client
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self.error: BaseException | None = None

    def _get_fetcher(self, exchange, trading_mode):
        if self.fetcher is None:
            self.fetcher = FakeFetcher(self.daemon._get_budget(exchange), self._latency_s)
        return self.fetcher

    def _run(self):
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._started.set()
            loop.run_until_complete(self.daemon.serve())
        except BaseException as exc:  # surfaced to the test by stop()
            self.error = exc
        finally:
            # Client handlers still parked on a queue would be destroyed noisily at close.
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def start(self, timeout: float = 20.0) -> PrivateDaemon:
        self._thread = threading.Thread(target=self._run, name="ftcache-test-daemon", daemon=True)
        self._thread.start()
        self._started.wait(timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if Path(self.socket_path).exists():
                return self
            time.sleep(0.05)
        raise RuntimeError("test daemon did not come up")

    def call(self, fn: Callable, *args):
        """Run ``fn`` inside the daemon's event loop and return its result."""
        assert self.loop is not None
        done: asyncio.Future = asyncio.run_coroutine_threadsafe(self._wrap(fn, *args), self.loop)
        return done.result(timeout=10)

    @staticmethod
    async def _wrap(fn, *args):
        return fn(*args)

    def stop(self):
        if self.loop is not None and self._thread is not None:
            self.loop.call_soon_threadsafe(self.daemon.request_shutdown)
            self._thread.join(timeout=30)

    def __enter__(self) -> PrivateDaemon:
        return self.start()

    def __exit__(self, *exc):
        self.stop()


# --------------------------------------------------------------------------- fleet


class SimConn:
    """One bot connection: sequential requests, newline-delimited JSON."""

    def __init__(self, path: str):
        self.path = path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> SimConn:
        self._reader, self._writer = await asyncio.open_unix_connection(
            self.path, limit=16 * 1024 * 1024
        )
        return self

    async def request(self, payload: dict, wait_s: float = 120.0) -> dict:
        assert self._reader is not None and self._writer is not None
        payload.setdefault("req_id", uuid.uuid4().hex)
        self._writer.write((json.dumps(payload) + "\n").encode())
        await self._writer.drain()
        line = await asyncio.wait_for(self._reader.readline(), timeout=wait_s)
        return json.loads(line)

    async def register(self, bot_id: str, *, priority_bot: bool = False, dry_run: bool = False):
        return await self.request(
            {
                "op": "register",
                "bot_id": bot_id,
                "exchange": "hyperliquid",
                "trading_mode": "futures",
                "dry_run": dry_run,
                "priority_bot": priority_bot,
                "pid": 1,
                "state": "running",
            }
        )

    async def acquire(
        self, priority: int, cost: float, *, prime: bool = False
    ) -> tuple[float, dict]:
        started = time.monotonic()
        payload = {
            "op": "acquire",
            "exchange": "hyperliquid",
            "priority": priority,
            "cost": cost,
            "capital": 0.0,
        }
        if prime:
            payload["prime"] = True
        resp = await self.request(payload)
        return time.monotonic() - started, resp

    async def close(self):
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


@dataclass
class SimResult:
    waits: dict[str, list[float]] = field(default_factory=dict)
    shed: dict[str, int] = field(default_factory=dict)
    # requests still waiting when the run ended: label -> ages in seconds
    unserved: dict[str, list[float]] = field(default_factory=dict)
    weight_by_label: dict[str, float] = field(default_factory=dict)
    duration_s: float = 0.0
    peak_util_pct: float = 0.0
    mean_util_pct: float = 0.0
    util_samples: list[float] = field(default_factory=list)

    def add(self, label: str, wait: float, cost: float, ok: bool) -> None:
        if ok:
            self.waits.setdefault(label, []).append(wait)
            self.weight_by_label[label] = self.weight_by_label.get(label, 0.0) + cost
        else:
            self.shed[label] = self.shed.get(label, 0) + 1

    def row(self, label: str) -> dict[str, float]:
        w = self.waits.get(label, [])
        return {
            "n": len(w),
            "p50": percentile(w, 0.50),
            "p95": percentile(w, 0.95),
            "max": max(w) if w else float("nan"),
            "shed": self.shed.get(label, 0),
        }

    def oldest_unserved(self, label_prefix: str = "") -> float:
        ages = [a for k, v in self.unserved.items() if k.startswith(label_prefix) for a in v]
        return max(ages) if ages else 0.0

    def served_weight(self, label_prefix: str) -> float:
        return sum(w for k, w in self.weight_by_label.items() if k.startswith(label_prefix))

    def table(self) -> str:
        lines = [
            f"{'call':<32}{'n':>5}{'p50 s':>8}{'p95 s':>8}{'max s':>8}{'shed':>5}"
            f"{'unserved':>9}{'oldest s':>9}"
        ]
        for label in sorted(set(self.waits) | set(self.shed) | set(self.unserved)):
            r = self.row(label)
            ages = self.unserved.get(label, [])
            lines.append(
                f"{label:<32}{r['n']:>5}{r['p50']:>8.3f}{r['p95']:>8.3f}{r['max']:>8.3f}"
                f"{r['shed']:>5}{len(ages):>9}{(max(ages) if ages else 0):>9.1f}"
            )
        lines.append(
            f"budget used: mean {self.mean_util_pct:.0f}% peak {self.peak_util_pct:.0f}% "
            f"over {self.duration_s:.0f}s"
        )
        return "\n".join(lines)


def prefill_window(bucket, weight: float, span_s: float = 55.0, n: int = 55) -> None:
    """Make the 60 s weight window carry ``weight`` as if a fleet had been running."""
    now = time.monotonic()
    for i in range(n):
        bucket._weight_window.append((now - span_s + i * span_s / n, weight / n))
    bucket._weight_used_last_min = weight


# Trading calls of a priority bot: (name, weight, band today, band as a priority bot)
TRADING_MIX = [
    ("book", 2.0, 1, -1),  # fetch_l2_order_book: HIGH today (CRITICAL for an entry)
    ("fetch_order", 1.0, 1, -1),
    ("create_order", 1.0, 0, -1),
    ("cancel_order", 1.0, 0, -1),
    ("positions", 2.0, 1, -1),
]


async def run_fleet(  # noqa: C901
    path: str,
    *,
    daemon: PrivateDaemon,
    duration_s: float = 60.0,
    n_fleet: int = 30,
    n_prime: int = 2,
    priority_mode: bool = True,
    fleet_util_pct: float = 80.0,
    think_s: float = 25.0,
    burst_at: float | None = 5.0,
    burst_depth: int = 3,
    background_per_s: float = 0.12,
    trade_gap_s: float = 1.5,
    seed: int = 7,
) -> SimResult:
    """Run the simulated fleet against a private daemon and return the waits seen.

    The fleet is ``n_fleet`` closed-loop bots (one request in flight each, like the real
    client) that think ``think_s`` between requests, all hitting a candle close at
    ``burst_at`` with ``burst_depth`` back-to-back requests, plus an open-loop trickle of
    background refreshes (the daemon's own SWR traffic, mostly LOW). The weight window
    starts at ``fleet_util_pct`` of the budget. ``n_prime`` bots send trading calls.

    ``priority_mode`` False sends those trading calls in the bands used today (HIGH for
    books and order follow-up, CRITICAL for orders) and registers the bots as ordinary
    ones: the "before" picture, on whichever daemon is under test. Requests still
    waiting when the run ends are counted in ``result.unserved`` with their age.
    """
    rng = random.Random(seed)
    result = SimResult()
    bucket = daemon.daemon._get_budget("hyperliquid")
    daemon.call(prefill_window, bucket, 1140 * fleet_util_pct / 100)

    t0 = time.monotonic()
    stop_at = t0 + duration_s
    conns: list[SimConn] = []
    inflight: dict[int, tuple[str, float]] = {}
    ticket = iter(range(10**9))

    async def timed_acquire(conn: SimConn, label: str, band: int, cost: float, prime: bool = False):
        key = next(ticket)
        inflight[key] = (label, time.monotonic())
        try:
            wait, resp = await conn.acquire(band, cost, prime=prime)
        finally:
            inflight.pop(key, None)
        result.add(label, wait, cost, resp["ok"])
        return wait

    async def fleet_bot(i: int):
        conn = await SimConn(path).open()
        conns.append(conn)
        await conn.register(f"fleet-{i}")
        await asyncio.sleep(rng.uniform(0, think_s))
        burst_done = False
        while time.monotonic() < stop_at:
            band = rng.choices([1, 2, 3], weights=[15, 55, 30])[0]
            depth = 1
            if burst_at is not None and not burst_done and time.monotonic() - t0 >= burst_at:
                depth, burst_done = burst_depth, True  # the candle close
            for _ in range(depth):
                await timed_acquire(
                    conn, f"fleet {['', 'HIGH', 'NORMAL', 'LOW'][band]}", band, 21.0
                )
            await asyncio.sleep(rng.expovariate(1.0 / think_s))

    async def background():
        """Open-loop refreshes on throw-away connections (the daemon's own SWR traffic)."""
        pending = []
        while time.monotonic() < stop_at:
            await asyncio.sleep(rng.expovariate(background_per_s))

            chosen_band = rng.choice([1, 3, 3, 3])

            async def one(band=chosen_band):
                conn = await SimConn(path).open()
                try:
                    await timed_acquire(
                        conn, f"background {['', 'HIGH', '', 'LOW'][band]}", band, 21.0
                    )
                finally:
                    await conn.close()

            pending.append(asyncio.ensure_future(one()))
        await asyncio.gather(*pending, return_exceptions=True)

    async def prime_bot(i: int):
        conn = await SimConn(path).open()
        conns.append(conn)
        await conn.register(f"prime-{i}", priority_bot=priority_mode)
        await asyncio.sleep(rng.uniform(0, trade_gap_s))
        while time.monotonic() < stop_at:
            name, cost, today, prime = rng.choice(TRADING_MIX)
            band = prime if priority_mode else today
            wait = await timed_acquire(
                conn, "trading calls (priority bots)", band, cost, prime=priority_mode
            )
            if name in ("create_order", "cancel_order"):
                result.add("  of which orders", wait, cost, True)
            await asyncio.sleep(rng.expovariate(1.0 / trade_gap_s))

    async def sampler():
        while time.monotonic() < stop_at:
            await asyncio.sleep(1.0)
            result.util_samples.append(bucket.weight_used_last_min / 1140 * 100)

    tasks = [asyncio.ensure_future(fleet_bot(i)) for i in range(n_fleet)]
    tasks += [asyncio.ensure_future(prime_bot(i)) for i in range(n_prime)]
    tasks += [asyncio.ensure_future(background()), asyncio.ensure_future(sampler())]
    await asyncio.sleep(duration_s)
    now = time.monotonic()
    for label, started in inflight.values():
        result.unserved.setdefault(label, []).append(now - started)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for conn in conns:
        await conn.close()
    result.duration_s = time.monotonic() - t0
    if result.util_samples:
        result.mean_util_pct = sum(result.util_samples) / len(result.util_samples)
        result.peak_util_pct = max(result.util_samples)
    return result
