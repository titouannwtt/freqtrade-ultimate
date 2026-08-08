from enum import StrEnum


class RPCMessageType(StrEnum):
    STATUS = "status"
    WARNING = "warning"
    EXCEPTION = "exception"
    STARTUP = "startup"

    ENTRY = "entry"
    ENTRY_FILL = "entry_fill"
    ENTRY_CANCEL = "entry_cancel"

    EXIT = "exit"
    EXIT_FILL = "exit_fill"
    EXIT_CANCEL = "exit_cancel"

    PROTECTION_TRIGGER = "protection_trigger"
    PROTECTION_TRIGGER_GLOBAL = "protection_trigger_global"

    STRATEGY_MSG = "strategy_msg"

    WHITELIST = "whitelist"
    ANALYZED_DF = "analyzed_df"
    NEW_CANDLE = "new_candle"

    # Fork-specific: periodic live profit snapshot for open trades, pushed over the
    # WS message stream to let dashboards replace REST /status polling. See
    # FreqtradeBot._emit_trade_snapshot(). In NO_ECHO_MESSAGES: the message is emitted
    # on a fixed cadence regardless of activity, so echoing its full payload at INFO
    # produced ~1440 log lines per bot per day — 72 000 across a 50-bot fleet. Those
    # lines then inflate the very /logs responses a dashboard polls, so the logging
    # made the problem it was meant to help diagnose measurably worse.
    TRADE_SNAPSHOT = "trade_snapshot"

    def __repr__(self):
        # TODO: do we still need to overwrite __repr__? Impact needs to be looked at in detail
        return self.value


# Enum for parsing requests from ws consumers
class RPCRequestType(StrEnum):
    SUBSCRIBE = "subscribe"

    WHITELIST = "whitelist"
    ANALYZED_DF = "analyzed_df"


NO_ECHO_MESSAGES = (
    RPCMessageType.ANALYZED_DF,
    RPCMessageType.WHITELIST,
    RPCMessageType.NEW_CANDLE,
    RPCMessageType.TRADE_SNAPSHOT,
)
