#!/bin/bash
# Backtests for 5 new strategies (short V1 + mirror V0)
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

# bb_coppock already done above

# psar_repulse (MOT=4, TF=15m)
run_backtest "psar_repulse_short_v1" "hl_120pairs_mot4.json" "15m"
run_backtest "psar_repulse_short_mirror_v0" "hl_120pairs_mot4.json" "15m"

# ultosc_keltner_fade (MOT=5, TF=2h)
run_backtest "ultosc_keltner_fade_short_v1" "hl_120pairs_mot5.json" "2h"
run_backtest "ultosc_keltner_fade_short_mirror_v0" "hl_120pairs_mot5.json" "2h"

# adx_obv_range (MOT=2, TF=30m)
run_backtest "adx_obv_range_short_v1" "hl_120pairs_mot2.json" "30m"
run_backtest "adx_obv_range_short_mirror_v0" "hl_120pairs_mot2.json" "30m"

# obv_vwap_divergence (MOT=3, TF=1d)
run_backtest "obv_vwap_divergence_short_v1" "hl_120pairs_mot3.json" "1d"
run_backtest "obv_vwap_divergence_short_mirror_v0" "hl_120pairs_mot3.json" "1d"

# bb_coppock mirror only (short already done)
run_backtest "bb_coppock_short_mirror_v0" "hl_120pairs_mot3.json"

echo "========================================"
echo "$(date) — ALL NEW STRATEGY BACKTESTS COMPLETE"
echo "========================================"
