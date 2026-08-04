from datetime import timedelta

from freqtrade.persistence import ProfitHistory, init_db
from freqtrade.util import dt_now


def test_profit_history_record_get_prune():
    init_db("sqlite://")
    ProfitHistory.record(120.5, -33.2, 4)
    ProfitHistory.record(121.0, -20.0, 3)

    rows = ProfitHistory.get_since()
    assert [(r.profit_closed_abs, r.profit_open_abs, r.open_trades) for r in rows] == [
        (120.5, -33.2, 4),
        (121.0, -20.0, 3),
    ]
    # Ascending order and recent timestamps
    assert rows[0].timestamp <= rows[1].timestamp

    # since filter (tz-aware input against tz-naive storage)
    assert ProfitHistory.get_since(dt_now() + timedelta(hours=1)) == []
    assert len(ProfitHistory.get_since(dt_now() - timedelta(hours=1))) == 2

    # prune (tz-aware cutoff)
    assert ProfitHistory.prune_older_than(dt_now() - timedelta(days=1)) == 0
    assert ProfitHistory.prune_older_than(dt_now() + timedelta(days=1)) == 2
    assert ProfitHistory.get_since() == []
