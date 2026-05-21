#!/usr/bin/env python3
"""
export_strategies.py — Export live/dry-run strategies into a shareable .zip archive.

Given a directory of freqtrade bot configs (e.g. ``live_configs/``), this tool
discovers every bot config, classifies it as **live** or **dry-run**, and bundles
the following per strategy into a single ``.zip``:

    <archive>/
    ├── MANIFEST.json          machine-readable summary of the export
    ├── README.txt             human summary + how it was generated + warnings
    ├── live/
    │   └── <StrategyName>/
    │       ├── config.json            sanitized config (NO api keys / secrets)
    │       ├── <StrategyFile>.py      the strategy source
    │       ├── <StrategyFile>.json    hyperopt params (if present)
    │       ├── backtest_stdout.txt    6-month backtest summary  (only --with-backtest)
    │       ├── <Strategy>_backtest.*  raw freqtrade backtest export (only --with-backtest)
    │       └── backtest_FAILED.txt    captured error if the backtest could not run
    └── dry/
        └── <StrategyName>/ ...

Design goals
------------
* **Generic** — not tied to one codebase. Point ``--config-dir`` at any folder of
  freqtrade configs; it works for spot/futures, any exchange, any pairlist setup.
* **Safe** — secrets (api keys, private keys, wallet addresses, passwords, tokens)
  are stripped, ``add_config_files`` references are dropped, and a final scan aborts
  the export if anything that looks like a private key still slipped through.
* **Robust** — every step is guarded. A single bad config or a failed backtest
  warns and is recorded, it never aborts the whole export.

The config the bot actually runs is reconstructed by resolving ``add_config_files``
exactly like freqtrade does, so ``dry_run`` (and other inherited keys) are read
correctly even when they live in an included file.

Usage
-----
    python user_data/export_strategies.py --config-dir live_configs

    # include a 6-month backtest per strategy (slow, needs local data):
    python user_data/export_strategies.py --config-dir live_configs --with-backtest

    # preview what would be exported, write nothing:
    python user_data/export_strategies.py --config-dir live_configs --list-only

Run ``python user_data/export_strategies.py --help`` for all options.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Exact (case-insensitive) config key names whose values are secrets / sensitive.
# Exact matching avoids false positives such as ``sort_key`` containing "key".
SENSITIVE_KEY_NAMES = {
    "key", "secret", "password", "uid", "apikey", "api_key",
    "apisecret", "api_secret", "privatekey", "walletaddress", "token",
    "jwt_secret_key", "ws_token", "chat_id", "username", "cors_origins",
}

# Keys that merely *reference* external files and must never be exported.
DROP_KEY_NAMES = {"add_config_files", "config_files"}

REDACTION_PLACEHOLDER = "<REDACTED — provide your own>"

# Config-dir filename globs skipped by default (freqtrade convention: a leading
# underscore marks an include / access file, not a runnable bot config).
DEFAULT_IGNORE_GLOBS = ("_*", "*example*", ".*")

# Keys stripped from the *backtest* config (runtime-only / fork-specific / would
# break a vanilla freqtrade schema). The *exported* config keeps everything but secrets.
BACKTEST_STRIP_KEYS = {
    "db_url", "api_server", "telegram", "bot_name", "initial_state",
    "position_coordination", "force_entry_enable", "edge", "webhook",
    "discord", "external_message_consumer",
}

# Patterns that, if found in a staged config.json, abort the export (backstop).
SUSPECTED_SECRET_PATTERNS = (
    re.compile(r"0x[0-9a-fA-F]{40,}"),       # eth address / private key
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

DATA_NON_CANDLE_MARKERS = ("funding_rate", "-mark", "-index", "-premiumIndex")


# --------------------------------------------------------------------------- #
# Logging helpers (no external deps, plays nice in a terminal)
# --------------------------------------------------------------------------- #

class Log:
    VERBOSE = False
    _warnings: list[str] = []

    @staticmethod
    def info(msg: str) -> None:
        print(f"  {msg}")

    @staticmethod
    def ok(msg: str) -> None:
        print(f"\033[32m[OK]\033[0m   {msg}")

    @staticmethod
    def warn(msg: str) -> None:
        Log._warnings.append(msg)
        print(f"\033[33m[WARN]\033[0m {msg}", file=sys.stderr)

    @staticmethod
    def error(msg: str) -> None:
        print(f"\033[31m[ERROR]\033[0m {msg}", file=sys.stderr)

    @staticmethod
    def debug(msg: str) -> None:
        if Log.VERBOSE:
            print(f"\033[90m[dbg]\033[0m  {msg}")

    @staticmethod
    def step(msg: str) -> None:
        print(f"\n\033[1m{msg}\033[0m")


# --------------------------------------------------------------------------- #
# Config loading — prefer freqtrade's own loader, fall back to a JSONC parser
# --------------------------------------------------------------------------- #

try:
    from freqtrade.configuration.load_config import load_config_file as _ft_load_config_file
    from freqtrade.configuration.load_config import load_from_files as _ft_load_from_files

    HAVE_FREQTRADE = True
except Exception:  # pragma: no cover - depends on environment
    HAVE_FREQTRADE = False


def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments (freqtrade configs allow them), string-aware."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str = True
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    stripped = "".join(out)
    # remove trailing commas before } or ]
    return re.sub(r",(\s*[}\]])", r"\1", stripped)


def _fallback_load_config_file(path: Path) -> dict[str, Any]:
    return json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))


def _deep_merge(source: dict, destination: dict) -> dict:
    """Mirror freqtrade.misc.deep_merge_dicts: values in *source* win."""
    for key, value in source.items():
        if isinstance(value, dict):
            node = destination.setdefault(key, {})
            if isinstance(node, dict):
                _deep_merge(value, node)
            else:
                destination[key] = deepcopy(value)
        elif value is not None:
            destination[key] = value
    return destination


def _fallback_load_from_files(path: Path, _level: int = 0) -> dict[str, Any]:
    """Resolve add_config_files relative to each file's parent, like freqtrade."""
    if _level > 5:
        raise ValueError("add_config_files loop detected")
    config = _fallback_load_config_file(path)
    sub_refs = config.get("add_config_files")
    if sub_refs:
        merged_sub: dict[str, Any] = {}
        for ref in sub_refs:
            sub_path = (path.resolve().parent / ref)
            sub = _fallback_load_from_files(sub_path, _level + 1)
            merged_sub = _deep_merge(sub, merged_sub)
        config = _deep_merge(config, merged_sub)
    return config


def load_raw_config(path: Path) -> dict[str, Any]:
    """Load a single config file (JSONC ok), WITHOUT resolving add_config_files."""
    if HAVE_FREQTRADE:
        return _ft_load_config_file(str(path))
    return _fallback_load_config_file(path)


def load_merged_config(path: Path) -> dict[str, Any]:
    """Load a config WITH add_config_files resolved/merged (what the bot really runs)."""
    if HAVE_FREQTRADE:
        return _ft_load_from_files([str(path)])
    return _fallback_load_from_files(path)


# --------------------------------------------------------------------------- #
# Sanitization
# --------------------------------------------------------------------------- #

def _db_url_has_credentials(url: str) -> bool:
    return bool(re.search(r"://[^/@\s]*:[^/@\s]*@", url))


def sanitize_config(cfg: Any, removed: set[str], _path: str = "") -> Any:
    """Recursively drop include refs and redact secret values. Records what changed."""
    if isinstance(cfg, dict):
        out: dict[str, Any] = {}
        for key, value in cfg.items():
            kl = key.lower()
            if kl in DROP_KEY_NAMES:
                removed.add(f"{key} (dropped)")
                continue
            if kl == "db_url" and isinstance(value, str) and _db_url_has_credentials(value):
                removed.add("db_url (credentials redacted)")
                out[key] = REDACTION_PLACEHOLDER
                continue
            if kl in SENSITIVE_KEY_NAMES:
                removed.add(key)
                out[key] = REDACTION_PLACEHOLDER
                continue
            out[key] = sanitize_config(value, removed, f"{_path}.{key}")
        return out
    if isinstance(cfg, list):
        return [sanitize_config(v, removed, _path) for v in cfg]
    return cfg


def final_secret_scan(text: str) -> list[str]:
    """Return human descriptions of any suspected secret still present in *text*."""
    hits = []
    for pat in SUSPECTED_SECRET_PATTERNS:
        if pat.search(text):
            hits.append(pat.pattern)
    return hits


# --------------------------------------------------------------------------- #
# Strategy / params / data resolution
# --------------------------------------------------------------------------- #

def find_strategy_file(strategy_name: str, search_dirs: list[Path]) -> Path | None:
    """Locate the .py file defining ``class <strategy_name>``.

    First tries ``<dir>/<strategy_name>.py`` (freqtrade convention), then scans
    every .py for ``class <strategy_name>(...)`` to support class != filename.
    """
    class_re = re.compile(rf"^\s*class\s+{re.escape(strategy_name)}\s*[\(:]", re.M)
    for d in search_dirs:
        candidate = d / f"{strategy_name}.py"
        if candidate.is_file():
            return candidate
    for d in search_dirs:
        if not d.is_dir():
            continue
        for py in sorted(d.glob("*.py")):
            try:
                if class_re.search(py.read_text(encoding="utf-8", errors="ignore")):
                    return py
            except OSError:
                continue
    return None


def find_params_file(strategy_py: Path) -> Path | None:
    """Hyperopt params live next to the strategy .py with the same stem + .json."""
    candidate = strategy_py.with_suffix(".json")
    return candidate if candidate.is_file() else None


def detect_timeframe(merged_cfg: dict, strategy_py: Path | None) -> str | None:
    tf = merged_cfg.get("timeframe")
    if tf:
        return str(tf)
    if strategy_py and strategy_py.is_file():
        m = re.search(
            r"^\s*timeframe\s*[:=]\s*['\"]([0-9]+[smhdwM])['\"]",
            strategy_py.read_text(encoding="utf-8", errors="ignore"),
            re.M,
        )
        if m:
            return m.group(1)
    return None


DATA_FORMAT_EXT = {"feather": ".feather", "parquet": ".parquet",
                   "json": ".json", "jsongz": ".json.gz"}


def discover_data_pairs(
    datadir: Path, exchange: str, trading_mode: str, timeframe: str | None,
    data_format: str = "feather",
) -> list[str]:
    """Build a static pair list from locally downloaded OHLCV data filenames."""
    base = datadir / exchange
    if trading_mode == "futures":
        base = base / "futures"
    if not base.is_dir():
        return []

    ext = DATA_FORMAT_EXT.get(data_format, ".feather")
    tf_glob = f"-{timeframe}-" if timeframe else "-"
    pairs: set[str] = set()
    for f in base.glob(f"*{tf_glob}*{ext}"):
        name = f.stem
        if any(marker in name for marker in DATA_NON_CANDLE_MARKERS):
            continue
        head = name.split("-")[0]            # BASE_QUOTE[_SETTLE]
        parts = head.split("_")
        if trading_mode == "futures":
            if len(parts) >= 3:
                pairs.add(f"{'_'.join(parts[:-2])}/{parts[-2]}:{parts[-1]}")
        else:
            if len(parts) >= 2:
                pairs.add(f"{'_'.join(parts[:-1])}/{parts[-1]}")
    return sorted(pairs)


def apply_blacklist(pairs: list[str], blacklist: list[str]) -> list[str]:
    compiled = []
    for pat in blacklist or []:
        try:
            compiled.append(re.compile(pat))
        except re.error:
            Log.debug(f"skipping invalid blacklist regex: {pat}")
    if not compiled:
        return pairs
    return [p for p in pairs if not any(c.fullmatch(p) for c in compiled)]


# --------------------------------------------------------------------------- #
# Backtesting (best-effort, never fatal)
# --------------------------------------------------------------------------- #

def build_backtest_config(merged_cfg: dict, static_pairs: list[str]) -> dict:
    cfg = sanitize_config(merged_cfg, set())  # secrets out, then trim runtime keys
    for key in list(cfg.keys()):
        if key.lower() in BACKTEST_STRIP_KEYS:
            cfg.pop(key, None)
    cfg["dry_run"] = True
    cfg["pairlists"] = [{"method": "StaticPairList"}]
    # entry/exit pricing are required by the backtest schema; live configs inherit
    # them via add_config_files, but inject sane defaults for minimal configs.
    cfg.setdefault("entry_pricing", {"price_side": "same", "use_order_book": True,
                                     "order_book_top": 1})
    cfg.setdefault("exit_pricing", {"price_side": "same", "use_order_book": True,
                                    "order_book_top": 1})
    cfg.setdefault("exchange", {})
    cfg["exchange"]["pair_whitelist"] = static_pairs
    # exchange auth is not needed for offline backtesting; leave placeholders out
    for secret in ("key", "secret", "password", "uid", "walletAddress", "privateKey"):
        cfg["exchange"].pop(secret, None)
    return cfg


def _collect_backtest_artifact(results_dir: Path, strategy: str, out_dir: Path) -> list[str]:
    """Copy the just-produced backtest result (via .last_result.json) into out_dir."""
    last = results_dir / ".last_result.json"
    if not last.is_file():
        return []
    try:
        latest = json.loads(last.read_text(encoding="utf-8")).get("latest_backtest")
    except (OSError, json.JSONDecodeError):
        return []
    if not latest:
        return []
    src = results_dir / latest
    copied: list[str] = []
    # the result file plus its sibling .meta.json (stem may end in .zip or .json)
    for candidate in (src, src.with_suffix(src.suffix + ".meta.json"),
                      src.with_suffix(".meta.json")):
        if candidate.is_file():
            dest_name = f"{strategy}_backtest{''.join(candidate.suffixes)}"
            shutil.copy2(candidate, out_dir / dest_name)
            copied.append(dest_name)
    return copied


def run_backtest(
    *,
    freqtrade_bin: str,
    strategy: str,
    strategy_dir: Path,
    bt_config: dict,
    timerange: str,
    userdir: Path,
    datadir: Path | None,
    exchange: str,
    out_dir: Path,
    timeout: int,
    data_format: str = "feather",
) -> dict:
    """Run one backtest. Returns a status dict; writes artifacts into *out_dir*."""
    result: dict[str, Any] = {"status": "unknown", "timerange": timerange}
    n_pairs = len(bt_config.get("exchange", {}).get("pair_whitelist", []))
    if n_pairs == 0:
        result["status"] = "skipped"
        result["reason"] = "no pairs available (no local data and no --backtest-pairs)"
        (out_dir / "backtest_FAILED.txt").write_text(
            "Backtest skipped: no pairs to test.\n"
            "Provide --backtest-pairs or download data with `freqtrade download-data`.\n",
            encoding="utf-8",
        )
        return result

    if shutil.which(freqtrade_bin) is None:
        result["status"] = "skipped"
        result["reason"] = f"freqtrade binary '{freqtrade_bin}' not found on PATH"
        (out_dir / "backtest_FAILED.txt").write_text(
            f"Backtest skipped: '{freqtrade_bin}' not found.\n", encoding="utf-8"
        )
        return result

    results_dir = userdir / "backtest_results"

    with tempfile.TemporaryDirectory(prefix="bt_") as tmp:
        tmp_path = Path(tmp)
        cfg_file = tmp_path / "backtest_config.json"
        cfg_file.write_text(json.dumps(bt_config, indent=2), encoding="utf-8")

        # NOTE: --export-filename is deprecated/ignored when backtesting; freqtrade
        # always writes to <userdir>/backtest_results/ and updates .last_result.json.
        cmd = [
            freqtrade_bin, "backtesting",
            "-c", str(cfg_file),
            "-s", strategy,
            "--strategy-path", str(strategy_dir),
            "--timerange", timerange,
            "--export", "trades",
            "--cache", "none",
            "--data-format-ohlcv", data_format,
            "--userdir", str(userdir),
        ]
        if datadir is not None:
            # freqtrade uses an explicit --datadir literally (it does NOT append the
            # exchange name like it does for the default), so point it at the
            # exchange-specific folder. The futures/ subdir is handled internally.
            ex_datadir = datadir / exchange if exchange else datadir
            cmd += ["--datadir", str(ex_datadir)]

        result["command"] = " ".join(cmd)
        result["pairs_tested"] = n_pairs
        Log.debug(f"backtest cmd: {result['command']}")

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=str(userdir.parent)
            )
        except subprocess.TimeoutExpired:
            result["status"] = "failed"
            result["reason"] = f"timed out after {timeout}s"
            (out_dir / "backtest_FAILED.txt").write_text(
                f"Backtest timed out after {timeout}s.\nCommand:\n{result['command']}\n",
                encoding="utf-8",
            )
            return result

        stdout, stderr = proc.stdout or "", proc.stderr or ""
        if proc.returncode != 0:
            result["status"] = "failed"
            result["reason"] = f"freqtrade exited with code {proc.returncode}"
            (out_dir / "backtest_FAILED.txt").write_text(
                f"Command:\n{result['command']}\n\n"
                f"--- STDOUT ---\n{stdout[-8000:]}\n\n--- STDERR ---\n{stderr[-8000:]}\n",
                encoding="utf-8",
            )
            return result

        # success: capture human-readable summary + raw export artifact
        (out_dir / "backtest_stdout.txt").write_text(stdout, encoding="utf-8")
        result["status"] = "ok"
        result["artifacts"] = _collect_backtest_artifact(results_dir, strategy, out_dir)
    return result


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def discover_configs(
    config_dir: Path, recursive: bool, ignore_globs: tuple[str, ...]
) -> list[Path]:
    files = sorted(config_dir.rglob("*.json") if recursive else config_dir.glob("*.json"))
    kept = []
    for f in files:
        if any(Path(f.name).match(g) for g in ignore_globs):
            Log.debug(f"ignored by convention: {f.name}")
            continue
        kept.append(f)
    return kept


def classify_environment(merged_cfg: dict, config_name: str) -> str:
    dry = merged_cfg.get("dry_run")
    if dry is True:
        return "dry"
    if dry is False:
        return "live"
    Log.warn(
        f"{config_name}: 'dry_run' not set anywhere (incl. add_config_files); "
        "classifying as 'dry' to be safe. Verify manually."
    )
    return "dry"


# --------------------------------------------------------------------------- #
# Main export routine
# --------------------------------------------------------------------------- #

def export(args: argparse.Namespace) -> int:  # noqa: C901 - linear orchestration, kept flat for readability
    config_dir = Path(args.config_dir).expanduser().resolve()
    if not config_dir.is_dir():
        Log.error(f"--config-dir is not a directory: {config_dir}")
        return 2

    userdir = Path(args.userdir).expanduser().resolve()
    if not userdir.is_dir():
        Log.warn(f"--userdir does not exist: {userdir} (backtests will likely fail)")

    strategy_dirs = [Path(d).expanduser().resolve() for d in args.strategy_dir]
    for d in strategy_dirs:
        if not d.is_dir():
            Log.warn(f"strategy dir does not exist: {d}")

    datadir = Path(args.datadir).expanduser().resolve() if args.datadir else (userdir / "data")

    Log.step(f"Scanning configs in {config_dir}")
    config_files = discover_configs(config_dir, args.recursive, tuple(args.ignore))
    if not config_files:
        Log.error("No candidate config files found.")
        return 2
    Log.info(f"{len(config_files)} candidate file(s) after ignore filter.")

    # timerange for backtests
    if args.backtest_timerange:
        timerange = args.backtest_timerange
    else:
        start = (datetime.now(UTC) - timedelta(days=args.backtest_days)).strftime("%Y%m%d")
        timerange = f"{start}-"

    staging_root = Path(tempfile.mkdtemp(prefix="strat_export_"))
    archive_stem = args.output_stem or (
        f"strategies_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    export_root = staging_root / archive_stem
    (export_root / "live").mkdir(parents=True, exist_ok=True)
    (export_root / "dry").mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "generator": "export_strategies.py",
        "freqtrade_available": HAVE_FREQTRADE,
        "config_dir": str(config_dir),
        "config_mode": args.config_mode,
        "include_backtest": args.with_backtest,
        "backtest": ({"timerange": timerange, "days": args.backtest_days}
                     if args.with_backtest else None),
        "environments": {"live": [], "dry": []},
        "redactions": sorted(set()),  # filled below
        "warnings": [],
    }
    all_redactions: set[str] = set()
    used_folders: dict[str, set[str]] = {"live": set(), "dry": set()}
    exported_count = 0

    for cfg_file in config_files:
        Log.step(f"> {cfg_file.name}")
        try:
            merged = load_merged_config(cfg_file)
        except Exception as e:
            Log.warn(f"{cfg_file.name}: cannot load/merge ({e}); skipped.")
            continue

        strategy = merged.get("strategy")
        if not strategy or not isinstance(strategy, str):
            Log.warn(
                f"{cfg_file.name}: no 'strategy' field after merge — skipped. "
                "Either an include/access file, or an incomplete bot config."
            )
            continue

        env = classify_environment(merged, cfg_file.name)

        # folder name = strategy, disambiguated on collision within the same env
        folder = strategy
        if folder in used_folders[env]:
            folder = f"{strategy}__{cfg_file.stem}"
            Log.warn(
                f"strategy '{strategy}' already exported under '{env}'; "
                f"using folder '{folder}' to disambiguate."
            )
        used_folders[env].add(folder)
        dest = export_root / env / folder
        dest.mkdir(parents=True, exist_ok=True)

        entry: dict[str, Any] = {
            "strategy": strategy,
            "config_file": cfg_file.name,
            "environment": env,
            "folder": f"{env}/{folder}",
            "warnings": [],
        }

        # 1) config.json (sanitized)
        source_cfg = merged if args.config_mode == "merged" else load_raw_config(cfg_file)
        redacted: set[str] = set()
        clean_cfg = sanitize_config(source_cfg, redacted)
        all_redactions |= redacted
        (dest / "config.json").write_text(
            json.dumps(clean_cfg, indent=4) + "\n", encoding="utf-8"
        )
        entry["config_redactions"] = sorted(redacted)
        Log.info(
            f"config.json written ({args.config_mode} mode, {len(redacted)} field(s) scrubbed)"
        )

        # 2) strategy file
        strategy_py = find_strategy_file(strategy, strategy_dirs)
        if strategy_py:
            shutil.copy2(strategy_py, dest / strategy_py.name)
            entry["strategy_file"] = strategy_py.name
            Log.info(f"strategy file: {strategy_py.name}")
        else:
            searched = [str(d) for d in strategy_dirs]
            msg = f"strategy file for class '{strategy}' not found in {searched}"
            Log.warn(f"{cfg_file.name}: {msg}")
            entry["warnings"].append(msg)
            entry["strategy_file"] = None

        # 3) params file
        params = find_params_file(strategy_py) if strategy_py else None
        if params:
            shutil.copy2(params, dest / params.name)
            entry["params_file"] = params.name
            Log.info(f"params file: {params.name}")
        else:
            entry["params_file"] = None

        # 4) backtest (optional, best-effort)
        if args.with_backtest:
            if not strategy_py:
                entry["backtest"] = {"status": "skipped", "reason": "strategy file not found"}
            else:
                if args.backtest_pairs:
                    static_pairs = list(args.backtest_pairs)
                else:
                    tf = detect_timeframe(merged, strategy_py)
                    pairs = discover_data_pairs(
                        datadir, merged.get("exchange", {}).get("name", ""),
                        merged.get("trading_mode", "spot"), tf, args.data_format,
                    )
                    blacklist = merged.get("exchange", {}).get("pair_blacklist", [])
                    static_pairs = apply_blacklist(pairs, blacklist)[: args.backtest_max_pairs]
                    Log.info(
                        f"backtest: {len(static_pairs)} pair(s) from local data "
                        f"(tf={tf or 'strategy-default'}, capped at {args.backtest_max_pairs})"
                    )
                bt_cfg = build_backtest_config(merged, static_pairs)
                Log.info(f"backtest: running {timerange} … (this can take a while)")
                bt_result = run_backtest(
                    freqtrade_bin=args.freqtrade_bin,
                    strategy=strategy,
                    strategy_dir=strategy_py.parent,
                    bt_config=bt_cfg,
                    timerange=timerange,
                    userdir=userdir,
                    datadir=datadir,
                    exchange=merged.get("exchange", {}).get("name", ""),
                    out_dir=dest,
                    timeout=args.backtest_timeout,
                    data_format=args.data_format,
                )
                entry["backtest"] = bt_result
                status = bt_result["status"]
                reason = f": {bt_result['reason']}" if bt_result.get("reason") else ""
                (Log.ok if status == "ok" else Log.warn)(f"backtest {status}{reason}")

        manifest["environments"][env].append(entry)
        exported_count += 1

    if exported_count == 0:
        Log.error("No bot configs (with a 'strategy' field) were found. Nothing to export.")
        shutil.rmtree(staging_root, ignore_errors=True)
        return 1

    manifest["redactions"] = sorted(all_redactions)
    manifest["warnings"] = list(Log._warnings)

    # write manifest + readme
    (export_root / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (export_root / "README.txt").write_text(_build_readme(manifest), encoding="utf-8")

    # ---- final safety scan over every staged config.json ----
    Log.step("Final secret scan")
    flagged: list[str] = []
    for cfg_json in export_root.rglob("config.json"):
        hits = final_secret_scan(cfg_json.read_text(encoding="utf-8", errors="ignore"))
        if hits:
            flagged.append(f"{cfg_json.relative_to(export_root)} → {hits}")
    if flagged:
        Log.error("Suspected secrets found in staged configs:")
        for f in flagged:
            Log.error(f"  {f}")
        if not args.allow_suspected_secrets:
            Log.error(
                "Aborting (no archive written). Re-run with --allow-suspected-secrets "
                "only if you are certain these are false positives."
            )
            shutil.rmtree(staging_root, ignore_errors=True)
            return 3
        Log.warn("Proceeding despite suspected secrets (--allow-suspected-secrets).")
    else:
        Log.ok("No secrets detected in staged configs.")

    # ---- list-only mode: print tree, write nothing ----
    if args.list_only:
        Log.step("Preview (--list-only, no archive written)")
        _print_tree(export_root)
        shutil.rmtree(staging_root, ignore_errors=True)
        return 0

    # ---- zip it ----
    if args.output:
        out_zip = Path(args.output).expanduser().resolve()
    else:
        # default into user_data (gitignored), so exports never get committed
        out_zip = userdir / f"{archive_stem}.zip"
    out_zip.parent.mkdir(parents=True, exist_ok=True)

    Log.step(f"Writing archive → {out_zip}")
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(export_root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(staging_root))
    shutil.rmtree(staging_root, ignore_errors=True)

    size_mb = out_zip.stat().st_size / (1024 * 1024)
    Log.step("Done")
    n_live = len(manifest["environments"]["live"])
    n_dry = len(manifest["environments"]["dry"])
    Log.ok(f"{exported_count} strategy folder(s): {n_live} live, {n_dry} dry")
    Log.ok(f"Archive: {out_zip} ({size_mb:.2f} MB)")
    if all_redactions:
        Log.info(f"Scrubbed fields: {', '.join(sorted(all_redactions))}")
    if Log._warnings:
        Log.info(f"{len(Log._warnings)} warning(s) — see above and MANIFEST.json.")
    return 0


def _build_readme(manifest: dict) -> str:
    lines = [
        "Strategy export",
        "=" * 60,
        f"Generated at : {manifest['generated_at']}",
        f"Source dir   : {manifest['config_dir']}",
        f"Config mode  : {manifest['config_mode']}",
        f"Backtests    : {'included' if manifest['include_backtest'] else 'not included'}",
    ]
    if manifest["include_backtest"]:
        lines.append(f"Backtest TR  : {manifest['backtest']['timerange']}")
    lines += [
        "",
        "SECURITY: configs were sanitized. add_config_files references were dropped",
        "and the following field names were redacted where present:",
        f"  {', '.join(manifest['redactions']) or '(none found)'}",
        "",
        "Layout: live/<Strategy>/ and dry/<Strategy>/ each contain the sanitized",
        "config.json, the strategy .py, its hyperopt .json (if any), and — when",
        "requested — a 6-month backtest summary. A backtest_FAILED.txt is written",
        "instead when a backtest could not run.",
        "",
        "NOTE: backtests use a StaticPairList rebuilt from locally available data,",
        "so they only *approximate* the live config's dynamic pairlists. Treat them",
        "as a sanity check, not a faithful replay of live behaviour.",
        "",
        "Contents",
        "-" * 60,
    ]
    for env in ("live", "dry"):
        for e in manifest["environments"][env]:
            bt = ""
            if manifest["include_backtest"] and e.get("backtest"):
                bt = f"  [backtest: {e['backtest'].get('status')}]"
            lines.append(f"  {e['folder']}  (from {e['config_file']}){bt}")
    return "\n".join(lines) + "\n"


def _print_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root.parent)
        depth = len(rel.parts) - 1
        prefix = "  " * depth
        print(f"{prefix}{path.name}{'/' if path.is_dir() else ''}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="export_strategies.py",
        description="Export live/dry strategies (config + code + params [+ backtest]) to a zip.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-d", "--config-dir", required=True,
                   help="Directory containing bot config JSON files (e.g. live_configs).")
    p.add_argument("-o", "--output", default=None,
                   help="Output .zip path. Default: <userdir>/strategies_export_<timestamp>.zip")
    p.add_argument("--output-stem", default=None,
                   help="Name of the top-level folder inside the zip (default: timestamped).")
    p.add_argument("--userdir", default="user_data",
                   help="freqtrade user_data dir (used for backtest data lookup and --userdir).")
    p.add_argument("--strategy-dir", action="append", default=None,
                   help="Strategy lookup dir (repeatable). Default: <userdir>/strategies")
    p.add_argument("--datadir", default=None,
                   help="OHLCV data dir for backtests. Default: <userdir>/data")
    p.add_argument("--config-mode", choices=("merged", "raw"), default="merged",
                   help="'merged' resolves add_config_files into a self-contained config; "
                        "'raw' exports only the literal bot file.")
    p.add_argument("--recursive", action="store_true",
                   help="Recurse into sub-directories of --config-dir.")
    p.add_argument("--ignore", action="append", default=list(DEFAULT_IGNORE_GLOBS),
                   help="Filename glob(s) to skip (repeatable). Default: _*, *example*, dotfiles.")

    bt = p.add_argument_group("backtest (optional, best-effort)")
    bt.add_argument("--with-backtest", action="store_true",
                    help="Run and include a backtest per strategy (slow; needs local data).")
    bt.add_argument("--backtest-days", type=int, default=180,
                    help="Lookback window in days for the backtest timerange.")
    bt.add_argument("--backtest-timerange", default=None,
                    help="Explicit timerange (e.g. 20250101-20250701). Overrides --backtest-days.")
    bt.add_argument("--backtest-pairs", nargs="+", default=None,
                    help="Explicit static pairlist for backtests (skips data-based discovery).")
    bt.add_argument("--backtest-max-pairs", type=int, default=100,
                    help="Cap on auto-discovered pairs per backtest (keeps runtime sane).")
    bt.add_argument("--backtest-timeout", type=int, default=1800,
                    help="Per-backtest timeout in seconds.")
    bt.add_argument("--data-format", choices=tuple(DATA_FORMAT_EXT), default="feather",
                    help="OHLCV data storage format used for backtest data discovery + loading.")
    bt.add_argument("--freqtrade-bin", default="freqtrade",
                    help="freqtrade executable used to run backtests.")

    p.add_argument("--list-only", action="store_true",
                   help="Show what would be exported and exit without writing the archive.")
    p.add_argument("--allow-suspected-secrets", action="store_true",
                   help="Do not abort if the final scan flags a suspected secret.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose/debug logging.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    Log.VERBOSE = args.verbose
    if args.strategy_dir is None:
        args.strategy_dir = [str(Path(args.userdir) / "strategies")]

    if not HAVE_FREQTRADE:
        Log.warn(
            "freqtrade is not importable in this interpreter — using the built-in "
            "JSONC loader/merger fallback. Results should match, but run inside the "
            "freqtrade venv for an exact match."
        )
    try:
        return export(args)
    except KeyboardInterrupt:
        Log.error("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
