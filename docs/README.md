# QuantEmbrace — Documentation

Welcome to the QuantEmbrace documentation. This is the single source of truth for understanding, developing, and operating the platform.

---

## Quick Navigation by Role

| I am... | Reading path |
|---|---|
| **Total newcomer** | → 01 → 02 → 03 → done |
| **Developer joining the team** | → 01 → 02 → 04 → 05 → 07 |
| **Setting up locally for the first time** | → 05 |
| **Working on AWS / infrastructure** | → 06 |
| **Adding a new trading strategy** | → 03 → 07 (Adding a Strategy section) |
| **Debugging a production issue** | → 04 (per-service debugging section) |
| **Looking up a term** | → 07 (Glossary) |

---

## Document Index

| Doc | What it covers |
|---|---|
| [01_introduction.md](01_introduction.md) | What is algo trading, what QuantEmbrace does, markets, big-picture trade flow, design philosophy |
| [02_architecture.md](02_architecture.md) | 6-layer architecture deep-dive, how layers communicate, failure modes, the golden rule (no layer skipping) |
| [03_signal_lifecycle.md](03_signal_lifecycle.md) | Complete trade lifecycle — tick → signal → risk → order → fill. Market hours, kill switch, error paths, order state machine |
| [04_services.md](04_services.md) | Per-service reference: key files, configuration, debugging guide, failure modes |
| [05_local_setup.md](05_local_setup.md) | Step-by-step local setup: prerequisites, environment config, LocalStack, running services, tests, backtesting |
| [06_aws_infrastructure.md](06_aws_infrastructure.md) | AWS services explained, cost breakdown, Terraform structure, ECS, DynamoDB, S3, SQS, networking, deployment |
| [07_contributing.md](07_contributing.md) | How to add strategies/brokers, risk rules checklist, git workflow, code standards, common mistakes, full glossary, FAQ |

---

## Architecture at a Glance

```
Zerodha (NSE)    Alpaca (US)
     │                │
     └─────┬──────────┘
           │
   [ Data Ingestion ]        ← Layer 1: watches prices, stores ticks
           │ SQS
   [ Strategy Engine ]       ← Layer 2: generates buy/sell signals
           │ SQS (FIFO)
   [  RISK ENGINE  ] ◄◄◄     ← Layer 4: CRITICAL GATE — validates every signal
           │ SQS (FIFO)
   [ Execution Engine ]      ← Layer 3: places orders with brokers
       │           │
  Zerodha API   Alpaca API
  
   [ AI/ML Engine ]          ← Layer 5: enriches signals with ML predictions
   [ Infrastructure ]        ← Layer 6: AWS ECS, S3, DynamoDB, SQS, Terraform
```

**The Non-Negotiable Rule:** Every signal flows `Strategy → Risk → Execution`. There is no bypass.

---

## System Vitals

| Fact | Value |
|---|---|
| Language | Python 3.11+ |
| Compute | AWS ECS Fargate |
| Brokers | Zerodha Kite Connect (NSE), Alpaca (US) |
| Markets | NSE India + US Equities (NYSE, NASDAQ) |
| Infrastructure | Terraform |
| Estimated monthly AWS cost | ~$97–154 (production, market hours only) |
| Risk Engine position in flow | Between Strategy and Execution — mandatory, no bypass |
| Order idempotency mechanism | UUID `signal_id` + DynamoDB conditional writes |
| Kill switch storage | DynamoDB `risk-state` table |
| Tick storage format | Parquet on S3 (partitioned by market/symbol/date/hour) |

---

## Keeping Docs Up to Date

Every doc has a "Last updated" line and a note about what should trigger an update.

**When architecture changes:**
1. Update `architecture/` files (system_design.md, data_flow.md, etc.) — these are the technical source of truth
2. Update the relevant section in `docs/02_architecture.md` — this is the human-friendly explanation
3. If a new service is added, add a section to `docs/04_services.md`
4. If AWS infrastructure changes, update `docs/06_aws_infrastructure.md`

**Practical rule:** If you make an architecture change and don't update the docs, your PR will be rejected in review.

---

*This documentation covers QuantEmbrace as of 2026-04-24. The system architecture is version 1.0.*
