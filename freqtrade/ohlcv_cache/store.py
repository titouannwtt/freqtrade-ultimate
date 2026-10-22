"""
In-memory OHLCV store with timeframe-aligned merge + partial-range support.

One CandleSeries per (exchange, trading_mode, pair, timeframe, candle_type).
Candles are stored as a sorted, de-duplicated np.ndarray with shape (N, 6):
columns are [ts_ms, open, high, low, close, volume].

The store intentionally allows internal gaps (non-contiguous timestamps)
so it can represent unions of disjoint fetched ranges. The higher-level
`compute_gaps()` helper decides what additional fetches are needed when
a bot requests a range.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np


logger = logging.getLogger("ftcache.daemon")


# A candle received less than this long after its close is still treated as
# possibly forming: exchanges keep adjusting the last trades of a candle for a
# moment after the boundary.
PARTIAL_GRACE_MS = 5_000


@dataclass
class CandleSeries:
    exchange: str
    trading_mode: str
    pair: str
    timeframe: str
    candle_type: str
    tf_ms: int
    candles: np.ndarray = field(default_factory=lambda: np.empty((0, 6), dtype=np.float64))
    # Earliest timestamp the exchange serves for this series. None until a
    # fetch returns fewer candles than requested with a first_ts > our
    # since_ms, indicating we've hit the historic boundary. Subsequent
    # requests for ts < earliest_available_ts are clamped.
    earliest_available_ts: int | None = None
    # Wall-clock ms of the last successful live-refresh fetch (since_ms=None).
    # Used to decide if cached data is fresh enough to serve without re-fetching.
    last_live_refresh_wall_ms: int = 0
    last_fetch_monotonic: float = 0.0
    hits: int = 0
    misses: int = 0
    # Set to True by merge(), cleared by persistence layer after flush.
    dirty: bool = False
    # Open timestamps of candles that were received BEFORE they closed (volume
    # and close are a snapshot that only the next fetch of that candle corrects).
    # compute_gaps() refetches each of them once it is closed, for live AND
    # historical requests; without this a candle cached while forming stays
    # frozen mid-series for ever (the cached tail is the only thing refreshed).
    partial_ts: set[int] = field(default_factory=set)
    # False only for series loaded from a meta file written before partial_ts
    # existed: their frozen candles are unknown until the daemon scans them against
    # its finer series (frozen.detect_frozen, once, on first access). New series are
    # tracked from their first merge.
    partial_scanned: bool = True

    @property
    def range_start_ms(self) -> int | None:
        return int(self.candles[0, 0]) if len(self.candles) else None

    @property
    def range_end_ms(self) -> int | None:
        """Inclusive timestamp of the last cached candle."""
        return int(self.candles[-1, 0]) if len(self.candles) else None

    @property
    def n_candles(self) -> int:
        return len(self.candles)

    def align_down(self, ts_ms: int) -> int:
        return (ts_ms // self.tf_ms) * self.tf_ms

    def align_up(self, ts_ms: int) -> int:
        return -(-ts_ms // self.tf_ms) * self.tf_ms

    def slice_range(self, start_ms: int, end_ms_exclusive: int):
        """Candles with start_ms <= ts < end_ms_exclusive, as a numpy VIEW-COPY.

        ⚠️ Returns an ndarray, NOT a list-of-lists. The `.tolist()` this used to
        end with was, after orjson landed, the single largest CPU consumer in the
        daemon: 64% of a profile of 2034 samples, against 18.5% for the whole
        serializer. Converting 5000x6 float64 into 30 000 boxed Python floats,
        only for the serializer to walk them straight back out, is pure waste.
        orjson serializes the ndarray directly (`OPT_SERIALIZE_NUMPY`).

        ⚠️ CALLERS: an ndarray has no truth value. Test emptiness with `.size`,
        never with `if not rows`, which raises ValueError on more than one element.
        Both call sites live in `daemon.py` (_handle_fetch).
        """
        if len(self.candles) == 0:
            return self.candles[:0]
        ts = self.candles[:, 0]
        mask = (ts >= start_ms) & (ts < end_ms_exclusive)
        return self.candles[mask]

    def closed_partials(self, now_ms: int, start_ms: int, end_ms_exclusive: int) -> list[int]:
        """Sorted open timestamps of flagged candles in [start, end) that have closed."""
        if not self.partial_ts:
            return []
        limit = now_ms - self.tf_ms - PARTIAL_GRACE_MS
        return sorted(t for t in self.partial_ts if start_ms <= t < end_ms_exclusive and t <= limit)

    def _track_partials(
        self,
        new_ts: np.ndarray,
        fetched_at_ms: int,
        covered: tuple[int, int] | None,
    ) -> None:
        closed = new_ts + self.tf_ms + PARTIAL_GRACE_MS <= fetched_at_ms
        self.partial_ts.difference_update(int(t) for t in new_ts[closed])
        self.partial_ts.update(int(t) for t in new_ts[~closed])
        if covered is not None:
            # The window was asked for after the candles closed: whatever the
            # exchange answered (even nothing) is the final word, so a flag that
            # cannot be cleared never triggers a refetch loop.
            lo, hi = covered
            self.partial_ts = {
                t
                for t in self.partial_ts
                if not (lo <= t < hi and t + self.tf_ms + PARTIAL_GRACE_MS <= fetched_at_ms)
            }

    def merge(
        self,
        new_candles: list[list],
        fetched_at_ms: int | None = None,
        covered: tuple[int, int] | None = None,
    ) -> int:
        """Merge new candles into this series.

        `fetched_at_ms` is the wall-clock time BEFORE the request was sent. When
        given, candles that had not closed by then are flagged in `partial_ts`
        and flags of candles that had are cleared. `covered` is the [start, end)
        window of the request (see _track_partials).

        - De-duplicates by timestamp
        - Overwrites existing candles at matching timestamps (this is how
          retroactive corrections from `refresh_overlap_candles` work)
        - Keeps everything sorted
        - Returns the number of candles after merge
        """
        if not new_candles:
            if fetched_at_ms is not None and covered is not None:
                before = len(self.partial_ts)
                self._track_partials(np.empty(0, dtype=np.int64), fetched_at_ms, covered)
                self.dirty = self.dirty or len(self.partial_ts) != before
            return len(self.candles)
        new_arr = np.asarray(new_candles, dtype=np.float64)
        if new_arr.ndim != 2 or new_arr.shape[1] < 6:
            logger.warning(
                "merge: unexpected shape %s for %s %s",
                new_arr.shape,
                self.pair,
                self.timeframe,
            )
            return len(self.candles)
        # Drop any extra columns (Binance quote volume, KuCoin turnover, etc.)
        new_arr = new_arr[:, :6].copy()

        if len(self.candles) == 0:
            order = np.argsort(new_arr[:, 0], kind="stable")
            self.candles = new_arr[order]
        else:
            # Drop existing rows whose ts is being refreshed.
            new_ts = new_arr[:, 0].astype(np.int64)
            existing_ts = self.candles[:, 0].astype(np.int64)
            keep_mask = ~np.isin(existing_ts, new_ts)
            combined = np.concatenate([self.candles[keep_mask], new_arr])
            order = np.argsort(combined[:, 0], kind="stable")
            self.candles = combined[order]

        if fetched_at_ms is not None:
            self._track_partials(new_arr[:, 0].astype(np.int64), fetched_at_ms, covered)
        self.dirty = True
        self.last_fetch_monotonic = time.monotonic()
        return len(self.candles)

    def trim_to(self, max_candles: int) -> int:
        """Keep only the most recent max_candles. Returns # dropped."""
        if len(self.candles) <= max_candles:
            return 0
        dropped = len(self.candles) - max_candles
        self.candles = self.candles[-max_candles:]
        if self.partial_ts:
            first = int(self.candles[0, 0])
            self.partial_ts = {t for t in self.partial_ts if t >= first}
        self.dirty = True
        return dropped


class CandleStore:
    """Process-wide mapping of (exchange, trading_mode, pair, tf, ct) → series."""

    def __init__(self) -> None:
        self._series: dict[tuple[str, str, str, str, str], CandleSeries] = {}

    @staticmethod
    def make_key(
        exchange: str,
        trading_mode: str,
        pair: str,
        timeframe: str,
        candle_type: str,
    ) -> tuple[str, str, str, str, str]:
        return (exchange, trading_mode, pair, timeframe, candle_type)

    def get_or_create(
        self,
        exchange: str,
        trading_mode: str,
        pair: str,
        timeframe: str,
        candle_type: str,
        tf_ms: int,
    ) -> CandleSeries:
        k = self.make_key(exchange, trading_mode, pair, timeframe, candle_type)
        s = self._series.get(k)
        if s is None:
            s = CandleSeries(
                exchange=exchange,
                trading_mode=trading_mode,
                pair=pair,
                timeframe=timeframe,
                candle_type=candle_type,
                tf_ms=tf_ms,
            )
            self._series[k] = s
        return s

    def get(self, key: tuple) -> CandleSeries | None:
        return self._series.get(key)

    def all(self) -> list[CandleSeries]:
        return list(self._series.values())

    def put(self, series: CandleSeries) -> None:
        """Insert a pre-built series (used by persistence loader)."""
        k = self.make_key(
            series.exchange,
            series.trading_mode,
            series.pair,
            series.timeframe,
            series.candle_type,
        )
        self._series[k] = series
