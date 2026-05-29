#!/bin/bash
# Phase 3 — V0 mirror backtests for dual-side screening
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
        --export none \
        2>&1 | tail -30

    echo "$(date) — Finished: $strat"
    echo ""
}

# Mirror 1: trix_cci_atr_dca_short_v2_mirror_v0 (MOT=4)
run_backtest "trix_cci_atr_dca_short_v2_mirror_v0" "hl_120pairs_mot4.json" "1h"

# Mirror 2: vwap_cci_momentum_short_v2_mirror_v0 (MOT=5)
run_backtest "vwap_cci_momentum_short_v2_mirror_v0" "hl_120pairs_mot5.json" "1h"

# Mirror 3: trix_bb_exhaustion_short_v2_mirror_v0 (MOT=4)
run_backtest "trix_bb_exhaustion_short_v2_mirror_v0" "hl_120pairs_mot4.json" "1h"

# Mirror 4: keltner_dpo_wavetrend_short_v2_mirror_v0 (MOT=3)
run_backtest "keltner_dpo_wavetrend_short_v2_mirror_v0" "hl_120pairs_mot3.json" "1h"

# Mirror 5: vd_keltner_short_v2_mirror_v0 (MOT=2)
run_backtest "vd_keltner_short_v2_mirror_v0" "hl_120pairs_mot2.json" "1h"

# Mirror 6: edge7_short_vwap_cross_dca_v2_mirror_v0 (MOT=3)
run_backtest "edge7_short_vwap_cross_dca_v2_mirror_v0" "hl_120pairs_mot3.json" "1h"

# Mirror 7: lrc_trix_short_v2_mirror_v0 (MOT=5)
run_backtest "lrc_trix_short_v2_mirror_v0" "hl_120pairs_mot5.json" "1h"

echo "========================================"
echo "$(date) — ALL PHASE 3 MIRROR BACKTESTS COMPLETE"
echo "========================================"
