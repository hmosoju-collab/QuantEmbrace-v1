"""Historical OHLCV loader for the QuantEmbrace backtesting lab.

Loads candles from a **local path** or **S3 prefix**, in **CSV** or **Parquet**,
and normalises them to the canonical schema (IST tz-aware), tagging each row with
its source and trust level.

Supports:
    * local file or directory, and ``s3://bucket/prefix`` (partitioned reads)
    * CSV and Parquet (auto-detected by extension, or forced via ``fmt``)
    * intervals 1m / 5m / 15m / 1d
    * IST (Asia/Kolkata) timezone normalisation
    * multi-symbol loading and inclusive date-range filtering

Backtest-only: no broker APIs. S3 access (when used) goes through the sanctioned
``shared.aws.clients.get_s3_client`` factory or an injected client — never a raw
``boto3.client`` (TID251 banned-api).
"""

from __future__ import annotations

import glob
import io
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from backtesting.s3_data_catalog import (
    TrustLevel,
    classify_source_trust,
    is_quarantined,
    is_s3_uri,
    split_s3_uri,
)

IST = "Asia/Kolkata"

# Canonical output columns (in order).
CANONICAL_COLUMNS: list[str] = [
    "timestamp",
    "symbol",
    "isin",
    "market",
    "segment",
    "interval",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "trust_level",
]

# Candidate header names for the timestamp column (lower-cased).
_TS_ALIASES: tuple[str, ...] = ("timestamp", "datetime", "date_time", "date", "time", "ts")

# Header aliases → canonical OHLCV names (lower-cased).
_COL_ALIASES: dict[str, str] = {
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
    "vol": "volume",
    "qty": "volume",
    "quantity": "volume",
    "open_price": "open",
    "high_price": "high",
    "low_price": "low",
    "close_price": "close",
    "ltp": "close",
}

_REQUIRED_OHLC = ("open", "high", "low", "close")


@dataclass
class LoadResult:
    """Outcome of a load: the normalised frame plus provenance metadata."""

    df: pd.DataFrame
    source: str
    trust_level: TrustLevel
    quarantined: bool
    files_read: list[str] = field(default_factory=list)


# ── public API ────────────────────────────────────────────────────────────────


def load_candles(
    source_path: str,
    *,
    symbol: str | None = None,
    interval: str | None = None,
    segment: str = "EQ",
    market: str = "NSE",
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    fmt: str = "auto",
    source_name: str | None = None,
    assume_tz: str = IST,
    localize_naive: bool = True,
    s3_client: Any = None,
) -> LoadResult:
    """Load and normalise candles from ``source_path``.

    Args:
        source_path: local file/dir or ``s3://bucket/prefix``.
        symbol/interval/segment/market: applied to rows missing those columns,
            and ``symbol`` additionally filters multi-symbol frames.
        date_from/date_to: inclusive IST date-range filter (``date`` or ISO str).
        fmt: ``auto`` | ``csv`` | ``parquet``.
        source_name: provenance label used to classify trust (e.g. ``bhavcopy``,
            ``github``). Unknown/empty → LOW trust (quarantine).
        assume_tz/localize_naive: how naive timestamps are handled.
        s3_client: optional injected S3 client (for tests / LocalStack). When
            ``None`` and an s3 URI is given, the shared factory is used lazily.

    Returns:
        LoadResult with a frame whose columns are ``CANONICAL_COLUMNS``.
    """
    trust = classify_source_trust(source_name)
    frames: list[pd.DataFrame] = []
    files: list[str] = []

    if is_s3_uri(source_path):
        for key, body in _iter_s3_objects(source_path, s3_client, fmt):
            frames.append(_read_bytes(body, key, fmt))
            files.append(key)
    else:
        for path in _iter_local_files(source_path, fmt):
            frames.append(_read_file(path, fmt))
            files.append(path)

    if frames:
        raw = pd.concat(frames, ignore_index=True)
    else:
        raw = pd.DataFrame()

    df = _normalize(
        raw,
        symbol=symbol,
        interval=interval,
        segment=segment,
        market=market,
        source_name=source_name or "unknown",
        trust=trust,
        assume_tz=assume_tz,
        localize_naive=localize_naive,
    )
    df = _apply_filters(df, symbol=symbol, date_from=_as_date(date_from), date_to=_as_date(date_to))
    if not df.empty:
        df = df.sort_values("timestamp").reset_index(drop=True)

    return LoadResult(
        df=df,
        source=source_name or "unknown",
        trust_level=trust,
        quarantined=is_quarantined(trust),
        files_read=files,
    )


# ── file discovery / reading ────────────────────────────────────────────────────


def _exts_for(fmt: str) -> tuple[str, ...]:
    if fmt == "csv":
        return (".csv",)
    if fmt == "parquet":
        return (".parquet", ".pq")
    return (".csv", ".parquet", ".pq")


def _iter_local_files(path: str, fmt: str):
    exts = _exts_for(fmt)
    if os.path.isdir(path):
        out: list[str] = []
        for ext in exts:
            out.extend(glob.glob(os.path.join(path, "**", f"*{ext}"), recursive=True))
        yield from sorted(out)
    else:
        yield path


def _read_file(path: str, fmt: str) -> pd.DataFrame:
    if fmt == "parquet" or path.endswith((".parquet", ".pq")):
        return pd.read_parquet(path)
    if fmt == "csv" or path.endswith(".csv"):
        return pd.read_csv(path)
    # Fallback: try parquet then csv.
    try:
        return pd.read_parquet(path)
    except Exception:
        return pd.read_csv(path)


def _read_bytes(body: bytes, key: str, fmt: str) -> pd.DataFrame:
    buf = io.BytesIO(body)
    if fmt == "parquet" or key.endswith((".parquet", ".pq")):
        return pd.read_parquet(buf)
    return pd.read_csv(buf)


def _iter_s3_objects(uri: str, s3_client: Any, fmt: str):
    """Yield ``(key, body_bytes)`` for matching objects under an s3 prefix."""
    if s3_client is None:
        # Lazy import keeps boto3 out of the import path and out of tests.
        from shared.aws.clients import get_s3_client

        s3_client = get_s3_client()

    bucket, prefix = split_s3_uri(uri)
    exts = _exts_for(fmt)
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3_client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []) or []:
            key = obj["Key"]
            if key.endswith(exts):
                body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
                yield key, body
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break


# ── normalisation ───────────────────────────────────────────────────────────────


def _normalize(
    raw: pd.DataFrame,
    *,
    symbol: str | None,
    interval: str | None,
    segment: str,
    market: str,
    source_name: str,
    trust: TrustLevel,
    assume_tz: str,
    localize_naive: bool,
) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    df = raw.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={k: v for k, v in _COL_ALIASES.items() if k in df.columns})

    # Timestamp column.
    ts_col = next((c for c in _TS_ALIASES if c in df.columns), None)
    if ts_col is None:
        raise ValueError(
            f"No timestamp column found. Looked for {_TS_ALIASES}; got {list(df.columns)}"
        )
    if ts_col != "timestamp":
        df = df.rename(columns={ts_col: "timestamp"})

    df["timestamp"] = _to_ist(df["timestamp"], assume_tz=assume_tz, localize_naive=localize_naive)

    # Numeric coercion.
    for col in (*_REQUIRED_OHLC, "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    missing = [c for c in _REQUIRED_OHLC if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required OHLC column(s): {missing}; got {list(df.columns)}")
    if "volume" not in df.columns:
        df["volume"] = 0

    # Metadata columns (fill if absent).
    if "symbol" not in df.columns:
        df["symbol"] = symbol if symbol is not None else "UNKNOWN"
    if "isin" not in df.columns:
        df["isin"] = pd.NA
    df["market"] = df["market"] if "market" in df.columns else market
    df["segment"] = df["segment"] if "segment" in df.columns else segment
    if "interval" not in df.columns:
        df["interval"] = interval if interval is not None else "1d"
    df["source"] = source_name
    df["trust_level"] = trust.value

    # Order canonical columns first; preserve any extras after.
    extras = [c for c in df.columns if c not in CANONICAL_COLUMNS]
    return df[[*CANONICAL_COLUMNS, *extras]]


def _to_ist(series: pd.Series, *, assume_tz: str, localize_naive: bool) -> pd.Series:
    """Parse to datetime and convert to IST. Naive inputs are localised to
    ``assume_tz`` (when ``localize_naive``), else left naive for QC to flag.

    Real NSE CSVs mix timestamp formats (``YYYY-MM-DD HH:MM:SS`` vs ISO ``T`` vs
    ``DD-MON-YYYY``); ``format="mixed"`` parses each element independently so a
    minority format is not silently coerced to NaT.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        ts = series
    else:
        try:
            ts = pd.to_datetime(series, errors="coerce", utc=False, format="mixed")
        except (ValueError, TypeError):
            ts = pd.to_datetime(series, errors="coerce", utc=False)
    tz = getattr(ts.dt, "tz", None)
    if tz is None:
        if localize_naive:
            return ts.dt.tz_localize(assume_tz)
        return ts
    return ts.dt.tz_convert(IST)


# ── filtering ────────────────────────────────────────────────────────────────────


def _apply_filters(
    df: pd.DataFrame,
    *,
    symbol: str | None,
    date_from: date | None,
    date_to: date | None,
) -> pd.DataFrame:
    if df.empty:
        return df
    if symbol is not None and "symbol" in df.columns:
        # Filter only when the frame actually carries multiple/ other symbols.
        present = set(df["symbol"].dropna().unique())
        if present - {symbol}:
            df = df[df["symbol"] == symbol]
    if (date_from or date_to) and df["timestamp"].notna().any():
        tzaware = getattr(df["timestamp"].dt, "tz", None) is not None
        local_dates = (df["timestamp"].dt.tz_convert(IST) if tzaware else df["timestamp"]).dt.date
        if date_from:
            df = df[local_dates >= date_from]
            local_dates = local_dates[df.index]
        if date_to:
            df = df[local_dates <= date_to]
    return df


def _as_date(value: date | str | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()
