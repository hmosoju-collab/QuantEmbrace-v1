#!/usr/bin/env python3
"""Run walk-forward validation for the QuantEmbrace backtesting lab.

Generates IS→OOS folds, optimises parameters on each train window, validates OOS,
and reports fold metrics, a stability score, an overfit warning, an advisory
strategy-eligibility recommendation, and a parameter-robustness report.

Backtest-only: no broker APIs, no live trading; advisory only.

In production, the ``evaluate`` callback runs the replay engine + strategy adapter
+ TEE + metrics engine over each window's lake data. ``--self-test`` uses a
deterministic synthetic ``evaluate`` so the harness runs end-to-end without data.

Usage:
    python scripts/backtest/run_walk_forward_aws.py --self-test --preset default
    python scripts/backtest/run_walk_forward_aws.py --self-test --preset long --strategy momentum
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.walk_forward import (  # noqa: E402
    PRESETS,
    fold_report,
    run_walk_forward,
)


def _synthetic_evaluate(strategy: str | None):
    """Deterministic stand-in for a real backtest over a window.

    Favours one parameter on train and applies mild IS→OOS degradation, so the
    harness produces a realistic (eligible-ish) result. Replace with a real
    evaluate that runs replay + adapter + TEE + metrics over the window's lake.
    """
    def evaluate(params: dict, fold, phase: str) -> dict:
        edge = 4.0 if params.get("atr_stop_multiplier") == 1.5 else 2.5
        factor = 1.0 if phase == "train" else 0.75  # OOS degradation
        # Small deterministic per-fold wobble (keeps it realistic, still stable).
        wobble = 1.0 + (fold.fold_id % 3) * 0.02
        expectancy = edge * factor * wobble
        return {
            "expectancy": expectancy,
            "profit_factor": 1.3 + 0.1 * factor,
            "net_pnl": 1000.0 * expectancy,
            "number_of_trades": 40,
        }

    return evaluate


def main() -> int:
    p = argparse.ArgumentParser(description="Walk-forward validation (backtest-only)")
    p.add_argument("--preset", default="default", choices=list(PRESETS))
    p.add_argument("--start", default="2010-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--strategy", default=None)
    p.add_argument("--objective", default="expectancy")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if not args.self_test:
        print("No real evaluate wired; running --self-test (synthetic).")

    grid = [{"atr_stop_multiplier": 1.5}, {"atr_stop_multiplier": 2.0}]
    result = run_walk_forward(
        start=args.start, end=args.end, spec=PRESETS[args.preset],
        param_grid=grid, evaluate=_synthetic_evaluate(args.strategy), objective=args.objective,
    )

    print(f"\n=== Walk-forward ({args.preset}: train {PRESETS[args.preset].train_months}m / "
          f"validate {PRESETS[args.preset].validate_months}m / roll {PRESETS[args.preset].roll_months}m) ===")
    print(f"  folds: {len(result.folds)} | objective: {result.objective}")
    for row in fold_report(result):
        print(f"  fold {row['fold_id']}: train {row['train_start']}→{row['train_end']} "
              f"val {row['validate_start']}→{row['validate_end']} "
              f"params={row['best_params']} IS={row['is_objective']} OOS={row['oos_objective']}")
    a = result.aggregate
    print("\n  aggregate:")
    print(f"    mean OOS expectancy : {a['mean_oos_expectancy']:.3f}")
    print(f"    mean OOS profit fac : {a['mean_oos_profit_factor']:.3f}")
    print(f"    total OOS net P&L   : {a['total_oos_net_pnl']:,.2f}")
    print(f"    IS→OOS degradation  : {a['is_oos_degradation']:.3f}")
    print(f"    win consistency     : {a['win_consistency']:.2f}")
    print(f"    stability score     : {result.stability_score:.2f}  (unstable={result.unstable})")
    print(f"    overfit warning     : {result.overfit_warning}")
    print(f"    OOS gates pass      : {a['gates']['overall_pass']}")
    print(f"    ELIGIBILITY         : {result.eligibility}  (advisory)")
    print(f"    parameter robustness: {json.dumps(result.parameter_robustness)}")
    for note in result.notes:
        print(f"    note: {note}")
    print("  (advisory only — walk-forward never promotes a strategy)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
