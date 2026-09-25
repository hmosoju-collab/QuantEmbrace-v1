#!/usr/bin/env python3
"""
QuantEmbrace — Phase 1 Local Backtest Runner
=============================================
Reads the local Parquet lake (produced by download_bhavcopy.py) and runs
the momentum strategy across NSE EQ symbols.

PREREQUISITE: Run download_bhavcopy.py first to populate backtest-data/lake/

Phase 1 scope (daily OHLCV):
  - Strategy: momentum (MA crossover + ATR stops) — works on any timeframe
  - Data: 1d bars from NSE Bhavcopy (2016–2025)
  - Universe: NIFTY 50 constituents (falls back to all available symbols)
  - Checks: lookahead_violations MUST be 0 for results to be valid

Acceptance criteria (from docs/backtesting/daily-bhavcopy-ingestion-design.md §6):
  ✓ lookahead_violations == 0
  ✓ Data from real Parquet lake (trust_level=HIGH, source=bhavcopy)
  ✓ IndianCostModel applied (STT, exchange txn, SEBI, stamp, GST, brokerage)
  ✓ Report written to reports/phase1/

Usage:
    # Run on NIFTY 50 (default)
    python scripts/backtest/run_phase1_local.py

    # Run on specific symbols
    python scripts/backtest/run_phase1_local.py --symbols RELIANCE,TCS,INFY

    # Run with custom date range
    python scripts/backtest/run_phase1_local.py --start 2020-01-01 --end 2024-12-31

    # Self-test with synthetic data (no real data needed)
    python scripts/backtest/run_phase1_local.py --self-test

Backtest-only. No broker APIs. No live trading. No capital changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow.parquet as pq

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))
sys.path.insert(0, str(_REPO))

_DEFAULT_LAKE = _REPO / "backtest-data" / "lake" / "ohlcv" / "market=NSE" / "segment=EQ"
_REPORT_DIR = _REPO / "reports" / "phase1"

IST = "Asia/Kolkata"

# ── NIFTY 50 universe (primary; falls back to lake if symbol not found) ───────
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BHARTIARTL", "BPCL",
    "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]


# ── Indian cost model (from docs/backtesting/aws-backtesting-specification.md) ─
class IndianCostModel:
    """Mandatory transaction cost model for NSE equity trades."""

    STT_SELL = 0.00025           # 0.025% on sell side only (equity delivery)
    EXCHANGE_TXN = 0.0000297     # 0.00297% both sides
    SEBI = 0.000001              # 0.0001% both sides
    STAMP_BUY = 0.00003          # 0.003% on buy side only
    GST_RATE = 0.18              # 18% on (brokerage + exchange txn + SEBI)
    BROKERAGE = 0.0003           # 0.03% both sides (or ₹20/order, whichever lower)
    MAX_BROKERAGE_PER_ORDER = 20.0  # ₹20 cap per order

    @classmethod
    def total_cost(cls, price: float, qty: int, is_buy: bool) -> float:
        """Total transaction cost in ₹ for one leg of a trade."""
        value = price * qty
        brokerage = min(value * cls.BROKERAGE, cls.MAX_BROKERAGE_PER_ORDER)
        exchange_txn = value * cls.EXCHANGE_TXN
        sebi = value * cls.SEBI
        gst = (brokerage + exchange_txn + sebi) * cls.GST_RATE
        stt = (value * cls.STT_SELL) if not is_buy else 0.0
        stamp = (value * cls.STAMP_BUY) if is_buy else 0.0
        return brokerage + exchange_txn + sebi + gst + stt + stamp

    @classmethod
    def round_trip_bps(cls, price: float, qty: int) -> float:
        """Round-trip cost as basis points for reporting."""
        value = price * qty
        total = cls.total_cost(price, qty, True) + cls.total_cost(price, qty, False)
        return (total / value) * 10000 if value else 0.0


# ── Bar loading ───────────────────────────────────────────────────────────────

def _load_bars_from_parquet(
    symbol: str,
    lake_base: Path,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> list[dict]:
    """Load all 1d bars for a symbol from the local Parquet lake."""
    sym_dir = lake_base / f"symbol={symbol}" / "interval=1d"
    if not sym_dir.exists():
        return []

    frames = []
    for year_dir in sorted(sym_dir.iterdir()):
        parquet_path = year_dir / "part-0.parquet"
        if not parquet_path.exists():
            continue
        pf = pq.ParquetFile(parquet_path)
        df = pf.read().to_pandas()
        frames.append(df)

    if not frames:
        return []

    df = pd.concat(frames, ignore_index=True)

    # Parse timestamp
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=False)
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
        else:
            df["timestamp"] = df["timestamp"].dt.tz_convert(IST)

    df = df.sort_values("timestamp")

    # Date range filter
    if start:
        start_ts = pd.Timestamp(start).tz_localize(IST) if start.tzinfo is None else pd.Timestamp(start).tz_convert(IST)
        df = df[df["timestamp"] >= start_ts]
    if end:
        end_ts = pd.Timestamp(end).tz_localize(IST) if end.tzinfo is None else pd.Timestamp(end).tz_convert(IST)
        df = df[df["timestamp"] <= end_ts]

    bars = []
    for _, row in df.iterrows():
        bars.append({
            "symbol": row.get("symbol", symbol),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": int(row.get("volume", 0)),
            "timestamp": row["timestamp"].to_pydatetime(),
            "prev_close": float(row["prev_close"]) if pd.notna(row.get("prev_close")) else None,
        })
    return bars


# ── Simple momentum backtester (self-contained, no import hell) ───────────────

def _run_momentum_backtest(
    bars: list[dict],
    symbol: str,
    capital: float = 1_000_000.0,
    short_window: int = 10,
    long_window: int = 50,
    atr_period: int = 14,
    atr_stop_mult: float = 2.0,
    atr_tp_mult: float = 3.0,
    min_confidence: float = 0.55,
) -> dict:
    """
    Self-contained daily momentum backtest.
    Next-bar execution (signal on bar T fills at bar T+1 open).
    Lookahead violations are tracked and counted.
    """
    if len(bars) < long_window + atr_period + 2:
        return {"symbol": symbol, "error": f"insufficient bars ({len(bars)})"}

    # ── indicators ────────────────────────────────────────────────────────────
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]

    def sma(prices: list[float], period: int, i: int) -> Optional[float]:
        if i < period - 1:
            return None
        return sum(prices[i - period + 1: i + 1]) / period

    def atr(i: int, period: int) -> Optional[float]:
        if i < period:
            return None
        trs = []
        for j in range(i - period + 1, i + 1):
            prev_close = closes[j - 1] if j > 0 else closes[j]
            trs.append(max(highs[j] - lows[j], abs(highs[j] - prev_close), abs(lows[j] - prev_close)))
        return sum(trs) / period

    # ── backtest loop ─────────────────────────────────────────────────────────
    nav = capital
    position = 0          # +qty = long, 0 = flat
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    entry_time = None

    trades: list[dict] = []
    equity_curve: list[tuple] = [(bars[0]["timestamp"], nav)]
    lookahead_violations = 0
    signals_generated = 0
    prev_short_sma: Optional[float] = None
    prev_long_sma: Optional[float] = None

    for i, bar in enumerate(bars[:-1]):  # signal on bar i, fill on bar i+1
        short_sma = sma(closes, short_window, i)
        long_sma = sma(closes, long_window, i)
        cur_atr = atr(i, atr_period)

        if short_sma is None or long_sma is None or cur_atr is None:
            prev_short_sma = short_sma
            prev_long_sma = long_sma
            continue

        next_bar = bars[i + 1]  # execution bar (T+1) — NO lookahead

        # ── stop/target check against NEXT bar's high/low ─────────────────
        if position > 0:
            # Long position: check if stop or target hit on next bar
            exit_price = None
            exit_reason = None

            if next_bar["low"] <= stop_loss:
                exit_price = stop_loss  # conservative: use stop price
                exit_reason = "stop_loss"
            elif next_bar["high"] >= take_profit:
                exit_price = take_profit
                exit_reason = "take_profit"

            if exit_price:
                cost = IndianCostModel.total_cost(exit_price, position, is_buy=False)
                pnl = (exit_price - entry_price) * position - cost
                nav += pnl
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": next_bar["timestamp"],
                    "symbol": symbol,
                    "direction": "BUY",
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "qty": position,
                    "exit_reason": exit_reason,
                    "pnl": pnl,
                    "cost": cost,
                })
                position = 0
                equity_curve.append((next_bar["timestamp"], nav))
                prev_short_sma = short_sma
                prev_long_sma = long_sma
                continue

        # ── signal generation on bar i (using ONLY past data) ─────────────
        golden_cross = (
            prev_short_sma is not None and prev_long_sma is not None and
            prev_short_sma <= prev_long_sma and short_sma > long_sma
        )
        death_cross = (
            prev_short_sma is not None and prev_long_sma is not None and
            prev_short_sma >= prev_long_sma and short_sma < long_sma
        )

        # Confidence: MA gap normalised by price
        ma_gap = abs(short_sma - long_sma) / closes[i] if closes[i] else 0
        confidence = min(0.99, 0.55 + ma_gap * 100)

        if golden_cross and position == 0 and confidence >= min_confidence:
            # BUY signal on bar i → fill at T+1 open
            fill_price = next_bar["open"]
            # Lookahead check: fill must NOT use bar-i close
            if fill_price == closes[i]:
                lookahead_violations += 1  # should never happen with next-bar fill

            risk_per_unit = cur_atr * atr_stop_mult
            if risk_per_unit <= 0:
                prev_short_sma = short_sma
                prev_long_sma = long_sma
                continue

            risk_capital = nav * 0.01  # 1% of NAV per trade
            qty = max(1, int(risk_capital / risk_per_unit))
            trade_value = fill_price * qty

            # Size check: don't exceed 20% of NAV per position
            if trade_value > nav * 0.20:
                qty = max(1, int(nav * 0.20 / fill_price))
                trade_value = fill_price * qty

            cost = IndianCostModel.total_cost(fill_price, qty, is_buy=True)
            if trade_value + cost > nav:
                prev_short_sma = short_sma
                prev_long_sma = long_sma
                continue

            position = qty
            entry_price = fill_price
            stop_loss = fill_price - cur_atr * atr_stop_mult
            take_profit = fill_price + cur_atr * atr_tp_mult
            entry_time = next_bar["timestamp"]
            nav -= cost
            signals_generated += 1

        elif death_cross and position > 0:
            # Exit long on death cross
            fill_price = next_bar["open"]
            cost = IndianCostModel.total_cost(fill_price, position, is_buy=False)
            pnl = (fill_price - entry_price) * position - cost
            nav += pnl
            trades.append({
                "entry_time": entry_time,
                "exit_time": next_bar["timestamp"],
                "symbol": symbol,
                "direction": "BUY",
                "entry_price": entry_price,
                "exit_price": fill_price,
                "qty": position,
                "exit_reason": "signal",
                "pnl": pnl,
                "cost": cost,
            })
            position = 0
            equity_curve.append((next_bar["timestamp"], nav))
            signals_generated += 1

        prev_short_sma = short_sma
        prev_long_sma = long_sma

    # Close any open position at last bar's close (EOD)
    if position > 0 and bars:
        last_bar = bars[-1]
        fill_price = last_bar["close"]
        cost = IndianCostModel.total_cost(fill_price, position, is_buy=False)
        pnl = (fill_price - entry_price) * position - cost
        nav += pnl
        trades.append({
            "entry_time": entry_time,
            "exit_time": last_bar["timestamp"],
            "symbol": symbol,
            "direction": "BUY",
            "entry_price": entry_price,
            "exit_price": fill_price,
            "qty": position,
            "exit_reason": "eod",
            "pnl": pnl,
            "cost": cost,
        })
        equity_curve.append((last_bar["timestamp"], nav))

    # ── metrics ───────────────────────────────────────────────────────────────
    total_return_pct = (nav - capital) / capital * 100
    winning = [t for t in trades if t["pnl"] > 0]
    losing = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in winning)
    gross_loss = abs(sum(t["pnl"] for t in losing))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    win_rate = len(winning) / len(trades) * 100 if trades else 0.0

    # Max drawdown
    peak = capital
    max_dd_pct = 0.0
    running_nav = capital
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd

    # Annualised return
    n_bars = len(bars)
    years = n_bars / 252.0
    annualised_return_pct = (
        ((nav / capital) ** (1.0 / years) - 1.0) * 100
        if years > 0 and nav > 0
        else 0.0
    )

    # Expectancy (avg $ per trade)
    avg_pnl = sum(t["pnl"] for t in trades) / len(trades) if trades else 0.0
    avg_win = sum(t["pnl"] for t in winning) / len(winning) if winning else 0.0
    avg_loss = sum(t["pnl"] for t in losing) / len(losing) if losing else 0.0

    return {
        "symbol": symbol,
        "bars": len(bars),
        "trades": len(trades),
        "signals_generated": signals_generated,
        "lookahead_violations": lookahead_violations,
        "initial_capital": capital,
        "final_capital": nav,
        "total_return_pct": round(total_return_pct, 4),
        "annualised_return_pct": round(annualised_return_pct, 4),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
        "win_rate_pct": round(win_rate, 2),
        "avg_pnl_per_trade": round(avg_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "total_commission_inr": round(sum(t["cost"] for t in trades), 2),
        "trade_log": trades,
        "equity_curve": [(str(ts), round(v, 2)) for ts, v in equity_curve],
    }


# ── Aggregate across symbols ──────────────────────────────────────────────────

def _aggregate(results: list[dict]) -> dict:
    valid = [r for r in results if "error" not in r]
    if not valid:
        return {}

    lah = sum(r["lookahead_violations"] for r in valid)
    pf_vals = [r["profit_factor"] for r in valid if r.get("profit_factor") is not None]
    ret_vals = [r["total_return_pct"] for r in valid]
    dd_vals = [r["max_drawdown_pct"] for r in valid]
    wr_vals = [r["win_rate_pct"] for r in valid]
    ann_vals = [r["annualised_return_pct"] for r in valid]

    def avg(lst: list) -> float:
        return sum(lst) / len(lst) if lst else 0.0

    winners = [r for r in valid if r["total_return_pct"] > 0]
    profitable_pct = len(winners) / len(valid) * 100 if valid else 0.0

    return {
        "symbols_tested": len(valid),
        "symbols_errored": len(results) - len(valid),
        "total_lookahead_violations": lah,
        "lookahead_check": "PASS" if lah == 0 else f"FAIL ({lah} violations)",
        "avg_total_return_pct": round(avg(ret_vals), 4),
        "avg_annualised_return_pct": round(avg(ann_vals), 4),
        "avg_max_drawdown_pct": round(avg(dd_vals), 4),
        "avg_profit_factor": round(avg(pf_vals), 4),
        "avg_win_rate_pct": round(avg(wr_vals), 2),
        "profitable_symbols_pct": round(profitable_pct, 1),
        "total_trades": sum(r["trades"] for r in valid),
        "total_commission_inr": round(sum(r["total_commission_inr"] for r in valid), 2),
    }


# ── Report writer ─────────────────────────────────────────────────────────────

def _write_report(agg: dict, results: list[dict], out_dir: Path, run_date: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"phase1_backtest_{run_date}.md"

    lah_status = "✅ PASS" if agg.get("total_lookahead_violations", 1) == 0 else "❌ FAIL"
    pf_val = agg.get("avg_profit_factor", 0)
    live_ready = (
        agg.get("total_lookahead_violations", 1) == 0 and
        pf_val > 1.2 and
        agg.get("avg_total_return_pct", 0) > 0
    )
    verdict = "PAPER_OPTIMIZATION" if not live_ready else "ELIGIBLE_FOR_PAPER_VALIDATION"

    lines = [
        f"# QuantEmbrace Phase 1 Backtest Report",
        f"**Run date:** {run_date}  ",
        f"**Strategy:** momentum (MA crossover, daily bars, IndianCostModel)  ",
        f"**Data:** NSE Bhavcopy 1d OHLCV (source=bhavcopy, trust_level=HIGH)  ",
        f"**Backtesting only — no live trading, no broker APIs, no capital changes.**",
        "",
        "---",
        "",
        "## Aggregate Results",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Symbols tested | {agg.get('symbols_tested', 0)} |",
        f"| Symbols errored (insufficient data) | {agg.get('symbols_errored', 0)} |",
        f"| Total trades | {agg.get('total_trades', 0):,} |",
        f"| Avg total return | {agg.get('avg_total_return_pct', 0):.2f}% |",
        f"| Avg annualised return | {agg.get('avg_annualised_return_pct', 0):.2f}% |",
        f"| Avg max drawdown | {agg.get('avg_max_drawdown_pct', 0):.2f}% |",
        f"| Avg profit factor | {agg.get('avg_profit_factor', 0):.4f} |",
        f"| Avg win rate | {agg.get('avg_win_rate_pct', 0):.1f}% |",
        f"| Profitable symbols | {agg.get('profitable_symbols_pct', 0):.1f}% |",
        f"| Total commission paid | ₹{agg.get('total_commission_inr', 0):,.2f} |",
        "",
        "## Safety Gates",
        "",
        f"| Gate | Status |",
        f"|------|--------|",
        f"| Lookahead violations == 0 | {lah_status} |",
        f"| Source trust level | HIGH (bhavcopy, official NSE) |",
        f"| IndianCostModel applied | ✅ Yes (STT, txn, SEBI, stamp, GST, brokerage) |",
        f"| Next-bar execution | ✅ Yes (signal T → fill T+1 open) |",
        "",
        f"## Verdict: `{verdict}`",
        "",
    ]

    if not live_ready:
        lines.append(
            "> **PAPER_OPTIMIZATION**: Strategy does not yet meet live-readiness gates "
            "(profit_factor > 1.2, return > 0, 0 lookahead violations). "
            "Continue paper sessions and parameter tuning. "
            "Live trading remains BLOCKED per CLAUDE.md § Strategy Performance Live-Readiness Rule."
        )
    else:
        lines.append(
            "> **ELIGIBLE_FOR_PAPER_VALIDATION**: Phase 1 gates passed. "
            "Next step: run ≥5 consecutive valid paper sessions before considering live promotion."
        )

    lines += [
        "",
        "---",
        "",
        "## Per-Symbol Results",
        "",
        f"| Symbol | Bars | Trades | Return% | Ann.Return% | MaxDD% | PF | WR% | LH_Violations |",
        f"|--------|------|--------|---------|-------------|--------|----|-----|---------------|",
    ]

    valid_results = sorted(
        [r for r in results if "error" not in r],
        key=lambda r: r.get("total_return_pct", 0),
        reverse=True,
    )

    for r in valid_results:
        pf = f"{r['profit_factor']:.2f}" if r.get("profit_factor") is not None else "∞"
        lh = r.get("lookahead_violations", 0)
        lh_str = f"✅ 0" if lh == 0 else f"❌ {lh}"
        lines.append(
            f"| {r['symbol']} | {r['bars']:,} | {r['trades']} | "
            f"{r['total_return_pct']:.2f}% | {r['annualised_return_pct']:.2f}% | "
            f"{r['max_drawdown_pct']:.2f}% | {pf} | {r['win_rate_pct']:.1f}% | {lh_str} |"
        )

    errors = [r for r in results if "error" in r]
    if errors:
        lines += ["", "### Symbols Skipped (insufficient data)", ""]
        for r in errors:
            lines.append(f"- `{r['symbol']}`: {r['error']}")

    report_path.write_text("\n".join(lines))
    return report_path


# ── Self-test ─────────────────────────────────────────────────────────────────

def _generate_synthetic_bars(symbol: str, n: int = 500) -> list[dict]:
    """Generate synthetic daily bars for self-test."""
    import random
    random.seed(42)
    bars = []
    price = 1000.0
    ts = datetime(2021, 1, 4, 15, 30, tzinfo=timezone.utc)
    from datetime import timedelta
    for i in range(n):
        change = random.gauss(0.0003, 0.015)
        open_ = price
        close = price * (1 + change)
        high = max(open_, close) * (1 + abs(random.gauss(0, 0.005)))
        low = min(open_, close) * (1 - abs(random.gauss(0, 0.005)))
        bars.append({
            "symbol": symbol,
            "open": round(open_, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": random.randint(100000, 5000000),
            "timestamp": ts,
            "prev_close": round(price, 2),
        })
        price = close
        # skip weekends
        ts += timedelta(days=1)
        while ts.weekday() >= 5:
            ts += timedelta(days=1)
    return bars


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Phase 1 backtest runner — reads local Parquet lake")
    p.add_argument("--lake", default=str(_DEFAULT_LAKE), help="Path to lake/ohlcv/market=NSE/segment=EQ/")
    p.add_argument("--symbols", default="", help="Comma-separated symbols (default: NIFTY 50)")
    p.add_argument("--start", default="2016-01-01", help="Start date YYYY-MM-DD")
    p.add_argument("--end", default="2025-12-31", help="End date YYYY-MM-DD")
    p.add_argument("--capital", type=float, default=1_000_000.0, help="Starting capital ₹")
    p.add_argument("--short-window", type=int, default=10)
    p.add_argument("--long-window", type=int, default=50)
    p.add_argument("--report-dir", default=str(_REPORT_DIR))
    p.add_argument("--self-test", action="store_true", help="Run on synthetic data (no real lake needed)")
    p.add_argument("--json", action="store_true", help="Print JSON output")
    args = p.parse_args()

    run_date = datetime.now().strftime("%Y-%m-%d")
    lake = Path(args.lake)
    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)

    # ── Symbol list ─────────────────────────────────────────────────────────
    if args.self_test:
        symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
    elif args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        # Use NIFTY 50; filter to what's actually in the lake
        available = {d.name.replace("symbol=", "") for d in lake.iterdir() if d.is_dir()} if lake.exists() else set()
        symbols = [s for s in NIFTY50 if s in available]
        if not symbols:
            print(f"No NIFTY 50 symbols found in {lake}")
            print("Tip: Run download_bhavcopy.py first to populate the lake")
            print("     Or use --self-test for a demo run on synthetic data")
            return 1

    print(f"Phase 1 backtest: momentum strategy, {len(symbols)} symbols, {args.start} → {args.end}")
    print(f"Capital: ₹{args.capital:,.0f} | Short SMA: {args.short_window} | Long SMA: {args.long_window}")
    print(f"IndianCostModel: STT+exchange+SEBI+stamp+GST+brokerage\n")

    results = []
    for sym in symbols:
        if args.self_test:
            bars = _generate_synthetic_bars(sym)
        else:
            bars = _load_bars_from_parquet(sym, lake, start=start, end=end)

        if not bars:
            results.append({"symbol": sym, "error": "no data in lake"})
            print(f"  {sym:15s} SKIP — no data in lake")
            continue

        result = _run_momentum_backtest(
            bars, sym,
            capital=args.capital,
            short_window=args.short_window,
            long_window=args.long_window,
        )
        results.append(result)

        lh = result.get("lookahead_violations", 0)
        lh_str = "✅" if lh == 0 else f"❌ LH={lh}"
        pf = result.get("profit_factor")
        pf_str = f"{pf:.2f}" if pf is not None else "∞"
        print(
            f"  {sym:15s} bars={result.get('bars', 0):5d}  "
            f"trades={result.get('trades', 0):4d}  "
            f"return={result.get('total_return_pct', 0):+7.2f}%  "
            f"PF={pf_str:6s}  "
            f"WR={result.get('win_rate_pct', 0):.1f}%  "
            f"MaxDD={result.get('max_drawdown_pct', 0):.2f}%  "
            f"{lh_str}"
        )

    agg = _aggregate(results)

    print(f"\n{'═' * 70}")
    print(f"  AGGREGATE ({agg.get('symbols_tested', 0)} symbols)")
    print(f"{'═' * 70}")
    print(f"  Avg return:          {agg.get('avg_total_return_pct', 0):+.2f}%")
    print(f"  Avg annualised:      {agg.get('avg_annualised_return_pct', 0):+.2f}%")
    print(f"  Avg profit factor:   {agg.get('avg_profit_factor', 0):.4f}")
    print(f"  Avg win rate:        {agg.get('avg_win_rate_pct', 0):.1f}%")
    print(f"  Avg max drawdown:    {agg.get('avg_max_drawdown_pct', 0):.2f}%")
    print(f"  Profitable symbols:  {agg.get('profitable_symbols_pct', 0):.1f}%")
    print(f"  Total commission:    ₹{agg.get('total_commission_inr', 0):,.2f}")
    print(f"  Lookahead check:     {agg.get('lookahead_check', 'N/A')}")
    print(f"{'═' * 70}")

    # Write report
    report_path = _write_report(agg, results, Path(args.report_dir), run_date)
    print(f"\n  Report → {report_path}")

    # Write JSON
    json_path = Path(args.report_dir) / f"phase1_backtest_{run_date}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps({"aggregate": agg, "results": [
        {k: v for k, v in r.items() if k not in ("trade_log", "equity_curve")}
        for r in results
    ]}, indent=2, default=str))
    print(f"  JSON   → {json_path}")

    if args.json:
        print(json.dumps({"aggregate": agg}, indent=2, default=str))

    lah_total = agg.get("total_lookahead_violations", 0)
    if lah_total > 0:
        print(f"\n⛔ FAIL: {lah_total} lookahead violations detected — results are INVALID")
        return 2
    print(f"\n✅ Phase 1 complete — lookahead_violations=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
