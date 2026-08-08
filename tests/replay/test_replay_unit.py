"""
Regression tests for the dry-run replay harness (freqtrade/replay).

Written BEFORE the performance-optimisation work: every assertion here pins the
*current* behaviour and must stay green after optimisation. Coverage spans the
safety gate, the data store slicing rules, and the replay exchange overrides.
"""

import pandas as pd
import pytest

from freqtrade.enums import CandleType, RunMode
from freqtrade.replay.clock import VirtualClock
from freqtrade.replay.data_store import ReplayDataStore
from freqtrade.replay.exchange import (
    ReplayExchangeMixin,
    build_replay_exchange,
    resolve_base_exchange_class,
)
from freqtrade.replay.safety import (
    REPLAY_DB_MARKER,
    ReplaySafetyError,
    enforce_replay_safety,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_feather(datadir, pair, tf, candle_type, start, n, freq, base_price=100.0):
    base = pair.replace("/", "_").replace(":", "_")
    suffix = "" if candle_type == CandleType.SPOT else f"-{candle_type.value}"
    path = datadir / f"{base}-{tf}{suffix}.feather"
    dates = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    closes = [base_price + i for i in range(n)]
    df = pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [10.0] * n,
        }
    )
    df.to_feather(path)
    return df


def _base_config(db="sqlite:///user_data/x.sqlite"):
    return {
        "dry_run": False,
        "runmode": RunMode.LIVE,
        "db_url": db,
        "exchange": {
            "name": "hyperliquid",
            "key": "K",
            "secret": "S",
            "password": "P",
            "uid": "U",
            "walletAddress": "0xABC",
            "privateKey": "PK",
            "ccxt_config": {"apiKey": "x", "secret": "z"},
        },
        "telegram": {"enabled": True},
        "api_server": {"enabled": True},
        "discord": {"enabled": True},
        "webhook": {"enabled": True},
    }


# ---------------------------------------------------------------------------
# Safety gate
# ---------------------------------------------------------------------------


class TestSafety:
    def test_forces_dry_run_and_runmode(self):
        cfg = _base_config()
        enforce_replay_safety(cfg)
        assert cfg["dry_run"] is True
        assert cfg["runmode"] == RunMode.DRY_RUN

    def test_blanks_all_credentials_including_hyperliquid_and_nested(self):
        cfg = _base_config()
        enforce_replay_safety(cfg)
        ex = cfg["exchange"]
        for field in ("key", "secret", "password", "uid", "walletAddress", "privateKey"):
            assert not ex[field], f"{field} not blanked"
        assert not ex["ccxt_config"]["secret"]

    def test_removes_outbound_channels(self):
        cfg = _base_config()
        enforce_replay_safety(cfg)
        for ch in ("telegram", "api_server", "discord", "webhook"):
            assert ch not in cfg

    def test_namespaces_db_url(self):
        cfg = _base_config("sqlite:///database/foo.sqlite")
        enforce_replay_safety(cfg)
        assert cfg["db_url"] == "sqlite:///database/foo.replay.sqlite"
        assert REPLAY_DB_MARKER in cfg["db_url"]

    def test_namespace_idempotent(self):
        cfg = _base_config("sqlite:///database/foo.replay.sqlite")
        enforce_replay_safety(cfg)
        assert cfg["db_url"] == "sqlite:///database/foo.replay.sqlite"

    def test_default_db_when_missing(self):
        cfg = _base_config(db=None)
        enforce_replay_safety(cfg)
        assert REPLAY_DB_MARKER in cfg["db_url"]
        assert cfg["db_url"].startswith("sqlite:///")

    def test_refuses_non_sqlite(self):
        cfg = _base_config("postgresql://u:p@h/db")
        with pytest.raises(ReplaySafetyError):
            enforce_replay_safety(cfg)

    def test_seed_mode_keeps_db_url_unchanged(self):
        cfg = _base_config("sqlite:///database/dry_bot.sqlite")
        enforce_replay_safety(cfg, namespace_db=False)
        # Seed mode targets the bot's own dry-run DB — no namespacing.
        assert cfg["db_url"] == "sqlite:///database/dry_bot.sqlite"
        assert cfg["dry_run"] is True

    def test_seed_mode_still_refuses_non_sqlite(self):
        cfg = _base_config("mysql://u:p@h/db")
        with pytest.raises(ReplaySafetyError):
            enforce_replay_safety(cfg, namespace_db=False)


# ---------------------------------------------------------------------------
# Data store
# ---------------------------------------------------------------------------


class TestDataStore:
    PAIR = "BTC/USDC:USDC"

    @pytest.fixture
    def store(self, tmp_path):
        # 50 x 15m futures candles from 2026-01-01.
        _make_feather(tmp_path, self.PAIR, "15m", CandleType.FUTURES, "2026-01-01", 50, "15min")
        return ReplayDataStore(tmp_path, trading_mode="futures")

    def test_drop_incomplete_excludes_forming_candle(self, store):
        up_to = pd.Timestamp("2026-01-01 02:30", tz="UTC")  # exactly a candle open
        df = store.get_candles(self.PAIR, "15m", CandleType.FUTURES, up_to, drop_incomplete=True)
        assert (df["date"] < up_to).all()
        assert up_to not in set(df["date"])

    def test_keep_incomplete_includes_candle_at_up_to(self, store):
        up_to = pd.Timestamp("2026-01-01 02:30", tz="UTC")
        df = store.get_candles(self.PAIR, "15m", CandleType.FUTURES, up_to, drop_incomplete=False)
        assert up_to in set(df["date"])

    def test_drop_incomplete_hides_forming_candle_mid_bar(self, store):
        # Regression for the look-ahead bug: a sub-step finer than the
        # timeframe (e.g. 1m ticks on 15m candles) must not reveal a candle
        # still forming — only once its close has actually passed.
        up_to = pd.Timestamp("2026-01-01 02:31", tz="UTC")  # 1 minute into the 02:30 candle
        df = store.get_candles(self.PAIR, "15m", CandleType.FUTURES, up_to, drop_incomplete=True)
        last_open = pd.Timestamp("2026-01-01 02:15", tz="UTC")
        assert df["date"].iloc[-1] == last_open
        assert pd.Timestamp("2026-01-01 02:30", tz="UTC") not in set(df["date"])

    def test_max_candles_cap(self, store):
        up_to = pd.Timestamp("2026-01-02", tz="UTC")
        df = store.get_candles(
            self.PAIR, "15m", CandleType.FUTURES, up_to, max_candles=5, drop_incomplete=True
        )
        assert len(df) == 5

    def test_since_ms_lower_bound(self, store):
        up_to = pd.Timestamp("2026-01-02", tz="UTC")
        since = pd.Timestamp("2026-01-01 06:00", tz="UTC")
        df = store.get_candles(
            self.PAIR,
            "15m",
            CandleType.FUTURES,
            up_to,
            since_ms=int(since.timestamp() * 1000),
            max_candles=None,
        )
        assert (df["date"] >= since).all()

    def test_get_last_price_strictly_before(self, store):
        up_to = pd.Timestamp("2026-01-01 01:00", tz="UTC")
        price = store.get_last_price(self.PAIR, up_to, CandleType.FUTURES)
        # last closed candle before 01:00 is the one opening 00:45 (index 3, close=103)
        assert price == pytest.approx(103.0)

    def test_get_candle_ohlc(self, store):
        up_to = pd.Timestamp("2026-01-01 01:00", tz="UTC")
        ohlc = store.get_candle_ohlc(self.PAIR, "15m", CandleType.FUTURES, up_to)
        assert set(ohlc) == {"open", "high", "low", "close"}
        assert ohlc["high"] == ohlc["close"] + 1

    def test_get_last_price_prefers_finest_tf(self, tmp_path):
        # 15m and 5m present → 5m (finest) wins for fill accuracy.
        _make_feather(
            tmp_path, self.PAIR, "15m", CandleType.FUTURES, "2026-01-01", 20, "15min", 100
        )
        _make_feather(tmp_path, self.PAIR, "5m", CandleType.FUTURES, "2026-01-01", 60, "5min", 500)
        store = ReplayDataStore(tmp_path, trading_mode="futures")
        up_to = pd.Timestamp("2026-01-01 02:00", tz="UTC")
        # 5m series starts at 500 → much higher than the 15m (100) series.
        assert store.get_last_price(self.PAIR, up_to, CandleType.FUTURES) > 400

    def test_validate_raises_on_missing(self, store):
        with pytest.raises(ValueError):
            store.validate(
                "ETH/USDC:USDC",
                "15m",
                CandleType.FUTURES,
                pd.Timestamp("2026-01-01 06:00", tz="UTC"),
                pd.Timestamp("2026-01-01 10:00", tz="UTC"),
                5,
            )

    def test_validate_raises_on_insufficient_warmup(self, store):
        with pytest.raises(ValueError):
            store.validate(
                self.PAIR,
                "15m",
                CandleType.FUTURES,
                pd.Timestamp("2026-01-01 00:30", tz="UTC"),  # only 2 candles before
                pd.Timestamp("2026-01-01 10:00", tz="UTC"),
                500,
            )

    def test_max_candles_attribute_default(self, tmp_path):
        store = ReplayDataStore(tmp_path, trading_mode="futures", max_candles=777)
        assert store.max_candles == 777


# ---------------------------------------------------------------------------
# Replay exchange
# ---------------------------------------------------------------------------


class TestExchange:
    PAIR = "BTC/USDC:USDC"

    @pytest.fixture
    def built(self, tmp_path):
        _make_feather(
            tmp_path, self.PAIR, "15m", CandleType.FUTURES, "2026-01-01", 60, "15min", 100
        )
        cfg = {
            "dry_run": True,
            "runmode": RunMode.DRY_RUN,
            "trading_mode": "futures",
            "margin_mode": "isolated",
            "stake_currency": "USDC",
            "stake_amount": "unlimited",
            "exchange": {
                "name": "hyperliquid",
                "pair_whitelist": [self.PAIR],
                "pair_blacklist": [],
            },
            "db_url": "sqlite:///user_data/x.replay.sqlite",
        }
        store = ReplayDataStore(tmp_path, trading_mode="futures")
        clock = VirtualClock()
        clock.start(pd.Timestamp("2026-01-01 06:00", tz="UTC").to_pydatetime())
        ex = build_replay_exchange(cfg, store, clock, [self.PAIR], slippage_pct=0.001)
        yield ex
        clock.stop()

    def test_resolves_hyperliquid_class(self):
        cls = resolve_base_exchange_class({"exchange": {"name": "hyperliquid"}})
        assert cls.__name__ == "Hyperliquid"

    def test_builds_without_network_and_forces_dry_run(self, built):
        assert type(built).__name__ == "ReplayHyperliquid"
        assert built._config["dry_run"] is True
        assert self.PAIR in built.get_markets()

    def test_exchange_has_capabilities(self, built):
        assert built.exchange_has("fetchOHLCV") is True
        assert built.exchange_has("fetchTrades") is False

    def test_get_fee_uses_config_override(self, built):
        built._config["fee"] = 0.00077
        assert built.get_fee(self.PAIR, taker_or_maker="maker") == 0.00077
        assert built.get_fee(self.PAIR, taker_or_maker="taker") == 0.00077

    def test_get_fee_uses_market_maker_taker(self, built):
        built._config.pop("fee", None)
        maker = built.get_fee(self.PAIR, taker_or_maker="maker")
        taker = built.get_fee(self.PAIR, taker_or_maker="taker")
        assert maker == built.get_markets()[self.PAIR]["maker"]
        assert taker == built.get_markets()[self.PAIR]["taker"]

    def test_does_not_override_funding_fees(self):
        # Funding must come from inherited Hyperliquid dry-run logic, not a stub.
        assert "get_funding_fees" not in ReplayExchangeMixin.__dict__

    def test_persist_klines_is_noop(self, built, tmp_path):
        # Must never write/read the live bots' shared klines cache.
        built._klines[(self.PAIR, "15m", CandleType.FUTURES)] = pd.DataFrame()
        assert built.persist_klines() is None
        assert built._load_persisted_klines() is None

    def test_fetch_ticker_synthesises_spread(self, built):
        t = built.fetch_ticker(self.PAIR)
        assert t["ask"] > t["last"] > t["bid"]
        assert t["symbol"] == self.PAIR

    def test_fetch_l2_order_book_synthesises(self, built):
        ob = built.fetch_l2_order_book(self.PAIR)
        assert ob["asks"][0][0] > ob["bids"][0][0]

    def test_refresh_latest_ohlcv_serves_and_caches(self, built):
        res = built.refresh_latest_ohlcv([(self.PAIR, "15m", CandleType.FUTURES)], cache=True)
        df = res[(self.PAIR, "15m", CandleType.FUTURES)]
        assert not df.empty
        # Cached into _klines, and mirrored to SPOT for informative-pair lookups.
        assert (self.PAIR, "15m", CandleType.FUTURES) in built._klines
        assert (self.PAIR, "15m", CandleType.SPOT) in built._klines

    def test_get_positions_empty(self, built):
        assert built.get_positions() == {}

    def test_markets_advertise_a_real_min_notional(self, built):
        """
        Regression: the synthetic market must refuse dust orders.

        With ``limits.cost.min == 0`` a DCA safety order requested on a fully
        committed wallet gets clamped to ~1e-06 USDC and still passes
        ``validate_stake_amount``. ``execute_entry()`` then reaches
        ``handle_similar_open_order()``, which cancels every open order of the trade —
        including a pending exit. In dry-run that cancellation fills the exit, books
        it, closes the trade, and the dust entry immediately reopens it: the same
        position gets sold (and its loss realised) again on the next candle.
        """
        market = built.get_markets()[self.PAIR]
        assert market["limits"]["cost"]["min"] == 10.0

        # The value freqtrade actually consults before placing an order.
        min_stake = built.get_min_pair_stake_amount(self.PAIR, 100.0, -0.10)
        assert min_stake is not None and min_stake >= 10.0
        # A dust safety order must be rejected outright.
        assert built.get_min_pair_stake_amount(self.PAIR, 100.0, -0.10) > 1e-06

    def test_min_notional_is_configurable(self, tmp_path):
        _make_feather(
            tmp_path, self.PAIR, "15m", CandleType.FUTURES, "2026-01-01", 60, "15min", 100
        )
        cfg = {
            "dry_run": True,
            "runmode": RunMode.DRY_RUN,
            "trading_mode": "futures",
            "margin_mode": "isolated",
            "stake_currency": "USDC",
            "stake_amount": "unlimited",
            "replay_min_notional": 25.0,
            "exchange": {
                "name": "hyperliquid",
                "pair_whitelist": [self.PAIR],
                "pair_blacklist": [],
            },
            "db_url": "sqlite:///user_data/x.replay.sqlite",
        }
        store = ReplayDataStore(tmp_path, trading_mode="futures")
        clock = VirtualClock()
        clock.start(pd.Timestamp("2026-01-01 06:00", tz="UTC").to_pydatetime())
        try:
            ex = build_replay_exchange(cfg, store, clock, [self.PAIR], slippage_pct=0.001)
            assert ex.get_markets()[self.PAIR]["limits"]["cost"]["min"] == 25.0
        finally:
            clock.stop()

    @staticmethod
    def _stop_order(pair, side, trigger, limit):
        return {
            "id": "sl1",
            "symbol": pair,
            "side": side,
            "type": "stop",
            "ft_order_type": "stoploss",
            "status": "open",
            "amount": 1.0,
            "filled": 0.0,
            "remaining": 1.0,
            "price": limit,
            "stopPrice": trigger,
            "fee": None,
        }

    def test_stoploss_fills_at_trigger_not_limit(self, built):
        # Short position stop (buy-to-close): trigger inside the candle range,
        # limit 1% beyond. The fill must be trigger + slippage, NOT the limit.
        candle = built._replay_store.get_candle_ohlc(
            self.PAIR, "15m", CandleType.FUTURES, built._replay_clock.now()
        )
        trigger = candle["low"] + 0.5  # crossed within the candle
        limit = trigger * 1.01
        order = self._stop_order(self.PAIR, "buy", trigger, limit)
        res = built.check_dry_limit_order_filled(order)
        assert res["status"] == "closed"
        assert res["average"] == pytest.approx(trigger * 1.001)  # slippage_pct=0.001
        assert res["average"] < limit

    def test_stoploss_fill_long_side(self, built):
        # Long position stop (sell-to-close): adverse slippage goes down.
        candle = built._replay_store.get_candle_ohlc(
            self.PAIR, "15m", CandleType.FUTURES, built._replay_clock.now()
        )
        trigger = candle["high"] - 0.5
        limit = trigger * 0.99
        order = self._stop_order(self.PAIR, "sell", trigger, limit)
        res = built.check_dry_limit_order_filled(order)
        assert res["status"] == "closed"
        assert res["average"] == pytest.approx(trigger * 0.999)
        assert res["average"] > limit

    def test_stoploss_fill_capped_at_limit(self, built):
        # If the limit sits inside the slippage band (tight stop-limit), the fill
        # can never be worse than the limit price.
        candle = built._replay_store.get_candle_ohlc(
            self.PAIR, "15m", CandleType.FUTURES, built._replay_clock.now()
        )
        trigger = candle["low"] + 0.5
        limit = trigger  # stop-market emulation: limit == trigger
        order = self._stop_order(self.PAIR, "buy", trigger, limit)
        res = built.check_dry_limit_order_filled(order)
        assert res["status"] == "closed"
        assert res["average"] == pytest.approx(trigger)


class TestWalletResolution:
    def test_falls_back_to_config_dry_run_wallet(self):
        from freqtrade.replay.runner import _resolve_wallet

        assert _resolve_wallet({"dry_run_wallet": 5000}, None) == 5000.0

    def test_defaults_to_1000_without_config_value(self):
        from freqtrade.replay.runner import _resolve_wallet

        assert _resolve_wallet({}, None) == 1000.0

    def test_explicit_wallet_wins_with_warning(self, caplog):
        from freqtrade.replay.runner import _resolve_wallet

        assert _resolve_wallet({"dry_run_wallet": 5000}, 2000.0) == 2000.0
        assert "overrides the config's" in caplog.text

    def test_build_cmd_omits_wallet_when_none(self):
        from freqtrade.replay.lifecycle import build_cmd

        common = dict(
            strategy="S",
            timerange="20260101-20260201",
            pairs=["BTC/USDC:USDC"],
            sub_step=60,
            db_url="sqlite:///x.sqlite",
            progress_file="p.json",
            reset_db=False,
        )
        cfg = {"config_files": ["c.json"]}
        assert "--wallet" not in build_cmd(cfg, wallet=None, **common)
        cmd = build_cmd(cfg, wallet=5000.0, **common)
        assert cmd[cmd.index("--wallet") + 1] == "5000.0"
