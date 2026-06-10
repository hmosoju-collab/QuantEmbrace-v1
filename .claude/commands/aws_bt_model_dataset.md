---
description: "Phase 9 — generate leakage-free training datasets for the ai_engine models."
argument-hint: "[dataset: signal_quality|regime]"
---

# /aws_bt_model_dataset — Phase 9: Model Training Dataset

Generate labeled datasets per `docs/backtesting/model-dataset-spec.md`. **Generates data only** — does not train, deploy, or alter any model or trading behavior.

## Load first
`model-dataset-spec.md`, `no-lookahead-rules.md`, `architecture/system_design.md` (Layer 5), `shared/features/feature_reader.py`.

## Do
1. Replay strategies + execution simulator (costs on) to produce per-signal `labels.parquet`.
2. Build point-in-time features matching the live `FeatureReader` set; attach labels.
3. Split by time (train/val/test) with an embargo gap; write `schema.json` + `manifest.json`; register in `qe-bt-datasets`.
4. Report class balance and split boundaries.

## Safety
No write to live `ai_engine` model artifacts. No training/deployment in this command.

## Output / Stop
Dataset spec + statistics report (rows, balance, splits, versions). **Stop for approval.**
