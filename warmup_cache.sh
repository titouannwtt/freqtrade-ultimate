#!/bin/bash
# Pre-warm ftcache with startup candles before restarting bots.
# Usage: ./warmup_cache.sh [configs...]
# Default: all live configs

CONFIGS="${@:-live_configs/hyperliquid_*.json live_configs/binance_*.json live_configs/gateio_*.json}"

echo "$(date +%H:%M:%S) Warming up ftcache..."
python3.11 -m freqtrade.ohlcv_cache.warmup --configs $CONFIGS
echo "$(date +%H:%M:%S) Warmup done. Safe to start bots."
