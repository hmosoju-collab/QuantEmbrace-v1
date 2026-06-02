#!/usr/bin/env python3
"""
QuantEmbrace Backtest Runner
============================

Loads historical OHLCV bars from S3 (or a local CSV for dev), runs a strategy
through the Backtester, and prints a full performance report.

Usage examples:
    # Local CSV — momentum strategy
    python scripts/backtest/run_backtest.py \
        --symbol RELIANCE --market NSE \
        --csv data/RELIANCE_1d.csv \
        --strategy momentum \
        --capital 1000000

    # S3 data — momentum strategy
    python scripts/backtest/run_backtest.py \
        --symbol AAPL --market US \
        --s3-bucket quantembrace-prod-ohlcv-data \
        --s3-prefix AAPL/1d/ \
        --from 2024-01-01 --to 2025-12-31 \
        --strategy momentum \
        --short-window 10 --long-window 50

    # scalp_1m hardened strategy
    python scripts/backtest/run_backtest.py \
        --symbol BHEL --market NSE \
        --csv data/BHEL_1m.csv \
        --strategy scalp_1m \
        --interval 1min

    # scalp_1m old (v1 without floors) strategy
    python scripts/backtest/run_backtest.py \
        --symbol BHEL --market NSE \
        --csv data/BHEL_1m.csv \
        --strategy scalp_1m_old \
        --interval 1min

    # A/B comparison: old scalp_1m vs hardened scalp_1m
    python scripts/backtest/run_backtest.py \
        --symbol BHEL --market NSE \
        --csv data/BHEL_1m.csv \
        --interval 1min \
        --compare-scalp

CSV format expected (header row):
    timestamp,open,high,low,close,volume

Timestamps: ISO-8601 (e.g. "2024-01-02T09:15:00+05:30" or "2024-01-02")
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root: python scripts/backtest/run_backtest.py
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services"))

from strategy_engine.backtesting.backtester import Backtester, IndianCostModel
from strategy_engine.strategies.base_strategy import Bar
from strategy_engine.strategies.momentum_strategy import MomentumStrategy
from strategy_engine.strategies.scalp_1m_strategy import Scalp1mStrategy


# ─────────────────────────────────────────────────────────────────────────────
# Bar loading helpers
# ─────────────────────────────────────────────────────────────────────────────


def _parse_ts(value: str) -> datetime:
    """Parse ISO timestamp; make timezone-aware (UTC if naive)."""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {value!r}")


def _load_csv(path: str, symbol: str, market: str, interval: str = "1d") -> list[Bar]:
    """Load bars from a local CSV file."""
    bars: list[Bar] = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            bars.append(
                Bar(
                    symbol=symbol,
                    market=market,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row.get("volume", 0))),
                    timestamp=_parse_ts(row["timestamp"]),
                    interval=interval,
                )
            )
    bars.sort(key=lambda b: b.timestamp)
    return bars


def _load_s3(
    bucket: str,
    prefix: str,
    symbol: str,
    market: str,
    date_from: str,
    date_to: str,
    interval: str = "1d",
) -> list[Bar]:
    """Load bars from S3 (concatenates all CSV objects under prefix)."""
    try:
        import boto3
    except ImportError:
        raise RuntimeError("boto3 required for S3 data loading: pip install boto3")

    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    bars: list[Bar] = []

    dt_from = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
    dt_to = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode()
            reader = csv.DictReader(io.StringIO(body))
            for row in reader:
                ts = _parse_ts(row["timestamp"])
                if dt_from <= ts <= dt_to:
                    bars.append(
                        Bar(
                            symbol=symbol,
                            market=market,
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=int(float(row.get("volume", 0))),
                            timestamp=ts,
                            interval=interval,
                        )
                    )

    bars.sort(key=lambda b: b.timestamp)
    return bars


# ─────────────────────────────────────────────────────────────────────────────
# Strategy factory
# ─────────────────────────────────────────────────────────────────────────────


def _build_strategy(args: argparse.Namespace):
    """Construct the requested strategy from CLI args."""
    if args.strategy == "momentum":
        return MomentumStrategy(
            name="momentum_backtest",
            symbols=[args.symbol],
            market=args.market,
            short_window=args.short_window,
            long_window=args.long_window,
            atr_period=args.atr_period,
            atr_stop_multiplier=args.atr_stop_mult,
            atr_tp_multiplier=args.atr_tp_mult,
            min_confidence=args.min_confidence,
            risk_pct_per_trade=args.risk_pct,
            capital=args.capital,
        )
    if args.strategy == "scalp_1m":
        return _build_scalp_new(args)
    if args.strategy == "scalp_1m_old":
        return _build_scalp_old(args)
    raise ValueError(f"Unknown strategy: {args.strategy!r}")


def _build_scalp_new(args: argparse.Namespace) -> Scalp1mStrategy:
    """Build the hardened scalp_1m strategy (v2) with stop/TP floors."""
    return Scalp1mStrategy(
        name="scalp_1m_v2_backtest",
        symbols=[args.symbol],
        market=args.market,
        atr_period=args.atr_period,
        atr_stop_multiplier=0.5,
        rr_ratio=1.5,
        volume_multiplier=1.5,
        min_body_atr_pct=0.3,
        min_confidence=args.min_confidence,
        max_signals_per_day=3,
        # Hardened floors (Session 8 post-mortem)
        min_stop_pct=0.15,
        min_target_pct=0.25,
        min_spread_multiple=3.0,
        min_tick_multiple=5.0,
        max_spread_pct=0.08,
        min_net_edge_pct=0.12,
        reject_if_spread_unavailable=True,
        reject_if_ltp_stale=True,
        nav=args.capital,
        paper_trade=True,
    )


def _build_scalp_old(args: argparse.Namespace) -> Scalp1mStrategy:
    """Build the pre-hardening scalp_1m strategy (v1) — no floors.

    Recreates the v1 behaviour by disabling all protective floors and
    filters.  Used only for A/B comparison in --compare-scalp mode.
    """
    return Scalp1mStrategy(
        name="scalp_1m_v1_backtest",
        symbols=[args.symbol],
        market=args.market,
        atr_period=args.atr_period,
        atr_stop_multiplier=0.5,
        rr_ratio=1.5,
        volume_multiplier=1.5,
        min_body_atr_pct=0.3,
        min_confidence=args.min_confidence,
        max_signals_per_day=3,
        # All floors disabled to match v1 (bare ATR stop, no viability check)
        min_stop_pct=0.0,
        min_target_pct=0.0,
        min_spread_multiple=0.0,
        min_tick_multiple=0.0,
        max_spread_pct=100.0,      # effectively disabled
        min_net_edge_pct=-999.0,   # effectively disabled
        reject_if_spread_unavailable=False,
        reject_if_ltp_stale=False,
        nav=args.capital,
        paper_trade=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Report printer
# ─────────────────────────────────────────────────────────────────────────────


def _print_report(result, verbose: bool = False, output_json: str | None = None) -> None:
    sep = "─" * 60

    print(f"\n{'═' * 60}")
    print("  QuantEmbrace Backtest Report")
    print(f"{'═' * 60}")
    print(f"  Strategy  : {result.strategy_name}")
    print(f"  Period    : {result.start_date:%Y-%m-%d} → {result.end_date:%Y-%m-%d}")
    print(f"  Capital   : {result.initial_capital:,.0f} → {result.final_capital:,.0f}")
    print(sep)
    print(f"  Return         : {result.total_return_pct:+.2f}%")
    print(f"  Ann. Return    : {result.annualised_return_pct:+.2f}%")
    print(f"  Max Drawdown   : {result.max_drawdown_pct:.2f}%  (₹{result.max_drawdown_abs:,.0f})")
    print(sep)
    print(f"  Sharpe Ratio   : {result.sharpe_ratio:.3f}")
    print(f"  Sortino Ratio  : {result.sortino_ratio:.3f}")
    print(sep)
    print(f"  Signals        : {result.signals_generated}  (B={result.buy_signals} S={result.sell_signals})")
    print(f"  Total Trades   : {result.total_trades}")
    print(f"  Rejected Orders: {result.rejected_orders}")
    print(f"  Win Rate       : {result.win_rate:.1f}%")
    print(f"  Profit Factor  : {result.profit_factor:.2f}")
    print(f"  Avg Win        : {result.avg_win:+,.2f}")
    print(f"  Avg Loss       : {result.avg_loss:+,.2f}")
    print(f"  Largest Win    : {result.largest_win:+,.2f}")
    print(f"  Largest Loss   : {result.largest_loss:+,.2f}")
    print(f"  Total Costs    : {result.total_costs:,.2f}")
    print(f"  Slippage Paid  : {result.total_slippage:,.2f}")
    print(f"  Lookahead Viol.: {result.lookahead_violations}")
    if result.drawdown_stress:
        print(
            "  DD Stress      : "
            f"1.5x={result.drawdown_stress.get('stress_1_5x_pct', 0.0):.2f}% "
            f"2x={result.drawdown_stress.get('stress_2x_pct', 0.0):.2f}%"
        )
    print(f"{'═' * 60}\n")

    if verbose and result.trades:
        print(f"  Trade Log ({len(result.trades)} trades)")
        print(sep)
        for t in result.trades:
            print(
                f"  {t.direction.value:<4} {t.symbol:<10} "
                f"entry={t.entry_price:>10.4f} exit={t.exit_price:>10.4f} "
                f"qty={t.quantity:>5} pnl={t.pnl:>+12.2f}  [{t.exit_reason}]"
                f"  {t.entry_time:%Y-%m-%d} → {t.exit_time:%Y-%m-%d}"
            )
        print()

    if output_json:
        data = {
            "strategy_name": result.strategy_name,
            "start_date": result.start_date.isoformat() if result.start_date else None,
            "end_date": result.end_date.isoformat() if result.end_date else None,
            "initial_capital": result.initial_capital,
            "final_capital": result.final_capital,
            "total_return_pct": result.total_return_pct,
            "annualised_return_pct": result.annualised_return_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "max_drawdown_abs": result.max_drawdown_abs,
            "sharpe_ratio": result.sharpe_ratio,
            "sortino_ratio": result.sortino_ratio,
            "total_trades": result.total_trades,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "avg_win": result.avg_win,
            "avg_loss": result.avg_loss,
            "largest_win": result.largest_win,
            "largest_loss": result.largest_loss,
            "total_commission": result.total_commission,
            "total_costs": result.total_costs,
            "total_slippage": result.total_slippage,
            "rejected_orders": result.rejected_orders,
            "lookahead_violations": result.lookahead_violations,
            "drawdown_stress": result.drawdown_stress,
            "signals_generated": result.signals_generated,
            "trades": [
                {
                    "symbol": t.symbol,
                    "market": t.market,
                    "direction": t.direction.value,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "quantity": t.quantity,
                    "entry_time": t.entry_time.isoformat(),
                    "exit_time": t.exit_time.isoformat(),
                    "exit_reason": t.exit_reason,
                    "pnl": t.pnl,
                    "commission": t.commission,
                    "slippage": t.slippage,
                }
                for t in result.trades
            ],
        }
        with open(output_json, "w") as fh:
            json.dump(data, fh, indent=2)
        print(f"  JSON results saved to: {output_json}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="QuantEmbrace backtest runner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Data source (mutually exclusive)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="Path to local CSV file")
    src.add_argument("--s3-bucket", help="S3 bucket name for OHLCV data")

    # S3 options
    p.add_argument("--s3-prefix", default="", help="S3 key prefix for bar files")
    p.add_argument("--from", dest="date_from", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    p.add_argument("--to", dest="date_to", default="2025-12-31", help="End date (YYYY-MM-DD)")

    # Instrument
    p.add_argument("--symbol", required=True, help="Trading symbol (e.g. RELIANCE, AAPL)")
    p.add_argument("--market", default="NSE", choices=["NSE", "BSE", "US"], help="Market")
    p.add_argument("--interval", default="1d", help="Bar interval (1min, 5min, 1h, 1d)")

    # Strategy
    p.add_argument(
        "--strategy",
        default="momentum",
        choices=["momentum", "scalp_1m", "scalp_1m_old"],
        help="Strategy to backtest",
    )
    p.add_argument(
        "--compare-scalp",
        action="store_true",
        help=(
            "Run scalp_1m v1 (old, no floors) vs v2 (hardened) on the same data "
            "and print a side-by-side comparison table. Ignores --strategy."
        ),
    )
    p.add_argument("--short-window", type=int, default=10, help="Short MA window")
    p.add_argument("--long-window", type=int, default=50, help="Long MA window")
    p.add_argument("--atr-period", type=int, default=14, help="ATR period")
    p.add_argument("--atr-stop-mult", type=float, default=2.0, help="ATR stop-loss multiplier")
    p.add_argument("--atr-tp-mult", type=float, default=3.0, help="ATR take-profit multiplier")
    p.add_argument("--min-confidence", type=float, default=0.55, help="Min signal confidence")
    p.add_argument("--risk-pct", type=float, default=0.01, help="Risk %% of capital per trade")

    # Execution
    p.add_argument("--capital", type=float, default=1_000_000.0, help="Starting capital")
    p.add_argument("--commission", type=float, default=0.03, help="Commission %% per leg")
    p.add_argument("--slippage-bps", type=float, default=2.0, help="Adverse fill slippage in bps")
    p.add_argument("--spread-bps", type=float, default=5.0, help="Bid/ask spread in bps")
    p.add_argument(
        "--max-order-bar-volume-pct",
        type=float,
        default=10.0,
        help="Reject orders above this percent of the bar volume",
    )
    p.add_argument(
        "--no-indian-costs",
        action="store_true",
        help="Disable Indian statutory costs and use commission only",
    )
    p.add_argument(
        "--disable-gap-stops",
        action="store_true",
        help="Fill stops at trigger price instead of worse gap-open price",
    )
    p.add_argument("--risk-free-rate", type=float, default=0.06, help="Annual risk-free rate")
    p.add_argument("--allow-short", action="store_true", help="Allow short selling")

    # Output
    p.add_argument("--verbose", "-v", action="store_true", help="Print full trade log")
    p.add_argument("--output-json", help="Save results to JSON file")

    return p


def _print_scalp_comparison(old_result, new_result) -> None:
    """Print a side-by-side A/B comparison table for scalp_1m v1 vs v2.

    Computes signals_rejected for v2 as the difference in signals_generated
    (since v1 has no viability filter, it generates more raw signals).
    """
    sep = "─" * 54

    def _pct(v) -> str:
        return f"{v:.1f}%"

    def _inr(v) -> str:
        return f"₹{v:,.0f}"

    def _f2(v) -> str:
        return f"{v:.2f}"

    def _i(v) -> str:
        return str(int(v))

    old_rejected = 0  # v1 has no rejection logic
    new_rejected = max(0, old_result.signals_generated - new_result.signals_generated)

    rows = [
        ("Metric",             "Old (v1)",                     "New (v2)"),
        (sep,                  sep,                            sep),
        ("win_rate_pct",       _pct(old_result.win_rate),      _pct(new_result.win_rate)),
        ("avg_winner",         _inr(old_result.avg_win),       _inr(new_result.avg_win)),
        ("avg_loser",          _inr(old_result.avg_loss),      _inr(new_result.avg_loss)),
        ("profit_factor",      _f2(old_result.profit_factor),  _f2(new_result.profit_factor)),
        ("net_pnl",            _inr(old_result.final_capital - old_result.initial_capital),
                               _inr(new_result.final_capital - new_result.initial_capital)),
        ("signals_generated",  _i(old_result.signals_generated), _i(new_result.signals_generated)),
        ("signals_rejected",   _i(old_rejected),               _i(new_rejected)),
    ]

    print(f"\n{'=' * 54}")
    print("  === Scalp A/B Comparison ===")
    print(f"{'=' * 54}")
    for label, old_val, new_val in rows:
        if label == sep:
            print(f"  {sep}")
        else:
            print(f"  {label:<24}{old_val:<14}{new_val}")
    print(f"{'=' * 54}\n")


async def _run_backtest(args: argparse.Namespace, strategy, bars: list[Bar]):
    """Run a single backtest and return the result."""
    bt = Backtester(
        strategy=strategy,
        initial_capital=args.capital,
        commission_pct=args.commission,
        risk_free_rate_annual=args.risk_free_rate,
        allow_short=args.allow_short,
        slippage_bps=args.slippage_bps,
        spread_bps=args.spread_bps,
        max_order_bar_volume_pct=args.max_order_bar_volume_pct,
        indian_cost_model=None if not args.no_indian_costs else IndianCostModel(enabled=False),
        gap_stop_behavior=not args.disable_gap_stops,
    )
    return await bt.run(bars)


async def _main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Load bars
    if args.csv:
        print(f"Loading bars from {args.csv}...")
        bars = _load_csv(args.csv, args.symbol, args.market, args.interval)
    else:
        print(f"Loading bars from s3://{args.s3_bucket}/{args.s3_prefix}...")
        bars = _load_s3(
            args.s3_bucket,
            args.s3_prefix,
            args.symbol,
            args.market,
            args.date_from,
            args.date_to,
            args.interval,
        )

    if not bars:
        print("ERROR: No bars loaded. Check your data source and date range.")
        sys.exit(1)

    print(f"Loaded {len(bars)} bars  ({bars[0].timestamp:%Y-%m-%d} → {bars[-1].timestamp:%Y-%m-%d})")

    # ── A/B comparison mode ──────────────────────────────────────────────────
    if args.compare_scalp:
        print("Running scalp A/B comparison (old v1 vs hardened v2)...")

        old_strategy = _build_scalp_old(args)
        new_strategy = _build_scalp_new(args)

        print("  Running scalp_1m v1 (old)...")
        old_result = await _run_backtest(args, old_strategy, bars)

        print("  Running scalp_1m v2 (hardened)...")
        new_result = await _run_backtest(args, new_strategy, bars)

        _print_report(old_result, verbose=False)
        _print_report(new_result, verbose=False)
        _print_scalp_comparison(old_result, new_result)
        return

    # ── Single-strategy mode (existing behaviour) ────────────────────────────
    strategy = _build_strategy(args)
    result = await _run_backtest(args, strategy, bars)
    _print_report(result, verbose=args.verbose, output_json=args.output_json)


if __name__ == "__main__":
    asyncio.run(_main())
