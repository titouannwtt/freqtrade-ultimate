"""
VirtualClock — the single authoritative time source for a replay.

Freqtrade reads "now" from many call sites: ``dt_now()`` is the documented
chokepoint, but plenty of code does ``datetime.now(UTC)`` directly (e.g.
``freqtradebot.process()``). Patching only the helper would leave those blind.
``freezegun.freeze_time`` patches every call site process-wide — including
modules that did ``from datetime import datetime`` — which is exactly what we
need to make the whole bot believe it lives at the replay timestamp.

freezegun is an optional dependency: it is imported lazily so the rest of the
``freqtrade.replay`` package (safety gate, data store) can be imported and unit
tested without it. ``start()`` raises a clear, actionable error if it is
missing.
"""

from __future__ import annotations

from datetime import UTC, datetime


_FREEZEGUN_HINT = (
    "freezegun is required for replay. Install it with:\n"
    "    pip install freezegun\n"
    "(it is declared as a replay extra in pyproject.toml)."
)


class VirtualClock:
    """Frozen, manually-advanced wall clock for the duration of a replay."""

    def __init__(self) -> None:
        self._freeze_ctx = None
        self._frozen = None
        self._current: datetime | None = None

    def start(self, start_dt: datetime) -> None:
        try:
            from freezegun import freeze_time
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ImportError(_FREEZEGUN_HINT) from exc

        start_dt = _as_utc(start_dt)
        self._current = start_dt
        self._freeze_ctx = freeze_time(start_dt)
        self._frozen = self._freeze_ctx.start()

    def advance_to(self, dt: datetime) -> None:
        if self._frozen is None:
            raise RuntimeError("VirtualClock.advance_to() called before start().")
        dt = _as_utc(dt)
        self._current = dt
        self._frozen.move_to(dt)

    def now(self) -> datetime:
        if self._current is None:
            raise RuntimeError("VirtualClock.now() called before start().")
        return self._current

    def stop(self) -> None:
        if self._freeze_ctx is not None:
            self._freeze_ctx.stop()
        self._freeze_ctx = None
        self._frozen = None
        self._current = None

    def __enter__(self) -> VirtualClock:
        return self

    def __exit__(self, *_) -> None:
        self.stop()


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
