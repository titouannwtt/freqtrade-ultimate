"""
Hyperliquid subclass that routes OHLCV fetches through the shared cache
daemon. See freqtrade/ohlcv_cache/ for the full architecture.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from freqtrade.exceptions import DDosProtection, TemporaryError
from freqtrade.exchange.hyperliquid import Hyperliquid
from freqtrade.ohlcv_cache.client import OhlcvCacheClient
from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin


if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)

_INIT_MAX_RETRIES = 10
_INIT_MAX_WAIT_S = 120


class CachedHyperliquid(CachedExchangeMixin, Hyperliquid):
    """Hyperliquid with shared OHLCV cache.

    MRO: CachedHyperliquid -> CachedExchangeMixin -> Hyperliquid -> Exchange
    so that `super()._async_get_candle_history` in the mixin resolves to
    Hyperliquid (no override there) -> Exchange's implementation.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ftcache_warn_deprecated_config()

    def fetch_liquidation_fills(self, pair: str, since: datetime) -> list[dict]:
        if not self._config.get("dry_run"):
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.HIGH)
        return super().fetch_liquidation_fills(pair, since)

    def additional_exchange_init(self) -> None:
        """Resilient init: retries beyond @retrier's 4 attempts.

        During 429 storms the inner retrier exhausts its attempts and raises.
        Instead of crashing the bot, we wait for the rate limit window to
        clear and try again (up to ~10 min total).
        """
        for attempt in range(_INIT_MAX_RETRIES):
            self._ftcache_acquire_sync(priority=OhlcvCacheClient.HIGH)
            try:
                return super().additional_exchange_init()
            except (DDosProtection, TemporaryError) as e:
                if attempt >= _INIT_MAX_RETRIES - 1:
                    raise
                wait = min(30 * (attempt + 1), _INIT_MAX_WAIT_S)
                logger.warning(
                    "additional_exchange_init failed (attempt %d/%d): %s"
                    " — waiting %ds for rate limit to clear",
                    attempt + 1, _INIT_MAX_RETRIES, e, wait,
                )
                time.sleep(wait)
