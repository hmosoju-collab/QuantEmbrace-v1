"""Canonical event contracts shared by trading services."""

from shared.events.schemas import (
    CANONICAL_KILL_SWITCH_TOPIC,
    CANONICAL_ORDER_EVENTS_TOPIC,
    CANONICAL_SIGNALS_APPROVED_TOPIC,
    CANONICAL_SIGNALS_PENDING_TOPIC,
    SCHEMA_VERSION,
    EventType,
    TopicNames,
    dlq_topic,
    retry_topic,
    validate_event,
)

__all__ = [
    "CANONICAL_KILL_SWITCH_TOPIC",
    "CANONICAL_ORDER_EVENTS_TOPIC",
    "CANONICAL_SIGNALS_APPROVED_TOPIC",
    "CANONICAL_SIGNALS_PENDING_TOPIC",
    "SCHEMA_VERSION",
    "EventType",
    "TopicNames",
    "dlq_topic",
    "retry_topic",
    "validate_event",
]
