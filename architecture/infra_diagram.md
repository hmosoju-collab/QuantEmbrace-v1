# QuantEmbrace - AWS Infrastructure

## Overview

All infrastructure is provisioned and managed via Terraform. The system runs on **AWS EC2 ARM64** Auto Scaling Groups (Graviton3) with **MSK Serverless** as the Kafka backbone.

**Current Phase**: Phase 2 complete — EC2 ARM64 ASGs + Kafka MSK Serverless is the sole, active architecture

Terraform modules: `vpc`, `s3`, `dynamodb`, `monitoring`, `ec2_services`, `kafka`  
Modules `ecs` and `sqs` have been permanently removed.

---

## Infrastructure Diagram — Phase 2 (EC2 + Kafka MSK Serverless)

```
                          +---------------------------+
                          |        INTERNET           |
                          +-------------+-------------+
                                        |
                              HTTPS/WSS (Zerodha Kite, Alpaca)
                                        |
+=======================================+=======================================+
|                         AWS ACCOUNT (ap-south-1)                             |
|                                                                               |
|  +------------------------------VPC (10.x.0.0/16)-------------------------+  |
|  |                                                                         |  |
|  |  +------ AZ: ap-south-1a ----------+   +------ AZ: ap-south-1b ----+  |  |
|  |  |                                  |   |                           |  |  |
|  |  |   PUBLIC SUBNET                  |   |  PUBLIC SUBNET            |  |  |
|  |  |   ┌──────────┐                   |   |  ┌──────────┐            |  |  |
|  |  |   │  NAT GW  │                   |   |  │  NAT GW  │            |  |  |
|  |  |   │(primary) │                   |   |  │(standby) │            |  |  |
|  |  |   └────┬─────┘                   |   |  └────┬─────┘            |  |  |
|  |  |        │                          |   |       │                   |  |  |
|  |  +--------+--------------------------+   +-------+-------------------+  |  |
|  |           │                                      │                      |  |
|  |           │  NAT route (primary)                 │  NAT route (HA)     |  |
|  |           │                                      │                      |  |
|  |  +--------+--------------------------+   +-------+-------------------+  |  |
|  |  |   PRIVATE SUBNET (10.x.10.0/24)  |   |  PRIVATE SUBNET           |  |  |
|  |  |                                  |   |                           |  |  |
|  |  |  ┌── EC2 ASG: data-ingest-nse ─┐ |   |                           |  |  |
|  |  |  │  [t4g.medium] WebSocket NSE │ |   |                           |  |  |
|  |  |  │  spread placement group     │ |   |                           |  |  |
|  |  |  └─────────────────────────────┘ |   |                           |  |  |
|  |  |                                  |   |                           |  |  |
|  |  |  ┌── EC2 ASG: data-ingest-us ──┐ |   |                           |  |  |
|  |  |  │  [t4g.medium] WebSocket US  │ |   |                           |  |  |
|  |  |  │  spread placement group     │ |   |                           |  |  |
|  |  |  └─────────────────────────────┘ |   |                           |  |  |
|  |  |                                  |   |                           |  |  |
|  |  |  ┌── EC2 ASG: strategy-engine ─┐ |   |  ┌── ASG scale-out ────┐ |  |  |
|  |  |  │  [c6g.large] max_size=2     │ |   |  │ strategy-engine x2  │ |  |  |
|  |  |  └─────────────────────────────┘ |   |  └─────────────────────┘ |  |  |
|  |  |                                  |   |                           |  |  |
|  |  |  ┌── EC2 ASG: risk-engine ─────┐ |   |                           |  |  |
|  |  |  │  [c6g.large] min=1, max=1   │ |   |                           |  |  |
|  |  |  └─────────────────────────────┘ |   |                           |  |  |
|  |  |                                  |   |                           |  |  |
|  |  |  ┌── EC2 ASG: exec-engine ─────┐ |   |                           |  |  |
|  |  |  │  [c6g.xlarge] cluster PG    │ |   |                           |  |  |
|  |  |  │  min_size ALWAYS ≥ 1        │ |   |                           |  |  |
|  |  |  └─────────────────────────────┘ |   |                           |  |  |
|  |  |                                  |   |                           |  |  |
|  |  +----------------------------------+   +---------------------------+  |  |
|  |                                                                         |  |
|  |  ┌─────────────────────────────────────────────────────────────────┐   |  |
|  |  │  MSK SERVERLESS (Kafka) — private subnets, port 9098            │   |  |
|  |  │  Auth: SASL/OAUTHBEARER + IAM token (aws-msk-iam-sasl-signer)  │   |  |
|  |  │                                                                  │   |  |
|  |  │  Topics:                                                         │   |  |
|  |  │    ticks.nse              (4 partitions, key=instrument_id)     │   |  |
|  |  │    ticks.us               (4 partitions, key=instrument_id)     │   |  |
|  |  │    signals.pending        (3 partitions, key=instrument_id)     │   |  |
|  |  │    signals.enriched       (3 partitions, key=instrument_id)     │   |  |
|  |  │    signals.approved       (3 partitions, key=instrument_id)     │   |  |
|  |  │    orders.events          (3 partitions, key=order_id)          │   |  |
|  |  │    risk.kill-switch       (1 partition, 7d retention)           │   |  |
|  |  │    ops.audit              (1 partition)                          │   |  |
|  |  │    signals.enriched.retry (1 partition)                          │   |  |
|  |  │    signals.enriched.dlq   (1 partition, 7d retention)           │   |  |
|  |  │    signals.pending.dlq    (1 partition, 7d retention)           │   |  |
|  |  └─────────────────────────────────────────────────────────────────┘   |  |
|  |                                                                         |  |
|  |  ┌─── VPC ENDPOINTS ────────────────────────────────────────────┐     |  |
|  |  │  S3 Gateway endpoint    — removes S3 from NAT Gateway         │     |  |
|  |  │  DynamoDB Gateway       — removes DynamoDB from NAT Gateway   │     |  |
|  |  │  Secrets Manager        — IAM credential refresh              │     |  |
|  |  └──────────────────────────────────────────────────────────────┘     |  |
|  |                                                                         |  |
|  +-------------------------------------------------------------------------+  |
|                                                                               |
|  ┌─── MANAGED SERVICES ──────────────────────────────────────────────────┐   |
|  │                                                                         │   |
|  │  DynamoDB (on-demand / provisioned)                                    │   |
|  │    orders | positions | latest-prices | risk-state | strategy-state    │   |
|  │                                                                         │   |
|  │  S3 Buckets (with lifecycle policies)                                  │   |
|  │    tick-data | ohlcv-data | trading-logs | model-artifacts             │   |
|  │                                                                         │   |
|  │  Secrets Manager                                                        │   |
|  │    quantembrace/{env}/zerodha | quantembrace/{env}/alpaca              │   |
|  │                                                                         │   |
|  │  CloudWatch                                                             │   |
|  │    Log groups per service | Custom metrics | Alarms | Dashboard        │   |
|  │    Key alarms: WebSocket gap >10s, DLQ depth >0, P&L drawdown,        │   |
|  │    Kafka consumer lag, order rejection rate >20%                        │   |
|  │                                                                         │   |
|  │  SNS                                                                    │   |
|  │    alerts-topic (email) | kill-switch-topic (Lambda + DynamoDB write)  │   |
|  │                                                                         │   |
|  │  ECR — Docker image repositories per service                           │   |
|  │                                                                         │   |
|  └─────────────────────────────────────────────────────────────────────────┘   |
|                                                                               |
+===============================================================================+
```

---

## IAM Model

Each service has a dedicated EC2 instance profile (IAM role) with least-privilege policies:

| Service          | Key IAM Permissions                                                    |
|-----------------|------------------------------------------------------------------------|
| data_ingestion  | MSK connect+produce (`ticks.nse`, `ticks.us`), S3 PutObject (tick-data), DynamoDB PutItem (prices) |
| strategy_engine | MSK connect+consume (`ticks.nse`/`ticks.us`, group: strategy-v1)+produce (`signals.pending`), DynamoDB read (prices, strategy-state) |
| risk_engine     | MSK connect+consume (risk-v1)+produce (signals.approved, kill.switch, ops.audit), DynamoDB read/write (risk-state, positions), S3 PutObject (audit log) |
| execution_engine| MSK connect+consume (execution-v1)+produce (orders.events, ops.audit), DynamoDB read/write (orders, positions), Secrets GetSecretValue (broker creds) |

All MSK policies use `kafka-cluster:Connect`, `kafka:DescribeGroup`, `kafka:ReadData`/`kafka:WriteData` on specific ARN patterns — not wildcard `*` — per ADR-015.

---

## Terraform Module Dependency Graph

```
vpc  ──────────────────────────────────────────────────────────────────┐
s3   ──────────────────────────────────────────────────────────────── ─┤
dynamodb ─────────────────────────────────────────────────────────────►│
                                                                         │
monitoring (needs dynamodb.risk_state_table_name) ────────────────────►│
                                                                         │
sqs (needs monitoring.alerts_sns_topic_arn) ──────────────────────────►│
                                                                         ▼
                                                               ec2_services
                                                               (needs: vpc, s3, dynamodb,
                                                                sqs, monitoring outputs)
                                                                         │
                                                                         ▼
                                                                     kafka
                                                               (needs: vpc, ec2_services
                                                                role names + sg)
```

**No cycles.** Monitoring → ec2_services is one-way (monitoring does not depend on ec2_services outputs).

---

## Environments

| Config               | dev                    | staging                | prod                          |
|---------------------|------------------------|------------------------|-------------------------------|
| VPC CIDR            | 10.0.0.0/16            | 10.2.0.0/16            | 10.1.0.0/16                   |
| NAT Gateway         | Single (cost)          | Single (cost)          | HA — one per AZ               |
| DynamoDB billing    | On-demand              | On-demand              | Provisioned + auto-scale      |
| Instance types      | t4g.small / t4g.medium | t4g.medium / c6g.large | t4g.medium / c6g.large+xlarge |
| Warm pools          | No                     | No                     | Yes                           |
| Kafka retentions    | 1h ticks, 7d audit     | 1h ticks, 7d audit     | 24h ticks, 90d audit          |
| Log retention       | 14 days                | 14 days                | 30 days                       |
| Cost alarm (USD/d)  | $10                    | $25                    | $50                           |

---

## Deployment Flow

```
1. docker build → ECR push (CI/CD — GitHub Actions)
2. terraform apply (updates ASG launch template with new image tag)
3. ASG instance refresh (rolling replace with health check gate)
4. Kafka topic creation: python scripts/kafka/create_topics.py --bootstrap-servers <BROKERS>
   (topics not created by Terraform — Terraform creates the MSK cluster only)
5. Smoke test: scripts/kafka/test_connectivity.py
6. Set KAFKA_BOOTSTRAP_SERVERS on each EC2 instance — all services start in Kafka-only mode
```

Rollback: revert the ASG launch template version tag (previous image) and re-trigger instance refresh.
