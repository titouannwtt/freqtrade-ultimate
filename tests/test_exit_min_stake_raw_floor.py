"""An EXIT must be judged against the venue's raw floor, not the entry reserve.

`get_min_pair_stake_amount()` inflates the exchange minimum by `amount_reserve_percent`
and by the stoploss, capped at 1.5x, so a freshly OPENED position stays above the floor
after an adverse move. That reserve was also applied when judging CLOSES, where nothing
is left to provision.

Measured 2026-09-22 on Hyperliquid: raw floor 10 USDC, guard refusing anything under 15,
and twelve closes inside that band all accepted and filled on-chain.
"""

from unittest.mock import PropertyMock

import pytest

from tests.conftest import EXMS, get_patched_exchange


def _exchange(mocker, default_conf, cost_min=2):
    exchange = get_patched_exchange(mocker, default_conf, exchange="binance")
    markets = {
        "ETH/BTC": {
            "symbol": "ETH/BTC",
            "limits": {
                "cost": {"min": cost_min, "max": 10000},
                "amount": {"min": None, "max": None},
            },
        }
    }
    mocker.patch(f"{EXMS}.markets", PropertyMock(return_value=markets))
    return exchange


def test_entry_keeps_its_reserve(mocker, default_conf) -> None:
    """Default behaviour is unchanged: the floor is still inflated for entries."""
    exchange = _exchange(mocker, default_conf)
    stoploss = -0.05
    result = exchange.get_min_pair_stake_amount("ETH/BTC", 1, stoploss)
    assert pytest.approx(result) == 2 * (1 + 0.05) / (1 - abs(stoploss))


def test_exit_gets_the_raw_floor(mocker, default_conf) -> None:
    """apply_reserve=False returns the venue minimum untouched."""
    exchange = _exchange(mocker, default_conf)
    result = exchange.get_min_pair_stake_amount("ETH/BTC", 1, -0.05, apply_reserve=False)
    assert pytest.approx(result) == 2


def test_the_reserve_is_worth_up_to_one_and_a_half_times(mocker, default_conf) -> None:
    """A deep stoploss hits the 1.5 cap: that is the whole gap the fix removes."""
    exchange = _exchange(mocker, default_conf, cost_min=10)
    inflated = exchange.get_min_pair_stake_amount("ETH/BTC", 1, -0.35)
    raw = exchange.get_min_pair_stake_amount("ETH/BTC", 1, -0.35, apply_reserve=False)
    assert pytest.approx(raw) == 10
    assert pytest.approx(inflated) == 15
    assert inflated == pytest.approx(raw * 1.5)


def test_raw_floor_still_divides_by_leverage(mocker, default_conf) -> None:
    """The leverage conversion is orthogonal to the reserve and must survive."""
    exchange = _exchange(mocker, default_conf, cost_min=10)
    result = exchange.get_min_pair_stake_amount("ETH/BTC", 1, -0.35, 2.0, apply_reserve=False)
    assert pytest.approx(result) == 5
