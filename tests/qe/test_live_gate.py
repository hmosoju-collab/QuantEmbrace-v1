"""Live is blocked BY CONSTRUCTION: no token without evidence, no trade without
a token AND a client. These tests are the safety proof for M6."""

import json

import pytest

from qe.execution import LiveBroker, LiveBrokerLocked
from qe.live_gate import (
    FORWARD_GATE_PASS,
    GATE_DIR,
    OPERATOR_APPROVAL,
    LiveGateRefused,
    LiveGateToken,
    evaluate_live_preconditions,
    mint_live_gate_token,
)

CFG = "a" * 64


def _mint_kwargs(base_dir, **overrides):
    kwargs = {
        "base_dir": base_dir,
        "config_hash": CFG,
        "factor": "delivery",
        "lake_age_days": 1.0,
        "kill_active": False,
        "approved_by": "operator",
    }
    kwargs.update(overrides)
    return kwargs


def test_gate_refuses_with_no_evidence(tmp_path):
    with pytest.raises(LiveGateRefused) as exc:
        mint_live_gate_token(**_mint_kwargs(tmp_path))
    failed = {c.name for c in exc.value.checks if not c.passed}
    # Every evidence-bearing precondition must be failing in a fresh repo.
    assert {
        "forward_gate_pass",
        "forward_months",
        "clean_paper_sessions",
        "operator_approval",
    } <= failed


def test_individual_checks_are_fail_closed(tmp_path):
    checks = {
        c.name: c
        for c in evaluate_live_preconditions(
            base_dir=tmp_path,
            config_hash=CFG,
            factor="delivery",
            lake_age_days=None,
            kill_active=True,
        )
    }
    assert not checks["lake_fresh"].passed  # unknown age fails closed
    assert not checks["kill_switch_clear"].passed  # active kill fails
    assert not checks["forward_months"].passed


def _write_full_evidence(base_dir):
    """Fabricate a complete passing evidence set (for the positive-path test only)."""
    gate = base_dir / GATE_DIR
    gate.mkdir(parents=True, exist_ok=True)
    (gate / FORWARD_GATE_PASS).write_text(json.dumps({"result": "PASS", "config_hash": CFG}))
    (gate / OPERATOR_APPROVAL).write_text(
        json.dumps({"approved": True, "config_hash": CFG, "operator": "hari"})
    )
    # 12 complete forward months across a qe study summary
    summ = base_dir / "reports" / "qe" / "s1"
    summ.mkdir(parents=True, exist_ok=True)
    (summ / "summary.json").write_text(json.dumps({"factor": "delivery", "n_full": 12}))
    # 3 clean paper sessions (distinct month-ends)
    jdir = base_dir / "journals"
    jdir.mkdir(parents=True, exist_ok=True)
    from qe.journal import JournalWriter

    for i, d in enumerate(["2026-01-30", "2026-02-27", "2026-03-31"]):
        with JournalWriter(jdir / f"paper-s{i}.jsonl") as j:
            j.session_start(
                session_id=f"s{i}",
                mode="paper",
                config_hash=CFG,
                config={},
                code_sha="x",
                data_snapshot_id="ds",
            )
            j.write("REBALANCE", {"date": d, "nav": 1_000_000})
            j.session_end("OK", {"as_of": d, "rebalanced": True, "nav": 1_000_000})


def test_gate_mints_only_when_all_evidence_present(tmp_path):
    _write_full_evidence(tmp_path)
    token = mint_live_gate_token(**_mint_kwargs(tmp_path))
    assert isinstance(token, LiveGateToken)
    assert token.config_hash == CFG
    assert token.is_valid(CFG)
    assert not token.is_valid("b" * 64)  # bound to exactly this config


def test_gate_still_refuses_if_any_single_check_fails(tmp_path):
    _write_full_evidence(tmp_path)
    # Flip just the kill switch → the whole gate must refuse.
    with pytest.raises(LiveGateRefused):
        mint_live_gate_token(**_mint_kwargs(tmp_path, kill_active=True))
    # Or a stale lake alone.
    with pytest.raises(LiveGateRefused):
        mint_live_gate_token(**_mint_kwargs(tmp_path, lake_age_days=30.0))


def test_live_broker_needs_valid_token_and_client(tmp_path):
    _write_full_evidence(tmp_path)
    token = mint_live_gate_token(**_mint_kwargs(tmp_path))

    with pytest.raises(LiveBrokerLocked, match="LiveGateToken"):
        LiveBroker(token=None, config_hash=CFG, client=object())  # no token
    with pytest.raises(LiveBrokerLocked, match="broker client"):
        LiveBroker(token=token, config_hash=CFG, client=None)  # token but no client
    with pytest.raises(LiveBrokerLocked):
        LiveBroker(token=token, config_hash="b" * 64, client=object())  # token for wrong config

    # Both present and valid → constructs (still trades nothing; no adapter wired).
    assert LiveBroker(token=token, config_hash=CFG, client=object()) is not None


def test_cli_live_refuses(tmp_path, capsys):
    from qe.cli import main

    cfg = tmp_path / "run.yaml"
    cfg.write_text(
        "name: x\nmode: paper\nstart_date: 2025-12-31\nend_date: 2026-12-31\n"
        "universe: {market: NSE, segment: EQ, symbols: null}\n"
        "data: {lake_root: nolake}\njournal_dir: j\n"
        "strategy: {kind: factor_book, factor: delivery}\n"
    )
    rc = main(["live", "--config", str(cfg), "--base-dir", str(tmp_path)])
    assert rc == 1  # refused
    out = capsys.readouterr().out
    assert "LIVE TRADING BLOCKED" in out and "FAIL" in out


def test_fail_closed_drills_pass():
    from qe.livecheck import run_fail_closed_drills

    results = run_fail_closed_drills()
    assert {r.name for r in results} == {"staleness", "kill_halt", "config_drift"}
    assert all(r.passed for r in results), [r for r in results if not r.passed]
