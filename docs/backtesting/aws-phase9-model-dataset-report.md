# AWS Backtesting Lab — Phase 9 Report: Model Dataset Generation

**Status:** COMPLETE — awaiting human approval before Phase 10  
**Date:** 2026-06-14  
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Phase 9 verifies the model training-dataset builder, closes 6 coverage gaps, fixes one bug
(`add_labels` crashing on empty signal list), and confirms the self-test runner end-to-end.
`model_dataset_builder.py` and 6 baseline tests were pre-existing from a prior session.
**Generates data only** — never trains, evaluates, or deploys a model. Backtest-only; no broker APIs.

---

## What Was Already Implemented

| File | Status |
|---|---|
| `services/backtesting/model_dataset_builder.py` | Pre-existing — complete |
| `scripts/backtest/build_model_dataset_aws.py` | Pre-existing — complete (`--self-test`) |
| `docs/backtesting/model-dataset-spec.md` | Pre-existing — matches implementation |
| `tests/backtest/test_model_dataset.py` (6 tests) | Pre-existing — all passing |

---

## Dataset Design

### Signal → Label pipeline (no lookahead)

```
SignalRecord.features   →  feat_{name} columns    (point-in-time, known at signal time)
SignalRecord.timestamp  →  forward_return_{Δ}     (future bars only: index >= signal_ts + Δ)
TEE outcome             →  exit_price, exit_reason, net_pnl, mfe, mae, hit_tp_before_sl
quality_label           →  int(net_pnl > threshold)
```

`assert_no_feature_leakage` rejects any `LABEL_FIELDS` key used as a feature — enforced
at row-build time, before any split.

### Column roles in `schema.json`

| Role | Columns |
|---|---|
| `identity` | `signal_id`, `strategy`, `symbol`, `timestamp` |
| `feature` | `feat_*` (prefixed; never bare `rsi_14` etc.) |
| `label` | `exit_price`, `exit_reason`, `net_pnl`, `mfe`, `mae`, `hit_tp_before_sl`, `profit_capture_ratio`, `forward_return_5m/15m/1h`, `quality_label` |
| `trade` | `entry_price`, `stop_price`, `target_price` |
| `meta` | `trust_level` |

### Chronological split + embargo

`chronological_split(train_frac, val_frac, embargo_minutes)` sorts by `timestamp` and slices:

```
train │ embargo │ val │ embargo │ test
```

`max(train.ts) ≤ min(val.ts)` and `max(val.ts) ≤ min(test.ts)` — guaranteed, with rows
inside embargo gaps removed. No shuffling.

### Trust enforcement

| Source trust | `allow_quarantined` | Result |
|---|---|---|
| ALL HIGH | either | Build succeeds; `authoritative=true` |
| ANY LOW | False (default) | `TrustError` raised — build rejected |
| ANY LOW | True | Build succeeds; `authoritative=false` in manifest |

### Outputs

`s3://quantembrace-backtest-results/datasets/<dataset_id>/` (and/or local):

| Artifact | Content |
|---|---|
| `train.parquet`, `val.parquet`, `test.parquet` | Split DataFrames |
| `schema.json` | Per-column dtype + role |
| `manifest.json` | versions, row counts, class balance per split, split boundaries, `trust_level`, `authoritative`, `created_at` |

---

## Self-Test Results

```
python scripts/backtest/build_model_dataset_aws.py --self-test

=== Model dataset ds_selftest ===
  rows total/train/val/test: 40/28/5/5
  trust: HIGH | authoritative: True
  features: ['feat_atr_14', 'feat_ema_ratio', 'feat_rsi_14']
  labels:   ['exit_price', 'exit_reason', 'forward_return_15m', 'forward_return_1h',
             'forward_return_5m', 'hit_tp_before_sl', 'mae', 'mfe', 'net_pnl',
             'profit_capture_ratio', 'quality_label']
  class_balance: {"train": {"1": 18, "0": 10}, "val": {"1": 3, "0": 2}, "test": {"1": 3, "0": 2}}
  versions: data=snapshot-unknown code=unknown strategy=momentum@2.0 exit_policy=tee@1.0
  written: 5 local file(s) → reports/model-datasets/ds_selftest/
  (generates data only — never trains/deploys a model)
```

---

## Bug Fix: `add_labels` Crashes on Empty Signal List

`add_labels` accessed `df["net_pnl"]` unconditionally. When `build_dataset([])` is called
(zero signals), `build_rows` returns a DataFrame with no columns, and `df["net_pnl"]` raises
`KeyError`.

**Fix** (`model_dataset_builder.py:165`):
```python
# Before:
df["quality_label"] = (df["net_pnl"].astype(float) > threshold).astype(int)

# After:
if df.empty or "net_pnl" not in df.columns:
    return df
df["quality_label"] = (df["net_pnl"].astype(float) > threshold).astype(int)
```

The empty-signal path now produces empty train/val/test DataFrames, `rows_total=0`, and
`authoritative=True` (no LOW-trust rows were present). This is a valid outcome — a dataset
run over a date range that produced no signals should not crash the pipeline.

---

## Tests

Phase 9 added 6 tests; combined total is 12 in `tests/backtest/test_model_dataset.py`.

**Pre-existing (6):**

| Test | Covers |
|---|---|
| `test_no_future_feature_leakage` | Guard rejects future fields; feature == point-in-time; feature/label disjoint |
| `test_label_generation_correct` | `quality_label`, `forward_return`, `profit_capture_ratio`, `hit_tp_before_sl` |
| `test_chronological_split` | `train.ts ≤ val.ts ≤ test.ts` |
| `test_chronological_split_with_embargo` | val starts ≥ train_end + embargo |
| `test_dataset_manifest_written` | 5 artifacts written; versions stamped; S3 keys; `authoritative=True` |
| `test_bad_source_trust_level_rejected` | LOW trust → `TrustError`; `allow_quarantined=True` → `authoritative=False` |

**New (6):**

| Test | Covers |
|---|---|
| `test_forward_return_handles_missing_series` | `None` series and empty series both return `None` |
| `test_quality_label_threshold` | Strictly > threshold; `0.0` → 0; non-default threshold respected |
| `test_feature_prefix_isolation` | Features appear as `feat_*`; bare names absent from `feature_columns` |
| `test_schema_column_roles` | identity/feature/label/trade/meta roles correctly assigned in schema |
| `test_empty_signals_build` | Zero signals → empty splits, `rows_total=0`, `authoritative=True` (verifies bug fix) |
| `test_no_broker_calls_in_dataset_builder` | `model_dataset_builder.py` contains no broker API references |

Full suite: **155 passed, 0 failed, 0 warnings** (`tests/backtest/` — all phases).

---

## File Manifest

```
# Python (modified)
services/backtesting/model_dataset_builder.py    (bug fix: add_labels guards empty df)
tests/backtest/test_model_dataset.py             (+6 tests, 6→12)
```

---

## Safety Invariants Verified

| Invariant | How verified |
|---|---|
| No broker API in builder | `test_no_broker_calls_in_dataset_builder` |
| Feature leakage guard enforced at build | `test_no_future_feature_leakage` (LeakageError) |
| Labels use future data only | `test_no_future_feature_leakage` (forward_return uses future bars) |
| Feature/label columns disjoint | `test_no_future_feature_leakage` (asserted in build_dataset) |
| LOW-trust source rejected for training | `test_bad_source_trust_level_rejected` (TrustError) |
| Explicit override flagged non-authoritative | `test_bad_source_trust_level_rejected` (authoritative=False) |
| Chronological ordering guaranteed, no shuffle | `test_chronological_split` |
| Embargo gap enforced | `test_chronological_split_with_embargo` |
| Empty signal list handled (no crash) | `test_empty_signals_build` |
| Generates data only — no model training/deployment | No ML library imports; no live ai_engine writes |
| 155 tests passing, 0 warnings | ✅ Confirmed: `python -m pytest tests/backtest/ -q` |

---

## Advisory Constraint

The dataset generator is advisory only. Per `model-dataset-spec.md` ADR (2026-06-06):

- No model is trained from this data in the current phase.
- Training requires: (1) authoritative HIGH-trust real NSE snapshot, (2) ≥1 base-strategy
  edge proven OOS, (3) sufficient labeled samples, (4) frozen features + embargo + human
  promotion, (5) shadow-mode plan.
- The `ai_engine` `ModelRegistry` boots `Dummy` stubs safely when no model exists —
  nothing is blocked by absence of trained models.

---

## Phase 10 Preview (NOT STARTED)

Phase 10 scope: **Serverless GenAI Layer**

`services/backtesting/genai/` exists from a prior session (analyst, bedrock_client, guardrails,
prompts, athena_queries). Phase 10 would:
- Verify the Bedrock/GenAI layer against its spec (`docs/backtesting/aws-serverless-genai-backtesting-design.md`)
- Confirm guardrails block trading-behavior changes
- Add missing coverage for analyst, prompt rendering, and guardrail enforcement
- Write the Phase 10 report

Phase 10 does **not** begin until this report is approved.

---

## Approval Required

Per governance: **a human must approve this report before Phase 10 begins.**

Checklist for approver:
- [ ] Dataset field groups accepted (identity / feature `feat_*` / label / trade / meta)
- [ ] Leakage-free design accepted (point-in-time features; future-only labels)
- [ ] `quality_label = int(net_pnl > threshold)` accepted
- [ ] Chronological split + embargo design accepted (defaults 0.70/0.15/0.15, 60 min embargo)
- [ ] Trust enforcement accepted (LOW → TrustError; override → `authoritative=false`)
- [ ] `add_labels` empty-DataFrame bug fix accepted
- [ ] Self-test results reviewed (40 signals → 28/5/5 split, HIGH-trust, 5 artifacts)
- [ ] 155 tests passing, 0 warnings (`python -m pytest tests/backtest/ -q`)
- [ ] Phase 10 scope (Serverless GenAI Layer) understood and approved
