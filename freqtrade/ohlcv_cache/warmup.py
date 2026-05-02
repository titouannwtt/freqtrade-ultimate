"""
ftcache warmup — Pre-fetch startup candles for all bots before they start.

Usage:
    python -m freqtrade.ohlcv_cache.warmup --configs live_configs/*.json
    python -m freqtrade.ohlcv_cache.warmup --configs live_configs/hyperliquid_*.json

Connects to the running ftcache daemon, reads each bot config to determine
which pairs/timeframes/candle counts are needed, and pre-fetches them.
When bots start afterward, they find everything in cache — instant init.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from freqtrade.enums import CandleType, TradingMode
from freqtrade.ohlcv_cache.client import CacheUnavailable, OhlcvCacheClient

logger = logging.getLogger("ftcache.warmup")


def _resolve_pairlist(config: dict, config_path: Path) -> list[str]:
    """Extract pair list from config, DB, or klines cache."""
    # 1. Static pair_whitelist
    pair_whitelist = config.get("exchange", {}).get("pair_whitelist", [])
    if pair_whitelist:
        return pair_whitelist

    # 2. StaticPairList in pairlists config
    for pl in config.get("pairlists", []):
        if pl.get("method") == "StaticPairList" and pl.get("pairs"):
            return pl["pairs"]

    # 3. Extract from trade database (pairs with recent trades)
    db_url = config.get("db_url", "")
    if db_url and db_url.startswith("sqlite:///"):
        db_path = Path(db_url.replace("sqlite:///", ""))
        if db_path.exists():
            try:
                import sqlite3
                conn = sqlite3.connect(str(db_path))
                cursor = conn.execute(
                    "SELECT DISTINCT pair FROM trades ORDER BY open_date DESC LIMIT 200"
                )
                pairs = [row[0] for row in cursor.fetchall()]
                conn.close()
                if pairs:
                    logger.info(
                        "%s: resolved %d pairs from trade database",
                        config_path.name, len(pairs),
                    )
                    return pairs
            except Exception:
                pass

    # 4. Extract from klines cache if it exists
    exchange_name = config.get("exchange", {}).get("name", "").lower()
    trading_mode = config.get("trading_mode", "spot")
    datadir = config.get("datadir", Path.home() / ".freqtrade")
    cache_file = Path(datadir) / "klines_cache" / f"{exchange_name}_{trading_mode}.pkl"
    if cache_file.exists():
        try:
            import pickle
            with cache_file.open("rb") as f:
                data = pickle.load(f)  # noqa: S301
            pairs = list({k[0] for k in data.get("klines", {}).keys()})
            if pairs:
                logger.info(
                    "%s: resolved %d pairs from klines cache",
                    config_path.name, len(pairs),
                )
                return pairs
        except Exception:
            pass

    return []


def _parse_config(config_path: Path) -> dict | None:
    """Parse a bot config and return warmup parameters."""
    try:
        config = json.loads(config_path.read_text())
    except Exception as e:
        logger.warning("Failed to parse %s: %s", config_path, e)
        return None

    exchange_name = config.get("exchange", {}).get("name", "").lower()
    if not exchange_name:
        return None

    trading_mode = config.get("trading_mode", "spot")
    timeframe = config.get("timeframe", "15m")
    startup_candles = config.get("startup_candle_count", 500)
    strategy_name = config.get("strategy", "")

    pairs = _resolve_pairlist(config, config_path)

    if not pairs:
        logger.info(
            "%s: no pairs resolved (VolumePairList without DB/cache), skipping.",
            config_path.name,
        )
        return None

    return {
        "config_path": str(config_path),
        "exchange": exchange_name,
        "trading_mode": trading_mode,
        "timeframe": timeframe,
        "startup_candles": startup_candles,
        "strategy": strategy_name,
        "pairs": pairs,
    }


async def _warmup_pairs(
    client: OhlcvCacheClient,
    pairs: list[str],
    timeframe: str,
    startup_candles: int,
    candle_type: CandleType,
    concurrency: int = 3,
) -> tuple[int, int]:
    """Fetch startup candles for all pairs. Returns (success, failed) counts."""
    from freqtrade.exchange.exchange_utils import timeframe_to_seconds

    tf_secs = timeframe_to_seconds(timeframe)
    # Calculate since_ms: go back startup_candles * timeframe + small buffer
    since_ms = int((time.time() - startup_candles * tf_secs - 3600) * 1000)

    sem = asyncio.Semaphore(concurrency)
    success = 0
    failed = 0

    async def fetch_one(pair: str) -> bool:
        async with sem:
            try:
                await client.fetch(
                    pair=pair,
                    timeframe=timeframe,
                    candle_type=candle_type,
                    since_ms=since_ms,
                    limit=None,
                    priority=OhlcvCacheClient.LOW,
                )
                return True
            except Exception as e:
                logger.warning("Failed to warm %s %s: %s", pair, timeframe, e)
                return False

    tasks = [fetch_one(pair) for pair in pairs]
    results = await asyncio.gather(*tasks)
    success = sum(1 for r in results if r)
    failed = sum(1 for r in results if not r)
    return success, failed


async def run_warmup(config_paths: list[Path], socket_path: str | None = None) -> int:
    """Main warmup logic."""
    # Group pairs by (exchange, trading_mode, timeframe)
    groups: dict[tuple[str, str, str], dict] = {}

    for path in config_paths:
        parsed = _parse_config(path)
        if not parsed or not parsed["pairs"]:
            continue

        key = (parsed["exchange"], parsed["trading_mode"], parsed["timeframe"])
        if key not in groups:
            groups[key] = {
                "exchange": parsed["exchange"],
                "trading_mode": parsed["trading_mode"],
                "timeframe": parsed["timeframe"],
                "startup_candles": parsed["startup_candles"],
                "pairs": set(),
                "configs": [],
            }
        groups[key]["pairs"].update(parsed["pairs"])
        groups[key]["startup_candles"] = max(
            groups[key]["startup_candles"], parsed["startup_candles"]
        )
        groups[key]["configs"].append(parsed["config_path"])

    if not groups:
        logger.info("No warmup targets found in configs.")
        return 0

    total_success = 0
    total_failed = 0

    for key, group in groups.items():
        exchange, trading_mode, timeframe = key
        pairs = sorted(group["pairs"])
        startup_candles = group["startup_candles"]

        logger.info(
            "Warming %s/%s %s: %d pairs × %d candles from %d configs",
            exchange, trading_mode, timeframe,
            len(pairs), startup_candles, len(group["configs"]),
        )

        # Determine candle type
        tm = TradingMode(trading_mode) if trading_mode != "spot" else TradingMode.SPOT
        candle_type = CandleType.get_default(tm)

        # Create client
        sock = socket_path or f"/tmp/ftcache-{__import__('os').getuid()}.sock"
        client = OhlcvCacheClient(
            exchange_id=exchange,
            trading_mode=trading_mode,
            config={},
            socket_path=sock,
        )

        try:
            success, failed = await _warmup_pairs(
                client, pairs, timeframe, startup_candles, candle_type,
            )
            total_success += success
            total_failed += failed
            logger.info(
                "  %s/%s %s: %d/%d pairs warmed (%d failed)",
                exchange, trading_mode, timeframe,
                success, len(pairs), failed,
            )
        finally:
            await client.close()

    logger.info(
        "Warmup complete: %d pairs cached, %d failed.",
        total_success, total_failed,
    )
    return 0 if total_failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-warm ftcache with startup candles for bot configs."
    )
    parser.add_argument(
        "--configs", nargs="+", required=True,
        help="Bot config files (glob-expanded by shell)",
    )
    parser.add_argument(
        "--socket", default=None,
        help="ftcache daemon socket path (default: /tmp/ftcache-$UID.sock)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config_paths = [Path(p) for p in args.configs if Path(p).exists()]
    if not config_paths:
        logger.error("No valid config files found.")
        return 1

    logger.info("Warming up ftcache for %d configs...", len(config_paths))
    return asyncio.run(run_warmup(config_paths, args.socket))


if __name__ == "__main__":
    sys.exit(main())
