# QuantEmbrace — AWS-BT-10 Model Dataset Report

> **Phase 10 — AI quality-scorer training dataset. Implemented + tested.** Generates data only — never trains, evaluates, or deploys a model, and never writes to live `ai_engine` artifacts. Backtest-only; no broker APIs.
> Generated: 2026-06-06 · Governed by `aws-backtesting-steering.md` · Consumes AWS-BT-5/7 outcomes + lake prices.

---

## 1. What was built

- `services/backtesting/model_dataset_builder.py` — builds a leakage-free, chronologically-split, versioned training dataset from backtest signal+outcome records.
- `scripts/backtest/build_model_dataset_aws.py` — runner with `--self-test`.
- `docs/backtesting/model-dataset-spec.md` — updated to the implementation.

## 2. Leakage-free by construction

- **Features are point-in-time** (`feat_*`, from `features_at_signal`); `assert_no_feature_leakage` rejects any future/label field used as a feature.
- **Labels use only the future**: forward returns computed from bars at `signal_ts + Δ`; outcome labels from the realized trade.
- Feature and label column sets are **disjoint** (asserted at build).

## 3. Fields

identity (`signal_id, strategy, symbol, timestamp`) · features (`feat_*`) · trade (`entry_price, stop_price, target_price`) · labels (`exit_price, exit_reason, net_pnl, mfe, mae, hit_tp_before_sl, profit_capture_ratio, forward_return_5m/15m/1h, quality_label`) · meta (`trust_level`).

## 4. Split & trust

- **Chronological** train/val/test (default 0.70/0.15/0.15) with an **embargo gap** so no split's features overlap a prior split's label window; `max(train.ts) ≤ min(val.ts) ≤ min(test.ts)`.
- **LOW-trust sources are rejected** for training (`TrustError`) unless explicitly approved (`allow_quarantined=True`), in which case the manifest is flagged `authoritative=false`.

## 5. Outputs

`s3://quantembrace-backtests/model-datasets/<dataset_id>/`: `train/val/test.parquet`, `schema.json` (column roles), `manifest.json` (`data_version`, `code_version`, `strategy_version`, `exit_policy_version`, row counts, **class balance** per split, split boundaries, source run ids, `trust_level`, `authoritative`).

> Bucket-name note: this phase uses the user-requested `quantembrace-backtests`; the lab's canonical results bucket is `quantembrace-backtest-results`. Reconcile before infra build.

## 6. Test results

`tests/backtest/test_model_dataset.py` — **6/6 passing** (5 required + embargo) (full lab suite **76/76**).

| Test | Verifies |
|---|---|
| `no_future_feature_leakage` | guard raises on future feature; feature==point-in-time value; forward return uses future; feature/label disjoint |
| `label_generation_correct` | quality_label from net P&L; forward returns; profit_capture = realized_r/mfe_r |
| `chronological_split` | train ≤ val ≤ test by time |
| `chronological_split_with_embargo` | val starts ≥ train_end + embargo |
| `dataset_manifest_written` | parquet/schema/manifest written; versions + class balance; S3 keys |
| `bad_source_trust_level_rejected` | LOW trust → TrustError; with override → non-authoritative |

## 7. Self-test

`--self-test` built `ds_selftest` (40 signals): split 28/5/5, all HIGH-trust/authoritative, features `[feat_atr_14, feat_ema_ratio, feat_rsi_14]`, balanced classes per split, versions stamped, 5 artifacts written. Synthetic demonstration; production signals come from the replay engine + adapters + TEE, and features from the point-in-time `FeatureReader`.

## 8. Files

| Artifact | Path |
|---|---|
| Builder | `services/backtesting/model_dataset_builder.py` |
| Runner | `scripts/backtest/build_model_dataset_aws.py` |
| Tests (6) | `tests/backtest/test_model_dataset.py` |
| Spec | `docs/backtesting/model-dataset-spec.md` |

## 9. Recommended next phase

**Phase 11 — Serverless GenAI layer** (`/aws_bt_genai_layer`): on-demand Bedrock/Anthropic analysis + RAG over run outputs (resolve the Bedrock-vs-Anthropic-SDK decision from Phase 0 §9), advisory only.

---

*Implemented + tested. No infra deployed, no model trained/deployed, no live trading, no broker APIs called. Stop for approval before the next phase.*
