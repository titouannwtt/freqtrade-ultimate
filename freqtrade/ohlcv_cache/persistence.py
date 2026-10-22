"""
Feather-backed persistence for the OHLCV cache.

Each CandleSeries is stored as one .feather file compatible with
freqtrade's native format. Layout:

    {persistence_root}/
      {exchange}/
        {trading_mode}/
          {PAIR_safe}-{timeframe}[-{candle_type}].feather

  PAIR_safe = PAIR.replace("/", "_").replace(":", "_")
  candle_type suffix is added only when != "spot" and != "futures"
  (mark, index, premiumIndex, funding_rate).

On daemon startup, all feather files are loaded. On periodic flush
(every flush_interval_s), only dirty series are written.

Also writes a small `_meta.json` alongside each feather to persist
auxiliary fields (earliest_available_ts, last_live_refresh_wall_ms, partial_ts).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

from freqtrade.ohlcv_cache.store import CandleSeries, CandleStore


logger = logging.getLogger("ftcache.daemon")


_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
# Filename: BTC_USDC_USDC-15m.feather  or  BTC_USDC_USDC-1h-mark.feather
_FNAME_RE = re.compile(r"^(?P<pair>.+?)-(?P<tf>\d+[smhdw])(?:-(?P<ct>[a-zA-Z_]+))?\.feather$")
_BASE_CANDLE_TYPES = {"spot", "futures"}


def _tf_to_ms(tf: str) -> int:
    unit = tf[-1]
    n = int(tf[:-1])
    return n * {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}.get(
        unit, 60_000
    )


# No candle exists before this: anything earlier is a corrupt timestamp.
_MIN_VALID_TS_MS = 1_483_228_800_000  # 2017-01-01 UTC


def _dates_to_ms(dates: pd.Series) -> np.ndarray:
    """Epoch milliseconds of a date column, whatever its storage unit.

    pandas 2.x writes datetime64[ns], pandas 3 writes datetime64[ms]; reading both
    through astype("int64") // 10**6 turns every ms file into 1970. The unit is
    normalised explicitly instead.
    """
    if pd.api.types.is_datetime64_any_dtype(dates):
        if getattr(dates.dt, "tz", None) is not None:
            dates = dates.dt.tz_convert("UTC").dt.tz_localize(None)
        return dates.to_numpy().astype("datetime64[ms]").astype("int64")
    return dates.to_numpy(dtype=np.int64)


def _pair_to_filename(pair: str) -> str:
    return pair.replace("/", "_").replace(":", "_")


def _filename_to_pair(fname_pair: str) -> str:
    # Reverse isn't unambiguous in general. We store the canonical pair in
    # the meta JSON and restore from there.
    return fname_pair


class FeatherPersistence:
    def __init__(self, root: Path, store: CandleStore) -> None:
        self.root = Path(root)
        self.store = store

    # -------------- path helpers

    def _dir_for(self, exchange: str, trading_mode: str) -> Path:
        return self.root / exchange / trading_mode

    def _file_for(self, series: CandleSeries) -> Path:
        name = f"{_pair_to_filename(series.pair)}-{series.timeframe}"
        if series.candle_type and series.candle_type not in _BASE_CANDLE_TYPES:
            name += f"-{series.candle_type}"
        name += ".feather"
        return self._dir_for(series.exchange, series.trading_mode) / name

    def _meta_file_for(self, series: CandleSeries) -> Path:
        return self._file_for(series).with_suffix(".meta.json")

    # -------------- load

    def load_all(self) -> int:
        """Scan `root` and populate the store. Returns series loaded."""
        if not self.root.exists():
            return 0
        loaded = 0
        for exchange_dir in sorted(self.root.iterdir()):
            if not exchange_dir.is_dir():
                continue
            exchange = exchange_dir.name
            for tm_dir in sorted(exchange_dir.iterdir()):
                if not tm_dir.is_dir():
                    continue
                trading_mode = tm_dir.name
                for f in sorted(tm_dir.glob("*.feather")):
                    try:
                        series = self._load_one(f, exchange, trading_mode)
                        if series is not None:
                            self.store.put(series)
                            loaded += 1
                    except Exception as e:
                        logger.warning(
                            "could not load %s: %s",
                            f,
                            e,
                        )
        return loaded

    def _load_one(
        self,
        path: Path,
        exchange: str,
        trading_mode: str,
    ) -> CandleSeries | None:
        m = _FNAME_RE.match(path.name)
        if not m:
            return None
        pair_fname = m.group("pair")
        tf = m.group("tf")
        ct_suffix = m.group("ct")

        meta_path = path.with_suffix(".meta.json")
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except Exception as e:
                logger.warning("bad meta %s: %s", meta_path, e)

        pair = meta.get("pair") or _filename_to_pair(pair_fname)
        candle_type = meta.get("candle_type") or ct_suffix or trading_mode

        df = pd.read_feather(path)
        if df.empty:
            return None
        # Convert back to np.ndarray [ts, o, h, l, c, v] with ts in ms.
        ts = _dates_to_ms(df["date"])
        arr = np.column_stack(
            [
                ts.astype(np.float64),
                df["open"].to_numpy(dtype=np.float64),
                df["high"].to_numpy(dtype=np.float64),
                df["low"].to_numpy(dtype=np.float64),
                df["close"].to_numpy(dtype=np.float64),
                df["volume"].to_numpy(dtype=np.float64),
            ]
        )

        # Rows dated before the first possible candle come from the unit bug fixed
        # here (ms values read as ns, i.e. 1970 timestamps): their real timestamps
        # are lost. Drop them, so range_start is the first real candle and the
        # prefix gap is refetched through the normal gap logic.
        keep = ts >= _MIN_VALID_TS_MS
        n_bad = int((~keep).sum())
        if n_bad:
            logger.warning(
                "%s: dropping %d row(s) dated before 2017 (kept %d); the history will be refetched",
                path.name,
                n_bad,
                int(keep.sum()),
            )
            if not keep.any():
                return None
            arr = arr[keep]
        arr = arr[np.argsort(arr[:, 0], kind="stable")]

        series = CandleSeries(
            exchange=exchange,
            trading_mode=trading_mode,
            pair=pair,
            timeframe=tf,
            candle_type=candle_type,
            tf_ms=_tf_to_ms(tf),
            candles=arr,
            earliest_available_ts=meta.get("earliest_available_ts"),
            last_live_refresh_wall_ms=int(meta.get("last_live_refresh_wall_ms", 0) or 0),
            partial_ts={int(t) for t in meta.get("partial_ts", [])},
            partial_scanned=bool(meta.get("partial_scanned", False)),
        )
        series.dirty = n_bad > 0  # rewrite the file without the polluted rows
        return series

    # -------------- flush

    def flush_dirty(self) -> int:
        written = 0
        for series in self.store.all():
            if not series.dirty:
                continue
            try:
                self._write_one(series)
                series.dirty = False
                written += 1
            except Exception as e:
                logger.warning(
                    "could not flush %s %s %s: %s",
                    series.exchange,
                    series.pair,
                    series.timeframe,
                    e,
                )
        return written

    def _write_one(self, series: CandleSeries) -> None:
        if len(series.candles) == 0:
            return
        path = self._file_for(series)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")

        arr = series.candles
        df = pd.DataFrame(
            {
                # explicit ms unit: round-trips identically on pandas 2 and 3
                "date": pd.to_datetime(arr[:, 0].astype(np.int64), unit="ms", utc=True).as_unit(
                    "ms"
                ),
                "open": arr[:, 1],
                "high": arr[:, 2],
                "low": arr[:, 3],
                "close": arr[:, 4],
                "volume": arr[:, 5],
            }
        )
        df.reset_index(drop=True).loc[:, _COLUMNS].to_feather(
            tmp_path,
            compression_level=9,
            compression="lz4",
        )
        tmp_path.replace(path)

        meta = {
            "pair": series.pair,
            "candle_type": series.candle_type,
            "earliest_available_ts": series.earliest_available_ts,
            "last_live_refresh_wall_ms": series.last_live_refresh_wall_ms,
            "n_candles": len(arr),
            # Candles stored while still forming and not refetched yet.
            # partial_scanned is absent from files written before the field
            # existed: the daemon then scans the series once on first access.
            "partial_ts": sorted(series.partial_ts),
            "partial_scanned": series.partial_scanned,
        }
        meta_path = self._meta_file_for(series)
        meta_tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
        meta_tmp.write_text(json.dumps(meta))
        meta_tmp.replace(meta_path)
