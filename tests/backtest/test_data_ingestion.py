"""Unit tests for historical-data ingestion (PHASE DATA-INGEST).

Covers: raw→processed conversion · checksum generated · source metadata persisted ·
S3 path layout correct · quarantine source blocked from model training.

Backtest-only: local temp base + an in-memory fake S3 — no AWS, no broker.

Run:  python -m pytest tests/backtest/test_data_ingestion.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.ingestion import (  # noqa: E402
    Ingestor,
    Normalizer,
    QuarantineError,
    SourceMeta,
    sha256_bytes,
)
from backtesting.s3_data_catalog import DataCatalog, TrustLevel, classify_source_trust  # noqa: E402


def _sample_csv() -> bytes:
    rows = ["timestamp,open,high,low,close,volume"]
    px = 100.0
    for i in range(30):
        d = f"2020-01-{(i % 28) + 1:02d}"
        rows.append(f"{d},{px:.2f},{px + 1:.2f},{px - 1:.2f},{px + 0.3:.2f},{100000 + i}")
        px += 0.5
    return ("\n".join(rows) + "\n").encode()


def _hi_meta():
    return SourceMeta("NSE-Bhavcopy", "Official NSE archives (free)", "bhavcopy-2020-v1",
                      "bhavcopy", "EQ", "1d")


def _lo_meta():
    return SourceMeta("GitHubScrape", "unknown/none", "github-2020-v0", "github", "EQ", "1d")


class FakeS3:
    def __init__(self):
        self.keys = []

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.keys.append(Key)
        return {}


# ── conversion ───────────────────────────────────────────────────────────────


def test_raw_to_processed_conversion(tmp_path):
    cat = DataCatalog(data_base=str(tmp_path / "data"))
    content = _sample_csv()
    manifest = Ingestor(cat).ingest([("RELIANCE_2020_1d.csv", content)], _hi_meta(), ingest_date="2026-06-06")

    raw_path = manifest["files"][0]["path"]
    assert Path(raw_path).read_bytes() == content  # raw preserved byte-for-byte

    out = Normalizer(cat).normalize_file(raw_path, _hi_meta(), symbol="RELIANCE", interval="1d")
    assert out["rows"] > 0 and out["processed_paths"]
    df = pd.read_parquet(out["processed_paths"][0])
    for col in ("timestamp", "symbol", "open", "high", "low", "close", "volume"):
        assert col in df.columns
    assert str(df["timestamp"].dt.tz) == "Asia/Kolkata"


# ── checksum ─────────────────────────────────────────────────────────────────


def test_checksum_generated(tmp_path):
    cat = DataCatalog(data_base=str(tmp_path / "data"))
    content = _sample_csv()
    manifest = Ingestor(cat).ingest([("f.csv", content)], _hi_meta(), ingest_date="2026-06-06")
    assert manifest["checksum_algo"] == "sha256"
    assert manifest["files"][0]["sha256"] == sha256_bytes(content)
    checks = Path(manifest["prefix"]) / "checksums.sha256"
    assert checks.exists() and sha256_bytes(content) in checks.read_text()


# ── source metadata ──────────────────────────────────────────────────────────


def test_source_metadata_persisted(tmp_path):
    cat = DataCatalog(data_base=str(tmp_path / "data"))
    manifest = Ingestor(cat).ingest([("f.csv", _sample_csv())], _hi_meta(), ingest_date="2026-06-06")
    on_disk = json.loads((Path(manifest["prefix"]) / "_ingest_manifest.json").read_text())
    for k in ("vendor", "license", "data_version", "source", "trust_level"):
        assert k in on_disk
    assert on_disk["vendor"] == "NSE-Bhavcopy"
    assert on_disk["license"].startswith("Official NSE")
    assert on_disk["data_version"] == "bhavcopy-2020-v1"
    assert on_disk["trust_level"] == "HIGH"


# ── path layout ──────────────────────────────────────────────────────────────


def test_s3_path_layout_correct(tmp_path):
    cat = DataCatalog(data_base=str(tmp_path / "data"))
    hi = Ingestor(cat).ingest([("f.csv", _sample_csv())], _hi_meta(), ingest_date="2026-06-06")
    lo = Ingestor(cat).ingest([("g.csv", _sample_csv())], _lo_meta(), ingest_date="2026-06-06")
    assert "/raw/bhavcopy/2026-06-06" in hi["prefix"]
    assert "/quarantine/github/2026-06-06" in lo["prefix"]
    lake = cat.lake_partition(symbol="RELIANCE", interval="1d", year=2020, segment="EQ", market="NSE")
    assert "lake/ohlcv/market=NSE/segment=EQ/symbol=RELIANCE/interval=1d/year=2020" in lake

    # S3 base routes through the (stubbed) client with the same layout.
    s3 = FakeS3()
    cat_s3 = DataCatalog(data_base="s3://quantembrace-backtest-data")
    Ingestor(cat_s3, s3_client=s3).ingest([("f.csv", _sample_csv())], _hi_meta(), ingest_date="2026-06-06")
    assert any(k.startswith("raw/bhavcopy/2026-06-06/") for k in s3.keys)
    assert any(k.endswith("_ingest_manifest.json") for k in s3.keys)


# ── quarantine ───────────────────────────────────────────────────────────────


def test_quarantine_source_blocked_from_model_training(tmp_path):
    assert classify_source_trust("github") is TrustLevel.LOW
    cat = DataCatalog(data_base=str(tmp_path / "data"))
    lo = Ingestor(cat).ingest([("g.csv", _sample_csv())], _lo_meta(), ingest_date="2026-06-06")
    assert lo["zone"] == "quarantine"
    assert "/quarantine/" in lo["prefix"] and "/lake/" not in lo["prefix"]

    raw_path = lo["files"][0]["path"]
    norm = Normalizer(cat)
    # Promotion to the trusted lake is refused.
    with pytest.raises(QuarantineError):
        norm.normalize_file(raw_path, _lo_meta(), symbol="X", interval="1d")
    # No lake artifacts were created for the quarantined source.
    lake_root = tmp_path / "data" / "lake"
    assert not lake_root.exists() or not any(lake_root.rglob("*.parquet"))

    # Explicit override still flags it non-authoritative (eligible_for_use False).
    out = norm.normalize_file(raw_path, _lo_meta(), symbol="X", interval="1d", allow_quarantine=True)
    assert out["trust_level"] == "LOW" and out["eligible_for_use"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
