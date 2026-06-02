"""Kafka consumer lag monitor with kill-switch activation callback."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="shared")

ConsumerProvider = Callable[[], Any]
ActivateCallback = Callable[[str, str], Awaitable[None]]


def measure_consumer_lag(consumer: Any) -> int:
    """Return maximum lag across the consumer's current assignment."""
    if consumer is None:
        return 0

    assignment = consumer.assignment() or []
    if not assignment:
        return 0

    positions = consumer.position(assignment) or []
    max_lag = 0
    for assigned, positioned in zip(assignment, positions):
        try:
            _, high = consumer.get_watermark_offsets(
                assigned,
                timeout=1.0,
                cached=False,
            )
            offset = int(getattr(positioned, "offset", -1))
            if offset < 0:
                # Negative sentinel (OFFSET_STORED=-1000, OFFSET_BEGINNING=-1001,
                # OFFSET_INVALID=-1001) means the consumer hasn't fetched any
                # message from this partition yet.  Real lag is unknown — skip
                # rather than produce a spurious large value (0 - (-1000) = 1000).
                continue
            max_lag = max(max_lag, max(0, int(high) - offset))
        except Exception:
            logger.exception("kafka_lag_monitor.measure_partition_failed")
    return max_lag


class KafkaLagKillSwitchMonitor:
    """Activate a kill switch when Kafka consumer lag remains unsafe."""

    def __init__(
        self,
        *,
        consumer_provider: ConsumerProvider,
        consumer_group: str,
        service_name: str,
        activate_callback: ActivateCallback,
        threshold_messages: int,
        consecutive_checks: int = 3,
        check_interval_seconds: float = 1.0,
        startup_grace_seconds: float = 30.0,
    ) -> None:
        self._consumer_provider = consumer_provider
        self._consumer_group = consumer_group
        self._service_name = service_name
        self._activate_callback = activate_callback
        self._threshold = max(0, int(threshold_messages))
        self._consecutive_checks = max(1, int(consecutive_checks))
        self._interval = max(0.1, float(check_interval_seconds))
        self._startup_grace = max(0.0, float(startup_grace_seconds))
        self._breach_count = 0
        self._activated = False
        self._running = False

    @property
    def activated(self) -> bool:
        """Whether this monitor has already activated the halt callback."""
        return self._activated

    async def run(self) -> None:
        """Poll lag until stopped. Skips checks during startup grace period."""
        self._running = True
        logger.info(
            "kafka_lag_monitor.started service=%s group=%s threshold=%d grace=%.0fs",
            self._service_name,
            self._consumer_group,
            self._threshold,
            self._startup_grace,
        )
        if self._startup_grace > 0:
            await asyncio.sleep(self._startup_grace)
        while self._running:
            await self.check_once()
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        """Stop the monitor loop."""
        self._running = False

    async def check_once(self) -> int:
        """Measure lag once and activate the callback after sustained breach."""
        if self._threshold <= 0 or self._activated:
            return 0

        consumer = self._consumer_provider()
        lag = await asyncio.to_thread(measure_consumer_lag, consumer)
        if lag <= self._threshold:
            self._breach_count = 0
            return lag

        self._breach_count += 1
        logger.critical(
            "kafka_lag_monitor.breach service=%s group=%s lag=%d threshold=%d "
            "breach_count=%d required=%d",
            self._service_name,
            self._consumer_group,
            lag,
            self._threshold,
            self._breach_count,
            self._consecutive_checks,
        )
        if self._breach_count >= self._consecutive_checks:
            reason = (
                "KAFKA_CONSUMER_LAG_EXCEEDED "
                f"service={self._service_name} group={self._consumer_group} "
                f"lag={lag} threshold={self._threshold}"
            )
            await self._activate_callback(reason, "kafka_lag_monitor")
            self._activated = True
        return lag
