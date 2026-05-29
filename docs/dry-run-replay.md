# Dry-Run Replay

> Validate a strategy in hours instead of months — by replaying historical data through the
> **real bot engine**, not the simplified backtester.

## What is it?

Dry-run replay drives `FreqtradeBot.process()` — the exact same code path as live and
dry-run trading — candle-by-candle over historical OHLCV data via a virtual clock and a
fake exchange overlay. The bot believes it is running a normal dry-run; it does not know
it is replaying the past.

This sits between backtesting (fast but unreliable) and real dry-run (reliable but takes
months):

| Mode | Engine | Speed | Fidelity |
|------|--------|-------|----------|
| **Backtest** | `Backtesting.backtest()` (simplified) | Seconds | Low — DCA before exit, no funding, static pairlist |
| **Dry-run replay** | `FreqtradeBot.process()` (real) | Hours | High — exit before DCA, real funding, 1-min resolution |
| **Real dry-run** | `FreqtradeBot.process()` (real) | Months | Highest — dynamic pairlist, real orderbook |

## Key properties

- **Same code as live.** The replay calls `FreqtradeBot.process()` in a loop. Every
  callback (`populate_indicators`, `populate_entry_trend`, `custom_stake_amount`,
  `custom_exit`, `adjust_trade_position`, `confirm_trade_exit`, ...) fires exactly as it
  would in production.
- **1-minute resolution (configurable).** Stoploss, ROI, and signals are checked every
  virtual minute by default (configurable to 5m or 15m). This is equivalent to permanent
  `--timeframe-detail 1m` on the real engine.
- **Real funding rate.** Funding is computed from local 1h funding-rate feather files —
  not ignored like in backtests.
- **Seed mode.** Replay writes trades directly into the bot's dry-run SQLite DB. When
  the replay finishes, the bot transitions to normal dry-run with the replay history
  already in place.
- **Static pairlist (main limitation).** The pairlist is a snapshot taken at launch time.
  It does not rotate dynamically like a `VolumePairList` would in a real dry-run.
- **Dry-run only (structurally enforced).** Quadruple-guarded: `safety.py` checks
  `dry_run=true`, blanks credentials, and namespaces the DB. The replay cannot run on a
  live bot.
- **Coordinator daemon.** Concurrent replays are capped to `nproc - 2 - hyperopt_cores`
  with a priority queue and SIGSTOP/SIGCONT pause/resume. Auto-spawned on first replay.

## Installation

```bash
pip install -e ".[replay]"
```

## Usage

### Option 1: From FreqUI (recommended)

Each dry-run bot in FreqUI has a **"Simulate dry-run (replay)"** button. Configure:

- **Start date** — e.g. `01/01/2026`
- **End date** — `today` or a fixed date
- **Resolution** — `1m` (most faithful), `5m`, or `15m`
- **Reset DB** — wipe existing trades before replaying

The replay runs with a visible progress bar. When it finishes, the bot transitions to
normal dry-run automatically.

### Option 2: Config-driven auto-launch (recommended for fleets)

Add a `dry_run_replay` block to your bot config JSON:

```json
{
    "dry_run_replay": {
        "automatic_launch": true,
        "start_date": "01/01/2026",
        "end_date": "today",
        "resolution": "1m",
        "reset_db": false
    }
}
```

When the bot starts, it detects this block and auto-launches the replay before
transitioning to normal dry-run. With `"reset_db": false`, the replay is **idempotent**:
it skips if the DB is already seeded.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `automatic_launch` | bool | *(required)* | Must be `true` to enable. |
| `start_date` | string | *(required)* | Start date. Accepts `DD/MM/YYYY`, `YYYY-MM-DD`, `YYYYMMDD`, `DD-MM-YYYY`. |
| `end_date` | string | `"today"` | End date. Same formats, plus literal `"today"` (UTC). |
| `resolution` | string | `"1m"` | Sub-step granularity: `"1m"`, `"5m"`, `"15m"`. |
| `reset_db` | bool | `false` | If `true`, wipes the DB before seeding. |

**Behavior at startup:**

1. Bot initializes exchange, pairlist, strategy (normal startup).
2. Detects `dry_run_replay` with `automatic_launch: true`.
3. Verifies `dry_run: true` (refuses on live bots — logs a warning and skips).
4. Checks idempotency marker (`KeyValueStore.get_string_value("ft_replay_seed")`). If
   `reset_db: false` and a previous replay exists → skips, goes to normal dry-run.
5. Runs the replay candle-by-candle.
6. On completion → transitions to normal dry-run.

### Option 3: CLI

```bash
python -m freqtrade.replay --config user_data/config.json --seed --sub-step 60 --timerange 20260101-
```

**CLI flags:**

| Flag | Description |
|------|-------------|
| `--config` | Bot config file (same as `freqtrade trade`) |
| `--seed` | Write trades into the bot's dry-run DB (seed mode) |
| `--sub-step` | Resolution in seconds (60 = 1m, 300 = 5m) |
| `--timerange` | Standard freqtrade timerange format |
| `--reset-db` | Wipe the DB before seeding |
| `--pairs` | Override pairs (default: use the config's pairlist) |

## Architecture

```
                  ┌─────────────────────────────────────────┐
                  │               FreqtradeBot              │
                  │            .process() loop              │
                  │  (same code as live / normal dry-run)   │
                  └──────────────┬──────────────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
    ┌──────────────┐   ┌────────────────┐   ┌──────────────┐
    │ VirtualClock │   │ ReplayExchange │   │ ReplayData   │
    │  (clock.py)  │   │ (exchange.py)  │   │    Store     │
    │              │   │                │   │ (data_store) │
    │ Advances 1m  │   │ Intercepts API │   │              │
    │  per tick    │   │ calls, serves  │   │ Feather OHLCV│
    │              │   │ synthetic book │   │ + funding    │
    └──────────────┘   └────────────────┘   └──────────────┘
              │
              ▼
    ┌──────────────┐
    │  runner.py   │
    │              │
    │ _drive_loop()│ ◄── Core loop: advance clock, call process()
    │ _prep_db()   │ ◄── DB backup/restore, integrity check
    └──────────────┘
              │
              ▼
    ┌──────────────┐
    │ Coordinator  │ ◄── Caps concurrent replays (SIGSTOP/SIGCONT)
    │(coordinator) │
    └──────────────┘
```

**Key modules:**

| Module | Purpose |
|--------|---------|
| `runner.py` | Core engine — `run_replay()` / `_drive_loop()`. DB prep, backup, restore, trade reconciliation, summary. |
| `exchange.py` | `ReplayExchangeMixin` + `build_replay_exchange()`. Thin overlay on the real exchange; intercepts API calls with local feather data. |
| `data_store.py` | `ReplayDataStore`. Loads OHLCV / mark / funding-rate feathers, serves time-windowed slices. |
| `clock.py` | `VirtualClock`. Single time source; `advance_to()`, `now()`. |
| `lifecycle.py` | Bot↔replay glue. `start_replay()`, `cancel_replay()`, `maybe_autolaunch_replay()`, `parse_autolaunch_config()`. |
| `safety.py` | `enforce_replay_safety()`. Hard gate: dry-run only, blanked credentials, DB namespacing. |
| `coordinator.py` | Daemon capping concurrent replays with priority queue + SIGSTOP/SIGCONT. |
| `coordinator_client.py` | Client helpers for the coordinator Unix socket. |
| `cli.py` | CLI argument parsing and orchestration. |

**REST API endpoints** (`rpc/api_server/api_replay.py`):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/replay` | GET | Current replay status (progress, date, state) |
| `/api/v1/replay` | POST | Start a replay |
| `/api/v1/replay` | DELETE | Cancel a running replay |
| `/api/v1/replay/queue` | GET | Coordinator queue status |
| `/api/v1/replay/restore` | POST | Restore DB from pre-replay backup |
| `/api/v1/replay/seeded` | GET | Check if DB has been seeded by a replay |

## DB integrity

- Before starting, the runner creates a backup of the current DB.
- On completion, `PRAGMA quick_check` validates integrity.
- If integrity fails, the backup is auto-restored.
- Real trades (from normal dry-run) always win over replay trades if the bot
  transitions mid-database.

## Known limitations

1. **Static pairlist.** The replay uses a snapshot of the pairlist at launch time. Pairs
   that would have entered/exited a `VolumePairList` during the replay period are not
   captured. This is the main source of divergence vs. a real dry-run.
2. **Synthetic orderbook.** The replay exchange provides a single-level orderbook with
   infinite depth. No partial fills, no real spread simulation.
3. **Fixed slippage.** Default 0.05%, configurable. Not derived from historical
   liquidity.
4. **No WebSocket events.** The replay does not simulate WebSocket ticker/trade streams.

## Performance

Typical execution time for a 5-month replay at 1-minute resolution on a 32-core machine
with 270 pairs: **4–10 hours** depending on strategy complexity and number of trades.

## Further reading

- [Blog post (FR)](https://buymeacoffee.com/freqtrade_france) — Dry-Run Replay:
  Valider une stratégie en quelques heures au lieu de plusieurs mois
- [Video demo (FR)](https://youtu.be/HkOIaxcPn9U) — Live demo in FreqUI
- [Original concept](https://github.com/saamy4r/Freqtrade_reply) — BigBroseur's repo
  that pioneered this idea
- [FreqUI Ultimate](https://github.com/titouannwtt/frequi-ultimate) — Companion
  dashboard with replay UI (button, progress bar, results)
