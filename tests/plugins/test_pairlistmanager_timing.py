"""A slow pairlist refresh says which handler spent the time (priority-bots L4, measure first)."""

from unittest.mock import MagicMock, PropertyMock

import pytest

from freqtrade.plugins.pairlistmanager import PairListManager
from tests.conftest import EXMS, get_patched_freqtradebot


@pytest.fixture
def manager(mocker, markets_static, default_conf):
    freqtrade = get_patched_freqtradebot(mocker, default_conf)
    mocker.patch(f"{EXMS}.markets", PropertyMock(return_value=markets_static))
    return PairListManager(freqtrade.exchange, default_conf, MagicMock())


def test_a_fast_refresh_stays_silent(manager, caplog):
    with caplog.at_level("INFO"):
        manager._log_slow_refresh([("StaticPairList", 0.2)], 0.2)
    assert "slow pairlist refresh" not in caplog.text
    assert manager._last_refresh_timings == [("StaticPairList", 0.2)]


def test_a_slow_refresh_names_the_handlers_that_took_the_time(manager, caplog):
    timings = [
        ("VolumePairList", 2.0),
        ("PerformanceFilter", 0.01),
        ("TrendRegularityFilter", 410.4),
    ]
    with caplog.at_level("INFO"):
        manager._log_slow_refresh(timings, 432.0)
    line = next(r.message for r in caplog.records if "slow pairlist refresh" in r.message)
    assert "432.0s" in line
    assert "TrendRegularityFilter 410.4s" in line and "VolumePairList 2.0s" in line
    assert "PerformanceFilter" not in line, "handlers under half a second are noise"


def test_refresh_pairlist_records_one_timing_per_handler(manager):
    manager.refresh_pairlist()
    names = [name for name, _ in manager._last_refresh_timings]
    assert names == [h.name for h in manager._pairlist_handlers]
    assert all(secs >= 0.0 for _, secs in manager._last_refresh_timings)
