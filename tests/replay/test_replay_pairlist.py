"""The replay must be able to reproduce the live pairlist chain — causally.

A live bot does not trade a fixed list: it trades whatever its VolumePairList /
PerformanceFilter chain selects that day, and that selection is part of the edge.
dynv1 live trades ~40 rotating top-volume pairs; a static replay of its 199 candidates
is a different strategy wearing the same name. A backtest cannot close this gap at all
(freqtrade evaluates pairlists once, at start, and refuses VolumePairList outright);
the replay can, because it drives the real live loop.

The property that makes it legitimate is causality: tickers are synthesised from the
local candle store, which only returns candles whose CLOSE has passed the virtual
clock. A pair can therefore never be selected on volume it has not yet traded.
"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from freqtrade.enums import CandleType
from freqtrade.replay.exchange import ReplayExchangeMixin


class _Clock:
    def __init__(self, t):
        self._t = t

    def now(self):
        return self._t


class _Store:
    """Candles that are flat until a volume spike at 12:00."""

    def __init__(self):
        base = datetime(2026, 1, 1)
        rows = []
        for i in range(48):
            ts = base + timedelta(hours=i)
            vol = 1_000_000.0 if ts.hour == 12 else 10.0
            rows.append({"date": pd.Timestamp(ts), "close": 2.0, "volume": vol})
        self.df = pd.DataFrame(rows)

    def get_candles(self, pair, tf, candle_type, up_to, max_candles=None, **kw):
        if tf != "1h":
            raise KeyError(tf)
        cut = self.df[self.df["date"] <= pd.Timestamp(up_to) - pd.Timedelta(hours=1)]
        return cut.tail(max_candles) if max_candles else cut


def _mixin(clock):
    m = ReplayExchangeMixin.__new__(ReplayExchangeMixin)
    m._replay_store = _Store()
    m._replay_clock = clock
    m._replay_candle_type = CandleType.FUTURES
    m._markets = {"AAA/USDC:USDC": {}}
    return m


def test_tickers_never_see_volume_from_the_future():
    """The core guarantee: no look-ahead in pair selection."""
    before = _mixin(_Clock(datetime(2026, 1, 1, 11))).get_tickers()
    after = _mixin(_Clock(datetime(2026, 1, 2, 0))).get_tickers()
    assert before["AAA/USDC:USDC"]["quoteVolume"] < 1000, (
        "the 12:00 spike must be invisible at 11:00"
    )
    assert after["AAA/USDC:USDC"]["quoteVolume"] > 1_000_000, "and visible once it has closed"


def test_tickers_carry_what_volumepairlist_reads():
    t = _mixin(_Clock(datetime(2026, 1, 2))).get_tickers()["AAA/USDC:USDC"]
    for key in ("symbol", "last", "quoteVolume", "baseVolume"):
        assert key in t
    assert t["quoteVolume"] == pytest.approx(t["baseVolume"] * t["last"])


def test_a_pair_without_candles_is_absent_not_zero():
    """Absent beats a fabricated zero: a zero-volume ticker would rank the pair last
    rather than exclude it, quietly admitting pairs the venue could not price."""
    m = _mixin(_Clock(datetime(2026, 1, 2)))
    m._markets = {"AAA/USDC:USDC": {}, "GHOST/USDC:USDC": {}}

    class _Empty(_Store):
        def get_candles(self, pair, *a, **kw):
            if pair == "GHOST/USDC:USDC":
                return pd.DataFrame(columns=["date", "close", "volume"])
            return super().get_candles(pair, *a, **kw)

    m._replay_store = _Empty()
    assert "GHOST/USDC:USDC" not in m.get_tickers()


def test_static_pinning_is_still_the_default():
    """Opt-in by design: pinning --pairs stays right when the question is 'how does
    this strategy behave on THESE pairs'."""
    import inspect

    from freqtrade.replay import runner

    assert inspect.signature(runner.run_replay).parameters["dynamic_pairlist"].default is False
    src = inspect.getsource(runner.run_replay)
    assert "if dynamic_pairlist:" in src
    assert 'config["pairlists"] = [{"method": "StaticPairList"}]' in src
