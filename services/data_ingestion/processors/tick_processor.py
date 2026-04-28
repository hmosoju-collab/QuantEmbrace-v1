"""
Tick Processor — validates and fans out normalized ticks to all backends.

Receives NormalizedTick objects from connectors, validates them, then
dispatches to four output destinations:

    1. S3Writer       — batched historical archive (all ticks)
    2. DynamoWriter   — latest price snapshot per symbol (fast lookups)
    3. SQSTickPublisher — streams latest-per-symbol to Strategy Engine
    4. In-memory stats — tick counts and last-price tracking

Adding a new output destination: inject it via __init__ and call it in
process_tick(). Never modify connector or storage code for routing changes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from shared.logging.logger import get_logger

from data_ingestion.connectors.base import NormalizedTick
from data_ingestion.storage.dynamo_writer import DynamoWriter
from data_ingestion.storage.s3_writer import S3Writer

if TYPE_CHECKING:
    # Avoid circular imports — SQSTickPublisher is injected at runtime
    from data_ingestion.publishers.sqs_publisher import SQSTickPublisher

logger = get_logger(__name__, service_name="data_ingestion")


class TickProcessor:
    """
    Validates and fans out normalized ticks to all configured backends.

    Routing:
        S3Writer          → every valid tick (batched, historical archive)
        DynamoWriter      → every valid tick (latest-value per symbol)
        SQSTickPublisher  → every valid tick (buffered; strategy engine reads)

    All three destinations are optional — the processor runs with whichever
    backends are injected, making it easy to test in isolation.
    """

    def __init__(
        self,
        s3_writer: Optional[S3Writer] = None,
        dynamo_writer: Optional[DynamoWriter] = None,
        sqs_publisher: Optional["SQSTickPublisher"] = None,
        stale_threshold_seconds: float = 60.0,
    ) -> None:
        """
        Initialize the tick processor.

        Args:
            s3_writer: Writer for batched historical data to S3.
            dynamo_writer: Writer for latest prices to DynamoDB.
            sqs_publisher: Publisher that forwards ticks to the Strategy Engine
                           via SQS. If None, ticks are not forwarded downstream.
            stale_threshold_seconds: Max age (seconds) of a tick before it is
                                     considered stale and rejected.
        """
        self._s3_writer = s3_writer
        self._dynamo_writer = dynamo_writer
        self._sqs_publisher = sqs_publisher
        self._stale_threshold = stale_threshold_seconds
        self._last_prices: dict[str, float] = {}
        self._last_timestamps: dict[str, datetime] = {}
        self._tick_counts: dict[str, int] = defaultdict(int)
        self._error_count: int = 0

    async def process_tick(self, tick: NormalizedTick) -> None:
        """
        Validate and fan out a single normalized tick to all backends.

        Flow:
            1. Validate (non-zero price, timezone-aware timestamp, not stale).
            2. Update in-memory tracking stats.
            3. Dispatch to S3 (historical archive).
            4. Dispatch to DynamoDB (latest price).
            5. Dispatch to SQS (strategy engine feed).

        Args:
            tick: The normalized tick received from a broker connector.
        """
        if not self._validate_tick(tick):
            self._error_count += 1
            return

        # Update in-memory tracking
        key = f"{tick.market.value}:{tick.symbol}"
        self._last_prices[key] = tick.last_price
        self._last_timestamps[key] = tick.timestamp
        self._tick_counts[key] += 1

        # 1. Historical archive (S3, batched)
        if self._s3_writer is not None:
            await self._s3_writer.write_tick(tick)

        # 2. Latest-price snapshot (DynamoDB)
        if self._dynamo_writer is not None:
            await self._dynamo_writer.write_tick(tick)

        # 3. Strategy engine feed (SQS, latest-value-per-symbol batching)
        if self._sqs_publisher is not None:
            await self._sqs_publisher.publish(tick)

    def _validate_tick(self, tick: NormalizedTick) -> bool:
        """
        Validate that a tick has reasonable data.

        Returns:
            True if the tick is valid, False otherwise.
        """
        if tick.last_price <= 0:
            logger.warning(
                "Invalid tick price for %s: %.4f", tick.symbol, tick.last_price
            )
            return False

        # Check for stale ticks
        now = datetime.now(timezone.utc)
        if tick.timestamp.tzinfo is None:
            logger.warning("Tick for %s has naive timestamp", tick.symbol)
            return False

        age = (now - tick.timestamp).total_seconds()
        if age > self._stale_threshold:
            logger.warning(
                "Stale tick for %s: %.1fs old (threshold: %.1fs)",
                tick.symbol,
                age,
                self._stale_threshold,
            )
            return False

        return True

    def get_last_price(self, market: str, symbol: str) -> Optional[float]:
        """Get the last observed price for a symbol."""
        return self._last_prices.get(f"{market}:{symbol}")

    def get_tick_count(self, market: str, symbol: str) -> int:
        """Get the total tick count for a symbol."""
        return self._tick_counts.get(f"{market}:{symbol}", 0)

    @property
    def total_ticks_processed(self) -> int:
        """Total number of valid ticks processed across all symbols."""
        return sum(self._tick_counts.values())

    @property
    def error_count(self) -> int:
        """Total number of ticks that failed validation."""
        return self._error_count
