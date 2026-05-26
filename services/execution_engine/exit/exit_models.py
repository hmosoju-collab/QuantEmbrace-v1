"""
Exit data models — TradeExitPolicy, PositionExitState, ExitOrderRequest.

These models are NEVER passed through the strategy→risk→execution signal pipeline.
The execution engine creates and routes them directly via ExitOrderRouter.

Signed quantity invariant (inherited from positions table):
    quantity > 0  → LONG  → close with SELL
    quantity < 0  → SHORT → close with BUY
    quantity == 0 → FLAT  → nothing to close

Side-aware price rules:
    LONG:  stop_price < entry_price,  targets > entry_price
    SHORT: stop_price > entry_price,  targets < entry_price
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Enumerations ───────────────────────────────────────────────────────────────


class ExitTriggerType(str, Enum):
    """What caused the exit to fire."""

    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MIS_CLOSE = "MIS_CLOSE"
    KILL_SWITCH = "KILL_SWITCH"
    TRAILING = "TRAILING"  # Phase 3 — reserved, not yet implemented


class PositionExitState(str, Enum):
    """
    Managed lifecycle state of an open position.

    Transitions:
        OPEN → EXIT_POLICY_ATTACHED → INITIAL_SL_ACTIVE
             → BREAKEVEN_LOCKED (Phase 3)
             → PARTIAL_PROFIT_BOOKED (Phase 3)
             → TRAILING_ACTIVE (Phase 3)
             → STOP_LOSS_HIT | FINAL_TARGET_HIT | TIME_EXIT_PENDING
             → CLOSED
             → RECONCILED
    """

    OPEN = "OPEN"
    EXIT_POLICY_ATTACHED = "EXIT_POLICY_ATTACHED"
    INITIAL_SL_ACTIVE = "INITIAL_SL_ACTIVE"
    BREAKEVEN_LOCKED = "BREAKEVEN_LOCKED"          # Phase 3
    PARTIAL_PROFIT_BOOKED = "PARTIAL_PROFIT_BOOKED"  # Phase 3
    TRAILING_ACTIVE = "TRAILING_ACTIVE"             # Phase 3
    FINAL_TARGET_HIT = "FINAL_TARGET_HIT"
    STOP_LOSS_HIT = "STOP_LOSS_HIT"
    TIME_EXIT_PENDING = "TIME_EXIT_PENDING"
    CLOSED = "CLOSED"
    RECONCILED = "RECONCILED"


# ── Data models ────────────────────────────────────────────────────────────────


class TradeExitPolicy(BaseModel):
    """
    Exit parameters attached to a position immediately after entry fill.

    Created by the execution service from the risk-approved signal's stop_loss
    and take_profit fields. Persisted to the DynamoDB positions table via
    ``OrderManager.attach_exit_policy()``.

    All price levels are side-aware:
        LONG:  stop_price < entry_price, targets above entry_price
        SHORT: stop_price > entry_price, targets below entry_price
    """

    policy_id: str = Field(..., description="Unique policy ID: POLICY-{signal_id}")
    symbol: str
    market: str  # "NSE" | "US"
    direction: str  # "LONG" | "SHORT"
    entry_price: float
    signed_quantity: float  # positive=LONG, negative=SHORT

    stop_price: float
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    final_target: Optional[float] = None
    breakeven_trigger: Optional[float] = None   # Phase 3
    profit_lock_trigger: Optional[float] = None  # Phase 3

    attached_at: datetime = Field(default_factory=_utc_now)
    version: int = 1

    @property
    def close_side(self) -> str:
        """Exit side: LONG closes with SELL, SHORT closes with BUY."""
        return "SELL" if self.direction == "LONG" else "BUY"

    @property
    def close_qty(self) -> float:
        """Always positive — abs(signed_quantity)."""
        return abs(self.signed_quantity)

    @field_validator("direction")
    @classmethod
    def direction_must_be_long_or_short(cls, v: str) -> str:
        if v not in ("LONG", "SHORT"):
            raise ValueError(f"direction must be LONG or SHORT, got {v!r}")
        return v

    @field_validator("stop_price")
    @classmethod
    def stop_price_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"stop_price must be > 0, got {v}")
        return v

    @field_validator("entry_price")
    @classmethod
    def entry_price_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"entry_price must be > 0, got {v}")
        return v


class ExitOrderRequest(BaseModel):
    """
    An exit order that bypasses the strategy→risk→execution signal pipeline.

    Created by TradeExitEngine or MISSquareOffManager and routed directly
    through ExitOrderRouter → broker/paper/backtest.

    Invariants:
        - Never increments signals_today.
        - Never blocked by daily strategy caps.
        - close_side is always derived from position_direction.
        - close_qty is always positive (abs of signed quantity).
    """

    exit_id: str = Field(
        ...,
        description="Idempotency key: EXIT-{symbol}-{market}-{trigger}-{session_date_ist}",
    )
    symbol: str
    market: str  # "NSE" | "US"
    position_direction: str  # "LONG" | "SHORT" — the position being closed
    close_side: str  # "SELL" (close LONG) | "BUY" (close SHORT) — derived
    close_qty: float  # abs(quantity), always positive
    trigger_type: ExitTriggerType
    order_type: str = "MARKET"  # "MARKET" | "LIMIT"
    exit_price: Optional[float] = None  # LIMIT price; None for MARKET
    product_type: str = "MIS"
    is_kill_switch: bool = False
    avg_entry_price: float = 0.0  # carried for P&L calculation in router

    @field_validator("close_qty")
    @classmethod
    def close_qty_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"close_qty must be > 0, got {v}")
        return v

    @field_validator("close_side")
    @classmethod
    def close_side_must_be_buy_or_sell(cls, v: str) -> str:
        if v not in ("BUY", "SELL"):
            raise ValueError(f"close_side must be BUY or SELL, got {v!r}")
        return v

    @classmethod
    def make_exit_id(
        cls,
        symbol: str,
        market: str,
        trigger: ExitTriggerType,
        session_date_ist: str,
    ) -> str:
        """Canonical idempotency key format."""
        return f"EXIT-{symbol}-{market}-{trigger.value}-{session_date_ist}"

    @classmethod
    def from_position(
        cls,
        *,
        symbol: str,
        market: str,
        direction: str,
        signed_quantity: float,
        trigger_type: ExitTriggerType,
        session_date_ist: str,
        avg_entry_price: float = 0.0,
        exit_price: Optional[float] = None,
        is_kill_switch: bool = False,
    ) -> "ExitOrderRequest":
        """
        Build an ExitOrderRequest from a position record.
        close_side and close_qty are derived from direction/signed_quantity.
        """
        close_side = "SELL" if direction == "LONG" else "BUY"
        exit_id = cls.make_exit_id(symbol, market, trigger_type, session_date_ist)
        return cls(
            exit_id=exit_id,
            symbol=symbol,
            market=market,
            position_direction=direction,
            close_side=close_side,
            close_qty=abs(signed_quantity),
            trigger_type=trigger_type,
            order_type="LIMIT" if exit_price is not None else "MARKET",
            exit_price=exit_price,
            avg_entry_price=avg_entry_price,
            is_kill_switch=is_kill_switch,
        )
