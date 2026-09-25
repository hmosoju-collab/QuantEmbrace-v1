#!/usr/bin/env python3
"""H5 — Post-Earnings Announcement Drift (PEAD) study (daily NSE lake).

Economic mechanism: Markets under-react to earnings surprises. A stock with a large positive
return on earnings-announcement day continues to drift up over the subsequent weeks; stocks with
negative surprises continue to drift down. This is one of the most replicated anomalies in
academic finance (Ball & Brown 1968 → still live in modern markets, particularly in mid-small caps
and markets with less institutional coverage). For India NSE equities, the holding horizon
amortizes the ~0.32% round-trip delivery cost (same property that makes the monthly factor viable).

TWO EVENT MODES (selected automatically by data availability):

  H5a — TRUE PEAD (preferred): uses `fetch_earnings_calendar.py` output:
      backtest-data/reference/earnings_calendar.csv
    The event is the actual results-announcement date; surprise direction = price return on that
    day (price-implied, since we have no analyst consensus in the lake).

  H5b — PRICE-IMPLIED PROXY (fallback, runs without earnings calendar):
    Event on day t when:
      * |return(t)| > ABS_Z σ  (abnormally large move — proxy for an announcement)
      * volume > VOL_MULT × median (elevated activity consistent with news)
      * name is in the top-N liquid universe, funds excluded
    Study both POSITIVE events (long side of PEAD) and NEGATIVE events (no-shorting constraint
    respected — results reported informatively, not as a trading signal).

NO-LOOKAHEAD: earnings announcements are published after market hours. Entry at close[t+1].

SEPARATION FROM H4 (delivery-spike):
  H4 events required delivery-% spike. H5 events are price/volume only. A stock can trigger
  H5 without a delivery spike (most earnings moves don't have anomalous delivery %). If both
  H4 and H5 have positive results, their correlation determines whether they add alpha.

Usage:
    # Mode H5a (real calendar if available, else H5b)
    python scripts/backtest/run_pead_study.py

    # Force H5b (price-implied)
    python scripts/backtest/run_pead_study.py --proxy

    # Custom params
    python scripts/backtest/run_pead_study.py --abs-z 3.5 --vol-mult 2.0 --hold 21

    # Self-test (no lake needed)
    python scripts/backtest/run_pead_study.py --self-test

Backtest-only. Advisory. No broker. No live/paper state. Entry at close[t+1].
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))
sys.path.insert(0, str(_REPO / "scripts" / "backtest"))

import numpy as np
import pandas as pd

from run_delivery_spike import (  # reuse H4 machinery
    EventStudy,
    _daily_metrics,
    _market,
    calendar_portfolio,
    event_study,
    per_trade_net,
)
from run_factor_study import _is_fund, _load_panel, _ROUND_TRIP  # noqa: E402

EARNINGS_CSV = _REPO / "backtest-data" / "reference" / "earnings_calendar.csv"

_HORIZONS = (1, 2, 3, 5, 10, 15, 21, 42, 63)
_NET_HOLDS = (5, 10, 21, 42)
_COOLDOWN = 21     # per-symbol: skip re-entry until prior PEAD position would be closed


# ── H5a: event detection from real earnings calendar ─────────────────────────


def detect_events_calendar(
    close: pd.DataFrame,
    turn: pd.DataFrame,
    cal: pd.DataFrame,
    top_n: int,
    min_abs_ret: float = 0.0,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """
    Map earnings dates from `cal` onto the price panel.

    For each (symbol, announce_date) in the calendar:
      * Find the row index on that date (or the next available trading day).
      * Compute the day-of return as the surprise proxy.
      * Entry is at close[t+1] (no-lookahead: announcement after market hours).

    Returns (positive_events, negative_events) — both lists are (entry_idx, symbol).
    `min_abs_ret` filters out near-zero day-of moves (e.g. pre-announced inline results).
    """
    keep = [c for c in close.columns if not _is_fund(c)]
    cl = close[keep]
    tn = turn[keep]
    date_idx = {d.date(): i for i, d in enumerate(cl.index)}
    liq_med = tn.rolling(60, min_periods=30).median()

    pos_events: list[tuple[int, str]] = []
    neg_events: list[tuple[int, str]] = []
    open_until_pos: dict[str, int] = {}
    open_until_neg: dict[str, int] = {}

    for _, row in cal.iterrows():
        sym = str(row["symbol"]).upper()
        ann_date = pd.Timestamp(row["announce_date"]).date()
        if sym not in cl.columns:
            continue

        # find t: the trading day on or after announce_date
        t = date_idx.get(ann_date)
        if t is None:
            # scan forward up to 3 days (holidays)
            for delta in range(1, 4):
                cand = ann_date + pd.Timedelta(days=delta)
                t = date_idx.get(cand.date() if hasattr(cand, "date") else cand)
                if t is not None:
                    break
        if t is None or t < 20 or t + 1 >= len(cl):
            continue

        # liquidity gate: must be in top_n on announce day
        liq_val = liq_med.iloc[t][sym] if sym in liq_med.columns else np.nan
        if not np.isfinite(liq_val):
            continue
        col_liq = liq_med.iloc[t].dropna()
        col_liq = col_liq[[c for c in col_liq.index if not _is_fund(c)]]
        rank = col_liq.rank(ascending=False)[sym] if sym in col_liq.index else top_n + 1
        if rank > top_n:
            continue

        # day-of return (surprise proxy)
        px_t = cl[sym].iloc[t]
        px_t_1 = cl[sym].iloc[t - 1]
        if not (np.isfinite(px_t) and np.isfinite(px_t_1) and px_t_1 > 0):
            continue
        ret_t = px_t / px_t_1 - 1.0

        entry = t + 1  # no-lookahead: enter at next close
        if abs(ret_t) < min_abs_ret:
            continue

        if ret_t > 0:
            if open_until_pos.get(sym, -1) < entry:
                pos_events.append((entry, sym))
                open_until_pos[sym] = entry + _COOLDOWN
        else:
            if open_until_neg.get(sym, -1) < entry:
                neg_events.append((entry, sym))
                open_until_neg[sym] = entry + _COOLDOWN

    return pos_events, neg_events


# ── H5b: price-implied event detection ───────────────────────────────────────


def detect_events_proxy(
    close: pd.DataFrame,
    turn: pd.DataFrame,
    top_n: int,
    abs_z: float,
    vol_mult: float,
    lookback: int = 63,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """
    Identify large-move events as a proxy for earnings announcements.

    Event on day t (for a given symbol) when:
      * |return(t)| > abs_z × trailing_std (large absolute move)
      * turnover(t) > vol_mult × trailing_median (elevated volume)
      * symbol is in top_n liquid universe (funds excluded)

    Returns (positive_events, negative_events) as (entry_idx, symbol) lists.
    Per-symbol cooldown of COOLDOWN days prevents stacking.
    Entry at close[t+1] (no-lookahead).
    """
    keep = [c for c in close.columns if not _is_fund(c)]
    cl, tn = close[keep], turn[keep]
    rets = cl.pct_change()
    trail_std = rets.shift(1).rolling(lookback, min_periods=lookback // 2).std()
    vol_med = tn.shift(1).rolling(lookback, min_periods=lookback // 2).median()
    vol_ratio = tn / vol_med
    liq_rank = tn.shift(1).rolling(60, min_periods=30).median().rank(axis=1, ascending=False)

    n = len(cl)
    cols = list(cl.columns)
    pos_events: list[tuple[int, str]] = []
    neg_events: list[tuple[int, str]] = []
    open_pos: dict[str, int] = {}
    open_neg: dict[str, int] = {}

    for ti in range(lookback + 1, n - 1):
        row_ret = rets.iloc[ti]
        row_std = trail_std.iloc[ti]
        row_vr = vol_ratio.iloc[ti]
        row_liq = liq_rank.iloc[ti]
        for sym in cols:
            r = row_ret.get(sym, np.nan)
            s = row_std.get(sym, np.nan)
            vr = row_vr.get(sym, np.nan)
            lk = row_liq.get(sym, top_n + 1)
            if not (np.isfinite(r) and np.isfinite(s) and s > 0 and np.isfinite(vr)):
                continue
            if lk > top_n or vr < vol_mult:
                continue
            z = r / s
            entry = ti + 1
            if z > abs_z:
                if open_pos.get(sym, -1) < entry:
                    pos_events.append((entry, sym))
                    open_pos[sym] = entry + _COOLDOWN
            elif z < -abs_z:
                if open_neg.get(sym, -1) < entry:
                    neg_events.append((entry, sym))
                    open_neg[sym] = entry + _COOLDOWN

    return pos_events, neg_events


# ── report ────────────────────────────────────────────────────────────────────


def _verdict(es: EventStudy, ptn: pd.DataFrame, pm: dict, mm: dict) -> str:
    finite_t = [t for t in es.tstat_abn if np.isfinite(t)]
    max_t = max(finite_t) if finite_t else float("nan")
    min_t = min(finite_t) if finite_t else float("nan")
    sig_pos = any(np.isfinite(t) and t > 2.0 for t in es.tstat_abn)
    sig_neg = any(np.isfinite(t) and t < -2.0 for t in es.tstat_abn)
    net_ok = bool((ptn["mean_net"] > 0).any() and (ptn["tstat_abn"] > 2.0).any())
    beats_mkt = pm["sharpe"] > mm["sharpe"]

    if sig_pos and net_ok and beats_mkt:
        return (
            f"**CARRY FORWARD.** Abnormal returns significantly POSITIVE (t up to {max_t:+.2f}), "
            "net per-trade positive with significant t-stat, and calendar portfolio beats the market. "
            "Next gate: walk-forward (per-year OOS) + correlation check vs delivery-% monthly factor "
            "before any combined book."
        )
    if sig_pos and not net_ok:
        return (
            f"WEAK POSITIVE. Drift exists (t up to {max_t:+.2f}) but does **not** clear the "
            f"delivery cost stack (round-trip ≈ {_ROUND_TRIP*100:.3f}%) at any tested hold. "
            "Not investable on this evidence."
        )
    if sig_neg:
        return (
            f"**REJECTED — anti-predictive.** Abnormal returns significantly NEGATIVE (t down to "
            f"{min_t:+.2f}). Post-event names underperform. Record negative result; do not carry "
            "forward as a long signal (flipping to short is a separate hypothesis)."
        )
    return (
        "**REJECTED — no reliable drift.** No horizon is statistically significant (|t|>2). "
        "The signal adds no information after market beta is removed."
    )


def write_report(
    mode: str,
    es_pos: EventStudy,
    ptn_pos: pd.DataFrame,
    port_pos: pd.Series,
    es_neg: EventStudy,
    mkt_r: pd.Series,
    n_pos: int,
    n_neg: int,
    hold: int,
    start: date,
    end: date,
    params: dict,
) -> Path:
    out = _REPO / "docs" / "backtesting" / "pead-study-report.md"
    pm = _daily_metrics(port_pos)
    mm = _daily_metrics(mkt_r.reindex(port_pos.index).fillna(0.0))
    verdict = _verdict(es_pos, ptn_pos, pm, mm)

    L = [
        "# H5 — Post-Earnings Announcement Drift (PEAD) Study (Daily NSE)",
        "",
        "**Status:** COMPLETE — advisory. Live trading remains BLOCKED.",
        f"**Date:** {date.today()}",
        "**Relates:** ADR-036, `qe-phase-next-cio-operating-doc-2026-06-15.md` §7 (H5).",
        f"**Mode:** {'H5a (real earnings calendar)' if mode == 'calendar' else 'H5b (price-implied proxy)'}.",
        "",
        "> **No-lookahead:** announcements are after-hours. Every event enters at **close[t+1]**. "
        f"Costs = full NSE delivery stack (round-trip ≈ {_ROUND_TRIP*100:.3f}%). "
        "Abnormal return = vs EW top-100 market index.",
        "",
    ]
    if mode == "calendar":
        L.append(f"Events sourced from `earnings_calendar.csv` — actual NSE results dates.")
        L.append(f"Surprise direction = price return on the announcement day (price-implied).")
        L.append(f"Min |day-of return| filter: {params.get('min_abs_ret', 0)*100:.1f}%.")
    else:
        L.append(
            f"Price-implied proxy: |return(t)| > {params['abs_z']}σ, "
            f"volume > {params['vol_mult']}× median, top-{params['top_n']} liquid (funds excluded)."
        )
    L += [
        f"Period {start} → {end}. **{n_pos} positive events, {n_neg} negative events.**",
        "",
        "---",
        "",
        "## 1. Positive-surprise / long-side PEAD",
        "",
        "### 1a. Forward drift (raw + abnormal)",
        "",
        "| Horizon (days) | Mean return | Mean abnormal | t-stat (abn) |",
        "|---|---:|---:|---:|",
    ]
    for i, h in enumerate(es_pos.horizons):
        L.append(
            f"| T+{h} | {es_pos.mean_ret[i]*100:+.2f}% "
            f"| {es_pos.mean_abn[i]*100:+.2f}% | {es_pos.tstat_abn[i]:+.2f} |"
        )
    L += [
        "",
        "### 1b. Net per-trade economics (full delivery cost stack)",
        "",
        "| Hold (days) | Trades | Mean net | Median net | Hit% | Mean abn-net | t-stat |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in ptn_pos.iterrows():
        L.append(
            f"| {int(r['hold'])} | {int(r['n'])} | {r['mean_net']*100:+.2f}% "
            f"| {r['median_net']*100:+.2f}% | {r['hit']*100:.0f}% "
            f"| {r['mean_abn_net']*100:+.2f}% | {r['tstat_abn']:+.2f} |"
        )
    L += [
        "",
        f"### 1c. Calendar-time daily EW portfolio (hold {hold}d)",
        "",
        "| Series | CAGR | Sharpe | MaxDD | Active days |",
        "|---|---:|---:|---:|---:|",
        f"| PEAD long portfolio | {pm['cagr']*100:.1f}% | {pm['sharpe']:.2f} "
        f"| {pm['maxdd']*100:.1f}% | {pm['days']} |",
        f"| market (EW top-100) | {mm['cagr']*100:.1f}% | {mm['sharpe']:.2f} "
        f"| {mm['maxdd']*100:.1f}% | — |",
        "",
        "---",
        "",
        "## 2. Negative-surprise side (informational only — no short in CNC universe)",
        "",
        f"{n_neg} negative-surprise events detected. "
        "CNC delivery does not support short selling; these results are reported to understand "
        "the full signal structure but are NOT proposed as a trading strategy.",
        "",
        "| Horizon (days) | Mean return | Mean abnormal | t-stat (abn) |",
        "|---|---:|---:|---:|",
    ]
    for i, h in enumerate(es_neg.horizons):
        L.append(
            f"| T+{h} | {es_neg.mean_ret[i]*100:+.2f}% "
            f"| {es_neg.mean_abn[i]*100:+.2f}% | {es_neg.tstat_abn[i]:+.2f} |"
        )
    L += [
        "",
        "---",
        "",
        "## 3. Verdict",
        "",
        verdict,
        "",
        "**Caveats:**",
        "- Single bull-market regime (Oct 2019 – present for the main lake; "
          "pre-2019 data extends once 2016–2018 download completes — Tier-1 regime-expansion).",
        "- No analyst consensus data: surprise direction is price-implied, not fundamental.",
        "- Correlation with delivery-% monthly factor not yet tested; if both carry forward, "
          "a correlation study determines whether they truly diversify.",
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production changes.",
        "> Live trading remains BLOCKED.",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n")
    return out


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    print("SELF-TEST: synthetic PEAD panel with planted post-announcement drift...")
    rng = np.random.default_rng(42)
    n_days, n_syms = 500, 30
    dates = pd.date_range("2020-01-01", periods=n_days, freq="B", tz="Asia/Kolkata")
    syms = [f"E{i:03d}" for i in range(n_syms)]
    steps = rng.normal(0.0003, 0.010, (n_days, n_syms))
    # plant positive surprises: spike + drift on following 21 days
    planted = []
    for t in range(90, n_days - 30, 25):
        s = syms[t % n_syms]
        ci = syms.index(s)
        steps[t, ci] += 0.06        # +6% surprise day
        steps[t + 1:t + 22, ci] += 0.002   # 2bp/day drift for 21 days
        planted.append((t + 1, s))  # entry t+1

    close = pd.DataFrame(100 * np.exp(np.cumsum(steps, 0)), index=dates, columns=syms)
    turn = close * pd.DataFrame(rng.uniform(5e5, 2e6, close.shape), index=dates, columns=syms)
    # boost volume on surprise days
    for t, s in planted:
        turn.iloc[t - 1, syms.index(s)] *= 5.0

    pos_ev, neg_ev = detect_events_proxy(close, turn, top_n=30, abs_z=2.5, vol_mult=2.0)
    assert len(pos_ev) > 0, "no positive events detected"
    mkt_r, mkt_l = _market(close, turn)
    es_pos = event_study(close, mkt_l, pos_ev, _HORIZONS)
    ptn_pos = per_trade_net(close, mkt_l, pos_ev, _NET_HOLDS)
    es_neg = event_study(close, mkt_l, neg_ev, _HORIZONS)
    port = calendar_portfolio(close, pos_ev, 21)

    assert es_pos.n > 0
    assert not ptn_pos.empty
    idx_21 = list(_HORIZONS).index(21)
    assert es_pos.mean_ret[idx_21] > 0.005, "planted positive drift not recovered at T+21"
    print(f"  OK — pos_events={len(pos_ev)}, neg_events={len(neg_ev)}, "
          f"T+21 mean_ret={es_pos.mean_ret[idx_21]*100:+.2f}%")
    print("SELF-TEST PASSED.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="H5 PEAD event study (NSE daily lake)")
    ap.add_argument("--proxy", action="store_true",
                    help="Force H5b price-implied proxy even if earnings calendar exists")
    ap.add_argument("--top-n", type=int, default=200)
    ap.add_argument("--abs-z", type=float, default=3.0,
                    help="[H5b] Absolute return z-score threshold (default 3.0)")
    ap.add_argument("--vol-mult", type=float, default=2.0,
                    help="[H5b] Volume / trailing-median threshold (default 2.0)")
    ap.add_argument("--min-abs-ret", type=float, default=0.02,
                    help="[H5a] Min |day-of return| to count as a surprise (default 0.02 = 2%%)")
    ap.add_argument("--hold", type=int, default=21,
                    help="Calendar-portfolio hold in days (default 21)")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    use_calendar = EARNINGS_CSV.exists() and not args.proxy

    print("=" * 72)
    print("QuantEmbrace — H5 PEAD Study (daily NSE)")
    print("Backtest-only. Advisory. No broker. Entry at close[t+1] (no lookahead).")
    print("=" * 72)
    if use_calendar:
        cal = pd.read_csv(EARNINGS_CSV, parse_dates=["announce_date"])
        cal = cal[(cal["announce_date"] >= pd.Timestamp(start))
                  & (cal["announce_date"] <= pd.Timestamp(end))]
        print(f"  Mode: H5a — real earnings calendar ({len(cal)} announcements in range)")
        mode = "calendar"
        params = {"min_abs_ret": args.min_abs_ret, "top_n": args.top_n}
    else:
        if not EARNINGS_CSV.exists():
            print("  Earnings calendar not found. Run fetch_earnings_calendar.py first,")
            print("  or use --proxy for price-implied H5b mode.")
        print(f"  Mode: H5b — price-implied proxy  (|z|>{args.abs_z}, vol>{args.vol_mult}×)")
        mode = "proxy"
        params = {"abs_z": args.abs_z, "vol_mult": args.vol_mult, "top_n": args.top_n}

    print("  Loading daily lake...")
    close, turn, deliv = _load_panel(start, end)
    print(f"  Loaded {close.shape[0]} days × {close.shape[1]} symbols")

    if use_calendar:
        pos_events, neg_events = detect_events_calendar(
            close, turn, cal, args.top_n, args.min_abs_ret
        )
    else:
        pos_events, neg_events = detect_events_proxy(
            close, turn, args.top_n, args.abs_z, args.vol_mult
        )

    print(f"  Positive-surprise events: {len(pos_events)}")
    print(f"  Negative-surprise events: {len(neg_events)}")

    mkt_r, mkt_l = _market(close, turn)

    print("\n  Running event studies...")
    es_pos = event_study(close, mkt_l, pos_events, _HORIZONS)
    ptn_pos = per_trade_net(close, mkt_l, pos_events, _NET_HOLDS)
    es_neg = event_study(close, mkt_l, neg_events, _HORIZONS)
    port_pos = calendar_portfolio(close, pos_events, args.hold)

    print("\n  Positive-surprise forward drift:")
    for i, h in enumerate(es_pos.horizons):
        print(f"    T+{h:<2}: ret {es_pos.mean_ret[i]*100:+6.2f}%  "
              f"abn {es_pos.mean_abn[i]*100:+6.2f}%  t {es_pos.tstat_abn[i]:+5.2f}")
    print("\n  Net per-trade (positive-surprise, long):")
    for _, r in ptn_pos.iterrows():
        print(f"    hold {int(r['hold']):>2}d: n={int(r['n']):>4}  net {r['mean_net']*100:+6.2f}%  "
              f"hit {r['hit']*100:3.0f}%  abn-net {r['mean_abn_net']*100:+6.2f}%  "
              f"t {r['tstat_abn']:+5.2f}")
    pm = _daily_metrics(port_pos)
    mm = _daily_metrics(mkt_r.reindex(port_pos.index).fillna(0.0))
    print(f"\n  Calendar portfolio (hold {args.hold}d, positive-surprise long): "
          f"Sharpe {pm['sharpe']:.2f}  CAGR {pm['cagr']*100:.1f}%  "
          f"vs market Sharpe {mm['sharpe']:.2f}")
    print("=" * 72)

    report = write_report(
        mode, es_pos, ptn_pos, port_pos, es_neg, mkt_r,
        len(pos_events), len(neg_events), args.hold, start, end, params
    )
    print(f"  Report → {report}")
    print("\nAdvisory only. Backtesting can recommend; it cannot promote. Live trading BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
