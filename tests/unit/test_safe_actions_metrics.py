"""Unit tests for SafeActionMetrics — Phase 6.

Verifies:
- CloudWatch metrics emitted for executed, blocked, forbidden actions.
- Idempotency skip metric emitted with correct source dimension.
- DynamoDB write failure metric emitted.
- block_new_entries and kill_switch_activated metrics emitted.
- Entry-block cache hit/miss metrics emitted.
- Monitoring agent metrics emitted.
- No exception propagates when CW client raises.
- In-memory counters always maintained independently of CW client.

All tests are synchronous and pure — no boto3, no I/O.
"""

from __future__ import annotations

from typing import Any

import pytest

from execution_engine.safe_actions.safe_action_metrics import SafeActionMetrics


# ── Fake CloudWatch client ────────────────────────────────────────────────────


class _FakeCW:
    """Records calls to record_count() / record_gauge()."""

    def __init__(self, raise_on_emit: bool = False) -> None:
        self.calls: list[dict] = []
        self._raise = raise_on_emit

    def record_count(self, name: str, value: float = 1.0, dimensions: dict | None = None) -> None:
        if self._raise:
            raise RuntimeError("CW unavailable")
        self.calls.append({"name": name, "value": value, "dims": dimensions or {}})

    def record_gauge(self, name: str, value: float, dimensions: dict | None = None) -> None:
        if self._raise:
            raise RuntimeError("CW unavailable")
        self.calls.append({"name": name, "value": value, "dims": dimensions or {}})

    def names(self) -> list[str]:
        return [c["name"] for c in self.calls]


# ── executor metric tests ─────────────────────────────────────────────────────


class TestExecutorMetrics:
    def test_action_executed_emits_metric_and_increments_count(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.action_executed("BLOCK_NEW_ENTRIES", "paper")
        assert "safe_actions.executed_total" in cw.names()
        assert m.counts["safe_actions.executed_total"] == 1

    def test_action_blocked_emits_blocked_metric(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.action_blocked("SEND_ALERT", "policy_deny")
        assert "safe_actions.blocked_total" in cw.names()
        assert m.counts["safe_actions.blocked_total"] == 1

    def test_action_blocked_emits_forbidden_metric_when_forbidden(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.action_blocked("FORBIDDEN_ACTION", "FORBIDDEN")
        assert "safe_actions.forbidden_total" in cw.names()
        assert m.counts.get("safe_actions.forbidden_total", 0) == 1

    def test_idempotency_skip_memory(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.idempotency_skip("BLOCK_NEW_ENTRIES", source="memory")
        assert "safe_actions.idempotency_skip_total" in cw.names()
        call = next(c for c in cw.calls if c["name"] == "safe_actions.idempotency_skip_total")
        assert call["dims"].get("Source") == "memory"

    def test_idempotency_skip_dynamo(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.idempotency_skip("ACTIVATE_KILL_SWITCH", source="dynamo")
        call = next(c for c in cw.calls if c["name"] == "safe_actions.idempotency_skip_total")
        assert call["dims"].get("Source") == "dynamo"

    def test_dynamo_write_failed_emits_metric(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.dynamo_write_failed("BLOCK_NEW_ENTRIES")
        assert "safe_actions.dynamo_write_failed_total" in cw.names()
        assert m.counts["safe_actions.dynamo_write_failed_total"] == 1

    def test_block_new_entries_metric(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.block_new_entries()
        assert "safe_actions.block_new_entries_total" in cw.names()
        assert m.counts["safe_actions.block_new_entries_total"] == 1

    def test_kill_switch_activated_metric(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.kill_switch_activated()
        assert "safe_actions.kill_switch_activated_total" in cw.names()
        assert m.counts["safe_actions.kill_switch_activated_total"] == 1

    def test_kill_switch_write_failed_metric(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.kill_switch_write_failed()
        assert "safe_actions.kill_switch_write_failed_total" in cw.names()


# ── entry-block metric tests ──────────────────────────────────────────────────


class TestEntryBlockMetrics:
    def test_entry_block_active_true(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.entry_block_active(True)
        call = next(c for c in cw.calls if c["name"] == "strategy.entry_block_active")
        assert call["value"] == 1.0

    def test_entry_block_active_false(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.entry_block_active(False)
        call = next(c for c in cw.calls if c["name"] == "strategy.entry_block_active")
        assert call["value"] == 0.0

    def test_entry_blocked_emits_with_strategy_dim(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.entry_blocked("momentum_v1")
        call = next(c for c in cw.calls if c["name"] == "strategy.entry_blocked_total")
        assert call["dims"].get("Strategy") == "momentum_v1"

    def test_entry_block_read_failure(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.entry_block_read_failure()
        assert "strategy.entry_block_read_failure_total" in cw.names()
        assert m.counts["strategy.entry_block_read_failure_total"] == 1

    def test_entry_block_cache_hit(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.entry_block_cache_hit()
        assert "strategy.entry_block_cache_hit_total" in cw.names()

    def test_entry_block_cache_miss(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.entry_block_cache_miss()
        assert "strategy.entry_block_cache_miss_total" in cw.names()


# ── monitoring agent metric tests ─────────────────────────────────────────────


class TestMonitoringAgentMetrics:
    def test_monitoring_executed(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.monitoring_executed()
        assert "monitoring.safe_actions_executed_total" in cw.names()

    def test_monitoring_blocked(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.monitoring_blocked()
        assert "monitoring.safe_actions_blocked_total" in cw.names()

    def test_monitoring_disabled(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        m.monitoring_disabled()
        assert "monitoring.safe_actions_disabled_total" in cw.names()


# ── resilience tests ──────────────────────────────────────────────────────────


class TestMetricsResilience:
    def test_cw_raise_does_not_propagate(self):
        """CW client failure must never crash the caller."""
        cw = _FakeCW(raise_on_emit=True)
        m = SafeActionMetrics(cw_client=cw)
        # None of these should raise
        m.action_executed("BLOCK_NEW_ENTRIES", "paper")
        m.action_blocked("SEND_ALERT", "policy_deny")
        m.block_new_entries()
        m.kill_switch_activated()
        m.entry_block_cache_hit()

    def test_in_memory_counts_maintained_even_when_cw_unavailable(self):
        """in-memory counters always track calls regardless of CW state."""
        cw = _FakeCW(raise_on_emit=True)
        m = SafeActionMetrics(cw_client=cw)
        m.action_executed("BLOCK_NEW_ENTRIES", "paper")
        m.action_executed("BLOCK_NEW_ENTRIES", "paper")
        m.action_blocked("SEND_ALERT", "policy_deny")
        assert m.counts["safe_actions.executed_total"] == 2
        assert m.counts["safe_actions.blocked_total"] == 1

    def test_no_cw_client_no_emit_no_crash(self):
        """None cw_client → no emit, no crash, counts still maintained."""
        m = SafeActionMetrics(cw_client=None)
        m.action_executed("SEND_ALERT", "paper")
        m.block_new_entries()
        assert m.counts["safe_actions.executed_total"] == 1
        assert m.counts["safe_actions.block_new_entries_total"] == 1

    def test_multiple_calls_accumulate_counts(self):
        cw = _FakeCW()
        m = SafeActionMetrics(cw_client=cw)
        for _ in range(5):
            m.entry_block_cache_hit()
        assert m.counts["strategy.entry_block_cache_hit_total"] == 5
        assert cw.names().count("strategy.entry_block_cache_hit_total") == 5
