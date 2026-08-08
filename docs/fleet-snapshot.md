# Fleet snapshot

One request that describes every bot, instead of one request per bot per datum.

If you run a single bot, this changes nothing for you and costs nothing: the digest is a
few fields pushed to a local socket once per cycle.

## The problem

A dashboard watching N bots polls each of them for status, profit, balance, trades and so
on. The request count is N × M, and — this is the part that hurts — the expensive work
happens *inside the request handlers*, competing with trading: ORM hydration, pandas
aggregates, and in the worst case an exchange round-trip inheriting the rate limiter's
queue.

Measured on a fleet of 55 bots, a full dashboard page load took **118.7 s of wall time**,
of which `/balance` alone accounted for 54.8 s.

## The approach

Each bot pushes a compact digest of itself to the shared ftcache daemon at the end of
every cycle. The daemon keeps the latest digest per bot. Any bot can then serve the whole
fleet in one response:

```bash
curl -u user:pass http://127.0.0.1:8080/api/v1/fleet/snapshot
```

```json
{
  "ok": true,
  "ts": 1786200000.0,
  "bot_count": 55,
  "bots": {
    "hippo_original": {
      "state": "State.RUNNING",
      "dry_run": false,
      "exchange": "hyperliquid",
      "strategy": "HippoDCA",
      "open_trade_count": 4,
      "max_open_trades": 5,
      "closed_profit_abs": 128.44,
      "open_profit_abs": -3.12,
      "balance_total": 1042.7,
      "wallet_age_s": 6.2,
      "age_s": 1.8
    }
  }
}
```

`max_age_s` omits bots whose digest is older than the given number of seconds.

## Two design rules

**Only data the bot already has.** A digest that recomputed `/profit`-style aggregates
every cycle would *move* the cost onto the trading loop rather than remove it. So the
expensive aggregates are deliberately absent, and a client that needs them still asks the
bot directly. `open_profit_abs` is best-effort for the same reason: it needs current
rates, so it is read from the rate cache and **omitted** when that cache cannot answer —
absent beats a figure computed from stale rates.

**Every entry carries its age.** Digests are pushed on each bot's own cycle, so a client
must be able to tell a live figure from one left behind by a bot that has since stopped.
`age_s` is per bot; `wallet_age_s` additionally reports how old the balance snapshot
behind the figures is. A monitoring dashboard that hides staleness is worse than a slow
one.

## Compatibility

Nothing here is required. Three independent failure modes all degrade to today's
behaviour:

- **No daemon** — the endpoint returns `{"error": ...}`; a client falls back to polling
  the bots directly.
- **Older daemon** — the protocol rejects unknown operations explicitly
  (`{"ok": false, "error_type": "UnknownOp"}`), per operation, so a new client against an
  old daemon gets a clear answer rather than a silent hang.
- **Push failure** — swallowed. A dashboard convenience must never be able to disturb
  trading; the snapshot simply ages out.

## Measuring it

`user_data/bench_dashboard.py` times every endpoint across the fleet concurrently — the
way a browser issues them — and can diff two runs:

```bash
python user_data/bench_dashboard.py --json before.json
# ... change something, restart the fleet ...
python user_data/bench_dashboard.py --compare before.json
```

Wall time and the p95 tail matter more than the median here: a freeze is caused by the
slowest bot, not the average one.

## What was attempted and rejected: caching `/profit`

`/profit` has an irreducible pandas floor of ~50 ms — measured on a bot holding **2**
trades, so it is setup cost, not data cost. Across 55 bots that is ~2.8 s of pure CPU per
dashboard refresh, recomputing figures that in most cases cannot have moved. Caching it
looks like the obvious win.

Two attempts, both rejected:

**A 10-second TTL.** Wrong for a reason worth stating: it serves figures a client can
prove are outdated. Right after a trade closes, the numbers a dashboard reads are money
that has already moved.

**A content-addressed key** — (closed trade count, last close date, open trade count) —
was exact for realised profit and still wrong overall. Unrealised profit on open positions
is derived from **live rates**, which no cheap token captures. The existing test suite
caught it precisely: a test that makes `get_rate` fail expects `profit_all_coin` to become
NaN, and the cache kept returning the previous successful figure.

Doing this correctly means splitting the computation — cache the realised part, always
recompute the unrealised part — which is a change to money arithmetic and deserves its own
pass rather than being bolted onto a performance sweep. The measurement stands; the
implementation is deliberately left undone.
