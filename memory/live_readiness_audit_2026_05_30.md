---
name: live-readiness-audit-2026-05-30
description: Phase A/B/C/D live-readiness audit findings and fixes (2026-05-30) — infrastructure blockers, code safety gaps, risk validator gaps, pre-live runbook created
metadata:
  type: project
---

Four-phase static code + infrastructure audit conducted 2026-05-30. No runtime AWS state was mutated. Stage-1 live validation (1 share, 1 strategy, manual sign-off) target for next week.

**Why:** Production readiness review before first live trade. Capital ₹1M blocked until all gates pass.

**How to apply:** Before any EC2-deployed session (paper or live), verify all items in INFRA-1/INFRA-2 are applied and ASG instances are refreshed. Full gate checklist in `docs/live-readiness/pre-live-runbook.md §4`.

---

## Critical Findings Fixed (ADR-022)

1. **symbol-status-index GSI** was missing from orders DynamoDB table. `PositionValidator._get_pending_quantity()` queries this GSI — its absence caused 100% live signal rejection and bypassed dirty-read protection on paper. **Fixed:** GSI added to `dynamodb/main.tf`. Requires `terraform apply`.

2. **sessions DynamoDB table** was absent from Terraform. `ZerodhaTokenManager` raised `ResourceNotFoundException` on every token read — env var token used as fallback, expires at 07:30 IST. **Partial fix:** resource block documented in `pre-live-runbook.md §5.1`; must be added to `dynamodb/main.tf` manually.

3. **RISK_MAX_SIGNAL_AGE_SECONDS=30** was not in EC2 userdata. Code default is 5s — all candle signals (7-12s old) rejected. **Fixed:** Added to `risk_engine.sh` and `strategy_engine.sh`.

4. **RISK_PROFILE=paper** was not in EC2 userdata. Default `tiny-live` gives max 1 concurrent position and ₹5k max order — paper sessions unrepresentative. **Fixed:** Added to `risk_engine.sh`.

5. **UNIVERSE_MODE=PAPER_SAFE_START** was not in EC2 userdata for execution_engine. **Fixed:** Added to `execution_engine.sh`.

6. **check_asg_health.py** did not exist — deploy.yml referenced it but only check_ecs_health.py (ECS API) existed. Every CI/CD deploy failed at health-check step. **Fixed:** `check_asg_health.py` created with ASG API.

7. **Per-symbol fill race** in `DailyLossValidator.record_fill()`. Concurrent fills for same symbol could corrupt cost basis. **Fixed:** asyncio.Lock per symbol via `self._symbol_locks`.

---

## Critical Blockers Still Requiring Manual Terraform Edit

- **INFRA-1:** `prod/main.tf:58` — change `single_nat_gateway = false` to `ha_nat = true`. Terraform plan fails without this.
- **INFRA-2:** `sessions` table resource block must be added to `dynamodb/main.tf` (see `pre-live-runbook.md §5.1`).

---

## Code Fixes Still Pending (pre-live)

- **B-001:** Candle signal publish failures silently dropped in `strategy_engine/service.py:634` — no retry/DLQ (unlike tick path which raises).
- **B-002:** `asyncio.gather(return_exceptions=True)` in strategy_engine `start()` — crashed loops appear healthy. Change to `return_exceptions=False`.
- **B-004:** `KafkaTickPublisher` receives `dynamodb_table_risk_state` for `dynamodb_table_sessions` param in data_ingestion. Needs verification — durable outbox disabled in non-prod so no current contamination.
- **HIGH-001:** `_signal_locks` memory leak in execution_engine/service.py:778 — no cleanup on signal completion.
- **HIGH-004:** MIS square-off has no watchdog — crash at 15:05 IST causes service restart during critical window.

---

## Risk Validator Audit Summary (Phase C)

All 12 validators audited. Chain order correct (cheap first). Paper/live isolation via `risk_data_unavailable_result()` confirmed consistent across all validators: paper signals approve with warning; live signals fail closed on missing data.

Key validator properties confirmed:
- `KillSwitchCache` is O(0) in-memory — background DynamoDB poll
- `PositionValidator` includes in-flight orders (dirty-read fix)
- `DailyLossValidator` uses atomic DynamoDB `ADD` for daily aggregate
- `DailyLossValidator.rehydrate()` called on startup — P&L not reset to 0 on restart
- Both `DailyLossValidator` and `ExposureValidator` paginate DynamoDB reads

---

## Key Documents

- `docs/live-readiness/pre-live-runbook.md` — full gate checklist, rollback procedure, env var table
- `docs/live-readiness/infra-discovery-report.md` — Phase A infrastructure findings
- `memory/decisions.md` ADR-022 — full decision record

## Related Memories

- [[live-tightening]] — ADR-018: lock poisoning fix, stale-LTP LIVE blocking
- [[signal-age-fix]] — root cause of zero trades Days 1-4; RISK_MAX_SIGNAL_AGE_SECONDS fix
- [[universe-model]] — ADR-019: three-mode universe, promotion gates
