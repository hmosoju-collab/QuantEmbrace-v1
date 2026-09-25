#!/usr/bin/env python3
"""First authoritative NIFTY50 momentum backtest — 5 years, real NSE Bhavcopy data.

Runs the production MomentumStrategy (SMA crossover, 10/50 window) on 47 NIFTY50
constituents over 2020–2024 using the real Bhavcopy Parquet lake on S3.

Cost model:
    NSE equity delivery (CNC) — STT 0.1% both legs, stamp 0.015% buy, exchange
    0.00297%, SEBI 0.0001%, GST 18% on exchange+brokerage+SEBI.
    Brokerage: Rs 0 (Zerodha equity delivery).
    Slippage: 5 bps per leg (realistic for NIFTY50 liquid caps).

Results registered in real AWS DynamoDB (qe-bt-runs) and uploaded to S3.
Backtest-only. No broker APIs. No live trading. No capital changes.
Advisory only — cannot promote. A human reviews all results.

Usage:
    python scripts/backtest/run_nifty50_momentum_5y.py
    python scripts/backtest/run_nifty50_momentum_5y.py --local   # read from local lake
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

import yaml
from backtesting.replay_engine import ParquetBarSource, ReplayConfig
from backtesting.run_registry import RunSpec
from backtesting.runner import BacktestRunner
from backtesting.strategy_adapter import get_adapter
from strategy_engine.backtesting.backtester import IndianCostModel

# ── configuration ─────────────────────────────────────────────────────────────

S3_LAKE = "s3://quantembrace-backtest-data/lake/ohlcv"
LOCAL_LAKE = str(_REPO / "backtest-data" / "lake" / "ohlcv")

START_DATE = date(2020, 1, 1)
END_DATE = date(2024, 12, 31)
DATA_VERSION = "bhavcopy-nse-2020-2024-v1"
CODE_VERSION = "nifty50-momentum-5y-v2"

# Symbols with confirmed data in the lake (47 of 50 NIFTY50 — M&M, JIOFIN,
# ETERNAL were listed/renamed post-2020 and are excluded from this 5y study).
NIFTY50_SYMBOLS = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB",
    "DRREDDY", "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK",
    "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK",
    "INDUSINDBK", "INFY", "ITC", "JSWSTEEL", "KOTAKBANK",
    "LT", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHREECEM",
    "SUNPHARMA", "TATAMOTORS", "TATACONSUM", "TATASTEEL", "TCS",
    "TECHM", "TITAN",
]

# NSE equity delivery cost model (CNC positions, multi-day holds).
COST_MODEL = IndianCostModel.delivery()


def _filter_to_available(symbols: list[str], base_path: str) -> list[str]:
    """Keep only symbols present in the lake for the backtest period."""
    import glob
    if base_path.startswith("s3://"):
        # Trust the list — S3 was just verified to have all 14,308 files.
        return symbols
    available = set()
    for f in glob.glob(f"{base_path}/market=NSE/segment=EQ/symbol=*/interval=1d/year=2020/part-0.parquet"):
        sym = f.split("symbol=")[1].split("/")[0]
        available.add(sym)
    kept = [s for s in symbols if s in available]
    skipped = [s for s in symbols if s not in available]
    if skipped:
        print(f"  WARN: {len(skipped)} symbols not in local lake, skipped: {skipped}")
    return kept


def main() -> int:
    ap = argparse.ArgumentParser(description="NIFTY50 momentum 5-year authoritative backtest")
    ap.add_argument("--local", action="store_true", help="Read from local Parquet lake instead of S3")
    ap.add_argument("--dry-run", action="store_true", help="Print plan without running")
    args = ap.parse_args()

    lake_path = LOCAL_LAKE if args.local else S3_LAKE
    symbols = _filter_to_available(NIFTY50_SYMBOLS, lake_path)

    print("=" * 68)
    print("QuantEmbrace — NIFTY50 Momentum Backtest (5 years, AUTHORITATIVE)")
    print("Backtest-only. Advisory. No broker. No live trading.")
    print("=" * 68)
    print(f"  Lake     : {lake_path}")
    print(f"  Symbols  : {len(symbols)} NIFTY50")
    print(f"  Period   : {START_DATE} → {END_DATE}")
    print(f"  Strategy : momentum (SMA 10/50, min_confidence=0.0)")
    print(f"  Costs    : {COST_MODEL.cost_model_version}")
    print(f"             STT buy={COST_MODEL.stt_buy_pct}% sell={COST_MODEL.stt_sell_pct}%")
    print(f"             exchange={COST_MODEL.exchange_txn_pct}%  stamp={COST_MODEL.stamp_buy_pct}%")
    print(f"             slippage=5bps/leg  brokerage=0 (delivery Rs 0)")
    print(f"  Data ver : {DATA_VERSION}")
    print()

    if args.dry_run:
        print("DRY RUN — no backtest executed.")
        return 0

    spec = RunSpec(
        strategy="momentum",
        symbols=symbols,
        timeframe="1d",
        start_date=START_DATE,
        end_date=END_DATE,
        config_s3_path="s3://quantembrace-backtest-results/configs/nifty50-momentum-5y.json",
        data_version=DATA_VERSION,
        code_version=CODE_VERSION,
        cost_model_version=COST_MODEL.cost_model_version,
        exit_policy_version="v1",
    )

    print(f"  run_id   : {spec.run_id()}")
    print()

    runner = BacktestRunner.from_aws(emit_cw=False)

    adapter = get_adapter("momentum")
    source = ParquetBarSource(lake_path, source_name="bhavcopy")
    config = ReplayConfig(
        symbols=symbols,
        timeframes=["1d"],
        partition_by="symbol_year",
        date_from=START_DATE,
        date_to=END_DATE,
        market_hours_filter=False,
    )

    print(f"Running {len(symbols)} symbols × 5 years ({len(symbols)*5} partitions)...")
    summary = runner.run(
        spec,
        source=source,
        config=config,
        # min_confidence=0.0 for daily data: SMA divergence ≈ 0 at crossover by
        # definition, so the confidence metric doesn't scale to daily ATR. The
        # crossover itself is the signal gate; R:R sizing still applies.
        strategy_factory=lambda: adapter.build_strategy(
            symbols,
            short_window=10,
            long_window=50,
            min_confidence=0.0,
        ),
        backtester_kwargs={
            "slippage_bps": 5.0,
            "spread_bps": 0.0,     # spread captured in slippage for daily data
            "commission_pct": 0.0,  # Zerodha delivery: Rs 0 brokerage
            "indian_cost_model": COST_MODEL,
        },
    )

    print()
    if summary.status == "COMPLETED":
        m = summary.metrics or {}
        print("=" * 68)
        print(f"COMPLETED — run_id={summary.run_id}")
        print(f"  Partitions   : {summary.partitions_processed}")
        print(f"  Trades       : {summary.total_trades}")
        print(f"  Net P&L      : ₹{m.get('net_pnl', 0):,.0f}")
        print(f"  Total Return : {m.get('total_return_pct', 0):.2f}%  (on ₹10L / partition)")
        print(f"  Win Rate     : {m.get('win_rate', 0):.1f}%")
        print(f"  Profit Fac   : {m.get('profit_factor', 0):.3f}")
        print(f"  Max DD       : {m.get('max_drawdown_pct', 0):.2f}%")
        print(f"  Sharpe       : {m.get('sharpe_ratio', 0):.3f}  (*)")
        print(f"  NSE Costs    : ₹{m.get('cost_impact', 0):,.0f}")
        print(f"  Avg Winner   : ₹{m.get('avg_winner', 0):,.0f}")
        print(f"  Avg Loser    : ₹{m.get('avg_loser', 0):,.0f}")
        print(f"  Payoff Ratio : {m.get('payoff_ratio', 0):.3f}")
        print()
        print("  (*) Sharpe/Ann-return are unreliable — equity curve resets each")
        print("      symbol-year shard; use net_pnl / win_rate / profit_factor.")

        print(f"  Report S3  : {summary.result_s3_path}")
        print(f"  Report dir : {summary.local_report_dir}")
        print()
        print("Advisory only. Backtesting can recommend. It cannot promote.")
        print("A human reviews all results before any strategy change.")
        print("=" * 68)
        return 0
    else:
        print(f"FAILED — {summary.error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
