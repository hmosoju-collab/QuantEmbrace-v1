"""
Kafka Kill Switch Listener — subscribes to the risk.kill-switch topic and activates
the KillSwitch immediately on receipt of a KILL_SWITCH_ACTIVE event.

Topic:          risk.kill-switch  (1 partition)
Consumer group: risk-v1
Offset reset:   latest — only react to *future* kill-switch events; do not
                replay historical activations (state is restored from DynamoDB
                on startup via KillSwitch.load_state()).

The risk.kill-switch topic is a secondary propagation path that complements the
primary DynamoDB + SNS mechanism:

    Primary:   DynamoDB state → SNS → all services poll SNS on each loop.
    Secondary: risk.kill-switch Kafka topic → this listener → immediate in-process
               activation without waiting for the next poll cycle.

This listener adds a sub-100ms propagation path inside the risk engine.  Any
service that also subscribes to risk.kill-switch (e.g. a future execution_engine
listener) gets the same benefit.

Expected event schema (schema_version 3.0):
    {
        "event_id":        str (uuid4)
        "trace_id":        str
        "event_type":      "KILL_SWITCH_ACTIVE" | "KILL_SWITCH_CLEARED"
        "schema_version":  "3.0"
        "source":          str (who triggered it)
        "published_time":  ISO8601 UTC
        "reason":          str (human-readable activation reason)
        "activated_by":    str (operator ID, auto-trigger name, etc.)
    }

Usage in service.py:
    listener = KafkaKillSwitchListener(
        bootstrap_servers=kafka_bootstrap,
        aws_region=settings.aws.region,
        kill_switch=self._kill_switch,
    )
    await listener.start()

    # Run as a dedicated asyncio task (never blocks the signal loop):
    asyncio.create_task(listener.listen())

    await listener.stop()
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional, TYPE_CHECKING

from shared.events.schemas import CANONICAL_KILL_SWITCH_TOPIC, EventType, validate_event
from shared.kafka.failure_publisher import KafkaFailurePublisher
from shared.logging.logger import get_logger

if TYPE_CHECKING:
    from risk_engine.killswitch.killswitch import KillSwitch

logger = get_logger(__name__, service_name="risk_engine")

# ── Import guard ──────────────────────────────────────────────────────────────
try:
    from confluent_kafka import Consumer, KafkaError
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning(
        "confluent-kafka not installed — KafkaKillSwitchListener will not function. "
        "Install: pip install confluent-kafka~=2.3"
    )

from shared.kafka.config import get_kafka_auth_config

_TOPIC = CANONICAL_KILL_SWITCH_TOPIC
_CONSUMER_GROUP = "risk-v1"


class KafkaKillSwitchListener:
    """
    Async listener for risk.kill-switch Kafka topic events.

    Runs as a dedicated asyncio task.  On receipt of a KILL_SWITCH_ACTIVE
    event, immediately calls ``kill_switch.activate()`` to halt trading.
    KILL_SWITCH_CLEARED events are logged but do not auto-deactivate the kill
    switch — deactivation is always a deliberate operator action via DynamoDB.

    Design decisions:
        - ``auto.offset.reset=latest``: Startup state comes from DynamoDB via
          KillSwitch.load_state(), not from topic replay.
        - ``enable.auto.commit=False``: offsets are committed manually after
          activation handling or DLQ routing succeeds.
        - Runs in a tight async loop using asyncio.to_thread() to avoid blocking
          the event loop.  poll() blocks for up to 0.5s per iteration.
        - On KILL_SWITCH_ACTIVE, activate() is called and the loop continues
          polling (idempotent — subsequent activation calls on an already-active
          switch are a no-op).

    Fault behaviour:
        - Any Kafka error is logged and the loop continues (does not halt trading).
        - A Kafka failure does NOT prevent the DynamoDB-based kill switch from
          functioning — it is a secondary fast-path only.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        kill_switch: "KillSwitch",
        aws_region: str = "ap-south-1",
        consumer_group: str = _CONSUMER_GROUP,
    ) -> None:
        """
        Initialize the kill switch listener.

        Args:
            bootstrap_servers: MSK bootstrap broker string (port 9098).
            kill_switch:       The KillSwitch instance to activate on receipt
                               of a KILL_SWITCH_ACTIVE event.
            aws_region:        AWS region for IAM token generation.
            consumer_group:    Kafka consumer group. Defaults to "risk-v1".

        Raises:
            RuntimeError: If confluent-kafka or aws-msk-iam-sasl-signer-python
                          are not installed.
        """
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required for KafkaKillSwitchListener. "
                "Install: pip install confluent-kafka~=2.3"
            )
        self._bootstrap_servers = bootstrap_servers
        self._kill_switch = kill_switch
        self._aws_region = aws_region
        self._consumer_group = consumer_group
        self._consumer: Optional["Consumer"] = None
        self._failure_publisher: Optional[KafkaFailurePublisher] = None
        self._last_failure_routed: bool = False
        self._running: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Build the Kafka consumer and subscribe to the risk.kill-switch topic."""
        self._consumer = self._build_consumer()
        self._failure_publisher = KafkaFailurePublisher(
            bootstrap_servers=self._bootstrap_servers,
            aws_region=self._aws_region,
            source_service="risk_engine",
        )
        self._failure_publisher.start()
        self._consumer.subscribe([_TOPIC])
        self._running = True
        logger.info(
            "KafkaKillSwitchListener started (group=%s, topic=%s)",
            self._consumer_group,
            _TOPIC,
        )

    async def stop(self) -> None:
        """Stop the listener and close the Kafka consumer."""
        self._running = False
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None
        if self._failure_publisher is not None:
            self._failure_publisher.close()
            self._failure_publisher = None
        logger.info(
            "KafkaKillSwitchListener stopped (group=%s)", self._consumer_group
        )

    # ── Main listen loop ──────────────────────────────────────────────────────

    async def listen(self) -> None:
        """
        Async loop: poll risk.kill-switch, activate KillSwitch on KILL_SWITCH_ACTIVE.

        Designed to run as a dedicated asyncio.Task.  Runs until stop() sets
        _running=False.  Never raises — all exceptions are caught and logged so
        that a Kafka outage does not crash the risk engine.

        Activation is idempotent: calling kill_switch.activate() on an already-
        active switch is a safe no-op.
        """
        logger.info(
            "KafkaKillSwitchListener.listen started (topic=%s)", _TOPIC
        )

        while self._running:
            try:
                event = await asyncio.to_thread(self._poll_once)
                if event is None:
                    continue

                event_type = event.get("event_type")
                raw_message = event.pop("_raw_message", None)

                if event_type == "KILL_SWITCH_ACTIVE":
                    reason = event.get("reason", "Kill switch activated via Kafka")
                    activated_by = event.get("activated_by", "kafka_listener")

                    logger.warning(
                        "kill_switch.kafka_activation_received "
                        "(reason=%r, activated_by=%r) — activating immediately",
                        reason,
                        activated_by,
                    )

                    await self._kill_switch.activate(
                        reason=reason,
                        activated_by=f"kafka:{activated_by}",
                    )
                    if raw_message is not None:
                        self._commit_message(raw_message)

                elif event_type == "KILL_SWITCH_CLEARED":
                    # Deactivation is a deliberate operator action only.
                    # Log but do NOT auto-deactivate — the operator must confirm
                    # via DynamoDB/API to resume trading.
                    logger.info(
                        "kill_switch.kafka_clear_received "
                        "(reason=%r) — manual deactivation required to resume trading",
                        event.get("reason", ""),
                    )
                    if raw_message is not None:
                        self._commit_message(raw_message)

                else:
                    logger.warning(
                        "kill_switch.unknown_event_type %r on risk.kill-switch topic — skipping",
                        event_type,
                    )
                    if raw_message is not None:
                        self._commit_message(raw_message)

            except asyncio.CancelledError:
                logger.info("KafkaKillSwitchListener.listen cancelled")
                break
            except Exception:
                logger.exception(
                    "kill_switch_listener.error — Kafka failure does not halt trading "
                    "(DynamoDB kill switch still active)"
                )
                await asyncio.sleep(2)  # brief backoff on unexpected error

        logger.info("KafkaKillSwitchListener.listen stopped")

    # ── Internal poll (synchronous — must run in asyncio.to_thread) ──────────

    def _poll_once(self) -> Optional[dict]:
        """
        Synchronous: poll the risk.kill-switch topic for one message.

        Blocks up to 0.5s. Returns the parsed JSON body on success,
        None on timeout, Kafka error, or parse failure.

        Must be called via asyncio.to_thread().
        """
        if not self._consumer or not self._running:
            return None

        msg = self._consumer.poll(0.5)
        if msg is None:
            return None

        if msg.error():
            err = msg.error()
            if err.code() == KafkaError._PARTITION_EOF:
                return None
            logger.error(
                "KafkaKillSwitchListener Kafka error: %s", err
            )
            return None

        try:
            body = json.loads(msg.value().decode("utf-8"))

            event_type = body.get("event_type")
            if event_type not in (
                EventType.KILL_SWITCH_ACTIVE.value,
                EventType.KILL_SWITCH_CLEARED.value,
            ):
                self._last_failure_routed = self._publish_message_to_dlq(
                    msg,
                    reason=f"Unexpected event_type {event_type!r}",
                    error_type="schema_validation_failed",
                )
                if self._last_failure_routed:
                    self._commit_message(msg)
                return None

            errors = validate_event(body, event_type)
            if errors:
                self._last_failure_routed = self._publish_message_to_dlq(
                    msg,
                    reason="; ".join(errors),
                    error_type="schema_validation_failed",
                )
                if self._last_failure_routed:
                    self._commit_message(msg)
                return None

            body["_raw_message"] = msg
            return body

        except (json.JSONDecodeError, AttributeError, UnicodeDecodeError) as exc:
            logger.exception(
                "Malformed risk.kill-switch message (offset=%s) — skipping",
                msg.offset() if msg.offset() is not None else "?",
            )
            self._last_failure_routed = self._publish_message_to_dlq(
                msg,
                reason=str(exc),
                error_type="malformed_kill_switch_event",
            )
            if self._last_failure_routed:
                self._commit_message(msg)
            return None

    def _commit_message(self, msg) -> None:
        if self._consumer is None:
            return
        self._consumer.commit(message=msg, asynchronous=False)

    def _publish_message_to_dlq(self, msg, *, reason: str, error_type: str) -> bool:
        if self._failure_publisher is None:
            return False
        return self._failure_publisher.publish_dlq(
            source_topic=msg.topic() or _TOPIC,
            key=msg.key() if msg.key() else None,
            value=msg.value() if msg.value() else None,
            reason=reason,
            error_type=error_type,
            details={
                "partition": msg.partition() if hasattr(msg, "partition") else None,
                "offset": msg.offset() if hasattr(msg, "offset") else None,
            },
        )

    # ── Consumer construction ─────────────────────────────────────────────────

    def _build_consumer(self) -> "Consumer":
        """Build a confluent-kafka Consumer for MSK Serverless with IAM auth."""
        region = self._aws_region
        return Consumer({
            "bootstrap.servers":                     self._bootstrap_servers,
            **get_kafka_auth_config(region),
            "group.id":                              self._consumer_group,
            # Only react to future kill-switch events.  Startup state from DynamoDB.
            "auto.offset.reset":                     "latest",
            # Manual commit only after activation handling or DLQ routing succeeds.
            "enable.auto.commit":                    False,
            "enable.auto.offset.store":              False,
            "session.timeout.ms":                    30000,
            "heartbeat.interval.ms":                 10000,
            "max.poll.interval.ms":                  300000,
            "socket.connection.setup.timeout.ms":    15000,
            "log.connection.close":                  False,
        })
