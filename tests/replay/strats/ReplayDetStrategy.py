"""Deterministic test strategy for replay end-to-end regression tests."""

from pandas import DataFrame

from freqtrade.strategy import IStrategy


class ReplayDetStrategy(IStrategy):
    timeframe = "15m"
    startup_candle_count = 20
    minimal_roi = {"0": 0.02}
    stoploss = -0.5
    can_short = True
    process_only_new_candles = True
    use_exit_signal = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma"] = dataframe["close"].rolling(5).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] > dataframe["sma"], "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["sma"], "exit_long"] = 1
        return dataframe
