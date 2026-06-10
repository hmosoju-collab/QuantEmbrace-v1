---
description: "Phase 2 — design/build the DynamoDB run registry + checkpoints (Terraform + client)."
---

# /aws_bt_run_registry — Phase 2: Run Registry & Checkpoints

Provision and wire the registry per `docs/backtesting/aws-backtest-run-registry.md`. **Backtest-only**, `qe-bt-` prefix, never touches live/paper tables.

## Load first
`aws-backtest-run-registry.md`, `aws-backtesting-steering.md` (§4 conventions).

## Do
1. Terraform (in `environments/backtest/` only): `qe-bt-runs`, `qe-bt-checkpoints`, `qe-bt-datasets` (on-demand, PITR on runs, GSIs per spec). Run `terraform plan` — confirm **no diff** to live `prod`/`staging` (`terraform_safety`).
2. Implement a registry client: deterministic `run_id` from `config_hash`; conditional-write claim; state machine `PENDING->RUNNING->CHECKPOINTED->COMPLETED/FAILED/CANCELLED`.
3. Implement checkpoint read/write + stale-heartbeat reclaim.
4. Emit CloudWatch `QuantEmbrace/Backtest` metrics; SNS on failures.

## Safety
IAM: R/W to `qe-bt-*` only. No live/paper table access, no broker creds.

## Output / Stop
Registry schema applied (plan output), idempotency + resume design proof. **Stop for approval.** (Do not run acceptance tests in this command.)
