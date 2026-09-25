#!/usr/bin/env python3
"""US Forward Gate — pre-registered bar the RPLITE book must clear FORWARD before any real-capital
reconsideration (ADR-041 P4). Mirrors `check_forward_gate.py` (the NSE Forward Factor Gate)
exactly in math and thresholds — same discipline, same reasons, different book and benchmark.

WHY PRE-REGISTERED: same as the NSE gate — fixed BEFORE the forward paper book (Phase 5) has
accrued a single month, so the goalposts cannot move once real numbers start arriving. These
thresholds are set now (2026-07-14, before Phase 5 starts) and must not be relaxed to make the
book pass.

Criteria (ALL must hold) — IDENTICAL to the NSE gate, benchmark = buy-and-hold SPY instead of the
EW liquid universe:
  1. Horizon   : >= 12 complete forward months tracked.
  2. Alpha     : cumulative net return beats SPY over the window (cum alpha > 0).
  3. Info ratio: monthly-alpha IR (mean/std * sqrt(12)) >= 0.50.
  4. Consistency: positive monthly alpha in >= 58% of months AND no single month contributing
                 > 50% of cumulative alpha.
  5. Risk      : forward max drawdown <= SPY's over the same window.
Clearing the gate => HUMAN REVIEW for a small gated capital pilot. NEVER auto-deploy. Live BLOCKED.

Usage:
    python scripts/paper/check_us_forward_gate.py
    python scripts/paper/check_us_forward_gate.py --self-test

Source: reads the qe study summary (`reports/qe/rplite-book-*/summary.json`), produced by
`qe.research.us_study.run_risk_parity_study`. There is no v1 US model to cross-check against
(RPLITE never existed in v1) — this is qe-only, unlike the NSE gate's qe/v1 dual-source design.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

import numpy as np

# ── pre-registered thresholds (DO NOT relax to make the book pass) ─────────────
# Identical to check_forward_gate.py's MIN_MONTHS/MIN_IR/MIN_POS_ALPHA_FRAC/MAX_SINGLE_ALPHA_SHARE.
MIN_MONTHS = 12
MIN_IR = 0.50
MIN_POS_ALPHA_FRAC = 0.58
MAX_SINGLE_ALPHA_SHARE = 0.50
QE_REPORTS_DIR = _REPO / "reports" / "qe"
BOOK_GLOB = "rplite-book-*"


def _latest_qe_summary() -> dict | None:
    """Freshest RPLITE forward-book summary (session dirs embed a UTC
    timestamp, so lexicographic sort orders them chronologically)."""
    if not QE_REPORTS_DIR.exists():
        return None
    for d in sorted(QE_REPORTS_DIR.glob(BOOK_GLOB), reverse=True):
        p = d / "summary.json"
        if not p.exists():
            continue
        s = json.loads(p.read_text())
        if s.get("nav_history") and s.get("months"):
            return s
    return None


def _state_from_qe_summary(s: dict) -> dict:
    """Adapt a qe study summary to the shape `_evaluate` consumes — identical
    adapter logic to check_forward_gate.py's `_state_from_qe_summary`."""
    navs = [float(h["nav"]) for h in s["nav_history"]]
    final_mtm = s.get("final_mtm") or {}
    if final_mtm and final_mtm.get("date") != s["nav_history"][-1]["date"]:
        navs.append(float(final_mtm["nav"]))
    bench = [float(m["bench"]) for m in s["months"]]
    if len(navs) - 1 != len(bench):
        raise ValueError(
            f"qe summary shape drift: {len(navs) - 1} book legs vs {len(bench)} bench legs "
            f"(session {s.get('session_id')})"
        )
    return {
        "seed_nav": s.get("seed_nav", 1_000_000.0),
        "_book_navs": navs,
        "_bench_rets": bench,
        "inception": s.get("inception"),
        "current_mtm": final_mtm or {"date": s["nav_history"][-1]["date"]},
        "benchmark_symbol": s.get("benchmark_symbol", "SPY"),
    }


def _max_dd(equity: list[float]) -> float:
    eq = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(eq)
    return float((eq / peak - 1.0).min()) if len(eq) else 0.0


def _evaluate(state: dict) -> dict:
    navs = state.get("_book_navs", [])
    bench = state.get("_bench_rets", [])
    seed = float(state.get("seed_nav", 1_000_000.0))
    if len(navs) < 2 or len(bench) < 1:
        return {"error": "insufficient state (need a replayed book)"}

    book_rets = [navs[i + 1] / navs[i] - 1.0 for i in range(len(navs) - 1)]
    n_legs = min(len(book_rets), len(bench))
    book_full = book_rets[:n_legs][:-1]  # drop the final partial month
    bench_full = bench[:n_legs][:-1]
    alpha = [b - k for b, k in zip(book_full, bench_full)]
    n = len(alpha)

    cum_book = float(np.prod([1 + r for r in book_full]) - 1.0) if n else 0.0
    cum_bench = float(np.prod([1 + r for r in bench_full]) - 1.0) if n else 0.0
    cum_alpha = cum_book - cum_bench
    a = np.asarray(alpha, dtype=float)
    ir = float(a.mean() / a.std() * np.sqrt(12)) if n > 1 and a.std() > 0 else 0.0
    pos_frac = float((a > 0).mean()) if n else 0.0
    pos_sum = float(a[a > 0].sum())
    max_share = float(a.max() / pos_sum) if pos_sum > 0 else float("inf")

    book_eq = navs[: n + 1]
    bench_eq = [seed]
    for r in bench_full:
        bench_eq.append(bench_eq[-1] * (1 + r))
    book_dd, bench_dd = _max_dd(book_eq), _max_dd(bench_eq)

    checks = {
        "horizon>=12mo": n >= MIN_MONTHS,
        "cum_alpha>0": cum_alpha > 0,
        f"IR>={MIN_IR}": ir >= MIN_IR,
        f"pos_alpha>={MIN_POS_ALPHA_FRAC:.0%}": pos_frac >= MIN_POS_ALPHA_FRAC,
        "no_month>50%_alpha": max_share <= MAX_SINGLE_ALPHA_SHARE,
        "maxDD<=bench": book_dd >= bench_dd,
    }
    eligible = n >= MIN_MONTHS
    status = (
        "IN PROGRESS"
        if not eligible
        else "PASS — eligible for gated capital review"
        if all(checks.values())
        else "FAIL — does not clear forward gate"
    )
    return {
        "n_months": n,
        "cum_book": cum_book,
        "cum_bench": cum_bench,
        "cum_alpha": cum_alpha,
        "ir": ir,
        "pos_frac": pos_frac,
        "max_share": max_share,
        "book_dd": book_dd,
        "bench_dd": bench_dd,
        "checks": checks,
        "eligible": eligible,
        "status": status,
    }


def _print(state: dict, r: dict, source: str) -> None:
    bench_sym = state.get("benchmark_symbol", "SPY")
    print(
        f"\n  ── rplite book ── (inception {state.get('inception')}, "
        f"latest {state.get('current_mtm', {}).get('date', '?')}, source: {source})"
    )
    if "error" in r:
        print(f"     {r['error']}")
        return
    print(f"     months tracked : {r['n_months']} / {MIN_MONTHS}")
    print(
        f"     cum return     : book {r['cum_book']*100:+.2f}%  vs {bench_sym} {r['cum_bench']*100:+.2f}%  "
        f"→ alpha {r['cum_alpha']*100:+.2f}%"
    )
    if r["max_share"] != float("inf"):
        print(
            f"     info ratio     : {r['ir']:.2f}   pos-alpha months: {r['pos_frac']*100:.0f}%   "
            f"max single-month alpha share: {r['max_share']*100:.0f}%"
        )
    else:
        print(
            f"     info ratio     : {r['ir']:.2f}   pos-alpha months: {r['pos_frac']*100:.0f}%   "
            "(cum alpha ≤ 0)"
        )
    print(f"     max drawdown   : book {r['book_dd']*100:.1f}%  vs {bench_sym} {r['bench_dd']*100:.1f}%")
    print(
        "     criteria       : "
        + "  ".join(f"{'✓' if v else '✗'}{k}" for k, v in r["checks"].items())
    )
    print(f"     STATUS         : {r['status']}")


def _self_test() -> int:
    print("SELF-TEST: gate evaluation on synthetic state...")
    seed = 1_000_000.0
    bench = [0.008] * 13  # SPY-like monthly drift
    navs = [seed]
    for _ in range(13):
        navs.append(navs[-1] * (1 + 0.016))  # book beats bench every month
    dates = [f"2026-{m:02d}-28" for m in range(1, 13)] + ["2027-01-29", "2027-02-05"]
    summary = {
        "session_id": "selftest-rplite-book",
        "benchmark_symbol": "SPY",
        "seed_nav": seed,
        "inception": "2025-12-31",
        "nav_history": [
            {"date": d, "nav": nav} for d, nav in zip(["2025-12-31"] + dates[:12], navs[:13])
        ],
        "final_mtm": {"date": dates[13], "nav": navs[13]},
        "months": [
            {
                "to": d,
                "book": navs[i + 1] / navs[i] - 1.0,
                "bench": bench[i],
                "alpha": (navs[i + 1] / navs[i] - 1.0) - bench[i],
            }
            for i, d in enumerate(dates[:13])
        ],
    }
    state = _state_from_qe_summary(summary)
    r = _evaluate(state)
    assert r["n_months"] == 12, r["n_months"]
    assert r["eligible"] and r["cum_alpha"] > 0 and r["ir"] > 0
    assert r["status"].startswith("PASS")

    short = {
        "seed_nav": seed,
        "_book_navs": navs[:4],
        "_bench_rets": bench[:3],
        "inception": "2025-12-31",
        "current_mtm": {"date": "2026-03-28"},
    }
    assert not _evaluate(short)["eligible"]

    bad = dict(summary, months=summary["months"][:-1])  # shape drift must fail loudly
    try:
        _state_from_qe_summary(bad)
        raise AssertionError("shape drift was not detected")
    except ValueError:
        pass
    print(f"  OK — full: n={r['n_months']} status={r['status']!r}; short: IN PROGRESS; shape-drift guard OK")
    print("SELF-TEST PASSED.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="US Forward Gate checker (pre-registered, ADR-041 P4)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    print("=" * 76)
    print("QuantEmbrace — US Forward Gate (pre-registered 2026-07-14, ADR-041 P4)")
    print("Deploy nothing until the book clears this FORWARD. Human approval always. Live BLOCKED.")
    print("=" * 76)
    qe = _latest_qe_summary()
    if qe is None:
        print(
            f"\n  ── rplite book ── no qe study summary under {QE_REPORTS_DIR} "
            "(run: python -m qe study --config configs/qe_us_rplite_book.yaml)"
        )
        return 0
    state = _state_from_qe_summary(qe)
    r = _evaluate(state)
    _print(state, r, source=f"qe study {qe.get('session_id', '?')}")
    print(
        f"\n  Gate: ≥{MIN_MONTHS} mo · cum-alpha>0 · IR≥{MIN_IR} · ≥{MIN_POS_ALPHA_FRAC:.0%} pos-alpha "
        "months · no month >50% alpha · maxDD≤SPY. Clearing → human review, NOT auto-deploy."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
