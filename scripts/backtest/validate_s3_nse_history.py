#!/usr/bin/env python3
"""Validate NSE historical OHLCV data for the QuantEmbrace backtesting lab.

Loads a dataset from a **local path** or **S3 prefix** (CSV or Parquet), runs the
full data-quality battery, classifies its **trust tier** (HIGH official/vendor vs
LOW GitHub/free → quarantine), and writes a markdown report to
``reports/data-quality/nse-10-15y-data-quality-report.md``.

Backtest-only: no broker APIs, no live trading, no order placement.

Usage:
    # Validate a real local Parquet lake partition
    python scripts/backtest/validate_s3_nse_history.py \
        --source-path data/lake/RELIANCE/1m --symbol RELIANCE --interval 1m \
        --source-name bhavcopy

    # Validate an S3 prefix (uses the shared S3 client / LocalStack)
    python scripts/backtest/validate_s3_nse_history.py \
        --source-path s3://quantembrace-backtest-data/lake/ohlcv/market=NSE/segment=EQ/symbol=RELIANCE/interval=1m/ \
        --symbol RELIANCE --interval 1m --source-name truedata

    # Self-test: generate synthetic trusted + quarantine datasets and write the report
    python scripts/backtest/validate_s3_nse_history.py --self-test
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.data_loader import load_candles  # noqa: E402
from backtesting.data_quality import QualityResult, run_quality_checks, to_markdown  # noqa: E402

DEFAULT_OUT = _REPO / "reports" / "data-quality" / "nse-10-15y-data-quality-report.md"

_SELF_TEST_NOTE = (
    "> **Harness demonstration on synthetic sample data.** No real 10–15-year NSE "
    "dataset has been ingested yet — that requires a licensed/vendor or official NSE "
    "feed (see `docs/backtesting/aws-phase0-discovery-report.md` §16 blocker and "
    "`aws-data-lake-contract.md`). This run exercises the loader + quality validator "
    "end-to-end on two synthetic sources (one HIGH-trust, one LOW-trust with injected "
    "faults) so the tooling is proven before real data lands. Re-run against real data "
    "with `--source-path` to produce the authoritative report."
)


# ── synthetic data (self-test only) ─────────────────────────────────────────────


def _clean_day(symbol: str, day: str, n: int = 375) -> list[dict]:
    """A full, valid 1m trading day starting 09:15 IST."""
    base = pd.Timestamp(f"{day} 09:15:00")
    rows = []
    px = 100.0
    for i in range(n):
        ts = base + pd.Timedelta(minutes=i)
        rows.append(
            {
                "timestamp": ts.isoformat(),
                "symbol": symbol,
                "open": round(px, 2),
                "high": round(px + 0.5, 2),
                "low": round(px - 0.5, 2),
                "close": round(px, 2),
                "volume": 1000 + i,
            }
        )
    return rows


def _dirty_day(symbol: str, day: str) -> list[dict]:
    """A short 1m day with several injected quality faults (LOW-trust demo)."""
    rows = _clean_day(symbol, day, n=60)  # only 60/375 → missing candles
    rows.append(dict(rows[0]))  # duplicate timestamp
    rows[5].update({"open": 100, "high": 95, "low": 99, "close": 101})  # invalid OHLC
    rows[7].update({"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0})  # zero price
    rows[9].update({"close": 175.0, "high": 176.0})  # ~75% outlier jump
    rows.append(  # market-hours violation (08:00 IST)
        {
            "timestamp": f"{day} 08:00:00",
            "symbol": symbol,
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 10,
        }
    )
    return rows


def _run_self_test(out_path: Path) -> list[QualityResult]:
    results: list[QualityResult] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # HIGH-trust (official) — clean Parquet.
        hi = tmp / "bhavcopy.parquet"
        pd.DataFrame(_clean_day("RELIANCE", "2020-06-01")).to_parquet(hi, index=False)
        hi_res = load_candles(str(hi), symbol="RELIANCE", interval="1m", source_name="bhavcopy")
        results.append(
            run_quality_checks(
                hi_res.df, interval="1m", source=hi_res.source, trust=hi_res.trust_level
            )
        )

        # LOW-trust (github) — dirty CSV.
        lo = tmp / "github.csv"
        pd.DataFrame(_dirty_day("TATAMOTORS", "2020-06-01")).to_csv(lo, index=False)
        lo_res = load_candles(str(lo), symbol="TATAMOTORS", interval="1m", source_name="github")
        results.append(
            run_quality_checks(
                lo_res.df, interval="1m", source=lo_res.source, trust=lo_res.trust_level
            )
        )

    md = to_markdown(
        results,
        title="NSE 10–15y Historical Data — Quality Report",
        notes=_SELF_TEST_NOTE,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    return results


# ── real validation ──────────────────────────────────────────────────────────


def _run_real(args: argparse.Namespace, out_path: Path) -> list[QualityResult]:
    res = load_candles(
        args.source_path,
        symbol=args.symbol,
        interval=args.interval,
        segment=args.segment,
        date_from=args.date_from,
        date_to=args.date_to,
        fmt=args.format,
        source_name=args.source_name,
    )
    result = run_quality_checks(
        res.df,
        interval=args.interval,
        segment=args.segment,
        source=res.source,
        trust=res.trust_level,
        symbol=args.symbol,
    )
    md = to_markdown(
        [result],
        title="NSE Historical Data — Quality Report",
        notes=f"> Source: `{args.source_path}` · files read: {len(res.files_read)}",
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    return [result]


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Validate NSE historical OHLCV for the backtesting lab")
    p.add_argument("--source-path", help="Local path or s3:// prefix to validate")
    p.add_argument("--symbol", help="Symbol (for single-symbol partitions)")
    p.add_argument("--interval", default="1m", choices=["1m", "5m", "15m", "1d"])
    p.add_argument("--segment", default="EQ", choices=["EQ", "INDEX", "FNO"])
    p.add_argument("--from", dest="date_from", help="Start date YYYY-MM-DD (inclusive)")
    p.add_argument("--to", dest="date_to", help="End date YYYY-MM-DD (inclusive)")
    p.add_argument("--format", default="auto", choices=["auto", "csv", "parquet"])
    p.add_argument("--source-name", help="Provenance label (e.g. bhavcopy, truedata, github)")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="Output markdown report path")
    p.add_argument("--self-test", action="store_true", help="Generate synthetic data and report")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    out_path = Path(args.out)

    if args.self_test or not args.source_path:
        if not args.self_test:
            print("No --source-path given; running --self-test (synthetic demonstration).")
        results = _run_self_test(out_path)
    else:
        results = _run_real(args, out_path)

    print(f"\nWrote data-quality report → {out_path}")
    for r in results:
        verdict = "PASS" if r.passed else "FAIL"
        elig = "eligible" if r.eligible_for_use else "NOT eligible"
        print(
            f"  [{verdict}] {r.source} {r.symbol} [{r.interval}] "
            f"trust={r.trust_level.value} ({elig}) "
            f"errors={len(r.errors)} warnings={len(r.warnings)} rows={r.rows}"
        )
    # Discovery/validation phase: report only. Never fail the process on data issues.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
