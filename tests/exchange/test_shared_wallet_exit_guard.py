"""An exit order must never OPEN an inverted position on a shared netted wallet.

Regression for the ENA incident of 2026-08-21:

    08:44:09  Close Short  3498 @ 0.13899   (liquidation — the wallet goes flat)
    08:44:24  Open Long    3498 @ 0.13891   (the bot's own close order, 15s late)

The wallet then carried a 3498 LONG no bot tracked. `shared_wallet: true` disables
reduceOnly fleet-wide, so nothing stopped the flip.
"""

import pytest

from freqtrade.exceptions import InvalidOrderException
from freqtrade.exchange.exchange import Exchange


def _ex(net, shared=True, dry=False, mode="futures"):
    from freqtrade.enums import TradingMode

    e = Exchange.__new__(Exchange)
    e._exchange_ws = None  # bare object: keep __del__ quiet
    e._config = {"dry_run": dry, "exchange": {"shared_wallet": shared}}
    e.trading_mode = TradingMode.FUTURES if mode == "futures" else TradingMode.SPOT
    e.net_position_size = lambda pair: net
    return e


def test_a_flat_wallet_refuses_the_exit():
    """The incident itself: nothing to close, so the order could only open."""
    e = _ex(net=0.0)
    with pytest.raises(InvalidOrderException, match="flat on this coin"):
        e._guard_shared_wallet_exit("ENA/USDC:USDC", "buy", 3498.0, True, {})


def test_dust_counts_as_flat():
    e = _ex(net=0.5)
    with pytest.raises(InvalidOrderException):
        e._guard_shared_wallet_exit("ENA/USDC:USDC", "buy", 3498.0, True, {})


def test_closing_a_short_against_a_real_short_re_enables_reduce_only():
    """net -3498, buying 3498 back: reduceOnly cannot be rejected, so arm it."""
    e = _ex(net=-3498.0)
    params: dict = {}
    e._guard_shared_wallet_exit("ENA/USDC:USDC", "buy", 3498.0, True, params)
    assert params["reduceOnly"] is True


def test_closing_a_long_against_a_real_long_re_enables_reduce_only():
    e = _ex(net=3498.0)
    params: dict = {}
    e._guard_shared_wallet_exit("ENA/USDC:USDC", "sell", 3498.0, True, params)
    assert params["reduceOnly"] is True


def test_a_legitimate_exit_across_bots_is_NOT_blocked():
    """Bot A closes a 100 short while bot B holds a 500 long: net is +400, the same
    side the buy pushes toward. A naive sign check would refuse this real exit and
    strand the trade — the guard must let it through, just without reduceOnly."""
    e = _ex(net=400.0)
    params: dict = {}
    e._guard_shared_wallet_exit("ENA/USDC:USDC", "buy", 100.0, True, params)
    assert "reduceOnly" not in params


def test_partial_cover_does_not_arm_reduce_only():
    """net -50 but closing 100: reduceOnly would be rejected, so leave it off."""
    e = _ex(net=-50.0)
    params: dict = {}
    e._guard_shared_wallet_exit("ENA/USDC:USDC", "buy", 100.0, True, params)
    assert "reduceOnly" not in params


def test_entries_are_untouched():
    """reduceOnly=False means this is an entry — the guard must not look at anything."""
    e = _ex(net=0.0)
    params: dict = {}
    e._guard_shared_wallet_exit("ENA/USDC:USDC", "buy", 3498.0, False, params)
    assert params == {}


def test_unknown_net_fails_open():
    """No fetchPositions / API error: trading must continue, not stop."""
    e = _ex(net=None)
    params: dict = {}
    e._guard_shared_wallet_exit("ENA/USDC:USDC", "buy", 3498.0, True, params)
    assert params == {}


def test_single_wallet_setups_are_untouched():
    """Without shared_wallet, _get_params already set reduceOnly; the guard is inert."""
    e = _ex(net=0.0, shared=False)
    params: dict = {}
    e._guard_shared_wallet_exit("ENA/USDC:USDC", "buy", 3498.0, True, params)
    assert params == {}


def test_spot_is_untouched():
    e = _ex(net=0.0, mode="spot")
    params: dict = {}
    e._guard_shared_wallet_exit("BTC/USDC", "sell", 1.0, True, params)
    assert params == {}
