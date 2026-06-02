# QuantEmbrace — Documentation

Welcome to the QuantEmbrace documentation. This is the single source of truth for understanding, developing, and operating the platform.

**Current architecture:** EC2 ARM64 Auto Scaling Groups + Kafka MSK Serverless. All documents reflect this current state.

---

## Quick Navigation by Role

| I am... | Reading path |
|---|---|
| **Total newcomer / curious person** | Start here → [01_introduction.md](01_introduction.md) → [02_architecture.md](02_architecture.md) |
| **Developer joining the team** | [01_introduction.md](01_introduction.md) → [02_architecture.md](02_architecture.md) → [04_services.md](04_services.md) → [05_local_setup.md](05_local_setup.md) → [07_contributing.md](07_contributing.md) |
| **Setting up locally for the first time** | [05_local_setup.md](05_local_setup.md) |
| **Running paper trading on Monday morning** | `make monday` — see [05_local_setup.md](05_local_setup.md) |
| **Working on AWS / infrastructure** | [06_aws_infrastructure.md](06_aws_infrastructure.md) |
| **Adding a new trading strategy** | [03_signal_lifecycle.md](03_signal_lifecycle.md) → [07_contributing.md](07_contributing.md#how-to-add-a-new-strategy) |
| **Debugging a live or paper trading issue** | [04_services.md](04_services.md) (per-service debugging section) |
| **Going live with real capital** | [runbooks/go_live_checklist.md](runbooks/go_live_checklist.md) |
| **On-call / daily operations** | [../runbooks/daily_operations.md](../runbooks/daily_operations.md) |
| **Looking up a term** | [07_contributing.md#glossary--trading-terms](07_contributing.md#glossary--trading-terms) |

---

## Document Index

| Doc | What it covers |
|---|---|
| **[hld.md](hld.md)** | **High-Level Design — system purpose, 6-layer architecture, component interactions, technology rationale, AWS deployment, failure model, cost model, key decisions** |
| **[lld.md](lld.md)** | **Low-Level Design — internal class hierarchies, Kafka message schemas, DynamoDB table schemas, state machines, idempotency, retry policy, Python type contracts** |
| [01_introduction.md](01_introduction.md) | What is algo trading, what QuantEmbrace does, markets, big-picture trade flow, design philosophy, system goals |
| [02_architecture.md](02_architecture.md) | 6-layer architecture deep-dive, how layers communicate via Kafka topics, failure modes, the golden rule |
| [03_signal_lifecycle.md](03_signal_lifecycle.md) | Complete trade lifecycle — tick → signal → AI enrichment → risk → order → fill. Kill switch, error paths, order state machine |
| [04_services.md](04_services.md) | Per-service reference: key files, configuration, Kafka connections, debugging, failure modes |
| [05_local_setup.md](05_local_setup.md) | Step-by-step local setup: prerequisites, .env, Docker Compose, running services, tests, backtesting |
| [06_aws_infrastructure.md](06_aws_infrastructure.md) | AWS services, EC2 ARM64 ASGs, MSK Serverless, cost breakdown, Terraform structure, monitoring, deployment |
| [07_contributing.md](07_contributing.md) | How to add strategies/brokers, risk rules checklist, git workflow, code standards, common mistakes, glossary, FAQ |
| [runbooks/go_live_checklist.md](runbooks/go_live_checklist.md) | Gate checklist for promoting from paper to live capital deployment |
| [../runbooks/daily_operations.md](../runbooks/daily_operations.md) | Daily pre-market, intraday, and post-market operating procedures |
| [../runbooks/phase5_operations.md](../runbooks/phase5_operations.md) | Phase 5 paper trading validation — checks, dashboards, Kafka map, exit criteria |

---

## Architecture at a Glance

```
Zerodha Kite Ticker   Alpaca WebSocket
  (NSE real-time)      (US real-time)
         │                   │
         └─────────┬──────────┘
                   │
        [ data_ingestion ]          Layer 1: normalises ticks, writes S3 + DynamoDB
                   │
          Kafka: ticks.nse
                  ticks.us
                   │
        [ strategy_engine ]         Layer 2: runs 6 strategies, generates signals
                   │
          Kafka: signals.pending
                   │
          [ ai_engine ]             Layer 5: enriches signals with ML predictions
                   │
          Kafka: signals.enriched
                   │
        [ risk_engine ] ◄◄◄         Layer 4: CRITICAL GATE — every signal validated
                   │                          No bypass path exists
          Kafka: signals.approved
                   │
        [ execution_engine ]        Layer 3: routes orders to correct broker
             │           │
        Zerodha API   Alpaca API
          (NSE)          (US)
                   │
          Kafka: orders.events       → DynamoDB order state, S3 execution logs
```

**The non-negotiable rule:** Every signal flows `Strategy → AI → Risk → Execution`. No bypass. No shortcuts.

---

## System Vitals

| Property | Value |
|---|---|
| Language | Python 3.11+ |
| Compute | AWS EC2 ARM64 Auto Scaling Groups (c6g/t4g) |
| Messaging | Kafka MSK Serverless — SASL/OAUTHBEARER IAM, port 9098 |
| Brokers | Zerodha Kite Connect (NSE India), Alpaca (US equities) |
| Markets | NSE India + US equities (NYSE, NASDAQ) |
| State storage | AWS DynamoDB (orders, positions, risk state, sessions) |
| Data storage | AWS S3 (historical ticks, audit logs, ML model artifacts) |
| Infrastructure | Terraform |
| Local dev stack | Docker Compose, Redpanda (Kafka), LocalStack (AWS) |
| Monday startup | `make monday` |
| Services | 5 microservices (data_ingestion, strategy_engine, ai_engine, risk_engine, execution_engine) |
| Strategies | 6 (Momentum, ORB, Scalp1m, VWAPReversion, IntradayTrend15m, PreCloseMomentum) |
| Risk validators | 11 (in order: SignalAge, KillSwitch, Position, Exposure, DailyLoss, Margin, Slippage, SpreadGate, Sector, Liquidity, Quality) |
| Order idempotency | UUID `signal_id` + DynamoDB conditional writes |
| Kill switch storage | DynamoDB `kill-switch` table |
| Tick storage | Parquet on S3, partitioned by market/symbol/date/hour |
| Paper trading | Built-in simulator — deterministic fills, full DynamoDB + Kafka write path |

---

## Monday Morning in 30 Seconds

```bash
# Clone and set up once:
make first-time-setup    # creates .venv, installs deps, copies .env

# Every Monday morning:
make monday              # checks prereqs → starts infra → starts services → health check

# Refresh Zerodha token (NSE data, required daily before 09:15 IST):
make zerodha-login

# Watch paper orders coming in:
make paper-orders

# Halt everything immediately (emergency):
make kill-switch-on
```

---

## Keeping Docs Up to Date

Every document has a "Last updated" line and a note about what should trigger an update.

**When architecture changes:**
1. Update `architecture/system_design.md` first — technical source of truth
2. Update the relevant section in `docs/02_architecture.md` — human-friendly explanation
3. If a new service is added → add a section to `docs/04_services.md`
4. If AWS infrastructure changes → update `docs/06_aws_infrastructure.md`
5. If operational procedures change → update `runbooks/daily_operations.md`

**Rule:** A PR that changes architecture without updating docs will be rejected in review.

---

*QuantEmbrace documentation — current as of 2026-05-15. Architecture: EC2 ARM64 ASGs + Kafka MSK Serverless (Phase 7+).*
