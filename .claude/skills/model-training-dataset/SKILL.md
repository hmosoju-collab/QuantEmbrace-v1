---
name: model-training-dataset
description: Generate leakage-free, time-split training datasets for the ai_engine quality scorer (GBT) and regime classifier (HMM) from backtest replays. Use for Phase 9 dataset generation. Generates data only — never trains/deploys models or changes trading behavior.
---

# Model Training Dataset

Authoritative spec: `docs/backtesting/model-dataset-spec.md`. Features must match the live `shared/features/feature_reader.py` set.

## When to use
Producing `signal_quality` or `regime` datasets for the existing ai_engine models.

## Procedure
1. Replay strategies + execution simulator (costs on) to produce per-signal `labels.parquet`.
2. Build point-in-time features (trailing windows only); attach labels.
3. Split by time (train/val/test) with an embargo gap; write `schema.json` + `manifest.json`; register in `qe-bt-datasets`.
4. Report class balance and split boundaries.

## Rules
No leakage (features never see their own/later label window). Generate data only — do not train, evaluate, deploy, or write live `ai_engine` artifacts.
