#!/usr/bin/env python3
"""
QuantEmbrace — Lake → LEAN custom-data exporter (ADR-041 Phase 2b)

Exports symbols from the curated US lake (Phase 1, snapshot ds-b9b110ac58d57cae)
as LEAN custom-data CSVs so the SAME total-return series feeds both engines:

    backtest-data/lean-workspace/data/us_eod_tr/{sym}.csv
    rows: YYYYMMDD,open,high,low,close,volume

close = adj_close (total-return series). open/high/low are scaled by the same
adjustment factor (adj_close/close) so bars stay internally consistent.

Usage:
    python scripts/backtest/export_lake_to_lean.py --symbols SPY,TLT,GLD
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_LAKE = _REPO / "backtest-data" / "lake"
_OUT = _REPO / "backtest-data" / "lean-workspace" / "data" / "us_eod_tr"


def export_symbol(sym: str, lake: Path, out_dir: Path) -> int:
    files = sorted(lake.glob(
        f"ohlcv/market=US/segment=EQ/symbol={sym}/interval=1d/year=*/part-*.parquet"))
    if not files:
        raise FileNotFoundError(f"no lake data for {sym}")
    df = pd.concat([pd.read_parquet(f) for f in files]).sort_values("timestamp")
    factor = df["adj_close"] / df["close"]
    out = pd.DataFrame({
        "date": df["timestamp"].dt.tz_convert("America/New_York").dt.strftime("%Y%m%d"),
        "open": (df["open"] * factor).round(6),
        "high": (df["high"] * factor).round(6),
        "low": (df["low"] * factor).round(6),
        "close": df["adj_close"].round(6),
        "volume": df["volume"],
    })
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{sym.lower()}.csv"
    out.to_csv(dest, index=False, header=False)
    return len(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="SPY,TLT,GLD")
    p.add_argument("--lake", default=str(_LAKE))
    p.add_argument("--out", default=str(_OUT))
    args = p.parse_args()
    for sym in args.symbols.split(","):
        n = export_symbol(sym.strip(), Path(args.lake), Path(args.out))
        print(f"  {sym}: {n} rows → {Path(args.out) / (sym.lower() + '.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
