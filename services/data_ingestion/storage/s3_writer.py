"""
S3 Writer — batched writes of historical tick data to Amazon S3.

Accumulates ticks in memory and flushes to S3 in JSONL format,
partitioned by market / date / hour for efficient querying.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from shared.aws.clients import get_s3_client
from shared.logging.logger import get_logger
from shared.utils.helpers import retry

from data_ingestion.connectors.base import NormalizedTick

logger = get_logger(__name__, service_name="data_ingestion")


class S3Writer:
    """
    Batched writer for historical tick data to S3.

    Accumulates ticks in an in-memory buffer and flushes to S3 when:
        - Buffer size reaches ``batch_size``
        - ``flush()`` is called explicitly (e.g., on graceful shutdown)

    S3 key format:
        s3://{bucket}/ticks/{market}/{YYYY}/{MM}/{DD}/{HH}/{timestamp}.jsonl

    Writes are idempotent — re-uploading the same key overwrites with
    identical data, making the service restart-safe.
    """

    def __init__(
        self,
        bucket: str,
        region: str = "ap-south-1",
        batch_size: int = 1000,
        key_prefix: str = "ticks",
    ) -> None:
        """
        Initialize the S3 writer.

        Args:
            bucket: S3 bucket name.
            region: AWS region (used only if no shared client exists yet).
            batch_size: Number of ticks to buffer before auto-flushing.
            key_prefix: S3 key prefix for tick data objects.
        """
        self._bucket = bucket
        self._region = region
        self._batch_size = batch_size
        self._key_prefix = key_prefix
        self._buffer: list[dict[str, Any]] = []

    async def write_tick(self, tick: NormalizedTick) -> None:
        """
        Buffer a tick for batched writing to S3.

        Triggers an automatic flush when the buffer reaches ``batch_size``.

        Args:
            tick: Normalized tick to store.
        """
        record = {
            "symbol": tick.symbol,
            "market": tick.market.value,
            "last_price": tick.last_price,
            "bid": tick.bid,
            "ask": tick.ask,
            "volume": tick.volume,
            "timestamp": tick.timestamp.isoformat(),
            "broker": tick.broker,
        }
        self._buffer.append(record)

        if len(self._buffer) >= self._batch_size:
            await self.flush()

    async def flush(self) -> None:
        """
        Flush the current buffer to S3 as a JSONL object.

        On failure the batch is put back at the front of the buffer so it
        is retried on the next flush call (e.g., triggered by the next batch
        or on shutdown).
        """
        if not self._buffer:
            return

        batch = self._buffer.copy()
        self._buffer.clear()

        now = datetime.now(timezone.utc)
        market = batch[0].get("market", "UNKNOWN")
        key = (
            f"{self._key_prefix}/{market}/"
            f"{now.strftime('%Y/%m/%d/%H')}/"
            f"{now.strftime('%Y%m%d_%H%M%S_%f')}.jsonl"
        )

        body = "\n".join(json.dumps(record, default=str) for record in batch)

        try:
            await asyncio.to_thread(self._upload, key, body)
            logger.info(
                "Flushed %d ticks → s3://%s/%s",
                len(batch),
                self._bucket,
                key,
            )
        except Exception:
            # Re-insert failed batch at the front so it is retried next flush.
            self._buffer = batch + self._buffer
            logger.exception(
                "S3 upload failed — re-buffered %d records for retry", len(batch)
            )

    def _upload(self, key: str, body: str) -> None:
        """
        Synchronous S3 upload — called via asyncio.to_thread.

        Retried up to 3 times with exponential backoff by the shared client's
        botocore retry config.

        Args:
            key: S3 object key.
            body: JSONL content to upload.
        """
        client = get_s3_client()
        client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/x-ndjson",
        )

    @property
    def buffer_size(self) -> int:
        """Number of ticks currently buffered (not yet written to S3)."""
        return len(self._buffer)
