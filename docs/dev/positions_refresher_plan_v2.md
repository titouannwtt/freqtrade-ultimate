# Plan v2 — Rafraîchisseur de positions côté mixin (implémentation-ready)

## 0. Objectif & invariants

**But** : un thread de fond par bot maintient le cache positions frais à cadence fixe, découplé
de `process()` et du backoff OHLCV. `fetch_positions()` lit ce cache (instantané, jamais bloquant).
HL → `/info clearinghouseState` public (address-only) ; autres exchanges → `fetch_positions` signé.
Le daemon reste OHLCV-only → **plus de SPOF ni de contention positions**.

**Invariants non négociables**
- I1. Un bot n'agit **jamais** sur des positions au-delà d'un seuil dur de péremption (circuit breaker).
- I2. Le cache positions ne **régresse jamais** vers des données plus anciennes (monotonic).
- I3. Le thread de refresh ne **meurt jamais** en silence (double garde + watchdog).
- I4. Actif **live uniquement** (jamais en dry/replay ; book simulé côté dry).
- I5. Toute décision de netting/coordination lit un cache **frais ou refuse**.

## 1. Config (`ohlcv_cache/defaults.py` + override `shared_ohlcv_cache`)

```python
"positions_refresh_enabled": True,
"positions_refresh_interval_s": 10,        # cadence nominale
"positions_refresh_jitter_pct": 0.3,       # ±30% pour désynchroniser la flotte
"positions_refresh_backoff_max_s": 120,    # plafond backoff sur échecs consécutifs
"positions_soft_stale_s": 45,              # = _STALE_POSITIONS_MAX_AGE_S : au-delà → fetch direct de secours
"positions_hard_stale_s": 90,              # CIRCUIT BREAKER : au-delà → refus d'actions risquées
"positions_equiv_check_interval_s": 3600,  # cross-check public vs signé (HL)
"positions_report_to_daemon": True,        # push observabilité (non bloquant)
```
Réutiliser `_STALE_POSITIONS_WARN_AGE_S = 120` (log WARNING sur cache servi vieux).

## 2. Nouveaux attributs du mixin (`OhlcvCacheMixin`)

```python
self._pos_lock = threading.Lock()          # protège cache + timestamps (lu par process(), écrit par thread)
self._pos_stop = threading.Event()
self._pos_thread: threading.Thread | None = None
self._pos_refresher_active = False
self._pos_last_fetched_at = 0.0            # monotonic de l'instant de fetch (pour I2)
self._pos_consecutive_fail = 0
self._pos_force_event = threading.Event()  # refresh événementiel (fills)
self._pos_source = "signed"                # "hl_public" | "signed" (déterminé au start)
# compteurs (via _ftcache_bump / ftcache_get_stats): refresh_ok/429/err, served_cache,
#   fallback_direct, hard_stale_block, equiv_divergence
```

## 3. Composant A — Garde monotonic (transverse, bénéficie aussi à l'existant)

Modifier `_ftcache_save_positions` pour **rejeter les écritures out-of-order** :

```python
def _ftcache_save_positions(self, positions: list, *, fetched_at: float | None = None) -> None:
    fa = fetched_at if fetched_at is not None else time.monotonic()
    with self._pos_lock:
        if fa <= self._pos_last_fetched_at:
            logger.debug("[positions] écriture out-of-order ignorée (fa=%.3f <= last=%.3f)",
                         fa, self._pos_last_fetched_at)
            return
        self._ftcache_last_positions = positions
        self._ftcache_last_positions_ts = time.monotonic()
        self._pos_last_fetched_at = fa
        self._ftcache_open_pairs = frozenset(
            p["symbol"] for p in positions if (p.get("contracts") or 0)
        )
```
> `fetched_at` doit être capturé **avant** l'appel réseau, pas après, pour ordonner correctement.

## 4. Composant B — Le refresher thread (loop + backoff + jitter + watchdog)

```python
def _positions_refresh_loop(self) -> None:
    # I3: la boucle elle-même ne doit jamais laisser filer une exception
    while not self._pos_stop.is_set():
        try:
            self._ftcache_refresh_positions_once()
            self._pos_consecutive_fail = 0
        except Exception as e:  # filet ultime — le thread survit à tout
            self._pos_consecutive_fail += 1
            logger.warning("[positions-refresh] itération en échec (#%d): %r",
                           self._pos_consecutive_fail, e)
        # cadence + jitter + backoff adaptatif
        base = self._pos_interval
        if self._pos_consecutive_fail:
            base = min(base * (2 ** self._pos_consecutive_fail), self._pos_backoff_max)
        jitter = base * self._pos_jitter_pct * (self._deterministic_jitter())  # [-1,1]
        wait = max(1.0, base + jitter)
        # réveil anticipé si refresh événementiel demandé
        self._pos_force_event.wait(timeout=wait)
        self._pos_force_event.clear()
    logger.info("[positions-refresh] thread arrêté")
```

`_ftcache_refresh_positions_once()` :
```python
def _ftcache_refresh_positions_once(self) -> None:
    fetched_at = time.monotonic()
    # I5 bis: si un 429 IP global est actif côté daemon, ralentir aussi
    if self._ftcache_ip_backoff_active():
        logger.debug("[positions-refresh] backoff IP actif — skip ce tour")
        return
    try:
        if self._pos_source == "hl_public":
            positions = self._fetch_positions_hl_public()   # publicPostInfo, non signé
        else:
            positions = super().fetch_positions()           # signé
        self._ftcache_save_positions(positions, fetched_at=fetched_at)  # I2 monotonic
        self._ftcache_bump("positions_refresh_ok")
        logger.debug("[positions-refresh] %s: %d positions en %.2fs",
                     self._pos_source, len(positions), time.monotonic()-fetched_at)
        if self._pos_report_to_daemon:
            try:
                self._ftcache_run_on_loop(self._ftcache_get_client().push_positions(positions))
            except Exception as e:
                logger.debug("[positions-refresh] push daemon non bloquant échoué: %s", e)
        self._maybe_equivalence_check(positions, fetched_at)   # §6
        raise StopIteration  # (pseudo) succès → sort du try
    except DDosProtection as e:
        age = time.monotonic() - self._ftcache_last_positions_ts
        logger.warning("[positions-refresh] 429 — conserve cache (age=%.0fs): %s", age, e)
        self._ftcache_bump("positions_refresh_429"); raise
    except StopIteration:
        return
    except Exception as e:
        age = time.monotonic() - (self._ftcache_last_positions_ts or 0)
        logger.warning("[positions-refresh] échec — conserve cache (age=%.0fs): %r", age, e)
        self._ftcache_bump("positions_refresh_err"); raise
```

**Watchdog** (dans `process()` ou le heartbeat du worker, coût nul) :
```python
def _positions_watchdog(self) -> None:
    if not self._pos_refresher_active:
        return
    if self._pos_thread is None or not self._pos_thread.is_alive():
        logger.error("[positions-refresh] thread MORT — redémarrage")
        self._ftcache_start_positions_refresher(restart=True)
        return
    age = time.monotonic() - self._ftcache_last_positions_ts
    if age > self._pos_hard_stale:
        logger.error("[positions-refresh] cache figé (age=%.0fs > hard=%ds) — refresh forcé",
                     age, self._pos_hard_stale)
        self._pos_force_event.set()
```

## 5. Composant C — Fetch HL public + choix de source

Au `start` : déterminer la source une fois.
```python
def _resolve_positions_source(self) -> str:
    if self.name == "hyperliquid" and self.trading_mode == TradingMode.FUTURES \
       and not self._config["dry_run"] and getattr(self._api, "walletAddress", None):
        return "hl_public"
    return "signed"
```
`_fetch_positions_hl_public()` — réutilise le pattern existant (`hyperliquid.py:89`) :
```python
raw = self._api.publicPostInfo({"type": "clearinghouseState", "user": self._api.walletAddress})
return self._api.parse_positions(raw.get("assetPositions", []))  # parser ccxt HL
```
> ⚠️ **Test préalable (1 ligne)** : vérifier si `ccxt.hyperliquid.fetch_positions()` signe ou non.
> S'il est déjà public (address-only), remplacer `_fetch_positions_hl_public` par un `super().fetch_positions()`
> **direct** (bypass daemon) — moins de mapping maison, moins de risque de divergence de format.

## 6. Composant — Validation d'équivalence public vs signé (HL)

Périodiquement (`positions_equiv_check_interval_s`), comparer le résultat public au signé et **logger toute divergence de champ** utilisé par freqtrade (`symbol, side, contracts, entryPrice, leverage, unrealizedPnl`) :
```python
def _maybe_equivalence_check(self, public_positions, fetched_at):
    if self._pos_source != "hl_public": return
    if time.monotonic() - self._pos_last_equiv < self._pos_equiv_interval: return
    self._pos_last_equiv = time.monotonic()
    try:
        signed = super().fetch_positions()
        diffs = _diff_positions(public_positions, signed, keys=("side","contracts","entryPrice","leverage"))
        if diffs:
            logger.error("[positions-equiv] DIVERGENCE public vs signé: %s", diffs)
            self._ftcache_bump("positions_equiv_divergence")
        else:
            logger.debug("[positions-equiv] OK (%d positions)", len(signed))
    except Exception as e:
        logger.warning("[positions-equiv] check impossible: %s", e)
```

## 7. Composant D — `fetch_positions()` override (échelle de fallback ordonnée)

Précédence unique et documentée :
```
1. cache mixin frais (age <= soft_stale)          -> retour instantané
2. sinon fetch direct de secours (public/signé)   -> WARNING, met à jour le cache
3. sinon (échec) cache mixin périmé                -> WARNING + expose l'age (le circuit breaker §8 tranche)
4. jamais de retour "vide silencieux"
```
```python
def fetch_positions(self, pair=None, params=None):
    if self._pos_refresher_active:
        with self._pos_lock:
            cached, ts = self._ftcache_last_positions, self._ftcache_last_positions_ts
        age = time.monotonic() - (ts or 0)
        if cached is not None and age <= self._pos_soft_stale:
            self._ftcache_bump("positions_served_cache")
            return self._filter(cached, pair)
        logger.warning("[positions] cache vieux (age=%.0fs) — fetch direct de secours", age)
        self._ftcache_bump("positions_fallback_direct")
        try:
            fresh = self._fetch_positions_direct()   # public si HL, sinon signé
            self._ftcache_save_positions(fresh, fetched_at=time.monotonic())
            return self._filter(fresh, pair)
        except Exception as e:
            logger.error("[positions] fetch direct de secours échoué (age cache=%.0fs): %r", age, e)
            if cached is not None:
                return self._filter(cached, pair)   # dernier connu — le circuit breaker §8 protège
            raise
    return self._fetch_positions_direct(pair, params)
```

## 8. Composant F — Circuit breaker positions-périmées (LE livrable sécurité)

Un helper central, appelé aux points de décision risqués :
```python
def positions_are_trustworthy(self) -> tuple[bool, float]:
    age = time.monotonic() - (self._ftcache_last_positions_ts or 0)
    return (age <= self._pos_hard_stale), age
```
Points d'ancrage (dans `freqtradebot.py`) :
- **`create_trades` / entrée** : si `not trustworthy` → **bloquer les nouvelles entrées** (log WARNING throttlé, compteur `hard_stale_block`), garder la gestion des trades existants.
- **Coordination / netting** (`_coordinate_initial_entry`, evaluate) : si `not trustworthy` → **refuser** (on ne décide pas d'un netting sur du périmé).
- **`_handle_external_close` / réconciliation** : si `not trustworthy` → **ne pas fabriquer** de fermeture externe ; attendre du frais (forcer `_pos_force_event`).
```python
ok, age = self.exchange.positions_are_trustworthy()
if not ok:
    self._throttled_warn("positions périmées (age=%.0fs) — entrées/netting suspendus", age)
    self.exchange.request_positions_refresh()   # set _pos_force_event
    return  # pas d'action risquée
```

## 9. Composant E — Refresh événementiel (fraîcheur post-ordre)

Sur tout changement d'état d'ordre (fill), déclencher un refresh immédiat au lieu d'attendre le timer :
- Dans `update_trade_state()` (après un fill) et après `execute_trade_exit`/`_execute_trade_entry` :
  `self.exchange.request_positions_refresh()` → `self._pos_force_event.set()`.
- Le loop se réveille immédiatement (`_pos_force_event.wait`) et refetch.
> Régime : timer 10s pour le fond, événement pour la fraîcheur critique.

## 10. Composant G — Cycle de vie

- **Start** (`additional_exchange_init`, après `_api` prêt, live only) :
  1. `self._pos_source = self._resolve_positions_source()`
  2. **Refresh synchrone initial** (bloquant) → cache jamais vide au premier `process()`.
  3. Démarrer le thread `daemon=True`. Log INFO `positions refresher started (mode=%s, interval=%ds)`.
- **Reload** (`RELOAD_CONFIG`) : `stop()` propre + `start()` rattaché au **nouvel** `_api`.
- **Stop / shutdown** (`close()`) : `_pos_stop.set()`, `_pos_force_event.set()`, `thread.join(timeout=5)` (pas de hang).
- **Dry/replay** : `_pos_refresher_active = False` (I4).

## 11. Composant H — Télémétrie & kill-switch

- Exposer dans `ftcache_get_stats()` : `positions_cache_age_s`, `positions_source`, `refresh_ok/429/err`,
  `consecutive_fail`, `fallback_direct`, `hard_stale_block`, `equiv_divergence`, p50/p99 latence.
- Endpoint/relais existant (stats) → **alerte sur `positions_cache_age_s`** côté FreqUI/monitor.
- **Kill-switch runtime** : `positions_refresh_enabled` relisible au reload → off = fallback direct pur, sans restart.

## 12. Plan de logging (récapitulatif)
| Événement | Niveau |
|---|---|
| start/stop refresher, source choisie | INFO |
| refresh OK (count, durée) | DEBUG |
| refresh 429 / échec (age conservé) | WARNING |
| cache servi vieux (> warn_age) | WARNING |
| fallback direct | WARNING |
| fetch direct de secours échoué | ERROR |
| **thread mort / cache figé (watchdog)** | ERROR |
| **circuit breaker: entrées/netting suspendus** | WARNING (throttlé) |
| **divergence équivalence public/signé** | ERROR |
| écriture out-of-order ignorée | DEBUG |

## 13. Fichiers touchés
- `freqtrade/ohlcv_cache/mixin.py` — thread, refresh, monotonic save, fetch_positions override, watchdog, telemetry, source resolution, force_event.
- `freqtrade/ohlcv_cache/defaults.py` — clés config.
- `freqtrade/exchange/hyperliquid.py` — `_fetch_positions_hl_public` (ou confirmation que fetch_positions est public → suppression du helper).
- `freqtrade/freqtradebot.py` — circuit breaker (create_trades, coordination, external_close), refresh événementiel (update_trade_state / exécution d'ordres), watchdog dans process().
- `tests/` — cf §14.

## 14. Tests (obligatoires)
1. **Monotonic** : une écriture out-of-order est ignorée.
2. **Source** : HL live → `publicPostInfo` (mock, non signé) ; autre → `fetch_positions` signé.
3. **Fallback ladder** : cache frais→sert ; vieux→direct ; direct échoue→dernier connu.
4. **Circuit breaker** : `positions_are_trustworthy` False → entrées/netting bloqués (assert pas d'ordre).
5. **Watchdog** : thread tué → détecté et relancé ; cache figé → force_event set.
6. **Backoff** : N échecs → intervalle croît (plafonné) ; succès → reset.
7. **Événementiel** : fill → force_event set → refresh immédiat.
8. **Concurrence** : hammering `fetch_positions` (main) + thread écrivant → pas de race (données cohérentes).
9. **Régression incident** : backoff/blocage OHLCV du daemon simulé → `fetch_positions()` reste instantané et frais.
10. **Shutdown** : join propre (pas de hang) ; reload → thread rattaché au nouvel `_api`.
11. **Dry/replay** : refresher inactif.

## 15. Déploiement & rollback
- Gated `positions_refresh_enabled` (rollback = flag off + reload).
- Editable install actif → **restart staggered** (10 min) pour charger.
- Surveiller `[positions-refresh]` + `positions_cache_age_s`.

## 16. Ordre d'implémentation (phasage sûr)
1. **A (monotonic save)** + config — transverse, bas risque, améliore déjà l'existant.
2. **B/C/D (thread + fetch + fallback ladder)** derrière le flag **off** — n'affecte rien tant que désactivé.
3. **G (lifecycle) + H (télémétrie)** — puis activer le flag sur **1 bot pilote**, observer 24-48h.
4. **F (circuit breaker) + E (événementiel)** — le vrai durcissement sécurité, après que le refresher est prouvé stable.
5. Rollout flotte staggered.

> Ne pas activer F (circuit breaker) avant que le refresher soit stable en pilote : sinon un refresher
> instable **bloquerait les entrées** à tort. Ordre = refresher fiable d'abord, puis on lui fait confiance.
</content>
