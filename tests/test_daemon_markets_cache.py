"""Resilience of the daemon's shared markets cache.

A live incident on 2026-09-08 showed every bot in the fleet logging
`markets=240.0s` on every cycle: the daemon could not complete a markets fetch,
kept no usable entry, and each waiting bot then started a fetch of its own. These
tests pin the three behaviours that break that loop.
"""

import asyncio
import time

import pytest

from freqtrade.ohlcv_cache.daemon import Daemon, _MarketsCacheEntry


@pytest.fixture(autouse=True)
def _preserve_event_loop():
    """Rend la boucle d'evenements trouvee a l'entree.

    `asyncio.run` ferme sa boucle et n'en laisse aucune courante. D'autres modules de tests
    construisent une boucle une fois par fixture puis appellent `run_until_complete` dessus :
    une boucle effacee faisait echouer leurs tests de delai selon l'ordre de collecte.
    """
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        previous = None
    yield
    asyncio.set_event_loop(previous)


class _Budget:
    def __init__(self):
        self.backoff_calls = []

    async def acquire(self, weight, priority=None):
        return None

    def trigger_backoff(self, factor):
        self.backoff_calls.append(factor)


def _make_daemon(fetch_result=None, fetch_error=None):
    """Minimal Daemon exposing only what _handle_markets touches."""
    d = Daemon.__new__(Daemon)
    d._markets_cache = {}
    d._markets_ttl_s = 3600.0
    d._markets_inflight = {}
    d._markets_failed_at = {}
    d._markets_retry_cooldown_s = 60.0
    d._markets_max_wait_s = 20.0
    d._markets_bg = {}
    d._budget = _Budget()
    d._get_budget = lambda exchange: d._budget
    d._get_weight = lambda exchange, op: 20.0

    calls = {"n": 0}

    class _Client:
        async def load_markets(self):
            calls["n"] += 1
            if fetch_error is not None:
                raise fetch_error
            return fetch_result

    class _Fetcher:
        async def _ensure_client(self):
            return _Client()

    d._get_fetcher = lambda exchange, mode: _Fetcher()
    d._fetch_calls = calls
    return d


REQ = {"op": "markets", "req_id": "r1", "exchange": "hyperliquid", "trading_mode": "futures"}
KEY = "hyperliquid:futures"


def test_fresh_cache_is_served_without_fetching():
    d = _make_daemon(fetch_result={"BTC/USDC:USDC": {}})
    d._markets_cache[KEY] = _MarketsCacheEntry(
        data={"ETH/USDC:USDC": {}}, fetched_at=time.monotonic()
    )

    resp = asyncio.run(d._handle_markets(dict(REQ)))

    assert resp["ok"] is True
    assert resp["served_from"] == "cache"
    assert d._fetch_calls["n"] == 0


def test_failed_fetch_serves_expired_markets_instead_of_an_error():
    """Symbols barely change: stale markets beat making the bot wait out its timeout."""
    d = _make_daemon(fetch_error=RuntimeError("connection reset"))
    d._markets_cache[KEY] = _MarketsCacheEntry(
        data={"ETH/USDC:USDC": {}}, fetched_at=time.monotonic() - 7200
    )

    resp = asyncio.run(d._handle_markets(dict(REQ)))

    assert resp["ok"] is True
    assert resp["served_from"] == "stale_after_error"
    assert resp["data"] == {"ETH/USDC:USDC": {}}
    assert resp["age_s"] > 3600


def test_failed_fetch_without_any_cache_still_reports_the_error():
    d = _make_daemon(fetch_error=RuntimeError("connection reset"))

    resp = asyncio.run(d._handle_markets(dict(REQ)))

    assert resp["ok"] is False
    assert resp["error_type"] == "RuntimeError"


def test_cooldown_stops_the_fleet_from_retrying_a_failing_fetch():
    d = _make_daemon(fetch_error=RuntimeError("boom"))
    d._markets_cache[KEY] = _MarketsCacheEntry(
        data={"ETH/USDC:USDC": {}}, fetched_at=time.monotonic() - 7200
    )

    first = asyncio.run(d._handle_markets(dict(REQ)))
    assert first["served_from"] == "stale_after_error"
    assert d._fetch_calls["n"] == 1

    # 30 more bots ask during the cooldown: none of them may hit the exchange.
    for _ in range(30):
        resp = asyncio.run(d._handle_markets(dict(REQ)))
        assert resp["served_from"] == "stale_during_cooldown"
    assert d._fetch_calls["n"] == 1


def test_cooldown_expires_and_a_successful_fetch_clears_the_failure_marker():
    d = _make_daemon(fetch_result={"BTC/USDC:USDC": {}})
    d._markets_failed_at[KEY] = time.monotonic() - 999
    d._markets_cache[KEY] = _MarketsCacheEntry(
        data={"ETH/USDC:USDC": {}}, fetched_at=time.monotonic() - 7200
    )

    resp = asyncio.run(d._handle_markets(dict(REQ)))

    assert resp["served_from"] == "fetch"
    assert resp["data"] == {"BTC/USDC:USDC": {}}
    assert KEY not in d._markets_failed_at


def test_a_rate_limited_fetch_still_triggers_backoff_while_serving_stale():
    d = _make_daemon(fetch_error=RuntimeError("HTTP 429 too many requests"))
    d._markets_cache[KEY] = _MarketsCacheEntry(
        data={"ETH/USDC:USDC": {}}, fetched_at=time.monotonic() - 7200
    )

    resp = asyncio.run(d._handle_markets(dict(REQ)))

    assert resp["served_from"] == "stale_after_error"
    assert d._budget.backoff_calls == [2.0], "the 429 must still slow the whole fleet down"


@pytest.mark.parametrize("waiters", [5, 30])
def test_waiters_of_a_failed_fetch_do_not_each_start_their_own(waiters):
    """One failure used to become one exchange fetch per waiting bot."""
    d = _make_daemon(fetch_error=RuntimeError("boom"))
    d._markets_cache[KEY] = _MarketsCacheEntry(
        data={"ETH/USDC:USDC": {}}, fetched_at=time.monotonic() - 7200
    )

    async def _run():
        return await asyncio.gather(*(d._handle_markets(dict(REQ)) for _ in range(waiters)))

    responses = asyncio.run(_run())

    assert all(r["ok"] for r in responses)
    assert d._fetch_calls["n"] == 1, "only the leader may hit the exchange"


class TestSlowMarketsFetch:
    """Une recuperation de marches lente ne doit pas bloquer le bot.

    Le client abandonne au bout de 240 s. Sur un pas de 5 minutes, ce blocage fait sauter
    environ quatre bougies sur TOUTES les paires a la fois, a chaque expiration du TTL :
    mesure le 2026-09-09, deux heures consecutives a 77 % de captation contre 92 % en
    regime normal. Passe un court delai, on sert l'entree perimee.
    """

    @staticmethod
    def _daemon(fetch_delay, result=None, max_wait=0.15):
        d = _make_daemon(fetch_result=result or {"BTC/USDC:USDC": {}})
        d._markets_max_wait_s = max_wait
        calls = {"n": 0, "fini": False}

        class _Client:
            async def load_markets(self):
                calls["n"] += 1
                await asyncio.sleep(fetch_delay)
                calls["fini"] = True
                return result or {"BTC/USDC:USDC": {}}

        class _Fetcher:
            async def _ensure_client(self):
                return _Client()

        d._get_fetcher = lambda exchange, mode: _Fetcher()
        d._calls = calls
        return d

    def test_une_recuperation_lente_sert_l_entree_perimee(self):
        d = self._daemon(fetch_delay=5.0)
        d._markets_cache[KEY] = _MarketsCacheEntry(
            data={"ETH/USDC:USDC": {}}, fetched_at=time.monotonic() - 7200
        )

        resp = asyncio.run(d._handle_markets(dict(REQ)))

        assert resp["ok"] is True
        assert resp["served_from"] == "stale_slow_fetch"
        assert resp["data"] == {"ETH/USDC:USDC": {}}

    def test_la_recuperation_n_est_pas_annulee_et_alimente_le_cache(self):
        """Sans shield, le depassement annulerait la tache et le cache ne se remplirait jamais."""

        async def _run():
            d = self._daemon(fetch_delay=0.3, result={"NEW/USDC:USDC": {}})
            d._markets_cache[KEY] = _MarketsCacheEntry(
                data={"OLD/USDC:USDC": {}}, fetched_at=time.monotonic() - 7200
            )
            resp = await d._handle_markets(dict(REQ))
            assert resp["served_from"] == "stale_slow_fetch"
            await asyncio.sleep(0.6)  # laisse la recuperation de fond se terminer
            return d

        d = asyncio.run(_run())
        assert d._calls["fini"] is True, "la recuperation a ete annulee"
        assert d._markets_cache[KEY].data == {"NEW/USDC:USDC": {}}, "cache non alimente"
        assert KEY not in d._markets_failed_at

    def test_une_recuperation_rapide_passe_normalement(self):
        d = self._daemon(fetch_delay=0.0, result={"BTC/USDC:USDC": {}})

        resp = asyncio.run(d._handle_markets(dict(REQ)))

        assert resp["served_from"] == "fetch"
        assert resp["data"] == {"BTC/USDC:USDC": {}}

    def test_sans_entree_perimee_on_attend_la_recuperation(self):
        """Mieux vaut attendre que renvoyer une erreur quand on n'a rien en main."""
        d = self._daemon(fetch_delay=0.3, result={"BTC/USDC:USDC": {}})

        resp = asyncio.run(d._handle_markets(dict(REQ)))

        assert resp["ok"] is True
        assert resp["served_from"] == "fetch"

    def test_une_seule_recuperation_de_fond_a_la_fois(self):
        """Le bug du 2026-09-10 : le bloc finally retirait le marqueur pendant que la tache
        de fond tournait, donc CHAQUE requete suivante lancait un nouveau load_markets.
        Avec 36 bots, tempete sur l'appel le plus lourd de la place."""

        async def _run():
            d = self._daemon(fetch_delay=3.0, result={"NEW/USDC:USDC": {}})
            d._markets_cache[KEY] = _MarketsCacheEntry(
                data={"OLD/USDC:USDC": {}}, fetched_at=time.monotonic() - 7200
            )
            premieres = await d._handle_markets(dict(REQ))
            assert premieres["served_from"] == "stale_slow_fetch"
            # 20 bots demandent pendant que la recuperation de fond tourne encore
            suivantes = [await d._handle_markets(dict(REQ)) for _ in range(20)]
            return d, suivantes

        d, suivantes = asyncio.run(_run())
        assert all(r["served_from"] == "stale_bg_fetch_running" for r in suivantes)
        assert d._calls["n"] == 1, f"{d._calls['n']} recuperations lancees au lieu d'une seule"
