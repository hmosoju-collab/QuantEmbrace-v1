"""Unit tests for EntryBlockValidator — Phase 6 defense-in-depth in risk_engine.

Verifies:
- New entry signals are rejected when ENTRY_BLOCK is active in DynamoDB.
- Closeout signals (is_closeout=True in metadata) are always approved.
- Paper signals are exempt in paper profile (fail-open).
- Live profile + ENTRY_BLOCK active → rejected.
- Live profile + DynamoDB read failure → fail closed (entries rejected).
- Paper profile + DynamoDB read failure → warn + allow (fail open).
- Cache prevents repeated DynamoDB reads within TTL.
- invalidate() forces fresh read on next validate().

All tests are synchronous (using asyncio.run) and pure — no boto3.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from risk_engine.validators.entry_block_validator import EntryBlockValidator
from shared.models.signal import Direction, Signal


# ── helpers ───────────────────────────────────────────────────────────────────


def _signal(
    *,
    paper_trade: bool = False,
    is_closeout: bool = False,
    symbol: str = "RELIANCE",
) -> Signal:
    s = Signal(
        symbol=symbol,
        market="NSE",
        direction=Direction.BUY,
        quantity=10,
        confidence=0.8,
        strategy_name="test_strategy",
        paper_trade=paper_trade,
        metadata={"is_closeout": is_closeout} if is_closeout else {},
    )
    return s


class _FakeDynamo:
    """In-memory DynamoDB stub for EntryBlockValidator tests."""

    def __init__(self, item: dict | None = None, raise_on_get: bool = False) -> None:
        self._item = item
        self._raise = raise_on_get
        self.call_count: int = 0

    def get_item(self, **kwargs: Any) -> dict:
        self.call_count += 1
        if self._raise:
            raise RuntimeError("DynamoDB unavailable")
        return {"Item": self._item} if self._item else {}


def _entry_block_item(*, blocked: bool = True, reason: str = "test") -> dict:
    return {
        "PK": {"S": "ENTRY_BLOCK"},
        "SK": {"S": "GLOBAL"},
        "blocked": {"BOOL": blocked},
        "status": {"S": "BLOCKED" if blocked else "CLEAR"},
        "reason": {"S": reason},
        "source": {"S": "safe_actions"},
        "action_id": {"S": "act-001"},
        "idempotency_key": {"S": "bne-test-20260531"},
        "created_at": {"S": "2026-05-31T10:00:00+00:00"},
        "schema_version": {"S": "1.0"},
    }


def _validator(
    dynamo: _FakeDynamo,
    profile: str = "paper",
    fail_closed: bool | None = None,
    ttl: float = 0.0,  # 0s TTL → always reads from DynamoDB
) -> EntryBlockValidator:
    return EntryBlockValidator(
        dynamo_client=dynamo,
        risk_state_table="qe-risk-state",
        risk_profile=profile,
        fail_closed_on_error=fail_closed,
        cache_ttl_seconds=ttl,
    )


def _validate(v: EntryBlockValidator, signal: Signal):
    return asyncio.run(v.validate(signal))


# ── Core behaviour ────────────────────────────────────────────────────────────


class TestEntryBlockValidatorCore:
    def test_no_block_flag_approves_entry_signal(self):
        dynamo = _FakeDynamo(item=None)
        v = _validator(dynamo)
        result = _validate(v, _signal())
        assert result.approved is True
        assert result.validator_name == "entry_block_validator"

    def test_block_flag_active_rejects_new_entry(self):
        dynamo = _FakeDynamo(item=_entry_block_item(blocked=True))
        v = _validator(dynamo, profile="paper")
        result = _validate(v, _signal(paper_trade=False))
        assert result.approved is False
        assert "ENTRY_BLOCK_ACTIVE" in result.reason

    def test_block_flag_contains_reason_and_source(self):
        dynamo = _FakeDynamo(item=_entry_block_item(blocked=True, reason="stale_ltp"))
        v = _validator(dynamo, profile="paper")
        result = _validate(v, _signal(paper_trade=False))
        assert "stale_ltp" in result.reason
        assert "safe_actions" in result.reason

    def test_block_false_in_dynamo_approves(self):
        """blocked=False in DynamoDB item → entry allowed."""
        dynamo = _FakeDynamo(item=_entry_block_item(blocked=False))
        v = _validator(dynamo)
        result = _validate(v, _signal())
        assert result.approved is True


# ── Closeout exemption ────────────────────────────────────────────────────────


class TestCloseoutExemption:
    def test_closeout_approved_when_block_active(self):
        dynamo = _FakeDynamo(item=_entry_block_item(blocked=True))
        v = _validator(dynamo)
        result = _validate(v, _signal(is_closeout=True))
        assert result.approved is True
        assert result.reason == "closeout_exempt"

    def test_closeout_approved_when_live_profile_and_block_active(self):
        dynamo = _FakeDynamo(item=_entry_block_item(blocked=True))
        v = _validator(dynamo, profile="tiny-live", fail_closed=True)
        result = _validate(v, _signal(is_closeout=True))
        assert result.approved is True

    def test_closeout_approved_when_dynamo_read_fails(self):
        dynamo = _FakeDynamo(raise_on_get=True)
        v = _validator(dynamo, profile="tiny-live", fail_closed=True)
        result = _validate(v, _signal(is_closeout=True))
        assert result.approved is True

    def test_non_closeout_blocked_when_block_active(self):
        dynamo = _FakeDynamo(item=_entry_block_item(blocked=True))
        v = _validator(dynamo)
        result = _validate(v, _signal(is_closeout=False))
        assert result.approved is False


# ── Paper / live profile behaviour ────────────────────────────────────────────


class TestProfileBehaviour:
    def test_paper_signal_exempt_in_paper_profile(self):
        dynamo = _FakeDynamo(item=_entry_block_item(blocked=True))
        v = _validator(dynamo, profile="paper")
        result = _validate(v, _signal(paper_trade=True))
        assert result.approved is True
        assert "paper_signal_exempt" in result.reason

    def test_live_profile_blocks_even_paper_trade_signal(self):
        """In live profile, paper_trade=True is NOT exempt from entry block."""
        dynamo = _FakeDynamo(item=_entry_block_item(blocked=True))
        v = _validator(dynamo, profile="tiny-live")
        result = _validate(v, _signal(paper_trade=True))
        assert result.approved is False

    def test_paper_read_failure_fails_open(self):
        dynamo = _FakeDynamo(raise_on_get=True)
        v = _validator(dynamo, profile="paper", fail_closed=False)
        result = _validate(v, _signal())
        assert result.approved is True
        assert "WARN_ALLOW" in result.reason

    def test_live_read_failure_fails_closed(self):
        dynamo = _FakeDynamo(raise_on_get=True)
        v = _validator(dynamo, profile="tiny-live", fail_closed=True)
        result = _validate(v, _signal())
        assert result.approved is False
        assert "FAIL_CLOSED" in result.reason

    def test_live_read_failure_does_not_block_closeout(self):
        """Even when live read fails and entries fail-closed, closeouts still pass."""
        dynamo = _FakeDynamo(raise_on_get=True)
        v = _validator(dynamo, profile="tiny-live", fail_closed=True)
        result = _validate(v, _signal(is_closeout=True))
        assert result.approved is True


# ── Cache behaviour ───────────────────────────────────────────────────────────


class TestCacheBehaviour:
    def test_reads_cached_within_ttl(self):
        """With high TTL, second validate() does not hit DynamoDB again."""
        dynamo = _FakeDynamo(item=None)
        v = _validator(dynamo, ttl=60.0)   # 60s TTL
        _validate(v, _signal())
        _validate(v, _signal())
        # First call reads DynamoDB; second uses cache
        assert dynamo.call_count == 1

    def test_invalidate_forces_fresh_read(self):
        dynamo = _FakeDynamo(item=None)
        v = _validator(dynamo, ttl=60.0)
        _validate(v, _signal())
        v.invalidate()
        _validate(v, _signal())
        assert dynamo.call_count == 2

    def test_zero_ttl_reads_on_every_call(self):
        dynamo = _FakeDynamo(item=None)
        v = _validator(dynamo, ttl=0.0)
        _validate(v, _signal())
        _validate(v, _signal())
        assert dynamo.call_count == 2


# ── Validator name ────────────────────────────────────────────────────────────


class TestValidatorName:
    def test_validator_name_always_entry_block_validator(self):
        for item, profile in [
            (None, "paper"),
            (_entry_block_item(), "paper"),
            (None, "tiny-live"),
        ]:
            dynamo = _FakeDynamo(item=item)
            v = _validator(dynamo, profile=profile)
            result = _validate(v, _signal())
            assert result.validator_name == "entry_block_validator"


# ── entry_block_active property ───────────────────────────────────────────────


class TestProperties:
    def test_entry_block_active_property_false_initially(self):
        dynamo = _FakeDynamo(item=None)
        v = _validator(dynamo)
        assert v.entry_block_active is False

    def test_entry_block_active_property_true_after_active_read(self):
        dynamo = _FakeDynamo(item=_entry_block_item(blocked=True))
        v = _validator(dynamo, ttl=0.0)
        _validate(v, _signal(paper_trade=False))
        assert v.entry_block_active is True

    def test_last_read_ok_false_after_dynamo_failure(self):
        dynamo = _FakeDynamo(raise_on_get=True)
        v = _validator(dynamo, profile="paper", fail_closed=False, ttl=0.0)
        _validate(v, _signal())
        assert v.last_read_ok is False
