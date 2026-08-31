"""
Newline-delimited JSON protocol between bots (clients) and the daemon.

Phase 0 uses JSON for easy debugging; a later phase will switch to msgpack
for throughput.

Messages are one JSON object per line, UTF-8 encoded, terminated by `\\n`.
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Any


PROTOCOL_VERSION = 2


@dataclass
class FetchRequest:
    req_id: str
    exchange: str
    trading_mode: str  # "spot" | "futures"
    pair: str
    timeframe: str
    candle_type: str  # CandleType string value
    since_ms: int | None = None
    limit: int | None = None
    op: str = "fetch"
    priority: int = 2  # 0=CRITICAL, 1=HIGH, 2=NORMAL, 3=LOW
    capital: float = 0.0  # stake capital for intra-priority ordering


@dataclass
class PingRequest:
    op: str = "ping"
    req_id: str = ""


@dataclass
class FetchResponse:
    req_id: str
    ok: bool
    pair: str = ""
    timeframe: str = ""
    candle_type: str = ""
    data: list = field(default_factory=list)  # list of [ts, o, h, l, c, v]
    drop_incomplete: bool = True
    served_from: str = ""  # "cache" | "fetch" | "fallback"
    latency_ms: float = 0.0
    error_type: str = ""
    error_message: str = ""


@dataclass
class PongResponse:
    req_id: str = ""
    ok: bool = True
    daemon_version: int = PROTOCOL_VERSION
    uptime_s: float = 0.0


@dataclass
class RegisterRequest:
    bot_id: str = ""
    config_file: str = ""
    exchange: str = ""
    trading_mode: str = ""
    strategy: str = ""
    timeframe: str = ""
    pairs_count: int = 0
    dry_run: bool = False
    api_port: int = 0
    pid: int = 0
    op: str = "register"
    req_id: str = ""


@dataclass
class StateUpdateRequest:
    state: str = ""
    pairs_count: int = 0
    op: str = "state_update"
    req_id: str = ""


try:  # pragma: no cover - exercised by whichever branch the host provides
    import orjson

    _ORJSON_NUMPY = orjson.OPT_SERIALIZE_NUMPY
except ImportError:  # pragma: no cover
    orjson = None  # type: ignore[assignment]
    _ORJSON_NUMPY = 0


def dumps(obj: Any) -> bytes:
    """Serialize one NDJSON frame.

    ⚠️ This is the hottest path in the daemon, by a wide margin. Profiled on a
    45-bot fleet (py-spy, 3055 samples): 57% of the daemon's CPU sat in
    `json.encoder.iterencode` here, plus 32% in the `.tolist()` that fed it, i.e.
    ~89% of a saturated single asyncio thread (measured at 85.7% of one core).
    The cause is volume, not inefficiency: bots ask for 5000 candles per refresh,
    so a single response is ~298 KB and the daemon encodes ~12 MB/s.

    That CPU wall, NOT the exchange rate limit and NOT a cache miss, is what made
    the `candles` phase take 13-17s median and occasionally trip `CacheTimedOut`.
    Worth stating plainly because the obvious suspect was wrong: the OHLCV cache
    serves ~100% of requests without touching the venue (106 misses out of
    2 239 190). The 13% "hit rate" the API reports is a broken gauge, not a
    broken cache. See `stats` handling in daemon.py.

    orjson serializes a numpy array directly, skipping the intermediate Python
    list entirely: measured here at 24.7ms -> 2.1ms for a 5000x6 float64 block,
    a 11.7x saving on ~89% of the load. The bytes it emits are ordinary JSON, so
    clients parse them unchanged: this is a daemon-side change only, no bot
    restart required.

    The stdlib fallback is deliberate, not defensive clutter: this fork is public
    and must keep working for someone who has two bots and no orjson.
    """
    if hasattr(obj, "__dataclass_fields__"):
        payload = asdict(obj)
    elif isinstance(obj, dict):
        payload = obj
    else:
        raise TypeError(f"Cannot serialize {type(obj)!r}")
    if orjson is not None:
        return orjson.dumps(payload, option=_ORJSON_NUMPY) + b"\n"
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def loads_request(line: bytes) -> dict:
    return json.loads(line.decode("utf-8"))


def loads_response(line: bytes) -> dict:
    return json.loads(line.decode("utf-8"))
