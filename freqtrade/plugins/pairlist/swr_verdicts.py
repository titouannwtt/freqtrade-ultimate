"""
Stale-while-revalidate (SWR) verdict store for per-pair pairlist filters.

Used by TrendRegularityFilter and ExtremeMoveFilter when `stale_while_revalidate` is
enabled. Without SWR, those filters keep each per-pair verdict in a TTL cache, so all the
verdicts computed together expire together and the whole list is recomputed in one burst
inside the bot loop. With SWR a verdict is never dropped when it expires: it keeps being
served until it has been recomputed, and at most `max_per_cycle` pairs are recomputed per
call, oldest verdict first.

Rules (the filter rules themselves are not touched, only WHEN a verdict is computed):

- Cold start: everything missing is computed at once, unbounded and without failure back-off,
  as without SWR, until every pair has a verdict (or COLD_MAX_CALLS calls have passed).
- Afterwards, a pair with no verdict yet ("missing", e.g. a new pair, or one whose verdict is
  older than `MAX_AGE_FACTOR` x refresh_period) is NOT ALLOWED (the filter removes it) until
  its verdict has been computed. Missing pairs are computed before expired ones.
- A verdict becomes due after `refresh_period` reduced by a deterministic per-pair jitter of
  up to `jitter_pct`, so verdicts computed together do not expire together.
- A failed computation (no candles) never overwrites a verdict, and the pair is not retried
  for `RETRY_AFTER_FAILURE_S`, so a broken pair cannot hog the per-cycle budget.
- `quiet_after_close_s` > 0: nothing is computed in the first seconds after each 15m candle
  close (cold start excepted), when the shared candle cache is busy with the trading pairs.
"""

import logging
import time
import zlib


logger = logging.getLogger(__name__)

# A verdict older than this many refresh periods is no longer served: the pair is "missing".
MAX_AGE_FACTOR = 2.0
# Delay before a pair whose recompute failed (no candles) is tried again.
RETRY_AFTER_FAILURE_S = 300
# A due verdict older than this many refresh periods is urgent: it is about to stop being
# served, so it is recomputed even beyond `max_per_cycle` (up to URGENT_BUDGET_FACTOR x).
URGENT_AGE_FACTOR = 1.5
URGENT_BUDGET_FACTOR = 4
# The cold start (unbounded, no failure back-off) ends when every pair has a verdict, or after
# this many calls so that a pair that never gets candles cannot keep the store cold forever.
COLD_MAX_CALLS = 5
# Candle close grid used by `quiet_after_close_s`.
QUIET_GRID_S = 15 * 60


def swr_parameters() -> dict:
    """Pairlist parameter descriptions of the SWR keys (shared by both filters)."""
    return {
        "stale_while_revalidate": {
            "type": "boolean",
            "default": False,
            "description": "Stale-while-revalidate",
            "help": (
                "Keep serving an expired verdict until it is recomputed, and recompute at "
                "most max_recompute_per_cycle pairs per refresh instead of all at once. "
                "A pair with no verdict yet is excluded until its verdict is computed."
            ),
        },
        "max_recompute_per_cycle": {
            "type": "number",
            "default": 2,
            "description": "Max recomputed pairs per refresh (SWR)",
            "help": "Upper bound of pairs recomputed per refresh when SWR is enabled.",
        },
        "quiet_after_close_s": {
            "type": "number",
            "default": 0,
            "description": "Quiet seconds after a 15m close (SWR)",
            "help": (
                "When SWR is enabled, do not recompute in the first N seconds after each "
                "15m candle close. 0 disables."
            ),
        },
        "swr_jitter_pct": {
            "type": "number",
            "default": 0.2,
            "description": "Expiry jitter (SWR)",
            "help": (
                "When SWR is enabled, each pair's verdict is due after refresh_period "
                "reduced by up to this fraction (deterministic per pair, 0 to 0.9)."
            ),
        },
    }


class StaleVerdictStore:
    """Per-pair boolean verdicts with their computation time, never expired by a TTL."""

    def __init__(
        self,
        refresh_period: float,
        max_per_cycle: int = 2,
        quiet_after_close_s: float = 0,
        jitter_pct: float = 0.2,
        timer=None,
    ) -> None:
        self.refresh_period = refresh_period
        self.max_per_cycle = max_per_cycle
        self.quiet_after_close_s = quiet_after_close_s
        self.jitter_pct = jitter_pct
        # Resolved at call time when None: a timer captured at import would freeze the clock
        # of whoever imported the module first (e.g. the replay runner's frozen time).
        self._timer = timer
        self.max_age = MAX_AGE_FACTOR * refresh_period
        self._verdicts: dict[str, tuple[bool, float]] = {}
        self._retry_at: dict[str, float] = {}
        self.warm = False
        self._cold_calls = 0
        self._urgent_warned_at = 0.0

    @staticmethod
    def validate(max_per_cycle, quiet_after_close_s, jitter_pct) -> str | None:
        """Return an error message when a SWR key is out of range."""
        if not isinstance(max_per_cycle, int) or isinstance(max_per_cycle, bool):
            return "max_recompute_per_cycle must be an integer"
        if max_per_cycle < 1:
            return "max_recompute_per_cycle must be >= 1"
        if not 0 <= quiet_after_close_s < QUIET_GRID_S:
            return f"quiet_after_close_s must be between 0 and {QUIET_GRID_S - 1}"
        if not 0 <= jitter_pct <= 0.9:
            return "swr_jitter_pct must be between 0 and 0.9"
        return None

    def now(self) -> float:
        return self._timer() if self._timer is not None else time.time()

    def jitter(self, pair: str) -> float:
        """Deterministic (process independent) fraction in [0, jitter_pct) for a pair."""
        return (zlib.crc32(pair.encode()) % 10000) / 10000 * self.jitter_pct

    def due_at(self, pair: str, computed_at: float) -> float:
        return computed_at + self.refresh_period * (1 - self.jitter(pair))

    def get(self, pair: str) -> bool | None:
        """Last verdict (possibly expired but still servable), None when missing."""
        entry = self._verdicts.get(pair)
        return None if entry is None else entry[0]

    def computed_at(self, pair: str) -> float | None:
        entry = self._verdicts.get(pair)
        return None if entry is None else entry[1]

    def is_blocked(self, pair: str, now: float) -> bool:
        return self._retry_at.get(pair, 0.0) > now

    def put(self, pair: str, exclude: bool, computed_at: float) -> None:
        self._verdicts[pair] = (exclude, computed_at)
        self._retry_at.pop(pair, None)

    def adopt(self, pair: str, exclude: bool, computed_at: float) -> bool:
        """Take a verdict computed elsewhere, only when newer than ours."""
        current = self.computed_at(pair)
        if current is not None and current >= computed_at:
            return False
        self.put(pair, exclude, computed_at)
        return True

    def fail(self, pair: str, now: float) -> None:
        """A recompute found no data: keep the old verdict, back off before retrying.

        No back-off during the cold start: the pair is retried on the next call, as without SWR.
        """
        if self.warm:
            self._retry_at[pair] = now + RETRY_AFTER_FAILURE_S

    def classify(self, pairlist: list[str], now: float) -> tuple[list[str], list[str]]:
        """
        Split the pairs needing a computation into (missing, due).
        `missing` keeps the pairlist order, `due` is sorted oldest verdict first.
        Verdicts too old to be served are dropped here.
        """
        missing: list[str] = []
        due: list[tuple[float, str]] = []
        for pair in pairlist:
            entry = self._verdicts.get(pair)
            if entry is not None and now - entry[1] > self.max_age:
                del self._verdicts[pair]
                entry = None
            if self.is_blocked(pair, now):
                continue
            if entry is None:
                missing.append(pair)
            elif now >= self.due_at(pair, entry[1]):
                due.append((entry[1], pair))
        due.sort()
        return missing, [pair for _, pair in due]

    def mark_warm(self, pairlist: list[str]) -> None:
        """End the cold start once every pair has a verdict (or after COLD_MAX_CALLS calls)."""
        if self.warm or not pairlist:
            return
        self._cold_calls += 1
        if self._cold_calls >= COLD_MAX_CALLS or all(pair in self._verdicts for pair in pairlist):
            self.warm = True

    def in_quiet_window(self, now: float) -> bool:
        return self.quiet_after_close_s > 0 and (now % QUIET_GRID_S) < self.quiet_after_close_s

    def select(self, missing: list[str], due: list[str], now: float) -> list[str]:
        """Pairs to compute during this call: unbounded on the first call, else bounded."""
        if not self.warm:
            return missing + due
        if self.in_quiet_window(now):
            return []
        budget = self.max_per_cycle
        urgent_age = URGENT_AGE_FACTOR * self.refresh_period
        urgent = sum(1 for pair in due if now - self._verdicts[pair][1] > urgent_age)
        if urgent:
            budget = min(
                max(budget, len(missing) + urgent), URGENT_BUDGET_FACTOR * self.max_per_cycle
            )
            if now - self._urgent_warned_at >= 3600:
                self._urgent_warned_at = now
                logger.warning(
                    f"SWR verdicts falling behind: {urgent} verdict(s) older than "
                    f"{URGENT_AGE_FACTOR} refresh periods, recomputing {budget} pairs this call."
                )
        return (missing + due)[:budget]
