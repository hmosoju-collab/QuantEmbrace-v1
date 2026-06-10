# QuantEmbrace — AWS Backtest Run Registry & Checkpoints

> **Status (AWS-BT-3):** the **registry + checkpoint clients are implemented** (`services/backtesting/run_registry.py`, `services/backtesting/checkpoint_manager.py`, `tests/backtest/test_run_registry.py`). The **DynamoDB tables / Terraform / CloudWatch / SNS are `[PLANNED — not yet implemented]`** (Phase 2 infra build). Backtest-only: **DynamoDB metadata only**, large outputs in S3; live/paper tables are rejected at construction.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md`.

The registry is the **index of record** for every backtest. S3 holds the heavy artifacts (trades, equity curves, partial outputs); DynamoDB holds lifecycle, lineage, and resume metadata.

---

## 1. Tables

### `qe-bt-runs` — run registry (one item per run, PK `run_id`)

Implemented fields (`run_registry.RUN_FIELDS`):

| Field | Notes |
|---|---|
| `run_id` | **PK** = `bt_{config_hash[:16]}` — deterministic from config (idempotency) |
| `status` | `CREATED` → `RUNNING` → `COMPLETED` / `FAILED` / `CANCELLED` |
| `strategy` | strategy name |
| `symbols` | list of symbols |
| `timeframe` | `1m` / `5m` / `15m` / `1d` |
| `start_date`, `end_date` | backtest window |
| `config_s3_path` | resolved run config in S3 (required; must be a path) |
| `result_s3_path` | metrics/trades/equity output prefix in S3 |
| `checkpoint_s3_path` | large partial-state prefix in S3 |
| `code_version` | git SHA / build id (reproducibility) |
| `data_version` | `data_snapshot_id` (reproducibility) |
| `started_at`, `updated_at`, `completed_at` | lifecycle timestamps (ISO-8601 UTC) |
| `error_reason` | failure reason (when `FAILED`) |
| `operator` | who launched the run |
| `trust_level` | `HIGH` / `LOW` of the input data (advisory eligibility) |
| `cost_model_version` | cost/slippage model version |
| `exit_policy_version` | TEE / exit-policy version replayed |
| `config_hash` | SHA-256[:16] of the resolved config |

**Planned GSIs:** `status-index` (status, updated_at) · `strategy-index` (strategy, start_date) · `config-hash-index` (idempotent lookup).

### `qe-bt-checkpoints` — resume state (one item per run, PK `run_id`)

Implemented fields (`checkpoint_manager.CheckpointRecord`):

| Field | Notes |
|---|---|
| `run_id` | **PK** |
| `completed_partitions` | list of finished `symbol#year` (or date-chunk) partitions |
| `partition_cursors` | map `partition → last processed timestamp` |
| `last_processed_timestamp` | overall furthest processed point (resume cursor) |
| `failed_partitions` | map `partition → {reason, retry_count, at}` |
| `partial_metrics` | small running metrics (JSON; floats kept out of DynamoDB number typing) |
| `partial_output_s3_path` | **S3 pointer** to large partial output (never inline) |
| `resumable_command` | exact command to resume the run |
| `retry_count` | run-level retry counter |
| `updated_at` | last checkpoint time |

> Design choice: a **single checkpoint item per run** (metadata-only, small). For very large fan-out a future variant may shard into per-partition items (PK `run_id`, SK `partition_id`) with `heartbeat_at`/`worker_id` for multi-worker claiming; the client API (`completed_partitions`, `pending_partitions`) stays the same.

### `qe-bt-datasets` — dataset registry

`dataset_id` (PK); links to source runs, schema version, split boundaries, row counts, S3 prefix. Detail in `model-dataset-spec.md`.

## 2. Run state machine

```
CREATED ──mark_running──► RUNNING ──mark_completed──► COMPLETED
                            │
                            ├── mark_failed(reason) ──► FAILED
                            └── mark_cancelled ───────► CANCELLED
RUNNING ⇄ checkpoint_partition()/fail_partition()   (resume cursor advances)
```

Checkpointing is recorded in `qe-bt-checkpoints` (not a run status); resume reads it to skip completed partitions.

## 3. Idempotency & determinism

- `run_id = bt_{config_hash[:16]}` from `(strategy, sorted symbols, timeframe, dates, code/data/cost/exit versions)`. Same config ⇒ same `run_id`.
- `create_run` is **idempotent**: it returns the existing record if the `run_id` already exists (get-first), and uses a DynamoDB `attribute_not_exists(run_id)` ConditionExpression on real AWS to win races.
- `init_checkpoint` is idempotent. Outputs for a `run_id` are immutable per the data-lake contract.

## 4. Resume protocol (restart-safe)

1. Worker loads `qe-bt-runs[run_id]` and `qe-bt-checkpoints[run_id]`.
2. `pending = pending_partitions(run_id, all_partitions)` → only the not-yet-completed partitions.
3. For each pending partition, process and call `checkpoint_partition(...)` (advances `partition_cursors` + overall `last_processed_timestamp`, writes the S3 partial-output pointer).
4. On partition error call `fail_partition(...)` (records reason, bumps `retry_count`).
5. When all partitions are complete, aggregate to S3 and `mark_completed(result_s3_path=...)`.

A resumed run **must** produce identical metrics to an uninterrupted run (acceptance check, later phase). Satisfies `restart_safety`.

## 5. Safety — metadata only, no live tables

- **Live-table guard** (`run_registry.assert_backtest_table`): construction is **rejected** unless the table name is a backtest table (`qe-bt-*` / contains `backtest`) and is **not** a known live/paper table (`orders`, `positions`, `risk-state`, …). Applied in both `RunRegistry` and `CheckpointManager`, and in `from_aws` *before* any AWS call.
- **DynamoDB stores metadata only.** Large outputs (trades, equity curves, partial state) are S3 pointers (`*_s3_path`); path fields are validated to be S3 URIs / paths, never inline blobs.
- No broker APIs, no live trading, no mutation of trading-runtime tables.
- DynamoDB access via the sanctioned `shared.aws.clients` factory or an injected table (tests / LocalStack) — never a raw `boto3.client`.

## 6. Planned infra (not built here)

On-demand DynamoDB tables with **PITR** on `qe-bt-runs`; CloudWatch `QuantEmbrace/Backtest` (`RunsRunning`, `RunFailures`, `ShardResumeCount`, `RunDurationSeconds`); SNS `quantembrace-backtest-alerts` on failure. Terraform in `environments/backtest/` only (no live diff). Built in the Phase-2 infra step, gated by its own report.

## 7. Tooling & tests (implemented)

| Artifact | Path |
|---|---|
| Run registry | `services/backtesting/run_registry.py` |
| Checkpoint manager | `services/backtesting/checkpoint_manager.py` |
| Tests (9 cases) | `tests/backtest/test_run_registry.py` |

```python
from backtesting.run_registry import RunRegistry, RunSpec
from backtesting.checkpoint_manager import CheckpointManager

reg = RunRegistry.from_aws("qe-bt-runs")            # guarded; rejects live tables
run = reg.create_run(RunSpec(strategy="momentum", symbols=["RELIANCE"], timeframe="1d",
                             start_date="2010-01-01", end_date="2024-12-31",
                             config_s3_path="s3://quantembrace-backtest-results/runs/cfg.json"))
reg.mark_running(run.run_id)
cp = CheckpointManager.from_aws("qe-bt-checkpoints")
cp.checkpoint_partition(run.run_id, "RELIANCE#2010", last_processed_timestamp="2010-12-31T15:30:00+05:30")
pending = cp.pending_partitions(run.run_id, ["RELIANCE#2010", "RELIANCE#2011"])  # → ["RELIANCE#2011"]
reg.mark_completed(run.run_id, result_s3_path="s3://quantembrace-backtest-results/runs/<id>/metrics.json")
```
