#!/usr/bin/env python3
"""
QuantEmbrace — Volatility-premium screen (options/vol track, Phase O-1, FREE).

PURPOSE
-------
Decide — using only FREE underlying data (NIFTY 50 spot + INDIA VIX) — whether
the NSE index volatility-risk-premium (VRP) is real, large, and consistent
enough to *justify buying* 3–5 yr of option-chain history for a proper
defined-risk options backtest (Phase O-2). It is a SCREEN, not a backtest:

  • A PASS authorises *spending on option-chain data*, nothing more.
  • It NEVER authorises deployment. Live trading stays BLOCKED. A human approves
    everything. (CLAUDE.md governance: backtesting can recommend, not promote.)

WHY THIS IS THE CHEAPEST-FIRST MOVE
-----------------------------------
Kite cannot cheaply backfill expired weekly-option chains (tokens are purged), so
the option backtest needs paid vendor data. But the *premium itself* lives in the
relationship between implied (INDIA VIX) and subsequently-realised NIFTY vol —
both free. If VIX does NOT systematically exceed realised vol by a margin an order
of magnitude above the options cost stack, there is no point paying for chains.

WHAT VRP IS (and why forward-realised is not lookahead)
-------------------------------------------------------
VRP_t = VIX_t (implied, known at t) − realised vol of NIFTY over the *next* window.
It is an *ex-post measurement* of "did vol sellers get paid", not a tradable
signal — so using forward-realised vol here is correct, not leakage. A short-vol
seller at t earns ≈ VRP over the cycle.

HONEST LIMITS (stated up front)
-------------------------------
  • This screens the UNDERLYING phenomenon, with a coarse straddle-vega ₹ proxy.
    Real option P&L (skew, pin, gamma path, bid/ask) is an O-2 question.
  • Short vol is "pennies in front of a steamroller": a positive mean with a fat
    left tail. The tail (G4) is reported as a MANDATORY caution regardless of PASS.
  • Costs use a documented estimate (Zerodha flat ₹20/leg + the NSE options
    statutory stack). The formal IndianCostModel.options() is an O-2 deliverable.

DATA
----
Reads the Parquet lake written by ``fetch_zerodha_indices.py`` (segment=INDICES,
NIFTY50 + INDIAVIX, interval=1d). Or pass ``--nifty-csv`` / ``--vix-csv`` (free
NSE history CSVs with date,close). Run the fetcher locally first (needs a same-day
Kite token; the sandbox has no egress).

    python scripts/backtest/fetch_zerodha_indices.py --indices niftyvix --intervals 1d \
        --start 2020-01-01 --end 2025-12-31
    python scripts/backtest/run_vol_premium_study.py
    python scripts/backtest/run_vol_premium_study.py --self-test   # offline logic check

Backtest-only. Advisory. No orders, no live/paper trading, no capital changes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = _REPO / "backtest-data"
_REPORT = _REPO / "docs" / "backtesting" / "vol-premium-study-report.md"

# ── pre-registered O-1 thresholds (fixed 2026-06-19; DO NOT relax to force a PASS) ──
RV_WINDOW = 21              # trading days of forward realised vol (≈ VIX's 30-cal-day tenor)
CYCLE_STEP = 21            # non-overlapping monthly cycles for the ₹ proxy
NIFTY_LOT = 75             # NIFTY option lot size (current; was 50/25 earlier — note in report)
T_CAL = 30.0 / 365.0       # monthly straddle tenor for the ₹ proxy
MIN_MEAN_VRP = 1.0         # G1: implied must exceed realised by ≥1.0 vol point on average
MIN_POS_FRAC = 0.65        # G2a: VIX>realised on ≥65% of days
MIN_POS_YEARS = 0.70       # G2b: positive mean VRP in ≥70% of calendar years
G3_COST_MARGIN = 2.0       # G3: median gross cycle edge ≥ 2× median round-trip cost
PREREG_DATE = "2026-06-19"


# ── data loading ──────────────────────────────────────────────────────────────


def _load_lake_daily(base: Path, symbol: str) -> pd.DataFrame:
    root = base / "lake" / "ohlcv" / "market=NSE" / "segment=INDICES" / f"symbol={symbol}" / "interval=1d"
    parts = sorted(root.glob("year=*/part-*.parquet"))
    if not parts:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("Asia/Kolkata").dt.normalize().dt.tz_localize(None)
    return df[["date", "close"]].sort_values("date").drop_duplicates("date").reset_index(drop=True)


def _parse_dates(s: pd.Series) -> pd.Series:
    """Robust to BOTH ISO YYYY-MM-DD (yfinance) and DD-MM-YYYY (NSE). Forcing
    dayfirst on ISO strings silently NaTs ~60% of them, so try default first and
    only fall back to dayfirst when it parses strictly more dates."""
    a = pd.to_datetime(s, errors="coerce")
    if a.isna().mean() > 0.05:
        b = pd.to_datetime(s, dayfirst=True, errors="coerce")
        if b.isna().mean() < a.isna().mean():
            a = b
    return a.dt.normalize()


def _load_csv_daily(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in df.columns}
    dcol = cols.get("date") or list(df.columns)[0]
    ccol = cols.get("close") or cols.get("close ") or list(df.columns)[-1]
    out = pd.DataFrame({
        "date": _parse_dates(df[dcol]),
        "close": pd.to_numeric(df[ccol], errors="coerce"),
    }).dropna()
    return out.sort_values("date").drop_duplicates("date").reset_index(drop=True)


# ── core VRP computation ───────────────────────────────────────────────────────


def compute_vrp(nifty: pd.DataFrame, vix: pd.DataFrame, rv_window: int = RV_WINDOW) -> pd.DataFrame:
    """Daily frame: date, spot, vix(implied), rv_fwd(realised, annualised %), vrp."""
    m = pd.merge(nifty.rename(columns={"close": "spot"}),
                 vix.rename(columns={"close": "vix"}), on="date", how="inner").reset_index(drop=True)
    if len(m) < rv_window + 5:
        return pd.DataFrame()
    logret = np.log(m["spot"]).diff()
    # forward realised vol at t = std of returns over (t+1 .. t+rv_window], annualised, in vol points
    rv_fwd = np.full(len(m), np.nan)
    r = logret.to_numpy()
    for t in range(len(m) - rv_window):
        window = r[t + 1: t + 1 + rv_window]
        if np.isfinite(window).sum() >= rv_window - 1:
            rv_fwd[t] = np.nanstd(window, ddof=1) * np.sqrt(252) * 100.0
    m["rv_fwd"] = rv_fwd
    m["vrp"] = m["vix"] - m["rv_fwd"]
    m = m.dropna(subset=["vrp"]).reset_index(drop=True)
    m["year"] = m["date"].dt.year
    return m


def _regime(spot: pd.Series) -> pd.Series:
    sma = spot.rolling(50, min_periods=50).mean()
    rising = sma.diff(10) > 0
    out = pd.Series("chop", index=spot.index)
    out[(spot > sma) & rising] = "bull"
    out[(spot < sma) & ~rising] = "bear"
    out[sma.isna()] = "n/a"
    return out


# ── ₹ proxy: monthly short-straddle vega economics ─────────────────────────────


def _straddle_credit_inr(spot: float, vix: float, lot: int, t_cal: float = T_CAL) -> float:
    """ATM straddle credit proxy in ₹/lot ≈ 0.8·S·σ·√T · lot (straddle ≈ 2× ATM option)."""
    return 0.8 * spot * (vix / 100.0) * np.sqrt(t_cal) * lot


def _round_trip_cost_inr(straddle_credit_inr: float, n_legs: int = 4) -> float:
    """Estimated defined-risk iron-condor round-trip cost (Zerodha flat + NSE options stack).
    Coarse O-1 estimate; the formal model is an O-2 deliverable.
    n_legs=4 (condor) → 8 executed orders round-trip @ ₹20 flat each."""
    brokerage = 20.0 * n_legs * 2
    prem_turnover = max(straddle_credit_inr, 0.0) * 3.0   # condor legs gross ≈ 3× the net credit scale
    stt = 0.001 * prem_turnover * 0.5                     # STT 0.1% on sell-side premium (~half legs sold)
    txn = 0.00035 * prem_turnover                         # NSE options exchange txn ≈ 0.035% premium
    sebi = 0.000001 * prem_turnover                       # ₹10/crore
    stamp = 0.00003 * prem_turnover * 0.5                 # 0.003% on buy-side premium
    gst = 0.18 * (brokerage + txn + sebi)
    return brokerage + stt + txn + sebi + stamp + gst


def monthly_cycles(daily: pd.DataFrame, step: int = CYCLE_STEP, lot: int = NIFTY_LOT) -> pd.DataFrame:
    """Non-overlapping cycles (avoids autocorrelation inflation): at each cycle start,
    gross VRP edge ≈ 0.8·S·√T·(VRP/100)·lot vs an estimated round-trip cost."""
    rows = []
    for i in range(0, len(daily), step):
        row = daily.iloc[i]
        credit = _straddle_credit_inr(row["spot"], row["vix"], lot)
        gross = 0.8 * row["spot"] * np.sqrt(T_CAL) * (row["vrp"] / 100.0) * lot
        cost = _round_trip_cost_inr(credit)
        rows.append({"date": row["date"], "year": int(row["year"]), "vrp": row["vrp"],
                     "credit_inr": credit, "gross_edge_inr": gross,
                     "cost_inr": cost, "net_edge_inr": gross - cost})
    return pd.DataFrame(rows)


# ── evaluation against the pre-registered gate ─────────────────────────────────


def evaluate(daily: pd.DataFrame, cycles: pd.DataFrame) -> dict:
    vrp = daily["vrp"].to_numpy()
    mean_vrp = float(np.mean(vrp))
    median_vrp = float(np.median(vrp))
    pos_frac = float((vrp > 0).mean())

    by_year = daily.groupby("year")["vrp"].mean()
    pos_years = float((by_year > 0).mean())

    reg = _regime(daily["spot"])
    by_regime = daily.assign(regime=reg).groupby("regime")["vrp"].agg(["mean", "count"])

    g_gross = cycles["gross_edge_inr"].to_numpy()
    g_cost = cycles["cost_inr"].to_numpy()
    g_net = cycles["net_edge_inr"].to_numpy()
    med_gross, med_cost, med_net = float(np.median(g_gross)), float(np.median(g_cost)), float(np.median(g_net))

    # short-vol cumulative equity proxy (sum of net cycle edge) → tail/drawdown
    eq = np.cumsum(g_net)
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    max_dd = float((eq - peak).min()) if len(eq) else 0.0
    worst_cycle = float(g_net.min()) if len(g_net) else 0.0
    tail_ratio = float(abs(worst_cycle) / med_gross) if med_gross > 0 else float("inf")

    checks = {
        f"G1 mean VRP>{MIN_MEAN_VRP}": mean_vrp > MIN_MEAN_VRP,
        f"G2a VIX>RV on>={MIN_POS_FRAC:.0%} days": pos_frac >= MIN_POS_FRAC,
        f"G2b +mean VRP in>={MIN_POS_YEARS:.0%} years": pos_years >= MIN_POS_YEARS,
        f"G3 gross>={G3_COST_MARGIN:.0f}x cost & net>0": (med_gross >= G3_COST_MARGIN * med_cost) and (med_net > 0),
    }
    verdict = ("PASS — VRP justifies buying option-chain data (O-2). NOT deployment."
               if all(checks.values())
               else "SHELVE — VRP does not clear the bar; do not pay for chains.")
    return {
        "n_days": len(daily), "years": f"{daily['date'].min().date()} → {daily['date'].max().date()}",
        "mean_vrp": mean_vrp, "median_vrp": median_vrp, "pos_frac": pos_frac,
        "pos_years": pos_years, "by_year": by_year, "by_regime": by_regime,
        "n_cycles": len(cycles), "med_gross": med_gross, "med_cost": med_cost, "med_net": med_net,
        "max_dd": max_dd, "worst_cycle": worst_cycle, "tail_ratio": tail_ratio,
        "checks": checks, "verdict": verdict,
    }


# ── report ─────────────────────────────────────────────────────────────────────


def _write_report(res: dict, out: Path) -> None:
    yr = "\n".join(f"| {int(y)} | {v:+.2f} |" for y, v in res["by_year"].items())
    reg = "\n".join(f"| {idx} | {row['mean']:+.2f} | {int(row['count'])} |"
                    for idx, row in res["by_regime"].iterrows())
    chk = "\n".join(f"| {k} | {'✅ PASS' if v else '❌ FAIL'} |" for k, v in res["checks"].items())
    tail_flag = ("⚠️ **FAT LEFT TAIL** — worst cycle loses "
                 f"{res['tail_ratio']:.1f}× the median gross edge"
                 if res["tail_ratio"] > 5 else
                 f"worst cycle = {res['tail_ratio']:.1f}× median gross edge")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"""# Volatility-Premium Screen (Options/Vol track — Phase O-1, FREE)

**Pre-registered:** {PREREG_DATE} · **Status:** advisory screen · **Live trading: BLOCKED**
A PASS authorises *buying option-chain data for an O-2 backtest only* — never deployment.
Backtesting can recommend; it cannot promote. A human approves all production changes.

## What this measures
VRP = INDIA VIX (implied) − NIFTY 50 realised vol over the next {RV_WINDOW} trading days,
in annualised vol points. Forward-realised is an *ex-post measurement* of whether vol
sellers got paid — not a tradable signal, so it is not lookahead.

## Data
- Window: **{res['years']}**  ·  trading days: **{res['n_days']:,}**  ·  monthly cycles: **{res['n_cycles']}**
- Source: Zerodha Kite indices + INDIA VIX (free underlying inputs). NIFTY lot = {NIFTY_LOT}
  (current; was 50/25 earlier — the ₹ proxy uses the current lot).

## VRP — is the premium there?
- Mean VRP: **{res['mean_vrp']:+.2f}** vol points · Median: **{res['median_vrp']:+.2f}**
- VIX > realised on **{res['pos_frac']*100:.0f}%** of days
- Positive-mean-VRP in **{res['pos_years']*100:.0f}%** of calendar years

### By year (mean VRP, vol points)
| Year | Mean VRP |
|---|---:|
{yr}

### By regime (the short-vol failure mode lives here)
| Regime | Mean VRP | Days |
|---|---:|---:|
{reg}

## ₹ economics — coarse monthly short-straddle vega proxy (defined-risk condor costs)
Per cycle, per NIFTY lot (estimates — formal options cost model is an O-2 deliverable):
- Median **gross** VRP edge: **₹{res['med_gross']:,.0f}**
- Median estimated **round-trip cost**: **₹{res['med_cost']:,.0f}**
- Median **net** edge: **₹{res['med_net']:,.0f}**

## Tail — the steamroller (MANDATORY caution, independent of verdict)
- Worst single cycle (short-vol proxy): **₹{res['worst_cycle']:,.0f}**
- Cumulative short-vol proxy max drawdown: **₹{res['max_dd']:,.0f}**
- {tail_flag}

Short vol earns small premiums most of the time and gives them back in crashes. A
positive mean with a fat tail is NOT a green light — O-2 must size for the tail and
test crash days (Mar-2020, budget/election/expiry gaps) explicitly.

## Pre-registered gate (fixed {PREREG_DATE} — not relaxed)
| Criterion | Result |
|---|---|
{chk}

## VERDICT
**{res['verdict']}**

---
*Advisory only. No orders, no live/paper trading, no capital changes. If PASS, the next
step is procuring 3–5 yr NIFTY option-chain history (Algotest export / GDFL / TrueData)
for a defined-risk O-2 backtest with the real options cost stack — then human review.*
""")


# ── self-test (offline, no data files) ─────────────────────────────────────────


def _synth(seed: int, vix_level: float, ann_vol: float, n: int = 900) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n)
    # ann_vol & vix_level are in vol POINTS (percent), e.g. 12.0 = 12%; returns need the fraction.
    daily_sigma = (ann_vol / 100.0) / np.sqrt(252)
    rets = rng.normal(0, daily_sigma, n)
    spot = 15000.0 * np.exp(np.cumsum(rets))
    nifty = pd.DataFrame({"date": dates, "close": spot})
    vix = pd.DataFrame({"date": dates, "close": np.full(n, vix_level) + rng.normal(0, 0.3, n)})
    return nifty, vix


def _self_test() -> int:
    print("SELF-TEST: VRP screen logic on synthetic data...")

    # Case A: implied (VIX≈18) consistently above realised (≈12) → premium exists → G1/G2 pass.
    nA, vA = _synth(seed=1, vix_level=18.0, ann_vol=12.0)
    dA = compute_vrp(nA, vA)
    assert not dA.empty
    rA = evaluate(dA, monthly_cycles(dA))
    assert rA["mean_vrp"] > MIN_MEAN_VRP, rA["mean_vrp"]
    assert rA["checks"][f"G1 mean VRP>{MIN_MEAN_VRP}"], "A should pass G1"
    assert rA["checks"][f"G2a VIX>RV on>={MIN_POS_FRAC:.0%} days"], "A should pass G2a"
    print(f"  A (premium present): mean VRP {rA['mean_vrp']:+.2f}, pos {rA['pos_frac']*100:.0f}% "
          f"→ verdict starts {rA['verdict'][:5]!r}")

    # Case B: implied ≈ realised (VIX≈12, realised≈12) → no premium → G1 fails → SHELVE.
    nB, vB = _synth(seed=2, vix_level=12.0, ann_vol=12.0)
    dB = compute_vrp(nB, vB)
    rB = evaluate(dB, monthly_cycles(dB))
    assert not rB["checks"][f"G1 mean VRP>{MIN_MEAN_VRP}"], f"B should FAIL G1 (mean {rB['mean_vrp']:+.2f})"
    assert rB["verdict"].startswith("SHELVE"), rB["verdict"]
    print(f"  B (no premium):      mean VRP {rB['mean_vrp']:+.2f} → {rB['verdict'][:6]!r}")

    # Report writer round-trips.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "r.md"
        _write_report(rA, p)
        assert p.exists() and "VERDICT" in p.read_text()
    print("SELF-TEST PASSED.")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Volatility-premium screen (O-1, free underlying data)")
    ap.add_argument("--base", default=str(_DEFAULT_BASE), help="Lake base directory")
    ap.add_argument("--nifty-csv", help="Free NSE NIFTY history CSV (date,close) — overrides lake")
    ap.add_argument("--vix-csv", help="Free NSE INDIA VIX history CSV (date,close) — overrides lake")
    ap.add_argument("--out", default=str(_REPORT), help="Report output path")
    ap.add_argument("--self-test", action="store_true", help="Offline logic check (no data)")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    base = Path(args.base)
    nifty = _load_csv_daily(Path(args.nifty_csv)) if args.nifty_csv else _load_lake_daily(base, "NIFTY50")
    vix = _load_csv_daily(Path(args.vix_csv)) if args.vix_csv else _load_lake_daily(base, "INDIAVIX")

    print("=" * 76)
    print("QuantEmbrace — Volatility-Premium Screen (Options/Vol track, Phase O-1, FREE)")
    print("Advisory screen. A PASS only justifies BUYING option-chain data. Live BLOCKED.")
    print("=" * 76)
    if nifty.empty or vix.empty:
        print("\n  No data. Fetch the free underlying inputs first (run LOCALLY with a Kite token):")
        print("    python scripts/backtest/fetch_zerodha_indices.py --indices niftyvix "
              "--intervals 1d --start 2020-01-01 --end 2025-12-31")
        print("  …or pass --nifty-csv / --vix-csv with free NSE history CSVs (date,close).")
        return 1

    daily = compute_vrp(nifty, vix)
    if daily.empty:
        print("  Not enough overlapping NIFTY/VIX history to compute VRP.", file=sys.stderr)
        return 1
    cycles = monthly_cycles(daily)
    res = evaluate(daily, cycles)

    print(f"\n  Window {res['years']}  ·  {res['n_days']:,} days  ·  {res['n_cycles']} cycles")
    print(f"  Mean VRP {res['mean_vrp']:+.2f} vp · median {res['median_vrp']:+.2f} · "
          f"VIX>RV {res['pos_frac']*100:.0f}% days · +years {res['pos_years']*100:.0f}%")
    print(f"  ₹/lot/cycle: gross {res['med_gross']:,.0f} · cost {res['med_cost']:,.0f} · "
          f"net {res['med_net']:,.0f}")
    print(f"  Tail: worst cycle ₹{res['worst_cycle']:,.0f} · maxDD ₹{res['max_dd']:,.0f} · "
          f"{res['tail_ratio']:.1f}× median gross")
    print("  Gate:")
    for k, v in res["checks"].items():
        print(f"    {'✅' if v else '❌'} {k}")
    print(f"\n  VERDICT: {res['verdict']}")

    _write_report(res, Path(args.out))
    print(f"\n  Report → {args.out}")
    print("  Advisory only. No orders, no live trading, no capital changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
