"""
Hyperliquid subclass that routes OHLCV fetches through the shared cache
daemon. See freqtrade/ohlcv_cache/ for the full architecture.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from freqtrade.exchange.hyperliquid import Hyperliquid
from freqtrade.ohlcv_cache.client import OhlcvCacheClient
from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin


if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


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
        self._ftcache_acquire_sync(priority=OhlcvCacheClient.HIGH)
        return super().additional_exchange_init()
