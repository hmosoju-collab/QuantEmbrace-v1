"""Unit tests for walk-forward validation (Phase 5).

Covers: train/validation windows do not overlap · validation always after train ·
no future leakage · fold reports generated · unstable strategy flagged ·
parameters selected only from the train period · REJECT eligibility ·
PAPER_OPTIMIZATION eligibility · registry run registration · INSUFFICIENT_DATA ·
no broker references.

Backtest-only: a stub ``evaluate`` callback — no real backtest, no broker, no AWS.

Run:  python -m pytest tests/backtest/test_walk_forward.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.walk_forward import (  # noqa: E402
    PRESETS,
    WindowSpec,
    fold_report,
    generate_folds,
    run_walk_forward,
)

GRID = [{"atr": 1.0}, {"atr": 2.0}]


def _good_evaluate(params, fold, phase):
    base = 5.0 if params["atr"] == 2.0 else 1.0
    factor = 1.0 if phase == "train" else 0.8
    return {"expectancy": base * factor, "profit_factor": 1.5, "net_pnl": 100.0}


# ── window geometry ──────────────────────────────────────────────────────────


def test_train_and_validation_windows_do_not_overlap():
    for preset in PRESETS.values():
        folds = generate_folds("2010-01-01", "2022-01-01", preset)
        assert folds
        for f in folds:
            # Half-open windows sharing only the boundary ⇒ no overlapping day.
            assert f.train_end <= f.validate_start


def test_validation_period_always_after_train():
    folds = generate_folds("2018-01-01", "2021-01-01", PRESETS["default"])
    for f in folds:
        assert f.validate_start >= f.train_end
        assert f.validate_start < f.validate_end
        assert f.train_start < f.train_end


def test_no_future_leakage():
    # IS strictly precedes OOS for every fold and every preset (anchored + rolling).
    for spec in [PRESETS["default"], PRESETS["long"], WindowSpec(12, 3, 3, anchored=True)]:
        for f in generate_folds("2012-01-01", "2020-01-01", spec):
            assert f.train_start < f.train_end <= f.validate_start < f.validate_end


def test_fold_reports_generated():
    res = run_walk_forward(start="2018-01-01", end="2020-07-01", spec=PRESETS["default"],
                           param_grid=GRID, evaluate=_good_evaluate)
    rows = fold_report(res)
    assert len(rows) == len(res.folds) > 0
    required = {"fold_id", "train_start", "validate_start", "best_params", "is_objective", "oos_objective"}
    assert required <= set(rows[0])
    assert res.eligibility == "ELIGIBLE_FOR_PAPER_PRIORITIZATION"  # good + stable


def test_unstable_strategy_flagged():
    # Best train param alternates by fold parity → low stability → flagged.
    def evaluate(params, fold, phase):
        fav = 2.0 if fold.fold_id % 2 == 0 else 1.0
        exp = (5.0 if params["atr"] == fav else 1.0) * (1.0 if phase == "train" else 0.7)
        return {"expectancy": exp, "profit_factor": 1.3, "net_pnl": 50.0}

    res = run_walk_forward(start="2018-01-01", end="2020-07-01", spec=PRESETS["default"],
                           param_grid=GRID, evaluate=evaluate)
    assert res.unstable is True
    assert res.stability_score < 0.7
    assert res.eligibility in ("PAPER_OPTIMIZATION", "REJECT")


def test_parameters_selected_only_from_train_period():
    calls: list[tuple] = []

    def evaluate(params, fold, phase):
        calls.append((fold.fold_id, phase, params["atr"]))
        # Train objective deterministically favours atr=2.0.
        base = 9.0 if params["atr"] == 2.0 else 3.0
        return {"expectancy": base * (1.0 if phase == "train" else 0.5),
                "profit_factor": 1.4, "net_pnl": 80.0}

    res = run_walk_forward(start="2018-01-01", end="2020-04-01", spec=PRESETS["default"],
                           param_grid=GRID, evaluate=evaluate)
    for fr in res.folds:
        fid = fr.fold.fold_id
        train_calls = [(p) for (f, ph, p) in calls if f == fid and ph == "train"]
        # Every grid param was tried on train; exactly the grid, nothing else.
        assert sorted(train_calls) == sorted(p["atr"] for p in GRID)
        # The selected param is the train argmax (atr=2.0 here) — not influenced by OOS.
        assert fr.best_params["atr"] == 2.0
        # Validate phase was called exactly once for this fold, with the chosen param.
        val_calls = [p for (f, ph, p) in calls if f == fid and ph == "validate"]
        assert val_calls == [2.0]


def test_reject_eligibility_when_oos_gates_fail():
    """Strategy with negative OOS expectancy → REJECT verdict."""
    def bad_evaluate(params, fold, phase):
        return {"expectancy": -2.0, "profit_factor": 0.8, "net_pnl": -5000.0}

    res = run_walk_forward(
        start="2018-01-01", end="2020-07-01", spec=PRESETS["default"],
        param_grid=GRID, evaluate=bad_evaluate,
    )
    assert res.eligibility == "REJECT"
    assert res.aggregate["gates"]["overall_pass"] is False
    assert res.overfit_warning is True


def test_paper_optimization_when_gates_pass_but_overfit():
    """OOS gates pass but severe IS→OOS degradation → PAPER_OPTIMIZATION."""
    call_counts: dict[str, int] = {}

    def degraded_evaluate(params, fold, phase):
        call_counts[phase] = call_counts.get(phase, 0) + 1
        if phase == "train":
            return {"expectancy": 20.0, "profit_factor": 3.0, "net_pnl": 50000.0}
        # OOS: expectancy barely positive but degradation ratio = 0.1 < 0.5 → overfit
        return {"expectancy": 2.0, "profit_factor": 1.3, "net_pnl": 5000.0}

    res = run_walk_forward(
        start="2018-01-01", end="2020-07-01", spec=PRESETS["default"],
        param_grid=GRID, evaluate=degraded_evaluate,
    )
    assert res.aggregate["gates"]["overall_pass"] is True
    assert res.aggregate["is_oos_degradation"] < 0.5
    assert res.overfit_warning is True
    assert res.eligibility == "PAPER_OPTIMIZATION"


def test_registry_run_registration():
    """When registry + run_spec_factory are supplied, each OOS fold gets a run_id."""
    from unittest.mock import MagicMock

    fake_rec = MagicMock()
    fake_rec.run_id = "bt_test_fold"
    registry = MagicMock()
    registry.create_run.return_value = fake_rec

    def run_spec_factory(fold, params):
        return MagicMock()

    res = run_walk_forward(
        start="2018-01-01", end="2019-07-01", spec=PRESETS["default"],
        param_grid=GRID, evaluate=_good_evaluate,
        registry=registry, run_spec_factory=run_spec_factory,
    )
    n = len(res.folds)
    assert n > 0
    assert registry.create_run.call_count == n
    assert registry.mark_completed.call_count == n
    for fr in res.folds:
        assert fr.run_id == "bt_test_fold"


def test_insufficient_data_returns_no_folds():
    """Date range shorter than one full train+validate window → no folds, INSUFFICIENT_DATA."""
    spec = PRESETS["default"]  # 12m train + 3m validate = 15m minimum
    # Provide only 6 months → cannot fit even one fold
    res = run_walk_forward(
        start="2020-01-01", end="2020-06-01", spec=spec,
        param_grid=GRID, evaluate=_good_evaluate,
    )
    assert res.folds == []
    assert res.eligibility == "INSUFFICIENT_DATA"
    assert res.stability_score == 0.0


def test_anchored_mode_train_window_grows():
    """Anchored mode: train_start is fixed while validate window rolls forward."""
    spec = WindowSpec(12, 3, 3, anchored=True)
    folds = generate_folds("2018-01-01", "2021-01-01", spec)
    assert len(folds) > 1
    # All train windows share the same start date.
    starts = {f.train_start for f in folds}
    assert len(starts) == 1
    # Train window grows each fold (end advances).
    ends = [f.train_end for f in folds]
    assert ends == sorted(ends)


def test_no_broker_calls_in_walk_forward():
    """walk_forward.py must not import or reference any broker API."""
    import backtesting.walk_forward as wf_mod
    src = Path(wf_mod.__file__).read_text().lower()
    forbidden = ["kiteconnect", "alpaca", "place_order", "zerodhabroker", "submit_order"]
    present = [t for t in forbidden if t in src]
    assert present == [], f"walk_forward must not reference brokers: {present}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
