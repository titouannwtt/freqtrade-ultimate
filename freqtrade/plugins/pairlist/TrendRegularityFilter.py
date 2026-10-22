"""
Trend Regularity pairlist filter

Filters out pairs that show a regular uptrend using linear regression.
A pair is excluded if:
- The slope of the linear regression is positive (uptrend)
- The R² (coefficient of determination) is above a threshold (regular/smooth trend)

This is useful for short strategies: you don't want to short a coin
that's been going up steadily.
"""

import logging
import time

import numpy as np
from pandas import DataFrame

from freqtrade.constants import ListPairsWithTimeframes
from freqtrade.exceptions import OperationalException
from freqtrade.exchange.exchange_types import Tickers
from freqtrade.plugins.pairlist.IPairList import IPairList, PairlistParameter, SupportsBacktesting
from freqtrade.plugins.pairlist.swr_verdicts import StaleVerdictStore, swr_parameters
from freqtrade.util import FtTTLCache


logger = logging.getLogger(__name__)


class TrendRegularityFilter(IPairList):
    """
    Filters pairs by trend direction and regularity using linear regression.
    Excludes pairs with a regular uptrend (positive slope + high R²).
    """

    supports_backtesting = SupportsBacktesting.NO

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self._lookback_timeframe = self._pairlistconfig.get("lookback_timeframe", "1h")
        self._lookback_period = self._pairlistconfig.get("lookback_period", 5000)
        self._min_r2 = self._pairlistconfig.get("min_r2", 0.6)
        self._refresh_period = self._pairlistconfig.get("refresh_period", 3600)
        self._def_candletype = self._config["candle_type_def"]

        # Opt-in stale-while-revalidate, see swr_verdicts.py. Off: behaviour is unchanged.
        self._swr: StaleVerdictStore | None = None
        if self._pairlistconfig.get("stale_while_revalidate", False):
            self._swr = StaleVerdictStore(
                refresh_period=self._refresh_period,
                max_per_cycle=self._pairlistconfig.get("max_recompute_per_cycle", 2),
                quiet_after_close_s=self._pairlistconfig.get("quiet_after_close_s", 0),
                jitter_pct=self._pairlistconfig.get("swr_jitter_pct", 0.2),
            )
            error = StaleVerdictStore.validate(
                self._swr.max_per_cycle, self._swr.quiet_after_close_s, self._swr.jitter_pct
            )
            if error:
                raise OperationalException(f"TrendRegularityFilter: {error}")

        self._pair_cache: FtTTLCache = FtTTLCache(maxsize=1000, ttl=self._refresh_period)

        self._shared_client = None
        self._params_hash = ""
        try:
            from freqtrade.pairlist_cache.client import PairlistCacheClient

            self._shared_client = PairlistCacheClient.get_or_spawn()
            self._params_hash = PairlistCacheClient.compute_params_hash(self._pairlistconfig)
        except Exception:
            logger.info("Shared pairlist cache unavailable, using local cache only.")

        if self._lookback_period < 2:
            raise OperationalException("TrendRegularityFilter requires lookback_period to be >= 2")

        from freqtrade.exchange import timeframe_to_minutes

        self._tf_in_min = timeframe_to_minutes(self._lookback_timeframe)

        candle_limit = self._exchange.ohlcv_candle_limit(
            self._lookback_timeframe, self._def_candletype
        )
        if self._lookback_period > candle_limit:
            raise OperationalException(
                "TrendRegularityFilter requires lookback_period to not "
                f"exceed exchange max request size ({candle_limit})"
            )

        if self._min_r2 < 0 or self._min_r2 > 1:
            raise OperationalException(
                "TrendRegularityFilter requires min_r2 to be between 0 and 1"
            )

    @property
    def needstickers(self) -> bool:
        return False

    def short_desc(self) -> str:
        return (
            f"{self.name} - Filtering pairs with regular uptrend "
            f"(R² >= {self._min_r2}) over {self._lookback_period} "
            f"{self._lookback_timeframe} candles."
        )

    @staticmethod
    def description() -> str:
        return (
            "Filter pairs with a regular uptrend using linear regression. "
            "Excludes pairs where slope > 0 and R² > threshold."
        )

    @staticmethod
    def available_parameters() -> dict[str, PairlistParameter]:
        return {
            "lookback_timeframe": {
                "type": "string",
                "default": "1h",
                "description": "Lookback Timeframe",
                "help": "Timeframe to use for candles.",
            },
            "lookback_period": {
                "type": "number",
                "default": 5000,
                "description": "Lookback Period",
                "help": "Number of candles to analyze for trend.",
            },
            "min_r2": {
                "type": "number",
                "default": 0.6,
                "description": "Minimum R² to exclude",
                "help": (
                    "R² threshold (0-1). Pairs with positive slope AND R² above "
                    "this value are excluded. Higher = only exclude very regular trends."
                ),
            },
            **IPairList.refresh_period_parameter(),
            **swr_parameters(),
        }

    def filter_pairlist(self, pairlist: list[str], tickers: Tickers) -> list[str]:
        """
        Filter pairlist - remove pairs with regular uptrend.
        """
        if self._swr is not None:
            return self._filter_pairlist_swr(pairlist, self._swr)

        if self._shared_client:
            locally_uncached = [p for p in pairlist if p not in self._pair_cache]
            if locally_uncached:
                shared = self._shared_client.mget(
                    "TrendRegularityFilter", self._params_hash, locally_uncached
                )
                for p, val in shared.items():
                    if val is not None:
                        self._pair_cache[p] = val["exclude"]

        needed_pairs: ListPairsWithTimeframes = [
            (p, self._lookback_timeframe, self._def_candletype)
            for p in pairlist
            if p not in self._pair_cache
        ]

        candles = self._exchange.refresh_ohlcv_with_cache(
            needed_pairs, lookback_period=self._lookback_period
        )

        freshly_needed = {p for p, _, _ in needed_pairs}
        newly_computed: dict[str, dict] = {}
        resulting_pairlist: list[str] = []
        for p in pairlist:
            pair_candles = candles.get((p, self._lookback_timeframe, self._def_candletype), None)

            should_exclude = self._check_trend(p, pair_candles)

            if p in freshly_needed and should_exclude is not None:
                newly_computed[p] = {"exclude": should_exclude}

            if should_exclude is None:
                self.log_once(f"Removed {p} from whitelist, no candles found.", logger.info)
            elif should_exclude:
                pass
            else:
                resulting_pairlist.append(p)

        if newly_computed and self._shared_client:
            self._shared_client.mput(
                "TrendRegularityFilter",
                self._params_hash,
                newly_computed,
                ttl=self._refresh_period,
            )

        return resulting_pairlist

    def _filter_pairlist_swr(self, pairlist: list[str], swr: StaleVerdictStore) -> list[str]:
        """
        Stale-while-revalidate variant of filter_pairlist: serve the last verdict of each
        pair and recompute only a bounded number of them per call (see swr_verdicts.py).
        A pair without any verdict yet is removed until its verdict has been computed.
        """
        now = swr.now()
        missing, due = swr.classify(pairlist, now)

        if self._shared_client and (missing or due):
            shared = self._shared_client.mget(
                "TrendRegularityFilter", self._params_hash, missing + due
            )
            for p, val in shared.items():
                if val is not None:
                    # Entries written without a timestamp are assumed one period old.
                    computed_at = val.get("ts", now - self._refresh_period)
                    swr.adopt(p, val["exclude"], computed_at)
            missing, due = swr.classify(pairlist, now)

        to_compute = swr.select(missing, due, now)
        if to_compute:
            started = time.monotonic()
            needed_pairs: ListPairsWithTimeframes = [
                (p, self._lookback_timeframe, self._def_candletype) for p in to_compute
            ]
            candles = self._exchange.refresh_ohlcv_with_cache(
                needed_pairs, lookback_period=self._lookback_period
            )
            computed_at = swr.now()
            newly_computed: dict[str, dict] = {}
            for p in to_compute:
                pair_candles = candles.get(
                    (p, self._lookback_timeframe, self._def_candletype), None
                )
                verdict = self._compute_trend(p, pair_candles)
                if verdict is None:
                    swr.fail(p, computed_at)
                else:
                    # bool(): the computation returns numpy booleans, which JSON cannot dump.
                    swr.put(p, bool(verdict), computed_at)
                    newly_computed[p] = {"exclude": bool(verdict), "ts": computed_at}

            if newly_computed and self._shared_client:
                self._shared_client.mput(
                    "TrendRegularityFilter",
                    self._params_hash,
                    newly_computed,
                    ttl=self._refresh_period,
                )
            elapsed = time.monotonic() - started
            pending = len(missing) + len(due) - len(to_compute)
            (logger.info if elapsed >= 1 or pending else logger.debug)(
                f"TrendRegularityFilter SWR: recomputed {len(to_compute)} pairs "
                f"({len(missing)} missing, {len(due)} due) in {elapsed:.2f}s, "
                f"{pending} still waiting."
            )
        swr.mark_warm(pairlist)

        resulting_pairlist: list[str] = []
        for p in pairlist:
            should_exclude = swr.get(p)
            if should_exclude is None:
                if swr.is_blocked(p, swr.now()):
                    self.log_once(f"Removed {p} from whitelist, no candles found.", logger.info)
                else:
                    self.log_once(
                        f"Removed {p} from whitelist, trend verdict not computed yet.",
                        logger.info,
                    )
            elif not should_exclude:
                resulting_pairlist.append(p)
        return resulting_pairlist

    def _check_trend(self, pair: str, candles: DataFrame | None) -> bool | None:
        """
        Check if a pair has a regular uptrend.
        Returns True if pair should be excluded, False if it should stay,
        None if no data available.
        """
        cached = self._pair_cache.get(pair, None)
        if cached is not None:
            return cached

        should_exclude = self._compute_trend(pair, candles)
        if should_exclude is not None:
            self._pair_cache[pair] = should_exclude
        return should_exclude

    def _compute_trend(self, pair: str, candles: DataFrame | None) -> bool | None:
        """
        Compute the verdict from the candles, without any cache.
        Same return values as _check_trend.
        """
        if candles is None or candles.empty:
            return None

        closes = candles["close"].values
        if len(closes) < 2:
            return None

        # Normalize closes to [0, 1] range for numerical stability
        c_min = closes.min()
        c_max = closes.max()
        if c_max == c_min:
            # Flat line - no trend
            return False

        y = (closes - c_min) / (c_max - c_min)
        x = np.arange(len(y), dtype=np.float64)

        # Linear regression using least squares
        n = len(x)
        sum_x = x.sum()
        sum_y = y.sum()
        sum_xy = (x * y).sum()
        sum_x2 = (x * x).sum()

        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return False

        slope = (n * sum_xy - sum_x * sum_y) / denom

        # R² calculation
        y_mean = sum_y / n
        intercept = (sum_y - slope * sum_x) / n
        y_pred = slope * x + intercept
        ss_res = ((y - y_pred) ** 2).sum()
        ss_tot = ((y - y_mean) ** 2).sum()

        if ss_tot == 0:
            return False

        r_squared = 1 - (ss_res / ss_tot)

        # Clamp to valid range and guard against NaN from numerical errors
        r_squared = max(0.0, min(1.0, r_squared))
        if np.isnan(r_squared) or np.isnan(slope):
            return False

        should_exclude = slope > 0 and r_squared >= self._min_r2

        if should_exclude:
            self.log_once(
                f"Removed {pair} from whitelist - regular uptrend detected: "
                f"slope={slope:.6f}, R²={r_squared:.4f} (threshold: {self._min_r2})",
                logger.info,
            )

        return should_exclude
