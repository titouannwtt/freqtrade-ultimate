"""
Main Freqtrade worker class.
"""

import logging
import time
import traceback
from collections.abc import Callable
from os import getpid
from typing import Any

import sdnotify

from freqtrade import __version__
from freqtrade.configuration import Configuration
from freqtrade.constants import PROCESS_THROTTLE_SECS, RETRY_TIMEOUT, Config
from freqtrade.enums import RPCMessageType, State
from freqtrade.exceptions import OperationalException, TemporaryError
from freqtrade.exchange import timeframe_to_next_date
from freqtrade.freqtradebot import FreqtradeBot


logger = logging.getLogger(__name__)


class Worker:
    """
    Freqtradebot worker class
    """

    def __init__(self, args: dict[str, Any], config: Config | None = None) -> None:
        """
        Init all variables and objects the bot needs to work
        """
        logger.info(f"Starting worker {__version__}")

        self._args = args
        self._config = config
        self._init(False)

        self._heartbeat_msg: float = 0

        # Tell systemd that we completed initialization phase
        self._notify("READY=1")

    def _init(self, reconfig: bool) -> None:
        """
        Also called from the _reconfigure() method (with reconfig=True).
        """
        if reconfig or self._config is None:
            # Load configuration
            self._config = Configuration(self._args, None).get_config()

        # Init the instance of the bot
        self.freqtrade = FreqtradeBot(self._config)

        internals_config = self._config.get("internals", {})
        self._throttle_secs = internals_config.get("process_throttle_secs", PROCESS_THROTTLE_SECS)
        self._heartbeat_interval = internals_config.get("heartbeat_interval", 60)

        # Per-bot candle boundary jitter to prevent thundering herd.
        # Deterministic hash of bot_name so the jitter is stable across restarts
        # but different for each bot.  Spread over 0-25s.
        import hashlib
        bot_name = self._config.get("bot_name", "")
        hash_val = int(hashlib.md5(bot_name.encode()).hexdigest()[:8], 16)
        self._candle_jitter_s = (hash_val % 250) / 10.0  # 0.0 to 25.0 seconds
        logger.info(
            "candle boundary jitter for '%s': %.1fs", bot_name, self._candle_jitter_s,
        )

        self._sd_notify = (
            sdnotify.SystemdNotifier()
            if self._config.get("internals", {}).get("sd_notify", False)
            else None
        )

    def _notify(self, message: str) -> None:
        """
        Removes the need to verify in all occurrences if sd_notify is enabled
        :param message: Message to send to systemd if it's enabled.
        """
        if self._sd_notify:
            logger.debug(f"sd_notify: {message}")
            self._sd_notify.notify(message)

    def run(self) -> None:
        state = None
        while True:
            state = self._worker(old_state=state)
            if state == State.RELOAD_CONFIG:
                self._reconfigure()

    def _worker(self, old_state: State | None) -> State:
        """
        The main routine that runs each throttling iteration and handles the states.
        :param old_state: the previous service state from the previous call
        :return: current service state
        """
        state = self.freqtrade.state

        # Log state transition
        if state != old_state:
            if old_state != State.RELOAD_CONFIG:
                self.freqtrade.notify_status(f"{state.name.lower()}")

            logger.info(
                f"Changing state{f' from {old_state.name}' if old_state else ''} to: {state.name}"
            )
            if state in (State.RUNNING, State.PAUSED) and old_state not in (
                State.RUNNING,
                State.PAUSED,
            ):
                self._apply_admission_hold_off()
                self._startup_with_patience()

            if state == State.STOPPED:
                self.freqtrade.check_for_open_trades()

            self._notify_fleet_state(state)

            # Reset heartbeat timestamp to log the heartbeat message at
            # first throttling iteration when the state changes
            self._heartbeat_msg = 0

        if state == State.STOPPED:
            # Ping systemd watchdog before sleeping in the stopped state
            self._notify("WATCHDOG=1\nSTATUS=State: STOPPED.")

            self._throttle(func=self._process_stopped, throttle_secs=self._throttle_secs)

        elif state in (State.RUNNING, State.PAUSED):
            state_str = "RUNNING" if state == State.RUNNING else "PAUSED"
            # Ping systemd watchdog before throttling
            self._notify(f"WATCHDOG=1\nSTATUS=State: {state_str}.")

            # Use an offset of 1s + per-bot jitter to stagger candle boundary wakeups.
            # Without jitter, all 14 bots wake at the exact same second and flood
            # the daemon with 560 OHLCV requests simultaneously.
            jitter = getattr(self, '_candle_jitter_s', 0.0)
            self._throttle(
                func=self._process_running,
                throttle_secs=self._throttle_secs,
                timeframe=self._config["timeframe"] if self._config else None,
                timeframe_offset=1 + jitter,
            )

        if self._heartbeat_interval:
            now = time.time()
            if (now - self._heartbeat_msg) > self._heartbeat_interval:
                version = __version__
                strategy_version = self.freqtrade.strategy.version()
                if strategy_version is not None:
                    version += ", strategy_version: " + strategy_version
                logger.info(
                    f"Bot heartbeat. PID={getpid()}, version='{version}', state='{state.name}'"
                )
                self._heartbeat_msg = now

        return state

    def _throttle(
        self,
        func: Callable[..., Any],
        throttle_secs: float,
        timeframe: str | None = None,
        timeframe_offset: float = 1.0,
        *args,
        **kwargs,
    ) -> Any:
        """
        Throttles the given callable that it
        takes at least `min_secs` to finish execution.
        :param func: Any callable
        :param throttle_secs: throttling iteration execution time limit in seconds
        :param timeframe: ensure iteration is executed at the beginning of the next candle.
        :param timeframe_offset: offset in seconds to apply to the next candle time.
        :return: Any (result of execution of func)
        """
        last_throttle_start_time = time.time()
        logger.debug("========================================")
        result = func(*args, **kwargs)
        time_passed = time.time() - last_throttle_start_time
        sleep_duration = throttle_secs - time_passed
        if timeframe:
            next_tf = timeframe_to_next_date(timeframe)
            # Maximum throttling should be until new candle arrives
            # Offset is added to ensure a new candle has been issued.
            next_tft = next_tf.timestamp() - time.time()
            next_tf_with_offset = next_tft + timeframe_offset
            if next_tft < sleep_duration and sleep_duration < next_tf_with_offset:
                # Avoid hitting a new loop between the new candle and the candle with offset
                sleep_duration = next_tf_with_offset
            sleep_duration = min(sleep_duration, next_tf_with_offset)
        sleep_duration = max(sleep_duration, 0.0)
        # next_iter = datetime.now(timezone.utc) + timedelta(seconds=sleep_duration)

        logger.debug(
            f"Throttling with '{func.__name__}()': sleep for {sleep_duration:.2f} s, "
            f"last iteration took {time_passed:.2f} s."
            #  f"next: {next_iter}"
        )
        self._sleep(sleep_duration)
        return result

    @staticmethod
    def _sleep(sleep_duration: float) -> None:
        """Local sleep method - to improve testability"""
        time.sleep(sleep_duration)

    def _apply_admission_hold_off(self) -> None:
        """Wait if the daemon requested a hold-off at registration time.

        This prevents thundering-herd init storms when many bots start at once
        or when the daemon is already under backoff pressure.
        """
        client = getattr(self.freqtrade.exchange, '_ftcache_client', None)
        if not client:
            return
        hold_off = getattr(client, 'hold_off_s', 0.0)
        if hold_off <= 0:
            return
        reason = getattr(client, 'hold_off_reason', '')
        logger.info(
            "admission hold-off: waiting %.0fs before startup (%s)",
            hold_off, reason or "fleet stagger",
        )
        import time as _time
        _time.sleep(hold_off)
        logger.info("admission hold-off complete, proceeding with startup")

    def _startup_with_patience(self, max_retries: int = 6, base_wait: float = 10.0) -> None:
        """Run startup() with patience: retry on rate-limit errors instead of crashing.

        During init, the daemon may be in backoff and API calls can fail.
        Instead of crashing the bot, we retry with exponential backoff
        (10s, 20s, 40s, 80s, 120s, 120s) — up to ~6 minutes total.
        """
        from freqtrade.exceptions import DDosProtection
        for attempt in range(max_retries):
            try:
                self.freqtrade.startup()
                return
            except (TemporaryError, DDosProtection) as e:
                wait = min(base_wait * (2 ** attempt), 120.0)
                logger.warning(
                    "startup() failed (attempt %d/%d): %s — retrying in %.0fs",
                    attempt + 1, max_retries, e, wait,
                )
                time.sleep(wait)
        logger.error("startup() failed after %d attempts — stopping bot", max_retries)
        self.freqtrade.state = State.STOPPED

    def _notify_fleet_state(self, state: State) -> None:
        state_map = {
            State.RUNNING: "running",
            State.PAUSED: "paused",
            State.STOPPED: "stopped",
        }
        state_str = state_map.get(state)
        if not state_str:
            return
        exchange = self.freqtrade.exchange
        run_on_loop = getattr(exchange, '_ftcache_run_on_loop', None)
        client = getattr(exchange, '_ftcache_client', None)
        if not run_on_loop or not client:
            return
        try:
            pairs_count = len(
                getattr(self.freqtrade, 'active_pair_whitelist', None) or []
            )
            run_on_loop(client.update_state(state_str, pairs_count=pairs_count))
        except Exception:  # noqa: S110
            pass

    def _process_stopped(self) -> None:
        self.freqtrade.process_stopped()

    def _process_running(self) -> None:
        try:
            self.freqtrade.process()
        except TemporaryError as error:
            logger.warning(f"Error: {error}, retrying in {RETRY_TIMEOUT} seconds...")
            time.sleep(RETRY_TIMEOUT)
        except OperationalException:
            tb = traceback.format_exc()
            hint = "Issue `/start` if you think it is safe to restart."

            self.freqtrade.notify_status(
                f"*OperationalException:*\n```\n{tb}```\n {hint}", msg_type=RPCMessageType.EXCEPTION
            )

            logger.exception("OperationalException. Stopping trader ...")
            self.freqtrade.state = State.STOPPED
        except Exception:
            logger.exception("Unexpected error in trading cycle, retrying in %s seconds...",
                             RETRY_TIMEOUT)
            try:
                self.freqtrade.notify_status(
                    f"*Unexpected error:*\n```\n{traceback.format_exc()}```\n"
                    "Bot will retry next cycle.",
                    msg_type=RPCMessageType.EXCEPTION,
                )
            except Exception:
                pass
            time.sleep(RETRY_TIMEOUT)

    def _reconfigure(self) -> None:
        """
        Cleans up current freqtradebot instance, reloads the configuration and
        replaces it with the new instance
        """
        # Tell systemd that we initiated reconfiguration
        self._notify("RELOADING=1")

        # Clean up current freqtrade modules
        self.freqtrade.cleanup()

        # Load and validate config and create new instance of the bot
        self._init(True)

        self.freqtrade.notify_status(f"{State(self.freqtrade.state)} after config reloaded")

        # Tell systemd that we completed reconfiguration
        self._notify("READY=1")

    def exit(self) -> None:
        # Tell systemd that we are exiting now
        self._notify("STOPPING=1")

        if self.freqtrade:
            self.freqtrade.notify_status("process died")
            self.freqtrade.cleanup()
