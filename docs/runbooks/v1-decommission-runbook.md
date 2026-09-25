# v1 Decommission Runbook — GATED, operator-executed, irreversible

> **Status: STAGED, NOT EXECUTED. The gate below is NOT yet met (0 clean v2 paper
> sessions; lake stale at 2026-06-12).** This runbook is the procedure to tear down the
> v1 Kafka/microservice trading stack once the v2 (`qe`) engine has earned it. Nothing
> here runs automatically. Every step is a deliberate operator action. ADR-037/038.

## Why this is gated and not done at M5

RA-1's M5 acceptance decommissions infra **"after N clean v2 paper sessions."** The
strangler-migration invariant (RA-1 §3.2) is *"the old stack keeps running until the new
engine has proven parity — no capability-gap days."* Tearing down the fallback before the
v2 stack has a live track record would be the exact big-bang the redesign forbids. So the
cutover (docs/commands/cadence → `qe`) is done; the teardown waits on evidence.

## Preconditions (ALL must hold before any step below)

- [ ] **≥ N clean v2 paper sessions** run on a fresh lake (recommend N ≥ 3 month-end
      sessions, or the operator's chosen bar), each with `status OK` and a config hash
      matching the approved book config.
- [ ] Lake refreshed to within `max_data_age_days` — the 2026-06-12 staleness that blocked
      the M4 dry-run is resolved.
- [ ] The forward factor gate program is unaffected (it runs on `qe` and needs no v1 infra).
- [ ] **Human sign-off recorded** (this is a production infra change; capital-protection
      ordering applies). Backtesting recommends; a human promotes.
- [ ] A tagged git restore point exists (`git tag pre-v1-decommission`) and the v1 stack is
      known-restorable from it.

## CRITICAL retention note — do NOT `rm -rf services/`

The `qe` **runtime** imports zero v1 service code (verified). But three v1 modules are
**test parity anchors** that qe's correctness tests import:

| Retained module | Imported by | Why it must survive teardown |
|---|---|---|
| `strategy_engine/backtesting/backtester.py` (`IndianCostModel`) | `tests/qe/test_costs_parity.py` | Locks qe.costs to the v1 cost source of truth |
| `scripts/backtest/run_factor_study.py` | wf/parity tests | Panel/metric/universe parity anchor |
| `scripts/backtest/run_delivery_walkforward.py`, `scripts/paper/replay_delivery_book_forward.py` | wf/book parity tests | The registered-numbers cross-check |

**Before archiving these**, convert their parity tests to frozen golden-value tests (snapshot
the v1 numbers into the test as constants), then the modules can move to `archive/v1/`. Until
then, keep them in place. This is the one non-obvious teardown hazard.

## Teardown order (each step reversible only via the git tag / Terraform state backup)

1. **Freeze + snapshot.** Tag git; export final v1 DynamoDB tables to S3; archive final MSK
   topic offsets if any audit value remains.
2. **Stop v1 compute.** Scale the strategy/ai/risk/execution ASGs to 0. Observe one cycle:
   the forward books on `qe` are unaffected (they never used this path).
3. **Decommission ai_engine.** Remove the service + its `signals.enriched` topic + the
   `EnrichmentWatchdog` path. (RA-1 F-4: it was never in the factor-book path.)
4. **Decommission MSK Serverless.** After confirming no producer/consumer remains, delete the
   cluster and topics. This is the single largest fixed-cost line (RA-1 F-7).
5. **Shrink DynamoDB.** Keep `sessions` (token), and any table `qe` uses. Export-then-delete
   the intraday-only tables (candle-cache, strategy-config, risk-state, etc.).
6. **Remove the local dev stack.** LocalStack + Redpanda from docker-compose; delete the
   `validate_session12` / preflight scripts that existed to police the Kafka stack.
7. **Prune Terraform.** Remove MSK, ai_engine, intraday-table, and surplus-ASG modules.
   `terraform plan` FIRST, review the destroy plan line by line, then apply. Keep S3 (lake +
   results), the `sessions` table, and one minimal EC2 profile for `qe`.
8. **Archive v1 code.** `git mv` the retired services to `archive/v1/` (after the parity-test
   golden-value conversion above). Keep git history intact.

## Rollback

Until step 7 (`terraform apply`), everything is restorable from the git tag + Terraform state
backup. After step 7, restoration means re-applying the pre-decommission Terraform. Do not
proceed past step 4 without confidence the `qe` paper track record is solid — MSK deletion is
the first genuinely one-way door.

## Post-decommission acceptance

- [ ] Monthly AWS bill drop is visible (MSK + surplus ASG + DynamoDB lines gone).
- [ ] `docs/runbooks/qe-operator-runbook.md` is the entire operating surface.
- [ ] All qe tests still green (parity anchors either retained or converted to golden values).
