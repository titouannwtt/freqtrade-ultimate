#!/bin/bash
# Staggered bot restart — 30 min between each HL bot.
# Only starts bots NOT already running.
set -e
cd /home/moutonneux/freqtrade

DELAY=1800  # 30 minutes between HL bots

start_bot() {
    local session="$1"
    local config="$2"
    # Check if already running
    if pgrep -f "live_configs/$config" > /dev/null 2>&1; then
        echo "$(date '+%H:%M:%S') SKIP $session — already running"
        return
    fi
    echo "$(date '+%H:%M:%S') Starting $session ($config)..."
    screen -ls | grep "$session" | awk -F. '{print $1}' | xargs -I{} screen -S {} -X quit 2>/dev/null || true
    screen -dmS "$session" bash -c "cd /home/moutonneux/freqtrade && ./launch_bot.sh $config"
    echo "$(date '+%H:%M:%S') $session started."
}

echo "=== Remaining live HL bots (30 min stagger) ==="

start_bot "HL_hippo_dynv1_long_sharpe_v5_hl_aggressive" "hyperliquid_hippo_dynv1_long_sharpe_v5_hl_aggressive.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s (30 min)..."
sleep $DELAY

start_bot "HL_hippo_dynv1_long_casino" "hyperliquid_hippo_dynv1_long_casino.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s (30 min)..."
sleep $DELAY

start_bot "HL_hippo_dynv1_short_casino" "hyperliquid_hippo_dynv1_short_casino.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s (30 min)..."
sleep $DELAY

echo ""
echo "=== Dry-run HL bots ==="

start_bot "HL_miracle_momentum_hl_dry" "hyperliquid_miracle_momentum_hl_dry.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s (30 min)..."
sleep $DELAY

start_bot "HL-mm_dca_tight_rsi65" "hyperliquid_mm_dca_tight_rsi65_dry.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s (30 min)..."
sleep $DELAY

start_bot "HL-mm_dca_rsi65_dry" "hyperliquid_mm_dca_rsi65_dry.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s (30 min)..."
sleep $DELAY

start_bot "HL_mm_hl_dca_dry" "hyperliquid_mm_hl_dca_dry.json"

echo ""
echo "=== All remaining bots started! ==="
echo "$(date '+%H:%M:%S') Done."
