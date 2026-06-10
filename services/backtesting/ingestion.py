"""Historical-data ingestion for the QuantEmbrace backtesting lab.

Two stages:
  * ``Ingestor``  — copy raw vendor files **unchanged** into the S3 raw zone (or
    the quarantine zone for LOW-trust sources), with a per-file **sha256 checksum
    manifest** and source/vendor/license/data_version metadata.
  * ``Normalizer``— read raw files, normalize to the **canonical Parquet** lake,
    run the data-quality battery, and emit a data-quality report. **Quarantined
    (LOW-trust) data is never promoted to the trusted lake.**

Trusted and quarantine data are kept in separate zones and never mixed. No broker
APIs, no live trading. S3 writes use an injected client or `get_s3_client`.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from backtesting.data_loader import IST, load_candles
from backtesting.data_quality import run_quality_checks, to_markdown
from backtesting.s3_data_catalog import (
    DataCatalog,
    TrustLevel,
    Zone,
    classify_source_trust,
    is_quarantined,
)


class QuarantineError(Exception):
    """Raised when LOW-trust data is asked to enter the trusted lake."""


@dataclass
class SourceMeta:
    vendor: str
    license: str
    data_version: str
    source_name: str            # provenance key → trust tier (e.g. bhavcopy, truedata, github)
    segment: str = "EQ"         # EQ | INDEX | FNO
    timeframe: str = "1d"       # 1m | 5m | 15m | 1d
    market: str = "NSE"

    @property
    def trust_level(self) -> str:
        return classify_source_trust(self.source_name).value


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_s3(uri: str) -> bool:
    return uri.startswith("s3://")


def _parquet_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


class _Writer:
    """Writes bytes to a local path or an s3:// URI (injected/sanctioned client)."""

    def __init__(self, s3_client: Any = None) -> None:
        self._s3 = s3_client

    def write(self, uri: str, content: bytes) -> None:
        if _is_s3(uri):
            bucket, _, key = uri[len("s3://"):].partition("/")
            client = self._s3
            if client is None:
                from shared.aws.clients import get_s3_client

                client = get_s3_client()
            client.put_object(Bucket=bucket, Key=key, Body=content)
        else:
            p = Path(uri)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)


# ── ingestion (raw zone, unchanged) ─────────────────────────────────────────────


class Ingestor:
    def __init__(self, catalog: DataCatalog, *, s3_client: Any = None) -> None:
        self._catalog = catalog
        self._writer = _Writer(s3_client)

    def ingest(self, files: list[tuple[str, bytes]], meta: SourceMeta, *,
               ingest_date: date | str | None = None) -> dict:
        """Copy ``files`` (name, bytes) unchanged into the correct zone; manifest it.

        HIGH-trust → ``raw/`` (eligible for normalization to the lake).
        LOW-trust  → ``quarantine/`` (NEVER promoted without explicit approval).
        """
        d = (ingest_date or date.today())
        trust = classify_source_trust(meta.source_name)
        if is_quarantined(trust):
            prefix = self._catalog.quarantine_prefix(source=meta.source_name, ingest_date=d)
            zone = Zone.QUARANTINE.value
        else:
            prefix = self._catalog.raw_prefix(source=meta.source_name, ingest_date=d)
            zone = Zone.RAW.value

        file_records = []
        checksum_lines = []
        for name, content in files:
            digest = sha256_bytes(content)
            dest = f"{prefix}/{name}"
            self._writer.write(dest, content)  # bytes preserved unchanged
            file_records.append({"name": name, "sha256": digest, "bytes": len(content), "path": dest})
            checksum_lines.append(f"{digest}  {name}")

        manifest = {
            "vendor": meta.vendor,
            "license": meta.license,
            "data_version": meta.data_version,
            "source": meta.source_name,
            "trust_level": trust.value,
            "zone": zone,
            "segment": meta.segment,
            "timeframe": meta.timeframe,
            "market": meta.market,
            "ingest_date": str(d.isoformat() if isinstance(d, date) else d),
            "checksum_algo": "sha256",
            "files": file_records,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._writer.write(f"{prefix}/_ingest_manifest.json", json.dumps(manifest, indent=2).encode())
        self._writer.write(f"{prefix}/checksums.sha256", ("\n".join(checksum_lines) + "\n").encode())
        manifest["prefix"] = prefix
        return manifest


# ── normalization (raw → curated Parquet lake) ──────────────────────────────────


class Normalizer:
    def __init__(self, catalog: DataCatalog, *, s3_client: Any = None) -> None:
        self._catalog = catalog
        self._writer = _Writer(s3_client)
        self._s3 = s3_client

    def normalize_file(self, raw_path: str, meta: SourceMeta, *, symbol: str,
                       interval: str | None = None, allow_quarantine: bool = False) -> dict:
        """Normalize one raw file to the lake (HIGH trust only) + quality report."""
        trust = classify_source_trust(meta.source_name)
        if is_quarantined(trust) and not allow_quarantine:
            raise QuarantineError(
                f"Source {meta.source_name!r} is {trust.value} trust — cannot promote to the "
                "trusted lake. It stays in quarantine until quality + license review."
            )
        iv = interval or meta.timeframe
        res = load_candles(raw_path, symbol=symbol, interval=iv, segment=meta.segment,
                           market=meta.market, source_name=meta.source_name, s3_client=self._s3)
        df = res.df
        quality = run_quality_checks(df, interval=iv, segment=meta.segment,
                                     source=meta.source_name, trust=res.trust_level, symbol=symbol)

        written: list[str] = []
        if not df.empty:
            ist = df["timestamp"].dt.tz_convert(IST)
            for year, part in df.assign(_year=ist.dt.year).groupby("_year"):
                part = part.drop(columns=["_year"])
                lake = self._catalog.lake_partition(symbol=symbol, interval=iv, year=int(year),
                                                    segment=meta.segment, market=meta.market)
                dest = f"{lake}/part-0.parquet"
                self._writer.write(dest, _parquet_bytes(part))
                written.append(dest)

        report_md = to_markdown(
            [quality],
            title=f"Data Quality — {meta.vendor} {symbol} [{iv}] ({meta.data_version})",
            notes=f"> Source: `{meta.source_name}` (trust {trust.value}) · license: {meta.license} · "
                  f"raw: `{raw_path}`",
        )
        return {
            "symbol": symbol, "interval": iv, "rows": int(len(df)),
            "trust_level": trust.value, "eligible_for_use": quality.eligible_for_use,
            "passed": quality.passed, "processed_paths": written, "report_md": report_md,
            "data_version": meta.data_version,
        }
