#!/usr/bin/env python3
"""Forward Factor Gate (FFG) — pre-registered bar a positional factor book must clear FORWARD
before any real-capital reconsideration. Reads the isolated forward paper-book state(s) and
reports PASS / FAIL / IN-PROGRESS against fixed, pre-registered criteria.

WHY PRE-REGISTERED: "let the forward books be the truth-test" only has integrity if the bar is
fixed BEFORE more data arrives — otherwise the goalposts move. These thresholds are set now
(2026-06-19) and must not be relaxed to make a book pass. The whole engagement's lesson is that
apparent edges dissolve out-of-sample, so the bar targets ALPHA (vs the equal-weight liquid
benchmark), consistency, and risk — NOT raw return (which is mostly beta).

Criteria (ALL must hold):
  1. Horizon   : >= 12 complete forward months tracked (a monthly strategy can't be judged sooner;
                 books seeded 2025-12-31 → eligible ~Dec-2026).
  2. Alpha     : cumulative net return beats the EW liquid benchmark over the window (cum alpha > 0).
  3. Info ratio: monthly-alpha IR (mean/std * sqrt(12)) >= 0.50 (modest, honest for small n).
  4. Consistency: positive monthly alpha in >= 58% of months AND no single month contributing
                 > 50% of cumulative alpha (guards the one-regime-illusion failure mode we caught).
  5. Risk      : forward max drawdown <= benchmark's over the same window (no extra risk for the alpha).
Clearing the gate => HUMAN REVIEW for a small gated capital pilot. NEVER auto-deploy. Live BLOCKED.

Usage:
    python scripts/paper/check_forward_gate.py            # check all forward books (auto source)
    python scripts/paper/check_forward_gate.py --factor delivery
    python scripts/paper/check_forward_gate.py --source qe|v1|auto
    python scripts/paper/check_forward_gate.py --self-test

Sources (ADR-040 follow-up, 2026-07-08): the gate reads the qe study summary
(`reports/qe/<factor>-book-*/summary.json`) as the primary source; the v1 replay state
(`backtest-data/paper_book/*_book_state.json`) is the legacy source, kept as a cross-check.
In `auto` mode the freshest qe summary is used and, when the v1 state sits at the same data
frontier, the two are cross-checked and any numeric mismatch is reported loudly. The
evaluation math and thresholds are IDENTICAL for both sources — porting the reader must
never move the goalposts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

import numpy as np

# ── pre-registered thresholds (DO NOT relax to make a book pass) ───────────────
MIN_MONTHS = 12
MIN_IR = 0.50
MIN_POS_ALPHA_FRAC = 0.58          # ~7 of 12
MAX_SINGLE_ALPHA_SHARE = 0.50
BOOK_DIR = _REPO / "backtest-data" / "paper_book"
STATE_FILES = {"delivery": "delivery_book_state.json", "momentum": "momentum_book_state.json"}
QE_REPORTS_DIR = _REPO / "reports" / "qe"


def _load_v1_state(factor: str) -> dict | None:
    path = BOOK_DIR / STATE_FILES[factor]
    return json.loads(path.read_text()) if path.exists() else None


def _latest_qe_summary(factor: str) -> dict | None:
    """Freshest `qe study` summary for this factor's forward book.

    Session dirs embed a UTC timestamp (`<factor>-book-<stamp>-<hash>`), so a
    lexicographic sort orders them chronologically."""
    if not QE_REPORTS_DIR.exists():
        return None
    for d in sorted(QE_REPORTS_DIR.glob(f"{factor}-book-*"), reverse=True):
        p = d / "summary.json"
        if not p.exists():
            continue
        s = json.loads(p.read_text())
        if s.get("factor") == factor and s.get("nav_history") and s.get("months"):
            return s
    return None


def _state_from_qe_summary(s: dict) -> dict:
    """Adapt a qe study summary to the exact shape `_evaluate` consumes.

    Leg NAVs = rebalance NAVs + the final MTM (the partial leg `_evaluate`
    drops); bench legs come from the summary's per-month benchmark returns.
    Fails loudly on shape drift — governance tooling must not guess."""
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
    # drop the final partial month (last leg = last rebalance -> latest MTM)
    book_full = book_rets[:n_legs][:-1]
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
    status = ("IN PROGRESS" if not eligible
              else "PASS — eligible for gated capital review" if all(checks.values())
              else "FAIL — does not clear forward gate")
    return {"n_months": n, "cum_book": cum_book, "cum_bench": cum_bench, "cum_alpha": cum_alpha,
            "ir": ir, "pos_frac": pos_frac, "max_share": max_share,
            "book_dd": book_dd, "bench_dd": bench_dd, "checks": checks,
            "eligible": eligible, "status": status}


def _print(factor: str, state: dict, r: dict, source: str = "v1 state") -> None:
    print(f"\n  ── {factor} book ── (inception {state.get('inception')}, "
          f"latest {state.get('current_mtm', {}).get('date', '?')}, source: {source})")
    if "error" in r:
        print(f"     {r['error']}")
        return
    print(f"     months tracked : {r['n_months']} / {MIN_MONTHS}")
    print(f"     cum return     : book {r['cum_book']*100:+.2f}%  vs bench {r['cum_bench']*100:+.2f}%  "
          f"→ alpha {r['cum_alpha']*100:+.2f}%")
    print(f"     info ratio     : {r['ir']:.2f}   pos-alpha months: {r['pos_frac']*100:.0f}%   "
          f"max single-month alpha share: {r['max_share']*100:.0f}%" if r['max_share'] != float('inf')
          else f"     info ratio     : {r['ir']:.2f}   pos-alpha months: {r['pos_frac']*100:.0f}%   (cum alpha ≤ 0)")
    print(f"     max drawdown   : book {r['book_dd']*100:.1f}%  vs bench {r['bench_dd']*100:.1f}%")
    print(f"     criteria       : " + "  ".join(f"{'✓' if v else '✗'}{k}" for k, v in r["checks"].items()))
    print(f"     STATUS         : {r['status']}")


def _self_test() -> int:
    print("SELF-TEST: gate evaluation on synthetic state...")
    seed = 1_000_000.0
    bench = [0.01] * 13
    navs = [seed]
    for i in range(13):
        navs.append(navs[-1] * (1 + 0.02))          # book beats bench every month
    state = {"seed_nav": seed, "_book_navs": navs, "_bench_rets": bench,
             "inception": "2025-12-31", "current_mtm": {"date": "2027-01-31"}}
    r = _evaluate(state)
    assert r["n_months"] == 12, r["n_months"]        # 13 legs - 1 partial
    assert r["eligible"] and r["cum_alpha"] > 0 and r["ir"] > 0
    short = {"seed_nav": seed, "_book_navs": navs[:4], "_bench_rets": bench[:3],
             "inception": "2025-12-31", "current_mtm": {"date": "2026-03-31"}}
    assert not _evaluate(short)["eligible"]
    print(f"  OK — full: n={r['n_months']} status={r['status']!r}; short: IN PROGRESS")

    print("SELF-TEST: qe summary reader produces an identical evaluation...")
    dates = [f"2026-{m:02d}-28" for m in range(1, 13)] + ["2027-01-29", "2027-02-05"]
    summary = {
        "session_id": "selftest-book",
        "factor": "delivery",
        "seed_nav": seed,
        "inception": "2025-12-31",
        "nav_history": [{"date": d, "nav": nav} for d, nav in zip(["2025-12-31"] + dates[:12], navs[:13])],
        "final_mtm": {"date": dates[13], "nav": navs[13]},
        "months": [
            {"to": d, "book": navs[i + 1] / navs[i] - 1.0, "bench": bench[i],
             "alpha": (navs[i + 1] / navs[i] - 1.0) - bench[i]}
            for i, d in enumerate(dates[:13])
        ],
    }
    adapted = _state_from_qe_summary(summary)
    r_qe = _evaluate(adapted)
    assert r_qe == r, "qe-sourced evaluation must be identical to v1-sourced"
    assert _cross_check(r_qe, r) == "cross-check vs v1 state: MATCH"
    bad = dict(summary, months=summary["months"][:-1])  # shape drift must fail loudly
    try:
        _state_from_qe_summary(bad)
        raise AssertionError("shape drift was not detected")
    except ValueError:
        pass
    print("  OK — qe reader parity + shape-drift guard")
    print("SELF-TEST PASSED.")
    return 0


def _cross_check(r_qe: dict, r_v1: dict) -> str:
    """Compare qe- vs v1-sourced evaluations at the same data frontier."""
    if "error" in r_qe or "error" in r_v1:
        return "cross-check skipped (one source insufficient)"
    same = (
        r_qe["n_months"] == r_v1["n_months"]
        and abs(r_qe["cum_alpha"] - r_v1["cum_alpha"]) < 1e-6
        and abs(r_qe["ir"] - r_v1["ir"]) < 1e-6
        and r_qe["checks"] == r_v1["checks"]
    )
    if same:
        return "cross-check vs v1 state: MATCH"
    return (
        "⚠️  CROSS-CHECK MISMATCH vs v1 state — investigate before trusting either: "
        f"qe n={r_qe['n_months']} alpha={r_qe['cum_alpha']:+.6f} ir={r_qe['ir']:.4f} | "
        f"v1 n={r_v1['n_months']} alpha={r_v1['cum_alpha']:+.6f} ir={r_v1['ir']:.4f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Forward Factor Gate checker (pre-registered)")
    ap.add_argument("--factor", choices=list(STATE_FILES), help="check one book (default: all)")
    ap.add_argument(
        "--source",
        choices=["auto", "qe", "v1"],
        default="auto",
        help="auto = freshest qe study summary, cross-checked against v1 state when frontiers match",
    )
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    print("=" * 76)
    print("QuantEmbrace — Forward Factor Gate (pre-registered 2026-06-19)")
    print("Deploy nothing until a book clears this FORWARD. Human approval always. Live BLOCKED.")
    print("=" * 76)
    factors = [args.factor] if args.factor else list(STATE_FILES)
    any_found = False
    for f in factors:
        qe = _latest_qe_summary(f) if args.source in ("auto", "qe") else None
        v1 = _load_v1_state(f) if args.source in ("auto", "v1") else None

        if args.source == "qe" and qe is None:
            print(f"\n  ── {f} book ── no qe study summary under {QE_REPORTS_DIR} "
                  f"(run: python -m qe study --config configs/qe_{f}_book.yaml)")
            continue
        if args.source == "v1" and v1 is None:
            print(f"\n  ── {f} book ── no state at {BOOK_DIR / STATE_FILES[f]} "
                  f"(run replay_delivery_book_forward.py --factor {f})")
            continue
        if qe is None and v1 is None:
            print(f"\n  ── {f} book ── no qe summary and no v1 state — run the monthly cadence first")
            continue
        any_found = True

        if qe is not None:
            state = _state_from_qe_summary(qe)
            r = _evaluate(state)
            _print(f, state, r, source=f"qe study {qe.get('session_id', '?')}")
            if args.source == "auto" and v1 is not None:
                qe_latest = state.get("current_mtm", {}).get("date")
                v1_latest = v1.get("current_mtm", {}).get("date")
                if qe_latest == v1_latest:
                    print(f"     {_cross_check(r, _evaluate(v1))}")
                else:
                    print(f"     (v1 state at {v1_latest} vs qe at {qe_latest} — cross-check skipped; "
                          f"stale source lagging the other)")
        else:
            label = "v1 state" if args.source == "v1" else "v1 state (no qe summary found)"
            _print(f, v1, _evaluate(v1), source=label)
    if any_found:
        print(f"\n  Gate: ≥{MIN_MONTHS} mo · cum-alpha>0 · IR≥{MIN_IR} · ≥{MIN_POS_ALPHA_FRAC:.0%} pos-alpha "
              f"months · no month >50% alpha · maxDD≤bench. Clearing → human review, NOT auto-deploy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
