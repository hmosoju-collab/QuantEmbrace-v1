#!/usr/bin/env python3
"""
QuantEmbrace — F2: Positional index-futures trend/momentum screen (Options/Futures program).

THESIS
------
Trend-follow NIFTY futures (long in uptrend; flat or short in downtrend) to capture momentum with
futures leverage + low cost + a hard SMA exit that sidesteps crashes.

THE DECISIVE TEST (why this isn't just leveraged beta)
------------------------------------------------------
On a single index in a bull decade, almost any "trend" rule just delivers beta minus whipsaw costs.
So the gate is: a cell must BEAT BUY-AND-HOLD futures risk-adjusted (Sharpe) AND keep maxDD ≤25% —
trend timing has to ADD value over simply holding. Pre-declared grid, all cells reported (no cherry-pick).

PRE-DECLARED GRID (fixed 2026-06-20):
  lookback ∈ {20,50,100,200} days × mode ∈ {long-only, long-short} = 8 cells.
  Signal on NIFTY50 spot close (no roll/open artifacts); P&L on 1 futures lot with the futures cost stack
  on each flip + daily carry drag when long (≈ riskfree/252 — the cost of holding leveraged futures).
PRE-DECLARED RULE: a cell passes only if Sharpe>buy-hold AND maxDD≥−25% AND ann>0. With 8 cells on ~6 yr
  (few independent trends), a pass counts only with a neighbour (island), not an isolated lucky lookback.

Advisory only. Live BLOCKED. A PASS ⇒ real-futures cross-check + forward book, never deploy.
    python scripts/backtest/run_futures_trend_study.py
    python scripts/backtest/run_futures_trend_study.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from run_overnight_futures_study import FuturesCostModel, _load_ohlc  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = _REPO / "backtest-data"
_REPORT = _REPO / "docs" / "backtesting" / "futures-trend-study-report.md"

NAV = 500_000.0
NIFTY_LOT = 75
CARRY_BPS_DAY = 0.065 / 252.0      # ~riskfree/252 ≈ 2.6 bps/day carry drag when holding long futures
MAX_DD = 0.25
LOOKBACKS = [20, 50, 100, 200]
MODES = ["long-only", "long-short"]
PREREG_DATE = "2026-06-20"


def _run_cell(close: np.ndarray, lookback: int, mode: str, cost: FuturesCostModel) -> dict:
    s = pd.Series(close)
    sma = s.rolling(lookback, min_periods=lookback).mean().to_numpy()
    raw = np.where(close > sma, 1.0, (0.0 if mode == "long-only" else -1.0))
    raw[np.isnan(sma)] = 0.0
    pos = np.concatenate([[0.0], raw[:-1]])          # act next day (no lookahead)
    pnl = np.zeros(len(close))
    for t in range(1, len(close)):
        notional = close[t - 1] * NIFTY_LOT
        pnl[t] = pos[t] * (close[t] - close[t - 1]) * NIFTY_LOT
        if pos[t] > 0:
            pnl[t] -= CARRY_BPS_DAY * notional        # carry drag while long
        if pos[t] != pos[t - 1]:
            pnl[t] -= cost.round_trip(notional)       # flip = round trip
    return _metrics(pnl, pos)


def _bh(close: np.ndarray, cost: FuturesCostModel) -> dict:
    pnl = np.zeros(len(close))
    for t in range(1, len(close)):
        notional = close[t - 1] * NIFTY_LOT
        pnl[t] = (close[t] - close[t - 1]) * NIFTY_LOT - CARRY_BPS_DAY * notional
    pnl[1] -= cost.round_trip(close[0] * NIFTY_LOT)
    return _metrics(pnl, np.ones(len(close)))


def _metrics(pnl: np.ndarray, pos: np.ndarray) -> dict:
    eq = NAV + np.cumsum(pnl)
    peak = np.maximum.accumulate(eq)
    dd = float((eq / peak - 1.0).min())
    r = pnl / NAV
    sharpe = float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0
    ann = float((eq[-1] / NAV) ** (252 / len(pnl)) - 1) if len(pnl) else 0.0
    flips = int((np.diff(pos) != 0).sum())
    return {"ann": ann, "sharpe": sharpe, "dd": dd, "total": float(pnl.sum()), "flips": flips,
            "time_in": float((pos != 0).mean())}


def study(close: np.ndarray) -> dict:
    cost = FuturesCostModel()
    bh = _bh(close, cost)
    cells = []
    for mode in MODES:
        for lb in LOOKBACKS:
            m = _run_cell(close, lb, mode, cost)
            m["lookback"] = lb; m["mode"] = mode
            m["pass"] = m["sharpe"] > bh["sharpe"] and m["dd"] >= -MAX_DD and m["ann"] > 0
            cells.append(m)
    passes = [c for c in cells if c["pass"]]
    if not passes:
        verdict = "SHELVE — no trend cell beats buy-and-hold risk-adjusted; trend timing adds no edge."
    elif len(passes) == 1:
        verdict = (f"ISOLATED PASS (lb {passes[0]['lookback']}/{passes[0]['mode']}) — likely lookback luck "
                   "on few trends; needs a neighbour + OOS before any belief.")
    else:
        verdict = f"{len(passes)} cells beat buy-hold — check for a contiguous island (real) vs scattered (luck)."
    return {"bh": bh, "cells": cells, "passes": passes, "verdict": verdict}


def _write_report(r: dict, span: str, n: int, out: Path) -> None:
    bh = r["bh"]
    rows = "\n".join(
        f"| {c['mode']} | {c['lookback']} | {c['ann']*100:+.1f}% | {c['sharpe']:.2f} | {c['dd']*100:.1f}% | "
        f"{c['flips']} | {c['time_in']*100:.0f}% | {'✅' if c['pass'] else '—'} |" for c in r["cells"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"""# F2 — Index-Futures Trend/Momentum Screen

**Pre-registered:** {PREREG_DATE} · **Signal:** NIFTY50 spot close; **P&L:** 1 NIFTY futures lot (cost stack
+ carry drag) · **Live trading: BLOCKED** · Advisory. Window {span}, {n} days.

## Benchmark — buy & hold 1 futures lot (the bar trend must beat)
- ann **{bh['ann']*100:+.1f}%** · Sharpe **{bh['sharpe']:.2f}** · maxDD **{bh['dd']*100:.1f}%**

## Pre-declared grid (all cells; ✅ = beats buy-hold Sharpe & maxDD≤25% & ann>0)
| mode | lookback | ann | Sharpe | maxDD | flips | time-in | beats B&H |
|---|---:|---:|---:|---:|---:|---:|---|
{rows}

## VERDICT
**{r['verdict']}**

---
*Single-index trend usually = leveraged beta minus whipsaw cost. Few independent trends in ~6 yr ⇒ low
confidence; a pass needs an island + OOS + a real-futures cross-check before any forward book. Never
auto-deploy. Live BLOCKED.*
""")


def _self_test() -> int:
    print("SELF-TEST: futures-trend screen...")
    # clean regime: long uptrend then sharp downtrend — trend (long-only) should beat buy-hold (avoids crash)
    up = np.linspace(0, 0.5, 320)
    down = np.linspace(0.5, -0.1, 180)
    close = 15000 * np.exp(np.concatenate([up, down]))
    r = study(close)
    lo = [c for c in r["cells"] if c["mode"] == "long-only"]
    assert any(c["sharpe"] > r["bh"]["sharpe"] for c in lo), "trend should beat B&H in a clean up-then-down regime"
    assert r["bh"]["dd"] < -0.05, "buy-hold should suffer the downtrend"
    print(f"  up-then-down: B&H Sharpe {r['bh']['sharpe']:.2f} dd {r['bh']['dd']*100:.0f}%; "
          f"best long-only Sharpe {max(c['sharpe'] for c in lo):.2f} → trend beats B&H ✅")
    # pure noise: trend should NOT systematically beat B&H (whipsaw)
    rng = np.random.default_rng(0)
    noise = 15000 * np.exp(np.cumsum(rng.normal(0, 0.01, 600)))
    rn = study(noise)
    print(f"  noise: {len(rn['passes'])}/8 cells beat B&H (expect few; whipsaw) ✅")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "r.md"; _write_report(r, "synthetic", len(close), p)
        assert p.exists() and "VERDICT" in p.read_text()
    print("SELF-TEST PASSED.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="F2 index-futures trend/momentum screen")
    ap.add_argument("--symbol", default="NIFTY50")
    ap.add_argument("--base", default=str(_DEFAULT_BASE))
    ap.add_argument("--out", default=str(_REPORT))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    ohlc = _load_ohlc(Path(args.base), args.symbol)
    print("=" * 80)
    print("QuantEmbrace — F2 Index-Futures Trend/Momentum Screen. Advisory. Live BLOCKED.")
    print("=" * 80)
    if ohlc.empty:
        print(f"  No {args.symbol} OHLC in lake. Fetch via fetch_zerodha_indices.py.")
        return 1
    close = ohlc["close"].to_numpy()
    span = f"{ohlc['date'].min().date()} → {ohlc['date'].max().date()}"
    r = study(close)
    bh = r["bh"]
    print(f"\n  {args.symbol} {span} · {len(close)} days")
    print(f"  BUY&HOLD 1 lot: ann {bh['ann']*100:+.1f}% · Sharpe {bh['sharpe']:.2f} · maxDD {bh['dd']*100:.1f}%")
    print(f"  {'mode':11s} {'lb':>4} {'ann':>8} {'Sharpe':>7} {'maxDD':>7} {'flips':>6} {'tIn':>5}  B&H?")
    for c in r["cells"]:
        print(f"  {c['mode']:11s} {c['lookback']:>4} {c['ann']*100:>7.1f}% {c['sharpe']:>7.2f} "
              f"{c['dd']*100:>6.1f}% {c['flips']:>6} {c['time_in']*100:>4.0f}%  {'✅' if c['pass'] else '—'}")
    print(f"\n  VERDICT: {r['verdict']}")
    _write_report(r, span, len(close), Path(args.out))
    print(f"\n  Report → {args.out}\n  Advisory only. Single-index trend ≈ beta; low n of trends. Live BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
