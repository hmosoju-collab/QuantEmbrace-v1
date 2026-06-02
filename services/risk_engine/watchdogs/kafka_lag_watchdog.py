"""
KafkaLagWatchdog — monitors risk-v1 consumer group lag and activates the
kill switch when the risk engine falls dangerously behind.

Phase 8 (ADR-015 F6): sustained consumer-group overload.

Root cause:
    If the risk engine's Kafka consumer (risk-v1) falls behind the signal
    producers (strategy_engine or ai_engine), signals accumulate in the
    partition. Once lag exceeds the threshold for N consecutive checks,
    the queue is so deep that any newly approved signal will expire before
    execution sees it. Continuing to validate and publish approved signals
    in this state wastes compute and creates orders against stale prices.

Response:
    Activate the kill switch. This immediately:
        1. Causes validate_signal() to reject all incoming signals (kill switch check).
        2. Prevents the execution engine from receiving new approved signals.
        3. Gives the risk engine time to drain the backlog.

Recovery:
    Operator manually clears the kill switch via killswitch_api.py after
    confirming the lag has returned to zero. Auto-recovery is intentionally
    NOT implemented (ADR-015 §5.3: operator-only reconciliation clear).

Tuning:
    threshold_messages: 500 — conservative default for this trading volume.
        At 500ms poll interval and worst-case 1 signal/second, 500 messages
        = 500s of backlog. By that point all signals are expired anyway.
    consecutive_checks: 3 checks × 1s = 3 seconds of sustained breach.
        Prevents a single spike from triggering the kill switch.

Integration:
    RiskEngineService instantiates this watchdog, passing a consumer_provider
    lambda that returns the active confluent-kafka Consumer object (primary
    or fallback, depending on EnrichmentState). The watchdog runs as a
    background asyncio.Task alongside the processing loops.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from shared.kafka.lag_monitor import KafkaLagKillSwitchMonitor
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="risk_engine")

# Default tuning (see module docstring rationale)
_DEFAULT_LAG_THRESHOLD:      int   = 500
_DEFAULT_CONSECUTIVE_CHECKS: int   = 3
_DEFAULT_CHECK_INTERVAL_S:   float = 1.0


class KafkaLagWatchdog:
    """
    Monitors risk-v1 consumer lag and activates the kill switch on overload.

    Wraps ``KafkaLagKillSwitchMonitor`` with risk_engine–specific defaults
    and adds a start/stop lifecycle so ``RiskEngineService`` can manage it
    as part of its asyncio.gather() loop set.

    Args:
        consumer_provider:      ``() -> confluent_kafka.Consumer | None``
                                Returns the active consumer for lag measurement.
                                Must return None when no consumer is assigned
                                (monitor will skip that cycle safely).
        kill_switch_activate:   Async callable ``(reason, activated_by) -> None``
                                from ``KillSwitch.activate``.
        lag_threshold:          Messages behind before breach is counted.
        consecutive_checks:     Consecutive breaching checks before activation.
        check_interval_seconds: Seconds between lag measurements.
    """

    def __init__(
        self,
        consumer_provider: Callable[[], Any],
        kill_switch_activate: Callable[[str, str], Any],
        lag_threshold: int = _DEFAULT_LAG_THRESHOLD,
        consecutive_checks: int = _DEFAULT_CONSECUTIVE_CHECKS,
        check_interval_seconds: float = _DEFAULT_CHECK_INTERVAL_S,
    ) -> None:
        self._monitor = KafkaLagKillSwitchMonitor(
            consumer_provider=consumer_provider,
            consumer_group="risk-v1",
            service_name="risk_engine",
            activate_callback=kill_switch_activate,
            threshold_messages=lag_threshold,
            consecutive_checks=consecutive_checks,
            check_interval_seconds=check_interval_seconds,
        )
        self._task: Optional[asyncio.Task] = None

    @property
    def activated(self) -> bool:
        """True if the kill switch has been activated by this watchdog."""
        return self._monitor.activated

    async def start(self) -> None:
        """Start the background lag monitoring task."""
        self._task = asyncio.create_task(
            self._monitor.run(),
            name="risk_engine_lag_watchdog",
        )
        logger.info(
            "kafka_lag_watchdog.started group=risk-v1 "
            "threshold=%d consecutive=%d interval=%.1fs",
            self._monitor._threshold,
            self._monitor._consecutive_checks,
            self._monitor._interval,
        )

    async def stop(self) -> None:
        """Stop the lag monitoring task."""
        await self._monitor.stop()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("kafka_lag_watchdog.stopped")

    async def run(self) -> None:
        """
        Run the lag monitor inline (for use in asyncio.gather).

        Delegates directly to the underlying KafkaLagKillSwitchMonitor.run().
        """
        await self._monitor.run()
