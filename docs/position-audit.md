# Position audit

An evidence trail for what your bots hold, independent of the bots themselves.

If you run **one bot on one exchange account**, you can stop reading: everything below is
inert, and nothing changes for you.

If you run **several bots on one account** — the normal setup on Hyperliquid, where the
exchange nets positions per coin across the whole wallet — then no single bot can tell
whether the account matches the fleet's books. This page describes what the fork adds so
that question has an answer, continuously and provably.

## The problem, concretely

On a shared account, the exchange's own endpoints answer about the *account*, not about
the bot asking:

- `fetch_orders(pair)` returns every order the wallet placed on that pair;
- `fetch_positions()` returns one netted position per coin, for the whole wallet.

Upstream freqtrade assumes one bot owns one account — a sound assumption that this
setup breaks. Two failure modes follow, and both are silent:

| | What it is | How it happens |
|---|---|---|
| **Orphan** | A position on the exchange that no bot pilots | A bot concludes its position is gone and closes the trade in its database without ever sending an exit order. The position keeps running, with no stop and no owner. |
| **Phantom** | A book claiming more than exists on-chain | A bot adopts an order it did not place and recomputes its size from it. Two books then claim one position; the first to exit closes more than it owns. |

Neither raises an error. Both are invisible until someone reconciles by hand.

## What the fork adds

### 1. Provable order ownership

Every order carries a client order id whose first bytes are a stable fingerprint of the
bot that placed it:

```
0x  a1b2c3d4  9f8e7d6c5b4a39281706f5e4
    ^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^^^
    bot        per-order randomness
```

The exchange echoes it back, so "is this order mine?" stops being an inference. This is
what lets the bot keep *recovering its own* lost orders — the reason upstream re-reads
the account at all — while never claiming a sibling's.

Automatic on exchanges that declare `supports_client_order_id` (Hyperliquid today).
Nothing to configure.

### 2. Guards around every order

- an external close may only be concluded from a positions view **newer than the trade's
  own last fill**, confirmed by a cache-bypassing read — a snapshot older than the fill
  shows no position simply because the position did not exist yet;
- an exit is capped at what the wallet actually holds, so it can never overshoot into an
  opposite position;
- the position arithmetic `after == before + filled` is asserted around each order.

### 3. An append-only, hash-chained ledger

Position-affecting events are additionally written to `user_data/audit/<bot>.jsonl`,
once, and never modified. Each entry carries the hash of the previous one, so any
alteration, reordering or deletion breaks the chain and is reported.

The trade database is a *working* record — rewritten, corrected, pruned. The ledger is
the *audit* record, and answers what the working record structurally cannot: what did we
hold last Tuesday at 00:00, and has this history been altered since?

### 4. Attestation and break register

```bash
freqtrade position-audit -c user_data/config.json
```

Reads the exchange **directly** (never through the bots' shared cache — an auditor that
consumes the audited system's view will faithfully confirm its errors), reads every
sibling bot's book read-only including stopped bots, and prints a dated statement:

```
Attestation 2026-08-06T13:24:15+00:00 — hyperliquid
  livres lus : 22   30 coins ISO, 1 écart(s)

  COIN              EXCHANGE        LIVRES         ÉCART  ÉTAT
  ETH                -0.0713       -0.0796        0.0083  PHANTOM  bot_a, bot_b

  Écarts ouverts (registre) :
    ETH          PHANTOM  écart=0.0083   ouvert depuis 6.4h

  Registre : 111 entrées, chaîne intacte (dernier hash ae321b886140…)
```

Each discrepancy is opened in the register on first sight, aged while it persists, and
closed by a later entry when it resolves — the history survives the resolution, because
"it was fixed" and "it never happened" must not look alike.

The command exits non-zero when anything is off, so a scheduler can alert without
parsing output. It cannot place, cancel or modify an order.

Verify the chain alone, without contacting the exchange:

```bash
freqtrade position-audit -c user_data/config.json --verify
```

## Running it continuously

Detection on demand is not detection. One line in `crontab -e`:

```cron
0 * * * * cd /path/to/freqtrade && .venv/bin/freqtrade position-audit -c live_configs/any_live_bot.json >> user_data/logs/position-audit.log 2>&1
```

Any *live* bot's config works — the command discovers the rest of the fleet from it. Use
a live config, not a dry-run one: a simulated book has no counterpart on the exchange and
every coin would read as a discrepancy.

## Reading a break

**ORPHAN** — the exchange holds more than anyone claims. Someone must take
responsibility for that position: it has no stop and no exit logic behind it. Before
closing it, check that the owning bot is not simply stopped — a stopped bot still owns
its positions, and the attestation counts stopped bots precisely so that its holdings are
not mistaken for unowned.

**PHANTOM** — a book claims more than exists. The position is not at risk, but the bot's
sizing is wrong and its next exit will try to close more than it owns. Correct the book,
never delete the trade: deleting it turns a phantom into an orphan.

A break that appears and disappears between two runs minutes apart is usually neither —
it is an exit filling in tranches, or a bot restarting. The register's aging column
separates the two: a real break persists.

## Files

| Path | Contents |
|---|---|
| `user_data/audit/<bot>.jsonl` | Per-bot fill ledger, append-only |
| `user_data/audit/position_ledger.jsonl` | Fleet attestations and break register |
| `user_data/logs/<bot>.log` | Bot log, rotating, created automatically |

Ledgers are append-only and grow slowly (one line per fill). They are not pruned by
design: an audit trail that deletes its own history is not one.
