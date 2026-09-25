#!/usr/bin/env python3
"""
QuantEmbrace — F1: Overnight index-futures premium screen (Options/Futures program).

THESIS
------
C6 proved the NSE equity premium accrues OVERNIGHT (cash NIFTY close→next-open ≈ +13 bps/day,
Sharpe 3.2) while intraday is negative. Cash can't harvest it (daily round-trip delivery cost ~0.22%
kills 13 bps). FUTURES costs are ~10× lower — so the real F1 question is twofold:
  (1) Does the overnight premium SURVIVE the much-lower futures cost stack? (cash's killer)
  (2) Is the LEVERAGED overnight GAP TAIL survivable on a ₹5L account? (futures' killer)

This is the cheap screen (like the O-1 VRP screen): it uses the NIFTY50 spot OHLC already in the lake
as the futures-move proxy. Basis decay (a few bps/day against a long in contango) and roll are OMITTED
here and are the key refinements for the full test (F1-full) IF this screen passes.

HONEST PRIOR
------------
The premium likely SURVIVES futures costs (unlike cash) — but a single COVID-type overnight gap on one
leveraged lot can take 20–40% of a ₹5L account in one night. So the expected outcome is: passes cost,
FAILS account-fit (naked is too dangerous) → motivates a DEFINED-RISK variant (long future + protective
put) or shelve. The gate below tests exactly that, with an account-ruin criterion, not just return.

Advisory only. Live trading BLOCKED. No orders. A PASS ⇒ build F1-full on REAL futures, never deploy.
    python scripts/backtest/run_overnight_futures_study.py            # on NIFTY50 lake
    python scripts/backtest/run_overnight_futures_study.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = _REPO / "backtest-data"
_REPORT = _REPO / "docs" / "backtesting" / "overnight-futures-study-report.md"

# ── pre-registered F1 gate (fixed 2026-06-20; DO NOT relax to force a PASS) ─────
NAV = 500_000.0            # the real account (₹5L)
NIFTY_LOT = 75
MARGIN_PCT = 0.15          # SPAN+exposure ≈ 12–15% of notional for NIFTY futures
MAX_NIGHT_LOSS_PCT = 0.25  # F1-G3: worst single overnight at trading size must be ≤25% of NAV (no near-ruin)
MIN_POS_YEARS = 0.70
MIN_SHARPE = 1.0
MIN_ANN_RET = 0.12
F1_MAX_DD = 0.25           # F1-full only: max drawdown ≤25% of NAV (the DD criterion the screen gate lacked)
PREREG_DATE = "2026-06-20"


@dataclass
class FuturesCostModel:
    """NSE index-futures round-trip cost (per lot). Far lower than cash delivery — the point of F1.
    Rates ~2024-10: brokerage ₹20/order (or 0.03%, min applies), STT 0.0125% sell, exch txn ~0.0019%,
    SEBI ₹10/cr, stamp 0.002% buy, GST 18% on (brokerage+txn+sebi)."""
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.0125
    exch_txn_pct: float = 0.0019
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.002
    gst_pct: float = 18.0
    version: str = "in-fut-2024.10"

    def round_trip(self, notional: float) -> float:
        brokerage = self.brokerage_per_order * 2
        stt = (self.stt_sell_pct / 100.0) * notional               # sell leg only
        txn = (self.exch_txn_pct / 100.0) * notional * 2
        sebi = (self.sebi_pct / 100.0) * notional * 2
        stamp = (self.stamp_buy_pct / 100.0) * notional            # buy leg only
        gst = (self.gst_pct / 100.0) * (brokerage + txn + sebi)
        return brokerage + stt + txn + sebi + stamp + gst


def _load_ohlc(base: Path, symbol: str) -> pd.DataFrame:
    root = base / "lake" / "ohlcv" / "market=NSE" / "segment=INDICES" / f"symbol={symbol}" / "interval=1d"
    parts = sorted(root.glob("year=*/part-*.parquet"))
    if not parts:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("Asia/Kolkata").dt.normalize().dt.tz_localize(None)
    return df[["date", "open", "high", "low", "close"]].sort_values("date").drop_duplicates("date").reset_index(drop=True)


def _overnight_stats(on: np.ndarray, notional: np.ndarray, yrs: np.ndarray,
                     cost: FuturesCostModel) -> dict:
    """Shared metric block for an overnight return series (spot proxy or real futures)."""
    costs = np.array([cost.round_trip(n) for n in notional])
    net = on * notional - costs
    net_ret = net / notional
    ann = 252.0
    sharpe = float(net_ret.mean() / net_ret.std() * np.sqrt(ann)) if net_ret.std() > 0 else 0.0
    eq = NAV + np.cumsum(net)
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq / peak - 1.0).min())
    ann_ret = float((eq[-1] / NAV) ** (ann / len(net)) - 1) if len(net) else 0.0
    wi = int(np.argmin(on))
    by_year = pd.Series(net).groupby(yrs).sum()
    margin = float(np.median(notional) * MARGIN_PCT)
    return {"n": len(net), "on_bps": float(on.mean() * 1e4), "mean_net_inr": float(net.mean()),
            "sharpe": sharpe, "ann_ret_1lot": ann_ret, "max_dd": max_dd,
            "worst_on": float(on.min()), "worst_night_inr": float(on[wi] * notional[wi]),
            "worst_night_pct": float(on[wi] * notional[wi] / NAV),
            "n_gap3": int((on < -0.03).sum()), "n_gap5": int((on < -0.05).sum()),
            "by_year": by_year, "pos_years": float((by_year > 0).mean()),
            "margin_per_lot": margin, "max_lots_5L": int(NAV // margin)}


def _gate(s: dict, dd_aware: bool) -> dict:
    checks = {
        "G1 net overnight>0 (beats futures cost)": s["mean_net_inr"] > 0,
        f"G2 pos-years>={MIN_POS_YEARS:.0%}": s["pos_years"] >= MIN_POS_YEARS,
        f"G3 worst night ≤{MAX_NIGHT_LOSS_PCT:.0%} NAV @1 lot": s["worst_night_pct"] >= -MAX_NIGHT_LOSS_PCT,
        f"G4 Sharpe≥{MIN_SHARPE} & ann≥{MIN_ANN_RET:.0%}": s["sharpe"] >= MIN_SHARPE and s["ann_ret_1lot"] >= MIN_ANN_RET,
    }
    if dd_aware:
        checks[f"G5 maxDD≤{F1_MAX_DD:.0%}"] = s["max_dd"] >= -F1_MAX_DD
    return checks


def study(ohlc: pd.DataFrame, cost: FuturesCostModel | None = None) -> dict:
    cost = cost or FuturesCostModel()
    o = ohlc["open"].to_numpy(); c = ohlc["close"].to_numpy(); d = ohlc["date"]
    on = o[1:] / c[:-1] - 1.0
    intr = c / o - 1.0
    yrs = d.dt.year.to_numpy()[:-1]
    notional = c[:-1] * NIFTY_LOT
    s = _overnight_stats(on, notional, yrs, cost)
    s["span"] = f"{d.min().date()} → {d.max().date()}"
    s["intr_bps"] = float(intr[:-1].mean() * 1e4)
    s["mode"] = "NIFTY50 spot proxy (basis/roll omitted — screen)"
    s["checks"] = _gate(s, dd_aware=False)   # screen: original pre-registered 4-gate (no DD criterion)
    s["verdict"] = ("PASS — overnight futures premium viable; build F1-full on REAL futures. NOT deploy."
                    if all(s["checks"].values())
                    else "FAIL — see which criterion broke (likely G3 tail → needs defined-risk hedge).")
    return s


def _load_near_month_futures(base: Path, underlying: str) -> pd.DataFrame:
    root = base / "lake" / "futures" / f"underlying={underlying}"
    parts = sorted(root.glob("date=*/part-0.parquet"))
    if not parts:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.normalize()
    return df


def study_futures(base: Path, underlying: str = "NIFTY", cost: FuturesCostModel | None = None) -> dict:
    """F1-full: overnight on REAL near-month futures. Each night buy the front-month (nearest expiry
    strictly after t, so it survives to t+1) at close, sell at next open — basis is in the prices.
    Stricter DD-aware gate (adds G5 maxDD)."""
    cost = cost or FuturesCostModel()
    df = _load_near_month_futures(base, underlying)
    if df.empty:
        return {}
    close_map = {(r.trade_date, r.expiry): r.close for r in df.itertuples()}
    open_map = {(r.trade_date, r.expiry): r.open for r in df.itertuples()}
    tds = sorted(df["trade_date"].unique())
    exps = sorted(df["expiry"].unique())
    on, notional, yrs, dates = [], [], [], []
    for t, t1 in zip(tds, tds[1:]):
        future_exps = [e for e in exps if e > t]
        if not future_exps:
            continue
        held = future_exps[0]                    # front month that survives the overnight
        ct = close_map.get((t, held)); ot1 = open_map.get((t1, held))
        if ct is None or ot1 is None or ct <= 0 or ot1 <= 0:
            continue
        on.append(ot1 / ct - 1.0); notional.append(ct * NIFTY_LOT)
        yrs.append(pd.Timestamp(t).year); dates.append(t)
    if len(on) < 30:
        return {"error": f"too few futures nights ({len(on)})"}
    on = np.array(on); notional = np.array(notional); yrs = np.array(yrs)
    s = _overnight_stats(on, notional, yrs, cost)
    s["span"] = f"{pd.Timestamp(tds[0]).date()} → {pd.Timestamp(tds[-1]).date()}"
    s["intr_bps"] = float("nan")             # intraday split already established on spot (C6)
    s["mode"] = f"REAL near-month {underlying} futures (basis+roll included)"
    s["checks"] = _gate(s, dd_aware=True)    # F1-full: stricter 5-gate incl maxDD
    s["verdict"] = ("PASS — real-futures overnight carry clears the DD-aware gate; build forward book. NOT deploy."
                    if all(s["checks"].values())
                    else "FAIL — naked real-futures overnight does not clear the DD-aware gate (the tail/DD); "
                         "next = defined-risk hedged variant (long future + protective put).")
    return s


def _write_report(r: dict, out: Path) -> None:
    yr = "\n".join(f"| {int(y)} | ₹{v:,.0f} |" for y, v in r["by_year"].items())
    chk = "\n".join(f"| {k} | {'✅ PASS' if v else '❌ FAIL'} |" for k, v in r["checks"].items())
    out.parent.mkdir(parents=True, exist_ok=True)
    intr = f"{r['intr_bps']:+.1f} bps/day" if r.get("intr_bps") == r.get("intr_bps") else "n/a (see C6)"
    out.write_text(f"""# F1 — Overnight Index-Futures Premium ({r.get('mode', 'screen')})

**Pre-registered:** {PREREG_DATE} · **Data:** {r.get('mode', 'NIFTY50 spot proxy')} · **Live trading:
BLOCKED** · Advisory only.

## Question
(1) Does the overnight premium survive the **futures** cost stack ({FuturesCostModel().version})?
(2) Is the leveraged overnight **gap tail** survivable on a **₹{NAV:,.0f}** account?

## Overnight vs intraday ({r['span']}, {r['n']} days)
- Overnight (close→next open): **{r['on_bps']:+.1f} bps/day** · Intraday (open→close): **{intr}**

## Economics @ 1 NIFTY lot (lot {NIFTY_LOT}; notional ≈ {np.median([1]) and ''}2.7–3.9× the ₹5L NAV — leveraged)
- Mean net overnight P&L: **₹{r['mean_net_inr']:,.0f}/night** · net Sharpe (ann): **{r['sharpe']:.2f}**
- 1-lot annualised return on ₹5L: **{r['ann_ret_1lot']*100:+.1f}%** · max drawdown: **{r['max_dd']*100:.1f}%**
- Positive years: **{r['pos_years']*100:.0f}%**

### By year (net P&L, 1 lot)
| Year | Net |
|---|---:|
{yr}

## The gap tail (the futures killer)
- Worst single overnight: **{r['worst_on']*100:.1f}%** = **₹{r['worst_night_inr']:,.0f}** = **{r['worst_night_pct']*100:.1f}% of NAV in one night**
- Nights worse than −3%: **{r['n_gap3']}** · worse than −5%: **{r['n_gap5']}**
- Affordable lots on ₹5L (margin ≈{MARGIN_PCT:.0%}/lot ≈ ₹{r['margin_per_lot']:,.0f}): **{r['max_lots_5L']}**

## Pre-registered F1 gate (fixed {PREREG_DATE} — not relaxed)
| Criterion | Result |
|---|---|
{chk}

## VERDICT
**{r['verdict']}**

---
*A naked leveraged overnight carry that fails only on the tail (G3) is NOT dead — it argues for the
DEFINED-RISK variant: long future + protective OTM put (priced from our options lake) = risk-capped
overnight carry. F1-full would test that on REAL futures with basis + roll. PASS ⇒ forward book → human
review for a small gated pilot. Never auto-deploy. Live BLOCKED.*
""")


def _self_test() -> int:
    print("SELF-TEST: overnight-futures screen...")
    cm = FuturesCostModel()
    rt = cm.round_trip(75 * 18000)         # ~₹13.5L notional
    assert 200 < rt < 600, rt              # round-trip ≈ ₹0.02% of notional, far below cash 0.22%
    print(f"  futures round-trip on ₹13.5L ≈ ₹{rt:,.0f} ({rt/(75*18000)*100:.3f}%) — far below cash 0.22% ✅")

    # synthetic path with a known +overnight / −intraday split and one crash gap
    rng = np.random.default_rng(0); n = 800
    on = rng.normal(0.0012, 0.006, n)       # +12 bps overnight
    idr = rng.normal(-0.0006, 0.006, n)     # −6 bps intraday
    on[400] = -0.11                          # inject a crash gap
    close = [18000.0]; rows = []
    for t in range(n):
        op = close[-1] * (1 + on[t])         # overnight gap to open
        cl = op * (1 + idr[t])               # intraday to close
        rows.append({"date": pd.Timestamp("2021-01-01") + pd.Timedelta(days=t), "open": op,
                     "high": max(op, cl), "low": min(op, cl), "close": cl})
        close.append(cl)
    r = study(pd.DataFrame(rows))
    assert r["on_bps"] > 0 > r["intr_bps"], (r["on_bps"], r["intr_bps"])
    assert r["checks"]["G1 net overnight>0 (beats futures cost)"], "should beat low futures cost"
    assert not r["checks"][f"G3 worst night ≤{MAX_NIGHT_LOSS_PCT:.0%} NAV @1 lot"], "injected crash must break G3"
    print(f"  detects +on/−intraday; G1 passes (cost survived); injected −11% gap breaks G3 "
          f"(worst night {r['worst_night_pct']*100:.0f}% NAV) ✅")

    # real-futures path: synthetic futures lake → study_futures builds nights + DD-aware 5-gate
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        days = pd.bdate_range("2022-01-03", periods=120)
        sp = 18000.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.008, len(days))))
        expiries = [pd.Timestamp(days[k]) for k in (19, 40, 61, 82, 103)]
        rows = []
        for i, d in enumerate(days):
            so, sc = float(sp[i]) * 1.0003, float(sp[i])
            for e in expiries:
                if e >= d and (e - d).days <= 95:
                    f = 1 + 0.00015 * (e - d).days        # mild contango basis, decays to expiry
                    rows.append({"trade_date": d, "expiry": e, "underlying": "NIFTY",
                                 "open": so * f, "high": so * f * 1.005, "low": so * f * 0.995,
                                 "close": sc * f, "settle": sc * f, "oi": 1000, "volume": 1000})
        fdf = pd.DataFrame(rows)
        for d, grp in fdf.groupby("trade_date"):
            dd = base / "lake" / "futures" / "underlying=NIFTY" / f"date={pd.Timestamp(d).date().isoformat()}"
            dd.mkdir(parents=True, exist_ok=True)
            grp.to_parquet(dd / "part-0.parquet", index=False)
        rf = study_futures(base, "NIFTY")
        assert rf and "error" not in rf and rf["n"] > 30, rf
        assert any("G5" in k for k in rf["checks"]), "F1-full gate must include G5 maxDD"
        print(f"  real-futures path: {rf['n']} nights from synthetic lake; gate includes G5 maxDD ✅")
        p = Path(tmp) / "r.md"; _write_report(r, p); assert p.exists() and "VERDICT" in p.read_text()
    print("SELF-TEST PASSED.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="F1 overnight index-futures premium (screen + real-futures)")
    ap.add_argument("--symbol", default="NIFTY50", help="spot index symbol for the screen")
    ap.add_argument("--futures", action="store_true", help="F1-full: use REAL near-month futures lake (basis+roll, DD-aware gate)")
    ap.add_argument("--underlying", default="NIFTY", help="futures underlying (NIFTY/BANKNIFTY) for --futures")
    ap.add_argument("--base", default=str(_DEFAULT_BASE))
    ap.add_argument("--out", default=str(_REPORT))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    print("=" * 76)
    print("QuantEmbrace — F1 Overnight Index-Futures Premium. Advisory. Live BLOCKED.")
    print("=" * 76)
    if args.futures:
        r = study_futures(Path(args.base), args.underlying)
        if not r or "error" in r:
            print(f"  No usable {args.underlying} futures lake ({r.get('error', 'empty') if r else 'empty'}).")
            print("  Download it first (run LOCALLY):")
            print("    python scripts/backtest/download_fo_futures.py --start 2022-06-01 --end 2025-06-30")
            return 1
    else:
        ohlc = _load_ohlc(Path(args.base), args.symbol)
        if ohlc.empty:
            print(f"  No {args.symbol} OHLC in lake. Fetch via fetch_zerodha_indices.py / kite_fetch_with_token.py.")
            return 1
        r = study(ohlc)
    intr_str = f"{r['intr_bps']:+.1f}" if r.get("intr_bps") == r.get("intr_bps") else "n/a"
    print(f"\n  {r['mode']} · {r['span']} · {r['n']} nights")
    print(f"  overnight {r['on_bps']:+.1f} bps/night vs intraday {intr_str} bps (C6 grounding)")
    print(f"  net/night ₹{r['mean_net_inr']:,.0f} · Sharpe {r['sharpe']:.2f} · 1-lot ann {r['ann_ret_1lot']*100:+.1f}% · maxDD {r['max_dd']*100:.1f}%")
    print(f"  TAIL: worst night {r['worst_on']*100:.1f}% = {r['worst_night_pct']*100:.0f}% of NAV (₹{r['worst_night_inr']:,.0f}); "
          f"<-3%: {r['n_gap3']} nights, <-5%: {r['n_gap5']} nights")
    print(f"  affordable lots on ₹5L: {r['max_lots_5L']} (margin ₹{r['margin_per_lot']:,.0f}/lot)")
    for k, v in r["checks"].items():
        print(f"    {'✅' if v else '❌'} {k}")
    print(f"\n  VERDICT: {r['verdict']}")
    _write_report(r, Path(args.out))
    print(f"\n  Report → {args.out}\n  Advisory only. Proxy=spot (basis/roll omitted). Live BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
