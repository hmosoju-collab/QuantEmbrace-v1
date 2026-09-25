#!/usr/bin/env python3
"""
QuantEmbrace — LEAN engine cross-check of RPLITE (ADR-041 Phase 2b)

Runs scripts/backtest/lean/main.py (RpliteCrossCheck) inside the LEAN engine
Docker image on the SAME total-return data as the pandas screen (exported by
export_lake_to_lean.py), then compares the two engines' monthly returns.

No QuantConnect credentials needed — this drives the LEAN engine directly
(config mounted over the image default; our custom data mounted into the
image's data folder).

PRE-DECLARED PARITY TOLERANCES (declared before the first run; engines differ
in fill-timing microstructure, so exactness is not expected):
    T1  monthly-return RMSE            <= 0.20% (20bps)
    T2  |final NAV multiple ratio - 1| <= 3%
    T3  monthly-return sign agreement  >= 95%
Comparison window: 2006-08-01 → 2026-07-09 (skips the warmup seam month).
Both engines at ZERO cost — this is an engine-mechanics parity check; the
cost model is exercised in the pandas harness (5/10bps runs) separately.

Usage:
    python scripts/backtest/run_lean_crosscheck.py            # run + compare
    python scripts/backtest/run_lean_crosscheck.py --compare-only
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_WS = _REPO / "backtest-data" / "lean-workspace"
_ALGO_DIR = _REPO / "scripts" / "backtest" / "lean"
_IMAGE = "quantconnect/lean:latest"

CMP_START = pd.Timestamp("2006-08-01")
T1_RMSE = 0.002
T2_NAV = 0.03
T3_SIGN = 0.95


def run_lean() -> Path:
    """Run the LEAN engine in Docker; return the results directory."""
    results = _WS / "results"
    results.mkdir(parents=True, exist_ok=True)

    raw = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "cat", _IMAGE,
         "/Lean/Launcher/bin/Debug/config.json"],
        capture_output=True, text=True, check=True).stdout
    # LEAN's config.json carries JS-style comments; strip them (whitespace
    # before "//" keeps string URLs like "https://" intact).
    import re
    raw = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
    raw = re.sub(r"\s+//.*$", "", raw, flags=re.M)  # ':' before '//' in URLs ≠ \s
    default = json.loads(raw)
    default.update({
        "environment": "backtesting",
        "algorithm-type-name": "RpliteCrossCheck",
        "algorithm-language": "Python",
        "algorithm-location": "/Algo/main.py",
        "results-destination-folder": "/Results",
        "debugging": False,
    })
    cfg = _WS / "lean-config.json"
    cfg.write_text(json.dumps(default, indent=2))

    cmd = ["docker", "run", "--rm",
           "-v", f"{_ALGO_DIR}:/Algo:ro",
           "-v", f"{_WS / 'data' / 'us_eod_tr'}:/Lean/Data/us_eod_tr:ro",
           "-v", f"{results}:/Results",
           "-v", f"{cfg}:/Lean/Launcher/bin/Debug/config.json:ro",
           _IMAGE]
    print("  Running LEAN engine (docker)...")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-12:])
    print(tail)
    if proc.returncode != 0:
        raise RuntimeError(f"LEAN exited {proc.returncode}")
    return results


def lean_equity_series(results: Path) -> pd.Series:
    """Extract the strategy equity curve from LEAN's result JSON."""
    candidates = sorted(results.glob("*.json"),
                        key=lambda f: f.stat().st_size, reverse=True)
    for f in candidates:
        try:
            j = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        charts = j.get("charts") or j.get("Charts") or {}
        eq = (charts.get("Strategy Equity") or {}).get("series") \
            or (charts.get("Strategy Equity") or {}).get("Series") or {}
        ser = eq.get("Equity") or {}
        values = ser.get("values") or ser.get("Values")
        if not values:
            continue
        ts, vals = [], []
        for v in values:
            if isinstance(v, dict):        # {"x": ts, "y": val}
                t, y = v.get("x"), v.get("y")
            elif isinstance(v, list) and len(v) >= 2:
                t, y = v[0], v[-1]         # [ts, o, h, l, c] candles or [ts, y]
            else:
                continue
            if t is None or y is None:
                continue
            # LEAN chart timestamps are UTC epoch; convert to NY so month-end
            # points don't spill into the next month when resampling.
            ny = (pd.to_datetime(int(t), unit="s", utc=True)
                  .tz_convert("America/New_York").tz_localize(None))
            ts.append(ny)
            vals.append(float(y))
        if vals:
            return pd.Series(vals, index=pd.DatetimeIndex(ts)).sort_index()
    raise RuntimeError(f"no Strategy Equity series found under {results}")


def pandas_leg() -> pd.Series:
    """Zero-cost RPLITE daily NAV from the screen harness (same code path)."""
    sys.path.insert(0, str(_REPO / "scripts" / "backtest"))
    from run_us_rotation_study import build_targets, load_adj_close, run_weights
    px = load_adj_close(_REPO / "backtest-data" / "lake",
                        symbols=["SPY", "TLT", "GLD", "SHY"])
    tgts = build_targets(px, "RPLITE", 63)
    net = run_weights(px, tgts, pd.Timestamp(date(2006, 6, 30)), cost_bps=0.0)
    return (1 + net).cumprod()


def compare(lean_nav: pd.Series, pd_nav: pd.Series) -> dict:
    # LEAN's chart emits a midnight sample every trading day (value BEFORE
    # that day's own 17:00 custom bar, i.e. reflecting prices through D-1)
    # but only an intermittent same-day 17:00 sample (~15% of days, a
    # charting-decimation artifact, not a trading difference). The prior
    # resample("D").last() let that sporadic 17:00 point silently override
    # the reliable midnight one whenever it happened to exist, corrupting
    # the day mapping on those dates. Use ONLY midnight samples, which are
    # never missing: pandas day d's value is LEAN's midnight sample on the
    # next trading day.
    midnight = lean_nav[lean_nav.index.time == pd.Timestamp("00:00:00").time()]
    midnight = midnight.groupby(midnight.index.normalize()).last()
    td = pd_nav.index
    aligned = {}
    for i, d in enumerate(td[:-1]):
        nxt = td[i + 1]
        if nxt in midnight.index:
            aligned[d] = float(midnight.loc[nxt])
    lean_aligned = pd.Series(aligned).sort_index()
    lm = lean_aligned.resample("ME").last().pct_change().dropna()
    pm = pd_nav.resample("ME").last().pct_change().dropna()
    joined = pd.DataFrame({"lean": lm, "pandas": pm}).dropna()
    joined = joined[joined.index >= CMP_START]
    diff = joined["lean"] - joined["pandas"]
    rmse = float(np.sqrt((diff ** 2).mean()))
    sign = float((np.sign(joined["lean"]) == np.sign(joined["pandas"])).mean())
    mult_lean = float((1 + joined["lean"]).prod())
    mult_pd = float((1 + joined["pandas"]).prod())
    nav_ratio = abs(mult_lean / mult_pd - 1)
    out = {
        "months_compared": int(len(joined)),
        "monthly_rmse": round(rmse, 6),
        "sign_agreement": round(sign, 4),
        "nav_multiple_lean": round(mult_lean, 3),
        "nav_multiple_pandas": round(mult_pd, 3),
        "nav_ratio_diff": round(nav_ratio, 4),
        "corr": round(float(joined["lean"].corr(joined["pandas"])), 4),
        "worst_months": diff.abs().nlargest(3).index.strftime("%Y-%m").tolist(),
        "gates": {
            "T1_rmse<=20bps": rmse <= T1_RMSE,
            "T2_nav_ratio<=3%": nav_ratio <= T2_NAV,
            "T3_sign>=95%": sign >= T3_SIGN,
        },
    }
    out["verdict"] = "PARITY PASS" if all(out["gates"].values()) else "PARITY FAIL"
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--compare-only", action="store_true")
    args = p.parse_args()
    results = _WS / "results"
    if not args.compare_only:
        results = run_lean()
    lean_nav = lean_equity_series(results)
    pd_nav = pandas_leg()
    r = compare(lean_nav, pd_nav)
    print(json.dumps(r, indent=2))
    out = _REPO / "reports" / "us_screen" / "lean_crosscheck.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(r, indent=2))
    print(f"  → {out}")
    return 0 if r["verdict"] == "PARITY PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
