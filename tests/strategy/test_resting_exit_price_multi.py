"""Candidate hippo_original_multi (lot L6): `resting_exit_price` equals the `min_roi_reached`
boundary (per-market ROI table, leverage, floor 0.005, cumulated funding), and drives the
resting take-profit through a ROI step.

The candidate strategy is not versioned (user_data); set HIPPO_L6_STRATEGY to its path.
"""

import importlib.util
import os
import random
from datetime import timedelta
from pathlib import Path

import pytest

from freqtrade.enums import TradingMode
from freqtrade.persistence import Trade
from freqtrade.util.datetime_helpers import dt_now


CANDIDATE = Path(
    os.environ.get(
        "HIPPO_L6_STRATEGY",
        "/home/moutonneux/freqtrade/user_data/research/hippo_fusion_2026-10-21/"
        "common/live_priority/l6/hippo_original_multi.py",
    )
)
FLOOR = 0.005

pytestmark = pytest.mark.skipif(not CANDIDATE.is_file(), reason="candidate strategy not found")


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("l6_hippo_original_multi", CANDIDATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _strategy(mod, market: str):
    strat = mod.hippo_original_multi.__new__(mod.hippo_original_multi)
    strat.market_of = lambda pair: market
    return strat


def _trade(rng: random.Random, lev: float, is_short: bool, now, dur_s: float) -> Trade:
    open_rate = 10 ** rng.uniform(-4, 4)
    amount = 10 ** rng.uniform(-2, 4)
    trade = Trade(
        pair="XYZ/USDC:USDC",
        stake_amount=amount * open_rate / lev,
        amount=amount,
        open_rate=open_rate,
        fee_open=rng.choice([0.0, 0.00015, 0.00045, rng.uniform(0, 0.001)]),
        fee_close=rng.choice([0.0, 0.00015, 0.00045, rng.uniform(0, 0.001)]),
        exchange="hyperliquid",
        is_open=True,
        is_short=is_short,
        leverage=lev,
        trading_mode=TradingMode.FUTURES,
        open_date=now - timedelta(seconds=dur_s),
    )
    notional = amount * open_rate
    trade.funding_fees = rng.choice([0.0, rng.uniform(-0.01, 0.01) * notional])
    trade.recalc_open_trade_value()
    return trade


def test_target_equals_min_roi_reached_boundary(mod):
    rng = random.Random(20261022)
    now = dt_now()
    markets = list(mod.MARKET_PARAMS)
    checked = 0
    for _ in range(10_000):
        market = rng.choice(markets)
        params = mod.MARKET_PARAMS[market]
        lev = rng.choice([params["leverage_value"], rng.choice([1.0, 2.0, 3.0])])
        is_short = rng.random() < 0.2
        steps = sorted(params["roi_price"])
        if rng.random() < 0.3:  # exactly on a ROI step, or one second before it
            dur_s = rng.choice(steps) * 60 - rng.choice([0, 1])
        else:
            dur_s = rng.uniform(0, 600 * 60)
        trade = _trade(rng, lev, is_short, now, max(dur_s, 0))
        strat = _strategy(mod, market)

        target = strat.resting_exit_price(trade, now)
        dur_min = int((now.timestamp() - trade.open_date_utc.timestamp()) // 60)
        if dur_min < min(steps):
            assert target is None
            continue
        assert target is not None and target > 0

        roi = params["roi_price"][max(k for k in steps if k <= dur_min)]
        required = max(roi * lev, FLOOR)
        assert trade.calc_profit_ratio(target) == pytest.approx(required, abs=2e-8)

        eps = 1e-6
        better = target * (1 - eps if is_short else 1 + eps)
        worse = target * (1 + eps if is_short else 1 - eps)
        # just beyond the TP price the strategy's own ROI rule fires and the exit is confirmed
        assert strat.min_roi_reached(trade, trade.calc_profit_ratio(better), now)
        assert trade.calc_profit_ratio(better) >= FLOOR
        # just before it, the effective rule (ROI exit AND confirm_trade_exit floor) does not
        worse_ratio = trade.calc_profit_ratio(worse)
        assert not (strat.min_roi_reached(trade, worse_ratio, now) and worse_ratio >= FLOOR)
        if roi * lev >= FLOOR:
            assert not strat.min_roi_reached(trade, worse_ratio, now)
        # at the boundary itself the ratio equals the requirement (8-decimal rounding of
        # calc_profit_ratio): the strict `>` of min_roi_reached is not met there, the floor is
        assert trade.calc_profit_ratio(target) >= FLOOR - 1e-8
        checked += 1
    assert checked > 9_000


def test_candidate_reprices_tp_at_roi_step(mocker, default_conf_usdt, mod):
    from tests.freqtradebot.test_resting_exit import _bot, _open_trade, _tp, install_book

    fake_book = install_book(mocker)
    strat = _strategy(mod, "crypto")
    bot = _bot(mocker, default_conf_usdt, target=None)
    mocker.patch.object(
        type(bot.strategy),
        "resting_exit_price",
        new=lambda self, trade, current_time, **kw: strat.resting_exit_price(trade, current_time),
    )
    trade = _open_trade()
    trade.open_date = dt_now() - timedelta(minutes=9, seconds=30)
    Trade.commit()
    bot._resting_exit.process()
    tp1 = _tp(trade)
    assert trade.calc_profit_ratio(tp1.price) == pytest.approx(0.05, abs=1e-6)

    trade.open_date = dt_now() - timedelta(minutes=10, seconds=1)  # step 10 min: 1.8 %
    Trade.commit()
    bot._resting_exit.process()
    tp2 = _tp(trade)
    assert tp2.order_id != tp1.order_id
    assert fake_book.orders[tp1.order_id]["status"] == "canceled"
    assert trade.calc_profit_ratio(tp2.price) == pytest.approx(0.018, abs=1e-6)
    # TP price = ROI boundary rounded up to the tick: the rule fires one tick above it,
    # not below it
    tick = bot.exchange.price_get_one_pip(trade.pair, tp2.price)
    above = trade.calc_profit_ratio(tp2.price + 3 * tick)
    assert strat.min_roi_reached(trade, above, dt_now())
    assert not strat.min_roi_reached(trade, trade.calc_profit_ratio(tp2.price * 0.9999), dt_now())
