"""The positions freshness circuit breaker, on an ENTRY, must wait rather than refuse.

Context (2026-09-08). `_positions_circuit_open` refuses a new entry whenever the
mixin-side positions cache has aged past `positions_hard_stale_s` (90s). On
hippo_original_multi — a 5m strategy with `process_only_new_candles` — that refusal
is not a delay: the signal lives on one candle and a 90s block covers it whole.
Seven entries were dropped that way between the 5th and the 8th of September, on
cache ages of 90 to 270 seconds.

The fix waits (bounded) for a fresh view, and only if that fails falls back to the
one permission that stays safe on a netted wallet: enter blind only when nothing we
know of holds the coin. Everything here pins that behaviour, plus the two things
that must NOT change — the default of 0 for the other 26 bots, and the fabricated
external close, which keeps the original circuit breaker.
"""

import inspect

import pytest

from freqtrade.freqtradebot import FreqtradeBot


class _Exchange:
    """Minimal stand-in for the ohlcv_cache mixin's positions surface."""

    def __init__(self, *, trustworthy, age=120.0, becomes_fresh_after=None, snapshot=None):
        self._trustworthy = trustworthy
        self._age = age
        self._becomes_fresh_after = becomes_fresh_after
        self._snapshot = snapshot
        self.refresh_requests = 0
        self.waits = []

    def positions_are_trustworthy(self):
        return self._trustworthy, self._age

    def request_positions_refresh(self):
        self.refresh_requests += 1

    def wait_for_trustworthy_positions(self, timeout_s):
        self.waits.append(timeout_s)
        if self._becomes_fresh_after is not None and self._becomes_fresh_after <= timeout_s:
            self._trustworthy = True
            self._age = 2.0
            return True, 2.0
        return False, self._age

    def last_known_positions(self):
        if self._snapshot is None:
            return None, float("inf")
        return self._snapshot, 130.0


class _Coordinator:
    def __init__(self, siblings=None):
        self._siblings = siblings or []

    def sibling_snapshot(self, pair):
        return list(self._siblings)


def _bot(exchange, *, wait_s=8, siblings=None, capital=6144.15, ratio=0.35, open_trades=()):
    bot = FreqtradeBot.__new__(FreqtradeBot)
    bot.config = {
        "shared_ohlcv_cache": {"positions_wait_on_entry_s": wait_s},
        "available_capital": capital,
        "max_position_stake_ratio": ratio,
    }
    bot.exchange = exchange
    bot._coordinator = _Coordinator(siblings)
    bot._open_trades_stub = list(open_trades)
    return bot


@pytest.fixture
def no_db(monkeypatch):
    """`_known_position_on_coin` reads the trade DB; give it an empty book by default."""
    from freqtrade.persistence import Trade

    monkeypatch.setattr(Trade, "get_trades_proxy", staticmethod(lambda **kw: []))
    return Trade


def test_a_fresh_view_during_the_wait_lets_the_entry_through(no_db, caplog):
    """The nominal win: the refresher lands a fresh copy inside the budget."""
    ex = _Exchange(trustworthy=False, age=120.0, becomes_fresh_after=8)
    bot = _bot(ex)
    assert bot._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC") is False
    assert ex.waits == [8.0], "the entry must actually wait, not just nudge"
    assert "proceeds on real data" in caplog.text
    assert bot._blind_entry_pairs == set(), "a fresh entry is not a blind one"


def test_the_fallback_allows_an_entry_when_nothing_known_holds_the_coin(no_db, caplog):
    ex = _Exchange(
        trustworthy=False,
        age=139.0,
        snapshot=[{"symbol": "ETH/USDC:USDC", "contracts": 3.0}],
    )
    bot = _bot(ex)
    assert bot._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC") is False
    assert "ALLOWED blind" in caplog.text
    assert bot._blind_entry_pairs == {"BTC/USDC:USDC"}, "the entry must be flagged for the cap"


def test_the_fallback_refuses_when_the_wallet_snapshot_shows_the_coin(no_db, caplog):
    """Blind on a coin we already hold is exactly the netting the breaker exists to stop."""
    ex = _Exchange(
        trustworthy=False,
        age=139.0,
        snapshot=[{"symbol": "BTC/USDC:USDC", "contracts": 0.4}],
    )
    bot = _bot(ex)
    assert bot._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC") is True
    assert "REFUSED" in caplog.text
    assert bot._blind_entry_pairs == set()


def test_the_fallback_refuses_on_an_open_trade_in_our_own_book(monkeypatch, caplog):
    from freqtrade.persistence import Trade

    monkeypatch.setattr(Trade, "get_trades_proxy", staticmethod(lambda **kw: [object()]))
    ex = _Exchange(trustworthy=False, age=100.0, snapshot=[])
    bot = _bot(ex)
    assert bot._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC") is True
    assert "our own book" in caplog.text


def test_the_fallback_refuses_when_a_sibling_bot_holds_the_coin(no_db, caplog):
    """On a shared netted wallet a sibling's position is our position too."""
    ex = _Exchange(trustworthy=False, age=100.0, snapshot=[])
    bot = _bot(ex, siblings=[{"bot": "hyperliquid_es146", "side": "long"}])
    assert bot._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC") is True
    assert "sibling bot holds this coin" in caplog.text


def test_an_unreadable_source_reads_as_a_position_never_as_flat(no_db):
    """ "We could not look" must never be mistaken for "nothing is there"."""
    ex = _Exchange(trustworthy=False, age=100.0, snapshot=None)  # no snapshot ever taken
    bot = _bot(ex)
    assert bot._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC") is True

    class _Broken(_Exchange):
        def last_known_positions(self):
            raise RuntimeError("boom")

    bot2 = _bot(_Broken(trustworthy=False, age=100.0))
    assert bot2._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC") is True


# --------------------------------------------------------------- the other 26 bots


def test_the_default_of_zero_keeps_the_original_refusal(caplog):
    """No config key -> the exact legacy path: warn, nudge, refuse for this cycle."""
    ex = _Exchange(trustworthy=False, age=120.0, becomes_fresh_after=1)
    bot = FreqtradeBot.__new__(FreqtradeBot)
    bot.config = {}
    bot.exchange = ex
    assert bot._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC") is True
    assert ex.waits == [], "a bot at the default must never wait"
    assert ex.refresh_requests == 1, "it must still nudge the refresher"
    assert "blocked until fresh" in caplog.text


def test_an_explicit_zero_and_a_garbage_value_both_mean_the_legacy_path():
    for value in (0, "0", None, "nonsense"):
        ex = _Exchange(trustworthy=False, age=120.0, becomes_fresh_after=1)
        bot = _bot(ex, wait_s=value)
        assert bot._positions_circuit_open("entry X/USDC:USDC", entry_pair="X/USDC:USDC") is True
        assert ex.waits == []


def test_the_wait_is_hard_capped_at_ten_seconds():
    """A config typo must never park the trading loop."""
    ex = _Exchange(trustworthy=False, age=120.0)
    bot = _bot(ex, wait_s=600)
    bot._positions_circuit_open("entry X/USDC:USDC", entry_pair="X/USDC:USDC")
    assert ex.waits == [10.0]


def test_a_trustworthy_view_never_waits_at_all():
    ex = _Exchange(trustworthy=True, age=3.0)
    bot = _bot(ex)
    assert bot._positions_circuit_open("entry X/USDC:USDC", entry_pair="X/USDC:USDC") is False
    assert ex.waits == [] and ex.refresh_requests == 0


# --------------------------------------------------------------- external close


def test_the_fabricated_external_close_keeps_the_original_breaker(caplog):
    """No `entry_pair` -> no wait, no fallback, whatever the config says."""
    ex = _Exchange(trustworthy=False, age=139.0, becomes_fresh_after=1, snapshot=[])
    bot = _bot(ex)
    assert bot._positions_circuit_open("external close BTC/USDC:USDC") is True
    assert ex.waits == [], "an external close must not be talked into freshness"
    assert "blocked until fresh" in caplog.text


def test_the_external_close_call_site_passes_no_entry_pair():
    src = inspect.getsource(FreqtradeBot._handle_external_close)
    assert '_positions_circuit_open(f"external close {trade.pair}")' in src
    assert "entry_pair" not in src


def test_the_entry_call_site_opts_in():
    src = inspect.getsource(FreqtradeBot.create_trade)
    assert "entry_pair=pair" in src
    assert "blind_entry" in src, "create_trade must consume the blind-entry marker"


# --------------------------------------------------------------- capital envelope


def test_a_blind_entry_is_capped_to_the_capital_envelope():
    """The envelope guard only sees DCA reinforcements; a blind FIRST entry can net
    onto an unseen position, so it carries the same ceiling up front."""
    bot = _bot(_Exchange(trustworthy=True))
    ceiling = 6144.15 * 0.35
    assert bot._cap_stake_to_envelope("BTC/USDC:USDC", 3072.0) == pytest.approx(ceiling)
    assert bot._cap_stake_to_envelope("BTC/USDC:USDC", 500.0) == 500.0


def test_the_cap_is_disabled_with_the_envelope_itself():
    bot = _bot(_Exchange(trustworthy=True), ratio=0)
    assert bot._cap_stake_to_envelope("BTC/USDC:USDC", 99999.0) == 99999.0


def test_the_envelope_guard_reads_no_position_data():
    """Pinned: stale positions cannot loosen the DCA envelope, because it never
    looks at positions at all — only the DB stake and the configured allocation."""
    src = inspect.getsource(FreqtradeBot._position_within_capital_envelope)
    for forbidden in ("fetch_positions", "last_known_positions", "positions_are_trustworthy"):
        assert forbidden not in src


def test_the_blind_marker_never_leaks_into_a_later_cycle(no_db):
    ex = _Exchange(trustworthy=False, age=139.0, snapshot=[])
    bot = _bot(ex)
    bot._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC")
    assert bot._blind_entry_pairs == {"BTC/USDC:USDC"}
    # create_trade consumes it unconditionally, signal or not.
    marked = "BTC/USDC:USDC" in bot._blind_entry_pairs
    bot._blind_entry_pairs.discard("BTC/USDC:USDC")
    assert marked and bot._blind_entry_pairs == set()


# --------------------------------------------------------------- the mixin-side wait


def _mixin(**attrs):
    """A bare CachedExchangeMixin carrying only the positions-refresher state."""
    from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin

    m = CachedExchangeMixin.__new__(CachedExchangeMixin)
    m._pos_refresher_active = True
    m._pos_hard_stale = 90.0
    m._pos_force_event = None
    m._ftcache_last_positions = None
    m._ftcache_last_positions_ts = 0.0
    m._POS_WAIT_POLL_S = 0.01
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def test_the_wait_returns_at_once_when_positions_are_already_fresh():
    import time as _t

    m = _mixin(_ftcache_last_positions_ts=_t.monotonic())
    started = _t.monotonic()
    ok, _age = m.wait_for_trustworthy_positions(10)
    assert ok is True and _t.monotonic() - started < 0.5


def test_the_wait_is_a_no_op_when_the_refresher_is_inactive():
    m = _mixin(_pos_refresher_active=False)
    assert m.wait_for_trustworthy_positions(10) == (True, 0.0)


def test_the_wait_polls_until_the_refresher_lands_a_copy(monkeypatch):
    """The whole point: `request_positions_refresh` is a nudge, this waits for it."""
    import time as _t

    m = _mixin(_ftcache_last_positions_ts=_t.monotonic() - 200.0)
    calls = {"n": 0}

    def _nudge():
        calls["n"] += 1

    m.request_positions_refresh = _nudge
    m._positions_sync_daemon_read = lambda: False
    m._ftcache_bump = lambda *_a, **_k: None

    real_trustworthy = m.positions_are_trustworthy
    state = {"i": 0}

    def _fake():
        state["i"] += 1
        if state["i"] > 3:  # the background thread "lands" a fresh copy
            m._ftcache_last_positions_ts = _t.monotonic()
        return real_trustworthy()

    m.positions_are_trustworthy = _fake
    ok, _age = m.wait_for_trustworthy_positions(5)
    assert ok is True
    assert calls["n"] == 1, "one nudge, then wait — never a nudge storm"


def test_the_wait_gives_up_inside_its_budget():
    import time as _t

    m = _mixin(_ftcache_last_positions_ts=_t.monotonic() - 200.0)
    m.request_positions_refresh = lambda: None
    m._positions_sync_daemon_read = lambda: False
    m._ftcache_bump = lambda *_a, **_k: None
    started = _t.monotonic()
    ok, age = m.wait_for_trustworthy_positions(0.3)
    elapsed = _t.monotonic() - started
    assert ok is False and age > 90
    assert elapsed < 2.0, "the budget is a hard bound on the trading loop"


def test_the_synchronous_daemon_read_costs_no_exchange_call_and_never_raises():
    """It reads the daemon's shared copy over the unix socket — the cheapest fresh
    data available during a 429 storm — and any failure is just a False."""
    m = _mixin(_api=object())
    m._ftcache_bump = lambda *_a, **_k: None
    m._positions_daemon_read = lambda wallet: (_ for _ in ()).throw(OSError("no daemon"))
    assert m._positions_sync_daemon_read() is False

    m._positions_daemon_read = lambda wallet: (False, [])
    assert m._positions_sync_daemon_read() is False

    m._pos_last_fetched_at = 0.0
    m._positions_daemon_read = lambda wallet: (True, [{"symbol": "BTC/USDC:USDC"}])
    assert m._positions_sync_daemon_read() is True
    assert m.positions_are_trustworthy()[0] is True


def test_last_known_positions_reports_unknown_rather_than_flat():
    m = _mixin()
    assert m.last_known_positions() == (None, float("inf"))
    import time as _t

    m._ftcache_last_positions = [{"symbol": "BTC/USDC:USDC", "contracts": 1.0}]
    m._ftcache_last_positions_ts = _t.monotonic() - 200.0
    positions, age = m.last_known_positions()
    assert positions and age > 190, "age is reported, never used to hide the data"


def test_only_one_pair_per_cycle_pays_the_wait(no_db):
    """`enter_positions` walks the whole whitelist (60 crypto + 20 stocks + ... here).
    A fleet-wide stale view must cost one wait, not one per pair, or the loop parks
    for minutes — and the extra waits would buy nothing anyway."""
    ex = _Exchange(trustworthy=False, age=120.0, snapshot=[])
    bot = _bot(ex)
    for pair in ("BTC/USDC:USDC", "ETH/USDC:USDC", "SOL/USDC:USDC", "LINK/USDC:USDC"):
        bot._positions_circuit_open(f"entry {pair}", entry_pair=pair)
    assert ex.waits == [8.0], "one wait for the whole pass, not one per pair"
    # ... and the pairs that skipped the wait still get the fallback decision.
    assert bot._blind_entry_pairs == {
        "BTC/USDC:USDC",
        "ETH/USDC:USDC",
        "SOL/USDC:USDC",
        "LINK/USDC:USDC",
    }


def test_a_later_cycle_may_wait_again(no_db, monkeypatch):
    ex = _Exchange(trustworthy=False, age=120.0, snapshot=[])
    bot = _bot(ex)
    bot._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC")
    assert ex.waits == [8.0]
    bot._pos_wait_last_attempt -= 60.0  # a minute later: the next pass
    bot._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC")
    assert ex.waits == [8.0, 8.0]


def test_the_fallback_decision_does_not_flood_the_log(no_db, caplog):
    """The decision is taken once per whitelisted pair (80+ here), for as long as the
    view stays stale. One WARNING per outcome per minute; the rest are counted."""
    import logging

    caplog.set_level(logging.WARNING)
    ex = _Exchange(trustworthy=False, age=120.0, snapshot=[])
    bot = _bot(ex)
    for i in range(40):
        bot._positions_circuit_open(f"entry P{i}/USDC:USDC", entry_pair=f"P{i}/USDC:USDC")
    allowed = [r for r in caplog.records if "ALLOWED blind" in r.getMessage()]
    assert len(allowed) == 1, "40 pairs must not write 40 warnings"
    assert len(bot._blind_entry_pairs) == 40, "but every pair still gets its decision"


def test_both_outcomes_keep_their_own_log_budget(monkeypatch, caplog):
    """An allow must never hide a refusal behind the same throttle."""
    import logging

    from freqtrade.persistence import Trade

    caplog.set_level(logging.WARNING)
    ex = _Exchange(
        trustworthy=False, age=120.0, snapshot=[{"symbol": "BTC/USDC:USDC", "contracts": 1.0}]
    )
    monkeypatch.setattr(Trade, "get_trades_proxy", staticmethod(lambda **kw: []))
    bot = _bot(ex)
    bot._positions_circuit_open("entry ETH/USDC:USDC", entry_pair="ETH/USDC:USDC")  # allowed
    bot._positions_circuit_open("entry BTC/USDC:USDC", entry_pair="BTC/USDC:USDC")  # refused
    text = caplog.text
    assert "ALLOWED blind" in text and "REFUSED" in text
