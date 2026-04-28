# QuantEmbrace - Reusable Patterns

> Proven patterns used across the platform. When implementing new functionality,
> check here first for established approaches. New patterns are added after
> they have been successfully used in at least one service.

---

## 1. Broker Adapter Pattern

### Intent

Decouple all broker-specific logic from the rest of the platform so that brokers can be added, removed, or swapped without modifying strategy, risk, or data processing code.

### Structure

```
BrokerAdapter (ABC)
    |
    |-- ZerodhaAdapter
    |       Uses: kiteconnect SDK
    |       Handles: Kite Connect auth, NSE order types, INR settlements
    |
    |-- AlpacaAdapter
    |       Uses: alpaca-py SDK
    |       Handles: API key auth, US order types, USD settlements
    |
    |-- MockAdapter (for testing)
            Uses: in-memory state
            Handles: deterministic fills, configurable latency
```

### Abstract Interface

```python
from abc import ABC, abstractmethod

class BrokerAdapter(ABC):
    @abstractmethod
    async def authenticate(self) -> None:
        """Establish authenticated session with the broker."""

    @abstractmethod
    async def place_order(self, order: Order) -> OrderResponse:
        """Submit an order. Returns broker-assigned order ID."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> CancelResponse:
        """Cancel a pending order."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Fetch current positions from the broker."""

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderStatus:
        """Query the current status of an order."""

    @abstractmethod
    async def subscribe_market_data(
        self, instruments: list[str], callback: Callable[[Tick], None]
    ) -> None:
        """Subscribe to real-time market data for instruments."""

    @abstractmethod
    async def close(self) -> None:
        """Clean up connections and resources."""
```

### Rules

- All broker-specific imports are confined to the adapter file.
- Adapters translate between broker models and internal models (Order, Position, Fill, Tick).
- No service outside the execution engine instantiates adapters directly. The execution engine owns adapter lifecycle.
- New adapters must pass the full `test_broker_adapter_interface.py` test suite.

---

## 2. Signal to Risk to Execution Pipeline

### Intent

Enforce that every trading signal passes through risk validation before reaching the execution engine. No signal can become an order without explicit risk approval.

### Flow

```
Strategy Engine          Risk Engine              Execution Engine
     |                       |                          |
     |--- Signal ----------->|                          |
     |                       |-- validate_signal()      |
     |                       |   check position limits  |
     |                       |   check daily loss       |
     |                       |   check order rate       |
     |                       |   check kill switch      |
     |                       |                          |
     |                       |--- Approved Signal ----->|
     |                       |                          |-- place_order()
     |                       |                          |-- track_order()
     |                       |                          |-- update_position()
     |                       |                          |
     |                       |<-- Position Update ------|
     |                       |   (for risk recalc)      |
```

### Implementation

```python
# In the orchestration layer or message handler:

async def process_signal(signal: Signal) -> None:
    # Step 1: Risk validation (mandatory, cannot be skipped)
    validation = await risk_engine.validate(signal)

    if not validation.approved:
        logger.warning(
            "signal_rejected",
            signal_id=signal.signal_id,
            reason=validation.reason,
        )
        await metrics.increment("signals.rejected", tags={"reason": validation.reason})
        return

    # Step 2: Execute only if approved
    order = signal_to_order(signal, validation.risk_metadata)
    response = await execution_engine.submit_order(order)

    logger.info(
        "order_submitted",
        signal_id=signal.signal_id,
        order_id=response.order_id,
    )
```

### Rules

- The strategy engine never imports or calls anything from the execution engine.
- The risk engine is the only gateway between signals and orders.
- If the risk engine is unreachable, the pipeline fails safe (no orders submitted).
- Every rejection is logged with the full signal context and rejection reason.

---

## 3. Idempotent Order Submission

### Intent

Prevent duplicate order submissions caused by retries, network issues, or race conditions. Every order is submitted exactly once, even if the submission call is retried multiple times.

### Mechanism

Each order is assigned a deterministic UUID derived from the signal that generated it. Before submitting to the broker, the execution engine attempts a conditional write to DynamoDB:

```python
import uuid
from datetime import datetime

def generate_order_id(signal: Signal) -> str:
    """Generate a deterministic order ID from a signal.

    Uses UUID5 with the signal_id as the name, ensuring the same signal
    always produces the same order_id.
    """
    namespace = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")  # fixed namespace
    return str(uuid.uuid5(namespace, str(signal.signal_id)))


async def submit_order_idempotent(order: Order) -> OrderResponse:
    """Submit an order with deduplication.

    Uses DynamoDB conditional write to ensure at-most-once submission.
    """
    try:
        # Attempt to claim this order_id
        await dynamodb.put_item(
            TableName="quantembrace-prod-orders",
            Item={
                "order_id": {"S": order.order_id},
                "status": {"S": "SUBMITTING"},
                "created_at": {"S": datetime.utcnow().isoformat()},
                "signal_id": {"S": str(order.signal_id)},
            },
            ConditionExpression="attribute_not_exists(order_id)",
        )
    except dynamodb.exceptions.ConditionalCheckFailedException:
        # Order already exists -- return existing status
        existing = await get_order_status(order.order_id)
        logger.info("order_dedup_hit", order_id=order.order_id)
        return existing

    # First claim succeeded -- submit to broker
    response = await broker_adapter.place_order(order)

    # Update DynamoDB with broker response
    await dynamodb.update_item(
        TableName="quantembrace-prod-orders",
        Key={"order_id": {"S": order.order_id}},
        UpdateExpression="SET #s = :s, broker_order_id = :b",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": {"S": "SUBMITTED"},
            ":b": {"S": response.broker_order_id},
        },
    )

    return response
```

### Rules

- Every order has a UUID generated from its source signal.
- DynamoDB conditional writes are the deduplication mechanism.
- If the DynamoDB write fails with ConditionalCheckFailed, the order already exists and should not be resubmitted.
- The broker is never called twice for the same order.

---

## 4. Circuit Breaker for Broker API Calls

### Intent

Prevent cascading failures when a broker API is degraded or down. Instead of repeatedly sending requests to a failing API (which adds load and delays recovery), the circuit breaker "opens" after a threshold of failures and immediately rejects calls for a cooling period.

### States

```
CLOSED (normal operation)
    |
    |-- failure count exceeds threshold
    v
OPEN (rejecting all calls)
    |
    |-- cooling period expires
    v
HALF_OPEN (allow one test call)
    |
    |-- test succeeds --> CLOSED
    |-- test fails --> OPEN
```

### Implementation

```python
import time
from enum import Enum

class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 60.0,
        name: str = "default",
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout_seconds
        self.name = name
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float | None = None

    async def call(self, func, *args, **kwargs):
        if self.state == CircuitState.OPEN:
            if self._should_attempt_recovery():
                self.state = CircuitState.HALF_OPEN
            else:
                raise CircuitBreakerOpenError(
                    f"Circuit breaker '{self.name}' is open. "
                    f"Retry after {self._seconds_until_recovery():.0f}s."
                )

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def _on_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN

    def _should_attempt_recovery(self) -> bool:
        if self.last_failure_time is None:
            return True
        return (time.monotonic() - self.last_failure_time) >= self.recovery_timeout

    def _seconds_until_recovery(self) -> float:
        if self.last_failure_time is None:
            return 0
        elapsed = time.monotonic() - self.last_failure_time
        return max(0, self.recovery_timeout - elapsed)
```

### Usage

```python
zerodha_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout_seconds=60.0,
    name="zerodha_api",
)

async def place_order_via_zerodha(order: Order) -> OrderResponse:
    return await zerodha_breaker.call(zerodha_adapter.place_order, order)
```

### Rules

- Each broker API endpoint has its own circuit breaker instance.
- Circuit breaker state is logged on every state transition.
- When a circuit breaker opens, the risk engine is notified (this may trigger a kill switch if the broker is the only one for a market).
- Circuit breaker metrics are published to CloudWatch.

---

## 5. Batched S3 Writes for Tick Data

### Intent

Market data ticks arrive at high frequency (hundreds to thousands per second across all instruments). Writing each tick individually to S3 would be prohibitively expensive and slow. Instead, ticks are buffered in memory and written to S3 in batches.

### Mechanism

```python
import asyncio
from datetime import datetime, timedelta

class TickBatchWriter:
    def __init__(
        self,
        s3_client,
        bucket: str,
        batch_size: int = 1000,
        max_wait_seconds: float = 5.0,
    ):
        self.s3_client = s3_client
        self.bucket = bucket
        self.batch_size = batch_size
        self.max_wait = timedelta(seconds=max_wait_seconds)
        self.buffer: list[Tick] = []
        self.last_flush: datetime = datetime.utcnow()
        self._lock = asyncio.Lock()

    async def add_tick(self, tick: Tick) -> None:
        async with self._lock:
            self.buffer.append(tick)

            should_flush = (
                len(self.buffer) >= self.batch_size
                or (datetime.utcnow() - self.last_flush) >= self.max_wait
            )

            if should_flush:
                await self._flush()

    async def _flush(self) -> None:
        if not self.buffer:
            return

        ticks_to_write = self.buffer.copy()
        self.buffer.clear()
        self.last_flush = datetime.utcnow()

        # Convert to Parquet and upload
        df = ticks_to_dataframe(ticks_to_write)
        parquet_bytes = df.to_parquet(index=False)

        key = self._generate_s3_key(ticks_to_write[0])
        await self.s3_client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=parquet_bytes,
        )

        logger.info(
            "tick_batch_flushed",
            count=len(ticks_to_write),
            s3_key=key,
        )

    async def close(self) -> None:
        """Flush remaining buffer on shutdown."""
        async with self._lock:
            await self._flush()

    def _generate_s3_key(self, sample_tick: Tick) -> str:
        ts = sample_tick.timestamp
        return (
            f"exchange={sample_tick.exchange}/"
            f"year={ts.year}/"
            f"month={ts.month:02d}/"
            f"day={ts.day:02d}/"
            f"{sample_tick.instrument}_{ts.strftime('%H%M%S')}_{uuid4().hex[:8]}.parquet"
        )
```

### Rules

- Ticks are flushed when either the batch size threshold or the time threshold is reached, whichever comes first.
- On graceful shutdown, the buffer is flushed to avoid data loss.
- Parquet format is used for efficient columnar storage and fast analytical reads.
- S3 keys are partitioned by exchange, year, month, and day for efficient querying with Athena.
- Each batch write is logged with the tick count and S3 key.

---

## 6. Structured Logging with Correlation IDs

### Intent

Trace a single trading signal from the moment it is generated through risk validation, order submission, and fill processing. Every log entry in the chain shares a correlation ID, enabling end-to-end debugging.

### Implementation

```python
import structlog
from uuid import uuid4
from contextvars import ContextVar

# Context variable for correlation ID
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")

def get_logger(service_name: str) -> structlog.BoundLogger:
    """Create a structured logger bound with service context."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    return structlog.get_logger(service=service_name)

# Usage in signal processing:
async def handle_signal(signal: Signal) -> None:
    # Bind correlation ID for the entire processing chain
    structlog.contextvars.bind_contextvars(
        correlation_id=str(signal.signal_id),
        strategy=signal.strategy_name,
        instrument=signal.instrument,
    )

    logger.info("signal_received", direction=signal.direction, strength=signal.strength)

    validation = await risk_engine.validate(signal)
    logger.info("risk_validation_complete", approved=validation.approved)

    if validation.approved:
        response = await execution_engine.submit(signal)
        logger.info("order_submitted", order_id=response.order_id)
```

### Log Output Example

```json
{
  "event": "signal_received",
  "service": "strategy_engine",
  "correlation_id": "a7b3c9d1-e5f6-4321-abcd-123456789abc",
  "strategy": "momentum_v1",
  "instrument": "RELIANCE",
  "direction": "BUY",
  "strength": 0.82,
  "level": "info",
  "timestamp": "2026-04-23T10:15:30.123456Z"
}
{
  "event": "risk_validation_complete",
  "service": "risk_engine",
  "correlation_id": "a7b3c9d1-e5f6-4321-abcd-123456789abc",
  "strategy": "momentum_v1",
  "instrument": "RELIANCE",
  "approved": true,
  "level": "info",
  "timestamp": "2026-04-23T10:15:30.127891Z"
}
{
  "event": "order_submitted",
  "service": "execution_engine",
  "correlation_id": "a7b3c9d1-e5f6-4321-abcd-123456789abc",
  "strategy": "momentum_v1",
  "instrument": "RELIANCE",
  "order_id": "ord-5f8a2b3c",
  "level": "info",
  "timestamp": "2026-04-23T10:15:30.134567Z"
}
```

### Rules

- Every service uses `structlog` with JSON output.
- The correlation ID is the signal's UUID, passed through all inter-service calls.
- Context variables (`contextvars`) carry the correlation ID without polluting function signatures.
- Log levels: `debug` for detailed tracing, `info` for business events, `warning` for recoverable issues, `error` for failures, `critical` for kill switch events.
- No `print()` statements anywhere in the codebase. All output goes through structlog.

---

## 7. Graceful Shutdown for WebSocket Connections

### Intent

When an ECS task receives a SIGTERM (during deployment, scaling, or manual stop), WebSocket connections to brokers must be closed cleanly. Abrupt disconnection can leave orphaned subscriptions on the broker side and cause missed data or duplicate data on reconnection.

### Implementation

```python
import asyncio
import signal
from typing import Any

class GracefulShutdownManager:
    def __init__(self):
        self._shutdown_event = asyncio.Event()
        self._cleanup_tasks: list[tuple[str, Any]] = []

    def register_cleanup(self, name: str, coro_func) -> None:
        """Register an async cleanup function to run on shutdown."""
        self._cleanup_tasks.append((name, coro_func))

    def setup_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register SIGTERM and SIGINT handlers."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_signal, sig)

    def _handle_signal(self, sig) -> None:
        logger.info("shutdown_signal_received", signal=sig.name)
        self._shutdown_event.set()

    async def wait_for_shutdown(self) -> None:
        """Block until a shutdown signal is received."""
        await self._shutdown_event.wait()

    async def run_cleanup(self, timeout_seconds: float = 25.0) -> None:
        """Run all registered cleanup tasks with a timeout.

        ECS sends SIGTERM and waits 30 seconds before SIGKILL.
        We use 25 seconds to leave a safety margin.
        """
        logger.info("graceful_shutdown_starting", task_count=len(self._cleanup_tasks))

        for name, coro_func in reversed(self._cleanup_tasks):
            try:
                await asyncio.wait_for(coro_func(), timeout=timeout_seconds)
                logger.info("cleanup_complete", task=name)
            except asyncio.TimeoutError:
                logger.error("cleanup_timeout", task=name)
            except Exception:
                logger.exception("cleanup_failed", task=name)

        logger.info("graceful_shutdown_complete")


# Usage in service main:
async def main():
    shutdown = GracefulShutdownManager()
    shutdown.setup_signal_handlers(asyncio.get_event_loop())

    # Initialize services
    ws_connector = ZerodhaWebSocketConnector()
    tick_writer = TickBatchWriter(s3_client, bucket="quantembrace-prod-tick-data")

    # Register cleanup in reverse-dependency order
    shutdown.register_cleanup("tick_writer", tick_writer.close)  # flush buffer first
    shutdown.register_cleanup("websocket", ws_connector.close)   # then close WS

    # Start services
    await ws_connector.connect()
    await ws_connector.subscribe(instruments)

    # Wait for shutdown signal
    await shutdown.wait_for_shutdown()

    # Run cleanup
    await shutdown.run_cleanup()
```

### Rules

- Every long-running service registers cleanup handlers with the shutdown manager.
- Cleanup timeout is 25 seconds (ECS default stop timeout is 30 seconds, leaving a 5-second margin before SIGKILL).
- Cleanup order matters: flush buffers before closing connections, close data consumers before data producers.
- The tick batch writer flushes its in-memory buffer to S3 during cleanup to prevent data loss.
- WebSocket connections send proper close frames to the broker.
- Shutdown events are logged at every step for post-mortem analysis.
