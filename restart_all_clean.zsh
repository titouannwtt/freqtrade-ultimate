#!/usr/bin/env zsh
cd /home/moutonneux/freqtrade

# Phase 1: Live bots with open trades (immediate)
echo "$(date) === PHASE 1: Live with open trades ==="

echo "$(date) Starting HL-hippo_original (1 trade)..."
screen -S HL-hippo_original -X stuff './launch_bot.sh hyperliquid_hippo_original.json\n' 2>/dev/null || \
    screen -dmS HL-hippo_original zsh -c 'cd /home/moutonneux/freqtrade && ./launch_bot.sh hyperliquid_hippo_original.json'
sleep 180

echo "$(date) Starting HL-hippo_dynv1_long_sharpe_OnlyProfit_200 (2 trades)..."
screen -dmS HL-hippo_dynv1_long_sharpe_OnlyProfit_200 zsh -c 'cd /home/moutonneux/freqtrade && ./launch_bot.sh hyperliquid_hippo_dynv1_long_sharpe_OnlyProfit_200.json'
sleep 180

# Phase 2: Remaining live HL bots
echo "$(date) === PHASE 2: Remaining live HL bots ==="

echo "$(date) Starting HL-hippo_dynv1_short_sharp..."
screen -dmS HL-hippo_dynv1_short_sharp zsh -c 'cd /home/moutonneux/freqtrade && ./launch_bot.sh hyperliquid_hippo_dynv1_short_sharpe.json'
sleep 180

echo "$(date) Starting HL_hippo_dynv1_long_sharpe_v5_hl_aggressive..."
screen -S HL_hippo_dynv1_long_sharpe_v5_hl_aggressive -X stuff './launch_bot.sh hyperliquid_hippo_dynv1_long_sharpe_v5_hl_aggressive.json\n'
sleep 180

echo "$(date) Starting HL_hippo_dynv1_long_casino..."
screen -S HL_hippo_dynv1_long_casino -X stuff './launch_bot.sh hyperliquid_hippo_dynv1_long_casino.json\n'
sleep 180

echo "$(date) Starting HL_hippo_dynv1_short_casino..."
screen -S "HL_hippo_dynv1_short_casino.json" -X stuff './launch_bot.sh hyperliquid_hippo_dynv1_short_casino.json\n'
sleep 180

echo "$(date) Starting HL-hippo_dynv1_short_sharpe_v4..."
screen -S HL-hippo_dynv1_short_sharpe_v4 -X stuff './launch_bot.sh hyperliquid_hippo_dynv1_short_sharpe_v4.json\n'
sleep 180

# Phase 3: Non-HL live bots
echo "$(date) === PHASE 3: Non-HL live bots ==="

echo "$(date) Starting BN-hippo_original_btc..."
screen -dmS BN-hippo_original_btc zsh -c 'cd /home/moutonneux/freqtrade && ./launch_bot.sh binance_hippo_original_btc.json'
sleep 180

echo "$(date) Starting GI-hippo_original_btc..."
screen -dmS GI-hippo_original_btc zsh -c 'cd /home/moutonneux/freqtrade && ./launch_bot.sh gateio_hippo_original_btc.json'
sleep 180

# Phase 4: Dry-run bots
echo "$(date) === PHASE 4: Dry-run bots ==="

echo "$(date) Starting HL_miracle_momentum_hl_dry..."
screen -S HL_miracle_momentum_hl_dry -X stuff './launch_bot.sh hyperliquid_miracle_momentum_hl_dry.json\n'
sleep 180

echo "$(date) Starting HL_mm_hl_dca_dry..."
screen -S HL_mm_hl_dca_dry -X stuff './launch_bot.sh hyperliquid_mm_hl_dca_dry.json\n'
sleep 180

echo "$(date) Starting HL-mm_dca_rsi65_dry..."
screen -S HL-mm_dca_rsi65_dry -X stuff './launch_bot.sh hyperliquid_mm_dca_rsi65_dry.json\n'
sleep 180

echo "$(date) Starting HL-mm_dca_tight_rsi65..."
screen -S HL-mm_dca_tight_rsi65 -X stuff './launch_bot.sh hyperliquid_mm_dca_tight_rsi65_dry.json\n'

echo "$(date) === ALL 13 BOTS RESTARTED (~39 min total) ==="
