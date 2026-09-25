#!/usr/bin/env python3
"""Delivery-% paper book — clean MONTHLY forward replay (out-of-sample track record).

    SUPERSEDED (ADR-038, 2026-07-06) by the v2 engine — run instead:
        python -m qe study --config configs/qe_delivery_book.yaml   (delivery, ₹0.00 parity)
        python -m qe study --config configs/qe_momentum_book.yaml   (momentum)
    Retained as the fallback + a qe-test parity anchor until the v1 decommission gate passes
    (docs/runbooks/v1-decommission-runbook.md). Do not delete without golden-value conversion.


Rebuilds the delivery-% paper book deterministically from inception and walks a proper
monthly rebalance at each month-end trading day through the latest lake date. This is the
genuine out-of-sample forward record: the factor was characterised on 2020-2025 and the book
seeded at 2025-12-31, so every 2026 month is OOS by construction.

Why a dedicated replay (not per-month CLI calls): ad-hoc rebalancing contaminated the saved
state (a single Dec->Jun jump + same-day re-rebalances, because the idempotency guard lives in
the CLI main(), not in `_rebalance`). This script rebuilds the monthly walk in one shot,
idempotently, reusing the *validated* rebalance + cost logic from run_delivery_paper_book.py.
Re-run after each Bhavcopy refresh to advance the record.

It also computes the equal-weight liquid-universe benchmark over the SAME months — the factor
study's core lesson is that most raw return is beta, so the honest question is alpha vs the
market over the same window, on a deliberately tiny sample.

Backtest/paper-research only. Advisory. No broker. Live trading remains BLOCKED.

Usage:
    python scripts/paper/replay_delivery_book_forward.py
    python scripts/paper/replay_delivery_book_forward.py --inception 2025-12-31
    python scripts/paper/replay_delivery_book_forward.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))
sys.path.insert(0, str(_REPO / "scripts" / "backtest"))
sys.path.insert(0, str(_REPO / "scripts" / "paper"))

import numpy as np
import pandas as pd

from run_factor_study import _drop_funds, _drop_corp_action_dislocations, _load_panel  # noqa: E402
from run_delivery_paper_book import (  # noqa: E402
    _latest_lake_date,
    _mtm,
    _prices_asof,
    _rebalance,
    _save_state,
    K,
    SEED_NAV,
    STATE_PATH,
    TOP_N,
    _IST,
)

INCEPTION_DEFAULT = date(2025, 12, 31)
REPORT_PATH = _REPO / "docs" / "backtesting" / "delivery-paper-book-forward-report.md"


# ── month-end calendar from the lake ──────────────────────────────────────────


def _month_end_positions(close: pd.DataFrame, from_date: date) -> list[tuple[date, int]]:
    """(last-trading-day, row-position) for each calendar month at/after from_date."""
    norm = pd.DatetimeIndex(close.index).tz_convert(_IST).normalize()
    df = pd.DataFrame({"pos": range(len(norm)), "ym": norm.to_period("M")}, index=norm)
    out: list[tuple[date, int]] = []
    for _, g in df.groupby("ym", sort=True):
        d = g.index[-1].date()
        if d >= from_date:
            out.append((d, int(g["pos"].iloc[-1])))
    return out


def _slice(close, turn, deliv, pos: int):
    return close.iloc[: pos + 1], turn.iloc[: pos + 1], deliv.iloc[: pos + 1]


def _ew_benchmark(close, turn, pos_a: int, pos_b: int) -> float:
    """Equal-weight liquid-universe simple return from pos_a -> pos_b (same filters as the book)."""
    liq = turn.iloc[pos_a - 60 : pos_a].median().dropna()
    valid = close.iloc[pos_a].dropna().index
    liq = _drop_funds(liq[liq.index.isin(valid)])
    liq = _drop_corp_action_dislocations(liq, close, pos_a, lookback=126)
    univ = liq.sort_values(ascending=False).head(TOP_N).index.tolist()
    p0 = close[univ].iloc[pos_a]
    p1 = close[univ].ffill().iloc[pos_b]
    return float((p1 / p0 - 1.0).dropna().mean())


# ── replay ────────────────────────────────────────────────────────────────────


def replay(close, turn, deliv, inception: date, seed: float, factor: str = "delivery") -> dict:
    """Deterministic monthly walk from inception to the latest complete month + final MTM."""
    me = _month_end_positions(close, inception)
    if not me:
        raise SystemExit(f"No month-ends at/after {inception} in the lake.")
    latest_pos = len(close) - 1
    latest_ym = pd.Period(pd.Timestamp(close.index[-1]).tz_convert(_IST), freq="M")
    # rebalance only at COMPLETE months (exclude the in-progress final month, if partial)
    rebal = [(d, p) for (d, p) in me if pd.Period(pd.Timestamp(d), freq="M") != latest_ym]
    if not rebal:
        rebal = me[:1]

    state = {
        "inception": rebal[0][0].isoformat(),
        "seed_nav": seed,
        "cash": seed,
        "holdings": {},
        "nav_history": [],
        "last_rebalance": None,
        "config": {"top_n": TOP_N, "k": K, "rebalance": "monthly", "replay": True, "factor": factor},
    }

    print(f"  Monthly rebalances ({len(rebal)}): "
          + ", ".join(d.isoformat() for d, _ in rebal))
    for d, pos in rebal:
        csub, tsub, dsub = _slice(close, turn, deliv, pos)
        state = _rebalance(state, d, csub, tsub, dsub, verbose=False, factor=factor)

    # final mark-to-market at the latest available date (report-only, not a rebalance)
    mtm_prices = _prices_asof(close.iloc[: latest_pos + 1], list(state["holdings"].keys()))
    mtm_nav, _ = _mtm(state, mtm_prices)
    latest_date = pd.Timestamp(close.index[-1]).tz_convert(_IST).date()
    state["current_mtm"] = {"date": latest_date.isoformat(), "nav": round(mtm_nav, 2)}
    state["rebal_positions"] = [[d.isoformat(), p] for d, p in rebal]

    # benchmark over the same legs (each rebalance->next, last rebalance->MTM)
    legs = [p for _, p in rebal] + [latest_pos]
    bench_rets, book_navs = [], [h["nav"] for h in state["nav_history"]] + [mtm_nav]
    for a, b in zip(legs[:-1], legs[1:]):
        bench_rets.append(_ew_benchmark(close, turn, a, b))
    state["_bench_rets"] = bench_rets
    state["_book_navs"] = book_navs
    state["_leg_dates"] = [d.isoformat() for d, _ in rebal] + [latest_date.isoformat()]
    return state


# ── reporting ─────────────────────────────────────────────────────────────────


def _summary(state: dict) -> dict:
    navs = state["_book_navs"]
    bench = state["_bench_rets"]
    dates = state["_leg_dates"]
    book_rets = [navs[i + 1] / navs[i] - 1.0 for i in range(len(navs) - 1)]
    seed = state["seed_nav"]
    months = [
        {"to": dates[i + 1], "book": book_rets[i], "bench": bench[i],
         "alpha": book_rets[i] - bench[i]}
        for i in range(len(book_rets))
    ]
    book_cum = navs[-1] / seed - 1.0
    bench_cum = float(np.prod([1.0 + b for b in bench]) - 1.0)
    n_full = max(len(book_rets) - 1, 0)  # last leg is a partial month
    return {"months": months, "book_cum": book_cum, "bench_cum": bench_cum,
            "n_full": n_full, "n_obs": len(book_rets)}


def _write_report(state: dict, s: dict, label: str, out_path: Path) -> None:
    m = s["months"]
    L = [
        f"# {label} Paper Book — Monthly Forward Replay (Out-of-Sample)",
        "",
        "**Status:** advisory · isolated paper book · live trading remains BLOCKED.",
        f"**Date:** {date.today()}   **Inception:** {state['inception']}   "
        f"**Latest MTM:** {state['current_mtm']['date']}   **Factor:** {state['config'].get('factor', 'delivery')}",
        "**Tool:** `scripts/paper/replay_delivery_book_forward.py` · **Builds on:** ADR-034/035/036.",
        "",
        f"Genuine out-of-sample: the {label} factor was characterised on 2020-2025 (momentum also "
        "verified 2016-2025) and the book seeded at 2025-12-31, so every 2026 month here is OOS.",
        "",
        "> **Sample-size honesty:** this is "
        f"**{s['n_full']} complete monthly returns** (+1 partial). That is far too few for any",
        "> Sharpe/t-stat claim — treat it as live plumbing proof + early hypothesis monitoring,",
        "> NOT validation. The benchmark column is the point: does the book add alpha over the",
        "> equal-weight market on the SAME months, or just ride beta?",
        "",
        "## Monthly returns (net of NSE delivery costs)",
        "",
        "| Month-end | Book | Benchmark (EW univ) | Alpha |",
        "|---|---:|---:|---:|",
    ]
    for r in m[:-1]:
        L.append(f"| {r['to']} | {r['book']*100:+.2f}% | {r['bench']*100:+.2f}% | {r['alpha']*100:+.2f}% |")
    if m:
        r = m[-1]
        L.append(f"| {r['to']} (partial) | {r['book']*100:+.2f}% | {r['bench']*100:+.2f}% | {r['alpha']*100:+.2f}% |")
    L += [
        "",
        f"**Cumulative since inception:** book **{s['book_cum']*100:+.2f}%** vs benchmark "
        f"**{s['bench_cum']*100:+.2f}%**  →  spread **{(s['book_cum']-s['bench_cum'])*100:+.2f} pts**.",
        f"Current NAV ₹{state['current_mtm']['nav']:,.0f} (seed ₹{state['seed_nav']:,.0f}).",
        "",
        "## Read",
        "",
        "- A handful of months cannot prove edge; it can only (a) prove the rebalance/cost/NAV",
        "  plumbing runs forward cleanly, and (b) flag early if reality diverges hard from the",
        "  backtest. Judge alpha vs benchmark, not the raw number.",
        "- The honest validation horizon for a monthly strategy is **12-24 months** of forward",
        "  record, or the extended-history walk-forward (2016-2026, now that 2016-2018 is in the",
        "  lake) — NOT this window.",
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production changes.",
        "> Live trading remains BLOCKED.",
    ]
    out_path.write_text("\n".join(L) + "\n")


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    print("SELF-TEST: synthetic monthly replay (no lake)...")
    rng = np.random.default_rng(5)
    dates = pd.date_range("2023-06-01", periods=520, freq="B", tz=_IST)
    syms = [f"S{i:03d}" for i in range(60)]
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.018, (len(dates), len(syms))), 0)),
                         index=dates, columns=syms)
    turn = close * pd.DataFrame(rng.uniform(1e5, 1e6, close.shape), index=dates, columns=syms)
    deliv = pd.DataFrame(rng.uniform(20, 80, close.shape), index=dates, columns=syms)
    st = replay(close, turn, deliv, inception=date(2025, 3, 31), seed=SEED_NAV)
    s = _summary(st)
    assert len(st["nav_history"]) >= 2, "need >=2 rebalances"
    assert len(s["months"]) == len(st["nav_history"]), "month/leg alignment off"
    assert st["current_mtm"]["nav"] > 0
    print(f"  OK — {len(st['nav_history'])} rebalances, {s['n_full']} full months, "
          f"book {s['book_cum']*100:+.1f}% vs bench {s['bench_cum']*100:+.1f}%")
    print("SELF-TEST PASSED.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Positional factor paper book monthly forward replay")
    ap.add_argument("--factor", choices=["delivery", "momentum"], default="delivery")
    ap.add_argument("--inception", default=INCEPTION_DEFAULT.isoformat())
    ap.add_argument("--seed", type=float, default=SEED_NAV)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    factor = args.factor
    label = {"delivery": "Delivery-%", "momentum": "Momentum (12-1)"}[factor]
    state_path = STATE_PATH if factor == "delivery" else STATE_PATH.parent / f"{factor}_book_state.json"
    report_path = (REPORT_PATH if factor == "delivery"
                   else _REPO / "docs" / "backtesting" / f"{factor}-paper-book-forward-report.md")
    inception = date.fromisoformat(args.inception)
    latest = _latest_lake_date()
    print("=" * 76)
    print(f"QuantEmbrace — {label} Paper Book: MONTHLY FORWARD REPLAY (OOS)")
    print("Isolated · advisory · positional/CNC · no broker · live trading BLOCKED.")
    print("=" * 76)
    print(f"  Factor {factor} · inception {inception} → latest lake {latest}.  Loading daily lake...")
    close, turn, deliv = _load_panel(date(inception.year - 2, inception.month, 1), latest)
    print(f"  Loaded {close.shape[0]} days × {close.shape[1]} symbols")

    state = replay(close, turn, deliv, inception, args.seed, factor=factor)
    state_path.write_text(json.dumps(state, indent=2, default=str))
    s = _summary(state)

    print(f"\n  {'Month-end':12} {'Book':>9} {'Benchmark':>10} {'Alpha':>9}")
    print("  " + "-" * 42)
    for i, r in enumerate(s["months"]):
        tag = " (partial)" if i == len(s["months"]) - 1 else ""
        print(f"  {r['to']:12} {r['book']*100:>+8.2f}% {r['bench']*100:>+9.2f}% {r['alpha']*100:>+8.2f}%{tag}")
    print("  " + "-" * 42)
    print(f"  Cumulative   book {s['book_cum']*100:>+7.2f}%   bench {s['bench_cum']*100:>+7.2f}%   "
          f"spread {(s['book_cum']-s['bench_cum'])*100:>+6.2f} pts")
    print(f"  Current NAV ₹{state['current_mtm']['nav']:,.0f}  (seed ₹{state['seed_nav']:,.0f})")
    print(f"  Sample: {s['n_full']} complete monthly returns — plumbing/monitoring, NOT validation.")

    _write_report(state, s, label, report_path)
    print(f"\n  State : {state_path}")
    print(f"  Report: {report_path}")
    print("  Advisory only. Backtesting can recommend; it cannot promote. Live trading BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
