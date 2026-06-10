# QuantEmbrace — AWS-BT-0 Discovery Report

> **Phase 0 — Discovery only.** Read-only audit of current AWS + backtesting readiness. No implementation, no deploy, no live, no broker calls.
> Generated: 2026-06-06 · Protocol: CLAUDE.md § *AWS Historical Backtesting Protocol* · Governed by `docs/backtesting/aws-backtesting-steering.md`.

Context read: `CLAUDE.md`, `architecture/system_design.md`, `memory/open_tasks.md`, `memory/decisions.md` (ADR-001…028), `docs/06_aws_infrastructure.md`, `docs/phase1_ec2_migration.md`, Terraform (`infra/terraform/**`), GitHub Actions (`.github/workflows/*`), `scripts/backtest/`, `services/strategy_engine/backtesting/`, and the strategy / risk / execution / exit (TEE+MIS) services.

Legend: ✅ present · 🟡 partial · ❌ missing.

---

## 1. Existing backtest code ✅ (core engine) / 🟡 (lab tooling)

| Asset | Path | Notes |
|---|---|---|
| Replay engine | `services/strategy_engine/backtesting/backtester.py` (668 ln) | Next-bar fills, `lookahead_violations` counter, `IndianCostModel`, slippage+half-spread, gap-stops, order-vs-bar-volume cap, drawdown stress, Sharpe/Sortino/profit-factor, trade log, equity curve, daily returns. **Reuse — do not rewrite.** |
| CLI runner | `scripts/backtest/run_backtest.py` | Local CSV + S3 (CSV) loader; momentum + scalp_1m (v1/v2 A/B); JSON output. |
| Command spec | `commands/run_backtest.yaml` | Params/steps/metrics; result path `backtests/{strategy}/{timestamp}/`. **Contains stale refs — see §16.** |
| Unit test | `tests/unit/test_momentum_backtester.py` | TASK-005 (complete). |
| Lab test dir | `tests/backtest/` | **Empty** ❌ |
| Top-level `services/backtesting/` | — | ❌ Does not exist (backtesting nested under strategy_engine — correct). |

## 2. Existing historical data paths 🟡 (multiple, inconsistent)

No single curated lake. At least four conflicting conventions exist today:

- `commands/run_backtest.yaml`: `historical/{symbol}/{date}/` under `${S3_BUCKET_DATA}`.
- `run_backtest.py`: S3 `bucket/prefix` (e.g. `quantembrace-prod-ohlcv-data`, `AAPL/1d/`) or local CSV.
- `architecture/system_design.md`: ticks `s3://{bucket}/{market}/{instrument}/{date}/{hour}/ticks.parquet`; results `backtest/results/{run_id}/`.
- `docs/06_aws_infrastructure.md`: `s3://quantembrace-{env}-data/ticks/...`; `backtest/results/{run_id}/`.

**Resolution exists on paper:** `aws-data-lake-contract.md` standardizes to `lake/ohlcv/market=/symbol=/interval=/year=/` + `runs/{run_id}/`. Not yet applied in code.

## 3. Existing data formats 🟡

- **Production pipeline writes Parquet**: `services/data_ingestion/features/{feature_archiver,feature_writer}.py`, `services/data_ingestion/service.py`, `services/shared/models/feature_set.py`; ticks stored as Parquet (per docs).
- **Backtest loader is CSV-only**: `scripts/backtest/run_backtest.py` (`csv.DictReader`). The engine itself (`backtester.py`) is format-agnostic (consumes `Bar` objects).
- **Gap:** the lab needs a Parquet `BarSource`; the current backtest path cannot read the Parquet lake.

## 4. Existing AWS infrastructure ✅ (live platform) / ❌ (lab)

Terraform modules: `vpc`, `ec2_services` (+ userdata: ai_engine, bootstrap, data_ingestion, execution_engine, risk_engine, strategy_engine), `dynamodb` (main, ai_engine, features), `kafka`, `s3`, `monitoring` (+ ai_engine_alarms). Envs: `dev`, `staging`, `prod`. EC2 ARM64 Graviton ASGs for 5 services; MSK Serverless; CloudWatch; SNS; Secrets Manager; VPC endpoints; least-privilege IAM. ADR-009 (EC2 over Fargate) / ADR-010 (Kafka, SQS banned).

**Lab-specific:** ❌ no `environments/backtest/`, ❌ no `backtest-worker` ASG, ❌ no lab IAM role.

## 5. Existing S3 buckets ✅ (5) — `{project}-{env}-*`

`tick-data`, `ohlcv-data`, `trading-logs`, **`backtest-results`** (retained forever), `model-artifacts` (`infra/terraform/modules/s3/main.tf`). ❌ No dedicated curated historical-lake bucket (`quantembrace-backtest-data`) and no separate backtest results bucket under the lab prefix.

## 6. Existing DynamoDB tables ✅ (13) / ❌ (registry)

`orders, positions, latest-prices, risk-state, sessions, candle-cache, strategy-config, strategy-state, signal-inbox, signal-outbox, features, regime-log, strategy-recommendations`. **None is a backtest run registry.** ❌ `qe-bt-runs`, `qe-bt-checkpoints`, `qe-bt-datasets` do not exist.

## 7. Existing EC2 / Docker deployment ✅ (services) / ❌ (worker)

`infra/deployment/Dockerfile`, `Dockerfile.monitoring_agent`, `deploy.sh` (EC2 ASG model, explicitly *not* ECS — ADR-009). Services run as systemd units; ASG instance refresh; `scripts/deploy/promote_ecr_image.sh`, `check_asg_health.py`. Local: `docker-compose.yml` (redpanda + localstack). ❌ No backtest worker image, no batch/shard runner, no checkpoint/resume.

## 8. Existing GitHub Actions ✅ (3) / ❌ (lab CI)

| Workflow | Lines | Purpose |
|---|---|---|
| `ci.yml` | 183 | ruff lint gate · pytest unit on LocalStack · terraform validate + tflint. Runs on all pushes/PRs. |
| `build.yml` | 177 | On merge to main: Docker per service → ECR (`<sha>` immutable + `latest-<env>`), OIDC. |
| `deploy.yml` | 273 | ECR promotion → EC2 ASG instance refresh, order risk→execution→data/strategy; staging auto, prod manual. |

❌ No backtest pipeline; `tests/backtest/` empty so nothing to gate.

## 9. Existing GenAI / Bedrock usage 🟡 (Anthropic SDK, not Bedrock)

- **No AWS Bedrock anywhere.**
- An LLM advisory agent exists: `services/ai_engine/agents/strategy_selector.py` — **Claude Haiku via the Anthropic SDK** (direct API), read-only/advisory, degrades safely if SDK or key absent. IAM optionally grants Secrets Manager access via `secrets_anthropic_arn` (`ec2_services/iam.tf`, `variables.tf`).
- ai_engine ML = joblib + sklearn/hmmlearn/lightgbm models from S3 (`ai_engine/models/model_registry.py`). No SageMaker.
- **Decision flagged:** the proposed backtest GenAI layer (`aws-serverless-genai-backtesting-design.md`) specifies **Bedrock**, but the repo already uses the **Anthropic SDK** pattern. Pick one before Phase 10 (reuse Anthropic SDK for consistency, or adopt Bedrock for IAM-native/no-key ops).

## 10. Existing scripts for reports 🟡

`scripts/monitoring/paper_session_report.py`, `scripts/monitoring/strategy_diagnosis_report.py`, plus `scripts/deploy/*` health/preflight. ❌ No backtest report generator — `backtester.py` prints to stdout + optional JSON only; no `report.md` renderer, no run/study aggregation.

## 11. Strategy adapters — 🟡 partial / ❌ lab adapters

Six production strategies exist: `momentum`, `orb`, `scalp_1m`, `vwap_reversion`, `intraday_trend_15m`, `preclose_momentum` (+ `base_strategy.py`, `candle_adapter.py`, `_math.py`, `_position_sizer.py`). The backtest CLI wires only **2 of 6** (momentum, scalp_1m). ❌ No generic, registry-driven backtest adapter set for all six. **Also:** `commands/run_backtest.yaml` references `services/strategy_engine/registry.py`, which **does not exist** (see §16).

## 12. TEE / MIS simulator — ✅ live code / ❌ offline simulator

Live/paper exit stack exists: `services/execution_engine/monitors/trade_exit_engine.py` (TEE), `services/execution_engine/mis_square_off.py` (MIS), `services/execution_engine/exit/{exit_order_router,exit_models,exit_policy_config}.py`, partial-profit-booking + R-based exits (ADR-028 / `docs/architecture/hybrid-trade-exit-and-mis-squareoff.md`). ❌ A **backtest** TEE old-vs-new + MIS square-off **simulator** (offline replay) does not exist; the live code is not wired into the backtester.

## 13. Cost / slippage support — ✅ core / 🟡 variants

Present in `backtester.py`: `IndianCostModel` (STT/exchange/SEBI/stamp/GST), brokerage per leg, `slippage_bps` + half-spread, gap-through stop fills, `max_order_bar_volume_pct` cap — **on by default**. 🟡 `commands/run_backtest.yaml` *declares* `slippage_model` (fixed/percentage/volume_based) and `commission_model` (zerodha/alpaca/zero), but only fixed-bps + IndianCostModel are implemented; `percentage`/`volume_based` and a `cost_model_version` stamp are **not yet built**.

## 14. No-lookahead controls — ✅ execution-time / ❌ data-layer

Present (execution-time): next-bar fills, `signal.generated_at` stamping, `lookahead_violations` counter, gap-stops checked on subsequent bars (`backtester.py`). ❌ Missing (data-layer, needed for a 15-yr lake): point-in-time corporate-action adjustment (read-time `adj_factor`), survivorship handling (delisted symbols), as-of universe/reference snapshots, walk-forward IS/OOS separation, dataset embargo. These depend on a lake that does not exist yet.

## 15. Missing pieces (consolidated)

❌ Parquet historical data lake + ingestion (bhavcopy backbone, vendor intraday) — CSV-only today · ❌ `backtest` env + `qe-bt-runs`/`qe-bt-checkpoints`/`qe-bt-datasets` · ❌ `backtest-worker` ARM64 ASG + Docker image + batch/shard runner · ❌ checkpoint/resume (currently single-process in-memory) · ❌ adapters for all 6 strategies (+ fix missing `registry.py`) · ❌ TEE/MIS offline simulator · ❌ walk-forward harness · ❌ model-dataset generator · 🟡 backtest report generator + metric extensions (Calmar, DD-duration, exposure, expectancy) · ❌ backtest GenAI analysis layer · ❌ backtest CI (empty `tests/backtest/`) · 🟡 data trust/quarantine model not yet in the data-lake contract (new CLAUDE.md rule) · 🟡 S3 path standardization not applied in code.

## 16. Blockers

1. **Historical data not procured (top dependency).** 10–15 yr NSE *intraday* requires a **licensed/vendor** feed (per the new CLAUDE.md rule: vendor/NSE data required for production validation; GitHub/free = LOW trust → quarantine). Only **NSE Bhavcopy daily** (official, free, survivorship-safe) is immediately usable. Intraday strategies (scalp_1m, ORB, VWAP, 15m, pre-close) cannot be production-validated until intraday history is sourced.
2. **No `backtest` AWS environment / scoped IAM** — must be created (Phase 2) before any infra runs.
3. **Stale references in `commands/run_backtest.yaml`:** `engine: scripts/backtest/run.py` (actual file is `run_backtest.py`) and `must_exist_in: services/strategy_engine/registry.py` (**no such file**). The existing command would fail as written; fix during Phase 4.
4. **GenAI approach undecided:** Bedrock (design doc) vs existing Anthropic SDK pattern (§9). Resolve before Phase 10.
5. **Repo working tree is dirty** — many uncommitted `M`/`??` files (live trading work) predate this lab. Recommend committing/branching before lab implementation so backtest changes stay isolated and reviewable.

## 17. Recommended next phase

**Phase 1 — Data Lake (`/aws_bt_data_lake`).** Build the Parquet lake on the **NSE Bhavcopy daily backbone** (free, official, survivorship-safe) with a pluggable `BarSource`, point-in-time corporate-action handling, quality checks, and a `data_snapshot_id`. The daily backbone unblocks the entire pipeline (registry → replay → metrics → walk-forward) without waiting on a vendor contract; licensed intraday is layered in later for production strategy validation.

**Suggested pre-Phase-1 housekeeping** (small, advisory — not a gate): formalize the source trust-tier / quarantine model in `aws-data-lake-contract.md` (per the new rule, and classify bhavcopy as trusted official NSE), fix the two stale refs in `run_backtest.yaml`, and register the lab in governance + add ADR-029 / `open_tasks.md` entries.

---

*Discovery only. No code implemented, no infra deployed, no live trading enabled, no broker APIs called. Stop for approval before Phase 1.*
