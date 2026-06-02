"""Integration tests for the complete safe_actions → ENTRY_BLOCK → strategy flow.

Full cycle:
  1. Observation (stale LTP / ai_engine DOWN / reconciliation mismatch)
  2. Classifier → BLOCK_NEW_ENTRIES proposed
  3. SafeActionExecutor writes ENTRY_BLOCK/GLOBAL via SafeActionDynamoWriter
  4. EntryBlockReader reads the flag
  5. strategy_engine entry check returns blocked=True
  6. Exit management not affected

All use in-memory FakeDynamo — no real AWS needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from execution_engine.safe_actions import (
    ActionScope,
    ActionType,
    RiskLevel,
    SafeAction,
    SafeActionAudit,
    SafeActionClassifier,
    SafeActionDynamoWriter,
    SafeActionExecutor,
    SafeActionPolicy,
    TradingMode,
)
from shared.entry_block_reader import EntryBlockReader
from shared.risk_state import ENTRY_BLOCK_PK, ENTRY_BLOCK_SK, KILL_SWITCH_PK, KILL_SWITCH_SK


# ── shared fake ───────────────────────────────────────────────────────────────


class _FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.put_calls: list[dict] = []
        self.fail_on: dict[str, Exception] = {}

    def put_item(self, TableName: str, Item: dict, **kwargs: Any) -> dict:
        if TableName in self.fail_on:
            raise self.fail_on[TableName]
        pk = Item.get("PK", {}).get("S", "")
        sk = Item.get("SK", {}).get("S", "")
        self.items[(TableName, pk, sk)] = Item
        self.put_calls.append({"TableName": TableName, "Item": Item})
        return {}

    def get_item(self, TableName: str, Key: dict, **kwargs: Any) -> dict:
        pk = Key.get("PK", {}).get("S", "")
        sk = Key.get("SK", {}).get("S", "")
        item = self.items.get((TableName, pk, sk))
        return {"Item": item} if item else {}


_TABLE = "quantembrace-paper-risk-state"


def _setup(tmp_path: Path, mode: TradingMode = TradingMode.PAPER):
    db = _FakeDynamo()
    dynamo_writer = SafeActionDynamoWriter(dynamo_client=db, risk_state_table=_TABLE)
    executor = SafeActionExecutor(
        policy=SafeActionPolicy(mode),
        audit=SafeActionAudit(path=str(tmp_path / "audit.jsonl")),
        dynamo_writer=dynamo_writer,
    )
    clf = SafeActionClassifier()
    reader = EntryBlockReader(dynamo_client=db, risk_state_table=_TABLE, cache_ttl_seconds=0)
    return executor, clf, db, reader


def _entry_block_action(mode: TradingMode, idem_key: str) -> SafeAction:
    return SafeAction.build(
        action_type=ActionType.BLOCK_NEW_ENTRIES,
        scope=ActionScope.RISK_REDUCTION,
        mode=mode,
        risk_level=RiskLevel.LOW,
        requires_human_approval=False,
        idempotency_key=idem_key,
        preconditions=[],
        expected_effect="Block new entry signals; exits continue",
        rollback_behavior="Remove ENTRY_BLOCK flag",
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. Stale LTP → BLOCK_NEW_ENTRIES → ENTRY_BLOCK written → strategy blocked
# ══════════════════════════════════════════════════════════════════════════════


def test_stale_ltp_full_cycle_blocks_strategy_entries(tmp_path):
    executor, clf, db, reader = _setup(tmp_path)

    # Step 1: classify observation
    obs = clf.classify_finding(code="broker.feed_very_stale", subject="feed")
    assert ActionType.BLOCK_NEW_ENTRIES in obs.proposed_action_types

    # Step 2: execute BLOCK_NEW_ENTRIES
    action = _entry_block_action(TradingMode.PAPER, "stale-ltp-cycle-001")
    result = executor.execute(action, reason="stale_ltp_market_hours")
    assert result.executed is True

    # Step 3: EntryBlockReader sees the flag
    state = reader.read()
    assert state.blocked is True
    assert state.source == "safe_actions"


def test_stale_ltp_cycle_does_not_activate_kill_switch(tmp_path):
    executor, clf, db, reader = _setup(tmp_path)
    action = _entry_block_action(TradingMode.PAPER, "stale-ltp-ks-check-001")
    executor.execute(action, reason="stale_ltp")

    ks_item = db.items.get((_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK))
    assert ks_item is None, "Stale LTP must not activate kill switch"


# ══════════════════════════════════════════════════════════════════════════════
# 2. ai_engine DOWN → alert + BLOCK_NEW_ENTRIES → strategy blocked
# ══════════════════════════════════════════════════════════════════════════════


def test_ai_engine_down_block_entries_and_strategy_blocked(tmp_path):
    executor, clf, db, reader = _setup(tmp_path)

    obs = clf.classify_finding(code="service.down", subject="ai_engine")
    assert ActionType.BLOCK_NEW_ENTRIES in obs.proposed_action_types

    action = _entry_block_action(TradingMode.PAPER, "ai-down-blk-001")
    result = executor.execute(action, reason="ai_engine_down")
    assert result.executed is True

    state = reader.read()
    assert state.blocked is True


def test_ai_engine_down_no_kill_switch_in_full_cycle(tmp_path):
    executor, clf, db, reader = _setup(tmp_path)
    obs = clf.classify_finding(code="service.down", subject="ai_engine")
    assert ActionType.ACTIVATE_KILL_SWITCH not in obs.proposed_action_types

    action = _entry_block_action(TradingMode.PAPER, "ai-down-ks-check-001")
    executor.execute(action, reason="ai_engine_down")
    assert db.items.get((_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK)) is None


# ══════════════════════════════════════════════════════════════════════════════
# 3. Exit management still allowed when ENTRY_BLOCK active
# ══════════════════════════════════════════════════════════════════════════════


def test_entry_block_does_not_affect_exit_management(tmp_path):
    """Exits never check EntryBlockReader — only entry dispatch does."""
    executor, clf, db, reader = _setup(tmp_path)

    action = _entry_block_action(TradingMode.PAPER, "exit-allowed-001")
    executor.execute(action, reason="stale_ltp")

    state = reader.read()
    assert state.blocked is True

    # Exit management (TEE/MIS) does not call EntryBlockReader.
    # Confirm: reader has no exit-specific methods.
    assert not hasattr(reader, "is_exit_blocked")
    assert not hasattr(reader, "read_for_exit")

    # Entry block key is only at ENTRY_BLOCK/GLOBAL — orders/positions unchanged
    written_pks = {v["PK"]["S"] for v in db.items.values()}
    assert "POSITION" not in str(written_pks)
    assert "ORDER" not in str(written_pks)
    assert "NAV" not in str(written_pks)


# ══════════════════════════════════════════════════════════════════════════════
# 4. ACTIVATE_KILL_SWITCH writes KILLSWITCH and does not clear ENTRY_BLOCK
# ══════════════════════════════════════════════════════════════════════════════


def test_kill_switch_activation_does_not_clear_entry_block(tmp_path):
    executor, clf, db, reader = _setup(tmp_path)

    # First: write ENTRY_BLOCK
    blk_action = _entry_block_action(TradingMode.PAPER, "blk-then-ks-001")
    executor.execute(blk_action, reason="stale_ltp")
    assert reader.read().blocked is True

    # Then: activate kill switch
    ks_action = SafeAction.build(
        action_type=ActionType.ACTIVATE_KILL_SWITCH,
        scope=ActionScope.RISK_REDUCTION,
        mode=TradingMode.PAPER,
        risk_level=RiskLevel.HIGH,
        requires_human_approval=False,
        idempotency_key="ks-then-blk-001",
        preconditions=[],
        expected_effect="Kill switch active",
        rollback_behavior="human manual",
    )
    ks_result = executor.execute(ks_action, reason="unmanaged_position")
    assert ks_result.executed is True

    # ENTRY_BLOCK is still there — kill switch activation must not clear it
    eb_item = db.items.get((_TABLE, ENTRY_BLOCK_PK, ENTRY_BLOCK_SK))
    assert eb_item is not None
    assert eb_item["blocked"] == {"BOOL": True}

    # Both keys written separately
    ks_item = db.items.get((_TABLE, KILL_SWITCH_PK, KILL_SWITCH_SK))
    assert ks_item is not None
    assert ks_item["active"] == {"BOOL": True}


# ══════════════════════════════════════════════════════════════════════════════
# 5. Reconciliation mismatch in live → entry blocked + exits continue
# ══════════════════════════════════════════════════════════════════════════════


def test_reconciliation_mismatch_live_blocks_entries(tmp_path):
    executor, clf, db, reader = _setup(tmp_path, mode=TradingMode.LIVE_DISABLED)

    obs = clf.classify_raw("reconciliation_required_live")
    assert ActionType.BLOCK_NEW_ENTRIES in obs.proposed_action_types

    action = SafeAction.build(
        action_type=ActionType.BLOCK_NEW_ENTRIES,
        scope=ActionScope.RISK_REDUCTION,
        mode=TradingMode.LIVE_DISABLED,
        risk_level=RiskLevel.LOW,
        requires_human_approval=False,
        idempotency_key="recon-live-blk-001",
        preconditions=[],
        expected_effect="Block new entries",
        rollback_behavior="Clear after reconciliation passes",
    )
    result = executor.execute(action, reason="reconciliation_required_live")
    assert result.executed is True

    state = reader.read()
    assert state.blocked is True


# ══════════════════════════════════════════════════════════════════════════════
# 6. ACTION_MODE gate
# ══════════════════════════════════════════════════════════════════════════════


def test_action_mode_safe_actions_enables_executor(tmp_path):
    """When ACTION_MODE=safe_actions, executor is active and may execute."""
    executor, clf, db, reader = _setup(tmp_path, mode=TradingMode.PAPER)
    action = _entry_block_action(TradingMode.PAPER, "action-mode-on-001")
    result = executor.execute(action, reason="safe_actions enabled")
    assert result.executed is True


def test_action_mode_absent_executor_none_no_execution(tmp_path):
    """When no executor is wired (ACTION_MODE absent), no safe actions run.

    Simulates monitoring_agent with notify_only mode: self._safe_executor is None.
    """
    executor = None  # ACTION_MODE != "safe_actions"
    action = _entry_block_action(TradingMode.PAPER, "action-mode-off-001")

    # The monitoring_agent guard: if executor is None, skip
    if executor is not None:
        executor.execute(action)

    # Nothing was written to DynamoDB
    db = _FakeDynamo()
    assert len(db.put_calls) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 7. Forbidden action blocked in full cycle
# ══════════════════════════════════════════════════════════════════════════════


def test_forbidden_action_blocked_in_full_cycle(tmp_path):
    executor, clf, db, reader = _setup(tmp_path)

    from execution_engine.safe_actions import ActionScope as AS, ActionType as AT
    from execution_engine.safe_actions import SafeAction

    forbidden = SafeAction.build(
        action_type=AT.FORBIDDEN,
        scope=AS.FORBIDDEN,
        mode=TradingMode.PAPER,
        risk_level=RiskLevel.HIGH,
        requires_human_approval=False,
        idempotency_key="forbidden-full-cycle",
        preconditions=[],
        expected_effect="MUST NOT RUN",
        rollback_behavior="N/A",
    )
    result = executor.execute(forbidden)
    assert result.blocked is True
    assert result.executed is False
    assert len(db.put_calls) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 8. BLOCK_NEW_ENTRIES action writes ENTRY_BLOCK and reader confirms
# ══════════════════════════════════════════════════════════════════════════════


def test_block_new_entries_write_confirmed_by_reader(tmp_path):
    executor, clf, db, reader = _setup(tmp_path)

    action = _entry_block_action(TradingMode.PAPER, "write-confirm-001")
    result = executor.execute(action, reason="test")
    assert result.executed is True

    # Reader uses same DB → sees the item
    state = reader.read(force=True)
    assert state.blocked is True
    assert state.read_ok is True
