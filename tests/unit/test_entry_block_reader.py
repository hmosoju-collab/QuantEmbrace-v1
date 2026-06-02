"""Unit tests for EntryBlockReader — Phase 5.

Uses the same fake-DynamoDB pattern as test_safe_action_dynamo_writer.py.
All tests are synchronous and pure.
"""

from __future__ import annotations

from typing import Any

import pytest

from shared.entry_block_reader import EntryBlockReader, EntryBlockState
from shared.risk_state import ENTRY_BLOCK_PK, ENTRY_BLOCK_SK


# ── fake DynamoDB low-level client ────────────────────────────────────────────


class _FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.get_calls: list[dict] = []
        self.fail_on_get: Exception | None = None

    def get_item(self, TableName: str, Key: dict, **kwargs: Any) -> dict:
        self.get_calls.append({"TableName": TableName, "Key": Key})
        if self.fail_on_get is not None:
            raise self.fail_on_get
        pk = Key.get("PK", {}).get("S", "")
        sk = Key.get("SK", {}).get("S", "")
        item = self.items.get((TableName, pk, sk))
        return {"Item": item} if item else {}

    def put_item(self, TableName: str, Item: dict, **kwargs: Any) -> dict:
        pk = Item.get("PK", {}).get("S", "")
        sk = Item.get("SK", {}).get("S", "")
        self.items[(TableName, pk, sk)] = Item
        return {}


_TABLE = "quantembrace-paper-risk-state"


def _reader(
    db: _FakeDynamo,
    ttl: float = 5.0,
    fail_closed: bool = False,
) -> EntryBlockReader:
    return EntryBlockReader(
        dynamo_client=db,
        risk_state_table=_TABLE,
        cache_ttl_seconds=ttl,
        fail_closed_on_error=fail_closed,
    )


def _write_entry_block(db: _FakeDynamo, *, blocked: bool = True, reason: str = "stale_ltp") -> None:
    db.put_item(
        TableName=_TABLE,
        Item={
            "PK": {"S": ENTRY_BLOCK_PK}, "SK": {"S": ENTRY_BLOCK_SK},
            "blocked": {"BOOL": blocked},
            "status": {"S": "BLOCKED" if blocked else "CLEAR"},
            "reason": {"S": reason},
            "source": {"S": "safe_actions"},
            "action_id": {"S": "act-001"},
            "idempotency_key": {"S": "blk-001"},
            "created_at": {"S": "2026-05-31T09:00:00+00:00"},
            "schema_version": {"S": "1.0"},
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. Entry block absent → entries allowed
# ══════════════════════════════════════════════════════════════════════════════


def test_entry_block_absent_allows_entries():
    db = _FakeDynamo()
    reader = _reader(db)
    state = reader.read()
    assert state.blocked is False
    assert state.read_ok is True


def test_entry_block_absent_does_not_cache_blocked():
    db = _FakeDynamo()
    reader = _reader(db, ttl=100)
    reader.read()
    # Even with long TTL, cached state should show not blocked
    assert reader.cached_state.blocked is False


# ══════════════════════════════════════════════════════════════════════════════
# 2. Entry block active → entries blocked
# ══════════════════════════════════════════════════════════════════════════════


def test_entry_block_active_blocks_entries():
    db = _FakeDynamo()
    _write_entry_block(db, blocked=True, reason="stale_ltp")
    reader = _reader(db)
    state = reader.read()
    assert state.blocked is True
    assert state.reason == "stale_ltp"
    assert state.source == "safe_actions"
    assert state.action_id == "act-001"
    assert state.read_ok is True


def test_entry_block_state_fields_populated():
    db = _FakeDynamo()
    _write_entry_block(db, blocked=True, reason="test")
    state = _reader(db).read()
    assert state.idempotency_key == "blk-001"
    assert state.created_at == "2026-05-31T09:00:00+00:00"
    assert state.schema_version == "1.0"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Entry block does not affect exit management (never checked for exits)
# ══════════════════════════════════════════════════════════════════════════════


def test_entry_block_reader_only_reads_entry_block_key():
    """Reader must only query ENTRY_BLOCK/GLOBAL — never order or position keys."""
    db = _FakeDynamo()
    _write_entry_block(db)
    _reader(db).read()
    for call in db.get_calls:
        key = call["Key"]
        assert key.get("PK", {}).get("S") == ENTRY_BLOCK_PK
        assert key.get("SK", {}).get("S") == ENTRY_BLOCK_SK


# ══════════════════════════════════════════════════════════════════════════════
# 4. Read failure in paper mode → warn + allow (fail-open)
# ══════════════════════════════════════════════════════════════════════════════


def test_read_failure_paper_fail_open_allows_entries():
    db = _FakeDynamo()
    db.fail_on_get = ConnectionError("DynamoDB unreachable")
    reader = _reader(db, fail_closed=False)  # paper mode
    state = reader.read()
    assert state.blocked is False
    assert state.read_ok is False
    assert state.reason == "entry_block_read_failure"


def test_read_failure_paper_does_not_raise():
    db = _FakeDynamo()
    db.fail_on_get = RuntimeError("boom")
    reader = _reader(db, fail_closed=False)
    state = reader.read()  # must not raise
    assert isinstance(state, EntryBlockState)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Read failure in live mode → fail closed (block entries)
# ══════════════════════════════════════════════════════════════════════════════


def test_read_failure_live_fail_closed_blocks_entries():
    db = _FakeDynamo()
    db.fail_on_get = ConnectionError("DynamoDB unreachable")
    reader = _reader(db, fail_closed=True)  # live mode
    state = reader.read()
    assert state.blocked is True
    assert state.read_ok is False
    assert state.reason == "entry_block_read_failure"


def test_read_failure_live_does_not_update_cache():
    """On live read failure, cache is not updated so we keep returning fail_closed."""
    db = _FakeDynamo()
    _write_entry_block(db, blocked=True, reason="stale_ltp")
    reader = _reader(db, fail_closed=True, ttl=0.0)  # force re-read each call
    # First read: success, blocked=True from DB
    state1 = reader.read(force=True)
    assert state1.blocked is True and state1.read_ok is True
    # Now simulate failure
    db.fail_on_get = RuntimeError("network error")
    state2 = reader.read(force=True)
    assert state2.blocked is True  # still blocked (fail-closed)
    assert state2.read_ok is False


# ══════════════════════════════════════════════════════════════════════════════
# 6. Cache TTL
# ══════════════════════════════════════════════════════════════════════════════


def test_reader_uses_cache_within_ttl():
    db = _FakeDynamo()
    reader = _reader(db, ttl=100.0)
    reader.read()
    reader.read()  # second call should use cache
    assert len(db.get_calls) == 1


def test_reader_bypasses_cache_on_force():
    db = _FakeDynamo()
    reader = _reader(db, ttl=100.0)
    reader.read()
    reader.read(force=True)
    assert len(db.get_calls) == 2


def test_reader_invalidate_forces_next_read():
    db = _FakeDynamo()
    reader = _reader(db, ttl=100.0)
    reader.read()
    reader.invalidate()
    reader.read()
    assert len(db.get_calls) == 2


# ══════════════════════════════════════════════════════════════════════════════
# 7. EntryBlockState helpers
# ══════════════════════════════════════════════════════════════════════════════


def test_entry_block_state_allow_is_not_blocked():
    assert EntryBlockState.allow().blocked is False
    assert EntryBlockState.allow().read_ok is True


def test_entry_block_state_fail_closed_is_blocked():
    s = EntryBlockState.fail_closed()
    assert s.blocked is True
    assert s.read_ok is False


def test_entry_block_state_fail_open_is_not_blocked():
    s = EntryBlockState.fail_open()
    assert s.blocked is False
    assert s.read_ok is False
