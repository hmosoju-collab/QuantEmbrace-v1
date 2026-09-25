"""Model training-dataset builder for the QuantEmbrace backtesting lab.

Turns backtest signal+outcome records into a **leakage-free** training dataset for
the `ai_engine` quality scorer (and, optionally, the regime classifier):

    * **features** are point-in-time (known at signal time) — never future-derived,
    * **labels** (forward returns, exit outcome, quality_label) use future data,
    * the **train/validation/test split is chronological** (with an embargo gap),
    * **LOW-trust** (quarantined) sources are **rejected** for training unless
      explicitly approved, and the dataset is then marked non-authoritative.

Every dataset records `data_version`, `code_version`, `strategy_version`, and
`exit_policy_version`. **Generates data only** — it never trains, evaluates, or
deploys a model, and never writes to live `ai_engine` artifacts. Backtest-only;
no broker APIs.
"""

from __future__ import annotations

import io
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from backtesting.s3_data_catalog import TrustLevel

DEFAULT_DATASET_BASE = "s3://quantembrace-backtest-results/datasets"

# Columns that depend on the FUTURE — must never appear among features.
LABEL_FIELDS: frozenset[str] = frozenset({
    "exit_price", "exit_reason", "net_pnl", "mfe", "mae", "hit_tp_before_sl",
    "profit_capture_ratio", "forward_return_5m", "forward_return_15m",
    "forward_return_1h", "quality_label",
})

IDENTITY_FIELDS = ("signal_id", "strategy", "symbol", "timestamp")
TRADE_FIELDS = ("entry_price", "stop_price", "target_price")


class LeakageError(Exception):
    """Raised when a future-derived value would enter the feature set."""


class TrustError(Exception):
    """Raised when LOW-trust data is used for a training dataset without approval."""


@dataclass
class SignalRecord:
    signal_id: str
    strategy: str
    symbol: str
    timestamp: pd.Timestamp          # signal time (tz-aware)
    features: dict                   # point-in-time features (known at signal time)
    entry_price: float
    stop_price: float
    target_price: float
    exit_price: float
    exit_reason: str
    net_pnl: float
    mfe_r: float
    mae_r: float
    realized_r: float
    hit_tp_before_sl: bool
    trust_level: str = TrustLevel.HIGH.value


@dataclass
class DatasetMeta:
    dataset_id: str
    dataset_type: str = "signal_quality"   # or "regime"
    data_version: str = "unknown"
    code_version: str = "unknown"
    strategy_version: str = "unknown"
    exit_policy_version: str = "unknown"
    quality_threshold: float = 0.0
    train_frac: float = 0.70
    val_frac: float = 0.15
    embargo_minutes: int = 60
    source_run_ids: tuple[str, ...] = ()


@dataclass
class DatasetResult:
    meta: DatasetMeta
    train: pd.DataFrame
    val: pd.DataFrame
    test: pd.DataFrame
    feature_columns: list[str]
    label_columns: list[str]
    trust_level: str
    manifest: dict = field(default_factory=dict)
    schema: dict = field(default_factory=dict)


def assert_no_feature_leakage(feature_keys) -> None:
    """Raise if any feature key is a future/label field."""
    bad = [k for k in feature_keys if k in LABEL_FIELDS]
    if bad:
        raise LeakageError(f"Future/label fields cannot be features: {bad}")


def quality_label(net_pnl: float, threshold: float = 0.0) -> int:
    """1 if the trade was net-of-cost profitable beyond threshold, else 0."""
    return int(net_pnl > threshold)


def forward_return(price_series: pd.Series | None, signal_ts: pd.Timestamp, delta: timedelta) -> float | None:
    """Return over the next ``delta`` from the signal (uses ONLY future bars)."""
    if price_series is None or price_series.empty:
        return None
    idx = price_series.index
    at_or_before = price_series[idx <= signal_ts]
    base = float(at_or_before.iloc[-1]) if len(at_or_before) else float(price_series.iloc[0])
    target_ts = signal_ts + delta
    future = price_series[idx >= target_ts]
    if future.empty or base == 0:
        return None
    return (float(future.iloc[0]) - base) / base


class ModelDatasetBuilder:
    def __init__(self, *, allow_quarantined: bool = False) -> None:
        self._allow_quarantined = allow_quarantined

    # ── row assembly ────────────────────────────────────────────────────────────
    def build_rows(self, signals: list[SignalRecord], price_lookup: dict[str, pd.Series]) -> pd.DataFrame:
        rows: list[dict] = []
        for s in signals:
            assert_no_feature_leakage(s.features.keys())  # guard: no future in features
            series = price_lookup.get(s.symbol)
            row: dict[str, Any] = {
                "signal_id": s.signal_id,
                "strategy": s.strategy,
                "symbol": s.symbol,
                "timestamp": s.timestamp,
                "entry_price": s.entry_price,
                "stop_price": s.stop_price,
                "target_price": s.target_price,
                "exit_price": s.exit_price,
                "exit_reason": s.exit_reason,
                "net_pnl": s.net_pnl,
                "mfe": s.mfe_r,
                "mae": s.mae_r,
                "hit_tp_before_sl": bool(s.hit_tp_before_sl),
                "profit_capture_ratio": (s.realized_r / s.mfe_r) if s.mfe_r > 0 else 0.0,
                "forward_return_5m": forward_return(series, s.timestamp, timedelta(minutes=5)),
                "forward_return_15m": forward_return(series, s.timestamp, timedelta(minutes=15)),
                "forward_return_1h": forward_return(series, s.timestamp, timedelta(hours=1)),
                "trust_level": s.trust_level,
            }
            # Point-in-time features (prefixed, kept separate from labels).
            for k, v in s.features.items():
                row[f"feat_{k}"] = v
            rows.append(row)
        df = pd.DataFrame(rows)
        return df

    # ── labels ──────────────────────────────────────────────────────────────────
    def add_labels(self, df: pd.DataFrame, *, threshold: float = 0.0) -> pd.DataFrame:
        df = df.copy()
        if df.empty or "net_pnl" not in df.columns:
            return df
        df["quality_label"] = (df["net_pnl"].astype(float) > threshold).astype(int)
        return df

    # ── chronological split ───────────────────────────────────────────────────────
    def chronological_split(
        self, df: pd.DataFrame, *, train_frac: float, val_frac: float, embargo_minutes: int
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        if df.empty:
            return df, df, df
        ordered = df.sort_values("timestamp").reset_index(drop=True)
        n = len(ordered)
        n_train = int(n * train_frac)
        n_val = int(n * val_frac)
        train = ordered.iloc[:n_train]
        val = ordered.iloc[n_train:n_train + n_val]
        test = ordered.iloc[n_train + n_val:]
        embargo = timedelta(minutes=embargo_minutes)
        # Drop rows that fall inside the embargo gap after the previous split's end.
        if len(train) and len(val):
            val = val[val["timestamp"] >= train["timestamp"].iloc[-1] + embargo]
        if len(val) and len(test):
            test = test[test["timestamp"] >= val["timestamp"].iloc[-1] + embargo]
        elif len(train) and len(test) and not len(val):
            test = test[test["timestamp"] >= train["timestamp"].iloc[-1] + embargo]
        return train.reset_index(drop=True), val.reset_index(drop=True), test.reset_index(drop=True)

    # ── full build ────────────────────────────────────────────────────────────────
    def build_dataset(
        self, signals: list[SignalRecord], price_lookup: dict[str, pd.Series], meta: DatasetMeta
    ) -> DatasetResult:
        trust = self._resolve_trust(signals)
        df = self.build_rows(signals, price_lookup)
        df = self.add_labels(df, threshold=meta.quality_threshold)

        feature_cols = sorted(c for c in df.columns if c.startswith("feat_"))
        label_cols = sorted(c for c in df.columns if c in LABEL_FIELDS)
        # Sanity: features and labels are disjoint.
        assert not (set(feature_cols) & set(label_cols))

        train, val, test = self.chronological_split(
            df, train_frac=meta.train_frac, val_frac=meta.val_frac, embargo_minutes=meta.embargo_minutes
        )
        result = DatasetResult(
            meta=meta, train=train, val=val, test=test,
            feature_columns=feature_cols, label_columns=label_cols, trust_level=trust,
        )
        result.schema = self._schema(df, feature_cols, label_cols)
        result.manifest = self._manifest(meta, result, df)
        return result

    def _resolve_trust(self, signals: list[SignalRecord]) -> str:
        levels = {s.trust_level for s in signals}
        if TrustLevel.LOW.value in levels and not self._allow_quarantined:
            raise TrustError(
                "LOW-trust (quarantined) source rejected for a training dataset. "
                "Validate + license-review and pass allow_quarantined=True to override "
                "(dataset will be marked non-authoritative)."
            )
        return TrustLevel.LOW.value if TrustLevel.LOW.value in levels else TrustLevel.HIGH.value

    def _schema(self, df: pd.DataFrame, feature_cols: list[str], label_cols: list[str]) -> dict:
        roles = {}
        for c in df.columns:
            if c in IDENTITY_FIELDS:
                role = "identity"
            elif c in feature_cols:
                role = "feature"
            elif c in label_cols:
                role = "label"
            elif c in TRADE_FIELDS:
                role = "trade"
            else:
                role = "meta"
            roles[c] = {"dtype": str(df[c].dtype), "role": role}
        return {"columns": roles, "feature_columns": feature_cols, "label_columns": label_cols}

    def _manifest(self, meta: DatasetMeta, result: DatasetResult, df: pd.DataFrame) -> dict:
        def balance(frame: pd.DataFrame) -> dict:
            if frame.empty or "quality_label" not in frame:
                return {}
            vc = frame["quality_label"].value_counts().to_dict()
            return {str(k): int(v) for k, v in vc.items()}

        return {
            **asdict(meta),
            "source_run_ids": list(meta.source_run_ids),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "trust_level": result.trust_level,
            "authoritative": result.trust_level == TrustLevel.HIGH.value,
            "rows_total": int(len(df)),
            "rows_train": int(len(result.train)),
            "rows_val": int(len(result.val)),
            "rows_test": int(len(result.test)),
            "feature_columns": result.feature_columns,
            "label_columns": result.label_columns,
            "class_balance": {
                "train": balance(result.train),
                "val": balance(result.val),
                "test": balance(result.test),
            },
            "split_boundaries": {
                "train_end": _ts(result.train, -1),
                "val_start": _ts(result.val, 0),
                "val_end": _ts(result.val, -1),
                "test_start": _ts(result.test, 0),
            },
        }

    # ── write ───────────────────────────────────────────────────────────────────
    def write(
        self,
        result: DatasetResult,
        *,
        base: str = DEFAULT_DATASET_BASE,
        local_dir: str | None = None,
        s3_client: Any = None,
    ) -> dict[str, Any]:
        from pathlib import Path

        files: list[str] = []
        s3_keys: list[str] = []
        ds_id = result.meta.dataset_id

        artifacts: dict[str, bytes] = {
            "train.parquet": _parquet(result.train),
            "val.parquet": _parquet(result.val),
            "test.parquet": _parquet(result.test),
            "schema.json": json.dumps(result.schema, indent=2, default=str).encode(),
            "manifest.json": json.dumps(result.manifest, indent=2, default=str).encode(),
        }

        if local_dir is not None:
            ddir = Path(local_dir) / ds_id
            ddir.mkdir(parents=True, exist_ok=True)
            for name, content in artifacts.items():
                (ddir / name).write_bytes(content)
                files.append(str(ddir / name))

        if base.startswith("s3://"):
            bucket, _, prefix = base[len("s3://"):].partition("/")
            client = s3_client
            if client is None and s3_client is None and local_dir is None:
                from shared.aws.clients import get_s3_client

                client = get_s3_client()
            if client is not None:
                for name, content in artifacts.items():
                    key = f"{prefix.strip('/')}/{ds_id}/{name}"
                    client.put_object(Bucket=bucket, Key=key, Body=content)
                    s3_keys.append(key)

        return {"dataset_id": ds_id, "files": files, "s3_keys": s3_keys, "s3_base": f"{base}/{ds_id}/"}


def _ts(frame: pd.DataFrame, i: int) -> str | None:
    if frame.empty:
        return None
    return str(pd.Timestamp(frame["timestamp"].iloc[i]))


def _parquet(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    (df if not df.empty else pd.DataFrame({"_empty": []})).to_parquet(buf, index=False)
    return buf.getvalue()
