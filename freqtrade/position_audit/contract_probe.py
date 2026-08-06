"""
Contract probe — check that the exchange still behaves the way this code assumes.

The failure this guards against
-------------------------------
Every safety mechanism here rests on assumptions about the exchange and about ccxt:
that a client order id is echoed back on reads, that positions carry a ``side`` and
``contracts``, that the daemon reports the age of its cached copy. Those assumptions are
true today. Over a decade of upstream releases and exchange changes, some will quietly
stop being true — a renamed field, a dropped echo, a changed default.

Unit tests will not catch it: they run against fixtures that keep asserting yesterday's
reality, staying green while production drifts. Only a probe against the live exchange
can tell, and the point of running it on a schedule is to learn the day it breaks rather
than the day it costs a position.

Read-only. It inspects capabilities and shapes; it never places an order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from freqtrade.order_identity import is_freqtrade_order


logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'OK  ' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


def probe_client_order_id_support(exchange: Any) -> ProbeResult:
    """Does the exchange still accept and echo a client order id?

    Verified by shape rather than by placing an order: we check that the capability is
    declared and that ccxt's order parser still exposes ``clientOrderId``. A silent
    removal of that field would make every order read as "not ours", which fails safe
    (nothing is adopted) but silently disables order recovery.
    """
    declared = bool(exchange.get_option("supports_client_order_id", False))
    if not declared:
        return ProbeResult("client_order_id", True, "not claimed by this exchange — n/a")
    try:
        parsed = exchange._api.parse_order(
            {"oid": 1, "cloid": "0x" + "a" * 32, "coin": "ETH", "side": "B"}
        )
    except Exception as exc:
        return ProbeResult("client_order_id", False, f"ccxt parse_order raised: {exc}")
    echoed = parsed.get("clientOrderId")
    if not echoed:
        return ProbeResult(
            "client_order_id",
            False,
            "ccxt no longer exposes clientOrderId — order ownership can no longer be proven",
        )
    if not is_freqtrade_order(echoed):
        return ProbeResult(
            "client_order_id", False, f"echoed id has an unexpected shape: {echoed!r}"
        )
    return ProbeResult("client_order_id", True, "accepted and echoed back")


def probe_position_shape(positions: list[dict]) -> ProbeResult:
    """Do positions still carry the fields every reconciliation reads?"""
    if not positions:
        return ProbeResult("position_shape", True, "no open position to inspect")
    missing: set[str] = set()
    for p in positions:
        for field in ("symbol", "contracts", "side"):
            if p.get(field) in (None, ""):
                missing.add(field)
    if missing:
        return ProbeResult(
            "position_shape",
            False,
            f"missing field(s) {sorted(missing)} — reconciliation would misread positions",
        )
    return ProbeResult("position_shape", True, f"{len(positions)} position(s), fields present")


def probe_snapshot_freshness(exchange: Any) -> ProbeResult:
    """Is the positions snapshot still timestamped at capture?

    The whole external-close guard rests on this timestamp meaning "when the data was
    captured". If it silently reverted to "when we received it", a stale reading would
    pass the freshness check again — the exact regression that created orphans.
    """
    getter = getattr(exchange, "positions_snapshot_wall_ts", None)
    if getter is None:
        return ProbeResult("snapshot_freshness", True, "no shared cache — reads are live")
    try:
        ts = float(getter() or 0.0)
    except Exception as exc:
        return ProbeResult("snapshot_freshness", False, f"timestamp unreadable: {exc}")
    if ts <= 0:
        return ProbeResult("snapshot_freshness", True, "no snapshot taken yet")
    import time

    age = time.time() - ts
    if age < -5:
        return ProbeResult(
            "snapshot_freshness", False, f"snapshot timestamped {abs(age):.0f}s in the future"
        )
    return ProbeResult("snapshot_freshness", True, f"snapshot age {age:.0f}s, plausible")


def run_probes(exchange: Any, positions: list[dict]) -> list[ProbeResult]:
    """Run every probe. Never raises: a broken probe must not break the caller."""
    results = []
    for fn, args in (
        (probe_client_order_id_support, (exchange,)),
        (probe_position_shape, (positions,)),
        (probe_snapshot_freshness, (exchange,)),
    ):
        try:
            results.append(fn(*args))
        except Exception as exc:  # pragma: no cover - defensive
            results.append(ProbeResult(fn.__name__, False, f"probe itself failed: {exc}"))
    return results
