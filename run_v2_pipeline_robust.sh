#!/bin/bash
set -uo pipefail

BASEDIR="/home/moutonneux/freqtrade"
VENV="$BASEDIR/.venv/bin/freqtrade"
STRAT_DIR="$BASEDIR/user_data/strategies"
RESULTS_DIR="$BASEDIR/v2_pipeline_results"
TIMERANGE="20250922-20260511"
LOCKFILE="$BASEDIR/user_data/hyperopt.lock"
MAX_RETRIES=2

mkdir -p "$RESULTS_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$RESULTS_DIR/pipeline.log"
}

clean_lock() {
    if [ -f "$LOCKFILE" ]; then
        rm -f "$LOCKFILE"
        log "Removed stale lock: $LOCKFILE"
    fi
}

run_strategy() {
    local v2_class="$1"
    local v1_class="$2"
    local v2_file="$3"
    local tf="$4"
    local config="$5"
    local loss="$6"
    local epochs="$7"
    local dca="$8"
    local v2_json="$STRAT_DIR/${v2_file%.py}.json"

    log "========== START: $v2_class =========="
    log "Config: $config | Loss: $loss | Epochs: $epochs | TF: $tf | DCA: $dca"

    # Clean stale lock before starting
    clean_lock

    # Clean any existing v2.json
    rm -f "$v2_json"
    log "Cleaned $v2_json"

    # Build hyperopt command
    local cmd="$VENV hyperopt --strategy $v2_class"
    cmd="$cmd --config $BASEDIR/backtest_configs/$config"
    cmd="$cmd --timerange $TIMERANGE"
    cmd="$cmd --timeframe $tf"
    cmd="$cmd --hyperopt-loss $loss"
    cmd="$cmd --sampler PlateauSampler"
    cmd="$cmd --epochs $epochs"
    cmd="$cmd --spaces buy -j -2"
    if [ "$dca" = "1" ] && [ "$tf" != "5m" ]; then
        cmd="$cmd --timeframe-detail 5m"
    fi

    log "HYPEROPT CMD: $cmd"

    # Try hyperopt with retries
    local attempt=1
    local success=0
    while [ $attempt -le $MAX_RETRIES ]; do
        clean_lock
        if $cmd > "$RESULTS_DIR/${v2_class}_hyperopt.log" 2>&1; then
            # Check if .json was generated (real success)
            if [ -f "$v2_json" ]; then
                log "HYPEROPT DONE: $v2_class (success, attempt $attempt)"
                success=1
                break
            else
                log "HYPEROPT attempt $attempt: exit 0 but no JSON generated — likely lock issue, retrying"
            fi
        else
            log "HYPEROPT attempt $attempt FAILED for $v2_class (exit $?)"
        fi
        attempt=$((attempt + 1))
        sleep 5
    done

    if [ $success -eq 0 ]; then
        log "HYPEROPT FAILED PERMANENTLY: $v2_class after $MAX_RETRIES attempts"
        log "========== END: $v2_class (FAILED) =========="
        return 1
    fi

    # CRITICAL: freqtrade's --spaces buy export writes empty roi/stoploss/trailing
    # sections that SILENTLY override (and disable) the strategy's class attributes.
    # An empty "roi": {} disables minimal_roi -> ROI exits never fire (cf.
    # ExhaustionHunterV2 incident: 186 -> 7 trades). Strip them so the v2 class
    # minimal_roi/stoploss are used.
    local strip_out
    strip_out=$("$BASEDIR/.venv/bin/python3" "$BASEDIR/user_data/strategies_generator/scripts/strip_param_overrides.py" "$v2_json" 2>&1)
    log "STRIP: $strip_out"

    # Build backtest base command
    local bt_base="$VENV backtesting --config $BASEDIR/backtest_configs/$config --timerange $TIMERANGE --timeframe $tf"
    if [ "$dca" = "1" ] && [ "$tf" != "5m" ]; then
        bt_base="$bt_base --timeframe-detail 5m"
    fi

    # Backtest v2
    clean_lock
    log "BACKTEST V2: $v2_class"
    if $bt_base --strategy $v2_class > "$RESULTS_DIR/${v2_class}_backtest.log" 2>&1; then
        log "BACKTEST V2 DONE: $v2_class"
    else
        log "BACKTEST V2 FAILED: $v2_class"
    fi

    # Backtest v1
    clean_lock
    log "BACKTEST V1: $v1_class"
    if $bt_base --strategy $v1_class > "$RESULTS_DIR/${v1_class}_backtest.log" 2>&1; then
        log "BACKTEST V1 DONE: $v1_class"
    else
        log "BACKTEST V1 FAILED: $v1_class"
    fi

    log "========== END: $v2_class =========="
}

log "=== V2 PIPELINE ROBUST (from #5 OBVDivergenceFade, with retry + lock cleanup) ==="
clean_lock

# 5. OBVDivergenceFadeV2 — 1h, MOT4, DCA, MoutonMeanRev, 9 params → 400 epochs
run_strategy OBVDivergenceFadeV2 OBVDivergenceFadeV1 obv_divergence_fade_v2.py 1h hl_120pairs_mot4.json MoutonMeanRevHyperOptLoss 400 1

# 6. ObvRvwapFadeV2 — 1h, MOT3, DCA, MoutonMeanRev, 9 params → 400 epochs
run_strategy ObvRvwapFadeV2 ObvRvwapFadeV1 obv_rvwap_fade_v2.py 1h hl_120pairs_mot3.json MoutonMeanRevHyperOptLoss 400 1

# 7. OverboughtEmaFadeV2 — 15m, MOT20, DCA, MoutonMeanRev, 8 params → 350 epochs
run_strategy OverboughtEmaFadeV2 OverboughtEmaFadeV1 overbought_ema_fade_v2.py 15m hl_120pairs_mot20.json MoutonMeanRevHyperOptLoss 350 1

# 8. PsarWavetrendShortV2 — 1h, MOT6, DCA, MoutonMeanRev, 12 params → 500 epochs
run_strategy PsarWavetrendShortV2 PsarWavetrendShortV1 psar_wavetrend_short_v2.py 1h hl_120pairs_mot6.json MoutonMeanRevHyperOptLoss 500 1

# 9. RangeBearFadeV2 — 30m, MOT2, DCA, MoutonMeanRev, 12 params → 500 epochs
run_strategy RangeBearFadeV2 RangeBearFadeV1 range_bear_fade_v2.py 30m hl_120pairs_mot2.json MoutonMeanRevHyperOptLoss 500 1

# 10. SentimentDivergenceShortV2 — 1h, MOT8, DCA, Calmar, 14 params → 500 epochs
run_strategy SentimentDivergenceShortV2 SentimentDivergenceShortV1 sentiment_divergence_short_v2.py 1h hl_120pairs_mot8.json CalmarHyperOptLoss 500 1

# 11. StochMomentumShortV2 — 2h, MOT3, DCA, MoutonMeanRev, 7 params → 300 epochs
run_strategy StochMomentumShortV2 StochMomentumShortV1 stoch_momentum_short_v2.py 2h hl_120pairs_mot3.json MoutonMeanRevHyperOptLoss 300 1

# 12. StochTrixFadeV2 — 2h, MOT12, DCA, Calmar, 12 params → 500 epochs
run_strategy StochTrixFadeV2 StochTrixFadeV1 stoch_trix_fade_v2.py 2h hl_120pairs_mot12.json CalmarHyperOptLoss 500 1

# 13. VwapDistributionV2 — 1d, MOT3, no DCA, Calmar, 8 params → 350 epochs
run_strategy VwapDistributionV2 VwapDistributionV1 vwap_distribution_v2.py 1d hl_120pairs_mot3.json CalmarHyperOptLoss 350 0

# 14. VwapExhaustShortV2 — 1h, MOT6, DCA, MoutonMeanRev, 8 params → 350 epochs
run_strategy VwapExhaustShortV2 VwapExhaustShortV1 vwap_exhaust_short_v2.py 1h hl_120pairs_mot6.json MoutonMeanRevHyperOptLoss 350 1

# 15. VwapRevertV2 — 1h, MOT5, DCA, MoutonMeanRev, 10 params → 450 epochs
run_strategy VwapRevertV2 VwapRevertV1 vwap_revert_v2.py 1h hl_120pairs_mot5.json MoutonMeanRevHyperOptLoss 450 1

# 16. WaveTrendFadeV2 — 15m, MOT8, DCA, MoutonMeanRev, 9 params → 400 epochs
run_strategy WaveTrendFadeV2 WaveTrendFadeV1 wavetrend_fade_v2.py 15m hl_120pairs_mot8.json MoutonMeanRevHyperOptLoss 400 1

# 17. WmaVwapShortV2 — 1h, MOT4, DCA, Calmar, 8 params → 350 epochs
run_strategy WmaVwapShortV2 WmaVwapShortV1 wma_vwap_short_v2.py 1h hl_120pairs_mot4.json CalmarHyperOptLoss 350 1

# 18. WtOverboughtFadeV2 — 15m, MOT3, DCA, MoutonMeanRev, 9 params → 400 epochs
run_strategy WtOverboughtFadeV2 WtOverboughtFadeV1 wt_overbought_fade_v2.py 15m hl_120pairs_mot3.json MoutonMeanRevHyperOptLoss 400 1

log "=== V2 PIPELINE COMPLETE ==="
log "Results in: $RESULTS_DIR"
