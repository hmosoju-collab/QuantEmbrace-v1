# QuantEmbrace — AWS Infrastructure Guide

> **Who is this for?** Developers and DevOps engineers who need to understand, modify, or deploy the AWS infrastructure for QuantEmbrace.

---

## Table of Contents

1. [Infrastructure Overview](#infrastructure-overview)
2. [AWS Services Used — and Why](#aws-services-used--and-why)
3. [Cost Breakdown](#cost-breakdown)
4. [Terraform Structure](#terraform-structure)
5. [ECS Fargate — Compute](#ecs-fargate--compute)
6. [DynamoDB — State Storage](#dynamodb--state-storage)
7. [S3 — Data and Logs](#s3--data-and-logs)
8. [SQS — Messaging](#sqs--messaging)
9. [Networking — VPC and Security](#networking--vpc-and-security)
10. [Secrets Management](#secrets-management)
11. [Monitoring and Alerting](#monitoring-and-alerting)
12. [Deployment Process](#deployment-process)
13. [Cost Optimization Decisions](#cost-optimization-decisions)

---

## Infrastructure Overview

```
                         ┌─────────────────────────────────────────────────────┐
                         │                    AWS Account                       │
                         │                                                       │
                         │  ┌──────────────── VPC ────────────────────────┐    │
                         │  │                                               │    │
                         │  │   Private Subnets (AZ-a, AZ-b)              │    │
                         │  │   ┌──────────────────────────────────────┐  │    │
                         │  │   │  ECS Fargate Cluster                 │  │    │
                         │  │   │                                       │  │    │
                         │  │   │  ┌─────────┐  ┌──────────────────┐  │  │    │
                         │  │   │  │data-nse │  │ data-us          │  │  │    │
                         │  │   │  └─────────┘  └──────────────────┘  │  │    │
                         │  │   │  ┌──────────┐ ┌──────────────────┐  │  │    │
                         │  │   │  │strategy  │ │ risk_engine      │  │  │    │
                         │  │   │  └──────────┘ └──────────────────┘  │  │    │
                         │  │   │  ┌──────────┐                        │  │    │
                         │  │   │  │execution │                        │  │    │
                         │  │   │  └──────────┘                        │  │    │
                         │  │   └──────────────────────────────────────┘  │    │
                         │  │                    │                          │    │
                         │  │             NAT Gateway                       │    │
                         │  │                    │                          │    │
                         │  └────────────────────┼─────────────────────────┘    │
                         │                        │ Outbound to internet         │
                         │                        ▼                              │
                         │  ┌─────────────────────────────────────────────┐    │
                         │  │  AWS Managed Services                        │    │
                         │  │  ┌──────────┐ ┌────────┐ ┌──────────────┐  │    │
                         │  │  │DynamoDB  │ │  SQS   │ │ Secrets Mgr  │  │    │
                         │  │  │(via VPC  │ │(FIFO + │ │  (API keys)  │  │    │
                         │  │  │endpoint) │ │ Std)   │ │              │  │    │
                         │  │  └──────────┘ └────────┘ └──────────────┘  │    │
                         │  │  ┌──────────┐ ┌──────────────────────────┐  │    │
                         │  │  │   S3     │ │      CloudWatch           │  │    │
                         │  │  │(via VPC  │ │  Logs + Metrics + Alarms │  │    │
                         │  │  │endpoint) │ │                           │  │    │
                         │  │  └──────────┘ └──────────────────────────┘  │    │
                         │  └─────────────────────────────────────────────┘    │
                         └─────────────────────────────────────────────────────┘
```

---

## AWS Services Used — and Why

| Service | What We Use It For | Why This Service (Not Alternatives) |
|---|---|---|
| **ECS Fargate** | Run all 5 services as containers | Long-lived processes needed for WebSocket connections. Lambda has 15-min limit and cold starts. EC2 requires managing instances. |
| **DynamoDB** | Order state, positions, risk state, latest prices | Sub-millisecond reads for risk validation. Serverless (no cluster management). On-demand pricing scales to zero. |
| **S3** | Historical tick data, audit logs, ML model artifacts | Cheapest durable storage. Parquet format for efficient columnar queries. Lifecycle policies for cost control. |
| **SQS FIFO** | Signal and order queues between services | Guaranteed ordering per symbol. Built-in deduplication. Services decouple — one going down doesn't crash others. |
| **SQS Standard** | Market data ticks (Strategy Engine input) | Ordering not critical for ticks (we use timestamps). Higher throughput than FIFO. |
| **Secrets Manager** | Zerodha API keys, Alpaca API keys | Industry standard for secrets. IAM role-based access (no env vars in containers). Supports rotation. |
| **CloudWatch Logs** | All service logs | Native ECS integration. 30-day hot retention, then archived to S3. Insights for cross-service queries. |
| **CloudWatch Metrics** | Trade counts, P&L, latency | Alarm integration. Dashboard for trading ops. |
| **CloudWatch Alarms** | WebSocket down, high error rate, cost anomalies | SNS integration for email/SMS. Can trigger ECS task restart. |
| **SNS** | Alert delivery | Fanout to email + SMS. Webhook support for PagerDuty. |
| **ECR** | Docker image registry | Native ECS integration. No egress charges within AWS. |
| **VPC Endpoints** | S3 and DynamoDB access without internet | Eliminates NAT Gateway costs for high-volume S3/DynamoDB traffic. |
| **IAM Task Roles** | Per-service AWS permissions | Least-privilege. Services only access what they need. No shared credentials. |

### What We Deliberately Don't Use

| Service | Why We Don't Use It |
|---|---|
| **Lambda** | Can't maintain persistent WebSocket connections. Cold starts cause unacceptable latency. More expensive for always-on workloads. |
| **Kinesis Data Streams** | SQS is simpler and cheaper for our message volumes (<1,000 ticks/second). Kinesis is designed for millions/second. |
| **SageMaker** | Overkill. We train models offline and serve them as files from S3. SageMaker adds cost and complexity we don't need. |
| **ElastiCache** | DynamoDB's on-demand mode is fast enough (single-digit milliseconds). ElastiCache adds infrastructure overhead. |
| **RDS** | We have no relational data requirements. DynamoDB's key-value model fits all our state perfectly. |

---

## Cost Breakdown

### Monthly Estimate (Production)

| Service | Estimated Monthly Cost | Notes |
|---|---|---|
| ECS Fargate | $45–65 | 5 tasks, only during market hours (~13 hrs/day, 5 days/week) |
| DynamoDB | $5–15 | On-demand capacity, low volume, auto-scales to zero |
| S3 | $3–10 | Market data + logs, lifecycle policies active |
| SQS | $1–3 | Very low volume for trading signals |
| NAT Gateway | $35–45 | **Largest fixed cost** — $0.045/hr + data transfer |
| CloudWatch | $5–10 | Logs ingestion + metrics + alarms |
| Secrets Manager | $2–3 | ~4 secrets × $0.40/month + API call costs |
| ECR | $1–2 | Storage for Docker images |
| SNS | < $1 | Alert volume is low |
| **Total** | **~$97–154/month** | Minimal viable production setup |

### Cost Reduction Strategies

**ECS Market Hours Scheduling (saves ~40% on Fargate)**

Services only run during market hours. ECS Application Auto Scaling with scheduled actions:

```
NSE services:  Start at 08:30 IST (03:00 UTC), Stop at 16:00 IST (10:30 UTC)
US services:   Start at 13:00 IST (07:30 UTC), Stop at 01:30 IST+1 (20:00 UTC)
```

Configured in Terraform as scheduled ECS scaling policies.

**NAT Gateway Alternative (saves ~$32/month)**

If NAT Gateway cost is critical, replace with a `t4g.nano` NAT instance (~$3/month):
- Downside: requires manual management, no AZ redundancy
- Only recommended for development/staging environments

**S3 Intelligent-Tiering (saves ~30% on S3)**

Historical tick data that may not be accessed regularly is stored with S3 Intelligent-Tiering. Data automatically moves between frequent/infrequent access tiers based on access patterns.

**VPC Endpoints (eliminates NAT data transfer charges)**

DynamoDB and S3 traffic stays within AWS network. Without VPC endpoints, every DynamoDB read and S3 write goes through NAT Gateway and incurs $0.045/GB data transfer charges.

---

## Terraform Structure

```
infra/terraform/
├── modules/                     ← Reusable infrastructure modules
│   ├── ecs/                     ← ECS cluster, task definitions, services
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── dynamodb/                ← All DynamoDB tables
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── s3/                      ← S3 buckets with lifecycle policies
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── vpc/                     ← VPC, subnets, NAT Gateway, endpoints
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   └── monitoring/              ← CloudWatch, alarms, SNS
│       ├── main.tf
│       ├── variables.tf
│       └── outputs.tf
│
└── environments/                ← Environment-specific configurations
    ├── dev/                     ← Development environment (LocalStack or real AWS)
    │   ├── main.tf              ← Calls modules with dev-specific values
    │   ├── variables.tf
    │   └── terraform.tfvars    ← Actual values (NOT committed to git — only example)
    ├── staging/
    └── prod/
        └── main.tf
```

### How Terraform Modules Work

Each module is self-contained. The `environments/prod/main.tf` calls modules like this:

```hcl
# environments/prod/main.tf

module "vpc" {
  source = "../../modules/vpc"
  
  environment     = "prod"
  aws_region      = "ap-south-1"
  vpc_cidr        = "10.0.0.0/16"
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24"]
}

module "ecs" {
  source = "../../modules/ecs"
  
  environment    = "prod"
  vpc_id         = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  
  services = {
    data_ingestion_nse = {
      cpu    = 256
      memory = 512
      min_tasks = 1
      max_tasks = 1
      schedule = {
        start = "cron(0 3 ? * MON-FRI *)"   # 03:00 UTC = 08:30 IST
        stop  = "cron(30 10 ? * MON-FRI *)"  # 10:30 UTC = 16:00 IST
      }
    }
    # ... other services
  }
}
```

---

## ECS Fargate — Compute

### Services and Resource Allocation

| Service | vCPU | Memory | Min Tasks | Max Tasks | Notes |
|---|---|---|---|---|---|
| `data-ingestion-nse` | 0.25 | 512 MB | 1 | 1 | Single instance — WebSocket connection |
| `data-ingestion-us` | 0.25 | 512 MB | 1 | 1 | Single instance — WebSocket connection |
| `strategy-engine` | 0.5 | 1 GB | 1 | 2 | More CPU for indicator calculations |
| `risk-engine` | 0.25 | 512 MB | 1 | 1 | Single instance — consistency requirement |
| `execution-engine` | 0.25 | 512 MB | 1 | 1 | Single instance — prevents duplicate orders |

**Why single instances for most services?**

Horizontal scaling (multiple instances) is complex for stateful services:
- Data ingestion: one WebSocket connection per market, can't have two
- Risk engine: running multiple would require distributed locking to prevent double-approvals
- Execution engine: running multiple would require distributed deduplication

For our trading volumes, single instances with fast restart are safer than distributed systems.

### Task Role Permissions (IAM)

Each service has its own IAM role with minimum permissions:

```
data_ingestion_role:
  - dynamodb:PutItem, UpdateItem        ← Write latest prices
  - s3:PutObject                        ← Write tick Parquet files
  - sqs:SendMessage                     ← Publish to market-data queue
  - secretsmanager:GetSecretValue       ← Read API keys

strategy_engine_role:
  - dynamodb:GetItem, Query             ← Read latest prices, strategy state
  - dynamodb:PutItem, UpdateItem        ← Write strategy state
  - sqs:ReceiveMessage, DeleteMessage   ← Consume market-data queue
  - sqs:SendMessage                     ← Publish to signals queue
  - s3:GetObject                        ← Read strategy configs, ML models

risk_engine_role:
  - dynamodb:GetItem, Query, PutItem, UpdateItem  ← Read/write risk state, positions
  - s3:PutObject                        ← Write audit logs
  - sqs:ReceiveMessage, DeleteMessage   ← Consume signals queue
  - sqs:SendMessage                     ← Publish to orders queue

execution_engine_role:
  - dynamodb:GetItem, Query, PutItem, UpdateItem  ← Order lifecycle management
  - s3:PutObject                        ← Write execution logs
  - sqs:ReceiveMessage, DeleteMessage   ← Consume orders queue
  - secretsmanager:GetSecretValue       ← Read broker API keys
```

---

## DynamoDB — State Storage

### Tables

| Table Name | Partition Key | Sort Key | GSI | Purpose |
|---|---|---|---|---|
| `{prefix}-latest-prices` | `instrument` (String) | — | — | Current market price cache, TTL 24h |
| `{prefix}-positions` | `position_id` (String) | — | `market-symbol-index` | Open positions |
| `{prefix}-orders` | `order_id` (String) | — | `signal-id-index` | Order lifecycle and history |
| `{prefix}-risk-state` | `key` (String) | — | — | Kill switch, daily P&L counters |
| `{prefix}-strategy-state` | `strategy_name` (String) | `symbol` (String) | — | Strategy indicator state for restart recovery |

### Capacity Mode

- **Development:** On-demand (pay per request, no provisioning)
- **Production:** On-demand for all tables (trading volumes are bursty and hard to predict)
- Consider switching to provisioned with auto-scaling if you see consistent high volumes

### TTL Settings

| Table | TTL Field | TTL Duration | Why |
|---|---|---|---|
| `latest-prices` | `expires_at` | 24 hours | Stale prices auto-expire, prevents queries on old data |
| `orders` | `ttl` | 90 days | Regulatory requirement to retain order history 90 days, then auto-delete |
| `risk-state` daily counters | `ttl` | End of trading day | Daily P&L resets automatically for next day |

---

## S3 — Data and Logs

### Buckets

| Bucket | Contents | Storage Class | Lifecycle |
|---|---|---|---|
| `quantembrace-market-data-history` | Historical tick data (Parquet) | S3 Standard → Intelligent-Tiering | Move to Glacier after 90 days, delete after 3 years |
| `quantembrace-trading-logs` | Risk audit logs, execution logs | S3 Standard | Move to IA after 30 days, Glacier after 90 days |
| `quantembrace-model-artifacts` | ML models, feature datasets | S3 Standard | No expiry (models kept for reproducibility) |

### Data Partitioning

Tick data is partitioned for efficient reads during backtesting:

```
s3://quantembrace-market-data-history/
  NSE/
    RELIANCE/
      2026-04-24/
        09/     ← Hour of trading day
          ticks.parquet    ← ~50–200 MB per file
        10/
          ticks.parquet
        ...
  US/
    AAPL/
      2026-04-24/
        13/     ← 13:30–14:30 UTC (09:30–10:30 ET)
          ticks.parquet
```

To read data for a specific day and symbol:
```python
# Using PyArrow (efficient columnar reads)
import pyarrow.parquet as pq
import pyarrow.fs as fs

s3 = fs.S3FileSystem(region="ap-south-1")
dataset = pq.ParquetDataset(
    "quantembrace-market-data-history/NSE/RELIANCE/2026-04-24/",
    filesystem=s3
)
df = dataset.read_pandas()
```

---

## SQS — Messaging

### Queues

| Queue Name | Type | Used By | Purpose |
|---|---|---|---|
| `qe-{env}-market-data` | Standard | data_ingestion → strategy | Normalized ticks, ordering not critical |
| `qe-{env}-signals.fifo` | FIFO | strategy → risk | Signals, ordered per symbol, deduplicated |
| `qe-{env}-orders.fifo` | FIFO | risk → execution | Approved signals, ordered per symbol, deduplicated |

### Why FIFO for Signals and Orders?

If two signals arrive for the same symbol (e.g., BUY RELIANCE then SELL RELIANCE), they must be processed in the correct order. FIFO queues guarantee this via `MessageGroupId=symbol`. Standard queues can deliver messages out of order.

### Dead Letter Queues

Each queue has a Dead Letter Queue (DLQ) configured. If a message fails processing 3 times, it goes to the DLQ. This prevents poison-pill messages from blocking the entire queue:

```
qe-{env}-signals.fifo
  └─► qe-{env}-signals-dlq.fifo  (after 3 failed receives)

qe-{env}-orders.fifo
  └─► qe-{env}-orders-dlq.fifo   (after 3 failed receives)
```

CloudWatch alarm fires if any DLQ has > 0 messages.

---

## Networking — VPC and Security

### VPC Layout

```
VPC: 10.0.0.0/16
│
├── Private Subnet AZ-a: 10.0.1.0/24  (ECS tasks run here)
├── Private Subnet AZ-b: 10.0.2.0/24  (ECS tasks run here)
├── Public Subnet AZ-a:  10.0.101.0/24 (NAT Gateway, ALB)
└── Public Subnet AZ-b:  10.0.102.0/24 (NAT Gateway, ALB)
```

**ECS tasks are in private subnets.** They cannot be accessed from the internet. They access the internet (broker APIs) through the NAT Gateway.

### VPC Endpoints

VPC endpoints allow ECS tasks to reach S3 and DynamoDB without going through the NAT Gateway:

```
ECS task → VPC Endpoint for S3      → S3 (stays inside AWS network)
ECS task → VPC Endpoint for DynamoDB → DynamoDB (stays inside AWS network)
ECS task → NAT Gateway              → Zerodha API (internet)
ECS task → NAT Gateway              → Alpaca API (internet)
```

This saves ~$0.045/GB of data transfer through NAT for S3 and DynamoDB traffic.

### Security Groups

```
sg-ecs-tasks:
  Inbound:  None (no inbound internet access)
  Outbound: 443 (HTTPS) to 0.0.0.0/0  ← For broker APIs, AWS services
  
sg-localstack (dev only):
  Inbound:  4566 from developer IPs
```

---

## Secrets Management

All API credentials are stored in AWS Secrets Manager, never in environment variables or code.

### Secret Names

```
/quantembrace/{env}/zerodha/api_key
/quantembrace/{env}/zerodha/api_secret
/quantembrace/{env}/zerodha/access_token    ← Updated daily
/quantembrace/{env}/alpaca/api_key
/quantembrace/{env}/alpaca/api_secret
```

### Access Pattern

ECS tasks access secrets via IAM task role at startup:

```python
# shared/config/settings.py
import boto3

def get_secret(secret_name: str) -> str:
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    return response["SecretString"]

# Called once at startup, value cached in memory
kite_api_key = get_secret(f"/quantembrace/{env}/zerodha/api_key")
```

### Zerodha Daily Token Refresh

Kite Connect tokens expire daily at ~07:30 IST. The login script updates Secrets Manager:

```bash
# Run this once before market hours each day (or automate with a scheduled Lambda)
python scripts/zerodha_refresh_token.py
# → Triggers browser login
# → Exchanges code for access token
# → Stores new token in Secrets Manager
# → data_ingestion and execution services auto-reload the token
```

---

## Monitoring and Alerting

### CloudWatch Dashboards

Key dashboard panels:
- WebSocket connection status (NSE, US) — green/red indicator
- Active positions count and total exposure
- Daily P&L in real time
- Order success/failure rate
- Signal generation rate per strategy
- SQS queue depths (high depth = service falling behind)

### Critical Alarms

| Alarm | Condition | Action |
|---|---|---|
| WebSocket Disconnected | `websocket_connected = 0` for 60s | SNS → Email + SMS |
| Order Failure Rate | `order_failures / order_attempts > 10%` in 5 min | SNS → SMS |
| Daily P&L Drawdown Warning | `daily_pnl_pct < -2.5%` | SNS → Email (warning) |
| Kill Switch Activated | `kill_switch_activations > 0` | SNS → Email + SMS (critical) |
| DLQ Messages | `ApproximateNumberOfMessagesVisible > 0` for any DLQ | SNS → Email |
| ECS Task Crash | `task_count < desired_count` for 2 min | SNS → Email |

### Log Queries (CloudWatch Insights)

**Find all risk rejections today:**
```
fields @timestamp, signal_id, reason
| filter service = "risk_engine" and status = "REJECTED"
| sort @timestamp desc
| limit 50
```

**Trace a single signal through all services:**
```
fields @timestamp, service, event
| filter correlation_id = "your-correlation-id-here"
| sort @timestamp asc
```

**Count orders per hour:**
```
fields @timestamp
| filter service = "execution_engine" and event like "order_placed"
| stats count() as orders by bin(1h)
```

---

## Deployment Process

### First-time Deployment (Dev)

```bash
cd infra/terraform/environments/dev

# Initialize Terraform
terraform init

# Preview what will be created
terraform plan

# Create infrastructure
terraform apply
# Type "yes" when prompted
```

### Deploying to Production

```bash
cd infra/terraform/environments/prod

terraform init
terraform plan -out=prod.tfplan
# Review the plan carefully!
terraform apply prod.tfplan
```

### Deploying a Service Update (New Code)

```bash
# 1. Build and push Docker image
docker build -t quantembrace/risk-engine:v1.2.0 -f infra/deployment/Dockerfile .
docker tag quantembrace/risk-engine:v1.2.0 {account_id}.dkr.ecr.ap-south-1.amazonaws.com/quantembrace/risk-engine:v1.2.0
docker push {account_id}.dkr.ecr.ap-south-1.amazonaws.com/quantembrace/risk-engine:v1.2.0

# 2. Update ECS task definition (Terraform updates the image tag)
cd infra/terraform/environments/prod
terraform apply -var="risk_engine_image_tag=v1.2.0"

# 3. ECS performs rolling deployment automatically
# New task starts → health check passes → old task stops
```

### CI/CD Pipeline (GitHub Actions)

The pipeline in `.github/workflows/deploy.yml`:
1. On push to `main`: build Docker images, push to ECR
2. Run all tests (unit + integration)
3. Apply Terraform for `staging` environment
4. Run smoke tests against staging
5. On manual approval: apply Terraform for `prod`

---

*Last updated: 2026-04-24 | Update this document whenever: new AWS services are adopted, Terraform modules are restructured, cost estimates change significantly, or deployment procedures change.*
