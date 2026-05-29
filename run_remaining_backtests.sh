#!/bin/bash
# Remaining backtests for strategies missing short-side data for bilan
set +e
cd /home/moutonneux/freqtrade
source .venv/bin/activate

TR="20260101-20260501"

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
        --timerange "$TR" \
        --timeframe "$tf" \
        --export none \
        2>&1 | tail -30

    echo "$(date) — Finished: $strat"
    echo ""
}

# Strategies with V2 + JSON (use correct timeframes)
run_backtest "ath_awesome_short_v2" "hl_120pairs_mot6.json" "30m"
run_backtest "fading_bounce_short_v2" "hl_120pairs_mot5.json" "2h"
run_backtest "keltner_rsi_breakdown_short_v2" "hl_120pairs_mot5.json" "2h"
run_backtest "stpmt_atl_compression_short_v2" "hl_120pairs_mot5.json" "1h"
run_backtest "stretched_short_v2" "hl_120pairs_mot6.json" "30m"
run_backtest "vwap_coppock_fade_short_v2" "hl_120pairs_mot3.json" "1h"
run_backtest "vwap_fade_short_v2" "hl_120pairs_mot5.json" "1h"
run_backtest "vwap_pierce_short_v2" "hl_120pairs_mot5.json" "1h"
run_backtest "wt_divergence_short_v2" "hl_120pairs_mot6.json" "30m"
run_backtest "di_cross_stpmt_fade_v2" "hl_120pairs_mot4.json" "2h"
run_backtest "near_ath_trix_v2" "hl_120pairs_mot6.json" "30m"
run_backtest "rsi_keltner_fade_v2" "hl_120pairs_mot5.json" "2h"
run_backtest "sell_the_bounce_v2" "hl_120pairs_mot5.json" "1h"
run_backtest "exhaustion_hunter_v2" "hl_120pairs_mot3.json" "15m"
run_backtest "cci_breadth_reversion_v2" "hl_120pairs_mot5.json" "1h"

# Strategies without V2 JSON (use V1)
run_backtest "obv_rvwap_fade_v1" "hl_120pairs_mot3.json" "1h"
run_backtest "repulse_stoch_fade_v1" "hl_120pairs_mot5.json" "2h"
run_backtest "stpmt_fade_v1" "hl_120pairs_mot4.json" "1h"
run_backtest "wavetrend_fade_v1" "hl_120pairs_mot5.json" "15m"
run_backtest "wt_overbought_fade_v1" "hl_120pairs_mot5.json" "15m"

# Dual strategies
run_backtest "repulse_flip_dual_v1" "hl_120pairs_mot5.json" "15m"
run_backtest "ultimate_divergence_dual_v1" "hl_120pairs_mot5.json" "4h"

echo "========================================"
echo "$(date) — ALL REMAINING BACKTESTS COMPLETE"
echo "========================================"
