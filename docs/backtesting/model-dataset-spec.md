# QuantEmbrace — Model Training Dataset Specification

> **Status (AWS-BT-10): implemented.** `services/backtesting/model_dataset_builder.py` + `scripts/backtest/build_model_dataset_aws.py` + `tests/backtest/test_model_dataset.py`. **Generates data only** — never trains, deploys, or alters any live model or trading behaviour. Backtest-only.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md`.

The lab generates labeled, leakage-free datasets to train the `ai_engine` **Signal Quality Scorer** (and, as a variant, the **Regime Classifier**) described in `architecture/system_design.md` (Layer 5).

---

## 1. Dataset fields

| Field | Group | Source |
|---|---|---|
| `signal_id`, `strategy`, `symbol`, `timestamp` | identity | signal |
| `feat_*` (e.g. `feat_rsi_14`, `feat_ema_ratio`, `feat_atr_14`) | **feature** (point-in-time) | `features_at_signal` |
| `entry_price`, `stop_price`, `target_price` | trade | signal |
| `exit_price`, `exit_reason`, `net_pnl`, `mfe`, `mae`, `hit_tp_before_sl`, `profit_capture_ratio` | **label** (future) | TEE outcome |
| `forward_return_5m`, `forward_return_15m`, `forward_return_1h` | **label** (future) | price series after the signal |
| `quality_label` | **label** | `1` if `net_pnl > threshold` else `0` |
| `trust_level` | meta | source provenance |

## 2. Leakage rules (enforced)

- **Features are point-in-time.** They come from `features_at_signal` (the Phase-5 adapter / `FeatureReader` set). `assert_no_feature_leakage` rejects any feature key that is a known future/label field.
- **Labels only use the future.** Forward returns are computed strictly from bars with `timestamp ≥ signal_ts + Δ`; outcome labels come from the realized trade.
- Feature columns (`feat_*`) and label columns are **disjoint** (asserted at build).

## 3. Labels

- `quality_label = 1` iff net-of-cost `net_pnl > quality_threshold` (default 0).
- `forward_return_Δ = (price(signal_ts+Δ) − price(signal_ts)) / price(signal_ts)`.
- `profit_capture_ratio = realized_r / mfe_r` (0 if `mfe_r ≤ 0`).
- `hit_tp_before_sl` carried from the TEE outcome.

## 4. Chronological split

`chronological_split` sorts by `timestamp` and slices **train → val → test** by fraction (defaults 0.70 / 0.15 / 0.15) with an **embargo gap** (default 60 min) so no split's feature window overlaps a prior split's label window. Guarantees `max(train.ts) ≤ min(val.ts) ≤ min(test.ts)`. No shuffling.

## 5. Trust enforcement

A training dataset requires **HIGH-trust** sources. If any row is **LOW-trust** (quarantined), the build **raises `TrustError`** unless `allow_quarantined=True` is passed after quality + license review — and the dataset is then flagged `authoritative=false` in the manifest.

## 6. Outputs

`s3://quantembrace-backtest-results/datasets/<dataset_id>/` (and/or local):

- `train.parquet`, `val.parquet`, `test.parquet`
- `schema.json` — per-column dtype + role (`identity` / `feature` / `label` / `trade` / `meta`)
- `manifest.json` — `data_version`, `code_version`, `strategy_version`, `exit_policy_version`, dataset_id/type, row counts per split, **class balance** per split, split boundaries, source run ids, `trust_level`, `authoritative`, `created_at`

## 7. Reproducibility & boundaries

Deterministic from `(source signals, price snapshot, config)`. The builder **does not** train, evaluate, or deploy models, and **does not** write to live `ai_engine` artifacts — it hands off versioned datasets + a stats manifest. Training and any model promotion are separate, human-approved activities.

## 8. Tooling & tests

| Artifact | Path |
|---|---|
| Builder | `services/backtesting/model_dataset_builder.py` |
| Runner | `scripts/backtest/build_model_dataset_aws.py` (`--self-test`) |
| Tests (6) | `tests/backtest/test_model_dataset.py` |

## 9. Decision — remain dataset-only; defer the model-training / MLOps track

> **ADR (2026-06-06): keep dataset generation only — do not build a training pipeline, experiment-tracking platform, or model-registry/promotion track yet.** Advisory-only governance intact. Revisit only when the prerequisites below hold. (The canonical ADR home `memory/decisions.md` was left untouched here because it carries live-trading edits; mirror there on request.)

**Why.** The `ai_engine` serving path already exists and degrades safely — `ModelRegistry` boots Dummy stubs when no model exists, `SignalQualityScorer` runs at threshold `0.0` (no filtering), `RegimeClassifier` returns `"unknown"` — so nothing is blocked by the absence of trained models. There is **no authoritative real data** (the data-quality gate fails for all strategies) and **no proven base-strategy edge**; a model trained now would be overfit noise and manufacture false confidence. For a solo retail operation, a full MLOps track is continuous maintenance disproportionate to advisory-only ML; the existing S3-joblib loader + hot-reload is sufficient serving infra.

**Premature-ML risks.** Overfitting to synthetic/short history; feature look-ahead leakage dressed as edge; data-snooping; false confidence raising the quality threshold on bad evidence; complexity debt obscuring whether the base strategy works; maintenance drag; non-authoritative models from LOW-trust data.

**Minimal future milestone.** One `SignalQualityScorer` (GBT) trained offline on a leakage-free HIGH-trust dataset, evaluated walk-forward OOS, serialized to one `model.joblib`, loaded by the existing `ModelRegistry`, run in shadow mode (threshold `0.0`) for ≥10 paper sessions before any threshold > 0. One model, one metric, manual promotion, no auto-retrain. Regime HMM strictly later.

**Prerequisites before training is worth doing.** (1) Authoritative HIGH-trust real NSE snapshot (`data_snapshot_id`); (2) ≥1 base-strategy edge proven on real data (walk-forward OOS positive after delivery costs); (3) enough real, leakage-checked HIGH-trust labeled samples for significance; (4) frozen point-in-time features + embargo split + single OOS gate + human promotion; (5) a shadow-mode activation plan; (6) reproducibility primitives (already present: `dataset_id`, `code_version`, `config_hash`).

**Non-goals now.** No MLflow / W&B / Kubeflow / SageMaker / feature store; no automated or continuous retraining; no CI/CD-for-models; no drift dashboards; no hyperparameter-search platform; no deep learning / ensembles / online learning; no GPU; no serving beyond the existing `ModelRegistry`; no raising the quality threshold above `0.0` without paper evidence; no ML on synthetic or LOW-trust data; ML never gains verdict authority.
