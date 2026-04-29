from __future__ import annotations

import gc
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import combinations as itertools_combinations
from math import comb, erf, log, sqrt
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import rapidjson

from freqtrade.constants import Config
from freqtrade.exceptions import OperationalException
from freqtrade.optimize.hyperopt_tools import HyperoptTools, hyperopt_serializer
from freqtrade.resolvers.exchange_resolver import ExchangeResolver


if TYPE_CHECKING:
    from freqtrade.optimize.wfa_output import WFADashboard


logger = logging.getLogger(__name__)

HYPER_PARAMS_FILE_FORMAT = rapidjson.NM_NATIVE | rapidjson.NM_NAN
PARAM_STABILITY_STABLE = 0.15
PARAM_STABILITY_UNSTABLE = 0.30
EULER_MASCHERONI = 0.5772156649


@dataclass
class WalkForwardWindow:
    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime

    def train_timerange_str(self) -> str:
        return f"{self.train_start.strftime('%Y%m%d')}-{self.train_end.strftime('%Y%m%d')}"

    def test_timerange_str(self) -> str:
        return f"{self.test_start.strftime('%Y%m%d')}-{self.test_end.strftime('%Y%m%d')}"


@dataclass
class WindowResult:
    window: WalkForwardWindow
    train_metrics: dict[str, Any] = field(default_factory=dict)
    test_metrics: dict[str, Any] = field(default_factory=dict)
    baseline_metrics: dict[str, Any] = field(default_factory=dict)
    market_context: dict[str, float] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    test_trade_count: int = 0
    test_trade_profits: list[float] = field(default_factory=list)
    wfe: float = 0.0


@dataclass
class MCResult:
    """Monte Carlo trade-shuffle simulation results.
    Return is invariant under permutation (sum doesn't change).
    Max DD and return/DD ratio are path-dependent — those vary."""

    total_return_pct: float = 0.0
    max_dd_p5: float = 0.0
    max_dd_p50: float = 0.0
    max_dd_p95: float = 0.0
    return_dd_p5: float = 0.0
    return_dd_p50: float = 0.0
    return_dd_p95: float = 0.0
    max_consec_loss_p50: int = 0
    max_consec_loss_p95: int = 0
    n_simulations: int = 0


@dataclass
class OOSEquityCurve:
    """Concatenated OOS equity curve metrics."""

    total_return_pct: float = 0.0
    max_dd_pct: float = 0.0
    k_ratio: float = 0.0
    n_trades: int = 0


@dataclass
class RegimeAnalysis:
    """OOS performance breakdown by market regime."""

    regime_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    worst_regime: str = ""
    regime_dependent: bool = False


@dataclass
class PerturbResult:
    """Parameter perturbation test results."""

    n_perturbations: int = 0
    profit_p5: float = 0.0
    profit_p50: float = 0.0
    profit_p95: float = 0.0
    pct_profitable: float = 0.0
    sensitivity: float = 0.0


@dataclass
class MultiSeedResult:
    """Multi-seed hyperopt convergence results."""

    n_seeds: int = 0
    convergence_pct: float = 0.0
    seed_params: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CPCVResult:
    """Combinatorial Purged Cross-Validation results."""

    n_groups: int = 0
    n_test_groups: int = 0
    n_combinations: int = 0
    n_paths: int = 0
    path_returns: list[float] = field(default_factory=list)
    avg_return: float = 0.0
    sharpe_of_paths: float = 0.0
    prob_of_loss: float = 0.0
    combo_metrics: list[dict[str, Any]] = field(default_factory=list)


class WalkForward:
    """
    Walk-forward analysis: N sequential cycles of
    hyperopt(train) -> backtest(test) with disjoint windows.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.n_windows: int = config.get("wf_windows", 5)
        self.train_ratio: float = config.get("wf_train_ratio", 0.75)
        self.embargo_days: int = config.get("wf_embargo_days", 7)
        self.holdout_months: int = config.get("wf_holdout_months", 0)
        self.min_test_trades: int = config.get("wf_min_test_trades", 30)
        self.wf_mode: str = config.get("wf_mode", "rolling")
        self.cpcv_groups: int = config.get("wf_cpcv_groups", 6)
        self.cpcv_test_groups: int = config.get("wf_cpcv_test_groups", 2)
        self.strategy_name: str = config["strategy"]

        self.results: list[WindowResult] = []
        self.holdout_result: WindowResult | None = None

        self._wfa_dir = Path(config["user_data_dir"]) / "walk_forward"
        self._wfa_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def get_lock_filename(config: Config) -> str:
        return str(Path(config["user_data_dir"]) / "walk_forward.lock")

    # ------------------------------------------------------------------
    # Window computation
    # ------------------------------------------------------------------

    def _parse_timerange(self) -> tuple[datetime, datetime]:
        timerange_str = self.config.get("timerange", "")
        if not timerange_str or "-" not in timerange_str:
            raise OperationalException(
                "Walk-forward analysis requires --timerange in format YYYYMMDD-YYYYMMDD."
            )
        parts = timerange_str.split("-")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise OperationalException(
                "Walk-forward analysis requires --timerange "
                "in format YYYYMMDD-YYYYMMDD (both start and end)."
            )
        start = datetime.strptime(parts[0], "%Y%m%d").replace(tzinfo=UTC)
        end = datetime.strptime(parts[1], "%Y%m%d").replace(tzinfo=UTC)
        return start, end

    def _compute_windows(self) -> list[WalkForwardWindow]:
        if self.wf_mode == "anchored":
            return self._compute_windows_anchored()
        return self._compute_windows_rolling()

    def _compute_windows_rolling(self) -> list[WalkForwardWindow]:
        full_start, full_end = self._parse_timerange()
        holdout_delta = timedelta(days=self.holdout_months * 30)
        usable_end = full_end - holdout_delta
        total_days = (usable_end - full_start).days

        if total_days <= 0:
            raise OperationalException(
                f"Not enough data after reserving {self.holdout_months} "
                f"months holdout. Total range: "
                f"{full_start:%Y-%m-%d} to {full_end:%Y-%m-%d}."
            )

        r = self.train_ratio
        n = self.n_windows
        embargo = timedelta(days=self.embargo_days)

        total_embargo_days = self.embargo_days * n
        effective_days = total_days - total_embargo_days
        if effective_days <= 0:
            raise OperationalException(
                f"Not enough data after accounting for {n} embargo "
                f"periods of {self.embargo_days} days each."
            )

        test_days = effective_days * (1 - r) / (r + n * (1 - r))
        train_days = test_days * r / (1 - r)

        if train_days < 60:
            raise OperationalException(
                f"Train window too short ({train_days:.0f} days). "
                f"Need at least 60 days. Reduce --wf-windows or "
                f"expand --timerange."
            )
        if test_days < 20:
            raise OperationalException(
                f"Test window too short ({test_days:.0f} days). "
                f"Need at least 20 days. Reduce --wf-windows or "
                f"expand --timerange."
            )

        step = test_days + self.embargo_days
        windows = []
        for i in range(n):
            t_start = full_start + timedelta(days=i * step)
            t_end = t_start + timedelta(days=train_days)
            test_start = t_end + embargo
            test_end = test_start + timedelta(days=test_days)

            if test_end > usable_end + timedelta(days=1):
                logger.warning(
                    f"Window {i + 1} test end ({test_end:%Y-%m-%d}) "
                    f"exceeds usable range ({usable_end:%Y-%m-%d}). "
                    f"Clamping."
                )
                test_end = usable_end

            windows.append(
                WalkForwardWindow(
                    index=i,
                    train_start=t_start,
                    train_end=t_end,
                    test_start=test_start,
                    test_end=test_end,
                )
            )

        return windows

    def _compute_windows_anchored(self) -> list[WalkForwardWindow]:
        """Anchored mode: train always starts at the beginning, growing
        for each subsequent window. Test windows are sequential."""
        full_start, full_end = self._parse_timerange()
        holdout_delta = timedelta(days=self.holdout_months * 30)
        usable_end = full_end - holdout_delta
        total_days = (usable_end - full_start).days

        if total_days <= 0:
            raise OperationalException(
                f"Not enough data after reserving {self.holdout_months} "
                f"months holdout. Total range: "
                f"{full_start:%Y-%m-%d} to {full_end:%Y-%m-%d}."
            )

        r = self.train_ratio
        n = self.n_windows

        step = total_days * (1 - r) / (r + n * (1 - r))
        test_days = step - self.embargo_days
        train_initial = total_days - n * step

        if train_initial < 60:
            raise OperationalException(
                f"Initial train window too short ({train_initial:.0f} days). "
                f"Need at least 60 days. Reduce --wf-windows or "
                f"expand --timerange."
            )
        if test_days < 20:
            raise OperationalException(
                f"Test window too short ({test_days:.0f} days). "
                f"Need at least 20 days. Reduce --wf-windows or "
                f"expand --timerange."
            )

        embargo = timedelta(days=self.embargo_days)
        windows = []
        for i in range(n):
            train_end = full_start + timedelta(days=train_initial + i * step)
            test_start = train_end + embargo
            test_end = test_start + timedelta(days=test_days)

            if test_end > usable_end + timedelta(days=1):
                test_end = usable_end

            windows.append(
                WalkForwardWindow(
                    index=i,
                    train_start=full_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                )
            )

        return windows

    def _compute_holdout_window(self, full_end: datetime) -> WalkForwardWindow | None:
        if self.holdout_months <= 0:
            return None
        holdout_start = full_end - timedelta(days=self.holdout_months * 30)
        return WalkForwardWindow(
            index=-1,
            train_start=holdout_start,
            train_end=holdout_start,
            test_start=holdout_start,
            test_end=full_end,
        )

    # ------------------------------------------------------------------
    # CPCV: group computation + combinations
    # ------------------------------------------------------------------

    def _compute_cpcv_groups(self) -> list[tuple[datetime, datetime]]:
        full_start, full_end = self._parse_timerange()
        holdout_delta = timedelta(days=self.holdout_months * 30)
        usable_end = full_end - holdout_delta
        total_days = (usable_end - full_start).days
        n = self.cpcv_groups
        group_days = total_days / n
        groups = []
        for i in range(n):
            g_start = full_start + timedelta(days=i * group_days)
            g_end = full_start + timedelta(days=(i + 1) * group_days)
            if i == n - 1:
                g_end = usable_end
            groups.append((g_start, g_end))
        return groups

    @staticmethod
    def _compute_cpcv_combinations(
        n_groups: int, n_test: int
    ) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
        combos = []
        for test_indices in itertools_combinations(range(n_groups), n_test):
            train_indices = tuple(i for i in range(n_groups) if i not in test_indices)
            combos.append((train_indices, test_indices))
        return combos

    def _apply_cpcv_embargo(
        self,
        groups: list[tuple[datetime, datetime]],
        train_indices: tuple[int, ...],
        test_indices: tuple[int, ...],
    ) -> tuple[list[tuple[datetime, datetime]], list[tuple[datetime, datetime]]]:
        embargo = timedelta(days=self.embargo_days)
        test_set = set(test_indices)
        purged_train: list[tuple[datetime, datetime]] = []
        for i in train_indices:
            start, end = groups[i]
            if i + 1 in test_set:
                end = end - embargo
            if i - 1 in test_set:
                start = start + embargo
            if end > start:
                purged_train.append((start, end))
        test_ranges = [groups[i] for i in test_indices]
        return purged_train, test_ranges

    def _run_cpcv_combination(
        self,
        combo_idx: int,
        test_ranges: list[tuple[datetime, datetime]],
        strategy_json: Path | None,
        exchange: Any | None = None,
    ) -> dict[str, Any]:
        all_profits: list[float] = []
        total_profit_pct = 0.0
        total_trades = 0
        total_dd = 0.0
        for start, end in test_ranges:
            tr_str = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
            metrics = self._run_backtest(tr_str, exchange=exchange)
            total_profit_pct += metrics.get("profit_pct", 0)
            total_trades += metrics.get("trades", 0)
            total_dd = max(total_dd, metrics.get("max_dd_pct", 0))
            all_profits.extend(metrics.get("trade_profits", []))
        return {
            "combo_idx": combo_idx,
            "profit_pct": total_profit_pct,
            "trades": total_trades,
            "max_dd_pct": total_dd,
            "trade_profits": all_profits,
        }

    @staticmethod
    def _aggregate_cpcv(
        combo_results: list[dict[str, Any]],
        n_groups: int,
        n_test: int,
    ) -> CPCVResult:
        if not combo_results:
            return CPCVResult()
        returns = [c["profit_pct"] for c in combo_results]
        arr = np.array(returns)
        avg_ret = float(np.mean(arr))
        std_ret = float(np.std(arr))
        sharpe = avg_ret / std_ret if std_ret > 1e-8 else 0.0
        prob_loss = sum(1 for r in returns if r <= 0) / len(returns)
        n_paths = comb(n_groups, n_test) * n_test // n_groups
        return CPCVResult(
            n_groups=n_groups,
            n_test_groups=n_test,
            n_combinations=len(combo_results),
            n_paths=n_paths,
            path_returns=returns,
            avg_return=round(avg_ret, 2),
            sharpe_of_paths=round(sharpe, 4),
            prob_of_loss=round(prob_loss, 4),
            combo_metrics=combo_results,
        )

    def _run_cpcv(
        self,
        consensus: dict[str, Any],
        strategy_json: Path | None,
        original_json_bytes: bytes | None,
        exchange: Any | None = None,
    ) -> CPCVResult:
        groups = self._compute_cpcv_groups()
        combos = self._compute_cpcv_combinations(self.cpcv_groups, self.cpcv_test_groups)
        logger.info(
            f"CPCV: {len(combos)} combinations (N={self.cpcv_groups}, K={self.cpcv_test_groups})"
        )
        self._restore_params(consensus, strategy_json)
        resolver_logger = logging.getLogger("freqtrade.resolvers")
        prev_level = resolver_logger.level
        resolver_logger.setLevel(logging.WARNING)
        results: list[dict[str, Any]] = []
        try:
            for idx, (train_idx, test_idx) in enumerate(combos):
                _, test_ranges = self._apply_cpcv_embargo(groups, train_idx, test_idx)
                logger.info(f"  Combo {idx + 1}/{len(combos)}: test groups {test_idx}")
                result = self._run_cpcv_combination(
                    idx,
                    test_ranges,
                    strategy_json,
                    exchange=exchange,
                )
                results.append(result)
        finally:
            resolver_logger.setLevel(prev_level)
            self._restore_original_json(strategy_json, original_json_bytes)
        return self._aggregate_cpcv(results, self.cpcv_groups, self.cpcv_test_groups)

    def _validate_cpcv(self) -> None:
        n = self.cpcv_groups
        k = self.cpcv_test_groups
        if n < 4:
            raise OperationalException(f"--wf-cpcv-groups must be >= 4, got {n}.")
        if k >= n:
            raise OperationalException(
                f"--wf-cpcv-test-groups ({k}) must be < --wf-cpcv-groups ({n})."
            )
        if k < 1:
            raise OperationalException(f"--wf-cpcv-test-groups must be >= 1, got {k}.")
        n_combos = comb(n, k)
        if n_combos > 120:
            raise OperationalException(
                f"C({n},{k}) = {n_combos} combinations is too many "
                f"(max 120). Reduce --wf-cpcv-groups or --wf-cpcv-test-groups."
            )
        full_start, full_end = self._parse_timerange()
        holdout_delta = timedelta(days=self.holdout_months * 30)
        usable_days = (full_end - holdout_delta - full_start).days
        group_days = usable_days / n
        if group_days < 20:
            raise OperationalException(
                f"CPCV group size too short ({group_days:.0f} days). "
                f"Need at least 20 days per group."
            )

    def _load_latest_consensus(self) -> dict[str, Any]:
        pattern = f"{self.strategy_name}_consensus_*.json"
        files = sorted(self._wfa_dir.glob(pattern))
        if files:
            with files[-1].open("r") as f:
                data = rapidjson.load(f, number_mode=HYPER_PARAMS_FILE_FORMAT)
            return data.get("params", {})
        json_path = self._get_strategy_json_path()
        if json_path and json_path.exists():
            with json_path.open("r") as f:
                data = rapidjson.load(f, number_mode=HYPER_PARAMS_FILE_FORMAT)
            return data.get("params", {})
        raise OperationalException(
            f"No consensus params found for {self.strategy_name}. "
            f"Run a standard WFA first, or provide a co-located strategy JSON."
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, windows: list[WalkForwardWindow]) -> None:
        if not (0.5 <= self.train_ratio <= 0.9):
            raise OperationalException(
                f"--wf-train-ratio must be between 0.5 and 0.9, got {self.train_ratio}."
            )

        for w in windows:
            train_days = (w.train_end - w.train_start).days
            if train_days > 18 * 30:
                logger.warning(
                    f"Window {w.index + 1}: train period is "
                    f"{train_days} days (>{18 * 30}). "
                    f"Risk of fitting to outdated regimes (tip #26)."
                )

        if not self.config.get("timeframe_detail"):
            logger.warning(
                "No --timeframe-detail specified. DCA/stoploss fills "
                "will be simulated at candle open price, which "
                "overestimates results (tip #20)."
            )

        epochs = self.config.get("epochs", 150)
        if epochs > 300:
            logger.warning(
                f"Epochs per window ({epochs}) > 300. Risk of per-window overfitting (tip #14)."
            )

        if 0 < self.holdout_months < 2:
            logger.warning(
                f"Holdout of {self.holdout_months} month(s) may be too "
                f"short to catch regime shifts. Consider >= 2 months."
            )

    # ------------------------------------------------------------------
    # Plan display
    # ------------------------------------------------------------------

    def _print_plan(self, windows: list[WalkForwardWindow]) -> None:
        logger.info("=" * 70)
        logger.info(f"Walk-Forward Analysis Plan — {self.strategy_name}")
        logger.info(
            f"Loss: {self.config.get('hyperopt_loss', 'default')} | "
            f"{self.n_windows} windows ({self.wf_mode}) | "
            f"{self.config.get('epochs', 150)} epochs/window"
        )
        logger.info("=" * 70)

        for w in windows:
            embargo_str = f" [{self.embargo_days}d embargo]"
            logger.info(
                f"  Window {w.index + 1}: "
                f"Train {w.train_start:%Y-%m-%d} -> "
                f"{w.train_end:%Y-%m-%d} |"
                f"{embargo_str} | "
                f"Test {w.test_start:%Y-%m-%d} -> "
                f"{w.test_end:%Y-%m-%d}"
            )

        if self.holdout_months > 0:
            _, full_end = self._parse_timerange()
            ho = self._compute_holdout_window(full_end)
            if ho:
                logger.info(
                    f"  Holdout: {ho.test_start:%Y-%m-%d} -> {ho.test_end:%Y-%m-%d} (untouched)"
                )

        logger.info("=" * 70)

    # ------------------------------------------------------------------
    # Strategy JSON helpers
    # ------------------------------------------------------------------

    def _get_strategy_json_path(self) -> Path | None:
        fn = HyperoptTools.get_strategy_filename(self.config, self.strategy_name)
        if fn:
            return fn.with_suffix(".json")
        return None

    def _delete_strategy_json(self, json_path: Path | None) -> None:
        if json_path and json_path.exists():
            json_path.unlink()
            logger.info(f"Deleted co-located JSON: {json_path}")

    def _save_window_params(self, window_index: int, json_path: Path | None) -> dict[str, Any]:
        if not json_path or not json_path.exists():
            return {}
        with json_path.open("r") as f:
            params = rapidjson.load(f, number_mode=HYPER_PARAMS_FILE_FORMAT)

        dest = self._wfa_dir / f"window_{window_index}_params.json"
        with dest.open("w") as f:
            rapidjson.dump(
                params,
                f,
                indent=2,
                default=hyperopt_serializer,
                number_mode=HYPER_PARAMS_FILE_FORMAT,
            )
        return params.get("params", {})

    def _restore_params(self, params_dict: dict[str, Any], json_path: Path | None) -> None:
        """Write params to the co-located JSON so Backtesting loads them."""
        if not json_path:
            return
        export_data = {
            "strategy_name": self.strategy_name,
            "params": params_dict,
            "ft_stratparam_v": 1,
            "export_time": datetime.now(UTC).isoformat(),
        }
        with json_path.open("w") as f:
            rapidjson.dump(
                export_data,
                f,
                indent=2,
                default=hyperopt_serializer,
                number_mode=HYPER_PARAMS_FILE_FORMAT,
            )

    # ------------------------------------------------------------------
    # Hyperopt / Backtest runners
    # ------------------------------------------------------------------

    def _run_hyperopt_window(
        self,
        window: WalkForwardWindow,
        base_seed: int | None,
        dashboard: WFADashboard | None = None,
    ) -> dict[str, Any]:
        from freqtrade.optimize.hyperopt import Hyperopt

        cfg = deepcopy(self.config)
        cfg["timerange"] = window.train_timerange_str()
        cfg["runmode"] = "hyperopt"
        cfg["wfa_silent"] = True

        if base_seed is not None:
            cfg["hyperopt_random_state"] = base_seed + window.index

        hyperopt = Hyperopt(cfg)
        if dashboard:
            hyperopt._epoch_callback = dashboard.on_epoch
        hyperopt.start()

        best = hyperopt.current_best_epoch
        if best:
            raw_metrics = best.get("results_metrics", {})
            return {
                "loss": best.get("loss", 0),
                "params": best.get("params_details", {}),
                "metrics": self._extract_metrics(raw_metrics),
            }

        logger.warning(
            f"Window {window.index + 1}: hyperopt produced no valid epochs! "
            f"All epochs may have been rejected by the loss function. "
            f"Test backtest will run with default params."
        )
        return {"loss": 0, "params": {}, "metrics": {}}

    def _run_backtest(self, timerange_str: str, exchange: Any | None = None) -> dict[str, Any]:
        from freqtrade.optimize.backtesting import Backtesting

        cfg = deepcopy(self.config)
        cfg["timerange"] = timerange_str
        cfg["runmode"] = "backtest"
        cfg["export"] = "none"
        cfg["wfa_silent"] = True
        cfg.pop("backtest_cache", None)

        bt = Backtesting(cfg, exchange=exchange)
        bt.start()

        strat_results: dict[str, Any] = {}
        if bt.results and "strategy" in bt.results:
            strat_data = bt.results["strategy"].get(self.strategy_name, {})
            strat_results = self._extract_metrics(strat_data)

            trades = strat_data.get("trades", [])
            if trades:
                profits = [t.get("profit_abs", 0) for t in trades]
                strat_results.update(self._compute_concentration(profits))
                strat_results["trade_profits"] = profits

        Backtesting.cleanup()
        return strat_results

    def _extract_metrics(self, strat_data: dict[str, Any]) -> dict[str, Any]:
        return {
            "profit_pct": strat_data.get("profit_total", 0) * 100,
            "profit_abs": strat_data.get("profit_total_abs", 0),
            "trades": strat_data.get("total_trades", 0),
            "calmar": strat_data.get("calmar", 0),
            "sharpe": strat_data.get("sharpe", 0),
            "sortino": strat_data.get("sortino", 0),
            "max_dd_pct": strat_data.get("max_drawdown_account", 0) * 100,
            "profit_factor": strat_data.get("profit_factor", 0),
            "win_rate": strat_data.get("winrate", 0),
            "avg_duration": strat_data.get("holding_avg", ""),
        }

    # ------------------------------------------------------------------
    # Market context (Improvement #2)
    # ------------------------------------------------------------------

    def _compute_market_context(self, test_start: datetime, test_end: datetime) -> dict[str, float]:
        """Load BTC data for the test period and compute context metrics."""
        try:
            from freqtrade.configuration.timerange import TimeRange
            from freqtrade.data.history import load_pair_history
            from freqtrade.enums import CandleType

            tr_str = f"{test_start.strftime('%Y%m%d')}-{test_end.strftime('%Y%m%d')}"
            tr = TimeRange.parse_timerange(tr_str)
            trading_mode = self.config.get("trading_mode", "spot")
            candle_type = CandleType.FUTURES if trading_mode == "futures" else CandleType.SPOT
            stake = self.config.get("stake_currency", "USDT")
            if candle_type == CandleType.FUTURES:
                btc_pair = f"BTC/{stake}:{stake}"
            else:
                btc_pair = f"BTC/{stake}"

            df = load_pair_history(
                pair=btc_pair,
                timeframe="1d",
                datadir=self.config["datadir"],
                timerange=tr,
                fill_up_missing=False,
                candle_type=candle_type,
            )
            if df.empty or len(df) < 2:
                return {}

            btc_change = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
            atr = (df["high"] - df["low"]).mean() / df["close"].mean() * 100
            daily_returns = df["close"].pct_change().dropna()
            vol_ann = float(daily_returns.std() * np.sqrt(365) * 100)

            cum_ret = (df["close"].iloc[-1] / df["close"].iloc[0]) - 1
            if cum_ret > 0.10:
                regime = "bull"
            elif cum_ret < -0.10:
                regime = "bear"
            else:
                regime = "range"

            return {
                "btc_change_pct": round(float(btc_change), 1),
                "atr_pct": round(float(atr), 1),
                "volatility_ann_pct": round(vol_ann, 1),
                "regime": regime,
            }
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Concentrated profit check (Improvement #3)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_concentration(profits: list[float]) -> dict[str, float]:
        """HHI and top-1 trade concentration from per-trade profits."""
        if not profits:
            return {"hhi": 0.0, "top1_pct": 0.0}

        total_abs = sum(abs(p) for p in profits)
        if total_abs < 1e-8:
            return {"hhi": 0.0, "top1_pct": 0.0}

        shares = [abs(p) / total_abs for p in profits]
        hhi = sum(s**2 for s in shares)

        pos_profits = [p for p in profits if p > 0]
        total_pos = sum(pos_profits) if pos_profits else 0
        top1 = max(pos_profits) / total_pos * 100 if total_pos > 0 else 0

        return {
            "hhi": round(hhi, 4),
            "top1_pct": round(top1, 1),
        }

    # ------------------------------------------------------------------
    # Param stability analysis
    # ------------------------------------------------------------------

    def _get_search_ranges(self) -> dict[str, tuple[float, float]]:
        """Extract parameter search ranges from the strategy."""
        from freqtrade.resolvers.strategy_resolver import StrategyResolver
        from freqtrade.strategy.parameters import NumericParameter

        strategy = StrategyResolver.load_strategy(self.config)
        ranges: dict[str, tuple[float, float]] = {}

        for attr_name in dir(strategy):
            attr = getattr(strategy, attr_name, None)
            if isinstance(attr, NumericParameter):
                ranges[attr_name] = (float(attr.low), float(attr.high))

        return ranges

    @staticmethod
    def _analyze_param_stability(
        all_params: list[dict[str, Any]],
        search_ranges: dict[str, tuple[float, float]],
    ) -> dict[str, dict[str, Any]]:
        stability: dict[str, dict[str, Any]] = {}

        all_keys: set[str] = set()
        for p in all_params:
            for space_params in p.values():
                if isinstance(space_params, dict):
                    all_keys.update(space_params.keys())

        for key in sorted(all_keys):
            values = []
            for p in all_params:
                for space_params in p.values():
                    if isinstance(space_params, dict) and key in space_params:
                        val = space_params[key]
                        if isinstance(val, int | float):
                            values.append(float(val))

            if len(values) < 2:
                continue

            arr = np.array(values)
            mean = float(np.mean(arr))
            std = float(np.std(arr))
            median = float(np.median(arr))

            if key in search_ranges:
                lo, hi = search_ranges[key]
                search_range = hi - lo
                std_over_range = std / search_range if search_range > 0 else 0
            else:
                std_over_range = std / abs(mean) if abs(mean) > 1e-8 else 0

            stability[key] = {
                "values": [round(v, 4) for v in values],
                "mean": round(mean, 4),
                "std": round(std, 4),
                "median": round(median, 4),
                "std_over_range": round(std_over_range, 4),
                "stable": std_over_range < PARAM_STABILITY_STABLE,
                "unstable": std_over_range > PARAM_STABILITY_UNSTABLE,
            }

        return stability

    @staticmethod
    def _weighted_median(values: list[float], weights: list[float]) -> float:
        """Weighted median: value where cumulative weight reaches 50%."""
        arr = np.array(values)
        w = np.array(weights)
        idx = np.argsort(arr)
        sorted_vals = arr[idx]
        cum_w = np.cumsum(w[idx])
        cutoff = cum_w[-1] / 2.0
        return float(sorted_vals[cum_w >= cutoff][0])

    @staticmethod
    def _resolve_consensus_value(
        values: list[Any],
        weights: list[float] | None,
        w_list: list[float],
    ) -> Any:
        """Resolve a single parameter's consensus value."""
        if all(isinstance(v, int | float) for v in values):
            if weights is not None and len(w_list) == len(values):
                med = WalkForward._weighted_median([float(v) for v in values], w_list)
            else:
                med = float(np.median(values))
            if all(isinstance(v, int) for v in values):
                return round(med)
            return round(med, 6)

        from collections import Counter

        return Counter(values).most_common(1)[0][0]

    @staticmethod
    def _compute_consensus_params(
        all_params: list[dict[str, Any]],
        weights: list[float] | None = None,
    ) -> dict[str, Any]:
        """Compute (weighted) median per parameter across all windows.
        When weights are provided, uses weighted median (by test Calmar)."""
        consensus: dict[str, Any] = {}

        spaces: set[str] = set()
        for p in all_params:
            spaces.update(p.keys())

        for space in sorted(spaces):
            consensus[space] = {}
            keys: set[str] = set()
            for p in all_params:
                if space in p and isinstance(p[space], dict):
                    keys.update(p[space].keys())

            for key in sorted(keys):
                values = []
                w_list: list[float] = []
                for i, p in enumerate(all_params):
                    if space in p and isinstance(p[space], dict) and key in p[space]:
                        values.append(p[space][key])
                        if weights is not None and i < len(weights):
                            w_list.append(weights[i])

                if values:
                    consensus[space][key] = WalkForward._resolve_consensus_value(
                        values, weights, w_list
                    )

        return consensus

    # ------------------------------------------------------------------
    # Deflated Sharpe Ratio (Improvement #5)
    # ------------------------------------------------------------------

    @staticmethod
    def _norm_cdf(z: float) -> float:
        return 0.5 * (1.0 + erf(z / sqrt(2.0)))

    @staticmethod
    def _deflated_sharpe_ratio(
        sr_observed: float,
        n_trials: int,
        n_obs: int,
        skewness: float = 0.0,
        kurtosis: float = 3.0,
    ) -> float:
        """Bailey & Lopez de Prado (2014) Deflated Sharpe Ratio.
        Returns probability that observed SR is statistically significant
        given n_trials independent tests."""
        if n_trials < 2 or n_obs < 2:
            return 0.0

        ln_n = log(max(n_trials, 2))
        sr_benchmark = sqrt(2.0 * ln_n) * (1.0 - EULER_MASCHERONI / (2.0 * ln_n))

        se_sr = sqrt(
            (1.0 - skewness * sr_observed + ((kurtosis - 1.0) / 4.0) * sr_observed**2)
            / max(n_obs - 1, 1)
        )

        if se_sr < 1e-10:
            return 0.0

        z = (sr_observed - sr_benchmark) / se_sr
        return WalkForward._norm_cdf(z)

    def _compute_dsr(self, all_oos_profits: list[float]) -> tuple[float, float, float]:
        sharpes = [r.test_metrics.get("sharpe", 0) for r in self.results]
        avg_sharpe = float(np.mean(sharpes)) if sharpes else 0.0
        n_trials = self.config.get("epochs", 150) * len(self.results)
        avg_trades = (
            int(np.mean([r.test_metrics.get("trades", 0) for r in self.results]))
            if self.results
            else 0
        )
        skewness = 0.0
        kurtosis = 3.0
        if len(all_oos_profits) > 2:
            arr = np.array(all_oos_profits)
            mu = float(np.mean(arr))
            std = float(np.std(arr))
            if std > 1e-10:
                z = (arr - mu) / std
                skewness = float(np.mean(z**3))
                kurtosis = float(np.mean(z**4))
        dsr = self._deflated_sharpe_ratio(
            avg_sharpe,
            n_trials,
            max(avg_trades, 2),
            skewness=skewness,
            kurtosis=kurtosis,
        )
        return dsr, skewness, kurtosis

    @staticmethod
    def _compute_degradation(train: dict[str, Any], test: dict[str, Any]) -> dict[str, float]:
        deg: dict[str, float] = {}
        for key in ("profit_pct", "calmar", "sharpe", "profit_factor"):
            tv = train.get(key, 0)
            testv = test.get(key, 0)
            if abs(tv) > 1e-8:
                deg[key] = round((testv - tv) / abs(tv), 4)
            else:
                deg[key] = 0.0
        return deg

    @staticmethod
    def _compute_wfe(train_pct: float, test_pct: float, train_days: int, test_days: int) -> float:
        """Walk-Forward Efficiency (Pardo): annualized OOS return / annualized IS return."""
        if train_days < 1 or test_days < 1:
            return 0.0
        ann_train = (1.0 + train_pct / 100.0) ** (365.0 / train_days) - 1.0
        ann_test = (1.0 + test_pct / 100.0) ** (365.0 / test_days) - 1.0
        if abs(ann_train) < 1e-8:
            return 0.0
        return ann_test / ann_train

    # ------------------------------------------------------------------
    # Monte Carlo trade shuffle (Phase 2)
    # ------------------------------------------------------------------

    @staticmethod
    def _mc_trade_shuffle(
        profits: list[float],
        starting_balance: float = 1000.0,
        n_simulations: int = 1000,
        seed: int = 42,
    ) -> MCResult:
        """Shuffle OOS trade sequence N times. Return is invariant (sum
        doesn't change), but max DD and return/DD ratio are path-dependent."""
        if len(profits) < 5:
            return MCResult()

        rng = np.random.RandomState(seed)
        arr = np.array(profits)
        total_return_pct = (np.sum(arr) / starting_balance) * 100.0
        max_dds = np.empty(n_simulations)
        return_dds = np.empty(n_simulations)
        max_consec = np.empty(n_simulations, dtype=int)

        for i in range(n_simulations):
            shuffled = rng.permutation(arr)
            equity = starting_balance + np.cumsum(shuffled)

            peak = np.maximum.accumulate(equity)
            dd = (peak - equity) / peak
            max_dd = float(np.max(dd)) * 100.0
            max_dds[i] = max_dd
            return_dds[i] = total_return_pct / max_dd if max_dd > 0.01 else 999.0

            consec = 0
            best_consec = 0
            for p in shuffled:
                if p < 0:
                    consec += 1
                    best_consec = max(best_consec, consec)
                else:
                    consec = 0
            max_consec[i] = best_consec

        return MCResult(
            total_return_pct=float(total_return_pct),
            max_dd_p5=float(np.percentile(max_dds, 5)),
            max_dd_p50=float(np.percentile(max_dds, 50)),
            max_dd_p95=float(np.percentile(max_dds, 95)),
            return_dd_p5=float(np.percentile(return_dds, 5)),
            return_dd_p50=float(np.percentile(return_dds, 50)),
            return_dd_p95=float(np.percentile(return_dds, 95)),
            max_consec_loss_p50=int(np.percentile(max_consec, 50)),
            max_consec_loss_p95=int(np.percentile(max_consec, 95)),
            n_simulations=n_simulations,
        )

    # ------------------------------------------------------------------
    # OOS equity curve concatenation (Phase 2)
    # ------------------------------------------------------------------

    @staticmethod
    def _concat_oos_equity(
        all_results: list[WindowResult],
        starting_balance: float = 1000.0,
    ) -> OOSEquityCurve:
        """Concatenate OOS trade profits chronologically into one equity curve."""
        all_profits: list[float] = []
        for r in all_results:
            all_profits.extend(r.test_trade_profits)

        if not all_profits:
            return OOSEquityCurve()

        arr = np.array(all_profits)
        equity = starting_balance + np.cumsum(arr)
        final_return = (equity[-1] / starting_balance - 1.0) * 100.0

        peak = np.maximum.accumulate(equity)
        dd = (peak - equity) / peak
        max_dd = float(np.max(dd)) * 100.0

        k = WalkForward._k_ratio_equity(equity)

        return OOSEquityCurve(
            total_return_pct=round(final_return, 2),
            max_dd_pct=round(max_dd, 2),
            k_ratio=round(k, 4),
            n_trades=len(all_profits),
        )

    @staticmethod
    def _k_ratio_equity(equity: np.ndarray) -> float:
        """K-ratio: slope / SE(slope) of equity curve."""
        n = len(equity)
        if n < 3:
            return 0.0
        x = np.arange(n, dtype=float)
        slope, intercept = np.polyfit(x, equity, 1)
        if slope <= 0:
            return 0.0
        residuals = equity - (slope * x + intercept)
        ss_res = np.sum(residuals**2)
        se_slope = np.sqrt(ss_res / (n - 2)) / np.sqrt(np.sum((x - x.mean()) ** 2))
        if se_slope < 1e-10:
            return 20.0
        return slope / se_slope

    # ------------------------------------------------------------------
    # Carver discount factor (Phase 2)
    # ------------------------------------------------------------------

    @staticmethod
    def _carver_discount(mc: MCResult) -> float:
        """p5/p50 of return/DD ratio — how fragile is the risk-adjusted edge.
        1.0 = robust, low = edge quality degrades badly in worst-case paths (tip #111)."""
        if abs(mc.return_dd_p50) < 1e-8:
            return 0.0
        return mc.return_dd_p5 / mc.return_dd_p50

    # ------------------------------------------------------------------
    # Regime analysis (Phase 3C)
    # ------------------------------------------------------------------

    @staticmethod
    def _analyze_regimes(all_results: list[WindowResult]) -> RegimeAnalysis:
        """Group OOS windows by market regime and detect regime dependency."""
        buckets: dict[str, list[WindowResult]] = {}
        for r in all_results:
            regime = (r.market_context or {}).get("regime", "")
            if regime:
                buckets.setdefault(regime, []).append(r)

        if not buckets:
            return RegimeAnalysis()

        stats: dict[str, dict[str, Any]] = {}
        for regime, results in sorted(buckets.items()):
            profits = [r.test_metrics.get("profit_pct", 0) for r in results]
            dds = [r.test_metrics.get("max_dd_pct", 0) for r in results]
            n_profitable = sum(1 for p in profits if p > 0)
            stats[regime] = {
                "windows": len(results),
                "avg_profit": round(float(np.mean(profits)), 2),
                "avg_dd": round(float(np.mean(dds)), 2),
                "pct_profitable": round(n_profitable / len(results), 2),
            }

        profitable_regimes = [r for r, s in stats.items() if s["avg_profit"] > 0]
        losing_regimes = [r for r, s in stats.items() if s["avg_profit"] <= 0]
        worst = min(stats, key=lambda r: stats[r]["avg_profit"]) if stats else ""
        dependent = len(profitable_regimes) > 0 and len(losing_regimes) > 0

        return RegimeAnalysis(
            regime_stats=stats,
            worst_regime=worst,
            regime_dependent=dependent,
        )

    # ------------------------------------------------------------------
    # Parameter perturbation (Phase 3A)
    # ------------------------------------------------------------------

    @staticmethod
    def _perturb_params(
        consensus: dict[str, Any],
        search_ranges: dict[str, tuple[float, float]],
        noise_pct: float,
        n: int,
        seed: int = 42,
    ) -> list[dict[str, Any]]:
        """Generate n perturbed copies of consensus params."""
        rng = np.random.RandomState(seed)
        perturbed_list: list[dict[str, Any]] = []
        for _ in range(n):
            variant: dict[str, Any] = {}
            for space, params in consensus.items():
                if not isinstance(params, dict):
                    continue
                variant[space] = {}
                for key, val in params.items():
                    if isinstance(val, int | float):
                        noise = rng.normal(0, noise_pct)
                        new_val = val * (1.0 + noise)
                        if key in search_ranges:
                            lo, hi = search_ranges[key]
                            new_val = max(lo, min(hi, new_val))
                        if isinstance(val, int):
                            variant[space][key] = round(new_val)
                        else:
                            variant[space][key] = round(new_val, 6)
                    else:
                        variant[space][key] = val
            perturbed_list.append(variant)
        return perturbed_list

    def _run_perturbation_test(
        self,
        consensus: dict[str, Any],
        search_ranges: dict[str, tuple[float, float]],
        oos_timerange: str,
        strategy_json: Path | None,
        exchange: Any | None = None,
    ) -> PerturbResult:
        """Backtest 60 perturbed param sets on the full OOS period."""
        variants_5 = self._perturb_params(consensus, search_ranges, 0.05, 30, seed=100)
        variants_10 = self._perturb_params(consensus, search_ranges, 0.10, 30, seed=200)
        all_variants = variants_5 + variants_10

        profits: list[float] = []
        resolver_logger = logging.getLogger("freqtrade.resolvers")
        prev_level = resolver_logger.level
        resolver_logger.setLevel(logging.WARNING)
        try:
            for variant in all_variants:
                self._restore_params(variant, strategy_json)
                result = self._run_backtest(oos_timerange, exchange=exchange)
                profits.append(result.get("profit_pct", 0))
        finally:
            resolver_logger.setLevel(prev_level)

        if not profits:
            return PerturbResult()

        arr = np.array(profits)
        median = float(np.median(arr))
        std = float(np.std(arr))
        sensitivity = std / abs(median) if abs(median) > 1e-8 else 99.0
        n_profitable = sum(1 for p in profits if p > 0)

        return PerturbResult(
            n_perturbations=len(profits),
            profit_p5=round(float(np.percentile(arr, 5)), 2),
            profit_p50=round(median, 2),
            profit_p95=round(float(np.percentile(arr, 95)), 2),
            pct_profitable=round(n_profitable / len(profits), 2),
            sensitivity=round(sensitivity, 4),
        )

    # ------------------------------------------------------------------
    # Multi-seed hyperopt (Phase 3B)
    # ------------------------------------------------------------------

    def _run_multi_seed_check(
        self,
        window: WalkForwardWindow,
        n_seeds: int,
        strategy_json: Path | None,
    ) -> MultiSeedResult:
        """Run N extra hyperopts with different seeds on the same window."""
        seed_params: list[dict[str, Any]] = []
        for i in range(n_seeds):
            self._delete_strategy_json(strategy_json)
            seed = 1000 + i
            ho_result = self._run_hyperopt_window(window, base_seed=seed)
            params = self._save_window_params(-(i + 1), strategy_json)
            seed_params.append(ho_result.get("params", params))

        if not seed_params:
            return MultiSeedResult()

        search_ranges = self._get_search_ranges()
        stability = self._analyze_param_stability(seed_params, search_ranges)
        total = len(stability)
        stable = sum(1 for v in stability.values() if v.get("stable"))
        convergence = stable / total if total else 0.0

        return MultiSeedResult(
            n_seeds=n_seeds,
            convergence_pct=round(convergence, 2),
            seed_params=seed_params,
        )

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _export_consensus_json(self, consensus: dict[str, Any]) -> Path:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = self._wfa_dir / (f"{self.strategy_name}_consensus_{ts}.json")
        export_data = {
            "strategy_name": self.strategy_name,
            "params": consensus,
            "ft_stratparam_v": 1,
            "export_time": datetime.now(UTC).isoformat(),
        }
        with filename.open("w") as f:
            rapidjson.dump(
                export_data,
                f,
                indent=2,
                default=hyperopt_serializer,
                number_mode=HYPER_PARAMS_FILE_FORMAT,
            )
        logger.info(f"Consensus exported: {filename.name}")
        return filename

    def _build_results_data(
        self,
        all_results: list[WindowResult],
        stability: dict[str, dict[str, Any]],
        consensus: dict[str, Any],
        warnings: list[str] | None = None,
        dsr: float | None = None,
        all_oos_profits: list[float] | None = None,
        mc: MCResult | None = None,
        oos_equity: OOSEquityCurve | None = None,
        regime: RegimeAnalysis | None = None,
        perturb: PerturbResult | None = None,
        multi_seed: MultiSeedResult | None = None,
        cpcv: CPCVResult | None = None,
    ) -> dict[str, Any]:
        oos_profits = all_oos_profits or []
        oos_sqn = 0.0
        oos_expectancy = 0.0
        if len(oos_profits) > 1:
            arr = np.array(oos_profits)
            std = float(np.std(arr))
            oos_sqn = sqrt(len(arr)) * float(np.mean(arr)) / max(std, 1e-10)
            oos_expectancy = float(np.mean(arr))

        grade, checks = self._compute_verdict(
            all_results,
            stability,
            dsr,
            len(stability),
            oos_profits,
            mc=mc,
            perturb=perturb,
            multi_seed=multi_seed,
            cpcv=cpcv,
        )

        data: dict[str, Any] = {
            "strategy": self.strategy_name,
            "hyperopt_loss": self.config.get("hyperopt_loss", ""),
            "epochs_per_window": self.config.get("epochs", 150),
            "n_windows": self.n_windows,
            "wf_mode": self.wf_mode,
            "train_ratio": self.train_ratio,
            "embargo_days": self.embargo_days,
            "timestamp": datetime.now(UTC).isoformat(),
            "deflated_sharpe_ratio": dsr,
            "oos_aggregate": {
                "total_trades": len(oos_profits),
                "sqn": round(oos_sqn, 2),
                "expectancy": round(oos_expectancy, 6),
            },
            "verdict": {
                "grade": grade,
                "checks": [(n, ok, d) for n, ok, d in checks],
            },
            "warnings": warnings or [],
            "oos_trade_profits": oos_profits,
            "monte_carlo": {
                "n_simulations": mc.n_simulations,
                "total_return_pct": round(mc.total_return_pct, 2),
                "max_dd_p5": round(mc.max_dd_p5, 2),
                "max_dd_p50": round(mc.max_dd_p50, 2),
                "max_dd_p95": round(mc.max_dd_p95, 2),
                "return_dd_p5": round(mc.return_dd_p5, 4),
                "return_dd_p50": round(mc.return_dd_p50, 4),
                "return_dd_p95": round(mc.return_dd_p95, 4),
                "max_consec_loss_p50": mc.max_consec_loss_p50,
                "max_consec_loss_p95": mc.max_consec_loss_p95,
                "carver_discount": round(self._carver_discount(mc), 4),
            }
            if mc
            else None,
            "oos_equity": {
                "total_return_pct": oos_equity.total_return_pct,
                "max_dd_pct": oos_equity.max_dd_pct,
                "k_ratio": oos_equity.k_ratio,
                "n_trades": oos_equity.n_trades,
            }
            if oos_equity and oos_equity.n_trades > 0
            else None,
            "regime_analysis": {
                "regime_stats": regime.regime_stats,
                "worst_regime": regime.worst_regime,
                "regime_dependent": regime.regime_dependent,
            }
            if regime and regime.regime_stats
            else None,
            "perturbation": {
                "n_perturbations": perturb.n_perturbations,
                "profit_p5": perturb.profit_p5,
                "profit_p50": perturb.profit_p50,
                "profit_p95": perturb.profit_p95,
                "pct_profitable": perturb.pct_profitable,
                "sensitivity": round(perturb.sensitivity, 4),
            }
            if perturb and perturb.n_perturbations > 0
            else None,
            "multi_seed": {
                "n_seeds": multi_seed.n_seeds,
                "convergence_pct": multi_seed.convergence_pct,
            }
            if multi_seed and multi_seed.n_seeds > 0
            else None,
            "cpcv": {
                "n_groups": cpcv.n_groups,
                "n_test_groups": cpcv.n_test_groups,
                "n_combinations": cpcv.n_combinations,
                "n_paths": cpcv.n_paths,
                "avg_return": cpcv.avg_return,
                "sharpe_of_paths": cpcv.sharpe_of_paths,
                "prob_of_loss": cpcv.prob_of_loss,
                "path_returns": cpcv.path_returns,
            }
            if cpcv and cpcv.n_combinations > 0
            else None,
            "windows": [],
            "holdout": None,
            "param_stability": stability,
            "consensus_params": consensus,
        }

        for r in all_results:
            data["windows"].append(
                {
                    "index": r.window.index + 1,
                    "train_range": r.window.train_timerange_str(),
                    "test_range": r.window.test_timerange_str(),
                    "wfe": round(r.wfe, 4),
                    "train_metrics": r.train_metrics,
                    "test_metrics": r.test_metrics,
                    "baseline_metrics": r.baseline_metrics,
                    "market_context": r.market_context,
                    "params": r.params,
                    "degradation": self._compute_degradation(r.train_metrics, r.test_metrics),
                }
            )

        if self.holdout_result:
            data["holdout"] = {
                "test_range": self.holdout_result.window.test_timerange_str(),
                "test_metrics": self.holdout_result.test_metrics,
                "baseline_metrics": self.holdout_result.baseline_metrics,
            }
        return data

    def _export_results_json(self, data: dict[str, Any]) -> Path:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = self._wfa_dir / f"{self.strategy_name}_wfa_results_{ts}.json"
        with filename.open("w") as f:
            rapidjson.dump(
                data,
                f,
                indent=2,
                default=hyperopt_serializer,
                number_mode=HYPER_PARAMS_FILE_FORMAT,
            )
        logger.info(f"Results exported: {filename.name}")
        return filename

    def _export_html_report(self, data: dict[str, Any]) -> Path | None:
        try:
            from freqtrade.optimize.wfa_html_report import generate_wfa_html_report

            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            html_path = self._wfa_dir / f"{self.strategy_name}_wfa_report_{ts}.html"
            generate_wfa_html_report(data, html_path)
            logger.info(f"HTML report: {html_path.name}")
            return html_path
        except Exception as e:
            logger.warning(f"HTML report generation failed: {e}")
            return None

    def _log_wfa_run(
        self,
        all_results: list[WindowResult],
        stability: dict[str, dict[str, Any]],
    ) -> int:
        """Append to persistent WFA log and return run count."""
        log_file = self._wfa_dir / "wfa_log.jsonl"
        prior_count = 0
        if log_file.exists():
            prior_count = sum(1 for line in log_file.read_text().splitlines() if line.strip())

        profitable_windows = sum(1 for r in all_results if r.test_metrics.get("profit_pct", 0) > 0)
        stable_params = sum(1 for s in stability.values() if s.get("stable", False))
        total_params = len(stability)

        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "strategy": self.strategy_name,
            "hyperopt_loss": self.config.get("hyperopt_loss", ""),
            "windows": self.n_windows,
            "windows_completed": len(all_results),
            "profitable_windows": profitable_windows,
            "stable_params": stable_params,
            "total_params": total_params,
            "timerange": self.config.get("timerange", ""),
        }

        with log_file.open("a") as f:
            f.write(
                rapidjson.dumps(
                    entry,
                    default=hyperopt_serializer,
                    number_mode=HYPER_PARAMS_FILE_FORMAT,
                )
                + "\n"
            )

        return prior_count + 1

    # ------------------------------------------------------------------
    # Warning flags
    # ------------------------------------------------------------------

    def _per_window_warnings(
        self,
        r: WindowResult,
        n_params: int,
    ) -> list[str]:
        warnings: list[str] = []
        wi = r.window.index + 1

        if r.test_trade_count < self.min_test_trades:
            warnings.append(
                f"W{wi}: test trades ({r.test_trade_count}) < minimum ({self.min_test_trades})"
            )
        top1 = r.test_metrics.get("top1_pct", 0)
        if top1 > 50:
            warnings.append(f"W{wi}: top-1 trade = {top1:.0f}% of profit — concentrated")
        hhi = r.test_metrics.get("hhi", 0)
        if hhi > 0.15:
            warnings.append(f"W{wi}: HHI {hhi:.3f} — profit depends on few trades")
        test_dd = r.test_metrics.get("max_dd_pct", 0)
        train_dd = r.train_metrics.get("max_dd_pct", 0)
        if train_dd > 0.1 and test_dd / train_dd > 1.5:
            warnings.append(
                f"W{wi}: DD ratio OOS/IS = {test_dd / train_dd:.1f}x > 1.5x — fragile (Davey)"
            )
        sqn = r.test_metrics.get("sqn", 0)
        if sqn > 5.0:
            warnings.append(f"W{wi}: SQN {sqn:.1f} > 5.0 — suspiciously good (Van Tharp)")
        if r.test_metrics.get("expectancy", 0) < 0 and r.test_trade_count > 0:
            warnings.append(f"W{wi}: negative expectancy in OOS")

        if n_params > 0:
            train_trades = r.train_metrics.get("trades", 0)
            ratio = train_trades / n_params
            if ratio < 10:
                warnings.append(
                    f"W{wi}: {train_trades} train trades / "
                    f"{n_params} params = {ratio:.0f}:1 — noise-fitting risk "
                    f"(Chan, need >10:1)"
                )

        return warnings

    def _generate_warning_flags(
        self,
        all_results: list[WindowResult],
        stability: dict[str, dict[str, Any]],
        run_count: int,
        n_params: int = 0,
        mc: MCResult | None = None,
        regime: RegimeAnalysis | None = None,
        perturb: PerturbResult | None = None,
        multi_seed: MultiSeedResult | None = None,
        cpcv: CPCVResult | None = None,
    ) -> list[str]:
        warnings: list[str] = []

        for r in all_results:
            warnings.extend(self._per_window_warnings(r, n_params))

        unstable = [k for k, v in stability.items() if v.get("unstable", False)]
        if unstable:
            warnings.append(
                f"Unstable parameters (std/range > "
                f"{PARAM_STABILITY_UNSTABLE:.0%}): "
                f"{', '.join(unstable)} — consider freezing (tip #81)"
            )

        if not self.config.get("timeframe_detail"):
            warnings.append("No --timeframe-detail: fills overestimated (tip #20)")

        if run_count > 1:
            warnings.append(
                f"WFA run #{run_count} on {self.strategy_name} — "
                f"more tests = higher false positive risk (tip #76, #180)"
            )

        total_oos_trades = sum(r.test_metrics.get("trades", 0) for r in all_results)
        if total_oos_trades < 200:
            warnings.append(
                f"Total OOS trades: {total_oos_trades} < 200 — insufficient sample (Aronson)"
            )

        pf_values = [
            r.test_metrics.get("profit_factor", 0)
            for r in all_results
            if r.test_metrics.get("profit_factor", 0) > 0
        ]
        if pf_values and float(np.mean(pf_values)) < 1.2:
            warnings.append(
                f"Avg OOS profit factor: {float(np.mean(pf_values)):.2f} "
                f"< 1.2 — marginal edge (Davey)"
            )

        profitable = sum(1 for r in all_results if r.test_metrics.get("profit_pct", 0) > 0)
        n = len(all_results)
        if n > 0 and profitable / n < 0.5:
            warnings.append(
                f"Only {profitable}/{n} OOS windows profitable "
                f"({profitable / n:.0%}) — below 50% threshold (Pardo)"
            )

        self._extend_analysis_warnings(
            warnings,
            mc=mc,
            regime=regime,
            perturb=perturb,
            multi_seed=multi_seed,
            cpcv=cpcv,
        )
        return warnings

    @staticmethod
    def _extend_analysis_warnings(
        warnings: list[str],
        mc: MCResult | None = None,
        regime: RegimeAnalysis | None = None,
        perturb: PerturbResult | None = None,
        multi_seed: MultiSeedResult | None = None,
        cpcv: CPCVResult | None = None,
    ) -> None:
        if mc and mc.n_simulations > 0:
            warnings.extend(WalkForward._mc_warnings(mc))
        if regime:
            warnings.extend(WalkForward._regime_warnings(regime))
        if perturb and perturb.n_perturbations > 0:
            warnings.extend(WalkForward._perturb_warnings(perturb))
        if multi_seed and multi_seed.n_seeds > 0:
            warnings.extend(WalkForward._multi_seed_warnings(multi_seed))
        if cpcv and cpcv.n_combinations > 0:
            warnings.extend(WalkForward._cpcv_warnings(cpcv))

    @staticmethod
    def _mc_warnings(mc: MCResult) -> list[str]:
        warnings: list[str] = []
        if mc.return_dd_p5 < 0.5:
            warnings.append(
                f"MC return/DD p5={mc.return_dd_p5:.2f} — "
                f"risk-adjusted edge fragile under reordering (Carver #111)"
            )
        if mc.max_dd_p50 > 0.1 and mc.max_dd_p95 / mc.max_dd_p50 > 2.0:
            warnings.append(
                f"MC tail DD risk: p95={mc.max_dd_p95:.1f}% vs "
                f"p50={mc.max_dd_p50:.1f}% "
                f"({mc.max_dd_p95 / mc.max_dd_p50:.1f}x) — fat tail (tip #117)"
            )
        if mc.max_consec_loss_p95 >= 10:
            warnings.append(
                f"MC p95 consecutive losses: {mc.max_consec_loss_p95} — "
                f"prepare for extended losing streaks"
            )
        return warnings

    @staticmethod
    def _regime_warnings(regime: RegimeAnalysis) -> list[str]:
        warnings: list[str] = []
        if regime.regime_dependent:
            profitable = [r for r, s in regime.regime_stats.items() if s["avg_profit"] > 0]
            warnings.append(
                f"Strategy profitable only in {', '.join(profitable)} — "
                f"fails in {regime.worst_regime} (tip #69)"
            )
        if len(regime.regime_stats) == 1:
            only = next(iter(regime.regime_stats))
            warnings.append(f"Only {only} windows tested — unknown behavior in other regimes")
        return warnings

    @staticmethod
    def _perturb_warnings(perturb: PerturbResult) -> list[str]:
        warnings: list[str] = []
        if perturb.pct_profitable < 0.70:
            warnings.append(
                f"Only {perturb.pct_profitable:.0%} of param perturbations "
                f"remain profitable — narrow peak (tip #81)"
            )
        if perturb.sensitivity > 2.0:
            warnings.append(
                f"Param sensitivity {perturb.sensitivity:.1f} — "
                f"results depend heavily on exact values"
            )
        return warnings

    @staticmethod
    def _multi_seed_warnings(ms: MultiSeedResult) -> list[str]:
        warnings: list[str] = []
        if ms.convergence_pct < 0.60:
            warnings.append(
                f"Multi-seed: only {ms.convergence_pct:.0%} params converge — "
                f"noisy optimization surface (tip #76)"
            )
        return warnings

    @staticmethod
    def _cpcv_warnings(cpcv: CPCVResult) -> list[str]:
        warnings: list[str] = []
        if cpcv.prob_of_loss > 0.40:
            warnings.append(
                f"CPCV: {cpcv.prob_of_loss:.0%} of paths have negative return — "
                f"fragile edge (Lopez de Prado)"
            )
        if cpcv.sharpe_of_paths < 0.5:
            warnings.append(
                f"CPCV: Sharpe of paths {cpcv.sharpe_of_paths:.2f} < 0.5 — "
                f"weak risk-adjusted edge across combinations"
            )
        return warnings

    # ------------------------------------------------------------------
    # Verdict
    # ------------------------------------------------------------------

    VERDICT_LABELS = {
        "A": "Deploy — all criteria met",
        "B": "Proceed to dry-run",
        "C": "Investigate — mixed signals",
        "D": "Rework — most criteria failed",
        "F": "Reject — critical failure",
    }

    def _compute_verdict(
        self,
        all_results: list[WindowResult],
        stability: dict[str, dict[str, Any]],
        dsr: float | None,
        n_params: int,
        all_oos_profits: list[float],
        mc: MCResult | None = None,
        perturb: PerturbResult | None = None,
        multi_seed: MultiSeedResult | None = None,
        cpcv: CPCVResult | None = None,
    ) -> tuple[str, list[tuple[str, bool, str]]]:
        checks: list[tuple[str, bool, str]] = []

        n = len(all_results)

        # 1. Windows profitables (Pardo: >= 60%)
        profitable = sum(1 for r in all_results if r.test_metrics.get("profit_pct", 0) > 0)
        pct = profitable / n if n else 0
        checks.append(
            (
                "profitable_windows",
                pct >= 0.6,
                f"{profitable}/{n} windows profitable ({pct:.0%})",
            )
        )

        # 2. WFE (Pardo: > 50%)
        wfe_values = [r.wfe for r in all_results if r.wfe != 0]
        wfe_med = float(np.median(wfe_values)) if wfe_values else 0.0
        checks.append(
            (
                "wfe",
                wfe_med > 0.5,
                f"WFE median {wfe_med:.0%}",
            )
        )

        # 3. DSR (Lopez de Prado: > 0.95)
        checks.append(
            (
                "dsr",
                dsr is not None and dsr > 0.95,
                f"DSR {dsr:.3f}" if dsr is not None else "DSR N/A",
            )
        )

        # 4. Total OOS trades (Aronson: >= 200)
        total_trades = sum(r.test_metrics.get("trades", 0) for r in all_results)
        checks.append(
            (
                "oos_trades",
                total_trades >= 200,
                f"OOS trades: {total_trades}",
            )
        )

        # 5. SQN (Van Tharp: 0 < sqn < 5.0)
        if len(all_oos_profits) > 1:
            std = float(np.std(all_oos_profits))
            sqn = sqrt(len(all_oos_profits)) * float(np.mean(all_oos_profits)) / max(std, 1e-10)
            sqn_ok = 0 < sqn < 5.0
        else:
            sqn = 0.0
            sqn_ok = False
        checks.append(("sqn", sqn_ok, f"SQN {sqn:.1f}"))

        # 6. Param stability (Clenow: >= 70% stable)
        total_p = len(stability)
        stable_p = sum(1 for v in stability.values() if v.get("stable"))
        stable_pct = stable_p / total_p if total_p else 0
        checks.append(
            (
                "param_stability",
                stable_pct >= 0.7,
                f"{stable_p}/{total_p} params stable ({stable_pct:.0%})",
            )
        )

        # 7. DD ratio OOS/IS (Davey: < 1.5x)
        max_dd_ratio = 0.0
        for r in all_results:
            t_dd = r.train_metrics.get("max_dd_pct", 0)
            o_dd = r.test_metrics.get("max_dd_pct", 0)
            if t_dd > 0.1:
                max_dd_ratio = max(max_dd_ratio, o_dd / t_dd)
        checks.append(
            (
                "dd_ratio",
                max_dd_ratio < 1.5 or max_dd_ratio == 0,
                f"Max DD ratio {max_dd_ratio:.1f}x",
            )
        )

        # 8. Avg profit factor (Davey: >= 1.2)
        pfs = [
            r.test_metrics.get("profit_factor", 0)
            for r in all_results
            if r.test_metrics.get("profit_factor", 0) > 0
        ]
        avg_pf = float(np.mean(pfs)) if pfs else 0.0
        checks.append(
            (
                "profit_factor",
                avg_pf >= 1.2,
                f"Avg PF {avg_pf:.2f}",
            )
        )

        # 9. Trades/params ratio (Chan: >= 10:1)
        if n_params > 0:
            min_ratio = min(
                (r.train_metrics.get("trades", 0) / n_params for r in all_results),
                default=0,
            )
        else:
            min_ratio = float("inf")
        checks.append(
            (
                "trades_params",
                min_ratio >= 10,
                f"Min trades/params {min_ratio:.0f}:1" if min_ratio < float("inf") else "N/A",
            )
        )

        # 10. MC return/DD p5 > 0.5 (Carver #111: edge survives reordering)
        if mc and mc.n_simulations > 0:
            checks.append(
                (
                    "mc_robust",
                    mc.return_dd_p5 > 0.5,
                    f"MC return/DD p5={mc.return_dd_p5:.2f}",
                )
            )

        # 11. Param perturbation (tip #81: >= 70% profitable under noise)
        if perturb and perturb.n_perturbations > 0:
            checks.append(
                (
                    "param_robust",
                    perturb.pct_profitable >= 0.70,
                    f"Perturbation: {perturb.pct_profitable:.0%} profitable",
                )
            )

        # 12. Multi-seed convergence (tip #76: >= 60% params stable)
        if multi_seed and multi_seed.n_seeds > 0:
            checks.append(
                (
                    "seed_convergence",
                    multi_seed.convergence_pct >= 0.60,
                    f"Seed convergence: {multi_seed.convergence_pct:.0%}",
                )
            )

        # 13. CPCV probability of loss (Lopez de Prado: < 30%)
        if cpcv and cpcv.n_combinations > 0:
            checks.append(
                (
                    "cpcv_prob_loss",
                    cpcv.prob_of_loss < 0.30,
                    f"CPCV prob of loss: {cpcv.prob_of_loss:.0%}",
                )
            )

        grade = self._grade_from_checks(checks)
        return grade, checks

    @staticmethod
    def _grade_from_checks(checks: list[tuple[str, bool, str]]) -> str:
        passed = sum(1 for _, ok, _ in checks if ok)
        total = len(checks)
        critical_names = {"profitable_windows", "dsr", "sqn", "mc_robust"}
        has_critical_fail = any(not ok and name in critical_names for name, ok, _ in checks)

        if has_critical_fail and passed < total * 0.3:
            return "F"
        if has_critical_fail:
            return "D"
        if passed == total:
            return "A"
        if passed >= total * 0.75:
            return "B"
        if passed >= total * 0.5:
            return "C"
        return "D"

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def _log_holdout_and_oos(
        self,
        all_results: list[WindowResult],
        oos_profits: list[float],
    ) -> None:
        if self.holdout_result:
            ho = self.holdout_result
            logger.info(
                f"  HO  "
                f"{ho.window.test_start:%m-%d} -> {ho.window.test_end:%m-%d}  "
                f"{self._fmt_metrics(ho.test_metrics)}"
            )
            if ho.baseline_metrics:
                bl_profit = ho.baseline_metrics.get("profit_pct", 0)
                logger.info(f"      Baseline: {bl_profit:+.1f}%")

        if oos_profits:
            total_profit = sum(r.test_metrics.get("profit_pct", 0) for r in all_results)
            total_trades = len(oos_profits)
            arr = np.array(oos_profits)
            std = float(np.std(arr))
            sqn = (
                sqrt(total_trades) * float(np.mean(arr)) / max(std, 1e-10)
                if total_trades > 1
                else 0.0
            )
            expectancy = float(np.mean(arr))
            logger.info("")
            logger.info(
                f"  OOS aggregate: {total_profit:+.1f}% | "
                f"{total_trades} trades | "
                f"SQN {sqn:.1f} | "
                f"Expectancy {expectancy:+.4f}"
            )

    @staticmethod
    def _fmt_metrics(m: dict[str, Any]) -> str:
        parts = [
            f"Profit {m.get('profit_pct', 0):+.1f}%",
            f"Calmar {m.get('calmar', 0):.2f}",
            f"DD {m.get('max_dd_pct', 0):.1f}%",
            f"{m.get('trades', 0)} trades",
        ]
        return "  ".join(parts)

    def _format_report(
        self,
        all_results: list[WindowResult],
        stability: dict[str, dict[str, Any]],
        consensus: dict[str, Any],
        warnings: list[str],
        dsr: float | None = None,
        all_oos_profits: list[float] | None = None,
        n_params: int = 0,
        skewness: float = 0.0,
        kurtosis: float = 3.0,
        mc: MCResult | None = None,
        oos_equity: OOSEquityCurve | None = None,
        regime: RegimeAnalysis | None = None,
        perturb: PerturbResult | None = None,
        multi_seed: MultiSeedResult | None = None,
    ) -> None:
        sep = "=" * 70
        n = len(all_results)
        mode = self.wf_mode
        epochs = self.config.get("epochs", 150)
        loss = self.config.get("hyperopt_loss", "N/A")
        oos_profits = all_oos_profits or []

        logger.info("")
        logger.info(sep)
        logger.info(f"  WFA Results — {self.strategy_name}")
        logger.info(f"  {loss} | {n} windows ({mode}) | {epochs} epochs/win")
        logger.info(sep)

        for r in all_results:
            w = r.window
            ctx = r.market_context or {}
            win_regime = ctx.get("regime", "")
            regime_str = f"  [{win_regime}]" if win_regime else ""
            wfe_str = f"  WFE {r.wfe:.0%}" if r.wfe != 0 else ""
            logger.info(
                f"  W{w.index + 1}  "
                f"{w.test_start:%m-%d} -> {w.test_end:%m-%d}  "
                f"{self._fmt_metrics(r.test_metrics)}"
                f"{wfe_str}{regime_str}"
            )

        self._log_holdout_and_oos(all_results, oos_profits)

        self._log_mc_and_equity(mc, oos_equity)

        self._log_phase3(regime, perturb, multi_seed)

        logger.info("")
        logger.info("  Consensus (weighted by Calmar):")
        for space, params in consensus.items():
            if isinstance(params, dict) and params:
                parts = [f"{k}={v}" for k, v in params.items()]
                logger.info(f"    {space}: {', '.join(parts)}")

        marginal = [
            k for k, v in stability.items() if not v.get("stable") and not v.get("unstable")
        ]
        unstable = [k for k, v in stability.items() if v.get("unstable")]
        if unstable:
            logger.info(f"  Unstable params: {', '.join(unstable)}")
        if marginal:
            logger.info(f"  Marginal params: {', '.join(marginal)}")

        if dsr is not None:
            n_trials = epochs * n
            dsr_label = "significant" if dsr > 0.95 else "weak" if dsr > 0.5 else "not significant"
            skew_str = f", skew={skewness:.1f}, kurt={kurtosis:.1f}" if abs(skewness) > 0.01 else ""
            logger.info(f"  DSR: {dsr:.3f} ({dsr_label}{skew_str}) — {n_trials} trials")

        # Verdict
        grade, checks = self._compute_verdict(
            all_results,
            stability,
            dsr,
            n_params,
            oos_profits,
            mc=mc,
            perturb=perturb,
            multi_seed=multi_seed,
        )
        logger.info("")
        logger.info(f"  VERDICT: {grade} — {self.VERDICT_LABELS.get(grade, '')}")
        for _name, ok, desc in checks:
            mark = "✓" if ok else "✗"
            logger.info(f"    {mark} {desc}")

        if warnings:
            logger.info("")
            for w in warnings:
                logger.info(f"  ! {w}")

        logger.info(sep)

    @staticmethod
    def _log_mc_and_equity(
        mc: MCResult | None,
        oos_equity: OOSEquityCurve | None,
    ) -> None:
        if mc and mc.n_simulations > 0:
            discount = WalkForward._carver_discount(mc)
            logger.info("")
            logger.info(
                f"  MC Shuffle ({mc.n_simulations} sims):  "
                f"Return {mc.total_return_pct:+.1f}% | "
                f"Max DD p5/p50/p95: "
                f"{mc.max_dd_p5:.1f}% / {mc.max_dd_p50:.1f}% / {mc.max_dd_p95:.1f}%"
            )
            logger.info(
                f"                          "
                f"Return/DD p5/p50/p95: "
                f"{mc.return_dd_p5:.2f} / {mc.return_dd_p50:.2f} / {mc.return_dd_p95:.2f} | "
                f"Consec loss p95: {mc.max_consec_loss_p95}"
            )
            logger.info(f"  Carver discount: {discount:.2f} (return/DD p5/p50)")

        if oos_equity and oos_equity.n_trades > 0:
            logger.info(
                f"  OOS Equity: {oos_equity.total_return_pct:+.1f}% | "
                f"Max DD {oos_equity.max_dd_pct:.1f}% | "
                f"K-ratio {oos_equity.k_ratio:.2f} | "
                f"{oos_equity.n_trades} trades"
            )

    @staticmethod
    def _log_phase3(
        regime: RegimeAnalysis | None,
        perturb: PerturbResult | None,
        multi_seed: MultiSeedResult | None,
    ) -> None:
        if regime and regime.regime_stats:
            parts = [
                f"{r} {s['windows']}W {s['avg_profit']:+.1f}%"
                for r, s in regime.regime_stats.items()
            ]
            logger.info(f"  Regime breakdown: {' | '.join(parts)}")

        if perturb and perturb.n_perturbations > 0:
            logger.info(
                f"  Perturbation ({perturb.n_perturbations} variants): "
                f"p5/p50/p95: {perturb.profit_p5:+.1f}% / "
                f"{perturb.profit_p50:+.1f}% / {perturb.profit_p95:+.1f}% | "
                f"{perturb.pct_profitable:.0%} profitable | "
                f"sensitivity {perturb.sensitivity:.2f}"
            )

        if multi_seed and multi_seed.n_seeds > 0:
            logger.info(
                f"  Multi-seed ({multi_seed.n_seeds} seeds): "
                f"{multi_seed.convergence_pct:.0%} convergence"
            )

    @staticmethod
    def _log_cpcv(cpcv: CPCVResult | None) -> None:
        if cpcv and cpcv.n_combinations > 0:
            logger.info(
                f"  CPCV ({cpcv.n_combinations} combos, "
                f"N={cpcv.n_groups} K={cpcv.n_test_groups}): "
                f"Avg return {cpcv.avg_return:+.1f}% | "
                f"Sharpe {cpcv.sharpe_of_paths:.2f} | "
                f"P(loss) {cpcv.prob_of_loss:.0%}"
            )
            if cpcv.path_returns:
                arr = np.array(cpcv.path_returns)
                logger.info(
                    f"           p5/p50/p95: "
                    f"{float(np.percentile(arr, 5)):+.1f}% / "
                    f"{float(np.percentile(arr, 50)):+.1f}% / "
                    f"{float(np.percentile(arr, 95)):+.1f}%"
                )

    # ------------------------------------------------------------------
    # Phase 3 orchestration
    # ------------------------------------------------------------------

    def _run_phase3(
        self,
        consensus: dict[str, Any],
        search_ranges: dict[str, tuple[float, float]],
        strategy_json: Path | None,
        original_json_bytes: bytes | None,
        exchange: Any | None = None,
    ) -> tuple[RegimeAnalysis | None, PerturbResult | None, MultiSeedResult | None]:
        regime = self._analyze_regimes(self.results)

        perturb: PerturbResult | None = None
        if consensus and search_ranges:
            first_test = min(r.window.test_start for r in self.results)
            last_test = max(r.window.test_end for r in self.results)
            oos_timerange = f"{first_test.strftime('%Y%m%d')}-{last_test.strftime('%Y%m%d')}"
            logger.info("Running parameter perturbation test (60 variants)...")
            perturb = self._run_perturbation_test(
                consensus,
                search_ranges,
                oos_timerange,
                strategy_json,
                exchange=exchange,
            )
            self._restore_original_json(strategy_json, original_json_bytes)

        multi_seed: MultiSeedResult | None = None
        n_multi_seeds = self.config.get("wf_multi_seed", 0)
        if n_multi_seeds > 0 and len(self.results) > 0:
            last_window = self.results[-1].window
            logger.info(f"Running multi-seed check ({n_multi_seeds} seeds) on last window...")
            multi_seed = self._run_multi_seed_check(
                last_window,
                n_multi_seeds,
                strategy_json,
            )
            self._restore_original_json(strategy_json, original_json_bytes)

        return regime, perturb, multi_seed

    # ------------------------------------------------------------------
    # Holdout
    # ------------------------------------------------------------------

    def _run_holdout(
        self,
        consensus: dict[str, Any],
        default_params: dict[str, Any],
        strategy_json: Path | None,
        original_json_bytes: bytes | None,
        exchange: Any | None = None,
    ) -> None:
        _, full_end = self._parse_timerange()
        holdout_window = self._compute_holdout_window(full_end)
        if not holdout_window or not consensus:
            return

        logger.info("Running holdout backtest...")
        resolver_logger = logging.getLogger("freqtrade.resolvers")
        prev_level = resolver_logger.level
        resolver_logger.setLevel(logging.WARNING)
        try:
            self._restore_params(consensus, strategy_json)
            ho_test = self._run_backtest(holdout_window.test_timerange_str(), exchange=exchange)

            ho_baseline: dict[str, Any] = {}
            if default_params:
                self._restore_params(default_params, strategy_json)
                ho_baseline = self._run_backtest(
                    holdout_window.test_timerange_str(), exchange=exchange
                )
        finally:
            resolver_logger.setLevel(prev_level)

        self.holdout_result = WindowResult(
            window=holdout_window,
            test_metrics=ho_test,
            baseline_metrics=ho_baseline,
            test_trade_count=ho_test.get("trades", 0),
        )

        self._restore_original_json(strategy_json, original_json_bytes)

    @staticmethod
    def _restore_original_json(
        strategy_json: Path | None, original_json_bytes: bytes | None
    ) -> None:
        if strategy_json and original_json_bytes is not None:
            strategy_json.write_bytes(original_json_bytes)
        elif strategy_json and strategy_json.exists():
            strategy_json.unlink()

    # ------------------------------------------------------------------
    # Main orchestration
    # ------------------------------------------------------------------

    def _start_cpcv(self) -> None:
        self._validate_cpcv()
        cfg_for_exchange = deepcopy(self.config)
        cfg_for_exchange["dry_run"] = True
        exchange = ExchangeResolver.load_exchange(
            cfg_for_exchange,
            load_leverage_tiers=True,
        )
        strategy_json = self._get_strategy_json_path()
        original_json_bytes: bytes | None = None
        if strategy_json and strategy_json.exists():
            original_json_bytes = strategy_json.read_bytes()

        consensus = self._load_latest_consensus()
        if not consensus:
            raise OperationalException("No consensus params found for CPCV.")

        n_combos = comb(self.cpcv_groups, self.cpcv_test_groups)
        logger.info(
            f"CPCV mode: N={self.cpcv_groups}, K={self.cpcv_test_groups}, "
            f"C({self.cpcv_groups},{self.cpcv_test_groups})={n_combos} combinations"
        )
        cpcv_result = self._run_cpcv(
            consensus,
            strategy_json,
            original_json_bytes,
            exchange=exchange,
        )
        self._restore_original_json(strategy_json, original_json_bytes)

        warnings = self._cpcv_warnings(cpcv_result)
        data = self._build_results_data(
            [],
            {},
            consensus,
            warnings=warnings,
            cpcv=cpcv_result,
        )
        self._export_results_json(data)
        self._export_html_report(data)
        self._log_cpcv(cpcv_result)

        grade, checks = self._compute_verdict(
            [],
            {},
            None,
            0,
            [],
            cpcv=cpcv_result,
        )
        logger.info("")
        logger.info(f"  VERDICT: {grade} — {self.VERDICT_LABELS.get(grade, '')}")
        for _name, ok, desc in checks:
            mark = "✓" if ok else "✗"
            logger.info(f"    {mark} {desc}")
        if warnings:
            for w in warnings:
                logger.info(f"  ! {w}")

    def start(self) -> None:
        if self.wf_mode == "cpcv":
            return self._start_cpcv()

        from freqtrade.optimize.wfa_output import WFADashboard

        windows = self._compute_windows()
        self._validate(windows)
        self._print_plan(windows)

        cfg_for_exchange = deepcopy(self.config)
        cfg_for_exchange["dry_run"] = True
        exchange = ExchangeResolver.load_exchange(cfg_for_exchange, load_leverage_tiers=True)
        logger.info("Exchange loaded once — reused for all backtests.")

        strategy_json = self._get_strategy_json_path()
        base_seed = self.config.get("hyperopt_random_state")

        original_json_bytes: bytes | None = None
        if strategy_json and strategy_json.exists():
            original_json_bytes = strategy_json.read_bytes()
            backup_path = self._wfa_dir / f"{strategy_json.name}.backup"
            backup_path.write_bytes(original_json_bytes)
            logger.info(f"Backed up live strategy JSON to {backup_path}")

        default_params: dict[str, Any] = {}
        if original_json_bytes is not None:
            raw = rapidjson.loads(
                original_json_bytes.decode(), number_mode=HYPER_PARAMS_FILE_FORMAT
            )
            default_params = raw.get("params", {})

        all_params: list[dict[str, Any]] = []
        dashboard = WFADashboard(
            windows=windows,
            strategy=self.strategy_name,
            epochs_per_window=self.config.get("epochs", 150),
            stake_currency=self.config.get("stake_currency", "USDT"),
        )

        try:
            with dashboard:
                for window in windows:
                    dashboard.set_window(window)

                    self._delete_strategy_json(strategy_json)

                    dashboard.set_phase("hyperopt")
                    ho_result = self._run_hyperopt_window(window, base_seed, dashboard)
                    train_metrics = ho_result.get("metrics", {})

                    params = self._save_window_params(window.index, strategy_json)
                    all_params.append(ho_result.get("params", {}))

                    dashboard.set_phase("backtest_optimized")
                    test_metrics = self._run_backtest(
                        window.test_timerange_str(), exchange=exchange
                    )

                    baseline_metrics: dict[str, Any] = {}
                    if default_params:
                        self._restore_params(default_params, strategy_json)
                        dashboard.set_phase("backtest_baseline")
                        baseline_metrics = self._run_backtest(
                            window.test_timerange_str(), exchange=exchange
                        )

                    market_ctx = self._compute_market_context(window.test_start, window.test_end)

                    trade_profits = test_metrics.pop("trade_profits", [])
                    train_days = (window.train_end - window.train_start).days
                    test_days = (window.test_end - window.test_start).days
                    wfe = self._compute_wfe(
                        train_metrics.get("profit_pct", 0),
                        test_metrics.get("profit_pct", 0),
                        train_days,
                        test_days,
                    )

                    # SQN + expectancy per window
                    if len(trade_profits) > 1:
                        tp_arr = np.array(trade_profits)
                        tp_std = float(np.std(tp_arr))
                        tp_mean = float(np.mean(tp_arr))
                        test_metrics["sqn"] = (
                            sqrt(len(tp_arr)) * tp_mean / tp_std if tp_std > 1e-10 else 0.0
                        )
                        test_metrics["expectancy"] = tp_mean
                        wins = tp_arr[tp_arr > 0]
                        losses = tp_arr[tp_arr < 0]
                        test_metrics["avg_win"] = float(wins.mean()) if len(wins) else 0.0
                        test_metrics["avg_loss"] = float(losses.mean()) if len(losses) else 0.0

                    result = WindowResult(
                        window=window,
                        train_metrics=train_metrics,
                        test_metrics=test_metrics,
                        baseline_metrics=baseline_metrics,
                        market_context=market_ctx,
                        params=params,
                        test_trade_count=test_metrics.get("trades", 0),
                        test_trade_profits=trade_profits,
                        wfe=wfe,
                    )
                    self.results.append(result)
                    dashboard.complete_window(result)

                    gc.collect()

        except KeyboardInterrupt:
            logger.info(f"Interrupted. Partial results for {len(self.results)} window(s).")

        finally:
            self._restore_original_json(strategy_json, original_json_bytes)
            if strategy_json:
                logger.debug(f"Restored original strategy JSON: {strategy_json}")

        if not self.results:
            logger.warning("No windows completed. Nothing to report.")
            return

        # Stability analysis
        search_ranges = self._get_search_ranges()
        stability = self._analyze_param_stability(all_params[: len(self.results)], search_ranges)

        # Weighted consensus: weight by test-period Calmar
        calmar_weights = [max(r.test_metrics.get("calmar", 0), 0.01) for r in self.results]
        consensus = self._compute_consensus_params(
            all_params[: len(self.results)], weights=calmar_weights
        )

        # Aggregate OOS trade profits
        all_oos_profits: list[float] = []
        for r in self.results:
            all_oos_profits.extend(r.test_trade_profits)

        dsr, skewness, kurtosis = self._compute_dsr(all_oos_profits)

        # Monte Carlo trade shuffle
        starting_balance = self.config.get("dry_run_wallet", 1000)
        mc = self._mc_trade_shuffle(
            all_oos_profits,
            starting_balance=starting_balance,
        )

        # OOS equity curve concatenation
        oos_equity = self._concat_oos_equity(self.results, starting_balance=starting_balance)

        regime, perturb, multi_seed = self._run_phase3(
            consensus,
            search_ranges,
            strategy_json,
            original_json_bytes,
            exchange,
        )

        self._run_holdout(
            consensus, default_params, strategy_json, original_json_bytes, exchange=exchange
        )

        # Export consensus JSON (to wfa_dir only, never overwrites live JSON)
        self._export_consensus_json(consensus)

        n_params = len(search_ranges)

        # Log and get run count
        run_count = self._log_wfa_run(self.results, stability)

        # Warnings (compute before export so they're included in data)
        warnings = self._generate_warning_flags(
            self.results,
            stability,
            run_count,
            n_params=n_params,
            mc=mc,
            regime=regime,
            perturb=perturb,
            multi_seed=multi_seed,
        )

        # Build data dict (shared between JSON export and HTML report)
        data = self._build_results_data(
            self.results,
            stability,
            consensus,
            warnings=warnings,
            dsr=dsr,
            all_oos_profits=all_oos_profits,
            mc=mc,
            oos_equity=oos_equity,
            regime=regime,
            perturb=perturb,
            multi_seed=multi_seed,
        )

        # Export JSON + HTML
        self._export_results_json(data)
        self._export_html_report(data)

        # Report
        self._format_report(
            self.results,
            stability,
            consensus,
            warnings,
            dsr=dsr,
            all_oos_profits=all_oos_profits,
            n_params=n_params,
            skewness=skewness,
            kurtosis=kurtosis,
            mc=mc,
            oos_equity=oos_equity,
            regime=regime,
            perturb=perturb,
            multi_seed=multi_seed,
        )
