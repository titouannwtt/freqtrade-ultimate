# freqtrade/replay — dry-run replay harness

Drive the **real** live bot loop (`FreqtradeBot.process()`) candle-by-candle
against historical data, at full CPU speed, producing a normal Freqtrade SQLite
database. Because it runs the exact live code path (DCA, `custom_stake_amount`,
`custom_exit`, callbacks, sizing), it surfaces look-ahead / repaint / sizing
bugs that the vectorised backtester hides.

> **What it is:** a *dry-run-only behaviour-validation* tool — and a way to
> **seed a dry-run bot's history** ("simulate as if it had been running since
> date X").
>
> **What it is NOT:** a backtester for picking strategies. See
> `.claude-tips/replay.md` for the rule and the reasoning.

Adapted from the public [`saamy4r/Freqtrade_reply`](https://github.com/saamy4r/Freqtrade_reply)
proof-of-concept, rebuilt for this fork: Hyperliquid-native, funding-aware, and
**structurally incapable of live trading**.

---

## Two ways to use it

### A) FreqUI — per-bot action (primary, the everyday path)

On a **dry-run** bot, the Bot-comparison action menu (between *Analyze strategy*
and *Edit configuration*) shows **"Simulate dry-run (replay)"**. It:

- is **hidden on live bots** (and the backend refuses to seed a non-dry DB);
- is **greyed once already seeded** — to re-run, reset the bot's dry-run DB
  (the lock is a `KeyValueStore` marker *inside* that DB, so wiping it re-enables);
- opens a modal where you pick the **period** + **resolution** (and optionally tick
  **"reset the database first"**); strategy, current pairs (the live filter snapshot)
  and wallet are auto-derived;
- **seeds the bot's own dry-run database** then **auto-reloads the bot**, so the
  simulated history shows up and the dry-run continues from there.

While it runs: the replay is an **isolated background subprocess** scheduled by the
coordinator — you can close the modal/browser. The per-bot **indicator** shows
⏩ running (% + ETA) · ⏸ paused · 🕒 queued; the bot's metric cells are **greyed**
(simulated, not live), and completion auto-reloads the bot. When paused/queued the modal
shows a **queue panel** (all active replays machine-wide + a **Prioritize** button).
Clicking the indicator reopens the modal; while active the menu entry becomes
**"View running dry-run replay"** (blinking amber).

After a seed, clicking the indicator opens an **enriched detail view**: period,
resolution, duration, pairs, the replay's own result, the current combined result
(replay + live-dry since), and the number of trades taken outside the replay.

The modal also shows **data-coverage warnings** computed from the local feathers
(see `GET /replay/coverage`): earliest available data at the chosen resolution,
a warning if `start_date` precedes it, and a note if `end_date` is in the future.

### B) CLI — standalone / advanced

```bash
pip install -e ".[replay]"          # installs freezegun

python -m freqtrade.replay \
  --config user_data/config.json \
  --strategy MyStrategy \
  --timerange 20250101-20250601 \
  --sub-step 60                     # intra-candle resolution s (60=1m, 300=5m, 900=15m)
```

By default this writes a namespaced `*.replay.sqlite`; view it by running a
normal dry-run with `api_server` enabled pointed at that DB. `--seed` instead
writes into the `--db-url` you pass (a dry-run bot's own DB; refuses non-dry).
Data must be present locally (`--auto-download` is opt-in, off by default, to
control HL rate-limits). Include `1m` for faithful intra-candle fills.

---

## API endpoints (`freqtrade/rpc/api_server/api_replay.py`)

Available on **any bot** (not webserver-only), since the replay runs in an
isolated subprocess and never blocks the trade loop:

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/replay` | Start a seed replay (strategy, timerange, pairs, wallet, **sub_step**, **reset_db**, priority). Stops the bot's trading + submits to the coordinator. Refuses for live bots / if already seeded (unless `reset_db`). |
| `GET /api/v1/replay` | Poll this bot's state from the coordinator: `queued` / `running` / `paused` / `done` / `error` (+ `progress`, `elapsed_s`/`eta_s`, final summary). |
| `DELETE /api/v1/replay` | Cancel this bot's replay + resume the bot. |
| `POST /api/v1/replay/restore` | Restore the dry DB from the pre-replay backup (undo a crashed/unwanted seed). |
| `GET /api/v1/replay/seeded` | Whether this bot is dry-run + already replay-seeded (+ enriched marker for the detail view + `backup_available`). |
| `GET /api/v1/replay/coverage` | Local data coverage at a timeframe (feeds the modal's fidelity warnings). Date-column-only, mtime-cached → fast. |
| `GET /api/v1/replay/queue` | Machine-wide coordinator view: `capacity`, `cores`, `hyperopt_cores`, and the `running` / `paused` / `queued` replays. |
| `POST /api/v1/replay/reprioritize` | Bump a (possibly different) bot's replay up the global queue. |

> The replay freezes the **process-wide** clock (freezegun), so it always runs as a
> separate subprocess — spawned and supervised by the **coordinator daemon** (below),
> never inside the always-on bot/webserver process. Progress + the final summary are
> exchanged via a JSON `--progress-file`.

---

## Concurrency — the coordinator daemon

Replays are CPU-heavy and spawned by independent bot processes, so a single
machine-wide scheduler caps how many run at once:

    capacity = nproc − CORE_RESERVE(2) − <cores used by running hyperopts>

- **`coordinator.py`** — a tiny daemon (Unix socket, JSON-line protocol, same idiom as
  the ftcache daemon). Owns the queue + the running set. When over capacity (e.g. a
  hyperopt starts), the lowest-priority running replays are **paused** (`SIGSTOP` —
  frozen, 0 CPU, state kept in memory) and **resumed** (`SIGCONT`) once cores free up;
  a higher-priority job **preempts** a lower one. One replay per bot. Self-spawned on
  first use, persists its queue, re-adopts live replays after a restart.
- **`coordinator_client.py`** — thin client used by `api_replay`; degrades gracefully
  (a coordinator outage never takes a bot down). Hyperopt detection parses each running
  `freqtrade hyperopt`'s `-j/--job-workers` (no `-j` → all cores → hyperopt wins).

From FreqUI: a **queue panel** lists the active replays machine-wide; **Prioritize**
bumps one to the front. Per-bot indicator: ⏩ running · ⏸ paused · 🕒 queued.

## Bot lifecycle around a seed

A replay must be the **sole writer** of the dry DB (no concurrency → no `StaleDataError`,
no scrambled dates). This fork's worker `sys.exit(0)`s on RUNNING→STOPPED (auto-restart),
so we do **not** stop the bot. Instead, while a replay is pending, `FreqtradeBot.process()`
**short-circuits** (skips the whole trading cycle); on completion the bot does
`RELOAD_CONFIG` to re-open the seeded DB and resume. See `lifecycle.py`.

## Config-driven auto-launch

A dry bot can seed itself on startup:

```jsonc
"dry_run_replay": {
  "automatic_launch": true,        // required
  "start_date": "01/01/2026",      // required, DD/MM/YYYY
  "end_date": "today",             // default "today"
  "resolution": "1m",              // default "1m" (1m|5m|15m)
  "reset_db": false                // default false
}
```

`lifecycle.maybe_autolaunch_replay` (called from `process()`) fires **once**, when the
bot is dry-run, not already seeded, and its pairlist is ready (retries until then).
Idempotent (the seed marker prevents re-firing); never runs on a live bot.

## Database integrity (open positions)

The bot's **real** trades are sacred; replay trades yield. Guarantees:

- **`reset_db=false`** (default) — existing trades preserved; the replay window is
  **capped at the first real (non-`[replay]`) trade**, so it never overlaps real ones.
  Post-seed reconciliation closes replay-left-open trades that collide with a real open
  trade's pair, or exceed `max_open_trades`, at their entry price
  (`exit_reason="replay_truncated"`, kept in history) — real positions untouched.
- **`reset_db=true`** — wipes trade rows via `DELETE` (**not** unlinking the file the bot
  holds open).
- Always: **full DB backup before** (`<db>.pre-replay.bak`) + **`PRAGMA quick_check`
  after** with **auto-restore** if corrupt → the DB is never left in a bad state.

---

## How it works

| Component | Role |
|---|---|
| `coordinator.py` / `coordinator_client.py` | Machine-wide scheduler daemon + client: global limit, queue, priority, pause/resume, hyperopt-aware (see above). |
| `lifecycle.py` | Bot ↔ replay glue: pause trading during a seed, resume on completion; config auto-launch; cancel. |
| `safety.py` | Hard gate: forces `dry_run=True`, blanks every credential (incl. HL `walletAddress`/`privateKey`), namespaces the DB to `*.replay.sqlite` (or, in **seed mode**, keeps the bot's dry DB), refuses non-sqlite. |
| `clock.py` | `VirtualClock` — `freezegun` freezes the process-wide clock; `advance_to()` jumps it forward. |
| `data_store.py` | Serves OHLCV + mark + funding-rate feathers as time-gated slices, lazy-loaded; `date_range()` is a cheap (date-column-only, mtime-cached) coverage probe. |
| `exchange.py` | Mixin over the **real resolved exchange class** (`Hyperliquid`). Mocks ccxt; serves data; synthesises the order book. Inherits dry-run order filling, fees, **and funding**. |
| `runner.py` | Orchestrates: enforce safety → seed-DB prep (reset/cap) → static pairlist → validate/drop pairs → start clock → patch resolver/wallet/analyze/trade-cache → step `bot.process()` → reconcile open trades → enriched seed marker → integrity check. |
| `cli.py` | `python -m freqtrade.replay` entry point (`--seed`, `--sub-step`, `--reset-db`). |

### Three independent safety layers (any one suffices)

1. `dry_run` forced to `True` and re-asserted — Freqtrade only hits real order
   endpoints when `dry_run` is `False`.
2. Every credential blanked — no authenticated request can be signed.
3. ccxt replaced by a `MagicMock` — there is no network client to call.

Plus, **seed mode refuses any config whose `dry_run` is not `True`**, so a live
bot's trade history can never be overwritten.

### Funding & fees — done right

In dry-run, Freqtrade computes funding from mark + funding-rate candles
(`_fetch_and_calculate_funding_fees` → `refresh_latest_ohlcv`). We **serve those
candles** instead of overriding `get_funding_fees`, so funding fees are real —
unlike the original PoC which zeroed them. `get_fee` honours `config["fee"]`
else the market maker/taker per order type (limit → maker).

### What the runner neutralises from a live config

- `dry_run_wallet` placeholder → `--wallet` (default 1000); `available_capital`
  → `--wallet`; `capital_withdrawal` → 0 (else deployable capital is zeroed).
- `pair_blacklist` cleared; pairlists pinned to `StaticPairList`.
- `shared_ohlcv_cache` + `persist_klines` disabled — never touches the live
  bots' on-disk klines cache.
- Pairs without local data are **skipped** (seed mode) instead of aborting the run.

---

## Performance

The loop runs `bot.process()` every sub-step. Two **faithful** optimisations
(replay-scoped monkeypatches, byte-identical results — see the regression tests)
cut redundant per-tick work, ~1.4× faster at 1m resolution:

1. **Analyze short-circuit** — skip `strategy.analyze_pair()` between candle
   closes (the served candles, and thus the cached analysed dataframe, are
   unchanged; freqtrade otherwise re-runs `remove_entry_exit_signals` every tick).
2. **Open-trades cache** — memoise `Trade.get_open_trades()` between SQLAlchemy
   flushes (the ORM round-trip dominated the profile; the set only changes on a
   create/adjust/close, i.e. a flush).

The **resolution** (`--sub-step` / modal selector) is the user-chosen lever:
coarser = much faster but less faithful (it changes time-granularity of
callbacks), so it is *not* part of the "faithful" set.

---

## Known divergences from live (read before trusting numbers)

| # | Divergence | Impact |
|---|---|---|
| 1 | **Static pairlist** = the bot's *current* filter snapshot; dynamic pairlists aren't replayed. | Survivorship/look-ahead on pair *selection*. |
| 2 | **Synthetic order book** = single price ± half-spread, infinite depth. | Optimistic fills; no size-dependent slippage. |
| 3 | **Candle-granular fills.** Without 1m data, exit/entry limit orders only fill at candle granularity → freqtrade's `unfilledtimeout` cancels more limit exits than live would (the trade then re-exits / ROI / stoploss). | Different exit timing vs live. Download 1m data to reduce. |
| 4 | **`MAX_CANDLES`** feed cap = `startup_candle_count + 200`. | Sufficient warmup; raise if a strategy needs more. |
| 5 | **Daily wallet snapshot** (once per simulated day). | Intra-day equity not captured. |
| 6 | Order/amount **precision** uses DECIMAL_PLACES, not HL significant-figures. | Negligible. |

---

## Tests

`tests/replay/` (run with `.venv` so freezegun is present) — ~104 tests:

- **Unit** (`test_replay_unit.py`): safety gate, data-store slicing, exchange overrides.
- **End-to-end** (`test_replay_e2e.py`): deterministic replay with an **exact frozen
  baseline** at 15m + 1m (fidelity lock), seed marker, live-config refusal, skip-missing.
- **Coordinator** (`test_coordinator.py`): scheduling, capacity math, hyperopt-`-j`
  parsing, queue/pause/resume, priority preemption, cancel — plus farfelu inputs.
- **DB integrity** (`test_reset_reconcile.py`): truncate, first-real-trade cap, open-trade
  reconciliation (pair conflict / MOT), quick-check.
- **Auto-launch** (`test_autolaunch.py`): date parsing (DD/MM/YYYY, `today`, garbage),
  config validation, the one-shot gating (dry/seeded/pairs-ready/live).

The coordinator's pause/resume was also validated against **real subprocesses**
(`SIGSTOP`/`SIGCONT` confirmed at the kernel level via `/proc/<pid>/stat`).

---

## Deployment

- `pip install -e ".[replay]"` (freezegun extra — opt-in; live `pip install -e .`
  stays lean).
- Engine/CLI changes (`runner.py`, `exchange.py`, `data_store.py`, `coordinator.py`,
  `coordinator_client.py`) run in a **subprocess** → take effect on the next run, **no
  bot restart**. The coordinator daemon **self-spawns** on first use (and re-reads its
  code each spawn); kill it (`pkill -f freqtrade.replay.coordinator`) to pick up changes
  while one is alive.
- Bot-process changes (`api_replay.py`, `api_schemas.py`, `lifecycle.py`,
  `freqtradebot.py` hook) → **require restarting the (dry-run) bot**.
- FreqUI changes → rebuild + install the UI (`npm run build` in the FreqUI fork,
  then `freqtrade install-ui` or copy `dist/*` into `ui/installed/`), browser refresh.

> The coordinator daemon runs under the user's uid on `/tmp/ft-replay-coord-<uid>.sock`.
> One per machine; shared by all bots.
