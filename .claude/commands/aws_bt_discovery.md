---
description: Discover and report current repo state relevant to the AWS backtesting lab. Read-only.
---

# /aws_bt_discovery — Backtesting Lab Discovery

Read-only inventory of what exists vs what is missing for the backtesting lab. **No writes, no implementation.**

## Load first
`docs/backtesting/aws-backtesting-steering.md`, `docs/backtesting/aws-backtesting-implementation-plan.md`.

## Do
1. Inventory existing backtest assets: `services/strategy_engine/backtesting/backtester.py`, `scripts/backtest/`, `commands/run_backtest.yaml`, Terraform `modules/{s3,dynamodb,ec2_services}`, S3 buckets, DynamoDB tables.
2. Check for lab-specific resources (likely absent): `qe-bt-runs`, `qe-bt-checkpoints`, `qe-bt-datasets` tables; `quantembrace-backtest-*` buckets; `backtest-worker` ASG; `environments/backtest/` Terraform.
3. Map each implementation-plan phase to existing-vs-missing.
4. Flag any conflicts with `governance/file_structure.md`, `naming_conventions.md`, and the no-duplicate/refactor hooks.

## Safety
Do not create, modify, deploy, or run anything. Backtest-only context.

## Output / Stop
A discovery report: existing, missing, conflicts, recommended next phase. **Stop for approval.**
