"""
ReplayRunner — drives ``FreqtradeBot.process()`` candle-by-candle at full speed.

Flow:
  1. Load config from file(s).
  2. enforce_replay_safety()  → dry-run forced, credentials blanked, DB namespaced.
  3. Force a StaticPairList on the requested pairs (dynamic pairlists can't be
     replayed historically — documented divergence).
  4. Validate local data; auto-download is opt-in (off by default).
  5. Start the virtual clock; build the replay exchange.
  6. Patch ExchangeResolver.load_exchange → our exchange, and make
     Wallets.record_wallet_state record one snapshot per simulated day.
  7. Build the bot, then step the clock at 1-minute resolution calling
     bot.process() each tick.
  8. Print a summary; restore every patch in ``finally``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from freqtrade.configuration import Configuration
from freqtrade.enums import CandleType, ExitCheckTuple, ExitType, State
from freqtrade.exceptions import OperationalException
from freqtrade.exchange import timeframe_to_seconds
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.persistence import Trade
from freqtrade.replay.clock import VirtualClock
from freqtrade.replay.data_store import ReplayDataStore
from freqtrade.replay.exchange import build_replay_exchange
from freqtrade.replay.safety import enforce_replay_safety
from freqtrade.resolvers import ExchangeResolver, StrategyResolver
from freqtrade.util.datetime_helpers import dt_floor_day, dt_now
from freqtrade.wallets import Wallets


logger = logging.getLogger(__name__)

# 1-minute sub-candle resolution: stops and limit orders are checked against
# real 1m prices, not just the strategy-timeframe close.
SUB_STEP_SECONDS = 60
# Warmup buffer for indicator history before the visible replay window.
WARMUP_GUARD_CANDLES = 50
# Seed-mode floor: never auto-advance the replay start past (end - this) days,
# so one very-new pair can't collapse the window to nothing.
MIN_SEED_WINDOW_DAYS = 14


def _resolve_wallet(config: dict, wallet: float | None) -> float:
    """
    Effective starting wallet for a replay: an explicit --wallet is authoritative;
    without it, fall back to the config's dry_run_wallet so replay and other tools
    run on the same capital (a silent hardcoded 1000 used to starve multi-position
    configs). Any explicit override that diverges from the config is logged loudly.
    """
    config_wallet = config.get("dry_run_wallet")
    if wallet is None:
        return float(config_wallet) if config_wallet else 1000.0
    if config_wallet and float(config_wallet) != wallet:
        logger.warning(
            "Replay wallet %.2f (explicit --wallet) overrides the config's "
            "dry_run_wallet %.2f — results will not be capital-comparable.",
            wallet,
            float(config_wallet),
        )
    return wallet


# Which pairlist handlers a replay can honour, and why the others cannot.
#
# A replay re-runs the real chain every cycle against local data, so anything derived
# from candles, from the synthetic tickers, or from the bot's own trade history is
# faithful. What it cannot do is reach the network or read state that only exists now:
# replaying those would silently substitute TODAY's answer for the past's, which is
# worse than refusing — it looks like a result.
_REPLAYABLE_PAIRLISTS = {
    "StaticPairList",
    "VolumePairList",  # ticker mode via get_tickers, range mode via candles
    "AgeFilter",  # listing age from candle history
    "ExtremeMoveFilter",  # daily candles
    "TrendRegularityFilter",  # candles
    "VolatilityFilter",  # candles
    "RangeStabilityFilter",
    "PerformanceFilter",  # the bot's own trades — the replay writes them
    "PriceFilter",  # synthetic ticker last price
    "PrecisionFilter",  # market metadata
    "PercentChangePairList",  # synthetic ticker 24h percentage
    "FullTradesFilter",
    "ShuffleFilter",
    "OffsetFilter",
}

_NON_REPLAYABLE_PAIRLISTS = {
    "MarketCapPairList": "queries an external market-cap API; a replay would rank past "
    "pairs by TODAY's capitalisation",
    "RemotePairList": "fetches the list from a remote URL that has no historical form",
    "ProducerPairList": "mirrors another running bot's live whitelist",
    "DelistFilter": "needs the venue's current delisting notices, which do not exist "
    "for a past date",
    "SpreadFilter": "needs a real bid/ask spread; the synthetic tickers price bid=ask, "
    "so every pair would pass and the filter would be silently inert",
    "PairInformationFilter": "declares itself biased in backtesting for the same reason",
    "CrossMarketPairList": "reads another exchange's live markets",
}


def _validate_replayable_pairlists(config: dict) -> None:
    """Refuse to start rather than replay a chain we cannot honour faithfully.

    Silently dropping an unsupported handler would produce a plausible-looking run whose
    universe never matched the bot's — the class of error that makes a whole audit
    worthless without ever raising.
    """
    methods = [p.get("method") for p in (config.get("pairlists") or [])]
    blocked = [m for m in methods if m in _NON_REPLAYABLE_PAIRLISTS]
    unknown = [m for m in methods if m not in _REPLAYABLE_PAIRLISTS and m not in blocked]
    if not blocked and not unknown:
        return
    lines = ["[dry-run replay] this config's pairlist chain cannot be replayed faithfully:"]
    for m in blocked:
        lines.append(f"  - {m}: {_NON_REPLAYABLE_PAIRLISTS[m]}")
    for m in unknown:
        lines.append(
            f"  - {m}: unknown to the replay harness. If it derives only from candles, "
            f"tickers or the bot's own trades, add it to _REPLAYABLE_PAIRLISTS in "
            f"freqtrade/replay/runner.py."
        )
    lines.append(
        "Remove the handler from the config used for the replay, or run with "
        "--static-pairlist to pin --pairs as a fixed list instead."
    )
    msg = "\n".join(lines)
    logger.error(msg)
    raise OperationalException(msg)


def run_replay(
    pairs: list[str],
    start_dt: datetime,
    end_dt: datetime,
    strategy: str,
    *,
    config: dict | None = None,
    config_path: str | None = None,
    slippage_pct: float = 0.0005,
    wallet: float | None = None,
    db_url: str | None = None,
    datadir: str | None = None,
    fresh: bool = True,
    auto_download: bool = False,
    seed: bool = False,
    sub_step: int = SUB_STEP_SECONDS,
    reset_db: bool = False,
    dynamic_pairlist: bool = True,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    """
    Run a dry-run replay and return a summary dict.

    Pass either ``config`` (an already-loaded config dict, e.g. from the API
    server) or ``config_path`` (a path the CLI loads from files).
    ``progress_callback(pct, step)`` is called periodically (pct in 0..1).

    ``seed=True`` writes into the caller-supplied ``db_url`` (the bot's own
    dry-run DB) instead of a namespaced ``*.replay.sqlite``, and refuses to run
    unless the loaded config is genuinely dry-run — so a live bot's trade
    history can never be overwritten.
    """
    # ── 1. Config ────────────────────────────────────────────────────────
    if config is None:
        if config_path is None:
            raise ValueError("run_replay needs either config or config_path")
        config = Configuration.from_files([config_path])
    config = deepcopy(config)
    # A replay simulates ONE bot, alone, in the PAST. Fleet coordination reads the
    # sibling registry — i.e. the real fleet's databases as they are TODAY — so leaving
    # it on makes January's entries answer to August's positions: every coin currently
    # held by a live sibling is silently BLOCKED for the whole replay. Observed: a
    # 36-pair replay produced 0 trades over two simulated months while its signals fired
    # and its sizing passed; the arbiter replay ran coordination='strict' against the
    # live fleet without anyone noticing. Coordination is a LIVE-fleet concern; in a
    # replay it is pure anachronism, so it is forced off unconditionally.
    config["position_coordination"] = {"mode": "off"}

    # Seed guard: refuse to populate a non-dry-run bot's database.
    if seed and config.get("dry_run") is not True:
        raise RuntimeError(
            "Refusing to seed: the bot is not in dry-run mode (would overwrite live history)."
        )

    config["strategy"] = strategy
    if db_url:
        config["db_url"] = db_url

    # Neutralise live-only capital accounting so the simulated wallet can trade:
    # available_capital / capital_withdrawal track the real account and would
    # otherwise zero out the deployable capital (e.g. a past withdrawal).
    wallet = _resolve_wallet(config, wallet)
    config["dry_run_wallet"] = wallet
    config["available_capital"] = wallet
    config["capital_withdrawal"] = 0.0
    # Freeze the sizing wallet: without this, paper profits over months of replay
    # compound into total_closed_profit -> available_amount -> stake, ballooning
    # positions (and the funding fees computed on them) into fantasy millions.
    # Wallets honours this flag to keep replay stakes bounded by the real capital.
    config["replay_lock_wallet"] = True
    # Profit-history sampling stamps wall-clock time; under the virtual clock that would
    # write misleading rows into the seeded DB - disable it for replays.
    config["profit_history_interval_s"] = 0

    exchange_cfg = config.setdefault("exchange", {})
    exchange_cfg["pair_whitelist"] = list(pairs)
    # The user explicitly chose --pairs; don't let the live blacklist drop them.
    if exchange_cfg.get("pair_blacklist"):
        logger.info("Clearing pair_blacklist for replay (explicit --pairs): %s", pairs)
        exchange_cfg["pair_blacklist"] = []
    # Pairlist: static by default, the config's real chain on request.
    #
    # A live bot does not trade a fixed list — it trades whatever its VolumePairList /
    # PerformanceFilter chain selects that day, and that selection IS part of the edge:
    # dynv1 live trades ~40 rotating top-volume pairs, while a static replay of its 199
    # candidates is a different strategy wearing the same name. A backtest cannot close
    # this gap at all (it evaluates pairlists once, at start); the replay can, because it
    # re-runs the chain every cycle against the synthetic tickers built from local
    # candles (see ReplayExchangeMixin.get_tickers).
    #
    # Off by default: it is slower, and pinning to --pairs stays the right choice when
    # the question is "how does this strategy behave on THESE pairs". Turn it on when
    # the question is "what would this bot actually have traded".
    if dynamic_pairlist:
        _validate_replayable_pairlists(config)
        logger.info(
            "[dry-run replay] dynamic pairlist ENABLED — replaying the config's own chain "
            "(%s) over %d candidate pairs.",
            ", ".join(p.get("method", "?") for p in (config.get("pairlists") or [])),
            len(pairs),
        )
    else:
        config["pairlists"] = [{"method": "StaticPairList"}]
    # Don't touch the fork's shared OHLCV disk cache during a replay — we feed
    # candles ourselves and must not pollute the live bots' cache.
    config["shared_ohlcv_cache"] = {"enabled": False}

    # ── 2. Safety gate (forces dry-run, blanks creds, namespaces DB) ─────
    # Seed mode keeps the bot's own dry-run db_url (no namespacing).
    enforce_replay_safety(config, namespace_db=not seed)

    # ── 2b. Seed-mode DB prep (DB-integrity critical) ────────────────────
    db_file = _sqlite_file(config["db_url"])
    end_dt, pre_existing_ids, noop = _prep_db(
        config,
        db_file,
        seed=seed,
        fresh=fresh,
        reset_db=reset_db,
        start_dt=start_dt,
        end_dt=end_dt,
        pairs=pairs,
        sub_step=sub_step,
    )
    if noop is not None:  # real trades already cover the window → graceful no-op
        return noop

    # Resolve timeframe + startup from the strategy when the config omits them
    # (strategies usually define `timeframe`/`startup_candle_count` in the class).
    # StrategyResolver populates config["timeframe"] as a side effect, keeping the
    # bot and our pre-flight data validation in agreement.
    strat = StrategyResolver.load_strategy(config)
    tf: str = config.get("timeframe") or strat.timeframe
    config["timeframe"] = tf
    tf_secs: int = timeframe_to_seconds(tf)
    trading_mode: str = config.get("trading_mode", "futures")
    candle_type = CandleType.get_default(trading_mode)
    startup: int = strat.startup_candle_count or config.get("startup_candle_count", 50)
    data_start = start_dt - timedelta(seconds=(startup + WARMUP_GUARD_CANDLES) * tf_secs)

    datadir = datadir or _default_datadir(config, trading_mode)

    # Feed enough history for the strategy's warmup, plus a buffer for signals.
    store_cap = startup + 200

    # ── 3. Data store + validation ───────────────────────────────────────
    store = ReplayDataStore(datadir, trading_mode=trading_mode, max_candles=store_cap)

    # Seed mode (dry auto-launch): short-history assets (e.g. xyz indices, listed
    # 2026-03) may lack the strategy's warmup at the requested start_date. Seed mode
    # then drops every such pair and aborts with "None of the bot's current pairs
    # have local data". Instead, advance the replay start so pairs that DO have data
    # get their full warmup. Capped by MIN_SEED_WINDOW_DAYS so a single very-new pair
    # can't shrink the replay window to nothing (those pairs are dropped as before).
    if seed:
        warmup_delta = timedelta(seconds=(startup + WARMUP_GUARD_CANDLES) * tf_secs)
        ready_starts = []
        for p in pairs:
            rng = store.date_range(p, tf, candle_type)
            if rng is not None:
                ready_starts.append(rng[0] + warmup_delta)
        if ready_starts:
            cap = end_dt - timedelta(days=MIN_SEED_WINDOW_DAYS)
            want = min(max(ready_starts), cap)
            if want > start_dt:
                logger.info(
                    "[replay] advancing start %s -> %s so short-history pairs get "
                    "the strategy's %d-candle warmup",
                    start_dt.date(),
                    want.date(),
                    startup,
                )
                start_dt = want
                data_start = start_dt - warmup_delta

    data_ctx = {
        "tf": tf,
        "candle_type": candle_type,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "startup": startup,
        "data_start": data_start,
        "datadir": datadir,
        "trading_mode": trading_mode,
        "store_cap": store_cap,
        "seed": seed,
        "auto_download": auto_download,
        "config_path": config_path,
    }
    store, pairs = _ensure_pair_data(store, pairs, data_ctx)
    config["exchange"]["pair_whitelist"] = list(pairs)

    # ── 4. Virtual clock + exchange ──────────────────────────────────────
    wall_clock = time.monotonic  # captured before freezegun patches time.*
    clock = VirtualClock()
    clock.start(data_start)
    exchange = build_replay_exchange(config, store, clock, pairs, slippage_pct=slippage_pct)

    # ── 5. Patches (restored in finally) ─────────────────────────────────
    original_load_exchange = ExchangeResolver.load_exchange
    ExchangeResolver.load_exchange = staticmethod(lambda cfg, **kw: exchange)

    original_record_wallet = Wallets.record_wallet_state
    Wallets.record_wallet_state = _make_daily_wallet_recorder(original_record_wallet)
    restore_open_cache = None
    restore_precompute = None

    try:
        bot = FreqtradeBot(config)
        bot.state = State.RUNNING  # Worker normally sets this; we drive process() directly.
        bot.strategy.process_only_new_candles = True  # recompute indicators only at tf close.

        # Seed mode shares the DB with the still-running live bot. Make our connection
        # wait for its write locks instead of failing — a lock-induced rollback would
        # orphan an INSERT in the identity map and cascade into "UPDATE … 0 rows matched".
        if seed:
            _harden_sqlite_concurrency()

        _register_informative_pairs(bot, exchange, store)
        # Precompute each pair's indicators+signals once over the full series, then serve
        # slices instead of recomputing the window every candle (installed BEFORE the
        # shortcut so the latter wraps it).
        restore_precompute = _install_precomputed_analyze(bot, store, pairs, tf, candle_type)
        _install_analyze_shortcut(bot, tf, candle_type)
        restore_open_cache = _install_open_trades_cache()

        total_candles = int((end_dt - start_dt).total_seconds() / tf_secs)
        logger.info(
            "Replay: %s -> %s | %d candles x %d pairs | tf=%s | wallet=%.2f %s | "
            "slippage=%.4f%% | sub-step=%ds",
            start_dt.date(),
            end_dt.date(),
            total_candles,
            len(pairs),
            tf,
            config["dry_run_wallet"],
            config.get("stake_currency", ""),
            slippage_pct * 100,
            sub_step,
        )

        run_t0 = wall_clock()
        _drive_loop(
            bot,
            clock,
            start_dt,
            end_dt,
            tf_secs,
            wall_clock,
            sub_step=sub_step,
            progress_callback=progress_callback,
            store=store,
            candle_type=candle_type,
        )
        duration_s = wall_clock() - run_t0
        summary = _build_summary(config["db_url"])
        if seed:
            summary = _finalize_seed(
                config,
                db_file,
                pairs,
                start_dt,
                end_dt,
                summary,
                bot=bot,
                sub_step=sub_step,
                duration_s=duration_s,
                reset_db=reset_db,
                pre_existing_ids=pre_existing_ids,
            )
        _print_summary(summary)
        return summary
    finally:
        if restore_open_cache is not None:
            restore_open_cache()
        if restore_precompute is not None:
            restore_precompute()
        ExchangeResolver.load_exchange = original_load_exchange
        Wallets.record_wallet_state = original_record_wallet
        clock.stop()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _enforce_intracandle_sl(bot, store, clock, candle_type) -> None:
    """Post-process stoploss check: exit trades whose stop_loss was breached by
    the sub-step candle's high (shorts) or low (longs).

    bot.process() only evaluates stoploss against the close price.  This misses
    intra-candle breaches (e.g. a 5m candle whose high spikes through a short's
    -5% SL but closes below it).  The backtester handles this via low=/high=
    params to should_exit(); here we achieve the same by post-checking OHLC."""
    now = clock.now()
    for trade in Trade.get_open_trades():
        if not trade.stop_loss or trade.stop_loss == 0:
            continue
        candle = None
        for tf in ("1m", "5m", "15m"):
            candle = store.get_candle_ohlc(trade.pair, tf, candle_type, now)
            if candle is not None:
                break
        if candle is None:
            continue
        breached = False
        if trade.is_short and candle["high"] >= trade.stop_loss:
            breached = True
        elif not trade.is_short and candle["low"] <= trade.stop_loss:
            breached = True
        if breached:
            try:
                exit_check = ExitCheckTuple(exit_type=ExitType.STOP_LOSS)
                bot.execute_trade_exit(trade, trade.stop_loss, exit_check)
                logger.info(
                    "[replay-SL] %s intra-candle SL hit: stop=%.6f candle=[%.6f, %.6f] → exited",
                    trade.pair,
                    trade.stop_loss,
                    candle["low"],
                    candle["high"],
                )
            except Exception as exc:
                logger.warning("[replay-SL] failed to exit %s: %s", trade.pair, exc)


def _enforce_liquidations(bot, store, clock, candle_type) -> None:
    """Liquidate trades whose liquidation price was crossed by the sub-step candle.

    Nothing else in the replay can do this: dry-run freqtrade never liquidates an open
    position (on a real venue the EXCHANGE does it, and the bot only detects the
    aftermath), and the fake exchange holds no positions at all. Without this hook a
    replayed loser can ride forever — measured on the dynv1 audit: 15 legitimate
    backtest liquidations against 0 possible in replay, which silently turned the
    replay into the optimistic simulator it was built to keep honest.

    Runs AFTER the intra-candle stoploss hook on purpose: for a short, the stop sits
    below the liquidation price, so a candle that crosses both is a stop exit in
    reality — the stop hook has already closed it by the time we look.

    The close fills at the liquidation price with the standard taker fee. Real
    Hyperliquid liquidations are slightly worse (mark-price execution, maintenance
    penalty), so this stays marginally optimistic — but bounded and documented, not
    unbounded and silent."""
    now = clock.now()
    for trade in Trade.get_open_trades():
        lp = trade.liquidation_price
        if not lp:
            continue
        candle = None
        for tf in ("1m", "5m", "15m"):
            candle = store.get_candle_ohlc(trade.pair, tf, candle_type, now)
            if candle is not None:
                break
        if candle is None:
            continue
        breached = (trade.is_short and candle["high"] >= lp) or (
            not trade.is_short and candle["low"] <= lp
        )
        if breached:
            try:
                exit_check = ExitCheckTuple(exit_type=ExitType.LIQUIDATION)
                bot.execute_trade_exit(trade, lp, exit_check)
                logger.info(
                    "[replay-LIQ] %s liquidated: liq=%.6f candle=[%.6f, %.6f] lev=%s",
                    trade.pair,
                    lp,
                    candle["low"],
                    candle["high"],
                    trade.leverage,
                )
            except Exception as exc:
                logger.warning("[replay-LIQ] failed to liquidate %s: %s", trade.pair, exc)


def _drive_loop(
    bot,
    clock,
    start_dt,
    end_dt,
    tf_secs,
    wall_clock,
    sub_step=SUB_STEP_SECONDS,
    progress_callback=None,
    store=None,
    candle_type=None,
) -> None:
    current = start_dt
    processed = 0
    wall_start = wall_clock()
    total_sim = (end_dt - start_dt).total_seconds()
    ticks = 0
    failed_ticks = 0
    first_tick_error: Exception | None = None

    while current < end_dt:
        clock.advance_to(current)
        ticks += 1
        try:
            bot.process()
        except Exception as exc:  # keep going; one bad tick shouldn't abort the run
            failed_ticks += 1
            if first_tick_error is None:
                first_tick_error = exc
            logger.warning(
                "[dry-run replay] bot.process() raised at %s: %s", current, exc, exc_info=True
            )
            # A failed flush poisons the SQLAlchemy session — every later commit then
            # fails with "transaction has been rolled back". Roll back so the next tick
            # starts from a clean session instead of cascading the whole run.
            with suppress(Exception):
                Trade.session.rollback()

        if store is not None and candle_type is not None:
            try:
                _enforce_intracandle_sl(bot, store, clock, candle_type)
            except Exception:
                pass
            try:
                _enforce_liquidations(bot, store, clock, candle_type)
            except Exception:
                pass

        if current.timestamp() % tf_secs == 0:
            processed += 1
            if processed % 24 == 0:
                elapsed_sim = (current - start_dt).total_seconds()
                frac = elapsed_sim / total_sim if total_sim else 0.0
                elapsed_wall = wall_clock() - wall_start
                eta_wall = elapsed_wall * (1 - frac) / frac if frac > 0 else None
                n_open = len(Trade.get_open_trades())
                n_closed = len(Trade.get_trades_proxy(is_open=False))
                step = f"{current:%Y-%m-%d %H:%M}  open={n_open} closed={n_closed}"
                logger.info("  %5.1f%%  %s  (%.0fs wall)", frac * 100, step, elapsed_wall)
                if progress_callback:
                    progress_callback(
                        {
                            "progress": frac,
                            "step": step,
                            "elapsed_s": round(elapsed_wall, 1),
                            "eta_s": round(eta_wall, 1) if eta_wall is not None else None,
                        }
                    )

        current += timedelta(seconds=sub_step)

    # Honest completion: a run where (nearly) every tick raised is NOT "done" —
    # it silently produced an empty book (incident 2026-08-02: 7 seeds "done" with
    # 0 trades, 100% of ticks failing on the same exception). Fail loudly instead.
    if ticks >= 100 and failed_ticks / ticks > 0.5:
        raise RuntimeError(
            f"[dry-run replay] {failed_ticks}/{ticks} ticks raised — run is not trustworthy. "
            f"First error: {first_tick_error!r}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_daily_wallet_recorder(original):
    """
    Wrap Wallets.record_wallet_state so it fires at most once per simulated day.

    The DB has a UNIQUE(timestamp, currency) constraint and the original floors
    the timestamp to the day; calling it on every 1m tick would collide. One
    snapshot per simulated day is exactly what FreqUI's equity chart wants, and
    avoids reimplementing internal wallet logic.
    """
    state = {"last_day": None}

    def _recorder(self):
        if getattr(self, "_is_backtest", False):
            return
        day = dt_floor_day(dt_now())
        if state["last_day"] == day:
            return
        state["last_day"] = day
        original(self)

    return _recorder


def _register_informative_pairs(bot, exchange, store) -> None:
    """Make informative pairs known to the exchange; warn if their data is absent."""
    try:
        inf_pairs = bot.strategy.informative_pairs()
    except Exception as exc:
        logger.warning("Could not read informative_pairs(): %s", exc)
        return

    extra = {item[0] for item in inf_pairs}
    extra -= set(exchange._markets)
    if not extra:
        return

    exchange.add_pair_markets(sorted(extra))
    for pair in sorted(extra):
        if not (store.has(pair, CandleType.FUTURES) or store.has(pair, CandleType.SPOT)):
            logger.warning(
                "Informative pair %s has no local data — its indicators will be empty.", pair
            )
    logger.info("Registered informative pairs: %s", sorted(extra))


def _precompute_analyzed(strat, store, pairs, tf, candle_type) -> dict:
    """Run advise_indicators+entry+exit once over each pair's full series."""
    out: dict = {}
    for pair in pairs:
        full = store._load(pair, tf, candle_type)
        if full is None or full.empty:
            continue
        meta = {"pair": pair}
        try:
            df = strat.advise_indicators(full.copy(), meta)
            df = strat.advise_entry(df, meta)
            df = strat.advise_exit(df, meta)
        except Exception as exc:
            logger.warning(
                "[dry-run replay] precompute failed for %s (%s); per-candle path", pair, exc
            )
            continue
        out[pair] = df.sort_values("date").reset_index(drop=True)
    return out


def _slice_to_window(full, window):
    """Slice the precomputed frame to the served window's date range (O(log n))."""
    lo = int(full["date"].searchsorted(window.iloc[0]["date"], side="left"))
    hi = int(full["date"].searchsorted(window.iloc[-1]["date"], side="right"))
    return full.iloc[lo:hi].reset_index(drop=True) if hi > lo else None


def _last_row_matches(a, b) -> bool:
    """Whether the last (decision) row of ``a`` equals ``b``'s across every column."""
    import pandas as pd

    if a is None or a.empty or b.empty or a.iloc[-1]["date"] != b.iloc[-1]["date"]:
        return False
    ra, rb = a.iloc[-1], b.iloc[-1]
    for col in b.columns:
        if col not in a.columns:
            return False
        va, vb = ra.get(col), rb.get(col)
        if pd.isna(va) and pd.isna(vb):
            continue
        if pd.isna(va) != pd.isna(vb):
            return False
        try:
            if abs(float(va) - float(vb)) > 1e-9 * (1 + abs(float(vb))):
                return False
        except (TypeError, ValueError):
            if va != vb:
                return False
    return True


def _install_precomputed_analyze(bot, store, pairs, tf, candle_type):
    """
    Precompute each pair's analysed dataframe (indicators + entry/exit signals) ONCE
    over the full series, then serve the matching slice on each new candle instead of
    recomputing the window every tick. The single biggest faithful speedup for long runs.

    **Self-validating**: on the first call per pair it does a real recompute and compares
    the last (decision) row to the precomputed slice; only pairs that match exactly switch
    to the fast path — strategies whose indicators don't precompute identically (repainting,
    or informative data pulled through the dataprovider) silently fall back to the
    per-candle recompute. So it is faithful for *any* strategy. Returns a restore callable.
    """
    import pandas as pd

    strat = bot.strategy
    dp = strat.dp
    original = strat.analyze_pair
    cdef = strat.config.get("candle_type_def", candle_type)

    precomputed = _precompute_analyzed(strat, store, pairs, tf, candle_type)
    verified: set[str] = set()

    def analyze_pair(pair):
        full = precomputed.get(pair)
        if full is None:
            return original(pair)
        if pair not in verified:
            # First touch: recompute for real (also caches it), then validate the slice.
            original(pair)
            live_df = dp.get_analyzed_dataframe(pair, tf)[0]
            # Guard: if the real analyze failed/produced nothing, live_df is empty and
            # _slice_to_window would IndexError on iloc[0] — fall through to the
            # mismatch path (per-candle recompute) instead of blowing up the tick.
            sliced = (
                _slice_to_window(full, live_df)
                if isinstance(live_df, pd.DataFrame) and not live_df.empty
                else None
            )
            if _last_row_matches(sliced, live_df):
                verified.add(pair)
            else:
                precomputed.pop(pair, None)
                logger.info("[dry-run replay] %s: precompute mismatch, per-candle path", pair)
            return  # original() already cached the correct dataframe for this candle
        window = dp.ohlcv(pair, tf, candle_type=cdef)
        if not isinstance(window, pd.DataFrame) or window.empty:
            return
        sliced = _slice_to_window(full, window)
        if sliced is None:
            return original(pair)
        dp._set_cached_df(pair, tf, sliced, candle_type=cdef)

    strat.analyze_pair = analyze_pair
    logger.info(
        "[dry-run replay] precomputed indicators for %d/%d pairs", len(precomputed), len(pairs)
    )

    def restore():
        strat.analyze_pair = original

    return restore


def _install_analyze_shortcut(bot, tf, candle_type) -> None:
    """
    Skip ``strategy.analyze_pair()`` between candle closes — replay-scoped, perfectly faithful.

    With ``process_only_new_candles=True`` freqtrade already skips the heavy indicator
    pass between closes, but it STILL re-runs ``remove_entry_exit_signals`` + dataframe
    validation on every 1-minute tick, and the result is discarded (the analysed dataframe
    used for entries/exits is only refreshed on a *new* candle). Since the served candles
    are unchanged between closes, that work is pure overhead.

    We short-circuit when the latest served candle date for a pair is unchanged: the cached
    analysed dataframe (and therefore every entry/exit decision) is byte-identical — only the
    redundant pandas/validation work is avoided. Fail-safe: if the klines aren't found we fall
    through to the original, so behaviour is never altered.
    """
    klines = bot.exchange._klines
    original = bot.strategy.analyze_pair
    seen: dict = {}

    def analyze_pair(pair: str) -> None:
        df = klines.get((pair, tf, candle_type))
        if df is not None and len(df):
            last = df["date"].iloc[-1]
            if seen.get(pair) == last:
                return  # same candle as last tick → re-analysis would be identical
            seen[pair] = last
        original(pair)

    bot.strategy.analyze_pair = analyze_pair


def _harden_sqlite_concurrency(timeout_ms: int = 60000) -> None:
    """
    Make the replay's SQLite connection resilient to the live bot writing the same DB.

    ``busy_timeout`` makes the connection wait for the other process's write lock instead
    of raising (a lock-induced rollback would orphan an INSERT in the identity map and
    cascade into "UPDATE … 0 rows matched"); WAL improves reader/writer concurrency.
    Both are best-effort.
    """
    from sqlalchemy import event, text

    engine = Trade.session.get_bind()
    if engine is None or engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute(f"PRAGMA busy_timeout={timeout_ms}")
        with suppress(Exception):
            cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    with suppress(Exception):
        Trade.session.execute(text(f"PRAGMA busy_timeout={timeout_ms}"))
        Trade.session.execute(text("PRAGMA journal_mode=WAL"))
        Trade.session.commit()


def _install_open_trades_cache():
    """
    Memoise ``Trade.get_open_trades()`` between database flushes — faithful and
    replay-scoped. The bot queries open trades ~5x per tick through the SQLAlchemy
    ORM (statement compile + cache-key gen + instance hydration), which the audit
    showed dominates the loop. The open-trade set only changes when a trade is
    created / adjusted / closed — i.e. on a session *flush*. We cache the result and
    invalidate on every ``after_flush`` (over-invalidation is safe), so callers always
    see an up-to-date set. Returns a restore callable.
    """
    from sqlalchemy import event

    original = Trade.get_open_trades
    state: dict = {"cache": None}

    def cached_get_open_trades():
        if state["cache"] is None:
            state["cache"] = original()
        return state["cache"]

    def _invalidate(*_args):
        state["cache"] = None

    session = Trade.session
    event.listen(session, "after_flush", _invalidate)
    Trade.get_open_trades = staticmethod(cached_get_open_trades)

    def restore():
        Trade.get_open_trades = staticmethod(original)
        with suppress(Exception):
            event.remove(session, "after_flush", _invalidate)

    return restore


def _ensure_pair_data(store, pairs, ctx):
    """
    Validate that local data covers each pair for the run window.

    Seed mode (the auto-pairs UI flow) tolerates pairs without data — they're dropped
    and the rest is replayed. Otherwise auto-download (if enabled) or raise with a hint.
    Returns ``(store, pairs)`` (store/pairs may be rebuilt/filtered).
    """
    missing = _validate_pairs(
        store, pairs, ctx["tf"], ctx["candle_type"], ctx["start_dt"], ctx["end_dt"], ctx["startup"]
    )
    if not missing:
        return store, pairs

    if ctx["seed"]:
        kept = [p for p in pairs if p not in missing]
        if not kept:
            raise RuntimeError("None of the bot's current pairs have local data for this period.")
        logger.warning(
            "Replay seed: skipping %d pair(s) without local data: %s", len(missing), missing
        )
        return store, kept

    if ctx["auto_download"] and ctx["config_path"]:
        _download_data(
            ctx["config_path"],
            missing,
            ctx["data_start"],
            ctx["end_dt"],
            ctx["datadir"],
            ctx["trading_mode"],
        )
        store = ReplayDataStore(
            ctx["datadir"], trading_mode=ctx["trading_mode"], max_candles=ctx["store_cap"]
        )
        _validate_pairs(
            store,
            pairs,
            ctx["tf"],
            ctx["candle_type"],
            ctx["start_dt"],
            ctx["end_dt"],
            ctx["startup"],
            raise_on_fail=True,
        )
        return store, pairs

    raise RuntimeError(
        "Missing/insufficient data for: "
        + ", ".join(missing)
        + "\nDownload it yourself (recommended — controls exchange rate-limits):\n"
        + _download_hint(
            ctx["config_path"] or "<config>",
            missing,
            ctx["data_start"],
            ctx["end_dt"],
            ctx["datadir"],
            ctx["trading_mode"],
        )
        + "\nOr re-run with --auto-download."
    )


def _validate_pairs(store, pairs, tf, candle_type, start_dt, end_dt, startup, raise_on_fail=False):
    missing = []
    for pair in pairs:
        try:
            store.validate(pair, tf, candle_type, start_dt, end_dt, startup)
        except ValueError as exc:
            if raise_on_fail:
                raise
            logger.warning("Data check failed for %s: %s", pair, exc)
            missing.append(pair)
    return missing


def _default_datadir(config: dict, trading_mode: str) -> str:
    # Freqtrade resolves config["datadir"] to user_data/data/<exchange> already,
    # so the feathers live one level down in <trading_mode>.
    base = config.get("datadir")
    if base:
        return str(Path(base) / trading_mode)
    exchange_name = config.get("exchange", {}).get("name", "hyperliquid")
    return str(Path("user_data/data") / exchange_name / trading_mode)


def _drop_sqlite_db(db_url: str) -> None:
    if not db_url.startswith("sqlite:///"):
        return
    path = db_url.replace("sqlite:///", "")
    for suffix in ("", "-shm", "-wal"):
        p = Path(path + suffix)
        if p.exists():
            p.unlink()
            logger.info("Removed existing replay DB file: %s", p)


def _sqlite_file(db_url: str | None) -> Path | None:
    prefix = "sqlite:///"
    if db_url and db_url.startswith(prefix):
        return Path(db_url[len(prefix) :])
    return None


def _finalize_seed(
    config,
    db_file,
    pairs,
    start_dt,
    end_dt,
    summary,
    *,
    bot,
    sub_step,
    duration_s,
    reset_db,
    pre_existing_ids,
):
    """Post-seed: flatten replay open trades, tag, write the marker, guarantee DB integrity."""
    # Hand the live bot a FLAT book: close every replay-created open trade at the last
    # simulated price. A simulated position left open would be marked against the *current*
    # live market on resume and display nonsensical profit — never carry one across.
    truncated = _flatten_replay_open_trades(bot, pre_existing_ids)
    _tag_replay_trades(exclude_ids=pre_existing_ids)
    summary = _build_summary(config["db_url"])  # refresh after reconciliation
    _write_seed_marker(
        start_dt,
        end_dt,
        pairs,
        summary,
        resolution=sub_step,
        duration_s=duration_s,
        reset_db=reset_db,
        truncated=truncated,
    )
    # Integrity guarantee: never leave a corrupt DB — restore the pre-seed backup.
    if not _db_quick_check(db_file):
        _restore_backup(db_file)
        raise RuntimeError("Replay DB failed the integrity check — restored the pre-replay backup.")
    return summary


class NothingToReplay(Exception):
    """Seed window is empty (real trades already cover it). Not an error — a graceful no-op."""


def _seed_noop(db_url, exc, start_dt, end_dt, pairs, sub_step, reset_db) -> dict:
    """Real trades already cover the requested window → nothing to seed. Mark the DB seeded
    (idempotent) and return a 'skipped' summary so the bot resumes cleanly (no error/reload)."""
    from freqtrade.persistence import init_db

    logger.info("[dry-run replay] nothing to replay: %s — marking seeded (no-op)", exc)
    init_db(db_url)
    summary = _build_summary(db_url)
    summary["skipped"] = True
    summary["skip_reason"] = str(exc)
    _write_seed_marker(start_dt, end_dt, pairs, summary, resolution=sub_step, reset_db=reset_db)
    return summary


def _prep_db(config, db_file, *, seed, fresh, reset_db, start_dt, end_dt, pairs, sub_step) -> tuple:
    """Seed/fresh DB preparation. Returns ``(end_dt, pre_existing_ids, noop_summary)`` where
    ``noop_summary`` is non-None only when there is nothing to replay (graceful skip)."""
    pre_existing_ids: set[int] = set()
    if seed:
        try:
            end_dt, pre_existing_ids = _prepare_seed_db(
                db_file, reset_db=reset_db, start_dt=start_dt, end_dt=end_dt
            )
        except NothingToReplay as exc:
            return (
                end_dt,
                pre_existing_ids,
                _seed_noop(config["db_url"], exc, start_dt, end_dt, pairs, sub_step, reset_db),
            )
    elif fresh:
        # Namespaced replay DB only — never drop the bot's live dry-run file.
        _drop_sqlite_db(config["db_url"])
    return end_dt, pre_existing_ids, None


def _prepare_seed_db(db_file, *, reset_db, start_dt, end_dt):
    """
    Seed-mode DB prep. ``reset_db`` → wipe rows. Otherwise preserve existing trades and
    cap the window at the first real (non-replay) trade. Returns ``(end_dt, pre_existing_ids)``.
    Raises ``NothingToReplay`` if the (capped) window is empty — the caller treats that as a
    successful no-op (the bot is already covered by real trades), not a failure.
    """
    if reset_db:
        _truncate_trades(db_file)
        return end_dt, set()
    pre_existing = _existing_trade_ids(db_file)
    cutoff = _earliest_real_trade_open(db_file)
    if cutoff is not None and cutoff < end_dt:
        logger.info(
            "[dry-run replay] reset_db=false → capping end at first real trade (%s)", cutoff
        )
        end_dt = cutoff
    if end_dt <= start_dt:
        raise NothingToReplay("existing real trades start at/before the requested start date")
    return end_dt, pre_existing


def _truncate_trades(db_file: Path | None) -> None:
    """Wipe trade rows (not the file) — reset_db=true. Safe under the bot's open connection."""
    if db_file is None or not db_file.exists():
        return
    import sqlite3

    conn = sqlite3.connect(str(db_file), timeout=60)
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with conn:  # transaction
            for table in ("orders", "trade_custom_data", "trades", "pairlocks"):
                if table in existing:
                    conn.execute(f"DELETE FROM {table}")
            if "KeyValueStore" in existing:
                conn.execute("DELETE FROM KeyValueStore WHERE key = 'ft_replay_seed'")
        logger.info("[dry-run replay] reset_db: cleared existing trades from %s", db_file.name)
    finally:
        conn.close()


def _existing_trade_ids(db_file: Path | None) -> set[int]:
    if db_file is None or not db_file.exists():
        return set()
    import sqlite3

    conn = sqlite3.connect(str(db_file), timeout=60)
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE name='trades'").fetchone():
            return set()
        return {int(r[0]) for r in conn.execute("SELECT id FROM trades")}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def _earliest_real_trade_open(db_file: Path | None) -> datetime | None:
    """Open date of the earliest non-replay trade (the cap for reset_db=false), or None."""
    if db_file is None or not db_file.exists():
        return None
    import sqlite3

    conn = sqlite3.connect(str(db_file), timeout=60)
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE name='trades'").fetchone():
            return None
        row = conn.execute(
            "SELECT MIN(open_date) FROM trades WHERE enter_tag IS NULL OR enter_tag NOT LIKE ?",
            (REPLAY_TAG + "%",),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    try:
        dt = datetime.fromisoformat(str(row[0]))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _db_quick_check(db_file: Path | None) -> bool:
    """PRAGMA quick_check — True if the DB is structurally sound."""
    if db_file is None or not db_file.exists():
        return True
    import sqlite3

    try:
        conn = sqlite3.connect(str(db_file), timeout=60)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _restore_backup(db_file: Path | None) -> None:
    """Restore the pre-replay snapshot over a corrupt seeded DB (last-resort safety)."""
    if db_file is None:
        return
    bak = db_file.with_suffix(db_file.suffix + ".pre-replay.bak")
    if not bak.exists():
        logger.error("[dry-run replay] no pre-replay backup to restore (%s)", bak.name)
        return
    import shutil

    from freqtrade.persistence import Trade

    with suppress(Exception):  # drop ORM connections before overwriting the file
        Trade.session.get_bind().dispose()
    for suffix in ("-wal", "-shm"):
        with suppress(OSError):
            Path(str(db_file) + suffix).unlink()
    shutil.copy2(bak, db_file)
    logger.error("[dry-run replay] DB integrity check failed — restored %s", bak.name)


def _download_hint(config_path, pairs, start_dt, end_dt, datadir, trading_mode) -> str:
    dl_datadir = datadir[: -(len(trading_mode) + 1)] if datadir.endswith(trading_mode) else datadir
    return (
        f"  freqtrade download-data --config {config_path} \\\n"
        f"    --pairs {' '.join(pairs)} \\\n"
        f"    --timeframes 1m 5m 15m 1h 4h \\\n"
        f"    --timerange {start_dt:%Y%m%d}-{end_dt:%Y%m%d} \\\n"
        f"    --trading-mode {trading_mode} --datadir {dl_datadir}"
    )


def _download_data(config_path, pairs, start_dt, end_dt, datadir, trading_mode) -> None:
    import subprocess

    dl_datadir = datadir[: -(len(trading_mode) + 1)] if datadir.endswith(trading_mode) else datadir
    cmd = [
        "freqtrade",
        "download-data",
        "--config",
        config_path,
        "--pairs",
        *pairs,
        "--timeframes",
        "1m",
        "5m",
        "15m",
        "1h",
        "4h",
        "--timerange",
        f"{start_dt:%Y%m%d}-{end_dt:%Y%m%d}",
        "--trading-mode",
        trading_mode,
        "--datadir",
        dl_datadir,
    ]
    logger.info("Auto-downloading data for %s …", pairs)
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        logger.warning("download-data exited with code %d", result.returncode)


REPLAY_TAG = "[replay]"


def _is_replay_tag(tag: str | None) -> bool:
    return bool(tag) and tag.startswith(REPLAY_TAG)


def _tag_replay_trades(exclude_ids: set[int] | None = None) -> None:
    """
    Prefix replay-created trades' ``enter_tag`` with ``[replay]`` so the UI flags them
    unambiguously. ``exclude_ids`` are trades that existed *before* the seed (the bot's
    real trades) — they must never be tagged. Idempotent.
    """
    from freqtrade.persistence import Trade

    exclude_ids = exclude_ids or set()
    try:
        for trade in Trade.get_trades_proxy():
            if trade.id in exclude_ids:
                continue
            tag = trade.enter_tag or ""
            if tag.startswith(REPLAY_TAG):
                continue
            trade.enter_tag = f"{REPLAY_TAG} {tag}".strip()
        Trade.commit()
    except Exception as exc:
        logger.warning("[dry-run replay] could not tag replay trades: %s", exc)


def _flatten_replay_open_trades(bot, pre_existing_ids: set[int]) -> int:
    """
    A seed must hand the live bot a FLAT book. Close *every* replay-created open trade at the
    last simulated price (``exit_reason='replay_seed_end'``); real (pre-existing) trades are
    left untouched. Carrying a simulated open position across the sim→live boundary would mark
    it against the *current* live market on resume and display nonsensical profit (e.g.
    +387000%), and could trigger DCA/exits on a position the bot never really held — so we
    never leave one open. Returns the count closed.
    """
    from freqtrade.persistence import Trade

    try:
        open_trades = Trade.get_open_trades()
    except Exception:
        return 0
    replay = [t for t in open_trades if t.id not in pre_existing_ids]
    closed = 0
    for trade in replay:
        rate = None
        try:
            rate = bot.exchange.fetch_ticker(trade.pair).get("last")
        except Exception:
            rate = None
        if not rate or rate <= 0:
            rate = trade.open_rate  # last resort: neutral close (price unavailable)
        try:
            trade.close(rate)
            trade.exit_reason = "replay_seed_end"
            closed += 1
        except Exception as exc:
            logger.warning("[dry-run replay] could not flatten replay trade %s: %s", trade.id, exc)
    if closed:
        Trade.commit()
        logger.info("[dry-run replay] flattened %d replay open trade(s) at seed end", closed)
    return closed


def _write_seed_marker(
    start_dt,
    end_dt,
    pairs,
    summary: dict,
    *,
    resolution=60,
    duration_s=None,
    reset_db=False,
    truncated=0,
) -> None:
    """
    Mark this dry-run DB as replay-seeded (KeyValueStore). The UI reads it to grey out
    re-seeding and to render the detail panel; wiping the dry DB removes the marker.
    """
    import json

    from freqtrade.persistence import KeyValueStore

    try:
        KeyValueStore.store_value(
            "ft_replay_seed",
            json.dumps(
                {
                    "timerange": f"{start_dt:%Y%m%d}-{end_dt:%Y%m%d}",
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "resolution_s": resolution,
                    "duration_s": round(duration_s, 1) if duration_s is not None else None,
                    "reset_db": bool(reset_db),
                    "truncated_open": truncated,
                    "pairs": list(pairs),
                    "result": summary,
                    "closed_trades": summary.get("closed_trades"),
                    "seeded_at": dt_now().isoformat(),
                }
            ),
        )
    except Exception as exc:
        logger.warning("Could not write replay seed marker: %s", exc)


def _build_summary(db_url: str) -> dict:
    """Aggregate replay results into a JSON-serialisable dict (also used by the API)."""
    closed = Trade.get_trades_proxy(is_open=False)
    open_trades = Trade.get_trades_proxy(is_open=True)

    profits_pct = [t.close_profit for t in closed if t.close_profit is not None]
    profits_abs = [t.close_profit_abs for t in closed if t.close_profit_abs is not None]
    wins = [p for p in profits_pct if p > 0]
    gross_profit = sum(p for p in profits_abs if p > 0)
    gross_loss = abs(sum(p for p in profits_abs if p <= 0))

    return {
        "db_url": db_url,
        "closed_trades": len(closed),
        "open_trades": len(open_trades),
        "wins": len(wins),
        "win_rate": (len(wins) / len(profits_pct)) if profits_pct else None,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else None,
        "total_profit_abs": round(sum(profits_abs), 4) if profits_abs else 0.0,
        "total_profit_ratio": round(sum(profits_pct), 6) if profits_pct else 0.0,
    }


def _print_summary(s: dict) -> None:
    width = 60
    print("\n" + "=" * width)
    print("  REPLAY SUMMARY  (simulated dry-run — not live)")
    print("=" * width)
    print(f"  DB           : {s['db_url']}")
    print(f"  Closed trades: {s['closed_trades']}")
    print(f"  Open trades  : {s['open_trades']}")
    if not s["closed_trades"]:
        print("  No closed trades.")
        print("=" * width + "\n")
        return
    wr = f"{100 * s['win_rate']:.1f}%" if s["win_rate"] is not None else "n/a"
    pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "inf"
    print(f"  Win rate     : {s['wins']}/{s['closed_trades']} ({wr})")
    print(f"  Profit factor: {pf}")
    print(f"  Total P&L    : {s['total_profit_abs']:+.2f}  ({100 * s['total_profit_ratio']:+.3f}%)")
    print("=" * width)
    print("  View in FreqUI: run a normal dry-run with api_server enabled and")
    print(f"  db_url={s['db_url']}\n")
