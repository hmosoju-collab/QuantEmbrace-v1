#!/usr/bin/env python3
"""Compare old global TEE vs new strategy-aware R-based TEE for the backtesting lab.

Runs the SAME set of trades through both exit policies (reusing the AWS-BT-6
execution simulator for fills and the AWS-BT-7 MIS simulator for 15:05 cleanup),
then aggregates the metrics and reports the **old vs new delta**:

    realized_r · profit_capture_ratio · giveback_ratio · MIS dependency · exit-reason mix

Backtest-only: no broker APIs, no live trading. Use ``--self-test`` to run on
synthetic trades; otherwise wire your own trade specs + bars.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.replay_engine import Candle  # noqa: E402
from backtesting.tee_simulator import compare_policies  # noqa: E402
from shared.models.signal import Direction  # noqa: E402

IST = "Asia/Kolkata"


def _c(t: str, o: float, h: float, low: float, c: float) -> Candle:
    return Candle("R", "NSE", "EQ", "1m", pd.Timestamp(t, tz=IST), o, h, low, c, 1000)


def _synthetic_trades() -> list[dict]:
    """Three archetypal intraday trades: runner, fade-back, and ride-to-EOD."""
    e = pd.Timestamp("2020-06-01 10:00", tz=IST)
    base = dict(symbol="R", direction=Direction.BUY, entry_price=100.0, stop=95.0, quantity=10,
                entry_time=e)

    # 1) Strong runner: rises to ~3R then closes high.
    runner = [_c("2020-06-01 10:%02d" % m, 100 + m, 100 + m + 1, 99 + m, 100 + m) for m in range(1, 16)]
    # 2) Fade-back: spikes to ~2.5R then gives most of it back.
    fade = (
        [_c("2020-06-01 10:%02d" % m, 100 + m, 101 + m, 99 + m, 100 + m) for m in range(1, 13)]
        + [_c("2020-06-01 10:%02d" % m, 112 - (m - 12) * 2, 112 - (m - 12) * 2, 100, 111 - (m - 12) * 2) for m in range(13, 20)]
    )
    # 3) Ride to EOD: flat near entry until the 15:05 square-off.
    ride = [_c(t, 100, 100.3, 99.7, 100) for t in ("2020-06-01 14:50", "2020-06-01 14:58", "2020-06-01 15:05")]
    ride_spec = {**base, "entry_time": pd.Timestamp("2020-06-01 14:50", tz=IST)}

    return [(dict(base), runner), (dict(base), fade), (ride_spec, ride)]


def _aggregate(outcomes: list) -> dict:
    n = len(outcomes) or 1
    return {
        "trades": len(outcomes),
        "avg_realized_r": sum(o.realized_r for o in outcomes) / n,
        "avg_capture": sum(o.profit_capture_ratio for o in outcomes) / n,
        "avg_giveback": sum(o.giveback_ratio for o in outcomes) / n,
        "mis_dependency": sum(1 for o in outcomes if o.mis_dependent) / n,
        "reasons": _reason_mix(outcomes),
    }


def _reason_mix(outcomes: list) -> dict:
    mix: dict[str, int] = {}
    for o in outcomes:
        mix[o.final_reason] = mix.get(o.final_reason, 0) + 1
    return mix


def run_comparison(trades: list[dict], *, strategy: str | None = None) -> dict:
    old_outs, new_outs = [], []
    for spec, bars in trades:
        res = compare_policies(spec, bars, strategy=strategy)
        old_outs.append(res["old"])
        new_outs.append(res["new"])
    old, new = _aggregate(old_outs), _aggregate(new_outs)
    delta = {
        "avg_realized_r": new["avg_realized_r"] - old["avg_realized_r"],
        "avg_capture": new["avg_capture"] - old["avg_capture"],
        "avg_giveback": new["avg_giveback"] - old["avg_giveback"],
        "mis_dependency": new["mis_dependency"] - old["mis_dependency"],
    }
    return {"old": old, "new": new, "delta": delta}


def _print(report: dict) -> None:
    old, new, delta = report["old"], report["new"], report["delta"]
    print("\n=== TEE policy comparison (old global vs new strategy-aware) ===")
    print(f"  trades: {old['trades']}")
    rows = [
        ("avg_realized_r", old["avg_realized_r"], new["avg_realized_r"], delta["avg_realized_r"]),
        ("avg_capture", old["avg_capture"], new["avg_capture"], delta["avg_capture"]),
        ("avg_giveback", old["avg_giveback"], new["avg_giveback"], delta["avg_giveback"]),
        ("mis_dependency", old["mis_dependency"], new["mis_dependency"], delta["mis_dependency"]),
    ]
    print(f"  {'metric':<16}{'old':>10}{'new':>10}{'delta':>10}")
    for label, o, nw, d in rows:
        print(f"  {label:<16}{o:>10.3f}{nw:>10.3f}{d:>+10.3f}")
    print(f"  exit-reason mix  old={old['reasons']}  new={new['reasons']}")
    print("  (advisory only — comparison never promotes a policy or changes trading)\n")


def main() -> int:
    p = argparse.ArgumentParser(description="Compare old vs new TEE policies (backtest-only)")
    p.add_argument("--self-test", action="store_true", help="Run on synthetic trades")
    p.add_argument("--strategy", default=None, help="Strategy name for the new per-strategy policy")
    args = p.parse_args()

    if not args.self_test:
        print("No trade source provided; running --self-test (synthetic).")
    report = run_comparison(_synthetic_trades(), strategy=args.strategy)
    _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
