"""
Signal Age Validator — candle-signal regression test.

Root cause of Days 1-4 zero-trade sessions:
  strategy_engine stamps generated_at = candle_close_time (not poll time).
  A 1-minute candle closing at T is written to DynamoDB at T+5s, polled at
  T+5.5s, enriched, and consumed by risk_engine at T+7-12s.

  Old default RISK_MAX_SIGNAL_AGE_SECONDS = 5.0  → 100% rejection.
  Fix applied 2026-05-26: RISK_MAX_SIGNAL_AGE_SECONDS = 30 in docker-compose.

This test pins the regression: a 10s-old candle signal must be accepted
with the fixed 30s limit and must be rejected with the old 5s default.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


# ── import path setup ─────────────────────────────────────────────────────────

def _setup_paths() -> None:
    project_root = Path(__file__).resolve().parents[2]
    services_dir = project_root / "services"
    for p in (project_root, services_dir):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)

    # Stub structlog (not installed in unit test environment)
    if "structlog" not in sys.modules:
        sl = types.ModuleType("structlog")
        sl.get_logger = lambda *a, **kw: MagicMock()  # type: ignore[attr-defined]
        sys.modules["structlog"] = sl

    # Alias services.shared → shared for the modules under test
    import services.shared as _sh
    import services.shared.logging as _shl
    import services.shared.logging.logger as _shll
    import services.shared.config as _sc
    import services.shared.config.settings as _scs
    import services.shared.models as _sm
    import services.shared.models.signal as _sms
    import services.shared.utils as _su
    import services.risk_engine as _re
    import services.risk_engine.limits as _rel
    import services.risk_engine.limits.risk_limits as _rell

    for alias, mod in (
        ("shared",                     _sh),
        ("shared.logging",             _shl),
        ("shared.logging.logger",      _shll),
        ("shared.config",              _sc),
        ("shared.config.settings",     _scs),
        ("shared.models",              _sm),
        ("shared.models.signal",       _sms),
        ("shared.utils",               _su),
        ("risk_engine",                _re),
        ("risk_engine.limits",         _rel),
        ("risk_engine.limits.risk_limits", _rell),
    ):
        sys.modules.setdefault(alias, mod)

    # stub shared.utils.helpers.utc_now if not present
    helpers_name = "shared.utils.helpers"
    if helpers_name not in sys.modules:
        h = types.ModuleType(helpers_name)
        from datetime import datetime, timezone
        h.utc_now = lambda: datetime.now(timezone.utc)  # type: ignore[attr-defined]
        sys.modules[helpers_name] = h

    # stub shared.logging.logger.get_logger
    import shared.logging.logger as _ll
    if not hasattr(_ll, "get_logger"):
        _ll.get_logger = lambda *a, **kw: MagicMock()  # type: ignore[attr-defined]


_setup_paths()

import pytest

from services.shared.models.signal import Signal, Direction
from services.risk_engine.validators.signal_age_validator import SignalAgeValidator


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _settings(max_age: float) -> SimpleNamespace:
    return SimpleNamespace(risk=SimpleNamespace(max_signal_age_seconds=max_age))


def _candle_signal(age_seconds: float) -> Signal:
    """Create a signal whose generated_at is age_seconds in the past (candle timestamp)."""
    generated_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return Signal(
        symbol="RELIANCE",
        market="NSE",
        direction=Direction.BUY,
        quantity=10,
        confidence=0.72,
        strategy_name="ORBStrategy",
        generated_at=generated_at,
    )


# ── Core regression: 5s default vs 30s fix ───────────────────────────────────

def test_candle_signal_10s_old_rejected_by_5s_default() -> None:
    """Pre-fix behaviour: a 10s-old candle signal is stale under the 5s default."""
    validator = SignalAgeValidator(settings=_settings(max_age=5.0))
    signal    = _candle_signal(age_seconds=10.0)
    result    = asyncio.get_event_loop().run_until_complete(validator.validate(signal))
    assert not result.approved, (
        "Expected rejection: 10s-old signal should be stale under the 5s default. "
        f"Got: approved={result.approved}, reason={result.reason}"
    )
    assert "stale" in result.reason.lower() or "age" in result.reason.lower()


def test_candle_signal_10s_old_passes_with_30s_fix() -> None:
    """Post-fix behaviour: a 10s-old candle signal is accepted with the 30s limit."""
    validator = SignalAgeValidator(settings=_settings(max_age=30.0))
    signal    = _candle_signal(age_seconds=10.0)
    result    = asyncio.get_event_loop().run_until_complete(validator.validate(signal))
    assert result.approved, (
        "Expected approval: 10s-old signal should be within the 30s limit. "
        f"Got: approved={result.approved}, reason={result.reason}"
    )


# ── Boundary conditions ───────────────────────────────────────────────────────

def test_candle_signal_just_under_30s_passes() -> None:
    """29s-old signal passes when limit is 30s."""
    validator = SignalAgeValidator(settings=_settings(max_age=30.0))
    result    = asyncio.get_event_loop().run_until_complete(
        validator.validate(_candle_signal(age_seconds=29.0))
    )
    assert result.approved

def test_candle_signal_just_over_30s_rejected() -> None:
    """31s-old signal is rejected even with 30s limit (absolute ceiling)."""
    validator = SignalAgeValidator(settings=_settings(max_age=30.0))
    result    = asyncio.get_event_loop().run_until_complete(
        validator.validate(_candle_signal(age_seconds=31.0))
    )
    assert not result.approved


def test_absolute_ceiling_caps_configured_value() -> None:
    """Configuring a value above 30s is silently capped to 30s by the hard ceiling."""
    validator = SignalAgeValidator(settings=_settings(max_age=120.0))
    # The validator should use 30s (ceiling), not 120s.
    assert validator._max_age_seconds == 30.0, (
        f"Expected ceiling of 30s but got {validator._max_age_seconds}s. "
        "_ABSOLUTE_MAX_AGE_SECONDS guard is broken."
    )
    # A 31s-old signal must still be rejected.
    result = asyncio.get_event_loop().run_until_complete(
        validator.validate(_candle_signal(age_seconds=31.0))
    )
    assert not result.approved, "31s signal should be rejected even with ceiling=30s"


def test_fresh_signal_always_passes() -> None:
    """A signal generated right now passes regardless of max_age setting."""
    for max_age in (5.0, 30.0):
        validator = SignalAgeValidator(settings=_settings(max_age=max_age))
        result    = asyncio.get_event_loop().run_until_complete(
            validator.validate(_candle_signal(age_seconds=0.1))
        )
        assert result.approved, f"Fresh signal rejected with max_age={max_age}"


def test_typical_candle_pipeline_latency_range_passes() -> None:
    """
    Typical candle signal pipeline latency is 7-12s.
    All ages in that range must pass with the 30s fix.
    Pre-fix (5s default) all would have failed.
    """
    validator = SignalAgeValidator(settings=_settings(max_age=30.0))
    for age in (7.0, 9.0, 10.5, 12.0):
        result = asyncio.get_event_loop().run_until_complete(
            validator.validate(_candle_signal(age_seconds=age))
        )
        assert result.approved, (
            f"Signal aged {age}s should pass with 30s limit (typical pipeline latency). "
            f"Got: approved={result.approved}"
        )


def test_validator_name_in_result() -> None:
    """Validator name must be present in result so session report can identify rejections."""
    validator = SignalAgeValidator(settings=_settings(max_age=30.0))
    result    = asyncio.get_event_loop().run_until_complete(
        validator.validate(_candle_signal(age_seconds=1.0))
    )
    assert result.validator_name == "signal_age_validator"
