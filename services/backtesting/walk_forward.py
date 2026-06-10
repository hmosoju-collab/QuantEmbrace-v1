"""Walk-forward validation for the QuantEmbrace backtesting lab.

Generates in-sample (IS) → out-of-sample (OOS) folds, optimises parameters on the
**train** window only, validates on the immediately following **validate** window,
aggregates OOS metrics, and reports overfitting / parameter-stability indicators
and an advisory eligibility recommendation.

Decoupled by design: the caller supplies an ``evaluate(params, fold, phase)``
callback that runs a backtest over a window and returns a metrics dict (from
`metrics_engine`). This keeps the harness pure and unit-testable, and guarantees
**no future leakage** — parameters are chosen only from train-phase results, and
each fold's validate window is strictly after its train window.

Backtest-only: no broker APIs, advisory only — never promotes a strategy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from backtesting.metrics_engine import evaluate_gates

# A strategy is "unstable" if fewer than this fraction of folds agree on params.
STABILITY_OK_THRESHOLD = 0.70
# OOS objective below this fraction of IS objective ⇒ overfit.
DEGRADATION_OK_RATIO = 0.50
# Fraction of folds with positive OOS expectancy required for robustness.
WIN_CONSISTENCY_MIN = 0.50


@dataclass(frozen=True)
class WindowSpec:
    train_months: int = 12
    validate_months: int = 3
    roll_months: int = 3
    anchored: bool = False


PRESETS: dict[str, WindowSpec] = {
    "default": WindowSpec(12, 3, 3),
    "medium": WindowSpec(24, 6, 6),
    "long": WindowSpec(36, 12, 12),
}


@dataclass(frozen=True)
class Fold:
    fold_id: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp        # exclusive; == validate_start (half-open windows)
    validate_start: pd.Timestamp
    validate_end: pd.Timestamp     # exclusive


@dataclass
class FoldResult:
    fold: Fold
    best_params: dict
    is_objective: float
    oos_metrics: dict
    oos_objective: float
    run_id: str | None = None


@dataclass
class WalkForwardResult:
    folds: list[FoldResult]
    objective: str
    spec: WindowSpec
    aggregate: dict
    stability_score: float
    unstable: bool
    overfit_warning: bool
    eligibility: str
    parameter_robustness: dict
    notes: list[str] = field(default_factory=list)


# ── fold generation ─────────────────────────────────────────────────────────────


def generate_folds(start, end, spec: WindowSpec) -> list[Fold]:
    """Build IS→OOS folds. IS is always strictly before OOS; windows never overlap."""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    folds: list[Fold] = []
    i = 0
    if spec.anchored:
        val_start = s + pd.DateOffset(months=spec.train_months)
        while True:
            val_end = val_start + pd.DateOffset(months=spec.validate_months)
            if val_end > e:
                break
            folds.append(Fold(i, s, val_start, val_start, val_end))  # train grows from fixed start
            val_start = val_start + pd.DateOffset(months=spec.roll_months)
            i += 1
    else:
        anchor = s
        while True:
            train_start = anchor
            train_end = train_start + pd.DateOffset(months=spec.train_months)
            val_start = train_end
            val_end = val_start + pd.DateOffset(months=spec.validate_months)
            if val_end > e:
                break
            folds.append(Fold(i, train_start, train_end, val_start, val_end))
            anchor = anchor + pd.DateOffset(months=spec.roll_months)
            i += 1
    _assert_no_leakage(folds)
    return folds


def _assert_no_leakage(folds: list[Fold]) -> None:
    for f in folds:
        if not (f.train_start < f.train_end <= f.validate_start < f.validate_end):
            raise ValueError(
                f"Fold {f.fold_id} violates IS<OOS ordering: "
                f"train[{f.train_start},{f.train_end}) validate[{f.validate_start},{f.validate_end})"
            )


# ── orchestration ───────────────────────────────────────────────────────────────

EvaluateFn = Callable[[dict, Fold, str], dict]


def run_walk_forward(
    *,
    start,
    end,
    spec: WindowSpec,
    param_grid: list[dict],
    evaluate: EvaluateFn,
    objective: str = "expectancy",
    registry: Any = None,
    run_spec_factory: Callable[[Fold, dict], Any] | None = None,
) -> WalkForwardResult:
    """Run walk-forward validation. ``evaluate(params, fold, phase)`` returns metrics."""
    if not param_grid:
        raise ValueError("param_grid must be non-empty")
    folds = generate_folds(start, end, spec)
    results: list[FoldResult] = []

    for fold in folds:
        # --- optimise on TRAIN only ---
        best_params: dict | None = None
        best_obj = float("-inf")
        for params in param_grid:
            train_metrics = evaluate(params, fold, "train")
            obj = float(train_metrics.get(objective, 0.0))
            if obj > best_obj:
                best_obj = obj
                best_params = params
        assert best_params is not None

        # --- validate OOS with the train-selected params ---
        oos = evaluate(best_params, fold, "validate")
        oos_obj = float(oos.get(objective, 0.0))

        run_id = None
        if registry is not None and run_spec_factory is not None:
            rec = registry.create_run(run_spec_factory(fold, best_params))
            registry.mark_completed(rec.run_id)
            run_id = rec.run_id

        results.append(FoldResult(fold, dict(best_params), best_obj, oos, oos_obj, run_id))

    return _aggregate(results, objective, spec)


# ── aggregation / scoring ────────────────────────────────────────────────────────


def _aggregate(results: list[FoldResult], objective: str, spec: WindowSpec) -> WalkForwardResult:
    notes: list[str] = []
    if not results:
        return WalkForwardResult([], objective, spec, {}, 0.0, True, True, "INSUFFICIENT_DATA", {},
                                 ["No folds generated — widen the date range or shorten windows."])

    n = len(results)
    oos_expectancy = [float(r.oos_metrics.get("expectancy", 0.0)) for r in results]
    oos_pf = [float(r.oos_metrics.get("profit_factor", 0.0)) for r in results]
    oos_net = [float(r.oos_metrics.get("net_pnl", 0.0)) for r in results]

    mean_oos_obj = sum(r.oos_objective for r in results) / n
    mean_is_obj = sum(r.is_objective for r in results) / n
    degradation = (mean_oos_obj / mean_is_obj) if mean_is_obj > 0 else (1.0 if mean_oos_obj > 0 else 0.0)
    win_consistency = sum(1 for x in oos_expectancy if x > 0) / n

    aggregate = {
        "folds": n,
        "mean_oos_expectancy": sum(oos_expectancy) / n,
        "mean_oos_profit_factor": sum(oos_pf) / n,
        "total_oos_net_pnl": sum(oos_net),
        "mean_is_objective": mean_is_obj,
        "mean_oos_objective": mean_oos_obj,
        "is_oos_degradation": degradation,
        "win_consistency": win_consistency,
        "gates": evaluate_gates({
            "expectancy": sum(oos_expectancy) / n,
            "profit_factor": sum(oos_pf) / n,
            "net_pnl": sum(oos_net),
        }),
    }

    stability_score, robustness = _parameter_stability(results)
    unstable = stability_score < STABILITY_OK_THRESHOLD
    overfit_warning = (
        degradation < DEGRADATION_OK_RATIO
        or win_consistency < WIN_CONSISTENCY_MIN
        or aggregate["mean_oos_expectancy"] <= 0
    )

    if unstable:
        notes.append(f"Parameter stability {stability_score:.2f} < {STABILITY_OK_THRESHOLD} — params vary across folds.")
    if degradation < DEGRADATION_OK_RATIO:
        notes.append(f"IS→OOS degradation {degradation:.2f} < {DEGRADATION_OK_RATIO} — likely overfit.")
    if win_consistency < WIN_CONSISTENCY_MIN:
        notes.append(f"Win consistency {win_consistency:.2f} — OOS edge not consistent across folds.")

    eligibility = _eligibility(aggregate["gates"]["overall_pass"], unstable, overfit_warning)

    return WalkForwardResult(
        folds=results, objective=objective, spec=spec, aggregate=aggregate,
        stability_score=stability_score, unstable=unstable, overfit_warning=overfit_warning,
        eligibility=eligibility, parameter_robustness=robustness, notes=notes,
    )


def _parameter_stability(results: list[FoldResult]) -> tuple[float, dict]:
    """Stability = modal-param-set frequency; robustness = per-param value counts."""
    n = len(results)
    keys = sorted({k for r in results for k in r.best_params})
    sigs: dict[tuple, int] = {}
    robustness: dict[str, dict] = {k: {} for k in keys}
    for r in results:
        sig = tuple((k, r.best_params.get(k)) for k in keys)
        sigs[sig] = sigs.get(sig, 0) + 1
        for k in keys:
            v = r.best_params.get(k)
            robustness[k][str(v)] = robustness[k].get(str(v), 0) + 1
    modal = max(sigs.values()) if sigs else 0
    return (modal / n if n else 0.0), robustness


def _eligibility(gates_pass: bool, unstable: bool, overfit: bool) -> str:
    """Advisory only — never promotes."""
    if gates_pass and not unstable and not overfit:
        return "ELIGIBLE_FOR_PAPER_PRIORITIZATION"
    if gates_pass:
        return "PAPER_OPTIMIZATION"  # positive OOS but unstable/overfit → keep iterating
    return "REJECT"


def fold_report(result: WalkForwardResult) -> list[dict]:
    """Flat per-fold report rows (for CSV / display)."""
    rows = []
    for r in result.folds:
        rows.append({
            "fold_id": r.fold.fold_id,
            "train_start": str(r.fold.train_start.date()),
            "train_end": str(r.fold.train_end.date()),
            "validate_start": str(r.fold.validate_start.date()),
            "validate_end": str(r.fold.validate_end.date()),
            "best_params": r.best_params,
            "is_objective": round(r.is_objective, 4),
            "oos_objective": round(r.oos_objective, 4),
            "oos_expectancy": round(float(r.oos_metrics.get("expectancy", 0.0)), 4),
            "oos_profit_factor": round(float(r.oos_metrics.get("profit_factor", 0.0)), 4),
            "run_id": r.run_id,
        })
    return rows
