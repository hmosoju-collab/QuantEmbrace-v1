"""
LocalOutbox — SQLite-backed durable buffer for Kafka messages during broker outages.

Used exclusively for kill-switch events and audit records that must survive a
transient Kafka unavailability window. Trading signals are NOT buffered here;
if Kafka is down, the kill switch activates and new signal processing halts.

Design decisions (ADR-015):
    - SQLite over DynamoDB: DynamoDB may itself be degraded when Kafka fails
      (same VPC, same AZ pressure). SQLite is process-local, zero-network.
    - /tmp over data volume: The outbox holds in-flight events only (minutes,
      not hours). Durability beyond the EC2 instance lifetime is not required.
    - Hard cap of 1 000 entries: prevents unbounded growth on a long outage.
      On overflow the service activates the kill switch and halts order intake.
    - Drain is best-effort: the background drain task runs every 2 seconds and
      retries each entry. Failed drain entries are not removed until confirmed.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 1_000
_DRAIN_INTERVAL_S = 2.0
_DB_FILENAME = "quantembrace_kafka_outbox.db"


class LocalOutbox:
    """
    SQLite-backed outbox for messages when Kafka is unavailable.

    Args:
        db_path:         Path to the SQLite database file. Defaults to a
                         temp-directory file so the outbox is ephemeral.
        max_entries:     Hard cap on the number of buffered messages.
        on_overflow:     Callable invoked (no args) when the hard cap is
                         reached. Expected to activate the kill switch.
    """

    def __init__(
        self,
        db_path: str | None = None,
        max_entries: int = _MAX_ENTRIES,
        on_overflow: Callable[[], None] | None = None,
    ) -> None:
        if db_path is None:
            db_path = str(Path(tempfile.gettempdir()) / _DB_FILENAME)
        self._db_path = db_path
        self._max_entries = max_entries
        self._on_overflow = on_overflow
        self._conn: sqlite3.Connection | None = None
        self._drain_task: asyncio.Task | None = None
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def open(self) -> None:
        """Open the SQLite connection and create the table if missing."""
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS outbox (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                topic    TEXT    NOT NULL,
                key      TEXT    NOT NULL,
                value    BLOB    NOT NULL,
                enqueued INTEGER NOT NULL   -- unix epoch seconds
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_id ON outbox(id)")
        self._conn.commit()
        logger.info("local_outbox.opened db_path=%s", self._db_path)

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def start_drain(
        self,
        producer_send: Callable[[str, str, bytes], None],
    ) -> None:
        """
        Start the background drain loop.

        Args:
            producer_send: Synchronous callable ``(topic, key, value) -> None``
                           that writes a single message to the Kafka producer.
                           Expected to raise on failure.
        """
        self._running = True
        self._drain_task = asyncio.create_task(
            self._drain_loop(producer_send),
            name="kafka-outbox-drain",
        )

    async def stop_drain(self) -> None:
        """Cancel the drain loop and wait for it to exit."""
        self._running = False
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

    # ── Write ─────────────────────────────────────────────────────────────────

    def enqueue(self, topic: str, key: str, value: bytes) -> bool:
        """
        Buffer a message for later delivery.

        Returns True if the message was enqueued, False if the outbox is full.
        When full, calls ``on_overflow`` (expected to activate kill switch).
        Thread-safe via SQLite's per-connection serialisation.
        """
        if self._conn is None:
            logger.error("local_outbox.enqueue_failed_not_open")
            return False

        count = self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        if count >= self._max_entries:
            logger.critical(
                "local_outbox.overflow topic=%s count=%d max=%d",
                topic, count, self._max_entries,
            )
            if self._on_overflow is not None:
                try:
                    self._on_overflow()
                except Exception:
                    logger.exception("local_outbox.overflow_callback_error")
            return False

        self._conn.execute(
            "INSERT INTO outbox (topic, key, value, enqueued) VALUES (?, ?, ?, ?)",
            (topic, key, value, int(time.time())),
        )
        self._conn.commit()
        logger.debug("local_outbox.enqueued topic=%s key=%s queue_depth=%d", topic, key, count + 1)
        return True

    def pending_count(self) -> int:
        """Return the number of messages waiting to be drained."""
        if self._conn is None:
            return 0
        return self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    # ── Drain ─────────────────────────────────────────────────────────────────

    async def _drain_loop(
        self,
        producer_send: Callable[[str, str, bytes], None],
    ) -> None:
        """Drain the outbox to Kafka every ``_DRAIN_INTERVAL_S`` seconds."""
        while self._running:
            await asyncio.sleep(_DRAIN_INTERVAL_S)
            try:
                drained = await asyncio.to_thread(self._drain_batch, producer_send)
                if drained > 0:
                    logger.info("local_outbox.drained count=%d", drained)
            except Exception:
                logger.exception("local_outbox.drain_error")

    def _drain_batch(self, producer_send: Callable[[str, str, bytes], None]) -> int:
        """
        Attempt to deliver all pending outbox entries to Kafka.

        Each successfully delivered entry is deleted. On error the entry stays
        and will be retried on the next drain cycle.
        Returns the number of entries successfully drained.
        """
        if self._conn is None:
            return 0

        rows = self._conn.execute(
            "SELECT id, topic, key, value FROM outbox ORDER BY id LIMIT 100"
        ).fetchall()

        drained = 0
        for row_id, topic, key, value in rows:
            try:
                producer_send(topic, key, value)
                self._conn.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
                self._conn.commit()
                drained += 1
            except Exception:
                logger.warning(
                    "local_outbox.drain_entry_failed id=%d topic=%s key=%s",
                    row_id, topic, key,
                )
                break  # stop on first failure; retry on next cycle

        return drained
