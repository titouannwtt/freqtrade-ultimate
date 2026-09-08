"""
Fork guard: the trading stack must stay UTC-correct even when the *server*
timezone is not UTC (the host was switched from UTC to Europe/Paris on
2026-09-08, a +2h offset in CEST).

Two classes of regression are covered:

1. Naive ``datetime.now()`` reaching a persisted date column. Dates are stored
   tz-naive and read back with ``.replace(tzinfo=UTC)``, so a value written in
   local time is re-interpreted as UTC and lands 2h in the *future*. Trade
   duration then goes negative, and ``min_roi_reached`` silently stops firing.

2. Naive ``datetime.now()`` / ``date.today()`` creeping back into the modules
   that drive live trading decisions.
"""

import ast
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freqtrade.persistence import Trade, init_db
from freqtrade.util import dt_now


@pytest.fixture
def tz_paris():
    """Run the body with the *process* timezone set to Europe/Paris (UTC+2 in CEST)."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Paris"
    time.tzset()
    yield
    if previous is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = previous
    time.tzset()


def test_tz_fixture_actually_shifts_the_clock(tz_paris):
    """Sanity check: without this, the tests below would pass vacuously."""
    offset = datetime.now() - datetime.now(UTC).replace(tzinfo=None)
    assert offset.total_seconds() > 3000, "TZ override had no effect on this platform"


def test_trade_open_date_default_is_utc_under_local_timezone(tz_paris):
    """
    A Trade flushed without an explicit ``open_date`` must still be stamped in
    UTC. Regression: ``default=datetime.now`` wrote local time, which made
    ``open_date_utc`` 2h in the future.
    """
    init_db("sqlite://")
    trade = Trade(
        pair="ADA/USDT",
        stake_amount=100.0,
        amount=100.0,
        fee_open=0.0,
        fee_close=0.0,
        open_rate=1.0,
        exchange="binance",
        is_open=True,
        # open_date deliberately omitted -> exercises the column default
    )
    Trade.session.add(trade)
    Trade.session.commit()

    drift = abs((trade.open_date_utc - dt_now()).total_seconds())
    assert drift < 60, (
        f"open_date default drifted by {drift}s from UTC "
        "-> the column default is writing server-local time"
    )


def test_trade_duration_is_not_negative_under_local_timezone(tz_paris):
    """
    The concrete damage of a local-time ``open_date``: ``min_roi_reached``
    computes ``current_time - open_date_utc``. A +2h drift yields a negative
    duration, no ROI tier matches, and the ROI exit is disabled for 2 hours.
    """
    init_db("sqlite://")
    trade = Trade(
        pair="ADA/USDT",
        stake_amount=100.0,
        amount=100.0,
        fee_open=0.0,
        fee_close=0.0,
        open_rate=1.0,
        exchange="binance",
        is_open=True,
    )
    Trade.session.add(trade)
    Trade.session.commit()

    trade_dur = int((dt_now().timestamp() - trade.open_date_utc.timestamp()) // 60)
    assert trade_dur >= 0, (
        f"trade duration is {trade_dur} minutes -> ROI table would never match "
        "and the ROI exit would be silently disabled"
    )


# --- Static guard -----------------------------------------------------------

# Modules whose output feeds a trading decision or a persisted date.
_LIVE_CRITICAL = (
    "freqtrade/persistence",
    "freqtrade/ohlcv_cache",
    "freqtrade/pairlist_cache",
    "freqtrade/plugins/pairlist",
    "freqtrade/freqtradebot.py",
    "freqtrade/worker.py",
    "freqtrade/wallets.py",
    "freqtrade/fleet_coordination.py",
    "freqtrade/data/dataprovider.py",
    "freqtrade/strategy/interface.py",
)

# (relative path, line) pairs that are reviewed and provably tz-safe.
_ALLOWED: set[tuple[str, int]] = set()


def _naive_now_calls(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, description) for naive datetime.now()/date.today() calls."""
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        base = node.func.value
        base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if attr == "utcnow":
            found.append((node.lineno, "datetime.utcnow()"))
        elif attr == "today" and base_name in ("date", "datetime"):
            found.append((node.lineno, f"{base_name}.today()"))
        elif attr == "now" and base_name in ("datetime", "pd", "Timestamp"):
            # naive only when called with no tz argument at all
            if not node.args and not node.keywords:
                found.append((node.lineno, f"{base_name}.now()"))
    return found


def _naive_column_defaults(path: Path) -> list[tuple[int, str]]:
    """
    Return (lineno, description) for SQLAlchemy ``default=``/``onupdate=`` that
    reference a naive clock *callable* (e.g. ``default=datetime.now``). These are
    references, not calls, so ``_naive_now_calls`` cannot see them - yet they are
    the most dangerous form: the naive value is written straight into a date
    column that is later read back as if it were UTC.
    """
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg not in ("default", "onupdate"):
                continue
            val = kw.value
            if isinstance(val, ast.Attribute) and val.attr in ("now", "utcnow", "today"):
                base = val.value
                base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                if base_name in ("datetime", "date", "pd", "Timestamp"):
                    found.append((node.lineno, f"{kw.arg}={base_name}.{val.attr}"))
    return found


def test_no_naive_clock_in_live_critical_modules():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for target in _LIVE_CRITICAL:
        p = root / target
        files = sorted(p.rglob("*.py")) if p.is_dir() else [p]
        for f in files:
            rel = f.relative_to(root).as_posix()
            for lineno, what in _naive_now_calls(f) + _naive_column_defaults(f):
                if (rel, lineno) not in _ALLOWED:
                    offenders.append(f"{rel}:{lineno}: {what}")
    assert not offenders, (
        "Naive (server-local) clock reads in live-critical modules. Use "
        "freqtrade.util.dt_now() / datetime.now(UTC):\n  " + "\n  ".join(offenders)
    )


# --- Log clock --------------------------------------------------------------


def test_log_timestamps_are_utc_under_local_timezone(tz_paris):
    """
    Log lines must stay in UTC so they remain comparable with the (UTC) trade
    dates in the database, and so a restart does not mix Paris-stamped lines into
    log files that already contain UTC-stamped ones.
    """
    import logging

    import freqtrade.loggers  # noqa: F401  (import pins Formatter.converter)

    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="x", args=(), exc_info=None
    )
    rendered = logging.Formatter("%(asctime)s").format(record)
    expected = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    assert rendered.startswith(expected), (
        f"log timestamp {rendered!r} is not UTC (expected {expected!r}) "
        "-> Formatter.converter fell back to server-local time"
    )


def test_ftcache_daemon_formatter_is_utc(tz_paris):
    """The ftcache daemon runs in its own process and must pin UTC itself."""
    import logging

    from freqtrade.ohlcv_cache.logger_setup import _utc_formatter

    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="x", args=(), exc_info=None
    )
    rendered = _utc_formatter("%(asctime)s").format(record)
    expected = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    assert rendered.startswith(expected)
