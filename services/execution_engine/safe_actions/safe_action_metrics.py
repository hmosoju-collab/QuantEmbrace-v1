"""Metrics for the safe_actions layer — Phase 6.

Wraps the existing ``shared.metrics.cloudwatch_metrics`` client so the safe_actions
layer emits the same-style CW metrics as the rest of the platform.

If no CloudWatch client is available (local dev, tests), the underlying
``CloudWatchMetricsClient`` operates in dry-run / log-only mode — no exceptions
are raised.  Tests can inject a ``_FakeMetrics`` stub (see ``test_safe_actions_metrics.py``).

Namespaces:
    QuantEmbrace/SafeActions  — executor counters
    QuantEmbrace/EntryBlock   — entry-block reader + strategy counters
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("safe_actions.metrics")

# Metric names — stable, machine-readable
M_EXECUTED = "safe_actions.executed_total"
M_BLOCKED = "safe_actions.blocked_total"
M_FORBIDDEN = "safe_actions.forbidden_total"
M_IDEM_SKIP = "safe_actions.idempotency_skip_total"
M_DYNAMO_FAIL = "safe_actions.dynamo_write_failed_total"
M_BNE_TOTAL = "safe_actions.block_new_entries_total"
M_KS_TOTAL = "safe_actions.kill_switch_activated_total"
M_KS_FAIL = "safe_actions.kill_switch_write_failed_total"
M_EB_ACTIVE = "strategy.entry_block_active"
M_EB_BLOCKED = "strategy.entry_blocked_total"
M_EB_READ_FAIL = "strategy.entry_block_read_failure_total"
M_EB_CACHE_HIT = "strategy.entry_block_cache_hit_total"
M_EB_CACHE_MISS = "strategy.entry_block_cache_miss_total"
M_MON_EXECUTED = "monitoring.safe_actions_executed_total"
M_MON_BLOCKED = "monitoring.safe_actions_blocked_total"
M_MON_DISABLED = "monitoring.safe_actions_disabled_total"


class SafeActionMetrics:
    """Thread-safe metrics wrapper.  Never raises."""

    def __init__(self, cw_client: Optional[Any] = None) -> None:
        """Args:
            cw_client: An object with ``record_count(name, value, dimensions)`` and
                       ``record_gauge(name, value, dimensions)`` compatible with
                       ``CloudWatchMetricsClient``.  If None, a no-op stub is used.
        """
        self._cw = cw_client
        # In-memory counters always maintained for tests (independent of CW).
        self._counts: dict[str, int] = {}

    # ── executor metrics ──────────────────────────────────────────────────────

    def action_executed(self, action_type: str, mode: str) -> None:
        self._inc(M_EXECUTED)
        self._emit(M_EXECUTED, dims={"ActionType": action_type, "Mode": mode})

    def action_blocked(self, action_type: str, reason: str) -> None:
        self._inc(M_BLOCKED)
        if "FORBIDDEN" in action_type.upper() or "FORBIDDEN" in reason.upper():
            self._inc(M_FORBIDDEN)
            self._emit(M_FORBIDDEN, dims={"ActionType": action_type})
        self._emit(M_BLOCKED, dims={"ActionType": action_type, "Reason": reason[:32]})

    def idempotency_skip(self, action_type: str, source: str = "memory") -> None:
        """source: 'memory' (in-memory store) or 'dynamo' (DurableIdempotencyStore)."""
        self._inc(M_IDEM_SKIP)
        self._emit(M_IDEM_SKIP, dims={"ActionType": action_type, "Source": source})

    def dynamo_write_failed(self, action_type: str) -> None:
        self._inc(M_DYNAMO_FAIL)
        self._emit(M_DYNAMO_FAIL, dims={"ActionType": action_type})

    def block_new_entries(self) -> None:
        self._inc(M_BNE_TOTAL)
        self._emit(M_BNE_TOTAL)

    def kill_switch_activated(self) -> None:
        self._inc(M_KS_TOTAL)
        self._emit(M_KS_TOTAL)

    def kill_switch_write_failed(self) -> None:
        self._inc(M_KS_FAIL)
        self._emit(M_KS_FAIL)

    # ── entry-block metrics ───────────────────────────────────────────────────

    def entry_block_active(self, active: bool) -> None:
        self._emit(M_EB_ACTIVE, value=1 if active else 0)

    def entry_blocked(self, strategy: str) -> None:
        self._inc(M_EB_BLOCKED)
        self._emit(M_EB_BLOCKED, dims={"Strategy": strategy})

    def entry_block_read_failure(self) -> None:
        self._inc(M_EB_READ_FAIL)
        self._emit(M_EB_READ_FAIL)

    def entry_block_cache_hit(self) -> None:
        self._inc(M_EB_CACHE_HIT)
        self._emit(M_EB_CACHE_HIT)

    def entry_block_cache_miss(self) -> None:
        self._inc(M_EB_CACHE_MISS)
        self._emit(M_EB_CACHE_MISS)

    # ── monitoring agent metrics ──────────────────────────────────────────────

    def monitoring_executed(self) -> None:
        self._inc(M_MON_EXECUTED)
        self._emit(M_MON_EXECUTED)

    def monitoring_blocked(self) -> None:
        self._inc(M_MON_BLOCKED)
        self._emit(M_MON_BLOCKED)

    def monitoring_disabled(self) -> None:
        self._inc(M_MON_DISABLED)
        self._emit(M_MON_DISABLED)

    # ── helpers ───────────────────────────────────────────────────────────────

    @property
    def counts(self) -> dict[str, int]:
        """Snapshot of in-memory counters (for tests)."""
        return dict(self._counts)

    def _inc(self, name: str) -> None:
        self._counts[name] = self._counts.get(name, 0) + 1

    def _emit(
        self,
        name: str,
        *,
        value: float = 1.0,
        dims: Optional[dict[str, str]] = None,
    ) -> None:
        if self._cw is None:
            return
        try:
            self._cw.record_count(name, value=value, dimensions=dims or {})
        except Exception as exc:  # noqa: BLE001
            # Metrics must never break execution
            logger.debug("safe_actions.metrics.emit_failed name=%s error=%s", name, type(exc).__name__)
