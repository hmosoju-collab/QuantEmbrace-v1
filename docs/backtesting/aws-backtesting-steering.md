# QuantEmbrace — AWS Historical Backtesting Lab: Steering

> **Status: PLANNED — design only.** No engine code or infrastructure is deployed by this document.
> **Scope: backtest-only.** This lab never places broker orders, never mutates live/paper tables, never changes capital, never enables live trading.
> Last updated: 2026-06-06 · North-star for every other `docs/backtesting/` doc, command, agent, and skill.

---

## 1. Why this lab exists

Live trading is **blocked** until ≥5 consecutive valid quality-gate paper sessions pass all strategy-performance gates (CLAUDE.md § *Strategy Performance Live-Readiness Rule*). Paper sessions accumulate edge slowly — one trading day at a time. A historical backtesting lab lets us validate strategy edge against **10–15 years of NSE data** in hours instead of months, generate the **AI quality-scorer training dataset**, and run **TEE old-vs-new** and **walk-forward** validation — all without risking capital.

The lab is an **offline research environment**, fully isolated from the live/paper trading platform.

---

## 2. Guiding principles

1. **Capital protection > trade count > profit** — inherited from the live platform. A backtest that hides costs or leaks future data is worse than no backtest.
2. **Reuse, don't rewrite** — the replay engine already exists at `services/strategy_engine/backtesting/backtester.py`. Extend it; never fork a parallel engine (`prefer_refactor_over_rewrite`).
3. **Realism is mandatory** — costs and slippage are always on; no-lookahead is enforced and asserted; survivorship bias is addressed by including delisted symbols.
4. **Everything is reproducible** — every run persists its config, code version, data snapshot, trades, metrics, equity curve, logs, model labels, and report.
5. **Everything is resumable** — long multi-year/multi-symbol runs checkpoint and restart safely and idempotently.
6. **Cost-conscious infra** — EC2 ARM64 (Graviton) Docker workers that scale from zero; S3 Parquet over hot stores; on-demand serverless for GenAI. No idle compute.
7. **Backtest-only safety** — no broker SDK in the worker path; no live/paper DynamoDB tables; a hard namespace boundary.

---

## 3. Hard boundaries (non-negotiable)

| Rule | Enforcement |
|---|---|
| No live trading enablement | No `QE_EXECUTION_LIVE_TRADING_ENABLED` anywhere in lab; no execution_engine broker path imported |
| No broker order placement | Worker images exclude/forbid Zerodha & Alpaca order APIs; lint gate |
| No capital change | Lab reads market data only; writes only to backtest namespace |
| No live/paper table mutation | Lab uses a **separate environment + table prefix**; cross-prefix access is a CI violation |
| No paper/live data mixing | Backtest namespace keys never overlap `PAPER#*` / `LIVE#*` snapshot namespaces |
| No lookahead leakage | `no-lookahead-rules.md` rules + `lookahead_violations == 0` assertion per run |
| Costs & slippage always applied | Disabling requires explicit flag, is logged, and is flagged in the report |

---

## 4. Canonical conventions (single source of truth)

All other docs and code MUST use these names. Resolves the pre-existing S3-path inconsistency (`backtest/results/{run_id}/` vs `backtests/{strategy}/{timestamp}/`) — this layout supersedes both.

**Environment / namespace**
- Environment name: `backtest`
- Resource prefix: `quantembrace-backtest-`
- DynamoDB table prefix: `qe-bt-`
- CloudWatch namespace: `QuantEmbrace/Backtest`
- SNS topic: `quantembrace-backtest-alerts`

**S3 buckets**
- `quantembrace-backtest-data` — raw drops, curated Parquet lake, reference/corporate-actions
- `quantembrace-backtest-results` — run outputs, walk-forward studies, datasets, reports

**S3 key layout** (detail in `aws-data-lake-contract.md`)
```
s3://quantembrace-backtest-data/
  raw/{source}/...
  lake/ohlcv/market={NSE|US}/symbol={SYM}/interval={1d|1m|5m|15m}/year={YYYY}/part-*.parquet
  reference/corporate_actions/{market}/{symbol}.parquet
s3://quantembrace-backtest-results/
  runs/{run_id}/{config.json, trades.parquet, metrics.json, equity_curve.parquet, labels.parquet, logs/, report.md}
  walkforward/{study_id}/...
  datasets/{dataset_id}/...
```

**DynamoDB tables** (detail in `aws-backtest-run-registry.md`)
- `qe-bt-runs` — run registry + metric summaries
- `qe-bt-checkpoints` — shard cursors for resume
- `qe-bt-datasets` — training-dataset registry

**Compute**
- `backtest-worker` EC2 ARM64 ASG (c6g/c7g), **min=0**, scale-up on queued runs, scale-to-0 when idle. Docker workers, reusing the existing `infra/terraform/modules/ec2_services` pattern. No Fargate / ECS / EKS / Lambda-for-compute / SQS / Kinesis (consistent with ADR-009/010 and `docs/06_aws_infrastructure.md`).

---

## 5. Reuse vs net-new

| Already exists (reuse) | Net-new (later phases) |
|---|---|
| `services/strategy_engine/backtesting/backtester.py` (replay engine) | Parquet data-lake loader (current loader is CSV-only) |
| `scripts/backtest/run_backtest.py` (CLI) | Batch/parallel shard runner + checkpoint/resume |
| `commands/run_backtest.yaml` (command spec) | `qe-bt-runs` / `qe-bt-checkpoints` DynamoDB tables |
| S3 buckets `*-ohlcv-data`, `*-backtest-results` | `backtest-worker` ASG + userdata |
| `IndianCostModel`, slippage/spread, gap-stops | Walk-forward harness; TEE old-vs-new + MIS sim at scale |
| `agents/*.yaml` role agents | Model-dataset generator; serverless GenAI layer |

## 6. Relationship to the live platform

The lab **consumes** the same strategy classes, cost model, and feature definitions so results transfer to live decisions, but runs in a **separate AWS environment**. It produces **advisory** outputs only: reports, datasets, and recommendations. It never writes to live/paper trading state and never promotes anything. Promotion to live remains a manual operator decision gated by paper sessions, exactly as today.

## 7. Document map

`aws-backtesting-specification.md` (what to build) · `aws-backtesting-implementation-plan.md` (phased build + report gates) · `aws-data-lake-contract.md` · `aws-backtest-run-registry.md` · `aws-serverless-genai-backtesting-design.md` · `no-lookahead-rules.md` · `cost-slippage-model.md` · `metrics-catalog.md` · `walk-forward-validation.md` · `model-dataset-spec.md`. Decisions are recorded as **ADR-029** in `memory/decisions.md` (to be added in a later approved step).
