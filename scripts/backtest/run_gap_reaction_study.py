#!/usr/bin/env python3
"""Phase 2 C1 — Overnight-gap reaction study (event-conditioned intraday equity).

Hypothesis: overnight gaps on liquid NIFTY50 names are event-conditioned moves large enough that
their intraday reaction can clear MIS costs where continuous intraday signals could not.
A-priori (NOT fitted): small/mid gaps FADE (overreaction → revert), large gaps CONTINUE (info).

Two stages, deliberately minimal-parameter (small sample → complexity = curve-fitting):

  STAGE 1 — parameter-free EVENT STUDY. Bucket each (symbol, day) by gap_z = gap / trailing-20d
  daily-return-vol. Report the mean entry→EOD return per bucket + t-stat + n + %positive at
  several horizons (+30m / +60m / EOD). This *characterises* the effect with zero strategy params.

  STAGE 2 — minimal A-PRIORI rule. Fixed boundaries (|gap_z| 0.5σ noise floor, 2σ = "large"),
  fixed directions (mid = fade against the gap, large = continue with it), EOD-only exit (no
  target, no stop — fewest params). Net of CORRECT MIS cost (intraday model ~0.035% + slippage).
  Walk-forward by calendar year + by regime. Cost-sensitivity: 5 bps vs 10 bps/leg slippage
  (the open is the widest-spread time, so 10 bps is the honest conservative case).

No lookahead: gap uses today's open (known 09:15) vs prior close; vol uses returns ending the
PRIOR day; entry is the 09:20 (first 5-min bar) close — executable, conservative.

Backtest-only. Advisory. Live trading remains BLOCKED.

Usage:
    python scripts/backtest/run_gap_reaction_study.py
    python scripts/backtest/run_gap_reaction_study.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))
sys.path.insert(0, str(_REPO / "scripts" / "backtest"))

import numpy as np
import pandas as pd

from strategy_engine.backtesting.backtester import IndianCostModel  # noqa: E402
from run_intraday_backtest import NIFTY50, _load_symbol, _IST  # noqa: E402
from phase1_strategy_audit import _regime_by_day  # noqa: E402

CAPITAL = 1_000_000.0
TRADE_NOTIONAL = 50_000.0          # fixed ₹ per trade (sizing-neutral for expectancy/PF)
BASE = str(_REPO / "backtest-data")
REPORT = _REPO / "docs/backtesting/gap-reaction-study-report.md"

# a-priori bucket boundaries (NOT fitted): 0.5σ = noise floor, 2σ = "large"
NOISE, LARGE = 0.5, 2.0
VOL_WINDOW = 20

# correct MIS statutory round-trip (from the cost model) + slippage variants
_m = IndianCostModel.intraday()
_exch, _sebi = _m.exchange_txn_pct / 100, _m.sebi_turnover_pct / 100
_gst = (_exch + _sebi) * (_m.gst_pct / 100)
STAT_RT = (_exch + _sebi + _m.stt_buy_pct / 100 + _m.stamp_buy_pct / 100 + _gst) \
        + (_exch + _sebi + _m.stt_sell_pct / 100 + _gst)          # ≈ 0.000352
SLIP = {"5bps/leg": 0.0010, "10bps/leg": 0.0020}                  # round-trip slippage


# ── build the per-(symbol,day) gap panel ──────────────────────────────────────


MAX_GAP = 0.25   # sanity cap: an overnight |gap| > 25% on a NIFTY50 name is a corporate action, not news


def _build_panel(symbols: list[str], start: date, end: date) -> tuple[pd.DataFrame, int]:
    """Gap panel measured ENTIRELY within the 5m source (self-consistent adjustment basis).

    Earlier cross-source version (Kite 5m open ÷ bhavcopy daily prev_close) was contaminated by
    differing split adjustment → fake −40% 'gaps' on post-split days. Here prev_close and trailing
    vol come from the prior day's last 5m close, so corporate actions don't create phantom gaps.
    """
    regime = _regime_by_day()
    rows, dropped = [], 0
    for sym in symbols:
        five = _load_symbol(BASE, sym, "5m", start, end)
        if five.empty:
            continue
        five = five.sort_values("timestamp").copy()
        five["d"] = five["timestamp"].dt.tz_convert(_IST).dt.date
        recs = []
        for d, g in five.groupby("d"):
            eod = float(g.iloc[-1]["close"])
            recs.append({"d": d, "o": float(g.iloc[0]["open"]), "entry": float(g.iloc[0]["close"]),
                         "eod": eod,
                         "p30": float(g.iloc[6]["close"]) if len(g) > 6 else eod,
                         "p60": float(g.iloc[12]["close"]) if len(g) > 12 else eod})
        dd = pd.DataFrame(recs).sort_values("d").reset_index(drop=True)
        dd["pc"] = dd["eod"].shift(1)                          # prior day's last 5m close (same source)
        dd["sigma"] = dd["eod"].pct_change().rolling(VOL_WINDOW).std().shift(1)  # trailing, no lookahead
        for r in dd.itertuples(index=False):
            if pd.isna(r.pc) or pd.isna(r.sigma) or r.sigma == 0 or r.pc <= 0 or r.entry <= 0:
                continue
            gap = r.o / r.pc - 1.0
            if abs(gap) > MAX_GAP:                              # residual corporate-action guard
                dropped += 1
                continue
            rows.append({
                "symbol": sym, "date": r.d, "year": r.d.year,
                "gap": gap, "gap_z": gap / r.sigma,
                "r30": r.p30 / r.entry - 1.0, "r60": r.p60 / r.entry - 1.0,
                "reod": r.eod / r.entry - 1.0, "regime": regime.get(r.d, "UNKNOWN"),
            })
    return pd.DataFrame(rows), dropped


def _bucket(z: float) -> str:
    if z <= -LARGE:   return "large_down"
    if z <= -NOISE:   return "mid_down"
    if z < NOISE:     return "flat"
    if z < LARGE:     return "mid_up"
    return "large_up"


# ── stage 1: event study ──────────────────────────────────────────────────────


def _event_study(p: pd.DataFrame) -> pd.DataFrame:
    p = p.copy()
    p["bucket"] = p["gap_z"].map(_bucket)
    order = ["large_down", "mid_down", "flat", "mid_up", "large_up"]
    out = []
    for b in order:
        g = p[p["bucket"] == b]
        if g.empty:
            continue
        r = g["reod"]
        t = float(r.mean() / (r.std() / np.sqrt(len(r)))) if r.std() > 0 and len(r) > 1 else 0.0
        out.append({
            "bucket": b, "n": len(g), "mean_gap_%": round(g["gap"].mean() * 100, 2),
            "r+30m_%": round(g["r30"].mean() * 100, 3),
            "r+60m_%": round(g["r60"].mean() * 100, 3),
            "rEOD_%": round(r.mean() * 100, 3), "t(EOD)": round(t, 2),
            "EOD>0_%": round((r > 0).mean() * 100, 1),
        })
    return pd.DataFrame(out)


# ── stage 2: a-priori rule ────────────────────────────────────────────────────


def _signed_net(p: pd.DataFrame, slip_rt: float) -> pd.DataFrame:
    """Per-trade net return: mid→fade (-sign gap), large→continue (+sign gap), EOD exit."""
    p = p.copy()
    p["bucket"] = p["gap_z"].map(_bucket)
    p = p[p["bucket"] != "flat"].copy()
    fade = p["bucket"].isin(["mid_up", "mid_down"])
    cont = p["bucket"].isin(["large_up", "large_down"])
    sign = np.where(fade, -np.sign(p["gap"]), 0.0) + np.where(cont, np.sign(p["gap"]), 0.0)
    p["mode"] = np.where(fade, "fade", "continue")
    p["gross"] = sign * p["reod"]
    p["net"] = p["gross"] - (STAT_RT + slip_rt)
    p["pnl"] = p["net"] * TRADE_NOTIONAL
    return p


def _agg(p: pd.DataFrame) -> dict:
    if p.empty:
        return {"trades": 0, "win": 0.0, "pf": 0.0, "exp_bps": 0.0, "net": 0.0, "sharpe": 0.0}
    net = p["net"]
    wins, losses = net[net > 0], net[net <= 0]
    pf = float(wins.sum() / -losses.sum()) if losses.sum() < 0 else (float("inf") if wins.sum() > 0 else 0.0)
    daily = p.groupby("date")["pnl"].sum() / CAPITAL
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    return {"trades": int(len(p)), "win": round((net > 0).mean() * 100, 1),
            "pf": round(pf, 3), "exp_bps": round(net.mean() * 1e4, 1),
            "net": round(p["pnl"].sum(), 0), "sharpe": round(sharpe, 2)}


# ── report ────────────────────────────────────────────────────────────────────


def _verdict(overall: dict) -> str:
    if overall["trades"] == 0:
        return "NO TRADES"
    if overall["exp_bps"] > 0 and overall["pf"] > 1.2 and overall["net"] > 0:
        return "EDGE — carry to walk-forward / paper"
    if overall["exp_bps"] > 0:
        return "MARGINAL — positive but sub-gate"
    return "REJECT — no edge after costs"


def _write_report(es: pd.DataFrame, stage2: dict, start: date, end: date, n_panel: int) -> None:
    L = [
        "# Phase 2 C1 — Overnight-Gap Reaction Study",
        "",
        "**Status:** COMPLETE — advisory. Live trading remains BLOCKED.",
        f"**Date:** {date.today()}   **Universe:** NIFTY50 (Zerodha Kite 5m)   **Period:** {start} → {end}",
        f"**Panel:** {n_panel:,} symbol-days   **Entry:** 09:20 (first 5m close, executable)   "
        "**Exit:** EOD (MIS).",
        f"**Costs:** MIS statutory {STAT_RT*100:.4f}% round-trip + slippage (5 & 10 bps/leg).",
        "**Params:** bucket bounds 0.5σ / 2σ and fade/continue directions are **a-priori, not fitted**.",
        "",
        "## Stage 1 — event study (parameter-free): entry→EOD return by gap bucket",
        "",
        "| Bucket | n | mean gap % | r+30m % | r+60m % | rEOD % | t(EOD) | EOD>0 % |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in es.iterrows():
        L.append(f"| {r['bucket']} | {r['n']} | {r['mean_gap_%']} | {r['r+30m_%']} | {r['r+60m_%']} "
                 f"| {r['rEOD_%']} | {r['t(EOD)']} | {r['EOD>0_%']} |")
    L += [
        "",
        "Reading: for *up* gaps a negative rEOD = fade; positive = continuation (and vice-versa for",
        f"down gaps). Compare |rEOD| against the round-trip cost (~{(STAT_RT+SLIP['5bps/leg'])*100:.2f}–"
        f"{(STAT_RT+SLIP['10bps/leg'])*100:.2f}%) — the effect must beat cost to be tradable.",
        "",
        "## Stage 2 — a-priori rule (mid=fade, large=continue, EOD exit), net of cost",
        "",
    ]
    for slip_name, blocks in stage2.items():
        o = blocks["overall"]
        L += [f"### Slippage {slip_name} — overall: **{_verdict(o)}**",
              "",
              "| Cut | Trades | Win% | PF | Exp (bps) | Net ₹ | Sharpe(d) |",
              "|---|---:|---:|---:|---:|---:|---:|",
              f"| **overall** | {o['trades']} | {o['win']} | {o['pf']} | {o['exp_bps']} | {o['net']:,.0f} | {o['sharpe']} |"]
        for k in ("fade", "continue"):
            b = blocks["by_mode"].get(k, {})
            if b:
                L.append(f"| mode={k} | {b['trades']} | {b['win']} | {b['pf']} | {b['exp_bps']} | {b['net']:,.0f} | {b['sharpe']} |")
        for y in sorted(blocks["by_year"]):
            b = blocks["by_year"][y]
            L.append(f"| year={y} | {b['trades']} | {b['win']} | {b['pf']} | {b['exp_bps']} | {b['net']:,.0f} | {b['sharpe']} |")
        for rg in ("UPTREND", "DOWNTREND"):
            b = blocks["by_regime"].get(rg, {})
            if b:
                L.append(f"| regime={rg} | {b['trades']} | {b['win']} | {b['pf']} | {b['exp_bps']} | {b['net']:,.0f} | {b['sharpe']} |")
        L.append("")
    L += [
        "## Read (honest)",
        "",
        "- Gate to 'carry forward': exp > 0 **and** PF > 1.2 **and** net > 0 at the **conservative",
        "  10 bps/leg** slippage (the open is the widest-spread time), holding across years + regimes.",
        "- This is one month-equivalent... no — it is 3 years; but per-trade edge on event days is thin,",
        "  so judge PF and the cost-sensitivity (does 5→10 bps flip the sign?), not the headline ₹.",
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production changes.",
    ]
    REPORT.write_text("\n".join(L) + "\n")


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    print("SELF-TEST: synthetic gap panel...")
    rng = np.random.default_rng(4)
    n = 2000
    z = rng.normal(0, 1.5, n)
    # construct a FADE effect: up gaps drift down by ~30% of the gap (entry→EOD)
    gap = z * 0.012
    reod = -0.30 * gap + rng.normal(0, 0.01, n)
    p = pd.DataFrame({
        "symbol": ["X"] * n, "date": pd.date_range("2023-01-02", periods=n, freq="6h").date,
        "year": 2023, "gap": gap, "gap_z": z, "r30": reod * 0.6, "r60": reod * 0.8,
        "reod": reod, "regime": "UPTREND",
    })
    es = _event_study(p)
    s2 = _signed_net(p, SLIP["5bps/leg"])
    agg = _agg(s2)
    assert not es.empty and agg["trades"] > 0
    assert STAT_RT > 0
    print(f"  event-study buckets={list(es['bucket'])}")
    print(f"  fade rule: trades={agg['trades']} win%={agg['win']} pf={agg['pf']} exp_bps={agg['exp_bps']}")
    print("SELF-TEST PASSED.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2 C1 — overnight-gap reaction study")
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    print("=" * 76)
    print("QuantEmbrace — Phase 2 C1: Overnight-Gap Reaction Study")
    print("Advisory. No broker. Live trading BLOCKED.")
    print("=" * 76)
    print(f"  Universe NIFTY50 · 5m · {start} → {end} · MIS cost {STAT_RT*100:.4f}% + slippage")
    print("  Building gap panel (daily vol + 5m open/EOD)...", flush=True)
    panel, dropped = _build_panel(NIFTY50, start, end)
    if panel.empty:
        print("  NO DATA — intraday 5m lake missing.")
        return 1
    print(f"  Panel: {len(panel):,} symbol-days across {panel['symbol'].nunique()} symbols "
          f"({dropped} dropped as corporate-action dislocations, |gap|>{MAX_GAP:.0%})")

    es = _event_study(panel)
    print("\n  STAGE 1 — event study (entry→EOD return by gap bucket):")
    print(es.to_string(index=False))

    stage2 = {}
    for slip_name, slip_rt in SLIP.items():
        s2 = _signed_net(panel, slip_rt)
        blocks = {
            "overall": _agg(s2),
            "by_mode": {k: _agg(g) for k, g in s2.groupby("mode")},
            "by_year": {int(y): _agg(g) for y, g in s2.groupby("year")},
            "by_regime": {str(r): _agg(g) for r, g in s2.groupby("regime")},
        }
        stage2[slip_name] = blocks
        o = blocks["overall"]
        print(f"\n  STAGE 2 — fade+continue rule @ slippage {slip_name}: "
              f"trades={o['trades']} win%={o['win']} PF={o['pf']} exp={o['exp_bps']}bps "
              f"net=₹{o['net']:,.0f} Sharpe(d)={o['sharpe']} → {_verdict(o)}")
        for y in sorted(blocks["by_year"]):
            b = blocks["by_year"][y]
            print(f"      {y}: trades={b['trades']} PF={b['pf']} exp={b['exp_bps']}bps net=₹{b['net']:,.0f}")

    # FOCUS — the only sub-signal that cleared cost: large-gap CONTINUATION. Is it consistent
    # across years/regimes/sides, or a small-sample fluke? (n is tiny — this is the real test.)
    print("\n  FOCUS — large-gap CONTINUATION (|gap_z|>2), consistency check:")
    for slip_name, slip_rt in SLIP.items():
        cont = _signed_net(panel, slip_rt)
        cont = cont[cont["mode"] == "continue"].copy()
        cont["side"] = np.where(cont["gap"] > 0, "long(up)", "short(down)")
        o = _agg(cont)
        print(f"    @ {slip_name}: overall n={o['trades']} PF={o['pf']} exp={o['exp_bps']}bps "
              f"win%={o['win']} net=₹{o['net']:,.0f} Sharpe(d)={o['sharpe']}")
        for y in sorted(cont["year"].unique()):
            b = _agg(cont[cont["year"] == y])
            print(f"        {y}: n={b['trades']} PF={b['pf']} exp={b['exp_bps']}bps win%={b['win']}")
        for sd in ("long(up)", "short(down)"):
            b = _agg(cont[cont["side"] == sd])
            print(f"        {sd}: n={b['trades']} PF={b['pf']} exp={b['exp_bps']}bps win%={b['win']}")

    _write_report(es, stage2, start, end, len(panel))
    print(f"\n  Report: {REPORT}")
    print("  Advisory only. Live trading remains BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
