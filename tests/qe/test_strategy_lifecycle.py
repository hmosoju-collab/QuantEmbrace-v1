"""Strategy lifecycle ledger: evidence-gated, human-approved, append-only."""

import json

import pytest
import yaml

from qe.cli import main
from qe.config import ExperimentConfig
from qe.research import lifecycle
from qe.research.lifecycle import LifecycleError, current_state, read_ledger, transition
from qe.research.registry import experiment_id, register_run

EXP = ExperimentConfig(name="wf-x", family="new-fam", hypothesis="x beats y")
EXP_ID = experiment_id(EXP.name, EXP.hypothesis)
PAPER_CFG = {
    "name": "x-paper",
    "mode": "paper",
    "start_date": "2026-01-01",
    "end_date": "2027-12-31",
    "universe": {"market": "NSE", "segment": "EQ", "symbols": None},
    "strategy": {"kind": "factor_book", "factor": "delivery"},
}


def _step(base, to, **kw):
    return transition(base, strategy_id="strat-x", to_state=to, approved_by="Hari", **kw)


@pytest.fixture()
def base(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "x_paper.yaml").write_text(yaml.safe_dump(PAPER_CFG))
    return tmp_path


def _to_backtest(base, passed=True):
    _step(base, "CANDIDATE", family="new-fam", hypothesis_ref="qe.ai:hyp-123")
    _step(base, "RESEARCH")
    register_run(base, EXP, {"session_id": "s1", "config_hash": "c" * 64, "engine_pass": passed})
    _step(base, "BACKTEST", experiment_id=EXP_ID)


def test_full_evidence_gated_path(base):
    _to_backtest(base)
    _step(base, "VALIDATION")
    rec = _step(base, "PAPER", paper_config="configs/x_paper.yaml")
    art = base / "governance" / "live-gate" / "forward-gate-pass.json"
    art.parent.mkdir(parents=True)
    art.write_text(json.dumps({"config_hash": rec["evidence"]["config_hash"], "passed": True}))
    _step(base, "PRODUCTION_ELIGIBLE", gate_evidence="governance/live-gate/forward-gate-pass.json")
    assert current_state(base, "strat-x") == "PRODUCTION_ELIGIBLE"
    ledger = read_ledger(base)
    assert [r["to_state"] for r in ledger] == [
        "CANDIDATE", "RESEARCH", "BACKTEST", "VALIDATION", "PAPER", "PRODUCTION_ELIGIBLE"
    ]  # fmt: skip
    assert ledger[0]["versions"]["research_signal_version"] == "research_signal/1"
    assert ledger[2]["versions"]["backtest_version"] == "c" * 64
    assert all(r["approved_by"] == "Hari" and r["versions"]["strategy_version"] for r in ledger)


@pytest.mark.parametrize("who", ["qe.ai", "Claude", "auto", "research-bot", "AI", "  "])
def test_non_human_approver_is_refused(base, who):
    with pytest.raises(LifecycleError):
        transition(base, strategy_id="strat-x", to_state="CANDIDATE", approved_by=who,
                   family="f", hypothesis_ref="doc.md")  # fmt: skip
    assert read_ledger(base) == []  # nothing written


def test_states_cannot_be_skipped(base):
    _step(base, "CANDIDATE", family="f", hypothesis_ref="doc.md")
    with pytest.raises(LifecycleError, match="illegal transition CANDIDATE"):
        _step(base, "BACKTEST", experiment_id=EXP_ID)
    assert len(read_ledger(base)) == 1


def test_backtest_requires_a_registered_study(base):
    _step(base, "CANDIDATE", family="f", hypothesis_ref="doc.md")
    _step(base, "RESEARCH")
    with pytest.raises(LifecycleError, match="no registered study run"):
        _step(base, "BACKTEST", experiment_id="exp-doesnotexist")


def test_validation_requires_passed_pre_registered_gates(base):
    _to_backtest(base, passed=False)
    with pytest.raises(LifecycleError, match="did not pass"):
        _step(base, "VALIDATION")


def test_production_eligible_requires_bound_gate_evidence(base):
    _to_backtest(base)
    _step(base, "VALIDATION")
    _step(base, "PAPER", paper_config="configs/x_paper.yaml")
    art = base / "wrong.json"
    art.write_text(json.dumps({"config_hash": "not-this-book"}))
    with pytest.raises(LifecycleError, match="not bound"):
        _step(base, "PRODUCTION_ELIGIBLE", gate_evidence="wrong.json")


def test_graveyard_needs_reason_and_is_terminal(base):
    _step(base, "CANDIDATE", family="f", hypothesis_ref="doc.md")
    with pytest.raises(LifecycleError, match="cause-of-death"):
        _step(base, "GRAVEYARD")
    _step(base, "GRAVEYARD", reason="failed walk-forward OOS")
    with pytest.raises(LifecycleError, match="terminal"):
        _step(base, "RESEARCH")


def test_cli_status_and_refusal(base, capsys):
    assert main(["lifecycle", "status", "--base-dir", str(base)]) == 0
    assert "no strategies registered" in capsys.readouterr().out
    rc = main([
        "lifecycle", "transition", "--strategy", "strat-y", "--to", "CANDIDATE",
        "--approved-by", "qe.ai", "--family", "f", "--hypothesis-ref", "d.md",
        "--base-dir", str(base),
    ])  # fmt: skip
    assert rc == 2 and "REFUSED" in capsys.readouterr().err
    rc = main([
        "lifecycle", "transition", "--strategy", "strat-y", "--to", "CANDIDATE",
        "--approved-by", "Hari", "--family", "f", "--hypothesis-ref", "d.md",
        "--base-dir", str(base),
    ])  # fmt: skip
    assert rc == 0 and lifecycle.status(base) == {"strat-y": "CANDIDATE"}
