"""
Tests for MarginValidator paper vs live mode behaviour when a margin snapshot
is unavailable (DynamoDB returns no item).

Covers:
    1. paper mode + missing margin snapshot → APPROVED, reason contains
       "PAPER_WARN_MARGIN_UNAVAILABLE" and log says "allowing paper signal"
    2. live mode + missing margin snapshot → REJECTED, reason contains
       "MARGIN_SNAPSHOT_UNAVAILABLE"
    3. paper_trade=True on signal overrides risk_profile when snapshot is missing
    4. paper_trade=False on signal rejects even when risk_profile=paper
"""

from __future__ import annotations

import asyncio
from typing import Any
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from risk_engine.validators.margin_validator import MarginValidator
from risk_engine.limits.risk_limits import RiskLimits
from shared.models.signal import Direction, Signal, SignalStatus


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_signal(
    paper_trade: bool = True,
    price: float = 2500.0,
    quantity: int = 10,
    symbol: str = "RELIANCE",
    market: str = "NSE",
) -> Signal:
    return Signal(
        signal_id="test-margin-001",
        symbol=symbol,
        market=market,
        direction=Direction.BUY,
        quantity=quantity,
        confidence=0.92,
        strategy_name="vwap_reversion",
        generated_at=datetime.now(timezone.utc),
        status=SignalStatus.PENDING,
        price_at_signal=price,
        stop_loss=price * 0.98,
        take_profit=price * 1.04,
        paper_trade=paper_trade,
    )


def _make_dynamo_returning_none() -> Any:
    """Return a fake DynamoDB client that always returns an empty item."""
    dynamo = MagicMock()
    dynamo.get_item.return_value = {"Item": None}
    return dynamo


def _make_limits() -> RiskLimits:
    return RiskLimits.for_profile("paper", portfolio_value=1_000_000)


# ── Test 1: paper mode + missing snapshot → APPROVED ─────────────────────────

@pytest.mark.asyncio
async def test_paper_mode_missing_snapshot_approved() -> None:
    """In paper mode, a missing margin snapshot must produce an approved result
    with a log message that does NOT say 'rejecting'."""
    dynamo = _make_dynamo_returning_none()
    validator = MarginValidator(
        limits=_make_limits(),
        dynamo_client=dynamo,
        risk_state_table="test-risk-state",
        risk_profile="paper",
    )
    signal = _make_signal(paper_trade=True)

    with patch.object(validator, "_fetch_from_dynamodb", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = None  # simulate missing snapshot

        with patch("risk_engine.validators.margin_validator.logger") as mock_logger:
            result = await validator.validate(signal)

    assert result.approved is True, "Paper mode missing margin snapshot must be approved"
    assert "PAPER_WARN_MARGIN_UNAVAILABLE" in result.reason, (
        "Reason must contain 'PAPER_WARN_MARGIN_UNAVAILABLE'"
    )

    # Verify the log message says "allowing paper signal" (not "rejecting")
    log_calls = [str(call) for call in mock_logger.warning.call_args_list]
    assert any("allowing paper signal" in c.lower() for c in log_calls), (
        "Logger must emit 'allowing paper signal' message in paper mode"
    )
    assert not any("rejecting" in c.lower() for c in log_calls), (
        "Logger must NOT say 'rejecting' in paper mode"
    )


# ── Test 2: live mode + missing snapshot → REJECTED ──────────────────────────

@pytest.mark.asyncio
async def test_live_mode_missing_snapshot_rejected() -> None:
    """In live mode, a missing margin snapshot must produce a rejected result
    with reason containing 'MARGIN_SNAPSHOT_UNAVAILABLE' and a log that says
    'rejecting live entry signal'."""
    dynamo = _make_dynamo_returning_none()
    validator = MarginValidator(
        limits=_make_limits(),
        dynamo_client=dynamo,
        risk_state_table="test-risk-state",
        risk_profile="tiny-live",
    )
    signal = _make_signal(paper_trade=False)

    with patch.object(validator, "_fetch_from_dynamodb", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = None  # simulate missing snapshot

        with patch("risk_engine.validators.margin_validator.logger") as mock_logger:
            result = await validator.validate(signal)

    assert result.approved is False, "Live mode missing margin snapshot must be rejected"
    assert "MARGIN_SNAPSHOT_UNAVAILABLE" in result.reason, (
        "Reason must contain 'MARGIN_SNAPSHOT_UNAVAILABLE'"
    )

    # Verify the log message says "rejecting live entry signal"
    log_calls = [str(call) for call in mock_logger.warning.call_args_list]
    assert any("rejecting live entry signal" in c.lower() for c in log_calls), (
        "Logger must emit 'rejecting live entry signal' message in live mode"
    )


# ── Test 3: paper_trade=True on signal overrides risk_profile ─────────────────

@pytest.mark.asyncio
async def test_signal_paper_trade_true_overrides_live_risk_profile() -> None:
    """When signal.paper_trade=True, the validator must fail-open even if
    risk_profile is 'tiny-live'."""
    dynamo = _make_dynamo_returning_none()
    validator = MarginValidator(
        limits=_make_limits(),
        dynamo_client=dynamo,
        risk_state_table="test-risk-state",
        risk_profile="tiny-live",  # live risk profile
    )
    # But the signal says paper_trade=True
    signal = _make_signal(paper_trade=True)

    with patch.object(validator, "_fetch_from_dynamodb", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = None

        result = await validator.validate(signal)

    assert result.approved is True, (
        "signal.paper_trade=True must override risk_profile and approve in paper mode"
    )
    assert "PAPER_WARN_MARGIN_UNAVAILABLE" in result.reason


# ── Test 4: paper_trade=False on signal rejects even when risk_profile=paper ──

@pytest.mark.asyncio
async def test_signal_paper_trade_false_rejects_even_with_paper_profile() -> None:
    """When signal.paper_trade=False, the validator must fail-closed regardless
    of what risk_profile is configured."""
    dynamo = _make_dynamo_returning_none()
    validator = MarginValidator(
        limits=_make_limits(),
        dynamo_client=dynamo,
        risk_state_table="test-risk-state",
        risk_profile="paper",
    )
    # Signal explicitly says live
    signal = _make_signal(paper_trade=False)

    with patch.object(validator, "_fetch_from_dynamodb", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = None

        result = await validator.validate(signal)

    assert result.approved is False, (
        "signal.paper_trade=False must cause rejection in live mode "
        "even when risk_profile=paper"
    )
    assert "MARGIN_SNAPSHOT_UNAVAILABLE" in result.reason
