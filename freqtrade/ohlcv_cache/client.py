"""
Client for the shared OHLCV cache daemon.

Exposes an `OhlcvCacheClient` used by `CachedExchangeMixin`. Handles:
  * async connection to the Unix socket (JSON newline protocol)
  * auto-spawn of the daemon via subprocess.Popen if no daemon is running
  * graceful fallback when the daemon is unreachable

The caller is responsible for calling `.fetch()` from an asyncio context
(freqtrade's Exchange loop).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import random
import subprocess
import sys
import time
import uuid
from pathlib import Path

from freqtrade.enums import CandleType
from freqtrade.ohlcv_cache.defaults import (
    default_log_dir,
    resolve_global_config,
)
from freqtrade.ohlcv_cache.logger_setup import get_client_logger
from freqtrade.ohlcv_cache.protocol import dumps, loads_response


logger = get_client_logger()


class CacheUnavailable(RuntimeError):
    """Raised when the cache daemon is unreachable and fallback is needed."""


class CacheRateLimited(CacheUnavailable):
    """Raised when the daemon reports a rate-limit error (429).

    Callers should NOT fall back to direct ccxt — that would bypass the
    centralized rate limiter and make the situation worse.
    """


class CacheTimedOut(CacheUnavailable):
    """Raised when a daemon request timed out (busy processing other bots).

    Callers should skip this cycle and retry next time, NOT fall back to
    direct ccxt — the daemon is overloaded, not dead.
    """


# Process-wide cache of clients to avoid spawning multiple daemons within one bot
_CLIENT_SINGLETONS: dict[str, OhlcvCacheClient] = {}


class OhlcvCacheClient:
    # Priority constants — mirrors TokenBucket in daemon.py
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3

    def __init__(
        self,
        socket_path: str,
        timeout_s: float = 30.0,
        exchange_id: str = "",
        trading_mode: str = "spot",
        respawn_cfg: dict | None = None,
        dry_run: bool = False,
        capital: float = 0.0,
    ) -> None:
        self.socket_path = socket_path
        self.timeout_s = timeout_s
        self.exchange_id = exchange_id
        self.trading_mode = trading_mode
        self.dry_run = dry_run
        self.capital = capital
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        # Cached parameters needed to respawn the daemon if it has died.
        # Populated by get_or_spawn().
        self._respawn_cfg: dict | None = respawn_cfg
        self._bot_identity: dict | None = None
        self._registered = False
        self.hold_off_s: float = 0.0
        self.hold_off_reason: str = ""

    # ---------------- connection lifecycle

    async def _connect(self) -> None:
        # 16 MB reader buffer: a 5000-candle JSON response can exceed 400 KB,
        # well past asyncio's 64 KB readline() default which raises
        # LimitOverrunError silently and poisons the bot's _klines cache.
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_unix_connection(
                self.socket_path, limit=16 * 1024 * 1024,
            ),
            timeout=self.timeout_s,
        )

    def set_bot_identity(self, identity: dict) -> None:
        self._bot_identity = identity

    async def _ensure_connected(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        self._registered = False
        try:
            await self._connect()
        except (TimeoutError, FileNotFoundError, ConnectionRefusedError) as e:
            first_err = e
            # Daemon socket missing — try to respawn once if we have the info.
            if self._respawn_cfg is None:
                raise CacheUnavailable(
                    f"cannot connect to daemon: {first_err}",
                ) from first_err
            try:
                logger.info("daemon socket missing, attempting respawn")
                _ensure_daemon_running(**self._respawn_cfg)
                await self._connect()
            except (TimeoutError, FileNotFoundError, ConnectionRefusedError) as e:
                raise CacheUnavailable(
                    f"cannot connect to daemon after respawn: {e}"
                ) from e
            except CacheUnavailable:
                raise
        await self._auto_register()

    async def close(self) -> None:
        if self._writer is not None:
            if self._registered:
                try:
                    self._writer.write(dumps({
                        "op": "unregister",
                        "req_id": uuid.uuid4().hex,
                    }))
                    await self._writer.drain()
                except Exception:  # noqa: S110
                    pass
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: S110
                pass
        self._reader = None
        self._writer = None
        self._registered = False

    async def _auto_register(self) -> None:
        if self._registered or not self._bot_identity:
            return
        try:
            payload = {
                "op": "register",
                "req_id": uuid.uuid4().hex,
                **self._bot_identity,
            }
            if self._writer is None or self._reader is None:
                return
            self._writer.write(dumps(payload))
            await self._writer.drain()
            line = await asyncio.wait_for(
                self._reader.readline(), timeout=10.0,
            )
            if line:
                resp = loads_response(line)
                if resp.get("ok"):
                    self._registered = True
                    self.hold_off_s = float(resp.get("hold_off_s", 0))
                    self.hold_off_reason = resp.get("hold_off_reason", "")
                    logger.info(
                        "registered with fleet orchestrator "
                        "(fleet_size=%d hold_off=%.0fs reason=%s)",
                        resp.get("fleet_size", 0),
                        self.hold_off_s,
                        self.hold_off_reason or "none",
                    )
        except Exception as e:
            logger.debug("fleet register failed (non-fatal): %s", e)

    async def update_state(self, state: str, pairs_count: int = 0) -> None:
        try:
            await self._send_and_receive({
                "op": "state_update",
                "req_id": uuid.uuid4().hex,
                "state": state,
                "pairs_count": pairs_count,
            })
        except Exception as e:
            logger.debug("fleet state_update failed (non-fatal): %s", e)

    async def fleet_status(self) -> dict:
        return await self._send_and_receive({
            "op": "fleet_status",
            "req_id": uuid.uuid4().hex,
        })

    async def fleet_events(
        self, since_ts: float = 0, event_types: list[str] | None = None,
        bot_id: str | None = None, limit: int = 100,
    ) -> dict:
        payload: dict = {
            "op": "fleet_events",
            "req_id": uuid.uuid4().hex,
            "since_ts": since_ts,
            "limit": limit,
        }
        if event_types:
            payload["event_types"] = event_types
        if bot_id:
            payload["bot_id"] = bot_id
        return await self._send_and_receive(payload)

    # ---------------- request/response

    async def _send_and_receive(self, payload: dict) -> dict:
        async with self._lock:
            await self._ensure_connected()
            if self._writer is None or self._reader is None:
                raise CacheUnavailable("not connected after _ensure_connected")
            try:
                self._writer.write(dumps(payload))
                await self._writer.drain()
                line = await asyncio.wait_for(
                    self._reader.readline(), timeout=self.timeout_s,
                )
                if not line:
                    raise CacheUnavailable("daemon closed connection")
                return loads_response(line)
            except asyncio.CancelledError:
                # Outer wait_for (e.g. _ftcache_acquire_sync timeout)
                # cancelled us mid-request. The daemon may still send a
                # response that would poison the next readline(), so we
                # must tear down this connection.
                await self.close()
                raise
            except TimeoutError as e:
                await self.close()
                raise CacheTimedOut(
                    f"daemon timed out: {e.__class__.__name__}: {e}"
                ) from e
            except (
                ConnectionError, BrokenPipeError, ValueError, EOFError,
            ) as e:
                await self.close()
                raise CacheUnavailable(
                    f"i/o error with daemon: {e.__class__.__name__}: {e}"
                ) from e

    async def ping(self) -> dict:
        return await self._send_and_receive({"op": "ping", "req_id": uuid.uuid4().hex})

    def _compute_priority(self, since_ms: int | None, priority: int | None) -> int:
        """Determine request priority based on context.

        Explicit ``priority`` overrides automatic detection (allows callers
        to set CRITICAL for open-position pairs).
        """
        if priority is not None:
            return priority
        if self.dry_run:
            return self.LOW
        if since_ms is None:
            return self.HIGH
        return self.NORMAL

    async def fetch(
        self, pair: str, timeframe: str,
        candle_type: CandleType | str, since_ms: int | None,
        limit: int | None,
        priority: int | None = None,
    ) -> tuple[str, str, CandleType, list, bool]:
        """Return an OHLCVResponse compatible with
        freqtrade.exchange.exchange.Exchange._async_get_candle_history.

        ``priority`` overrides the auto-detected level (use
        ``OhlcvCacheClient.CRITICAL`` for pairs with open positions).
        """
        ct_str = candle_type.value if isinstance(candle_type, CandleType) else str(candle_type)
        req = {
            "op": "fetch",
            "req_id": uuid.uuid4().hex,
            "exchange": self.exchange_id,
            "trading_mode": self.trading_mode,
            "pair": pair,
            "timeframe": timeframe,
            "candle_type": ct_str,
            "since_ms": since_ms,
            "limit": limit,
            "priority": self._compute_priority(since_ms, priority),
            "capital": self.capital,
        }
        resp = await self._send_and_receive(req)
        if not resp.get("ok"):
            err_type = resp.get("error_type", "")
            err_msg = resp.get("error_message", "")
            if "429" in err_msg or "RateLimit" in err_type:
                raise CacheRateLimited(f"daemon rate-limited: {err_type} {err_msg}")
            raise CacheUnavailable(f"daemon error: {err_type} {err_msg}")
        try:
            ct_ret = CandleType(resp.get("candle_type", ct_str))
        except (ValueError, KeyError):
            ct_ret = candle_type if isinstance(candle_type, CandleType) else CandleType.SPOT
        data = resp.get("data", [])
        if not isinstance(data, list):
            logger.warning(
                "daemon returned data as %s for %s/%s — discarding",
                type(data).__name__, pair, timeframe,
            )
            data = []
        return (
            resp.get("pair", pair), resp.get("timeframe", timeframe), ct_ret,
            data, resp.get("drop_incomplete", True),
        )

    # ---------------- centralized rate limiter

    async def report_429(self, method: str = "", pair: str = "") -> None:
        """Notify daemon that a bot received a 429 on a direct ccxt call.

        The daemon will trigger backoff for ALL bots so subsequent requests
        are queued by priority instead of hitting the exchange.
        """
        try:
            await self._send_and_receive({
                "op": "report_429",
                "req_id": uuid.uuid4().hex,
                "exchange": self.exchange_id,
                "method": method,
                "pair": pair,
            })
        except (CacheUnavailable, CacheTimedOut, CacheRateLimited):
            pass

    async def acquire_rate_token(
        self, priority: int | None = None, cost: float = 1.0,
    ) -> None:
        """Acquire a rate token from the daemon's centralized TokenBucket.

        Bots must call this before any non-OHLCV REST call so that ALL
        API traffic from all bots shares the same rate limit.
        """
        prio = priority if priority is not None else (
            self.LOW if self.dry_run else self.HIGH
        )
        req = {
            "op": "acquire",
            "req_id": uuid.uuid4().hex,
            "exchange": self.exchange_id,
            "priority": prio,
            "capital": self.capital,
            "cost": cost,
        }
        resp = await self._send_and_receive(req)
        if not resp.get("ok"):
            if resp.get("throttled"):
                raise CacheRateLimited(
                    f"acquire shed: {resp.get('error_message')}"
                )
            raise CacheUnavailable(
                f"acquire failed: {resp.get('error_type')} {resp.get('error_message')}"
            )

    async def get_tickers(self, market_type: str = "") -> dict:
        """Get tickers from the daemon's shared cache (one fetch for all bots)."""
        req = {
            "op": "tickers",
            "req_id": uuid.uuid4().hex,
            "exchange": self.exchange_id,
            "trading_mode": self.trading_mode,
            "market_type": market_type,
        }
        resp = await self._send_and_receive(req)
        if not resp.get("ok"):
            err_type = resp.get("error_type", "")
            err_msg = resp.get("error_message", "")
            if "429" in err_msg or "RateLimit" in err_type:
                raise CacheRateLimited(f"tickers rate-limited: {err_type} {err_msg}")
            raise CacheUnavailable(f"tickers failed: {err_type} {err_msg}")
        return resp.get("data", {})

    async def push_positions(self, positions: list) -> None:
        """Push fetch_positions() result into the daemon's shared cache."""
        req = {
            "op": "positions_put",
            "req_id": uuid.uuid4().hex,
            "exchange": self.exchange_id,
            "data": positions,
        }
        resp = await self._send_and_receive(req)
        if not resp.get("ok"):
            raise CacheUnavailable(
                f"positions_put failed: {resp.get('error_type')} "
                f"{resp.get('error_message')}"
            )

    async def get_positions(self) -> tuple[bool, list]:
        """Get cached positions from the daemon. Returns (hit, data)."""
        req = {
            "op": "positions_get",
            "req_id": uuid.uuid4().hex,
            "exchange": self.exchange_id,
        }
        resp = await self._send_and_receive(req)
        if not resp.get("ok"):
            raise CacheUnavailable(
                f"positions_get failed: {resp.get('error_type')} "
                f"{resp.get('error_message')}"
            )
        return resp.get("hit", False), resp.get("data", [])

    async def push_balances(self, balances: dict) -> None:
        """Push get_balances() result into the daemon's shared cache."""
        req = {
            "op": "balances_put",
            "req_id": uuid.uuid4().hex,
            "exchange": self.exchange_id,
            "data": balances,
        }
        resp = await self._send_and_receive(req)
        if not resp.get("ok"):
            raise CacheUnavailable(
                f"balances_put failed: {resp.get('error_type')} "
                f"{resp.get('error_message')}"
            )

    async def get_markets(self) -> tuple[bool, dict]:
        """Get cached markets from the daemon. Returns (hit, data)."""
        req = {
            "op": "markets",
            "req_id": uuid.uuid4().hex,
            "exchange": self.exchange_id,
            "trading_mode": self.trading_mode,
        }
        resp = await self._send_and_receive(req)
        if not resp.get("ok"):
            err_type = resp.get("error_type", "")
            err_msg = resp.get("error_message", "")
            if "429" in err_msg or "RateLimit" in err_type:
                raise CacheRateLimited(f"markets rate-limited: {err_type} {err_msg}")
            raise CacheUnavailable(f"markets failed: {err_type} {err_msg}")
        return True, resp.get("data", {})

    async def get_balances(self) -> tuple[bool, dict]:
        """Get cached balances from the daemon. Returns (hit, data)."""
        req = {
            "op": "balances_get",
            "req_id": uuid.uuid4().hex,
            "exchange": self.exchange_id,
        }
        resp = await self._send_and_receive(req)
        if not resp.get("ok"):
            raise CacheUnavailable(
                f"balances_get failed: {resp.get('error_type')} "
                f"{resp.get('error_message')}"
            )
        return resp.get("hit", False), resp.get("data", {})

    async def get_funding_rates(self) -> tuple[bool, dict]:
        """Get cached funding rates from daemon (bulk fetch, all pairs)."""
        req = {
            "op": "funding_rates",
            "req_id": uuid.uuid4().hex,
            "exchange": self.exchange_id,
            "trading_mode": self.trading_mode,
        }
        resp = await self._send_and_receive(req)
        if not resp.get("ok"):
            err_type = resp.get("error_type", "")
            err_msg = resp.get("error_message", "")
            if "429" in err_msg or "RateLimit" in err_type:
                raise CacheRateLimited(f"funding_rates rate-limited: {err_type} {err_msg}")
            raise CacheUnavailable(f"funding_rates failed: {err_type} {err_msg}")
        return True, resp.get("data", {})

    async def get_leverage_tiers(self) -> tuple[bool, dict]:
        """Get cached leverage tiers from daemon (bulk fetch, all pairs)."""
        req = {
            "op": "leverage_tiers",
            "req_id": uuid.uuid4().hex,
            "exchange": self.exchange_id,
            "trading_mode": self.trading_mode,
        }
        resp = await self._send_and_receive(req)
        if not resp.get("ok"):
            err_type = resp.get("error_type", "")
            err_msg = resp.get("error_message", "")
            if "429" in err_msg or "RateLimit" in err_type:
                raise CacheRateLimited(f"leverage_tiers rate-limited: {err_type} {err_msg}")
            raise CacheUnavailable(f"leverage_tiers failed: {err_type} {err_msg}")
        return True, resp.get("data", {})

    # ---------------- spawn-on-demand

    @classmethod
    def get_or_spawn(
        cls, exchange_id: str, trading_mode: str, bot_config: dict,
    ) -> OhlcvCacheClient:
        """Return a process-wide singleton client for (exchange_id, trading_mode),
        spawning the daemon if necessary."""
        cache_cfg = bot_config.get("shared_ohlcv_cache") or {}
        global_cfg = resolve_global_config({
            k: v for k, v in cache_cfg.items()
            if k in {
                "socket_path", "lock_path", "log_path",
                "persistence_path", "flush_interval_s",
                "max_candles_per_series",
                "idle_daemon_shutdown_s", "client_timeout_s",
                "client_spawn_timeout_s", "client_stagger_s",
            }
        })
        socket_path = global_cfg["socket_path"]

        key = f"{exchange_id}:{trading_mode}:{socket_path}"
        existing = _CLIENT_SINGLETONS.get(key)
        if existing is not None:
            return existing

        respawn_cfg = {
            "socket_path": socket_path,
            "lock_path": global_cfg["lock_path"],
            "log_path": global_cfg["log_path"],
            "spawn_timeout_s": global_cfg["client_spawn_timeout_s"],
            "daemon_config": {
                "global": {
                    "idle_daemon_shutdown_s": global_cfg["idle_daemon_shutdown_s"],
                    "log_path": global_cfg["log_path"],
                    "persistence_path": global_cfg["persistence_path"],
                    "flush_interval_s": global_cfg["flush_interval_s"],
                    "max_candles_per_series": global_cfg["max_candles_per_series"],
                },
                "exchanges": cache_cfg.get("exchanges") or {},
            },
        }
        _ensure_daemon_running(**respawn_cfg)
        # Extract bot identity for priority scheduling
        dry_run = bool(bot_config.get("dry_run", False))
        capital = float(bot_config.get("dry_run_wallet", 0.0))
        if not dry_run:
            capital = float(bot_config.get("available_capital", capital))
        client = cls(
            socket_path=socket_path,
            timeout_s=float(global_cfg["client_timeout_s"]),
            exchange_id=exchange_id,
            trading_mode=trading_mode,
            respawn_cfg=respawn_cfg,
            dry_run=dry_run,
            capital=capital,
        )
        _CLIENT_SINGLETONS[key] = client
        logger.info("client configured for %s/%s via %s",
                    exchange_id, trading_mode, socket_path)

        stagger_max = float(global_cfg.get("client_stagger_s", 30))
        stagger_s = random.uniform(0, stagger_max)  # noqa: S311
        if stagger_s > 0.5:
            logger.info(
                "startup stagger: waiting %.1fs before first request", stagger_s,
            )
            time.sleep(stagger_s)

        return client


# ---------------- spawn helpers (module-level, sync)


def _is_socket_live(socket_path: str, timeout_s: float = 1.0) -> bool:
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect(socket_path)
    except (TimeoutError, FileNotFoundError, ConnectionRefusedError, OSError):
        return False
    else:
        return True
    finally:
        try:
            s.close()
        except Exception:  # noqa: S110
            pass


def _ensure_daemon_running(
    socket_path: str,
    lock_path: str,
    log_path: str,
    spawn_timeout_s: float,
    daemon_config: dict,
) -> None:
    """Fast-path check → flock acquire → subprocess.Popen → poll socket.

    Raises CacheUnavailable if the daemon cannot be started in time.
    """
    if _is_socket_live(socket_path):
        return

    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    default_log_dir().mkdir(parents=True, exist_ok=True)

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another bot is spawning — wait for socket to appear
            deadline = time.monotonic() + spawn_timeout_s
            while time.monotonic() < deadline:
                if _is_socket_live(socket_path):
                    return
                time.sleep(0.2)
            raise CacheUnavailable("timed out waiting for concurrent daemon spawn")

        # We hold the lock. Re-check in case a recent spawn completed.
        if _is_socket_live(socket_path):
            return

        logger.info("spawning ftcache daemon (socket=%s)", socket_path)
        log_f = Path(log_path).open("ab", buffering=0)
        try:
            subprocess.Popen(
                [
                    sys.executable, "-m", "freqtrade.ohlcv_cache.daemon",
                    "--socket", socket_path,
                    "--config", json.dumps(daemon_config),
                    "--log-level", "INFO",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log_f.close()

        # Poll for readiness
        deadline = time.monotonic() + spawn_timeout_s
        while time.monotonic() < deadline:
            if _is_socket_live(socket_path):
                logger.info("daemon is up on %s", socket_path)
                return
            time.sleep(0.2)
        raise CacheUnavailable(
            f"daemon did not become ready within {spawn_timeout_s}s (see {log_path})"
        )
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:  # noqa: S110
            pass
        try:
            os.close(lock_fd)
        except Exception:  # noqa: S110
            pass
