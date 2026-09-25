# AWS Backtesting Lab — Phase 12 Report: End-to-End Integration Smoke Test

**Status:** COMPLETE — awaiting human approval (final code-level phase)  
**Date:** 2026-06-14  
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Phase 12 is the **cross-layer** test that the prior phases (1–11) lacked: it wires
the *real* production classes of every lab layer together and drives one flow
through all of them, then asserts the lab's hard isolation invariants hold when
everything is connected — not just per-module in isolation.

Pipeline exercised end-to-end:

```
DataFrameBarSource → CandleReplayEngine → Backtester (real) →
  StrategyAdapter (real production strategy) → BacktestRunner →
  metrics_engine → ReportWriter (real, local) →
  ModelDatasetBuilder → GenAI analyst (stubbed LLM)
```

**Advisory / backtest-only.** Synthetic candles, in-memory DynamoDB fakes, a stub
LLM, a local `ReportWriter` — no AWS, no broker, no network, no model training,
and no live or paper trading behaviour is touched. Results are non-authoritative.

---

## What Was Verified at Runtime

### 1. Full-pipeline dry-run (`run_full_backtest_aws.py`)

Executed the pre-existing orchestrator end-to-end. It registered 5 runs, exercised
all 6 adapters, ran TEE old-vs-new, walk-forward, dataset build, and a cited GenAI
report. Summary (`reports/backtests/_full_run_summary.json`):

| Layer | Result |
|---|---|
| Runs registered | 5, all `bt_*` run_ids, `authoritative=false` |
| Adapters exercised | momentum, preclose, vwap_reversion, orb, trend_15m, scalp_1m |
| `scalp_1m.paper_only` | `True` |
| Walk-forward verdict | `ELIGIBLE_FOR_PAPER_PRIORITIZATION` (44 folds) — note: **paper**, never live |
| Model dataset | 120 rows, `authoritative=True`, features `feat_*`-prefixed |
| GenAI report | cited (`## Sources`), `model=stub` |
| TEE comparison | `old` vs `new` policy |

### 2. New integration test suite (`tests/backtest/test_e2e_integration.py`)

15 tests, all passing, 0 warnings (`-W error`).

| # | Test | What it proves |
|---|---|---|
| 1 | `test_full_pipeline_with_real_strategy_adapter` | Real `MomentumStrategy` → engine → Backtester → runner → metrics → real local report; run reaches COMPLETED; result path is on `quantembrace-backtest-results`; 2 shards processed |
| 2 | `test_all_six_adapters_collect_signals_end_to_end` | All 6 production adapters run through the engine path without error; emitted signals carry `strategy_version`, `data_version`, `backtest=True`, TEE `exit_policy_version` |
| 3 | `test_scalp_is_paper_only_end_to_end` | `scalp_1m` forces `paper_trade=True` even when the caller passes `paper_trade=False` |
| 4 | `test_pipeline_outputs_feed_leakage_free_dataset` | Signal+outcome → `feat_*` features disjoint from labels, chronological train≤val≤test, HIGH-trust → authoritative |
| 5 | `test_genai_explains_but_never_recommends_promotion` | Cited report produced; even textbook-strong evidence → `recommend_promotion=False` |
| 6 | `test_registry_and_checkpoint_reject_live_tables` (×6) | `RunRegistry` and `CheckpointManager` refuse `quantembrace-*-orders`, `positions`, `risk-state`, `strategy-config`, `sessions`, bare `orders` |
| 7 | `test_registry_accepts_only_backtest_tables` | `qe-bt-runs` / `qe-bt-checkpoints` accepted — isolation does not block the lab itself |
| 8 | `test_no_broker_imports_or_order_calls_in_backtesting_package` | Scans **every** `.py` in `services/backtesting/` — no broker import, no `place_order(`/`submit_order(` call site |
| 9 | `test_resume_skips_completed_partitions` | A worker resuming a partially-done RUNNING run reprocesses only the pending shard; reaches COMPLETED |
| 10 | `test_spot_interrupted_partition_is_retried_on_resume` | A shard marked FAILED (spot interruption) returns to the pending work list with `reason=spot_interruption`, `retry_count=1` |

---

## Cross-Contamination Findings

| Check | Result |
|---|---|
| Live DynamoDB tables touched | **None** — `assert_backtest_table` rejects all 14 trading-runtime tables at construction (test 6) |
| Broker library imported anywhere in package | **None** — package-wide regex scan (test 8) |
| Broker order call site (`place_order(`/`submit_order(`) | **None** — only `guardrails.py` names them as regex *literals it blocks* (no call sites) |
| Capital / NAV mutation | **None** — backtest uses a fixed `initial_capital`; no write reaches a live table or broker |
| `scalp_1m` reaching a real broker | **Impossible** — `paper_only=True` forced through `enrich()` (test 3) |
| GenAI recommending promotion | **Never** — `recommend_promotion` is permanently `False` (test 5) |
| Result/checkpoint writes | Only to `quantembrace-backtest-*` (test 1) and `qe-bt-*` tables |

**No blocking issues found.** The lab is isolated from live/paper trading when fully wired.

---

## A Real Finding (not a bug, a semantic boundary)

While writing the resume test I confirmed the registry's **terminal-state guard**
is strict: a `COMPLETED` or `FAILED` run cannot be moved back to `RUNNING` via
`mark_running` (`IllegalTransition`). This means:

- A genuinely *resumable* run is one still in `RUNNING` state (the worker died,
  the run was never marked terminal). Resume reprocesses only the shards the
  checkpoint table has not marked DONE. ✅ (test 9)
- The SIGTERM/Spot path marks the run `FAILED`. Re-running the **same config**
  returns the existing `FAILED` record (idempotent `run_id`); a retry of a
  terminal run is therefore an **explicit operator decision**, not an automatic
  re-drive — consistent with "a human approves all production changes." The
  shard-level checkpoint still correctly lists the interrupted shard as pending
  (test 10), so the data needed to resume is intact.

This is the correct, conservative behaviour for a capital-protection-first
platform: nothing silently resurrects a terminal run. No code change made.

---

## File Manifest

```
# Python (new)
tests/backtest/test_e2e_integration.py    (NEW — 15 cross-layer integration tests)

# No production code modified in Phase 12.
docs/backtesting/aws-phase12-e2e-integration-report.md   (NEW — this report)
```

The dry-run orchestrator (`scripts/backtest/run_full_backtest_aws.py`) and all
layer modules were pre-existing and unchanged. Phase 12 added test coverage only.

---

## Test Results

```
$ python -m pytest tests/backtest/ -q
176 passed in 1.47s

$ python -m pytest tests/backtest/test_e2e_integration.py -q -W error
15 passed   (0 warnings)
```

Phase progression: 161 (after Phase 11) → **176** (after Phase 12), +15 integration tests.

---

## Safety Invariants Verified

| Invariant | How verified |
|---|---|
| Full pipeline runs with real production strategy | `test_full_pipeline_with_real_strategy_adapter` |
| All 6 adapters wired correctly | `test_all_six_adapters_collect_signals_end_to_end` |
| `scalp_1m` is paper-only end-to-end | `test_scalp_is_paper_only_end_to_end` |
| Datasets remain leakage-free through the pipeline | `test_pipeline_outputs_feed_leakage_free_dataset` |
| GenAI explains, never promotes | `test_genai_explains_but_never_recommends_promotion` |
| Live tables rejected when fully wired | `test_registry_and_checkpoint_reject_live_tables` |
| Backtest tables still accepted | `test_registry_accepts_only_backtest_tables` |
| No broker code anywhere in the package | `test_no_broker_imports_or_order_calls_in_backtesting_package` |
| Runs are resumable (skip completed shards) | `test_resume_skips_completed_partitions` |
| Interrupted shards are retried | `test_spot_interrupted_partition_is_retried_on_resume` |
| No terminal run silently resurrected | Confirmed via registry `IllegalTransition` guard (documented above) |
| 176 tests passing, 0 warnings | ✅ `python -m pytest tests/backtest/ -q` |

---

## Lab Status After Phase 12

All **code-level** phases of the AWS backtesting lab are now implemented and
cross-layer tested:

| Phase | Layer | State |
|---|---|---|
| 1 | S3 data lake / catalog | Code complete, tested |
| 2 | Data quality + leakage gates | Code complete, tested |
| 3 | Replay engine (resume/checkpoint) | Code complete, tested |
| 4 | Run registry + checkpoint manager | Code complete, tested |
| 5 | Execution simulator (costs/slippage) | Code complete, tested |
| 6 | TEE old-vs-new + MIS square-off | Code complete, tested |
| 7 | Metrics + report writer | Code complete, tested |
| 8 | Strategy adapters (6 strategies) | Code complete, tested |
| 9 | Model dataset generation | Code complete, tested |
| 10 | Serverless GenAI layer | Code complete, tested |
| 11 | AWS infrastructure (Terraform) | `terraform validate` PASS; **not applied** |
| 12 | End-to-end integration smoke test | Code complete, tested |

**Still `[PLANNED — not yet implemented]` / pending operator action:**

- No real licensed 10–15-year NSE data ingested (Phase-0 data blocker). All runs
  to date are SYNTHETIC and **non-authoritative**.
- No AWS infrastructure provisioned — `terraform apply` is withheld pending
  explicit operator sign-off (Phase 11 prerequisites: state bucket, plan review).
- No strategy has been validated against real data; the lab can **recommend**,
  it **cannot promote**. Live trading remains BLOCKED.

---

## Phase 13 Preview (NOT STARTED)

With every code layer complete and cross-layer tested, the remaining work is
**operator-gated and data-gated**, not code:

1. **Operator-approved Terraform apply** — provision the `backtest` environment
   (state bucket → `terraform plan` review → explicit `apply`).
2. **Real NSE historical data ingestion** — licensed/official source into the
   curated lake; everything before this is non-authoritative.
3. **First authoritative backtest** — once real data exists, run the validated
   pipeline and produce the first *authoritative* (still advisory) report.

Each is a distinct, human-approved step. None auto-proceeds.

---

## Approval Required

Per governance: **a human must approve this report.** This is the final
code-level phase; the next steps require operator sign-off and real data.

Checklist for approver:
- [ ] Full-pipeline cross-layer flow accepted (real strategy → engine → runner → metrics → report → dataset → GenAI)
- [ ] All 6 adapters integration-tested
- [ ] `scalp_1m` paper-only invariant confirmed end-to-end
- [ ] Leakage-free dataset confirmed through the pipeline
- [ ] GenAI never-promote invariant confirmed
- [ ] Live-table rejection confirmed for all trading-runtime tables
- [ ] Package-wide no-broker scan accepted
- [ ] Resume + spot-interruption semantics accepted (including strict terminal-state guard)
- [ ] 176 tests passing, 0 warnings
- [ ] Lab status understood: all code complete; data + infra remain operator/data-gated; live stays BLOCKED
