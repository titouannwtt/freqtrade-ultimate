# pragma pylint: disable=too-many-instance-attributes, pointless-string-statement

"""
This module contains the hyperopt logic
"""

import gc
import logging
import random
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Any

import rapidjson
from joblib import Parallel, cpu_count
from optuna.trial import FrozenTrial, Trial, TrialState

from freqtrade.constants import FTHYPT_FILEVERSION, LAST_BT_RESULT_FN, Config
from freqtrade.enums import HyperoptState
from freqtrade.misc import file_dump_json, plural
from freqtrade.optimize.hyperopt.hyperopt_optimizer import INITIAL_POINTS, HyperOptimizer
from freqtrade.optimize.hyperopt.hyperopt_output import HyperoptOutput
from freqtrade.optimize.hyperopt_tools import (
    HyperoptStateContainer,
    HyperoptTools,
    hyperopt_serializer,
)
from freqtrade.util import get_progress_tracker


logger = logging.getLogger(__name__)


class Hyperopt:
    """
    Hyperopt class, this class contains all the logic to run a hyperopt simulation

    To start a hyperopt run:
    hyperopt = Hyperopt(config)
    hyperopt.start()
    """

    def __init__(self, config: Config) -> None:
        self._hyper_out: HyperoptOutput = HyperoptOutput(streaming=True)
        self._epoch_callback: Any | None = None

        self.config = config

        self.analyze_per_epoch = self.config.get("analyze_per_epoch", False)
        HyperoptStateContainer.set_state(HyperoptState.STARTUP)

        time_now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        strategy = str(self.config["strategy"])
        self.results_file: Path = (
            self.config["user_data_dir"]
            / "hyperopt_results"
            / f"strategy_{strategy}_{time_now}.fthypt"
        )
        self.data_pickle_file = (
            self.config["user_data_dir"] / "hyperopt_results" / "hyperopt_tickerdata.pkl"
        )
        self.total_epochs = config.get("epochs", 0)

        self.current_best_loss = 100

        self.clean_hyperopt()

        self.num_epochs_saved = 0
        self.current_best_epoch: dict[str, Any] | None = None

        if HyperoptTools.has_space(self.config, "sell"):
            # Make sure use_exit_signal is enabled
            self.config["use_exit_signal"] = True

        self.print_all = self.config.get("print_all", False)
        self.hyperopt_table_header = 0
        self.print_json = self.config.get("print_json", False)

        self.hyperopter = HyperOptimizer(self.config, self.data_pickle_file)
        self.count_skipped_epochs = 0

    @staticmethod
    def get_lock_filename(config: Config) -> str:
        return str(config["user_data_dir"] / "hyperopt.lock")

    _HYPEROPT_DEFAULTS: dict[str, Any] = {
        "epochs": 0,
        "spaces": [],
        "hyperopt_min_trades": 1,
        "timerange": "",
        "timeframe": "",
        "hyperopt_jobs": -1,
        "hyperopt_random_state": None,
        "analyze_per_epoch": False,
        "print_all": False,
        "print_json": False,
        "hyperopt_sampler": None,
        "stake_currency": "USDT",
    }

    def _detect_user_params(self) -> list[str]:
        user = []
        for key, default in self._HYPEROPT_DEFAULTS.items():
            val = self.config.get(key)
            if val is not None and val != default:
                user.append(key)
        always_user = ["strategy", "hyperopt_loss"]
        for k in always_user:
            if self.config.get(k):
                user.append(k)
        return user

    def clean_hyperopt(self) -> None:
        """
        Remove hyperopt pickle files to restart hyperopt.
        """
        for f in [self.data_pickle_file, self.results_file]:
            p = Path(f)
            if p.is_file():
                logger.info(f"Removing `{p}`.")
                p.unlink()

    def _save_run_metadata(self) -> None:
        from freqtrade.configuration.config_secrets import sanitize_config

        strategy_name = str(self.config.get("strategy", ""))
        meta: dict[str, Any] = {
            "strategy": strategy_name,
            "hyperopt_loss": self.config.get("hyperopt_loss", ""),
            "sampler": self.config.get("hyperopt_sampler"),
            "timerange": self.config.get("timerange", ""),
            "timeframe": self.config.get("timeframe", ""),
            "epochs_total": self.total_epochs,
            "spaces": self.config.get("spaces", []),
            "hyperopt_min_trades": self.config.get("hyperopt_min_trades", 1),
            "stake_currency": self.config.get("stake_currency", "USDT"),
            "exchange": self.config.get("exchange", {}).get("name", ""),
            "pairlist": self.config.get("exchange", {}).get("pair_whitelist", []),
            "run_start_time": datetime.now().isoformat(),
            "run_start_ts": int(datetime.now().timestamp()),
            "config": sanitize_config(self.config.get("original_config", self.config)),
            "command": self._reconstruct_command(),
        }
        fn = HyperoptTools.get_strategy_filename(self.config, strategy_name)
        if fn and fn.is_file():
            meta["strategy_source"] = fn.read_text(encoding="utf-8")
            params_file = fn.with_suffix(".json")
            if params_file.is_file():
                meta["strategy_params_snapshot"] = params_file.read_text(encoding="utf-8")
        file_dump_json(self.results_file.with_suffix(".meta.json"), meta)

    def _save_run_end_metadata(self) -> None:
        meta_path = self.results_file.with_suffix(".meta.json")
        if not meta_path.exists():
            return
        with meta_path.open() as f:
            meta = rapidjson.load(f)
        meta["run_end_time"] = datetime.now().isoformat()
        meta["run_end_ts"] = int(datetime.now().timestamp())
        meta["epochs_completed"] = self.num_epochs_saved
        meta["best_loss"] = round(self.current_best_loss, 6)
        if self.current_best_epoch:
            rm = self.current_best_epoch.get("results_metrics", {})
            meta["best_profit_pct"] = round(rm.get("profit_total", 0) * 100, 2)
            meta["best_trades"] = rm.get("total_trades", 0)
            meta["best_sharpe"] = round(rm.get("sharpe", 0.0), 4)
        file_dump_json(meta_path, meta)

    _CMD_FLAGS: list[tuple[str, str, Any]] = [
        ("strategy", "--strategy", None),
        ("hyperopt_loss", "--hyperopt-loss", None),
        ("epochs", "--epochs", None),
        ("timerange", "--timerange", None),
        ("timeframe", "--timeframe", None),
        ("hyperopt_min_trades", "--min-trades", 1),
        ("hyperopt_random_state", "--random-state", None),
        ("hyperopt_sampler", "--hyperopt-sampler", None),
    ]

    def _reconstruct_command(self) -> str:
        parts = ["freqtrade hyperopt"]
        cfg = self.config
        for key, flag, skip_val in self._CMD_FLAGS:
            val = cfg.get(key)
            if val is not None and val != skip_val:
                parts.append(f"{flag} {val}")
        spaces = cfg.get("spaces")
        if spaces:
            parts.append(f"--spaces {' '.join(spaces)}")
        jobs = cfg.get("hyperopt_jobs", -1)
        if jobs != -1:
            parts.append(f"-j {jobs}")
        configs = cfg.get("config", [])
        if isinstance(configs, list):
            for c in configs:
                parts.append(f"-c {c}")
        elif configs:
            parts.append(f"-c {configs}")
        return " ".join(parts)

    def _save_result(self, epoch: dict) -> None:
        """
        Save hyperopt results to file
        Store one line per epoch.
        While not a valid json object - this allows appending easily.
        :param epoch: result dictionary for this epoch.
        """
        epoch[FTHYPT_FILEVERSION] = 2
        with self.results_file.open("a") as f:
            rapidjson.dump(
                epoch,
                f,
                default=hyperopt_serializer,
                number_mode=rapidjson.NM_NATIVE | rapidjson.NM_NAN,
            )
            f.write("\n")

        self.num_epochs_saved += 1
        logger.debug(
            f"{self.num_epochs_saved} {plural(self.num_epochs_saved, 'epoch')} "
            f"saved to '{self.results_file}'."
        )
        # Store hyperopt filename
        latest_filename = Path.joinpath(self.results_file.parent, LAST_BT_RESULT_FN)
        file_dump_json(latest_filename, {"latest_hyperopt": str(self.results_file.name)}, log=False)

    def print_results(self, results: dict[str, Any]) -> None:
        """
        Log results if it is better than any previous evaluation
        TODO: this should be moved to HyperoptTools too
        """
        is_best = results["is_best"]

        if self.print_all or is_best:
            self._hyper_out.add_data(
                self.config,
                [results],
                self.total_epochs,
                self.print_all,
            )

    def run_optimizer_parallel(self, parallel: Parallel, asked: list[list]) -> list[dict[str, Any]]:
        """Start optimizer in a parallel way"""

        return parallel(self.hyperopter.generate_optimizer_wrapped(v) for v in asked)

    def _set_random_state(self, random_state: int | None) -> int:
        return random_state or random.randint(1, 2**16 - 1)  # noqa: S311

    def get_optuna_asked_points(self, n_points: int, dimensions: dict) -> list[Any]:
        asked: list[list[Any]] = []
        for i in range(n_points):
            asked.append(self.opt.ask(dimensions))
        return asked

    def duplicate_optuna_asked_points(self, trial: Trial, asked_trials: list[FrozenTrial]) -> bool:
        asked_trials_no_dups: list[FrozenTrial] = []
        trials_to_consider = trial.study.get_trials(deepcopy=False, states=[TrialState.COMPLETE])
        # Check whether we already evaluated the sampled `params`.
        for t in reversed(trials_to_consider):
            if trial.params == t.params:
                return True
        # Check whether same`params` in one batch (asked_trials). Autosampler is doing this.
        for t in asked_trials:
            if t.params not in asked_trials_no_dups:
                asked_trials_no_dups.append(t)
        if len(asked_trials_no_dups) != len(asked_trials):
            return True
        return False

    def get_asked_points(self, n_points: int, dimensions: dict) -> tuple[list[Any], list[bool]]:
        """
        Enforce points returned from `self.opt.ask` have not been already evaluated

        Steps:
        1. Try to get points using `self.opt.ask` first
        2. Discard the points that have already been evaluated
        3. Retry using `self.opt.ask` up to `n_points` times
        """
        asked_non_tried: list[FrozenTrial] = []
        optuna_asked_trials = self.get_optuna_asked_points(n_points=n_points, dimensions=dimensions)
        asked_non_tried += [
            x
            for x in optuna_asked_trials
            if not self.duplicate_optuna_asked_points(x, optuna_asked_trials)
        ]
        i = 0
        while i < 2 * n_points and len(asked_non_tried) < n_points:
            asked_new = self.get_optuna_asked_points(n_points=1, dimensions=dimensions)[0]
            if not self.duplicate_optuna_asked_points(asked_new, asked_non_tried):
                asked_non_tried.append(asked_new)
            i += 1
        if len(asked_non_tried) < n_points:
            if self.count_skipped_epochs == 0:
                logger.warning("Duplicate params detected. Maybe your search space is too small?")
            self.count_skipped_epochs += n_points - len(asked_non_tried)

        return asked_non_tried, [False for _ in range(len(asked_non_tried))]

    def evaluate_result(self, val: dict[str, Any], current: int, is_random: bool):
        """
        Evaluate results returned from generate_optimizer
        """
        val["current_epoch"] = current
        val["is_initial_point"] = current <= INITIAL_POINTS

        logger.debug("Optimizer epoch evaluated: %s", val)

        is_best = HyperoptTools.is_best_loss(val, self.current_best_loss)
        # This value is assigned here and not in the optimization method
        # to keep proper order in the list of results. That's because
        # evaluations can take different time. Here they are aligned in the
        # order they will be shown to the user.
        val["is_best"] = is_best
        val["is_random"] = is_random
        self.print_results(val)

        if is_best:
            self.current_best_loss = val["loss"]
            self.current_best_epoch = val

        self._save_result(val)

        if self._epoch_callback:
            self._epoch_callback(val)

    def start(self) -> None:
        self._start_time = datetime.now()
        self.random_state = self._set_random_state(self.config.get("hyperopt_random_state"))
        logger.info(f"Using optimizer random state: {self.random_state}")
        self.hyperopt_table_header = -1
        self.hyperopter.prepare_hyperopt()
        self._save_run_metadata()

        cpus = cpu_count()
        logger.info(f"Found {cpus} CPU cores. Let's make them scream!")
        config_jobs = self.config.get("hyperopt_jobs", -1)
        logger.info(f"Number of parallel jobs set as: {config_jobs}")

        self.opt = self.hyperopter.get_optimizer(self.random_state)
        try:
            with Parallel(n_jobs=config_jobs) as parallel:
                jobs = parallel._effective_n_jobs()
                logger.info(f"Effective number of parallel workers used: {jobs}")

                # Define progressbar
                with get_progress_tracker(
                    cust_callables=[self._hyper_out],
                    disable=self.config.get("wfa_silent", False),
                ) as pbar:
                    task = pbar.add_task("Epochs", total=self.total_epochs)

                    start = 0

                    if self.analyze_per_epoch:
                        # First analysis not in parallel mode when using --analyze-per-epoch.
                        # This allows dataprovider to load it's informative cache.
                        asked, is_random = self.get_asked_points(
                            n_points=1, dimensions=self.hyperopter.o_dimensions
                        )
                        f_val0 = self.hyperopter.generate_optimizer(asked[0].params)
                        self.opt.tell(asked[0], [f_val0["loss"]])
                        self.evaluate_result(f_val0, 1, is_random[0])
                        pbar.update(task, advance=1)
                        start += 1

                    evals = ceil((self.total_epochs - start) / jobs)
                    for i in range(evals):
                        # Correct the number of epochs to be processed for the last
                        # iteration (should not exceed self.total_epochs in total)
                        n_rest = (i + 1) * jobs - (self.total_epochs - start)
                        current_jobs = jobs - n_rest if n_rest > 0 else jobs

                        asked, is_random = self.get_asked_points(
                            n_points=current_jobs, dimensions=self.hyperopter.o_dimensions
                        )

                        f_val = self.run_optimizer_parallel(
                            parallel,
                            [asked1.params for asked1 in asked],
                        )

                        f_val_loss = [v["loss"] for v in f_val]
                        for o_ask, v in zip(asked, f_val_loss, strict=False):
                            self.opt.tell(o_ask, v)

                        for j, val in enumerate(f_val):
                            # Use human-friendly indexes here (starting from 1)
                            current = i * jobs + j + 1 + start

                            self.evaluate_result(val, current, is_random[j])
                            pbar.update(task, advance=1)
                        self.hyperopter.handle_mp_logging()
                        gc.collect()

                        if (
                            self.hyperopter.es_epochs > 0
                            and self.hyperopter.es_terminator.should_terminate(self.opt)
                        ):
                            logger.info(f"Early stopping after {(i + 1) * jobs} epochs")
                            break

        except KeyboardInterrupt:
            print("User interrupted..")

        if self.count_skipped_epochs > 0:
            logger.info(
                f"{self.count_skipped_epochs} {plural(self.count_skipped_epochs, 'epoch')} "
                f"skipped due to duplicate parameters."
            )

        logger.info(
            f"{self.num_epochs_saved} {plural(self.num_epochs_saved, 'epoch')} "
            f"saved to '{self.results_file}'."
        )
        self._save_run_end_metadata()

        if self.current_best_epoch:
            HyperoptTools.try_export_params(
                self.config,
                self.hyperopter.get_strategy_name(),
                self.current_best_epoch,
            )

            if not self.config.get("wfa_silent"):
                HyperoptTools.show_epoch_details(
                    self.current_best_epoch, self.total_epochs, self.print_json
                )
                self._print_post_run_summary(self.current_best_epoch)
                self._export_html_report()
        elif self.num_epochs_saved > 0:
            print(
                f"No good result found for given optimization function in {self.num_epochs_saved} "
                f"{plural(self.num_epochs_saved, 'epoch')}."
            )
        else:
            # This is printed when Ctrl+C is pressed quickly, before first epochs have
            # a chance to be evaluated.
            print("No epochs evaluated yet, no best result.")

    @staticmethod
    def _threshold_label(slug: str, value: float) -> str:
        from freqtrade.optimize.wfa_glossary import METRIC_GLOSSARY

        entry = METRIC_GLOSSARY.get(slug)
        if not entry or not entry.get("thresholds"):
            return ""
        label = ""
        for threshold_val, threshold_label, _color in entry["thresholds"]:
            if value >= threshold_val:
                label = threshold_label
        return label

    def _print_post_run_summary(self, best_epoch: dict[str, Any]) -> None:
        rm = best_epoch.get("results_metrics", {})
        if not rm:
            return

        metrics = [
            ("Calmar", "calmar", rm.get("calmar", 0)),
            ("SQN", "sqn", rm.get("sqn", 0)),
            ("Sharpe", "sharpe", rm.get("sharpe", 0)),
            ("Sortino", "sortino", rm.get("sortino", 0)),
            ("Profit Factor", "pf", rm.get("profit_factor", 0)),
            (
                "Max DD",
                "dd",
                abs(rm.get("max_drawdown_account", 0)) * 100,
            ),
            ("Win Rate", "win_rate", rm.get("winrate", 0)),
            ("Expectancy", "expectancy", rm.get("expectancy", 0)),
        ]

        logger.info("")
        logger.info("=" * 60)
        logger.info("  Key Metrics (best epoch):")
        logger.info("-" * 60)
        for name, slug, val in metrics:
            label = self._threshold_label(slug, val)
            tag = f" [{label}]" if label else ""
            if slug == "dd":
                logger.info(f"    {name:16s} {val:8.1f}%{tag}")
            elif slug == "win_rate":
                logger.info(f"    {name:16s} {val:8.1%}{tag}")
            else:
                logger.info(f"    {name:16s} {val:8.2f}{tag}")
        logger.info("=" * 60)

        self._log_hyperopt_next_steps(rm)

    def _log_hyperopt_next_steps(self, rm: dict[str, Any]) -> None:
        from freqtrade.optimize.wfa_glossary import (
            HYPEROPT_NEXT_STEPS,
            LOSS_GLOSSARY,
        )

        profit = rm.get("profit_total", 0)
        dd = abs(rm.get("max_drawdown_account", 0))
        trades = rm.get("total_trades", 0)

        logger.info("")
        logger.info("  NEXT STEPS:")
        if profit <= 0:
            guide = HYPEROPT_NEXT_STEPS["unprofitable"]
        elif trades < 30:
            guide = HYPEROPT_NEXT_STEPS["low_trades"]
        elif dd > 0.30:
            guide = HYPEROPT_NEXT_STEPS["high_dd"]
        else:
            guide = HYPEROPT_NEXT_STEPS["profitable"]

        strategy = self.config.get("strategy") or "MyStrategy"
        guide = guide.replace("{strategy}", strategy)
        guide = guide.replace("{tr}", self.config.get("timerange") or "")
        logger.info(f"  {guide}")

        loss_name = self.config.get("hyperopt_loss", "")
        loss_info = LOSS_GLOSSARY.get(loss_name, {})
        if loss_info:
            logger.info("")
            logger.info(f"  Loss: {loss_name} — {loss_info.get('one_liner', '')}")
        logger.info("")

    def _export_html_report(self) -> None:
        try:
            self._do_export_html_report()
        except Exception as e:
            logger.warning(f"HTML report generation failed: {e}")

    @staticmethod
    def _compute_skew_kurtosis(
        values: list[float],
    ) -> tuple[float, float]:
        n = len(values)
        if n < 3:
            return 0.0, 0.0
        mean = sum(values) / n
        m2 = sum((x - mean) ** 2 for x in values) / n
        m3 = sum((x - mean) ** 3 for x in values) / n
        m4 = sum((x - mean) ** 4 for x in values) / n
        if m2 < 1e-15:
            return 0.0, 0.0
        skew = m3 / (m2**1.5)
        kurt = m4 / (m2**2) - 3.0
        return round(skew, 4), round(kurt, 4)

    @staticmethod
    def _compute_pearson(xs: list[float], ys: list[float]) -> float:
        n = len(xs)
        if n < 3:
            return 0.0
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
        sx = sum((x - mx) ** 2 for x in xs) ** 0.5
        sy = sum((y - my) ** 2 for y in ys) ** 0.5
        if sx < 1e-15 or sy < 1e-15:
            return 0.0
        return round(cov / (sx * sy), 4)

    @staticmethod
    def _compute_histogram_bins(values: list[float], n_bins: int = 10) -> list[dict]:
        if not values:
            return []
        lo, hi = min(values), max(values)
        n_bins = min(n_bins, max(len(set(values)), 1))
        bw = (hi - lo) / n_bins if n_bins > 0 and hi > lo else 1.0
        bins = []
        for i in range(n_bins):
            edge_lo = lo + i * bw
            edge_hi = lo + (i + 1) * bw
            if i == n_bins - 1:
                count = sum(1 for v in values if edge_lo <= v <= edge_hi)
            else:
                count = sum(1 for v in values if edge_lo <= v < edge_hi)
            bins.append(
                {
                    "lo": round(edge_lo, 4),
                    "hi": round(edge_hi, 4),
                    "count": count,
                }
            )
        return bins

    @staticmethod
    def _compute_trade_metrics(
        best_trades: list[dict],
    ) -> dict:
        result: dict = {}
        profit_ratios = [t.get("profit_ratio", 0.0) for t in best_trades if isinstance(t, dict)]
        if len(profit_ratios) >= 10:
            skew, kurt = Hyperopt._compute_skew_kurtosis(profit_ratios)
            result["distribution_analysis"] = {
                "skewness": skew,
                "excess_kurtosis": kurt,
                "n_trades": len(profit_ratios),
                "skew_alert": skew < -1.0,
                "kurtosis_alert": kurt > 3.0,
            }
        profits_sorted = sorted(
            [t.get("profit_abs", 0.0) for t in best_trades if isinstance(t, dict)],
            reverse=True,
        )
        if profits_sorted:
            total_p = sum(profits_sorted)
            if total_p > 0:
                w1 = total_p - profits_sorted[0]
                w2 = total_p - sum(profits_sorted[:2]) if len(profits_sorted) >= 2 else total_p
                result["sans_top_trade"] = {
                    "total_profit": round(total_p, 4),
                    "without_top1": round(w1, 4),
                    "without_top1_pct": round(w1 / total_p * 100, 1),
                    "without_top2": round(w2, 4),
                    "without_top2_pct": round(w2 / total_p * 100, 1),
                    "fragile": w2 <= 0,
                }
        pair_data: dict[str, dict] = {}
        for t in best_trades:
            if not isinstance(t, dict):
                continue
            pair = t.get("pair", "unknown")
            if pair not in pair_data:
                pair_data[pair] = {"profit": 0.0, "count": 0, "wins": 0}
            pair_data[pair]["profit"] += t.get("profit_abs", 0.0)
            pair_data[pair]["count"] += 1
            if t.get("profit_ratio", 0.0) > 0:
                pair_data[pair]["wins"] += 1
        result["pair_profit_distribution"] = sorted(
            [
                {
                    "pair": p,
                    "profit_abs": round(d["profit"], 4),
                    "trade_count": d["count"],
                    "win_rate": round(d["wins"] / d["count"], 3) if d["count"] else 0,
                    "avg_profit": round(d["profit"] / d["count"], 4) if d["count"] else 0,
                }
                for p, d in pair_data.items()
            ],
            key=lambda x: abs(x["profit_abs"]),
            reverse=True,
        )
        return result

    @staticmethod
    def _compute_param_analytics(
        param_values: dict[str, list],
        top_10: list[dict],
        rm: dict,
    ) -> dict:
        import statistics

        result: dict = {}
        num_params = {
            k: v
            for k, v in param_values.items()
            if len(v) >= 3 and all(isinstance(x, (int, float)) for x in v)
        }
        pnames = sorted(num_params.keys())
        corr: list[dict] = []
        for i, pa in enumerate(pnames):
            for pb in pnames[i + 1 :]:
                r = Hyperopt._compute_pearson(
                    [float(x) for x in num_params[pa]],
                    [float(x) for x in num_params[pb]],
                )
                corr.append({"param_a": pa, "param_b": pb, "correlation": r})
        result["param_correlation"] = corr
        pc: dict = {"params": pnames, "lines": []}
        for ep in top_10:
            pd = ep.get("params_dict", {})
            normalized = {}
            for pn in pnames:
                vals = num_params.get(pn, [])
                v = pd.get(pn)
                if isinstance(v, (int, float)) and vals and max(vals) > min(vals):
                    normalized[pn] = round(
                        (float(v) - min(vals)) / (max(vals) - min(vals)),
                        4,
                    )
                else:
                    normalized[pn] = 0.5
            pc["lines"].append(
                {
                    "values": normalized,
                    "loss": ep.get("loss", 0),
                }
            )
        result["parallel_coords"] = pc
        top10_profits = [e.get("results_metrics", {}).get("profit_total", 0.0) for e in top_10]
        if len(top10_profits) >= 2:
            med = statistics.median(top10_profits)
            bp = rm.get("profit_total", 0.0)
            if med > 0:
                gap = round(bp / med, 2)
            else:
                gap = 0.0
            result["best_vs_median_gap"] = {
                "best_profit": round(bp * 100, 2),
                "median_profit": round(med * 100, 2),
                "gap_ratio": gap,
                "outlier": med > 0 and gap > 2.0,
            }

        def _band(key: str, mult: float = 1.0):
            vals = [e.get("results_metrics", {}).get(key, 0) * mult for e in top_10]
            if not vals:
                return None
            return {
                "min": round(min(vals), 2),
                "median": round(statistics.median(vals), 2),
                "max": round(max(vals), 2),
            }

        result["dispersion_bands"] = {
            "profit": _band("profit_total", 100),
            "drawdown": _band("max_drawdown_account", 100),
            "sharpe": _band("sharpe"),
        }
        return result

    @staticmethod
    def _compute_param_deep_dive(
        dimensions,
        best_params_dict: dict,
        top10_values: dict[str, list],
        all_values: dict[str, list],
        all_losses: list[float],
    ) -> dict[str, dict]:
        import statistics

        from freqtrade.optimize.space.optunaspaces import (
            ft_CategoricalDistribution,
            ft_FloatDistribution,
            ft_IntDistribution,
        )

        result: dict[str, dict] = {}
        for dim in dimensions:
            name = dim.name
            info: dict[str, Any] = {"name": name}

            if isinstance(dim, ft_CategoricalDistribution):
                info["type"] = "Categorical"
                info["categories"] = list(dim.choices)
                cats = all_values.get(name, [])
                freq: dict[str, int] = {}
                for v in cats:
                    freq[str(v)] = freq.get(str(v), 0) + 1
                info["category_counts"] = freq
            elif isinstance(dim, ft_IntDistribution):
                info["type"] = "Int"
                info["range_low"] = dim.low
                info["range_high"] = dim.high
            elif isinstance(dim, ft_FloatDistribution):
                info["type"] = "Decimal" if getattr(dim, "step", None) else "Float"
                info["range_low"] = dim.low
                info["range_high"] = dim.high
            else:
                info["type"] = "Unknown"
                info["range_low"] = getattr(dim, "low", None)
                info["range_high"] = getattr(dim, "high", None)

            info["best_value"] = best_params_dict.get(name)

            t10 = top10_values.get(name, [])
            t10_nums = [v for v in t10 if isinstance(v, (int, float))]
            if len(t10_nums) >= 2:
                info["top10_min"] = min(t10_nums)
                info["top10_max"] = max(t10_nums)
                info["top10_median"] = round(statistics.median(t10_nums), 4)
                info["top10_std"] = round(statistics.stdev(t10_nums), 4)

            rng_lo = info.get("range_low")
            rng_hi = info.get("range_high")
            info.update(Hyperopt._param_tendency(t10_nums, rng_lo, rng_hi))

            all_vals = all_values.get(name, [])
            all_nums = [v for v in all_vals if isinstance(v, (int, float))]
            if all_nums:
                info["histogram"] = Hyperopt._compute_histogram_bins(all_nums, 8)

            if all_nums and len(all_nums) == len(all_losses):
                corr = abs(
                    Hyperopt._compute_pearson(
                        [float(x) for x in all_nums],
                        list(all_losses),
                    )
                )
                info["sensitivity"] = round(corr, 4)
                if corr > 0.5:
                    info["sensitivity_label"] = "high"
                elif corr > 0.25:
                    info["sensitivity_label"] = "medium"
                else:
                    info["sensitivity_label"] = "low"

            result[name] = info
        return result

    @staticmethod
    def _param_tendency(t10_nums: list, rng_lo, rng_hi) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if len(t10_nums) >= 3 and rng_lo is not None and rng_hi is not None:
            full_range = rng_hi - rng_lo
            if full_range > 0:
                t10_span = max(t10_nums) - min(t10_nums)
                ratio = t10_span / full_range
                result["tendency"] = "converging" if ratio < 0.10 else "spread"
                med = sum(t10_nums) / len(t10_nums)
                near_lo = (med - rng_lo) / full_range < 0.05
                near_hi = (rng_hi - med) / full_range < 0.05
                result["boundary_cluster"] = result.get("tendency") == "converging" and (
                    near_lo or near_hi
                )
        return result

    @staticmethod
    def _compute_monte_carlo(
        best_trades: list[dict],
        n_sims: int = 1000,
    ) -> dict | None:
        profits = [t.get("profit_ratio", 0.0) for t in best_trades if isinstance(t, dict)]
        if len(profits) < 10:
            return None
        import random as _rng

        state = _rng.getstate()
        _rng.seed(42)
        finals = []
        for _ in range(n_sims):
            shuffled = profits[:]
            _rng.shuffle(shuffled)
            cum = 1.0
            for p in shuffled:
                cum *= 1 + p
            finals.append((cum - 1) * 100)
        _rng.setstate(state)
        finals.sort()

        def _pct(p: float) -> float:
            idx = int(len(finals) * p / 100)
            return round(finals[min(idx, len(finals) - 1)], 2)

        return {
            "p5": _pct(5),
            "p25": _pct(25),
            "p50": _pct(50),
            "p75": _pct(75),
            "p95": _pct(95),
            "mean": round(sum(finals) / len(finals), 2),
            "n_simulations": n_sims,
            "n_trades": len(profits),
            "prob_positive": round(
                sum(1 for f in finals if f > 0) / len(finals) * 100,
                1,
            ),
        }

    @staticmethod
    def _compute_sensitivity_grid(
        top_epochs: list[dict],
        param_values: dict[str, list],
    ) -> list[dict]:
        num_params = {
            k: v
            for k, v in param_values.items()
            if len(v) >= 3 and all(isinstance(x, (int, float)) for x in v)
        }
        pnames = sorted(num_params.keys())
        grids = []
        for i, pa in enumerate(pnames):
            for pb in pnames[i + 1 :]:
                cells: dict[tuple, list] = {}
                n_bins = 5
                a_vals = [float(x) for x in num_params[pa]]
                b_vals = [float(x) for x in num_params[pb]]
                a_lo, a_hi = min(a_vals), max(a_vals)
                b_lo, b_hi = min(b_vals), max(b_vals)
                a_bw = (a_hi - a_lo) / n_bins if a_hi > a_lo else 1
                b_bw = (b_hi - b_lo) / n_bins if b_hi > b_lo else 1
                for ep in top_epochs:
                    pd = ep.get("params_dict", {})
                    va, vb = pd.get(pa), pd.get(pb)
                    if not isinstance(va, (int, float)):
                        continue
                    if not isinstance(vb, (int, float)):
                        continue
                    ai = min(
                        int((float(va) - a_lo) / a_bw),
                        n_bins - 1,
                    )
                    bi = min(
                        int((float(vb) - b_lo) / b_bw),
                        n_bins - 1,
                    )
                    loss = ep.get("loss", 0)
                    cells.setdefault((ai, bi), []).append(loss)
                grid = []
                for ai in range(n_bins):
                    row = []
                    for bi in range(n_bins):
                        vs = cells.get((ai, bi), [])
                        row.append(round(sum(vs) / len(vs), 4) if vs else None)
                    grid.append(row)
                grids.append(
                    {
                        "param_a": pa,
                        "param_b": pb,
                        "grid": grid,
                        "a_range": [round(a_lo, 4), round(a_hi, 4)],
                        "b_range": [round(b_lo, 4), round(b_hi, 4)],
                        "n_bins": n_bins,
                    }
                )
        return grids[:6]

    @staticmethod
    def _compute_regime_analysis(
        best_trades: list[dict],
    ) -> dict | None:
        trades = [t for t in best_trades if isinstance(t, dict)]
        if len(trades) < 10:
            return None
        dated = []
        for t in trades:
            ts = t.get("open_timestamp")
            if ts:
                dated.append((ts, t))
        if len(dated) < 6:
            return None
        dated.sort(key=lambda x: x[0])
        mid = len(dated) // 2
        first = [d[1] for d in dated[:mid]]
        second = [d[1] for d in dated[mid:]]

        def _stats(tl: list) -> dict:
            pr = [t.get("profit_ratio", 0) for t in tl]
            pa = [t.get("profit_abs", 0) for t in tl]
            wins = sum(1 for p in pr if p > 0)
            return {
                "trades": len(tl),
                "profit_pct": round(sum(pr) * 100, 2),
                "profit_abs": round(sum(pa), 2),
                "win_rate": round(wins / len(tl) * 100, 1) if tl else 0,
                "avg_profit": round(sum(pr) / len(pr) * 100, 2) if pr else 0,
            }

        return {
            "first_half": _stats(first),
            "second_half": _stats(second),
            "first_label": "First half",
            "second_label": "Second half",
            "consistent": abs(_stats(first)["profit_pct"] - _stats(second)["profit_pct"])
            < max(
                abs(_stats(first)["profit_pct"]),
                abs(_stats(second)["profit_pct"]),
                1,
            )
            * 0.5,
        }

    @staticmethod
    def _compute_return_vs_dd(
        top_epochs: list[dict],
    ) -> list[dict]:
        points = []
        for ep in top_epochs:
            rm = ep.get("results_metrics", {})
            profit = rm.get("profit_total", 0)
            dd = rm.get("max_drawdown_account", 0)
            trades = rm.get("total_trades", 0)
            if trades > 0:
                points.append(
                    {
                        "profit_pct": round(profit * 100, 2),
                        "dd_pct": round(dd * 100, 2),
                        "trades": trades,
                        "loss": ep.get("loss", 0),
                    }
                )
        return points

    @staticmethod
    def _compute_dof_analysis(n_trades: int, n_params: int) -> dict:
        ratio = n_trades / n_params if n_params > 0 else 0
        if ratio >= 30:
            level = "green"
            label = "Excellent"
        elif ratio >= 15:
            level = "green"
            label = "Good"
        elif ratio >= 10:
            level = "yellow"
            label = "Marginal"
        elif ratio >= 5:
            level = "orange"
            label = "Low"
        else:
            level = "red"
            label = "Critical"
        return {
            "n_trades": n_trades,
            "n_params": n_params,
            "ratio": round(ratio, 1),
            "level": level,
            "label": label,
        }

    @staticmethod
    def _compute_overfit_warnings(
        dsr: dict | None,
        param_deep: dict,
        n_params: int,
        n_trades: int,
        sans_top: dict | None,
        bvm_gap: dict | None,
        dist_analysis: dict | None,
    ) -> list[dict]:
        warnings: list[dict] = []

        if dsr and not dsr.get("genuine"):
            warnings.append(
                {
                    "severity": "high",
                    "warning_type": "dsr",
                    "title_en": "DSR: Sharpe likely overfitted",
                    "title_fr": "DSR : Sharpe probablement surajusté",
                    "detail_en": (
                        "Observed Sharpe is below the expected maximum "
                        "from pure noise given the number of trials."
                    ),
                    "detail_fr": (
                        "Le Sharpe observé est inférieur au maximum "
                        "attendu du bruit pur vu le nombre d'essais."
                    ),
                    "actions_en": [
                        "Reduce total epochs to lower E[max SR]",
                        "Increase min-trades to require more statistical evidence",
                        "Switch to CalmarHyperOptLoss (penalizes drawdown, harder to overfit)",
                        "Use walk-forward validation to confirm out-of-sample performance",
                    ],
                    "actions_fr": [
                        "Réduire le nombre d'epochs pour baisser E[max SR]",
                        "Augmenter min-trades pour exiger plus de preuves statistiques",
                        "Passer à CalmarHyperOptLoss (pénalise le drawdown, plus dur à surajuster)",
                        "Utiliser la validation walk-forward pour "
                        "confirmer la performance hors-échantillon",
                    ],
                    "values": {
                        "Sharpe": dsr.get("observed_sharpe"),
                        "E[max SR]": dsr.get("expected_max_sharpe"),
                        "N trials": dsr.get("n_trials"),
                    },
                }
            )

        conv = sum(1 for p in param_deep.values() if p.get("tendency") == "converging")
        if n_params >= 3 and conv / max(n_params, 1) > 0.5:
            warnings.append(
                {
                    "severity": "medium",
                    "warning_type": "clustering",
                    "title_en": "Excessive parameter clustering",
                    "title_fr": "Clustering excessif des paramètres",
                    "detail_en": (
                        "Most parameters converge to a narrow range "
                        "— possible curve-fitting to training data."
                    ),
                    "detail_fr": (
                        "La plupart des paramètres convergent vers "
                        "une plage étroite — possible surapprentissage."
                    ),
                    "actions_en": [
                        "Widen parameter search ranges",
                        "Reduce epochs to avoid over-exploration",
                        "Check if converging params are truly independent",
                    ],
                    "actions_fr": [
                        "Élargir les plages de recherche",
                        "Réduire le nombre d'epochs pour éviter la sur-exploration",
                        "Vérifier si les paramètres convergents sont vraiment indépendants",
                    ],
                    "values": {
                        "converging": f"{conv}/{n_params}",
                    },
                }
            )

        if n_params > 0 and n_trades > 0:
            dof = n_trades / n_params
            if dof < 10:
                sev = "high" if dof < 5 else "medium"
                warnings.append(
                    {
                        "severity": sev,
                        "warning_type": "dof",
                        "title_en": "Low degrees of freedom",
                        "title_fr": "Peu de degrés de liberté",
                        "detail_en": (
                            "Too few trades relative to optimized "
                            "parameters — results lack statistical "
                            "confidence."
                        ),
                        "detail_fr": (
                            "Trop peu de trades par rapport aux "
                            "paramètres optimisés — résultats peu "
                            "fiables statistiquement."
                        ),
                        "actions_en": [
                            "Increase min-trades (aim for 30+ per parameter)",
                            "Reduce the number of optimized parameters",
                            "Extend the training timerange",
                        ],
                        "actions_fr": [
                            "Augmenter min-trades (viser 30+ par paramètre)",
                            "Réduire le nombre de paramètres optimisés",
                            "Étendre le timerange d'entraînement",
                        ],
                        "values": {
                            "trades/params": (f"{n_trades}/{n_params}"),
                            "ratio": round(dof, 1),
                        },
                    }
                )

        if sans_top and sans_top.get("fragile"):
            warnings.append(
                {
                    "severity": "high",
                    "warning_type": "concentration",
                    "title_en": "Profit concentration: fragile",
                    "title_fr": "Concentration du profit : fragile",
                    "detail_en": (
                        "Removing the top 2 trades makes profit "
                        "negative — the edge depends on lucky hits."
                    ),
                    "detail_fr": (
                        "Sans les 2 meilleurs trades, le profit "
                        "devient négatif — l'edge dépend de coups "
                        "de chance."
                    ),
                    "actions_en": [
                        "Increase min-trades to force more diversified results",
                        "Add more pairs to spread risk",
                        "Check if the top trade is a lucky outlier or a repeatable pattern",
                    ],
                    "actions_fr": [
                        "Augmenter min-trades pour forcer des résultats plus diversifiés",
                        "Ajouter plus de paires pour répartir le risque",
                        "Vérifier si le meilleur trade est un "
                        "outlier chanceux ou un pattern répétable",
                    ],
                    "values": {
                        "total": sans_top.get("total_profit"),
                        "without_top2": sans_top.get("without_top2"),
                    },
                }
            )

        if bvm_gap and bvm_gap.get("outlier"):
            warnings.append(
                {
                    "severity": "medium",
                    "warning_type": "outlier",
                    "title_en": "Best epoch is an outlier",
                    "title_fr": "Le meilleur epoch est un outlier",
                    "detail_en": (
                        "Best profit > 2x the median top-10 — "
                        "the best epoch may be the luckiest, "
                        "not the best."
                    ),
                    "detail_fr": (
                        "Le profit du meilleur > 2x la médiane — "
                        "l'epoch est peut-être le plus chanceux, "
                        "pas le meilleur."
                    ),
                    "actions_en": [
                        "Consider using 2nd or 3rd best epoch",
                        "Compare param values of best vs median to find what differs",
                        "Run a shorter timerange to check if the best epoch is robust",
                    ],
                    "actions_fr": [
                        "Envisager le 2e ou 3e meilleur epoch",
                        "Comparer les paramètres du meilleur vs "
                        "la médiane pour identifier les écarts",
                        "Lancer sur un timerange plus court pour vérifier la robustesse",
                    ],
                    "values": {
                        "best": f"{bvm_gap.get('best_profit')}%",
                        "median": (f"{bvm_gap.get('median_profit')}%"),
                        "gap": f"{bvm_gap.get('gap_ratio')}x",
                    },
                }
            )

        if dist_analysis:
            if dist_analysis.get("skew_alert"):
                warnings.append(
                    {
                        "severity": "medium",
                        "warning_type": "skew",
                        "title_en": "Negative skew: tail risk",
                        "title_fr": "Skew négatif : risque de queue",
                        "detail_en": (
                            "Return distribution has heavy left tail — occasional large losses."
                        ),
                        "detail_fr": (
                            "La distribution a une queue gauche "
                            "lourde — grosses pertes occasionnelles."
                        ),
                        "actions_en": [
                            "Add or tighten stoploss to cap downside",
                            "Check if a few pairs dominate the left tail",
                            "Consider CalmarHyperOptLoss to penalize drawdown-heavy results",
                        ],
                        "actions_fr": [
                            "Ajouter ou resserrer le stoploss pour limiter les pertes",
                            "Vérifier si quelques paires dominent la queue gauche",
                            "Envisager CalmarHyperOptLoss pour "
                            "pénaliser les résultats à fort "
                            "drawdown",
                        ],
                        "values": {
                            "skewness": dist_analysis.get("skewness"),
                        },
                    }
                )
            if dist_analysis.get("kurtosis_alert"):
                warnings.append(
                    {
                        "severity": "medium",
                        "warning_type": "kurtosis",
                        "title_en": "Fat tails: extreme events",
                        "title_fr": ("Queues épaisses : événements extrêmes"),
                        "detail_en": (
                            "Excess kurtosis > 3 — extreme gains "
                            "and losses more frequent than normal."
                        ),
                        "detail_fr": (
                            "Kurtosis > 3 — les gains et pertes "
                            "extrêmes sont plus fréquents que la "
                            "normale."
                        ),
                        "actions_en": [
                            "Expect occasional extreme P&L days",
                            "Size positions conservatively",
                            "Use a loss function that accounts for tail risk (Calmar, Sortino)",
                        ],
                        "actions_fr": [
                            "S'attendre à des jours de P&L extrêmes occasionnels",
                            "Dimensionner les positions prudemment",
                            "Utiliser une loss function qui "
                            "tient compte du risque de queue "
                            "(Calmar, Sortino)",
                        ],
                        "values": {
                            "kurtosis": dist_analysis.get("excess_kurtosis"),
                        },
                    }
                )

        boundary = [n for n, p in param_deep.items() if p.get("boundary_cluster")]
        if boundary:
            warnings.append(
                {
                    "severity": "medium",
                    "warning_type": "boundary",
                    "title_en": "Boundary clustering",
                    "title_fr": "Clustering aux bornes",
                    "detail_en": (
                        "Some params cluster at the edge of the "
                        "search range — the optimum may lie "
                        "outside."
                    ),
                    "detail_fr": (
                        "Certains paramètres se concentrent au "
                        "bord du range — l'optimum est peut-être "
                        "hors de l'espace de recherche."
                    ),
                    "actions_en": [
                        "Extend the search range for flagged parameters",
                        "Re-run hyperopt with wider bounds to check if loss improves",
                    ],
                    "actions_fr": [
                        "Étendre la plage de recherche des paramètres signalés",
                        "Relancer l'hyperopt avec des bornes "
                        "plus larges pour voir si la loss "
                        "s'améliore",
                    ],
                    "values": {
                        "params": ", ".join(boundary),
                    },
                }
            )

        return warnings

    @staticmethod
    def _compute_param_stats(
        param_values: dict[str, list],
    ) -> dict[str, dict]:
        import statistics
        from collections import Counter

        param_stats: dict[str, dict] = {}
        for pname, vals in param_values.items():
            n = len(vals)
            if n < 2:
                continue
            nums = [v for v in vals if isinstance(v, (int, float))]
            entry: dict[str, Any] = {}
            if len(nums) >= 2:
                entry["median"] = round(statistics.median(nums), 4)
                entry["mean"] = round(sum(nums) / len(nums), 4)
                for k in (3, 5):
                    sl = nums[:k]
                    if len(sl) >= 2:
                        entry[f"median_top{k}"] = round(statistics.median(sl), 4)
                        entry[f"mean_top{k}"] = round(sum(sl) / len(sl), 4)
            # Majority (mode) for top-5 and top-10
            for k in (5, 10):
                sl = vals[:k]
                if sl:
                    c = Counter(sl)
                    most = c.most_common(1)[0]
                    entry[f"majority_top{k}"] = most[0]
                    entry[f"majority_top{k}_count"] = most[1]

            # Recommended = median of top-5 if available
            if "median_top5" in entry:
                entry["recommended"] = entry["median_top5"]
            elif "median" in entry:
                entry["recommended"] = entry["median"]
            if entry:
                param_stats[pname] = entry
        return param_stats

    def _build_loss_histogram(self, all_losses: list[float]) -> dict | None:
        if not all_losses:
            return None
        best = min(all_losses)
        return {
            "bins": self._compute_histogram_bins(all_losses, 10),
            "best_loss": round(best, 4),
            "best_percentile": round(
                sum(1 for v in all_losses if v > best) / max(len(all_losses), 1) * 100,
                1,
            ),
        }

    def _do_export_html_report(self) -> None:
        import math
        import statistics

        from freqtrade.optimize.hyperopt_html_report import (
            generate_hyperopt_html_report,
        )

        all_losses: list[float] = []
        epoch_dd_data: list[float] = []
        top_epochs: list[dict] = []
        for batch in HyperoptTools._read_results(self.results_file):
            for ep in batch:
                loss = ep.get("loss", 1e6)
                if loss < 1e5:
                    all_losses.append(loss)
                    epoch_dd_data.append(
                        ep.get("results_metrics", {}).get("max_drawdown_account", 0.0)
                    )
                    top_epochs.append(ep)

        top_epochs.sort(key=lambda e: e.get("loss", 1e6))
        top_10 = top_epochs[:10]

        best = self.current_best_epoch or {}
        rm = best.get("results_metrics", {})
        best_trades = rm.get("trades", [])

        param_values: dict[str, list] = {}
        for ep in top_10:
            pd = ep.get("params_dict", {})
            for k, v in pd.items():
                param_values.setdefault(k, []).append(v)

        all_param_values: dict[str, list] = {}
        for ep in top_epochs:
            pd = ep.get("params_dict", {})
            for k, v in pd.items():
                all_param_values.setdefault(k, []).append(v)

        param_stability: dict[str, dict] = {}
        for pname, vals in param_values.items():
            nums = [v for v in vals if isinstance(v, (int, float))]
            if len(nums) >= 2:
                std = statistics.stdev(nums)
                rng = max(nums) - min(nums)
                ratio = std / rng if rng > 0 else 0.0
                param_stability[pname] = {
                    "values": nums,
                    "median": statistics.median(nums),
                    "std": round(std, 4),
                    "std_over_range": round(ratio, 4),
                    "stable": ratio < 0.15,
                    "unstable": ratio > 0.30,
                }

        exp_max_sr = math.sqrt(2 * math.log(max(self.total_epochs, 2)))
        observed_sharpe = rm.get("sharpe", 0.0)

        trade_metrics = self._compute_trade_metrics(best_trades)
        param_analytics = self._compute_param_analytics(param_values, top_10, rm)

        param_stats = self._compute_param_stats(param_values)

        pair_whitelist = self.config.get("exchange", {}).get("pair_whitelist", [])
        total_pairs = len(pair_whitelist) if pair_whitelist else 0

        loss_histogram = self._build_loss_histogram(all_losses)

        param_deep_dive = self._compute_param_deep_dive(
            self.hyperopter.dimensions,
            best.get("params_dict", {}),
            param_values,
            all_param_values,
            all_losses,
        )

        dsr_analysis = {
            "observed_sharpe": round(observed_sharpe, 4),
            "expected_max_sharpe": round(exp_max_sr, 4),
            "n_trials": self.total_epochs,
            "genuine": observed_sharpe > exp_max_sr,
        }

        overfit_warnings = self._compute_overfit_warnings(
            dsr=dsr_analysis,
            param_deep=param_deep_dive,
            n_params=len(self.hyperopter.dimensions),
            n_trades=rm.get("total_trades", 0),
            sans_top=trade_metrics.get("sans_top_trade"),
            bvm_gap=param_analytics.get("best_vs_median_gap"),
            dist_analysis=trade_metrics.get("distribution_analysis"),
        )

        best_params_raw = best.get("params_dict", {})

        data = {
            "strategy": self.config.get("strategy", "Unknown"),
            "hyperopt_loss": self.config.get("hyperopt_loss", "Unknown"),
            "sampler": self.config.get("hyperopt_sampler"),
            "total_epochs": self.total_epochs,
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "stake_currency": self.config.get("stake_currency", "USDT"),
            "best_epoch": rm,
            "best_params": best.get("params_details", {}),
            "best_params_raw": best_params_raw,
            "best_loss": round(best.get("loss", 0), 6),
            "top_epochs": [
                {
                    "loss": e.get("loss", 0),
                    "results_metrics": e.get("results_metrics", {}),
                    "params_dict": e.get("params_dict", {}),
                }
                for e in top_10
            ],
            "all_losses": all_losses,
            "param_stability": param_stability,
            "config_summary": {
                "epochs": self.total_epochs,
                "spaces": self.config.get("spaces", []),
                "min_trades": self.config.get("hyperopt_min_trades", 1),
                "timerange": self.config.get("timerange", ""),
                "timeframe": self.config.get("timeframe", ""),
                "jobs": self.config.get("hyperopt_jobs", -1),
                "random_state": self.config.get("hyperopt_random_state"),
                "analyze_per_epoch": self.config.get("analyze_per_epoch", False),
                "print_all": self.config.get("print_all", False),
                "print_json": self.config.get("print_json", False),
            },
            "elapsed_secs": (datetime.now() - self._start_time).total_seconds(),
            "user_params": self._detect_user_params(),
            "total_pairs": total_pairs,
            "param_stats": param_stats,
            "dsr_analysis": dsr_analysis,
            "param_deep_dive": param_deep_dive,
            "overfit_warnings": overfit_warnings,
            "distribution_analysis": trade_metrics.get("distribution_analysis"),
            "sans_top_trade": trade_metrics.get("sans_top_trade"),
            "pair_profit_distribution": trade_metrics.get("pair_profit_distribution", []),
            "best_vs_median_gap": param_analytics.get("best_vs_median_gap"),
            "param_correlation": param_analytics.get("param_correlation", []),
            "loss_histogram": loss_histogram,
            "parallel_coords": param_analytics.get("parallel_coords", {"params": [], "lines": []}),
            "benchmark_comparison": {
                "sharpe": {
                    "value": round(rm.get("sharpe", 0), 4),
                    "benchmark": 0.85,
                    "above": rm.get("sharpe", 0) > 0.85,
                },
                "dd": {
                    "value": round(rm.get("max_drawdown_account", 0) * 100, 2),
                    "benchmark": 25.0,
                    "above": rm.get("max_drawdown_account", 0) > 0.25,
                },
            },
            "dispersion_bands": param_analytics.get("dispersion_bands", {}),
            "epoch_dd_data": epoch_dd_data,
            "monte_carlo": self._compute_monte_carlo(best_trades),
            "sensitivity_grid": self._compute_sensitivity_grid(top_epochs, all_param_values),
            "regime_analysis": self._compute_regime_analysis(best_trades),
            "return_vs_dd": self._compute_return_vs_dd(top_epochs),
            "dof_analysis": self._compute_dof_analysis(
                rm.get("total_trades", 0),
                len(self.hyperopter.dimensions),
            ),
        }

        out_path = self.results_file.with_suffix(".html")
        result = generate_hyperopt_html_report(data, out_path)
        logger.info(f"HTML report saved to '{result}'")
