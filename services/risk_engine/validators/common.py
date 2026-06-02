"""Shared validator helpers for mode-aware risk failures."""

from __future__ import annotations

from typing import Any, Optional

from risk_engine.limits.risk_limits import RiskValidationResult


def is_paper_signal(signal: Any) -> bool:
    """Return True when a signal is explicitly paper/shadow routed."""
    return bool(getattr(signal, "paper_trade", False))


def risk_data_unavailable_result(
    *,
    signal: Any,
    validator_name: str,
    reason: str,
    details: Optional[dict[str, Any]] = None,
) -> RiskValidationResult:
    """Reject live signals when core risk data is unavailable; warn in paper."""
    if is_paper_signal(signal):
        return RiskValidationResult(
            approved=True,
            validator_name=validator_name,
            reason=f"PAPER_WARN_RISK_DATA_UNAVAILABLE: {reason}",
            details=details or {},
        )
    return RiskValidationResult(
        approved=False,
        validator_name=validator_name,
        reason=f"LIVE_RISK_DATA_UNAVAILABLE: {reason}",
        details=details or {},
    )
