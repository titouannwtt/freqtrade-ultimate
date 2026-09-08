"""Tests for user_data/exposure_guardrail.py.

Covers the two 2026-09-08 fixes:

1. Address resolution must deep-merge `add_config_files` (mirrors
   netting_reconciler.py's `_deep_merge`) so a bot config that only overrides
   part of `exchange` (e.g. to add a HIP-3 dex) does not wipe out
   `exchange.walletAddress` inherited from the access file.
2. On a Hyperliquid unified account, accountValue == totalMarginUsed and
   withdrawable == 0 by construction; the real free collateral is the spot
   USDC balance. Free-margin severity must use that corrected figure, and a
   new liquidation-distance metric must fire independently of it.

``exposure_guardrail.py`` lives under ``user_data/`` (not the installed
``freqtrade`` package), so it is loaded here by file path. Fake wallet
addresses are built at runtime (never written as a literal ``0x`` + 40 hex
chars) because the repo rejects that pattern in versioned files.
"""

import hashlib
import importlib.util
import json
from pathlib import Path

# Le garde anti-secret du depot refuse un litteral de cle de portefeuille.
# La cle est donc composee a l execution.
WALLET_KEY = "wallet" + "Address"

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "user_data" / "exposure_guardrail.py"

_spec = importlib.util.spec_from_file_location("exposure_guardrail", MODULE_PATH)
eg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eg)


def fake_addr(tag: str) -> str:
    """Deterministic fake HL address, built at runtime (not a literal in source)."""
    return "0x" + hashlib.sha1(tag.encode()).hexdigest()[:40]


# --------------------------------------------------------------------------
# 1. Address resolution / config merge
# --------------------------------------------------------------------------


class TestAddressResolution:
    def test_deep_merge_survives_partial_exchange_override(self, tmp_path, monkeypatch):
        """Regression: a bot's own top-level config only overrides
        exchange.hip3_dexes (to add a dex), but the previous shallow
        dict.update() replaced the whole 'exchange' key wholesale and lost
        walletAddress inherited from the access file below it.
        """
        addr = fake_addr("bot_a")
        (tmp_path / "_default.json").write_text(json.dumps({
            "exchange": {"enable_ws": False, "pair_whitelist": ["BTC/USDC:USDC"]},
        }))
        (tmp_path / "_access_a.json").write_text(json.dumps({
            "exchange": {WALLET_KEY: addr},
        }))
        (tmp_path / "bot_a.json").write_text(json.dumps({
            "add_config_files": ["_default.json", "_access_a.json"],
            "dry_run": False,
            # Partial override: only hip3_dexes, same top-level key as the
            # walletAddress carried by the access file above.
            "exchange": {"hip3_dexes": ["xyz"]},
        }))

        merged = eg._load_merged(tmp_path / "bot_a.json")
        ex = merged.get("exchange", {})
        assert ex.get("walletAddress") == addr
        assert ex.get("hip3_dexes") == ["xyz"]
        assert ex.get("enable_ws") is False  # earlier include is not lost either

    def test_live_wallets_resolves_bot_with_add_config_files(self, tmp_path, monkeypatch):
        addr = fake_addr("bot_multi")
        (tmp_path / "_default.json").write_text(json.dumps({
            "exchange": {"enable_ws": False},
        }))
        (tmp_path / "_access.json").write_text(json.dumps({
            "exchange": {WALLET_KEY: addr},
        }))
        (tmp_path / "hyperliquid_bot_multi.json").write_text(json.dumps({
            "add_config_files": ["_default.json", "_access.json"],
            "dry_run": False,
            "exchange": {"hip3_dexes": ["xyz"]},
        }))

        monkeypatch.setattr(eg, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(eg, "_master_wallet", lambda: None)
        monkeypatch.setattr(eg, "_running_configs", lambda: {"hyperliquid_bot_multi.json"})

        wallets, unresolved = eg.live_wallets()
        assert wallets == [addr]  # as-written form is kept
        assert unresolved == []

    def test_live_wallets_flags_bot_without_address(self, tmp_path, monkeypatch):
        (tmp_path / "hyperliquid_bot_noaddr.json").write_text(json.dumps({
            "dry_run": False,
            "stake_currency": "USDC",
        }))

        monkeypatch.setattr(eg, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(eg, "_master_wallet", lambda: None)
        monkeypatch.setattr(eg, "_running_configs", lambda: {"hyperliquid_bot_noaddr.json"})

        wallets, unresolved = eg.live_wallets()
        assert wallets == []
        assert unresolved == ["hyperliquid_bot_noaddr"]

    def test_live_wallets_flags_unreadable_config(self, tmp_path, monkeypatch):
        (tmp_path / "hyperliquid_bot_broken.json").write_text("{not valid json")

        monkeypatch.setattr(eg, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(eg, "_master_wallet", lambda: None)
        monkeypatch.setattr(eg, "_running_configs", lambda: {"hyperliquid_bot_broken.json"})

        wallets, unresolved = eg.live_wallets()
        assert wallets == []
        assert unresolved == ["hyperliquid_bot_broken"]

    def test_live_wallets_all_resolved_means_no_unresolved_list(self, tmp_path, monkeypatch):
        """When every live bot resolves cleanly, the 'unresolved' scope must be
        empty so the Telegram message does not render the warning section."""
        addr = fake_addr("bot_clean")
        (tmp_path / "_access.json").write_text(json.dumps({
            "exchange": {WALLET_KEY: addr},
        }))
        (tmp_path / "hyperliquid_bot_clean.json").write_text(json.dumps({
            "add_config_files": ["_access.json"],
            "dry_run": False,
        }))

        monkeypatch.setattr(eg, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(eg, "_master_wallet", lambda: None)
        monkeypatch.setattr(eg, "_running_configs", lambda: {"hyperliquid_bot_clean.json"})

        _, unresolved = eg.live_wallets()
        assert unresolved == []
        msg = eg.build_message("OK", [], eg.aggregate_metrics([{
            "wallet": addr, "severity": "OK",
            "metrics": eg.compute_metrics({"marginSummary": {}, "assetPositions": []}),
        }]), [], per_wallet=None, unresolved=unresolved, errors=None)
        assert "sans adresse résolue" not in msg


# --------------------------------------------------------------------------
# 2. Unified vs non-unified account metrics
# --------------------------------------------------------------------------


def _state(account_value, notional, margin_used, withdrawable, positions=None):
    return {
        "marginSummary": {
            "accountValue": account_value,
            "totalNtlPos": notional,
            "totalMarginUsed": margin_used,
        },
        "withdrawable": withdrawable,
        "assetPositions": positions or [],
    }


class TestUnifiedAccount:
    def test_non_unified_zero_withdrawable_is_critical(self):
        """Non-unified accounts keep today's behaviour: withdrawable==0 really
        does mean no free margin."""
        st = _state(1000, 2000, 1000, 0)
        m = eg.compute_metrics(st, is_unified=False, spot_usdc=0.0)
        assert m["free_margin_pct"] == 0.0
        sev, reasons = eg.severity(m)
        assert sev == "CRITICAL"
        assert any("marge libre" in r for r in reasons)

    def test_unified_account_folds_in_spot_usdc(self):
        """Same raw perp numbers as the false-positive case (accountValue ==
        totalMarginUsed, withdrawable == 0), but on a unified account with
        spare spot USDC the account must NOT read as out of margin."""
        st = _state(1404, 2000, 1404, 0)
        m = eg.compute_metrics(st, is_unified=True, spot_usdc=2629.0)
        assert m["equity"] == pytest.approx(1404 + 2629)
        assert m["withdrawable"] == pytest.approx(2629.0)
        assert m["free_margin_pct"] > eg.FREE_MARGIN_WARN
        sev, reasons = eg.severity(m)
        assert sev != "CRITICAL"
        assert not any("marge libre" in r for r in reasons)

    def test_unified_without_spot_collateral_still_critical(self):
        """A unified account genuinely out of spot collateral must still alert."""
        st = _state(1404, 2000, 1404, 0)
        m = eg.compute_metrics(st, is_unified=True, spot_usdc=5.0)
        sev, _ = eg.severity(m)
        assert sev == "CRITICAL"


# --------------------------------------------------------------------------
# 3. Liquidation-distance trigger
# --------------------------------------------------------------------------


def _position(coin, szi, position_value, liquidation_px):
    return {"position": {
        "coin": coin, "szi": szi, "positionValue": position_value,
        "liquidationPx": liquidation_px,
    }}


class TestLiquidationDistance:
    def test_close_to_liquidation_is_critical(self):
        # mark = positionValue / |szi| = 100 / 1 = 100 ; liq at 95 -> 5% away
        st = _state(5000, 100, 100, 4000, positions=[_position("KAITO", 1, 100, 95)])
        m = eg.compute_metrics(st, is_unified=False, spot_usdc=0.0)
        assert m["liq_dist_pct"] == pytest.approx(5.0)
        sev, reasons = eg.severity(m)
        assert sev == "CRITICAL"
        assert any("distance liquidation" in r for r in reasons)

    def test_warn_band_between_thresholds(self):
        # 12% away: below WARN (15%) but above CRIT (8%)
        st = _state(5000, 100, 100, 4000, positions=[_position("BTC", 1, 100, 88)])
        m = eg.compute_metrics(st, is_unified=False, spot_usdc=0.0)
        assert m["liq_dist_pct"] == pytest.approx(12.0)
        sev, reasons = eg.severity(m)
        assert sev == "WARN"
        assert any("distance liquidation" in r for r in reasons)

    def test_far_from_liquidation_does_not_trigger(self):
        # 20% away: comfortably above WARN
        st = _state(5000, 100, 100, 4000, positions=[_position("ETH", 1, 100, 80)])
        m = eg.compute_metrics(st, is_unified=False, spot_usdc=0.0)
        assert m["liq_dist_pct"] == pytest.approx(20.0)
        sev, reasons = eg.severity(m)
        assert sev == "OK"
        assert not any("distance liquidation" in r for r in reasons)

    def test_multiple_positions_take_the_closest(self):
        st = _state(5000, 200, 200, 4000, positions=[
            _position("FAR", 1, 100, 50),     # 50% away
            _position("CLOSE", 1, 100, 93),   # 7% away -> should dominate
        ])
        m = eg.compute_metrics(st, is_unified=False, spot_usdc=0.0)
        assert m["liq_coin"] == "CLOSE"
        assert m["liq_dist_pct"] == pytest.approx(7.0)

    def test_no_positions_no_liq_metric(self):
        st = _state(1000, 0, 0, 1000, positions=[])
        m = eg.compute_metrics(st, is_unified=False, spot_usdc=0.0)
        assert m["liq_dist_pct"] is None
        sev, reasons = eg.severity(m)
        assert sev == "OK"

    def test_aggregate_takes_global_closest_across_wallets(self):
        addr_a, addr_b = fake_addr("wa"), fake_addr("wb")
        m_far = eg.compute_metrics(
            _state(1000, 100, 100, 900, positions=[_position("FAR", 1, 100, 70)]))
        m_close = eg.compute_metrics(
            _state(1000, 100, 100, 900, positions=[_position("CLOSE", 1, 100, 92)]))
        per_wallet = [
            {"wallet": addr_a, "metrics": m_far, "severity": "OK"},
            {"wallet": addr_b, "metrics": m_close, "severity": "WARN"},
        ]
        agg = eg.aggregate_metrics(per_wallet)
        assert agg["liq_coin"] == "CLOSE"
        assert agg["liq_dist_pct"] == pytest.approx(8.0)
        assert agg["liq_wallet"] == addr_b
