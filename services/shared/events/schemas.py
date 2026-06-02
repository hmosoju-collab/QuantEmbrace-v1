"""Canonical Kafka event schemas for the trading lifecycle.

This module is intentionally lightweight: it formalizes the fields that must be
present on every critical trading event without introducing a schema registry
runtime dependency. Producers build events with these constants; consumers use
``validate_event`` before acting on messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

SCHEMA_VERSION = "3.0"
ENRICHED_SCHEMA_VERSION = "4.0"  # Phase 6 — signals.enriched topic

CANONICAL_SIGNALS_PENDING_TOPIC  = "signals.pending"
CANONICAL_SIGNALS_ENRICHED_TOPIC = "signals.enriched"  # Phase 6 — ai_engine publishes here
CANONICAL_SIGNALS_APPROVED_TOPIC = "signals.approved"
CANONICAL_ORDER_EVENTS_TOPIC     = "orders.events"
CANONICAL_KILL_SWITCH_TOPIC      = "risk.kill-switch"


class EventType(str, Enum):
    """Canonical trading event type names."""

    SIGNAL_PENDING  = "SIGNAL_PENDING"
    SIGNAL_ENRICHED = "SIGNAL_ENRICHED"   # Phase 6 — ai_engine enriched signal (v4.0)
    SIGNAL_APPROVED = "SIGNAL_APPROVED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_PLACED    = "ORDER_PLACED"
    ORDER_PARTIAL   = "ORDER_PARTIAL"
    ORDER_FILLED    = "ORDER_FILLED"
    ORDER_REJECTED  = "ORDER_REJECTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    KILL_SWITCH_ACTIVE  = "KILL_SWITCH_ACTIVE"
    KILL_SWITCH_CLEARED = "KILL_SWITCH_CLEARED"


@dataclass(frozen=True)
class TopicNames:
    """Primary, retry, and DLQ topic names for one event stream."""

    primary: str

    @property
    def retry(self) -> str:
        return retry_topic(self.primary)

    @property
    def dlq(self) -> str:
        return dlq_topic(self.primary)


def retry_topic(topic: str) -> str:
    """Return the retry topic for a primary Kafka topic."""
    return f"{topic}.retry"


def dlq_topic(topic: str) -> str:
    """Return the dead-letter topic for a primary Kafka topic."""
    return f"{topic}.dlq"


BASE_REQUIRED_FIELDS = frozenset(
    {
        "event_id",
        "trace_id",
        "event_type",
        "schema_version",
        "source",
        "published_time",
    }
)

SIGNAL_REQUIRED_FIELDS = BASE_REQUIRED_FIELDS | frozenset(
    {
        "signal_id",
        "strategy_id",
        "strategy_name",
        "instrument_id",
        "market",
        "direction",
        "quantity",
        "price_at_signal",
        "confidence",
        "signal_time",
        "expires_at",
        "stop_loss",
        "take_profit",
        "product_type",
        "paper_trade",
        "metadata",
    }
)

SIGNAL_ENRICHED_REQUIRED_FIELDS = SIGNAL_REQUIRED_FIELDS | frozenset(
    {
        # Phase 6 enrichment fields — must all be present even if degraded
        "regime",
        "regime_confidence",
        "quality_score",
        "filtered",
        "enriched_at",
        "enrichment_latency_ms",
        "model_versions",
    }
)

SIGNAL_APPROVED_REQUIRED_FIELDS = SIGNAL_REQUIRED_FIELDS | frozenset(
    {
        "risk_decision_id",
        "approved_at",
    }
)

ORDER_REQUIRED_FIELDS = BASE_REQUIRED_FIELDS | frozenset(
    {
        "order_id",
        "signal_id",
        "risk_decision_id",
        "strategy_id",
        "instrument_id",
        "market",
        "direction",
        "quantity_ordered",
        "quantity_filled",
        "avg_fill_price",
        "broker_order_id",
        "fill_time",
        "reject_reason",
        "product_type",
        "expires_at",
        "stop_loss",
        "take_profit",
    }
)

KILL_SWITCH_REQUIRED_FIELDS = BASE_REQUIRED_FIELDS | frozenset(
    {
        "reason",
        "activated_by",
    }
)

EVENT_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    EventType.SIGNAL_PENDING.value:  SIGNAL_REQUIRED_FIELDS,
    EventType.SIGNAL_ENRICHED.value: SIGNAL_ENRICHED_REQUIRED_FIELDS,   # Phase 6
    EventType.SIGNAL_APPROVED.value: SIGNAL_APPROVED_REQUIRED_FIELDS,
    EventType.ORDER_SUBMITTED.value: ORDER_REQUIRED_FIELDS,
    EventType.ORDER_PLACED.value: ORDER_REQUIRED_FIELDS,
    EventType.ORDER_PARTIAL.value: ORDER_REQUIRED_FIELDS,
    EventType.ORDER_FILLED.value: ORDER_REQUIRED_FIELDS,
    EventType.ORDER_REJECTED.value: ORDER_REQUIRED_FIELDS,
    EventType.ORDER_CANCELLED.value: ORDER_REQUIRED_FIELDS,
    EventType.KILL_SWITCH_ACTIVE.value: KILL_SWITCH_REQUIRED_FIELDS,
    EventType.KILL_SWITCH_CLEARED.value: KILL_SWITCH_REQUIRED_FIELDS,
}


def validate_event(event: dict[str, Any], expected_type: str | EventType) -> list[str]:
    """
    Validate required canonical fields for a Kafka event.

    Returns a list of human-readable errors. An empty list means the envelope is
    safe for the consumer-specific parser to process. Fields may explicitly be
    ``None`` where the schema permits nullable values, for example ``stop_loss``
    on a rejected or non-protective event; the key still has to exist so schema
    drift is visible.
    """
    expected = expected_type.value if isinstance(expected_type, EventType) else expected_type
    errors: list[str] = []

    # SIGNAL_ENRICHED uses schema v4.0; all others use v3.0
    expected_sv = (
        ENRICHED_SCHEMA_VERSION
        if expected == EventType.SIGNAL_ENRICHED.value
        else SCHEMA_VERSION
    )
    if event.get("schema_version") != expected_sv:
        errors.append(
            f"schema_version must be {expected_sv!r}, got {event.get('schema_version')!r}"
        )

    if event.get("event_type") != expected:
        errors.append(f"event_type must be {expected!r}, got {event.get('event_type')!r}")

    required = EVENT_REQUIRED_FIELDS.get(expected)
    if required is None:
        errors.append(f"unknown expected event type {expected!r}")
        return errors

    missing = sorted(field for field in required if field not in event)
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")

    return errors
