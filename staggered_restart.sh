#!/bin/bash
# Staggered restart of all 15 bots with 30s delay between each
# to avoid 429 rate limit storm on Hyperliquid
cd /home/moutonneux/freqtrade

BOTS=(
    "BN-hippo_original_btc:binance_hippo_original_btc.json"
    "GI-hippo_original_btc:gateio_hippo_original_btc.json"
    "HL-hippo_original:hyperliquid_hippo_original.json"
    "HL-hippo_dynv1_short_sharp:hyperliquid_hippo_dynv1_short_sharpe.json"
    "HL-hippo_dynv1_short_sharpe_v4:hyperliquid_hippo_dynv1_short_sharpe_v4.json"
    "HL-hippo_dynv1_long_sharpe_OnlyProfit_200:hyperliquid_hippo_dynv1_long_sharpe_OnlyProfit_200.json"
    "HL_hippo_dynv1_long_sharpe_v5_hl_aggressive:hyperliquid_hippo_dynv1_long_sharpe_v5_hl_aggressive.json"
    "HL_hippo_dynv1_long_casino:hyperliquid_hippo_dynv1_long_casino.json"
    "HL_hippo_dynv1_short_casino:hyperliquid_hippo_dynv1_short_casino.json"
    "HL_hippo_casino_v6:hyperliquid_hippo_dynv1_casino_v6.json"
    "HL_hippo_casino_v11:hyperliquid_hippo_dynv1_casino_v11.json"
    "HL_miracle_momentum_hl_dry:hyperliquid_miracle_momentum_hl_dry.json"
    "HL-mm_dca_tight_rsi65:hyperliquid_mm_dca_tight_rsi65_dry.json"
    "HL-mm_dca_rsi65_dry:hyperliquid_mm_dca_rsi65_dry.json"
    "HL_mm_hl_dca_dry:hyperliquid_mm_hl_dca_dry.json"
)

DELAY=${1:-30}
echo "Starting ${#BOTS[@]} bots with ${DELAY}s delay between each..."

for entry in "${BOTS[@]}"; do
    session="${entry%%:*}"
    config="${entry##*:}"
    echo "$(date +%H:%M:%S) Starting $session ($config)..."
    screen -dmS "$session" bash -c "cd /home/moutonneux/freqtrade && ./launch_bot.sh $config"
    sleep "$DELAY"
done

echo "$(date +%H:%M:%S) All ${#BOTS[@]} bots started."
