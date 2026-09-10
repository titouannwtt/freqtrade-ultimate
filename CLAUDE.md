# Freqtrade Ultimate — Claude Code

Fork of Freqtrade with Hyperliquid-specific handling and a trading co-pilot. Real money may be involved.

## Before trading-related work
1. Read `.claude-tips/README.md` and load only relevant tips.
2. Check strict guardrails before strategy, sizing, config, pairlist or deployment work.
3. Give opinionated risk-aware advice and push back when necessary.
4. `tips.txt` is the source of truth if an index diverges.

## Hard constraints
- Never commit API keys, wallet keys, passwords or tokens.
- Personal/exchange-specific constraints belong in gitignored `CLAUDE.local.md`.
- Trade DB work uses Python `sqlite3`, not the sqlite3 CLI.
- Never use `pkill`/`kill -9` on hyperopt; use the safe interruption procedure.
- No AI-tool attribution in published Git material.

## Architecture
Trading loop: `freqtradebot.py:process()` → create trades → manage open orders → exit positions.
Hyperliquid additions include external-close handling, liquidation detection and shared-wallet exit guard.
Replay is for live-behavior validation/dry-run seeding, not strategy selection; read `.claude-tips/replay.md`.
Visual diagrams (system architecture, `ftcache` sequence, bot loop): `docs/diagrams/README.md`.

Strategy parameters: `JSON > buy_params/sell_params > DecimalParameter default`.
Always check co-located hyperopt JSON for actual values.

## Commands
```bash
pip install -e .
pytest --random-order -n auto
pytest tests/test_freqtradebot.py::test_function_name -x
ruff check freqtrade/
ruff format freqtrade/
mypy freqtrade/
python3 -c "import sqlite3; ..."
```

## Strategy export
`user_data/export_strategies.py` exports sanitized live/dry configs, code and optional backtests.
Generated archives are not committed and must never contain secrets.

## Upstream
```bash
git fetch upstream --tags
git merge upstream/stable --no-edit
pip install -e .
```
After merges, verify fork-specific code survived.

## FreqUI
For the separate FreqUI repo: bump its package version, build and release when publishing.
Load detailed `.claude-tips/*.md` only for the relevant task.
