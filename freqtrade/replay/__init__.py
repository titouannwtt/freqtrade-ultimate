"""
Freqtrade Replay — drive the *real* live bot loop against historical data.

This is a **dry-run-only behaviour-validation harness**, not a backtester for
strategy selection. It runs ``FreqtradeBot.process()`` candle-by-candle through
a virtual clock and a fake exchange, producing a normal Freqtrade SQLite
database you can open in FreqUI. Because it exercises the exact live code path
(DCA, ``custom_stake_amount``, ``custom_exit``, callbacks), it surfaces
look-ahead / repaint / sizing bugs that the vectorised backtester hides.

See ``freqtrade/replay/README.md`` for design notes and known divergences.
"""

from freqtrade.replay.runner import run_replay
from freqtrade.replay.safety import ReplaySafetyError, enforce_replay_safety


__all__ = ["run_replay", "enforce_replay_safety", "ReplaySafetyError"]
