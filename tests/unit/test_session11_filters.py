"""
Tests for Session 11 strategy optimization filters.

IMPORTANT — Sessions 10 and 11 stale-image discovery (2026-06-05):
    Sessions 10 and 11 ran on a Docker image that was built BEFORE
    paper_quality_gate_validator.py and symbol_trade_count_validator.py were
    added to the risk_engine. The quality gates were NOT active during those
    sessions. Session 12 (image rebuilt 2026-06-05) is the first valid
    quality-gate performance test.
    Verify via: python scripts/validate_session12_runtime.py

Covers:
    1.  test_second_trade_on_same_symbol_is_rejected
    2.  test_exits_allowed_after_max_trades_reached
    3.  test_vwap_confidence_089_rejected
    4.  test_vwap_confidence_090_allowed
    5.  test_rr_below_120_rejected_for_vwap
    6.  test_rr_below_130_rejected_for_orb
    7.  test_rejected_rr_entry_creates_no_fill_or_position
    8.  test_confidence_and_rr_persisted_to_order_metadata
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from risk_engine.validators.symbol_trade_count_validator import SymbolTradeCountValidator
from risk_engine.validators.paper_quality_gate_validator import PaperQualityGateValidator
from shared.models.signal import Direction, Signal, SignalStatus
from shared.monitoring.strategy_performance import (
    StrategyPerformanceAnalyzer,
    TradeRecord,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _signal(
    symbol: str = "RELIANCE",
    strategy_name: str = "vwap_reversion",
    confidence: float = 0.92,
    direction: Direction = Direction.BUY,
    price: float = 2500.0,
    stop_loss: float = 2450.0,
    take_profit: float = 2600.0,
    signal_id: str = "sig-001",
    is_closeout: bool = False,
) -> Signal:
    s = Signal(
        signal_id=signal_id,
        symbol=symbol,
        market="NSE",
        direction=direction,
        quantity=10,
        confidence=confidence,
        strategy_name=strategy_name,
        generated_at=datetime.now(timezone.utc),
        status=SignalStatus.PENDING,
        price_at_signal=price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        paper_trade=True,
    )
    if is_closeout:
        s.metadata["is_closeout"] = True
    return s


class _FakeDynamo:
    """Minimal fake DynamoDB for SymbolTradeCountValidator unit tests."""

    def __init__(self, scan_items: list[dict] | None = None) -> None:
        self._items = scan_items or []

    def scan(self, **kwargs: Any) -> dict:
        return {"Items": self._items}


# ── Test 1: second trade on same symbol is rejected ───────────────────────────

@pytest.mark.asyncio
async def test_second_trade_on_same_symbol_is_rejected() -> None:
    """After 1 approved entry for RELIANCE, a second entry for RELIANCE is rejected."""
    dynamo = _FakeDynamo(scan_items=[])  # no prior fills in DB — fresh session
    validator = SymbolTradeCountValidator(
        dynamo_client=dynamo,
        orders_table="orders",
        max_trades_per_symbol=1,
    )

    sig1 = _signal(symbol="RELIANCE", signal_id="sig-001")
    result1 = await validator.validate(sig1)
    assert result1.approved is True, "First entry should be approved"

    sig2 = _signal(symbol="RELIANCE", signal_id="sig-002")
    result2 = await validator.validate(sig2)
    assert result2.approved is False, "Second entry for same symbol should be rejected"
    assert "MAX_TRADES_PER_SYMBOL_REACHED" in result2.reason
    assert result2.details["symbol"] == "RELIANCE"
    assert result2.details["current_count"] == 1


# ── Test 2: exits allowed after max trades reached ────────────────────────────

@pytest.mark.asyncio
async def test_exits_allowed_after_max_trades_reached() -> None:
    """Even after the symbol limit is reached, EXIT- signals and is_closeout signals pass."""
    dynamo = _FakeDynamo(scan_items=[])
    validator = SymbolTradeCountValidator(
        dynamo_client=dynamo,
        orders_table="orders",
        max_trades_per_symbol=1,
    )

    # Fill the entry slot for RELIANCE
    entry = _signal(symbol="RELIANCE", signal_id="sig-003")
    r_entry = await validator.validate(entry)
    assert r_entry.approved is True

    # Second entry rejected
    entry2 = _signal(symbol="RELIANCE", signal_id="sig-004")
    r_entry2 = await validator.validate(entry2)
    assert r_entry2.approved is False

    # EXIT- signal should pass through
    exit_sig = _signal(symbol="RELIANCE", signal_id="EXIT-sig-003")
    r_exit = await validator.validate(exit_sig)
    assert r_exit.approved is True, "EXIT- signal must not be blocked by trade count"

    # is_closeout signal should pass through
    closeout = _signal(symbol="RELIANCE", signal_id="sig-005", is_closeout=True)
    r_closeout = await validator.validate(closeout)
    assert r_closeout.approved is True, "is_closeout signal must not be blocked by trade count"


# ── Test 3: vwap confidence 0.89 rejected ────────────────────────────────────

def test_vwap_confidence_089_rejected() -> None:
    """A VWAP signal with confidence 0.89 is below the 0.90 threshold — must be rejected."""
    validator = PaperQualityGateValidator(
        min_confidence_by_strategy={"vwap_reversion": 0.90},
        min_rr_by_strategy={},
    )
    sig = _signal(
        strategy_name="vwap_reversion",
        confidence=0.89,
        stop_loss=2450.0,
        take_profit=2600.0,  # R:R = 100/50 = 2.0 — passes R:R check
    )
    result = validator.validate(sig)
    assert result.approved is False
    assert "CONFIDENCE_BELOW_THRESHOLD" in result.reason
    assert result.details["signal_confidence"] == pytest.approx(0.89, abs=1e-6)
    assert result.details["min_confidence"] == pytest.approx(0.90, abs=1e-6)


# ── Test 4: vwap confidence 0.90 allowed ─────────────────────────────────────

def test_vwap_confidence_090_allowed() -> None:
    """A VWAP signal with confidence exactly 0.90 meets the threshold — must be approved."""
    validator = PaperQualityGateValidator(
        min_confidence_by_strategy={"vwap_reversion": 0.90},
        min_rr_by_strategy={},
    )
    sig = _signal(
        strategy_name="vwap_reversion",
        confidence=0.90,
        stop_loss=2450.0,
        take_profit=2600.0,
    )
    result = validator.validate(sig)
    assert result.approved is True


# ── Test 5: R:R below 1.20 rejected for vwap ─────────────────────────────────

def test_rr_below_120_rejected_for_vwap() -> None:
    """VWAP BUY signal with R:R = 0.80 (< 1.20 threshold) must be rejected."""
    validator = PaperQualityGateValidator(
        min_confidence_by_strategy={},
        min_rr_by_strategy={"vwap_reversion": 1.20},
    )
    # entry=2500, stop=2460, tp=2532 → reward=32, risk=40 → R:R=0.80
    sig = _signal(
        strategy_name="vwap_reversion",
        confidence=0.95,
        price=2500.0,
        stop_loss=2460.0,
        take_profit=2532.0,
    )
    result = validator.validate(sig)
    assert result.approved is False
    assert "REWARD_RISK_TOO_LOW" in result.reason
    assert result.details["reward_risk_ratio"] == pytest.approx(0.80, abs=0.01)
    assert result.details["min_reward_risk_ratio"] == pytest.approx(1.20, abs=0.01)


# ── Test 6: R:R below 1.30 rejected for orb ──────────────────────────────────

def test_rr_below_130_rejected_for_orb() -> None:
    """ORB BUY signal with R:R = 1.20 (< 1.30 threshold) must be rejected."""
    validator = PaperQualityGateValidator(
        min_confidence_by_strategy={},
        min_rr_by_strategy={"orb_15m": 1.30},
    )
    # entry=1000, stop=960, tp=1048 → reward=48, risk=40 → R:R=1.20
    sig = _signal(
        symbol="TCS",
        strategy_name="orb_15m",
        confidence=0.95,
        price=1000.0,
        stop_loss=960.0,
        take_profit=1048.0,
    )
    result = validator.validate(sig)
    assert result.approved is False
    assert "REWARD_RISK_TOO_LOW" in result.reason
    assert result.details["reward_risk_ratio"] == pytest.approx(1.20, abs=0.01)


# ── Test 7: rejected low-R:R entry creates no order/fill/position ─────────────

def test_rejected_rr_entry_creates_no_fill_or_position() -> None:
    """When PaperQualityGateValidator rejects a signal, the result is not approved.

    This means the risk engine will short-circuit before writing any DynamoDB
    record or publishing any Kafka message. We verify the result at the validator
    boundary — no DynamoDB or Kafka is involved in this unit test.
    """
    validator = PaperQualityGateValidator(
        min_confidence_by_strategy={},
        min_rr_by_strategy={"vwap_reversion": 1.20},
    )
    # R:R = (2510-2500) / (2500-2490) = 10/10 = 1.0 < 1.20
    sig = _signal(
        strategy_name="vwap_reversion",
        confidence=0.95,
        price=2500.0,
        stop_loss=2490.0,
        take_profit=2510.0,
    )
    result = validator.validate(sig)

    assert result.approved is False, "Low R:R entry must be rejected"
    assert "REWARD_RISK_TOO_LOW" in result.reason

    # The validator produces no DynamoDB writes or Kafka publishes on its own.
    # Downstream code only runs if approved=True — confirmed by the assertion above.


# ── Test 8: confidence and R:R persisted to order metadata ───────────────────

def test_confidence_and_rr_persisted_to_order_metadata() -> None:
    """_build_paper_order_metadata correctly computes and embeds analytics fields."""
    from execution_engine.service import _build_paper_order_metadata

    class _FakeApproved:
        signal_id = "sig-001"
        confidence = 0.92
        stop_loss = 2450.0
        take_profit = 2600.0
        price_at_signal = 2500.0
        direction = "BUY"
        strategy_id = "vwap_reversion"

    class _FakeCfg:
        paper_slippage_bps = 5.0
        paper_spread_bps = 10.0
        paper_market_open_gap_bps = 0.0

    meta = _build_paper_order_metadata(_FakeApproved(), _FakeCfg())

    assert meta["confidence_score"] == pytest.approx(0.92, abs=1e-6)
    # BUY: reward = 2600-2500 = 100, risk = 2500-2450 = 50 → R:R = 2.0
    assert meta["reward_risk_ratio"] == pytest.approx(2.0, abs=0.01)
    assert meta["entry_price"] == pytest.approx(2500.0, abs=0.01)
    assert meta["stop_price"] == pytest.approx(2450.0, abs=0.01)
    assert meta["take_profit_price"] == pytest.approx(2600.0, abs=0.01)
    assert meta["expected_reward"] == pytest.approx(100.0, abs=0.01)
    assert meta["expected_risk"] == pytest.approx(50.0, abs=0.01)
    assert meta["strategy_id"] == "vwap_reversion"
    assert "time_bucket" in meta
    assert meta["paper_trade"] is True
