# pragma pylint: disable=missing-docstring, W0212, line-too-long, C0103, unused-argument

from copy import deepcopy
from unittest.mock import MagicMock

import pandas as pd
import pytest

from freqtrade.configuration import TimeRange
from freqtrade.data import history
from freqtrade.data.history import get_timerange
from freqtrade.enums import ExitType
from freqtrade.optimize.backtesting import Backtesting
from freqtrade.strategy import stoploss_from_open
from freqtrade.util.datetime_helpers import dt_utc
from tests.conftest import EXMS, patch_exchange


def test_backtest_position_adjustment(default_conf, fee, mocker, testdatadir) -> None:
    default_conf["use_exit_signal"] = False
    default_conf["max_open_trades"] = 10
    mocker.patch(f"{EXMS}.get_fee", fee)
    mocker.patch(
        "freqtrade.optimize.backtesting.amount_to_contract_precision",
        lambda x, *args, **kwargs: round(x, 8),
    )
    mocker.patch(f"{EXMS}.get_min_pair_stake_amount", return_value=0.00001)
    mocker.patch(f"{EXMS}.get_max_pair_stake_amount", return_value=float("inf"))
    patch_exchange(mocker)
    default_conf.update(
        {"stake_amount": 100.0, "dry_run_wallet": 1000.0, "strategy": "StrategyTestV3"}
    )
    backtesting = Backtesting(default_conf)
    backtesting._set_strategy(backtesting.strategylist[0])
    pair = "UNITTEST/BTC"
    timerange = TimeRange("date", None, 1517227800, 0)
    data = history.load_data(
        datadir=testdatadir, timeframe="5m", pairs=["UNITTEST/BTC"], timerange=timerange
    )
    backtesting.strategy.position_adjustment_enable = True
    processed = backtesting.strategy.advise_all_indicators(data)
    min_date, max_date = get_timerange(processed)
    result = backtesting.backtest(
        processed=deepcopy(processed),
        start_date=min_date,
        end_date=max_date,
    )
    results = result["results"]
    assert not results.empty
    assert len(results) == 2

    expected = pd.DataFrame(
        {
            "pair": [pair, pair],
            "stake_amount": [500.0, 100.0],
            "max_stake_amount": [500.0, 100],
            "amount": [4806.87657523, 970.63960782],
            "open_date": pd.to_datetime(
                [dt_utc(2018, 1, 29, 18, 40, 0), dt_utc(2018, 1, 30, 3, 30, 0)], utc=True
            ),
            "close_date": pd.to_datetime(
                [dt_utc(2018, 1, 29, 22, 00, 0), dt_utc(2018, 1, 30, 4, 10, 0)], utc=True
            ),
            "open_rate": [0.10401764891917063, 0.10302485],
            "close_rate": [0.10453904064307624, 0.10354126528822055],
            "fee_open": [0.0025, 0.0025],
            "fee_close": [0.0025, 0.0025],
            "trade_duration": [200, 40],
            "profit_ratio": [0.0, 0.0],
            "profit_abs": [0.0, 0.0],
            "exit_reason": [ExitType.ROI.value, ExitType.ROI.value],
            "initial_stop_loss_abs": [0.0940005, 0.092722365],
            "initial_stop_loss_ratio": [-0.1, -0.1],
            "stop_loss_abs": [0.0940005, 0.092722365],
            "stop_loss_ratio": [-0.1, -0.1],
            "min_rate": [0.10370188, 0.103000000],
            "max_rate": [0.10481985, 0.103888870],
            "is_open": [False, False],
            "enter_tag": ["", ""],
            "leverage": [1.0, 1.0],
            "is_short": [False, False],
            "open_timestamp": [1517251200000, 1517283000000],
            "close_timestamp": [1517263200000, 1517285400000],
            "funding_fees": [0.0, 0.0],
        }
    )
    # TODO: pandas3 - create correctly above ?!?
    expected["open_date"] = expected["open_date"].astype("datetime64[ms, UTC]")
    expected["close_date"] = expected["close_date"].astype("datetime64[ms, UTC]")
    results_no = results.drop(columns=["orders"])
    pd.testing.assert_frame_equal(results_no, expected, check_exact=True)

    data_pair = processed[pair]
    assert len(results.iloc[0]["orders"]) == 6
    assert len(results.iloc[1]["orders"]) == 2

    for _, t in results.iterrows():
        ln = data_pair.loc[data_pair["date"] == t["open_date"]]
        # Check open trade rate aligns to open rate
        assert ln is not None
        # check close trade rate aligns to close rate or is between high and low
        ln = data_pair.loc[data_pair["date"] == t["close_date"]]
        assert round(ln.iloc[0]["open"], 6) == round(t["close_rate"], 6) or round(
            ln.iloc[0]["low"], 6
        ) < round(t["close_rate"], 6) < round(ln.iloc[0]["high"], 6)


@pytest.mark.parametrize("leverage", [1, 2])
def test_backtest_position_adjustment_detailed(default_conf, fee, mocker, leverage) -> None:
    default_conf["use_exit_signal"] = False
    mocker.patch(f"{EXMS}.get_fee", fee)
    mocker.patch(f"{EXMS}.get_min_pair_stake_amount", return_value=10)
    mocker.patch(f"{EXMS}.get_max_pair_stake_amount", return_value=float("inf"))
    mocker.patch(f"{EXMS}.get_max_leverage", return_value=10)
    mocker.patch(f"{EXMS}.get_maintenance_ratio_and_amt", return_value=(0.1, 0.1))
    mocker.patch("freqtrade.optimize.backtesting.Backtesting._run_funding_fees")

    patch_exchange(mocker)
    default_conf.update(
        {
            "stake_amount": 100.0,
            "dry_run_wallet": 1000.0,
            "strategy": "StrategyTestV3",
            "trading_mode": "futures",
            "margin_mode": "isolated",
        }
    )
    default_conf["pairlists"] = [{"method": "StaticPairList", "allow_inactive": True}]
    backtesting = Backtesting(default_conf)
    backtesting._can_short = True
    backtesting._set_strategy(backtesting.strategylist[0])
    pair = "XRP/USDT:USDT"
    row_enter = [
        pd.Timestamp(year=2020, month=1, day=1, hour=4, minute=0),
        2.1,  # Open
        2.2,  # High
        1.9,  # Low
        2.1,  # Close
        1,  # enter_long
        0,  # exit_long
        0,  # enter_short
        0,  # exit_short
        "",  # enter_tag
        "",  # exit_tag
    ]
    # Exit row - with slightly different values
    row_exit = [
        pd.Timestamp(year=2020, month=1, day=1, hour=5, minute=0),
        2.2,  # Open
        2.3,  # High
        2.0,  # Low
        2.2,  # Close
        1,  # enter_long
        0,  # exit_long
        0,  # enter_short
        0,  # exit_short
        "",  # enter_tag
        "",  # exit_tag
    ]
    backtesting.strategy.leverage = MagicMock(return_value=leverage)
    trade = backtesting._enter_trade(pair, row=row_enter, direction="long")
    current_time = row_enter[0].to_pydatetime()
    assert trade
    assert pytest.approx(trade.stake_amount) == 100.0
    assert pytest.approx(trade.amount) == 47.61904762 * leverage
    assert len(trade.orders) == 1
    backtesting.strategy.adjust_trade_position = MagicMock(return_value=None)
    assert pytest.approx(trade.liquidation_price) == (0.10278333 if leverage == 1 else 1.2122249)

    trade = backtesting._check_adjust_trade_for_candle(trade, row_enter, current_time)
    assert trade
    assert pytest.approx(trade.stake_amount) == 100.0
    assert pytest.approx(trade.amount) == 47.61904762 * leverage
    assert len(trade.orders) == 1
    # Increase position by 100
    backtesting.strategy.adjust_trade_position = MagicMock(return_value=(100, "PartIncrease"))

    trade = backtesting._check_adjust_trade_for_candle(trade, row_enter, current_time)

    liq_price = 0.1038916 if leverage == 1 else 1.2127791
    assert trade
    assert pytest.approx(trade.stake_amount) == 200.0
    assert pytest.approx(trade.amount) == 95.23809524 * leverage
    assert len(trade.orders) == 2
    assert trade.orders[-1].ft_order_tag == "PartIncrease"
    assert pytest.approx(trade.liquidation_price) == liq_price

    # Reduce by more than amount - no change to trade.
    backtesting.strategy.adjust_trade_position = MagicMock(return_value=-500)
    current_time = row_exit[0].to_pydatetime()

    trade = backtesting._check_adjust_trade_for_candle(trade, row_exit, current_time)

    assert trade
    assert pytest.approx(trade.stake_amount) == 200.0
    assert pytest.approx(trade.amount) == 95.23809524 * leverage
    assert len(trade.orders) == 2
    assert trade.nr_of_successful_entries == 2
    assert pytest.approx(trade.liquidation_price) == liq_price

    # Reduce position by 50
    backtesting.strategy.adjust_trade_position = MagicMock(return_value=(-100, "partDecrease"))
    trade = backtesting._check_adjust_trade_for_candle(trade, row_exit, current_time)

    assert trade
    assert pytest.approx(trade.stake_amount) == 100.0
    assert pytest.approx(trade.amount) == 47.61904762 * leverage
    assert len(trade.orders) == 3
    assert trade.orders[-1].ft_order_tag == "partDecrease"
    assert trade.nr_of_successful_entries == 2
    assert trade.nr_of_successful_exits == 1
    assert pytest.approx(trade.liquidation_price) == liq_price

    # Adjust below minimum
    backtesting.strategy.adjust_trade_position = MagicMock(return_value=-99)
    trade = backtesting._check_adjust_trade_for_candle(trade, row_exit, current_time)

    assert trade
    assert pytest.approx(trade.stake_amount) == 100.0
    assert pytest.approx(trade.amount) == 47.61904762 * leverage
    assert len(trade.orders) == 3
    assert trade.nr_of_successful_entries == 2
    assert trade.nr_of_successful_exits == 1
    assert pytest.approx(trade.liquidation_price) == liq_price

    # Adjust to close trade
    backtesting.strategy.adjust_trade_position = MagicMock(return_value=-trade.stake_amount)
    trade = backtesting._check_adjust_trade_for_candle(trade, row_exit, current_time)
    assert trade.is_open is False


def test_backtest_stoploss_after_fill_not_ratcheted_by_recalc(default_conf, fee, mocker) -> None:
    """
    custom_stoploss(after_fill=True) with stoploss_from_open must leave the stop at the same
    distance from the new average as in live (recalc first, then after_fill), and not be
    ratcheted up by a second recalc_trade_from_orders() after the fill.
    """
    default_conf["use_exit_signal"] = False
    mocker.patch(f"{EXMS}.get_fee", fee)
    mocker.patch(f"{EXMS}.get_min_pair_stake_amount", return_value=10)
    mocker.patch(f"{EXMS}.get_max_pair_stake_amount", return_value=float("inf"))
    mocker.patch("freqtrade.optimize.backtesting.Backtesting._run_funding_fees")
    patch_exchange(mocker)
    default_conf.update(
        {"stake_amount": 100.0, "dry_run_wallet": 1000.0, "strategy": "StrategyTestV3"}
    )
    backtesting = Backtesting(default_conf)
    backtesting._set_strategy(backtesting.strategylist[0])
    strategy = backtesting.strategy
    strategy.use_custom_stoploss = True
    strategy._ft_stop_uses_after_fill = True

    def custom_stoploss(pair, trade, current_time, current_rate, current_profit, after_fill, **kw):
        if after_fill:
            return stoploss_from_open(-0.35, current_profit, is_short=trade.is_short)
        return None

    strategy.custom_stoploss = custom_stoploss

    def row(hour: int, price: float):
        return [
            pd.Timestamp(year=2020, month=1, day=1, hour=hour, minute=0),
            price,  # Open
            price * 1.01,  # High
            price * 0.99,  # Low
            price,  # Close
            0,  # enter_long
            0,  # exit_long
            0,  # enter_short
            0,  # exit_short
            "",  # enter_tag
            "",  # exit_tag
        ]

    pair = "XRP/USDT"
    row_enter = row(4, 2.1)
    trade = backtesting._enter_trade(pair, row=row_enter, direction="long")
    assert trade

    for hour, price in ((5, 1.9), (6, 1.7)):
        r = row(hour, price)
        strategy.adjust_trade_position = MagicMock(return_value=(100, "SafetyOrder"))
        trade = backtesting._check_adjust_trade_for_candle(trade, r, r[0].to_pydatetime())
        assert trade
    assert len(trade.orders) == 3

    expected = 0.65 * (1 + trade.fee_open) / (1 - trade.fee_close)
    assert trade.stop_loss / trade.open_rate == pytest.approx(expected, abs=1e-6)
