"""
Tests for the position ISO guard and the three drift fixes around it.

Every scenario below is a replay of a real incident on the shared Hyperliquid wallet,
not a hypothetical: the numbers come from the forensic reconstruction of the orphans
and phantoms found on 2026-08-05.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from freqtrade.position_iso_guard import MODE_BLOCK, PositionIsoGuard


class FakeExchange:
    """Minimal exchange stub: a position book plus a snapshot clock we control."""

    def __init__(self, positions=None, wall_ts=1000.0):
        self._positions = positions or []
        self._wall_ts = wall_ts
        self.authoritative_calls = 0

    def fetch_positions(self, pair=None, params=None):
        if pair is None:
            return self._positions
        return [p for p in self._positions if p["symbol"] == pair]

    def fetch_positions_authoritative(self, pair=None):
        self.authoritative_calls += 1
        self._wall_ts += 100.0  # an authoritative read is by definition current
        return self.fetch_positions(pair)

    def positions_snapshot_wall_ts(self):
        return self._wall_ts

    def set_position(self, symbol, contracts, side):
        self._positions = [p for p in self._positions if p["symbol"] != symbol]
        if contracts:
            self._positions.append({"symbol": symbol, "contracts": contracts, "side": side})


def make_guard(exchange, mode="warn"):
    return PositionIsoGuard({"bot_name": "test", "position_iso_guard": {"mode": mode}}, exchange)


# --------------------------------------------------------------------- the invariant


def test_matching_fill_raises_no_breach():
    ex = FakeExchange()
    guard = make_guard(ex)
    guard.before_order("ETH/USDC:USDC", "sell", 10, is_entry=True)
    ex.set_position("ETH/USDC:USDC", 10, "short")
    assert guard.after_order("ETH/USDC:USDC", -10) is None


def test_position_that_did_not_move_is_a_breach():
    """The wallet ignored our fill — the classic precursor to a phantom book."""
    ex = FakeExchange()
    guard = make_guard(ex)
    guard.before_order("ETH/USDC:USDC", "sell", 10, is_entry=True)
    # position stays flat although we were filled for 10
    breach = guard.after_order("ETH/USDC:USDC", -10)
    assert breach is not None
    assert breach.expected == pytest.approx(-10)
    assert breach.observed == pytest.approx(0)


def test_breach_is_confirmed_by_an_authoritative_read_before_being_reported():
    """A cached snapshot must never be enough to accuse: siblings share this wallet."""
    ex = FakeExchange()
    guard = make_guard(ex)
    guard.before_order("ETH/USDC:USDC", "sell", 10, is_entry=True)
    guard.after_order("ETH/USDC:USDC", -10)
    assert ex.authoritative_calls >= 1


def test_rounding_noise_is_not_a_breach():
    ex = FakeExchange()
    guard = make_guard(ex)
    guard.before_order("ETH/USDC:USDC", "sell", 100, is_entry=True)
    ex.set_position("ETH/USDC:USDC", 99.9, "short")  # 0.1% off, well inside tolerance
    assert guard.after_order("ETH/USDC:USDC", -100) is None


def test_sibling_leg_on_the_shared_wallet_is_not_a_breach():
    """The wallet legitimately holds MORE than our book: netting, not drift."""
    ex = FakeExchange([{"symbol": "ETH/USDC:USDC", "contracts": 50, "side": "short"}])
    guard = make_guard(ex)
    guard.before_order("ETH/USDC:USDC", "sell", 10, is_entry=True)
    ex.set_position("ETH/USDC:USDC", 60, "short")
    assert guard.after_order("ETH/USDC:USDC", -10) is None


# --------------------------------------------------------------------- book check


def test_book_claiming_more_than_the_wallet_is_a_breach():
    """The EIGEN phantom: two bots each claimed 1296.46 of a 1380.44 position."""
    ex = FakeExchange([{"symbol": "EIGEN/USDC:USDC", "contracts": 1380.44, "side": "short"}])
    guard = make_guard(ex)
    breach = guard.check_book("EIGEN/USDC:USDC", -2592.92)
    assert breach is not None
    assert breach.delta > 0


def test_book_smaller_than_the_wallet_is_fine():
    ex = FakeExchange([{"symbol": "EIGEN/USDC:USDC", "contracts": 1380.44, "side": "short"}])
    guard = make_guard(ex)
    assert guard.check_book("EIGEN/USDC:USDC", -1296.46) is None


# --------------------------------------------------------------------- safety properties


def test_exits_are_never_blocked():
    ex = FakeExchange()
    guard = make_guard(ex, mode=MODE_BLOCK)
    guard._breaches.append(
        SimpleNamespace(pair="ETH/USDC:USDC", describe=lambda: "x")  # type: ignore[arg-type]
    )
    assert guard.before_order("ETH/USDC:USDC", "buy", 10, is_entry=False) is True


def test_block_mode_gates_entries_on_a_pair_with_an_open_breach():
    ex = FakeExchange()
    guard = make_guard(ex, mode=MODE_BLOCK)
    guard.before_order("ETH/USDC:USDC", "sell", 10, is_entry=True)
    guard.after_order("ETH/USDC:USDC", -10)  # creates a breach (position did not move)
    assert guard.before_order("ETH/USDC:USDC", "sell", 10, is_entry=True) is False


def test_warn_mode_never_gates_entries():
    ex = FakeExchange()
    guard = make_guard(ex, mode="warn")
    guard.before_order("ETH/USDC:USDC", "sell", 10, is_entry=True)
    guard.after_order("ETH/USDC:USDC", -10)
    assert guard.before_order("ETH/USDC:USDC", "sell", 10, is_entry=True) is True


def test_a_broken_exchange_never_breaks_the_trading_loop():
    broken = MagicMock()
    broken.fetch_positions.side_effect = RuntimeError("exchange down")
    guard = make_guard(broken)
    assert guard.before_order("ETH/USDC:USDC", "sell", 10, is_entry=True) is True
    assert guard.after_order("ETH/USDC:USDC", -10) is None
    assert guard.check_book("ETH/USDC:USDC", -10) is None


def test_disabled_guard_is_inert():
    ex = FakeExchange()
    guard = make_guard(ex, mode="off")
    assert guard.before_order("ETH/USDC:USDC", "sell", 10, is_entry=True) is True
    assert guard.after_order("ETH/USDC:USDC", -10) is None
    assert ex.authoritative_calls == 0


# --------------------------------------------------------- fix 1: temporal invariant


class _BotStub:
    """Just enough FreqtradeBot to exercise _positions_view_covers_fill."""

    from freqtrade.freqtradebot import FreqtradeBot

    _positions_view_covers_fill = FreqtradeBot._positions_view_covers_fill
    _POSITIONS_COVERAGE_MARGIN_S = FreqtradeBot._POSITIONS_COVERAGE_MARGIN_S

    def __init__(self, exchange):
        self.exchange = exchange


def _trade_filled_at(dt):
    return SimpleNamespace(pair="ETH/USDC:USDC", date_last_filled_utc=dt)


def test_snapshot_older_than_the_fill_is_refused():
    """The orphan factory: a snapshot from before our fill shows no position."""
    now = datetime.now(UTC)
    ex = FakeExchange(wall_ts=(now - timedelta(seconds=30)).timestamp())
    ex.fetch_positions_authoritative = MagicMock(side_effect=RuntimeError("no fresh read"))
    bot = _BotStub(ex)
    assert bot._positions_view_covers_fill(_trade_filled_at(now)) is False


def test_snapshot_newer_than_the_fill_is_accepted():
    now = datetime.now(UTC)
    ex = FakeExchange(wall_ts=(now + timedelta(seconds=30)).timestamp())
    bot = _BotStub(ex)
    assert bot._positions_view_covers_fill(_trade_filled_at(now)) is True


def test_a_stale_snapshot_is_rescued_by_an_authoritative_read():
    now = datetime.now(UTC)
    ex = FakeExchange(wall_ts=(now - timedelta(seconds=30)).timestamp())
    # the authoritative read jumps the clock forward past the fill
    ex._wall_ts = (now - timedelta(seconds=30)).timestamp()

    def _fresh(pair=None):
        ex._wall_ts = (now + timedelta(seconds=10)).timestamp()
        return []

    ex.fetch_positions_authoritative = _fresh
    bot = _BotStub(ex)
    assert bot._positions_view_covers_fill(_trade_filled_at(now)) is True


def test_exchange_without_the_shared_cache_is_unaffected():
    """Single-account exchanges read live: the guard must not change their behaviour."""
    bot = _BotStub(SimpleNamespace())
    assert bot._positions_view_covers_fill(_trade_filled_at(datetime.now(UTC))) is True


# ------------------------------------------- the two holes production found (2026-08-05)


class _BotStub2:
    """FreqtradeBot slice for _position_confirmed_absent."""

    from freqtrade.freqtradebot import FreqtradeBot

    _position_confirmed_absent = FreqtradeBot._position_confirmed_absent

    def __init__(self, exchange):
        self.exchange = exchange


def test_absence_must_be_confirmed_against_the_exchange():
    """A wallet reading of 0 is not proof: the position may simply not be in it yet."""
    ex = FakeExchange([{"symbol": "INIT/USDC:USDC", "contracts": 2890, "side": "short"}])
    bot = _BotStub2(ex)
    assert (
        bot._position_confirmed_absent(
            _trade_filled_at(datetime.now(UTC)) if False else SimpleNamespace(pair="INIT/USDC:USDC")
        )
        is False
    )


def test_absence_confirmed_when_the_exchange_really_shows_nothing():
    ex = FakeExchange([])
    bot = _BotStub2(ex)
    assert bot._position_confirmed_absent(SimpleNamespace(pair="INIT/USDC:USDC")) is True


def test_unreadable_exchange_blocks_the_close_rather_than_allowing_it():
    broken = MagicMock()
    broken.fetch_positions_authoritative.side_effect = RuntimeError("down")
    bot = _BotStub2(broken)
    assert bot._position_confirmed_absent(SimpleNamespace(pair="INIT/USDC:USDC")) is False


def test_cached_hit_is_stamped_at_capture_time_not_arrival():
    """A 15s-old daemon hit delivered instantly must not read as fresh."""
    import time as _t

    from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin

    class M(CachedExchangeMixin):
        def __init__(self):
            self._ftcache_last_positions = None
            self._ftcache_last_positions_ts = 0.0
            self._ftcache_last_positions_wall = 0.0
            self._pos_last_fetched_at = 0.0

    m = M()
    before = _t.time()
    m._ftcache_save_positions([], captured_age_s=15.0)
    assert m.positions_snapshot_wall_ts() <= before - 14.0


# --------------------------------------- the adoption guard must not depend on discovery


def test_adoption_guard_does_not_consult_sibling_discovery():
    """Regression: gating on "are there siblings?" disarmed the guard in production.

    Fleet discovery swallows its own errors and returns an empty list, so a transient
    failure looked exactly like a lone bot and let a sibling's order be adopted (NIL,
    2026-08-06 05:21). The exchange capability must be the only precondition.
    """
    import inspect

    from freqtrade.freqtradebot import FreqtradeBot

    src = inspect.getsource(FreqtradeBot.handle_onexchange_order)
    guard = src.split("orders_are_account_scoped")[1].split("continue")[0]
    assert "shares_account" not in guard


def test_exit_clamp_does_not_consult_sibling_discovery():
    import inspect

    from freqtrade.freqtradebot import FreqtradeBot

    src = inspect.getsource(FreqtradeBot._clamp_exit_to_wallet_position)
    assert "shares_account" not in src


def test_discovery_failure_is_not_reported_as_an_empty_fleet():
    from freqtrade.fleet_coordination import PositionCoordinator

    coord = PositionCoordinator(
        {"bot_name": "x", "exchange": {"name": "hyperliquid"}, "user_data_dir": "/tmp"}
    )

    class _Registry:
        last_discovery_failed = True

        def siblings(self):
            return []

    coord._registry = _Registry()
    assert coord.shares_account() is True, "a failed lookup must not read as 'no siblings'"
    _Registry.last_discovery_failed = False
    assert coord.shares_account() is False
