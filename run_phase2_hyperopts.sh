#!/bin/bash
# Phase 2 — Sequential PlateauSampler hyperopts for all v2 strategies
# Launched automatically. Each hyperopt runs sequentially (single-thread-safe).

set -e
cd /home/moutonneux/freqtrade
source .venv/bin/activate

TR="20260101-20260501"

run_hyperopt() {
    local strat="$1"
    local config="$2"
    local tf="$3"
    local epochs="$4"

    echo "========================================"
    echo "$(date) — Starting: $strat ($epochs epochs)"
    echo "========================================"

    rm -f "user_data/strategies/${strat}.json"

    .venv/bin/freqtrade hyperopt \
        --strategy "$strat" \
        --config "backtest_configs/$config" \
        --timerange "$TR" \
        --timeframe "$tf" \
        --hyperopt-loss MoutonMeanRevHyperOptLoss \
        --sampler PlateauSampler \
        --epochs "$epochs" \
        --spaces buy \
        -j -2

    echo "$(date) — Finished: $strat"

    # Strip empty sections from any generated json
    local json="user_data/strategies/${strat}.json"
    if [ -f "$json" ]; then
        .venv/bin/python3 user_data/strategies_generator/scripts/strip_param_overrides.py "$json" 2>/dev/null || true
    fi
    echo ""
}

# Strategy 1: vwap_cci_momentum_short_v2 (8 params, 1h, MOT=5)
run_hyperopt "vwap_cci_momentum_short_v2" "hl_120pairs_mot5.json" "1h" 350

# Strategy 2: trix_bb_exhaustion_short_v2 (15 params, 1h, MOT=4)
run_hyperopt "trix_bb_exhaustion_short_v2" "hl_120pairs_mot4.json" "1h" 500

# Strategy 3: keltner_dpo_wavetrend_short_v2 (15 params, 1h, MOT=3)
run_hyperopt "keltner_dpo_wavetrend_short_v2" "hl_120pairs_mot3.json" "1h" 500

# Strategy 6: vd_keltner_short_v2 (11 params, 1h, MOT=2)
run_hyperopt "vd_keltner_short_v2" "hl_120pairs_mot2.json" "1h" 500

# Strategy 7: edge7_short_vwap_cross_dca_v2 (11 params, 1h, MOT=3)
run_hyperopt "edge7_short_vwap_cross_dca_v2" "hl_120pairs_mot3.json" "1h" 500

# Strategy 8: lrc_trix_short_v2 (11 params, 1h, MOT=5)
run_hyperopt "lrc_trix_short_v2" "hl_120pairs_mot5.json" "1h" 500

echo "========================================"
echo "$(date) — ALL PHASE 2 HYPEROPTS COMPLETE"
echo "========================================"
/home/moutonneux/.config/claude-notify/send.sh "Phase 2 hyperopts ALL DONE" 2>/dev/null || true
