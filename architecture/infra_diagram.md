# QuantEmbrace - AWS Infrastructure

## Overview

All infrastructure is provisioned and managed via Terraform. The system runs entirely on
AWS using managed services (ECS Fargate, S3, DynamoDB) to minimize operational overhead.
There are no self-managed servers, no EC2 instances, and no Kubernetes clusters.

---

## Infrastructure Diagram

```
                            +-----------------------+
                            |      INTERNET         |
                            +-----------+-----------+
                                        |
                                        | HTTPS/WSS (broker APIs)
                                        |
+=======================================+=======================================+
|                           AWS ACCOUNT (quantembrace-prod)                      |
|                                                                               |
|  +-------------------------------VPC (10.0.0.0/16)------------------------+  |
|  |                                                                         |  |
|  |  +------ AZ: ap-south-1a ------+   +------ AZ: ap-south-1b ------+    |  |
|  |  |                             |   |                              |    |  |
|  |  |  PUBLIC SUBNET              |   |  PUBLIC SUBNET               |    |  |
|  |  |  10.0.1.0/24               |   |  10.0.2.0/24                |    |  |
|  |  |                             |   |                              |    |  |
|  |  |  +----------+              |   |  (standby, no resources)     |    |  |
|  |  |  | NAT GW   |              |   |                              |    |  |
|  |  |  | (EIP)    |              |   |                              |    |  |
|  |  |  +----+-----+              |   |                              |    |  |
|  |  |       |                     |   |                              |    |  |
|  |  +-------+---------------------+   +------------------------------+    |  |
|  |          |                                                              |  |
|  |          | NAT route                                                    |  |
|  |          |                                                              |  |
|  |  +-------+---------------------+   +------------------------------+    |  |
|  |  |                             |   |                              |    |  |
|  |  |  PRIVATE SUBNET             |   |  PRIVATE SUBNET              |    |  |
|  |  |  10.0.10.0/24              |   |  10.0.20.0/24               |    |  |
|  |  |                             |   |                              |    |  |
|  |  |  +--ECS FARGATE CLUSTER--+  |   |  +--ECS FARGATE CLUSTER--+  |    |  |
|  |  |  |                       |  |   |  |  (failover AZ)        |  |    |  |
|  |  |  | data-ingestion-nse    |  |   |  |                       |  |    |  |
|  |  |  | data-ingestion-us     |  |   |  |                       |  |    |  |
|  |  |  | strategy-engine       |  |   |  |                       |  |    |  |
|  |  |  | risk-engine           |  |   |  |                       |  |    |  |
|  |  |  | execution-engine      |  |   |  |                       |  |    |  |
|  |  |  |                       |  |   |  |                       |  |    |  |
|  |  |  +-----------+-----------+  |   |  +-----------------------+  |    |  |
|  |  |              |              |   |                              |    |  |
|  |  +--------------+--------------+   +------------------------------+    |  |
|  |                 |                                                       |  |
|  |                 | VPC Endpoints (no internet traversal)                 |  |
|  |                 |                                                       |  |
|  |  +--------------+-------------------------------------------------+    |  |
|  |  |  VPC ENDPOINT: S3 (Gateway)                                    |    |  |
|  |  |  VPC ENDPOINT: DynamoDB (Gateway)                              |    |  |
|  |  |  VPC ENDPOINT: CloudWatch Logs (Interface)                     |    |  |
|  |  |  VPC ENDPOINT: Secrets Manager (Interface)                     |    |  |
|  |  |  VPC ENDPOINT: ECR (Interface, for image pulls)                |    |  |
|  |  +----------------------------------------------------------------+    |  |
|  |                                                                         |  |
|  +-------------------------------------------------------------------------+  |
|                                                                               |
|  +--- AWS Services (outside VPC) ----------------------------------------+   |
|  |                                                                        |   |
|  |  +-------------+  +-------------+  +-------------+  +-------------+   |   |
|  |  |    S3       |  |  DynamoDB   |  | CloudWatch  |  |  Secrets    |   |   |
|  |  |             |  |             |  |             |  |  Manager    |   |   |
|  |  | Buckets:    |  | Tables:     |  | Log Groups: |  |             |   |   |
|  |  | - market-   |  | - latest-   |  | - /qe/data  |  | Secrets:   |   |   |
|  |  |   data-     |  |   prices    |  | - /qe/strat |  | - zerodha- |   |   |
|  |  |   history   |  | - positions |  | - /qe/risk  |  |   api-key  |   |   |
|  |  | - trading-  |  | - orders    |  | - /qe/exec  |  | - zerodha- |   |   |
|  |  |   logs      |  | - strategy- |  |             |  |   secret   |   |   |
|  |  | - model-    |  |   state     |  | Alarms:     |  | - alpaca-  |   |   |
|  |  |   artifacts |  | - risk-     |  | - critical  |  |   api-key  |   |   |
|  |  |             |  |   state     |  | - warning   |  | - alpaca-  |   |   |
|  |  |             |  | - risk-     |  |             |  |   secret   |   |   |
|  |  |             |  |   config    |  | Dashboards: |  |             |   |   |
|  |  |             |  |             |  | - trading   |  |             |   |   |
|  |  |             |  |             |  | - infra     |  |             |   |   |
|  |  +-------------+  +-------------+  +-------------+  +-------------+   |   |
|  |                                                                        |   |
|  |  +-------------+  +-------------+                                      |   |
|  |  |    ECR      |  |    SNS      |                                      |   |
|  |  |             |  |             |                                      |   |
|  |  | Repos:      |  | Topics:     |                                      |   |
|  |  | - qe-data   |  | - critical  |---> Email: hari.mosoju@gmail.com    |   |
|  |  | - qe-strat  |  |   alerts    |---> SMS (optional)                  |   |
|  |  | - qe-risk   |  | - warning   |                                      |   |
|  |  | - qe-exec   |  |   alerts    |                                      |   |
|  |  +-------------+  +-------------+                                      |   |
|  |                                                                        |   |
|  +------------------------------------------------------------------------+   |
|                                                                               |
+===============================================================================+
```

---

## ECS Fargate Cluster

### Cluster: `quantembrace-prod`

All services run as ECS Fargate tasks within a single cluster. Each service is an
independent ECS Service with its own task definition, scaling policy, and health check.

### Service Definitions

#### Service 1: data-ingestion-nse

```
Task Definition:  qe-data-ingestion-nse
CPU:              256 (0.25 vCPU)
Memory:           512 MB
Image:            {account}.dkr.ecr.ap-south-1.amazonaws.com/qe-data:latest
Desired Count:    1
Min/Max:          1/1 (no scaling -- single WebSocket connection)
Health Check:     HTTP /health on port 8080
Schedule:         Active 08:45-16:00 IST (Mon-Fri, excluding NSE holidays)
Environment:
  MARKET=NSE
  DYNAMODB_TABLE_PRICES=latest-prices
  S3_BUCKET_HISTORY=quantembrace-market-data-history
  LOG_LEVEL=INFO
Secrets:
  KITE_API_KEY     -> arn:aws:secretsmanager:...:zerodha-api-key
  KITE_API_SECRET  -> arn:aws:secretsmanager:...:zerodha-api-secret
  KITE_ACCESS_TOKEN -> arn:aws:secretsmanager:...:zerodha-access-token
```

#### Service 2: data-ingestion-us

```
Task Definition:  qe-data-ingestion-us
CPU:              256 (0.25 vCPU)
Memory:           512 MB
Image:            {account}.dkr.ecr.ap-south-1.amazonaws.com/qe-data:latest
Desired Count:    1
Min/Max:          1/1
Health Check:     HTTP /health on port 8080
Schedule:         Active 19:00-06:30 IST (Mon-Fri, i.e., US market hours in IST)
Environment:
  MARKET=US
  DYNAMODB_TABLE_PRICES=latest-prices
  S3_BUCKET_HISTORY=quantembrace-market-data-history
  LOG_LEVEL=INFO
Secrets:
  ALPACA_API_KEY    -> arn:aws:secretsmanager:...:alpaca-api-key
  ALPACA_API_SECRET -> arn:aws:secretsmanager:...:alpaca-api-secret
```

#### Service 3: strategy-engine

```
Task Definition:  qe-strategy-engine
CPU:              512 (0.5 vCPU)
Memory:           1024 MB
Image:            {account}.dkr.ecr.ap-south-1.amazonaws.com/qe-strategy:latest
Desired Count:    1
Min/Max:          1/2 (scale up if strategy count grows)
Health Check:     HTTP /health on port 8080
Schedule:         Active during any market's open hours
Environment:
  DYNAMODB_TABLE_PRICES=latest-prices
  DYNAMODB_TABLE_STRATEGY_STATE=strategy-state
  S3_BUCKET_MODELS=quantembrace-model-artifacts
  STRATEGY_CONFIG_S3=s3://quantembrace-model-artifacts/config/strategies.yaml
  LOG_LEVEL=INFO
```

#### Service 4: risk-engine

```
Task Definition:  qe-risk-engine
CPU:              256 (0.25 vCPU)
Memory:           512 MB
Image:            {account}.dkr.ecr.ap-south-1.amazonaws.com/qe-risk:latest
Desired Count:    1
Min/Max:          1/1 (MUST be single instance for consistency)
Health Check:     HTTP /health on port 8080
Schedule:         Active during any market's open hours
Environment:
  DYNAMODB_TABLE_POSITIONS=positions
  DYNAMODB_TABLE_ORDERS=orders
  DYNAMODB_TABLE_RISK_STATE=risk-state
  DYNAMODB_TABLE_RISK_CONFIG=risk-config
  LOG_LEVEL=INFO
```

#### Service 5: execution-engine

```
Task Definition:  qe-execution-engine
CPU:              256 (0.25 vCPU)
Memory:           512 MB
Image:            {account}.dkr.ecr.ap-south-1.amazonaws.com/qe-exec:latest
Desired Count:    1
Min/Max:          1/1
Health Check:     HTTP /health on port 8080
Schedule:         Active during any market's open hours
Environment:
  DYNAMODB_TABLE_ORDERS=orders
  DYNAMODB_TABLE_POSITIONS=positions
  LOG_LEVEL=INFO
Secrets:
  KITE_API_KEY      -> arn:aws:secretsmanager:...:zerodha-api-key
  KITE_API_SECRET   -> arn:aws:secretsmanager:...:zerodha-api-secret
  KITE_ACCESS_TOKEN -> arn:aws:secretsmanager:...:zerodha-access-token
  ALPACA_API_KEY    -> arn:aws:secretsmanager:...:alpaca-api-key
  ALPACA_API_SECRET -> arn:aws:secretsmanager:...:alpaca-api-secret
```

---

## S3 Buckets

### quantembrace-market-data-history

```
Purpose:       Store historical tick/bar data for backtesting and ML
Region:        ap-south-1
Versioning:    Disabled (append-only writes, no overwrites)
Encryption:    SSE-S3 (AES-256)

Structure:
  {market}/{instrument}/{date}/{hour}/ticks_{minute}.parquet

Example:
  NSE/RELIANCE/2026-04-23/10/ticks_30.parquet
  US/AAPL/2026-04-22/14/ticks_00.parquet

Lifecycle Rules:
  - Standard:           0-90 days
  - Glacier Instant:    90-365 days
  - Glacier Deep:       365-1095 days
  - Delete:             After 1095 days (3 years)

Estimated Size:
  ~50 instruments x 5MB/day x 252 trading days = ~63 GB/year
```

### quantembrace-trading-logs

```
Purpose:       Archive structured logs from all services
Region:        ap-south-1
Versioning:    Disabled
Encryption:    SSE-S3

Structure:
  {service}/{date}/{hour}/logs.json.gz

Lifecycle Rules:
  - Standard:    0-90 days
  - Glacier:     90-365 days
  - Delete:      After 365 days

Note: Primary logs are in CloudWatch (30-day retention).
      This bucket is for long-term compliance/audit archival.
```

### quantembrace-model-artifacts

```
Purpose:       ML model files, feature datasets, strategy configs
Region:        ap-south-1
Versioning:    Enabled (critical -- need rollback for models)
Encryption:    SSE-S3

Structure:
  models/{model_name}/{version}/model.onnx
  models/{model_name}/{version}/metadata.json
  features/{date}/features.parquet
  config/strategies.yaml
  config/risk_params.yaml

Lifecycle Rules:
  - Keep all versions indefinitely (small total size)
  - Noncurrent versions: move to IA after 30 days

Estimated Size:
  Models: ~100 MB total (small models)
  Features: ~1 GB/year
```

---

## DynamoDB Tables

### latest-prices

```
Purpose:       Hot cache of current market prices
Billing:       On-Demand (pay per request)
Key:           market_instrument (String) -- e.g., "NSE#RELIANCE"
TTL:           expires_at (24 hours from write)

Attributes:
  market_instrument:  String  (PK)   "NSE#RELIANCE"
  ltp:                Number          2465.50
  bid:                Number          2465.00
  ask:                Number          2466.00
  volume:             Number          1234567
  timestamp:          String          "2026-04-23T10:30:00Z"
  expires_at:         Number          1745412600 (epoch)

Read Pattern:   BatchGetItem (strategy engine reads all instruments every 500ms)
Write Pattern:  PutItem (data ingestion writes on each tick)
Estimated RCU:  ~100/day (bursty during market hours)
Estimated WCU:  ~50,000/day (depends on instrument count)
```

### positions

```
Purpose:       Current open positions across all markets
Billing:       On-Demand
Key:           market_instrument (String) -- e.g., "NSE#RELIANCE"

Attributes:
  market_instrument:  String  (PK)   "NSE#RELIANCE"
  direction:          String          "LONG"
  quantity:           Number          100
  avg_entry_price:    Number          2450.50
  current_price:      Number          2465.00
  unrealized_pnl:     Number          1450.00
  realized_pnl:       Number          0.00
  stop_loss_price:    Number          2401.49
  opened_at:          String          "2026-04-23T10:15:00Z"
  last_updated:       String          "2026-04-23T11:30:00Z"
  strategy:           String          "momentum_breakout"

Read Pattern:   Scan (risk engine reads all positions frequently)
Write Pattern:  UpdateItem (execution engine updates on fills)
Estimated Items: 5-20 (small table, few open positions at any time)
```

### orders

```
Purpose:       Full order lifecycle tracking, idempotency
Billing:       On-Demand
Key:           order_id (String) -- UUID

GSI:           market_instrument-created_at-index
               (for querying orders by instrument and time)

Attributes:
  order_id:           String  (PK)   "abc-123-def-456"
  signal_id:          String          "sig-789"
  market:             String          "NSE"
  instrument:         String          "RELIANCE"
  direction:          String          "BUY"
  quantity:           Number          100
  order_type:         String          "LIMIT"
  limit_price:        Number          2450.00
  status:             String          "FILLED" | "PLACED" | "PENDING" | "FAILED" | "CANCELLED"
  broker_order_id:    String          "230423000123456"
  fill_price:         Number          2450.50
  fill_quantity:      Number          100
  strategy:           String          "momentum_breakout"
  created_at:         String          "2026-04-23T10:15:00Z"
  updated_at:         String          "2026-04-23T10:15:02Z"
  error:              String          null
  retry_count:        Number          0

Read Pattern:   GetItem by order_id (idempotency check)
Write Pattern:  PutItem + UpdateItem (create then update status)
```

### strategy-state

```
Purpose:       Persistent state for running strategies
Billing:       On-Demand
Key:           strategy_name (String)

Attributes:
  strategy_name:      String  (PK)   "momentum_breakout"
  state:              Map             {indicator values, counters, etc.}
  last_signal_at:     String          "2026-04-23T10:30:00Z"
  updated_at:         String          "2026-04-23T10:30:01Z"

Note: Strategies persist state here so they can resume after restart
      without losing computed indicator values.
```

### risk-state

```
Purpose:       Risk engine state (PnL tracking, kill switch, limits)
Billing:       On-Demand
Key:           state_key (String)

Example Items:
  { state_key: "kill_switch",        active: false, activated_at: null }
  { state_key: "daily_pnl_NSE_2026-04-23", value: -12500.00, currency: "INR" }
  { state_key: "daily_pnl_US_2026-04-22",  value: 340.50,    currency: "USD" }
  { state_key: "weekly_pnl_NSE_2026-W17",  value: 8500.00,   currency: "INR" }
```

### risk-config

```
Purpose:       Risk parameters (changeable at runtime without redeployment)
Billing:       On-Demand
Key:           config_key (String)

Example Items:
  { config_key: "max_positions_NSE",         value: 10 }
  { config_key: "max_positions_US",          value: 5 }
  { config_key: "max_daily_drawdown_pct",    value: 3.0 }
  { config_key: "max_weekly_drawdown_pct",   value: 7.0 }
  { config_key: "max_exposure_gross_pct",    value: 200.0 }
  { config_key: "max_exposure_net_pct",      value: 100.0 }
  { config_key: "max_per_instrument_pct",    value: 5.0 }
  { config_key: "margin_buffer_pct",         value: 20.0 }
  { config_key: "default_sl_intraday_pct",   value: 2.0 }
  { config_key: "default_sl_positional_pct", value: 5.0 }
```

---

## Networking Detail

### VPC Configuration

```
VPC CIDR:           10.0.0.0/16
Region:             ap-south-1 (Mumbai -- closest to NSE)

Subnets:
  Public  AZ-a:     10.0.1.0/24   (NAT Gateway lives here)
  Public  AZ-b:     10.0.2.0/24   (standby)
  Private AZ-a:     10.0.10.0/24  (primary ECS tasks)
  Private AZ-b:     10.0.20.0/24  (failover AZ)

Internet Gateway:   Yes (for NAT Gateway's outbound)
NAT Gateway:        1x in AZ-a (single NAT to save cost)
                    ~$35/month + data transfer

VPC Endpoints (Gateway -- free):
  - com.amazonaws.ap-south-1.s3
  - com.amazonaws.ap-south-1.dynamodb

VPC Endpoints (Interface -- ~$7.50/month each):
  - com.amazonaws.ap-south-1.logs          (CloudWatch Logs)
  - com.amazonaws.ap-south-1.secretsmanager
  - com.amazonaws.ap-south-1.ecr.api
  - com.amazonaws.ap-south-1.ecr.dkr

Security Groups:
  sg-ecs-tasks:
    Inbound:  None (no inbound from internet)
    Outbound: 443 to 0.0.0.0/0 (HTTPS to broker APIs via NAT)
              443 to VPC endpoint prefix lists (AWS services)
```

### Why ap-south-1 (Mumbai)?

- NSE datacenter is in Mumbai -- lowest latency for Zerodha API
- Alpaca API (US) will have higher latency (~200ms), but our strategies are not latency-sensitive
- If US latency becomes an issue, the US execution engine can be moved to us-east-1 as a separate deployment

---

## Monitoring and Alerting

### CloudWatch Log Groups

```
/quantembrace/data-ingestion-nse     (30-day retention)
/quantembrace/data-ingestion-us      (30-day retention)
/quantembrace/strategy-engine        (30-day retention)
/quantembrace/risk-engine            (30-day retention)
/quantembrace/execution-engine       (30-day retention)
```

### CloudWatch Dashboards

**Trading Dashboard:**
- Daily PnL (NSE + US)
- Open positions count
- Signals generated vs. approved vs. rejected
- Order fill rate
- Execution latency p50/p95/p99

**Infrastructure Dashboard:**
- ECS task status (running/stopped)
- CPU/memory utilization per task
- WebSocket connection status
- DynamoDB read/write capacity consumed
- S3 storage size
- Error rates per service

### CloudWatch Alarms

| Alarm Name                    | Metric                        | Threshold        | Period | Action               |
|-------------------------------|-------------------------------|-------------------|--------|----------------------|
| ws-nse-disconnected           | ws_connected (NSE)            | = 0               | 60s    | SNS critical         |
| ws-us-disconnected            | ws_connected (US)             | = 0               | 60s    | SNS critical         |
| high-order-failure-rate       | order_failures / order_total  | > 10%             | 5min   | SNS critical         |
| daily-drawdown-warning        | daily_pnl                     | < -80% of limit   | 1min   | SNS warning          |
| daily-drawdown-critical       | daily_pnl                     | < -100% of limit  | 1min   | SNS critical + kill  |
| ecs-task-crash                | ECS RunningTaskCount          | < expected        | 60s    | SNS critical         |
| strategy-no-signals           | signals_generated             | = 0               | 30min  | SNS warning          |
| high-execution-latency        | execution_latency_p99         | > 2000ms          | 5min   | SNS warning          |

---

## Cost Estimate (Detailed)

### Monthly Cost Breakdown (Production)

```
+-----------------------------------+-------------+---------------------------+
| Resource                          | Monthly ($) | Notes                     |
+-----------------------------------+-------------+---------------------------+
| ECS Fargate                       |             |                           |
|   data-ingestion-nse (7h x 22d)  |   $3.50     | 0.25 vCPU, 512MB          |
|   data-ingestion-us  (8h x 22d)  |   $4.00     | 0.25 vCPU, 512MB          |
|   strategy-engine    (15h x 22d) |  $15.00     | 0.50 vCPU, 1GB            |
|   risk-engine        (15h x 22d) |   $7.50     | 0.25 vCPU, 512MB          |
|   execution-engine   (15h x 22d) |   $7.50     | 0.25 vCPU, 512MB          |
| Subtotal ECS                      |  $37.50     |                           |
+-----------------------------------+-------------+---------------------------+
| NAT Gateway                       |  $35.00     | Fixed cost + data         |
|   Data processing (~10GB)         |   $5.00     | $0.045/GB                 |
| Subtotal NAT                      |  $40.00     |                           |
+-----------------------------------+-------------+---------------------------+
| DynamoDB (On-Demand)              |             |                           |
|   Write requests (~2M/month)      |   $2.50     | $1.25 per million         |
|   Read requests (~5M/month)       |   $1.25     | $0.25 per million         |
|   Storage (~1GB)                  |   $0.25     | $0.25/GB                  |
| Subtotal DynamoDB                 |   $4.00     |                           |
+-----------------------------------+-------------+---------------------------+
| S3                                |             |                           |
|   Storage (~10GB Standard)        |   $0.25     | $0.023/GB                 |
|   Requests (~100K PUT, 500K GET)  |   $0.75     |                           |
| Subtotal S3                       |   $1.00     |                           |
+-----------------------------------+-------------+---------------------------+
| VPC Endpoints (Interface, x4)     |  $30.00     | $7.50 each (avoidable*)   |
+-----------------------------------+-------------+---------------------------+
| CloudWatch                        |             |                           |
|   Logs (5GB ingestion)            |   $2.50     | $0.50/GB                  |
|   Custom metrics (20)             |   $6.00     | $0.30 each                |
|   Alarms (10)                     |   $1.00     | $0.10 each                |
|   Dashboard (2)                   |   $6.00     | $3.00 each                |
| Subtotal CloudWatch               |  $15.50     |                           |
+-----------------------------------+-------------+---------------------------+
| Secrets Manager (5 secrets)       |   $2.00     | $0.40 each                |
+-----------------------------------+-------------+---------------------------+
| ECR (image storage ~2GB)          |   $0.20     |                           |
+-----------------------------------+-------------+---------------------------+
|                                   |             |                           |
| TOTAL                             | ~$130.00    |                           |
+-----------------------------------+-------------+---------------------------+

* VPC Interface Endpoints can be removed if you route through NAT Gateway
  instead. This saves ~$30/month but adds NAT data processing charges and
  a small latency increase for AWS API calls. For a small trading system,
  routing through NAT is acceptable and saves money.

  Without VPC Interface Endpoints: ~$100/month
```

### Cost Optimization Opportunities

| Optimization                        | Savings     | Trade-off                          |
|-------------------------------------|-------------|------------------------------------|
| Remove VPC Interface Endpoints      | ~$30/month  | Slightly higher latency to AWS APIs|
| Replace NAT Gateway with NAT Instance (t4g.nano) | ~$30/month  | Self-managed, less reliable |
| Use Fargate Spot for strategy engine| ~$5/month   | Possible interruptions (risky)     |
| Reduce CloudWatch dashboards to 1  | $3/month    | Less visibility                    |
| Reduce custom metrics              | $3/month    | Less monitoring granularity        |

**Recommended minimum setup: ~$70-100/month**

---

## Terraform Module Structure

```
terraform/
  main.tf                  # Provider config, backend (S3 + DynamoDB lock)
  variables.tf             # All configurable variables
  outputs.tf               # VPC ID, ECS cluster ARN, etc.
  
  modules/
    vpc/
      main.tf              # VPC, subnets, NAT, IGW, route tables
      endpoints.tf         # VPC endpoints for S3, DynamoDB, etc.
      security_groups.tf   # Security group definitions
      variables.tf
      outputs.tf
      
    ecs/
      cluster.tf           # ECS cluster definition
      services.tf          # 5 ECS service definitions
      task_definitions.tf  # 5 task definitions with container configs
      iam.tf               # Task execution role, task role
      scaling.tf           # Scheduled scaling (market hours)
      variables.tf
      outputs.tf
      
    storage/
      s3.tf                # 3 S3 buckets with lifecycle policies
      dynamodb.tf          # 6 DynamoDB tables
      variables.tf
      outputs.tf
      
    monitoring/
      cloudwatch.tf        # Log groups, metrics, dashboards
      alarms.tf            # CloudWatch alarms
      sns.tf               # SNS topics and subscriptions
      variables.tf
      outputs.tf
      
    secrets/
      secrets_manager.tf   # Secret definitions
      variables.tf
      outputs.tf

  environments/
    prod/
      terraform.tfvars     # Production variable values
      backend.tf           # S3 backend config for prod state
    staging/
      terraform.tfvars     # Staging variable values (paper trading)
      backend.tf
```

---

## Disaster Recovery

### Failure Scenarios

| Scenario                          | Impact            | Recovery                              |
|-----------------------------------|-------------------|---------------------------------------|
| Single ECS task crash             | Service restarts  | ECS auto-restarts, state in DynamoDB  |
| AZ failure                        | Services affected | ECS schedules in alternate AZ         |
| NAT Gateway failure               | No outbound       | Failover NAT in AZ-b (manual today)  |
| DynamoDB throttling               | Slow reads/writes | On-demand scales automatically        |
| Broker API outage                 | Cannot trade      | Kill switch activates, wait for broker|
| Region failure (ap-south-1 down)  | Full outage       | Manual failover to alternate region   |

### Recovery Priority

1. **Risk engine** -- must come up first to enforce kill switch state
2. **Data ingestion** -- need market data to make decisions
3. **Strategy engine** -- resumes signal generation
4. **Execution engine** -- resumes order placement (pending orders in DynamoDB)

All state is in DynamoDB (multi-AZ by default). No in-memory state that cannot be
recovered from DynamoDB. This is a deliberate design choice.
