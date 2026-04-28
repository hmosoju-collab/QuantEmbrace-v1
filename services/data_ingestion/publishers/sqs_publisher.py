"""
SQS Tick Publisher — forwards normalized ticks to the Strategy Engine.

Design — why we batch:
    Live market data arrives at high frequency (potentially thousands of ticks
    per second across all instruments). Publishing every tick individually to
    SQS would be extremely expensive and wasteful — the Strategy Engine only
    needs the *latest* price per symbol to make momentum decisions.

    This publisher maintains a "latest-value cache" per symbol: each incoming
    tick overwrites the previous pending entry for that symbol. A background
    flush loop runs every ``flush_interval`` seconds (default: 1 s) and
    sends one SQS message per symbol per interval.

Cost model (default 1-second flush, 30 symbols, 6.5-hour US trading day):
    30 symbols × 3_600 s/hr × 6.5 hr ≈ 702_000 messages/day
    At $0.40 / 1M messages ≈ $0.28/day — negligible.

SQS queue:
    Uses FIFO queue (configured via ``queue_url``). Each message uses
    MessageGroupId=symbol to preserve per-symbol ordering.
    MessageDeduplicationId = symbol + timestamp ensures idempotency if the
    flush loop accidentally fires twice for the same second.

Usage::

    publisher = SQSTickPublisher(
        queue_url="https://sqs.ap-south-1.amazonaws.com/.../market-data.fifo"
    )
    await publisher.start()

    # Feed ticks in from TickProcessor
    await publisher.publish(tick)

    # On shutdown
    await publisher.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from shared.aws.clients import get_sqs_client
from shared.logging.logger import get_logger

from data_ingestion.connectors.base import NormalizedTick

logger = get_logger(__name__, service_name="data_ingestion")

# SQS SendMessageBatch allows at most 10 entries per call
_SQS_BATCH_SIZE = 10


class SQSTickPublisher:
    """
    Batched SQS publisher for normalized market-data ticks.

    Architecture:
        - ``publish(tick)`` is called by TickProcessor for every valid tick.
          It is O(1) — just an in-memory dict write (latest-value per symbol).
        - A background asyncio Task (``_flush_loop``) wakes every
          ``flush_interval`` seconds, snapshots the pending dict, and sends
          all pending ticks to SQS in batches of up to 10.
        - On ``stop()``, the flush loop is cancelled and one final flush runs
          to drain any ticks accumulated after the last scheduled flush.
    """

    def __init__(
        self,
        queue_url: str,
        flush_interval: float = 1.0,
    ) -> None:
        """
        Initialize the SQS tick publisher.

        Args:
            queue_url: Full SQS FIFO queue URL for the market-data queue.
            flush_interval: How often (seconds) to flush pending ticks to SQS.
                            Lower values = more messages but lower latency.
                            1.0 s is a good default for momentum strategies.
        """
        self._queue_url = queue_url
        self._flush_interval = flush_interval

        # Latest tick per symbol key (e.g., "NSE:RELIANCE")
        # Dict writes/reads are thread-safe in CPython due to the GIL,
        # but we're single-threaded async here so it's fine regardless.
        self._pending: dict[str, dict] = {}

        self._flush_task: Optional[asyncio.Task[None]] = None
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background flush loop."""
        if self._running:
            return
        self._running = True
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="sqs_tick_flush"
        )
        logger.info(
            "SQS tick publisher started (queue=%s flush_interval=%.1fs)",
            self._queue_url,
            self._flush_interval,
        )

    async def stop(self) -> None:
        """
        Stop the flush loop and drain remaining pending ticks.

        Called on service shutdown to ensure no ticks are dropped.
        """
        self._running = False

        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Final flush to drain anything accumulated since the last tick
        await self.flush()
        logger.info("SQS tick publisher stopped")

    async def publish(self, tick: NormalizedTick) -> None:
        """
        Buffer a tick for publishing.

        Only the latest tick per symbol is kept — if RELIANCE ticks 5 times
        before the next flush, only the 5th (most recent) tick is sent to SQS.

        Args:
            tick: Normalized tick to buffer.
        """
        key = f"{tick.market.value}:{tick.symbol}"
        self._pending[key] = {
            "symbol": tick.symbol,
            "market": tick.market.value,
            "last_price": tick.last_price,
            "bid": tick.bid,
            "ask": tick.ask,
            "volume": tick.volume,
            "timestamp": tick.timestamp.isoformat(),
            "broker": tick.broker,
        }

    async def flush(self) -> None:
        """
        Send all pending ticks to SQS using SendMessageBatch.

        Snapshots and clears the pending dict atomically (within the async
        event loop — no races with ``publish()``), then sends in chunks of
        up to 10 (SQS limit per batch call).
        """
        if not self._pending:
            return

        # Snapshot and clear atomically
        snapshot = dict(self._pending)
        self._pending.clear()

        items = list(snapshot.items())
        total_sent = 0

        for chunk_start in range(0, len(items), _SQS_BATCH_SIZE):
            chunk = items[chunk_start : chunk_start + _SQS_BATCH_SIZE]
            try:
                await asyncio.to_thread(self._send_batch, chunk)
                total_sent += len(chunk)
            except Exception:
                logger.exception(
                    "SQS batch send failed for %d ticks — re-queuing for next flush",
                    len(chunk),
                )
                # Re-insert failed ticks; newer pending entries take priority
                for key, msg in chunk:
                    self._pending.setdefault(key, msg)

        if total_sent:
            logger.debug(
                "Flushed %d tick(s) to SQS market-data queue", total_sent
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        """
        Background loop: flush pending ticks every ``flush_interval`` seconds.
        """
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in SQS flush loop")

    def _send_batch(self, items: list[tuple[str, dict]]) -> None:
        """
        Synchronous SQS SendMessageBatch — called via asyncio.to_thread.

        SQS FIFO constraints applied:
            - MessageGroupId = symbol      (preserves per-symbol ordering)
            - MessageDeduplicationId = symbol + timestamp
              (prevents duplicate processing if the same message is sent twice)

        Args:
            items: List of (key, tick_dict) tuples to send.
        """
        sqs = get_sqs_client()
        entries = [
            {
                "Id": str(idx),
                "MessageBody": json.dumps(msg, default=str),
                "MessageGroupId": msg["symbol"],
                "MessageDeduplicationId": (
                    f"{msg['symbol']}-{msg['timestamp']}"
                ),
            }
            for idx, (_, msg) in enumerate(items)
        ]

        response = sqs.send_message_batch(
            QueueUrl=self._queue_url,
            Entries=entries,
        )

        # Log any partial failures (SQS batch returns per-entry success/fail)
        failed = response.get("Failed", [])
        if failed:
            for failure in failed:
                logger.error(
                    "SQS batch entry failed: Id=%s Code=%s Message=%s",
                    failure.get("Id"),
                    failure.get("Code"),
                    failure.get("Message"),
                )

    @property
    def pending_count(self) -> int:
        """Number of ticks buffered and awaiting the next flush."""
        return len(self._pending)
