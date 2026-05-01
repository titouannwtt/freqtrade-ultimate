"""
Unified logger setup for ftcache.

Both daemon-side and client-side log records are emitted with a
`[ftcache]` or `[ftcache-client]` prefix in the message, and under the
`ftcache` logger namespace. This makes them trivial to grep:

    grep '\\[ftcache\\]' ~/.freqtrade/ftcache/logs/daemon.log
    grep 'ftcache-client' /path/to/bot.log
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


_DAEMON_FMT = (
    "%(asctime)s [ftcache] %(levelname)s %(name)s: %(message)s"
)
_CLIENT_FMT = (
    "%(asctime)s [ftcache-client] %(levelname)s %(name)s: %(message)s"
)


def setup_daemon_logger(log_path: str | Path | None, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("ftcache")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter(_DAEMON_FMT)

    if log_path:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            p, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    else:
        sh = logging.StreamHandler(stream=sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    logger.propagate = False
    return logger


def get_client_logger() -> logging.Logger:
    """Client-side logger. Uses the bot's logging configuration (propagates)
    but prefixes records with [ftcache-client] via a custom Filter."""
    logger = logging.getLogger("ftcache.client")
    if not getattr(logger, "_ftcache_configured", False):
        class _PrefixFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                if not record.msg.startswith("[ftcache-client]"):
                    record.msg = "[ftcache-client] " + str(record.msg)
                return True

        logger.addFilter(_PrefixFilter())
        logger._ftcache_configured = True  # type: ignore[attr-defined]
    return logger
