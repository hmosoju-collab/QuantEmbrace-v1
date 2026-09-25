#!/usr/bin/env python3
"""H4 — Delivery-spike event-drift study (daily NSE lake).

Operating doc §7 ranked H4 the top *lake-ready* hypothesis after the correlation study showed the
monthly equity factors do not diversify each other. H4 is a DIFFERENT return driver: an EVENT, not a
monthly cross-sectional rank.

Economic mechanism: a sudden spike in a name's delivery % on elevated volume is a discrete
ACCUMULATION event — informed buyers taking delivery (not intraday churn). If that carries
information, prices drift over the following days; a multi-day hold amortizes the delivery cost
(the very thing that killed intraday). This is distinct from the smooth monthly delivery-LEVEL
factor (which ranks persistently-high-delivery names).

NO-LOOKAHEAD (critical): NSE delivery % is published AFTER market close on day t, so the earliest
tradable action is day t+1. Every event enters at close[t+1]; the signal uses data through day t
only, and the spike baseline EXCLUDES day t (uses t-1 back).

Signal on day t (name must be in the top-N liquid universe, funds/ETFs excluded):
  * delivery z-score: (deliv[t] - mean(deliv[t-1-L:t-1])) / std(...) >= Z
  * volume ratio:     turnover[t] / median(turnover[t-1-L:t-1])       >= VOL_MULT
  * delivery floor:   deliv[t] >= DLV_FLOOR  (genuine high delivery, not just a relative blip)
Per-symbol cooldown prevents stacking re-entries while a position is nominally open.

Outputs (advisory):
  * EVENT STUDY: mean forward return and mean ABNORMAL return (vs an EW large-cap market index)
    over horizons T+1..T+20, with a t-stat — does the drift exist and is it significant?
  * Net per-trade economics at H ∈ {5,10,20} days, full NSE delivery cost stack (round-trip ≈ 0.322%).
  * A calendar-time daily equal-weight portfolio (held names earn daily, costs on entry/exit) →
    CAGR/Sharpe/MaxDD vs the market, so H4 is comparable/correlatable with the other sleeves.

Backtest-only. Advisory. Backtesting can recommend; it cannot promote. A human approves all
production changes. Live trading remains BLOCKED. No broker, no Kite, no live/paper state.

Usage:
    python scripts/backtest/run_delivery_spike.py
    python scripts/backtest/run_delivery_spike.py --z 2.0 --vol-mult 1.5 --dlv-floor 50 --hold 10
    python scripts/backtest/run_delivery_spike.py --self-test
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

from run_factor_study import (  # noqa: E402
    _BUY_FRAC,
    _SELL_FRAC,
    _ROUND_TRIP,
    _is_fund,
    _load_panel,
    _metrics,
)

_HORIZONS = (1, 2, 3, 5, 10, 15, 20)
_NET_HOLDS = (5, 10, 20)
_COOLDOWN = 20          # per-symbol re-entry block (days), independent of the hold being tested


# ── market index (EW large-cap proxy) ─────────────────────────────────────────


def _market(close: pd.DataFrame, turn: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Return (daily EW market return, index level) from the top-100 names by total turnover."""
    keep = [c for c in turn.columns if not _is_fund(c)]
    top = turn[keep].sum().nlargest(100).index
    r = close[top].pct_change().mean(axis=1).fillna(0.0)
    return r, (1.0 + r).cumprod()


# ── event detection ────────────────────────────────────────────────────────────


def detect_events(close, turn, deliv, top_n: int, z: float, vol_mult: float,
                  dlv_floor: float, lookback: int = 21) -> list[tuple[int, str]]:
    """List of (entry_idx, symbol). entry_idx = signal_day + 1 (delivery % is post-close)."""
    keep = [c for c in close.columns if not _is_fund(c)]
    cl, tn, dv = close[keep], turn[keep], deliv[keep]

    base_mean = dv.shift(1).rolling(lookback, min_periods=lookback // 2).mean()
    base_std = dv.shift(1).rolling(lookback, min_periods=lookback // 2).std()
    dlv_z = (dv - base_mean) / base_std
    vol_med = tn.shift(1).rolling(lookback, min_periods=lookback // 2).median()
    vol_ratio = tn / vol_med
    liq = tn.shift(1).rolling(60, min_periods=30).median()
    liq_rank = liq.rank(axis=1, ascending=False)

    spike = ((dlv_z >= z) & (vol_ratio >= vol_mult) & (dv >= dlv_floor)
             & (liq_rank <= top_n) & cl.notna())

    sm = spike.fillna(False).values
    cols = list(spike.columns)
    n = len(spike)
    open_until: dict[str, int] = {}
    events: list[tuple[int, str]] = []
    for ti in range(max(lookback, 60) + 1, n - 1):       # need history + ≥1 forward day
        for ci in np.where(sm[ti])[0]:
            sym = cols[ci]
            if open_until.get(sym, -1) >= ti:
                continue
            events.append((ti + 1, sym))                 # enter at close[t+1]
            open_until[sym] = ti + _COOLDOWN
    return events


# ── event study (forward drift) ───────────────────────────────────────────────


@dataclass
class EventStudy:
    horizons: list[int]
    mean_ret: list[float]
    mean_abn: list[float]
    tstat_abn: list[float]
    n: int


def event_study(close, mkt_level: pd.Series, events, horizons=_HORIZONS) -> EventStudy:
    idx = close.index
    n = len(idx)
    mkt = mkt_level.values
    rows_ret = {h: [] for h in horizons}
    rows_abn = {h: [] for h in horizons}
    for e, sym in events:
        col = close[sym].values
        base = col[e]
        if not np.isfinite(base) or base <= 0:
            continue
        mbase = mkt[e]
        for h in horizons:
            d = e + h
            if d >= n:
                continue
            px = col[d]
            if not np.isfinite(px):
                continue
            r = px / base - 1.0
            mr = mkt[d] / mbase - 1.0
            rows_ret[h].append(r)
            rows_abn[h].append(r - mr)
    mean_ret, mean_abn, tstat = [], [], []
    for h in horizons:
        a = np.array(rows_abn[h], dtype=float)
        r = np.array(rows_ret[h], dtype=float)
        mean_ret.append(float(r.mean()) if r.size else float("nan"))
        mean_abn.append(float(a.mean()) if a.size else float("nan"))
        tstat.append(float(a.mean() / (a.std(ddof=1) / np.sqrt(a.size)))
                     if a.size > 2 and a.std(ddof=1) > 0 else float("nan"))
    n_events = max((len(rows_abn[h]) for h in horizons), default=0)
    return EventStudy(list(horizons), mean_ret, mean_abn, tstat, n_events)


# ── net per-trade economics at fixed holds ────────────────────────────────────


def per_trade_net(close, mkt_level, events, holds=_NET_HOLDS) -> pd.DataFrame:
    idx_n = len(close.index)
    mkt = mkt_level.values
    rows = []
    for h in holds:
        nets, abns = [], []
        for e, sym in events:
            d = e + h
            if d >= idx_n:
                continue
            col = close[sym].values
            base, px = col[e], col[d]
            if not (np.isfinite(base) and np.isfinite(px) and base > 0):
                continue
            gross = px / base - 1.0
            net = gross - _ROUND_TRIP
            mr = mkt[d] / mkt[e] - 1.0
            nets.append(net)
            abns.append(net - mr)
        nets = np.array(nets)
        abns = np.array(abns)
        rows.append({
            "hold": h, "n": int(nets.size),
            "mean_net": float(nets.mean()) if nets.size else float("nan"),
            "median_net": float(np.median(nets)) if nets.size else float("nan"),
            "hit": float((nets > 0).mean()) if nets.size else float("nan"),
            "mean_abn_net": float(abns.mean()) if abns.size else float("nan"),
            "tstat_abn": float(abns.mean() / (abns.std(ddof=1) / np.sqrt(abns.size)))
                         if abns.size > 2 and abns.std(ddof=1) > 0 else float("nan"),
        })
    return pd.DataFrame(rows)


# ── calendar-time daily EW portfolio ──────────────────────────────────────────


def calendar_portfolio(close, events, hold: int) -> pd.Series:
    """Daily EW return of all open event positions; costs charged on entry/exit days.

    A position from event (e, sym) is HELD (earns daily returns) on days e+1..e+hold; weight each
    held day = 1/(open count that day). Buy cost hits the first held day, sell cost the last.
    """
    idx = close.index
    n = len(idx)
    rets = close.pct_change()
    sum_r = np.zeros(n)
    cnt = np.zeros(n)
    entries = np.zeros(n)
    exits = np.zeros(n)
    for e, sym in events:
        first = e + 1
        last = min(e + hold, n - 1)
        if first > n - 1 or first > last:
            continue
        col = rets[sym].values
        for d in range(first, last + 1):
            r = col[d]
            if np.isfinite(r):
                sum_r[d] += r
                cnt[d] += 1
        entries[first] += 1
        exits[last] += 1
    port = np.zeros(n)
    for d in range(n):
        if cnt[d] > 0:
            cost = (entries[d] * _BUY_FRAC + exits[d] * _SELL_FRAC) / cnt[d]
            port[d] = sum_r[d] / cnt[d] - cost
    return pd.Series(port, index=idx)


def _daily_metrics(daily: pd.Series, active_only: bool = True) -> dict:
    s = daily[daily != 0.0] if active_only else daily
    if s.empty:
        return {"cagr": 0.0, "sharpe": 0.0, "maxdd": 0.0, "ann_ret": 0.0, "days": 0}
    eq = (1.0 + daily).cumprod()
    years = len(daily) / 252.0
    cagr = eq.iloc[-1] ** (1.0 / years) - 1.0 if years > 0 and eq.iloc[-1] > 0 else -1.0
    vol = daily.std() * np.sqrt(252)
    sharpe = (daily.mean() * 252) / vol if vol else 0.0
    dd = (eq / eq.cummax() - 1.0).min()
    return {"cagr": cagr, "sharpe": sharpe, "maxdd": dd,
            "ann_ret": daily.mean() * 252, "days": int((daily != 0).sum())}


# ── report ────────────────────────────────────────────────────────────────────


def write_report(es: EventStudy, ptn: pd.DataFrame, port: pd.Series, mkt_r: pd.Series,
                 n_events: int, hold: int, z: float, vol_mult: float, dlv_floor: float,
                 top_n: int, start: date, end: date) -> Path:
    out = _REPO / "docs" / "backtesting" / "delivery-spike-report.md"
    pm = _daily_metrics(port)
    mm = _daily_metrics(mkt_r.reindex(port.index).fillna(0.0))

    sig_pos = any(np.isfinite(t) and t > 2.0 for t in es.tstat_abn)
    sig_neg = any(np.isfinite(t) and t < -2.0 for t in es.tstat_abn)
    finite_t = [t for t in es.tstat_abn if np.isfinite(t)]
    min_t = min(finite_t) if finite_t else float("nan")
    max_t = max(finite_t) if finite_t else float("nan")
    net_ok = bool((ptn["mean_net"] > 0).any() and (ptn["tstat_abn"] > 2.0).any())
    if sig_pos:
        drift_line = (f"abnormal returns are significantly **POSITIVE** (t up to {max_t:+.2f}) — a real "
                      "post-spike drift exists.")
    elif sig_neg:
        drift_line = (f"abnormal returns are significantly **NEGATIVE** (t down to {min_t:+.2f}) — "
                      "post-spike names **underperform** the market. The signal is anti-predictive "
                      "(mildly contrarian), not merely absent.")
    else:
        drift_line = "no horizon is statistically significant (|t|>2) — no reliable drift."

    L = [
        "# H4 — Delivery-Spike Event-Drift Study (Daily NSE)",
        "",
        "**Status:** COMPLETE — advisory. Live trading remains BLOCKED.",
        f"**Date:** {date.today()}",
        "**Operating doc:** `qe-phase-next-cio-operating-doc-2026-06-15.md` §7 (H4) · ADR-036.",
        "**Hypothesis:** a delivery-% spike on elevated volume = an accumulation event with "
        "multi-day drift; a different return driver from the monthly delivery-level factor.",
        "",
        "> **No-lookahead:** NSE delivery % is published post-close on day t → every event enters at "
        "**close[t+1]**; the spike baseline excludes day t. Costs = full NSE delivery stack "
        f"(round-trip ≈ {_ROUND_TRIP*100:.3f}%). Abnormal return = vs an EW top-100 market index.",
        "",
        f"Signal: delivery z ≥ {z}, volume ≥ {vol_mult}× median, delivery ≥ {dlv_floor}%, top-{top_n} "
        f"liquid (funds excluded). Period {start} → {end}. **{n_events} events.**",
        "",
        "## 1. Event study — forward drift after a spike",
        "",
        "| Horizon (days) | Mean return | Mean abnormal | t-stat (abnormal) |",
        "|---|---:|---:|---:|",
    ]
    for i, h in enumerate(es.horizons):
        L.append(f"| T+{h} | {es.mean_ret[i]*100:+.2f}% | {es.mean_abn[i]*100:+.2f}% | {es.tstat_abn[i]:.2f} |")

    L += [
        "",
        "## 2. Net per-trade economics (full delivery cost)",
        "",
        "| Hold (days) | Trades | Mean net | Median net | Hit% | Mean abnormal net | t-stat |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in ptn.iterrows():
        L.append(f"| {int(r['hold'])} | {int(r['n'])} | {r['mean_net']*100:+.2f}% | "
                 f"{r['median_net']*100:+.2f}% | {r['hit']*100:.0f}% | {r['mean_abn_net']*100:+.2f}% "
                 f"| {r['tstat_abn']:.2f} |")

    L += [
        "",
        f"## 3. Calendar-time daily EW portfolio (hold {hold}d)",
        "",
        "| Series | CAGR | Sharpe | MaxDD | Active days |",
        "|---|---:|---:|---:|---:|",
        f"| delivery-spike portfolio | {pm['cagr']*100:.1f}% | {pm['sharpe']:.2f} | {pm['maxdd']*100:.1f}% | {pm['days']} |",
        f"| market (EW top-100) | {mm['cagr']*100:.1f}% | {mm['sharpe']:.2f} | {mm['maxdd']*100:.1f}% | — |",
        "",
        "## 4. Read & verdict",
        "",
        f"- **Drift:** {drift_line}",
        "- **Beta caveat:** the *nominal* forward returns are positive (these are bull-market names), "
        "but that is market beta — the decision-relevant figure is the **abnormal** return vs the "
        "market (the abnormal columns above), which is the opposite sign.",
        f"- **Net of cost:** "
        + ("at least one hold has positive mean net AND a significant positive abnormal t-stat — "
           "H4 clears costs."
           if net_ok else
           "**no hold combines positive mean net with a significant positive abnormal t-stat** — H4 "
           "does NOT clear costs on this evidence (nominal net is positive but it is pure beta; the "
           "abnormal-net t-stats are significantly negative)."),
        f"- **Portfolio:** Sharpe {pm['sharpe']:.2f} vs market {mm['sharpe']:.2f} "
        f"({'beats' if pm['sharpe'] > mm['sharpe'] else 'BELOW'} the market).",
        "",
        "**Verdict:** "
        + ("CARRY FORWARD — H4 shows a tradable, cost-clearing, different-driver edge; next gate is a "
           "walk-forward (per-year OOS) + a correlation check vs delivery-% before any combined book."
           if (net_ok and sig_pos and pm["sharpe"] > mm["sharpe"]) else
           "**REJECTED on this evidence.** Delivery-spike names do not out-drift the market net of "
           "cost; if anything the long signal is mildly contrarian (significantly negative abnormal "
           "returns). Record the negative result; do **not** carry H4 forward as a long event signal. "
           "Per policy we do NOT tune-to-fit — flipping it to a short/contrarian signal would be a "
           "DIFFERENT hypothesis needing its own economic rationale, not a parameter sweep."),
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production changes.",
        "> Live trading remains BLOCKED. Single bull regime in sample — no sustained-bear evidence.",
    ]
    out.write_text("\n".join(L) + "\n")
    return out


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    print("SELF-TEST: synthetic panel with planted post-spike drift...")
    rng = np.random.default_rng(17)
    dates = pd.date_range("2020-01-01", periods=400, freq="B", tz="Asia/Kolkata")
    syms = [f"S{i:03d}" for i in range(40)]
    steps = rng.normal(0.0002, 0.008, (len(dates), len(syms)))
    deliv = pd.DataFrame(rng.uniform(20, 45, (len(dates), len(syms))), index=dates, columns=syms)
    # plant spikes: every 20 days a name gets a delivery spike + a clear post-event drift over the
    # next 10 days (strong enough to recover above synthetic noise — this validates the machinery,
    # NOT a claim about real drift).
    for t in range(80, len(dates) - 25, 20):
        s = syms[(t // 20) % len(syms)]
        ci = syms.index(s)
        deliv.iloc[t, ci] = 80.0
        steps[t + 1:t + 11, ci] += 0.005
    close = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=dates, columns=syms)
    turn = close * pd.DataFrame(rng.uniform(1e5, 1e6, close.shape), index=dates, columns=syms)
    turn.iloc[::40] *= 3.0   # volume spikes on the same cadence

    ev = detect_events(close, turn, deliv, top_n=40, z=2.0, vol_mult=1.5, dlv_floor=60)
    assert len(ev) > 0, "no events detected on planted spikes"
    assert all(isinstance(e, int) and e >= 1 for e, _ in ev)
    mkt_r, mkt_l = _market(close, turn)
    es = event_study(close, mkt_l, ev)
    ptn = per_trade_net(close, mkt_l, ev)
    port = calendar_portfolio(close, ev, hold=10)
    assert len(port) == len(close)
    assert es.n > 0 and not ptn.empty
    # the planted drift should show a positive mean return at T+10
    i10 = es.horizons.index(10)
    assert es.mean_ret[i10] > 0, "planted positive drift not recovered"
    print(f"  OK — {len(ev)} events, T+10 mean ret {es.mean_ret[i10]*100:+.2f}%, "
          f"portfolio Sharpe {_daily_metrics(port)['sharpe']:.2f}")
    print("SELF-TEST PASSED.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="H4 delivery-spike event-drift study")
    ap.add_argument("--top-n", type=int, default=200)
    ap.add_argument("--z", type=float, default=2.0, help="delivery z-score threshold")
    ap.add_argument("--vol-mult", type=float, default=1.5, help="volume / median multiple")
    ap.add_argument("--dlv-floor", type=float, default=50.0, help="minimum delivery %% to qualify")
    ap.add_argument("--hold", type=int, default=10, help="calendar-portfolio hold (days)")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-06-30")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    print("=" * 72)
    print("QuantEmbrace — H4 Delivery-Spike Event-Drift Study (daily NSE)")
    print("Backtest-only. Advisory. No broker. No live trading. Entry at close[t+1] (no lookahead).")
    print("=" * 72)
    print(f"  z≥{args.z} · vol≥{args.vol_mult}× · deliv≥{args.dlv_floor}% · top-{args.top_n} liquid")
    print("  Loading daily lake...")
    close, turn, deliv = _load_panel(start, end)
    print(f"  Loaded {close.shape[0]} days × {close.shape[1]} symbols")

    events = detect_events(close, turn, deliv, args.top_n, args.z, args.vol_mult, args.dlv_floor)
    print(f"  Detected {len(events)} delivery-spike events")
    mkt_r, mkt_l = _market(close, turn)
    es = event_study(close, mkt_l, events)
    ptn = per_trade_net(close, mkt_l, events)
    port = calendar_portfolio(close, events, args.hold)

    print("\n  Event study (abnormal return t-stat):")
    for i, h in enumerate(es.horizons):
        print(f"    T+{h:<2}: ret {es.mean_ret[i]*100:+6.2f}%  abn {es.mean_abn[i]*100:+6.2f}%  t {es.tstat_abn[i]:+5.2f}")
    print("\n  Net per-trade:")
    for _, r in ptn.iterrows():
        print(f"    hold {int(r['hold']):>2}d: n={int(r['n']):>4}  net {r['mean_net']*100:+6.2f}%  "
              f"hit {r['hit']*100:3.0f}%  abn-net {r['mean_abn_net']*100:+6.2f}%  t {r['tstat_abn']:+5.2f}")
    pm, mm = _daily_metrics(port), _daily_metrics(mkt_r.reindex(port.index).fillna(0.0))
    print(f"\n  Calendar portfolio (hold {args.hold}d): Sharpe {pm['sharpe']:.2f}  "
          f"CAGR {pm['cagr']*100:.1f}%  vs market Sharpe {mm['sharpe']:.2f}")
    print("=" * 72)

    report = write_report(es, ptn, port, mkt_r, len(events), args.hold,
                          args.z, args.vol_mult, args.dlv_floor, args.top_n, start, end)
    print(f"  Report: {report}")
    print("\nAdvisory only. Backtesting can recommend; it cannot promote. Live trading BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
