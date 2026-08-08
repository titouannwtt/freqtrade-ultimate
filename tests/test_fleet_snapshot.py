"""Tests for the fleet snapshot: one request describing every bot."""

import time

import pytest


class FakeDaemon:
    """The two handlers under test, lifted out of the daemon's socket plumbing."""

    def __init__(self):
        from freqtrade.ohlcv_cache.daemon import Daemon

        self._summaries = {}
        self._put = Daemon._handle_summary_put.__get__(self)
        self._get = Daemon._handle_summary_get.__get__(self)

    def put(self, bot_id, data, req_id="t"):
        return self._put({"req_id": req_id, "bot_id": bot_id, "data": data}, 1)

    def get(self, **kw):
        return self._get({"req_id": "t", **kw})


def test_a_pushed_digest_comes_back():
    d = FakeDaemon()
    assert d.put("alpha", {"state": "running", "open_trade_count": 3})["ok"] is True
    snap = d.get()
    assert snap["bot_count"] == 1
    assert snap["bots"]["alpha"]["open_trade_count"] == 3


def test_every_entry_carries_its_age():
    """A client must be able to tell a live figure from one left by a stopped bot."""
    d = FakeDaemon()
    d.put("alpha", {"state": "running"})
    assert "age_s" in d.get()["bots"]["alpha"]


def test_a_bot_replaces_its_own_entry_rather_than_accumulating():
    d = FakeDaemon()
    d.put("alpha", {"open_trade_count": 1})
    d.put("alpha", {"open_trade_count": 2})
    snap = d.get()
    assert snap["bot_count"] == 1, "a restart must not leave a ghost entry"
    assert snap["bots"]["alpha"]["open_trade_count"] == 2


def test_stale_digests_can_be_filtered_out():
    d = FakeDaemon()
    d.put("alpha", {"state": "running"})
    d._summaries["alpha"]["ts"] = time.time() - 600
    assert d.get(max_age_s=60)["bot_count"] == 0
    assert d.get()["bot_count"] == 1, "no filter means no filtering"


def test_several_bots_are_returned_together():
    d = FakeDaemon()
    for i in range(5):
        d.put(f"bot{i}", {"open_trade_count": i})
    snap = d.get()
    assert snap["bot_count"] == 5
    assert sum(b["open_trade_count"] for b in snap["bots"].values()) == 10


@pytest.mark.parametrize("bad", [None, "", 0])
def test_a_digest_without_a_bot_id_is_rejected(bad):
    d = FakeDaemon()
    res = d._put({"req_id": "t", "bot_id": bad, "data": {}}, 1)
    assert res["ok"] is False


def test_a_non_object_payload_is_rejected():
    """Guards the snapshot against a malformed push corrupting the whole response."""
    d = FakeDaemon()
    assert d._put({"req_id": "t", "bot_id": "alpha", "data": ["nope"]}, 1)["ok"] is False


def test_the_digest_only_uses_data_the_bot_already_has():
    """Regression guard on the design: pushing must not reintroduce the cost it removes.

    The point of the digest is to keep expensive aggregates *out* of it — a digest that
    recomputed /profit-style figures every cycle would move the cost onto the trading
    loop instead of removing it.
    """
    import inspect

    from freqtrade.freqtradebot import FreqtradeBot

    src = inspect.getsource(FreqtradeBot._push_fleet_digest)
    for forbidden in ("_rpc_trade_statistics", "DataFrame", "calculate_max_drawdown"):
        assert forbidden not in src, f"{forbidden} does not belong in a per-cycle digest"


def test_open_profit_is_omitted_rather_than_guessed():
    """It needs live rates; absent beats a figure computed from stale ones."""
    import inspect

    from freqtrade.freqtradebot import FreqtradeBot

    src = inspect.getsource(FreqtradeBot._push_fleet_digest)
    assert "refresh=False" in src, "the digest must not trigger a rate fetch"
    assert "open_profit_abs" in src


# ------------------------------------------------------ /stats robustness to missing data


def test_stats_groups_trades_with_no_exit_reason_instead_of_dropping_them():
    """Regression: a null exit_reason became a null dict key and 500'd the endpoint.

    Observed on 6 of 24 live bots. A position can leave the book without the bot recording
    why (external close, a fill it never saw). Those trades must still be counted — silently
    dropping them would change the win/loss totals, which is worse than an ugly label.
    """
    import inspect

    from freqtrade.rpc.rpc import RPC

    src = inspect.getsource(RPC._rpc_stats)
    assert 'trade.exit_reason is not None else "unknown"' in src
    assert "exit_reasons[trade.exit_reason]" not in src, "the raw, nullable key must not be used"


def test_stats_counts_unknown_profit_as_a_draw():
    """The other null on the same endpoint: close_profit=None used to raise TypeError."""
    import inspect

    from freqtrade.rpc.rpc import RPC

    src = inspect.getsource(RPC._rpc_stats)
    assert "if trade.close_profit is None:" in src
