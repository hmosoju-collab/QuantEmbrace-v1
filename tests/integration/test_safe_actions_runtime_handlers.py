"""Integration tests for Phase 4 safe_actions runtime handlers.

Tests the full cycle:
    classifier → safe action → executor (with dynamo_writer) → DynamoDB state

Uses the same in-memory _FakeDynamo from the unit tests (no moto, no real AWS).
All tests are synchronous and pure.

Scenarios:
    1. Stale LTP → BLOCK_NEW_ENTRIES written, exits continue
    2. Unmanaged live position → ACTIVATE_KILL_SWITCH written
    3. ai_engine DOWN → alert + block entries; no Docker restart; kill switch not activated
    4. Reconciliation mismatch in live → block entries + alert; exits allowed
    5. Executor counter increments correctly
    6. DynamoDB write failure → fail closed, audit written, no crash
    7. Backtest mode → BLOCK_NEW_ENTRIES and ACTIVATE_KILL_SWITCH denied
    8. FORBIDDEN action → never reaches DynamoDB
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from execution_engine.safe_actions import (
    ActionScope,
    ActionType,
    ClassificationResult,
    RiskLevel,
    SafeAction,
    SafeActionAudit,
    SafeActionClassifier,
    SafeActionDynamoWriter,
    SafeActionExecutor,
    SafeActionPolicy,
    TradingMode,
)
from shared.risk_state import ENTRY_BLOCK_PK, ENTRY_BLOCK_SK, KILL_SWITCH_PK, KILL_SWITCH_SK


# ── shared fake ───────────────────────────────────────────────────────────────


class _FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.fail_on: dict[str, Exception] = {}

    def put_item(self, TableName: str, Item: dict[str, Any], **kwargs: Any) -> dict:
        if TableName in self.fail_on:
            raise self.fail_on[TableName]
        pk = Item.get("PK", {}).get("S", "")
        sk = Item.get("SK", {}).get("S", "")
        self.items[(TableName, pk, sk)] = Item
        self.put_calls.append({"TableName": TableName, "Item": Item})
        return {}

    def get_written(self, table: str, pk: str, sk: str) -> dict[str, Any] | None:
        return self.items.get((table, pk, sk))


_TABLE = "quantembrace-paper-risk-state"


def _setup(
    tmp_path: Path,
    mode: TradingMode = TradingMode.PAPER,
    db: _FakeDynamo | None = None,
    fail_dynamo: bool = False,
):
    fake_db = db or _FakeDynamo()
    if fail_dynamo:
        fake_db.fail_on[_TABLE] = RuntimeError("DynamoDB unavailable")
    dynamo_writer = SafeActionDynamoWriter(dynamo_client=fake_db, risk_state_table=_TABLE)
    alerts: list[str] = []
    executor = SafeActionExecutor(
        policy=SafeActionPolicy(mode),
        audit=SafeActionAudit(path=str(tmp_path / "audit.jsonl")),
        notifier=lambda msg: alerts.append(msg) or True,
        dynamo_writer=dynamo_writer,
    )
    clf = SafeActionClassifier()
    return executor, clf, fake_db, alerts


def _build_action(action_type: ActionType, mode: TradingMode, idem_key: str) -> SafeAction:
    scope_map = {
        ActionType.BLOCK_NEW_ENTRIES: ActionScope.RISK_REDUCTION,
        ActionType.ACTIVATE_KILL_SWITCH: ActionScope.RISK_REDUCTION,
        ActionType.SEND_ALERT: ActionScope.READ_ONLY,
        ActionType.FORBIDDEN: ActionScope.FORBIDDEN,
    }
    return SafeAction.build(
        action_type=action_type,
        scope=scope_map.get(action_type, ActionScope.RISK_REDUCTION),
        mode=mode,
        risk_level=RiskLevel.LOW if action_type is ActionType.SEND_ALERT else RiskLevel.HIGH,
        requires_human_approval=False,
        idempotency_key=idem_key,
        preconditions=[],
        expected_effect=f"Execute {action_type.value}",
        rollback_behavior="N/A",
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. Stale LTP → BLOCK_NEW_ENTRIES written, exits continue
# ══════════════════════════════════════════════════════════════════════════════


def test_stale_ltp_block_new_entries_written(tmp_path):
    executor, clf, db, _ = _setup(tmp_path, mode=TradingMode.PAPER)

    obs = clf.classify_finding(code="broker.feed_very_stale", subject="feed")
    assert ClassificationResult.BLOCK_ENTRIES == obs.classification
    assert ActionType.BLOCK_NEW_ENTRIES in obs.proposed_action_types

    action = _build_action(ActionType.BLOCK_NEW_ENTRIES, TradingMode.PAPER, "blk-stale-001")
    result = executor.execute(action, reason="stale_ltp_market_hours")

    assert result.executed is True
    eb = db.get_written(_TABLE, ENTRY_BLOCK_PK, ENTRY_BLOCK_SK)
    assert eb is not None
    assert eb["blocked"] == {"BOOL": True}


def test_stale_ltp_does_not_write_kill_switch(tmp_path):
    executor, clf, db, _ = _setup(tmp_path, mode=TradingMode.PAPER)
    action = _build_action(ActionType.BLOCK_NEW_ENTRIES, TradingMode.PAPER, "blk-stale-002")
    executor.execute(action, reason="stale_ltp")

    ks = db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)
    assert ks is None, "Stale LTP block must not activate kill switch"


def test_stale_ltp_does_not_touch_exit_management_keys(tmp_path):
    executor, clf, db, _ = _setup(tmp_path, mode=TradingMode.PAPER)
    action = _build_action(ActionType.BLOCK_NEW_ENTRIES, TradingMode.PAPER, "blk-stale-003")
    executor.execute(action, reason="stale_ltp")

    written_pks = {v["PK"]["S"] for v in db.items.values()}
    assert written_pks == {ENTRY_BLOCK_PK}
    for pk in written_pks:
        assert "ORDER" not in pk and "POSITION" not in pk and "TEE" not in pk


# ══════════════════════════════════════════════════════════════════════════════
# 2. Unmanaged live position → ACTIVATE_KILL_SWITCH written
# ══════════════════════════════════════════════════════════════════════════════


def test_unmanaged_live_position_activates_kill_switch(tmp_path):
    executor, clf, db, _ = _setup(tmp_path, mode=TradingMode.PAPER)

    obs = clf.classify_raw("unmanaged_live_position")
    assert ClassificationResult.KILL_SWITCH == obs.classification
    assert ActionType.ACTIVATE_KILL_SWITCH in obs.proposed_action_types

    action = _build_action(ActionType.ACTIVATE_KILL_SWITCH, TradingMode.PAPER, "ks-unmanaged-001")
    result = executor.execute(action, reason="unmanaged_live_position")

    assert result.executed is True
    ks = db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)
    assert ks is not None
    assert ks["active"] == {"BOOL": True}
    assert ks["status"]["S"] == "ACTIVE"


def test_kill_switch_write_never_deactivates(tmp_path):
    executor, _, db, _ = _setup(tmp_path, mode=TradingMode.PAPER)
    action = _build_action(ActionType.ACTIVATE_KILL_SWITCH, TradingMode.PAPER, "ks-deact-002")
    executor.execute(action, reason="test")

    ks = db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)
    # Must be active, never inactive
    assert ks["active"] == {"BOOL": True}
    assert "deactivated_at" not in ks


def test_kill_switch_counter_increments(tmp_path):
    executor, _, _, _ = _setup(tmp_path, mode=TradingMode.PAPER)
    action = _build_action(ActionType.ACTIVATE_KILL_SWITCH, TradingMode.PAPER, "ks-cnt-003")
    executor.execute(action, reason="counter_test")
    assert executor.counters["kill_switch_activated_total"] == 1


def test_kill_switch_emits_audit_record(tmp_path):
    executor, _, _, _ = _setup(tmp_path, mode=TradingMode.PAPER)
    action = _build_action(ActionType.ACTIVATE_KILL_SWITCH, TradingMode.PAPER, "ks-audit-004")
    executor.execute(action, reason="unmanaged_position")

    records = SafeActionAudit(path=str(tmp_path / "audit.jsonl")).read_all()
    ks_records = [r for r in records if r["action_type"] == "ACTIVATE_KILL_SWITCH"]
    assert len(ks_records) == 1
    assert ks_records[0]["executed"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 3. ai_engine DOWN → alert + block entries; NO Docker restart; NO kill switch
# ══════════════════════════════════════════════════════════════════════════════


def test_ai_engine_down_no_kill_switch_activation(tmp_path):
    executor, clf, db, alerts = _setup(tmp_path, mode=TradingMode.PAPER)

    obs = clf.classify_finding(code="service.down", subject="ai_engine")
    assert ClassificationResult.ALERT_ONLY == obs.classification
    assert ActionType.ACTIVATE_KILL_SWITCH not in obs.proposed_action_types

    # Execute the proposed actions
    for i, at in enumerate(obs.proposed_action_types):
        action = _build_action(at, TradingMode.PAPER, f"ai-down-{i}")
        executor.execute(action, reason="ai_engine_down")

    ks = db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)
    assert ks is None, "ai_engine DOWN must not activate kill switch"


def test_ai_engine_down_suppress_docker_restart_is_always_true(tmp_path):
    _, clf, _, _ = _setup(tmp_path)
    obs = clf.classify_finding(code="service.down", subject="ai_engine")
    assert obs.suppress_docker_restart is True

    # Also verify for docker.down and docker.restart_loop on ai_engine
    obs2 = clf.classify_finding(code="docker.down", subject="ai_engine")
    obs3 = clf.classify_finding(code="docker.restart_loop", subject="ai_engine")
    assert obs2.suppress_docker_restart is True
    assert obs3.suppress_docker_restart is True


def test_ai_engine_down_block_entries_written(tmp_path):
    executor, clf, db, _ = _setup(tmp_path, mode=TradingMode.PAPER)
    obs = clf.classify_finding(code="service.down", subject="ai_engine")
    assert ActionType.BLOCK_NEW_ENTRIES in obs.proposed_action_types

    action = _build_action(ActionType.BLOCK_NEW_ENTRIES, TradingMode.PAPER, "ai-blk-001")
    result = executor.execute(action, reason="ai_engine_down")
    assert result.executed is True
    eb = db.get_written(_TABLE, ENTRY_BLOCK_PK, ENTRY_BLOCK_SK)
    assert eb is not None


# ══════════════════════════════════════════════════════════════════════════════
# 4. Reconciliation mismatch in live → block entries + alert; exits allowed
# ══════════════════════════════════════════════════════════════════════════════


def test_reconciliation_required_live_block_entries_written(tmp_path):
    executor, clf, db, _ = _setup(tmp_path, mode=TradingMode.LIVE_DISABLED)

    obs = clf.classify_raw("reconciliation_required_live")
    assert ActionType.BLOCK_NEW_ENTRIES in obs.proposed_action_types

    action = _build_action(
        ActionType.BLOCK_NEW_ENTRIES, TradingMode.LIVE_DISABLED, "recon-blk-001"
    )
    result = executor.execute(action, reason="reconciliation_required")
    assert result.executed is True

    eb = db.get_written(_TABLE, ENTRY_BLOCK_PK, ENTRY_BLOCK_SK)
    assert eb is not None
    assert eb["blocked"] == {"BOOL": True}


def test_reconciliation_block_does_not_touch_kill_switch(tmp_path):
    executor, clf, db, _ = _setup(tmp_path, mode=TradingMode.LIVE_DISABLED)
    action = _build_action(
        ActionType.BLOCK_NEW_ENTRIES, TradingMode.LIVE_DISABLED, "recon-blk-002"
    )
    executor.execute(action, reason="reconciliation_required")
    assert db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK) is None


# ══════════════════════════════════════════════════════════════════════════════
# 5. Executor counters increment correctly
# ══════════════════════════════════════════════════════════════════════════════


def test_executor_counters_block_new_entries(tmp_path):
    executor, _, _, _ = _setup(tmp_path)
    action = _build_action(ActionType.BLOCK_NEW_ENTRIES, TradingMode.PAPER, "cnt-blk-001")
    executor.execute(action)
    assert executor.counters["block_new_entries_total"] == 1
    assert executor.counters["kill_switch_activated_total"] == 0


def test_executor_counters_kill_switch(tmp_path):
    executor, _, _, _ = _setup(tmp_path)
    action = _build_action(ActionType.ACTIVATE_KILL_SWITCH, TradingMode.PAPER, "cnt-ks-001")
    executor.execute(action)
    assert executor.counters["kill_switch_activated_total"] == 1
    assert executor.counters["block_new_entries_total"] == 0


def test_executor_counter_idempotency_skipped(tmp_path):
    executor, _, _, _ = _setup(tmp_path)
    action = _build_action(ActionType.BLOCK_NEW_ENTRIES, TradingMode.PAPER, "cnt-idem-001")
    executor.execute(action)
    executor.execute(action)  # duplicate
    assert executor.counters["idempotency_skipped_total"] == 1


def test_executor_counter_action_blocked(tmp_path):
    executor, _, _, _ = _setup(tmp_path, mode=TradingMode.BACKTEST)
    action = _build_action(ActionType.BLOCK_NEW_ENTRIES, TradingMode.BACKTEST, "cnt-blocked-001")
    executor.execute(action)
    assert executor.counters["action_blocked_total"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# 6. DynamoDB write failure → fail closed, audit written, no crash
# ══════════════════════════════════════════════════════════════════════════════


def test_block_new_entries_dynamo_failure_fails_closed(tmp_path):
    executor, _, db, _ = _setup(tmp_path, mode=TradingMode.PAPER, fail_dynamo=True)
    action = _build_action(ActionType.BLOCK_NEW_ENTRIES, TradingMode.PAPER, "fail-blk-001")
    result = executor.execute(action, reason="stale_ltp")

    assert result.executed is False
    assert result.error is not None
    assert executor.counters["dynamo_write_failed_total"] == 1


def test_kill_switch_dynamo_failure_fails_closed(tmp_path):
    executor, _, db, _ = _setup(tmp_path, mode=TradingMode.PAPER, fail_dynamo=True)
    action = _build_action(ActionType.ACTIVATE_KILL_SWITCH, TradingMode.PAPER, "fail-ks-001")
    result = executor.execute(action, reason="unmanaged_position")

    assert result.executed is False
    assert executor.counters["dynamo_write_failed_total"] == 1


def test_dynamo_failure_writes_audit_record(tmp_path):
    executor, _, db, _ = _setup(tmp_path, mode=TradingMode.PAPER, fail_dynamo=True)
    action = _build_action(ActionType.BLOCK_NEW_ENTRIES, TradingMode.PAPER, "fail-audit-001")
    executor.execute(action, reason="test")

    records = SafeActionAudit(path=str(tmp_path / "audit.jsonl")).read_all()
    assert len(records) == 1
    assert records[0]["executed"] is False
    assert records[0]["error"] is not None


def test_executor_never_raises_on_dynamo_failure(tmp_path):
    executor, _, _, _ = _setup(tmp_path, mode=TradingMode.PAPER, fail_dynamo=True)
    for at in (ActionType.BLOCK_NEW_ENTRIES, ActionType.ACTIVATE_KILL_SWITCH):
        action = _build_action(at, TradingMode.PAPER, f"no-raise-{at.value}")
        result = executor.execute(action)  # must not raise
        assert result is not None


# ══════════════════════════════════════════════════════════════════════════════
# 7. BACKTEST mode → BLOCK_NEW_ENTRIES and ACTIVATE_KILL_SWITCH denied
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("action_type", [
    ActionType.BLOCK_NEW_ENTRIES,
    ActionType.ACTIVATE_KILL_SWITCH,
])
def test_backtest_mode_denies_risk_reducing_actions(tmp_path, action_type):
    executor, _, db, _ = _setup(tmp_path, mode=TradingMode.BACKTEST)
    action = _build_action(action_type, TradingMode.BACKTEST, f"bt-{action_type.value}")
    result = executor.execute(action)

    assert result.blocked is True
    assert result.executed is False
    # Nothing written to DynamoDB
    assert len(db.put_calls) == 0


def test_backtest_mode_audit_written_for_denied(tmp_path):
    executor, _, _, _ = _setup(tmp_path, mode=TradingMode.BACKTEST)
    action = _build_action(ActionType.BLOCK_NEW_ENTRIES, TradingMode.BACKTEST, "bt-audit-001")
    executor.execute(action)

    records = SafeActionAudit(path=str(tmp_path / "audit.jsonl")).read_all()
    assert len(records) == 1
    assert records[0]["blocked"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 8. FORBIDDEN action → never reaches DynamoDB
# ══════════════════════════════════════════════════════════════════════════════


def test_forbidden_action_never_writes_dynamodb(tmp_path):
    executor, _, db, _ = _setup(tmp_path, mode=TradingMode.PAPER)
    action = SafeAction.build(
        action_type=ActionType.FORBIDDEN,
        scope=ActionScope.FORBIDDEN,
        mode=TradingMode.PAPER,
        risk_level=RiskLevel.HIGH,
        requires_human_approval=False,
        idempotency_key="forbidden-test",
        preconditions=[],
        expected_effect="MUST NOT EXECUTE",
        rollback_behavior="N/A",
    )
    result = executor.execute(action)
    assert result.blocked is True
    assert result.executed is False
    assert len(db.put_calls) == 0


def test_forbidden_action_writes_audit_record(tmp_path):
    executor, _, _, _ = _setup(tmp_path, mode=TradingMode.PAPER)
    action = SafeAction.build(
        action_type=ActionType.FORBIDDEN,
        scope=ActionScope.FORBIDDEN,
        mode=TradingMode.PAPER,
        risk_level=RiskLevel.HIGH,
        requires_human_approval=False,
        idempotency_key="forbidden-audit-001",
        preconditions=[],
        expected_effect="test",
        rollback_behavior="N/A",
    )
    executor.execute(action)
    records = SafeActionAudit(path=str(tmp_path / "audit.jsonl")).read_all()
    assert any(r["action_type"] == "FORBIDDEN" and r["blocked"] for r in records)


# ══════════════════════════════════════════════════════════════════════════════
# 9. No dynamo_writer injected → falls back to stub gracefully
# ══════════════════════════════════════════════════════════════════════════════


def test_no_dynamo_writer_falls_back_to_stub(tmp_path):
    """Without dynamo_writer the executor returns stub_not_implemented, never raises."""
    executor = SafeActionExecutor(
        policy=SafeActionPolicy(TradingMode.PAPER),
        audit=SafeActionAudit(path=str(tmp_path / "audit.jsonl")),
        dynamo_writer=None,
    )
    for at in (ActionType.BLOCK_NEW_ENTRIES, ActionType.ACTIVATE_KILL_SWITCH):
        action = _build_action(at, TradingMode.PAPER, f"no-writer-{at.value}")
        result = executor.execute(action)
        assert result.stub_not_implemented is True
        assert result.executed is False
