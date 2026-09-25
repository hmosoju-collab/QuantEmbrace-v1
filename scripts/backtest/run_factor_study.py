#!/usr/bin/env python3
"""Strategy-thesis evidence — cross-sectional equity FACTOR study on the daily lake.

Tests the redirected thesis (`docs/strategy/strategy-thesis-redirection-2026-06-15.md`):
does a horizon-appropriate, positional/CNC factor strategy clear the full NSE statutory
cost stack where intraday technicals did not?

Method (long-only, survivorship-robust, point-in-time):
  * Universe per rebalance = top-N NSE names by trailing 60-day median turnover
    (close×volume) with ≥1yr price history. Delisted names simply leave the universe.
  * Monthly rebalance. For each factor, rank the liquid universe and hold the top-K
    equal-weight until the next rebalance.
  * Factors: 12-1 momentum · 1-month reversal · low-volatility · delivery-% (India
    conviction) · an equal-weight z-score combo.
  * Costs: NSE equity DELIVERY model (same rates as `IndianCostModel.delivery()` —
    STT 0.1% both legs, exchange, SEBI, stamp, GST) + slippage, charged on turnover.
  * Benchmark: equal-weight liquid-universe return (gross).

Reports gross vs NET CAGR, vol, Sharpe, max drawdown, hit rate, turnover, cost drag.

Backtest-only. Advisory. Backtesting can recommend; it cannot promote. A human approves
all production changes. Live trading remains BLOCKED. No broker, no live/paper state.

Usage:
    python scripts/backtest/run_factor_study.py
    python scripts/backtest/run_factor_study.py --top-n 200 --k 20 --start 2021-01-01
    python scripts/backtest/run_factor_study.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

import numpy as np
import pandas as pd

from strategy_engine.backtesting.backtester import IndianCostModel

LAKE = _REPO / "backtest-data" / "lake" / "ohlcv" / "market=NSE" / "segment=EQ"
_IST = "Asia/Kolkata"
COST = IndianCostModel.delivery()
SLIPPAGE_FRAC = 0.0005   # 5 bps per leg
FACTORS = ["momentum", "reversal", "lowvol", "delivery", "combo"]
# `value` is a price-based reversion proxy (see _factor_scores). It is deliberately NOT in the
# default FACTORS list so the published factor-study report is unchanged; the diversification
# study (run_factor_correlations.py) requests it explicitly. A TRUE value factor needs
# fundamentals (P/B, earnings yield) which the lake does not hold — this proxy is flagged, not hidden.
FACTORS_ALL = FACTORS + ["value"]


# ── cost model (replicates Backtester._leg_cost as a fraction of notional) ────


def _leg_cost_frac(model: IndianCostModel, side: str) -> float:
    exch = model.exchange_txn_pct / 100.0
    sebi = model.sebi_turnover_pct / 100.0
    stt = (model.stt_sell_pct if side == "SELL" else model.stt_buy_pct) / 100.0
    stamp = (model.stamp_buy_pct / 100.0) if side == "BUY" else 0.0
    gst = (exch + sebi) * (model.gst_pct / 100.0)   # brokerage = 0 for delivery
    return exch + sebi + stt + stamp + gst


_BUY_FRAC = _leg_cost_frac(COST, "BUY") + SLIPPAGE_FRAC
_SELL_FRAC = _leg_cost_frac(COST, "SELL") + SLIPPAGE_FRAC
_ROUND_TRIP = _BUY_FRAC + _SELL_FRAC   # cost to replace one name's worth of weight


# ── ETF / fund exclusion ──────────────────────────────────────────────────────
# NSE ETFs/liquid funds trade in the EQ segment and have trivially ~100% delivery%
# (units aren't day-traded), so the delivery factor ranks them top — but they are
# cash/index/commodity instruments, not stock conviction. Exclude them so factors
# measure equity selection. ISIN-based (INF=fund) would be cleaner but the lake's
# isin column is unpopulated, so this is a conservative ticker-pattern filter.
_FUND_TOKENS = ("BEES", "ETF", "LIQUID", "SETF", "NIFTY", "SENSEX", "GSEC",
                "BHARATBOND", "IETF", "MAFANG", "MON100", "HNGSNG")


def _is_fund(symbol: str) -> bool:
    s = str(symbol).upper()
    if any(t in s for t in _FUND_TOKENS):
        return True
    return s.endswith("GOLD") or s.endswith("SILVER")


def _drop_funds(s: pd.Series) -> pd.Series:
    """Drop ETF/fund tickers from a symbol-indexed Series (e.g. a turnover ranking)."""
    return s[~s.index.to_series().map(_is_fund).values]


def _drop_corp_action_dislocations(
    s: pd.Series,
    close: pd.DataFrame,
    i: int,
    lookback: int = 63,
    week_threshold: float = -0.35,
) -> pd.Series:
    """Drop symbols that suffered a ≥35% single-week price drop in the past lookback days.

    A ≥35% move in 5 trading days is not a market return — it is a demerger ex-date,
    reverse split, or special dividend. Such symbols are excluded for the current
    rebalance so the delivery-% factor doesn't mistake post-action high delivery%
    (repositioning activity) for genuine pre-move accumulation.
    """
    syms = [sym for sym in s.index if sym in close.columns]
    if not syms or i < lookback + 5:
        return s
    px = close[syms].iloc[max(0, i - lookback): i + 1]
    weekly_ret = px.pct_change(5)
    dislocated = weekly_ret.min() < week_threshold
    clean = dislocated[~dislocated].index.tolist()
    return s[s.index.isin(clean)]


# ── data loading ──────────────────────────────────────────────────────────────


def _load_panel(start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load the daily lake into wide [date × symbol] matrices: close, turnover, delivery%."""
    import glob
    import pyarrow.dataset as ds

    files = glob.glob(str(LAKE / "symbol=*" / "interval=1d" / "year=*" / "part-0.parquet"))
    if not files:
        raise FileNotFoundError(f"No daily parquet under {LAKE}")
    table = ds.dataset(files, format="parquet").to_table(
        columns=["timestamp", "symbol", "close", "volume", "delivery_pct"]
    )
    df = table.to_pandas()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(_IST).dt.normalize()
    df = df[(df["date"] >= pd.Timestamp(start, tz=_IST)) & (df["date"] <= pd.Timestamp(end, tz=_IST))]
    df["turnover"] = df["close"] * df["volume"]

    close = df.pivot_table(index="date", columns="symbol", values="close")
    turn = df.pivot_table(index="date", columns="symbol", values="turnover")
    deliv = df.pivot_table(index="date", columns="symbol", values="delivery_pct")
    return close.sort_index(), turn.sort_index(), deliv.sort_index()


# ── factor computation ────────────────────────────────────────────────────────


def _zscore(s: pd.Series) -> pd.Series:
    mu, sd = s.mean(), s.std()
    return (s - mu) / sd if sd and not np.isnan(sd) else s * 0.0


def _factor_scores(close: pd.DataFrame, deliv: pd.DataFrame, univ: list[str],
                   i: int) -> pd.DataFrame:
    """Compute factor scores for the universe at price-index position i."""
    px = close[univ]
    # returns over trailing windows (i is the rebalance row index)
    p_now = px.iloc[i]
    p_21 = px.iloc[i - 21]
    p_252 = px.iloc[i - 252]
    mom = p_21 / p_252 - 1.0            # 12-1 momentum (skip last month)
    rev = -(p_now / p_21 - 1.0)         # 1-month reversal
    rets = px.iloc[i - 63:i].pct_change()
    lowvol = -rets.std()                # low vol → high score
    dlv = deliv.reindex(columns=univ).iloc[i - 21:i].mean()
    # value (price-based proxy): trading below your own trailing 1y mean = "cheap" → high score.
    # Long-horizon reversion, distinct from 1m reversal and 12-1 momentum. Non-NaN wherever
    # momentum is (both need a valid 252d window), so adding it does not change the other factors'
    # surviving universe. A real value factor needs fundamentals (absent from the lake) — flagged.
    p_mean_252 = px.iloc[i - 252:i].mean()
    value = -(p_now / p_mean_252 - 1.0)

    f = pd.DataFrame({"momentum": mom, "reversal": rev, "lowvol": lowvol,
                      "delivery": dlv, "value": value})
    f = f.dropna()
    f["combo"] = (_zscore(f["momentum"]) + _zscore(f["reversal"])
                  + _zscore(f["lowvol"]) + _zscore(f["delivery"])) / 4.0
    return f


# ── backtest engine (monthly rebalance, long-only top-K) ──────────────────────


@dataclass
class FactorResult:
    factor: str
    monthly_net: pd.Series
    monthly_gross: pd.Series
    avg_turnover: float
    total_cost_frac: float
    n_rebalances: int


def _rebalance_rows(index: pd.DatetimeIndex) -> list[int]:
    """Row positions of the last trading day of each month."""
    months = index.to_period("M")
    rows = []
    for m in months.unique():
        mask = months == m
        rows.append(np.where(mask)[0][-1])
    return sorted(rows)


def run_factor(factor: str, close: pd.DataFrame, turn: pd.DataFrame, deliv: pd.DataFrame,
               top_n: int, k: int) -> FactorResult:
    idx = close.index
    rb = [r for r in _rebalance_rows(idx) if r >= 252 and r < len(idx) - 1]
    held: set[str] = set()
    net_rets, gross_rets, turns = [], [], []

    for j in range(len(rb) - 1):
        i, i_next = rb[j], rb[j + 1]
        # liquid universe: top-N by trailing 60d median turnover, valid price now
        liq = turn.iloc[i - 60:i].median().dropna()
        valid_now = close.iloc[i].dropna().index
        liq = _drop_funds(liq[liq.index.isin(valid_now)])
        liq = _drop_corp_action_dislocations(liq, close, i)
        univ = liq.sort_values(ascending=False).head(top_n).index.tolist()
        if len(univ) < k:
            continue

        scores = _factor_scores(close, deliv, univ, i)
        if scores.empty:
            continue
        picks = set(scores[factor].sort_values(ascending=False).head(k).index)

        # holding-period return: close[i] → close[i_next], equal weight, ffill gaps
        p0 = close[list(picks)].iloc[i]
        p1 = close[list(picks)].ffill().iloc[i_next]
        name_rets = (p1 / p0 - 1.0).dropna()
        gross = name_rets.mean() if len(name_rets) else 0.0

        # cost: replace (picks Δ held) names; turnover one-way fraction = changed/k
        changed = len(picks.symmetric_difference(held)) / 2.0 if held else len(picks)
        turn_frac = changed / k
        cost = turn_frac * _ROUND_TRIP
        net = gross - cost

        gross_rets.append(gross)
        net_rets.append(net)
        turns.append(turn_frac)
        held = picks

    dates = [idx[rb[j + 1]] for j in range(len(rb) - 1)][:len(net_rets)]
    return FactorResult(
        factor=factor,
        monthly_net=pd.Series(net_rets, index=dates),
        monthly_gross=pd.Series(gross_rets, index=dates),
        avg_turnover=float(np.mean(turns)) if turns else 0.0,
        total_cost_frac=float(np.sum([t * _ROUND_TRIP for t in turns])),
        n_rebalances=len(net_rets),
    )


def benchmark(close: pd.DataFrame, turn: pd.DataFrame, top_n: int) -> pd.Series:
    """Equal-weight liquid-universe monthly return (gross), as the market benchmark."""
    idx = close.index
    rb = [r for r in _rebalance_rows(idx) if r >= 252 and r < len(idx) - 1]
    rets, dates = [], []
    for j in range(len(rb) - 1):
        i, i_next = rb[j], rb[j + 1]
        liq = turn.iloc[i - 60:i].median().dropna()
        valid = close.iloc[i].dropna().index
        liq = _drop_funds(liq[liq.index.isin(valid)])
        univ = liq.sort_values(ascending=False).head(top_n).index.tolist()
        p0 = close[univ].iloc[i]
        p1 = close[univ].ffill().iloc[i_next]
        r = (p1 / p0 - 1.0).dropna()
        rets.append(r.mean() if len(r) else 0.0)
        dates.append(idx[i_next])
    return pd.Series(rets, index=dates)


# ── metrics ───────────────────────────────────────────────────────────────────


def _metrics(monthly: pd.Series) -> dict:
    if monthly.empty:
        return {"cagr": 0.0, "vol": 0.0, "sharpe": 0.0, "maxdd": 0.0, "hit": 0.0, "months": 0}
    eq = (1.0 + monthly).cumprod()
    years = len(monthly) / 12.0
    cagr = eq.iloc[-1] ** (1.0 / years) - 1.0 if years > 0 and eq.iloc[-1] > 0 else -1.0
    vol = monthly.std() * np.sqrt(12)
    sharpe = (monthly.mean() * 12) / vol if vol else 0.0
    dd = (eq / eq.cummax() - 1.0).min()
    hit = (monthly > 0).mean()
    return {"cagr": cagr, "vol": vol, "sharpe": sharpe, "maxdd": dd, "hit": hit, "months": len(monthly)}


# ── report ────────────────────────────────────────────────────────────────────


def _write_report(results: list[FactorResult], bench: pd.Series, top_n: int, k: int,
                  start: date, end: date, n_symbols: int,
                  report_path: Optional[Path] = None) -> Path:
    bm = _metrics(bench)
    out = report_path or (_REPO / "docs" / "backtesting" / "factor-study-report.md")
    L = [
        "# Strategy-Thesis Evidence — Cross-Sectional Factor Study (Daily NSE)",
        "",
        "**Status:** COMPLETE — advisory. Live trading remains BLOCKED.",
        f"**Date:** {date.today()}",
        "**Thesis:** `docs/strategy/strategy-thesis-redirection-2026-06-15.md` (ADR-034)",
        "",
        "Tests whether a positional/CNC cross-sectional factor strategy clears the full NSE",
        "delivery cost stack where intraday technicals did not.",
        "",
        "| Parameter | Value |",
        "|---|---|",
        f"| Universe | top-{top_n} NSE by trailing 60d turnover, point-in-time (survivorship-robust) |",
        f"| Symbols seen | {n_symbols} |",
        f"| Portfolio | long-only top-{k} equal-weight, monthly rebalance |",
        f"| Period | {start} → {end} |",
        f"| Cost model | NSE delivery (`{COST.cost_model_version}`) + {SLIPPAGE_FRAC*1e4:.0f}bps/leg slippage; round-trip ≈ {_ROUND_TRIP*100:.3f}% |",
        "",
        "## Results (net of costs unless noted)",
        "",
        "| Factor | Net CAGR | Gross CAGR | Vol | Sharpe | MaxDD | Hit% | Turn/mo | Cost drag |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    table = []
    for r in results:
        n, g = _metrics(r.monthly_net), _metrics(r.monthly_gross)
        L.append(
            f"| **{r.factor}** | {n['cagr']*100:.1f}% | {g['cagr']*100:.1f}% | {n['vol']*100:.1f}% "
            f"| {n['sharpe']:.2f} | {n['maxdd']*100:.1f}% | {n['hit']*100:.0f}% "
            f"| {r.avg_turnover*100:.0f}% | {r.total_cost_frac*100:.1f}% |"
        )
        table.append((r.factor, n))
    L.append(
        f"| _benchmark (EW univ, gross)_ | — | {bm['cagr']*100:.1f}% | {bm['vol']*100:.1f}% "
        f"| {bm['sharpe']:.2f} | {bm['maxdd']*100:.1f}% | {bm['hit']*100:.0f}% | — | — |"
    )

    best = max(table, key=lambda x: x[1]["cagr"]) if table else ("none", {"cagr": -1})
    beats = [f for f, m in table if m["cagr"] > bm["cagr"]]
    L += [
        "",
        "## Read",
        "",
        f"- Best net factor: **{best[0]}** ({best[1]['cagr']*100:.1f}% net CAGR, "
        f"Sharpe {best[1]['sharpe']:.2f}).",
        f"- Benchmark (equal-weight liquid universe, gross): {bm['cagr']*100:.1f}% CAGR, "
        f"Sharpe {bm['sharpe']:.2f}.",
        f"- Factors beating the benchmark net of costs: "
        f"{', '.join(beats) if beats else 'NONE'}.",
        "",
        "> **Backtesting can recommend. It cannot promote. A human approves all production",
        "> changes.** Long-only, monthly, ffill on delisting gaps (slightly optimistic on",
        "> delisting losses); no walk-forward parameter optimisation yet. If a factor clears",
        "> the benchmark net with margin, the next gate is a walk-forward + paper validation",
        "> (positional/CNC) — NOT promotion. Live trading remains BLOCKED.",
    ]
    out.write_text("\n".join(L) + "\n")
    return out


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    print("SELF-TEST: synthetic panel (no lake)...")
    rng = np.random.default_rng(7)
    dates = pd.date_range("2020-01-01", periods=400, freq="B", tz=_IST)
    syms = [f"S{i:03d}" for i in range(60)]
    # random-walk prices with a small persistent drift per name (so momentum has signal)
    drift = rng.normal(0, 0.0006, len(syms))
    steps = rng.normal(drift, 0.02, (len(dates), len(syms)))
    close = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=dates, columns=syms)
    vol = pd.DataFrame(rng.uniform(1e5, 1e6, (len(dates), len(syms))), index=dates, columns=syms)
    turn = close * vol
    deliv = pd.DataFrame(rng.uniform(20, 80, (len(dates), len(syms))), index=dates, columns=syms)
    r = run_factor("momentum", close, turn, deliv, top_n=40, k=8)
    assert r.n_rebalances > 0, "no rebalances produced"
    m = _metrics(r.monthly_net)
    assert "cagr" in m and r.monthly_net.notna().all()
    assert _ROUND_TRIP > 0.002, "delivery round-trip cost looks too low"
    print(f"  OK — {r.n_rebalances} rebalances, net CAGR {m['cagr']*100:.1f}%, "
          f"round-trip cost {_ROUND_TRIP*100:.3f}%")
    print("SELF-TEST PASSED.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross-sectional factor study on the daily NSE lake")
    ap.add_argument("--top-n", type=int, default=200, help="Liquid universe size (default 200)")
    ap.add_argument("--k", type=int, default=20, help="Portfolio size, top-K (default 20)")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-06-30")
    ap.add_argument("--report", default=None,
                    help="Output report path (default: docs/backtesting/factor-study-report.md)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    print("=" * 72)
    print("QuantEmbrace — Cross-Sectional Factor Study (daily NSE, positional/CNC)")
    print("Backtest-only. Advisory. No broker. No live trading.")
    print("=" * 72)
    print(f"  Universe top-{args.top_n} by turnover · long-only top-{args.k} · monthly rebalance")
    print(f"  Period {start} → {end} · round-trip cost ≈ {_ROUND_TRIP*100:.3f}% (delivery)")
    print("  Loading daily lake...")
    close, turn, deliv = _load_panel(start, end)
    print(f"  Loaded {close.shape[0]} trading days × {close.shape[1]} symbols")

    results = [run_factor(f, close, turn, deliv, args.top_n, args.k) for f in FACTORS]
    bench = benchmark(close, turn, args.top_n)
    bm = _metrics(bench)

    print()
    print(f"{'Factor':>10}  {'NetCAGR':>8}  {'GrossCAGR':>9}  {'Sharpe':>6}  {'MaxDD':>7}  {'Hit':>5}  {'Turn/mo':>7}")
    print("-" * 64)
    for r in results:
        n, g = _metrics(r.monthly_net), _metrics(r.monthly_gross)
        print(f"{r.factor:>10}  {n['cagr']*100:>7.1f}%  {g['cagr']*100:>8.1f}%  {n['sharpe']:>6.2f}  "
              f"{n['maxdd']*100:>6.1f}%  {n['hit']*100:>4.0f}%  {r.avg_turnover*100:>6.0f}%")
    print(f"{'benchmark':>10}  {'—':>8}  {bm['cagr']*100:>8.1f}%  {bm['sharpe']:>6.2f}  "
          f"{bm['maxdd']*100:>6.1f}%  {bm['hit']*100:>4.0f}%  {'—':>7}")
    print("=" * 72)

    report_path = Path(args.report) if args.report else None
    report = _write_report(results, bench, args.top_n, args.k, start, end, close.shape[1],
                           report_path=report_path)
    print(f"  Report: {report}")
    print("\nAdvisory only. Backtesting can recommend; it cannot promote. Live trading BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
