"""
Order models — pydantic data models for the order lifecycle.

Defines OrderRequest, OrderResponse, and OrderStatus with strict
validation. These models are the canonical representation of orders
throughout the execution layer.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from shared.utils.helpers import generate_order_id, utc_now


class OrderStatus(str, Enum):
    """
    Order lifecycle status.

    Transitions:
        PENDING -> PLACED -> FILLED
        PENDING -> PLACED -> PARTIALLY_FILLED -> FILLED
        PENDING -> PLACED -> CANCELLED
        PENDING -> PLACED -> REJECTED
        PENDING -> REJECTED  (pre-flight rejection)
    """

    PENDING = "PENDING"
    PLACED = "PLACED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OrderSide(str, Enum):
    """Order side."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Order type supported across brokers."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    STOP_LOSS_MARKET = "SL-M"
    TRAILING_STOP = "TRAILING_STOP"


class Market(str, Enum):
    """
    Market / exchange group that determines broker routing.

    NSE: Indian equity markets (NSE / BSE via Zerodha Kite Connect).
    US:  US equity markets (NYSE / NASDAQ via Alpaca).
    """

    NSE = "NSE"
    US = "US"


class ProductType(str, Enum):
    """
    Product type for position management.

    CNC: Cash and carry (delivery, NSE).
    MIS: Margin intraday square-off (NSE).
    NRML: Normal F&O margin (NSE).
    DAY: Standard US equity order.
    """

    CNC = "CNC"
    MIS = "MIS"
    NRML = "NRML"
    DAY = "DAY"


class OrderRequest(BaseModel):
    """
    Request to place an order with a broker.

    Created by the Execution Engine from a risk-approved signal.
    The ``order_id`` serves as the idempotency key in DynamoDB.
    """

    order_id: str = Field(default_factory=generate_order_id)
    signal_id: str = Field(..., description="ID of the originating signal")
    risk_decision_id: str = Field(..., description="ID of the risk approval")
    symbol: str = Field(..., description="Trading symbol")
    market: Market = Field(..., description="Market identifier (NSE or US)")
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    quantity: float = Field(..., gt=0, description="Number of shares/units")
    limit_price: Optional[float] = Field(default=None, ge=0)
    stop_price: Optional[float] = Field(default=None, ge=0)
    product_type: ProductType = ProductType.DAY
    extended_hours: bool = Field(default=False, description="US extended hours trading")
    time_in_force: str = Field(default="DAY", description="Time in force (DAY, GTC, IOC)")
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("limit_price")
    @classmethod
    def limit_price_required_for_limit_orders(
        cls, v: Optional[float], info: Any
    ) -> Optional[float]:
        """Ensure limit_price is set for LIMIT and STOP_LIMIT orders."""
        order_type = info.data.get("order_type")
        if order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and v is None:
            raise ValueError("limit_price is required for LIMIT and STOP_LIMIT orders")
        return v


class OrderResponse(BaseModel):
    """
    Response from a broker after order placement.

    Contains the broker-assigned order ID and initial status.
    """

    order_id: str = Field(..., description="Internal QuantEmbrace order ID")
    broker_order_id: str = Field(..., description="Broker-assigned order ID")
    status: OrderStatus
    symbol: str
    market: Market
    filled_quantity: float = Field(default=0.0, ge=0)
    avg_fill_price: float = Field(default=0.0, ge=0)
    broker_message: str = Field(default="")
    timestamp: datetime = Field(default_factory=utc_now)


class StoredOrder(OrderResponse):
    """
    Full order record as stored in DynamoDB.

    Extends ``OrderResponse`` with the original order parameters written by
    ``submit_order``.  These extra fields are required during startup
    reconciliation so ``_reconcile_state`` can reconstruct the exact
    ``execute_approved_signal`` call needed to retry a stranded PENDING order
    without guessing or fabricating any parameters.

    All fields default to safe sentinel values so that reading a legacy record
    that was written before this model existed does not raise a ValidationError.
    """

    signal_id: str = Field(default="", description="Originating signal ID")
    risk_decision_id: str = Field(default="", description="Risk approval ID")
    side: OrderSide = Field(default=OrderSide.BUY, description="BUY or SELL")
    order_type: OrderType = Field(default=OrderType.MARKET, description="Order type")
    quantity: float = Field(default=0.0, ge=0, description="Original requested quantity")
    limit_price: Optional[float] = Field(default=None, description="Limit price (LIMIT/STOP_LIMIT orders)")
    stop_price: Optional[float] = Field(default=None, description="Stop price (STOP/STOP_LIMIT orders)")
    order_created_at: Optional[datetime] = Field(default=None, description="Order creation timestamp")


class OrderStatusUpdate(BaseModel):
    """
    An update to an existing order's status.

    Used for tracking partial fills, cancellations, and rejections.
    """

    order_id: str
    broker_order_id: str
    previous_status: OrderStatus
    new_status: OrderStatus
    filled_quantity: float = Field(default=0.0, ge=0)
    avg_fill_price: float = Field(default=0.0, ge=0)
    slippage: float = Field(default=0.0, description="Price slippage from signal price")
    broker_message: str = Field(default="")
    timestamp: datetime = Field(default_factory=utc_now)


class Position(BaseModel):
    """
    Normalized representation of a broker position.

    Returned by ``BrokerClient.get_positions()``. All broker-specific
    position objects are translated into this model so the risk engine and
    execution engine stay broker-agnostic.
    """

    symbol: str = Field(..., description="Trading symbol (e.g., 'AAPL', 'RELIANCE')")
    market: Market = Field(..., description="Market identifier (NSE or US)")
    side: str = Field(..., description="'long' or 'short'")
    quantity: float = Field(..., description="Number of shares/units held")
    average_entry_price: float = Field(..., ge=0, description="Average cost basis")
    current_price: float = Field(..., ge=0, description="Latest market price")
    market_value: float = Field(..., description="Current market value (qty × current_price)")
    unrealized_pnl: float = Field(default=0.0, description="Unrealized profit/loss")
    unrealized_pnl_pct: float = Field(default=0.0, description="Unrealized P&L as %")
    broker: str = Field(..., description="Broker name ('alpaca', 'zerodha')")
    exchange: str = Field(default="", description="Exchange code (e.g., 'NASDAQ', 'NSE')")
    asset_class: str = Field(default="equity", description="Asset class")
    timestamp: datetime = Field(default_factory=utc_now)
