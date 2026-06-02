"""
Phase 2 Live-Readiness Verification — Fail-Closed Lock-In Tests.

These tests lock in the mode-aware fail-closed behaviour that the Phase 2
open-question audit verified by reading production code. They exercise REAL
production modules (no reimplemented copies):

  Q2 / HIGH-003 — risk_data_unavailable_result (services/risk_engine/validators/common.py)
      When core risk data is unavailable:
        * LIVE  signal (paper_trade=False) -> REJECTED  (fail closed)
        * PAPER signal (paper_trade=True)  -> APPROVED with explicit warning

  Q2 / HIGH-003 — SectorConcentrationValidator with instrument_registry=None
      (services/risk_engine/validators/sector_validator.py)
        * registry None -> sector resolves to "UNKNOWN"
        * UNKNOWN sector + LIVE  -> REJECTED (cannot bound sector exposure)
        * UNKNOWN sector + PAPER -> APPROVED with warning

Rationale: the Phase 8 suite (test_phase8_hardening.py) covers the
ReconciliationValidator (Q1) but has NO coverage for the registry-load /
risk-data-unavailable fail-closed path. This file fills that gap so the
fail-closed behaviour cannot silently regress to fail-open.

Standalone:
    python tests/unit/test_phase2_live_readiness.py
"""

from __future__ import annotations

import importlib.util as _ilu
import logging
import os
import sys
import types
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


# ── Path helpers ──────────────────────────────────────────────────────────────

_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")


def _load_module(rel: str, name: str):
    path = os.path.join(_SERVICES_DIR, rel)
    spec = _ilu.spec_from_file_location(name, path)
    mod  = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Logger:
    def __init__(self, name=""):
        self._log = logging.getLogger(name)

    def info(self, msg, *a, **kw):      self._log.info(msg)
    def warning(self, msg, *a, **kw):   self._log.warning(msg)
    def debug(self, msg, *a, **kw):     self._log.debug(msg)
    def error(self, msg, *a, **kw):     self._log.error(msg)
    def exception(self, msg, *a, **kw): self._log.exception(msg)
    def critical(self, msg, *a, **kw):  self._log.critical(msg)


# ── Shared stubs (mirror test_phase8_hardening.py) ─────────────────────────────

def _install_stubs():
    # shared.logging.logger
    logging_mod = types.ModuleType("shared.logging.logger")
    logging_mod.get_logger = lambda *a, **kw: _Logger()
    logging_mod.set_correlation_id = lambda *a, **kw: None
    sys.modules.setdefault("shared",                types.ModuleType("shared"))
    sys.modules.setdefault("shared.logging",        types.ModuleType("shared.logging"))
    sys.modules.setdefault("shared.logging.logger", logging_mod)

    # shared.models.risk_context — only the RiskContext symbol is imported.
    # The validator never type-checks it, so a placeholder class is enough; the
    # tests pass a SimpleNamespace as the context.
    sys.modules.setdefault("shared.models", types.ModuleType("shared.models"))
    if "shared.models.risk_context" not in sys.modules:
        rc_mod = types.ModuleType("shared.models.risk_context")
        rc_mod.RiskContext = object
        sys.modules["shared.models.risk_context"] = rc_mod

    # risk_engine.limits.risk_limits — minimal RiskValidationResult + RiskLimits.
    if "risk_engine.limits.risk_limits" not in sys.modules:
        limits_mod = types.ModuleType("risk_engine.limits.risk_limits")

        @dataclass
        class _RiskValidationResult:
            approved:       bool
            validator_name: str = ""
            reason:         str = ""
            details:        dict = field(default_factory=dict)

        class _RiskLimits:
            """Minimal stand-in; only get_limit is exercised here."""
            def __init__(self, **limits): self._limits = limits
            def get_limit(self, name): return self._limits.get(name, 0.0)
            def get_portfolio_value(self): return self._limits.get("portfolio_value", 0.0)

        limits_mod.RiskValidationResult = _RiskValidationResult
        limits_mod.RiskLimits           = _RiskLimits
        sys.modules.setdefault("risk_engine",        types.ModuleType("risk_engine"))
        sys.modules.setdefault("risk_engine.limits", types.ModuleType("risk_engine.limits"))
        sys.modules["risk_engine.limits.risk_limits"] = limits_mod

    # risk_engine.validators package + the REAL common.py module.
    sys.modules.setdefault("risk_engine.validators", types.ModuleType("risk_engine.validators"))
    _load_module("risk_engine/validators/common.py", "risk_engine.validators.common")


_install_stubs()


def _signal(paper_trade: bool, symbol: str = "RELIANCE") -> Any:
    """A minimal signal object: only paper_trade + symbol are read."""
    return SimpleNamespace(signal_id="sig-001", symbol=symbol, paper_trade=paper_trade, metadata={})


# ═══════════════════════════════════════════════════════════════════════════════
# Q2 — risk_data_unavailable_result (the shared fail-closed primitive)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRiskDataUnavailableFailClosed(unittest.TestCase):
    """services/risk_engine/validators/common.py — mode-aware fail-closed helper."""

    def _fn(self):
        from risk_engine.validators.common import risk_data_unavailable_result
        return risk_data_unavailable_result

    def _is_paper(self):
        from risk_engine.validators.common import is_paper_signal
        return is_paper_signal

    def test_live_signal_fails_closed(self):
        """LIVE signal (paper_trade=False) must be REJECTED when risk data is unavailable."""
        result = self._fn()(
            signal=_signal(paper_trade=False),
            validator_name="unit_validator",
            reason="ADV missing",
        )
        self.assertFalse(result.approved)
        self.assertIn("LIVE_RISK_DATA_UNAVAILABLE", result.reason)

    def test_paper_signal_warns_and_approves(self):
        """PAPER signal (paper_trade=True) approves with an explicit warning."""
        result = self._fn()(
            signal=_signal(paper_trade=True),
            validator_name="unit_validator",
            reason="ADV missing",
        )
        self.assertTrue(result.approved)
        self.assertIn("PAPER_WARN_RISK_DATA_UNAVAILABLE", result.reason)

    def test_details_passthrough(self):
        result = self._fn()(
            signal=_signal(paper_trade=False),
            validator_name="unit_validator",
            reason="spread missing",
            details={"symbol": "TCS"},
        )
        self.assertEqual(result.details.get("symbol"), "TCS")

    def test_is_paper_signal_defaults_false(self):
        """A signal with no paper_trade attribute is treated as LIVE (fail-closed default)."""
        self.assertFalse(self._is_paper()(SimpleNamespace()))
        self.assertTrue(self._is_paper()(_signal(paper_trade=True)))


# ═══════════════════════════════════════════════════════════════════════════════
# Q2 / HIGH-003 — SectorConcentrationValidator with registry=None
# ═══════════════════════════════════════════════════════════════════════════════

class TestSectorValidatorRegistryNone(unittest.TestCase):
    """services/risk_engine/validators/sector_validator.py — registry-load-failure path."""

    def _load(self):
        try:
            _load_module(
                "risk_engine/validators/sector_validator.py",
                "risk_engine.validators.sector_validator",
            )
            from risk_engine.validators.sector_validator import SectorConcentrationValidator
            return SectorConcentrationValidator
        except Exception as e:  # pragma: no cover - import guard
            self.skipTest(f"SectorConcentrationValidator not importable: {e}")

    def _make_validator(self, SCV):
        from risk_engine.limits.risk_limits import RiskLimits
        return SCV(limits=RiskLimits(max_sector_exposure_pct=30.0), instrument_registry=None)

    def test_get_sector_unknown_without_registry(self):
        SCV = self._load()
        v = self._make_validator(SCV)
        self.assertEqual(v._get_sector("ANYTHING"), "UNKNOWN")

    def test_live_signal_rejected_when_sector_unknown(self):
        """registry=None -> UNKNOWN sector -> LIVE order fails closed."""
        SCV = self._load()
        v = self._make_validator(SCV)
        ctx = SimpleNamespace(signal=_signal(paper_trade=False))
        result = v.validate(ctx)
        self.assertFalse(result.approved)
        self.assertIn("LIVE_RISK_DATA_UNAVAILABLE", result.reason)

    def test_paper_signal_approved_with_warning_when_sector_unknown(self):
        """registry=None -> UNKNOWN sector -> PAPER order approves with warning."""
        SCV = self._load()
        v = self._make_validator(SCV)
        ctx = SimpleNamespace(signal=_signal(paper_trade=True))
        result = v.validate(ctx)
        self.assertTrue(result.approved)
        self.assertIn("PAPER_WARN_RISK_DATA_UNAVAILABLE", result.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
