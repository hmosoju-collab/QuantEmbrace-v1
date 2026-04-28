"""
Strategy Engine Service — runs trading strategies on incoming market data.

Consumes market data ticks from SQS, feeds them to registered strategies,
collects generated signals, and publishes them to the Risk Engine via SQS FIFO.

CRITICAL: This service NEVER places orders directly. All signals go through
the Risk Engine for validation before reaching the Execution Engine.

INSTRUMENT UNIVERSE:
    Which stocks are watched is controlled entirely by configs/instruments.yaml.
    Users edit that file to add/remove/pause instruments — no code changes needed.
    The algorithm auto-decides WHEN to trade based on momentum signals.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any, Optional

from shared.aws.clients import get_dynamodb_resource, get_sqs_client
from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger, set_correlation_id

from strategy_engine.signals.signal import Signal
from strategy_engine.strategies.base_strategy import BaseStrategy
from strategy_engine.strategies.momentum_strategy import MomentumStrategy
from strategy_engine.universe.instrument_loader import InstrumentLoader

logger = get_logger(__name__, service_name="strategy_engine")

# How long to wait for messages from SQS before looping (long-poll seconds).
_SQS_WAIT_SECONDS = 10
# Max messages to receive per SQS call (1–10).
_SQS_MAX_MESSAGES = 10


class StrategyEngineService:
    """
    Main strategy engine that orchestrates strategy execution.

    Lifecycle:
        1. start() — loads instrument universe, registers strategies,
                     restores state from DynamoDB, begins SQS polling.
        2. _processing_loop() — consumes ticks from SQS, fans out to
                                strategies, publishes signals to risk queue.
        3. stop() — saves strategy states to DynamoDB, signals shutdown.

    Restart-safety:
        Strategy indicator states (moving average windows, etc.) are saved
        to DynamoDB on shutdown and restored on startup so warm-up periods
        are not repeated after a container restart.
    """

    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        instruments_config_path: Optional[str] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._strategies: list[BaseStrategy] = []
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Instrument universe loader — reads configs/instruments.yaml
        config_path = (
            os.environ.get("INSTRUMENTS_CONFIG_PATH")
            or instruments_config_path
            or Path(__file__).parent.parent.parent.parent / "configs" / "instruments.yaml"
        )
        self._instrument_loader = InstrumentLoader(config_path=config_path)

    # ── Public API ────────────────────────────────────────────────────────────

    def register_strategy(self, strategy: BaseStrategy) -> None:
        """
        Register a strategy with the engine.

        Args:
            strategy: Strategy instance implementing BaseStrategy.
        """
        self._strategies.append(strategy)
        logger.info("Registered strategy: %s (symbols=%s)", strategy.name, strategy.symbols)

    async def start(self) -> None:
        """
        Start the strategy engine.

        Steps:
            1. Load instrument universe from instruments.yaml.
            2. Register momentum strategies for each active market.
            3. Restore strategy state from DynamoDB (warm-start).
            4. Wire SQS clients for tick consumption and signal publishing.
            5. Start processing loop.
        """
        set_correlation_id()

        # 1. Load instrument universe
        self._instrument_loader.load()
        logger.info("Instrument universe:\n%s", self._instrument_loader.summary())

        # 2. Register strategies
        if not self._strategies:
            self._register_strategies_from_config()

        logger.info(
            "Starting Strategy Engine with %d strategies", len(self._strategies)
        )

        # 3. Restore strategy state from DynamoDB
        for strategy in self._strategies:
            saved_state = await self._load_strategy_state(strategy.name)
            await strategy.initialize(saved_state)
            logger.info(
                "Initialized strategy: %s (state_restored=%s)",
                strategy.name,
                saved_state is not None,
            )

        # 4. Register OS signal handlers for ECS SIGTERM
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        self._running = True
        logger.info("Strategy Engine Service started — polling SQS for ticks")

        # 5. Main loop
        await self._processing_loop()

    async def stop(self) -> None:
        """
        Gracefully stop the strategy engine.

        Persists all strategy states to DynamoDB before exit so the next
        container start can warm-start without replaying history.
        """
        if not self._running:
            return

        logger.info("Stopping Strategy Engine Service")
        self._running = False

        for strategy in self._strategies:
            state = strategy.get_state()
            await self._save_strategy_state(strategy.name, state)

        self._shutdown_event.set()
        logger.info("Strategy Engine Service stopped")

    async def process_tick(
        self, symbol: str, price: float, volume: int, timestamp: Any
    ) -> list[Signal]:
        """
        Fan a single tick out to all strategies watching that symbol.

        Args:
            symbol: Trading symbol (e.g. "RELIANCE", "AAPL").
            price: Last traded price.
            volume: Tick volume.
            timestamp: Exchange timestamp (datetime or ISO string).

        Returns:
            List of signals emitted by strategies on this tick.
        """
        signals: list[Signal] = []

        for strategy in self._strategies:
            if symbol not in strategy.symbols:
                continue

            await strategy.on_tick(symbol, price, volume, timestamp)
            signal_out = await strategy.generate_signal()

            if signal_out is not None:
                signals.append(signal_out)
                await self._publish_signal(signal_out)

        return signals

    # ── SQS tick consumer ────────────────────────────────────────────────────

    async def _processing_loop(self) -> None:
        """
        Main loop: long-poll SQS for ticks, dispatch to strategies.

        Uses asyncio.to_thread so the synchronous boto3 receive_message
        call does not block the event loop during the long-poll wait.

        Queue layout:
            sqs_market_data_queue  ← ticks published by DataIngestionService
            sqs_signals_queue      → signals we forward to RiskEngine
        """
        queue_url = self._settings.aws.sqs_market_data_queue  # ticks from data_ingestion

        while self._running:
            # Check for shutdown before blocking on SQS
            if self._shutdown_event.is_set():
                break

            try:
                messages = await asyncio.to_thread(
                    self._receive_messages, queue_url
                )
                for message in messages:
                    await self._handle_tick_message(message)
                    # Delete after successful processing
                    await asyncio.to_thread(
                        self._delete_message,
                        queue_url,
                        message["ReceiptHandle"],
                    )
            except Exception:
                logger.exception(
                    "Error in strategy engine processing loop — backing off 5s"
                )
                await asyncio.sleep(5)

    def _receive_messages(self, queue_url: str) -> list[dict]:
        """
        Synchronous SQS long-poll — called via asyncio.to_thread.

        Args:
            queue_url: SQS queue URL to poll.

        Returns:
            List of SQS message dicts (may be empty).
        """
        sqs = get_sqs_client()
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=_SQS_MAX_MESSAGES,
            WaitTimeSeconds=_SQS_WAIT_SECONDS,  # long-poll — no busy-wait cost
            AttributeNames=["All"],
        )
        return response.get("Messages", [])

    def _delete_message(self, queue_url: str, receipt_handle: str) -> None:
        """
        Delete a processed SQS message — called via asyncio.to_thread.

        Args:
            queue_url: SQS queue URL.
            receipt_handle: Receipt handle from the received message.
        """
        sqs = get_sqs_client()
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)

    async def _handle_tick_message(self, message: dict) -> None:
        """
        Parse an SQS tick message and dispatch to process_tick.

        Expected message body (JSON):
            {
                "symbol": "RELIANCE",
                "market": "NSE",
                "last_price": 2453.50,
                "volume": 100000,
                "timestamp": "2026-04-24T03:46:04Z"
            }

        Args:
            message: Raw SQS message dict.
        """
        try:
            body = json.loads(message["Body"])
            await self.process_tick(
                symbol=body["symbol"],
                price=float(body["last_price"]),
                volume=int(body.get("volume", 0)),
                timestamp=body.get("timestamp"),
            )
        except (KeyError, ValueError, json.JSONDecodeError):
            logger.exception(
                "Malformed tick message — skipping: %s",
                message.get("Body", "")[:200],
            )

    # ── SQS signal publisher ─────────────────────────────────────────────────

    async def _publish_signal(self, signal_out: Signal) -> None:
        """
        Publish a generated signal to the Risk Engine's SQS FIFO queue.

        Uses signal_id as the deduplication ID — if the same signal is
        accidentally published twice, SQS deduplication prevents the risk
        engine from processing it twice.

        Args:
            signal_out: The trading signal to publish.
        """
        logger.info(
            "Publishing signal: strategy=%s direction=%s symbol=%s qty=%d confidence=%.2f",
            signal_out.strategy_name,
            signal_out.direction.value,
            signal_out.symbol,
            signal_out.quantity,
            signal_out.confidence,
        )

        queue_url = self._settings.aws.sqs_signals_queue

        try:
            await asyncio.to_thread(
                self._send_signal_message,
                queue_url,
                signal_out,
            )
        except Exception:
            logger.exception(
                "Failed to publish signal %s to SQS — signal lost",
                signal_out.signal_id,
            )

    def _send_signal_message(self, queue_url: str, signal_out: Signal) -> None:
        """
        Synchronous SQS send — called via asyncio.to_thread.

        Args:
            queue_url: SQS FIFO queue URL for the risk engine.
            signal_out: Signal to serialize and send.
        """
        sqs = get_sqs_client()
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(signal_out.to_dict(), default=str),
            MessageGroupId=signal_out.symbol,        # FIFO: order per symbol
            MessageDeduplicationId=signal_out.signal_id,  # Idempotency
        )

    # ── DynamoDB state persistence ───────────────────────────────────────────

    async def _save_strategy_state(self, strategy_name: str, state: dict) -> None:
        """
        Persist strategy indicator state to DynamoDB for restart recovery.

        Item key: strategy_name (partition key)
        Attributes: state (JSON string), updated_at

        Args:
            strategy_name: Unique strategy identifier.
            state: Dict of indicator state to persist (e.g., price history).
        """
        try:
            table_name = f"{self._settings.aws.dynamodb_table_prefix}-strategy-state"
            await asyncio.to_thread(
                self._dynamo_put_state,
                table_name,
                strategy_name,
                state,
            )
            logger.info("Saved state for strategy: %s", strategy_name)
        except Exception:
            logger.exception(
                "Failed to save state for strategy %s — restart will cold-start",
                strategy_name,
            )

    def _dynamo_put_state(
        self, table_name: str, strategy_name: str, state: dict
    ) -> None:
        """Synchronous DynamoDB put — called via asyncio.to_thread."""
        dynamodb = get_dynamodb_resource()
        table = dynamodb.Table(table_name)
        table.put_item(
            Item={
                "strategy_name": strategy_name,
                "state": json.dumps(state, default=str),
                "updated_at": __import__("datetime").datetime.utcnow().isoformat(),
            }
        )

    async def _load_strategy_state(self, strategy_name: str) -> Optional[dict]:
        """
        Load persisted strategy state from DynamoDB.

        Returns None (cold start) if no state exists, allowing the strategy
        to initialise from scratch.

        Args:
            strategy_name: Unique strategy identifier.

        Returns:
            State dict if found, None for cold start.
        """
        try:
            table_name = f"{self._settings.aws.dynamodb_table_prefix}-strategy-state"
            result = await asyncio.to_thread(
                self._dynamo_get_state, table_name, strategy_name
            )
            if result:
                return json.loads(result["state"])
            return None
        except Exception:
            logger.warning(
                "Could not load state for strategy %s — cold start",
                strategy_name,
            )
            return None

    def _dynamo_get_state(
        self, table_name: str, strategy_name: str
    ) -> Optional[dict]:
        """Synchronous DynamoDB get — called via asyncio.to_thread."""
        dynamodb = get_dynamodb_resource()
        table = dynamodb.Table(table_name)
        response = table.get_item(Key={"strategy_name": strategy_name})
        return response.get("Item")

    # ── Strategy registration ────────────────────────────────────────────────

    def _register_strategies_from_config(self) -> None:
        """
        Register strategies dynamically from configs/instruments.yaml.

        One MomentumStrategy per market covering all active symbols.
        Per-instrument overrides (short_window, long_window, etc.) are
        respected via each instrument's strategy_params.

        To change which stocks are traded:
            Edit configs/instruments.yaml — set active: true/false
            Restart the strategy engine — no code change required.
        """
        for market in ("NSE", "US"):
            instruments = self._instrument_loader.get_active_instruments(market)

            if not instruments:
                logger.warning(
                    "No active instruments for %s — check configs/instruments.yaml",
                    market,
                )
                continue

            strategy_groups: dict[str, list] = {}
            for instrument in instruments:
                key = instrument.strategy_params.name
                strategy_groups.setdefault(key, []).append(instrument)

            for strategy_type, group in strategy_groups.items():
                symbols = [i.symbol for i in group]
                first = group[0]

                if strategy_type == "momentum":
                    strategy = MomentumStrategy(
                        name=f"{market.lower()}_{strategy_type}_v1",
                        symbols=symbols,
                        market=market,
                        short_window=first.strategy_params.short_window,
                        long_window=first.strategy_params.long_window,
                        min_confidence=first.strategy_params.min_confidence,
                        quantity_per_signal=first.strategy_params.quantity,
                    )
                    self.register_strategy(strategy)
                    logger.info(
                        "Registered %s/%s: %d symbols — %s",
                        market,
                        strategy_type,
                        len(symbols),
                        ", ".join(symbols),
                    )
                else:
                    logger.warning(
                        "Unknown strategy type '%s' for %s/%s — skipped",
                        strategy_type,
                        market,
                        symbols,
                    )


async def main() -> None:
    """Entry point for the Strategy Engine Service."""
    service = StrategyEngineService()
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
