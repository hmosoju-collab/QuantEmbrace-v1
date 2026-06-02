"""Unit tests for SafeActionDynamoWriter — Phase 4 BLOCK_NEW_ENTRIES and ACTIVATE_KILL_SWITCH.

Uses a pure in-memory fake DynamoDB client; no moto, no boto3 required.
All tests are synchronous and pure.
"""

from __future__ import annotations

from typing import Any

import pytest

from execution_engine.safe_actions.safe_action_dynamo_writer import (
    SafeActionDynamoWriter,
    WriteResult,
)
from shared.risk_state import (
    ENTRY_BLOCK_PK,
    ENTRY_BLOCK_SK,
    KILL_SWITCH_PK,
    KILL_SWITCH_SK,
)


# ── fake DynamoDB client ──────────────────────────────────────────────────────


class _FakeDynamo:
    """In-memory fake for the boto3 low-level DynamoDB client."""

    def __init__(self) -> None:
        # (table, pk, sk) → item dict
        self.items: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.put_calls: list[dict[str, Any]] = []
        # Map table name → exception to simulate failures
        self.fail_on: dict[str, Exception] = {}

    def put_item(self, TableName: str, Item: dict[str, Any], **kwargs: Any) -> dict:
        if TableName in self.fail_on:
            raise self.fail_on[TableName]
        pk = Item.get("PK", {}).get("S", "")
        sk = Item.get("SK", {}).get("S", "")
        self.items[(TableName, pk, sk)] = Item
        self.put_calls.append({"TableName": TableName, "Item": Item})
        return {}

    def get_item(self, TableName: str, Key: dict[str, Any], **kwargs: Any) -> dict:
        pk = Key.get("PK", {}).get("S", "")
        sk = Key.get("SK", {}).get("S", "")
        item = self.items.get((TableName, pk, sk))
        return {"Item": item} if item else {}

    def get_written(self, table: str, pk: str, sk: str) -> dict[str, Any] | None:
        return self.items.get((table, pk, sk))


_TABLE = "quantembrace-paper-risk-state"


def _writer(dynamo: _FakeDynamo | None = None) -> tuple[SafeActionDynamoWriter, _FakeDynamo]:
    db = dynamo or _FakeDynamo()
    return SafeActionDynamoWriter(dynamo_client=db, risk_state_table=_TABLE), db


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK_NEW_ENTRIES — DynamoDB key and schema
# ══════════════════════════════════════════════════════════════════════════════


def test_block_new_entries_writes_correct_pk_sk():
    w, db = _writer()
    result = w.write_block_new_entries(
        action_id="act-001",
        idempotency_key="block-001",
        reason="stale_ltp",
    )
    assert result.success is True
    assert result.pk == ENTRY_BLOCK_PK
    assert result.sk == ENTRY_BLOCK_SK
    item = db.get_written(_TABLE, ENTRY_BLOCK_PK, ENTRY_BLOCK_SK)
    assert item is not None


def test_block_new_entries_item_schema():
    w, db = _writer()
    w.write_block_new_entries(
        action_id="act-002",
        idempotency_key="block-002",
        reason="stale_ltp",
        source="safe_actions_test",
    )
    item = db.get_written(_TABLE, ENTRY_BLOCK_PK, ENTRY_BLOCK_SK)
    # blocked=True
    assert item["blocked"] == {"BOOL": True}
    assert item["status"] == {"S": "BLOCKED"}
    # required fields present
    assert item["reason"]["S"] == "stale_ltp"
    assert item["source"]["S"] == "safe_actions_test"
    assert item["action_id"]["S"] == "act-002"
    assert item["idempotency_key"]["S"] == "block-002"
    assert "created_at" in item
    assert item["schema_version"]["S"] == "1.0"


def test_block_new_entries_is_idempotent():
    """Calling write_block_new_entries twice leaves exactly one item in the table."""
    w, db = _writer()
    r1 = w.write_block_new_entries(action_id="act-003a", idempotency_key="blk-003", reason="r")
    r2 = w.write_block_new_entries(action_id="act-003b", idempotency_key="blk-003", reason="r")
    assert r1.success is True
    assert r2.success is True
    assert len(db.put_calls) == 2  # two writes
    # but only one item in the table (second overwrites first with same blocked=True)
    item = db.get_written(_TABLE, ENTRY_BLOCK_PK, ENTRY_BLOCK_SK)
    assert item["blocked"] == {"BOOL": True}


def test_block_new_entries_does_not_write_kill_switch_key():
    w, db = _writer()
    w.write_block_new_entries(action_id="act-004", idempotency_key="blk-004", reason="r")
    ks_item = db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)
    assert ks_item is None, "BLOCK_NEW_ENTRIES must not touch the kill switch key"


def test_block_new_entries_does_not_write_positions_orders_tee():
    """BLOCK_NEW_ENTRIES must not touch any exit-management keys."""
    w, db = _writer()
    w.write_block_new_entries(action_id="act-005", idempotency_key="blk-005", reason="r")
    written_pks = {v["PK"]["S"] for v in db.items.values()}
    # Only the ENTRY_BLOCK key should have been written
    assert written_pks == {ENTRY_BLOCK_PK}
    # No position, order, TEE, or NAV keys
    for pk in written_pks:
        assert "POSITION" not in pk
        assert "ORDER" not in pk
        assert "NAV" not in pk
        assert "TEE" not in pk
        assert "MIS" not in pk


def test_block_new_entries_does_not_set_live_trading_enabled():
    w, db = _writer()
    w.write_block_new_entries(action_id="act-006", idempotency_key="blk-006", reason="r")
    item = db.get_written(_TABLE, ENTRY_BLOCK_PK, ENTRY_BLOCK_SK)
    assert "live_trading_enabled" not in item
    assert "capital" not in item
    assert "nav_limit" not in item


def test_block_new_entries_failure_returns_failed_write_result():
    _, db = _writer()
    db.fail_on[_TABLE] = RuntimeError("ProvisionedThroughputExceededException")
    w = SafeActionDynamoWriter(dynamo_client=db, risk_state_table=_TABLE)
    result = w.write_block_new_entries(action_id="act-007", idempotency_key="blk-007", reason="r")
    assert result.success is False
    assert result.error_type == "RuntimeError"


# ══════════════════════════════════════════════════════════════════════════════
# ACTIVATE_KILL_SWITCH — DynamoDB key and schema
# ══════════════════════════════════════════════════════════════════════════════


def test_kill_switch_writes_correct_pk_sk():
    w, db = _writer()
    result = w.write_kill_switch_active(
        action_id="ks-001",
        idempotency_key="ks-idem-001",
        reason="unmanaged_live_position",
    )
    assert result.success is True
    assert result.pk == KILL_SWITCH_PK
    assert result.sk == KILL_SWITCH_SK
    item = db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)
    assert item is not None


def test_kill_switch_item_schema():
    w, db = _writer()
    w.write_kill_switch_active(
        action_id="ks-002",
        idempotency_key="ks-idem-002",
        reason="test_reason",
        activated_by="safe_actions_test",
    )
    item = db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)
    # active = True
    assert item["active"] == {"BOOL": True}
    assert item["status"]["S"] == "ACTIVE"
    # canonical fields from kill_switch_item()
    assert item["reason"]["S"] == "test_reason"
    assert item["activated_by"]["S"] == "safe_actions_test"
    assert "activated_at" in item
    assert "updated_at" in item
    assert item["schema_version"]["S"] == "1.0"
    # source info embedded in detail
    assert "ks-002" in item["detail"]["S"]
    assert "ks-idem-002" in item["detail"]["S"]


def test_kill_switch_is_idempotent():
    """Calling write_kill_switch_active twice always results in active=True."""
    w, db = _writer()
    r1 = w.write_kill_switch_active(action_id="ks-003a", idempotency_key="ks-003", reason="r")
    r2 = w.write_kill_switch_active(action_id="ks-003b", idempotency_key="ks-003", reason="r")
    assert r1.success and r2.success
    item = db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)
    assert item["active"] == {"BOOL": True}


def test_kill_switch_never_writes_active_false():
    """The writer can only set active=True; it never deactivates the kill switch."""
    w, db = _writer()
    w.write_kill_switch_active(action_id="ks-004", idempotency_key="ks-004", reason="r")
    item = db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)
    # The boolean value must be True — never False
    assert item["active"] == {"BOOL": True}
    assert item["status"]["S"] == "ACTIVE"
    # No deactivated_at field (would only appear on deactivation)
    assert "deactivated_at" not in item


def test_kill_switch_does_not_write_entry_block_key():
    w, db = _writer()
    w.write_kill_switch_active(action_id="ks-005", idempotency_key="ks-005", reason="r")
    eb_item = db.get_written(_TABLE, ENTRY_BLOCK_PK, ENTRY_BLOCK_SK)
    assert eb_item is None, "ACTIVATE_KILL_SWITCH must not touch the entry-block key"


def test_kill_switch_does_not_set_live_trading_enabled():
    w, db = _writer()
    w.write_kill_switch_active(action_id="ks-006", idempotency_key="ks-006", reason="r")
    item = db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)
    assert "live_trading_enabled" not in item
    assert "capital" not in item


def test_kill_switch_failure_returns_failed_write_result():
    _, db = _writer()
    db.fail_on[_TABLE] = ConnectionError("Network unreachable")
    w = SafeActionDynamoWriter(dynamo_client=db, risk_state_table=_TABLE)
    result = w.write_kill_switch_active(action_id="ks-007", idempotency_key="ks-007", reason="r")
    assert result.success is False
    assert result.error_type == "ConnectionError"


# ══════════════════════════════════════════════════════════════════════════════
# Cross-action isolation
# ══════════════════════════════════════════════════════════════════════════════


def test_both_actions_write_to_different_keys_in_same_table():
    w, db = _writer()
    w.write_block_new_entries(action_id="x-001", idempotency_key="xk-001", reason="r")
    w.write_kill_switch_active(action_id="x-002", idempotency_key="xk-002", reason="r")

    eb_item = db.get_written(_TABLE, ENTRY_BLOCK_PK, ENTRY_BLOCK_SK)
    ks_item = db.get_written(_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)

    assert eb_item is not None and ks_item is not None
    assert eb_item != ks_item
    assert len(db.put_calls) == 2
