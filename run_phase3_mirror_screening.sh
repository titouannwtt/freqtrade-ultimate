#!/bin/bash
# Phase 3 — V0 mirror backtests for dual-side screening (all strategies)
# Backtests both short V2 and mirror V0 for each strategy to compute edge_relatif
set +e
cd /home/moutonneux/freqtrade
source .venv/bin/activate

TR="20260101-20260501"
NOFTCACHE="backtest_configs/_no_ftcache.json"

run_backtest() {
    local strat="$1"
    local config="$2"
    local tf="${3:-1h}"

    echo "========================================"
    echo "$(date) — Backtesting: $strat (tf=$tf)"
    echo "========================================"

    .venv/bin/freqtrade backtesting \
        --strategy "$strat" \
        --config "backtest_configs/$config" \
        --config "$NOFTCACHE" \
        --timerange "$TR" \
        --timeframe "$tf" \
        --export none \
        2>&1 | tail -30

    echo "$(date) — Finished: $strat"
    echo ""
}

# --- Batch: all strategies needing Phase 3 screening ---
# Format: short_v2 then mirror_v0, same config

# 1. ath_awesome (MOT=6, TF=1h)
run_backtest "ath_awesome_short_v2" "hl_120pairs_mot6.json"
run_backtest "ath_awesome_short_mirror_v0" "hl_120pairs_mot6.json"

# 2. fading_bounce (MOT=5, TF=1h)
run_backtest "fading_bounce_short_v2" "hl_120pairs_mot5.json"
run_backtest "fading_bounce_short_mirror_v0" "hl_120pairs_mot5.json"

# 3. keltner_rsi_breakdown (MOT=5, TF=2h)
run_backtest "keltner_rsi_breakdown_short_v2" "hl_120pairs_mot5.json" "2h"
run_backtest "keltner_rsi_breakdown_short_mirror_v0" "hl_120pairs_mot5.json" "2h"

# 4. psar_wavetrend (MOT=6, TF=1h)
run_backtest "psar_wavetrend_short_v2" "hl_120pairs_mot6.json"
run_backtest "psar_wavetrend_short_mirror_v0" "hl_120pairs_mot6.json"

# 5. sentiment_divergence (MOT=8, TF=1h)
run_backtest "sentiment_divergence_short_v2" "hl_120pairs_mot8.json"
run_backtest "sentiment_divergence_short_mirror_v0" "hl_120pairs_mot8.json"

# 6. stpmt_atl_compression (MOT=5, TF=1h)
run_backtest "stpmt_atl_compression_short_v2" "hl_120pairs_mot5.json"
run_backtest "stpmt_atl_compression_short_mirror_v0" "hl_120pairs_mot5.json"

# 7. stretched (MOT=6, TF=1h)
run_backtest "stretched_short_v2" "hl_120pairs_mot6.json"
run_backtest "stretched_short_mirror_v0" "hl_120pairs_mot6.json"

# 8. vwap_coppock_fade (MOT=3, TF=1h)
run_backtest "vwap_coppock_fade_short_v2" "hl_120pairs_mot3.json"
run_backtest "vwap_coppock_fade_short_mirror_v0" "hl_120pairs_mot3.json"

# 9. vwap_fade (MOT=5, TF=1h)
run_backtest "vwap_fade_short_v2" "hl_120pairs_mot5.json"
run_backtest "vwap_fade_short_mirror_v0" "hl_120pairs_mot5.json"

# 10. vwap_pierce (MOT=5, TF=1h)
run_backtest "vwap_pierce_short_v2" "hl_120pairs_mot5.json"
run_backtest "vwap_pierce_short_mirror_v0" "hl_120pairs_mot5.json"

# 11. wt_divergence (MOT=6, TF=1h)
run_backtest "wt_divergence_short_v2" "hl_120pairs_mot6.json"
run_backtest "wt_divergence_short_mirror_v0" "hl_120pairs_mot6.json"

# --- NEW: 5 strategies from edge_strategy batch ---

# 12. psar_repulse (MOT=4, TF=15m)
run_backtest "psar_repulse_short_v1" "hl_120pairs_mot4.json" "15m"
run_backtest "psar_repulse_short_mirror_v0" "hl_120pairs_mot4.json" "15m"

# 13. ultosc_keltner_fade (MOT=5, TF=2h)
run_backtest "ultosc_keltner_fade_short_v1" "hl_120pairs_mot5.json" "2h"
run_backtest "ultosc_keltner_fade_short_mirror_v0" "hl_120pairs_mot5.json" "2h"

# 14. adx_obv_range (MOT=2, TF=30m)
run_backtest "adx_obv_range_short_v1" "hl_120pairs_mot2.json" "30m"
run_backtest "adx_obv_range_short_mirror_v0" "hl_120pairs_mot2.json" "30m"

# 15. obv_vwap_divergence (MOT=3, TF=1d)
run_backtest "obv_vwap_divergence_short_v1" "hl_120pairs_mot3.json" "1d"
run_backtest "obv_vwap_divergence_short_mirror_v0" "hl_120pairs_mot3.json" "1d"

# 16. bb_coppock (MOT=3, TF=1h)
run_backtest "bb_coppock_short_v1" "hl_120pairs_mot3.json"
run_backtest "bb_coppock_short_mirror_v0" "hl_120pairs_mot3.json"

echo "========================================"
echo "$(date) — ALL PHASE 3 BACKTESTS COMPLETE"
echo "========================================"
