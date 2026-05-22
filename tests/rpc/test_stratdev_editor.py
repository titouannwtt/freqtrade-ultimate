"""Tests for the strategy dev config editor validation endpoint (`/stratdev/editor/validate`).

Regression coverage for the fork-specific fix: a bot config that pulls in
`add_config_files` is a *partial* config — keys like `exchange.name` and the
`api_server` credentials block are supplied by the inherited files at merge time.
Validating the entry file standalone must therefore not flag those as missing,
while value checks (type/enum/range) on keys actually present must still fire.
"""
import json

from freqtrade.rpc.api_server.api_stratdev_editor import ValidateRequest, validate_content


# A partial config missing keys that, in a real bot, live in add_config_files
# (exchange.name, the api_server credentials block). On its own it trips six
# `required` warnings; with add_config_files declared those must be suppressed.
_PARTIAL_CONFIG = {
    "exchange": {"pair_whitelist": []},
    "api_server": {"enabled": True},
    "trading_mode": "futures",
}


def _validate(obj: dict):
    return validate_content(ValidateRequest(content=json.dumps(obj), language="json"))


class TestValidateContentInheritance:
    """`required`-property warnings are suppressed only when the config inherits."""

    def test_standalone_config_reports_required(self):
        # No add_config_files → the config is genuinely incomplete, so the missing
        # required keys must be surfaced (general stratdev editor behaviour, unchanged).
        res = _validate(_PARTIAL_CONFIG)
        msgs = [e.message for e in res.errors]
        assert any("exchange: 'name'" in m for m in msgs)
        assert any("api_server" in m for m in msgs)
        assert all(e.severity == "warning" for e in res.errors)
        assert not res.valid

    def test_inheriting_config_suppresses_required(self):
        # add_config_files present → those required keys come from the inherited files,
        # so no 'required' warning should be emitted. This is the bug being fixed.
        cfg = {**_PARTIAL_CONFIG, "add_config_files": ["_creds.json"]}
        res = _validate(cfg)
        assert res.errors == []
        assert res.valid

    def test_inheriting_config_still_reports_value_errors(self):
        # Suppression is limited to 'required': type/enum/range errors on keys actually
        # present must still be reported even when the config inherits.
        cfg = {
            **_PARTIAL_CONFIG,
            "add_config_files": ["_creds.json"],
            "trading_mode": "bogus",  # invalid enum
            "max_open_trades": -9,  # below the schema minimum of -1
        }
        res = _validate(cfg)
        msgs = " ".join(e.message for e in res.errors)
        assert "trading_mode" in msgs
        assert "max_open_trades" in msgs
        # No 'required' warning leaked through despite the partial config.
        assert not any("is a required property" in e.message for e in res.errors)

    def test_empty_add_config_files_does_not_suppress(self):
        # An explicit empty list is falsy → treated as a standalone config, so the
        # required warnings still fire (guards the bool(...) check, not just key presence).
        cfg = {**_PARTIAL_CONFIG, "add_config_files": []}
        res = _validate(cfg)
        assert any("is a required property" in e.message for e in res.errors)


class TestValidateContentBasics:
    def test_invalid_json_is_error(self):
        res = validate_content(ValidateRequest(content="{not valid", language="json"))
        assert not res.valid
        assert res.errors[0].severity == "error"

    def test_python_without_istrategy_warns(self):
        res = validate_content(ValidateRequest(content="x = 1\n", language="python"))
        assert any("IStrategy" in e.message for e in res.errors)
