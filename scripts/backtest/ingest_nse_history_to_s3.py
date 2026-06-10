#!/usr/bin/env python3
"""Ingest licensed NSE historical data into the S3 raw zone (unchanged) + manifest.

Copies vendor files **byte-for-byte** into the correct zone — HIGH-trust →
``raw/``, LOW-trust (GitHub/free) → ``quarantine/`` — and writes a sha256 checksum
manifest plus source/vendor/license/data_version metadata. Trusted and quarantine
data are never mixed.

Backtest-only: no broker APIs, no live trading. Normalization to Parquet is a
separate step (`normalize_nse_history.py`). Raw files are preserved unchanged.

Usage:
    python scripts/backtest/ingest_nse_history_to_s3.py \
        --delivery-path /deliveries/truedata_2010_2024 \
        --vendor TrueData --license "TrueData NSE EOD+Intraday 2026" \
        --data-version td-nse-2010-2024-v1 --source-name truedata \
        --segment EQ --timeframe 1d --base s3://quantembrace-backtest-data

    python scripts/backtest/ingest_nse_history_to_s3.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.ingestion import Ingestor, SourceMeta  # noqa: E402
from backtesting.s3_data_catalog import DataCatalog  # noqa: E402


def _read_delivery(path: str) -> list[tuple[str, bytes]]:
    p = Path(path)
    files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file())
    return [(f.name, f.read_bytes()) for f in files]


def _sample_csv() -> bytes:
    rows = ["timestamp,open,high,low,close,volume"]
    px = 100.0
    for i in range(30):
        d = f"2020-01-{(i % 28) + 1:02d}"
        rows.append(f"{d},{px:.2f},{px + 1:.2f},{px - 1:.2f},{px + 0.3:.2f},{100000 + i}")
        px += 0.5
    return ("\n".join(rows) + "\n").encode()


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest NSE history into S3 raw zone (backtest-only)")
    p.add_argument("--delivery-path")
    p.add_argument("--vendor", default="UNSPECIFIED")
    p.add_argument("--license", default="UNSPECIFIED")
    p.add_argument("--data-version", default="UNSPECIFIED")
    p.add_argument("--source-name", default="unknown", help="bhavcopy|truedata|globaldatafeeds|github|...")
    p.add_argument("--segment", default="EQ", choices=["EQ", "INDEX", "FNO"])
    p.add_argument("--timeframe", default="1d", choices=["1m", "5m", "15m", "1d"])
    p.add_argument("--market", default="NSE")
    p.add_argument("--base", default="s3://quantembrace-backtest-data")
    p.add_argument("--ingest-date")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test or not args.delivery_path:
        if not args.self_test:
            print("No --delivery-path; running --self-test (sample data).")
        base = str(_REPO / "reports" / "ingest-selftest" / "data")
        cat = DataCatalog(data_base=base)
        ing = Ingestor(cat)
        hi = ing.ingest([("RELIANCE_2020_1d.csv", _sample_csv())],
                        SourceMeta("NSE-Bhavcopy", "Official NSE archives (free)", "bhavcopy-2020-v1",
                                   "bhavcopy", "EQ", "1d"), ingest_date="2026-06-06")
        lo = ing.ingest([("scraped_reliance.csv", _sample_csv())],
                        SourceMeta("GitHubScrape", "unknown/none", "github-2020-v0",
                                   "github", "EQ", "1d"), ingest_date="2026-06-06")
        print(f"\n  HIGH-trust → zone={hi['zone']} prefix={hi['prefix']}")
        print(f"    files: {[f['name'] for f in hi['files']]} checksum0={hi['files'][0]['sha256'][:12]}…")
        print(f"  LOW-trust  → zone={lo['zone']} prefix={lo['prefix']} (quarantine — not promotable)")
        print("  raw and quarantine zones are separate; trusted data is never mixed with quarantine.\n")
        return 0

    files = _read_delivery(args.delivery_path)
    meta = SourceMeta(args.vendor, args.license, args.data_version, args.source_name,
                      args.segment, args.timeframe, args.market)
    cat = DataCatalog(data_base=args.base)
    manifest = Ingestor(cat).ingest(files, meta, ingest_date=args.ingest_date)
    print(f"Ingested {len(manifest['files'])} file(s) → zone={manifest['zone']} prefix={manifest['prefix']}")
    print(f"  vendor={manifest['vendor']} license={manifest['license']} "
          f"data_version={manifest['data_version']} trust={manifest['trust_level']}")
    print(f"  manifest: {manifest['prefix']}/_ingest_manifest.json · checksums: {manifest['prefix']}/checksums.sha256")
    if manifest["zone"] == "quarantine":
        print("  NOTE: LOW-trust → quarantine. Not eligible for the lake / model training without review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
