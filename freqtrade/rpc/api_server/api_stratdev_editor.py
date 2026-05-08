"""
Strategy Dev Editor — read/write/validate config & strategy files.

Provides file editing capabilities for the FreqUI job launcher,
including JSON schema-driven autocompletion hints and syntax validation.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from freqtrade.rpc.api_server.deps import get_config


logger = logging.getLogger(__name__)
router = APIRouter()

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_\-\./]+$")

_WRITABLE_DIRS = {"custom_configs", "user_data/strategies"}

_READABLE_DIRS = {
    "backtest_configs", "config_examples", "live_configs",
    "user_data", "custom_configs",
}


class FileContent(BaseModel):
    path: str
    content: str
    language: str
    is_custom: bool


class FileSaveRequest(BaseModel):
    path: str
    content: str
    create_custom: bool = False


class FileSaveResponse(BaseModel):
    path: str
    is_custom: bool


class DiagnosticItem(BaseModel):
    line: int
    column: int
    message: str
    severity: str


class ValidationResult(BaseModel):
    valid: bool
    errors: list[DiagnosticItem]


class ValidateRequest(BaseModel):
    content: str
    language: str


def _resolve_and_guard(file_path: str, project_root: Path, writable: bool = False) -> Path:
    if not _SAFE_PATH_RE.match(file_path) or ".." in file_path:
        raise HTTPException(status_code=422, detail="Invalid file path")

    allowed = _WRITABLE_DIRS if writable else _READABLE_DIRS
    first_segment = file_path.split("/")[0]
    if first_segment not in {d.split("/")[0] for d in allowed}:
        raise HTTPException(status_code=403, detail=f"Access denied: {first_segment}/")

    resolved = (project_root / file_path).resolve()
    if not str(resolved).startswith(str(project_root.resolve())):
        raise HTTPException(status_code=403, detail="Path traversal blocked")

    return resolved


def _detect_language(path: str) -> str:
    if path.endswith(".json"):
        return "json"
    if path.endswith(".py"):
        return "python"
    return "text"


def _next_custom_id(project_root: Path) -> int:
    custom_dir = project_root / "custom_configs"
    custom_dir.mkdir(exist_ok=True)
    existing = []
    for f in custom_dir.glob("custom_config_*.json"):
        m = re.match(r"custom_config_(\d+)\.json", f.name)
        if m:
            existing.append(int(m.group(1)))
    return max(existing, default=0) + 1


@router.get("/stratdev/editor/file", response_model=FileContent)
def read_file(file_path: str, config=Depends(get_config)):
    project_root = Path(config.get("user_data_dir", "")).parent
    resolved = _resolve_and_guard(file_path, project_root, writable=False)

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        content = resolved.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Read error: {e}")

    return FileContent(
        path=file_path,
        content=content,
        language=_detect_language(file_path),
        is_custom=file_path.startswith("custom_configs/"),
    )


@router.post("/stratdev/editor/file", response_model=FileSaveResponse)
def save_file(req: FileSaveRequest, config=Depends(get_config)):
    project_root = Path(config.get("user_data_dir", "")).parent

    if req.create_custom:
        cid = _next_custom_id(project_root)
        rel_path = f"custom_configs/custom_config_{cid}.json"
        resolved = project_root / rel_path
        resolved.parent.mkdir(exist_ok=True)
    else:
        rel_path = req.path
        resolved = _resolve_and_guard(rel_path, project_root, writable=True)

        if resolved.is_file():
            bak = resolved.with_suffix(resolved.suffix + ".bak")
            try:
                shutil.copy2(resolved, bak)
            except Exception:
                pass

    try:
        resolved.write_text(req.content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Write error: {e}")

    logger.info(f"Saved file: {rel_path}")
    return FileSaveResponse(
        path=rel_path,
        is_custom=rel_path.startswith("custom_configs/"),
    )


@router.post("/stratdev/editor/validate", response_model=ValidationResult)
def validate_content(req: ValidateRequest):
    errors: list[DiagnosticItem] = []

    if req.language == "json":
        try:
            parsed = json.loads(req.content)
        except json.JSONDecodeError as e:
            errors.append(DiagnosticItem(
                line=e.lineno or 1,
                column=e.colno or 1,
                message=e.msg,
                severity="error",
            ))
            return ValidationResult(valid=False, errors=errors)

        try:
            from freqtrade.config_schema.config_schema import CONF_SCHEMA
            import jsonschema
            validator = jsonschema.Draft7Validator(CONF_SCHEMA)
            for err in validator.iter_errors(parsed):
                path_str = ".".join(str(p) for p in err.absolute_path) or "(root)"
                errors.append(DiagnosticItem(
                    line=_find_key_line(req.content, err.absolute_path),
                    column=1,
                    message=f"{path_str}: {err.message}",
                    severity="warning",
                ))
        except ImportError:
            pass

    elif req.language == "python":
        try:
            ast.parse(req.content)
        except SyntaxError as e:
            errors.append(DiagnosticItem(
                line=e.lineno or 1,
                column=e.offset or 1,
                message=e.msg,
                severity="error",
            ))
            return ValidationResult(valid=False, errors=errors)

        lines = req.content.split("\n")
        has_istrategy = any("IStrategy" in l for l in lines)
        if not has_istrategy:
            errors.append(DiagnosticItem(
                line=1, column=1,
                message="No IStrategy class found — this may not be a valid freqtrade strategy",
                severity="warning",
            ))

    return ValidationResult(valid=len(errors) == 0, errors=errors)


def _find_key_line(content: str, path) -> int:
    """Best-effort line number for a JSON schema error path."""
    if not path:
        return 1
    key = str(list(path)[-1])
    for i, line in enumerate(content.split("\n"), 1):
        if f'"{key}"' in line:
            return i
    return 1


@router.get("/stratdev/editor/schema")
def get_config_schema():
    try:
        from freqtrade.config_schema.config_schema import CONF_SCHEMA
    except ImportError:
        raise HTTPException(status_code=500, detail="Schema not available")

    props = {}
    for key, spec in CONF_SCHEMA.get("properties", {}).items():
        entry: dict = {
            "type": spec.get("type"),
            "description": spec.get("description", ""),
        }
        if "enum" in spec:
            entry["enum"] = spec["enum"]
        if "default" in spec:
            entry["default"] = spec["default"]
        if "minimum" in spec:
            entry["minimum"] = spec["minimum"]
        if "maximum" in spec:
            entry["maximum"] = spec["maximum"]
        props[key] = entry

    return {
        "properties": props,
        "required": CONF_SCHEMA.get("required", []),
    }


@router.get("/stratdev/editor/strategy-hints")
def get_strategy_hints():
    return {
        "hooks": [
            {"name": "populate_indicators", "signature": "(self, dataframe: DataFrame, metadata: dict) -> DataFrame", "doc": "Add technical indicators to the dataframe"},
            {"name": "populate_entry_trend", "signature": "(self, dataframe: DataFrame, metadata: dict) -> DataFrame", "doc": "Define entry signal conditions"},
            {"name": "populate_exit_trend", "signature": "(self, dataframe: DataFrame, metadata: dict) -> DataFrame", "doc": "Define exit signal conditions"},
            {"name": "bot_start", "signature": "(self, **kwargs) -> None", "doc": "Called once when the bot starts"},
            {"name": "bot_loop_start", "signature": "(self, current_time: datetime, **kwargs) -> None", "doc": "Called at the start of each trading loop iteration"},
            {"name": "custom_stake_amount", "signature": "(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float, min_stake: float | None, max_stake: float, leverage: float, entry_tag: str | None, side: str, **kwargs) -> float", "doc": "Customize the stake amount for each trade"},
            {"name": "custom_exit", "signature": "(self, pair: str, trade: Trade, current_time: datetime, current_rate: float, current_profit: float, **kwargs) -> str | bool | None", "doc": "Custom exit signal logic"},
            {"name": "adjust_trade_position", "signature": "(self, trade: Trade, current_time: datetime, current_rate: float, current_profit: float, min_stake: float | None, max_stake: float, current_entry_rate: float, current_exit_rate: float, current_entry_profit: float, current_exit_profit: float, **kwargs) -> float | None | tuple[float | None, str | None]", "doc": "DCA: adjust position size for open trades"},
            {"name": "confirm_trade_entry", "signature": "(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str, current_time: datetime, entry_tag: str | None, side: str, **kwargs) -> bool", "doc": "Confirm or reject a trade entry"},
            {"name": "confirm_trade_exit", "signature": "(self, pair: str, trade: Trade, order_type: str, amount: float, rate: float, time_in_force: str, exit_reason: str, current_time: datetime, **kwargs) -> bool", "doc": "Confirm or reject a trade exit"},
            {"name": "custom_stoploss", "signature": "(self, pair: str, trade: Trade, current_time: datetime, current_rate: float, current_profit: float, after_fill: bool, **kwargs) -> float | None", "doc": "Dynamic stoploss based on trade state"},
            {"name": "custom_entry_price", "signature": "(self, pair: str, trade: Trade | None, current_time: datetime, proposed_rate: float, entry_tag: str | None, side: str, **kwargs) -> float", "doc": "Custom entry price for limit orders"},
            {"name": "custom_exit_price", "signature": "(self, pair: str, trade: Trade, current_time: datetime, proposed_rate: float, current_profit: float, exit_tag: str | None, **kwargs) -> float", "doc": "Custom exit price for limit orders"},
            {"name": "leverage", "signature": "(self, pair: str, current_time: datetime, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag: str | None, side: str, **kwargs) -> float", "doc": "Custom leverage for futures trading"},
            {"name": "informative_pairs", "signature": "(self) -> list[tuple[str, str]]", "doc": "Define additional pairs/timeframes to download"},
        ],
        "dataframe_columns": [
            "open", "high", "low", "close", "volume", "date",
            "enter_long", "exit_long", "enter_short", "exit_short",
            "enter_tag", "exit_tag",
        ],
        "common_indicators": [
            "rsi", "ema", "sma", "wma", "dema", "tema", "kama",
            "bollinger", "bbands", "macd", "atr", "adx", "cci",
            "mfi", "obv", "willr", "stoch", "stochrsi", "roc",
            "mom", "trix", "psar", "ichimoku", "vwap", "supertrend",
        ],
        "parameters": {
            "DecimalParameter": "(default, low, high, decimals=3, space='buy', optimize=True)",
            "IntParameter": "(default, low, high, space='buy', optimize=True)",
            "BooleanParameter": "(default=True, space='buy', optimize=True)",
            "CategoricalParameter": "(categories, default=None, space='buy', optimize=True)",
        },
        "imports": [
            "from freqtrade.strategy import IStrategy, merge_informative_pair",
            "from freqtrade.strategy import DecimalParameter, IntParameter, BooleanParameter, CategoricalParameter",
            "from freqtrade.persistence import Trade",
            "import talib.abstract as ta",
            "import pandas_ta as pta",
            "from pandas import DataFrame",
            "from datetime import datetime",
        ],
    }
