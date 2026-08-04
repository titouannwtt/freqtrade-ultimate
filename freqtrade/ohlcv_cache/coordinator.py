"""
In-flight request coalescing.

When two bots (or two tasks in the same bot) request overlapping gaps for
the same series, only one fetch goes out; the others `await` the shared
future.

Keys are aligned-timestamp ranges, so concurrent requests that differ by a
few seconds still coalesce as long as they want the same candles.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable


logger = logging.getLogger("ftcache.daemon")


InflightKey = tuple[str, str, str, str, str, int, int]
# (exchange, trading_mode, pair, tf, ct, chunk_start_ms, chunk_end_ms_exclusive)


class RequestCoordinator:
    """Deduplicates concurrent identical fetches."""

    def __init__(self) -> None:
        self._inflight: dict[InflightKey, asyncio.Future] = {}

    async def run(
        self,
        key: InflightKey,
        fetcher: Callable[[], Awaitable[None]],
    ) -> None:
        """Either run `fetcher()` and broadcast its result, or await an
        already-running one for this key."""
        existing = self._inflight.get(key)
        if existing is not None:
            try:
                await existing
            except Exception:
                # Waiters never receive exceptions from other waiters'
                # fetches. The caller re-checks store state itself.
                pass
            return

        fut = asyncio.get_running_loop().create_future()
        # Swallow unawaited-exception asyncio warnings for this future.
        fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self._inflight[key] = fut
        try:
            await fetcher()
            if not fut.done():
                fut.set_result(None)
        except Exception:
            if not fut.done():
                # Signal completion to waiters without propagating the error.
                fut.set_result(None)
            raise
        finally:
            self._inflight.pop(key, None)

    def active_count(self) -> int:
        return len(self._inflight)
