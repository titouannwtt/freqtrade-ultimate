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

## Persisted cache: timestamp unit bug (found in review, fixed with this change)

`persistence._load_one` turned dates into ms with `astype("int64") // 10**6`, right only
for nanosecond files. pandas 3 (the daemon's venv) writes millisecond files, so every
such file loaded as 1970: `range_start_ms` sat in 1970, `compute_gaps` never created a
prefix gap, and warmups/history were served truncated. Each restart then flushed the
polluted state back to disk. Measured on a copy (2026-10-22): 1538 of 2482 persisted
Hyperliquid series hold 4.13M rows dated 1970 and a handful of real rows.

Fix: the unit is normalised explicitly (works on pandas 2.3.3 and 3.0.3), rows dated
before 2017-01-01 are dropped at load with a warning (their real timestamps are lost),
the range is recomputed from the clean rows so the prefix gap is refetched, and the
writer always writes `timestamp[ms, tz=UTC]`.

## Restart procedure

Do not restart the daemon for anything else than a reason that justifies it; this one
does (frozen candles served to bots, truncated histories). Restart ONCE.

**Before**
1. Merge the branch. Merging = deploying: the next relaunch (by anyone) runs the new code.
2. Back up the persistence directory (the first flush rewrites cleaned files):
   `cp -r ~/.freqtrade/ftcache/hyperliquid <backup dir>` (about 210 MB).
3. Quiet window: equity/commodity markets closed (weekend), not during a 429 backoff
   (`backoff=off` in the `STATS` line of `~/.freqtrade/ftcache/logs/daemon.log`).
   Reconnection of the fleet (not the refetch) is the main cost: budget a long catch-up,
   see your restart notes.

**Cost of the history refetch** (estimate, `scripts/ftcache_restart_estimate.py` on a
copy, request = 5000 candles, Hyperliquid weight: 20 per call + 1 per 60 candles,
5000 candles per call; measured 2026-10-22):

| Scope | Weight | At the full 1140/min budget |
|---|---|---|
| Series live-refreshed in the last 24 h (what bots ask for) | ~12 750 | ~11 min |
| Static-whitelist bots' main + informative + funding series | ~28 700 (25 000 funding) | ~25 min |
| Every persisted series (never reached: refetch is lazy) | ~392 000 | ~5.7 h |

For comparison, restarting WITHOUT this loader fix makes all ~2190 millisecond files
load as 1970: the whole cache is refetched.
Baseline load before the restart: median 78 % of the 1140/min budget over 12 h (p90 82 %),
so the refetch competes with the existing traffic; the figures above are a floor.

**How it is paced.** The refetch is the prefix gap of a normal request, i.e. synchronous
(not SWR), at the request's priority: live bot with `since_ms=None` HIGH, dry bots LOW,
open-position pairs and bots in their init phase CRITICAL. The token bucket refills at
19 weight/s (burst 150); non-critical grants wait while the last minute used more than
80 % of the budget; strict priority with a starvation grant for lower bands, so LOW
(dry) refreshes are delayed while HIGH/CRITICAL refetches run. A 429 triggers graduated
backoff (NORMAL/LOW shed first). A client waiting more than 240 s gets `CacheTimedOut`
and retries on its next cycle (it does not fall back to ccxt).

**Restart**: `kill -TERM <daemon pid>`; the first bot request respawns it. Do not
restart twice (each restart restarts the catch-up). Wait for a full bot cycle before
concluding anything.

**Check**
- `daemon.log`: `dropping N row(s) dated before 2017` (expected for ~1538 series, once),
  then `legacy partial scan: ... flagged N frozen candle(s)` lines as series are first read.
- `STATS` line: `backoff=off`, `429s=` not moving, `w=` back under ~80 % after the catch-up,
  `queue=` draining.
- A bot's dataframe for a 1h/4h/1d pair has its full history again.
