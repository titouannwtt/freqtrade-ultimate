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
from datetime import timedelta

import numpy as np
from pandas import DataFrame

from freqtrade.constants import ListPairsWithTimeframes
from freqtrade.exceptions import OperationalException
from freqtrade.exchange.exchange_types import Tickers
from freqtrade.plugins.pairlist.IPairList import IPairList, PairlistParameter, SupportsBacktesting
from freqtrade.util import FtTTLCache, dt_now, dt_ts


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
        }

    def filter_pairlist(self, pairlist: list[str], tickers: Tickers) -> list[str]:
        """
        Filter pairlist - remove pairs with regular uptrend.
        """
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

        since_ms = dt_ts(dt_now() - timedelta(minutes=self._lookback_period * self._tf_in_min))
        candles = self._exchange.refresh_ohlcv_with_cache(needed_pairs, since_ms=since_ms)

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

    def _check_trend(self, pair: str, candles: DataFrame | None) -> bool | None:
        """
        Check if a pair has a regular uptrend.
        Returns True if pair should be excluded, False if it should stay,
        None if no data available.
        """
        cached = self._pair_cache.get(pair, None)
        if cached is not None:
            return cached

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
            self._pair_cache[pair] = False
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
            self._pair_cache[pair] = False
            return False

        slope = (n * sum_xy - sum_x * sum_y) / denom

        # R² calculation
        y_mean = sum_y / n
        intercept = (sum_y - slope * sum_x) / n
        y_pred = slope * x + intercept
        ss_res = ((y - y_pred) ** 2).sum()
        ss_tot = ((y - y_mean) ** 2).sum()

        if ss_tot == 0:
            self._pair_cache[pair] = False
            return False

        r_squared = 1 - (ss_res / ss_tot)

        # Clamp to valid range and guard against NaN from numerical errors
        r_squared = max(0.0, min(1.0, r_squared))
        if np.isnan(r_squared) or np.isnan(slope):
            self._pair_cache[pair] = False
            return False

        should_exclude = slope > 0 and r_squared >= self._min_r2

        if should_exclude:
            self.log_once(
                f"Removed {pair} from whitelist - regular uptrend detected: "
                f"slope={slope:.6f}, R²={r_squared:.4f} (threshold: {self._min_r2})",
                logger.info,
            )

        self._pair_cache[pair] = should_exclude
        return should_exclude
