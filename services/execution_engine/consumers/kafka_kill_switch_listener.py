"""Execution kill-switch listener for the canonical risk.kill-switch topic."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

from shared.events.schemas import CANONICAL_KILL_SWITCH_TOPIC, EventType, validate_event
from shared.kafka.failure_publisher import KafkaFailurePublisher
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="execution_engine")

try:
    from confluent_kafka import Consumer, KafkaError
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning("confluent-kafka not installed - execution kill-switch listener disabled")

from shared.kafka.config import get_kafka_auth_config


_TOPIC = CANONICAL_KILL_SWITCH_TOPIC
_CONSUMER_GROUP = "execution-v1-kill-switch"


class KafkaExecutionKillSwitchListener:
    """Consumes KILL_SWITCH_* events and calls execution-local callbacks."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        on_activate: Callable[..., Awaitable[None]],
        on_clear: Callable[..., Awaitable[None]],
        aws_region: str = "ap-south-1",
        consumer_group: str = _CONSUMER_GROUP,
    ) -> None:
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError("confluent-kafka is required for execution kill-switch listener")
        self._bootstrap_servers = bootstrap_servers
        self._aws_region = aws_region
        self._consumer_group = consumer_group
        self._on_activate = on_activate
        self._on_clear = on_clear
        self._consumer: Optional["Consumer"] = None
        self._failure_publisher: Optional[KafkaFailurePublisher] = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._consumer = self._build_consumer()
        self._failure_publisher = KafkaFailurePublisher(
            bootstrap_servers=self._bootstrap_servers,
            aws_region=self._aws_region,
            source_service="execution_engine",
        )
        self._failure_publisher.start()
        self._consumer.subscribe([_TOPIC])
        self._running = True
        logger.info(
            "KafkaExecutionKillSwitchListener started (group=%s, topic=%s)",
            self._consumer_group,
            _TOPIC,
        )

    async def stop(self) -> None:
        self._running = False
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None
        if self._failure_publisher is not None:
            self._failure_publisher.close()
            self._failure_publisher = None

    async def listen(self) -> None:
        logger.info("KafkaExecutionKillSwitchListener.listen started")
        while self._running:
            try:
                event = await asyncio.to_thread(self._poll_once)
                if event is None:
                    continue
                raw_message = event.pop("_raw_message", None)
                event_type = event.get("event_type")
                reason = event.get("reason", "Kill switch event received")
                activated_by = event.get("activated_by", "risk.kill-switch")

                if event_type == EventType.KILL_SWITCH_ACTIVE.value:
                    await self._on_activate(reason=reason, activated_by=activated_by)
                elif event_type == EventType.KILL_SWITCH_CLEARED.value:
                    await self._on_clear(reason=reason, activated_by=activated_by)

                if raw_message is not None:
                    self._commit_message(raw_message)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("execution kill-switch listener error")
                await asyncio.sleep(1)
        logger.info("KafkaExecutionKillSwitchListener.listen stopped")

    def _poll_once(self) -> Optional[dict[str, Any]]:
        if self._consumer is None or not self._running:
            return None
        msg = self._consumer.poll(0.5)
        if msg is None:
            return None
        if msg.error():
            err = msg.error()
            if err.code() == KafkaError._PARTITION_EOF:
                return None
            logger.error("Execution kill-switch Kafka error: %s", err)
            return None

        try:
            body = json.loads(msg.value().decode("utf-8"))
            event_type = body.get("event_type")
            if event_type not in (
                EventType.KILL_SWITCH_ACTIVE.value,
                EventType.KILL_SWITCH_CLEARED.value,
            ):
                self._route_dlq(msg, f"Unexpected event_type {event_type!r}")
                return None
            errors = validate_event(body, event_type)
            if errors:
                self._route_dlq(msg, "; ".join(errors))
                return None
            body["_raw_message"] = msg
            return body
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
            self._route_dlq(msg, str(exc))
            return None

    def _route_dlq(self, msg: Any, reason: str) -> None:
        if self._failure_publisher is None:
            return
        routed = self._failure_publisher.publish_dlq(
            source_topic=msg.topic() or _TOPIC,
            key=msg.key() if msg.key() else None,
            value=msg.value() if msg.value() else None,
            reason=reason,
            error_type="execution_kill_switch_event_invalid",
            details={
                "partition": msg.partition() if hasattr(msg, "partition") else None,
                "offset": msg.offset() if hasattr(msg, "offset") else None,
            },
        )
        if routed:
            self._commit_message(msg)

    def _commit_message(self, msg: Any) -> None:
        if self._consumer is not None:
            self._consumer.commit(message=msg, asynchronous=False)

    def _build_consumer(self) -> "Consumer":
        region = self._aws_region
        return Consumer({
            "bootstrap.servers": self._bootstrap_servers,
            **get_kafka_auth_config(region),
            "group.id": self._consumer_group,
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "session.timeout.ms": 30000,
            "heartbeat.interval.ms": 10000,
            "max.poll.interval.ms": 300000,
            "socket.connection.setup.timeout.ms": 15000,
            "log.connection.close": False,
        })
