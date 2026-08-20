"""
Replay CLI.

    python -m freqtrade.replay --config user_data/config.json \
        --strategy MyStrategy --timerange 20250101-20250601

Kept as a standalone entry point (rather than a freqtrade subcommand) to
minimise merge surface against upstream. The replay is dry-run-only and refuses
to start against a live configuration (see safety.py).
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from freqtrade.replay.runner import run_replay


def _parse_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%d").replace(tzinfo=UTC)


def _pairs_from_config(config_path: str) -> list[str]:
    with Path(config_path).open() as fh:
        cfg = json.load(fh)
    pairs = cfg.get("exchange", {}).get("pair_whitelist", [])
    if not pairs:
        raise SystemExit(f"No --pairs given and pair_whitelist in {config_path} is empty.")
    return pairs


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m freqtrade.replay",
        description="Drive the real freqtrade dry-run loop over historical data (dry-run only).",
    )
    p.add_argument("--config", default="user_data/config.json", help="Path to config.json")
    p.add_argument("--strategy", required=True, help="Strategy class name")
    p.add_argument("--timerange", required=True, help="YYYYMMDD-YYYYMMDD")
    p.add_argument("--pairs", nargs="+", default=None, help="Override pairs (default: whitelist)")
    p.add_argument(
        "--slippage", type=float, default=0.0005, help="Bid-ask half-spread (default 0.05%%)"
    )
    p.add_argument(
        "--wallet",
        type=float,
        default=None,
        help="Simulated starting wallet in stake currency. If omitted, falls back to the "
        "config's dry_run_wallet (default 1000) so replay and backtest/hyperopt stay "
        "comparable. When given, overrides the config.",
    )
    p.add_argument("--db-url", default=None, help="sqlite URL; forced into the .replay. namespace")
    p.add_argument("--datadir", default=None, help="Feather data dir (default: from config)")
    p.add_argument(
        "--no-fresh", action="store_true", help="Keep existing replay DB instead of wiping it"
    )
    p.add_argument(
        "--auto-download", action="store_true", help="Download missing data (hits the exchange)"
    )
    p.add_argument(
        "--progress-file",
        default=None,
        help="Write live progress + final summary as JSON to this path (used by the API server).",
    )
    p.add_argument(
        "--seed",
        action="store_true",
        help="Seed mode: write into the given --db-url (a bot's dry-run DB) instead of a "
        "namespaced *.replay.sqlite. Refuses to run unless the config is dry-run.",
    )
    p.add_argument(
        "--sub-step",
        type=int,
        default=60,
        help="Intra-candle resolution in seconds (default 60 = 1m). Larger = much faster "
        "but coarser order fills (e.g. 900 = 15m).",
    )
    p.add_argument(
        "--reset-db",
        action="store_true",
        help="Seed mode: wipe the bot's dry-run trades first. Without it, existing trades are "
        "preserved and the replay stops at the first real (non-replay) trade.",
    )
    p.add_argument(
        "--static-pairlist",
        action="store_true",
        help="Pin --pairs as a fixed list instead of replaying the config's own pairlist "
        "chain. The chain is replayed by default, so a replay trades what the bot would "
        "actually have selected; use this when the question is about THESE pairs only.",
    )
    return p


def _write_progress(path: str, payload: dict) -> None:
    try:
        Path(path).write_text(json.dumps(payload))
    except OSError:
        pass


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[dry-run replay] %(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("freqtrade.exchange", "freqtrade.resolvers", "ccxt", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    args = build_parser().parse_args(argv)
    try:
        start_str, end_str = args.timerange.split("-")
        start_dt, end_dt = _parse_day(start_str), _parse_day(end_str)
    except ValueError:
        raise SystemExit("--timerange must be YYYYMMDD-YYYYMMDD")
    if start_dt >= end_dt:
        raise SystemExit("Start date must be before end date")

    pairs = args.pairs or _pairs_from_config(args.config)

    progress_cb = None
    if args.progress_file:
        _write_progress(
            args.progress_file, {"status": "running", "progress": 0.0, "step": "startup"}
        )

        def progress_cb(payload: dict):
            _write_progress(args.progress_file, {"status": "running", **payload})

    try:
        summary = run_replay(
            config_path=args.config,
            pairs=pairs,
            start_dt=start_dt,
            end_dt=end_dt,
            strategy=args.strategy,
            slippage_pct=args.slippage,
            wallet=args.wallet,
            db_url=args.db_url,
            datadir=args.datadir,
            fresh=not args.no_fresh,
            auto_download=args.auto_download,
            seed=args.seed,
            sub_step=args.sub_step,
            reset_db=args.reset_db,
            dynamic_pairlist=not args.static_pairlist,
            progress_callback=progress_cb,
        )
        if args.progress_file:
            _write_progress(
                args.progress_file, {"status": "done", "progress": 1.0, "result": summary}
            )
    except Exception as exc:
        if args.progress_file:
            _write_progress(args.progress_file, {"status": "error", "error": str(exc)})
        raise


if __name__ == "__main__":
    main()
