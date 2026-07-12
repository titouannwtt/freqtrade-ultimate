"""
ReplayDataStore — loads OHLCV / mark / funding-rate feathers and serves
time-gated slices.

Keyed by ``(pair, timeframe, candle_type)`` and lazy-loaded from disk on first
request, which transparently covers:

* the strategy's own timeframe candles (``CandleType.FUTURES``),
* informative pairs declared by ``strategy.informative_pairs()``,
* the mark + funding-rate candles that Freqtrade's *dry-run* funding-fee
  calculation asks for (``_fetch_and_calculate_funding_fees`` →
  ``refresh_latest_ohlcv([mark_comb, funding_comb])``).

That last point is why this harness gets funding fees right where the original
``Freqtrade_reply`` zeroed them out: we simply serve the data Freqtrade already
knows how to consume.

Slicing rules mirror live behaviour:

* ``drop_incomplete=True`` (the strategy feed) returns only candles whose open
  time is strictly before ``up_to`` — the currently-forming candle is hidden,
  exactly as ``refresh_latest_ohlcv(drop_incomplete=True)`` does live.
* a ``max_candles`` cap mirrors the live bot fetching ~500 candles rather than
  all history, and keeps indicator recomputation cheap.

File naming follows Freqtrade's own convention:
``{BASE}_{QUOTE}_{SETTLE}-{tf}-{candle_type}.feather`` (no suffix for spot).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from freqtrade.enums import CandleType
from freqtrade.exchange import timeframe_to_minutes


logger = logging.getLogger(__name__)

# Finest first, so order-fill prices are taken from the highest-resolution data
# available for a pair.
PRICE_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]

_EMPTY_COLS = ["date", "open", "high", "low", "close", "volume"]

# Module-level cache for cheap date-range lookups (coverage checks), keyed by
# (path, mtime) so it survives across stateless API requests yet self-invalidates
# if a feather is re-downloaded.
_DATE_RANGE_CACHE: dict = {}


class ReplayDataStore:
    # Default rows returned to the strategy per call. The runner overrides this
    # to startup_candle_count + buffer so indicator warmup is always sufficient,
    # while still avoiding a recompute over the full multi-year history each tick.
    MAX_CANDLES = 500

    def __init__(
        self, data_dir: str | Path, trading_mode: str = "futures", max_candles: int | None = None
    ) -> None:
        self._data_dir = Path(data_dir)
        self._trading_mode = trading_mode
        self.max_candles = max_candles or self.MAX_CANDLES
        # (pair, tf, candle_type_value) -> DataFrame | None  (None = known-absent)
        self._cache: dict[tuple[str, str, str], pd.DataFrame | None] = {}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _filename(self, pair: str, tf: str, candle_type: CandleType) -> Path:
        # "BTC/USDC:USDC" -> "BTC_USDC_USDC"
        base = pair.replace("/", "_").replace(":", "_")
        suffix = "" if candle_type == CandleType.SPOT else f"-{candle_type.value}"
        return self._data_dir / f"{base}-{tf}{suffix}.feather"

    def _load(self, pair: str, tf: str, candle_type: CandleType) -> pd.DataFrame | None:
        key = (pair, tf, candle_type.value)
        if key in self._cache:
            return self._cache[key]

        path = self._filename(pair, tf, candle_type)
        if not path.exists():
            self._cache[key] = None
            return None

        df = pd.read_feather(path).sort_values("date").reset_index(drop=True)
        if df["date"].dt.tz is None:
            df["date"] = df["date"].dt.tz_localize("UTC")
        self._cache[key] = df
        logger.info(
            "Loaded %s %s %s: %d candles  %s -> %s",
            pair,
            tf,
            candle_type.value,
            len(df),
            df.iloc[0]["date"].isoformat(),
            df.iloc[-1]["date"].isoformat(),
        )
        return df

    def has(self, pair: str, candle_type: CandleType) -> bool:
        """True if any timeframe of ``candle_type`` exists on disk for ``pair``."""
        return any(self._load(pair, tf, candle_type) is not None for tf in PRICE_TIMEFRAMES)

    def date_range(self, pair: str, tf: str, candle_type: CandleType):
        """
        First/last candle date for a pair (or None if absent) — cheap coverage probe.

        Reads only the ``date`` column (not the whole frame) and caches by file mtime,
        so scanning ~100 pairs for the modal's data-coverage warnings stays fast.
        """
        path = self._filename(pair, tf, candle_type)
        if not path.exists():
            return None
        key = (str(path), path.stat().st_mtime)
        if key in _DATE_RANGE_CACHE:
            return _DATE_RANGE_CACHE[key]
        try:
            col = pd.read_feather(path, columns=["date"])["date"]
        except (OSError, ValueError, KeyError):
            return None
        rng = None if col.empty else (col.iloc[0], col.iloc[-1])
        _DATE_RANGE_CACHE[key] = rng
        return rng

    # ------------------------------------------------------------------
    # Serving
    # ------------------------------------------------------------------

    def get_candles(
        self,
        pair: str,
        tf: str,
        candle_type: CandleType,
        up_to: datetime,
        *,
        since_ms: int | None = None,
        drop_incomplete: bool = True,
        max_candles: int | None = MAX_CANDLES,
    ) -> pd.DataFrame:
        df = self._load(pair, tf, candle_type)
        if df is None or df.empty:
            return pd.DataFrame(columns=_EMPTY_COLS)

        up_to_ts = pd.Timestamp(up_to)
        if drop_incomplete:
            # A candle is only usable once its CLOSE has passed: open + tf <= now.
            # Gating on open time alone (open < now) re-admits the still-forming
            # candle as soon as the clock leaves the open boundary — a look-ahead
            # of up to (timeframe - sub_step) whenever the sub-step is finer than
            # the timeframe (e.g. 1m sub-step on a 1h strategy).
            tf_delta = pd.Timedelta(minutes=timeframe_to_minutes(tf))
            hi = int(df["date"].searchsorted(up_to_ts - tf_delta, side="right"))
        else:
            hi = int(df["date"].searchsorted(up_to_ts, side="right"))

        lo = 0
        if since_ms is not None:
            since_ts = pd.Timestamp(since_ms, unit="ms", tz="UTC")
            lo = int(df["date"].searchsorted(since_ts, side="left"))
        if max_candles is not None:
            lo = max(lo, hi - max_candles)

        if hi <= lo:
            return pd.DataFrame(columns=_EMPTY_COLS)
        return df.iloc[lo:hi].reset_index(drop=True)

    def get_last_price(self, pair: str, up_to: datetime, candle_type: CandleType) -> float:
        """Last known close strictly before ``up_to``, finest resolution first."""
        up_to_ts = pd.Timestamp(up_to)
        for tf in PRICE_TIMEFRAMES:
            df = self._load(pair, tf, candle_type)
            if df is None or df.empty:
                continue
            idx = int(df["date"].searchsorted(up_to_ts, side="left")) - 1
            if idx >= 0:
                return float(df.iloc[idx]["close"])
        raise ValueError(f"No price data for {pair} ({candle_type.value}) before {up_to}")

    def get_candle_ohlc(
        self, pair: str, tf: str, candle_type: CandleType, up_to: datetime
    ) -> dict | None:
        """OHLC of the last completed ``tf`` candle before ``up_to`` (intra-candle fills)."""
        df = self._load(pair, tf, candle_type)
        if df is None or df.empty:
            return None
        idx = int(df["date"].searchsorted(pd.Timestamp(up_to), side="left")) - 1
        if idx < 0:
            return None
        row = df.iloc[idx]
        return {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }

    # ------------------------------------------------------------------
    # Validation (pre-flight)
    # ------------------------------------------------------------------

    def validate(
        self,
        pair: str,
        tf: str,
        candle_type: CandleType,
        start_dt: datetime,
        end_dt: datetime,
        startup_count: int,
    ) -> None:
        """Raise ValueError if data is missing or insufficient for the run window."""
        df = self._load(pair, tf, candle_type)
        if df is None:
            raise ValueError(
                f"No {tf} {candle_type.value} data for {pair}. "
                f"Expected file: {self._filename(pair, tf, candle_type)}"
            )

        start_ts = pd.Timestamp(start_dt)
        end_ts = pd.Timestamp(end_dt)

        warmup = df[df["date"] < start_ts]
        if len(warmup) < startup_count:
            raise ValueError(
                f"{pair} {tf}: need {startup_count} warmup candles before {start_dt:%Y-%m-%d}, "
                f"have {len(warmup)}. Download more historical data."
            )
        replay_range = df[(df["date"] >= start_ts) & (df["date"] < end_ts)]
        if replay_range.empty:
            raise ValueError(
                f"{pair} {tf}: no data in range {start_dt:%Y-%m-%d} -> {end_dt:%Y-%m-%d}"
            )

        logger.info(
            "Validated %s %s: %d warmup + %d replay candles",
            pair,
            tf,
            len(warmup),
            len(replay_range),
        )
