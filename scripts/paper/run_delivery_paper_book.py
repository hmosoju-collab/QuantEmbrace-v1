#!/usr/bin/env python3
"""Delivery-% positional paper book — ISOLATED advisory forward-test harness.

    SUPERSEDED (ADR-038, 2026-07-06) by the v2 engine — run instead:
        python -m qe paper  --config configs/qe_delivery_book_paper.yaml
        python -m qe study  --config configs/qe_delivery_book.yaml
    This script is retained as (a) the fallback until the v1 decommission gate passes
    (docs/runbooks/v1-decommission-runbook.md) and (b) a parity anchor for qe's tests.
    Do not delete without the golden-value conversion noted in that runbook.


Stands up the delivery-% factor (ADR-034) as a paper portfolio: monthly cross-sectional
rebalance, long-only top-K equal-weight, CNC/delivery economics, positions held overnight.
It is **fully isolated** from the live/paper intraday pipeline:

  * No broker, no Kite, no DynamoDB live/paper tables, no MIS square-off, no Kafka.
  * State is a single JSON file under `backtest-data/paper_book/` (gitignored).
  * Advisory only — it simulates fills against the daily lake; it places no orders.

Why isolated (not wired into strategy_engine→...→execution_engine): that pipeline is
intraday/MIS; a positional/CNC book is a different model. Integrating it (real paper-broker
CNC orders, overnight risk handling, monitoring) is a separate, approval-gated step (ADR-035).

A monthly strategy cannot be "validated in 5 sessions" — the backtest is the edge estimate;
this harness proves the rebalance/holdings/NAV plumbing and accrues slow out-of-sample track
record. It is a SEPARATE track from the intraday 5-session live gate.

Usage:
    python scripts/paper/run_delivery_paper_book.py --init                 # seed NAV, no holdings
    python scripts/paper/run_delivery_paper_book.py --rebalance            # rebalance as of latest lake date
    python scripts/paper/run_delivery_paper_book.py --as-of 2025-12-31 --rebalance
    python scripts/paper/run_delivery_paper_book.py                        # report current book (MTM)
    python scripts/paper/run_delivery_paper_book.py --self-test

Backtest/paper-research only. Backtesting can recommend; it cannot promote. A human approves
all production changes. Live trading remains BLOCKED.
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

import numpy as np
import pandas as pd

from run_factor_study import _BUY_FRAC, _SELL_FRAC, _drop_funds, _drop_corp_action_dislocations, _load_panel, COST  # noqa: E402

# ── config (isolated; mirrors the validated factor study) ─────────────────────
STATE_PATH = _REPO / "backtest-data" / "paper_book" / "delivery_book_state.json"
SEED_NAV = 1_000_000.0      # ₹10L, same as the paper platform seed
TOP_N = 200                 # liquid universe
K = 20                      # book size (top-K by delivery %)
MAX_WEIGHT = 0.08           # per-name cap (safety; equal-weight 1/20=5% is under this)
CASH_BUFFER = 0.02          # keep ~2% cash so costs + share-rounding never overdraw
_IST = "Asia/Kolkata"


# ── state ─────────────────────────────────────────────────────────────────────


def _load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    return json.loads(STATE_PATH.read_text())


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


# ── data ──────────────────────────────────────────────────────────────────────


def _panel_asof(as_of: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load ~2yr of daily lake up to as_of (enough for 60d turnover + 21d delivery + history)."""
    start = date(as_of.year - 2, as_of.month, 1)
    close, turn, deliv = _load_panel(start, as_of)
    return close, turn, deliv


def _latest_lake_date() -> date:
    import glob
    fs = glob.glob(str(_REPO / "backtest-data/lake/ohlcv/market=NSE/segment=EQ/symbol=RELIANCE/interval=1d/year=*/part-0.parquet"))
    d = pd.read_parquet(sorted(fs)[-1], columns=["timestamp"])["timestamp"].max()
    return pd.Timestamp(d).date()


def _target_basket(close, turn, deliv, factor: str = "delivery") -> list[str]:
    """Top-K names by the chosen factor within the top-N liquid universe (as of last row).

    factor='delivery': trailing 21d mean delivery % (conviction / accumulation).
    factor='momentum': 12-1 cross-sectional momentum, close[i-21]/close[i-252]-1 (skip last month),
                       matching run_factor_study's momentum definition.
    """
    i = len(close) - 1
    liq = turn.iloc[i - 60:i].median().dropna()
    valid = close.iloc[i].dropna().index
    liq = _drop_funds(liq[liq.index.isin(valid)])
    liq = _drop_corp_action_dislocations(liq, close, i, lookback=126)
    univ = liq.sort_values(ascending=False).head(TOP_N).index.tolist()
    if factor == "momentum":
        if i < 252:
            return []
        score = (close[univ].iloc[i - 21] / close[univ].iloc[i - 252] - 1.0).dropna()
    else:
        score = deliv[univ].iloc[i - 21:i].mean().dropna()
    return score.sort_values(ascending=False).head(K).index.tolist()


# ── valuation ─────────────────────────────────────────────────────────────────


def _prices_asof(close: pd.DataFrame, symbols: list[str]) -> dict[str, float]:
    last = close.ffill().iloc[-1]
    return {s: float(last[s]) for s in symbols if s in last.index and not pd.isna(last[s])}


def _mtm(state: dict, prices: dict[str, float]) -> tuple[float, float]:
    pos_val = sum(h["qty"] * prices.get(s, h["avg_price"]) for s, h in state["holdings"].items())
    return state["cash"] + pos_val, pos_val


# ── rebalance ─────────────────────────────────────────────────────────────────


def _rebalance(state: dict, as_of: date, close, turn, deliv, verbose: bool,
               factor: str = "delivery") -> dict:
    basket = _target_basket(close, turn, deliv, factor=factor)
    prices = _prices_asof(close, basket + list(state["holdings"].keys()))
    nav, _ = _mtm(state, prices)

    # equal-weight target with per-name cap + cash buffer, only names with a valid price
    w = min((1.0 - CASH_BUFFER) / K, MAX_WEIGHT)
    target_val = {s: nav * w for s in basket if s in prices}
    target_qty = {s: int(target_val[s] // prices[s]) for s in target_val if prices[s] > 0}

    cur_qty = {s: h["qty"] for s, h in state["holdings"].items()}
    orders, buy_cost, sell_cost = [], 0.0, 0.0
    all_syms = set(cur_qty) | set(target_qty)
    for s in sorted(all_syms):
        c, t = cur_qty.get(s, 0), target_qty.get(s, 0)
        d = t - c
        if d == 0:
            continue
        px = prices.get(s)
        if px is None:
            continue
        notional = abs(d) * px
        if d > 0:
            buy_cost += notional * _BUY_FRAC
            side = "BUY"
        else:
            sell_cost += notional * _SELL_FRAC
            side = "SELL"
        orders.append({"symbol": s, "side": side, "qty": abs(d), "price": round(px, 2),
                       "notional": round(notional, 0)})

    # apply fills (at as-of close) + costs
    new_holdings: dict[str, dict] = {}
    cash = state["cash"]
    for s, t in target_qty.items():
        if t <= 0:
            continue
        px = prices[s]
        new_holdings[s] = {"qty": t, "avg_price": round(px, 4)}
    # cash flow: sell proceeds + buy spend, all at as-of close
    spend = sum(target_qty.get(s, 0) * prices[s] for s in target_qty)
    proceeds = sum(cur_qty.get(s, 0) * prices[s] for s in cur_qty if s in prices)
    cash = cash + proceeds - spend - buy_cost - sell_cost

    state["holdings"] = new_holdings
    state["cash"] = round(cash, 2)
    state["last_rebalance"] = as_of.isoformat()
    nav_after, pos_val = _mtm(state, prices)
    state["nav_history"].append({"date": as_of.isoformat(), "nav": round(nav_after, 2)})
    state["last_orders"] = orders
    state["last_cost"] = round(buy_cost + sell_cost, 2)

    if verbose:
        for o in orders:
            print(f"    {o['side']:4} {o['symbol']:12} {o['qty']:>6} @ ₹{o['price']:>10.2f}  ₹{o['notional']:>12,.0f}")
    return state


# ── reporting ─────────────────────────────────────────────────────────────────


def _report(state: dict, as_of: date, close: pd.DataFrame) -> None:
    prices = _prices_asof(close, list(state["holdings"].keys()))
    nav, pos_val = _mtm(state, prices)
    inception_nav = state.get("seed_nav", SEED_NAV)
    ret = (nav / inception_nav - 1.0) * 100.0
    print(f"\n  Delivery-% Paper Book  (ISOLATED · advisory · CNC/positional · no broker)")
    print(f"  As of: {as_of}   Inception: {state.get('inception')}   Last rebalance: {state.get('last_rebalance')}")
    print(f"  NAV: ₹{nav:,.0f}   (positions ₹{pos_val:,.0f} + cash ₹{state['cash']:,.0f})   "
          f"Since inception: {ret:+.2f}%")
    if state["holdings"]:
        print(f"  Holdings ({len(state['holdings'])}):")
        rows = []
        for s, h in state["holdings"].items():
            px = prices.get(s, h["avg_price"])
            val = h["qty"] * px
            rows.append((s, h["qty"], h["avg_price"], px, val, val / nav * 100))
        for s, q, avg, px, val, wpct in sorted(rows, key=lambda x: -x[4]):
            pnl = (px / avg - 1) * 100 if avg else 0.0
            print(f"    {s:12} qty={q:>6}  avg ₹{avg:>9.2f}  last ₹{px:>9.2f}  "
                  f"val ₹{val:>11,.0f}  {wpct:>4.1f}%  ({pnl:+.1f}%)")


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    print("SELF-TEST: synthetic paper book (no lake)...")
    rng = np.random.default_rng(3)
    dates = pd.date_range("2024-01-01", periods=300, freq="B", tz=_IST)
    syms = [f"S{i:02d}" for i in range(40)]
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, (len(dates), len(syms))), 0)),
                         index=dates, columns=syms)
    turn = close * pd.DataFrame(rng.uniform(1e5, 1e6, close.shape), index=dates, columns=syms)
    deliv = pd.DataFrame(rng.uniform(20, 80, close.shape), index=dates, columns=syms)
    state = {"inception": "2024-01-01", "seed_nav": SEED_NAV, "cash": SEED_NAV,
             "holdings": {}, "nav_history": [], "last_rebalance": None, "config": {}}
    state = _rebalance(state, date(2024, 6, 30), close, turn, deliv, verbose=False)
    nav, pos = _mtm(state, _prices_asof(close, list(state["holdings"])))
    assert len(state["holdings"]) > 0, "no holdings after rebalance"
    assert abs(nav - SEED_NAV) / SEED_NAV < 0.02, "NAV should be ~seed right after rebalance (cost only)"
    assert state["last_cost"] > 0, "rebalance should incur cost"
    assert state["cash"] >= 0, "cash went negative"
    print(f"  OK — {len(state['holdings'])} holdings, NAV ₹{nav:,.0f}, rebalance cost ₹{state['last_cost']:,.0f}")
    print("SELF-TEST PASSED.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Delivery-% positional paper book (isolated, advisory)")
    ap.add_argument("--init", action="store_true", help="Initialize the book (seed NAV, no holdings)")
    ap.add_argument("--rebalance", action="store_true", help="Rebalance to the current delivery-% basket")
    ap.add_argument("--as-of", help="Operate as of YYYY-MM-DD (default: latest lake date)")
    ap.add_argument("--seed", type=float, default=SEED_NAV)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    as_of = date.fromisoformat(args.as_of) if args.as_of else _latest_lake_date()
    print("=" * 72)
    print("QuantEmbrace — Delivery-% Paper Book (ISOLATED · advisory · positional/CNC)")
    print("No broker. No live/paper pipeline. No MIS. Live trading remains BLOCKED.")
    print("=" * 72)

    state = _load_state()
    if args.init or state is None:
        if state is not None and args.init:
            print("  (re-initialising existing book)")
        state = {"inception": as_of.isoformat(), "seed_nav": args.seed, "cash": args.seed,
                 "holdings": {}, "nav_history": [], "last_rebalance": None,
                 "config": {"top_n": TOP_N, "k": K, "max_weight": MAX_WEIGHT,
                            "cost_model": COST.cost_model_version, "rebalance": "monthly"}}
        _save_state(state)
        print(f"  Initialised book — seed NAV ₹{args.seed:,.0f}, inception {as_of}")
        if not args.rebalance:
            print("  Run with --rebalance to build the first basket.")
            return 0

    close, turn, deliv = _panel_asof(as_of)

    if args.rebalance:
        if state.get("last_rebalance") == as_of.isoformat():
            print(f"  Already rebalanced on {as_of} — skipping (idempotent).")
        else:
            print(f"  Rebalancing to delivery-% basket as of {as_of}...")
            state = _rebalance(state, as_of, close, turn, deliv, verbose=True)
            _save_state(state)
            print(f"  Rebalance cost: ₹{state['last_cost']:,.0f}")

    _report(state, as_of, close)
    print(f"\n  State: {STATE_PATH}")
    print("  Advisory only. Backtesting can recommend; it cannot promote. Live trading BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
