"""
Gap computation for partial-range OHLCV fetches.

Pure function module — no I/O, no state. Easy to unit-test.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Gap:
    """Half-open aligned range [start_ms, end_ms) we still need to fetch."""

    start_ms: int
    end_ms: int  # exclusive

    def n_candles(self, tf_ms: int) -> int:
        return max(0, (self.end_ms - self.start_ms) // tf_ms)


def compute_gaps(
    requested_start_ms: int,
    requested_end_ms: int,
    cached_start_ms: int | None,
    cached_end_ms: int | None,
    tf_ms: int,
    *,
    refresh_overlap_candles: int = 0,
    earliest_available_ts: int | None = None,
    stale_ts: Sequence[int] = (),
) -> list[Gap]:
    """Decide which ranges must be fetched to cover [requested_start, requested_end).

    All timestamps assumed aligned to timeframe boundaries and in ms.
    - requested_end_ms is EXCLUSIVE (= last_wanted_ts + tf_ms).
    - cached_end_ms is INCLUSIVE (the ts of the last cached candle), or None.

    Args:
        refresh_overlap_candles: when the requested range extends past the
            cached tail, re-fetch the last N cached candles too. Catches
            retroactive corrections that some exchanges apply to recent
            candles (HL on low-liquidity pairs, Gate futures).
        earliest_available_ts: known historic lower bound for this series.
            Requests for ts < earliest_available_ts are clamped.
        stale_ts: open timestamps of cached candles that were stored while
            still forming and have closed since (CandleSeries.closed_partials).
            They are refetched whatever their position: the refresh overlap only
            ever reaches the last few candles, so a frozen candle mid-series
            would otherwise be served as complete for ever. One contiguous gap
            from the first to the last of them (a fetch costs a base weight plus
            1 per 60 candles, so one wide call beats many narrow ones).

    Returns:
        A list of disjoint Gaps (possibly empty). Caller is expected to
        further chunk them by max_candles_per_call and coalesce in-flight.
    """
    if earliest_available_ts is not None:
        requested_start_ms = max(requested_start_ms, earliest_available_ts)
    if requested_start_ms >= requested_end_ms:
        return []
    # Nothing cached → one big gap
    if cached_start_ms is None or cached_end_ms is None:
        return [Gap(requested_start_ms, requested_end_ms)]
    gaps = _base_gaps(
        requested_start_ms,
        requested_end_ms,
        cached_start_ms,
        cached_end_ms,
        tf_ms,
        refresh_overlap_candles,
    )
    stale = [t for t in stale_ts if requested_start_ms <= t < requested_end_ms]
    if stale:
        gaps = _union([*gaps, Gap(min(stale), max(stale) + tf_ms)])
    # Drop any gap that spans less than one full candle (defensive: a
    # sub-candle gap has nothing to fetch and loops forever otherwise).
    return [g for g in gaps if g.end_ms - g.start_ms >= tf_ms]


def _union(gaps: list[Gap]) -> list[Gap]:
    out: list[Gap] = []
    for g in sorted(gaps, key=lambda x: x.start_ms):
        if out and g.start_ms <= out[-1].end_ms:
            out[-1] = Gap(out[-1].start_ms, max(out[-1].end_ms, g.end_ms))
        else:
            out.append(g)
    return out


def _base_gaps(
    requested_start_ms: int,
    requested_end_ms: int,
    cached_start_ms: int,
    cached_end_ms: int,
    tf_ms: int,
    refresh_overlap_candles: int,
) -> list[Gap]:
    # Cached boundaries can be off the timeframe grid when the exchange
    # returns a listing candle at a non-aligned timestamp (e.g. a pair
    # listed mid-candle). Align them down so we never compute a sub-candle
    # gap from the misalignment — such a gap spans zero candles, can never
    # be satisfied, and would be re-fetched on every cycle forever.
    cached_start_ms = (cached_start_ms // tf_ms) * tf_ms
    cached_end_ms = (cached_end_ms // tf_ms) * tf_ms

    cached_end_exclusive = cached_end_ms + tf_ms
    gaps: list[Gap] = []

    # ----- prefix gap (we need ts older than cache)
    if requested_start_ms < cached_start_ms:
        gaps.append(Gap(requested_start_ms, min(cached_start_ms, requested_end_ms)))

    # ----- suffix gap (we need ts newer than cache) + refresh overlap
    if cached_end_exclusive < requested_end_ms:
        overlap_ms = refresh_overlap_candles * tf_ms
        suffix_start = cached_end_exclusive - overlap_ms
        # Don't rewind past what's been requested
        suffix_start = max(suffix_start, requested_start_ms)
        # Don't rewind into a prefix gap we already added
        if gaps and suffix_start < gaps[-1].end_ms:
            suffix_start = cached_end_exclusive
        gaps.append(Gap(suffix_start, requested_end_ms))

    return gaps


def chunk_gap(gap: Gap, max_candles_per_chunk: int, tf_ms: int) -> list[Gap]:
    """Split an oversized gap into exchange-compatible chunks."""
    if max_candles_per_chunk <= 0:
        return [gap]
    chunk_ms = max_candles_per_chunk * tf_ms
    out: list[Gap] = []
    cur = gap.start_ms
    while cur < gap.end_ms:
        nxt = min(cur + chunk_ms, gap.end_ms)
        out.append(Gap(cur, nxt))
        cur = nxt
    return out
