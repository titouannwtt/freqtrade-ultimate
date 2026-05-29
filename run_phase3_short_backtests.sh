#!/bin/bash
# Phase 3 — Short V2 backtests for final report
set -e
cd /home/moutonneux/freqtrade
source .venv/bin/activate

TR="20260101-20260501"

run_backtest() {
    local strat="$1"
    local config="$2"
    local tf="$3"

    echo "========================================"
    echo "$(date) — Backtesting: $strat"
    echo "========================================"

    .venv/bin/freqtrade backtesting \
        --strategy "$strat" \
        --config "backtest_configs/$config" \
        --timerange "$TR" \
        --timeframe "$tf" \
        --export none

    echo "$(date) — Finished: $strat"
    echo ""
}

run_backtest "trix_cci_atr_dca_short_v2" "hl_120pairs_mot4.json" "1h"
run_backtest "vwap_cci_momentum_short_v2" "hl_120pairs_mot5.json" "1h"
run_backtest "trix_bb_exhaustion_short_v2" "hl_120pairs_mot4.json" "1h"
run_backtest "keltner_dpo_wavetrend_short_v2" "hl_120pairs_mot3.json" "1h"
run_backtest "vd_keltner_short_v2" "hl_120pairs_mot2.json" "1h"
run_backtest "edge7_short_vwap_cross_dca_v2" "hl_120pairs_mot3.json" "1h"
run_backtest "lrc_trix_short_v2" "hl_120pairs_mot5.json" "1h"

echo "========================================"
echo "$(date) — ALL SHORT V2 BACKTESTS COMPLETE"
echo "========================================"
