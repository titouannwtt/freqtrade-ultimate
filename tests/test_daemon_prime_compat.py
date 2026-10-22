"""Compatibility of the PRIME band in both directions, on a real daemon and a real socket.

* old client -> new daemon: a client that knows nothing of ``prime`` / ``priority_bot``
  gets exactly the behaviour it had, and does not arm the reserve.
* new client -> old daemon: the daemon the fleet runs today (read from git, the last
  commit before this feature) accepts the new fields, serves a PRIME request and does
  not choke on the registration. This is what lets the bots be deployed before the
  daemon is restarted.

Every daemon here listens on a socket under ``tmp_path`` and talks to a fake exchange
(see ``tests/ftcache_sim.py``): no real daemon, persistence directory or exchange is
touched.
"""

from __future__ import annotations

import asyncio

import pytest

from freqtrade.ohlcv_cache.client import OhlcvCacheClient
from tests.ftcache_sim import PrivateDaemon, SimConn, load_legacy_daemon_module


@pytest.fixture(autouse=True)
def _preserve_event_loop():
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        previous = None
    yield
    asyncio.set_event_loop(previous)


@pytest.fixture
def new_daemon(tmp_path):
    with PrivateDaemon(tmp_path) as d:
        yield d


@pytest.fixture
def legacy_daemon(tmp_path):
    module = load_legacy_daemon_module()
    if module is None:
        pytest.skip("pre-PRIME daemon.py not readable from git in this checkout")
    assert not hasattr(module.TokenBucket, "PRIME"), "the reference must predate the feature"
    with PrivateDaemon(tmp_path, daemon_module=module) as d:
        yield d


def _fetch_payload(pair="BTC/USDC:USDC", **kw):
    payload = {
        "op": "fetch",
        "exchange": "hyperliquid",
        "trading_mode": "futures",
        "pair": pair,
        "timeframe": "15m",
        "candle_type": "futures",
        "since_ms": None,
        "limit": 5,
        "priority": 1,
        "capital": 100.0,
    }
    payload.update(kw)
    return payload


# --------------------------------------------------------------------------- old client, new daemon


def test_old_client_gets_the_same_service_from_the_new_daemon(new_daemon):
    async def scenario():
        conn = await SimConn(new_daemon.socket_path).open()
        # An old client registers without `priority_bot` and never sends `prime`.
        reg = await conn.request(
            {
                "op": "register",
                "bot_id": "old-bot",
                "exchange": "hyperliquid",
                "trading_mode": "futures",
                "strategy": "S",
                "timeframe": "15m",
                "dry_run": False,
                "api_port": 0,
                "pid": 1,
            }
        )
        assert reg["ok"] is True
        for band in (0, 1, 2, 3):
            resp = await conn.request(
                {"op": "acquire", "exchange": "hyperliquid", "priority": band, "cost": 1}
            )
            assert resp["ok"] is True
        fetched = await conn.request(_fetch_payload())
        assert fetched["ok"] is True and len(fetched["data"]) >= 1
        stats = await conn.request({"op": "stats"})
        await conn.close()
        return stats

    stats = asyncio.run(scenario())
    bucket = new_daemon.daemon.budgets["hyperliquid"]
    assert bucket.prime_enabled is False and bucket.gate_ratio == pytest.approx(0.80)
    assert stats["prime"]["armed"] is False
    assert stats["prime_ineligible_requests"] == 0
    assert stats["bands"]["critical"]["grants_total"] >= 1
    assert stats["bands"]["prime"]["grants_total"] == 0
    # the fake exchange saw an ordinary HIGH request with the bot's own capital
    assert new_daemon.fetcher.calls[-1]["priority"] == 1
    assert new_daemon.fetcher.calls[-1]["capital"] == 100.0


def test_an_old_client_asking_for_a_negative_priority_is_served_as_critical(new_daemon):
    """Nothing but a priority bot may use the band, whatever the wire says."""

    async def scenario():
        conn = await SimConn(new_daemon.socket_path).open()
        await conn.register("odd-bot")
        resp = await conn.request(
            {"op": "acquire", "exchange": "hyperliquid", "priority": -1, "cost": 1}
        )
        stats = await conn.request({"op": "stats"})
        await conn.close()
        return resp, stats

    resp, stats = asyncio.run(scenario())
    assert resp["ok"] is True
    assert stats["bands"]["critical"]["grants_total"] == 1
    assert stats["bands"]["prime"]["grants_total"] == 0
    assert stats["prime_ineligible_requests"] == 1


# --------------------------------------------------------------------------- new client, new daemon


def test_a_priority_client_is_served_in_prime_by_the_new_daemon(new_daemon):
    async def scenario():
        client = OhlcvCacheClient(
            new_daemon.socket_path,
            timeout_s=10.0,
            exchange_id="hyperliquid",
            trading_mode="futures",
            priority_bot=True,
        )
        client.set_bot_identity(
            {"bot_id": "multi", "exchange": "hyperliquid", "dry_run": False, "pid": 1}
        )
        await client.acquire_rate_token(priority=OhlcvCacheClient.PRIME, cost=2.0)
        await client.fetch("BTC/USDC:USDC", "15m", "futures", None, 5)
        bucket = new_daemon.daemon.budgets["hyperliquid"]
        seen = {
            "hold_off": client.hold_off_s,
            "armed": bucket.prime_enabled,
            "prime_grants": bucket.grants_by_band.get(-1),
            "prime_weight": dict(bucket.prime_weight_by_bot),
        }
        await client.close()
        return seen

    seen = asyncio.run(scenario())
    assert seen["hold_off"] == 0.0
    assert seen["armed"] is True
    assert seen["prime_grants"] == 1
    assert seen["prime_weight"] == {"multi": 2.0}
    candle = new_daemon.fetcher.calls[-1]
    assert candle["priority"] == 1, "candles are not PRIME"
    assert candle["capital"] >= 1e9, "...but they lead their band"


def test_the_reserve_disarms_when_the_priority_client_disconnects(new_daemon):
    async def scenario():
        client = OhlcvCacheClient(
            new_daemon.socket_path,
            timeout_s=10.0,
            exchange_id="hyperliquid",
            trading_mode="futures",
            priority_bot=True,
        )
        client.set_bot_identity({"bot_id": "multi", "exchange": "hyperliquid", "pid": 1})
        await client.acquire_rate_token(priority=OhlcvCacheClient.PRIME)
        armed = new_daemon.daemon.budgets["hyperliquid"].prime_enabled
        await client.close()  # clean unregister
        await asyncio.sleep(0.3)
        return armed

    assert asyncio.run(scenario()) is True
    assert new_daemon.daemon.budgets["hyperliquid"].prime_enabled is False


# --------------------------------------------------------------------------- new client, old daemon


def test_a_priority_client_works_against_the_pre_prime_daemon(legacy_daemon):
    """The bots are deployed first: their PRIME traffic must be served by today's daemon."""

    async def scenario():
        client = OhlcvCacheClient(
            legacy_daemon.socket_path,
            timeout_s=10.0,
            exchange_id="hyperliquid",
            trading_mode="futures",
            priority_bot=True,
        )
        client.set_bot_identity(
            {"bot_id": "multi", "exchange": "hyperliquid", "dry_run": False, "pid": 1}
        )
        started = asyncio.get_event_loop().time()
        await client.acquire_rate_token(priority=OhlcvCacheClient.PRIME, cost=2.0)
        waited = asyncio.get_event_loop().time() - started
        data = await client.fetch("BTC/USDC:USDC", "15m", "futures", None, 5)
        status = await client.fleet_status()
        await client.close()
        return waited, data, status

    waited, data, status = asyncio.run(scenario())
    assert waited < 2.0
    assert len(data[3]) >= 1
    # the old daemon ignored the unknown fields and registered the bot normally
    assert status["ok"] is True
    assert "multi" in str(status)


def test_the_pre_prime_daemon_serves_a_negative_priority_ahead_of_critical(legacy_daemon):
    """The property the phase-1 rollout relies on, measured on the real old daemon."""

    async def scenario():
        bucket = legacy_daemon.daemon._get_budget("hyperliquid")
        legacy_daemon.call(setattr, bucket, "tokens", 0.0)
        order = []

        async def one(priority, label):
            conn = await SimConn(legacy_daemon.socket_path).open()
            await conn.register(f"b-{label}", priority_bot=False)
            resp = await conn.request(
                {"op": "acquire", "exchange": "hyperliquid", "priority": priority, "cost": 2}
            )
            order.append(label)
            await conn.close()
            return resp

        # CRITICAL first in time, PRIME right after: both wait for tokens
        crit = asyncio.ensure_future(one(0, "critical"))
        await asyncio.sleep(0.05)
        prime = asyncio.ensure_future(one(-1, "prime"))
        await asyncio.gather(crit, prime)
        return order

    assert asyncio.run(scenario()) == ["prime", "critical"]


def test_the_old_daemon_has_no_prime_block_in_its_stats_and_the_client_copes(legacy_daemon):
    """``fleet_status`` and friends are unchanged; the new fields are simply absent."""

    async def scenario():
        conn = await SimConn(legacy_daemon.socket_path).open()
        await conn.register("multi", priority_bot=True)
        stats = await conn.request({"op": "stats"})
        await conn.close()
        return stats

    stats = asyncio.run(scenario())
    assert stats["ok"] is True
    assert "prime" not in stats


def test_identity_set_after_the_first_connection_still_registers(new_daemon):
    """Real bot order: the exchange connects during its init, FreqtradeBot sets the identity
    afterwards. The bot must register on its next request, not only after a reconnect."""

    async def scenario():
        client = OhlcvCacheClient(
            new_daemon.socket_path,
            timeout_s=10.0,
            exchange_id="hyperliquid",
            trading_mode="futures",
            priority_bot=True,
        )
        await client.fetch("BTC/USDC:USDC", "15m", "futures", None, 5)  # connected, anonymous
        before = new_daemon.daemon.budgets["hyperliquid"].prime_enabled
        client.set_bot_identity(
            {"bot_id": "multi", "exchange": "hyperliquid", "dry_run": False, "pid": 1}
        )
        await client.acquire_rate_token(priority=OhlcvCacheClient.PRIME, cost=2.0)
        after = new_daemon.daemon.budgets["hyperliquid"].prime_enabled
        registered = client._registered
        await client.close()
        return before, after, registered

    before, after, registered = asyncio.run(scenario())
    assert before is False
    assert registered is True
    assert after is True
