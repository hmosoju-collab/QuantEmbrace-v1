# QuantEmbrace - Internal API Contracts

## Overview

This document defines the internal message contracts between QuantEmbrace services.
All inter-service communication uses in-process message passing (Python function calls
and async queues). There are no REST APIs or gRPC between services -- the system is
designed as a set of cooperating modules within a shared codebase, deployed as separate
ECS tasks that communicate via DynamoDB as the shared state store.

**Communication patterns:**
- **Real-time path**: Data Ingestion -> DynamoDB -> Strategy Engine -> Risk Engine -> Execution Engine
- **State sharing**: All services read/write DynamoDB tables as the source of truth
- **No direct service-to-service network calls** (simplicity, fewer failure modes)

---

## Type Definitions

All message types are defined as Python dataclasses with strict typing. These are the
canonical definitions used across the entire codebase.

### Core Types

```python
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import uuid4


class Market(str, Enum):
    NSE = "NSE"
    US = "US"


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "SL"
    STOP_LOSS_MARKET = "SL-M"


class OrderStatus(str, Enum):
    PENDING = "PENDING"        # Created, not yet sent to broker
    PLACED = "PLACED"          # Sent to broker, awaiting fill
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"          # Fully executed
    CANCELLED = "CANCELLED"    # Cancelled by us or broker
    REJECTED = "REJECTED"      # Rejected by broker
    FAILED = "FAILED"          # System error during placement


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class RiskRejectionReason(str, Enum):
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED"
    EXPOSURE_LIMIT_EXCEEDED = "EXPOSURE_LIMIT_EXCEEDED"
    NO_STOP_LOSS = "NO_STOP_LOSS"
    DAILY_DRAWDOWN_EXCEEDED = "DAILY_DRAWDOWN_EXCEEDED"
    WEEKLY_DRAWDOWN_EXCEEDED = "WEEKLY_DRAWDOWN_EXCEEDED"
    INSTRUMENT_LIMIT_EXCEEDED = "INSTRUMENT_LIMIT_EXCEEDED"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    INVALID_SIGNAL = "INVALID_SIGNAL"


class MarketRegime(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"
```

---

## Contract 1: Data Ingestion -> Strategy Engine

**Channel**: DynamoDB `latest-prices` table (shared state)

The data ingestion services write normalized ticks to DynamoDB. The strategy engine
polls this table to get the latest market state.

### MarketTick (Written by Data Ingestion)

```python
@dataclass(frozen=True)
class MarketTick:
    """
    Normalized market data tick. Written to DynamoDB by data ingestion services.
    Read by strategy engine via polling.
    
    DynamoDB Key: market_instrument = f"{market.value}#{instrument}"
    """
    market: Market
    instrument: str              # Symbol without market prefix: "RELIANCE", "AAPL"
    ltp: Decimal                 # Last traded price
    bid: Decimal                 # Best bid price
    ask: Decimal                 # Best ask price
    bid_qty: int                 # Best bid quantity
    ask_qty: int                 # Best ask quantity
    volume: int                  # Total volume traded today
    open: Decimal                # Day open price
    high: Decimal                # Day high price
    low: Decimal                 # Day low price
    close: Decimal               # Previous close price
    change_pct: Decimal          # Percentage change from previous close
    timestamp: datetime          # UTC timestamp of this tick
    exchange_timestamp: datetime # Exchange timestamp (if available)

    @property
    def market_instrument(self) -> str:
        """DynamoDB partition key."""
        return f"{self.market.value}#{self.instrument}"

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def mid_price(self) -> Decimal:
        return (self.bid + self.ask) / 2
```

### DynamoDB Schema for latest-prices

```json
{
    "TableName": "latest-prices",
    "KeySchema": [
        {"AttributeName": "market_instrument", "KeyType": "HASH"}
    ],
    "AttributeDefinitions": [
        {"AttributeName": "market_instrument", "AttributeType": "S"}
    ],
    "BillingMode": "PAY_PER_REQUEST",
    "TimeToLiveSpecification": {
        "AttributeName": "expires_at",
        "Enabled": true
    }
}
```

### Example DynamoDB Item

```json
{
    "market_instrument": "NSE#RELIANCE",
    "market": "NSE",
    "instrument": "RELIANCE",
    "ltp": 2465.50,
    "bid": 2465.00,
    "ask": 2466.00,
    "bid_qty": 500,
    "ask_qty": 300,
    "volume": 1234567,
    "open": 2450.00,
    "high": 2470.00,
    "low": 2445.00,
    "close": 2448.00,
    "change_pct": 0.71,
    "timestamp": "2026-04-23T05:00:00.123Z",
    "exchange_timestamp": "2026-04-23T04:59:59.980Z",
    "expires_at": 1745499600
}
```

---

## Contract 2: Strategy Engine -> Risk Engine

**Channel**: DynamoDB `signals` queue table (or in-process async queue if co-located)

When a strategy generates a trading signal, it writes it to the signals table.
The risk engine polls this table for new signals.

### Signal (Written by Strategy Engine)

```python
@dataclass
class Signal:
    """
    A trading signal generated by a strategy. Must be validated by the risk
    engine before it can become an order.
    """
    signal_id: str = field(default_factory=lambda: str(uuid4()))
    strategy_name: str = ""          # Which strategy generated this
    market: Market = Market.NSE
    instrument: str = ""             # "RELIANCE", "AAPL"
    direction: Direction = Direction.BUY
    quantity: int = 0
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[Decimal] = None   # Required if order_type is LIMIT
    stop_price: Optional[Decimal] = None    # Required if order_type is SL/SL-M
    stop_loss_price: Optional[Decimal] = None  # Protective stop-loss for position
    take_profit_price: Optional[Decimal] = None  # Optional take-profit level
    confidence: float = 0.0          # Strategy confidence score, 0.0-1.0
    urgency: str = "normal"          # "normal" or "immediate"
    metadata: dict = field(default_factory=dict)  # Strategy-specific context
    ml_enrichment: Optional["MLEnrichment"] = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    def validate(self) -> list[str]:
        """Return list of validation errors. Empty list means valid."""
        errors = []
        if not self.instrument:
            errors.append("instrument is required")
        if self.quantity <= 0:
            errors.append("quantity must be positive")
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            errors.append("limit_price required for LIMIT orders")
        if self.order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET) \
           and self.stop_price is None:
            errors.append("stop_price required for SL/SL-M orders")
        if not (0.0 <= self.confidence <= 1.0):
            errors.append("confidence must be between 0.0 and 1.0")
        return errors
```

### MLEnrichment (Optional, from AI/ML Layer)

```python
@dataclass(frozen=True)
class MLEnrichment:
    """
    AI/ML predictions attached to a signal for risk-aware position sizing.
    """
    predicted_volatility_1h: float    # Predicted 1-hour volatility (annualized)
    predicted_volatility_1d: float    # Predicted 1-day volatility
    regime: MarketRegime              # Current market regime classification
    regime_confidence: float          # 0.0-1.0
    suggested_position_scale: float   # 0.5-1.5 multiplier for position sizing
    model_version: str                # e.g., "vol_predictor_v1.0.0"
    inference_timestamp: datetime
```

### Signal JSON Schema (for DynamoDB / logging)

```json
{
    "signal_id": "abc-123-def-456",
    "strategy_name": "momentum_breakout",
    "market": "NSE",
    "instrument": "RELIANCE",
    "direction": "BUY",
    "quantity": 100,
    "order_type": "LIMIT",
    "limit_price": 2460.00,
    "stop_price": null,
    "stop_loss_price": 2411.80,
    "take_profit_price": 2510.00,
    "confidence": 0.78,
    "urgency": "normal",
    "metadata": {
        "breakout_level": 2455.00,
        "volume_confirmation": true,
        "lookback_period": 20
    },
    "ml_enrichment": {
        "predicted_volatility_1h": 0.15,
        "predicted_volatility_1d": 0.22,
        "regime": "trending",
        "regime_confidence": 0.85,
        "suggested_position_scale": 1.2,
        "model_version": "vol_predictor_v1.0.0",
        "inference_timestamp": "2026-04-23T05:00:00Z"
    },
    "created_at": "2026-04-23T05:00:01Z"
}
```

---

## Contract 3: Risk Engine -> Execution Engine

**Channel**: DynamoDB `approved-orders` table (or in-process async queue)

Signals that pass all risk checks are transformed into `ApprovedOrder` objects and
written to the approved-orders table for the execution engine to pick up.

### ApprovedOrder (Written by Risk Engine)

```python
@dataclass
class ApprovedOrder:
    """
    An order that has been validated and approved by the risk engine.
    This is the ONLY type that the execution engine accepts.
    The execution engine MUST NOT accept raw Signals.
    """
    order_id: str = field(default_factory=lambda: str(uuid4()))
    signal_id: str = ""              # Link back to originating signal
    strategy_name: str = ""
    market: Market = Market.NSE
    instrument: str = ""
    direction: Direction = Direction.BUY
    quantity: int = 0
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    stop_loss_price: Decimal = Decimal("0")  # REQUIRED -- risk engine enforces this
    product_type: str = "INTRADAY"   # "INTRADAY" or "DELIVERY"
    time_in_force: str = "DAY"       # "DAY", "IOC", "GTC"
    risk_approval: "RiskApproval" = None  # Audit trail of risk checks
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None  # Auto-cancel if not filled by this time


@dataclass(frozen=True)
class RiskApproval:
    """
    Audit trail attached to every approved order showing which checks passed.
    """
    approved_at: datetime
    checks_passed: list[str]         # ["kill_switch", "position_limit", "exposure", ...]
    pre_trade_exposure_pct: float    # Exposure % before this trade
    post_trade_exposure_pct: float   # Projected exposure % after this trade
    available_margin: Decimal        # Margin available at time of approval
    daily_pnl_at_approval: Decimal   # Current daily PnL when approved
    risk_engine_version: str         # For debugging: which version approved this
```

### RejectedSignal (Written by Risk Engine, for logging/alerting)

```python
@dataclass(frozen=True)
class RejectedSignal:
    """
    A signal that was rejected by the risk engine. Logged for audit and debugging.
    Strategies can optionally read rejections to adjust their behavior.
    """
    signal_id: str
    strategy_name: str
    market: Market
    instrument: str
    direction: Direction
    quantity: int
    rejection_reason: RiskRejectionReason
    rejection_details: str           # Human-readable explanation
    risk_state_snapshot: dict        # Snapshot of risk state at rejection time
    rejected_at: datetime = field(default_factory=datetime.utcnow)
```

### ApprovedOrder JSON Schema

```json
{
    "order_id": "ord-789-ghi-012",
    "signal_id": "abc-123-def-456",
    "strategy_name": "momentum_breakout",
    "market": "NSE",
    "instrument": "RELIANCE",
    "direction": "BUY",
    "quantity": 100,
    "order_type": "LIMIT",
    "limit_price": 2460.00,
    "stop_price": null,
    "stop_loss_price": 2411.80,
    "product_type": "INTRADAY",
    "time_in_force": "DAY",
    "risk_approval": {
        "approved_at": "2026-04-23T05:00:02Z",
        "checks_passed": [
            "kill_switch",
            "position_limit",
            "exposure",
            "stop_loss",
            "drawdown",
            "instrument_limit",
            "margin"
        ],
        "pre_trade_exposure_pct": 45.2,
        "post_trade_exposure_pct": 52.8,
        "available_margin": 450000.00,
        "daily_pnl_at_approval": -5200.00,
        "risk_engine_version": "1.3.0"
    },
    "created_at": "2026-04-23T05:00:02Z",
    "expires_at": "2026-04-23T09:30:00Z"
}
```

### RejectedSignal JSON Schema

```json
{
    "signal_id": "abc-123-def-456",
    "strategy_name": "momentum_breakout",
    "market": "NSE",
    "instrument": "RELIANCE",
    "direction": "BUY",
    "quantity": 100,
    "rejection_reason": "DAILY_DRAWDOWN_EXCEEDED",
    "rejection_details": "Daily PnL of -31500.00 INR exceeds limit of -30000.00 INR. Kill switch activated.",
    "risk_state_snapshot": {
        "daily_pnl": -31500.00,
        "max_daily_drawdown": -30000.00,
        "kill_switch_active": true,
        "open_positions": 8,
        "gross_exposure_pct": 85.3
    },
    "rejected_at": "2026-04-23T07:15:00Z"
}
```

---

## Contract 4: Execution Engine -> Risk Engine (Execution Results)

**Channel**: DynamoDB `orders` table (execution engine writes, risk engine reads)

After placing an order with the broker, the execution engine writes the result back.
The risk engine monitors this table to update positions and PnL.

### OrderResult (Written by Execution Engine)

```python
@dataclass
class OrderResult:
    """
    Result of an order placement attempt. Written to DynamoDB orders table.
    """
    order_id: str                    # Same as ApprovedOrder.order_id
    signal_id: str                   # Link to originating signal
    broker_order_id: Optional[str]   # Broker's order ID (None if failed before placement)
    market: Market
    instrument: str
    direction: Direction
    requested_quantity: int
    filled_quantity: int = 0
    fill_price: Optional[Decimal] = None    # Average fill price
    status: OrderStatus = OrderStatus.PENDING
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    is_retryable: bool = False
    retry_count: int = 0
    placed_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    updated_at: datetime = field(default_factory=datetime.utcnow)
    broker_raw_response: Optional[dict] = None  # Full broker response for debugging
    slippage: Optional[Decimal] = None  # Difference from expected price
    execution_latency_ms: Optional[int] = None  # Time from submission to broker response


@dataclass
class Fill:
    """
    Individual fill event (for partial fills).
    """
    fill_id: str
    order_id: str
    broker_order_id: str
    quantity: int
    price: Decimal
    timestamp: datetime
    exchange_timestamp: Optional[datetime] = None
```

### OrderResult JSON Schema

```json
{
    "order_id": "ord-789-ghi-012",
    "signal_id": "abc-123-def-456",
    "broker_order_id": "230423000123456",
    "market": "NSE",
    "instrument": "RELIANCE",
    "direction": "BUY",
    "requested_quantity": 100,
    "filled_quantity": 100,
    "fill_price": 2460.50,
    "status": "FILLED",
    "error_message": null,
    "error_code": null,
    "is_retryable": false,
    "retry_count": 0,
    "placed_at": "2026-04-23T05:00:03Z",
    "filled_at": "2026-04-23T05:00:03.450Z",
    "updated_at": "2026-04-23T05:00:03.500Z",
    "slippage": 0.50,
    "execution_latency_ms": 450
}
```

---

## Contract 5: Position Update (Execution Engine -> DynamoDB)

### Position (Read by Risk Engine, Written by Execution Engine)

```python
@dataclass
class Position:
    """
    Current position in an instrument. Maintained by the execution engine,
    read by the risk engine for exposure calculations.
    """
    market: Market
    instrument: str
    side: PositionSide
    quantity: int                     # Absolute quantity (always positive)
    avg_entry_price: Decimal
    current_price: Decimal            # Updated from latest-prices
    unrealized_pnl: Decimal
    realized_pnl: Decimal             # PnL from closed portions
    stop_loss_price: Decimal          # Currently active stop-loss
    take_profit_price: Optional[Decimal]
    strategy_name: str
    opened_at: datetime
    last_updated: datetime
    order_ids: list[str]              # All order IDs that built this position
    notional_value: Decimal           # quantity * current_price

    @property
    def market_instrument(self) -> str:
        return f"{self.market.value}#{self.instrument}"

    @property
    def is_profitable(self) -> bool:
        return self.unrealized_pnl > 0

    @property
    def pnl_pct(self) -> float:
        if self.avg_entry_price == 0:
            return 0.0
        return float(
            (self.current_price - self.avg_entry_price) / self.avg_entry_price * 100
        )
```

### Position JSON (DynamoDB)

```json
{
    "market_instrument": "NSE#RELIANCE",
    "market": "NSE",
    "instrument": "RELIANCE",
    "side": "LONG",
    "quantity": 100,
    "avg_entry_price": 2460.50,
    "current_price": 2475.00,
    "unrealized_pnl": 1450.00,
    "realized_pnl": 0.00,
    "stop_loss_price": 2411.80,
    "take_profit_price": 2510.00,
    "strategy_name": "momentum_breakout",
    "opened_at": "2026-04-23T05:00:03Z",
    "last_updated": "2026-04-23T05:30:00Z",
    "order_ids": ["ord-789-ghi-012"],
    "notional_value": 247500.00
}
```

---

## Contract 6: Risk Engine Configuration

### RiskConfig (Read by Risk Engine from DynamoDB)

```python
@dataclass
class RiskConfig:
    """
    Runtime-configurable risk parameters. Stored in DynamoDB risk-config table.
    Changes take effect on the next risk check cycle (no restart required).
    """
    # Position limits
    max_positions_nse: int = 10
    max_positions_us: int = 5
    max_per_instrument_pct: float = 5.0       # Max % of capital per instrument

    # Exposure limits
    max_gross_exposure_pct: float = 200.0     # Sum of |position_value| / capital
    max_net_exposure_pct: float = 100.0       # (long - short) / capital

    # Drawdown limits
    max_daily_drawdown_pct: float = 3.0       # Triggers kill switch
    max_weekly_drawdown_pct: float = 7.0      # Triggers kill switch
    drawdown_warning_pct: float = 80.0        # % of limit -> warning alert

    # Stop-loss defaults
    default_sl_intraday_pct: float = 2.0
    default_sl_positional_pct: float = 5.0

    # Margin
    margin_buffer_pct: float = 20.0           # Keep 20% margin as buffer

    # Order limits
    max_order_value_inr: Decimal = Decimal("500000")  # Max single order value
    max_order_value_usd: Decimal = Decimal("10000")
    max_orders_per_minute: int = 10            # Rate limit

    # Kill switch
    kill_switch_auto_reset: bool = False        # Never auto-reset by default
    kill_switch_reset_time: Optional[str] = None  # e.g., "09:00" IST next day
```

---

## Contract 7: Kill Switch

### KillSwitchState (DynamoDB risk-state table)

```python
@dataclass
class KillSwitchState:
    """
    Kill switch state. When active, ALL trading is halted.
    """
    active: bool = False
    activated_at: Optional[datetime] = None
    activated_by: str = ""           # "system:drawdown", "system:error_rate", "manual:api"
    reason: str = ""
    open_orders_cancelled: bool = False
    deactivated_at: Optional[datetime] = None
    deactivated_by: Optional[str] = None
```

### KillSwitchActivation Event (logged)

```json
{
    "event": "kill_switch_activated",
    "timestamp": "2026-04-23T07:15:00Z",
    "activated_by": "system:daily_drawdown_exceeded",
    "reason": "Daily PnL of -31500.00 INR exceeds limit of -30000.00 INR",
    "actions_taken": [
        "Rejected all pending signals",
        "Cancelled 3 open orders on NSE",
        "Cancelled 1 open order on US",
        "Sent alert to SNS topic"
    ],
    "risk_state_at_activation": {
        "daily_pnl_nse": -28000.00,
        "daily_pnl_us": -3500.00,
        "open_positions": 8,
        "gross_exposure_pct": 85.3
    }
}
```

---

## Error Handling Contracts

### Standard Error Response

All services use a common error format for logging and inter-service communication:

```python
@dataclass(frozen=True)
class ServiceError:
    """
    Standard error type used across all services.
    """
    error_code: str              # Machine-readable: "BROKER_TIMEOUT", "INVALID_SIGNAL"
    error_message: str           # Human-readable description
    service: str                 # Which service generated this error
    severity: str                # "INFO", "WARNING", "ERROR", "CRITICAL"
    timestamp: datetime
    context: dict                # Additional context for debugging
    is_retryable: bool = False
    suggested_action: str = ""   # "retry", "alert_human", "kill_switch"
```

### Error Codes

| Code                      | Service         | Severity | Retryable | Description                          |
|---------------------------|-----------------|----------|-----------|--------------------------------------|
| WS_DISCONNECTED           | Data Ingestion  | CRITICAL | Yes       | WebSocket connection lost            |
| WS_RECONNECT_EXHAUSTED    | Data Ingestion  | CRITICAL | No        | All reconnect attempts failed        |
| TICK_PARSE_ERROR          | Data Ingestion  | WARNING  | No        | Could not parse broker tick data     |
| DYNAMO_WRITE_FAILED       | Any             | ERROR    | Yes       | DynamoDB write failed                |
| DYNAMO_READ_FAILED        | Any             | ERROR    | Yes       | DynamoDB read failed                 |
| SIGNAL_VALIDATION_FAILED  | Strategy Engine | WARNING  | No        | Signal failed self-validation        |
| RISK_CHECK_FAILED         | Risk Engine     | INFO     | No        | Signal rejected by risk check        |
| KILL_SWITCH_ACTIVATED     | Risk Engine     | CRITICAL | No        | Kill switch turned on                |
| BROKER_TIMEOUT            | Execution       | ERROR    | Yes       | Broker API call timed out            |
| BROKER_RATE_LIMITED       | Execution       | WARNING  | Yes       | Hit broker rate limit                |
| BROKER_REJECTED           | Execution       | ERROR    | No        | Broker rejected the order            |
| BROKER_INSUFFICIENT_MARGIN| Execution       | ERROR    | No        | Not enough margin at broker          |
| ORDER_IDEMPOTENCY_HIT     | Execution       | INFO     | No        | Duplicate order detected, skipped    |
| POSITION_MISMATCH         | Execution       | CRITICAL | No        | Broker vs DB position mismatch       |
| MODEL_LOAD_FAILED         | AI/ML           | WARNING  | Yes       | Could not load model from S3         |
| INFERENCE_FAILED          | AI/ML           | WARNING  | No        | Model inference threw exception      |

### Retry Policy (per error type)

```python
RETRY_POLICIES = {
    "WS_DISCONNECTED": RetryPolicy(
        max_attempts=5,
        backoff="exponential",
        base_delay_seconds=1,
        max_delay_seconds=30,
        on_exhausted="activate_kill_switch",
    ),
    "DYNAMO_WRITE_FAILED": RetryPolicy(
        max_attempts=3,
        backoff="exponential",
        base_delay_seconds=0.5,
        max_delay_seconds=5,
        on_exhausted="log_and_alert",
    ),
    "BROKER_TIMEOUT": RetryPolicy(
        max_attempts=3,
        backoff="exponential",
        base_delay_seconds=1,
        max_delay_seconds=10,
        on_exhausted="mark_order_failed_and_alert",
    ),
    "BROKER_RATE_LIMITED": RetryPolicy(
        max_attempts=5,
        backoff="fixed",
        base_delay_seconds=2,
        max_delay_seconds=2,
        on_exhausted="mark_order_failed_and_alert",
    ),
}
```

---

## Service Health Check Contract

All ECS services expose a `/health` endpoint on port 8080:

### Health Check Response

```python
@dataclass
class HealthCheck:
    status: str              # "healthy", "degraded", "unhealthy"
    service: str             # Service name
    version: str             # Deployed version
    uptime_seconds: float
    checks: dict[str, bool]  # Individual subsystem checks
    timestamp: datetime
```

### Example Response

```json
{
    "status": "healthy",
    "service": "risk-engine",
    "version": "1.3.0",
    "uptime_seconds": 14523.5,
    "checks": {
        "dynamodb_readable": true,
        "dynamodb_writable": true,
        "kill_switch_state_loaded": true,
        "config_loaded": true
    },
    "timestamp": "2026-04-23T09:00:00Z"
}
```

ECS uses this endpoint for task health checks. If a task returns `"unhealthy"` or fails
to respond within 5 seconds, ECS will restart the task.
