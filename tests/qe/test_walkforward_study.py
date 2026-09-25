"""End-to-end declarative walk-forward study on a synthetic panel."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from qe.config import (
    DataConfig,
    ExperimentConfig,
    GateSpec,
    RunConfig,
    StrategyConfig,
    UniverseConfig,
    WalkForwardConfig,
)
from qe.data.panel import Panel
from qe.research import run_walk_forward_study
from qe.research.registry import read_registry

IST = "Asia/Kolkata"


@pytest.fixture(scope="module")
def panel() -> Panel:
    rng = np.random.default_rng(11)
    dates = pd.date_range("2020-01-01", periods=600, freq="B", tz=IST)
    syms = [f"S{i:03d}" for i in range(80)]
    steps = rng.normal(0.0004, 0.02, (len(dates), len(syms)))
    close = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=dates, columns=syms)
    turn = close * pd.DataFrame(rng.uniform(1e5, 1e6, close.shape), index=dates, columns=syms)
    deliv = pd.DataFrame(rng.uniform(20, 80, close.shape), index=dates, columns=syms)
    return Panel(close=close, turnover=turn, delivery=deliv)


def _config(tmp_path, panel: Panel) -> RunConfig:
    return RunConfig(
        name="wf-test",
        mode="sim",
        study_kind="walk_forward",
        start_date=date(2020, 1, 1),
        end_date=pd.Timestamp(panel.index[-1]).tz_convert(IST).date(),
        universe=UniverseConfig(symbols=None),
        data=DataConfig(lake_root="unused"),
        journal_dir=str(tmp_path / "journals"),
        seed_nav=1_000_000.0,
        strategy=StrategyConfig(factor="delivery", top_n=40, k=8),
        walk_forward=WalkForwardConfig(),
        experiment=ExperimentConfig(
            name="wf-test",
            family="test-family",
            hypothesis="synthetic delivery panel has no real edge",
            gates=(GateSpec(name="months_min", metric="months", op=">=", value=3),),
        ),
    )


def test_walk_forward_study_end_to_end(tmp_path, panel):
    wf = run_walk_forward_study(
        _config(tmp_path, panel), base_dir=tmp_path, panel=panel, report_dir=tmp_path / "out"
    )

    # Engine leg produced a real monthly series with per-year breakdown.
    assert wf.engine_metrics["months"] >= 3
    assert np.isfinite(wf.engine_metrics["sharpe"])
    assert len(wf.engine_per_year) >= 1
    assert wf.engine_pass  # months gate

    # v1 cross-check ran all four variants and matches a direct wf_v1 call.
    from qe.research import wf_v1
    from qe.research.metrics import monthly_metrics

    direct = wf_v1.run_delivery(panel.close, panel.turnover, panel.delivery, None, 40, 8)
    assert wf.v1_variants["delivery"]["metrics"]["sharpe"] == pytest.approx(
        monthly_metrics(direct.monthly_net)["sharpe"], abs=1e-12
    )
    assert set(wf.v1_variants) == {
        "delivery",
        "delivery+overlay",
        "benchmark",
        "benchmark+overlay",
        "delivery+overlay_pit",
        "benchmark+overlay_pit",
    }
    assert "look-ahead proxy (F-10)" in wf.report_path.read_text()

    # Registry recorded the run with the family budget.
    records = read_registry(tmp_path)
    assert len(records) == 1
    assert records[0]["family"] == "test-family"
    assert records[0]["run"]["session_id"] == wf.sim.session_id

    # Report + summary written with provenance.
    assert wf.report_path.exists() and wf.summary_path.exists()
    text = wf.report_path.read_text()
    assert wf.sim.session_id in text
    assert "experiment #1 in this family" in text


def test_gate_failure_is_reported_not_hidden(tmp_path, panel):
    config = _config(tmp_path, panel)
    config = config.model_copy(
        update={
            "experiment": config.experiment.model_copy(
                update={
                    "gates": (GateSpec(name="impossible", metric="sharpe", op=">=", value=99.0),)
                }
            )
        }
    )
    wf = run_walk_forward_study(
        config, base_dir=tmp_path, panel=panel, report_dir=tmp_path / "out2"
    )
    assert not wf.engine_pass
    assert not wf.v1_pass
    assert "❌ FAIL" in wf.report_path.read_text()


@pytest.mark.parametrize("ungated", ["empty_gates", "no_experiment"])
def test_ungated_study_fails_closed(tmp_path, panel, ungated):
    """F-11: no pre-registered gates means nothing was tested — FAIL, never a
    vacuous pass (all_passed([]) is False by design)."""
    config = _config(tmp_path, panel)
    experiment = config.experiment.model_copy(update={"gates": ()})
    config = config.model_copy(
        update={"experiment": experiment if ungated == "empty_gates" else None}
    )
    wf = run_walk_forward_study(
        config, base_dir=tmp_path, panel=panel, report_dir=tmp_path / f"out-{ungated}"
    )
    assert wf.engine_pass is False
    assert wf.v1_pass is False
    assert "verdict is FAIL" in wf.report_path.read_text()
