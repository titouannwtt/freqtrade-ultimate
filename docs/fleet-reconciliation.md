# Fleet position reconciliation & safe retirement

Two small, **optional** helper scripts for running a fleet of bots on a **shared (netted)
Hyperliquid wallet**. Everything in this fork works without them — they only add a safety net
against *orphan positions*.

## The problem they solve

On Hyperliquid the wallet is **netted**: each bot's position on a coin is merged into a single
net position on-chain. If a live bot is stopped (Ctrl+C in its screen) **without first closing
its trades**, the position stays on the wallet — and once the bot's config/DB is removed, no bot
tracks it anymore. That is an **orphan**: unmanaged exposure that rides until liquidation or a
manual close. Orphans accumulate silently across many stop/relaunch cycles.

## `retire_bot.py` — cut a bot without leaving an orphan

Codifies the safe teardown so a bot is **never stopped while it still holds open trades**:

```bash
python3 retire_bot.py hyperliquid_foo.json            # API force-exit, then stop + archive
python3 retire_bot.py hyperliquid_foo.json --onchain  # also close stuck trades on-chain
```

Steps: `/stopentry` → `/forceexit all` → wait until flat → (with `--onchain`) close any trade the
bot can't fill — under 429 backoff — with a signed **`reduceOnly`** market order for the bot's
**exact** amount (reduceOnly can never flip the net or touch another bot's share) → stop the
screen (double `Ctrl+C`) → move config + DB to `_retired/`. The strategy `.py` is never touched.
It **refuses to stop a bot that still has open trades**.

Use it instead of a raw `Ctrl+C` whenever you permanently retire a live bot.

## `reconcile_positions.py` — detect drift

Read-only. For every coin it compares the net position on the wallet with the signed sum of open
trades across running **live** bots, and flags:

- **ORPHAN** — a position on the wallet no live bot backs;
- **PHANTOM** — a bot's DB claims more than exists on-chain (a stale trade).

```bash
python3 reconcile_positions.py            # human report (exit code 1 if any drift)
python3 reconcile_positions.py --json     # machine output
python3 reconcile_positions.py --telegram # also push a Telegram alert on drift
```

It places no orders and never touches a bot.

## Optional: run it on a schedule

Purely optional — **the fork works fine without any cron**. If you *want* to be alerted the
moment drift appears, add a crontab entry (adjust the path/venv to your setup):

```cron
# every 30 minutes, alert on Telegram if the wallet drifts from the live fleet
*/30 * * * * cd /path/to/freqtrade && .venv/bin/python reconcile_positions.py --telegram >/dev/null 2>&1
```

Without the cron you can still run `reconcile_positions.py` by hand whenever you retire bots or
review the fleet.
