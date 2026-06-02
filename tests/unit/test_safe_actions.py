"""Unit tests for Phase 3 safe_actions layer.

Covers all 15 user-specified scenarios plus policy, classifier, audit,
executor, and idempotency behaviour.

All tests are synchronous and pure — no I/O, no DynamoDB, no Kafka.
The audit log is redirected to a tmp_path file for each test that reads it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution_engine.safe_actions import (
    ActionScope,
    ActionType,
    ClassificationResult,
    RiskLevel,
    SafeAction,
    SafeActionAudit,
    SafeActionClassifier,
    SafeActionExecutor,
    SafeActionPolicy,
    TradingMode,
)
from execution_engine.safe_actions.safe_action_policy import (
    FORBIDDEN_CONTEXT_LABELS,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _audit(tmp_path: Path) -> SafeActionAudit:
    return SafeActionAudit(path=str(tmp_path / "audit.jsonl"))


def _policy(mode: TradingMode = TradingMode.PAPER) -> SafeActionPolicy:
    return SafeActionPolicy(mode)


def _executor(
    tmp_path: Path,
    mode: TradingMode = TradingMode.PAPER,
    notifier=None,
) -> SafeActionExecutor:
    return SafeActionExecutor(
        policy=_policy(mode),
        audit=_audit(tmp_path),
        notifier=notifier,
    )


def _block_entries_action(
    mode: TradingMode = TradingMode.PAPER,
    idempotency_key: str = "block-entries-stale-ltp-20260531",
) -> SafeAction:
    return SafeAction.build(
        action_type=ActionType.BLOCK_NEW_ENTRIES,
        scope=ActionScope.RISK_REDUCTION,
        mode=mode,
        risk_level=RiskLevel.LOW,
        requires_human_approval=False,
        idempotency_key=idempotency_key,
        preconditions=["market_open=True", "newest_age > max_age"],
        expected_effect="No new entry signals accepted; exit management continues",
        rollback_behavior="Remove block-entries flag from strategy-config DynamoDB",
        audit_payload={"reason": "stale_ltp", "market": "NSE"},
    )


def _kill_switch_action(mode: TradingMode = TradingMode.PAPER) -> SafeAction:
    return SafeAction.build(
        action_type=ActionType.ACTIVATE_KILL_SWITCH,
        scope=ActionScope.RISK_REDUCTION,
        mode=mode,
        risk_level=RiskLevel.HIGH,
        requires_human_approval=False,
        idempotency_key="kill-switch-unmanaged-live-20260531",
        preconditions=["unmanaged_live_position=True"],
        expected_effect="Kill switch set ACTIVE; all new orders blocked",
        rollback_behavior="python scripts/kill_switch_cli.py deactivate --reason <reason>",
    )


def _alert_action(
    mode: TradingMode = TradingMode.PAPER,
    message: str = "ai_engine DOWN — fallback path active",
    idempotency_key: str = "alert-ai-down-20260531",
) -> SafeAction:
    return SafeAction.build(
        action_type=ActionType.SEND_ALERT,
        scope=ActionScope.READ_ONLY,
        mode=mode,
        risk_level=RiskLevel.LOW,
        requires_human_approval=False,
        idempotency_key=idempotency_key,
        preconditions=[],
        expected_effect="Slack/SNS alert posted",
        rollback_behavior="N/A — read-only",
        audit_payload={"message": message},
    )


def _forbidden_action(mode: TradingMode = TradingMode.PAPER) -> SafeAction:
    return SafeAction.build(
        action_type=ActionType.FORBIDDEN,
        scope=ActionScope.FORBIDDEN,
        mode=mode,
        risk_level=RiskLevel.HIGH,
        requires_human_approval=False,
        idempotency_key="forbidden-test",
        preconditions=[],
        expected_effect="MUST NEVER EXECUTE",
        rollback_behavior="N/A",
    )


def _paper_exit_repair_action() -> SafeAction:
    return SafeAction.build(
        action_type=ActionType.ATTACH_EXIT_POLICY_PAPER,
        scope=ActionScope.PAPER_ONLY,
        mode=TradingMode.PAPER,
        risk_level=RiskLevel.LOW,
        requires_human_approval=False,
        idempotency_key="attach-exit-RELIANCE-20260531",
        preconditions=["position.exit_policy is None", "position.status=OPEN"],
        expected_effect="Default SL/TP policy attached to paper position",
        rollback_behavior="Remove exit_policy field from DynamoDB position record",
        audit_payload={"symbol": "RELIANCE", "mode": "paper"},
    )


def _paper_direction_repair_action() -> SafeAction:
    return SafeAction.build(
        action_type=ActionType.UPDATE_PAPER_DIRECTION_FROM_QUANTITY,
        scope=ActionScope.PAPER_ONLY,
        mode=TradingMode.PAPER,
        risk_level=RiskLevel.LOW,
        requires_human_approval=False,
        idempotency_key="direction-repair-INFY-20260531",
        preconditions=["position.direction != derived_direction"],
        expected_effect="Direction field corrected to match signed quantity",
        rollback_behavior="Restore original direction value",
        audit_payload={"symbol": "INFY"},
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. ai_engine DOWN — does NOT auto-restart Docker
# ══════════════════════════════════════════════════════════════════════════════


def test_ai_engine_down_never_proposes_docker_restart():
    clf = SafeActionClassifier()
    obs = clf.classify_finding(code="service.down", subject="ai_engine")
    assert obs.suppress_docker_restart is True
    restart_types = {ActionType.FORBIDDEN}  # no restart action type exists
    # Confirm no action type even resembles a restart
    for proposed in obs.proposed_action_types:
        assert proposed not in restart_types
        assert "restart" not in proposed.value.lower()
        assert "docker" not in proposed.value.lower()


def test_ai_engine_down_classification_is_alert_only_not_kill_switch():
    clf = SafeActionClassifier()
    obs = clf.classify_finding(code="service.down", subject="ai_engine")
    # ai_engine is non-critical (EnrichmentWatchdog fallback) — must not be KILL_SWITCH
    assert obs.classification is ClassificationResult.ALERT_ONLY
    assert ActionType.ACTIVATE_KILL_SWITCH not in obs.proposed_action_types


# ══════════════════════════════════════════════════════════════════════════════
# 2. ai_engine DOWN — blocks AI-dependent entries and sends alert
# ══════════════════════════════════════════════════════════════════════════════


def test_ai_engine_down_blocks_ai_dependent_entries():
    clf = SafeActionClassifier()
    obs = clf.classify_finding(code="service.down", subject="ai_engine")
    assert ActionType.BLOCK_NEW_ENTRIES in obs.proposed_action_types
    assert ActionType.SEND_ALERT in obs.proposed_action_types


def test_ai_engine_docker_down_generates_runbook_not_restart():
    clf = SafeActionClassifier()
    obs = clf.classify_finding(code="docker.down", subject="ai_engine")
    assert ActionType.GENERATE_RUNBOOK_COMMAND in obs.proposed_action_types
    assert obs.suppress_docker_restart is True
    assert obs.classification is ClassificationResult.ALERT_ONLY


# ══════════════════════════════════════════════════════════════════════════════
# 3. Stale LTP blocks new entries
# ══════════════════════════════════════════════════════════════════════════════


def test_stale_ltp_classifies_as_block_entries():
    clf = SafeActionClassifier()
    for code in ("broker.feed_very_stale", "broker.no_fresh_prices"):
        obs = clf.classify_finding(code=code)
        assert obs.classification is ClassificationResult.BLOCK_ENTRIES
        assert ActionType.BLOCK_NEW_ENTRIES in obs.proposed_action_types

    # Raw observation path
    obs = clf.classify_raw("stale_ltp_market_hours")
    assert obs.classification is ClassificationResult.BLOCK_ENTRIES
    assert ActionType.BLOCK_NEW_ENTRIES in obs.proposed_action_types


def test_stale_ltp_block_entries_allowed_in_paper_mode(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = _block_entries_action(mode=TradingMode.PAPER)
    result = exe.execute(action, reason="stale_ltp_market_hours")
    # Framework-complete but Phase 4 stub — must not be policy-blocked
    assert not result.blocked
    assert result.stub_not_implemented is True  # Phase 4 wires the writer


# ══════════════════════════════════════════════════════════════════════════════
# 4. Stale LTP does NOT disable exit management
# ══════════════════════════════════════════════════════════════════════════════


def test_stale_ltp_block_entries_expected_effect_preserves_exits():
    action = _block_entries_action()
    # The expected_effect description must confirm exits continue
    assert "exit" in action.expected_effect.lower()


def test_block_new_entries_scope_is_risk_reduction_not_paper_only():
    # BLOCK_NEW_ENTRIES uses RISK_REDUCTION scope — applies to exits
    # continuing because it only blocks entry signals, not the exit path.
    action = _block_entries_action()
    assert action.scope is ActionScope.RISK_REDUCTION


# ══════════════════════════════════════════════════════════════════════════════
# 5. reconciliation_required in live blocks entries
# ══════════════════════════════════════════════════════════════════════════════


def test_reconciliation_required_live_classifies_block_entries():
    clf = SafeActionClassifier()
    obs = clf.classify_raw("reconciliation_required_live")
    assert obs.classification is ClassificationResult.BLOCK_ENTRIES
    assert ActionType.BLOCK_NEW_ENTRIES in obs.proposed_action_types
    assert ActionType.MARK_LIVE_READINESS_BLOCKED in obs.proposed_action_types


def test_reconciliation_required_live_block_allowed_in_live_disabled(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.LIVE_DISABLED)
    action = SafeAction.build(
        action_type=ActionType.BLOCK_NEW_ENTRIES,
        scope=ActionScope.RISK_REDUCTION,
        mode=TradingMode.LIVE_DISABLED,
        risk_level=RiskLevel.LOW,
        requires_human_approval=False,
        idempotency_key="block-entries-recon-live-20260531",
        preconditions=["reconciliation_required=True"],
        expected_effect="No new entry signals; exit management continues",
        rollback_behavior="Remove block flag after reconciliation passes",
    )
    result = exe.execute(action, reason="reconciliation_required_live")
    assert not result.blocked


# ══════════════════════════════════════════════════════════════════════════════
# 6. Kill switch ON is allowed as risk-reducing
# ══════════════════════════════════════════════════════════════════════════════


def test_kill_switch_on_allowed_in_paper_mode(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = _kill_switch_action(mode=TradingMode.PAPER)
    result = exe.execute(action, reason="unmanaged_live_position")
    assert not result.blocked
    # Phase 4 stub — policy passed
    assert result.stub_not_implemented is True


def test_kill_switch_on_allowed_in_live_disabled(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.LIVE_DISABLED)
    action = _kill_switch_action(mode=TradingMode.LIVE_DISABLED)
    result = exe.execute(action, reason="unmanaged_live_position")
    assert not result.blocked


def test_kill_switch_on_scope_is_risk_reduction():
    action = _kill_switch_action()
    assert action.scope is ActionScope.RISK_REDUCTION


# ══════════════════════════════════════════════════════════════════════════════
# 7. Clearing kill switch is FORBIDDEN
# ══════════════════════════════════════════════════════════════════════════════


def test_clear_kill_switch_is_in_forbidden_context_labels():
    assert "clear_kill_switch" in FORBIDDEN_CONTEXT_LABELS


def test_clear_kill_switch_classifies_as_forbidden():
    clf = SafeActionClassifier()
    obs = clf.classify_forbidden_context("clear_kill_switch")
    assert obs.classification is ClassificationResult.FORBIDDEN
    assert ActionType.FORBIDDEN in obs.proposed_action_types


def test_forbidden_action_type_always_blocked_by_policy(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = _forbidden_action()
    result = exe.execute(action, reason="test")
    assert result.blocked is True
    assert result.executed is False


# ══════════════════════════════════════════════════════════════════════════════
# 8. Enabling live trading is FORBIDDEN
# ══════════════════════════════════════════════════════════════════════════════


def test_enable_live_trading_in_forbidden_context_labels():
    assert "enable_live_trading" in FORBIDDEN_CONTEXT_LABELS
    assert "set_trading_mode_live" in FORBIDDEN_CONTEXT_LABELS


def test_enable_live_trading_classifies_forbidden():
    clf = SafeActionClassifier()
    obs = clf.classify_forbidden_context("enable_live_trading")
    assert obs.classification is ClassificationResult.FORBIDDEN


# ══════════════════════════════════════════════════════════════════════════════
# 9. Changing capital limits is FORBIDDEN
# ══════════════════════════════════════════════════════════════════════════════


def test_change_capital_limits_in_forbidden_context_labels():
    assert "change_capital_limits" in FORBIDDEN_CONTEXT_LABELS


# ══════════════════════════════════════════════════════════════════════════════
# 10. Broker order action is FORBIDDEN
# ══════════════════════════════════════════════════════════════════════════════


def test_place_real_broker_order_in_forbidden_context_labels():
    assert "place_real_broker_order" in FORBIDDEN_CONTEXT_LABELS


def test_no_broker_order_action_type_exists():
    # Confirm there is no ActionType that could place a broker order
    broker_order_values = [
        v for v in ActionType
        if "order" in v.value.lower() or "broker" in v.value.lower()
    ]
    # Only FORBIDDEN itself (which has neither word) — assert the list is empty
    assert broker_order_values == [], (
        f"ActionType must not contain broker-order types: {broker_order_values}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 11. Paper missing exit policy repair is allowed
# ══════════════════════════════════════════════════════════════════════════════


def test_attach_exit_policy_paper_allowed_in_paper_mode(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = _paper_exit_repair_action()
    result = exe.execute(action, reason="paper_position_missing_exit_policy")
    assert not result.blocked
    assert result.stub_not_implemented is True  # Phase 4 wires writer


def test_attach_exit_policy_paper_scope_is_paper_only():
    action = _paper_exit_repair_action()
    assert action.scope is ActionScope.PAPER_ONLY


def test_paper_repair_classifies_correctly():
    clf = SafeActionClassifier()
    obs = clf.classify_raw("paper_position_missing_exit_policy")
    assert obs.classification is ClassificationResult.PAPER_REPAIR
    assert ActionType.ATTACH_EXIT_POLICY_PAPER in obs.proposed_action_types


# ══════════════════════════════════════════════════════════════════════════════
# 12. Paper direction repair is allowed
# ══════════════════════════════════════════════════════════════════════════════


def test_paper_direction_repair_allowed_in_paper_mode(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = _paper_direction_repair_action()
    result = exe.execute(action, reason="paper_position_direction_mismatch")
    assert not result.blocked


def test_paper_direction_repair_classifies_correctly():
    clf = SafeActionClassifier()
    obs = clf.classify_raw("paper_position_direction_mismatch")
    assert obs.classification is ClassificationResult.PAPER_REPAIR
    assert ActionType.UPDATE_PAPER_DIRECTION_FROM_QUANTITY in obs.proposed_action_types


# ══════════════════════════════════════════════════════════════════════════════
# 13. Live direction repair is FORBIDDEN (paper-only scope denied in live modes)
# ══════════════════════════════════════════════════════════════════════════════


def test_live_direction_repair_blocked_in_live_disabled(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.LIVE_DISABLED)
    # Same action type but in LIVE_DISABLED mode
    action = SafeAction.build(
        action_type=ActionType.UPDATE_PAPER_DIRECTION_FROM_QUANTITY,
        scope=ActionScope.PAPER_ONLY,
        mode=TradingMode.LIVE_DISABLED,
        risk_level=RiskLevel.LOW,
        requires_human_approval=False,
        idempotency_key="direction-repair-LIVE-test",
        preconditions=[],
        expected_effect="MUST NOT RUN in live mode",
        rollback_behavior="N/A",
    )
    result = exe.execute(action, reason="test")
    assert result.blocked is True
    assert result.executed is False
    assert "PAPER_ONLY" in result.blocked_reason or "denied" in result.blocked_reason.lower()


@pytest.mark.parametrize("mode", [
    TradingMode.LIVE_DISABLED,
    TradingMode.LIVE_STAGE_1,
    TradingMode.LIVE_FULL_BLOCKED,
])
def test_paper_repair_actions_denied_in_all_live_modes(tmp_path, mode):
    exe = _executor(tmp_path, mode=mode)
    for action_type in (
        ActionType.ATTACH_EXIT_POLICY_PAPER,
        ActionType.REPAIR_PAPER_ZERO_QTY_OPEN,
        ActionType.UPDATE_PAPER_DIRECTION_FROM_QUANTITY,
        ActionType.FORCE_PAPER_SQUARE_OFF,
    ):
        action = SafeAction.build(
            action_type=action_type,
            scope=ActionScope.PAPER_ONLY,
            mode=mode,
            risk_level=RiskLevel.LOW,
            requires_human_approval=False,
            idempotency_key=f"{action_type.value}-{mode.value}-test",
            preconditions=[],
            expected_effect="test",
            rollback_behavior="N/A",
        )
        result = exe.execute(action)
        assert result.blocked is True, (
            f"{action_type.value} must be blocked in {mode.value}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 14. Duplicate safe action idempotency prevents repeat execution
# ══════════════════════════════════════════════════════════════════════════════


def test_duplicate_action_idempotency_skipped(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = _alert_action(idempotency_key="alert-test-idem-001")

    first = exe.execute(action, reason="first attempt")
    second = exe.execute(action, reason="second attempt — same key")

    assert first.executed is True
    assert second.idempotency_skipped is True
    assert second.executed is False
    # Second result references the first action
    assert first.action_id in second.result_payload.get("prior_action_id", "")


def test_different_idempotency_keys_both_execute(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    a1 = _alert_action(idempotency_key="key-001")
    a2 = _alert_action(idempotency_key="key-002")

    r1 = exe.execute(a1)
    r2 = exe.execute(a2)

    assert r1.executed is True
    assert r2.executed is True
    assert not r2.idempotency_skipped


# ══════════════════════════════════════════════════════════════════════════════
# 15. Every action writes an audit record
# ══════════════════════════════════════════════════════════════════════════════


def test_executed_action_writes_audit_record(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = _alert_action(idempotency_key="audit-exec-001")
    exe.execute(action, reason="test")

    records = _audit(tmp_path).read_all()
    assert len(records) == 1
    assert records[0]["action_type"] == ActionType.SEND_ALERT.value
    assert records[0]["executed"] is True


def test_blocked_action_still_writes_audit_record(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = _forbidden_action()
    exe.execute(action, reason="forbidden test")

    records = _audit(tmp_path).read_all()
    assert len(records) == 1
    assert records[0]["blocked"] is True
    assert records[0]["executed"] is False


def test_forbidden_action_writes_audit_record(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = _forbidden_action()
    exe.execute(action, reason="forbidden test")

    records = _audit(tmp_path).read_all()
    assert any(r["action_type"] == ActionType.FORBIDDEN.value for r in records)
    assert all(not r["executed"] for r in records)


def test_idempotency_skip_writes_audit_record(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = _alert_action(idempotency_key="audit-idem-001")
    exe.execute(action)
    exe.execute(action)  # duplicate

    records = _audit(tmp_path).read_all()
    assert len(records) == 2
    skip_records = [r for r in records if r["idempotency_skipped"]]
    assert len(skip_records) == 1


def test_precondition_failure_writes_audit_record(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = _block_entries_action(idempotency_key="block-precond-fail")
    exe.execute(
        action,
        preconditions_met=False,
        precondition_description="market is closed",
    )

    records = _audit(tmp_path).read_all()
    assert len(records) == 1
    assert records[0]["precondition_failed"] == "market is closed"
    assert records[0]["executed"] is False


def test_multiple_actions_each_write_audit_record(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    for i in range(5):
        action = _alert_action(idempotency_key=f"multi-audit-{i}")
        exe.execute(action, reason=f"reason {i}")

    records = _audit(tmp_path).read_all()
    assert len(records) == 5


# ══════════════════════════════════════════════════════════════════════════════
# Additional policy and classifier coverage
# ══════════════════════════════════════════════════════════════════════════════


def test_policy_paper_allows_risk_reduction():
    policy = _policy(TradingMode.PAPER)
    allowed, _ = policy.is_allowed(ActionType.BLOCK_NEW_ENTRIES, ActionScope.RISK_REDUCTION)
    assert allowed


def test_policy_paper_allows_paper_only():
    policy = _policy(TradingMode.PAPER)
    allowed, _ = policy.is_allowed(
        ActionType.ATTACH_EXIT_POLICY_PAPER, ActionScope.PAPER_ONLY
    )
    assert allowed


def test_policy_backtest_denies_risk_reduction():
    policy = _policy(TradingMode.BACKTEST)
    allowed, reason = policy.is_allowed(
        ActionType.BLOCK_NEW_ENTRIES, ActionScope.RISK_REDUCTION
    )
    assert not allowed


def test_policy_live_full_blocked_only_allows_read_only():
    policy = _policy(TradingMode.LIVE_FULL_BLOCKED)
    allowed, _ = policy.is_allowed(ActionType.READ_RUNTIME_STATE, ActionScope.READ_ONLY)
    assert allowed
    blocked, _ = policy.is_allowed(ActionType.BLOCK_NEW_ENTRIES, ActionScope.RISK_REDUCTION)
    assert not blocked


def test_policy_from_env_defaults_to_live_disabled(monkeypatch):
    monkeypatch.delenv("RISK_PROFILE", raising=False)
    monkeypatch.delenv("QE_EXECUTION_LIVE_TRADING_ENABLED", raising=False)
    policy = SafeActionPolicy.from_env()
    assert policy.mode is TradingMode.LIVE_DISABLED


def test_policy_from_env_paper_when_risk_profile_paper(monkeypatch):
    monkeypatch.setenv("RISK_PROFILE", "paper")
    monkeypatch.delenv("QE_EXECUTION_LIVE_TRADING_ENABLED", raising=False)
    policy = SafeActionPolicy.from_env()
    assert policy.mode is TradingMode.PAPER


def test_policy_from_env_live_stage1(monkeypatch):
    monkeypatch.setenv("RISK_PROFILE", "tiny-live")
    monkeypatch.setenv("QE_EXECUTION_LIVE_TRADING_ENABLED", "true")
    policy = SafeActionPolicy.from_env()
    assert policy.mode is TradingMode.LIVE_STAGE_1


def test_classifier_unknown_code_defaults_to_alert_only():
    clf = SafeActionClassifier()
    obs = clf.classify_finding(code="future.finding.code")
    assert obs.classification is ClassificationResult.ALERT_ONLY
    assert ActionType.SEND_ALERT in obs.proposed_action_types


def test_classifier_suppress_docker_restart_is_always_true():
    clf = SafeActionClassifier()
    for code in ("service.down", "docker.down", "docker.restart_loop", "kafka.unreachable"):
        for subject in ("ai_engine", "execution_engine", "risk_engine", ""):
            obs = clf.classify_finding(code=code, subject=subject)
            assert obs.suppress_docker_restart is True, (
                f"suppress_docker_restart must be True for ({subject}, {code})"
            )


def test_executor_send_alert_fully_implemented(tmp_path):
    alerts = []
    exe = SafeActionExecutor(
        policy=_policy(TradingMode.PAPER),
        audit=_audit(tmp_path),
        notifier=lambda msg: alerts.append(msg) or True,
    )
    action = _alert_action()
    result = exe.execute(action)
    assert result.executed is True
    assert result.stub_not_implemented is False
    assert len(alerts) == 1


def test_executor_generate_runbook_command_returns_string(tmp_path):
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    action = SafeAction.build(
        action_type=ActionType.GENERATE_RUNBOOK_COMMAND,
        scope=ActionScope.LIVE_HUMAN_GATED,
        mode=TradingMode.PAPER,
        risk_level=RiskLevel.LOW,
        requires_human_approval=True,
        idempotency_key="runbook-ai-engine-20260531",
        preconditions=[],
        expected_effect="Returns restart command for human execution",
        rollback_behavior="N/A — advisory only",
        audit_payload={"subject": "ai_engine"},
    )
    result = exe.execute(action)
    assert result.executed is True
    assert "runbook_command" in result.result_payload
    cmd = result.result_payload["runbook_command"]
    assert "ai_engine" in cmd
    assert "docker-compose" in cmd


def test_executor_never_raises_on_any_action_type(tmp_path):
    """The executor must return an ExecutionResult for every action type."""
    exe = _executor(tmp_path, mode=TradingMode.PAPER)
    for action_type in ActionType:
        scope_map = {
            ActionType.FORBIDDEN: ActionScope.FORBIDDEN,
            ActionType.ATTACH_EXIT_POLICY_PAPER: ActionScope.PAPER_ONLY,
            ActionType.REPAIR_PAPER_ZERO_QTY_OPEN: ActionScope.PAPER_ONLY,
            ActionType.UPDATE_PAPER_DIRECTION_FROM_QUANTITY: ActionScope.PAPER_ONLY,
            ActionType.FORCE_PAPER_SQUARE_OFF: ActionScope.PAPER_ONLY,
            ActionType.GENERATE_RUNBOOK_COMMAND: ActionScope.LIVE_HUMAN_GATED,
        }
        scope = scope_map.get(action_type, ActionScope.RISK_REDUCTION)
        action = SafeAction.build(
            action_type=action_type,
            scope=scope,
            mode=TradingMode.PAPER,
            risk_level=RiskLevel.LOW,
            requires_human_approval=(action_type is ActionType.GENERATE_RUNBOOK_COMMAND),
            idempotency_key=f"never-raises-{action_type.value}",
            preconditions=[],
            expected_effect="test",
            rollback_behavior="N/A",
        )
        result = exe.execute(action)  # must not raise
        assert isinstance(result, __import__(
            "execution_engine.safe_actions.safe_action_models",
            fromlist=["ExecutionResult"],
        ).ExecutionResult)


def test_safe_action_to_dict_shape():
    action = _alert_action()
    d = action.to_dict()
    assert set(d.keys()) == {
        "action_id", "action_type", "scope", "mode", "risk_level",
        "requires_human_approval", "idempotency_key", "preconditions",
        "expected_effect", "rollback_behavior", "audit_payload",
    }
    assert d["action_type"] == ActionType.SEND_ALERT.value


def test_execution_result_to_dict_shape(tmp_path):
    exe = _executor(tmp_path)
    action = _alert_action()
    result = exe.execute(action)
    d = result.to_dict()
    assert "action_id" in d
    assert "executed" in d
    assert "blocked" in d
    assert "timestamp" in d


def test_all_forbidden_context_labels_classify_forbidden():
    clf = SafeActionClassifier()
    for label in FORBIDDEN_CONTEXT_LABELS:
        obs = clf.classify_forbidden_context(label)
        assert obs.classification is ClassificationResult.FORBIDDEN, (
            f"'{label}' must classify as FORBIDDEN"
        )
