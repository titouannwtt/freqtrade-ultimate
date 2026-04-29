from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock


RING_BUFFER_MAX = 100_000
BUCKET_SIZE_S = 10
MAX_429_HISTORY = 200
MAX_TIMELINE_BUCKETS = 10_000


@dataclass(slots=True)
class ApiCall:
    ts: float
    method: str
    exchange: str
    latency_ms: float
    cached: bool
    success: bool
    error_type: str | None
    pair: str | None = None


@dataclass
class BucketStats:
    ts: int
    total: int = 0
    cached: int = 0
    direct: int = 0
    errors: int = 0
    errors_429: int = 0
    latency_sum_ms: float = 0.0
    latency_count: int = 0
    by_method: dict[str, int] = field(default_factory=dict)

    @property
    def avg_latency_ms(self) -> float:
        return (
            self.latency_sum_ms / self.latency_count
            if self.latency_count
            else 0.0
        )


class ExchangeMetrics:
    def __init__(self, exchange_name: str) -> None:
        self._exchange = exchange_name
        self._lock = Lock()
        self._calls: deque[ApiCall] = deque(maxlen=RING_BUFFER_MAX)
        self._buckets: dict[int, BucketStats] = {}
        self._recent_429s: deque[ApiCall] = deque(maxlen=MAX_429_HISTORY)

    def record(self, call: ApiCall) -> None:
        with self._lock:
            self._calls.append(call)

            bucket_ts = int(call.ts) // BUCKET_SIZE_S * BUCKET_SIZE_S
            bucket = self._buckets.get(bucket_ts)
            if bucket is None:
                bucket = BucketStats(ts=bucket_ts)
                self._buckets[bucket_ts] = bucket
                self._prune_old_buckets()

            bucket.total += 1
            if call.cached:
                bucket.cached += 1
            else:
                bucket.direct += 1
            if not call.success:
                bucket.errors += 1
            if call.error_type == "429":
                bucket.errors_429 += 1
                self._recent_429s.append(call)

            if not call.cached:
                bucket.latency_sum_ms += call.latency_ms
                bucket.latency_count += 1

            bucket.by_method[call.method] = (
                bucket.by_method.get(call.method, 0) + 1
            )

    def get_timeline(
        self, since_s: float, bucket_s: int = 10,
    ) -> list[dict]:
        cutoff = time.time() - since_s
        with self._lock:
            result = []
            for ts in sorted(self._buckets):
                if ts >= cutoff:
                    b = self._buckets[ts]
                    result.append({
                        "ts": b.ts,
                        "total": b.total,
                        "cached": b.cached,
                        "direct": b.direct,
                        "errors": b.errors,
                        "errors_429": b.errors_429,
                        "avg_latency_ms": round(b.avg_latency_ms, 1),
                        "by_method": dict(b.by_method),
                    })
            if bucket_s != BUCKET_SIZE_S:
                result = self._rebucket(result, bucket_s)
            return result

    def get_summary(self, window_s: int = 3600) -> dict:
        cutoff = time.time() - window_s
        totals = {
            "total": 0, "cached": 0, "direct": 0,
            "errors": 0, "errors_429": 0,
        }
        by_method: dict[str, dict] = {}
        latencies: list[float] = []

        with self._lock:
            for call in reversed(self._calls):
                if call.ts < cutoff:
                    break
                totals["total"] += 1
                if call.cached:
                    totals["cached"] += 1
                else:
                    totals["direct"] += 1
                    latencies.append(call.latency_ms)
                if not call.success:
                    totals["errors"] += 1
                if call.error_type == "429":
                    totals["errors_429"] += 1

                m = by_method.get(call.method)
                if m is None:
                    m = {
                        "count": 0, "cached": 0, "direct": 0,
                        "errors": 0, "latencies": [],
                    }
                    by_method[call.method] = m
                m["count"] += 1
                if call.cached:
                    m["cached"] += 1
                else:
                    m["direct"] += 1
                    m["latencies"].append(call.latency_ms)
                if not call.success:
                    m["errors"] += 1

        latencies.sort()
        method_summary = {}
        for name, m in by_method.items():
            lats = sorted(m["latencies"])
            n = len(lats)
            method_summary[name] = {
                "count": m["count"],
                "cached": m["cached"],
                "direct": m["direct"],
                "errors": m["errors"],
                "avg_latency_ms": round(
                    sum(lats) / n, 1,
                ) if n else 0,
                "p95_latency_ms": round(
                    lats[int(n * 0.95)], 1,
                ) if n else 0,
            }

        n_lat = len(latencies)
        return {
            **totals,
            "avg_latency_ms": round(
                sum(latencies) / n_lat, 1,
            ) if n_lat else 0,
            "p95_latency_ms": round(
                latencies[int(n_lat * 0.95)], 1,
            ) if n_lat else 0,
            "by_method": method_summary,
        }

    def get_recent_429s(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [
                {
                    "ts": c.ts, "method": c.method,
                    "exchange": c.exchange, "pair": c.pair,
                }
                for c in list(self._recent_429s)[-limit:]
            ]

    def _prune_old_buckets(self) -> None:
        if len(self._buckets) >= MAX_TIMELINE_BUCKETS:
            cutoff = time.time() - 100_000
            to_remove = [ts for ts in self._buckets if ts < cutoff]
            for ts in to_remove:
                del self._buckets[ts]

    @staticmethod
    def _rebucket(buckets: list[dict], target_s: int) -> list[dict]:
        if not buckets:
            return []
        merged: dict[int, dict] = {}
        for b in buckets:
            key = b["ts"] // target_s * target_s
            if key not in merged:
                merged[key] = {
                    "ts": key, "total": 0, "cached": 0,
                    "direct": 0, "errors": 0, "errors_429": 0,
                    "latency_sum": 0.0, "latency_count": 0,
                    "by_method": {},
                }
            m = merged[key]
            m["total"] += b["total"]
            m["cached"] += b["cached"]
            m["direct"] += b["direct"]
            m["errors"] += b["errors"]
            m["errors_429"] += b["errors_429"]
            avg = b.get("avg_latency_ms", 0)
            direct = b.get("direct", 0)
            m["latency_sum"] += avg * direct
            m["latency_count"] += direct
            for method, count in b.get("by_method", {}).items():
                m["by_method"][method] = (
                    m["by_method"].get(method, 0) + count
                )
        result = []
        for v in sorted(merged.values(), key=lambda x: x["ts"]):
            lc = v.pop("latency_count")
            ls = v.pop("latency_sum")
            v["avg_latency_ms"] = round(ls / lc, 1) if lc else 0
            result.append(v)
        return result
