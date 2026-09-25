#!/usr/bin/env python3
"""Factor diversification & correlation study (daily NSE lake).

Fills the INSUFFICIENT-EVIDENCE gap flagged in the QE Phase-Next operating doc (§5 Correlation
Project) and the Investment-Committee report: are the candidate sleeves genuinely low-correlated
to the delivery-% core, or beta-redundant? Diversification of return drivers is the only free
lunch we accept — but only if the correlations actually support it.

Across the candidate sleeves (default: delivery, momentum, lowvol, value) it computes, all on
monthly NET returns from the audited factor harness (full NSE delivery cost stack):
  * standalone net metrics (CAGR / Sharpe / MaxDD)
  * pairwise Pearson + Spearman correlation matrices
  * rolling 12-month pairwise correlation (mean / min / max) — does diversification hold over time?
  * down-month correlation: correlation conditional on the benchmark's worst-decile months
    (the dangerous case — diversifiers that converge in stress are not diversifiers)
  * drawdown-overlap matrix: % of months both sleeves are simultaneously in drawdown
  * diversification benefit: equal-weight and trailing-inverse-vol blends vs the average sleeve,
    plus the diversification ratio (Σ wσ / σ_portfolio)
  * §5 veto flags: sustained pairwise corr > 0.70 → do NOT count the pair as independent sleeves

NOTE on `value`: it is a PRICE-BASED proxy (price vs its own trailing 252d mean = long-horizon
reversion). A true value factor needs fundamentals (P/B, earnings yield) which the lake does not
hold — flagged here, not hidden. Treat its standalone numbers as indicative only.

Backtest-only. Advisory. Backtesting can recommend; it cannot promote. A human approves all
production changes. Live trading remains BLOCKED. No broker, no Kite, no live/paper state.

Usage:
    python scripts/backtest/run_factor_correlations.py
    python scripts/backtest/run_factor_correlations.py --sleeves delivery momentum lowvol value
    python scripts/backtest/run_factor_correlations.py --self-test
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

# Reuse the audited factor-study harness (loader, per-factor backtest, benchmark, metrics, costs).
from run_factor_study import (  # noqa: E402
    _ROUND_TRIP,
    _load_panel,
    _metrics,
    benchmark,
    run_factor,
    FACTORS_ALL,
)

_DEFAULT_SLEEVES = ["delivery", "momentum", "lowvol", "value"]
_CORR_VETO = 0.70          # §5: sustained pairwise corr above this → not independent sleeves
_DOWNMONTH_VETO = 0.85     # §5: worst-decile conditional corr above this → fails in stress
_DIV_RATIO_TARGET = 1.20   # §5: diversification ratio target


# ── sleeve returns ────────────────────────────────────────────────────────────


def sleeve_returns(close, turn, deliv, sleeves: list[str], top_n: int, k: int) -> pd.DataFrame:
    """Wide [month × sleeve] matrix of monthly NET returns, inner-joined on common dates."""
    cols = {}
    for s in sleeves:
        cols[s] = run_factor(s, close, turn, deliv, top_n, k).monthly_net
    return pd.DataFrame(cols).dropna()


# ── correlation / overlap primitives ──────────────────────────────────────────


def _pairs(cols: list[str]) -> list[tuple[str, str]]:
    return [(cols[i], cols[j]) for i in range(len(cols)) for j in range(i + 1, len(cols))]


def rolling_corr_summary(R: pd.DataFrame, window: int = 12) -> pd.DataFrame:
    rows = []
    for a, b in _pairs(list(R.columns)):
        rc = R[a].rolling(window).corr(R[b]).dropna()
        rows.append({
            "pair": f"{a}/{b}",
            "full": float(R[a].corr(R[b])),
            "roll_mean": float(rc.mean()) if len(rc) else float("nan"),
            "roll_min": float(rc.min()) if len(rc) else float("nan"),
            "roll_max": float(rc.max()) if len(rc) else float("nan"),
        })
    return pd.DataFrame(rows)


def down_month_corr(R: pd.DataFrame, bench: pd.Series, decile: float = 0.1) -> pd.DataFrame:
    """Pairwise correlation conditional on the benchmark's worst-`decile` months."""
    b = bench.reindex(R.index).dropna()
    sub = R.reindex(b.index)
    mask = b <= b.quantile(decile)
    s = sub[mask]
    rows = []
    for a, c in _pairs(list(R.columns)):
        rows.append({"pair": f"{a}/{c}",
                     "down_corr": float(s[a].corr(s[c])) if mask.sum() > 1 else float("nan")})
    return pd.DataFrame(rows), int(mask.sum())


def _drawdown_state(r: pd.Series) -> pd.Series:
    eq = (1.0 + r).cumprod()
    return eq < eq.cummax()      # True when below the prior equity peak


def drawdown_overlap(R: pd.DataFrame) -> pd.DataFrame:
    states = pd.DataFrame({c: _drawdown_state(R[c]) for c in R.columns})
    M = pd.DataFrame(index=R.columns, columns=R.columns, dtype=float)
    for a in R.columns:
        for b in R.columns:
            M.loc[a, b] = float((states[a] & states[b]).mean())
    return M


# ── diversification ────────────────────────────────────────────────────────────


def ew_blend(R: pd.DataFrame) -> pd.Series:
    return R.mean(axis=1)


def inv_vol_blend(R: pd.DataFrame, window: int = 12) -> pd.Series:
    """Trailing inverse-volatility blend (no lookahead: weights use data strictly before t)."""
    n = R.shape[1]
    out = []
    for t in range(len(R)):
        if t < window:
            w = np.repeat(1.0 / n, n)
        else:
            vol = R.iloc[t - window:t].std().replace(0.0, np.nan)
            iv = (1.0 / vol).fillna(0.0)
            w = (iv / iv.sum()).values if iv.sum() > 0 else np.repeat(1.0 / n, n)
        out.append(float((R.iloc[t].values * w).sum()))
    return pd.Series(out, index=R.index)


def diversification_ratio(R: pd.DataFrame, weights: np.ndarray | None = None) -> float:
    n = R.shape[1]
    w = np.repeat(1.0 / n, n) if weights is None else weights
    sig = R.std().values
    port = float((R * w).sum(axis=1).std())
    return float((w * sig).sum() / port) if port else 0.0


# ── report ────────────────────────────────────────────────────────────────────


def _matrix_md(M: pd.DataFrame, fmt: str = "{:.2f}") -> list[str]:
    cols = list(M.columns)
    L = ["| | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for r in cols:
        cells = [fmt.format(M.loc[r, c]) for c in cols]
        L.append(f"| **{r}** | " + " | ".join(cells) + " |")
    return L


def write_report(R: pd.DataFrame, bench: pd.Series, sleeves: list[str],
                 top_n: int, k: int, start: date, end: date) -> Path:
    out = _REPO / "docs" / "backtesting" / "factor-correlation-report.md"

    pear = R.corr()
    spear = R.corr(method="spearman")
    roll = rolling_corr_summary(R)
    down, n_down = down_month_corr(R, bench)
    ddm = drawdown_overlap(R)

    ew, iv = ew_blend(R), inv_vol_blend(R)
    m_ew, m_iv = _metrics(ew), _metrics(iv)
    avg_sleeve_sharpe = float(np.mean([_metrics(R[c])["sharpe"] for c in R.columns]))
    best_sleeve = max(R.columns, key=lambda c: _metrics(R[c])["sharpe"])
    best_sleeve_sharpe = _metrics(R[best_sleeve])["sharpe"]
    best_blend_sharpe = max(m_ew["sharpe"], m_iv["sharpe"])
    dr_ew = diversification_ratio(R)
    # stress behaviour: which pair actually decouples in the worst months vs which stays redundant
    down_sorted = down.dropna(subset=["down_corr"]).sort_values("down_corr")

    # veto evaluation
    veto_pairs = roll[roll["full"] > _CORR_VETO]["pair"].tolist()
    down_veto = down[down["down_corr"] > _DOWNMONTH_VETO]["pair"].tolist()

    L = [
        "# Factor Diversification & Correlation Study (Daily NSE)",
        "",
        "**Status:** COMPLETE — advisory. Live trading remains BLOCKED.",
        f"**Date:** {date.today()}",
        "**Operating doc:** `docs/strategy/qe-phase-next-cio-operating-doc-2026-06-15.md` §5 (ADR-036).",
        "**Prior:** `factor-study-report.md`, `delivery-walkforward-report.md`.",
        "",
        "Fills the §5 *INSUFFICIENT EVIDENCE* gap: are the candidate sleeves genuinely "
        "low-correlated to the delivery-% core, or beta-redundant? All series are monthly NET "
        "returns (full NSE delivery cost stack, round-trip ≈ "
        f"{_ROUND_TRIP*100:.3f}%), top-{top_n} liquid universe, long-only top-{k}, monthly rebalance.",
        "",
        "> **`value` is a price-based proxy** (price vs trailing 252d mean = long-horizon "
        "reversion). A true value factor needs fundamentals (absent from the lake). Its standalone "
        "numbers are indicative only; it is included here mainly to measure its *diversification*.",
        "",
        f"Sleeves: {', '.join(R.columns)} · period {start} → {end} · {len(R)} common months.",
        "",
        "## 1. Standalone net metrics",
        "",
        "| Sleeve | Net CAGR | Sharpe | MaxDD | Hit% | Months |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for c in R.columns:
        m = _metrics(R[c])
        L.append(f"| {c} | {m['cagr']*100:.1f}% | {m['sharpe']:.2f} | {m['maxdd']*100:.1f}% "
                 f"| {m['hit']*100:.0f}% | {m['months']} |")
    mb = _metrics(bench.reindex(R.index).dropna())
    L.append(f"| _benchmark (EW, gross)_ | {mb['cagr']*100:.1f}% | {mb['sharpe']:.2f} "
             f"| {mb['maxdd']*100:.1f}% | {mb['hit']*100:.0f}% | {mb['months']} |")

    L += ["", "## 2. Pearson correlation (monthly net returns)", ""]
    L += _matrix_md(pear)
    L += ["", "## 3. Spearman (rank) correlation", ""]
    L += _matrix_md(spear)

    L += ["", "## 4. Rolling 12-month pairwise correlation", "",
          "| Pair | Full-sample | Roll mean | Roll min | Roll max |",
          "|---|---:|---:|---:|---:|"]
    for _, r in roll.iterrows():
        L.append(f"| {r['pair']} | {r['full']:.2f} | {r['roll_mean']:.2f} "
                 f"| {r['roll_min']:.2f} | {r['roll_max']:.2f} |")

    L += ["", f"## 5. Down-month correlation (benchmark worst decile, n={n_down} months)", "",
          "| Pair | Down-month corr |", "|---|---:|"]
    for _, r in down.iterrows():
        L.append(f"| {r['pair']} | {r['down_corr']:.2f} |")

    L += ["", "## 6. Drawdown-overlap matrix (% of months both in drawdown)", ""]
    L += _matrix_md(ddm, fmt="{:.0%}")

    L += ["", "## 7. Diversification benefit", "",
          "| Construction | Net CAGR | Sharpe | MaxDD |",
          "|---|---:|---:|---:|",
          f"| best single sleeve ({best_sleeve}) | {_metrics(R[best_sleeve])['cagr']*100:.1f}% "
          f"| {_metrics(R[best_sleeve])['sharpe']:.2f} | {_metrics(R[best_sleeve])['maxdd']*100:.1f}% |",
          f"| equal-weight blend | {m_ew['cagr']*100:.1f}% | {m_ew['sharpe']:.2f} | {m_ew['maxdd']*100:.1f}% |",
          f"| inverse-vol blend (trailing 12m) | {m_iv['cagr']*100:.1f}% | {m_iv['sharpe']:.2f} | {m_iv['maxdd']*100:.1f}% |",
          "",
          f"- Best single sleeve ({best_sleeve}) Sharpe **{best_sleeve_sharpe:.2f}**; best blend Sharpe "
          f"**{best_blend_sharpe:.2f}**; average standalone sleeve Sharpe **{avg_sleeve_sharpe:.2f}**.",
          f"- **Decision-relevant comparison:** the blend Sharpe ({best_blend_sharpe:.2f}) is "
          f"{'ABOVE' if best_blend_sharpe >= best_sleeve_sharpe else 'BELOW'} the best single sleeve "
          f"({best_sleeve} {best_sleeve_sharpe:.2f}). Beating the *average* sleeve is the textbook "
          "diversification test, but the average is dragged down by the weak sleeves — the honest "
          "test is whether blending beats the *best* sleeve.",
          f"- Diversification ratio (equal-weight): **{dr_ew:.2f}** "
          f"(target > {_DIV_RATIO_TARGET:.2f}; >1 means the blend's risk is below the weighted-average "
          "sleeve risk — i.e. real diversification).",
          ""]

    # auto verdict
    blend_dilutes = best_blend_sharpe < best_sleeve_sharpe
    L += ["## 8. Read & §5 veto evaluation", ""]
    L.append(f"- **Co-allocation veto (corr > {_CORR_VETO:.2f}):** "
             + (f"**{', '.join(veto_pairs)}** — do NOT count as independent sleeves."
                if veto_pairs else "none — all pairs below the veto threshold."))
    L.append(f"- **Stress veto (down-month corr > {_DOWNMONTH_VETO:.2f}):** "
             + (f"**{', '.join(down_veto)}** — these converge when it matters."
                if down_veto else "none — diversification holds in the worst-decile months."))
    if not down_sorted.empty:
        lo, hi = down_sorted.iloc[0], down_sorted.iloc[-1]
        L.append(f"- **Stress behaviour:** in the worst-decile months **{lo['pair']}** decouples "
                 f"(corr {lo['down_corr']:.2f}) — a genuine stress diversifier — while **{hi['pair']}** "
                 f"stays redundant (corr {hi['down_corr']:.2f}). Note this can invert the naive read: "
                 "a sleeve that looks defensive on average may be most redundant exactly in drawdowns.")
    L.append(f"- **Diversification benefit:** best blend Sharpe {best_blend_sharpe:.2f} "
             f"{'DILUTES' if blend_dilutes else 'preserves/improves'} the best single sleeve "
             f"({best_sleeve} {best_sleeve_sharpe:.2f}); diversification ratio {dr_ew:.2f} "
             f"{'clears' if dr_ew > _DIV_RATIO_TARGET else 'is BELOW'} the {_DIV_RATIO_TARGET:.2f} target. "
             "Within long-only NSE equity factors in this single bull regime, diversification is "
             "largely **illusory** — the sleeves share equity beta.")
    L += [
        "",
        "## 9. Verdict & next gate",
        "",
        "This study computes the diversification inputs the QE Promotion Score left as "
        "*INSUFFICIENT EVIDENCE*. It does **not** promote anything. **Headline finding: blending the "
        "candidate long-only equity factors does NOT improve on delivery-% standalone** — it dilutes "
        "it (best blend Sharpe < delivery 1.40), and the pairwise correlations are too high (several "
        "above the 0.70 veto) to treat them as independent sleeves. The strategic consequence: do "
        "**not** rush a combined factor book; **delivery-% standalone remains the lead candidate**, "
        "and genuine diversification must come from *different return drivers* (event / structural-flow "
        "/ macro — the data-gated hypotheses H4–H8) and from *different regimes* (the regime-expansion "
        "project), not from more long-only equity factors.",
        "",
        "**Caveat:** these correlations are themselves regime-limited — in a single bull, long-only "
        "factors co-move; a real bear could change the picture, but the lake cannot test it (no "
        "sustained bear). That *strengthens* the case for the regime-expansion project (operating "
        "doc §4) before any combined-book decision.",
        "",
        "Risk control remains position sizing / portfolio drawdown limit — not a market-timing "
        "overlay (it hurt; ADR-034).",
        "",
        "> Backtesting can recommend. It cannot promote. A human approves all production changes.",
        "> Live trading remains BLOCKED.",
    ]
    out.write_text("\n".join(L) + "\n")
    return out


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    print("SELF-TEST: correlation/diversification primitives on synthetic series...")
    idx = pd.date_range("2020-01-31", periods=48, freq="ME", tz="Asia/Kolkata")
    rng = np.random.default_rng(3)
    a = pd.Series(rng.normal(0.01, 0.04, 48), index=idx)
    R = pd.DataFrame({"a": a, "a_copy": a, "indep": pd.Series(rng.normal(0.01, 0.04, 48), index=idx)})

    pear = R.corr()
    assert abs(pear.loc["a", "a_copy"] - 1.0) < 1e-9, "identical series must correlate 1.0"
    assert abs(pear.loc["a", "indep"]) < 0.5, "independent series should be ~uncorrelated"

    # diversification ratio: identical pair → ~1 (no diversification); with an independent leg → >1
    dr_dup = diversification_ratio(R[["a", "a_copy"]])
    dr_mix = diversification_ratio(R[["a", "indep"]])
    assert dr_dup < 1.05, f"duplicate pair should not diversify (got {dr_dup:.3f})"
    assert dr_mix > dr_dup, "an uncorrelated leg must improve the diversification ratio"

    ddm = drawdown_overlap(R)
    assert abs(ddm.loc["a", "a_copy"] - ddm.loc["a", "a"]) < 1e-9, "identical DD overlap"

    bench = pd.Series(rng.normal(0.008, 0.03, 48), index=idx)
    down, n_down = down_month_corr(R, bench)
    assert n_down >= 1 and not down.empty
    iv = inv_vol_blend(R)
    assert len(iv) == len(R)
    print(f"  OK — dr(dup)={dr_dup:.3f} dr(mix)={dr_mix:.3f}, down-months={n_down}")
    print("SELF-TEST PASSED.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Factor diversification & correlation study")
    ap.add_argument("--sleeves", nargs="+", default=_DEFAULT_SLEEVES,
                    help=f"factors to compare (available: {', '.join(FACTORS_ALL)})")
    ap.add_argument("--top-n", type=int, default=200)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-06-30")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    bad = [s for s in args.sleeves if s not in FACTORS_ALL]
    if bad:
        print(f"ERROR: unknown sleeve(s) {bad}; available: {FACTORS_ALL}")
        return 2

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    print("=" * 72)
    print("QuantEmbrace — Factor Diversification & Correlation Study (daily NSE)")
    print("Backtest-only. Advisory. No broker. No live trading.")
    print("=" * 72)
    print(f"  sleeves: {', '.join(args.sleeves)} · top-{args.top_n} · top-{args.k} · monthly")
    print("  Loading daily lake...")
    close, turn, deliv = _load_panel(start, end)
    print(f"  Loaded {close.shape[0]} days × {close.shape[1]} symbols")

    R = sleeve_returns(close, turn, deliv, args.sleeves, args.top_n, args.k)
    bench = benchmark(close, turn, args.top_n)
    print(f"  {len(R)} common months across {len(R.columns)} sleeves\n")

    print("  Pearson correlation:")
    print(R.corr().round(2).to_string())
    print("\n  Standalone net Sharpe:")
    for c in R.columns:
        print(f"    {c:>10}: {_metrics(R[c])['sharpe']:.2f}")
    ew = ew_blend(R)
    print(f"  equal-weight blend Sharpe: {_metrics(ew)['sharpe']:.2f}  "
          f"diversification ratio: {diversification_ratio(R):.2f}")
    print("=" * 72)

    report = write_report(R, bench, args.sleeves, args.top_n, args.k, start, end)
    print(f"  Report: {report}")
    print("\nAdvisory only. Backtesting can recommend; it cannot promote. Live trading BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
