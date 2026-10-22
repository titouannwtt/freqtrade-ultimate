# ftcache: candles stored while forming

## Problem (2026-04-24 to fix)

The daemon merged every fetched candle into its series, including the one still
forming. Only the cached tail (plus 3 candles for live requests) was ever
refetched, so a candle cached while forming stayed frozen mid-series and was
served as complete: to `download-data` (stored on disk for good, never rewritten
because the incremental download starts after the last stored candle) and to live
bots (indicator history, informatives).

## Fix

- `store.CandleSeries.partial_ts`: open timestamps of candles received before their
  close (`merge(..., fetched_at_ms=...)`). Persisted in the `.meta.json` file.
- `gaps.compute_gaps(stale_ts=...)` / `daemon._handle_fetch`: every flagged candle that
  has closed is refetched once, live or historical request, in one contiguous call.
- `CachedExchangeMixin.ftcache_authoritative_fetch()`: `get_historic_ohlcv` and the
  parallel download path read the exchange (rate limited, LOW priority) instead of the
  cache, so a download never depends on cached data.

## Operations

1. Merge the branch (the download-data cron is fixed at once, no restart needed).
2. Repair the on-disk data: `scripts/repair_partial_candles.py` (dry-run by default,
   `--apply --backup-dir DIR`, nothing else reading `user_data/data`).
3. Daemon (restart needed to run the new code; read the restart-cost notes first, a
   restart costs hours of catch-up for the fleet, so pick a quiet window):
   stop the daemon, run `scripts/ftcache_flag_partials.py --root ~/.freqtrade/ftcache
   --apply --backup-dir DIR` (flags the frozen candles of the persisted cache), start it.
   The daemon then heals the flagged series lazily, through its own rate limiter.
   Without step 3 the old daemon keeps serving its frozen candles to the bots.
