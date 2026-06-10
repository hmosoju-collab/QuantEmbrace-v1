"""Unit tests for the model dataset builder (Phase AWS-BT-10).

Covers: no future feature leakage · label generation correct · chronological
split · dataset manifest written · bad source trust level rejected for training.

Backtest-only: synthetic signals + price series, fake S3 — no AWS, no broker,
no model training.

Run:  python -m pytest tests/backtest/test_model_dataset.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.model_dataset_builder import (  # noqa: E402
    LABEL_FIELDS,
    DatasetMeta,
    LeakageError,
    ModelDatasetBuilder,
    SignalRecord,
    TrustError,
    assert_no_feature_leakage,
    forward_return,
    quality_label,
)

IST = "Asia/Kolkata"


def _series() -> pd.Series:
    idx = pd.date_range("2020-06-01 10:00", periods=24, freq="5min", tz=IST)
    return pd.Series([100 + i for i in range(24)], index=idx)  # rising 1/bar


def _sig(i: int, pnl: float, *, trust: str = "HIGH") -> SignalRecord:
    t = pd.Timestamp("2020-06-01 10:00", tz=IST) + pd.Timedelta(minutes=5 * i)
    return SignalRecord(
        signal_id=f"s{i}", strategy="momentum", symbol="R", timestamp=t,
        features={"rsi_14": 50 + i, "ema_ratio": 1.0},
        entry_price=100.0, stop_price=99.0, target_price=103.0,
        exit_price=102.0 if pnl > 0 else 99.0, exit_reason="FINAL_TARGET" if pnl > 0 else "STOP_LOSS",
        net_pnl=pnl, mfe_r=2.0, mae_r=-0.5, realized_r=1.5 if pnl > 0 else -1.0,
        hit_tp_before_sl=pnl > 0, trust_level=trust,
    )


def _meta(**kw) -> DatasetMeta:
    base = dict(dataset_id="ds_test", data_version="snap1", code_version="abc123",
                strategy_version="momentum@2.0", exit_policy_version="tee@1.0",
                train_frac=0.6, val_frac=0.2, embargo_minutes=0)
    base.update(kw)
    return DatasetMeta(**base)


class FakeS3:
    def __init__(self):
        self.keys: list[str] = []

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.keys.append(Key)
        return {}


# ── leakage ──────────────────────────────────────────────────────────────────


def test_no_future_feature_leakage():
    # Guard rejects future fields used as features.
    with pytest.raises(LeakageError):
        assert_no_feature_leakage(["rsi_14", "forward_return_5m"])

    res = ModelDatasetBuilder().build_dataset([_sig(i, 100 if i % 2 == 0 else -50) for i in range(12)],
                                              {"R": _series()}, _meta())
    # Feature and label columns are disjoint.
    assert not (set(res.feature_columns) & set(res.label_columns))
    # Feature is the point-in-time value (s0 had rsi_14=50) — not a future value.
    assert res.train["feat_rsi_14"].iloc[0] == 50
    # Forward return reflects the FUTURE price (label side): (101-100)/100 = 0.01.
    assert round(float(res.train["forward_return_5m"].iloc[0]), 5) == 0.01
    # No feature column is a known future/label field.
    assert all(c.replace("feat_", "") not in LABEL_FIELDS for c in res.feature_columns)


# ── labels ───────────────────────────────────────────────────────────────────


def test_label_generation_correct():
    assert quality_label(100) == 1 and quality_label(-1) == 0 and quality_label(0) == 0
    s = _series()
    # forward return over 15m from 10:00: (103-100)/100 = 0.03.
    assert round(forward_return(s, pd.Timestamp("2020-06-01 10:00", tz=IST), pd.Timedelta(minutes=15)), 5) == 0.03

    res = ModelDatasetBuilder().build_dataset([_sig(0, 100), _sig(1, -50)], {"R": s}, _meta(train_frac=1.0, val_frac=0.0))
    row0 = res.train.iloc[0]
    assert row0["quality_label"] == 1
    assert row0["hit_tp_before_sl"]
    assert round(float(row0["profit_capture_ratio"]), 5) == 0.75  # realized_r 1.5 / mfe_r 2.0


# ── split ────────────────────────────────────────────────────────────────────


def test_chronological_split():
    res = ModelDatasetBuilder().build_dataset([_sig(i, 100 if i % 2 == 0 else -50) for i in range(15)],
                                              {"R": _series()}, _meta(train_frac=0.6, val_frac=0.2))
    assert len(res.train) and len(res.val) and len(res.test)
    assert res.train["timestamp"].max() <= res.val["timestamp"].min()
    assert res.val["timestamp"].max() <= res.test["timestamp"].min()


def test_chronological_split_with_embargo():
    res = ModelDatasetBuilder().build_dataset([_sig(i, 100) for i in range(15)], {"R": _series()},
                                              _meta(train_frac=0.6, val_frac=0.2, embargo_minutes=15))
    if len(res.val):
        gap = res.val["timestamp"].min() - res.train["timestamp"].max()
        assert gap >= pd.Timedelta(minutes=15)


# ── manifest / write ─────────────────────────────────────────────────────────


def test_dataset_manifest_written(tmp_path):
    b = ModelDatasetBuilder()
    res = b.build_dataset([_sig(i, 100 if i % 2 == 0 else -50) for i in range(12)], {"R": _series()}, _meta())
    s3 = FakeS3()
    out = b.write(res, base="s3://quantembrace-backtest-results/datasets",
                  local_dir=str(tmp_path), s3_client=s3)
    ds_dir = tmp_path / "ds_test"
    for f in ("train.parquet", "val.parquet", "test.parquet", "schema.json", "manifest.json"):
        assert (ds_dir / f).exists()
    manifest = json.loads((ds_dir / "manifest.json").read_text())
    assert manifest["data_version"] == "snap1" and manifest["code_version"] == "abc123"
    assert manifest["strategy_version"] == "momentum@2.0" and manifest["exit_policy_version"] == "tee@1.0"
    assert manifest["rows_total"] == 12 and "class_balance" in manifest
    assert manifest["authoritative"] is True
    assert any(k.endswith("manifest.json") for k in s3.keys)


# ── trust ────────────────────────────────────────────────────────────────────


def test_bad_source_trust_level_rejected():
    sigs = [_sig(0, 100, trust="LOW"), _sig(1, -50)]
    with pytest.raises(TrustError):
        ModelDatasetBuilder().build_dataset(sigs, {"R": _series()}, _meta())
    # Explicit approval builds, but the dataset is marked non-authoritative.
    res = ModelDatasetBuilder(allow_quarantined=True).build_dataset(sigs, {"R": _series()}, _meta(train_frac=1.0, val_frac=0.0))
    assert res.trust_level == "LOW"
    assert res.manifest["authoritative"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
