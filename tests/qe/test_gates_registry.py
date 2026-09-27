from qe.config import ExperimentConfig, GateSpec
from qe.research.gates import all_passed, evaluate_gates
from qe.research.registry import (
    experiment_id,
    family_experiment_count,
    read_registry,
    register_run,
)


def _gates() -> tuple[GateSpec, ...]:
    return (
        GateSpec(name="sharpe_min", metric="sharpe", op=">=", value=1.0),
        GateSpec(name="dd_cap", metric="maxdd", op=">=", value=-0.35),
    )


def test_gates_pass_and_fail():
    results = evaluate_gates(_gates(), {"sharpe": 1.4, "maxdd": -0.28})
    assert all_passed(results)
    results = evaluate_gates(_gates(), {"sharpe": 0.9, "maxdd": -0.28})
    assert not all_passed(results)
    assert [r["passed"] for r in results] == [False, True]


def test_missing_metric_fails_closed():
    results = evaluate_gates(_gates(), {"sharpe": 1.4})  # no maxdd computed
    assert not all_passed(results)
    assert results[1]["actual"] is None and not results[1]["passed"]


def test_no_gates_is_not_a_pass():
    assert not all_passed([])  # an ungated "pass" must never look like a gated one


def test_registry_append_and_family_budget(tmp_path):
    exp_a = ExperimentConfig(name="wf-a", family="delivery-factor", hypothesis="H_a")
    exp_b = ExperimentConfig(name="wf-b", family="delivery-factor", hypothesis="H_b")

    r1 = register_run(tmp_path, exp_a, {"session_id": "s1", "engine_pass": True})
    assert r1["experiment_id"] == experiment_id("wf-a", "H_a")
    assert r1["family_experiment_count"] == 1

    # Re-running the SAME experiment adds a run, not a new experiment.
    r2 = register_run(tmp_path, exp_a, {"session_id": "s2", "engine_pass": True})
    assert r2["family_experiment_count"] == 1

    # A new hypothesis in the family consumes test budget.
    r3 = register_run(tmp_path, exp_b, {"session_id": "s3", "engine_pass": False})
    assert r3["family_experiment_count"] == 2

    records = read_registry(tmp_path)
    assert len(records) == 3
    assert family_experiment_count(tmp_path, "delivery-factor") == 2
    assert family_experiment_count(tmp_path, "other") == 0


def test_family_count_is_persisted_in_the_ledger(tmp_path):
    """F-12: the multiple-testing count must be in the committed ledger line,
    not only in the returned dict."""
    exp_a = ExperimentConfig(name="a", family="fam", hypothesis="h-a")
    exp_b = ExperimentConfig(name="b", family="fam", hypothesis="h-b")
    register_run(tmp_path, exp_a, {"session_id": "s1"})
    register_run(tmp_path, exp_a, {"session_id": "s2"})  # rerun: same experiment
    register_run(tmp_path, exp_b, {"session_id": "s3"})
    counts = [r["family_experiment_count"] for r in read_registry(tmp_path)]
    assert counts == [1, 1, 2]
