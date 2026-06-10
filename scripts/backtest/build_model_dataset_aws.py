#!/usr/bin/env python3
"""Build an AI quality-scorer training dataset from backtest replays.

Assembles leakage-free, chronologically-split datasets for the `ai_engine`
quality scorer (point-in-time features, future-only labels) and writes them to
``s3://quantembrace-backtest-results/datasets/<dataset_id>/`` (and/or locally).

**Generates data only** — never trains, evaluates, or deploys a model, and never
writes to live `ai_engine` artifacts. Backtest-only; no broker APIs.

In production, signals + outcomes come from the replay engine + strategy adapters
+ TEE, and features from the point-in-time `FeatureReader`. ``--self-test`` uses
synthetic signals so the builder runs end-to-end without data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.model_dataset_builder import (  # noqa: E402
    DEFAULT_DATASET_BASE,
    DatasetMeta,
    ModelDatasetBuilder,
    SignalRecord,
)

IST = "Asia/Kolkata"


def _synthetic(n: int = 40) -> tuple[list[SignalRecord], dict]:
    base = pd.Timestamp("2020-06-01 09:30", tz=IST)
    idx = pd.date_range(base, periods=n + 24, freq="5min", tz=IST)
    price = pd.Series([100 + (i % 17) - 8 for i in range(len(idx))], index=idx)  # oscillating
    sigs: list[SignalRecord] = []
    for i in range(n):
        t = base + pd.Timedelta(minutes=5 * i)
        pnl = 80.0 if i % 3 else -60.0
        sigs.append(SignalRecord(
            signal_id=f"bt_s{i}", strategy="momentum", symbol="R", timestamp=t,
            features={"rsi_14": 40 + (i % 30), "ema_ratio": 1.0 + (i % 5) * 0.01, "atr_14": 1.5},
            entry_price=float(price.asof(t)), stop_price=float(price.asof(t)) - 1.0,
            target_price=float(price.asof(t)) + 2.0, exit_price=float(price.asof(t)) + (2.0 if pnl > 0 else -1.0),
            exit_reason="FINAL_TARGET" if pnl > 0 else "STOP_LOSS", net_pnl=pnl,
            mfe_r=2.0, mae_r=-0.6, realized_r=1.5 if pnl > 0 else -1.0, hit_tp_before_sl=pnl > 0,
            trust_level="HIGH",
        ))
    return sigs, {"R": price}


def main() -> int:
    p = argparse.ArgumentParser(description="Build AI quality-scorer dataset (backtest-only)")
    p.add_argument("--dataset-id", default="ds_selftest")
    p.add_argument("--data-version", default="snapshot-unknown")
    p.add_argument("--code-version", default="unknown")
    p.add_argument("--strategy-version", default="momentum@2.0")
    p.add_argument("--exit-policy-version", default="tee@1.0")
    p.add_argument("--base", default=DEFAULT_DATASET_BASE)
    p.add_argument("--local-dir", default=str(_REPO / "reports" / "model-datasets"))
    p.add_argument("--allow-quarantined", action="store_true")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if not args.self_test:
        print("No real signal source wired; running --self-test (synthetic).")
    signals, price_lookup = _synthetic()

    meta = DatasetMeta(
        dataset_id=args.dataset_id, data_version=args.data_version, code_version=args.code_version,
        strategy_version=args.strategy_version, exit_policy_version=args.exit_policy_version,
        source_run_ids=("bt_selftest",),
        # Synthetic self-test signals are 5 min apart; a small embargo keeps the
        # demo split non-degenerate. Real (multi-year) datasets use a larger gap.
        embargo_minutes=10,
    )
    builder = ModelDatasetBuilder(allow_quarantined=args.allow_quarantined)
    result = builder.build_dataset(signals, price_lookup, meta)
    # Local only for the self-test (no S3 client); pass an s3_client in production.
    out = builder.write(result, base=args.base, local_dir=args.local_dir)

    print(f"\n=== Model dataset {result.meta.dataset_id} ===")
    print(f"  rows total/train/val/test: {result.manifest['rows_total']}/"
          f"{result.manifest['rows_train']}/{result.manifest['rows_val']}/{result.manifest['rows_test']}")
    print(f"  trust: {result.trust_level} | authoritative: {result.manifest['authoritative']}")
    print(f"  features: {result.feature_columns}")
    print(f"  labels:   {result.label_columns}")
    print(f"  class_balance: {json.dumps(result.manifest['class_balance'])}")
    print(f"  versions: data={meta.data_version} code={meta.code_version} "
          f"strategy={meta.strategy_version} exit_policy={meta.exit_policy_version}")
    print(f"  written: {len(out['files'])} local file(s) → {args.local_dir}/{result.meta.dataset_id}/")
    print(f"  s3 base: {out['s3_base']}")
    print("  (generates data only — never trains/deploys a model)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
