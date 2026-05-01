#!/bin/bash
# Staggered bot restart — 5 min between each HL bot.
# BN/GI bots start first (no HL rate limit impact).
set -e
cd /home/moutonneux/freqtrade

DELAY=300  # 5 minutes between HL bots

start_bot() {
    local session="$1"
    local config="$2"
    echo "$(date '+%H:%M:%S') Starting $session ($config)..."
    # Kill duplicate screens with same name
    screen -ls | grep "$session" | awk -F. '{print $1}' | xargs -I{} screen -S {} -X quit 2>/dev/null || true
    screen -dmS "$session" bash -c "cd /home/moutonneux/freqtrade && ./launch_bot.sh $config"
    echo "$(date '+%H:%M:%S') $session started."
}

echo "=== Phase 0: Non-HL bots (no rate limit conflict) ==="
start_bot "BN-hippo_original_btc" "binance_hippo_original_btc.json"
start_bot "GI-hippo_original_btc" "gateio_hippo_original_btc.json"
echo "$(date '+%H:%M:%S') BN/GI started. Waiting 60s before HL bots..."
sleep 60

echo ""
echo "=== Phase 1: Live HL bots with open trades ==="

start_bot "HL-hippo_original" "hyperliquid_hippo_original.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s..."
sleep $DELAY

start_bot "HL-hippo_dynv1_short_sharp" "hyperliquid_hippo_dynv1_short_sharpe.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s..."
sleep $DELAY

start_bot "HL-hippo_dynv1_long_sharpe_OnlyProfit_200" "hyperliquid_hippo_dynv1_long_sharpe_OnlyProfit_200.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s..."
sleep $DELAY

echo ""
echo "=== Phase 2: Other live HL bots ==="

start_bot "HL-hippo_dynv1_short_sharpe_v4" "hyperliquid_hippo_dynv1_short_sharpe_v4.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s..."
sleep $DELAY

start_bot "HL_hippo_dynv1_long_sharpe_v5_hl_aggressive" "hyperliquid_hippo_dynv1_long_sharpe_v5_hl_aggressive.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s..."
sleep $DELAY

start_bot "HL_hippo_dynv1_long_casino" "hyperliquid_hippo_dynv1_long_casino.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s..."
sleep $DELAY

start_bot "HL_hippo_dynv1_short_casino" "hyperliquid_hippo_dynv1_short_casino.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s..."
sleep $DELAY

echo ""
echo "=== Phase 3: Dry-run HL bots ==="

start_bot "HL_miracle_momentum_hl_dry" "hyperliquid_miracle_momentum_hl_dry.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s..."
sleep $DELAY

start_bot "HL-mm_dca_tight_rsi65" "hyperliquid_mm_dca_tight_rsi65_dry.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s..."
sleep $DELAY

start_bot "HL-mm_dca_rsi65_dry" "hyperliquid_mm_dca_rsi65_dry.json"
echo "$(date '+%H:%M:%S') Waiting ${DELAY}s..."
sleep $DELAY

start_bot "HL_mm_hl_dca_dry" "hyperliquid_mm_hl_dca_dry.json"

echo ""
echo "=== All bots started! ==="
echo "$(date '+%H:%M:%S') Total: 13 bots (2 BN/GI + 7 live HL + 4 dry HL)"
