"""Execution Engine Service.

Receives ONLY risk-approved signals and translates them into broker orders.
Routes to the correct broker based on instrument market (Zerodha for NSE, Alpaca for US).

CRITICAL: This service NEVER receives signals directly from the strategy engine.
All signals must pass through the risk engine first.
"""

import asyncio
import json
import signal as _signal
from datetime import datetime, timezone
from typing import Optional

import structlog

from services.execution_engine.brokers.base_broker import BrokerClient
from services.execution_engine.brokers.zerodha_broker import ZerodhaBrokerClient
from services.execution_engine.brokers.alpaca_broker import AlpacaBroker
from services.execution_engine.orders.order import (
    OrderRequest,
    OrderResponse,
    OrderStatus,
    OrderSide,
    OrderType,
    Market,
    StoredOrder,
)
from services.execution_engine.orders.order_manager import OrderManager
from services.execution_engine.retry.retry_handler import RetryHandler
from services.shared.config.settings import AppSettings
from services.shared.logging.logger import get_logger

logger = get_logger(__name__)


class ExecutionService:
    """Main execution engine service.

    Responsibilities:
        - Consume risk-approved signals from SQS queue
        - Route orders to the correct broker (Zerodha for NSE, Alpaca for US)
        - Track order lifecycle in DynamoDB
        - Handle retries, partial fills, and circuit breaking

    This service MUST NOT:
        - Generate trading signals (that's strategy_engine)
        - Override risk decisions (that's risk_engine)
        - Receive signals from any source other than risk_engine
    """

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._running = False

        self._zerodha: Optional[ZerodhaBrokerClient] = None
        self._alpaca: Optional[AlpacaBroker] = None
        self._order_manager: Optional[OrderManager] = None
        # Serialise same-signal order handling inside a single worker process.
        # Cross-process dedup is still enforced by DynamoDB signal reservation.
        self._signal_locks: dict[str, asyncio.Lock] = {}
        self._retry_handler = RetryHandler(
            max_retries=settings.execution.max_retries,
            base_delay=settings.execution.retry_base_delay,
            max_delay=settings.execution.retry_max_delay,
        )

    async def start(self) -> None:
        """Start the execution service.

        Initializes broker connections, reconciles state with brokers
        and DynamoDB, then begins consuming approved signals.
        """
        logger.info("execution_service.starting")

        from services.shared.aws.clients import get_dynamodb_client  # noqa: PLC0415

        # Fail fast with clear messages if broker secrets are absent.
        # (Secrets are injected by ECS task definition / Secrets Manager.)
        if self._settings.zerodha is None:
            raise RuntimeError(
                "Zerodha config is not available. Ensure ZERODHA_API_KEY and "
                "ZERODHA_API_SECRET are present in the environment."
            )
        if self._settings.alpaca is None:
            raise RuntimeError(
                "Alpaca config is not available. Ensure ALPACA_API_KEY and "
                "ALPACA_API_SECRET are present in the environment."
            )

        # Both constructors accept `settings` — not individual credential kwargs.
        # The brokers load credentials internally (Secrets Manager → env var fallback).
        self._zerodha = ZerodhaBrokerClient(settings=self._settings)
        self._alpaca = AlpacaBroker(settings=self._settings)

        # Establish broker connections and authenticate before reconciliation.
        await self._zerodha.connect()
        await self._alpaca.connect()

        # Wire the real DynamoDB low-level client so persistence is not a no-op.
        self._order_manager = OrderManager(
            dynamo_client=get_dynamodb_client(),
            orders_table=self._settings.aws.dynamodb_table_orders,
        )

        # Reconcile state on startup — critical for restart safety
        await self._reconcile_state()

        self._running = True
        logger.info("execution_service.started")

        # Begin consuming risk-approved signals
        await self._consume_approved_signals()

    async def stop(self) -> None:
        """Gracefully stop the execution service.

        Stops consuming new signals, waits for in-flight orders to settle,
        then shuts down broker connections.
        """
        logger.info("execution_service.stopping")
        self._running = False

        # Wait for in-flight orders to reach terminal state
        if self._order_manager:
            await self._order_manager.wait_for_inflight_orders(timeout_seconds=30)

        # Cleanly close broker connections
        if self._zerodha:
            await self._zerodha.disconnect()
        if self._alpaca:
            await self._alpaca.disconnect()

        logger.info("execution_service.stopped")

    async def execute_approved_signal(
        self,
        signal_id: str,
        risk_decision_id: str,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType,
        market: Market,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> OrderResponse:
        """Execute a risk-approved signal by placing an order with the broker.

        Args:
            signal_id: Original signal identifier from strategy engine.
            risk_decision_id: Risk approval ID — proves this signal was validated.
            symbol: Trading symbol (e.g., 'RELIANCE', 'AAPL').
            side: BUY or SELL.
            quantity: Number of shares/units to trade.
            order_type: MARKET, LIMIT, STOP_LOSS, etc.
            market: NSE or US — determines which broker to route to.
            limit_price: Limit price for LIMIT orders.
            stop_price: Stop price for stop-loss orders.

        Returns:
            OrderResponse with order ID and initial status.

        Raises:
            ValueError: If risk_decision_id is missing (signal not risk-approved).
        """
        if not risk_decision_id:
            raise ValueError(
                "Cannot execute signal without risk_decision_id. "
                "All signals MUST pass through risk engine first."
            )

        signal_lock = self._signal_locks.setdefault(signal_id, asyncio.Lock())
        async with signal_lock:
            # ── Step 1: Check for an existing order for this signal ──────────────
            #
            # Three cases:
            #
            # A. PENDING — a previous attempt wrote the DynamoDB record but the
            #    broker call failed or the process crashed before it completed.
            #    The order is stranded: the dedup check would normally block retry,
            #    and reconciliation skips PENDING rows (no broker_order_id yet).
            #    Fix: reuse the existing order_id and retry broker placement
            #    directly, bypassing submit_order (already written).
            #
            # B. Any active or terminal state (PLACED / PARTIALLY_FILLED / FILLED /
            #    REJECTED / CANCELLED) — this is a genuine duplicate delivery from
            #    SQS.  Return the existing record without touching the broker.
            #
            # C. Not found — first-time submission.  Fall through to the atomic
            #    write + broker placement path below.

            existing = await self._order_manager.get_order_by_signal(signal_id)

            if existing is not None:
                if existing.status == OrderStatus.PENDING:
                    # Case A: retry the broker call for a stranded PENDING order.
                    # Rebuild OrderRequest reusing the stored order_id so that
                    # record_order() updates the correct DynamoDB item.
                    order_request = OrderRequest(
                        order_id=existing.order_id,
                        signal_id=signal_id,
                        risk_decision_id=risk_decision_id,
                        symbol=symbol,
                        side=side,
                        quantity=quantity,
                        order_type=order_type,
                        market=market,
                        limit_price=limit_price,
                        stop_price=stop_price,
                        created_at=datetime.now(timezone.utc),
                    )
                    logger.info(
                        "execution_service.retrying_pending_order",
                        signal_id=signal_id,
                        order_id=existing.order_id,
                    )
                    # Skip submit_order — DynamoDB record + signal reservation
                    # already exist from the first attempt.
                    broker = self._get_broker(market)
                    response = await self._retry_handler.execute_with_retry(
                        func=broker.place_order,
                        order=order_request,
                        operation_name=f"place_order_{symbol}",
                    )
                    await self._order_manager.record_order(response)
                    logger.info(
                        "execution_service.pending_order_placed",
                        order_id=response.order_id,
                        signal_id=signal_id,
                        market=market.value,
                    )
                    return response
                else:
                    # Case B: genuine SQS duplicate — already processed.
                    logger.warning(
                        "execution_service.duplicate_signal",
                        signal_id=signal_id,
                        existing_order_id=existing.order_id,
                        existing_status=existing.status.value,
                    )
                    return existing

            # ── Step 2: First-time submission ─────────────────────────────────
            # Build a fresh OrderRequest (new order_id generated by default_factory).
            order_request = OrderRequest(
                signal_id=signal_id,
                risk_decision_id=risk_decision_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                market=market,
                limit_price=limit_price,
                stop_price=stop_price,
                created_at=datetime.now(timezone.utc),
            )

            # Atomically write the order record + signal reservation.
            # submit_order uses transact_write_items so concurrent consumers that
            # each generate a different order_id for the same signal_id still race
            # on the SIGNAL#{signal_id} reservation — only one wins.
            submitted = await self._order_manager.submit_order(order_request)
            if not submitted:
                # Another consumer won the signal_id reservation race.
                existing = await self._order_manager.get_order_by_signal(signal_id)
                if existing:
                    logger.warning(
                        "execution_service.concurrent_duplicate",
                        signal_id=signal_id,
                        winning_order_id=existing.order_id,
                    )
                    return existing
                # Transaction cancelled AND no record found — should never happen,
                # but leave the message visible in SQS so it gets retried.
                raise RuntimeError(
                    f"submit_order transaction cancelled but no record found for "
                    f"signal_id={signal_id}. Leaving message for SQS retry."
                )

            # ── Step 3: Place order with broker ───────────────────────────────
            broker = self._get_broker(market)

            response = await self._retry_handler.execute_with_retry(
                func=broker.place_order,
                order=order_request,
                operation_name=f"place_order_{symbol}",
            )

            await self._order_manager.record_order(response)

            logger.info(
                "execution_service.order_placed",
                order_id=response.order_id,
                symbol=symbol,
                side=side.value,
                quantity=quantity,
                market=market.value,
                risk_decision_id=risk_decision_id,
            )

            return response

    def _get_broker(self, market: Market) -> BrokerClient:
        """Route to the correct broker based on market.

        Args:
            market: NSE for Indian equities, US for American equities.

        Returns:
            The appropriate broker client instance.
        """
        if market == Market.NSE:
            return self._zerodha
        elif market == Market.US:
            return self._alpaca
        else:
            raise ValueError(f"Unsupported market: {market}")

    async def _reconcile_state(self) -> None:
        """Reconcile local state with brokers on startup.

        Two separate passes over open orders:

        Pass 1 — PENDING orders (stranded before broker placement):
            These exist in DynamoDB but were never submitted to the broker
            (the service crashed or timed out between ``submit_order`` and
            ``broker.place_order``).  Re-route them through
            ``execute_approved_signal`` which detects the PENDING status and
            retries broker placement using the existing order_id.

        Pass 2 — PLACED / PARTIALLY_FILLED orders (placed but not confirmed):
            Query the broker for the current status and update DynamoDB if the
            broker has advanced the order to a terminal state while we were down.
        """
        logger.info("execution_service.reconciling_state")

        open_orders = await self._order_manager.get_open_orders()
        pending_count = 0
        placed_count = 0

        for order in open_orders:
            if order.status == OrderStatus.PENDING:
                # Pass 1: re-attempt broker placement for stranded PENDING orders.
                #
                # ``order`` is a ``StoredOrder`` so it carries all the original
                # order parameters written by ``submit_order``.  We pass them
                # directly — no hasattr guards, no fabricated fallbacks.
                # ``execute_approved_signal`` will find the existing PENDING
                # DynamoDB record via ``get_order_by_signal``, reuse its
                # order_id, and retry broker placement without writing a second
                # DynamoDB item.
                pending_count += 1
                try:
                    if not order.signal_id or not order.risk_decision_id:
                        # Guard: if these sentinel fields are empty the record
                        # is corrupt or pre-dates StoredOrder.  Mark it rejected
                        # so it doesn't block reconciliation on every restart.
                        logger.error(
                            "execution_service.pending_order_missing_ids",
                            order_id=order.order_id,
                            symbol=order.symbol,
                            note="signal_id or risk_decision_id is empty — "
                                 "marking REJECTED to prevent infinite retry",
                        )
                        await self._order_manager.update_order_status(
                            order_id=order.order_id,
                            new_status=OrderStatus.REJECTED,
                            broker_message="Corrupt PENDING record: missing signal_id/risk_decision_id",
                        )
                        continue

                    await self.execute_approved_signal(
                        signal_id=order.signal_id,
                        risk_decision_id=order.risk_decision_id,
                        symbol=order.symbol,
                        side=order.side,
                        quantity=order.quantity,
                        order_type=order.order_type,
                        market=order.market,
                        limit_price=order.limit_price,
                        stop_price=order.stop_price,
                    )
                except Exception as exc:
                    logger.error(
                        "execution_service.pending_retry_error",
                        order_id=order.order_id,
                        error=str(exc),
                    )

            elif order.broker_order_id:
                # Pass 2: check broker status for already-placed orders.
                placed_count += 1
                try:
                    broker = self._get_broker(order.market)
                    current_status = await broker.get_order_status(order.broker_order_id)

                    if current_status.new_status != order.status:
                        await self._order_manager.update_order_status(
                            order_id=order.order_id,
                            new_status=current_status.new_status,
                            filled_quantity=current_status.filled_quantity,
                            average_price=current_status.avg_fill_price,
                        )
                        logger.info(
                            "execution_service.reconciled_order",
                            order_id=order.order_id,
                            old_status=order.status.value,
                            new_status=current_status.new_status.value,
                        )
                except Exception as exc:
                    logger.error(
                        "execution_service.reconciliation_error",
                        order_id=order.order_id,
                        error=str(exc),
                    )

        logger.info(
            "execution_service.reconciliation_complete",
            total_open=len(open_orders),
            pending_retried=pending_count,
            placed_checked=placed_count,
        )

    async def _consume_approved_signals(self) -> None:
        """
        Long-poll SQS for risk-approved signals and execute each one.

        Runs continuously until stop() is called.

        Queue contract:
            Messages are published by the Risk Engine in SQS FIFO format.
            Each body is a JSON-serialised Signal dict with an extra
            ``risk_decision_id`` field added by the risk engine.

        Idempotency:
            execute_approved_signal() deduplicates by signal_id using a
            DynamoDB conditional check, so re-delivered messages are safe.
        """
        from shared.aws.clients import get_sqs_client

        logger.info("execution_service.consuming_signals")
        queue_url = self._settings.aws.sqs_orders_queue
        sqs = get_sqs_client()

        # Register OS signal handlers so ECS SIGTERM triggers a clean stop.
        loop = asyncio.get_running_loop()
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        while self._running:
            try:
                response = await asyncio.to_thread(
                    sqs.receive_message,
                    QueueUrl=queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=10,   # Long-poll — avoids busy-wait cost
                    AttributeNames=["All"],
                )
                for message in response.get("Messages", []):
                    try:
                        await self._handle_approved_signal_message(message)
                        # Delete only after successful processing
                        await asyncio.to_thread(
                            sqs.delete_message,
                            QueueUrl=queue_url,
                            ReceiptHandle=message["ReceiptHandle"],
                        )
                    except Exception:
                        logger.exception(
                            "execution_service.signal_handling_error",
                            message_id=message.get("MessageId"),
                        )
                        # Leave the message in the queue — SQS visibility
                        # timeout will redeliver it for retry.

            except Exception:
                logger.exception("execution_service.consume_error")
                await asyncio.sleep(5)  # Back off before retrying

    async def _handle_approved_signal_message(self, message: dict) -> None:
        """
        Parse a single SQS message and call execute_approved_signal().

        Expected message body (JSON produced by risk_engine):
            {
                "signal_id":        "uuid",
                "risk_decision_id": "uuid",
                "symbol":           "RELIANCE",
                "direction":        "BUY",       # SignalDirection value
                "quantity":         100,
                "market":           "NSE",        # or "US"
                "target_price":     null,         # optional limit price
                "stop_loss":        null,         # optional stop price
                ...                               # other signal fields ignored here
            }

        Args:
            message: Raw SQS message dict (with "Body" key).
        """
        body = json.loads(message["Body"])

        raw_direction = body.get("direction", "").upper()
        if raw_direction not in ("BUY", "SELL"):
            logger.warning(
                "execution_service.unsupported_direction",
                direction=raw_direction,
                signal_id=body.get("signal_id"),
            )
            return

        side = OrderSide(raw_direction)
        market = Market(body.get("market", "NSE").upper())

        await self.execute_approved_signal(
            signal_id=body["signal_id"],
            risk_decision_id=body["risk_decision_id"],
            symbol=body["symbol"],
            side=side,
            quantity=float(body["quantity"]),
            order_type=OrderType.MARKET,       # Default to MARKET; extend later
            market=market,
            limit_price=body.get("target_price"),
            stop_price=body.get("stop_loss"),
        )


async def main() -> None:
    """
    Entry point for the Execution Engine Service.

    Settings are loaded from environment variables. The SQS queue URL
    (``aws.sqs_orders_queue``) must be set so the engine knows where
    to consume risk-approved signals.
    """
    from services.shared.config.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    service = ExecutionService(settings=settings)
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
