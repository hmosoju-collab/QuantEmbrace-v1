#!/usr/bin/env python3
"""
QuantEmbrace — O1: Scheduled-event IV-crush screen (Options/Futures program).

THESIS
------
Implied vol (INDIA VIX / ATM IV) is systematically ELEVATED before known scheduled events (Union Budget,
RBI MPC, general election) and CRUSHES after the uncertainty resolves. A DEFINED-RISK short-vol position
entered before and closed after harvests the crush. Unlike the always-on condor (which failed O-2 on
directional risk every cycle), this is EVENT-TIMED and concentrated.

THE DECISIVE TEST (why this isn't just the failed VRP again)
-----------------------------------------------------------
A short straddle around an event wins only if the elevated IV (credit) exceeds the realised event MOVE.
The unconditional VRP already failed to harvest via condors. So the screen's core question is:
**does event timing add edge OVER random-day short vol?** We compute the same short-straddle proxy on
event windows AND on a random non-event baseline, and compare. Event-conditioned must beat baseline.

DATA (free, in hand): INDIA VIX + NIFTY50 daily (lake). Event calendar is a CURATED, editable constant —
budgets + 2024 election are high-confidence; RBI dates are best-effort (±1 day tolerant since we hold
across a window). Per-event output lets bad dates be spotted. Small n ⇒ screen, not proof.

Advisory only. Live trading BLOCKED. A PASS ⇒ build a defined-risk event-vol backtest, never deploy.
    python scripts/backtest/run_event_vol_study.py
    python scripts/backtest/run_event_vol_study.py --self-test
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
from run_options_vol_backtest import OptionsCostModel  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = _REPO / "backtest-data"
_REPORT = _REPO / "docs" / "backtesting" / "event-vol-study-report.md"

NIFTY_LOT = 75
PREREG_DATE = "2026-06-20"
# ── pre-registered O1 gate (fixed 2026-06-20; DO NOT relax) ────────────────────
MIN_CRUSH_VP = 1.0         # mean IV drop across the event window (vol points)
MIN_WIN = 0.55             # short-straddle win rate
EDGE_MARGIN = 1.5          # event mean net P&L must be ≥1.5× the random-day baseline (timing adds edge)

# ── CURATED event calendar (editable; verify dates before relying) ─────────────
# (date, label, confidence). Budgets/election = HIGH; RBI = best-effort (±1 day tolerant).
EVENTS: list[tuple[str, str, str]] = [
    ("2020-02-01", "Budget-2020", "HIGH"), ("2021-02-01", "Budget-2021", "HIGH"),
    ("2022-02-01", "Budget-2022", "HIGH"), ("2023-02-01", "Budget-2023", "HIGH"),
    ("2024-02-01", "Budget-2024", "HIGH"), ("2025-02-01", "Budget-2025", "HIGH"),
    ("2024-06-04", "Election-2024-result", "HIGH"),
    # RBI MPC (best-effort result dates)
    ("2022-02-10", "RBI", "BE"), ("2022-04-08", "RBI", "BE"), ("2022-06-08", "RBI", "BE"),
    ("2022-08-05", "RBI", "BE"), ("2022-09-30", "RBI", "BE"), ("2022-12-07", "RBI", "BE"),
    ("2023-02-08", "RBI", "BE"), ("2023-04-06", "RBI", "BE"), ("2023-06-08", "RBI", "BE"),
    ("2023-08-10", "RBI", "BE"), ("2023-10-06", "RBI", "BE"), ("2023-12-08", "RBI", "BE"),
    ("2024-02-08", "RBI", "BE"), ("2024-04-05", "RBI", "BE"), ("2024-06-07", "RBI", "BE"),
    ("2024-08-08", "RBI", "BE"), ("2024-10-09", "RBI", "BE"), ("2024-12-06", "RBI", "BE"),
    ("2025-02-07", "RBI", "BE"), ("2025-04-09", "RBI", "BE"), ("2025-06-06", "RBI", "BE"),
]


def _load_close(base: Path, symbol: str) -> pd.DataFrame:
    root = base / "lake" / "ohlcv" / "market=NSE" / "segment=INDICES" / f"symbol={symbol}" / "interval=1d"
    parts = sorted(root.glob("year=*/part-*.parquet"))
    if not parts:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("Asia/Kolkata").dt.normalize().dt.tz_localize(None)
    return df[["date", "close"]].sort_values("date").drop_duplicates("date").reset_index(drop=True)


def _straddle_pnl_inr(spot_entry: float, spot_exit: float, iv_entry: float, dte_cal: float,
                      cost: OptionsCostModel, lot: int = NIFTY_LOT) -> tuple[float, float]:
    """Short ATM straddle held over the window. credit ≈ 0.8·S·σ·√T; exit ≈ intrinsic |move|.
    Returns (net_inr, credit_pts)."""
    credit_pts = 0.8 * spot_entry * (iv_entry / 100.0) * np.sqrt(max(dte_cal, 0.5) / 365.0)
    move_pts = abs(spot_exit - spot_entry)
    pnl_pts = credit_pts - move_pts
    qty = lot
    # cost: 2 sell legs entry (premium≈credit/2) + 2 buy legs exit (premium≈move/2)
    c = (2 * cost.leg_cost(credit_pts / 2, qty, "SELL") + 2 * cost.leg_cost(max(move_pts, 1.0) / 2, qty, "BUY"))
    return pnl_pts * qty - c, credit_pts


def _window(tds: list, vix: dict, spot: dict, E: pd.Timestamp):
    before = [t for t in tds if t < E]
    after = [t for t in tds if t > E]
    if not before or not after:
        return None
    entry, exit_ = before[-1], after[0]
    return entry, exit_, vix[entry], vix[exit_], spot[entry], spot[exit_]


def study(base: Path, seed: int = 7) -> dict:
    vixd = _load_close(base, "INDIAVIX"); nif = _load_close(base, "NIFTY50")
    if vixd.empty or nif.empty:
        return {}
    m = pd.merge(vixd.rename(columns={"close": "vix"}), nif.rename(columns={"close": "spot"}), on="date")
    tds = list(m["date"]); vix = dict(zip(m["date"], m["vix"])); spot = dict(zip(m["date"], m["spot"]))
    cost = OptionsCostModel()

    rows = []
    for ds, label, conf in EVENTS:
        E = pd.Timestamp(ds)
        w = _window(tds, vix, spot, E)
        if w is None:
            continue
        entry, exit_, iv0, iv1, s0, s1 = w
        dte = (exit_ - entry).days
        net, credit = _straddle_pnl_inr(s0, s1, iv0, dte, cost)
        rows.append({"event": label, "conf": conf, "entry": entry.date(), "exit": exit_.date(),
                     "iv0": iv0, "iv1": iv1, "crush": iv0 - iv1, "move_pct": (s1 / s0 - 1) * 100,
                     "net": net})
    ev = pd.DataFrame(rows)
    if ev.empty:
        return {"error": "no events matched the data window"}

    # baseline: random non-event days, same ~2-trading-day hold, same straddle proxy
    rng = np.random.default_rng(seed)
    event_set = set()
    for ds, *_ in EVENTS:
        E = pd.Timestamp(ds)
        event_set |= {t for t in tds if abs((t - E).days) <= 3}
    cand = [i for i, t in enumerate(tds) if t not in event_set and i + 2 < len(tds)]
    base_net = []
    for i in rng.choice(cand, size=min(300, len(cand)), replace=False):
        t0, t2 = tds[i], tds[i + 2]
        n, _ = _straddle_pnl_inr(spot[t0], spot[t2], vix[t0], (t2 - t0).days, cost)
        base_net.append(n)
    base_mean = float(np.mean(base_net))

    crush_mean = float(ev["crush"].mean())
    win = float((ev["net"] > 0).mean())
    ev_mean = float(ev["net"].mean())
    worst = ev.loc[ev["net"].idxmin()]
    edge_ratio = (ev_mean / base_mean) if base_mean > 0 else (float("inf") if ev_mean > 0 else 0.0)

    checks = {
        f"G1 mean crush>{MIN_CRUSH_VP}vp": crush_mean > MIN_CRUSH_VP,
        f"G2 win>={MIN_WIN:.0%}": win >= MIN_WIN,
        "G3 event net>0 & beats baseline": ev_mean > 0 and (ev_mean >= EDGE_MARGIN * base_mean if base_mean > 0 else ev_mean > 0),
    }
    verdict = ("PASS — event timing adds short-vol edge; build a defined-risk event-vol backtest. NOT deploy."
               if all(checks.values())
               else "SHELVE — event timing does not add reliable edge over random-day short vol.")
    return {"ev": ev, "n": len(ev), "crush_mean": crush_mean, "win": win, "ev_mean": ev_mean,
            "base_mean": base_mean, "edge_ratio": edge_ratio,
            "worst_label": worst["event"], "worst_net": float(worst["net"]),
            "checks": checks, "verdict": verdict}


def _write_report(r: dict, out: Path) -> None:
    ev = r["ev"].sort_values("entry")
    tbl = "\n".join(f"| {x.event} | {x.conf} | {x.entry} | {x.iv0:.1f}→{x.iv1:.1f} | {x.crush:+.1f} | "
                    f"{x.move_pct:+.1f}% | ₹{x.net:,.0f} |" for x in ev.itertuples())
    chk = "\n".join(f"| {k} | {'✅ PASS' if v else '❌ FAIL'} |" for k, v in r["checks"].items())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"""# O1 — Scheduled-Event IV-Crush Screen (Options/Futures program)

**Pre-registered:** {PREREG_DATE} · **Data:** INDIA VIX + NIFTY50 (free) · **Live trading: BLOCKED** · Advisory.

Does implied vol crush after scheduled events, and does a defined-risk short straddle held across the event
beat **random-day** short vol (the control that makes this distinct from the failed unconditional VRP)?

## Result (n={r['n']} events; small ⇒ screen, not proof)
- Mean IV crush: **{r['crush_mean']:+.1f} vol points** · short-straddle win rate: **{r['win']*100:.0f}%**
- Mean event net P&L (1 lot proxy): **₹{r['ev_mean']:,.0f}** vs random-day baseline **₹{r['base_mean']:,.0f}**
  → edge ratio **{r['edge_ratio']:.2f}×**
- Worst event: **{r['worst_label']}** at **₹{r['worst_net']:,.0f}** (the event-surprise tail)

## Per-event detail (verify the curated dates here)
| Event | conf | entry | VIX in→out | crush(vp) | move | net ₹ |
|---|---|---|---|---:|---:|---:|
{tbl}

## Pre-registered O1 gate (fixed {PREREG_DATE} — not relaxed)
| Criterion | Result |
|---|---|
{chk}

## VERDICT
**{r['verdict']}**

---
*Proxy = VIX-implied ATM straddle (defined-risk fly in the real build). n is small and the calendar curated
(budgets/election HIGH confidence; RBI best-effort). A PASS ⇒ a defined-risk event-vol backtest on the real
options chain, with explicit event-surprise tail sizing — never auto-deploy. Live BLOCKED.*
""")


def _self_test() -> int:
    print("SELF-TEST: event IV-crush screen...")
    import tempfile
    rng = np.random.default_rng(0)
    days = pd.bdate_range("2022-01-01", periods=600)
    # baseline VIX ~14, spot random walk; inject crush around our event dates
    vix = pd.Series(14.0, index=days) + pd.Series(rng.normal(0, 0.3, len(days)), index=days)
    spot = 18000 * np.exp(np.cumsum(rng.normal(0.0003, 0.007, len(days))))
    for ds, *_ in EVENTS:
        E = pd.Timestamp(ds)
        pre = [d for d in days if d < E]
        if pre:
            vix.loc[pre[-1]] += 5.0          # IV elevated just before event → crushes after (back to ~14)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        for sym, ser in [("INDIAVIX", vix), ("NIFTY50", pd.Series(spot, index=days))]:
            for yr, grp in ser.groupby(ser.index.year):
                dd = base / "lake" / "ohlcv" / "market=NSE" / "segment=INDICES" / f"symbol={sym}" / "interval=1d" / f"year={yr}"
                dd.mkdir(parents=True, exist_ok=True)
                pd.DataFrame({"timestamp": pd.to_datetime(grp.index).tz_localize("Asia/Kolkata"),
                              "close": grp.values}).to_parquet(dd / "part-0.parquet", index=False)
        r = study(base)
        assert r and "error" not in r, r
        assert r["crush_mean"] > MIN_CRUSH_VP, r["crush_mean"]
        assert r["checks"][f"G1 mean crush>{MIN_CRUSH_VP}vp"], "injected crush must pass G1"
        print(f"  injected +5vp pre-event crush → mean crush {r['crush_mean']:+.1f}vp, "
              f"win {r['win']*100:.0f}%, edge {r['edge_ratio']:.2f}× → G1 ✅")
        p = Path(tmp) / "r.md"; _write_report(r, p); assert p.exists() and "VERDICT" in p.read_text()
    print("SELF-TEST PASSED.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="O1 scheduled-event IV-crush screen")
    ap.add_argument("--base", default=str(_DEFAULT_BASE))
    ap.add_argument("--out", default=str(_REPORT))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    print("=" * 76)
    print("QuantEmbrace — O1 Scheduled-Event IV-Crush Screen. Advisory. Live BLOCKED.")
    print("=" * 76)
    r = study(Path(args.base))
    if not r or "error" in r:
        print(f"  {r.get('error', 'No INDIA VIX / NIFTY50 in lake — fetch via fetch_zerodha_indices.py') if r else 'No data'}")
        return 1
    print(f"\n  events n={r['n']} · mean crush {r['crush_mean']:+.1f}vp · win {r['win']*100:.0f}%")
    print(f"  event net ₹{r['ev_mean']:,.0f}/event vs baseline ₹{r['base_mean']:,.0f} → edge {r['edge_ratio']:.2f}×")
    print(f"  worst event: {r['worst_label']} ₹{r['worst_net']:,.0f}")
    for k, v in r["checks"].items():
        print(f"    {'✅' if v else '❌'} {k}")
    print(f"\n  VERDICT: {r['verdict']}")
    _write_report(r, Path(args.out))
    print(f"\n  Report → {args.out}\n  Advisory only. Small n + curated calendar. Live BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
