#!/usr/bin/env python3
"""Delivery + Momentum COMBINED book study (diversification backtest).

The two verified factors decoupled in the forward window (delivery −9.85% vs momentum +5.43%
over the same months). This tests, on PROPER history (2020→latest, not the 5-month forward
window), whether COMBINING them as a sleeve actually improves the risk-adjusted result and —
the real prize — REDUCES drawdown vs either factor alone.

Structure = SLEEVE (correct for decorrelated factors): hold the delivery top-K book and the
momentum top-K book side by side, split capital, rebalance monthly. Combined monthly return =
w·r_delivery + (1−w)·r_momentum. We test two NON-FITTED weightings (no weight optimisation =
no overfitting):
  * 50/50 (the natural prior).
  * inverse-vol (rolling 12m trailing vol, no lookahead) — principled, down-weights the
    higher-vol leg (momentum).

A composite SCORE (rank by z(delivery)+z(momentum)) is deliberately NOT used: for decorrelated
factors it selects names good on *both* (rare → mediocre), defeating the diversification; the
factor study already showed the multi-factor composite DILUTES.

Reuses the validated factor engine (`run_factor_study.run_factor`) so delivery/momentum numbers
reconcile with the published factor study. Costs (NSE delivery stack) are inside each leg.

HONEST SCOPE: this is portfolio construction on the SAME data the factors were found on — it
shows whether combining HELPS historically, NOT a fresh OOS validation. The two forward paper
books remain the live OOS track. Advisory. Live trading remains BLOCKED.

Usage:
    python scripts/backtest/run_combined_book_study.py
    python scripts/backtest/run_combined_book_study.py --start 2020-01-01 --end 2026-06-12
    python scripts/backtest/run_combined_book_study.py --self-test
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

from run_factor_study import _load_panel, _metrics, benchmark, run_factor  # noqa: E402

TOP_N, K = 200, 20
REPORT = _REPO / "docs" / "backtesting" / "combined-book-study-report.md"


# ── combine ───────────────────────────────────────────────────────────────────


def _inverse_vol_weights(d: pd.Series, m: pd.Series) -> pd.Series:
    """Rolling-12m inverse-vol weight on the delivery leg (no lookahead); 50/50 until warm."""
    vd = d.rolling(12).std().shift(1)
    vm = m.rolling(12).std().shift(1)
    wd = (1.0 / vd) / ((1.0 / vd) + (1.0 / vm))
    return wd.fillna(0.5).clip(0.1, 0.9)


def _per_year(monthly: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"r": monthly})
    df["year"] = [d.year for d in df.index]
    rows = []
    for y, g in df.groupby("year"):
        eq = (1.0 + g["r"]).cumprod()
        vol = g["r"].std() * np.sqrt(12)
        rows.append({"year": y, "return": eq.iloc[-1] - 1.0,
                     "sharpe": (g["r"].mean() * 12 / vol) if vol else 0.0,
                     "maxdd": float((eq / eq.cummax() - 1.0).min()), "months": len(g)})
    return pd.DataFrame(rows).set_index("year")


def _study(close, turn, deliv) -> dict:
    dr = run_factor("delivery", close, turn, deliv, TOP_N, K)
    mr = run_factor("momentum", close, turn, deliv, TOP_N, K)
    bench = benchmark(close, turn, TOP_N)

    df = pd.DataFrame({"delivery": dr.monthly_net, "momentum": mr.monthly_net}).dropna()
    wd = _inverse_vol_weights(df["delivery"], df["momentum"])
    df["combo_5050"] = 0.5 * df["delivery"] + 0.5 * df["momentum"]
    df["combo_invvol"] = wd * df["delivery"] + (1.0 - wd) * df["momentum"]
    b = bench.reindex(df.index).dropna()

    series = {"delivery": df["delivery"], "momentum": df["momentum"],
              "combo_5050": df["combo_5050"], "combo_invvol": df["combo_invvol"],
              "benchmark": b}
    return {"df": df, "series": series, "corr": float(df["delivery"].corr(df["momentum"])),
            "mean_wd": float(wd.mean())}


# ── report ────────────────────────────────────────────────────────────────────


def _verdict(s: dict) -> str:
    M = {k: _metrics(v) for k, v in s["series"].items()}
    c, d, m = M["combo_5050"], M["delivery"], M["momentum"]
    sharpe_better = c["sharpe"] >= max(d["sharpe"], m["sharpe"]) - 0.02
    dd_better = c["maxdd"] >= max(d["maxdd"], m["maxdd"])  # maxdd negative; higher = shallower
    if sharpe_better and dd_better:
        return "DIVERSIFICATION WORKS — combo Sharpe ≥ both legs AND drawdown shallower than both"
    if dd_better:
        return "PARTIAL — combo cuts drawdown vs both legs but Sharpe between the legs"
    if c["sharpe"] >= max(d["sharpe"], m["sharpe"]) - 0.02:
        return "PARTIAL — combo Sharpe competitive but drawdown not better than both"
    return "NO CLEAR BENEFIT — combo between the legs on both Sharpe and drawdown"


def _row(name: str, m: dict, extra: str = "") -> str:
    return (f"| {name} | {m['cagr']*100:.1f}% | {m['vol']*100:.1f}% | {m['sharpe']:.2f} "
            f"| {m['maxdd']*100:.1f}% | {m['hit']*100:.0f}% |{extra}")


def _write_report(full: dict, subs: dict, start: date, end: date) -> None:
    M = {k: _metrics(v) for k, v in full["series"].items()}
    L = [
        "# Delivery + Momentum Combined Book — Diversification Study",
        "",
        "**Status:** COMPLETE — advisory. Live trading remains BLOCKED.",
        f"**Date:** {date.today()}   **Period:** {start} → {end}   **Universe:** top-{TOP_N} liquid, top-{K} each leg",
        "**Tool:** `scripts/backtest/run_combined_book_study.py` (reuses `run_factor_study`).",
        "",
        "> **Scope honesty:** portfolio construction on the SAME data the factors were found on —",
        "> shows whether combining HELPS historically, NOT a fresh OOS test. The two forward paper",
        "> books are the live OOS track. Weights (50/50, inverse-vol) are non-fitted.",
        "",
        f"**Correlation of the two legs' monthly net returns: {full['corr']:+.2f}** "
        f"(the diversification thesis — low/negative is what makes combining work). "
        f"Mean inverse-vol delivery weight: {full['mean_wd']*100:.0f}%.",
        "",
        "## Full period (net of costs)",
        "",
        "| Book | CAGR | Vol | Sharpe | MaxDD | Hit% |",
        "|---|---:|---:|---:|---:|---:|",
        _row("delivery", M["delivery"]),
        _row("momentum", M["momentum"]),
        _row("**combo 50/50**", M["combo_5050"]),
        _row("combo inverse-vol", M["combo_invvol"]),
        _row("_benchmark (EW, gross)_", M["benchmark"]),
        "",
        f"**Verdict: {_verdict(full)}**",
        "",
        "## Sub-period robustness",
        "",
        "| Book | 2020–22 Sharpe / MaxDD | 2023–26 Sharpe / MaxDD |",
        "|---|---:|---:|",
    ]
    for k, name in [("delivery", "delivery"), ("momentum", "momentum"),
                    ("combo_5050", "**combo 50/50**")]:
        a = _metrics(subs["A"]["series"][k]); b = _metrics(subs["B"]["series"][k])
        L.append(f"| {name} | {a['sharpe']:.2f} / {a['maxdd']*100:.1f}% | {b['sharpe']:.2f} / {b['maxdd']*100:.1f}% |")
    L += ["", "## Combo 50/50 — per-year (walk-forward consistency)", "",
          "| Year | Return | Sharpe | MaxDD | Months |", "|---|---:|---:|---:|---:|"]
    for y, r in _per_year(full["series"]["combo_5050"]).iterrows():
        L.append(f"| {y} | {r['return']*100:.1f}% | {r['sharpe']:.2f} | {r['maxdd']*100:.1f}% | {int(r['months'])} |")
    L += [
        "",
        "## Read",
        "",
        "- The prize is **drawdown reduction + Sharpe lift** from the low/negative leg correlation,",
        "  not higher return (a 50/50 averages the legs' returns). Judge MaxDD and Sharpe vs *both*",
        "  standalone legs.",
        "- Momentum is the higher-vol, crash-prone leg; delivery is defensive. They are",
        "  regime-complementary, so the combo should be smoother than either across regimes.",
        "- Still in-sample for factor selection; the forward paper books (both factors) remain the",
        "  honest OOS test. A combined forward book is the natural next live-tracking step.",
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production changes.",
    ]
    REPORT.write_text("\n".join(L) + "\n")


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    print("SELF-TEST: combine math on synthetic anti-correlated legs...")
    idx = pd.date_range("2021-01-31", periods=48, freq="ME")
    rng = np.random.default_rng(8)
    base = rng.normal(0.01, 0.04, 48)
    d = pd.Series(base + rng.normal(0, 0.03, 48), index=idx)
    m = pd.Series(-base + rng.normal(0.012, 0.05, 48), index=idx)  # anti-correlated, higher vol
    df = pd.DataFrame({"delivery": d, "momentum": m})
    wd = _inverse_vol_weights(df["delivery"], df["momentum"])
    c = 0.5 * df["delivery"] + 0.5 * df["momentum"]
    mc, md, mm = _metrics(c), _metrics(df["delivery"]), _metrics(df["momentum"])
    assert 0.0 < wd.mean() < 1.0 and len(_per_year(c)) >= 3
    assert mc["vol"] <= max(md["vol"], mm["vol"]) + 1e-9, "combo vol should not exceed the worse leg"
    print(f"  OK — corr={df['delivery'].corr(df['momentum']):+.2f} "
          f"combo vol {mc['vol']*100:.1f}% vs legs {md['vol']*100:.1f}%/{mm['vol']*100:.1f}%")
    print("SELF-TEST PASSED.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Delivery+Momentum combined book diversification study")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-06-12")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    print("=" * 76)
    print("QuantEmbrace — Delivery + Momentum Combined Book (diversification study)")
    print("Advisory. No broker. Live trading BLOCKED.")
    print("=" * 76)
    print(f"  Period {start} → {end} · top-{TOP_N}/{K} each leg · sleeve (50/50 + inverse-vol)")
    print("  Loading daily lake + running both factor legs...", flush=True)
    close, turn, deliv = _load_panel(start, end)
    full = _study(close, turn, deliv)

    mid = date(start.year + 3, 1, 1)
    subs = {}
    for tag, (s, e) in {"A": (start, date(2022, 12, 31)), "B": (date(2022, 1, 1), end)}.items():
        c2, t2, d2 = _load_panel(s, e)
        subs[tag] = _study(c2, t2, d2)

    M = {k: _metrics(v) for k, v in full["series"].items()}
    print(f"\n  Leg correlation (delivery vs momentum monthly): {full['corr']:+.2f}")
    print(f"  {'Book':>16}  {'CAGR':>7}  {'Vol':>6}  {'Sharpe':>6}  {'MaxDD':>7}  {'Hit':>4}")
    print("  " + "-" * 56)
    for k in ("delivery", "momentum", "combo_5050", "combo_invvol", "benchmark"):
        m = M[k]
        print(f"  {k:>16}  {m['cagr']*100:>6.1f}%  {m['vol']*100:>5.1f}%  {m['sharpe']:>6.2f}  "
              f"{m['maxdd']*100:>6.1f}%  {m['hit']*100:>3.0f}%")
    print("  " + "-" * 56)
    print(f"  VERDICT: {_verdict(full)}")

    _write_report(full, subs, start, end)
    print(f"\n  Report: {REPORT}")
    print("  Advisory only. Backtesting can recommend; it cannot promote. Live trading BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
