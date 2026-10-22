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
  close (`merge(..., fetched_at_ms=...)`, taken AFTER the rate-limiter wait, when the
  request really leaves; 5 s grace). Persisted in the `.meta.json` file.
- `gaps.compute_gaps(stale_ts=...)` / `daemon._handle_fetch`: every flagged candle that
  has closed is refetched once, live or historical request, in one contiguous call
  (`until` is passed to HL so the call is billed for its real span).
- Steady state costs nothing extra: for live requests the flagged candles inside the
  last `refresh_overlap_candles` of the cached tail are left to the suffix gap of the
  next close (they are refetched there anyway). Pinned by
  `TestSteadyStateNoExtraFetch` (0 extra fetches over several closes).
- Legacy series (meta files written before the fix, no `partial_scanned`): the daemon
  scans each one itself on first access (`frozen.detect_frozen`, against the finer
  series of its own store), flags the frozen candles and heals them lazily. There is
  no offline writer, hence no race with the daemon.
- `CachedExchangeMixin.ftcache_authoritative_fetch()`: `get_historic_ohlcv` and the
  parallel download path read the exchange (rate limited, LOW priority) instead of the
  cache, so a download never depends on cached data.

Coverage limits of the legacy scan: a candle is only checked against finer series held
in the cache, each 5000 candles deep (5m ~17 days, 15m ~52 days, 30m ~104 days, 1h ~208
days). The finest timeframe (5m) cannot be checked at all (no 1m), and 1h candles older
than ~104 days or 30m older than ~52 days have no finer source. The figure of
`scripts/ftcache_flag_partials.py` (7080 on the 2026-10-22 copy) is therefore a lower
bound; what cannot be checked ages out through `trim_to()`.

## Operations

**Merging is deploying.** Bots relaunch the daemon from the editable install whenever
its socket is missing (`client.py` respawn), so the daemon runs the new code as soon as
it is restarted, by anyone, for any reason. Nothing else has to be done in the daemon.

1. Merge the branch (the download-data cron is fixed at once, no restart needed).
2. Repair the on-disk data: `scripts/repair_partial_candles.py` (dry-run by default,
   `--apply --backup-dir DIR`, nothing else reading `user_data/data`).
3. The running daemon keeps the OLD code until it is restarted (it is a long-lived
   process): it keeps serving its frozen candles to the bots. A restart costs hours of
   catch-up for the fleet: pick a quiet window (equity markets closed), once. After it,
   the new daemon scans and heals its legacy series by itself, lazily, through its rate
   limiter (`legacy partial scan:` lines in `daemon.log`).
4. `scripts/ftcache_flag_partials.py --root <copy of the persistence directory>` is a
   read-only estimate of what step 3 will find.
