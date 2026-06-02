"""Unit tests for DurableIdempotencyStore — Phase 6.

Verifies:
- Persistent idempotency key survives executor restart (mark → new executor → check).
- BLOCK_NEW_ENTRIES durable idempotency prevents repeat execution.
- ACTIVATE_KILL_SWITCH durable idempotency prevents repeat execution.
- Idempotency skip is audited with source=dynamo.
- ConditionalCheckFailed treated as already-marked (duplicate mark returns False).
- DynamoDB read failure treated as not-found (allows execution attempt).
- SafeActionExecutor wires durable store: check before dispatch, mark after success.

All tests are synchronous, pure in-memory — no boto3 required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from execution_engine.safe_actions.safe_action_idempotency_store import DurableIdempotencyStore
from execution_engine.safe_actions import (
    ActionScope,
    ActionType,
    RiskLevel,
    SafeAction,
    SafeActionAudit,
    SafeActionExecutor,
    SafeActionPolicy,
    TradingMode,
)
from execution_engine.safe_actions.safe_action_dynamo_writer import SafeActionDynamoWriter


# ── FakeDynamo ────────────────────────────────────────────────────────────────


class _FakeDynamo:
    """Minimal in-memory DynamoDB stub for idempotency store tests."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.put_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.fail_get: bool = False
        self.fail_put: bool = False

    def get_item(self, **kwargs: Any) -> dict:
        self.get_calls.append(kwargs)
        if self.fail_get:
            raise RuntimeError("DynamoDB get_item unavailable")
        key = kwargs["Key"]
        pk = key["PK"]["S"]
        sk = key["SK"]["S"]
        item = self.items.get((pk, sk))
        return {"Item": item} if item else {}

    def put_item(self, **kwargs: Any) -> dict:
        self.put_calls.append(kwargs)
        if self.fail_put:
            raise RuntimeError("DynamoDB put_item unavailable")
        item = kwargs["Item"]
        pk = item["PK"]["S"]
        sk = item["SK"]["S"]
        condition = kwargs.get("ConditionExpression", "")
        if "attribute_not_exists" in condition and (pk, sk) in self.items:
            raise _ConditionalCheckFailed("ConditionalCheckFailedException")
        self.items[(pk, sk)] = item
        return {}


class _ConditionalCheckFailed(Exception):
    pass


# ── helpers ───────────────────────────────────────────────────────────────────


def _store(dynamo: _FakeDynamo) -> DurableIdempotencyStore:
    return DurableIdempotencyStore(dynamo_client=dynamo, risk_state_table="qe-risk-state")


def _audit(tmp_path: Path) -> SafeActionAudit:
    return SafeActionAudit(path=str(tmp_path / "audit.jsonl"))


def _writer(dynamo: _FakeDynamo) -> SafeActionDynamoWriter:
    return SafeActionDynamoWriter(dynamo_client=dynamo, risk_state_table="qe-risk-state")


def _block_entries_action(key: str = "bne-test-20260531") -> SafeAction:
    return SafeAction.build(
        action_type=ActionType.BLOCK_NEW_ENTRIES,
        scope=ActionScope.RISK_REDUCTION,
        mode=TradingMode.PAPER,
        risk_level=RiskLevel.LOW,
        requires_human_approval=False,
        idempotency_key=key,
        preconditions=[],
        expected_effect="Block new entries",
        rollback_behavior="clear ENTRY_BLOCK",
        audit_payload={"reason": "test"},
    )


def _ks_action(key: str = "ks-test-20260531") -> SafeAction:
    return SafeAction.build(
        action_type=ActionType.ACTIVATE_KILL_SWITCH,
        scope=ActionScope.RISK_REDUCTION,
        mode=TradingMode.PAPER,
        risk_level=RiskLevel.HIGH,
        requires_human_approval=False,
        idempotency_key=key,
        preconditions=[],
        expected_effect="Activate kill switch",
        rollback_behavior="deactivate kill switch",
        audit_payload={"reason": "test"},
    )


def _executor(tmp_path: Path, dynamo: _FakeDynamo) -> SafeActionExecutor:
    return SafeActionExecutor(
        policy=SafeActionPolicy(TradingMode.PAPER),
        audit=_audit(tmp_path),
        dynamo_writer=_writer(dynamo),
        durable_idempotency=_store(dynamo),
        requested_by="test",
    )


# ── DurableIdempotencyStore unit tests ───────────────────────────────────────


class TestDurableIdempotencyStore:
    def test_check_returns_not_found_for_new_key(self):
        dynamo = _FakeDynamo()
        store = _store(dynamo)
        found, item = store.check("new-key-abc")
        assert not found
        assert item is None

    def test_mark_writes_to_dynamo(self):
        dynamo = _FakeDynamo()
        store = _store(dynamo)
        result = store.mark(
            "block-entries-test",
            action_id="act-001",
            action_type="BLOCK_NEW_ENTRIES",
            mode="paper",
        )
        assert result is True
        assert len(dynamo.put_calls) == 1

    def test_check_returns_found_after_mark(self):
        dynamo = _FakeDynamo()
        store = _store(dynamo)
        store.mark("block-entries-xyz", action_id="act-001", action_type="BLOCK_NEW_ENTRIES", mode="paper")
        found, item = store.check("block-entries-xyz")
        assert found is True
        assert item is not None

    def test_survives_executor_restart(self):
        """Simulates restart: new store instance same DynamoDB — key still found."""
        dynamo = _FakeDynamo()
        # "First executor session" marks the key
        store_session1 = _store(dynamo)
        store_session1.mark("bne-restart-test", action_id="act-100", action_type="BLOCK_NEW_ENTRIES", mode="paper")

        # "Restarted executor" creates new store with same dynamo
        store_session2 = _store(dynamo)
        found, existing = store_session2.check("bne-restart-test")
        assert found is True
        assert existing is not None

    def test_conditional_check_failed_treated_as_already_marked(self):
        """ConditionalCheckFailed during mark → returns False (duplicate race)."""
        dynamo = _FakeDynamo()
        store = _store(dynamo)
        store.mark("dup-key", action_id="act-001", action_type="BLOCK_NEW_ENTRIES", mode="paper")
        # Second mark on same key → ConditionalCheckFailed
        result = store.mark("dup-key", action_id="act-002", action_type="BLOCK_NEW_ENTRIES", mode="paper")
        assert result is False

    def test_get_failure_returns_not_found(self):
        """DynamoDB read failure → (False, None) so execution is attempted."""
        dynamo = _FakeDynamo()
        dynamo.fail_get = True
        store = _store(dynamo)
        found, item = store.check("any-key")
        assert not found
        assert item is None

    def test_mark_failure_returns_false(self):
        dynamo = _FakeDynamo()
        dynamo.fail_put = True
        store = _store(dynamo)
        result = store.mark("fail-key", action_id="act-001", action_type="BLOCK_NEW_ENTRIES", mode="paper")
        assert result is False

    def test_result_dict_truncated_to_256_chars(self):
        dynamo = _FakeDynamo()
        store = _store(dynamo)
        big_result = {"key": "x" * 1000}
        store.mark("big-result-key", action_id="act-001", action_type="BLOCK_NEW_ENTRIES",
                   mode="paper", result_dict=big_result)
        pk = "SAFE_ACTION_IDEMPOTENCY"
        item = dynamo.items[("SAFE_ACTION_IDEMPOTENCY", "big-result-key")]
        result_str = item["result"]["S"]
        assert len(result_str) <= 256


# ── SafeActionExecutor durable idempotency integration ───────────────────────


class TestExecutorDurableIdempotency:
    def test_block_new_entries_marks_durable_store(self, tmp_path):
        dynamo = _FakeDynamo()
        ex = _executor(tmp_path, dynamo)
        action = _block_entries_action("bne-exec-001")
        result = ex.execute(action, reason="test")
        assert result.executed is True
        # Durable store should have been marked
        store = _store(dynamo)
        found, _ = store.check("bne-exec-001")
        assert found is True

    def test_activate_kill_switch_marks_durable_store(self, tmp_path):
        dynamo = _FakeDynamo()
        ex = _executor(tmp_path, dynamo)
        action = _ks_action("ks-exec-001")
        result = ex.execute(action, reason="test")
        assert result.executed is True
        store = _store(dynamo)
        found, _ = store.check("ks-exec-001")
        assert found is True

    def test_durable_idempotency_prevents_repeat_after_restart(self, tmp_path):
        """Executor 2 (restart) sees key already in dynamo → skips execution."""
        dynamo = _FakeDynamo()
        # Executor 1 executes the action
        ex1 = _executor(tmp_path, dynamo)
        action = _block_entries_action("bne-restart-002")
        r1 = ex1.execute(action, reason="first execution")
        assert r1.executed is True

        # Executor 2 — new instance (restart), same dynamo
        ex2 = _executor(tmp_path, dynamo)
        action2 = _block_entries_action("bne-restart-002")
        r2 = ex2.execute(action2, reason="second attempt after restart")
        assert r2.executed is False
        assert r2.idempotency_skipped is True
        assert "durable_idempotency" in (r2.blocked_reason or "")

    def test_durable_idempotency_skip_audited(self, tmp_path):
        """Durable idempotency skip writes an audit record."""
        dynamo = _FakeDynamo()
        ex1 = _executor(tmp_path, dynamo)
        ex1.execute(_block_entries_action("bne-audit-003"), reason="first")

        audit = _audit(tmp_path)
        ex2 = SafeActionExecutor(
            policy=SafeActionPolicy(TradingMode.PAPER),
            audit=audit,
            dynamo_writer=_writer(dynamo),
            durable_idempotency=_store(dynamo),
        )
        ex2.execute(_block_entries_action("bne-audit-003"), reason="second")
        records = audit.read_all()
        skips = [r for r in records if r.get("idempotency_skipped")]
        assert len(skips) >= 1
        assert any("durable" in (r.get("blocked_reason") or "") for r in skips)

    def test_in_memory_idempotency_still_works_without_durable_store(self, tmp_path):
        """Without durable_idempotency, in-memory gate still deduplicates within session."""
        dynamo = _FakeDynamo()
        ex = SafeActionExecutor(
            policy=SafeActionPolicy(TradingMode.PAPER),
            audit=_audit(tmp_path),
            dynamo_writer=_writer(dynamo),
            durable_idempotency=None,  # no durable store
        )
        action = _block_entries_action("mem-only-key")
        r1 = ex.execute(action, reason="first")
        assert r1.executed is True

        r2 = ex.execute(_block_entries_action("mem-only-key"), reason="second")
        assert r2.executed is False
        assert r2.idempotency_skipped is True

    def test_kill_switch_durable_idempotency(self, tmp_path):
        """ACTIVATE_KILL_SWITCH repeated after restart is blocked by durable store."""
        dynamo = _FakeDynamo()
        ex1 = _executor(tmp_path, dynamo)
        r1 = ex1.execute(_ks_action("ks-restart-test"), reason="first")
        assert r1.executed is True

        ex2 = _executor(tmp_path, dynamo)
        r2 = ex2.execute(_ks_action("ks-restart-test"), reason="second attempt")
        assert r2.executed is False
        assert r2.idempotency_skipped is True
