"""AlphaShadowPublisher — the ONLY Kafka egress of the Alpha Engine.

Hard shadow-isolation boundary (ADR-031 governance): this publisher may produce
to exactly one topic — ``alpha.opportunities``. Any attempt to publish elsewhere
(``signals.pending``, ``signals.approved``, ``orders.*``, …) raises
``ShadowIsolationError``. The allowlist is a module-level ``frozenset`` and is
unit-tested. No broker SDK is imported anywhere under ``services/alpha_engine``.

Every event carries ``shadow_mode: true`` so any accidental downstream consumer
can hard-assert this stream never authorizes a trade.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import uuid

from shared.events.schemas import (
    ALPHA_SCHEMA_VERSION,
    CANONICAL_ALPHA_OPPORTUNITIES_TOPIC,
    EventType,
    validate_event,
)
from shared.logging.logger import get_logger
from shared.models.alpha import AlphaOpportunity

logger = get_logger(__name__, service_name="alpha_engine")

# The single permitted egress topic. Enforced on every produce.
ALLOWED_TOPICS: frozenset[str] = frozenset({CANONICAL_ALPHA_OPPORTUNITIES_TOPIC})

# ── Import guard (mirror of the strategy_engine publisher) ─────────────────────
try:
    from confluent_kafka import KafkaException, Producer

    _CONFLUENT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without confluent-kafka
    _CONFLUENT_AVAILABLE = False
    logger.warning(
        "confluent-kafka not installed — AlphaShadowPublisher will not function. "
        "Install: pip install confluent-kafka~=2.3"
    )

from shared.kafka.config import get_kafka_auth_config  # noqa: E402


class ShadowIsolationError(RuntimeError):
    """Raised when the Alpha Engine attempts to publish outside its allowlist.

    This is a governance failure, not a transient error — it must surface loudly.
    """


def build_alpha_opportunity_event(
    opportunity: AlphaOpportunity, *, source: str = "alpha_engine"
) -> dict:
    """Build the v1.0 ALPHA_OPPORTUNITY envelope from a ranked opportunity."""
    f = opportunity.forecast
    now_utc = datetime.now(UTC)
    decision_ts = f.decision_ts
    if decision_ts.tzinfo is None:
        decision_ts = decision_ts.replace(tzinfo=UTC)
    expires_at = decision_ts + timedelta(minutes=f.horizon_minutes)

    return {
        "event_id": str(uuid.uuid4()),
        "trace_id": f.trace_id or str(uuid.uuid4()),
        "event_type": EventType.ALPHA_OPPORTUNITY.value,
        "schema_version": ALPHA_SCHEMA_VERSION,
        "source": source,
        "published_time": now_utc.isoformat(),
        "forecast_id": f.forecast_id,
        "cycle_id": opportunity.cycle_id,
        "rank": opportunity.rank,
        "score": opportunity.score,
        "model_id": f.model_id,
        "model_version": f.model_version,
        "alpha_family": f.alpha_family,
        "symbol": f.symbol,
        "instrument_id": f.instrument_id,
        "market": f.market,
        "universe": f.universe,
        "timeframe": f.timeframe,
        "direction": f.direction.value,
        "horizon_minutes": f.horizon_minutes,
        "forecast_return_bps": f.forecast_return_bps,
        "net_edge_bps": f.net_edge_bps,
        "edge_band": f.edge_band,
        "confidence": f.confidence,
        "top_features": [fc.to_dict() for fc in f.top_features],
        "conflict_group_id": opportunity.conflict_group_id,
        "decision_price": f.decision_price,
        "decision_ts": decision_ts.isoformat(),
        "expires_at": expires_at.isoformat(),
        "shadow_mode": True,
    }


class AlphaShadowPublisher:
    """Confluent-kafka producer locked to the ``alpha.opportunities`` topic."""

    def __init__(self, bootstrap_servers: str, aws_region: str = "ap-south-1") -> None:
        # Note: the confluent-kafka requirement is enforced in start(), not here,
        # so the shadow-isolation guard (publish()) is unit-testable without Kafka.
        self._bootstrap_servers = bootstrap_servers
        self._aws_region = aws_region
        self._producer: Producer | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required for AlphaShadowPublisher. "
                "Install: pip install confluent-kafka~=2.3"
            )
        self._producer = self._build_producer()
        self._running = True
        logger.info(
            "alpha_engine.shadow_publisher_started topic=%s",
            CANONICAL_ALPHA_OPPORTUNITIES_TOPIC,
        )

    async def stop(self) -> None:
        self._running = False
        if self._producer is not None:
            remaining = await asyncio.to_thread(self._producer.flush, 10)
            if remaining > 0:
                logger.warning(
                    "alpha_engine.shadow_publisher: %d opportunity(ies) undelivered at shutdown",
                    remaining,
                )
            logger.info("alpha_engine.shadow_publisher_stopped")

    async def publish_opportunity(self, opportunity: AlphaOpportunity) -> bool:
        """Publish one ranked opportunity to ``alpha.opportunities``."""
        event = build_alpha_opportunity_event(opportunity)
        return self.publish(
            topic=CANONICAL_ALPHA_OPPORTUNITIES_TOPIC,
            key=opportunity.forecast.instrument_id,
            event=event,
        )

    def publish(self, *, topic: str, key: str, event: dict) -> bool:
        """Single guarded produce path. Refuses any topic outside the allowlist.

        Raises:
            ShadowIsolationError: if ``topic`` is not ``alpha.opportunities``.
        """
        # ── Governance guards first (independent of producer readiness) ──────
        if topic not in ALLOWED_TOPICS:
            raise ShadowIsolationError(
                f"alpha_engine is shadow-only and may publish ONLY to "
                f"{sorted(ALLOWED_TOPICS)}; refused {topic!r}. "
                "The Alpha Engine must never reach signals.* / orders.*."
            )
        if event.get("shadow_mode") is not True:
            raise ShadowIsolationError("alpha.opportunities event missing shadow_mode=true")
        errors = validate_event(event, EventType.ALPHA_OPPORTUNITY)
        if errors:
            logger.error("alpha_engine.shadow_event_invalid %s", "; ".join(errors))
            return False

        if not self._producer:
            logger.error("alpha_engine.shadow_publisher.publish called before start()")
            return False

        try:
            self._producer.produce(
                topic=topic,
                key=key.encode("utf-8"),
                value=json.dumps(event, default=str).encode("utf-8"),
            )
            self._producer.poll(0)
            return True
        except KafkaException as exc:
            logger.error("alpha_engine.shadow_publish_error %s: %s", key, exc)
            return False
        except Exception:
            logger.exception("alpha_engine.shadow_publish_unexpected_error %s", key)
            return False

    def _build_producer(self) -> Producer:
        conf = {
            "bootstrap.servers": self._bootstrap_servers,
            **get_kafka_auth_config(self._aws_region),
            "acks": "all",
            "enable.idempotence": True,
            "max.in.flight.requests.per.connection": 1,
            "retries": 5,
            "retry.backoff.ms": 200,
            "compression.type": "lz4",
            "batch.size": 4096,
            "linger.ms": 5,
            "delivery.timeout.ms": 30000,
            "socket.connection.setup.timeout.ms": 15000,
            "log.connection.close": False,
        }
        return Producer(conf)
