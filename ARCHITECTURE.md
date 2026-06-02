# QuantEmbrace Architecture — Source of Truth

> This document defines the canonical system architecture. Any code that
> contradicts it is a defect. Any proposal to change it requires an ADR.

---

## Approved Signal Flow

```
Zerodha WebSocket / Alpaca WebSocket
          │
          ▼
  data_ingestion/service.py
  KafkaTickPublisher.publish()
          │
     ┌────┴────┐
     │ Kafka   │  ticks.nse  (2 partitions, key=symbol)
     │         │  ticks.us   (2 partitions, key=symbol)
     └────┬────┘
          │
          ▼
  strategy_engine/service.py
  KafkaTickConsumer (group: strategy-v1)
  → Signal generation
  KafkaSignalPublisher.publish()
          │
     ┌────┴────┐
     │ Kafka   │  signals.pending  (2 partitions, key=signal_id)
     └────┬────┘
          │
          ▼
  risk_engine/service.py
  KafkaSignalConsumer (group: risk-v1)
  → Risk validation (position, exposure, margin, signal age)
  KafkaApprovedPublisher.publish()
          │
     ┌────┴────┐
     │ Kafka   │  signals.approved  (2 partitions, key=instrument_id)
     └────┬────┘
          │
          ▼
  execution_engine/service.py
  KafkaApprovedConsumer (group: execution-v1)
  → Broker order placement (Zerodha / Alpaca)
  KafkaOrderEventsPublisher.publish_fill()
          │
     ┌────┴────┐
     │ Kafka   │  orders.events  (2 partitions, key=order_id)
     └────┬────┘
          │
          ▼
  risk_engine (risk-orders-v1 consumer)
  → Real-time P&L update, position state, NAV
```

---

## Kafka Topics

| Topic              | Partitions | Key            | Consumers            | Retention |
|--------------------|-----------|----------------|----------------------|-----------|
| `ticks.nse`        | 2         | symbol         | strategy-v1, risk-v1 | 1h dev / 4h prod |
| `ticks.us`         | 2         | symbol         | strategy-v1          | 1h dev / 4h prod |
| `signals.pending`  | 2         | signal_id      | risk-v1              | 10m dev / 30m prod |
| `signals.approved` | 2         | instrument_id  | execution-v1         | 5m dev / 15m prod |
| `orders.events`    | 2         | order_id       | risk-v1              | 24h |
| `kill.switch`      | 1         | —              | all services         | 24h |
| `ops.audit`        | 1         | service        | (logging only)       | 7d |

---

## Schema Version

All events use **schema_version: "3.0"** (frozen). Envelope fields:

```json
{
  "event_id":       "<uuid4>",
  "trace_id":       "<propagated from originating tick>",
  "event_type":     "TICK_NSE | SIGNAL_PENDING | SIGNAL_APPROVED | ORDER_FILLED | ...",
  "schema_version": "3.0",
  "source":         "<service_name>",
  "published_time": "<ISO8601 UTC>"
}
```

---

## Forbidden Patterns

These patterns are **banned by ruff TID251** (`pyproject.toml`). A CI check will
fail any PR that introduces them.

| Pattern | Why Forbidden |
|---------|--------------|
| `boto3.client("sqs")` | SQS is not in the trading path |
| `get_sqs_client()` | Removed from `shared/aws/clients.py` |
| `SQSTickPublisher` | Deleted — replaced by `KafkaTickPublisher` |
| `SQSSignalPublisher` | Deleted — replaced by `KafkaSignalPublisher` |
| `PHASE2_KAFKA_ENABLED` | Migration flag — fully deleted |
| Direct strategy→execution communication | Violates risk gate requirement |

---

## Service Boundaries

| Service            | Produces to           | Consumes from         |
|--------------------|-----------------------|-----------------------|
| `data_ingestion`   | `ticks.nse`, `ticks.us` | Broker WebSockets   |
| `strategy_engine`  | `signals.pending`     | `ticks.nse`, `ticks.us` |
| `risk_engine`      | `signals.approved`    | `signals.pending`, `orders.events` |
| `execution_engine` | `orders.events`       | `signals.approved`   |
| `ai_engine`        | HTTP responses        | S3 (features)        |

---

## Mandatory Environment Variables

Every service raises `RuntimeError` on startup if `KAFKA_BOOTSTRAP_SERVERS` is
not set. This is intentional — a service that cannot reach Kafka must not start.

| Variable                  | Required By             |
|---------------------------|-------------------------|
| `KAFKA_BOOTSTRAP_SERVERS` | All 4 trading services  |
| `AWS_REGION`              | All services (IAM auth) |
| `ZERODHA_API_KEY`         | execution_engine only   |
| `ALPACA_API_KEY`          | execution_engine only   |

---

## IAM & Auth

MSK Serverless uses **SASL/OAUTHBEARER + IAM** on port 9098. Each service's EC2
instance role has an inline IAM policy granting `kafka-cluster:*` on the cluster
ARN. The `aws-msk-iam-sasl-signer-python` library generates short-lived tokens.

SQS IAM policies have been **removed** from all service roles.

---

## Adding a New Inter-Service Communication Channel

1. Define a new Kafka topic in `infra/terraform/modules/kafka/main.tf`
2. Add topic retention and partition config
3. Write a new publisher + consumer pair following the existing pattern
4. Update schema_version if the envelope changes (requires ADR)
5. Add the topic to this document's table above

**Do not use SQS, SNS, or HTTP for real-time trading data flows.**
HTTP is acceptable only for the AI engine's inference API (request/response, not streaming).
