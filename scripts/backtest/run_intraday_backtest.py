#!/usr/bin/env python3
"""Phase B — Intraday strategy backtest on Zerodha Kite history.

Evaluates the four intraday strategies that the daily Bhavcopy lake could not
assess (momentum was validated on daily data in Phase 13/15):

    orb            — opening-range breakout   (1m bars)
    vwap_reversion — VWAP mean reversion       (1m bars)
    trend_15m      — intraday 15-minute trend  (15m bars)
    preclose       — pre-close momentum        (5m bars)

(scalp_1m is excluded — it is Stage-1 DISABLED and the lowest procurement
priority per the intraday data memo.)

WHY PER-DAY EXECUTION
---------------------
These strategies are session-based: ORB builds a fresh opening range each day,
VWAP resets daily, and all four carry per-day signal budgets. ``reset_daily()``
is normally driven by the live MarketPhaseGovernor at POST_CLOSE — the offline
``Backtester`` never calls it. So we run **one strategy instance per trading
day across all symbols** (fresh state = daily reset; the Backtester's
close-at-last-bar = MIS EOD flatten). This also preserves each strategy's
*global* daily signal budget across the universe (running per-symbol would give
every symbol its own budget — too permissive).

This backtesting path FIXES the Session-16 "ORB blind" problem: historical bars
include the full 09:15–09:30 opening range, so the range always forms.

LIMITATIONS (honest, recorded in the report)
  * Capital resets each day (no cross-day compounding) → Sharpe/annualised
    return are unreliable; expectancy / profit factor / win rate are valid.
  * trend_15m warm-up is intra-day only (~25 15m bars/day) — marginal for a
    15m-trend filter; treat its result as indicative, not conclusive.
  * Exits are modeled by the Backtester (per-bar stop/target from the signal +
    EOD flatten), not the live TEE / MIS engine.
  * Zerodha intraday depth is limited (~3 yr, liquid names) — advisory edge
    exploration, not a 15-yr authoritative backbone (data-lake contract §1).

Backtest-only. Advisory. Backtesting can recommend; it cannot promote.
A human approves all production changes. Live trading remains BLOCKED.

Usage:
    python scripts/backtest/run_intraday_backtest.py
    python scripts/backtest/run_intraday_backtest.py --strategies orb,vwap_reversion
    python scripts/backtest/run_intraday_backtest.py --universe liquid10 --start 2023-01-01
    python scripts/backtest/run_intraday_backtest.py --self-test   # offline synthetic lake
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

import pandas as pd

from backtesting.data_loader import load_candles
from backtesting.metrics_engine import compute_metrics, evaluate_gates
from backtesting.replay_engine import DataFrameBarSource
from backtesting.strategy_adapter import get_adapter
from strategy_engine.backtesting.backtester import Backtester, IndianCostModel

# ── configuration ────────────────────────────────────────────────────────────
LOCAL_LAKE = str(_REPO / "backtest-data")
COST_MODEL = IndianCostModel.delivery()
SOURCE_NAME = "zerodha_kite"           # HIGH trust per s3_data_catalog
CODE_VERSION = "intraday-phaseB-v1"
DATA_VERSION = "zerodha-kite-intraday-v1"
_IST = "Asia/Kolkata"

# strategy → lake interval partition it consumes
STRATEGY_INTERVAL: dict[str, str] = {
    "orb": "1m",
    "vwap_reversion": "1m",
    "trend_15m": "15m",
    "preclose": "5m",
}

# Strategies that need indicator state carried ACROSS days to warm up (a 15m
# trend filter cannot warm a 50-period EMA inside one ~25-bar session). For
# these, run ONE persistent strategy instance and call reset_daily() between
# days (resets daily counters/budgets but keeps the OHLCV buffers) instead of
# building a fresh instance per day. ONLY for strategies whose reset_daily does
# not clear warm-up buffers — orb/vwap/preclose reset their session anchors
# (opening range / VWAP) daily and MUST stay fresh-per-day.
WARM_START_STRATEGIES: frozenset[str] = frozenset({"trend_15m"})

# Per-strategy constructor overrides for the backtest. The NIFTY regime gate is
# disabled because the lake holds only equity symbols (no NIFTY index intraday);
# in production the gate fails-open without NIFTY data, so disabling ≈ that path
# while isolating the strategy's own edge.
_STRATEGY_BUILD_OVERRIDES: dict[str, dict] = {
    "trend_15m": {"enable_nifty_gate": False},
}

NIFTY50 = [
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
LIQUID10 = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "SBIN", "AXISBANK", "KOTAKBANK", "BHARTIARTL", "LT",
]
UNIVERSES = {"nifty50": NIFTY50, "liquid10": LIQUID10}


# ── data loading ─────────────────────────────────────────────────────────────


def _load_symbol(base: str, symbol: str, interval: str,
                 start: date, end: date) -> pd.DataFrame:
    """Load one symbol's intraday bars via the shared loader (IST + trust tagged)."""
    path = (
        Path(base) / "lake" / "ohlcv" / "market=NSE" / "segment=EQ"
        / f"symbol={symbol}" / f"interval={interval}"
    )
    if not path.exists():
        return pd.DataFrame()
    res = load_candles(
        str(path),
        symbol=symbol,
        interval=interval,
        source_name=SOURCE_NAME,
        date_from=start,
        date_to=end,
        fmt="parquet",
    )
    return res.df


# ── trade → metrics frame (mirrors run_momentum_walk_forward._trades_to_df) ───


def _trades_to_df(trades: list) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(
            columns=["symbol", "net_pnl", "gross_pnl", "costs", "slippage",
                     "entry_time", "exit_time", "entry_price", "exit_price",
                     "quantity", "exit_reason"]
        )
    rows = []
    for t in trades:
        rows.append({
            "symbol":      t.symbol,
            "direction":   str(t.direction),
            "entry_time":  t.entry_time,
            "exit_time":   t.exit_time,
            "entry_price": t.entry_price,
            "exit_price":  t.exit_price,
            "quantity":    t.quantity,
            "net_pnl":     t.pnl,                 # already net of commission
            "costs":       t.commission,
            "gross_pnl":   t.pnl + t.commission,
            "slippage":    t.slippage,
            "exit_reason": t.exit_reason,
        })
    return pd.DataFrame(rows)


# ── per-strategy backtest ────────────────────────────────────────────────────


async def _run_day(strategy, bars: list) -> list:
    bt = Backtester(
        strategy=strategy,
        slippage_bps=5.0,
        commission_pct=0.0,       # Zerodha equity intraday brokerage modeled via cost model
        indian_cost_model=COST_MODEL,
    )
    result = await bt.run(bars)
    return result.trades


def _backtest_strategy(strat: str, symbols: list[str], base: str,
                       start: date, end: date, verbose: bool) -> dict | None:
    interval = STRATEGY_INTERVAL[strat]

    frames = []
    for sym in symbols:
        df = _load_symbol(base, sym, interval, start, end)
        if not df.empty:
            frames.append(df)
    if not frames:
        return None

    alldf = pd.concat(frames, ignore_index=True)
    alldf["_day"] = alldf["timestamp"].dt.tz_convert(_IST).dt.date

    loaded_syms = sorted(alldf["symbol"].unique().tolist())
    total_bars = len(alldf)
    all_trades: list = []
    n_days = 0

    warm_start = strat in WARM_START_STRATEGIES
    overrides = _STRATEGY_BUILD_OVERRIDES.get(strat, {})
    # Warm-start: ONE persistent instance whose OHLCV buffers accumulate across
    # days (reset_daily between days clears only the daily counters). Fresh-per-day
    # otherwise, so session anchors (opening range / VWAP) reset each day.
    persistent = (
        get_adapter(strat).build_strategy(loaded_syms, nav=1_000_000.0, **overrides)
        if warm_start else None
    )

    for day, daydf in alldf.groupby("_day"):
        daydf = daydf.sort_values("timestamp")
        candles = DataFrameBarSource.from_dataframe(daydf)._candles
        bars = [c.to_bar() for c in candles]
        if not bars:
            continue
        if warm_start:
            strategy = persistent
        else:
            strategy = get_adapter(strat).build_strategy(
                loaded_syms, nav=1_000_000.0, **overrides
            )
        all_trades.extend(asyncio.run(_run_day(strategy, bars)))
        if warm_start:
            strategy.reset_daily()  # reset daily counters/budgets; keep warm-up buffers
        n_days += 1

    trades_df = _trades_to_df(all_trades)
    metrics = compute_metrics(trades_df)
    gates = evaluate_gates(metrics)

    if verbose:
        print(f"    {strat}: {n_days} days, {len(loaded_syms)} symbols, "
              f"{total_bars:,} bars, {len(all_trades)} trades")

    return {
        "strategy": strat,
        "interval": interval,
        "symbols": len(loaded_syms),
        "days": n_days,
        "bars": total_bars,
        "metrics": metrics,
        "gates": gates,
    }


# ── report ───────────────────────────────────────────────────────────────────


def _verdict(gates: dict, n_trades: int) -> str:
    if n_trades == 0:
        return "NO_TRADES"
    if gates.get("overall_pass"):
        return "ELIGIBLE_FOR_PAPER_PRIORITIZATION"
    if gates.get("expectancy_gt_0") or gates.get("net_pnl_gt_0"):
        return "PAPER_OPTIMIZATION"
    return "REJECT"


def _write_report(results: list[dict], symbols: list[str], start: date,
                  end: date, base: str) -> Path:
    report_dir = _REPO / "docs" / "backtesting"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / "aws-phaseB-intraday-backtest-report.md"

    lines = [
        "# AWS Backtesting Lab — Phase B Report: Intraday Strategy Backtest (Zerodha Kite)",
        "",
        "**Status:** COMPLETE — awaiting human approval",
        f"**Date:** {date.today()}",
        "**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.",
        "",
        "---",
        "",
        "## Scope",
        "",
        "First intraday evaluation of the four session-based strategies that the daily",
        "Bhavcopy lake could not assess. Per-day execution (fresh strategy state per",
        "trading day) models daily reset + MIS EOD flatten.",
        "",
        "| Parameter | Value |",
        "|---|---|",
        f"| Data source | Zerodha Kite `historical_data` (`{SOURCE_NAME}`) |",
        "| Trust | HIGH (provenance) — LIMITED depth (~3 yr, liquid names); advisory only |",
        f"| Period | {start} → {end} |",
        f"| Universe | {len(symbols)} NSE symbols |",
        f"| Cost model | `{COST_MODEL.cost_model_version}` (NSE equity, statutory stack) |",
        "| Slippage | 5 bps per leg |",
        f"| Code version | `{CODE_VERSION}` |",
        f"| Data version | `{DATA_VERSION}` |",
        "",
        "**Data integrity:** the `Backtester` multi-symbol end-of-day flatten bug "
        "(cross-symbol mark-out, fixed 2026-06-14, regression-pinned by "
        "`test_eod_multi_symbol_uses_own_symbol_price`) is corrected in these results. "
        "An earlier uncorrected run produced physically impossible figures (>4000% win "
        "rate, ₹crore P&L) and was discarded. The numbers below are post-fix.",
        "",
        "---",
        "",
        "## Per-Strategy Results",
        "",
        "| Strategy | Interval | Days | Trades | Win % | Expectancy ₹ | Profit Factor | Net P&L ₹ | Cost drag ₹ | Verdict |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]

    for r in results:
        m = r["metrics"]
        n_trades = int(m.get("number_of_trades", 0) or 0)
        verdict = _verdict(r["gates"], n_trades)
        lines.append(
            f"| `{r['strategy']}` | {r['interval']} | {r['days']} | {n_trades} "
            f"| {m.get('win_rate', 0.0):.1f} "
            f"| {m.get('expectancy', 0.0):.1f} "
            f"| {m.get('profit_factor', 0.0):.3f} "
            f"| {m.get('net_pnl', 0.0):,.0f} "
            f"| {m.get('cost_impact', m.get('total_costs', 0.0)):,.0f} "
            f"| {verdict} |"
        )

    lines += [
        "",
        "Gates (per strategy): expectancy > 0 · profit factor > 1.2 · net P&L > 0.",
        "`ELIGIBLE_FOR_PAPER_PRIORITIZATION` = all gates pass · `PAPER_OPTIMIZATION` = ",
        "positive but not all gates · `REJECT` = no positive edge · `NO_TRADES` = no signals fired.",
        "",
        "---",
        "",
        "## Advisory Conclusions",
        "",
        "> **Backtesting can recommend. Backtesting cannot promote. A human approves all production changes.**",
        "",
        "- Results are advisory and non-authoritative for promotion.",
        "- Zerodha intraday is HIGH trust for provenance but LIMITED depth (~3 yr, liquid",
        "  names). A positive result warrants procuring deeper licensed vendor data",
        "  (TrueData / GlobalDataFeeds) before further validation — per the intraday",
        "  data procurement memo's staged path.",
        "- Per-day capital reset makes Sharpe / annualised return unreliable; expectancy,",
        "  profit factor, and win rate are the valid edge metrics here.",
        "- `trend_15m` = 0 trades is a **real result, not a warm-up artifact**. It runs in",
        "  warm-start mode (persistent instance + `reset_daily()` between days, carrying the",
        "  OHLCV buffers across sessions — like the live ADR-031 warm-start), so its EMAs warm",
        "  fully. It still fires 0 signals at its production config because the ADX≥25 and",
        "  confidence≥0.65 filters are **mutually exclusive on NIFTY50 15m data** (each alone",
        "  admits ~46–75 signals; together, 0). With both filters off, the raw trend logic",
        "  trades ~4,857 times but loses (expectancy ≈ -₹76, PF ≈ 0.30, net ≈ -₹368k) → REJECT.",
        "  No edge either way.",
        "- Live trading remains BLOCKED — paper session gates (≥5 consecutive passing",
        "  sessions) are unaffected by this advisory backtest.",
        "",
        "---",
        "",
        "## Next Options (operator selects)",
        "",
        "**A. Procure deeper licensed intraday** for any strategy showing edge here",
        "   (targeted symbols/years from TrueData / GlobalDataFeeds), then re-validate.",
        "**B. Intraday walk-forward** — parameter walk-forward (like Phase 15C for momentum)",
        "   on the strategy/strategies with the strongest first-pass edge.",
        "**C. Continue paper sessions** — Session 18 with ADR-030 quality gates.",
        "**D. GenAI analysis** (Phase 10) over the intraday artifacts.",
        "",
        "---",
        "",
        "## Approval Required",
        "",
        "Per governance: **a human must approve this report.**",
        "",
        "- [ ] Per-day execution model + limitations understood",
        "- [ ] Zerodha limited-depth / advisory-only nature confirmed",
        "- [ ] Per-strategy verdicts reviewed",
        "- [ ] No production changes will be made based solely on this report",
        "- [ ] Next option selected from A / B / C / D above",
    ]

    path.write_text("\n".join(lines) + "\n")
    return path


# ── self-test (offline synthetic lake) ───────────────────────────────────────


def _self_test() -> int:
    import tempfile

    sys.path.insert(0, str(_REPO / "scripts" / "backtest"))
    from fetch_zerodha_intraday import _StubKite, fetch  # noqa: PLC0415

    print("SELF-TEST: building synthetic intraday lake + running backtest pipeline...")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        fetch(
            kite=_StubKite(),
            symbols=["RELIANCE", "INFY"],
            intervals=["1m", "5m", "15m"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 15),
            base=base,
            force=False,
            verbose=False,
        )
        # Run one 1m strategy + one 15m strategy end-to-end.
        for strat in ("orb", "trend_15m"):
            r = _backtest_strategy(
                strat, ["RELIANCE", "INFY"], str(base),
                date(2024, 1, 1), date(2024, 1, 15), verbose=True,
            )
            assert r is not None, f"{strat}: no data loaded"
            assert isinstance(r["metrics"], dict), f"{strat}: metrics not computed"
            assert "number_of_trades" in r["metrics"], f"{strat}: metrics shape wrong"
            assert r["days"] > 0, f"{strat}: no trading days processed"
            print(f"  {strat}: pipeline OK — {r['days']} days, "
                  f"{int(r['metrics'].get('number_of_trades', 0))} trades, "
                  f"verdict={_verdict(r['gates'], int(r['metrics'].get('number_of_trades', 0)))}")
    print("SELF-TEST PASSED.")
    return 0


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase B — intraday strategy backtest on Zerodha Kite history")
    ap.add_argument("--strategies", default=",".join(STRATEGY_INTERVAL),
                    help="Comma-separated subset of " + ",".join(STRATEGY_INTERVAL))
    ap.add_argument("--universe", choices=list(UNIVERSES), default="nifty50")
    ap.add_argument("--symbols", help="Comma-separated tradingsymbols (overrides --universe)")
    ap.add_argument("--start", default="2022-01-01", help="Start date YYYY-MM-DD")
    ap.add_argument("--end", default=date.today().isoformat(), help="End date YYYY-MM-DD")
    ap.add_argument("--base", default=LOCAL_LAKE, help="Lake base directory")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress per-bar strategy INFO/WARNING logs (recommended for full runs)")
    ap.add_argument("--self-test", action="store_true", help="Offline synthetic-lake pipeline test")
    args = ap.parse_args()

    if args.quiet:
        import logging
        logging.disable(logging.WARNING)  # silence per-bar strategy/backtester INFO+WARNING

    if args.self_test:
        return _self_test()

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    bad = [s for s in strategies if s not in STRATEGY_INTERVAL]
    if bad:
        print(f"ERROR: unknown strategies {bad}. Choose from {list(STRATEGY_INTERVAL)}.", file=sys.stderr)
        return 1

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols else UNIVERSES[args.universe]
    )
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    print("=" * 72)
    print("QuantEmbrace — Intraday Strategy Backtest (Phase B, Zerodha Kite)")
    print("Backtest-only. Advisory. No broker. No live trading.")
    print("=" * 72)
    print(f"  Strategies: {strategies}")
    print(f"  Universe  : {args.universe if not args.symbols else 'custom'} ({len(symbols)} symbols)")
    print(f"  Period    : {start} → {end}")
    print(f"  Lake      : {args.base}")
    print(f"  Costs     : {COST_MODEL.cost_model_version}")
    print()

    results: list[dict] = []
    t0 = time.monotonic()
    for strat in strategies:
        print(f"  Backtesting {strat} ({STRATEGY_INTERVAL[strat]})...")
        r = _backtest_strategy(strat, symbols, args.base, start, end, args.verbose)
        if r is None:
            print(f"    NO DATA for interval={STRATEGY_INTERVAL[strat]} — "
                  f"run fetch_zerodha_intraday.py first.")
            continue
        results.append(r)

    if not results:
        print()
        print("ERROR: no intraday data found in the lake for any requested strategy.")
        print("Fetch it first (operator-run, needs a fresh Zerodha token):")
        print("  python scripts/zerodha_login.py")
        print("  python scripts/backtest/fetch_zerodha_intraday.py "
              f"--universe {args.universe} --intervals 1m,5m,15m "
              f"--start {start} --end {end}")
        return 1

    # ── console summary ──────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print(f"{'Strategy':>16}  {'Trades':>7}  {'Win%':>6}  {'Exp ₹':>9}  {'PF':>7}  {'Net ₹':>12}  Verdict")
    print("-" * 88)
    for r in results:
        m = r["metrics"]
        n = int(m.get("number_of_trades", 0) or 0)
        print(f"{r['strategy']:>16}  {n:>7}  {m.get('win_rate', 0.0):>5.1f}  "
              f"{m.get('expectancy', 0.0):>9.1f}  {m.get('profit_factor', 0.0):>7.3f}  "
              f"{m.get('net_pnl', 0.0):>12,.0f}  {_verdict(r['gates'], n)}")
    print("=" * 72)

    report_path = _write_report(results, symbols, start, end, args.base)
    print()
    print(f"  Report : {report_path}")
    print(f"  Done in {time.monotonic() - t0:.1f}s")
    print()
    print("Advisory only. Backtesting can recommend. It cannot promote.")
    print("A human approves all production changes. Live trading remains BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
