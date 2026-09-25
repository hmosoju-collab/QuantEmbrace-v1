#!/usr/bin/env python3
"""
QuantEmbrace — Options-vol structure robustness sweep (O-2 follow-up).

The naive 4%-OTM / 2%-wing monthly condor FAILED O-2 (loses gross even frictionless).
Before shelving, this checks whether that FAIL is *structural* or merely a bad single
config — by running a PRE-DECLARED grid of structures on the SAME free EOD F&O-bhavcopy
data and reporting EVERY cell honestly. This is the opposite of parameter-fishing: the
grid + decision rule are fixed up front and all results are shown.

PRE-DECLARED GRID (fixed 2026-06-20):
  short-strike distance OTM ∈ {1,2,3,4,5}%  ×  wing width ∈ {1,2}%   = 10 configs
  monthly held-to-expiry, 2.5% slippage, full cost stack, NAV ₹10L, ≥1 lot.

PRE-DECLARED DECISION RULE:
  A cell "passes" only against the UNCHANGED O-2 gate (expectancy>0, PF>1.3,
  pos-years≥60%, maxDD≤20%). With 10 configs on ~31 cycles, ~0.5 false passes are
  expected, so a pass counts only if it clears with margin AND has ≥1 passing neighbour
  (contiguous island), not an isolated point. None pass → shelve the naive short-vol harvest.

Advisory only. Live trading BLOCKED. Backtesting cannot promote.
    python scripts/backtest/run_options_vol_sweep.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import run_options_vol_backtest as M  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_REPORT = _REPO / "docs" / "backtesting" / "options-vol-sweep-report.md"

OTM_GRID = [0.01, 0.02, 0.03, 0.04, 0.05]
WING_GRID = [0.01, 0.02]


def _run_cell(chains, spot, otm: float, wing: float) -> dict:
    # monkeypatch the structure config the validated engine reads at call time
    o0, w0 = M.SHORT_OTM_PCT, M.WING_PCT
    M.SHORT_OTM_PCT, M.WING_PCT = otm, wing
    try:
        net_cyc = M.backtest_real(chains, spot, slippage=0.025)
        gross_cyc = M.backtest_real(chains, spot, slippage=0.0)
    finally:
        M.SHORT_OTM_PCT, M.WING_PCT = o0, w0
    if net_cyc.empty:
        return {"otm": otm, "wing": wing, "n": 0, "pass": False}
    r = M.evaluate(net_cyc)
    gross_net = float(gross_cyc["net"].sum()) if not gross_cyc.empty else float("nan")
    return {"otm": otm, "wing": wing, "n": r["n"], "net": r["total_net"], "pf": r["pf"],
            "exp": r["expectancy"], "posyr": r["pos_years"], "dd": r["dd"],
            "gross_net": gross_net, "pass": all(r["checks"].values())}


def main() -> int:
    base = M._DEFAULT_BASE
    chains = M._load_fo_chains(base)
    spot = M._nifty_path_from_lake(base)
    if chains.empty or spot.empty:
        print("Need the F&O chain lake + NIFTY50 spot. Run download_fo_bhavcopy.py first.", file=sys.stderr)
        return 1

    print("=" * 84)
    print("QuantEmbrace — Options-Vol Structure Robustness Sweep (pre-declared). Advisory. Live BLOCKED.")
    print("=" * 84)
    print(f"  Grid: OTM {[f'{x:.0%}' for x in OTM_GRID]} × wing {[f'{x:.0%}' for x in WING_GRID]} "
          f"= {len(OTM_GRID)*len(WING_GRID)} configs · monthly held-to-expiry · 2.5% slip\n")
    print(f"  {'OTM':>4} {'wing':>4} {'cyc':>4} {'net ₹':>11} {'gross₹(0slip)':>13} {'PF':>5} "
          f"{'exp ₹':>9} {'posYr':>6} {'maxDD':>7}  gate")
    print("  " + "-" * 80)

    results = []
    for wing in WING_GRID:
        for otm in OTM_GRID:
            r = _run_cell(chains, spot, otm, wing)
            results.append(r)
            if r["n"] == 0:
                print(f"  {otm:>4.0%} {wing:>4.0%}   no valid cycles"); continue
            print(f"  {otm:>4.0%} {wing:>4.0%} {r['n']:>4d} {r['net']:>11,.0f} {r['gross_net']:>13,.0f} "
                  f"{r['pf']:>5.2f} {r['exp']:>9,.0f} {r['posyr']*100:>5.0f}% {r['dd']*100:>6.1f}%  "
                  f"{'✅ PASS' if r['pass'] else '— fail'}")

    passes = [r for r in results if r.get("pass")]
    gross_pos = [r for r in results if r.get("n") and r.get("gross_net", -1) > 0]
    n_total = len([r for r in results if r.get("n")])
    print("  " + "-" * 80)
    print(f"\n  Configs with valid cycles: {n_total}/{len(results)}")
    print(f"  Gross-positive (zero-slip) configs: {len(gross_pos)}  ← if 0, NO structure even captures the "
          f"premium before costs")
    print(f"  Configs clearing the O-2 gate: {len(passes)}")
    if not passes:
        verdict = ("SHELVE — no monthly condor structure clears the O-2 gate; the naive short-vol harvest "
                   "is structural, not a single-config artifact.")
    elif len(passes) == 1:
        verdict = (f"ISOLATED PASS at OTM {passes[0]['otm']:.0%}/wing {passes[0]['wing']:.0%} — likely "
                   "multiple-testing luck on ~31 cycles; do NOT promote without a neighbour + OOS check.")
    else:
        verdict = (f"{len(passes)} configs pass — inspect for a contiguous island (real) vs scattered "
                   "(luck). Still EOD/held-to-expiry; needs intraday + active-management before any pilot.")
    print(f"\n  VERDICT: {verdict}")

    _write_report(results, gross_pos, passes, verdict, _REPORT)
    print(f"\n  Report → {_REPORT}")
    print("  Advisory only. No orders, no live trading. PASS (if any) ⇒ scrutiny, never deploy.")
    return 0


def _write_report(results, gross_pos, passes, verdict, out: Path) -> None:
    rows = []
    for r in results:
        if not r.get("n"):
            rows.append(f"| {r['otm']:.0%} | {r['wing']:.0%} | — | — | — | — | — | — | no cycles |")
            continue
        rows.append(f"| {r['otm']:.0%} | {r['wing']:.0%} | {r['n']} | ₹{r['net']:,.0f} | "
                    f"₹{r['gross_net']:,.0f} | {r['pf']:.2f} | ₹{r['exp']:,.0f} | {r['posyr']*100:.0f}% | "
                    f"{r['dd']*100:.1f}% | {'✅ PASS' if r['pass'] else 'fail'} |")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"""# Options-Vol Structure Robustness Sweep (O-2 follow-up)

**Pre-registered grid & rule:** 2026-06-20 · **Data:** free NSE F&O bhavcopy (EOD, 2022-06→2025-06) ·
**Live trading: BLOCKED.** Advisory only — backtesting cannot promote.

Purpose: confirm whether the naive-condor O-2 FAIL is **structural** or a single-config artifact, by
running a pre-declared OTM×wing grid on the same data and reporting **every** cell (no cherry-picking).

## Results (monthly held-to-expiry, 2.5% slippage, full cost stack, NAV ₹10L, ≥1 lot)
| OTM | wing | cycles | net | gross (0-slip) | PF | exp/cycle | pos-yrs | maxDD | O-2 gate |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
{chr(10).join(rows)}

- **Gross-positive (zero-slip) configs:** {len(gross_pos)} — if 0, no structure captures the premium even
  before costs (an edge problem, not a friction problem).
- **Configs clearing the O-2 gate:** {len(passes)}.

## VERDICT
**{verdict}**

---
*Decision rule (pre-declared): a pass counts only against the unchanged O-2 gate, with margin AND a passing
neighbour (contiguous island) — guarding the ~0.5 false passes expected from 10 configs × ~31 cycles.
Even a genuine pass here is EOD / held-to-expiry and would still need intraday + active-management testing
before any human-reviewed pilot. Live trading remains BLOCKED.*
""")


if __name__ == "__main__":
    raise SystemExit(main())
