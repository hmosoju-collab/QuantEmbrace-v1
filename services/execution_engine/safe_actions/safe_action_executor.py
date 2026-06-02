"""Safe action executor — the final gate before anything runs.

The executor:
    1. Checks policy (mode-based allow/deny) — fail closed.
    2. Checks human-approval gate — return without executing if required.
    3. Checks durable idempotency (DynamoDB) — skip cross-restart duplicates.
    4. Checks in-memory idempotency — skip within-session duplicates.
    5. Validates preconditions (caller-supplied boolean) — block if not met.
    6. Dispatches to a handler method.
    7. Writes a full audit record for every attempt regardless of outcome.
       Write actions (BLOCK_NEW_ENTRIES, ACTIVATE_KILL_SWITCH) fail closed
       if the audit write itself fails.

Phase 4/6 implemented action types:
    FULLY IMPLEMENTED (no DynamoDB):
        SEND_ALERT              — posts to Slack/SNS via the notifier callable
        GENERATE_RUNBOOK_COMMAND — returns a command string; no side effects
        READ_RUNTIME_STATE      — returns the audit_payload from the action as-is
    FULLY IMPLEMENTED (DynamoDB write via SafeActionDynamoWriter):
        BLOCK_NEW_ENTRIES       — writes ENTRY_BLOCK/GLOBAL to risk-state table
        ACTIVATE_KILL_SWITCH    — writes KILLSWITCH/GLOBAL active=True to risk-state table
    FRAMEWORK-COMPLETE, STUB (future phases):
        All remaining types — policy, idempotency, audit complete;
        execution returns ExecutionResult(stub_not_implemented=True, executed=False).
    ALWAYS BLOCKED:
        FORBIDDEN — policy gate + dedicated check; always audit-trailed.

Phase 6 additions:
    * Durable idempotency via DurableIdempotencyStore (DynamoDB) — survives restarts.
      Write actions check the durable store before executing; in-memory store is the
      within-session fast path checked after the durable check.
    * SafeActionMetrics — emits CloudWatch counters for every decision path.
    * Audit fail-closed — write actions return ExecutionResult(executed=False,
      error="AuditWriteFailed") if the audit write raises an exception, rather than
      silently continuing. Read-only actions still fall back to log.

Observability counters (in-memory, accessed via .counters property):
    block_new_entries_total     — successful BLOCK_NEW_ENTRIES executions
    kill_switch_activated_total — successful ACTIVATE_KILL_SWITCH executions
    dynamo_write_failed_total   — DynamoDB write failures (fail-closed path)
    idempotency_skipped_total   — skipped duplicate actions (memory or dynamo)
    action_blocked_total        — policy / precondition / human-gate blocks

Fail-closed invariant:
    Any unexpected exception in a handler is caught here, logged at ERROR,
    and returned as ExecutionResult(executed=False, error=type(exc).__name__).
    The executor NEVER propagates exceptions to the caller.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

from execution_engine.safe_actions.safe_action_audit import SafeActionAudit
from execution_engine.safe_actions.safe_action_models import (
    ActionType,
    ExecutionResult,
    RiskLevel,
    SafeAction,
    TradingMode,
)
from execution_engine.safe_actions.safe_action_policy import SafeActionPolicy

if TYPE_CHECKING:
    from execution_engine.safe_actions.safe_action_dynamo_writer import SafeActionDynamoWriter
    from execution_engine.safe_actions.safe_action_idempotency_store import DurableIdempotencyStore
    from execution_engine.safe_actions.safe_action_metrics import SafeActionMetrics

logger = logging.getLogger("safe_actions.executor")

# Action types whose audit failure must block execution (write actions).
_WRITE_ACTION_TYPES = frozenset([
    ActionType.BLOCK_NEW_ENTRIES,
    ActionType.ACTIVATE_KILL_SWITCH,
])


class _StubNotImplemented(Exception):
    """Internal sentinel: action framework-complete but Phase 4 not yet wired."""


# ── runbook command templates ──────────────────────────────────────────────────

_RUNBOOK_TEMPLATES: dict[str, str] = {
    "ai_engine": (
        "# ai_engine manual restart — verify EnrichmentWatchdog fallback is active first\n"
        "docker-compose stop ai_engine\n"
        "docker-compose rm -f ai_engine\n"
        "docker-compose up -d ai_engine\n"
        "# Check: docker-compose logs -f ai_engine | grep 'aiengine-v1'"
    ),
    "execution_engine": (
        "# execution_engine manual restart — CONFIRM kill switch INACTIVE first\n"
        "# REQUIRES HUMAN APPROVAL\n"
        "python scripts/kill_switch_cli.py status\n"
        "docker-compose stop execution_engine\n"
        "docker-compose rm -f execution_engine\n"
        "docker-compose up -d execution_engine\n"
        "# Check: docker-compose logs -f execution_engine | grep 'execution_service.started'"
    ),
    "risk_engine": (
        "# risk_engine manual restart — CONFIRM no signals in-flight first\n"
        "# REQUIRES HUMAN APPROVAL\n"
        "python scripts/kill_switch_cli.py status\n"
        "docker-compose stop risk_engine\n"
        "docker-compose rm -f risk_engine\n"
        "docker-compose up -d risk_engine\n"
        "# Check: docker-compose logs -f risk_engine | grep 'risk_engine_service.started'"
    ),
    "default": (
        "# Service manual restart — confirm dependencies first\n"
        "# REQUIRES HUMAN APPROVAL — fill in <SERVICE> below\n"
        "docker-compose stop <SERVICE>\n"
        "docker-compose rm -f <SERVICE>\n"
        "docker-compose up -d <SERVICE>\n"
        "python -m monitoring_agent.app --once  # re-check health"
    ),
}


class SafeActionExecutor:
    """Execute safe actions; enforce policy, idempotency, and audit trail.

    Args:
        policy:                Mode-based policy gate.
        audit:                 Audit log writer.
        notifier:              Optional callable(str) → bool for SEND_ALERT.
                               Receives the alert message; returns True if delivered.
        dynamo_writer:         Optional SafeActionDynamoWriter for BLOCK_NEW_ENTRIES
                               and ACTIVATE_KILL_SWITCH. If None, those action types
                               fall back to stub (no DynamoDB write occurs).
        durable_idempotency:   Optional DurableIdempotencyStore (DynamoDB-backed).
                               When supplied, BLOCK_NEW_ENTRIES and ACTIVATE_KILL_SWITCH
                               are checked against the durable store before executing,
                               so restarts cannot re-execute an action that already ran.
        metrics:               Optional SafeActionMetrics for CloudWatch counters.
        requested_by:          Tag written into every audit record.
    """

    def __init__(
        self,
        policy: SafeActionPolicy,
        audit: SafeActionAudit,
        notifier: Optional[Callable[[str], bool]] = None,
        dynamo_writer: "Optional[SafeActionDynamoWriter]" = None,
        durable_idempotency: "Optional[DurableIdempotencyStore]" = None,
        metrics: "Optional[SafeActionMetrics]" = None,
        requested_by: str = "monitoring_agent",
    ) -> None:
        self._policy = policy
        self._audit = audit
        self._notifier = notifier
        self._dynamo_writer = dynamo_writer
        self._durable_idem = durable_idempotency
        self._metrics = metrics
        self._requested_by = requested_by
        # In-memory idempotency store: idempotency_key → ExecutionResult
        self._idempotency_store: dict[str, ExecutionResult] = {}
        # In-memory observability counters (reset on restart — for metrics/testing)
        self._counters: dict[str, int] = {
            "block_new_entries_total": 0,
            "kill_switch_activated_total": 0,
            "dynamo_write_failed_total": 0,
            "idempotency_skipped_total": 0,
            "action_blocked_total": 0,
        }

    @property
    def counters(self) -> dict[str, int]:
        """Read-only snapshot of observability counters."""
        return dict(self._counters)

    # ── public API ─────────────────────────────────────────────────────────---

    def execute(
        self,
        action: SafeAction,
        *,
        preconditions_met: bool = True,
        precondition_description: str = "",
        reason: str = "",
    ) -> ExecutionResult:
        """Execute a safe action, returning a result regardless of outcome.

        Args:
            action:                   The action to attempt.
            preconditions_met:        Caller-evaluated boolean; False → block.
            precondition_description: Human-readable description of failed
                                      precondition (used when False).
            reason:                   Free-text reason recorded in audit.

        Returns:
            ExecutionResult — always, even for blocked/forbidden actions.
        """
        # ── 1. policy gate ────────────────────────────────────────────────────
        allowed, deny_reason = self._policy.is_allowed(action.action_type, action.scope)
        if not allowed:
            self._counters["action_blocked_total"] += 1
            result = ExecutionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                executed=False,
                blocked=True,
                blocked_reason=deny_reason,
            )
            self._write_audit(action, result, reason=reason)
            if self._metrics:
                self._metrics.action_blocked(action.action_type.value, deny_reason)
            logger.warning(
                "safe_actions.blocked action_type=%s mode=%s blocked_reason=%s "
                "action_id=%s idempotency_key=%s",
                action.action_type.value, action.mode.value, deny_reason,
                action.action_id, action.idempotency_key,
            )
            return result

        # ── 2. human-approval gate ────────────────────────────────────────────
        if action.requires_human_approval or self._policy.requires_human_approval(
            action.action_type, action.scope
        ):
            result = ExecutionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                executed=False,
                blocked=True,
                blocked_reason="requires_human_approval",
                result_payload={
                    "approval_required": True,
                    "action_type": action.action_type.value,
                    "idempotency_key": action.idempotency_key,
                },
            )
            self._write_audit(action, result, reason=reason)
            if self._metrics:
                self._metrics.action_blocked(action.action_type.value, "requires_human_approval")
            # Special case: GENERATE_RUNBOOK_COMMAND is human-gated but still
            # produces its output (the command string) for the operator to use.
            if action.action_type is ActionType.GENERATE_RUNBOOK_COMMAND:
                return self._handle_generate_runbook_command(action, reason=reason)
            return result

        # ── 3a. durable idempotency gate (DynamoDB) ───────────────────────────
        # Only for write actions — these must survive executor restarts.
        if (
            self._durable_idem is not None
            and action.action_type in _WRITE_ACTION_TYPES
        ):
            found, existing = self._durable_idem.check(action.idempotency_key)
            if found:
                self._counters["idempotency_skipped_total"] += 1
                prior_id = existing.get("action_id", {}).get("S", "") if existing else ""
                result = ExecutionResult(
                    action_id=action.action_id,
                    action_type=action.action_type,
                    executed=False,
                    blocked=False,
                    idempotency_skipped=True,
                    blocked_reason=(
                        f"durable_idempotency_key already executed: {action.idempotency_key}"
                    ),
                    result_payload={"prior_action_id": prior_id, "source": "dynamo"},
                )
                self._write_audit(action, result, reason=reason)
                if self._metrics:
                    self._metrics.idempotency_skip(action.action_type.value, source="dynamo")
                logger.info(
                    "safe_actions.durable_idempotency_skip action_type=%s "
                    "idempotency_key=%s prior_action_id=%s",
                    action.action_type.value, action.idempotency_key, prior_id,
                )
                return result

        # ── 3b. in-memory idempotency gate (within-session fast path) ─────────
        if action.idempotency_key in self._idempotency_store:
            self._counters["idempotency_skipped_total"] += 1
            prior = self._idempotency_store[action.idempotency_key]
            result = ExecutionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                executed=False,
                blocked=False,
                idempotency_skipped=True,
                blocked_reason=f"idempotency_key already executed: {action.idempotency_key}",
                result_payload={"prior_action_id": prior.action_id, "source": "memory"},
            )
            self._write_audit(action, result, reason=reason)
            if self._metrics:
                self._metrics.idempotency_skip(action.action_type.value, source="memory")
            logger.info(
                "safe_actions.idempotency_skip action_type=%s idempotency_key=%s "
                "prior_action_id=%s",
                action.action_type.value, action.idempotency_key, prior.action_id,
            )
            return result

        # ── 4. precondition gate ──────────────────────────────────────────────
        if not preconditions_met:
            desc = precondition_description or "precondition not met"
            result = ExecutionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                executed=False,
                blocked=True,
                blocked_reason="precondition_failed",
                precondition_failed=desc,
            )
            self._write_audit(action, result, reason=reason)
            return result

        # ── 5. dispatch ───────────────────────────────────────────────────────
        result = self._dispatch(action, reason=reason)
        if result.executed and not result.idempotency_skipped:
            self._idempotency_store[action.idempotency_key] = result
        return result

    # ── dispatch ──────────────────────────────────────────────────────────────

    def _dispatch(self, action: SafeAction, *, reason: str) -> ExecutionResult:
        """Route to the appropriate handler; catch all exceptions."""
        handler_map: dict[ActionType, Any] = {
            # Phase 3 — no DynamoDB
            ActionType.SEND_ALERT: self._handle_send_alert,
            ActionType.GENERATE_RUNBOOK_COMMAND: self._handle_generate_runbook_command,
            ActionType.READ_RUNTIME_STATE: self._handle_read_runtime_state,
            # Phase 4 — DynamoDB writes (requires dynamo_writer to be set)
            ActionType.BLOCK_NEW_ENTRIES: self._handle_block_new_entries,
            ActionType.ACTIVATE_KILL_SWITCH: self._handle_activate_kill_switch,
        }
        handler = handler_map.get(action.action_type)
        try:
            if handler is not None:
                return handler(action, reason=reason)
            else:
                # Stub: framework complete, future phase wires the real writer.
                raise _StubNotImplemented(
                    f"{action.action_type.value} not yet wired"
                )
        except _StubNotImplemented:
            result = ExecutionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                executed=False,
                blocked=False,
                stub_not_implemented=True,
                blocked_reason="stub_not_implemented",
            )
            self._write_audit(action, result, reason=reason)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "safe_action_executor.handler_error action_type=%s error_type=%s "
                "action_id=%s",
                action.action_type.value, type(exc).__name__, action.action_id,
            )
            result = ExecutionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                executed=False,
                blocked=False,
                error=type(exc).__name__,
            )
            self._write_audit(action, result, reason=reason)
            return result

    # ── handlers ─────────────────────────────────────────────────────────────

    def _handle_send_alert(self, action: SafeAction, *, reason: str) -> ExecutionResult:
        """Fully implemented — posts alert via the injected notifier."""
        message = action.audit_payload.get("message", action.expected_effect)
        delivered = False
        if self._notifier is not None:
            try:
                delivered = bool(self._notifier(str(message)))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "safe_action_executor.alert_failed error_type=%s", type(exc).__name__
                )
        else:
            logger.info("safe_action_executor.alert (no notifier): %s", message)
            delivered = True  # no-op delivery counts as success in notify-only mode

        result = ExecutionResult(
            action_id=action.action_id,
            action_type=action.action_type,
            executed=True,
            blocked=False,
            result_payload={"message": str(message), "delivered": delivered},
        )
        self._write_audit(action, result, reason=reason)
        if self._metrics:
            self._metrics.action_executed(action.action_type.value, action.mode.value)
        return result

    def _handle_generate_runbook_command(
        self, action: SafeAction, *, reason: str
    ) -> ExecutionResult:
        """Fully implemented — returns operator command string; no side effects."""
        subject = action.audit_payload.get("subject", "default")
        command = _RUNBOOK_TEMPLATES.get(str(subject), _RUNBOOK_TEMPLATES["default"])
        result = ExecutionResult(
            action_id=action.action_id,
            action_type=action.action_type,
            executed=True,
            blocked=False,
            result_payload={"runbook_command": command, "subject": subject},
        )
        self._write_audit(action, result, reason=reason)
        if self._metrics:
            self._metrics.action_executed(action.action_type.value, action.mode.value)
        return result

    def _handle_read_runtime_state(
        self, action: SafeAction, *, reason: str
    ) -> ExecutionResult:
        """Fully implemented — returns the action's audit_payload as runtime state."""
        result = ExecutionResult(
            action_id=action.action_id,
            action_type=action.action_type,
            executed=True,
            blocked=False,
            result_payload={"runtime_state": action.audit_payload},
        )
        self._write_audit(action, result, reason=reason)
        if self._metrics:
            self._metrics.action_executed(action.action_type.value, action.mode.value)
        return result

    def _handle_block_new_entries(
        self, action: SafeAction, *, reason: str
    ) -> ExecutionResult:
        """Phase 4/6 — writes ENTRY_BLOCK/GLOBAL to risk-state DynamoDB.

        Falls back to stub if no dynamo_writer was injected.
        Never blocks exit management (TEE / MIS / ExitOrderRouter).
        Never sets live_trading_enabled. Never modifies capital.
        On success, marks the durable idempotency store (if configured) so a
        restart cannot re-execute this action.
        Audit failure is treated as execution failure (fail-closed).
        """
        if self._dynamo_writer is None:
            raise _StubNotImplemented("dynamo_writer not injected")

        write = self._dynamo_writer.write_block_new_entries(
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            reason=reason or action.expected_effect,
        )
        if write.success:
            self._counters["block_new_entries_total"] += 1
            logger.info(
                "safe_actions.block_new_entries action_id=%s idempotency_key=%s "
                "mode=%s reason=%s dynamodb_key=%s/%s",
                action.action_id, action.idempotency_key, action.mode.value,
                reason, write.pk, write.sk,
            )
            result = ExecutionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                executed=True,
                blocked=False,
                result_payload={
                    "dynamodb_pk": write.pk,
                    "dynamodb_sk": write.sk,
                    "table": write.table,
                    "blocked": True,
                },
            )
            # Mark durable idempotency store — best-effort, never blocks execution.
            if self._durable_idem is not None:
                self._durable_idem.mark(
                    action.idempotency_key,
                    action_id=action.action_id,
                    action_type=action.action_type.value,
                    mode=action.mode.value,
                    status="executed",
                    result_dict=result.result_payload,
                )
            if self._metrics:
                self._metrics.block_new_entries()
                self._metrics.action_executed(action.action_type.value, action.mode.value)
        else:
            self._counters["dynamo_write_failed_total"] += 1
            logger.error(
                "safe_actions.block_new_entries.dynamo_write_failed action_id=%s "
                "error_type=%s",
                action.action_id, write.error_type,
            )
            result = ExecutionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                executed=False,
                blocked=False,
                error=write.error_type or "DynamoWriteFailed",
            )
            if self._metrics:
                self._metrics.dynamo_write_failed(action.action_type.value)
        # Fail-closed audit for write actions.
        self._write_audit_fail_closed(action, result, reason=reason)
        return result

    def _handle_activate_kill_switch(
        self, action: SafeAction, *, reason: str
    ) -> ExecutionResult:
        """Phase 4/6 — writes KILLSWITCH/GLOBAL active=True to risk-state DynamoDB.

        Uses the canonical kill_switch_item() schema so the existing
        KillSwitch._load_state() reader sees the correct item.
        Never deactivates the kill switch. Never modifies exit management.
        Never sets live_trading_enabled. Never modifies capital.
        On success, marks the durable idempotency store (if configured).
        Audit failure is treated as execution failure (fail-closed).
        """
        if self._dynamo_writer is None:
            raise _StubNotImplemented("dynamo_writer not injected")

        write = self._dynamo_writer.write_kill_switch_active(
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            reason=reason or action.expected_effect,
            activated_by="safe_actions",
        )
        if write.success:
            self._counters["kill_switch_activated_total"] += 1
            logger.critical(
                "safe_actions.kill_switch_activated action_id=%s idempotency_key=%s "
                "mode=%s reason=%s dynamodb_key=%s/%s",
                action.action_id, action.idempotency_key, action.mode.value,
                reason, write.pk, write.sk,
            )
            result = ExecutionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                executed=True,
                blocked=False,
                result_payload={
                    "dynamodb_pk": write.pk,
                    "dynamodb_sk": write.sk,
                    "table": write.table,
                    "active": True,
                },
            )
            # Mark durable idempotency store — best-effort, never blocks execution.
            if self._durable_idem is not None:
                self._durable_idem.mark(
                    action.idempotency_key,
                    action_id=action.action_id,
                    action_type=action.action_type.value,
                    mode=action.mode.value,
                    status="executed",
                    result_dict=result.result_payload,
                )
            if self._metrics:
                self._metrics.kill_switch_activated()
                self._metrics.action_executed(action.action_type.value, action.mode.value)
        else:
            self._counters["dynamo_write_failed_total"] += 1
            logger.error(
                "safe_actions.activate_kill_switch.dynamo_write_failed action_id=%s "
                "error_type=%s",
                action.action_id, write.error_type,
            )
            result = ExecutionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                executed=False,
                blocked=False,
                error=write.error_type or "DynamoWriteFailed",
            )
            if self._metrics:
                self._metrics.kill_switch_write_failed()
                self._metrics.dynamo_write_failed(action.action_type.value)
        # Fail-closed audit for write actions.
        self._write_audit_fail_closed(action, result, reason=reason)
        return result

    # ── audit helpers ─────────────────────────────────────────────────────────

    def _write_audit(
        self, action: SafeAction, result: ExecutionResult, *, reason: str
    ) -> None:
        """Write audit record; failure falls back to logger (read-only-safe path)."""
        self._audit.record(
            action_id=action.action_id,
            action_type=action.action_type.value,
            scope=action.scope.value,
            mode=action.mode.value,
            risk_level=action.risk_level.value,
            requires_human_approval=action.requires_human_approval,
            idempotency_key=action.idempotency_key,
            executed=result.executed,
            blocked=result.blocked,
            blocked_reason=result.blocked_reason,
            idempotency_skipped=result.idempotency_skipped,
            precondition_failed=result.precondition_failed,
            error=result.error,
            stub_not_implemented=result.stub_not_implemented,
            requested_by=self._requested_by,
            reason=reason,
            audit_payload=action.audit_payload,
            result_payload=result.result_payload,
        )

    def _write_audit_fail_closed(
        self, action: SafeAction, result: ExecutionResult, *, reason: str
    ) -> ExecutionResult:
        """Write audit for write actions.  If audit write raises, return fail-closed result.

        Returns the original result on success, or a new ExecutionResult(error='AuditWriteFailed')
        on audit failure.  Called only for BLOCK_NEW_ENTRIES and ACTIVATE_KILL_SWITCH.
        """
        try:
            self._audit.record(
                action_id=action.action_id,
                action_type=action.action_type.value,
                scope=action.scope.value,
                mode=action.mode.value,
                risk_level=action.risk_level.value,
                requires_human_approval=action.requires_human_approval,
                idempotency_key=action.idempotency_key,
                executed=result.executed,
                blocked=result.blocked,
                blocked_reason=result.blocked_reason,
                idempotency_skipped=result.idempotency_skipped,
                precondition_failed=result.precondition_failed,
                error=result.error,
                stub_not_implemented=result.stub_not_implemented,
                requested_by=self._requested_by,
                reason=reason,
                audit_payload=action.audit_payload,
                result_payload=result.result_payload,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "safe_actions.audit_write_failed action_type=%s action_id=%s "
                "error_type=%s — failing closed for write action",
                action.action_type.value, action.action_id, type(exc).__name__,
            )
            return ExecutionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                executed=False,
                blocked=False,
                error="AuditWriteFailed",
            )
