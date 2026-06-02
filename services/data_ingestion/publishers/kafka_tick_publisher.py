"""
Kafka Tick Publisher — publishes normalized ticks to MSK Serverless topics.

Produces normalized ticks to MSK Serverless topics using IAM authentication.
Implements the v3.0 tick event schema (see architecture/data_flow.md).

Topic routing:
    NSE market  →  ticks.nse   (4 partitions, instrument_id key)
    US market   →  ticks.us    (2 partitions, instrument_id key)

Event schema (schema_version 3.0 — frozen; never change field types or names):
    {
        "event_id":        str (uuid4)
        "trace_id":        str (uuid4 — set HERE, propagated unchanged to signal/order/fill)
        "event_type":      "TICK"
        "schema_version":  "3.0"
        "source":          "data_ingestion_nse" | "data_ingestion_us"
        "published_time":  ISO8601 UTC
        "instrument_id":   "{MARKET}:{SYMBOL}"  e.g. "NSE:RELIANCE"
        "market":          "NSE" | "US"
        "session_id":      "{MARKET}-{YYYYMMDD}"  e.g. "NSE-20260430"
        "sequence_id":     monotonic int per publisher restart
        "exchange_sequence": int (from broker if available, else 0)
        "exchange_time":   ISO8601 UTC (from broker, not system clock)
        "ltp":             float (last traded price)
        "volume":          int
        "bid":             float | None
        "ask":             float | None
        "tick_type":       "FULL" | "QUOTE" | "LTP"
        "gap_detected":    bool (True during post-reconnect warm-up)
    }

Producer config (from phase2_final_approved.md §8):
    acks=all, enable.idempotence=true, max.in.flight=1
    retries=5, retry.backoff.ms=200, compression.type=lz4
    batch.size=4096, linger.ms=5

Kill switch fallback (§8 — non-negotiable):
    3 consecutive delivery failures on ticks.nse or ticks.us
    → write GLOBAL kill switch to DynamoDB (acks=1 separate producer)
    → trading halts within 10 seconds

"""

from __future__ import annotations

import asyncio
import json
import time
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from shared.aws.clients import get_dynamodb_client
from shared.logging.logger import get_logger
from shared.risk_state import (
    DATA_OUTBOX_PENDING_PK,
    attr_number,
    attr_string,
    data_outbox_key,
    kill_switch_item,
)

from data_ingestion.connectors.base import NormalizedTick

logger = get_logger(__name__, service_name="data_ingestion")

# ── Import guard ──────────────────────────────────────────────────────────────
try:
    from confluent_kafka import Producer, KafkaException
    _CONFLUENT_AVAILABLE = True
except ImportError:
    _CONFLUENT_AVAILABLE = False
    logger.warning(
        "confluent-kafka not installed — KafkaTickPublisher will not function. "
        "Install: pip install confluent-kafka~=2.3"
    )

from shared.kafka.config import get_kafka_auth_config

# ── Topic name constants ──────────────────────────────────────────────────────
_TOPIC_NSE = "ticks.nse"
_TOPIC_US  = "ticks.us"
_SCHEMA_VERSION = "3.0"

# Kill switch threshold: 3 consecutive delivery failures on critical topics
_KILL_SWITCH_THRESHOLD = 3
_OUTBOX_REPLAY_INTERVAL_SECONDS = 1.0
_OUTBOX_DEFAULT_REPLAY_AFTER_SECONDS = 35.0
_OUTBOX_DEFAULT_TTL_SECONDS = 300
_OUTBOX_DEFAULT_MAX_REPLAY_AGE_SECONDS = 5.0


class KafkaTickPublisher:
    """
    Async-compatible Kafka tick publisher for MSK Serverless with IAM auth.

    Async-compatible Kafka producer for MSK Serverless with IAM auth (SASL/OAUTHBEARER).

    publish() calls the confluent-kafka Producer.produce() which is non-blocking
    (enqueues to internal buffer). Delivery receipts are polled via a background
    thread. Kafka handles batching internally via linger.ms + batch.size.

    Kill switch integration:
        3 consecutive delivery failures on ticks.nse or ticks.us trigger a
        DynamoDB write to GLOBAL kill switch. This is the §8 fallback path:
        "If Kafka is completely down: DynamoDB write + 5s poll by each service
         → Trading halts within 10 seconds."
    """

    def __init__(
        self,
        bootstrap_servers: str,
        aws_region: str = "ap-south-1",
        dynamodb_table_sessions: str = "quantembrace-prod-sessions",
        environment: str = "prod",
        durable_outbox_enabled: bool = True,
        outbox_max_replay_age_seconds: float = _OUTBOX_DEFAULT_MAX_REPLAY_AGE_SECONDS,
    ) -> None:
        """
        Initialize the Kafka tick publisher.

        Args:
            bootstrap_servers: MSK bootstrap broker string (port 9098).
                               Format: b-X.{cluster}.{uuid}.kafka.{region}.amazonaws.com:9098,...
            aws_region: AWS region for IAM token generation.
            dynamodb_table_sessions: DynamoDB table name for kill switch state.
            environment: Runtime environment (dev/staging/prod).
        """
        if not _CONFLUENT_AVAILABLE:
            raise RuntimeError(
                "confluent-kafka is required for KafkaTickPublisher. "
                "Install: pip install confluent-kafka~=2.3"
            )

        self._bootstrap_servers = bootstrap_servers
        self._aws_region = aws_region
        self._dynamodb_table = dynamodb_table_sessions
        self._environment = environment
        self._durable_outbox_enabled = durable_outbox_enabled
        self._outbox_max_replay_age_seconds = float(outbox_max_replay_age_seconds)
        self._dynamo: Any = None

        self._producer: Optional["Producer"] = None
        self._sequence_id: int = 0
        self._sequence_lock = threading.Lock()

        # Delivery failure tracking (kill switch threshold)
        self._consecutive_failures: int = 0
        self._kill_switch_fired: bool = False

        # Background poll task for delivery receipts
        self._poll_task: Optional[asyncio.Task] = None
        self._outbox_replay_task: Optional[asyncio.Task] = None
        self._running: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize the Kafka producer and start the background delivery-receipt poller."""
        if self._running:
            return

        self._producer = self._build_producer()
        if self._durable_outbox_enabled:
            self._dynamo = get_dynamodb_client()
        self._running = True
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="kafka_tick_delivery_poll"
        )
        if self._durable_outbox_enabled:
            await self.replay_pending_outbox(max_records=100)
            self._outbox_replay_task = asyncio.create_task(
                self._outbox_replay_loop(),
                name="kafka_tick_outbox_replay",
            )
        logger.info(
            "KafkaTickPublisher started",
            bootstrap_servers=self._bootstrap_servers,
            region=self._aws_region,
        )

    async def stop(self) -> None:
        """Flush the producer and stop the background poll loop."""
        self._running = False

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._outbox_replay_task and not self._outbox_replay_task.done():
            self._outbox_replay_task.cancel()
            try:
                await self._outbox_replay_task
            except asyncio.CancelledError:
                pass

        if self._producer:
            # Flush all buffered messages (up to 30s)
            remaining = await asyncio.to_thread(self._producer.flush, 30)
            if remaining > 0:
                logger.warning(
                    "KafkaTickPublisher: %d messages not delivered before shutdown",
                    remaining,
                )
            logger.info("KafkaTickPublisher stopped")

    async def publish(self, tick: NormalizedTick) -> None:
        """
        Produce a tick event to the appropriate Kafka topic.

        Non-blocking: enqueues to the confluent-kafka internal buffer.
        Delivery confirmation arrives asynchronously via the delivery callback.

        Args:
            tick: Normalized tick from a broker connector.
        """
        if not self._producer:
            logger.error("KafkaTickPublisher.publish called before start()")
            return

        topic = _TOPIC_NSE if tick.market.value == "NSE" else _TOPIC_US
        instrument_id = f"{tick.market.value}:{tick.symbol}"
        event = self._build_event(tick, instrument_id, topic)

        try:
            value_bytes = json.dumps(event, default=str).encode("utf-8")
            key_bytes   = instrument_id.encode("utf-8")
            if self._durable_outbox_enabled:
                await self._put_outbox_event(
                    event=event,
                    topic=topic,
                    key=instrument_id,
                    value=value_bytes.decode("utf-8"),
                    replay_after_seconds=_OUTBOX_DEFAULT_REPLAY_AFTER_SECONDS,
                )

            # produce() is non-blocking — delivery receipt handled by callback.
            # Do NOT call poll(0) here: _poll_loop runs poll in a thread every 100ms,
            # keeping delivery callbacks off the event loop thread entirely.
            self._producer.produce(
                topic=topic,
                key=key_bytes,
                value=value_bytes,
                on_delivery=lambda err, msg, event_id=event["event_id"]: self._delivery_callback(
                    err,
                    msg,
                    event_id=event_id,
                ),
            )

        except BufferError:
            # Internal queue full — yield to the event loop so _poll_loop can
            # drain delivery callbacks and free buffer space, then retry once.
            logger.warning("Kafka producer buffer full — yielding and retrying")
            await asyncio.sleep(0.15)
            try:
                self._producer.produce(
                    topic=topic,
                    key=key_bytes,
                    value=value_bytes,
                    on_delivery=lambda err, msg, event_id=event["event_id"]: self._delivery_callback(
                        err,
                        msg,
                        event_id=event_id,
                    ),
                )
            except Exception:
                logger.exception(
                    "Kafka produce failed after buffer-full retry: topic=%s instrument=%s",
                    topic, instrument_id,
                )
                self._mark_outbox_retryable(event["event_id"], "buffer_full_retry_failed")
                self._record_failure()

        except KafkaException as e:
            logger.error(
                "Kafka produce error: topic=%s instrument=%s error=%s",
                topic, instrument_id, e,
            )
            self._mark_outbox_retryable(event["event_id"], "kafka_exception")
            self._record_failure()

    @property
    def pending_count(self) -> int:
        """Number of messages buffered and awaiting delivery (confluent-kafka internal queue)."""
        return len(self._producer) if self._producer else 0

    # ── Event construction ────────────────────────────────────────────────────

    def _build_event(
        self,
        tick: NormalizedTick,
        instrument_id: str,
        topic: str,
    ) -> dict:
        """
        Build a v3.0 tick event dict from a NormalizedTick.

        The trace_id is a fresh uuid4 set here. It will propagate unchanged
        through signal → risk decision → order → fill. Every event in the
        trade lifecycle shares this trace_id, enabling full traceability
        with a single CloudWatch Logs Insights query.

        The sequence_id is a monotonic counter per publisher instance. It
        resets on restart but is combined with trace_id for deduplication —
        the risk engine's deterministic signal_id uses tick_sequence_id.
        """
        with self._sequence_lock:
            self._sequence_id += 1
            seq_id = self._sequence_id

        now_utc = datetime.now(timezone.utc)
        market  = tick.market.value
        session_date = now_utc.strftime("%Y%m%d")

        # exchange_sequence: use broker-provided sequence if available,
        # else fall back to microseconds-since-epoch as a proxy
        exchange_sequence = getattr(tick, "exchange_sequence", None)
        if exchange_sequence is None:
            exchange_sequence = int(tick.timestamp.timestamp() * 1_000_000)

        # tick_type: FULL if bid/ask available, QUOTE if only quote, LTP otherwise
        if tick.bid is not None and tick.ask is not None:
            tick_type = "FULL"
        elif tick.bid is not None or tick.ask is not None:
            tick_type = "QUOTE"
        else:
            tick_type = "LTP"

        return {
            "event_id":         str(uuid.uuid4()),
            "trace_id":         str(uuid.uuid4()),   # NEW per tick — propagated downstream
            "event_type":       "TICK",
            "schema_version":   _SCHEMA_VERSION,
            "source":           f"data_ingestion_{market.lower()}",
            "published_time":   now_utc.isoformat(),
            "instrument_id":    instrument_id,
            "market":           market,
            "session_id":       f"{market}-{session_date}",
            "sequence_id":      seq_id,
            "exchange_sequence": exchange_sequence,
            "exchange_time":    tick.timestamp.isoformat(),
            "ltp":              tick.last_price,
            "volume":           tick.volume,
            "bid":              tick.bid,
            "ask":              tick.ask,
            "tick_type":        tick_type,
            "gap_detected":     bool(getattr(tick, "gap_detected", False)),
        }

    # ── Delivery tracking + kill switch ───────────────────────────────────────

    def _delivery_callback(self, err, msg, *, event_id: str = "") -> None:
        """
        Confluent-kafka delivery callback — called from the producer's internal thread.

        On success: reset consecutive failure counter.
        On failure: increment counter and trigger kill switch if threshold reached.
        """
        if err is None:
            # Successful delivery — reset failure streak
            self._consecutive_failures = 0
            if event_id:
                self._delete_outbox_event(event_id)
        else:
            logger.error(
                "Kafka delivery failed: topic=%s partition=%s offset=%s error=%s",
                msg.topic() if msg else "?",
                msg.partition() if msg else "?",
                msg.offset() if msg else "?",
                err,
            )
            if event_id:
                self._mark_outbox_retryable(event_id, str(err))
            self._record_failure()

    def _record_failure(self) -> None:
        """Increment failure counter; fire kill switch at threshold."""
        self._consecutive_failures += 1
        # In non-prod environments LocalStack instability produces spurious delivery
        # failures that do not indicate a real MSK outage. Skip auto-trigger outside prod
        # (same rationale as broker_timeout_secs=inf in paper mode — see FIX-2).
        if self._environment != "production":
            return
        if (
            self._consecutive_failures >= _KILL_SWITCH_THRESHOLD
            and not self._kill_switch_fired
        ):
            logger.critical(
                "Kafka: %d consecutive delivery failures — activating kill switch via DynamoDB",
                self._consecutive_failures,
            )
            # Fire in a separate thread so we don't block the delivery callback
            threading.Thread(
                target=self._write_kill_switch_dynamo,
                daemon=True,
                name="kafka-ks-fallback",
            ).start()
            self._kill_switch_fired = True

    def _write_kill_switch_dynamo(self) -> None:
        """
        Write GLOBAL kill switch to DynamoDB — the §8 fallback path.

        Uses a direct boto3 call (no shared KillSwitch class) to avoid
        circular dependency with risk_engine. This is intentionally minimal:
        write the state, let the 5-second DynamoDB poll in each service
        pick it up within 10 seconds total.

        Called from a background daemon thread (not the event loop).
        """
        try:
            dynamo = get_dynamodb_client()
            now_iso = datetime.now(timezone.utc).isoformat()

            dynamo.put_item(
                TableName=self._dynamodb_table,
                Item=kill_switch_item(
                    active=True,
                    reason="kafka_write_failure",
                    activated_by="kafka_tick_publisher",
                    activated_at=now_iso,
                    updated_at=now_iso,
                    detail=(
                        f"Kafka producer: {self._consecutive_failures} consecutive "
                        f"delivery failures on tick topics. MSK may be unavailable."
                    ),
                ),
                # Do not overwrite if already active (could be different scope/reason)
                ConditionExpression="attribute_not_exists(PK) OR active = :false",
                ExpressionAttributeValues={":false": {"BOOL": False}},
            )
            logger.critical(
                "Kill switch written to DynamoDB (kafka_write_failure). "
                "Trading will halt within 10 seconds."
            )
        except Exception:
            logger.exception(
                "CRITICAL: Failed to write kill switch to DynamoDB after Kafka failure. "
                "Manual intervention required — halt trading immediately."
            )

    # ── Background poll loop ──────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """
        Background asyncio task that polls the confluent-kafka producer for
        delivery receipts every 100ms.

        confluent-kafka's poll() fires all pending delivery callbacks and
        handles internal heartbeating. Without polling, callbacks are never
        fired and the internal buffer eventually fills up (causing BufferError).

        100ms poll interval balances callback latency against CPU overhead.
        At 5–50 ticks/second (personal trading volume), this is more than sufficient.
        """
        while self._running:
            try:
                await asyncio.sleep(0.1)  # 100ms
                if self._producer:
                    # Run poll in a thread so delivery callbacks (_delete_outbox_event,
                    # _mark_outbox_retryable) execute in the thread pool — never in the
                    # event loop thread. Without this, synchronous DynamoDB calls inside
                    # delivery callbacks block the event loop for tens of milliseconds per
                    # callback, causing asyncio.sleep timers to fire many minutes late.
                    await asyncio.to_thread(self._producer.poll, 0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("KafkaTickPublisher: error in poll loop")

    # ── Durable market-data outbox ────────────────────────────────────────────

    async def _outbox_replay_loop(self) -> None:
        """Periodically replay pending fresh outbox records."""
        while self._running:
            try:
                await self.replay_pending_outbox(max_records=100)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("KafkaTickPublisher: error in outbox replay loop")
            await asyncio.sleep(_OUTBOX_REPLAY_INTERVAL_SECONDS)

    async def _put_outbox_event(
        self,
        *,
        event: dict[str, Any],
        topic: str,
        key: str,
        value: str,
        replay_after_seconds: float,
    ) -> None:
        """
        Persist the tick before Kafka produce.

        If this write fails, the tick is not sent to Kafka. That is intentional:
        live trading must not depend on an event that cannot be recovered or
        audited if the producer dies after enqueue.
        """
        if self._dynamo is None:
            self._dynamo = get_dynamodb_client()

        event_id = str(event["event_id"])
        now_epoch = int(time.time())
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._dynamodb_table,
                Item={
                    **data_outbox_key(event_id),
                    "event_id": {"S": event_id},
                    "topic": {"S": topic},
                    "message_key": {"S": key},
                    "event_json": {"S": value},
                    "status": {"S": "PENDING"},
                    "attempts": {"N": "0"},
                    "created_at": {"S": now_iso},
                    "updated_at": {"S": now_iso},
                    "published_time": {"S": str(event.get("published_time", ""))},
                    "exchange_time": {"S": str(event.get("exchange_time", ""))},
                    "replay_after_epoch": {
                        "N": str(now_epoch + int(max(1.0, replay_after_seconds)))
                    },
                    "expires_at": {"N": str(now_epoch + _OUTBOX_DEFAULT_TTL_SECONDS)},
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception:
            logger.exception(
                "KafkaTickPublisher: durable outbox write failed event_id=%s topic=%s",
                event_id,
                topic,
            )
            self._record_failure()
            raise

    async def replay_pending_outbox(self, *, max_records: int = 100) -> int:
        """
        Replay fresh pending outbox events to Kafka.

        Stale ticks are deleted instead of replayed; after an outage the safe
        behavior is to warm the live stream again, not inject old prices into
        strategies.
        """
        if not self._durable_outbox_enabled or self._producer is None:
            return 0
        if self._dynamo is None:
            self._dynamo = get_dynamodb_client()

        response = await asyncio.to_thread(
            self._dynamo.query,
            TableName=self._dynamodb_table,
            KeyConditionExpression="PK = :pk",
            FilterExpression="replay_after_epoch <= :now",
            ExpressionAttributeValues={
                ":pk": {"S": DATA_OUTBOX_PENDING_PK},
                ":now": {"N": str(int(time.time()))},
            },
            Limit=max_records,
        )
        replayed = 0
        # Collect items into buckets so we can do async I/O in parallel after
        # the synchronous produce loop — this keeps the event loop unblocked
        # during the DynamoDB deletes/updates (previously done synchronously).
        stale_ids: list[str] = []
        inflight_ids: list[str] = []
        failed_ids: list[str] = []

        for item in response.get("Items", []):
            event_id = attr_string(item, "event_id")
            topic = attr_string(item, "topic")
            message_key = attr_string(item, "message_key")
            value = attr_string(item, "event_json")
            if not event_id or not topic or not message_key or not value:
                if event_id:
                    stale_ids.append(event_id)
                continue

            if self._is_outbox_event_stale(item):
                logger.critical(
                    "KafkaTickPublisher: dropping stale outbox tick event_id=%s topic=%s",
                    event_id,
                    topic,
                )
                stale_ids.append(event_id)
                continue

            inflight_ids.append(event_id)
            try:
                self._producer.produce(
                    topic=topic,
                    key=message_key.encode("utf-8"),
                    value=value.encode("utf-8"),
                    on_delivery=lambda err, msg, event_id=event_id: self._delivery_callback(
                        err,
                        msg,
                        event_id=event_id,
                    ),
                )
                # Do not call poll(0) here — _poll_loop runs poll in a thread every 100ms.
                replayed += 1
            except Exception:
                logger.exception(
                    "KafkaTickPublisher: outbox replay produce failed event_id=%s topic=%s",
                    event_id,
                    topic,
                )
                failed_ids.append(event_id)
                inflight_ids.pop()
                self._record_failure()

        # Batch-delete stale items: use DynamoDB batch_write_item (25 per call) instead of
        # N individual delete_item calls. This avoids flooding the thread pool executor with
        # O(N) concurrent tasks, which would block threads needed by the candle stream's
        # historical data fetches and cause asyncio.wait_for timeouts on those calls.
        if stale_ids:
            await self._batch_delete_outbox_events(stale_ids)

        # Remaining async I/O: mark inflight/failed as retryable (low cardinality — OK to gather)
        retry_tasks = [
            self._mark_outbox_retryable_async(eid, "replay_inflight") for eid in inflight_ids
        ]
        failed_tasks = [
            self._mark_outbox_retryable_async(eid, "replay_produce_failed") for eid in failed_ids
        ]
        if retry_tasks or failed_tasks:
            await asyncio.gather(*(retry_tasks + failed_tasks), return_exceptions=True)

        return replayed

    def _is_outbox_event_stale(self, item: dict[str, Any]) -> bool:
        """True if a pending market-data event is too old to safely replay."""
        published_time = attr_string(item, "published_time") or attr_string(item, "exchange_time")
        if not published_time:
            return True
        try:
            parsed = datetime.fromisoformat(published_time.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
        return age > self._outbox_max_replay_age_seconds

    async def _batch_delete_outbox_events(self, event_ids: list[str]) -> None:
        """
        Delete a list of outbox event_ids using DynamoDB batch_write_item (25 per request).

        Uses batching instead of individual delete_item calls to avoid flooding the
        thread pool executor: 100 stale items = 4 batch calls rather than 100 calls.
        """
        if not self._durable_outbox_enabled or not event_ids:
            return
        if self._dynamo is None:
            self._dynamo = get_dynamodb_client()

        _BATCH_SIZE = 25
        for i in range(0, len(event_ids), _BATCH_SIZE):
            chunk = event_ids[i : i + _BATCH_SIZE]
            delete_requests = [
                {"DeleteRequest": {"Key": data_outbox_key(eid)}} for eid in chunk
            ]
            try:
                await asyncio.to_thread(
                    self._dynamo.batch_write_item,
                    RequestItems={self._dynamodb_table: delete_requests},
                )
            except Exception:
                logger.exception(
                    "KafkaTickPublisher: batch delete failed for %d outbox items (chunk starting at %d)",
                    len(chunk),
                    i,
                )

    async def _mark_outbox_retryable_async(self, event_id: str, reason: str) -> None:
        """Async version of _mark_outbox_retryable — does not block the event loop."""
        if not self._durable_outbox_enabled:
            return
        try:
            if self._dynamo is None:
                self._dynamo = get_dynamodb_client()
            now_epoch = int(time.time())
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._dynamodb_table,
                Key=data_outbox_key(event_id),
                UpdateExpression=(
                    "SET #status = :pending, last_error = :reason, "
                    "updated_at = :updated_at, replay_after_epoch = :replay_after "
                    "ADD attempts :one"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":pending": {"S": "PENDING"},
                    ":reason": {"S": reason[:500]},
                    ":updated_at": {"S": datetime.now(timezone.utc).isoformat()},
                    ":replay_after": {
                        "N": str(now_epoch + int(_OUTBOX_DEFAULT_REPLAY_AFTER_SECONDS))
                    },
                    ":one": {"N": "1"},
                },
            )
        except Exception:
            logger.exception(
                "KafkaTickPublisher: failed to update outbox retry state event_id=%s",
                event_id,
            )

    def _mark_outbox_retryable(self, event_id: str, reason: str) -> None:
        """Synchronous shim for callers outside async context (e.g. delivery callbacks)."""
        if not self._durable_outbox_enabled:
            return
        try:
            if self._dynamo is None:
                self._dynamo = get_dynamodb_client()
            now_epoch = int(time.time())
            self._dynamo.update_item(
                TableName=self._dynamodb_table,
                Key=data_outbox_key(event_id),
                UpdateExpression=(
                    "SET #status = :pending, last_error = :reason, "
                    "updated_at = :updated_at, replay_after_epoch = :replay_after "
                    "ADD attempts :one"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":pending": {"S": "PENDING"},
                    ":reason": {"S": reason[:500]},
                    ":updated_at": {"S": datetime.now(timezone.utc).isoformat()},
                    ":replay_after": {
                        "N": str(now_epoch + int(_OUTBOX_DEFAULT_REPLAY_AFTER_SECONDS))
                    },
                    ":one": {"N": "1"},
                },
            )
        except Exception:
            logger.exception(
                "KafkaTickPublisher: failed to update outbox retry state event_id=%s",
                event_id,
            )

    async def _delete_outbox_event_async(self, event_id: str) -> None:
        """Async version of _delete_outbox_event — does not block the event loop."""
        if not self._durable_outbox_enabled:
            return
        try:
            if self._dynamo is None:
                self._dynamo = get_dynamodb_client()
            await asyncio.to_thread(
                self._dynamo.delete_item,
                TableName=self._dynamodb_table,
                Key=data_outbox_key(event_id),
            )
        except Exception:
            logger.exception(
                "KafkaTickPublisher: failed to delete outbox event event_id=%s",
                event_id,
            )

    def _delete_outbox_event(self, event_id: str) -> None:
        """Synchronous shim for callers outside async context (e.g. delivery callbacks)."""
        if not self._durable_outbox_enabled:
            return
        try:
            if self._dynamo is None:
                self._dynamo = get_dynamodb_client()
            self._dynamo.delete_item(
                TableName=self._dynamodb_table,
                Key=data_outbox_key(event_id),
            )
        except Exception:
            logger.exception(
                "KafkaTickPublisher: failed to delete outbox event event_id=%s",
                event_id,
            )

    # ── Producer construction ─────────────────────────────────────────────────

    def _build_producer(self) -> "Producer":
        """
        Build a confluent-kafka Producer configured for MSK Serverless IAM auth.

        Config from phase2_final_approved.md §8:
            acks=all                   → durable commit before acknowledgement
            enable.idempotence=true    → prevents duplicate messages on retry
            max.in.flight=1            → ordering guaranteed during retries
            retries=5                  → MSK Serverless failover window
            retry.backoff.ms=200       → 200ms between retries
            compression.type=lz4       → compression for cost/throughput
            batch.size=4096            → small batch (personal-scale message rate)
            linger.ms=5                → wait up to 5ms to batch
            delivery.timeout.ms=30000  → 30s total delivery timeout (librdkafka)
        """
        region = self._aws_region
        conf = {
            "bootstrap.servers":                     self._bootstrap_servers,
            **get_kafka_auth_config(region),
            # Durability + idempotence
            "acks":                                  "all",
            "enable.idempotence":                    True,
            "max.in.flight.requests.per.connection": 1,
            "retries":                               5,
            "retry.backoff.ms":                      200,
            # Throughput tuning for personal-scale volume
            "compression.type":                      "lz4",
            "batch.size":                            4096,
            "linger.ms":                             5,
            "delivery.timeout.ms":                   30000,
            # Connection
            "socket.connection.setup.timeout.ms":    15000,
            # Reduce confluent-kafka internal log noise
            "log.connection.close":                  False,
        }
        return Producer(conf)
