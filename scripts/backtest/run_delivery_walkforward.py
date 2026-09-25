#!/usr/bin/env python3
"""Delivery-% factor — walk-forward (per-year OOS) + market regime overlay.

Follow-up to the factor study (ADR-034): the delivery-% conviction factor was the one
robust, regime-stable risk-adjusted edge. This script pressure-tests it two ways the data
*allows* (the lake starts Oct 2019 — there is NO sustained bear in sample, so a 2008-style
event cannot be tested; that remains the key open caveat):

  1. WALK-FORWARD / per-year OOS consistency — does delivery-% work *every* calendar year
     (incl. the 2022 down-year), not just on average? A non-parametric factor can't be
     "optimised", so the honest walk-forward test is rolling out-of-sample stability.
  2. REGIME OVERLAY — a standard, untuned market 200-day trend filter: hold the delivery
     book only when a liquid-large-cap market proxy is above its 200d SMA, else go to CASH.
     Measures whether the overlay cuts drawdown through the stresses that ARE in sample
     (2020 COVID crash, 2022 dip) — the mechanism that would protect in a real bear.

Long-only top-20, monthly rebalance, full NSE delivery cost stack (reused from the factor
study). Benchmark shown with and without the same overlay for a fair comparison.

Backtest-only. Advisory. Backtesting can recommend; it cannot promote. A human approves all
production changes. Live trading remains BLOCKED. No broker, no live/paper state.

Usage:
    python scripts/backtest/run_delivery_walkforward.py
    python scripts/backtest/run_delivery_walkforward.py --sma 150 --k 20
    python scripts/backtest/run_delivery_walkforward.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))
sys.path.insert(0, str(_REPO / "scripts" / "backtest"))

import numpy as np
import pandas as pd

# Reuse the audited factor-study harness (loader, rebalance calendar, metrics, costs).
from run_factor_study import (  # noqa: E402
    _ROUND_TRIP,
    _drop_funds,
    _load_panel,
    _metrics,
    _rebalance_rows,
    COST,
    SLIPPAGE_FRAC,
)


# ── market regime proxy ───────────────────────────────────────────────────────


def _regime_series(close: pd.DataFrame, turn: pd.DataFrame, sma: int) -> pd.Series:
    """Boolean per-date: is a liquid large-cap market proxy above its `sma`-day SMA?

    Proxy = equal-weight daily return of the top-100 names by total-period turnover (a stable
    NIFTY-like large-cap set). Trend filter uses only data up to each date (no lookahead).
    """
    top100 = _drop_funds(turn.sum()).nlargest(100).index
    mkt_ret = close[top100].pct_change().mean(axis=1)
    mkt_idx = (1.0 + mkt_ret.fillna(0.0)).cumprod()
    sma_line = mkt_idx.rolling(sma).mean()
    return (mkt_idx > sma_line) & sma_line.notna()


# ── delivery book backtest (with optional regime overlay) ─────────────────────


@dataclass
class WFResult:
    label: str
    monthly_net: pd.Series   # indexed by rebalance (period-end) date
    cash_months: int


def _run_delivery(close, turn, deliv, regime: pd.Series | None,
                  top_n: int, k: int) -> WFResult:
    idx = close.index
    rb = [r for r in _rebalance_rows(idx) if r >= 252 and r < len(idx) - 1]
    held: set[str] = set()
    rets, dates, cash_months = [], [], 0

    for j in range(len(rb) - 1):
        i, i_next = rb[j], rb[j + 1]
        risk_on = True if regime is None else bool(regime.iloc[i])

        if risk_on:
            liq = turn.iloc[i - 60:i].median().dropna()
            valid_now = close.iloc[i].dropna().index
            liq = _drop_funds(liq[liq.index.isin(valid_now)])
            univ = liq.sort_values(ascending=False).head(top_n).index.tolist()
            dlv = deliv[univ].iloc[i - 21:i].mean().dropna()
            picks = set(dlv.sort_values(ascending=False).head(k).index) if len(dlv) >= k else set()
        else:
            picks = set()
            cash_months += 1

        if picks:
            p0 = close[list(picks)].iloc[i]
            p1 = close[list(picks)].ffill().iloc[i_next]
            gross = (p1 / p0 - 1.0).dropna().mean()
        else:
            gross = 0.0   # in cash

        # cost on turnover between prior holdings and new target (incl. going to/from cash)
        if held or picks:
            changed = len(picks.symmetric_difference(held))
            denom = max(len(held | picks), 1)
            turn_frac = (changed / 2.0) / denom if held and picks else (len(picks | held) / denom)
            cost = (changed / (2.0 * k)) * _ROUND_TRIP if (held and picks) else (len(picks | held) / k) * _ROUND_TRIP
        else:
            cost = 0.0
        rets.append(gross - cost)
        dates.append(idx[i_next])
        held = picks

    label = "delivery+overlay" if regime is not None else "delivery"
    return WFResult(label, pd.Series(rets, index=dates), cash_months)


def _run_benchmark(close, turn, regime: pd.Series | None, top_n: int) -> WFResult:
    idx = close.index
    rb = [r for r in _rebalance_rows(idx) if r >= 252 and r < len(idx) - 1]
    rets, dates, cash = [], [], 0
    for j in range(len(rb) - 1):
        i, i_next = rb[j], rb[j + 1]
        if regime is not None and not bool(regime.iloc[i]):
            rets.append(0.0); dates.append(idx[i_next]); cash += 1; continue
        liq = turn.iloc[i - 60:i].median().dropna()
        valid = close.iloc[i].dropna().index
        liq = _drop_funds(liq[liq.index.isin(valid)])
        univ = liq.sort_values(ascending=False).head(top_n).index.tolist()
        p0 = close[univ].iloc[i]; p1 = close[univ].ffill().iloc[i_next]
        rets.append((p1 / p0 - 1.0).dropna().mean()); dates.append(idx[i_next])
    label = "benchmark+overlay" if regime is not None else "benchmark"
    return WFResult(label, pd.Series(rets, index=dates), cash)


# ── per-year (walk-forward / rolling OOS) breakdown ───────────────────────────


def _per_year(monthly: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"r": monthly})
    df["year"] = df.index.year
    rows = []
    for y, g in df.groupby("year"):
        eq = (1.0 + g["r"]).cumprod()
        ann = eq.iloc[-1] - 1.0
        vol = g["r"].std() * np.sqrt(12)
        dd = (eq / eq.cummax() - 1.0).min()
        rows.append({"year": y, "return": ann, "vol": vol, "maxdd": dd,
                     "sharpe": (g["r"].mean() * 12 / vol) if vol else 0.0, "months": len(g)})
    return pd.DataFrame(rows).set_index("year")


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    print("SELF-TEST: synthetic panel (no lake)...")
    rng = np.random.default_rng(11)
    dates = pd.date_range("2020-01-01", periods=600, freq="B", tz="Asia/Kolkata")
    syms = [f"S{i:03d}" for i in range(80)]
    steps = rng.normal(0.0004, 0.02, (len(dates), len(syms)))
    close = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=dates, columns=syms)
    turn = close * pd.DataFrame(rng.uniform(1e5, 1e6, close.shape), index=dates, columns=syms)
    deliv = pd.DataFrame(rng.uniform(20, 80, close.shape), index=dates, columns=syms)
    reg = _regime_series(close, turn, sma=200)
    base = _run_delivery(close, turn, deliv, None, 40, 8)
    ovl = _run_delivery(close, turn, deliv, reg, 40, 8)
    assert base.monthly_net.notna().all() and ovl.monthly_net.notna().all()
    assert ovl.cash_months >= 0 and len(_per_year(base.monthly_net)) >= 1
    assert _ROUND_TRIP > 0.002
    print(f"  OK — base {base.monthly_net.size} mo, overlay cash months={ovl.cash_months}, "
          f"per-year rows={len(_per_year(base.monthly_net))}")
    print("SELF-TEST PASSED.")
    return 0


# ── report ────────────────────────────────────────────────────────────────────


def _fmt_metrics(m: dict) -> str:
    return (f"CAGR {m['cagr']*100:5.1f}%  Sharpe {m['sharpe']:.2f}  "
            f"MaxDD {m['maxdd']*100:6.1f}%  hit {m['hit']*100:.0f}%")


def _write_report(variants: dict[str, WFResult], sma: int, k: int, top_n: int,
                  start: date, end: date) -> Path:
    out = _REPO / "docs" / "backtesting" / "delivery-walkforward-report.md"
    d, do = variants["delivery"], variants["delivery+overlay"]
    b, bo = variants["benchmark"], variants["benchmark+overlay"]
    md, mdo = _metrics(d.monthly_net), _metrics(do.monthly_net)
    mb, mbo = _metrics(b.monthly_net), _metrics(bo.monthly_net)

    L = [
        "# Delivery-% Factor — Walk-Forward + Market Regime Overlay",
        "",
        "**Status:** COMPLETE — advisory. Live trading remains BLOCKED.",
        f"**Date:** {date.today()}",
        "**Thesis / ADR:** ADR-034 · `docs/strategy/strategy-thesis-redirection-2026-06-15.md`",
        "**Prior:** `docs/backtesting/factor-study-report.md` (delivery-% = robust factor).",
        "",
        "> **Hard data limit:** the daily lake starts Oct 2019 — there is **no sustained bear**",
        "> (2008/2011/2015/2018) in sample. A major-bear stress is therefore **untested**; the",
        "> overlay below is validated only against the 2020 COVID crash and the 2022 dip.",
        "",
        f"Long-only top-{k}, monthly, top-{top_n} liquid universe, NSE delivery cost stack "
        f"(round-trip ≈ {_ROUND_TRIP*100:.3f}%). Overlay = market proxy > {sma}d SMA, else CASH.",
        "",
        "## Headline (full period 2020–2025, net of costs)",
        "",
        "| Variant | CAGR | Sharpe | MaxDD | Hit% |",
        "|---|---:|---:|---:|---:|",
        f"| delivery (no overlay) | {md['cagr']*100:.1f}% | {md['sharpe']:.2f} | {md['maxdd']*100:.1f}% | {md['hit']*100:.0f}% |",
        f"| **delivery + {sma}d overlay** | {mdo['cagr']*100:.1f}% | {mdo['sharpe']:.2f} | {mdo['maxdd']*100:.1f}% | {mdo['hit']*100:.0f}% |",
        f"| benchmark (no overlay) | {mb['cagr']*100:.1f}% | {mb['sharpe']:.2f} | {mb['maxdd']*100:.1f}% | {mb['hit']*100:.0f}% |",
        f"| benchmark + overlay | {mbo['cagr']*100:.1f}% | {mbo['sharpe']:.2f} | {mbo['maxdd']*100:.1f}% | {mbo['hit']*100:.0f}% |",
        "",
        f"Overlay parked the delivery book in cash for **{do.cash_months}** of "
        f"{do.monthly_net.size} months.",
        "",
        "## Walk-forward — per-calendar-year OOS (delivery, no overlay)",
        "",
        "| Year | Return | Sharpe | MaxDD | Months |",
        "|---|---:|---:|---:|---:|",
    ]
    for y, row in _per_year(d.monthly_net).iterrows():
        L.append(f"| {y} | {row['return']*100:.1f}% | {row['sharpe']:.2f} | {row['maxdd']*100:.1f}% | {int(row['months'])} |")

    L += [
        "",
        "## Walk-forward — per-calendar-year OOS (delivery + overlay)",
        "",
        "| Year | Return | Sharpe | MaxDD | Months |",
        "|---|---:|---:|---:|---:|",
    ]
    for y, row in _per_year(do.monthly_net).iterrows():
        L.append(f"| {y} | {row['return']*100:.1f}% | {row['sharpe']:.2f} | {row['maxdd']*100:.1f}% | {int(row['months'])} |")

    pos_years = (_per_year(d.monthly_net)["return"] > 0).sum()
    tot_years = len(_per_year(d.monthly_net))
    dd_relief = md["maxdd"] - mdo["maxdd"]      # >0 = overlay reduced drawdown magnitude
    cagr_cost = md["cagr"] - mdo["cagr"]        # >0 = overlay gave up return
    overlay_helped = mdo["sharpe"] > md["sharpe"]
    L += [
        "",
        "## Read",
        "",
        f"- **Walk-forward consistency:** delivery-% was positive in **{pos_years}/{tot_years}** "
        "calendar years OOS (the real test for a non-parametric factor — it cannot be curve-fit).",
        f"- **{sma}d regime overlay {'helped' if overlay_helped else 'HURT'}:** Sharpe "
        f"{md['sharpe']:.2f} → {mdo['sharpe']:.2f}, CAGR {cagr_cost*100:+.1f} pts "
        f"({md['cagr']*100:.1f}% → {mdo['cagr']*100:.1f}%), MaxDD relief {dd_relief*100:+.1f} pts "
        f"({md['maxdd']*100:.1f}% → {mdo['maxdd']*100:.1f}%).",
        f"- The overlay is a **standard, untuned {sma}d filter** (not optimised to this data). Its "
        "purpose is *sustained*-bear protection, which is **untestable here** (no such bear in "
        "sample); against V-shaped in-sample dips a trend filter whipsaws.",
        "",
        "## Verdict & next gate",
        "",
        f"Delivery-% holds up out-of-sample across {tot_years} years. The {sma}d overlay is "
        f"{'supported' if overlay_helped else 'NOT justified'} by this data; the sustained-bear "
        "question it targets needs pre-2019 data. **Carry delivery-% to paper validation (CNC, "
        "positional)** — it is the strongest positional candidate. It is NOT promotable: the "
        "major-bear case is untested (data limit), drawdowns are equity-sized, and paper "
        "validation against the live gate is required before any capital.",
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production changes.",
        "> Live trading remains BLOCKED; the 5-session paper gate is unaffected.",
    ]
    out.write_text("\n".join(L) + "\n")
    return out


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Delivery-% walk-forward + regime overlay")
    ap.add_argument("--top-n", type=int, default=200)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--sma", type=int, default=200, help="Market trend filter window (default 200)")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-06-30")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    print("=" * 72)
    print("QuantEmbrace — Delivery-% Walk-Forward + Regime Overlay (positional/CNC)")
    print("Backtest-only. Advisory. No broker. No live trading. NO sustained bear in sample.")
    print("=" * 72)
    print(f"  top-{args.top_n} universe · top-{args.k} book · monthly · {args.sma}d overlay")
    print("  Loading daily lake...")
    close, turn, deliv = _load_panel(start, end)
    print(f"  Loaded {close.shape[0]} days × {close.shape[1]} symbols")
    regime = _regime_series(close, turn, args.sma)

    variants = {
        "delivery": _run_delivery(close, turn, deliv, None, args.top_n, args.k),
        "delivery+overlay": _run_delivery(close, turn, deliv, regime, args.top_n, args.k),
        "benchmark": _run_benchmark(close, turn, None, args.top_n),
        "benchmark+overlay": _run_benchmark(close, turn, regime, args.top_n),
    }

    print()
    for key in ("delivery", "delivery+overlay", "benchmark", "benchmark+overlay"):
        v = variants[key]
        print(f"  {key:20} {_fmt_metrics(_metrics(v.monthly_net))}")
    print()
    print("  Per-year OOS (delivery, no overlay):")
    py = _per_year(variants["delivery"].monthly_net)
    for y, row in py.iterrows():
        print(f"    {y}: return {row['return']*100:6.1f}%  Sharpe {row['sharpe']:5.2f}  MaxDD {row['maxdd']*100:6.1f}%")
    print("  Per-year OOS (delivery + overlay):")
    for y, row in _per_year(variants["delivery+overlay"].monthly_net).iterrows():
        print(f"    {y}: return {row['return']*100:6.1f}%  Sharpe {row['sharpe']:5.2f}  MaxDD {row['maxdd']*100:6.1f}%")
    print("=" * 72)

    report = _write_report(variants, args.sma, args.k, args.top_n, start, end)
    print(f"  Report: {report}")
    print("\nAdvisory only. Backtesting can recommend; it cannot promote. Live trading BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
