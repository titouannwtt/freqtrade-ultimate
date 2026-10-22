"""
Extreme Move pairlist filter

Directional guard filter: excludes pairs whose price change over the last
`lookback_days` daily candles exceeds a threshold in a given direction.

- `max_up_pct`: exclude pairs that gained more than this percentage
  (guard for SHORT strategies: don't short a coin in a violent pump).
- `max_down_pct`: exclude pairs that lost more than this percentage
  (guard for LONG strategies: don't buy a coin in free fall).

Unlike TrendRegularityFilter (which detects long, regular trends via linear
regression R²), this filter catches short, violent moves regardless of how
"clean" the trend is - e.g. a memecoin that did +80% in 4 days.
"""

import logging
import time

from pandas import DataFrame

from freqtrade.constants import ListPairsWithTimeframes
from freqtrade.exceptions import OperationalException
from freqtrade.exchange.exchange_types import Tickers
from freqtrade.plugins.pairlist.IPairList import IPairList, PairlistParameter, SupportsBacktesting
from freqtrade.plugins.pairlist.swr_verdicts import StaleVerdictStore, swr_parameters
from freqtrade.util import FtTTLCache


logger = logging.getLogger(__name__)


class ExtremeMoveFilter(IPairList):
    """
    Filters out pairs with an extreme recent price move.
    Excludes pairs up more than max_up_pct and/or down more than
    max_down_pct over the last lookback_days daily candles.
    """

    supports_backtesting = SupportsBacktesting.NO

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self._lookback_days = self._pairlistconfig.get("lookback_days", 7)
        self._max_up_pct = self._pairlistconfig.get("max_up_pct", 0)
        self._max_down_pct = self._pairlistconfig.get("max_down_pct", 0)
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
                raise OperationalException(f"ExtremeMoveFilter: {error}")

        self._pair_cache: FtTTLCache = FtTTLCache(maxsize=1000, ttl=self._refresh_period)

        self._shared_client = None
        self._params_hash = ""
        try:
            from freqtrade.pairlist_cache.client import PairlistCacheClient

            self._shared_client = PairlistCacheClient.get_or_spawn()
            self._params_hash = PairlistCacheClient.compute_params_hash(self._pairlistconfig)
        except Exception:
            logger.info("Shared pairlist cache unavailable, using local cache only.")

        if self._lookback_days < 1:
            raise OperationalException("ExtremeMoveFilter requires lookback_days to be >= 1")

        candle_limit = self._exchange.ohlcv_candle_limit("1d", self._def_candletype)
        if self._lookback_days + 1 > candle_limit:
            raise OperationalException(
                "ExtremeMoveFilter requires lookback_days to not "
                f"exceed exchange max request size ({candle_limit - 1})"
            )

        if self._max_up_pct < 0 or self._max_down_pct < 0:
            raise OperationalException(
                "ExtremeMoveFilter requires max_up_pct and max_down_pct to be >= 0"
            )
        if self._max_up_pct == 0 and self._max_down_pct == 0:
            raise OperationalException(
                "ExtremeMoveFilter requires at least one of max_up_pct or "
                "max_down_pct to be set (> 0)"
            )

    @property
    def needstickers(self) -> bool:
        return False

    def short_desc(self) -> str:
        bounds = []
        if self._max_up_pct:
            bounds.append(f"up > {self._max_up_pct}%")
        if self._max_down_pct:
            bounds.append(f"down > {self._max_down_pct}%")
        return (
            f"{self.name} - Filtering pairs that moved {' or '.join(bounds)} "
            f"over the last {self._lookback_days} days."
        )

    @staticmethod
    def description() -> str:
        return (
            "Filter pairs with an extreme recent move (daily candles). "
            "Excludes pumps above max_up_pct and/or dumps below -max_down_pct "
            "over lookback_days."
        )

    @staticmethod
    def available_parameters() -> dict[str, PairlistParameter]:
        return {
            "lookback_days": {
                "type": "number",
                "default": 7,
                "description": "Lookback days",
                "help": "Number of daily candles to measure the price change over.",
            },
            "max_up_pct": {
                "type": "number",
                "default": 0,
                "description": "Maximum gain (%) before exclusion",
                "help": (
                    "Exclude pairs that gained more than this percentage over the "
                    "lookback (guard for short strategies). 0 disables the upper bound."
                ),
            },
            "max_down_pct": {
                "type": "number",
                "default": 0,
                "description": "Maximum loss (%) before exclusion",
                "help": (
                    "Exclude pairs that lost more than this percentage over the "
                    "lookback (guard for long strategies). 0 disables the lower bound."
                ),
            },
            **IPairList.refresh_period_parameter(),
            **swr_parameters(),
        }

    def filter_pairlist(self, pairlist: list[str], tickers: Tickers) -> list[str]:
        """
        Filter pairlist - remove pairs with an extreme recent move.
        """
        if self._swr is not None:
            return self._filter_pairlist_swr(pairlist, self._swr)

        if self._shared_client:
            locally_uncached = [p for p in pairlist if p not in self._pair_cache]
            if locally_uncached:
                shared = self._shared_client.mget(
                    "ExtremeMoveFilter", self._params_hash, locally_uncached
                )
                for p, val in shared.items():
                    if val is not None:
                        self._pair_cache[p] = val["exclude"]

        needed_pairs: ListPairsWithTimeframes = [
            (p, "1d", self._def_candletype) for p in pairlist if p not in self._pair_cache
        ]

        candles = self._exchange.refresh_ohlcv_with_cache(
            needed_pairs, lookback_period=self._lookback_days
        )

        freshly_needed = {p for p, _, _ in needed_pairs}
        newly_computed: dict[str, dict] = {}
        resulting_pairlist: list[str] = []
        for p in pairlist:
            pair_candles = candles.get((p, "1d", self._def_candletype), None)

            should_exclude = self._check_move(p, pair_candles)

            if p in freshly_needed and should_exclude is not None:
                newly_computed[p] = {"exclude": should_exclude}

            if should_exclude is None:
                self.log_once(f"Removed {p} from whitelist, no candles found.", logger.info)
            elif not should_exclude:
                resulting_pairlist.append(p)

        if newly_computed and self._shared_client:
            self._shared_client.mput(
                "ExtremeMoveFilter",
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
            shared = self._shared_client.mget("ExtremeMoveFilter", self._params_hash, missing + due)
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
                (p, "1d", self._def_candletype) for p in to_compute
            ]
            candles = self._exchange.refresh_ohlcv_with_cache(
                needed_pairs, lookback_period=self._lookback_days
            )
            computed_at = swr.now()
            newly_computed: dict[str, dict] = {}
            for p in to_compute:
                verdict = self._compute_move(p, candles.get((p, "1d", self._def_candletype), None))
                if verdict is None:
                    swr.fail(p, computed_at)
                else:
                    swr.put(p, bool(verdict), computed_at)
                    newly_computed[p] = {"exclude": bool(verdict), "ts": computed_at}

            if newly_computed and self._shared_client:
                self._shared_client.mput(
                    "ExtremeMoveFilter",
                    self._params_hash,
                    newly_computed,
                    ttl=self._refresh_period,
                )
            elapsed = time.monotonic() - started
            pending = len(missing) + len(due) - len(to_compute)
            (logger.info if elapsed >= 1 or pending else logger.debug)(
                f"ExtremeMoveFilter SWR: recomputed {len(to_compute)} pairs "
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
                        f"Removed {p} from whitelist, move verdict not computed yet.",
                        logger.info,
                    )
            elif not should_exclude:
                resulting_pairlist.append(p)
        return resulting_pairlist

    def _check_move(self, pair: str, candles: DataFrame | None) -> bool | None:
        """
        Check if a pair had an extreme move over the lookback.
        Returns True if pair should be excluded, False if it should stay,
        None if no data available.
        """
        cached = self._pair_cache.get(pair, None)
        if cached is not None:
            return cached

        should_exclude = self._compute_move(pair, candles)
        if should_exclude is not None:
            self._pair_cache[pair] = should_exclude
        return should_exclude

    def _compute_move(self, pair: str, candles: DataFrame | None) -> bool | None:
        """
        Compute the verdict from the candles, without any cache.
        Same return values as _check_move.
        """
        if candles is None or candles.empty or len(candles) < 2:
            return None

        first_close = candles["close"].iloc[0]
        last_close = candles["close"].iloc[-1]
        if first_close <= 0:
            return False

        change_pct = (last_close / first_close - 1) * 100

        should_exclude = False
        if self._max_up_pct and change_pct > self._max_up_pct:
            should_exclude = True
            self.log_once(
                f"Removed {pair} from whitelist - extreme pump: "
                f"{change_pct:+.1f}% over {self._lookback_days}d "
                f"(max_up_pct: {self._max_up_pct}%)",
                logger.info,
            )
        elif self._max_down_pct and change_pct < -self._max_down_pct:
            should_exclude = True
            self.log_once(
                f"Removed {pair} from whitelist - extreme dump: "
                f"{change_pct:+.1f}% over {self._lookback_days}d "
                f"(max_down_pct: {self._max_down_pct}%)",
                logger.info,
            )

        return should_exclude
