#!/usr/bin/env python3
"""Phase 1 audit — retire-candidate intraday strategies, broken down BY YEAR and BY REGIME.

Produces the evidence for the formal retirement of the four intraday strategies
(orb / vwap_reversion / trend_15m / preclose). Reuses the *validated* per-day backtest
(run_intraday_backtest.py) — same data, costs, warm-start handling — but collects every
trade so it can cut metrics by calendar year and by market regime, and builds a fixed-capital
DAILY P&L series for the risk metrics the per-day runner deliberately omits.

Honest metric scope (per the runner's own note: per-day capital reset makes compounded
annualised return unreliable):
  * Valid edge metrics: trades, win%, profit factor, expectancy, gross/net P&L.
  * Daily-series metrics on FIXED ₹10L capital (defensible for a flat-by-EOD intraday book):
    daily-return Sharpe & Sortino (×√252), max drawdown on cumulative P&L + its duration,
    period net return, turnover (daily traded notional / capital), avg trades/day.
  * Overnight exposure = 0 by construction (MIS flat by EOD) — classic exposure/beta N/A.

Regime = equal-weight NIFTY50 daily proxy vs its 50-day SMA (UPTREND / DOWNTREND), no
lookahead (SMA uses only trailing closes). Each trade is bucketed by its day's regime.

Backtest-only. Advisory. Live trading remains BLOCKED.

Usage:
    python scripts/backtest/phase1_strategy_audit.py
    python scripts/backtest/phase1_strategy_audit.py --strategies orb,preclose
    python scripts/backtest/phase1_strategy_audit.py --self-test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))
sys.path.insert(0, str(_REPO / "scripts" / "backtest"))

import numpy as np
import pandas as pd

from backtesting.metrics_engine import compute_metrics  # noqa: E402
from backtesting.replay_engine import DataFrameBarSource  # noqa: E402
from backtesting.strategy_adapter import get_adapter  # noqa: E402
from strategy_engine.backtesting.backtester import Backtester, IndianCostModel  # noqa: E402
from run_intraday_backtest import (  # noqa: E402
    COST_MODEL,
    NIFTY50,
    STRATEGY_INTERVAL,
    WARM_START_STRATEGIES,
    _IST,
    _STRATEGY_BUILD_OVERRIDES,
    _load_symbol,
    _trades_to_df,
)

CAPITAL = 1_000_000.0
# MIS intraday statutory stack (~0.035% round-trip) is correct for these EOD-flat strategies.
# Phase B used IndianCostModel.delivery() (~0.222%), ~6x too harsh for intraday — corrected here.
AUDIT_COST = IndianCostModel.intraday()
RETIRE = ["orb", "vwap_reversion", "trend_15m", "preclose"]
EOD = _REPO / "backtest-data/lake/ohlcv/market=NSE/segment=EQ"
REPORT = _REPO / "docs/backtesting/phase1-strategy-audit-report.md"
JSON_OUT = _REPO / "backtest-data/phase1_audit.json"


# ── trade collection (reuses the validated per-day loop) ──────────────────────


def _collect_trades(strat: str, symbols: list[str], base: str, start: date, end: date) -> pd.DataFrame:
    interval = STRATEGY_INTERVAL[strat]
    frames = [df for s in symbols if not (df := _load_symbol(base, s, interval, start, end)).empty]
    if not frames:
        return pd.DataFrame()
    alldf = pd.concat(frames, ignore_index=True)
    alldf["_day"] = alldf["timestamp"].dt.tz_convert(_IST).dt.date
    loaded = sorted(alldf["symbol"].unique().tolist())
    warm = strat in WARM_START_STRATEGIES
    ov = _STRATEGY_BUILD_OVERRIDES.get(strat, {})
    persistent = get_adapter(strat).build_strategy(loaded, nav=CAPITAL, **ov) if warm else None

    trades: list = []
    for _, daydf in alldf.groupby("_day"):
        daydf = daydf.sort_values("timestamp")
        bars = [c.to_bar() for c in DataFrameBarSource.from_dataframe(daydf)._candles]
        if not bars:
            continue
        strategy = persistent if warm else get_adapter(strat).build_strategy(loaded, nav=CAPITAL, **ov)
        bt = Backtester(strategy=strategy, slippage_bps=5.0, commission_pct=0.0, indian_cost_model=AUDIT_COST)
        trades.extend(asyncio.run(bt.run(bars)).trades)
        if warm:
            strategy.reset_daily()
    return _trades_to_df(trades)


# ── regime proxy (equal-weight NIFTY50 vs 50d SMA; no lookahead) ──────────────


def _regime_by_day() -> dict:
    closes = {}
    for s in NIFTY50:
        fs = sorted((EOD / f"symbol={s}/interval=1d").glob("year=*/part-0.parquet"))
        if not fs:
            continue
        d = pd.concat([pd.read_parquet(f, columns=["timestamp", "close"]) for f in fs])
        d["date"] = pd.to_datetime(d["timestamp"]).dt.tz_convert(_IST).dt.normalize()
        closes[s] = d.set_index("date")["close"].sort_index()
    px = pd.DataFrame(closes).sort_index()
    idx = (1.0 + px.pct_change().mean(axis=1).fillna(0.0)).cumprod()
    sma = idx.rolling(50).mean()
    reg = np.where(idx > sma, "UPTREND", "DOWNTREND")
    out = {}
    for dt, r, smav in zip(idx.index, reg, sma):
        out[dt.date()] = ("UNKNOWN" if pd.isna(smav) else r)
    return out


# ── metrics ───────────────────────────────────────────────────────────────────


def _max_run(mask) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def _trade_block(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "expectancy": 0.0,
                "net_pnl": 0.0, "gross_pnl": 0.0}
    m = compute_metrics(df)
    return {
        "trades": int(m.get("number_of_trades", 0) or 0),
        "win_rate": round(float(m.get("win_rate", 0.0)), 1),
        "profit_factor": round(float(m.get("profit_factor", 0.0)), 3),
        "expectancy": round(float(m.get("expectancy", 0.0)), 1),
        "net_pnl": round(float(m.get("net_pnl", 0.0)), 0),
        "gross_pnl": round(float(df["gross_pnl"].sum()), 0),
    }


def _daily_block(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"days": 0, "sharpe": 0.0, "sortino": 0.0, "max_dd_pct": 0.0,
                "max_dd_days": 0, "net_return_pct": 0.0, "turnover_x_per_day": 0.0,
                "avg_trades_per_day": 0.0}
    d = df.copy()
    d["day"] = pd.to_datetime(d["exit_time"]).dt.tz_convert(_IST).dt.normalize()
    daily = d.groupby("day")["net_pnl"].sum().sort_index()
    r = daily / CAPITAL
    sharpe = float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0
    dn = r[r < 0]
    sortino = float(r.mean() / dn.std() * np.sqrt(252)) if len(dn) > 1 and dn.std() > 0 else 0.0
    eq = CAPITAL + daily.cumsum()
    peak = eq.cummax()
    dd = float((eq / peak - 1.0).min())
    dd_days = _max_run((eq < peak).tolist())
    d["notional"] = d["entry_price"] * d["quantity"]
    turn = float((d.groupby("day")["notional"].sum() / CAPITAL).mean())
    return {
        "days": int(daily.size),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "max_dd_pct": round(dd * 100, 1),
        "max_dd_days": int(dd_days),
        "net_return_pct": round(float(daily.sum() / CAPITAL * 100), 2),
        "turnover_x_per_day": round(turn, 2),
        "avg_trades_per_day": round(len(d) / max(daily.size, 1), 1),
    }


def _audit_strategy(strat: str, df: pd.DataFrame, regime: dict) -> dict:
    res = {"overall": {**_trade_block(df), **_daily_block(df)}, "by_year": {}, "by_regime": {}}
    if df is None or df.empty:
        return res
    d = df.copy()
    d["year"] = pd.to_datetime(d["entry_time"]).dt.tz_convert(_IST).dt.year
    d["rday"] = pd.to_datetime(d["entry_time"]).dt.tz_convert(_IST).dt.date
    d["regime"] = d["rday"].map(lambda x: regime.get(x, "UNKNOWN"))
    for y, g in d.groupby("year"):
        res["by_year"][int(y)] = {**_trade_block(g), "sharpe": _daily_block(g)["sharpe"]}
    for rg, g in d.groupby("regime"):
        res["by_regime"][str(rg)] = _trade_block(g)
    return res


# ── report ────────────────────────────────────────────────────────────────────


def _verdict(o: dict) -> str:
    if o["trades"] == 0:
        return "REJECT (0 trades at production config)"
    if o["expectancy"] > 0 and o["profit_factor"] > 1.2 and o["net_pnl"] > 0:
        return "PASS"
    return "REJECT"


def _write_report(audit: dict, start: date, end: date) -> None:
    L = [
        "# Phase 1 Audit — Intraday Strategies by Year and Regime (Retire Candidates)",
        "",
        "**Status:** COMPLETE — advisory. Live trading remains BLOCKED.",
        f"**Date:** {date.today()}   **Period:** {start} → {end} (Zerodha Kite intraday, NIFTY50)",
        "**Capital basis:** fixed ₹10L (no cross-day compounding). **Costs:** NSE **intraday/MIS** "
        "statutory stack (~0.035% round-trip — correct for EOD-flat strategies; Phase B used "
        "delivery ~0.222%, ~6× harsher) + 5 bps/leg slippage.",
        "",
        "> Per-day capital reset makes compounded annualised return unreliable; the valid edge",
        "> metrics are trades/win%/PF/expectancy/net. Sharpe/Sortino/DD below are on the",
        "> fixed-capital DAILY P&L series. Overnight exposure = 0 (MIS flat by EOD).",
        "",
        "## Overall (full period)",
        "",
        "| Strategy | Trades | Win% | PF | Exp ₹ | Net ₹ | Sharpe(d) | Sortino(d) | MaxDD | DD days | Turn×/day | Verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for s in RETIRE:
        if s not in audit:
            continue
        o = audit[s]["overall"]
        L.append(
            f"| `{s}` | {o['trades']} | {o['win_rate']} | {o['profit_factor']} | {o['expectancy']} "
            f"| {o['net_pnl']:,.0f} | {o['sharpe']} | {o['sortino']} | {o['max_dd_pct']}% "
            f"| {o['max_dd_days']} | {o['turnover_x_per_day']} | {_verdict(o)} |"
        )
    L += ["", "## By calendar year (PF / expectancy / net ₹ / trades / daily-Sharpe)", ""]
    for s in RETIRE:
        if s not in audit or not audit[s]["by_year"]:
            continue
        L += [f"### `{s}`", "", "| Year | Trades | Win% | PF | Exp ₹ | Net ₹ | Sharpe(d) |",
              "|---|---:|---:|---:|---:|---:|---:|"]
        for y in sorted(audit[s]["by_year"]):
            b = audit[s]["by_year"][y]
            L.append(f"| {y} | {b['trades']} | {b['win_rate']} | {b['profit_factor']} | "
                     f"{b['expectancy']} | {b['net_pnl']:,.0f} | {b['sharpe']} |")
        L.append("")
    L += ["## By market regime (equal-weight NIFTY50 vs 50d SMA)", ""]
    for s in RETIRE:
        if s not in audit or not audit[s]["by_regime"]:
            continue
        L += [f"### `{s}`", "", "| Regime | Trades | Win% | PF | Exp ₹ | Net ₹ |",
              "|---|---:|---:|---:|---:|---:|"]
        for rg in ("UPTREND", "DOWNTREND", "UNKNOWN"):
            if rg in audit[s]["by_regime"]:
                b = audit[s]["by_regime"][rg]
                L.append(f"| {rg} | {b['trades']} | {b['win_rate']} | {b['profit_factor']} | "
                         f"{b['expectancy']} | {b['net_pnl']:,.0f} |")
        L.append("")
    L += [
        "## Read",
        "",
        "- The diagnostic question: do these lose **universally** (costs > edge) or only in a",
        "  particular year/regime (regime-dependence / alpha-decay)? Compare the by-year and",
        "  by-regime PF columns — PF < 1 in *every* cell ⇒ structural cost-bleed, not bad luck.",
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production changes.",
    ]
    REPORT.write_text("\n".join(L) + "\n")


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    print("SELF-TEST: metric helpers on a synthetic trades frame...")
    rng = np.random.default_rng(2)
    n = 400
    ts = pd.date_range("2023-01-02 10:00", periods=n, freq="3h", tz=_IST)
    pnl = rng.normal(-30, 200, n)  # slight negative drift, like a cost-bled strategy
    df = pd.DataFrame({
        "symbol": ["X"] * n, "direction": ["BUY"] * n,
        "entry_time": ts, "exit_time": ts + pd.Timedelta("30min"),
        "entry_price": 100.0, "exit_price": 100.0 + pnl / 10, "quantity": 10,
        "net_pnl": pnl, "costs": 5.0, "gross_pnl": pnl + 5.0, "slippage": 1.0,
        "exit_reason": "eod",
    })
    tb, db = _trade_block(df), _daily_block(df)
    assert tb["trades"] == n and db["days"] > 0
    assert "sharpe" in db and "max_dd_pct" in db
    a = _audit_strategy("orb", df, {})
    assert a["by_year"] and "overall" in a
    print(f"  OK — trades={tb['trades']} pf={tb['profit_factor']} sharpe={db['sharpe']} "
          f"maxDD={db['max_dd_pct']}% years={list(a['by_year'])}")
    print("SELF-TEST PASSED.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 1 intraday retirement audit (by year + regime)")
    ap.add_argument("--strategies", default=",".join(RETIRE))
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--base", default=str(_REPO / "backtest-data"))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    import logging
    logging.disable(logging.WARNING)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    print("=" * 76)
    print("QuantEmbrace — Phase 1 Intraday Retirement Audit (by year + regime)")
    print("Advisory. No broker. Live trading BLOCKED.")
    print("=" * 76)
    print(f"  Strategies: {strategies}  Period: {start} → {end}")
    print("  Building regime proxy (equal-weight NIFTY50 vs 50d SMA)...")
    regime = _regime_by_day()
    print(f"  Regime days classified: {len(regime)}")

    audit: dict = {}
    for s in strategies:
        print(f"  Auditing {s} ({STRATEGY_INTERVAL[s]})...", flush=True)
        df = _collect_trades(s, NIFTY50, args.base, start, end)
        audit[s] = _audit_strategy(s, df, regime)
        o = audit[s]["overall"]
        print(f"    {s}: {o['trades']} trades, PF {o['profit_factor']}, "
              f"net ₹{o['net_pnl']:,.0f}, Sharpe(d) {o['sharpe']}, verdict {_verdict(o)}")

    JSON_OUT.write_text(json.dumps(audit, indent=2, default=str))
    _write_report(audit, start, end)
    print(f"\n  JSON  : {JSON_OUT}")
    print(f"  Report: {REPORT}")
    print("  Advisory only. Live trading remains BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
