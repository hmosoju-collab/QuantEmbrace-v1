"""Unit tests for walk-forward validation (Phase AWS-BT-9).

Covers: train/validation windows do not overlap · validation always after train ·
no future leakage · fold reports generated · unstable strategy flagged ·
parameters selected only from the train period.

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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
