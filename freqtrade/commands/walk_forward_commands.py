from __future__ import annotations

import logging
from typing import Any

from freqtrade.enums import RunMode
from freqtrade.exceptions import OperationalException


logger = logging.getLogger(__name__)


def start_walk_forward(args: dict[str, Any]) -> None:
    """Start Walk-forward analysis."""
    try:
        from filelock import FileLock, Timeout

        from freqtrade.optimize.walk_forward import WalkForward
    except ImportError as e:
        raise OperationalException(
            f"{e}. Please ensure that the hyperopt dependencies are installed."
        ) from e

    from freqtrade.commands.optimize_commands import setup_optimize_configuration

    config = setup_optimize_configuration(args, RunMode.WALKFORWARD)

    logger.info("Starting freqtrade in Walk-Forward Analysis mode")

    lock = FileLock(WalkForward.get_lock_filename(config))

    try:
        with lock.acquire(timeout=1):
            logging.getLogger("hyperopt.tpe").setLevel(logging.WARNING)
            logging.getLogger("filelock").setLevel(logging.WARNING)

            wfa = WalkForward(config)
            wfa.start()

    except Timeout:
        logger.info(
            "Another running instance of freqtrade Walk-Forward detected."
        )
        logger.info(
            "Simultaneous execution is not supported. "
            "Please run Walk-Forward analysis sequentially."
        )
