<!--
╔══════════════════════════════════════════════════════════════════════╗
║  ARCHIVED DOCUMENT — DO NOT USE AS IMPLEMENTATION REFERENCE         ║
║                                                                      ║
║  This document describes the Phase 1 plan to migrate THREE services  ║
║  (data_ingestion, strategy_engine, execution_engine) to EC2.         ║
║                                                                      ║
║  CURRENT STATE (Phase 7 complete):                                   ║
║  ALL FIVE services run on EC2 ARM64 ASGs. risk_engine and ai_engine  ║
║  (described below as "staying on Fargate") have since migrated to    ║
║  EC2. SQS has been fully removed. System is Kafka-native.           ║
║                                                                      ║
║  Source of truth for current architecture: architecture/system_design.md
║  Any SQS, Fargate, or dual-path references below describe a         ║
║  historical Phase 1 plan that no longer reflects the codebase.       ║
╚══════════════════════════════════════════════════════════════════════╝
-->

# Phase 1 — EC2 Backbone Migration

**System**: QuantEmbrace V2  
**Phase**: 1 of 7  
**Status**: Design Complete — Ready for Implementation  
**Author**: Principal Quant Architect  
**Date**: 2026-04-29  
**Supersedes**: ADR-001 (ECS Fargate Over Lambda) — partial supersession for latency-critical services  

---

## Executive Summary

Phase 1 migrates three latency-critical services from ECS Fargate to EC2-backed Auto Scaling Groups. The risk engine and AI engine remain on Fargate. SQS messaging is preserved unchanged. No Kafka is introduced in this phase.

The primary driver is architectural maturity: hedge-fund grade platforms run latency-critical workloads on dedicated EC2 with kernel-tunable network stacks, placement groups, and predictable CPU. Fargate's virtualization layer and shared tenancy introduce jitter that is unacceptable as the system scales toward institutional standards.

**Services migrating to EC2**: `data_ingestion`, `strategy_engine`, `execution_engine`  
**Services staying on Fargate**: `risk_engine`, `ai_engine`

---

## 1. Architecture Design

### 1.1 Service-to-Compute Mapping

```
+-------------------------------------------------------------------+
|              PHASE 1 HYBRID COMPUTE ARCHITECTURE                  |
+-------------------------------------------------------------------+
|                                                                   |
|  LATENCY-CRITICAL (EC2 ASG)          MANAGED (ECS Fargate)       |
|  --------------------------------    -------------------------    |
|  data-ingestion-nse  t4g.medium      risk-engine   0.25vCPU      |
|  data-ingestion-us   t4g.medium      ai-engine     0.25vCPU      |
|  strategy-engine     c6g.large                                    |
|  execution-engine    c6g.large                                    |
|                                                                   |
|  MESSAGING (unchanged)               STATE (unchanged)           |
|  --------------------------------    -------------------------    |
|  SQS: market-data queue              DynamoDB: all tables        |
|  SQS: signals queue                  S3: all buckets             |
|  SQS: approved-signals queue                                      |
+-------------------------------------------------------------------+
```

### 1.2 Instance Type Selection

All EC2 instances use **AWS Graviton3 ARM architecture** (c6g/t4g families). Graviton provides
15–40% better price/performance vs. Intel/AMD equivalents for Python workloads and is fully
supported in ap-south-1 (Mumbai).

| Service | Instance | vCPU | RAM | Network | On-Demand/hr | Rationale |
|---|---|---|---|---|---|---|
| data-ingestion-nse | `t4g.medium` | 2 | 4 GB | Up to 5 Gbps | $0.0336 | Burstable, WebSocket I/O bound. T4g credits handle ingest bursts without paying for sustained c-class. |
| data-ingestion-us | `t4g.medium` | 2 | 4 GB | Up to 5 Gbps | $0.0336 | Same profile as NSE ingest. |
| strategy-engine | `c6g.large` | 2 | 4 GB | Up to 10 Gbps | $0.0680 | Compute-optimized ARM. Sustained CPU for indicator math, SQS polling, signal generation. No bursting. |
| execution-engine | `c6g.large` | 2 | 4 GB | Up to 10 Gbps | $0.0680 | Compute-optimized, predictable CPU. Order placement is latency-sensitive — no burstable credits. |

**Why not c6gn (enhanced networking variant)?**  
c6gn provides up to 25 Gbps but costs 27% more. Our bottleneck is broker API latency (~50-100ms
to NSE, ~200ms to Alpaca US), not NIC throughput. Add c6gn in Phase 3 when order throughput
increases with horizontal scaling.

**Why not r6g (memory-optimized)?**  
Strategy engine in-memory state (indicator windows, loaded models) fits in 4 GB comfortably for
our current instrument universe. Re-evaluate at 100+ instruments or when the AI engine moves
to the strategy engine process.

### 1.3 Auto Scaling Group Configuration

Each service gets its own ASG for independent lifecycle management.

```
+--------------------------------------------------------------------+
| Service            | Min | Max | Desired | Scaling Policy         |
+--------------------+-----+-----+---------+------------------------+
| data-ingestion-nse |  1  |  1  |    1    | No scaling (WebSocket) |
| data-ingestion-us  |  1  |  1  |    1    | No scaling (WebSocket) |
| strategy-engine    |  1  |  2  |    1    | CPU > 70% for 5min     |
| execution-engine   |  1  |  1  |    1    | No scaling (order lock) |
+--------------------------------------------------------------------+
```

**Why Min=1 for data ingestion?**  
Each ingest service holds a single WebSocket connection to the broker. Two instances would create
duplicate tick processing and double-publish to SQS. Singleton constraint is enforced at the ASG
level (Max=1) not in application code.

**Why Max=2 for strategy engine?**  
Strategy computation is stateless per tick (state lives in DynamoDB). Two instances can safely
consume from the SQS market-data queue in parallel when signal load is high. This is Phase 3
territory but the ASG ceiling is set now.

**Why Max=1 for execution engine?**  
The execution engine must be a singleton for correct idempotency guarantees. Two instances racing
to place the same approved signal could produce duplicate orders despite DynamoDB conditional
writes (race window exists between the check and the write). Singleton enforced at ASG level.

### 1.4 Placement Groups

```
+------------------------------------------+
| Placement Group Strategy                 |
+------------------------------------------+
| execution-engine: CLUSTER placement      |
|   → co-located in same physical rack     |
|   → lowest latency to DynamoDB endpoint  |
|   → lowest intra-AZ network jitter       |
|                                           |
| data-ingestion: SPREAD placement         |
|   → across separate hardware             |
|   → fault isolation per AZ              |
|                                           |
| strategy-engine: no placement group      |
|   → allows ASG to scale across AZs      |
+------------------------------------------+
```

**Placement group detail**: The execution engine's cluster placement group colocates the EC2
instance on the same physical host rack as nearby DynamoDB endpoint traffic. This shaves 1-3ms
from the risk state read path (kill switch check + position lookup) that happens for every order.

### 1.5 OS and Runtime Configuration

All instances run **Amazon Linux 2023 (AL2023)** ARM64 with:

- **Kernel parameters tuned for low-latency networking:**
  ```
  net.core.rmem_max = 134217728
  net.core.wmem_max = 134217728
  net.ipv4.tcp_rmem = 4096 87380 134217728
  net.ipv4.tcp_wmem = 4096 65536 134217728
  net.ipv4.tcp_no_delay = 1
  net.core.netdev_max_backlog = 5000
  ```
- **IMDSv2 enforced** — no IMDSv1 access
- **SSM Session Manager** — operator shell access without bastion host or SSH keys
- **CloudWatch Agent** — metrics and logs identical to Fargate format
- **Docker** — services run as containers pulled from ECR (same images as Fargate)
- **systemd service unit** — containers auto-start on instance boot and auto-restart on failure

---

## 2. Networking Considerations

### 2.1 VPC Placement

EC2 instances drop into the **same private subnets** as the current Fargate tasks. No VPC
changes required.

```
Private AZ-a: 10.0.10.0/24  → data-ingestion-nse, strategy-engine, execution-engine
Private AZ-b: 10.0.20.0/24  → ASG failover AZ (strategy-engine max=2 can land here)
```

### 2.2 Security Groups

New security group `sg-ec2-trading-services` derived from existing `sg-ecs-tasks`:

```
Inbound Rules:
  Port 8080  →  sg-internal-alb   (health check from internal ALB)
  Port 8080  →  10.0.0.0/16       (VPC-internal health polling)
  All traffic → sg-ec2-trading-services (self-referencing, intra-service comms)

Outbound Rules:
  Port 443   →  0.0.0.0/0         (HTTPS to broker APIs via NAT GW)
  Port 443   →  VPC endpoint prefix lists (S3, DynamoDB, SQS, CloudWatch)
  Port 443   →  ECR interface endpoint  (image pulls)
```

Fargate `sg-ecs-tasks` stays unchanged — risk-engine and ai-engine are unaffected.

### 2.3 Service Discovery

Existing **AWS Cloud Map** namespace `quantembrace.internal` registers EC2 instances via
ASG lifecycle hooks. Health checks use HTTP `/health` endpoint.

EC2 instances register at launch, deregister at termination. Services discover each other by
DNS (e.g., `risk-engine.quantembrace.internal`) — the risk engine Fargate IP and EC2 strategy
engine IP are both resolvable through the same namespace.

### 2.4 NAT Gateway

No change. All four EC2 services route outbound traffic (broker API calls) through the existing
NAT Gateway in the public subnet. Data transfer cost increases slightly with higher tick throughput
but remains within the existing $5/month estimate.

### 2.5 SSM Session Manager Access

No bastion host. No SSH keypairs. Operators use SSM Session Manager via the AWS console or CLI:

```bash
# Open shell to execution engine instance
aws ssm start-session --target <instance-id> --region ap-south-1

# Run a diagnostic command
aws ssm send-command \
  --instance-ids <instance-id> \
  --document-name "AWS-RunShellScript" \
  --parameters '{"commands":["journalctl -u quantembrace-execution-engine --since -1h"]}'
```

IAM policies restrict SSM access to operators with the `QuantEmbraceOperator` IAM role.

### 2.6 IMDSv2 Token Flow

All application code that reads instance metadata (region, instance ID for logging) must use
IMDSv2 with a token. Add this helper to `shared/aws/metadata.py`:

```python
import aiohttp

async def get_instance_metadata(path: str) -> str:
    """Fetch EC2 instance metadata using IMDSv2."""
    async with aiohttp.ClientSession() as session:
        # Step 1: get token
        token_resp = await session.put(
            "http://169.254.169.254/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        token = await token_resp.text()
        # Step 2: fetch metadata
        meta_resp = await session.get(
            f"http://169.254.169.254/latest/meta-data/{path}",
            headers={"X-aws-ec2-metadata-token": token},
        )
        return await meta_resp.text()
```

---

## 3. Migration Steps (Fargate → EC2)

Migration is **blue/green per service**. Fargate tasks remain running throughout. Each service
is cut over independently in three stages: deploy, validate, decommission.

### 3.1 Pre-Migration Checklist

- [ ] EC2 Terraform module reviewed and applied to staging
- [ ] All three Docker images pass health checks on EC2 instances
- [ ] CloudWatch agent emitting metrics from EC2 instances
- [ ] SSM Session Manager access verified for all instances
- [ ] Staging traffic routed through EC2 for one full trading day without errors

### 3.2 Migration Order

**Order is mandatory.** Migrate from lowest risk to highest risk:

```
Step 1: data-ingestion-nse     (lowest risk — no order placement)
Step 2: data-ingestion-us      (lowest risk — no order placement)
Step 3: strategy-engine        (medium risk — signal generation only)
Step 4: execution-engine       (highest risk — actual order placement)
```

Never migrate the execution engine before validating strategy engine on EC2.

### 3.3 Step-by-Step Per Service

#### STEP 1 — Deploy EC2 Service in Parallel

```bash
# Apply Terraform EC2 module for this service
cd infra/terraform/environments/prod
terraform apply -target=module.ec2_services.aws_autoscaling_group.<service>

# Verify instance launched and healthy
aws ec2 describe-instances \
  --filters "Name=tag:Service,Values=data-ingestion-nse" \
            "Name=instance-state-name,Values=running" \
  --query "Reservations[].Instances[].{ID:InstanceId,IP:PrivateIpAddress,State:State.Name}"

# Verify container running
aws ssm start-session --target <instance-id>
> sudo systemctl status quantembrace-data-ingestion
> sudo docker logs quantembrace-data-ingestion --tail 50
```

Both Fargate task AND EC2 instance are running simultaneously at this point.
Both are publishing to SQS — duplicate ticks will be produced temporarily.

**For data ingestion only**: set the Fargate task desired count to 0 BEFORE starting EC2
(WebSocket singleton — cannot have two connections to the same broker feed).

```bash
aws ecs update-service \
  --cluster quantembrace-prod \
  --service data-ingestion-nse \
  --desired-count 0
```

Then start EC2 ASG desired count = 1.

#### STEP 2 — Validate EC2 Service

Run validation for a minimum of **1 full trading day** before proceeding:

```bash
# Check tick throughput matches historical baseline
aws cloudwatch get-metric-statistics \
  --namespace QuantEmbrace \
  --metric-name ticks_processed_per_minute \
  --dimensions Name=Service,Value=data-ingestion-nse \
               Name=ComputeType,Value=EC2 \
  --start-time $(date -u -d "1 day ago" +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 --statistics Average

# Check error rate is zero
aws logs filter-log-events \
  --log-group-name /quantembrace/data-ingestion-nse \
  --filter-pattern "ERROR" \
  --start-time $(date -d "1 day ago" +%s)000

# Verify SQS queue depth is healthy (not growing)
aws sqs get-queue-attributes \
  --queue-url <market-data-queue-url> \
  --attribute-names ApproximateNumberOfMessages
```

**Pass criteria before proceeding:**
- Zero ERROR-level log lines
- Tick throughput within ±5% of Fargate baseline
- SQS queue depth not growing (messages consumed as fast as produced)
- CloudWatch alarms in OK state for 4+ hours

#### STEP 3 — Decommission Fargate Task

After 1 full validated trading day on EC2:

```bash
# For services other than data-ingestion (already set to 0 in Step 1):
# Set Fargate desired count to 0
aws ecs update-service \
  --cluster quantembrace-prod \
  --service <service-name> \
  --desired-count 0

# After 3 days of stable EC2 operation, delete the Fargate service
terraform apply -target=module.ecs_services.<service> -var="desired_count=0"
# (Do not destroy the task definition — keep for rollback reference)
```

#### STEP 4 — Rollback Procedure (if needed)

If EC2 service fails at any point:

```bash
# Set EC2 ASG desired count to 0
aws autoscaling update-auto-scaling-group \
  --auto-scaling-group-name quantembrace-<service>-asg \
  --desired-capacity 0

# Restore Fargate task
aws ecs update-service \
  --cluster quantembrace-prod \
  --service <service-name> \
  --desired-count 1

# Rollback takes <60 seconds (Fargate task starts in ~30s)
```

---

## 4. Cost Comparison

### 4.1 Current Fargate Costs (3 Latency-Critical Services)

Market hours only — services scheduled on/off via ECS scheduled scaling.

```
+-------------------------------+----------+-----------------------------+
| Service                       | $/month  | Calculation                 |
+-------------------------------+----------+-----------------------------+
| data-ingestion-nse            |  $3.50   | 0.25vCPU×7h×22d + 512MB    |
| data-ingestion-us             |  $4.00   | 0.25vCPU×8h×22d + 512MB    |
| strategy-engine               | $15.00   | 0.5vCPU×15h×22d + 1GB      |
| execution-engine              |  $7.50   | 0.25vCPU×15h×22d + 512MB   |
+-------------------------------+----------+-----------------------------+
| Subtotal (3 services)         | $30.00   |                             |
+-------------------------------+----------+-----------------------------+
```

### 4.2 EC2 Costs — Three Scenarios

**Scenario A: On-Demand, Market Hours Only (scheduled stop/start via Lambda or Instance Scheduler)**

```
+-------------------------------+----------+-----------------------------+
| Service                       | $/month  | Calculation                 |
+-------------------------------+----------+-----------------------------+
| data-ingestion-nse (t4g.med)  |  $5.17   | $0.0336 × 154h (7h×22d)    |
| data-ingestion-us  (t4g.med)  |  $5.91   | $0.0336 × 176h (8h×22d)    |
| strategy-engine    (c6g.lg)   | $22.44   | $0.0680 × 330h (15h×22d)   |
| execution-engine   (c6g.lg)   | $22.44   | $0.0680 × 330h (15h×22d)   |
+-------------------------------+----------+-----------------------------+
| Subtotal                      | $55.96   | +87% vs Fargate             |
+-------------------------------+----------+-----------------------------+
```

**Scenario B: 1-Year Reserved Instances, Market Hours Only**

```
+-------------------------------+----------+-----------------------------+
| Service                       | $/month  | Calculation                 |
+-------------------------------+----------+-----------------------------+
| data-ingestion-nse (t4g.med)  |  $3.23   | $0.021×154h (RI effective)  |
| data-ingestion-us  (t4g.med)  |  $3.70   | $0.021×176h                |
| strategy-engine    (c6g.lg)   | $14.19   | $0.043×330h (RI effective)  |
| execution-engine   (c6g.lg)   | $14.19   | $0.043×330h                |
+-------------------------------+----------+-----------------------------+
| Subtotal                      | $35.31   | +18% vs Fargate             |
+-------------------------------+----------+-----------------------------+
```

**Scenario C: Spot Instances for Strategy Engine + On-Demand for others**

```
+-------------------------------+----------+-----------------------------+
| Service                       | $/month  | Calculation                 |
+-------------------------------+----------+-----------------------------+
| data-ingestion-nse (t4g.med)  |  $5.17   | On-demand                  |
| data-ingestion-us  (t4g.med)  |  $5.91   | On-demand                  |
| strategy-engine (c6g.lg Spot) |  $6.73   | $0.0204×330h (~70% saving) |
| execution-engine   (c6g.lg)   | $22.44   | On-demand (Spot too risky) |
+-------------------------------+----------+-----------------------------+
| Subtotal                      | $40.25   | +34% vs Fargate             |
+-------------------------------+----------+-----------------------------+
```

### 4.3 Cost vs. Capability Trade-off

```
+------------------+----------+----------+--------+----------+----------+
| Option           | Cost/mo  | Latency  | Kernel | Placement| RI Lock-in|
+------------------+----------+----------+--------+----------+-----------+
| Fargate (current)| $30.00   | ~5-20ms  | No     | No       | None      |
| EC2 On-Demand    | $55.96   | ~1-5ms   | Yes    | Yes      | None      |
| EC2 Reserved 1yr | $35.31   | ~1-5ms   | Yes    | Yes      | 12 months |
| EC2 Spot+OnDem   | $40.25   | ~1-5ms   | Yes    | Yes      | None      |
+------------------+----------+----------+--------+----------+-----------+
```

**Recommendation for Phase 1**: Start with **On-Demand** (Scenario A) — no commitment while
validating EC2 behavior. After 30 days of stable production, purchase **1-year Reserved** for
data-ingestion instances (low churn) and keep strategy/execution on On-Demand until Phase 3
horizontal scaling solidifies the instance type choice.

### 4.4 Total System Cost After Phase 1

```
+----------------------------------+---------------+
| Component                        | Monthly Cost  |
+----------------------------------+---------------+
| EC2: data-ingestion (x2 t4g.med) |   $11.08      |
| EC2: strategy-engine (c6g.lg)    |   $22.44      |
| EC2: execution-engine (c6g.lg)   |   $22.44      |
| Fargate: risk-engine             |    $7.50      |
| Fargate: ai-engine               |    ~$5.00     |
| NAT Gateway + data transfer      |   $40.00      |
| DynamoDB                         |    $4.00      |
| S3                               |    $1.00      |
| VPC Endpoints (Interface x7)     |   $35.00      |
| CloudWatch                       |   $15.50      |
| Secrets Manager                  |    $2.00      |
| ECR                              |    $0.20      |
+----------------------------------+---------------+
| TOTAL PHASE 1                    |  ~$166/month  |
| vs Current (~$130/month)         |  +$36/month   |
+----------------------------------+---------------+

The $36/month premium buys:
  ✓ 3–10ms lower order-path latency
  ✓ Kernel-tunable TCP stack (no Fargate virtualization overhead)
  ✓ Cluster placement group for execution engine
  ✓ Self-healing ASGs with warm pools (instant restart vs 30s Fargate cold start)
  ✓ Foundation for Kafka on persistent EBS volumes (Phase 2)
  ✓ Foundation for horizontal EC2 scaling (Phase 3)
```

---

## 5. Risks and Trade-offs

### 5.1 Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| EC2 instance failure | Low | High | ASG auto-replaces within 2-3 min. Warm pool reduces to <60s. |
| Data ingestion duplicate ticks during cutover | High (certain) | Low | Set Fargate desired=0 BEFORE EC2 desired=1. Brief gap (<30s) acceptable. |
| Strategy engine SQS race (2 instances) | Low | Medium | DynamoDB conditional writes prevent duplicate signal submission. Test in staging. |
| EC2 Spot interruption (if Spot used) | Medium | High | Only use Spot for strategy engine. Data ingestion and execution use On-Demand. |
| Kernel param misconfiguration | Low | Medium | Applied via UserData at launch. Tested in staging. Immutable once deployed (no live edits). |
| IAM instance profile over-permissioned | Low | High | Least-privilege policy, same permissions as Fargate task role. Reviewed by ADR-004 standards. |
| EC2 cost overrun (instances left running overnight) | Medium | Low | Instance Scheduler or Lambda stops instances at market close. CloudWatch billing alarm at $200. |

### 5.2 Architectural Trade-offs

**Trade-off 1: Operational complexity vs. latency**  
EC2 requires OS management, patching, and instance health monitoring that Fargate handles
invisibly. We accept this overhead because:
- AL2023 AMI patching is automated via SSM Patch Manager (scheduled weekly)
- Instance replacement via ASG is the patching mechanism — no in-place patching
- CloudWatch Agent provides equivalent observability to Fargate Container Insights

**Trade-off 2: Startup time**  
Fargate tasks start in ~30 seconds. EC2 ASG replacement takes 2-3 minutes (instance init +
Docker pull + service startup). Mitigated with ASG warm pools — pre-warmed instances stand
by and reduce failover to <60 seconds.

**Trade-off 3: Risk engine stays on Fargate**  
The risk engine is the consistency-critical singleton. Moving it to EC2 gains nothing in latency
(signals flow TO it via SQS, not time-critical) and introduces OS management overhead for a
service that is already reliable. The risk engine migrates in Phase 4 when the distributed risk
engine redesign justifies the operational change.

---

## 6. Updated Architecture State

### 6.1 Phase 1 Signal Flow (unchanged)

```
data_ingestion (EC2)
       │ SQS: market-data-queue
       ▼
strategy_engine (EC2)
       │ SQS: signals-queue
       ▼
risk_engine (FARGATE) ← CRITICAL GATE — unchanged
       │ SQS: approved-signals-queue
       ▼
execution_engine (EC2)
       │
  ┌────┴────┐
  ▼         ▼
Zerodha   Alpaca
 (NSE)     (US)
```

The trade flow is unchanged. Only the compute substrate changes for three services.

### 6.2 Files Changed in Phase 1

```
NEW:
  docs/phase1_ec2_migration.md              ← this document
  infra/terraform/modules/ec2_services/
    main.tf                                  ← launch templates, ASGs, placement groups
    iam.tf                                   ← instance profiles (replaces task roles)
    variables.tf
    outputs.tf
    userdata/bootstrap.sh                    ← shared bootstrap script
    userdata/data_ingestion.sh               ← service-specific startup
    userdata/strategy_engine.sh
    userdata/execution_engine.sh

UPDATED:
  architecture/infra_diagram.md             ← hybrid EC2/Fargate diagram
  memory/decisions.md                       ← ADR-009 added

UNCHANGED:
  All Python service code                   ← zero application changes required
  infra/terraform/modules/ecs/              ← risk-engine and ai-engine unaffected
  infra/terraform/modules/storage/          ← no storage changes
  infra/terraform/modules/monitoring/       ← CloudWatch alarms unchanged
  .github/workflows/                        ← CI/CD unchanged (same Docker images)
```

---

## 7. Phase 1 Acceptance Criteria

Phase 1 is complete when all of the following are true for a minimum of **5 consecutive trading days**:

- [ ] All 3 EC2 services running with zero ERROR-level logs during market hours
- [ ] Tick throughput within ±5% of Fargate baseline
- [ ] Signal generation rate unchanged vs. Fargate baseline
- [ ] Order placement latency p99 < 500ms (measure via CloudWatch metric `execution_latency_p99`)
- [ ] Zero duplicate orders produced
- [ ] Kill switch functional and tested (manual activate/deactivate via CLI)
- [ ] All Fargate tasks for migrated services at desired_count=0
- [ ] ASG warm pools configured and tested (replacement time < 60s)
- [ ] SSM access verified for all EC2 instances
- [ ] 1-year Reserved Instance purchase decision made (buy or defer)

---

## 8. Next Phase Preview

**Phase 2: Introduce Kafka Streaming Core**

With EC2 now providing persistent compute and EBS-backed storage, Phase 2 introduces Apache
Kafka as the streaming backbone:

- Replace SQS market-data queue with Kafka topic `market.ticks`
- Replace SQS signals queue with Kafka topic `strategy.signals`
- SQS approved-signals queue retained for risk→execution path (lower volume, simpler)
- Kafka enables: tick replay, exactly-once delivery guarantees, stream processing with KSQL,
  consumer group semantics for horizontal strategy scaling

EC2 instances (Phase 1) are the prerequisite for Kafka because Kafka brokers require persistent
local disk (EBS) which Fargate cannot provide.
