"""
Unified logger setup for ftcache.

Both daemon-side and client-side log records are emitted with a
`[ftcache]` or `[ftcache-client]` prefix in the message, and under the
`ftcache` logger namespace. This makes them trivial to grep:

    grep '\\[ftcache\\]' ~/.freqtrade/ftcache/logs/daemon.log
    grep 'ftcache-client' /path/to/bot.log
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


_DAEMON_FMT = "%(asctime)s [ftcache] %(levelname)s %(name)s: %(message)s"
_CLIENT_FMT = "%(asctime)s [ftcache-client] %(levelname)s %(name)s: %(message)s"


class SafeRotatingFileHandler(RotatingFileHandler):
    """`RotatingFileHandler` qui SURVIT AU FORK et à une rotation faite par quelqu'un d'autre.

    ## L'incident qui l'a rendu nécessaire (30 août 2026)
    Le disque du VPS s'est rempli à 100 % : **un seul fichier occupait 197 Go**, et c'était
    `~/.freqtrade/ftcache/logs/daemon.log.5` — **déjà supprimé**, mais encore ouvert par le démon,
    qui continuait d'écrire dedans. Sous Linux l'espace n'est rendu qu'à la fermeture du
    descripteur : le fichier n'existait plus, et pesait quand même 197 Go. Le démon tournait depuis
    584 jours. Toute la machine était bloquée, y compris d'autres projets sans rapport.

    ## La cause, et pourquoi la rotation existante ne protégeait pas
    `RotatingFileHandler` **n'est pas sûr au fork**. `setup_daemon_logger()` est pourtant
    idempotent (il retire les anciens handlers), mais si le processus **fork** après l'ouverture,
    parent et enfant héritent du **même descripteur**. Quand l'un fait tourner le journal
    (`daemon.log.5` → renommé puis supprimé), l'autre **continue d'écrire dans l'inode orphelin**,
    que plus aucun nom ne désigne et que plus aucune rotation ne peut borner. La taille croît alors
    **sans limite**, indéfiniment. Le diagnostic l'a confirmé : **deux descripteurs**, même
    processus, sur le même fichier supprimé.

    ## Ce que cette classe fait
    Avant chaque écriture, elle compare l'inode du flux ouvert à celui du chemin sur le disque, et
    **ré-ouvre** dès que l'un des trois signaux apparaît :
      1. le fichier ouvert n'a **plus aucun nom** (`st_nlink == 0`) — il a été supprimé sous nos
         pieds, c'est exactement le cas des 197 Go ;
      2. l'inode ouvert **diffère** de celui du chemin — quelqu'un d'autre a fait tourner le journal ;
      3. le flux ouvert dépasse **très largement** `maxBytes` — filet de sécurité si les deux
         premiers signaux échouaient.
    Le contrôle est **strictement non fatal** : un journal ne doit jamais faire tomber le démon
    qu'il observe.
    """

    _FACTEUR_GARDE = 4          # on ré-ouvre au-delà de 4 × maxBytes, quoi qu'il arrive

    def _reouvrir_si_orphelin(self) -> None:
        try:
            if self.stream is None:
                return
            st = os.fstat(self.stream.fileno())
            besoin = st.st_nlink == 0                      # (1) plus aucun nom
            if not besoin:
                try:
                    sd = os.stat(self.baseFilename)
                    besoin = (sd.st_ino != st.st_ino)      # (2) rotation faite ailleurs
                except FileNotFoundError:
                    besoin = True
            if not besoin and self.maxBytes:
                besoin = st.st_size > self._FACTEUR_GARDE * self.maxBytes   # (3) filet
            if besoin:
                self.acquire()
                try:
                    try:
                        self.stream.close()
                    except Exception:
                        pass
                    self.stream = self._open()
                finally:
                    self.release()
        except Exception:
            pass          # un journal ne fait jamais tomber le service qu'il observe

    def emit(self, record):                       # noqa: D102
        self._reouvrir_si_orphelin()
        super().emit(record)


def setup_daemon_logger(log_path: str | Path | None, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("ftcache")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter(_DAEMON_FMT)

    if log_path:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # ⛔️ `SafeRotatingFileHandler` et NON `RotatingFileHandler` : voir sa docstring.
        # Un journal supprimé mais tenu ouvert a atteint 197 Go et rempli le disque du VPS.
        fh = SafeRotatingFileHandler(
            p,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
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
