#!/usr/bin/env python3
"""
QuantEmbrace — US Positional Candidate Screen (Phase 2 of ADR-041)

Screens 7 candidate strategies — all canonical QuantConnect-Strategy-Library /
published-literature families — on the curated US EOD lake (Phase 1, snapshot
ds-b9b110ac58d57cae) against a PRE-REGISTERED gate.

EVERYTHING BELOW IS DECLARED BEFORE THE FIRST REAL RUN (O-1/F1 discipline).

Candidates (monthly rebalance, long-only, ETF-first):
    GEM        Antonacci Global Equities Momentum: 12-mo TR picks SPY vs EFA,
               absolute-momentum filter vs SHY, defensive asset IEF.
    GTAA5      Faber tactical asset allocation: SPY/EFA/IEF/GLD/VNQ each 20%
               iff price > 10-month SMA else that sleeve in SHY.
    SECTOR     Sector rotation: top-3 of 9 SPDR sectors by 6-mo TR, EW;
               a sector must beat SHY's 6-mo TR else its slot goes to IEF.
    XSMOM      12-1 cross-sectional momentum, top-10 of the mega-cap list, EW.
               [SURVIVORSHIP-SELECTED universe — flagged, screen-only]
    AAA        Adaptive asset allocation lite: top-3 of {SPY,EFA,EEM,TLT,GLD,
               VNQ,IEF} by 6-mo TR, inverse-63d-vol weighted.
    LOWVOL     Low-volatility tilt: bottom-20 mega-caps by 252d vol, EW.
               [SURVIVORSHIP-SELECTED universe — flagged, screen-only]
    RPLITE     Risk-parity lite: SPY/TLT/GLD, inverse-63d-vol weights.

Benchmarks: SPY buy-and-hold (TR); 60/40 SPY/IEF monthly.

Conventions (pre-registered):
    - Signals on adjusted (total-return) closes at month-end close T.
    - Execution at the close of T+1 (next trading day) — new weights earn
      returns from T+2 onward. No same-bar execution.
    - Costs: one-way turnover x 5 bps (ETF spread+impact cushion; zero
      commission; SEC/TAF immaterial at this scale). 10 bps sensitivity
      reported. Turnover = 0.5 * sum|w_new - w_old| per rebalance (x2 one-way).
    - Sharpe on daily returns in EXCESS of SHY (cash proxy), annualized 252.
    - Screen window: 2006-06-30 → lake frontier (12-mo lookbacks + margin).

PRE-REGISTERED GATE (all must hold to be shortlist-eligible):
    G1  net excess Sharpe >= 0.80
    G2  >= 70% of calendar years (with >= 10 months of data) net-positive
    G3  net max drawdown <= SPY buy-hold max drawdown (same window)
    G4  net excess Sharpe > SPY's net excess Sharpe (same window)
    G5  robustness: primary lookback x0.75 and x1.25 both keep
        net excess Sharpe >= 0.60

Usage:
    python scripts/backtest/run_us_rotation_study.py              # real screen
    python scripts/backtest/run_us_rotation_study.py --self-test  # offline
    python scripts/backtest/run_us_rotation_study.py --cost-bps 10

Backtest-only, advisory-only. Recommends; a human promotes. No live behavior.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_LAKE = _REPO / "backtest-data" / "lake"
_OUT = _REPO / "reports" / "us_screen"

SNAPSHOT_ID = "ds-b9b110ac58d57cae"  # Phase 1 pinned snapshot (provenance)

ETFS = ["SPY", "QQQ", "DIA", "IWM", "XLB", "XLE", "XLF", "XLI", "XLK", "XLP",
        "XLU", "XLV", "XLY", "TLT", "IEF", "SHY", "LQD", "HYG", "GLD", "SLV",
        "EFA", "EEM", "VNQ"]
SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]

SCREEN_START = date(2006, 6, 30)
COST_BPS_ONE_WAY = 5.0
TRADING_DAYS = 252

# Pre-registered gate thresholds
G1_SHARPE = 0.80
G2_POS_YEARS = 0.70
G5_SHARPE_PERTURBED = 0.60

# Pre-registered primary lookbacks (months unless noted) and ±25% variants
PARAMS = {
    "GEM":    {"base": 12, "perturbed": [9, 15]},
    "GTAA5":  {"base": 10, "perturbed": [8, 13]},
    "SECTOR": {"base": 6,  "perturbed": [5, 8]},
    "XSMOM":  {"base": 12, "perturbed": [9, 15]},   # 12-1 → (L)-1
    "AAA":    {"base": 6,  "perturbed": [5, 8]},
    "LOWVOL": {"base": 252, "perturbed": [189, 315]},  # days
    "RPLITE": {"base": 63,  "perturbed": [47, 79]},    # days
}

# Published anchors for the TR sanity check (slickcharts.com, fetched 2026-07-10)
SPY_TR_ANCHORS = {2023: 26.18, 2024: 24.89, 2025: 17.72}
SPY_ANCHOR_TOL_PP = 0.5  # percentage points


# ── data loading ──────────────────────────────────────────────────────────────

def load_adj_close(lake_root: Path, symbols: list[str] | None = None) -> pd.DataFrame:
    """Wide adj_close matrix (NY-date index) from the promoted US lake."""
    base = lake_root / "ohlcv" / "market=US" / "segment=EQ"
    files = sorted(base.glob("symbol=*/interval=1d/year=*/part-*.parquet"))
    if symbols is not None:
        keep = {f"symbol={s}" for s in symbols}
        files = [f for f in files if f.parts[-4] in keep]
    if not files:
        raise FileNotFoundError(f"no US lake files under {base}")
    import pyarrow.dataset as ds
    table = ds.dataset([str(f) for f in files], format="parquet").to_table(
        columns=["timestamp", "symbol", "adj_close"])
    df = table.to_pandas()
    df["d"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("America/New_York").dt.date
    wide = df.pivot_table(index="d", columns="symbol", values="adj_close")
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()


def month_ends(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last trading day of each month present in the index."""
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.groupby([index.year, index.month]).max().values)


# ── strategy target-weight functions ─────────────────────────────────────────
# Each returns {symbol: weight} at one month-end, given the TR price history
# up to and including that date. Pure functions — no forward data visible.

def _mom(px: pd.DataFrame, t: pd.Timestamp, months: int, skip_last: int = 0) -> pd.Series:
    hist = px.loc[:t]
    me = month_ends(hist.index)
    if len(me) < months + 1 + skip_last:
        return pd.Series(dtype=float)
    p_end = hist.loc[me[-1 - skip_last]]
    p_start = hist.loc[me[-1 - months - skip_last]]
    return (p_end / p_start - 1).dropna()


def w_gem(px, t, L):
    m = _mom(px, t, L)
    if not {"SPY", "EFA", "SHY", "IEF"} <= set(m.index):
        return {}
    risk = "SPY" if m["SPY"] >= m["EFA"] else "EFA"
    return {risk: 1.0} if m[risk] > m["SHY"] else {"IEF": 1.0}


def w_gtaa5(px, t, L):
    assets = ["SPY", "EFA", "IEF", "GLD", "VNQ"]
    hist = px.loc[:t]
    me = month_ends(hist.index)
    if len(me) < L + 1:
        return {}
    monthly = hist.loc[me]
    sma = monthly[assets].rolling(L).mean().iloc[-1]
    last = monthly[assets].iloc[-1]
    w: dict[str, float] = {}
    for a in assets:
        if pd.notna(last[a]) and pd.notna(sma[a]) and last[a] > sma[a]:
            w[a] = w.get(a, 0) + 0.2
        else:
            w["SHY"] = w.get("SHY", 0) + 0.2
    return w


def w_sector(px, t, L):
    m = _mom(px, t, L)
    avail = [s for s in SECTORS if s in m.index]
    if len(avail) < 9 or "SHY" not in m.index or "IEF" not in m.index:
        return {}
    top = m[avail].nlargest(3)
    w: dict[str, float] = {}
    for s, mom in top.items():
        if mom > m["SHY"]:
            w[s] = w.get(s, 0) + 1 / 3
        else:
            w["IEF"] = w.get("IEF", 0) + 1 / 3
    return w


def w_xsmom(px, t, L, megacaps):
    m = _mom(px, t, L - 1, skip_last=1)  # (L)-1 momentum with 1-month skip
    m = m[[s for s in m.index if s in megacaps]]
    if len(m) < 30:
        return {}
    return {s: 1 / 10 for s in m.nlargest(10).index}


def w_aaa(px, t, L):
    canon = ["SPY", "EFA", "EEM", "TLT", "GLD", "VNQ", "IEF"]
    m = _mom(px, t, L)
    m = m[[s for s in canon if s in m.index]]
    if len(m) < len(canon):
        return {}
    top = m.nlargest(3).index
    vol = px[list(top)].loc[:t].pct_change().iloc[-63:].std()
    iv = 1 / vol
    iv = iv / iv.sum()
    return {s: float(iv[s]) for s in top}


def w_lowvol(px, t, L, megacaps):
    hist = px.loc[:t]
    rets = hist[[s for s in megacaps if s in hist.columns]].pct_change().iloc[-L:]
    vol = rets.std().dropna()
    vol = vol[rets.notna().sum() >= int(L * 0.9)]
    if len(vol) < 30:
        return {}
    return {s: 1 / 20 for s in vol.nsmallest(20).index}


def w_rplite(px, t, L):
    assets = ["SPY", "TLT", "GLD"]
    vol = px[assets].loc[:t].pct_change().iloc[-L:].std()
    if vol.isna().any():
        return {}
    iv = 1 / vol
    iv = iv / iv.sum()
    return {s: float(iv[s]) for s in assets}


# ── backtest engine (weights → daily net returns) ────────────────────────────

def run_weights(px: pd.DataFrame, targets: dict[pd.Timestamp, dict[str, float]],
                start: pd.Timestamp, cost_bps: float) -> pd.Series:
    """Daily net portfolio returns. Target decided at month-end T is executed
    at the close of T+1; new weights earn returns from T+2 onward. Between
    rebalances weights DRIFT with returns (real share holdings — the LEAN
    cross-check caught the earlier constant-mix version silently re-balancing
    daily for free). Cost = one-way turnover x cost_bps at execution."""
    rets = px.pct_change()
    days = px.index[px.index >= start]
    out = pd.Series(0.0, index=days)
    current: dict[str, float] = {}
    pending: tuple[int, dict[str, float]] | None = None  # (exec_pos, target)
    positions = {d: i for i, d in enumerate(px.index)}

    tkeys = sorted(targets.keys())
    ti = 0
    for d in days:
        pos = positions[d]
        # apply a pending switch the day AFTER its execution close
        if pending and pos > pending[0]:
            current = pending[1]
            pending = None
        # earn today's return on current weights
        day_rets = {s: (rets.at[d, s] if pd.notna(rets.at[d, s]) else 0.0)
                    for s in current}
        r = sum(w * day_rets[s] for s, w in current.items())
        # drift weights with today's returns (implicit cash sleeve earns 0)
        if current and r > -1:
            current = {s: w * (1 + day_rets[s]) / (1 + r) for s, w in current.items()}
        # charge costs at execution close
        while ti < len(tkeys) and tkeys[ti] <= d:
            t = tkeys[ti]
            if t == d:  # signal day: execution at next trading day's close
                tgt = targets[t]
                turnover = 0.5 * sum(abs(tgt.get(s, 0) - current.get(s, 0))
                                     for s in set(tgt) | set(current))
                r -= 2 * turnover * cost_bps / 1e4  # one-way each side ≈ 2x half-turnover
                pending = (pos + 1, tgt)
            ti += 1
        out[d] = r
    return out


def build_targets(px, strat, L, megacaps=None):
    fns = {"GEM": w_gem, "GTAA5": w_gtaa5, "SECTOR": w_sector, "AAA": w_aaa,
           "RPLITE": w_rplite}
    tgts = {}
    for t in month_ends(px.index):
        if strat in ("XSMOM", "LOWVOL"):
            w = (w_xsmom if strat == "XSMOM" else w_lowvol)(px, t, L, megacaps)
        else:
            w = fns[strat](px, t, L)
        if w:
            assert sum(w.values()) < 1.0001, (strat, t, sum(w.values()))
            tgts[t] = w
    return tgts


# ── metrics + gates ───────────────────────────────────────────────────────────

def metrics(net: pd.Series, cash: pd.Series, label: str) -> dict:
    net = net.dropna()
    if not len(net):
        return {"label": label, "error": "no returns"}
    nav = (1 + net).cumprod()
    yrs = len(net) / TRADING_DAYS
    cagr = nav.iloc[-1] ** (1 / yrs) - 1
    excess = net - cash.reindex(net.index).fillna(0.0)
    sharpe = excess.mean() / excess.std() * np.sqrt(TRADING_DAYS) if excess.std() > 0 else 0.0
    dd = (nav / nav.cummax() - 1).min()
    yr_ret = net.groupby(net.index.year).apply(lambda r: (1 + r).prod() - 1)
    yr_n = net.groupby(net.index.year).size()
    full = yr_ret[yr_n >= 210]  # ~10 months
    return {
        "label": label,
        "cagr_net": round(float(cagr), 4),
        "vol_ann": round(float(net.std() * np.sqrt(TRADING_DAYS)), 4),
        "sharpe_net_excess": round(float(sharpe), 3),
        "max_dd": round(float(dd), 4),
        "pos_years": f"{int((full > 0).sum())}/{len(full)}",
        "pos_years_frac": round(float((full > 0).mean()), 3) if len(full) else None,
        "worst_year": round(float(yr_ret.min()), 4),
        "final_nav_multiple": round(float(nav.iloc[-1]), 2),
    }


def evaluate_gates(m: dict, m_spy: dict, m_p1: dict, m_p2: dict) -> dict:
    g = {
        "G1_sharpe>=0.80": m["sharpe_net_excess"] >= G1_SHARPE,
        "G2_pos_years>=70%": (m["pos_years_frac"] or 0) >= G2_POS_YEARS,
        "G3_dd<=spy": m["max_dd"] >= m_spy["max_dd"],  # dd is negative
        "G4_sharpe>spy": m["sharpe_net_excess"] > m_spy["sharpe_net_excess"],
        "G5_robust>=0.60": (m_p1["sharpe_net_excess"] >= G5_SHARPE_PERTURBED
                            and m_p2["sharpe_net_excess"] >= G5_SHARPE_PERTURBED),
    }
    g["verdict"] = "PASS" if all(g.values()) else f"FAIL ({sum(g.values())}/5)"
    return g


# ── main screen ───────────────────────────────────────────────────────────────

def run_screen(lake_root: Path, cost_bps: float, out_dir: Path,
               px: pd.DataFrame | None = None, start: date = SCREEN_START,
               megacaps: list[str] | None = None, do_anchors: bool = True) -> dict:
    if px is None:
        px = load_adj_close(lake_root)
    if megacaps is None:
        uni = json.loads((lake_root.parent / "reference" / "us_universe.json").read_text())
        megacaps = [s for s, v in uni["symbols"].items() if v["type"] == "stock"]

    start_ts = pd.Timestamp(start)
    cash = px["SHY"].pct_change()

    # TR sanity anchors (published SPY total returns)
    anchors = {}
    if do_anchors:
        spy_yr = px["SPY"].pct_change().groupby(px.index.year).apply(
            lambda r: ((1 + r.dropna()).prod() - 1) * 100)
        for yr, pub in SPY_TR_ANCHORS.items():
            got = float(spy_yr.get(yr, np.nan))
            anchors[yr] = {"lake": round(got, 2), "published": pub,
                           "ok": bool(abs(got - pub) <= SPY_ANCHOR_TOL_PP)}
        print("  TR sanity anchors (SPY, lake vs slickcharts):",
              {y: (a['lake'], a['published'], '✓' if a['ok'] else '✗')
               for y, a in anchors.items()})
        if not all(a["ok"] for a in anchors.values()):
            raise RuntimeError(f"TR sanity anchors FAILED: {anchors} — do not screen "
                               "on unverified total-return data")

    # Benchmarks
    spy_net = px["SPY"].pct_change().loc[px.index >= start_ts]
    m_spy = metrics(spy_net, cash, "SPY buy-hold")
    b6040 = run_weights(px, {t: {"SPY": 0.6, "IEF": 0.4} for t in month_ends(px.index)},
                        start_ts, cost_bps)
    m_6040 = metrics(b6040, cash, "60/40 SPY/IEF")

    results = {"benchmarks": {"SPY": m_spy, "B6040": m_6040}, "candidates": {},
               "anchors": anchors,
               "config": {"cost_bps_one_way": cost_bps, "start": str(start),
                          "end": str(px.index.max().date()),
                          "snapshot_id": SNAPSHOT_ID, "gates": {
                              "G1": G1_SHARPE, "G2": G2_POS_YEARS,
                              "G5": G5_SHARPE_PERTURBED},
                          "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}}

    for strat, p in PARAMS.items():
        runs = {}
        for tag, L in [("base", p["base"]), ("p1", p["perturbed"][0]),
                       ("p2", p["perturbed"][1])]:
            tgts = build_targets(px, strat, L, megacaps)
            net = run_weights(px, tgts, start_ts, cost_bps)
            runs[tag] = metrics(net, cash, f"{strat}(L={L})")
        gates = evaluate_gates(runs["base"], m_spy, runs["p1"], runs["p2"])
        results["candidates"][strat] = {"base": runs["base"], "perturbed":
                                        [runs["p1"], runs["p2"]], "gates": gates}
        print(f"  {strat:7} {gates['verdict']:12} sharpe={runs['base']['sharpe_net_excess']:6.2f} "
              f"cagr={runs['base']['cagr_net']*100:6.2f}% dd={runs['base']['max_dd']*100:6.1f}% "
              f"posyr={runs['base']['pos_years']} (SPY sharpe={m_spy['sharpe_net_excess']:.2f} "
              f"dd={m_spy['max_dd']*100:.1f}%)")

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"screen_{datetime.now().strftime('%Y%m%d_%H%M%S')}_cost{int(cost_bps)}bps.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"  → {out}")
    return results


# ── self-test ─────────────────────────────────────────────────────────────────

def self_test() -> int:
    print("Self-test (synthetic, offline)...")
    rng = np.random.default_rng(7)
    days = pd.bdate_range("2005-01-03", "2015-12-31")
    n = len(days)

    def walk(mu, sig, s0=100.0):
        return s0 * np.exp(np.cumsum(rng.normal(mu / TRADING_DAYS,
                                                sig / np.sqrt(TRADING_DAYS), n)))
    cols = {"SPY": walk(0.30, 0.15), "EFA": walk(-0.10, 0.18), "SHY": walk(0.01, 0.005),
            "IEF": walk(0.03, 0.06), "GLD": walk(0.05, 0.16), "VNQ": walk(0.04, 0.2),
            "TLT": walk(0.03, 0.12), "EEM": walk(0.02, 0.22)}
    for s in SECTORS + ["QQQ", "DIA", "IWM", "LQD", "HYG", "SLV"]:
        cols[s] = walk(0.06, 0.18)
    px = pd.DataFrame(cols, index=days)

    # 1) GEM must overwhelmingly pick SPY (strong uptrend vs downtrend EFA)
    tgts = build_targets(px, "GEM", 12)
    picks = [list(w)[0] for w in tgts.values()]
    assert picks.count("SPY") / len(picks) > 0.7, picks.count("SPY") / len(picks)

    # 2) T+1 execution boundary. Signal at d1 → trade at d2's close → new
    #    weights earn returns from d3 onward.
    idx = pd.bdate_range("2024-01-01", periods=6)
    # (a) jump INTO the execution close (d2): must NOT be earned (no lookahead)
    two = pd.DataFrame({"A": [100, 100, 200, 200, 200, 200], "B": [100.0] * 6},
                       index=idx)
    r = run_weights(two, {idx[1]: {"A": 1.0}}, idx[0], cost_bps=0.0)
    assert abs(r[idx[2]]) < 1e-12, "jump into execution close must not be earned"
    assert abs(r[idx[3]]) < 1e-12
    # (b) positive control — jump the day AFTER execution (d3): must be earned
    two_b = pd.DataFrame({"A": [100, 100, 100, 200, 200, 200], "B": [100.0] * 6},
                         index=idx)
    r_b = run_weights(two_b, {idx[1]: {"A": 1.0}}, idx[0], cost_bps=0.0)
    assert abs(r_b[idx[3]] - 1.0) < 1e-12, "post-execution move must be earned"

    # 3) costs reduce returns monotonically
    tgts_rp = build_targets(px, "RPLITE", 63)
    r0 = run_weights(px, tgts_rp, pd.Timestamp("2006-06-30"), 0.0)
    r10 = run_weights(px, tgts_rp, pd.Timestamp("2006-06-30"), 10.0)
    assert (1 + r0).prod() > (1 + r10).prod()

    # 4) weights bounded, metrics sane
    m = metrics(r0, px["SHY"].pct_change(), "rp")
    assert -1 < m["max_dd"] <= 0 and m["vol_ann"] > 0

    # 5) gate logic: fabricated PASS/FAIL
    good = {"sharpe_net_excess": 1.0, "pos_years_frac": 0.9, "max_dd": -0.2}
    spy = {"sharpe_net_excess": 0.5, "max_dd": -0.5}
    g = evaluate_gates({**good}, spy, {"sharpe_net_excess": 0.7},
                       {"sharpe_net_excess": 0.9})
    assert g["verdict"] == "PASS", g
    g2 = evaluate_gates({**good, "sharpe_net_excess": 0.5}, spy,
                        {"sharpe_net_excess": 0.7}, {"sharpe_net_excess": 0.9})
    assert g2["verdict"].startswith("FAIL"), g2

    print("Self-test PASS ✅ (signal selection, T+1 no-lookahead, cost "
          "monotonicity, metrics, gate logic)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="US positional candidate screen (ADR-041 P2)")
    p.add_argument("--lake", default=str(_LAKE))
    p.add_argument("--cost-bps", type=float, default=COST_BPS_ONE_WAY)
    p.add_argument("--out", default=str(_OUT))
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        return self_test()
    print(f"── US candidate screen (cost {args.cost_bps}bps one-way, "
          f"window {SCREEN_START}→frontier, snapshot {SNAPSHOT_ID}) ──")
    run_screen(Path(args.lake), args.cost_bps, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
