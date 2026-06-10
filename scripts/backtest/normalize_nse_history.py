#!/usr/bin/env python3
"""Normalize ingested NSE raw files into the curated Parquet lake + quality report.

Reads raw files (CSV/Parquet) from the raw zone, normalizes to the canonical
schema (IST tz-aware), runs the data-quality battery, and writes processed
**Parquet** partitioned by ``market/segment/symbol/interval/year`` into the lake.
**LOW-trust (quarantined) data is never promoted** unless `--allow-quarantine` is
passed after review (and remains flagged non-authoritative).

Backtest-only: no broker APIs, no live trading. Raw files are not modified.

Usage:
    python scripts/backtest/normalize_nse_history.py \
        --raw-path s3://quantembrace-backtest-data/raw/truedata/2026-06-06/RELIANCE_1d.csv \
        --vendor TrueData --license "..." --data-version td-...-v1 --source-name truedata \
        --symbol RELIANCE --interval 1d --segment EQ --base s3://quantembrace-backtest-data

    python scripts/backtest/normalize_nse_history.py --self-test
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.ingestion import Ingestor, Normalizer, QuarantineError, SourceMeta  # noqa: E402
from backtesting.s3_data_catalog import DataCatalog  # noqa: E402


def _sample_csv() -> bytes:
    rows = ["timestamp,open,high,low,close,volume"]
    px = 100.0
    for i in range(60):
        d = f"2020-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
        rows.append(f"{d},{px:.2f},{px + 1:.2f},{px - 1:.2f},{px + 0.3:.2f},{100000 + i}")
        px += 0.4
    return ("\n".join(rows) + "\n").encode()


def main() -> int:
    p = argparse.ArgumentParser(description="Normalize NSE raw → Parquet lake (backtest-only)")
    p.add_argument("--raw-path")
    p.add_argument("--vendor", default="UNSPECIFIED")
    p.add_argument("--license", default="UNSPECIFIED")
    p.add_argument("--data-version", default="UNSPECIFIED")
    p.add_argument("--source-name", default="unknown")
    p.add_argument("--symbol", default="RELIANCE")
    p.add_argument("--interval", default="1d", choices=["1m", "5m", "15m", "1d"])
    p.add_argument("--segment", default="EQ", choices=["EQ", "INDEX", "FNO"])
    p.add_argument("--market", default="NSE")
    p.add_argument("--base", default="s3://quantembrace-backtest-data")
    p.add_argument("--allow-quarantine", action="store_true")
    p.add_argument("--out-report", default=str(_REPO / "reports" / "data-quality" / "ingest-normalize-quality.md"))
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test or not args.raw_path:
        if not args.self_test:
            print("No --raw-path; running --self-test (sample data).")
        with tempfile.TemporaryDirectory() as td:
            base = str(Path(td) / "data")
            cat = DataCatalog(data_base=base)
            ing = Ingestor(cat)
            hi = ing.ingest([("RELIANCE_2020_1d.csv", _sample_csv())],
                            SourceMeta("NSE-Bhavcopy", "Official NSE (free)", "bhavcopy-2020-v1",
                                       "bhavcopy", "EQ", "1d"), ingest_date="2026-06-06")
            raw_file = hi["files"][0]["path"]
            norm = Normalizer(cat)
            out = norm.normalize_file(raw_file, SourceMeta("NSE-Bhavcopy", "Official NSE (free)",
                                      "bhavcopy-2020-v1", "bhavcopy", "EQ", "1d"),
                                      symbol="RELIANCE", interval="1d")
            print(f"\n  HIGH-trust normalized: rows={out['rows']} eligible={out['eligible_for_use']} "
                  f"parquet={len(out['processed_paths'])} partition(s)")
            for pth in out["processed_paths"]:
                print(f"    → {pth}")
            # Quarantine block demo.
            lo = ing.ingest([("scraped.csv", _sample_csv())],
                            SourceMeta("GitHubScrape", "none", "github-v0", "github", "EQ", "1d"),
                            ingest_date="2026-06-06")
            try:
                norm.normalize_file(lo["files"][0]["path"], SourceMeta("GitHubScrape", "none",
                                    "github-v0", "github", "EQ", "1d"), symbol="X", interval="1d")
                print("  ERROR: quarantine was promoted (should not happen)")
            except QuarantineError:
                print("  LOW-trust → blocked from the lake (QuarantineError) ✓")
            Path(args.out_report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out_report).write_text(out["report_md"])
            print(f"  data-quality report → {args.out_report}\n")
        return 0

    cat = DataCatalog(data_base=args.base)
    meta = SourceMeta(args.vendor, args.license, args.data_version, args.source_name,
                      args.segment, args.interval, args.market)
    out = Normalizer(cat).normalize_file(args.raw_path, meta, symbol=args.symbol,
                                         interval=args.interval, allow_quarantine=args.allow_quarantine)
    Path(args.out_report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_report).write_text(out["report_md"])
    print(f"Normalized {args.symbol} [{args.interval}]: rows={out['rows']} eligible={out['eligible_for_use']}")
    print(f"  processed: {len(out['processed_paths'])} partition(s); report → {args.out_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
