#!/usr/bin/env zsh
# Staggered bot restart script
# Phase 1: Immediately restart live bots WITH open trades
# Phase 2: +10 min, restart remaining live bots
# Phase 3: +20 min, restart dry-run bots

cd /home/moutonneux/freqtrade

echo "$(date) === PHASE 1: Live bots with open trades ==="

# hippo_original (1 open trade) — needs a new screen since old one died
echo "$(date) Starting HL-hippo_original..."
screen -dmS HL-hippo_original zsh -c 'cd /home/moutonneux/freqtrade && ./launch_bot.sh hyperliquid_hippo_original.json'

sleep 5

# OnlyProfit_200 (2 open trades)
echo "$(date) Starting HL-hippo_dynv1_long_sharpe_OnlyProfit_200..."
screen -dmS HL-hippo_dynv1_long_sharpe_OnlyProfit_200 zsh -c 'cd /home/moutonneux/freqtrade && ./launch_bot.sh hyperliquid_hippo_dynv1_long_sharpe_OnlyProfit_200.json'

echo "$(date) Phase 1 done. Waiting 10 minutes for Phase 2..."
sleep 600

echo "$(date) === PHASE 2: Remaining live bots (no open trades) ==="

echo "$(date) Starting HL-hippo_dynv1_short_sharp..."
screen -dmS HL-hippo_dynv1_short_sharp zsh -c 'cd /home/moutonneux/freqtrade && ./launch_bot.sh hyperliquid_hippo_dynv1_short_sharpe.json'
sleep 5

echo "$(date) Starting HL_hippo_dynv1_long_sharpe_v5_hl_aggressive..."
screen -S HL_hippo_dynv1_long_sharpe_v5_hl_aggressive -X stuff './launch_bot.sh hyperliquid_hippo_dynv1_long_sharpe_v5_hl_aggressive.json\n'
sleep 5

echo "$(date) Starting HL_hippo_dynv1_long_casino..."
screen -S HL_hippo_dynv1_long_casino -X stuff './launch_bot.sh hyperliquid_hippo_dynv1_long_casino.json\n'
sleep 5

echo "$(date) Starting HL_hippo_dynv1_short_casino..."
screen -S "HL_hippo_dynv1_short_casino.json" -X stuff './launch_bot.sh hyperliquid_hippo_dynv1_short_casino.json\n'
sleep 5

echo "$(date) Starting HL-hippo_dynv1_short_sharpe_v4..."
screen -S HL-hippo_dynv1_short_sharpe_v4 -X stuff './launch_bot.sh hyperliquid_hippo_dynv1_short_sharpe_v4.json\n'
sleep 5

echo "$(date) Starting BN-hippo_original_btc..."
screen -dmS BN-hippo_original_btc zsh -c 'cd /home/moutonneux/freqtrade && ./launch_bot.sh binance_hippo_original_btc.json'
sleep 5

echo "$(date) Starting GI-hippo_original_btc..."
screen -dmS GI-hippo_original_btc zsh -c 'cd /home/moutonneux/freqtrade && ./launch_bot.sh gateio_hippo_original_btc.json'

echo "$(date) Phase 2 done. Waiting 10 minutes for Phase 3..."
sleep 600

echo "$(date) === PHASE 3: Dry-run bots ==="

echo "$(date) Starting HL_miracle_momentum_hl_dry..."
screen -S HL_miracle_momentum_hl_dry -X stuff './launch_bot.sh hyperliquid_miracle_momentum_hl_dry.json\n'
sleep 5

echo "$(date) Starting HL_mm_hl_dca_dry..."
screen -S HL_mm_hl_dca_dry -X stuff './launch_bot.sh hyperliquid_mm_hl_dca_dry.json\n'
sleep 5

echo "$(date) Starting HL-mm_dca_rsi65_dry..."
screen -S HL-mm_dca_rsi65_dry -X stuff './launch_bot.sh hyperliquid_mm_dca_rsi65_dry.json\n'
sleep 5

echo "$(date) Starting HL-mm_dca_tight_rsi65..."
screen -S HL-mm_dca_tight_rsi65 -X stuff './launch_bot.sh hyperliquid_mm_dca_tight_rsi65_dry.json\n'

echo "$(date) === ALL BOTS RESTARTED ==="
