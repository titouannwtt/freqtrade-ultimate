from __future__ import annotations

import gc
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import erf, log, sqrt
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

    def _export_results_json(
        self,
        all_results: list[WindowResult],
        stability: dict[str, dict[str, Any]],
        consensus: dict[str, Any],
        dsr: float | None = None,
        all_oos_profits: list[float] | None = None,
    ) -> Path:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = self._wfa_dir / (f"{self.strategy_name}_wfa_results_{ts}.json")

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
            "verdict": {"grade": grade, "checks": {n: ok for n, ok, _ in checks}},
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
                "test_range": (self.holdout_result.window.test_timerange_str()),
                "test_metrics": self.holdout_result.test_metrics,
                "baseline_metrics": self.holdout_result.baseline_metrics,
            }

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

        # Grade
        passed = sum(1 for _, ok, _ in checks if ok)
        total = len(checks)
        critical_names = {"profitable_windows", "dsr", "sqn"}
        has_critical_fail = any(not ok and name in critical_names for name, ok, _ in checks)

        if has_critical_fail and passed < total * 0.3:
            grade = "F"
        elif has_critical_fail:
            grade = "D"
        elif passed == total:
            grade = "A"
        elif passed >= total * 0.75:
            grade = "B"
        elif passed >= total * 0.5:
            grade = "C"
        else:
            grade = "D"

        return grade, checks

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
            regime = ctx.get("regime", "")
            regime_str = f"  [{regime}]" if regime else ""
            wfe_str = f"  WFE {r.wfe:.0%}" if r.wfe != 0 else ""
            logger.info(
                f"  W{w.index + 1}  "
                f"{w.test_start:%m-%d} -> {w.test_end:%m-%d}  "
                f"{self._fmt_metrics(r.test_metrics)}"
                f"{wfe_str}{regime_str}"
            )

        self._log_holdout_and_oos(all_results, oos_profits)

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

    def start(self) -> None:
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

        # Deflated Sharpe Ratio (with empirical skew/kurtosis)
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

        self._run_holdout(
            consensus, default_params, strategy_json, original_json_bytes, exchange=exchange
        )

        # Export consensus JSON (to wfa_dir only, never overwrites live JSON)
        self._export_consensus_json(consensus)

        n_params = len(search_ranges)

        # Export full results
        self._export_results_json(
            self.results,
            stability,
            consensus,
            dsr=dsr,
            all_oos_profits=all_oos_profits,
        )

        # Log and get run count
        run_count = self._log_wfa_run(self.results, stability)

        # Warnings
        warnings = self._generate_warning_flags(
            self.results,
            stability,
            run_count,
            n_params=n_params,
        )

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
        )
