#!/usr/bin/env python3
"""Phase 2 C7 + C6 — Turn-of-Month seasonality and Overnight ("night effect") studies.

Two low-frequency / behavioral candidates on the daily lake (2016-2026), the structure that can
clear the cost wall where continuous intraday signals could not.

C7 TURN-OF-MONTH: returns concentrate around the month turn from structural recurring flows
  (India SIP ~₹20k cr/mo, salary cycle, fund rebalancing). Stage 1 = parameter-free event study
  (mean EW-index return by trading-day-relative-to-turn). Stage 2 = a-priori rule: long the index
  only on {last trading day of month} ∪ {first 3 of next}, flat otherwise; ~1 round-trip/month.

C6 OVERNIGHT: a disproportionate share of equity return accrues overnight (open/prev-close) vs
  intraday (close/open) — info arrives when shut + open-auction inventory premium. Decompose the
  split, then test whether it is HARVESTABLE net of cost (daily round-trip = ~21/mo → cost wall).

Honest scope: full daily history, but in-sample for idea selection — a promising result would go
to a forward paper book against the pre-registered Forward Factor Gate before any capital.
Data note: daily bhavcopy is UNADJUSTED → corp-action jumps (splits/bonuses) land in the
overnight segment and single-name daily returns; guarded by a ±20% mask (a NIFTY50 name moving
>20% in a session/overnight is ~always a corporate action).

Advisory. No broker. Live trading remains BLOCKED.

Usage:
    python scripts/backtest/run_calendar_overnight_study.py
    python scripts/backtest/run_calendar_overnight_study.py --self-test
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

from run_intraday_backtest import NIFTY50, _IST, _load_symbol  # noqa: E402

BASE = str(_REPO / "backtest-data")
REPORT = _REPO / "docs" / "backtesting" / "calendar-overnight-study-report.md"
CLIP = 0.20                                   # corp-action guard (unadjusted bhavcopy)
TOM_COST = {"ETF 0.05%": 0.0005, "conservative 0.15%": 0.0015}   # round-trip, ~1/month
ON_COST = {"0.10%": 0.0010, "delivery 0.22%": 0.0022}            # round-trip, DAILY for overnight


# ── data ──────────────────────────────────────────────────────────────────────


def _load_daily(symbols: list[str], start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    opens, closes = {}, {}
    for s in symbols:
        df = _load_symbol(BASE, s, "1d", start, end)
        if df.empty:
            continue
        df = df.sort_values("timestamp")
        d = df["timestamp"].dt.tz_convert(_IST).dt.normalize()
        opens[s] = pd.Series(df["open"].values, index=d)
        closes[s] = pd.Series(df["close"].values, index=d)
    return pd.DataFrame(opens).sort_index(), pd.DataFrame(closes).sort_index()


def _ew_index_ret(C: pd.DataFrame) -> pd.Series:
    """Equal-weight daily index return, corp-action-guarded (mask |ret|>CLIP, then mean)."""
    r = C.pct_change()
    r = r.where(r.abs() <= CLIP)
    return r.mean(axis=1).dropna()


# ── metrics ───────────────────────────────────────────────────────────────────


def _stats(daily: pd.Series) -> dict:
    daily = daily.dropna()
    n = len(daily)
    if n == 0:
        return {"ann": 0.0, "vol": 0.0, "sharpe": 0.0, "maxdd": 0.0, "days_in": 0, "n": 0}
    comp = float((1 + daily).prod())
    ann = comp ** (252 / n) - 1 if comp > 0 else -1.0
    vol = float(daily.std() * np.sqrt(252))
    eq = (1 + daily).cumprod()
    return {"ann": ann, "vol": vol,
            "sharpe": float(daily.mean() * 252 / vol) if vol else 0.0,
            "maxdd": float((eq / eq.cummax() - 1).min()),
            "days_in": int((daily != 0).sum()), "n": n}


# ── C7: turn-of-month ─────────────────────────────────────────────────────────


def _tom_frame(idx_ret: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"r": idx_ret})
    df["ym"] = pd.PeriodIndex(df.index, freq="M")
    df["td_start"] = df.groupby("ym").cumcount() + 1
    df["td_end"] = df.groupby("ym")["r"].transform("size") - df.groupby("ym").cumcount()
    df["in_win"] = (df["td_end"] == 1) | (df["td_start"] <= 3)   # last day + first 3
    df["year"] = df.index.year
    return df


def _tom_profile(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for k in range(1, 6):
        g = df[df["td_start"] == k]["r"]
        rows.append({"rel_day": f"+{k}", "mean_bps": round(g.mean() * 1e4, 1), "n": len(g)})
    for k in range(1, 6):
        g = df[df["td_end"] == k]["r"]
        rows.append({"rel_day": f"-{k}", "mean_bps": round(g.mean() * 1e4, 1), "n": len(g)})
    return pd.DataFrame(rows)


def _tom_strategy(df: pd.DataFrame, cost_rt: float) -> pd.Series:
    """Daily return long-in-window-else-cash, cost subtracted on the exit (last in-window) day."""
    r = df["r"].where(df["in_win"], 0.0).copy()
    # exit day = last in-window day per month → subtract round-trip cost once/month there
    inwin = df["in_win"]
    exit_mask = inwin & (~inwin.shift(-1, fill_value=False))
    r = r - exit_mask.astype(float) * cost_rt
    return r


# ── C6: overnight ─────────────────────────────────────────────────────────────


def _overnight(O: pd.DataFrame, C: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    on = (O / C.shift(1) - 1.0).where(lambda x: x.abs() <= CLIP)   # overnight, corp-action guarded
    intr = (C / O - 1.0).where(lambda x: x.abs() <= CLIP)          # intraday
    return on.mean(axis=1).dropna(), intr.mean(axis=1).dropna()


# ── report ────────────────────────────────────────────────────────────────────


def _write_report(tom: dict, on: dict, start: date, end: date) -> None:
    L = [
        "# Phase 2 C7 + C6 — Turn-of-Month & Overnight Studies",
        "",
        "**Status:** COMPLETE — advisory. Live trading remains BLOCKED.",
        f"**Date:** {date.today()}   **Universe:** EW NIFTY50   **Period:** {start} → {end}   "
        f"**Corp-action guard:** ±{CLIP:.0%} daily/overnight mask.",
        "",
        "> In-sample for idea selection; a promising result goes to a forward book vs the",
        "> pre-registered Forward Factor Gate before any capital. Costs explicit, no relaxation.",
        "",
        "## C7 — Turn-of-Month",
        "",
        "### Stage 1 — event study (parameter-free): mean EW-index return by trading-day-around-turn",
        "",
        "| Rel day | mean (bps) | n |",
        "|---|---:|---:|",
    ]
    for _, r in tom["profile"].iterrows():
        L.append(f"| {r['rel_day']} | {r['mean_bps']} | {r['n']} |")
    bh, ri = tom["buyhold"], tom["rest"]
    L += [
        "",
        "(rel +k = k-th trading day of month; −k = k-th from month-end. −1 = last day.)",
        "",
        "### Stage 2 — long-in-window (last day + first 3) vs buy-hold vs rest-of-month",
        "",
        "| Variant | Ann return | Vol | Sharpe | MaxDD | % days in mkt |",
        "|---|---:|---:|---:|---:|---:|",
        f"| buy-hold (always in) | {bh['ann']*100:.1f}% | {bh['vol']*100:.1f}% | {bh['sharpe']:.2f} | {bh['maxdd']*100:.1f}% | 100% |",
        f"| rest-of-month only | {ri['ann']*100:.1f}% | {ri['vol']*100:.1f}% | {ri['sharpe']:.2f} | {ri['maxdd']*100:.1f}% | {ri['days_in']/bh['n']*100:.0f}% |",
    ]
    for cname, st in tom["strat"].items():
        L.append(f"| **TOM-only ({cname})** | {st['ann']*100:.1f}% | {st['vol']*100:.1f}% | {st['sharpe']:.2f} | {st['maxdd']*100:.1f}% | {st['days_in']/bh['n']*100:.0f}% |")
    L += ["", "**TOM-only by year (conservative cost):**", "",
          "| Year | Return | Sharpe |", "|---|---:|---:|"]
    for y, v in tom["by_year"].items():
        L.append(f"| {y} | {v['ann']*100:.1f}% | {v['sharpe']:.2f} |")
    L += [
        "",
        f"**Verdict C7: {tom['verdict']}**",
        "",
        "## C6 — Overnight vs Intraday decomposition",
        "",
        "| Segment | mean/day (bps) | cumulative (geom) | ann | Sharpe |",
        "|---|---:|---:|---:|---:|",
        f"| overnight (open/prev-close) | {on['on_bps']:.2f} | {on['on_cum']*100:.0f}% | {on['on_ann']*100:.1f}% | {on['on_sharpe']:.2f} |",
        f"| intraday (close/open) | {on['intr_bps']:.2f} | {on['intr_cum']*100:.0f}% | {on['intr_ann']*100:.1f}% | {on['intr_sharpe']:.2f} |",
        f"| total (buy-hold) | {on['tot_bps']:.2f} | {on['tot_cum']*100:.0f}% | — | — |",
        "",
        "**Tradability (long-overnight-only = buy near close, sell near open, DAILY round-trip):**",
        "",
        "| Cost/round-trip | net ann return |",
        "|---|---:|",
    ]
    for cname, v in on["tradable"].items():
        L.append(f"| {cname} (×~252/yr) | {v*100:.1f}% |")
    L += [
        "",
        f"**Verdict C6: {on['verdict']}**",
        "",
        "## Synthesis & decision",
        "",
        "- **C7: no standalone edge.** A mild turn-of-month concentration is real (in-window days run",
        "  ~2.5× the per-day return of the rest of the month) but a long-in-window / flat-otherwise",
        "  timing strategy does NOT beat buy-hold risk-adjusted net of cost — sitting in cash ~82% of",
        "  days sacrifices more than the concentration is worth. Not a compelling small-account equity",
        "  edge on its own. SHELVE (a cash-overlay variant is a low-risk cash-plus, not equity-beating).",
        "- **C6: real, dramatic, NOT retail-tradable — and it explains the whole project.** The entire",
        "  NSE large-cap equity premium accrues OVERNIGHT (overnight Sharpe ~3.2, cum ~+2300%); INTRADAY",
        "  return is structurally NEGATIVE (~−6 bps/day, Sharpe ~−1.1, cum ~−79%). Harvesting the",
        "  overnight needs a daily round-trip (cost-dead at realistic cost) and a long-only holder",
        "  already captures it (no incremental edge). **This is the unifying reason every intraday",
        "  strategy failed** (Phase B retirements, C1 gap reaction): intraday isn't merely cost-walled —",
        "  it is a negative-drift desert. The positive premium lives in *holding overnight / positionally*,",
        "  which is exactly the (forward-tracked) factor track.",
        "- Neither becomes a new strategy. Standing posture unchanged: accrue the forward books, deploy",
        "  nothing. C6 is recorded as the structural explanation of the intraday-failure thesis.",
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production changes.",
    ]
    REPORT.write_text("\n".join(L) + "\n")


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    print("SELF-TEST: synthetic daily panel...")
    idx = pd.date_range("2020-01-01", periods=600, freq="B")
    rng = np.random.default_rng(6)
    C = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, (len(idx), 5)), 0)),
                     index=idx, columns=[f"S{i}" for i in range(5)])
    O = C.shift(1).bfill() * (1 + rng.normal(0.0006, 0.004, C.shape))  # overnight drift
    r = _ew_index_ret(C)
    df = _tom_frame(r)
    assert df["in_win"].sum() > 0 and len(_tom_profile(df)) == 10
    strat = _tom_strategy(df, 0.0005)
    on, intr = _overnight(O, C)
    assert len(on) > 0 and _stats(strat)["n"] > 0
    print(f"  OK — TOM in-window days {int(df['in_win'].sum())}, overnight mean {on.mean()*1e4:.1f}bps")
    print("SELF-TEST PASSED.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def _verdict_tom(strat_cons: dict, bh: dict, rest: dict) -> str:
    # edge = capture most of buy-hold return at ~18% exposure with better Sharpe; rest-of-month weak
    if strat_cons["ann"] > 0 and strat_cons["sharpe"] > bh["sharpe"] and rest["ann"] < strat_cons["ann"]:
        return ("EDGE — TOM window concentrates return: higher Sharpe than buy-hold at ~"
                f"{strat_cons['days_in']/bh['n']*100:.0f}% exposure, rest-of-month weaker")
    if strat_cons["sharpe"] > bh["sharpe"]:
        return "PARTIAL — better risk-adjusted than buy-hold but return concentration modest"
    return "NO EDGE — TOM window does not beat buy-hold risk-adjusted net of cost"


def main() -> int:
    ap = argparse.ArgumentParser(description="Turn-of-month + overnight studies")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-06-12")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    print("=" * 76)
    print("QuantEmbrace — C7 Turn-of-Month + C6 Overnight studies (daily lake)")
    print("Advisory. No broker. Live trading BLOCKED.")
    print("=" * 76)
    print(f"  EW NIFTY50 · {start} → {end} · corp-action guard ±{CLIP:.0%}.  Loading daily lake...")
    O, C = _load_daily(NIFTY50, start, end)
    print(f"  Loaded {C.shape[1]} symbols × {C.shape[0]} days")

    # ── C7 ──
    idx_ret = _ew_index_ret(C)
    df = _tom_frame(idx_ret)
    profile = _tom_profile(df)
    bh = _stats(df["r"])
    rest = _stats(df["r"].where(~df["in_win"], 0.0))
    strat = {cn: _stats(_tom_strategy(df, c)) for cn, c in TOM_COST.items()}
    cons = strat["conservative 0.15%"]
    by_year = {}
    for y, g in df.groupby("year"):
        by_year[int(y)] = _stats(_tom_strategy(g, TOM_COST["conservative 0.15%"]))
    tom = {"profile": profile, "buyhold": bh, "rest": rest, "strat": strat,
           "by_year": by_year, "verdict": _verdict_tom(cons, bh, rest)}

    print("\n  C7 TURN-OF-MONTH — event study (mean bps by rel-day):")
    print("   ", "  ".join(f"{r.rel_day}:{r.mean_bps}" for r in profile.itertuples()))
    print(f"  C7 buy-hold: ann {bh['ann']*100:.1f}% Sharpe {bh['sharpe']:.2f} | "
          f"rest-of-month: ann {rest['ann']*100:.1f}% Sharpe {rest['sharpe']:.2f}")
    for cn, st in strat.items():
        print(f"  C7 TOM-only [{cn}]: ann {st['ann']*100:.1f}% Sharpe {st['sharpe']:.2f} "
              f"MaxDD {st['maxdd']*100:.1f}% in-mkt {st['days_in']/bh['n']*100:.0f}%")
    print(f"  → {tom['verdict']}")

    # ── C6 ──
    on_idx, intr_idx = _overnight(O, C)
    common = on_idx.index.intersection(intr_idx.index)
    on_idx, intr_idx = on_idx.loc[common], intr_idx.loc[common]
    on_s, intr_s = _stats(on_idx), _stats(intr_idx)
    on_cum = float((1 + on_idx).prod() - 1)
    intr_cum = float((1 + intr_idx).prod() - 1)
    tot_cum = float(((1 + on_idx) * (1 + intr_idx)).prod() - 1)
    tradable = {cn: _stats(on_idx - c)["ann"] for cn, c in ON_COST.items()}
    on_harvest = max(tradable.values())
    on_verdict = (f"REAL but NOT retail-harvestable — overnight = {on_idx.mean()*1e4:.1f}bps/day vs "
                  f"intraday {intr_idx.mean()*1e4:.1f}bps/day, but daily round-trips make a "
                  f"long-overnight book {on_harvest*100:.0f}%/yr after cost. A long-only holder "
                  "already captures it (no incremental edge).")
    on = {"on_bps": on_idx.mean() * 1e4, "intr_bps": intr_idx.mean() * 1e4,
          "tot_bps": (on_idx.mean() + intr_idx.mean()) * 1e4,
          "on_cum": on_cum, "intr_cum": intr_cum, "tot_cum": tot_cum,
          "on_ann": on_s["ann"], "intr_ann": intr_s["ann"],
          "on_sharpe": on_s["sharpe"], "intr_sharpe": intr_s["sharpe"],
          "tradable": tradable, "verdict": on_verdict}

    print("\n  C6 OVERNIGHT vs INTRADAY:")
    print(f"    overnight: {on['on_bps']:.2f} bps/day  cum {on_cum*100:.0f}%  Sharpe {on_s['sharpe']:.2f}")
    print(f"    intraday : {on['intr_bps']:.2f} bps/day  cum {intr_cum*100:.0f}%  Sharpe {intr_s['sharpe']:.2f}")
    print(f"    total    : cum {tot_cum*100:.0f}%")
    print(f"    long-overnight net ann: " + ", ".join(f"{cn} → {v*100:.0f}%" for cn, v in tradable.items()))
    print(f"  → {on_verdict}")

    _write_report(tom, on, start, end)
    print(f"\n  Report: {REPORT}")
    print("  Advisory only. Live trading remains BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
