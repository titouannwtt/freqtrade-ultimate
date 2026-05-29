#!/usr/bin/env python3
"""Extract best PlateauSampler params from .fthypt files and create clean .json files."""
import json
import glob
import sys
import os


def process_strategy(strategy_name: str):
    pattern = f"user_data/hyperopt_results/strategy_{strategy_name}_*.fthypt"
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"  ❌ No .fthypt file found for {strategy_name}")
        return None

    path = files[-1]
    with open(path) as f:
        lines = f.readlines()

    if not lines:
        print(f"  ❌ Empty .fthypt file for {strategy_name}")
        return None

    best_loss, best_data = float("inf"), None
    for line in lines:
        d = json.loads(line)
        if d.get("loss", float("inf")) < best_loss:
            best_loss = d["loss"]
            best_data = d

    if not best_data:
        return None

    m = best_data["results_metrics"]
    buy_params = best_data.get("params_details", {}).get("buy", {})

    print(f"  Epochs: {len(lines)}")
    print(f"  Best loss: {best_loss:.4f}")
    print(f"  Trades: {m['total_trades']}, Profit: {m['profit_total']*100:.2f}%")
    print(f"  DD: {m['max_drawdown_account']*100:.1f}%, WR: {m['wins']}/{m['total_trades']}")
    print(f"  Params: {buy_params}")

    json_path = f"user_data/strategies/{strategy_name}.json"
    clean_json = {
        "strategy_name": strategy_name,
        "params": {
            "buy": buy_params
        }
    }

    with open(json_path, "w") as f:
        json.dump(clean_json, f, indent=2)
    print(f"  ✅ Written: {json_path} (buy-only, no empty sections)")

    return {
        "strategy": strategy_name,
        "epochs": len(lines),
        "trades": m["total_trades"],
        "profit_pct": m["profit_total"] * 100,
        "dd_pct": m["max_drawdown_account"] * 100,
        "wins": m["wins"],
        "losses": m["losses"],
        "params": buy_params,
    }


if __name__ == "__main__":
    strategies = sys.argv[1:] if len(sys.argv) > 1 else [
        "trix_cci_atr_dca_short_v2",
        "vwap_cci_momentum_short_v2",
        "trix_bb_exhaustion_short_v2",
        "keltner_dpo_wavetrend_short_v2",
        "vd_keltner_short_v2",
        "edge7_short_vwap_cross_dca_v2",
        "lrc_trix_short_v2",
    ]

    results = []
    for s in strategies:
        print(f"\n{'='*60}")
        print(f"  {s}")
        print(f"{'='*60}")
        r = process_strategy(s)
        if r:
            results.append(r)

    if results:
        print(f"\n\n{'='*80}")
        print(f"{'SUMMARY':^80}")
        print(f"{'='*80}")
        print(f"{'Strategy':<35} {'Trades':>6} {'Profit':>8} {'DD':>6} {'WR':>6}")
        print("-" * 80)
        for r in results:
            wr = r["wins"] / max(1, r["trades"]) * 100
            print(f"{r['strategy']:<35} {r['trades']:>6} {r['profit_pct']:>+7.2f}% {r['dd_pct']:>5.1f}% {wr:>5.1f}%")
