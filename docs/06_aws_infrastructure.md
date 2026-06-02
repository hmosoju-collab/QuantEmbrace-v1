# QuantEmbrace — AWS Infrastructure Guide

> **Who is this for?** Developers and DevOps engineers who need to understand, modify, or deploy the AWS infrastructure for QuantEmbrace.

---

## Table of Contents

1. [Infrastructure Overview](#infrastructure-overview)
2. [AWS Services Used — and Why](#aws-services-used--and-why)
3. [Cost Breakdown](#cost-breakdown)
4. [Terraform Structure](#terraform-structure)
5. [Compute — EC2 ARM64 Auto Scaling Groups](#compute--ec2-arm64-auto-scaling-groups)
6. [Messaging — Kafka MSK Serverless](#messaging--kafka-msk-serverless)
7. [State Storage — DynamoDB](#state-storage--dynamodb)
8. [Data and Logs — S3](#data-and-logs--s3)
9. [Networking — VPC and Security](#networking--vpc-and-security)
10. [Secrets Management](#secrets-management)
11. [Monitoring and Alerting](#monitoring-and-alerting)
12. [Deployment Process](#deployment-process)
13. [Cost Optimisation Decisions](#cost-optimisation-decisions)

---

## Infrastructure Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                        AWS Account (ap-south-1)                       │
│                                                                        │
│  ┌───────────────────── VPC: 10.0.0.0/16 ──────────────────────┐    │
│  │                                                                │    │
│  │  Private Subnets (10.0.1.0/24, 10.0.2.0/24)  — AZ-a, AZ-b  │    │
│  │                                                                │    │
│  │   ┌─────────────────────────────────────────────────────┐    │    │
│  │   │   EC2 ARM64 Auto Scaling Groups                       │    │    │
│  │   │                                                       │    │    │
│  │   │  ┌────────────────────┐  ┌──────────────────────┐   │    │    │
│  │   │  │ data-ingestion-asg │  │  strategy-engine-asg │   │    │    │
│  │   │  │  t4g.medium        │  │  c6g.large            │   │    │    │
│  │   │  │  min=1 max=1       │  │  min=1 max=2          │   │    │    │
│  │   │  └────────────────────┘  └──────────────────────┘   │    │    │
│  │   │  ┌────────────────────┐  ┌──────────────────────┐   │    │    │
│  │   │  │  risk-engine-asg   │  │  execution-engine-asg│   │    │    │
│  │   │  │  c6g.large         │  │  c6g.xlarge           │   │    │    │
│  │   │  │  min=1 max=1       │  │  min=1 max=1          │   │    │    │
│  │   │  └────────────────────┘  └──────────────────────┘   │    │    │
│  │   │  ┌────────────────────┐                              │    │    │
│  │   │  │   ai-engine-asg    │                              │    │    │
│  │   │  │  c6g.large         │                              │    │    │
│  │   │  │  min=1 max=2       │                              │    │    │
│  │   │  └────────────────────┘                              │    │    │
│  │   └─────────────────────────────────────────────────────┘    │    │
│  │                        │                                       │    │
│  │                   NAT Gateway                                  │    │
│  │                        │                                       │    │
│  └────────────────────────┼───────────────────────────────────── ┘    │
│                            │ Outbound to internet (broker APIs)        │
│                                                                        │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │  AWS Managed Services                                         │     │
│  │                                                               │     │
│  │  ┌──────────────┐  ┌────────────────┐  ┌─────────────────┐  │     │
│  │  │  MSK          │  │  DynamoDB      │  │  S3             │  │     │
│  │  │  Serverless   │  │  (on-demand)   │  │  (2 buckets)    │  │     │
│  │  │  SASL/IAM     │  │  10 tables     │  │  lifecycle      │  │     │
│  │  │  port 9098    │  │                │  │  policies       │  │     │
│  │  └──────────────┘  └────────────────┘  └─────────────────┘  │     │
│  │  ┌──────────────┐  ┌────────────────┐  ┌─────────────────┐  │     │
│  │  │  CloudWatch  │  │  Secrets Mgr   │  │  SNS            │  │     │
│  │  │  Logs+Metrics│  │  (API keys)    │  │  (alerts)       │  │     │
│  │  │  +Alarms     │  │                │  │                 │  │     │
│  │  └──────────────┘  └────────────────┘  └─────────────────┘  │     │
│  └─────────────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────┘
```

---

## AWS Services Used — and Why

| Service | What We Use It For | Why This Over Alternatives |
|---|---|---|
| **EC2 ARM64 ASGs** | Run all 5 services as long-lived processes | Persistent WebSocket connections require long-lived processes. Lambda has a 15-minute timeout and cold starts. ECS Fargate was tried and removed (Phase 1 migration) — EC2 gives better CPU scheduling and 20-40% cost saving with Graviton ARM64. |
| **MSK Serverless** | Inter-service Kafka messaging | No cluster to provision or manage. Automatic scaling. IAM-native authentication with no key rotation. Pay-per-use — zero cost when markets are closed. |
| **DynamoDB on-demand** | All transactional state | Sub-millisecond reads for risk validation. Serverless. Auto-scales with trading volume. Key-value model fits all state perfectly. |
| **S3** | Historical tick data, audit logs, ML model artifacts | Cheapest durable storage. Parquet format for efficient columnar reads. Lifecycle policies for automatic cost management. |
| **Secrets Manager** | Broker API keys, access tokens | IAM role-based access. No credentials in env vars or code. Supports secret rotation. Zerodha daily token stored here. |
| **CloudWatch Logs** | All service logs | Native EC2 integration via CloudWatch agent. 30-day hot retention, then archived. Insights for cross-service trace queries. |
| **CloudWatch Metrics** | Trade counts, P&L, latency | Alarm integration. Dashboard for trading ops. Custom namespaces per service. |
| **CloudWatch Alarms** | WebSocket drops, error rates, cost anomalies | SNS integration for email/SMS. Can trigger ASG scaling or SNS notifications. |
| **SNS** | Alert delivery | Fanout to email + SMS. PagerDuty webhook support. |
| **VPC Endpoints** | S3 and DynamoDB access without internet | Eliminates NAT Gateway data transfer charges for high-volume DynamoDB polling and S3 writes. |
| **IAM Instance Profiles** | Per-service AWS permissions | Least-privilege. No shared credentials. Each service has its own role. |

### What We Deliberately Don't Use

| Service | Why Not |
|---|---|
| **ECS Fargate** | Removed in Phase 1 migration. EC2 ARM64 ASGs provide better CPU scheduling for persistent WebSocket services and are 20-40% cheaper. |
| **Lambda** | Can't maintain persistent WebSocket connections. 15-minute timeout. Cold starts unacceptable for trading latency. More expensive than EC2 for always-on workloads. |
| **SQS** | Replaced by Kafka MSK Serverless. Kafka provides message replay, consumer group offset management, and ordered partitioned streams that SQS cannot match. |
| **Kinesis** | Overkill for our signal volume (<10,000 signals/day). MSK Serverless is simpler and cheaper. |
| **SageMaker** | Overkill. Models are trained offline and served as files from S3. No real-time inference infrastructure needed. |
| **ElastiCache** | DynamoDB sub-millisecond reads are sufficient. Adding Redis would increase infrastructure cost and complexity. |
| **RDS** | No relational data requirements. DynamoDB key-value model fits all use cases. |

---

## Cost Breakdown

### Monthly Estimate (Production, Phase C — ₹10 lakh capital)

| Service | Estimated Monthly Cost | Notes |
|---|---|---|
| EC2 ARM64 (t4g.medium × 2 data, c6g.large × 2 strategy+risk, c6g.xlarge × 1 execution) | $55–80 | Running only during market hours (NSE 09:00–16:00 IST + US overlap) |
| EC2 ai_engine (c6g.large) | $20–30 | Running during both market sessions |
| MSK Serverless | $15–25 | Per-message pricing. ~0 cost when markets closed. |
| DynamoDB on-demand | $5–15 | Low volume during Phase C |
| S3 | $4–10 | Market data + logs. Lifecycle policies active. |
| NAT Gateway | $35–45 | **Largest fixed cost** — $0.045/hr + data transfer to broker APIs |
| CloudWatch | $5–10 | Logs ingestion + metrics + alarms |
| Secrets Manager | $2–4 | ~5 secrets × $0.40/month + API call costs |
| SNS | < $1 | Alert volume is low |
| **Total** | **~$146–225/month** | Minimal viable production setup |

### Why EC2 ARM64 Is Cheaper Than Fargate

| Configuration | On-Demand Monthly |
|---|---|
| Fargate 0.5 vCPU, 1 GB per service × 5 | ~$65/month (market hours only) |
| EC2 t4g.medium × 2 + c6g.large × 3 + c6g.xlarge × 1 | ~$50/month (market hours only) |

EC2 ARM64 is ~30% cheaper for equivalent compute, and provides better sustained CPU performance for the risk engine and strategy indicator computations.

---

## Terraform Structure

```
infra/terraform/
├── modules/                        ← Reusable infrastructure modules
│   ├── ec2_services/               ← EC2 ASGs, launch templates, user-data scripts
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── dynamodb/                   ← All 10 DynamoDB tables
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── s3/                         ← S3 buckets with lifecycle policies
│   │   ├── main.tf
│   │   └── variables.tf
│   ├── vpc/                        ← VPC, subnets, NAT Gateway, VPC endpoints
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   └── monitoring/                 ← CloudWatch alarms, SNS, dashboards
│       ├── main.tf
│       ├── variables.tf
│       └── outputs.tf
│
└── environments/                   ← Environment-specific configurations
    ├── dev/
    │   ├── main.tf
    │   └── variables.tf
    ├── staging/
    │   ├── main.tf
    │   └── variables.tf
    └── prod/
        ├── main.tf
        └── variables.tf
```

### How Environment Configs Work

```hcl
# infra/terraform/environments/prod/main.tf

module "vpc" {
  source          = "../../modules/vpc"
  environment     = "prod"
  aws_region      = "ap-south-1"
  vpc_cidr        = "10.0.0.0/16"
}

module "ec2_services" {
  source     = "../../modules/ec2_services"
  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnet_ids

  services = {
    data_ingestion = {
      instance_type = "t4g.medium"
      min_size      = 1
      max_size      = 1
    }
    strategy_engine = {
      instance_type = "c6g.large"
      min_size      = 1
      max_size      = 2
    }
    risk_engine = {
      instance_type = "c6g.large"
      min_size      = 1
      max_size      = 1   # Single instance — consistency requirement
    }
    execution_engine = {
      instance_type = "c6g.xlarge"
      min_size      = 1
      max_size      = 1   # Single instance — idempotency requirement; cluster placement group
    }
    ai_engine = {
      instance_type = "c6g.large"
      min_size      = 1
      max_size      = 2
    }
  }
}
```

---

## Compute — EC2 ARM64 Auto Scaling Groups

### Service Sizing

| Service | Instance Type | vCPU | RAM | Min | Max | Reason |
|---|---|---|---|---|---|---|
| `data_ingestion` | t4g.medium | 1 | 2 GB | 1 | 1 | One WebSocket connection per broker; two ASGs (NSE + US) |
| `strategy_engine` | c6g.large | 2 | 4 GB | 1 | 2 | Strategy computations are CPU-intensive |
| `risk_engine` | c6g.large | 2 | 4 GB | 1 | 1 | **Must be single instance** — state consistency; c6g for sustained CPU under validator throughput |
| `execution_engine` | c6g.xlarge | 4 | 8 GB | 1 | 1 | **Must be single instance** — order deduplication; cluster placement group; min=1 always |
| `ai_engine` | c6g.large | 2 | 4 GB | 1 | 2 | HMM inference is CPU-bound; c6g avoids t4g burst credit exhaustion; single AZ for partition stability |

**Why single instances for risk and execution?**

Horizontal scaling of stateful services is complex. For `risk_engine`, running two instances would require distributed locking to prevent double-approvals on the same signal. For `execution_engine`, running two instances would require cluster-wide deduplication. At our trading volumes, single instances with fast ASG auto-replacement (< 60s) are safer than distributed systems.

### IAM Instance Profile Permissions (Least-Privilege)

Each service has its own IAM instance profile. Example for `risk_engine`:

```
risk_engine_role:
  dynamodb:GetItem, Query, PutItem, UpdateItem  → risk-state, positions tables
  s3:PutObject                                  → audit log writes
  kafka:Connect, kafka:DescribeCluster          → MSK Serverless access
  kms:GenerateDataKey                           → MSK IAM token signing
  secretsmanager:GetSecretValue                 → read API keys (read-only)
```

### Deployment / Service Updates

Services run as `systemd` units on the EC2 instances. Updating a service means:

1. Build a new Docker image (or update the Python package on S3)
2. Trigger an ASG instance refresh:
   ```bash
   aws autoscaling start-instance-refresh \
     --auto-scaling-group-name qe-prod-risk-engine \
     --preferences '{"MinHealthyPercentage": 100}'
   ```
3. The ASG launches a new instance with the updated code, waits for health checks, then terminates the old one

---

## Messaging — Kafka MSK Serverless

### Cluster Configuration

```
Cluster name: qe-{env}-msk
Bootstrap URL: boot-abc123.c1.kafka-serverless.ap-south-1.amazonaws.com:9098
Authentication: SASL_SSL + OAUTHBEARER (IAM)
Auth library: aws-msk-iam-sasl-signer-python
```

### Topics

| Topic | Partitions | Retention | Purpose |
|---|---|---|---|
| `ticks.nse` | 2 | 2h | NSE real-time ticks |
| `ticks.us` | 2 | 2h | US real-time ticks |
| `signals.pending` | 3 | 24h | Raw strategy signals awaiting enrichment |
| `signals.enriched` | 3 | 24h | ML-enriched signals awaiting risk validation |
| `signals.approved` | 3 | 24h | Risk-approved signals awaiting execution |
| `orders.events` | 3 | 24h | Order fills and state changes |
| `risk.kill-switch` | 1 | 7d | Kill switch activate/clear events |
| `signals.enriched.retry` | 1 | 24h | Failed enrichments awaiting retry |
| `signals.enriched.dlq` | 1 | 7d | Dead letter queue — manual inspection |
| `signals.pending.dlq` | 1 | 7d | Dead letter queue — manual inspection |

### Kafka Auth for Local Development

Local development uses Redpanda (Kafka-compatible, no auth required):

```bash
# In .env for local dev:
KAFKA_BOOTSTRAP_SERVERS=localhost:19092
KAFKA_USE_IAM=false

# Docker Compose overrides this to the internal Docker network:
KAFKA_BOOTSTRAP_SERVERS=redpanda:9092
KAFKA_USE_IAM=false
```

The shared `get_kafka_auth_config()` function handles the switch automatically — no code changes needed between local and production.

---

## State Storage — DynamoDB

### Tables

| Table Name | Partition Key | Sort Key | Purpose |
|---|---|---|---|
| `{prefix}-orders` | `order_id` | — | Order lifecycle and history |
| `{prefix}-positions` | `position_id` | — | Open positions with unrealised P&L |
| `{prefix}-risk-state` | `key` | — | Kill switch, daily P&L counters |
| `{prefix}-sessions` | `session_id` | — | Zerodha access tokens (daily refresh) |
| `{prefix}-strategy-config` | `strategy_name` | — | Strategy parameters, paper_trade flag |
| `{prefix}-kill-switch` | `key` | — | Kill switch state (active, reason, activated_by) |
| `{prefix}-candle-cache` | `instrument` | `candle_type` | Candle data for strategy computations |
| `{prefix}-instrument-registry` | `symbol` | `market` | Instrument metadata, sector, lot size |
| `{prefix}-signal-inbox` | `signal_id` | — | Signal deduplication inbox |
| `{prefix}-signal-outbox` | `signal_id` | — | Signal outbox for at-least-once delivery |

### Capacity Mode

- **Development:** On-demand (pay per request, zero minimum)
- **Production:** On-demand for all tables — trading volumes are bursty

### TTL Settings

| Table | TTL Field | Duration | Reason |
|---|---|---|---|
| `orders` | `ttl` | 90 days | Regulatory retention requirement, then auto-delete |
| `risk-state` daily counters | `ttl` | End of trading day | Daily P&L resets automatically for next day |
| `candle-cache` | `ttl` | 24 hours | Stale candles don't build up |
| `signal-inbox` | `ttl` | 1 hour | Deduplication window only |
| `sessions` | `ttl` | 24 hours | Zerodha tokens expire daily |

---

## Data and Logs — S3

### Buckets

| Bucket Name | Contents | Lifecycle |
|---|---|---|
| `quantembrace-{env}-data` | Historical ticks (Parquet), ML model artifacts, backtest data | Intelligent-Tiering after 30 days; Glacier after 90 days; delete after 3 years |
| `quantembrace-{env}-logs` | Risk audit logs, execution logs, DLQ messages | IA after 30 days; Glacier after 90 days |

### Tick Data Structure

```
s3://quantembrace-{env}-data/
  ticks/
    NSE/
      RELIANCE/
        2026-04-24/
          09/   ← Trading hour (IST)
            ticks.parquet
          10/
            ticks.parquet
    US/
      AAPL/
        2026-04-24/
          14/   ← UTC hour (09:30 ET = 14:30 UTC)
            ticks.parquet

  models/
    volatility_predictor/v1.0.0/model.pkl
    regime_classifier/v1.0.0/model.pkl

  backtest/
    results/{run_id}/
```

### Risk Audit Log Structure

```
s3://quantembrace-{env}-logs/
  risk-audit/
    2026-04-24/
      {risk_decision_id}.json    ← One file per decision (approve or reject)

  execution/
    2026-04-24/
      {order_id}.json

  dlq/
    signals.enriched.dlq/
      {timestamp}_{signal_id}.json
```

---

## Networking — VPC and Security

### VPC Layout

```
VPC: 10.0.0.0/16
│
├── Private Subnet AZ-a: 10.0.1.0/24  ← EC2 instances run here
├── Private Subnet AZ-b: 10.0.2.0/24  ← EC2 instances run here (AZ redundancy)
├── Public Subnet AZ-a:  10.0.101.0/24 ← NAT Gateway
└── Public Subnet AZ-b:  10.0.102.0/24 ← NAT Gateway (standby)
```

**EC2 instances run in private subnets.** They cannot be reached from the internet. They access broker APIs (Zerodha, Alpaca) through the NAT Gateway.

### VPC Endpoints (Reduces Costs)

```
EC2 instance → VPC Endpoint for S3        → S3 (no NAT data charges)
EC2 instance → VPC Endpoint for DynamoDB  → DynamoDB (no NAT data charges)
EC2 instance → VPC Endpoint for MSK       → MSK Serverless (no NAT data charges)
EC2 instance → NAT Gateway                → Zerodha API (internet, charged)
EC2 instance → NAT Gateway                → Alpaca API (internet, charged)
```

Without VPC endpoints, every DynamoDB poll and S3 write goes through NAT Gateway at $0.045/GB. On a typical trading day with thousands of DynamoDB reads and hundreds of S3 log writes, this adds up to $5–15/month that VPC endpoints eliminate.

### Security Groups

```
sg-trading-ec2:
  Inbound:  port 8081–8085 from sg-alb (health checks from internal load balancer only)
  Outbound: 443 (HTTPS) → 0.0.0.0/0  (broker APIs, AWS endpoints)
            9098 (Kafka) → MSK security group
            443 (DynamoDB, S3) → VPC endpoints
```

---

## Secrets Management

All API credentials are stored in AWS Secrets Manager. Never hardcode credentials in code or environment variables.

### Secret Names

```
/quantembrace/{env}/zerodha/api_key
/quantembrace/{env}/zerodha/api_secret
/quantembrace/{env}/zerodha/access_token     ← Updated daily by scripts/zerodha_login.py
/quantembrace/{env}/alpaca/api_key
/quantembrace/{env}/alpaca/api_secret
```

### Access Pattern

Services read secrets at startup via IAM instance role. No credentials in process environment:

```python
# shared/config/settings.py
def _load_broker_credentials(self) -> None:
    client = boto3.client("secretsmanager")
    self.zerodha_api_key = client.get_secret_value(
        SecretId=f"/quantembrace/{self.qe_environment}/zerodha/api_key"
    )["SecretString"]
```

### Local Development

For local development, credentials are read from `.env` directly (not Secrets Manager):

```bash
# .env — local only, never committed
ZERODHA_API_KEY=your_key_here
ALPACA_API_KEY=your_key_here
```

The `AppSettings` Pydantic model reads from environment variables first. In local dev, `.env` satisfies this. In production, the EC2 instance role populates the values from Secrets Manager via a startup script.

---

## Monitoring and Alerting

### CloudWatch Namespaces

| Namespace | Metrics |
|---|---|
| `QuantEmbrace/DataIngestion` | `TicksPublished`, `WebSocketConnectionStatus`, `TickStalenessSeconds` |
| `QuantEmbrace/StrategyEngine` | `SignalsPublished`, `StrategyErrors`, `InstrumentsWatched` |
| `QuantEmbrace/AIEngine` | `SignalsEnriched`, `EnrichmentLatencyMs`, `ModelInferenceErrors` |
| `QuantEmbrace/RiskEngine` | `SignalsApproved`, `SignalsRejected`, `KillSwitchActivations`, `DailyPnL` |
| `QuantEmbrace/ExecutionEngine` | `OrdersPlaced`, `OrdersFilled`, `OrdersFailed`, `PaperOrdersSimulated` |

### Critical Alarms

| Alarm | Trigger Condition | Action |
|---|---|---|
| `DataFeedStaleness` | No new ticks for 60s | SNS → SMS + email |
| `KillSwitchActivated` | `KillSwitchActivations > 0` | SNS → SMS + email (critical) |
| `DailyLossWarning` | `DailyPnL < -2% NAV` | SNS → email (warning) |
| `DailyLossHalt` | `DailyPnL < -3% NAV` | Auto-fires kill switch; SNS → SMS + email |
| `OrderFailureRate` | `OrdersFailed / (OrdersFailed + OrdersFilled) > 10%` in 5min | SNS → SMS |
| `EnrichmentWatchdogFallback` | `EnrichmentFallbackActive > 0` | SNS → email |
| `KafkaConsumerLag` | Consumer lag > 1000 messages on any trading topic | SNS → email |
| `DLQMessageCount` | Any DLQ topic has messages | SNS → email |

### CloudWatch Logs Insights Queries

**Trace a single signal across all services:**
```
fields @timestamp, service, event, signal_id
| filter correlation_id = "paste-your-correlation-id-here"
| sort @timestamp asc
```

**All risk rejections today:**
```
fields @timestamp, signal_id, reason
| filter service = "risk_engine" and status = "REJECTED"
| sort @timestamp desc
| limit 100
```

**Order placement latency (tick to fill):**
```
fields @timestamp, signal_id, total_latency_ms
| filter service = "execution_engine" and event = "order_filled"
| stats avg(total_latency_ms), max(total_latency_ms), percentile(total_latency_ms, 95) by bin(1h)
```

---

## Deployment Process

### First-Time Infrastructure Deployment

```bash
# 1. Configure AWS credentials
aws configure --profile quantembrace-prod

# 2. Initialise Terraform
cd infra/terraform/environments/prod
terraform init

# 3. Preview what will be created
terraform plan -var-file=prod.tfvars

# 4. Create infrastructure (type "yes" when prompted)
terraform apply -var-file=prod.tfvars
```

### Deploying a Service Code Update

```bash
# 1. Build new Python wheel or Docker image
./scripts/build.sh risk_engine v1.2.0

# 2. Upload to S3 (services pull the package from S3 on startup)
aws s3 cp dist/risk_engine-v1.2.0.tar.gz \
    s3://quantembrace-prod-data/deployments/risk_engine-v1.2.0.tar.gz

# 3. Trigger rolling instance refresh (launches new instance, waits for health, terminates old)
aws autoscaling start-instance-refresh \
    --auto-scaling-group-name qe-prod-risk-engine \
    --preferences '{"MinHealthyPercentage": 100, "InstanceWarmup": 60}'

# 4. Monitor rollout
aws autoscaling describe-instance-refreshes \
    --auto-scaling-group-name qe-prod-risk-engine
```

### Emergency Scale-Down (All Services)

If something is critically wrong and you need to stop everything immediately:

```bash
# Option 1: Kill switch (trading stops, processes keep running)
make kill-switch-on

# Option 2: Scale all ASGs to zero (processes stop, positions held at broker)
for svc in data-ingestion strategy-engine risk-engine execution-engine ai-engine; do
    aws autoscaling set-desired-capacity \
        --auto-scaling-group-name qe-prod-$svc \
        --desired-capacity 0
done
```

### CI/CD Pipeline (GitHub Actions)

The pipeline in `.github/workflows/deploy.yml`:

1. **On push to any branch:** Run unit tests
2. **On push to `main`:** Run all tests + build Docker images + push to ECR
3. **On tag `v*`:** Apply Terraform to staging, run smoke tests
4. **On manual trigger:** Apply Terraform to production (requires confirmation)

---

## Cost Optimisation Decisions

### ARM64 Graviton Over x86 (saves 20–40%)

All EC2 instances use the c6g (compute-optimised Graviton) family. For the same price as a c5.medium (x86), a c6g.medium provides the same or better compute performance. Over a full year of trading, this saves hundreds of dollars.

### MSK Serverless Over Provisioned (saves ~$100/month vs. kafka.t3.small cluster)

MSK Serverless charges per storage-hour and per gigabyte of data streamed. During non-market hours (nights, weekends, holidays — roughly 80% of the time), cost is near-zero. A provisioned MSK cluster costs ~$160/month regardless of whether markets are open.

### S3 Over DynamoDB for Historical Data (saves ~$50/month)

Historical tick data is stored in S3 as Parquet, not in DynamoDB. A single day of RELIANCE ticks is ~500MB as Parquet; storing the same data in DynamoDB at 25¢/GB/month would cost significantly more than S3's $0.023/GB/month.

### VPC Endpoints Over NAT for DynamoDB + S3 (saves ~$15/month)

Without VPC endpoints, every DynamoDB and S3 API call transits the NAT Gateway at $0.045/GB. With endpoints, this traffic stays inside the AWS backbone at zero data transfer cost. The endpoint itself costs $0.01/hr ($7/month) but saves more in data transfer.

### On-Demand DynamoDB Over Provisioned (saves upfront but costs ~20% more per operation)

For Phase C with unpredictable access patterns and bursty trading (6.5 trading hours/day), on-demand is correct. If trading scales to consistent high-volume patterns, switching to provisioned with auto-scaling will reduce costs.

---

*Last updated: 2026-05-28 | Update this document when: EC2 instance types change, Terraform modules are restructured, new AWS services are adopted, cost estimates change significantly, or deployment procedures change.*
