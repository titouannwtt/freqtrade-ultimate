"""
Cached* exchange subclasses auto-generated from Cached{Exchange} = type(...).

Each class inherits from:
    CachedExchangeMixin  (overrides _async_get_candle_history → daemon)
    <NativeExchange>     (the freqtrade-native subclass)
    Exchange             (the base, via the native subclass' MRO)

The MRO ensures that super()._async_get_candle_history in the mixin resolves
to the native subclass' method (usually inherited from Exchange), preserving
@retrier_async + original ccxt params.

ExchangeResolver prefers Cached{X} when config.shared_ohlcv_cache.enabled
is not explicitly False (default on).

Notes:
  - Binance.get_historic_ohlcv_fast uses binance.vision CDN and is NOT
    intercepted (it doesn't go through _async_get_candle_history). Big
    historical downloads keep their fast-path.
"""

from __future__ import annotations

import logging

from freqtrade.exchange.binance import Binance, Binanceus, Binanceusdm
from freqtrade.exchange.bingx import Bingx
from freqtrade.exchange.bitget import Bitget
from freqtrade.exchange.bitvavo import Bitvavo
from freqtrade.exchange.bybit import Bybit
from freqtrade.exchange.coinex import Coinex
from freqtrade.exchange.cryptocom import Cryptocom
from freqtrade.exchange.gate import Gate
from freqtrade.exchange.htx import Htx
from freqtrade.exchange.kraken import Kraken
from freqtrade.exchange.krakenfutures import Krakenfutures
from freqtrade.exchange.kucoin import Kucoin
from freqtrade.exchange.okx import Myokx, Okx, Okxus
from freqtrade.ohlcv_cache.mixin import CachedExchangeMixin


logger = logging.getLogger(__name__)


def _make_cached(native_cls: type) -> type:
    """Build Cached{native} = type('Cached{native}', (Mixin, native), {...})."""
    name = f"Cached{native_cls.__name__}"
    new_cls = type(
        name,
        (CachedExchangeMixin, native_cls),
        {
            "__module__": __name__,
            "__doc__": (
                f"{native_cls.__name__} with shared OHLCV cache.\n"
                "Routes _async_get_candle_history through the ftcache daemon."
            ),
        },
    )
    # Tag with a simple post-init to emit the deprecation warning once.
    original_init = new_cls.__init__

    def __init__(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._ftcache_warn_deprecated_config()

    new_cls.__init__ = __init__
    return new_cls


# Generate Cached* classes. Order here determines nothing, it's purely
# for declaration clarity.
CachedBinance = _make_cached(Binance)
CachedBinanceus = _make_cached(Binanceus)
CachedBinanceusdm = _make_cached(Binanceusdm)
CachedBingx = _make_cached(Bingx)
CachedBitget = _make_cached(Bitget)
CachedBitvavo = _make_cached(Bitvavo)
CachedBybit = _make_cached(Bybit)
CachedCoinex = _make_cached(Coinex)
CachedCryptocom = _make_cached(Cryptocom)
CachedGate = _make_cached(Gate)
CachedHtx = _make_cached(Htx)
CachedKraken = _make_cached(Kraken)
CachedKrakenfutures = _make_cached(Krakenfutures)
CachedKucoin = _make_cached(Kucoin)
CachedMyokx = _make_cached(Myokx)
CachedOkx = _make_cached(Okx)
CachedOkxus = _make_cached(Okxus)
