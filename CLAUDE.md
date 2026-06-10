# QuantEmbrace — Claude Rules & Protocols

_Last updated: 2026-06-05 | Detail in `architecture/system_design.md`, `docs/`, `memory/`_

---

## Session Start Protocol

**Run this before every session — no exceptions:**

```
1. Read CLAUDE.md                → these rules
2. Read architecture/system_design.md → current architecture and signal flow
3. Read memory/open_tasks.md    → blockers, pending work
4. Summarize state              → implemented vs. stubbed, blockers, next task
5. Ask before implementing      → explain + suggest, wait for approval
```

---

## Paper Trading Start Protocol

**Trigger:** user says "start paper trading" / "run paper session" / `/start_paper_trading`
→ Execute `.claude/commands/start_paper_trading.md` in full. Never summarise. Run it.

**Load before touching any service:**
`architecture/system_design.md` · `memory/decisions.md` · `memory/paper_trading_fixes.md` · `memory/open_tasks.md` · `docs/operations/paper-trading-acceptance-checklist.md`

**Preferred startup command (Session 12+):**
```bash
make start-paper-session   # rebuilds risk_engine image, validates quality gates, starts stack
```

**Manual pre-session sequence (backup if make is unavailable — all steps mandatory):**
```bash
docker-compose build risk_engine               # MUST rebuild to pick up quality-gate validators
docker-compose down -v
docker-compose up -d localstack redpanda
docker-compose run --rm setup
python scripts/deploy/paper_preflight_check.py   # must exit 0 — STOP if fails
python scripts/validate_session12_runtime.py     # must exit 0 — STOP if any check fails
python scripts/zerodha_login.py
docker-compose up -d
```

**Paper session safety gates (all must hold):**

| Gate | Value |
|------|-------|
| `paper_trade` on all strategies | `True` |
| `RISK_MAX_SIGNAL_AGE_SECONDS` | `30` (never lower than 20) |
| `QE_EXECUTION_LIVE_TRADING_ENABLED` | absent or `false` |
| `RISK_PROFILE` | `paper` |
| `UNIVERSE_MODE` | `PAPER_SAFE_START` |
| Real Zerodha credentials | **HARD BLOCK** — stop session if detected |
| `PAPER_SEED_NAV` | `1000000` |

**During session:** `python scripts/monitoring/paper_trading_monitor.py --counters /tmp/qe_live_counters.json --prefix quantembrace-development --endpoint http://localhost:4566`
**Session close:** `AWS_ENDPOINT_URL=http://localhost:4566 python scripts/monitoring/paper_session_report.py --date YYYY-MM-DD`

---

## AWS Historical Backtesting Protocol

**Status: `[PLANNED — not yet implemented]`.** The lab's design docs, commands, agents, and skills exist; the engine code and AWS infra do **not** yet. Do not claim any lab phase works until its files exist at the claimed path.

**Governance invariant (NON-NEGOTIABLE):**
- Backtesting can **recommend**. Backtesting **cannot promote**.
- GenAI can **explain**. GenAI **cannot trade**.
- **A human approves all production changes.**

The backtesting lab is an **offline research environment, fully isolated from live/paper trading.** It validates strategy edge over 10–15 yr NSE history, runs TEE old-vs-new and walk-forward validation, and generates AI quality-scorer training datasets. It is **advisory only** — it never promotes a strategy or changes trading behavior.

**Trigger phrases:**

- "start AWS backtesting"
- "run 10 year backtest"
- "run 15 year NSE backtest"
- "AWS backtest phase"
- "historical backtesting project"
- "build model dataset"

→ Execute `.claude/commands/aws_bt_session_start.md`. Load context, summarize lab state, **stop for approval.** Never auto-proceed between phases.

**Before AWS backtesting work, Claude must read:**

1. `CLAUDE.md`
2. `architecture/system_design.md`
3. `memory/open_tasks.md`
4. `memory/decisions.md`
5. AWS onboarding/deployment docs if present (`docs/06_aws_infrastructure.md`, `docs/phase1_ec2_migration.md`)
6. `docs/backtesting/aws-backtesting-steering.md`
7. `docs/backtesting/aws-backtesting-specification.md`
8. `docs/backtesting/aws-backtesting-implementation-plan.md`

Engine to **reuse**: `services/strategy_engine/backtesting/backtester.py` (never rewrite — `prefer_refactor_over_rewrite`).

**Rules:**

- AWS backtesting only.
- No live trading.
- No broker orders.
- No capital changes.
- No live table mutation.
- No paper/live table mixing.
- Use existing AWS architecture.
- No Fargate/EKS/Lambda-only redesign unless explicitly approved.
- S3 is the source of historical data.
- DynamoDB is run registry and metadata only.
- Large backtest outputs go to S3.
- Every long run must be resumable.
- Every run must persist config, code version, data version, trades, metrics, equity curve, reports, labels, and logs.
- Costs and slippage are mandatory.
- No lookahead leakage.
- Every phase writes a report and stops for approval.
- GitHub/free datasets default to **LOW trust** and must be quarantined (validate before promotion to the curated lake — see `docs/backtesting/aws-data-lake-contract.md`).
- Licensed/vendor/NSE data required for production strategy validation.

**Approved infra (design only; build is later, phase-gated):** separate `backtest` env / `qe-bt-` table prefix / `quantembrace-backtest-*` buckets · EC2 ARM64 `backtest-worker` ASG (scale-from-0) · DynamoDB `qe-bt-runs`/`qe-bt-checkpoints`/`qe-bt-datasets` · CloudWatch `QuantEmbrace/Backtest` · SNS `quantembrace-backtest-alerts` · on-demand serverless GenAI (EventBridge + Step Functions + Bedrock — no SQS, no polling). No Fargate/ECS/EKS/Lambda-for-compute. Each phase produces a report and **stops for human approval** before the next.

---

## Platform Identity

Personal Indian equities algo trading platform. **Paper trading is the current primary mode.** Live trading requires explicit operator gate sign-off — it is never automatic.

**Capital protection > trade count > profit.** Every design decision that conflicts with this ordering is wrong.

Signal path: `strategy_engine → ai_engine → risk_engine → execution_engine`
Detail: `architecture/system_design.md`

---

## Non-Negotiable Safety Rules

### Trading Safety
- Never weaken risk controls to increase fill rate or P&L.
- Never bypass: universe validation · risk validation · kill switch · idempotency · order validation.
- Never auto-promote from paper to live. Promotion is always a manual operator decision.
- Live mode must **fail closed** on missing, stale, or partial critical data (universe snapshot, surveillance list, LTP, margin).
- Paper mode may degrade gracefully only when explicitly configured, logged, and audited.
- `UniverseOrderValidator` in execution_engine is the **final hard gate** — risk approval does not bypass it.
- Paper and live broker paths must remain **fully isolated**. `paper_trade=True` must never call a real broker API.

### Code and Config Safety
- Never auto-deploy, auto-merge, or auto-apply any generated fix or recommendation.
- The Self-Improvement Assistant may observe, collect, store, analyze, report, and recommend. It must never change live trading behavior directly.
- Strategy changes must be evidence-based (from logs, session reports, tests), tested, and reversible.
- Do not silently ignore missing or stale data — log, alert, degrade or fail.
- Never include secrets (API keys, tokens, credentials) in reports, logs, Claude prompts, or generated artifacts.

### Strategy Performance Live-Readiness Rule

The platform is **not live-ready just because infrastructure is healthy**.

Live trading remains BLOCKED until ALL of the following pass across ≥5 consecutive paper sessions:
- Strategy expectancy > 0
- Profit factor > 1.2
- Realized P&L > 0
- Reconciliation: 0 mismatches
- Section 16 netting gate: PASS
- TEE: exits routing correctly
- MIS square-off: all positions closed
- Section 17 Strategy Performance Health gate: PASS

Until all pass: verdict = `PAPER_OPTIMIZATION`. Do not promote to live.

### Session Validity Rules
- Sessions 10 and 11 ran on a stale Docker image. Quality gates were inactive. They are NOT valid quality-gate performance tests.
- Sessions 12–15 ran with the confidence/RR quality filters **silently disabled** by a strategy-name key mismatch (YAML keys unprefixed, signals `nse_`-prefixed — fixed in ADR-030, 2026-06-10). They are NOT valid quality-gate performance tests either.
- Session 16 is the first valid quality-gate proof session (Week-1 entry-economics rebuild: universal viability gate, ORB v2, VWAP v2, 15-entry/day global budget — ADR-030).
- A paper session is not valid unless `scripts/validate_session12_runtime.py` passes at startup.
- Live trading remains BLOCKED until ≥5 consecutive valid quality-gate sessions pass all strategy performance gates.
- Infrastructure health alone does not prove strategy edge.

### Claude-Specific Rules
- Read code before making claims. Documentation and code may disagree — code is truth.
- Make minimal targeted changes. No opportunistic refactors while fixing a session bug.
- Human approval required before any change affects paper or live trading behavior.
- Do not overfit a single bad trading day.

---

## Current Trading Modes

| Mode | Universe | Behavior on Missing Data | Promotion |
|------|----------|-------------------------|-----------|
| `PAPER_SAFE_START` | NIFTY 50 (≈50 symbols) | YAML fallback (paper only) | Manual after 5 sessions + gate pass |
| `PAPER_EXPAND` | NIFTY 100 + F&O (≈100-150 symbols) | WARN + degrade (paper only) | Manual after 10 sessions + gate pass |
| `LIVE_ADVANCED` | NIFTY 200/500 screened (≈200 symbols) | **FAIL CLOSED** — no live fallback | Manual after PAPER_EXPAND gate + full checklist |

**No order may reach `broker.place_order()` unless the symbol is in the universe snapshot for the current trading date and mode.**

Paper and live universe snapshots use separate namespace keys. They must never be mixed.

Gate evaluation (manual, report-only — never changes mode):
```bash
python scripts/evaluate_promotion_gate.py --gate PAPER_SAFE_START_TO_EXPAND --metrics /tmp/metrics.json
python scripts/evaluate_promotion_gate.py --gate PAPER_EXPAND_TO_LIVE       --metrics /tmp/metrics.json
```

---

## Execution and Risk Flow

```
signals.pending (Kafka)
  → ai_engine (aiengine-v1)          enriches: market_regime + quality_score
      [EnrichmentWatchdog fallback: signals.pending → risk-v1 if ai_engine lags]
  → signals.enriched (Kafka)
  → risk_engine (risk-v1)            11 validators — see docs/04_services.md
  → signals.approved (Kafka)
  → execution_engine (execution-v1)
      → UniverseOrderValidator       FINAL HARD GATE
      → idempotency (DynamoDB cw)
      → PaperSimulator (paper_trade=True) OR Zerodha/Alpaca (paper_trade=False)
  → orders.events (Kafka)
```

Key invariants:
- Risk-approved but execution-rejected = correct behavior (log reason code, audit trail).
- `submit_order` returns `False` on DynamoDB race → return immediately, no fill recorded.
- Kill switch checked in risk validator #2 AND by `KafkaKillSwitchListener` in every service.

---

## Self-Improvement Assistant

**Status: PARTIALLY IMPLEMENTED.** Only post-session reporting and live monitoring exist.

| Action | Status |
|--------|--------|
| Live monitoring (`paper_trading_monitor.py`) | Implemented |
| Post-session report (`paper_session_report.py`) | Implemented |
| Intraday snapshots, EOD analysis, Claude prompt generator | **PLANNED — not yet implemented** |

Loop (current): Observe → Collect → `paper_session_report.py` → Human reviews → Human asks Claude → Claude recommends → Human approves → Apply

Loop (planned): Adds automated intraday snapshots and `self_improvement_assistant.py` script (does not exist yet).

**Before modifying strategy, execution, universe, or risk logic based on a trading session, run or review `paper_session_report.py` for that date.**

**Prohibited:** auto-deploy, auto-merge, auto-promote, auto-change live behavior, send secrets to Claude.

---

## Claude Working Rules

**Read first:** `memory/open_tasks.md`, `architecture/system_design.md`, relevant service code.
**Change minimum:** one fix per PR, no opportunistic refactors.
**After change:** add tests, update `architecture/system_design.md` if behavior changed, update runbooks if operator steps changed, write ADR in `memory/decisions.md` for major design decisions.

**Always test:** universe selection · snapshot isolation · execution validation · risk validators · kill switch · idempotency · paper/live broker isolation · stale-data handling · order rejection audit · signal age · promotion gates.

**Do not:**
- Claim a feature works without verifying the file exists at the claimed path.
- Remove fail-closed live behavior or degrade it to warn-only.
- Claim a planned feature is implemented. Mark it: `[PLANNED — not yet implemented]`.
- Include secrets in any generated output.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Broker (India) | Zerodha Kite Connect (NSE/BSE) |
| Broker (US) | Alpaca |
| Compute | AWS EC2 ARM64 ASGs (c6g/t4g) — ECS Fargate removed |
| State | DynamoDB (orders, positions, risk-state, strategy-config, candle-cache, + 5 more) |
| Messaging | Kafka MSK Serverless (SASL/OAUTHBEARER IAM, port 9098) |
| Storage | S3 (ticks, models, audit logs) |
| Infra | Terraform · Docker · GitHub Actions |
| Testing | pytest · hypothesis · locust |

---

## Service Boundaries

| Service | Primary Reads | Primary Writes |
|---|---|---|
| data_ingestion | Broker WebSocket (Zerodha, Alpaca) | Kafka `ticks.nse` / `ticks.us`; DynamoDB `latest-prices`, `candle-cache` |
| strategy_engine | Kafka `ticks.*` (strategy-v1); DynamoDB `candle-cache` | Kafka `signals.pending` |
| ai_engine | Kafka `signals.pending` (aiengine-v1); S3 models | Kafka `signals.enriched`; DynamoDB `strategy-recommendations` |
| risk_engine | Kafka `signals.enriched` (risk-v1 primary); `signals.pending` (risk-v1-fallback); `orders.events` | Kafka `signals.approved`; `risk.kill-switch`; `ops.audit`; S3 audit |
| execution_engine | Kafka `signals.approved` (execution-v1); DynamoDB `strategy-config`, `instrument-registry` | Broker APIs; Kafka `orders.events`; DynamoDB `orders`, `positions` |

---

## Critical Env Vars

| Variable | Rule | EC2 Injected? |
|---|---|---|
| `RISK_MAX_SIGNAL_AGE_SECONDS` | Always `30`. Never below `20`. Candle signals are 7-12s old at risk_engine. Code default is `5s` — would reject all candle signals if missing. | ✅ risk_engine.sh, strategy_engine.sh |
| `RISK_PROFILE` | `paper` for all paper sessions · `tiny-live` for Stage-1 live · change only after explicit operator sign-off | ✅ risk_engine.sh |
| `UNIVERSE_MODE` | `PAPER_SAFE_START` / `PAPER_EXPAND` / `LIVE_ADVANCED` on execution_engine | ✅ execution_engine.sh |
| `QE_EXECUTION_LIVE_TRADING_ENABLED` | Must be absent or `false` for all paper sessions. **HARD BLOCK.** Commented out in execution_engine.sh — uncomment only after full promotion gate sign-off. | Commented in execution_engine.sh |
| `PAPER_SEED_NAV` | `1000000` (₹10L) — seeded by `setup` service | Local only |
| `STRATEGY_WATCHLIST_NSE` | Comma-separated symbols for LiveQuotePoller — required for candle strategies | Set per-deployment |
| `ZERODHA_ACCESS_TOKEN` | Never source from env var at startup — always resolve from DynamoDB `sessions` table via `ZerodhaTokenManager` (anti-patterns #14, #15). Env var in EC2 userdata is the boot fallback only — expires at 07:30 IST. | Boot fallback only |

Full env var reference: `docs/04_services.md § Environment Variables Reference`

---

## Broker Notes

**Zerodha (NSE):** Token expires daily ~07:30 IST. Refresh with `python scripts/zerodha_login.py`. `ZerodhaBrokerClient` **must** receive `dynamo_client=` at construction — never construct it before DynamoDB is ready, never use stale env var token. See `memory/anti_patterns.md` #14 and #15. Token is stored in the `{prefix}-sessions` DynamoDB table (Terraform-provisioned as of ADR-022 — must exist before any Zerodha session).

**Paper broker:** `paper_trade=True` in DynamoDB `strategy-config` → `PaperSimulator` only. Zero real broker calls.

---

## Monitoring Status Rule

When asked for monitoring status: use the 15-section template in `docs/operations/monitoring-status-template.md`. Never free-form.

**GREEN only if:** TradingMode=PAPER · `live_trading_enabled=false` · TradeExitEngine active · ExitOrderRouter active · MIS Square-Off armed · reconciliation ran or safely skipped · no unmanaged positions · no critical alerts · daily caps do not block exits.

---

## Documentation Update Rules

Update `CLAUDE.md` + `architecture/system_design.md` when: trading modes change · universe logic changes · execution/risk flow changes · broker behavior changes · operational commands change · live/paper safety assumptions change.

Update `docs/runbooks/` + `docs/operations/` when: operator steps change · troubleshooting changes · incident response changes.

Update `memory/decisions.md` (ADR) when: major design decisions made or reversed · safety model changes · automation scope changes.

Update `docs/live-readiness/` when: a live-readiness audit phase completes · blockers change status · the pre-live runbook changes. The canonical pre-live checklist is `docs/live-readiness/pre-live-runbook.md`.

**Mark unimplemented features:** `[PLANNED — not yet implemented]`. Never describe planned behavior as current.
