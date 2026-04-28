import time
from threading import Thread
from unittest.mock import MagicMock

import pytest

from freqtrade.exchange.exchange_metrics import (
    BUCKET_SIZE_S,
    ApiCall,
    BucketStats,
    ExchangeMetrics,
)


def _make_call(
    method="fetch_ticker", exchange="hyperliquid",
    latency_ms=50.0, cached=False, success=True,
    error_type=None, pair=None, ts=None,
) -> ApiCall:
    return ApiCall(
        ts=ts or time.time(),
        method=method,
        exchange=exchange,
        latency_ms=latency_ms,
        cached=cached,
        success=success,
        error_type=error_type,
        pair=pair,
    )


class TestBucketStats:
    def test_avg_latency_no_data(self):
        b = BucketStats(ts=0)
        assert b.avg_latency_ms == 0.0

    def test_avg_latency_with_data(self):
        b = BucketStats(ts=0, latency_sum_ms=300.0, latency_count=3)
        assert b.avg_latency_ms == 100.0


class TestExchangeMetrics:
    def test_record_basic(self):
        m = ExchangeMetrics("test")
        call = _make_call()
        m.record(call)

        summary = m.get_summary(window_s=60)
        assert summary["total"] == 1
        assert summary["direct"] == 1
        assert summary["cached"] == 0
        assert summary["errors"] == 0

    def test_record_cached(self):
        m = ExchangeMetrics("test")
        m.record(_make_call(cached=True, latency_ms=2.0))

        summary = m.get_summary(window_s=60)
        assert summary["total"] == 1
        assert summary["cached"] == 1
        assert summary["direct"] == 0
        assert summary["avg_latency_ms"] == 0

    def test_record_429(self):
        m = ExchangeMetrics("test")
        m.record(_make_call(
            success=False, error_type="429", latency_ms=10.0,
        ))

        summary = m.get_summary(window_s=60)
        assert summary["errors"] == 1
        assert summary["errors_429"] == 1

        events = m.get_recent_429s()
        assert len(events) == 1
        assert events[0]["method"] == "fetch_ticker"

    def test_record_error_non_429(self):
        m = ExchangeMetrics("test")
        m.record(_make_call(success=False, error_type="error"))

        summary = m.get_summary(window_s=60)
        assert summary["errors"] == 1
        assert summary["errors_429"] == 0

    def test_timeline_basic(self):
        m = ExchangeMetrics("test")
        now = time.time()
        m.record(_make_call(ts=now))
        m.record(_make_call(ts=now + 1))

        timeline = m.get_timeline(since_s=60)
        assert len(timeline) >= 1
        total = sum(b["total"] for b in timeline)
        assert total == 2

    def test_timeline_window_filter(self):
        m = ExchangeMetrics("test")
        now = time.time()
        m.record(_make_call(ts=now - 120))
        m.record(_make_call(ts=now))

        timeline = m.get_timeline(since_s=60)
        total = sum(b["total"] for b in timeline)
        assert total == 1

    def test_timeline_rebucket(self):
        m = ExchangeMetrics("test")
        now = time.time()
        base = int(now) // 60 * 60
        for i in range(6):
            m.record(_make_call(ts=base + i * BUCKET_SIZE_S))

        timeline = m.get_timeline(since_s=120, bucket_s=60)
        total = sum(b["total"] for b in timeline)
        assert total == 6
        assert len(timeline) <= 2

    def test_summary_by_method(self):
        m = ExchangeMetrics("test")
        for _ in range(5):
            m.record(_make_call(method="fetch_ohlcv", latency_ms=100))
        for _ in range(3):
            m.record(_make_call(method="get_tickers", latency_ms=50))

        summary = m.get_summary(window_s=60)
        assert summary["total"] == 8
        by_m = summary["by_method"]
        assert by_m["fetch_ohlcv"]["count"] == 5
        assert by_m["get_tickers"]["count"] == 3
        assert by_m["fetch_ohlcv"]["avg_latency_ms"] == 100.0

    def test_summary_p95(self):
        m = ExchangeMetrics("test")
        for i in range(100):
            m.record(_make_call(latency_ms=float(i + 1)))

        summary = m.get_summary(window_s=60)
        assert summary["p95_latency_ms"] >= 95.0

    def test_ring_buffer_overflow(self):
        m = ExchangeMetrics("test")
        m._calls = type(m._calls)(maxlen=100)

        for i in range(150):
            m.record(_make_call(ts=time.time()))

        assert len(m._calls) == 100

    def test_prune_old_buckets(self):
        m = ExchangeMetrics("test")
        old_ts = time.time() - 100000
        for i in range(10000):
            m.record(_make_call(ts=old_ts + i * BUCKET_SIZE_S))

        assert len(m._buckets) <= 8640 + 100

    def test_thread_safety(self):
        m = ExchangeMetrics("test")
        errors = []

        def writer(n):
            try:
                for _ in range(100):
                    m.record(_make_call())
            except Exception as e:
                errors.append(e)

        threads = [Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        summary = m.get_summary(window_s=60)
        assert summary["total"] == 1000

    def test_recent_429s_limit(self):
        m = ExchangeMetrics("test")
        for i in range(100):
            m.record(_make_call(
                success=False, error_type="429",
                ts=time.time() + i * 0.01,
            ))

        events = m.get_recent_429s(limit=10)
        assert len(events) == 10

    def test_empty_metrics(self):
        m = ExchangeMetrics("test")
        assert m.get_timeline(since_s=60) == []
        assert m.get_summary(window_s=60)["total"] == 0
        assert m.get_recent_429s() == []

    def test_rebucket_empty(self):
        assert ExchangeMetrics._rebucket([], 60) == []

    def test_rebucket_preserves_methods(self):
        buckets = [
            {"ts": 0, "total": 3, "cached": 1, "direct": 2,
             "errors": 0, "errors_429": 0, "avg_latency_ms": 50.0,
             "by_method": {"fetch_ohlcv": 2, "get_tickers": 1}},
            {"ts": 10, "total": 2, "cached": 0, "direct": 2,
             "errors": 0, "errors_429": 0, "avg_latency_ms": 80.0,
             "by_method": {"fetch_ohlcv": 1, "fetch_order": 1}},
        ]
        result = ExchangeMetrics._rebucket(buckets, 60)
        assert len(result) == 1
        assert result[0]["by_method"]["fetch_ohlcv"] == 3
        assert result[0]["by_method"]["get_tickers"] == 1
        assert result[0]["by_method"]["fetch_order"] == 1


class TestRetryerInstrumentation:
    def test_retrier_records_success(self):
        from freqtrade.exchange.common import retrier

        mock_metrics = MagicMock()

        class FakeExchange:
            name = "test"
            _metrics = mock_metrics

            @retrier
            def do_call(self):
                return "ok"

        ex = FakeExchange()
        result = ex.do_call()
        assert result == "ok"
        assert mock_metrics.record.called
        call_arg = mock_metrics.record.call_args[0][0]
        assert call_arg.method == "do_call"
        assert call_arg.success is True

    def test_retrier_records_429(self):
        from freqtrade.exceptions import DDosProtection
        from freqtrade.exchange.common import retrier

        mock_metrics = MagicMock()
        call_count = 0

        class FakeExchange:
            name = "test"
            _metrics = mock_metrics

            @retrier(retries=0)
            def do_call(self):
                nonlocal call_count
                call_count += 1
                raise DDosProtection("429 Too Many Requests")

        ex = FakeExchange()
        with pytest.raises(DDosProtection):
            ex.do_call()

        assert mock_metrics.record.called
        call_arg = mock_metrics.record.call_args[0][0]
        assert call_arg.error_type == "429"
        assert call_arg.success is False

    def test_retrier_no_metrics_no_crash(self):
        from freqtrade.exchange.common import retrier

        class FakeExchange:
            name = "test"

            @retrier
            def do_call(self):
                return "ok"

        ex = FakeExchange()
        assert ex.do_call() == "ok"

    @pytest.mark.asyncio
    async def test_retrier_async_records_success(self):
        from freqtrade.exchange.common import retrier_async

        mock_metrics = MagicMock()

        class FakeExchange:
            name = "test"
            _metrics = mock_metrics

            @retrier_async
            async def do_call(self):
                return "ok"

        ex = FakeExchange()
        result = await ex.do_call()
        assert result == "ok"
        assert mock_metrics.record.called
        call_arg = mock_metrics.record.call_args[0][0]
        assert call_arg.success is True
