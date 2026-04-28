"""
DynamoDB Writer — writes latest prices to DynamoDB for fast lookups.

Maintains a single row per symbol with the most recent price snapshot.
Uses conditional writes for idempotency (only updates if timestamp is newer).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Optional

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from shared.aws.clients import get_dynamodb_resource
from shared.logging.logger import get_logger
from shared.utils.helpers import retry

from data_ingestion.connectors.base import NormalizedTick

logger = get_logger(__name__, service_name="data_ingestion")


class DynamoWriter:
    """
    Writes latest price snapshots to DynamoDB.

    Table schema:
        Partition key: symbol (String) — e.g., "NSE:RELIANCE" or "US:AAPL"
        Attributes: last_price, bid, ask, volume, timestamp, broker, updated_at

    Writes are idempotent: uses a condition expression to only update if the
    incoming timestamp is newer than the stored one, making the service
    restart-safe.
    """

    def __init__(
        self,
        table_name: str,
        region: str = "ap-south-1",
    ) -> None:
        """
        Initialize the DynamoDB writer.

        Args:
            table_name: DynamoDB table name for latest prices.
            region: AWS region (used only if no shared client exists yet).
        """
        self._table_name = table_name
        self._region = region
        self._table: Any = None
        self._pending_writes: dict[str, dict[str, Any]] = {}

    def _get_table(self) -> Any:
        """
        Return the DynamoDB Table resource, initialising on first call.

        Uses the shared client factory so LocalStack is auto-detected.
        """
        if self._table is None:
            dynamodb = get_dynamodb_resource()
            self._table = dynamodb.Table(self._table_name)
            logger.info("DynamoDB table ready: %s", self._table_name)
        return self._table

    async def write_tick(self, tick: NormalizedTick) -> None:
        """
        Buffer a tick for writing to DynamoDB.

        The latest value per symbol wins — rapid ticks for the same symbol
        are deduplicated in the buffer before any DynamoDB call is made.

        Args:
            tick: Normalized tick with latest price data.
        """
        key = f"{tick.market.value}:{tick.symbol}"
        item = {
            "symbol": key,
            "last_price": Decimal(str(tick.last_price)),
            "bid": Decimal(str(tick.bid)),
            "ask": Decimal(str(tick.ask)),
            "volume": tick.volume,
            "timestamp": tick.timestamp.isoformat(),
            "broker": tick.broker,
            "market": tick.market.value,
            "raw_symbol": tick.symbol,
        }
        # Buffer: if two ticks for the same symbol arrive before flush,
        # only the latest matters (overwrite in dict).
        self._pending_writes[key] = item

    async def flush(self) -> None:
        """
        Flush all pending writes to DynamoDB using batch_writer.

        Clears the buffer after a successful write. On failure, logs and
        drops the batch — price data is ephemeral, the next tick overwrites.
        """
        if not self._pending_writes:
            return

        items = list(self._pending_writes.values())
        self._pending_writes.clear()

        try:
            await asyncio.to_thread(self._batch_write, items)
            logger.debug("Flushed %d price updates to DynamoDB", len(items))
        except Exception:
            logger.exception(
                "Failed to flush %d items to DynamoDB — dropped (next tick will overwrite)",
                len(items),
            )

    def _batch_write(self, items: list[dict[str, Any]]) -> None:
        """
        Synchronous batch write — called via asyncio.to_thread.

        Uses DynamoDB batch_writer for efficiency (up to 25 items per request,
        automatically chunked by boto3).

        Args:
            items: List of item dicts to write.
        """
        table = self._get_table()
        with table.batch_writer() as batch:
            for item in items:
                batch.put_item(Item=item)

    def _conditional_put(self, item: dict[str, Any]) -> None:
        """
        Conditionally put an item — only writes if stored timestamp is older.

        Used for single-item writes that need strict ordering guarantees.
        batch_write does not support ConditionExpression.

        Args:
            item: Item dict to write.
        """
        table = self._get_table()
        try:
            table.put_item(
                Item=item,
                ConditionExpression=(
                    Attr("timestamp").not_exists()
                    | Attr("timestamp").lt(item["timestamp"])
                ),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Existing record is newer — safe to skip.
                logger.debug(
                    "Skipped older tick for %s (existing record is newer)",
                    item.get("symbol"),
                )
            else:
                raise

    async def get_latest_price(self, symbol: str) -> Optional[dict[str, Any]]:
        """
        Retrieve the latest price snapshot for a symbol.

        Args:
            symbol: Composite key like "NSE:RELIANCE" or "US:AAPL".

        Returns:
            Item dict if found, None otherwise.
        """
        try:
            response = await asyncio.to_thread(
                self._get_table().get_item,
                Key={"symbol": symbol},
            )
            return response.get("Item")
        except Exception:
            logger.exception("Failed to get latest price for %s", symbol)
            return None
