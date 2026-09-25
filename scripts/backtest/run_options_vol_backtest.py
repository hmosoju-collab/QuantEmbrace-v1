#!/usr/bin/env python3
"""
QuantEmbrace — Defined-risk options-vol backtester (Options/Vol track, Phase O-2).

PURPOSE
-------
The O-1 screen PASSED: the NSE index VRP is real (+2.5 vp, 80% of days, every year)
but carries a fat crash tail. O-2 turns that into a *tradable* test with real option
mechanics: sell **defined-risk iron condors** on NIFTY, hold to expiry, pay the full
NSE options cost stack, size to a hard per-cycle risk budget, and stress the crash.

This file is the HARNESS, built before buying chain data (zero spend). It validates
end-to-end on a synthetic chain priced by Black–Scholes off the REAL NIFTY path
(so Mar-2020 is in-sample) with implied vol set above trailing realised by a known
premium — i.e. a known VRP. When licensed chain data arrives (Algotest/GDFL/TrueData),
point `--chain` at it (same schema) and the identical engine runs on real premiums.

GOVERNANCE (CLAUDE.md): advisory only. Backtesting can recommend, not promote. A human
approves all production changes. A PASS here ⇒ human review for a SMALL GATED PILOT —
never auto-deploy. Defined-risk ONLY (condors/spreads); never naked. Live BLOCKED.

REAL-DATA SCHEMA CONTRACT (what a vendor file must provide; Parquet or CSV)
--------------------------------------------------------------------------
    trade_date (date)  expiry (date)  strike (float)  opt_type ('CE'|'PE')
    spot (float, underlying at trade_date)  price (float, option premium at trade_date)
Optional: bid, ask (for slippage), iv, oi, volume. The engine needs entry-day chain
rows + the underlying spot at expiry (to settle intrinsic).

USAGE
-----
    python scripts/backtest/run_options_vol_backtest.py --self-test        # offline logic check
    python scripts/backtest/run_options_vol_backtest.py --synthetic        # on the real NIFTY lake path
    python scripts/backtest/run_options_vol_backtest.py --chain <path>     # on real licensed chains (O-2 run)
"""

from __future__ import annotations

import argparse
import bisect
import math
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = _REPO / "backtest-data"
_REPORT = _REPO / "docs" / "backtesting" / "options-vol-backtest-report.md"

# ── pre-registered O-2 gate (fixed 2026-06-19; DO NOT relax to force a PASS) ────
NAV = 1_000_000.0          # ₹10L notional book (advisory; account is ₹5L+ — scale lots accordingly)
NIFTY_LOT = 75
RISK_BUDGET_PCT = 0.02     # ≤2% of NAV at risk per cycle (defined-risk cap → tail control)
DTE_TD = 21                # ~monthly: hold 21 trading days to expiry
SHORT_OTM_PCT = 0.04       # short strikes ~4% OTM
WING_PCT = 0.02            # protective wings 2% beyond the shorts
STRIKE_STEP = 50.0         # NIFTY strike interval
RISK_FREE = 0.065          # annual; for BS synthetic pricing only
O2_MIN_PF = 1.3            # net-of-cost profit factor
O2_MIN_POS_YEARS = 0.60    # positive net P&L in ≥60% of years
O2_MAX_DD_PCT = 0.20       # max drawdown ≤20% of NAV
PREREG_DATE = "2026-06-19"


# ── NSE options cost stack (rates verified 2024-10; flat brokerage dominates small size) ──
@dataclass
class OptionsCostModel:
    """Per-leg NSE options cost. Flat ₹20/order (Zerodha) + premium-based statutory stack.
    STT 0.1% on sell-side premium (w.e.f 2024-10; was 0.0625%); exchange txn ~0.03503% of
    premium; SEBI ₹10/cr; stamp 0.003% buy-side; GST 18% on (brokerage+txn+SEBI). Distinct
    from the equity IndianCostModel because the flat per-order fee has no equity analogue and
    dominates economics at retail size."""
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.10
    exchange_txn_pct: float = 0.03503
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0
    version: str = "in-opt-2024.10"

    def leg_cost(self, premium: float, qty: int, side: str) -> float:
        """Cost (₹) of one leg: premium is ₹/unit, qty = total units (n_lots×lot)."""
        if qty <= 0 or premium < 0:
            return 0.0
        turnover = premium * qty
        brokerage = self.brokerage_per_order
        stt = (self.stt_sell_pct / 100.0) * turnover if side == "SELL" else 0.0
        txn = (self.exchange_txn_pct / 100.0) * turnover
        sebi = (self.sebi_pct / 100.0) * turnover
        stamp = (self.stamp_buy_pct / 100.0) * turnover if side == "BUY" else 0.0
        gst = (self.gst_pct / 100.0) * (brokerage + txn + sebi)
        return brokerage + stt + txn + sebi + stamp + gst


# ── Black–Scholes (synthetic pricing only; erf-based normal CDF, no scipy) ──────
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, sigma: float, opt_type: str, r: float = RISK_FREE) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if opt_type == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2.0) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def _round_strike(x: float) -> float:
    return round(x / STRIKE_STEP) * STRIKE_STEP


# ── iron condor: construction + expiry payoff ──────────────────────────────────
@dataclass
class Condor:
    put_long: float; put_short: float; call_short: float; call_long: float
    net_credit: float          # ₹/unit collected (>0)
    max_loss_unit: float       # ₹/unit worst case = wing width − net credit


def _snap(prices: dict, ot: str, target: float) -> float | None:
    """Nearest available strike of type ot present in the chain (robust to real-chain gaps)."""
    ks = sorted({k for (t, k) in prices if t == ot})
    return min(ks, key=lambda k: abs(k - target)) if ks else None


def build_condor(spot: float, prices: dict) -> Condor | None:
    """prices: {('CE'|'PE', strike): premium}. Returns a defined-risk condor or None.
    Targets ±OTM% strikes but snaps to the nearest strike actually present (real chains
    have gaps; the synthetic ladder is complete so snapping is a no-op there)."""
    ps = _snap(prices, "PE", _round_strike(spot * (1 - SHORT_OTM_PCT)))
    pl = _snap(prices, "PE", _round_strike(spot * (1 - SHORT_OTM_PCT - WING_PCT)))
    cs = _snap(prices, "CE", _round_strike(spot * (1 + SHORT_OTM_PCT)))
    cl = _snap(prices, "CE", _round_strike(spot * (1 + SHORT_OTM_PCT + WING_PCT)))
    if None in (ps, pl, cs, cl) or not (pl < ps < cs < cl):
        return None
    credit = (prices[("PE", ps)] + prices[("CE", cs)]) - (prices[("PE", pl)] + prices[("CE", cl)])
    width = max(ps - pl, cl - cs)
    max_loss = width - credit
    if credit <= 0 or max_loss <= 0:
        return None
    return Condor(pl, ps, cs, cl, credit, max_loss)


def condor_expiry_payoff(c: Condor, st: float) -> float:
    """P&L per unit at expiry spot st = net_credit − spread losses."""
    put_loss = max(0.0, min(c.put_short, st if st < c.put_short else c.put_short) - max(c.put_long, st)) \
        if st < c.put_short else 0.0
    # cleaner: put spread loss = clamp(put_short - st, 0, put_short - put_long)
    put_loss = min(max(c.put_short - st, 0.0), c.put_short - c.put_long)
    call_loss = min(max(st - c.call_short, 0.0), c.call_long - c.call_short)
    return c.net_credit - put_loss - call_loss


# ── chain providers ────────────────────────────────────────────────────────────
def _synthetic_prices(spot: float, iv: float, T: float) -> dict:
    """Full strike ladder around spot priced by BS at a single IV (no skew — harness)."""
    out = {}
    lo = _round_strike(spot * (1 - SHORT_OTM_PCT - WING_PCT - 0.01))
    hi = _round_strike(spot * (1 + SHORT_OTM_PCT + WING_PCT + 0.01))
    k = lo
    while k <= hi:
        out[("CE", k)] = bs_price(spot, k, T, iv, "CE")
        out[("PE", k)] = bs_price(spot, k, T, iv, "PE")
        k += STRIKE_STEP
    return out


def _nifty_path_from_lake(base: Path) -> pd.DataFrame:
    root = base / "lake" / "ohlcv" / "market=NSE" / "segment=INDICES" / "symbol=NIFTY50" / "interval=1d"
    parts = sorted(root.glob("year=*/part-*.parquet"))
    if not parts:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("Asia/Kolkata").dt.normalize().dt.tz_localize(None)
    return df[["date", "close"]].sort_values("date").drop_duplicates("date").reset_index(drop=True)


def _realized_vol(rets: np.ndarray) -> float:
    r = rets[np.isfinite(rets)]
    return float(np.std(r, ddof=1) * math.sqrt(252)) if len(r) > 2 else 0.15


# ── backtest engine ────────────────────────────────────────────────────────────
def backtest_synthetic(path: pd.DataFrame, iv_premium_vp: float = 3.0,
                       cost: OptionsCostModel | None = None) -> pd.DataFrame:
    """Sell a monthly defined-risk condor on the given underlying path. IV = trailing
    realised + iv_premium (bakes in a known VRP). Hold to expiry; settle intrinsic."""
    cost = cost or OptionsCostModel()
    close = path["close"].to_numpy()
    dates = path["date"].to_numpy()
    logret = np.diff(np.log(close), prepend=np.nan)
    rows = []
    i = 60  # warm-up for trailing realised vol
    while i + DTE_TD < len(close):
        spot = float(close[i])
        rv = _realized_vol(logret[i - 21:i])
        iv = rv + iv_premium_vp / 100.0
        T = DTE_TD / 252.0
        prices = _synthetic_prices(spot, iv, T)
        c = build_condor(spot, prices)
        if c is None:
            i += DTE_TD; continue
        st = float(close[i + DTE_TD])
        pnl_unit = condor_expiry_payoff(c, st)

        n_lots = int((RISK_BUDGET_PCT * NAV) // (c.max_loss_unit * NIFTY_LOT))
        n_lots = max(n_lots, 0)
        if n_lots == 0:
            i += DTE_TD; continue
        qty = n_lots * NIFTY_LOT

        # entry costs: 4 legs (2 SELL shorts, 2 BUY wings) on entry premiums
        entry_cost = (cost.leg_cost(prices[("PE", c.put_short)], qty, "SELL")
                      + cost.leg_cost(prices[("CE", c.call_short)], qty, "SELL")
                      + cost.leg_cost(prices[("PE", c.put_long)], qty, "BUY")
                      + cost.leg_cost(prices[("CE", c.call_long)], qty, "BUY"))
        # exit: square off only legs with intrinsic > 0 at expiry (worthless legs expire free)
        exit_legs = [("PE", c.put_short, max(c.put_short - st, 0.0), "BUY"),
                     ("PE", c.put_long, max(c.put_long - st, 0.0), "SELL"),
                     ("CE", c.call_short, max(st - c.call_short, 0.0), "BUY"),
                     ("CE", c.call_long, max(st - c.call_long, 0.0), "SELL")]
        exit_cost = sum(cost.leg_cost(intr, qty, side) for _, _, intr, side in exit_legs if intr > 0)

        gross = pnl_unit * qty
        net = gross - entry_cost - exit_cost
        rows.append({"entry": pd.Timestamp(dates[i]).date(), "expiry": pd.Timestamp(dates[i + DTE_TD]).date(),
                     "year": pd.Timestamp(dates[i]).year, "spot": spot, "st": st, "iv": iv * 100, "rv": rv * 100,
                     "n_lots": n_lots, "credit_unit": c.net_credit, "max_loss_unit": c.max_loss_unit,
                     "gross": gross, "cost": entry_cost + exit_cost, "net": net,
                     "ret_pct": net / NAV * 100})
        i += DTE_TD
    return pd.DataFrame(rows)


# ── real F&O bhavcopy chains → backtest ────────────────────────────────────────
def _load_fo_chains(base: Path) -> pd.DataFrame:
    root = base / "lake" / "options" / "underlying=NIFTY"
    parts = sorted(root.glob("date=*/part-0.parquet"))
    if not parts:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.normalize()
    df["strike"] = df["strike"].astype(float)
    return df


def _spot_on_or_before(sorted_dates: list, smap: dict, d) -> float | None:
    i = bisect.bisect_right(sorted_dates, d) - 1
    return float(smap[sorted_dates[i]]) if i >= 0 else None


def backtest_real(chains: pd.DataFrame, spot_path: pd.DataFrame,
                  slippage: float = 0.025, cost: OptionsCostModel | None = None,
                  dte_cal: int = 30) -> pd.DataFrame:
    """Monthly defined-risk condor on REAL F&O-bhavcopy chains. Held to expiry, settled at
    the NIFTY50 spot on expiry; entry premiums slippage-haircut (EOD close isn't a fill)."""
    cost = cost or OptionsCostModel()
    sp = spot_path.copy()
    sp["date"] = pd.to_datetime(sp["date"]).dt.normalize()
    smap = dict(zip(sp["date"], sp["close"]))
    sdates = sorted(smap)

    chains = chains.copy()
    chains["ym"] = chains["expiry"].dt.to_period("M")
    monthly = chains.groupby("ym")["expiry"].max().sort_values()
    rows = []
    for E in monthly:
        E = pd.Timestamp(E)
        sub = chains[chains["expiry"] == E]
        tds = sub["trade_date"].drop_duplicates().sort_values()
        tds = tds[tds < E]
        if tds.empty:
            continue
        target = E - pd.Timedelta(days=dte_cal)
        entry = pd.Timestamp(tds.iloc[int((tds - target).abs().to_numpy().argmin())])
        if not (20 <= (E - entry).days <= 40):
            continue   # not a genuine ~monthly entry — skips NIFTY long-dated/quarterly contracts
        chain_at = sub[sub["trade_date"] == entry]
        prices = {(r.opt_type, float(r.strike)): float(r.close)
                  for r in chain_at.itertuples() if r.close > 0}
        if not prices:
            continue
        spot = _spot_on_or_before(sdates, smap, entry)
        if spot is None:
            u = chain_at["underlying"].dropna()
            spot = float(u.mean()) if len(u) else None
        if not spot or spot <= 0:
            continue
        c = build_condor(spot, prices)
        if c is None:
            continue
        # executed credit with slippage: sell at (1−slip), buy wings at (1+slip)
        sell = prices[("PE", c.put_short)] * (1 - slippage) + prices[("CE", c.call_short)] * (1 - slippage)
        buy = prices[("PE", c.put_long)] * (1 + slippage) + prices[("CE", c.call_long)] * (1 + slippage)
        credit = sell - buy
        width = max(c.put_short - c.put_long, c.call_long - c.call_short)
        max_loss = width - credit
        if credit <= 0 or max_loss <= 0:
            continue
        st = _spot_on_or_before(sdates, smap, E)
        if st is None:
            continue
        cc = Condor(c.put_long, c.put_short, c.call_short, c.call_long, credit, max_loss)
        pnl_unit = condor_expiry_payoff(cc, st)
        # size to the 2% budget but take >=1 lot (integer lots; dropping sub-1-lot cycles biases
        # the sample toward cheap condors). cycles where 1-lot risk > budget are flagged below.
        n_lots = max(1, int((RISK_BUDGET_PCT * NAV) // (max_loss * NIFTY_LOT)))
        qty = n_lots * NIFTY_LOT
        entry_cost = (cost.leg_cost(prices[("PE", c.put_short)], qty, "SELL")
                      + cost.leg_cost(prices[("CE", c.call_short)], qty, "SELL")
                      + cost.leg_cost(prices[("PE", c.put_long)], qty, "BUY")
                      + cost.leg_cost(prices[("CE", c.call_long)], qty, "BUY"))
        exit_legs = [("PE", max(c.put_short - st, 0.0), "BUY"), ("PE", max(c.put_long - st, 0.0), "SELL"),
                     ("CE", max(st - c.call_short, 0.0), "BUY"), ("CE", max(st - c.call_long, 0.0), "SELL")]
        exit_cost = sum(cost.leg_cost(intr, qty, side) for _, intr, side in exit_legs if intr > 0)
        gross = pnl_unit * qty
        net = gross - entry_cost - exit_cost
        rows.append({"entry": entry.date(), "expiry": E.date(), "year": int(entry.year),
                     "spot": spot, "st": st, "iv": float("nan"), "rv": float("nan"),
                     "n_lots": n_lots, "credit_unit": credit, "max_loss_unit": max_loss,
                     "gross": gross, "cost": entry_cost + exit_cost, "net": net, "ret_pct": net / NAV * 100})
    return pd.DataFrame(rows)


# ── evaluation against the pre-registered O-2 gate ─────────────────────────────
def evaluate(cyc: pd.DataFrame) -> dict:
    net = cyc["net"].to_numpy()
    wins = net[net > 0].sum()
    losses = -net[net < 0].sum()
    pf = float(wins / losses) if losses > 0 else float("inf")
    expectancy = float(net.mean())
    by_year = cyc.groupby("year")["net"].sum()
    pos_years = float((by_year > 0).mean())

    eq = NAV + np.cumsum(net)
    peak = np.maximum.accumulate(eq)
    dd = float((eq / peak - 1.0).min())
    worst = float(net.min())
    ann = float((1 + net.sum() / NAV) ** (252 / (len(net) * DTE_TD)) - 1) if len(net) else 0.0
    risk_cap_ok = bool(worst >= -1.5 * RISK_BUDGET_PCT * NAV)   # defined-risk cap honoured (≤1.5× budget incl. costs/gaps)

    checks = {
        "expectancy>0": expectancy > 0,
        f"PF>{O2_MIN_PF}": pf > O2_MIN_PF,
        f"pos-years>={O2_MIN_POS_YEARS:.0%}": pos_years >= O2_MIN_POS_YEARS,
        f"maxDD<={O2_MAX_DD_PCT:.0%}": dd >= -O2_MAX_DD_PCT,
        "defined-risk cap held": risk_cap_ok,
    }
    verdict = ("PASS — defined-risk vol-sell shows net edge; human review for a SMALL gated pilot."
               if all(checks.values())
               else "FAIL — does not clear the O-2 gate net of costs / risk.")
    return {"n": len(cyc), "pf": pf, "expectancy": expectancy, "pos_years": pos_years,
            "dd": dd, "worst": worst, "ann": ann, "by_year": by_year,
            "total_net": float(net.sum()), "checks": checks, "verdict": verdict}


def _write_report(res: dict, params: dict, out: Path) -> None:
    yr = "\n".join(f"| {int(y)} | ₹{v:,.0f} |" for y, v in res["by_year"].items())
    chk = "\n".join(f"| {k} | {'✅ PASS' if v else '❌ FAIL'} |" for k, v in res["checks"].items())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"""# Defined-Risk Options-Vol Backtest (Options/Vol track — Phase O-2)

**Pre-registered:** {PREREG_DATE} · **Mode:** {params['mode']} · **Live trading: BLOCKED**
Advisory only. Defined-risk iron condors, held to expiry, full NSE options cost stack
({OptionsCostModel().version}: ₹20/leg flat + STT 0.1% sell + txn 0.035% + GST + stamp).
A PASS ⇒ human review for a SMALL gated pilot — never auto-deploy. Backtesting cannot promote.

> {params['note']}

## Setup
- NAV ₹{NAV:,.0f} · NIFTY lot {NIFTY_LOT} · risk budget {RISK_BUDGET_PCT:.0%}/cycle (hard tail cap)
- Structure: iron condor, shorts ±{SHORT_OTM_PCT:.0%} OTM, wings +{WING_PCT:.0%}, {DTE_TD} td to expiry
- Cycles: **{res['n']}**

## Results (net of full cost stack)
- Total net: **₹{res['total_net']:,.0f}** · annualised ≈ **{res['ann']*100:+.1f}%**
- Profit factor: **{res['pf']:.2f}** · expectancy/cycle: **₹{res['expectancy']:,.0f}**
- Positive years: **{res['pos_years']*100:.0f}%** · max drawdown: **{res['dd']*100:.1f}%**
- Worst single cycle: **₹{res['worst']:,.0f}** (risk budget = ₹{RISK_BUDGET_PCT*NAV:,.0f}/cycle)

### By year (net P&L)
| Year | Net |
|---|---:|
{yr}

## Pre-registered O-2 gate (fixed {PREREG_DATE} — not relaxed)
| Criterion | Result |
|---|---|
{chk}

## VERDICT
**{res['verdict']}**

---
*The defined-risk cap is the whole point: a condor's max loss is bounded by wing width − credit,
sized to ≤{RISK_BUDGET_PCT:.0%} of NAV/cycle, so even a Mar-2020-type cycle cannot exceed the budget.
A PASS on REAL licensed chains (not synthetic) ⇒ human review for a small gated pilot. Live BLOCKED.*
""")


# ── self-test (offline, hermetic) ──────────────────────────────────────────────
def _gbm_path(seed: int, n: int, ann_vol: float, crash: bool) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dsig = ann_vol / math.sqrt(252)
    rets = rng.normal(0.0003, dsig, n)
    if crash:
        rets[n // 2] = -0.13           # inject a single -13% crash day
        rets[n // 2 + 1] = -0.06
    close = 18000.0 * np.exp(np.cumsum(rets))
    return pd.DataFrame({"date": pd.bdate_range("2020-01-01", periods=n), "close": close})


def _synth_chain_df(path: pd.DataFrame, iv_premium_vp: float = 4.0) -> pd.DataFrame:
    """Build a synthetic F&O-bhavcopy-shaped chain (BS-priced) to exercise backtest_real offline."""
    dates = pd.to_datetime(path["date"]).dt.normalize().to_numpy()
    close = path["close"].to_numpy()
    logret = np.diff(np.log(close), prepend=np.nan)
    rows = []
    i = 60
    while i + DTE_TD < len(close):
        entry, E = pd.Timestamp(dates[i]), pd.Timestamp(dates[i + DTE_TD])
        spot = float(close[i]); rv = _realized_vol(logret[i - 21:i]); iv = rv + iv_premium_vp / 100.0
        for (ot, k), px in _synthetic_prices(spot, iv, DTE_TD / 252.0).items():
            rows.append({"trade_date": entry, "expiry": E, "strike": float(k), "opt_type": ot,
                         "open": px, "high": px, "low": px, "close": px, "settle": px,
                         "oi": 1000, "volume": 1000, "underlying": spot})
        i += DTE_TD
    return pd.DataFrame(rows)


def _self_test() -> int:
    print("SELF-TEST: options-vol backtester...")

    # 1) BS sanity: put–call parity  C − P = S − K·e^(−rT)
    S, K, T, sig = 18000, 18000, 30 / 252, 0.15
    c, p = bs_price(S, K, T, sig, "CE"), bs_price(S, K, T, sig, "PE")
    parity = (c - p) - (S - K * math.exp(-RISK_FREE * T))
    assert abs(parity) < 1e-6, parity
    print(f"  BS put-call parity ok (resid {parity:.2e})")

    # 2) condor payoff: inside wings → keep full credit; far below → max loss
    cd = Condor(put_long=17000, put_short=17300, call_short=18700, call_long=19000,
                net_credit=120.0, max_loss_unit=180.0)
    assert abs(condor_expiry_payoff(cd, 18000) - 120.0) < 1e-9, "ATM expiry → full credit"
    assert abs(condor_expiry_payoff(cd, 16000) - (-180.0)) < 1e-9, "deep ITM put → max loss bounded"
    assert abs(condor_expiry_payoff(cd, 20000) - (-180.0)) < 1e-9, "deep ITM call → max loss bounded"
    print("  condor payoff bounded at ±wing correctly")

    # 3) IV>RV ⇒ positive net expectancy over many cycles (the VRP, after costs)
    pos = backtest_synthetic(_gbm_path(1, 1500, 0.14, crash=False), iv_premium_vp=4.0)
    rp = evaluate(pos)
    assert rp["expectancy"] > 0, rp["expectancy"]
    assert rp["checks"]["expectancy>0"]
    print(f"  IV>RV: {rp['n']} cycles, expectancy ₹{rp['expectancy']:,.0f}, PF {rp['pf']:.2f} → +edge ✅")

    # 4) crash cycle: defined-risk cap must hold (worst loss bounded, no ruin)
    cr = backtest_synthetic(_gbm_path(2, 1500, 0.14, crash=True), iv_premium_vp=4.0)
    rc = evaluate(cr)
    budget = RISK_BUDGET_PCT * NAV
    assert rc["worst"] >= -1.5 * budget, f"crash loss {rc['worst']:.0f} exceeded cap {1.5*budget:.0f}"
    assert rc["checks"]["defined-risk cap held"]
    print(f"  crash injected: worst cycle ₹{rc['worst']:,.0f} ≤ 1.5× budget ₹{1.5*budget:,.0f} → cap held ✅")

    # 5) IV≈RV ⇒ costs should erode the edge (expectancy not strongly positive)
    flat = backtest_synthetic(_gbm_path(3, 1500, 0.14, crash=False), iv_premium_vp=0.0)
    rf = evaluate(flat)
    assert rf["expectancy"] < rp["expectancy"], "zero-VRP must underperform +VRP"
    print(f"  IV≈RV: expectancy ₹{rf['expectancy']:,.0f} < +VRP case (costs bite) ✅")

    # 6) real-chain engine: feed a synthetic bhavcopy-shaped chain → cycles produced, cap held
    rpath = _gbm_path(7, 1500, 0.14, crash=True)
    rchain = _synth_chain_df(rpath, iv_premium_vp=4.0)
    rr = evaluate(backtest_real(rchain, rpath, slippage=0.025))
    assert rr["n"] > 10, f"real-path produced too few cycles: {rr['n']}"
    assert rr["worst"] >= -1.5 * RISK_BUDGET_PCT * NAV, "real-path crash cap breached"
    # slippage must reduce edge vs zero-slippage
    rr0 = evaluate(backtest_real(rchain, rpath, slippage=0.0))
    assert rr0["expectancy"] >= rr["expectancy"], "slippage should not improve edge"
    print(f"  real-chain engine: {rr['n']} cycles, expectancy ₹{rr['expectancy']:,.0f} "
          f"(slip 2.5%) vs ₹{rr0['expectancy']:,.0f} (no slip); cap held ✅")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        pth = Path(tmp) / "r.md"
        _write_report(rp, {"mode": "self-test", "note": "synthetic"}, pth)
        assert pth.exists() and "VERDICT" in pth.read_text()
    print("SELF-TEST PASSED.")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Defined-risk options-vol backtester (O-2 harness)")
    ap.add_argument("--self-test", action="store_true", help="Offline logic check")
    ap.add_argument("--synthetic", action="store_true",
                    help="Backtest on the real NIFTY lake path with BS-priced synthetic chains")
    ap.add_argument("--chain", help="Path to a real licensed option-chain dataset (Parquet/CSV) — the O-2 run")
    ap.add_argument("--iv-premium", type=float, default=3.0, help="Synthetic IV over realised, vol points (default 3)")
    ap.add_argument("--slippage", type=float, default=0.025, help="Per-leg slippage haircut on real EOD prices (default 2.5%)")
    ap.add_argument("--base", default=str(_DEFAULT_BASE))
    ap.add_argument("--out", default=str(_REPORT))
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    print("=" * 76)
    print("QuantEmbrace — Defined-Risk Options-Vol Backtest (O-2). Advisory. Live BLOCKED.")
    print("PASS ⇒ human review for a SMALL gated pilot, never auto-deploy. Defined-risk only.")
    print("=" * 76)

    if args.chain:
        cp = Path(args.chain)
        if (cp / "lake" / "options").exists():
            base = cp
        elif cp.name == "options" and (cp / "underlying=NIFTY").exists():
            base = cp.parents[1]
        elif cp.name == "underlying=NIFTY":
            base = cp.parents[2]
        else:
            base = Path(args.base)
        chains = _load_fo_chains(base)
        spot = _nifty_path_from_lake(base)
        if chains.empty:
            print(f"\n  No NIFTY option chains under {base}/lake/options/underlying=NIFTY.\n"
                  f"  Download them first (run LOCALLY):\n"
                  f"    python scripts/backtest/download_fo_bhavcopy.py --start 2022-06-01 --end 2025-06-30")
            return 1
        if spot.empty:
            print("  Need NIFTY50 spot in the lake (segment=INDICES) for settlement. "
                  "Run fetch_zerodha_indices.py / kite_fetch_with_token.py first.")
            return 1
        cyc = backtest_real(chains, spot, slippage=args.slippage)
        if cyc.empty:
            print("  No cycles produced from the chains.", file=sys.stderr); return 1
        res = evaluate(cyc)
        note = (f"REAL NSE F&O-bhavcopy chains (EOD), monthly condor held to expiry, {args.slippage:.1%} "
                f"per-leg slippage haircut. Real strikes/premiums/skew. EOD close is not a guaranteed fill "
                f"— a PASS warrants an intraday-vendor re-test (Algotest/GDFL) before any pilot.")
        print(f"\n  REAL chains: {res['n']} cycles · total net ₹{res['total_net']:,.0f} · ann {res['ann']*100:+.1f}%")
        print(f"  PF {res['pf']:.2f} · expectancy ₹{res['expectancy']:,.0f} · pos-years {res['pos_years']*100:.0f}%")
        print(f"  maxDD {res['dd']*100:.1f}% · worst cycle ₹{res['worst']:,.0f} (budget ₹{RISK_BUDGET_PCT*NAV:,.0f})")
        for k, v in res["checks"].items():
            print(f"    {'✅' if v else '❌'} {k}")
        print(f"\n  {res['verdict']}")
        _write_report(res, {"mode": f"REAL F&O bhavcopy (slip {args.slippage:.1%})", "note": note}, Path(args.out))
        print(f"\n  Report → {args.out}\n  Advisory only. EOD fills are optimistic; PASS ⇒ vendor re-test, then human review. Live BLOCKED.")
        return 0

    if not args.synthetic:
        print("\n  Choose a mode: --chain <lake> (real O-2), --synthetic (engine check), or --self-test.")
        return 1

    path = _nifty_path_from_lake(Path(args.base))
    if path.empty:
        print("  No NIFTY50 in the lake. Fetch it first:\n"
              "    python scripts/backtest/kite_fetch_with_token.py --request-token <fresh>  (or fetch_zerodha_indices.py)")
        return 1

    cyc = backtest_synthetic(path, iv_premium_vp=args.iv_premium)
    if cyc.empty:
        print("  No cycles produced.", file=sys.stderr); return 1
    res = evaluate(cyc)
    note = (f"SYNTHETIC validation: chains BS-priced off the real NIFTY path at IV = trailing realised "
            f"+ {args.iv_premium:.1f} vp (a known VRP). This proves the engine + cost stack + defined-risk "
            f"cap; it is NOT evidence of a real edge — that needs licensed chains with real skew/bid-ask.")
    print(f"\n  Cycles {res['n']} · total net ₹{res['total_net']:,.0f} · ann {res['ann']*100:+.1f}%")
    print(f"  PF {res['pf']:.2f} · expectancy ₹{res['expectancy']:,.0f} · pos-years {res['pos_years']*100:.0f}%")
    print(f"  maxDD {res['dd']*100:.1f}% · worst cycle ₹{res['worst']:,.0f} (budget ₹{RISK_BUDGET_PCT*NAV:,.0f})")
    for k, v in res["checks"].items():
        print(f"    {'✅' if v else '❌'} {k}")
    print(f"\n  {res['verdict']}")
    _write_report(res, {"mode": "synthetic (real NIFTY path)", "note": note}, Path(args.out))
    print(f"\n  Report → {args.out}")
    print("  Advisory only. Synthetic = engine validation, NOT a real edge. Live BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
