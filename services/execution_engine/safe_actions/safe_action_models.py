"""Data models for the Phase 3 safe_actions layer.

All types are immutable where possible (frozen dataclasses / str enums) so
safe actions can be passed across coroutine and thread boundaries without
defensive copying.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ── enumerations ──────────────────────────────────────────────────────────────


class ActionType(str, enum.Enum):
    """What the executor is being asked to do.

    Every value is either fully implemented (SEND_ALERT, GENERATE_RUNBOOK_COMMAND)
    or stubbed-but-framework-complete for Phase 4 wiring (all others).
    FORBIDDEN is the catch-all for anything that must never execute.
    """

    # ── read / observe ────────────────────────────────────────────────────────
    READ_RUNTIME_STATE = "READ_RUNTIME_STATE"
    """Read-only snapshot of current runtime state (kill switch, token, tables)."""

    # ── alert ─────────────────────────────────────────────────────────────────
    SEND_ALERT = "SEND_ALERT"
    """Post a Slack/SNS alert. Fully implemented in Phase 3."""

    # ── paper repair ──────────────────────────────────────────────────────────
    RUN_RECONCILIATION = "RUN_RECONCILIATION"
    """Trigger a reconciliation pass (paper mode only without live flag)."""

    ATTACH_EXIT_POLICY_PAPER = "ATTACH_EXIT_POLICY_PAPER"
    """Attach a missing stop-loss / take-profit policy to a paper position."""

    REPAIR_PAPER_ZERO_QTY_OPEN = "REPAIR_PAPER_ZERO_QTY_OPEN"
    """Mark a ZERO_QTY paper position as closed to remove the stale record."""

    UPDATE_PAPER_DIRECTION_FROM_QUANTITY = "UPDATE_PAPER_DIRECTION_FROM_QUANTITY"
    """Fix a paper position whose direction field disagrees with signed quantity."""

    FORCE_PAPER_SQUARE_OFF = "FORCE_PAPER_SQUARE_OFF"
    """Simulate an immediate flat fill on all open paper positions (EOD / emergency)."""

    # ── risk reduction ────────────────────────────────────────────────────────
    BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"
    """Write a global block-entries flag to strategy-config DynamoDB.
    Does NOT stop exit management, TEE, or MIS square-off."""

    PAUSE_STRATEGY_ENTRIES = "PAUSE_STRATEGY_ENTRIES"
    """Pause a single strategy from producing new entry signals (targeted BLOCK)."""

    ACTIVATE_KILL_SWITCH = "ACTIVATE_KILL_SWITCH"
    """Set kill switch ACTIVE in DynamoDB risk-state + publish to risk.kill-switch.
    Blocks all new orders immediately; exits can still process."""

    MARK_LIVE_READINESS_BLOCKED = "MARK_LIVE_READINESS_BLOCKED"
    """Write live_readiness=BLOCKED to DynamoDB risk-state."""

    # ── human-gated / advisory ────────────────────────────────────────────────
    GENERATE_RUNBOOK_COMMAND = "GENERATE_RUNBOOK_COMMAND"
    """Return a safe operator command string for human execution. No side effects.
    Fully implemented in Phase 3 — human still runs the command manually."""

    # ── sentinel ──────────────────────────────────────────────────────────────
    FORBIDDEN = "FORBIDDEN"
    """This action type must never execute. Policy always blocks it."""


class ActionScope(str, enum.Enum):
    """Blast radius of a safe action — used by SafeActionPolicy for mode gating."""

    READ_ONLY = "READ_ONLY"
    """No state changes; reads and alerts only."""

    PAPER_ONLY = "PAPER_ONLY"
    """Modifies paper simulation state. Forbidden in live modes."""

    RISK_REDUCTION = "RISK_REDUCTION"
    """Modifies live/paper state to reduce exposure (block, pause, kill switch)."""

    LIVE_HUMAN_GATED = "LIVE_HUMAN_GATED"
    """Produces an advisory artifact; a human must execute the actual change."""

    FORBIDDEN = "FORBIDDEN"
    """This scope must never be executed by the autonomous layer."""


class RiskLevel(str, enum.Enum):
    """Operator-facing severity of the safe action itself."""

    LOW = "LOW"
    """Easily reversible; unlikely to cause disruption."""

    MEDIUM = "MEDIUM"
    """Requires care; operator should review before Phase 4 automation."""

    HIGH = "HIGH"
    """Risk-reducing but operationally significant; always audit-trailed."""


class TradingMode(str, enum.Enum):
    """Runtime trading mode, resolved from env vars by the policy factory.

    Mapped from:
        PAPER        : RISK_PROFILE=paper, QE_EXECUTION_LIVE_TRADING_ENABLED absent/false
        BACKTEST     : running under backtester context
        LIVE_DISABLED: live flag absent or false, any RISK_PROFILE
        LIVE_STAGE_1 : RISK_PROFILE=tiny-live, QE_EXECUTION_LIVE_TRADING_ENABLED=true
        LIVE_FULL_BLOCKED: unknown / emergency blocked state
    """

    PAPER = "PAPER"
    BACKTEST = "BACKTEST"
    LIVE_DISABLED = "LIVE_DISABLED"
    LIVE_STAGE_1 = "LIVE_STAGE_1"
    LIVE_FULL_BLOCKED = "LIVE_FULL_BLOCKED"


class ClassificationResult(str, enum.Enum):
    """What the classifier recommends doing about an observation."""

    NO_ACTION = "NO_ACTION"
    """Component is healthy; no action warranted."""

    ALERT_ONLY = "ALERT_ONLY"
    """Send a Slack/SNS alert. No state change."""

    READ_ONLY_VERIFY = "READ_ONLY_VERIFY"
    """Run a read-only verification script; no state mutation."""

    PAPER_REPAIR = "PAPER_REPAIR"
    """Apply a paper-state repair action (paper mode only)."""

    RISK_REDUCTION = "RISK_REDUCTION"
    """Apply a risk-reducing action (block entries / pause strategy)."""

    BLOCK_ENTRIES = "BLOCK_ENTRIES"
    """Block new entry signals platform-wide. Exit management continues."""

    KILL_SWITCH = "KILL_SWITCH"
    """Activate the kill switch. Reserved for critical unmanaged exposure."""

    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
    """Generate runbook command; human executes it."""

    FORBIDDEN = "FORBIDDEN"
    """The requested operation is categorically forbidden."""


# ── core models ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SafeAction:
    """A single proposed safe action — immutable, fully described, auditable.

    Build instances via :meth:`SafeAction.build` (generates action_id automatically).
    """

    action_id: str
    """Unique identifier for this specific action instance (UUID4)."""

    action_type: ActionType
    """What to do."""

    scope: ActionScope
    """Blast radius — used for mode-based policy gating."""

    mode: TradingMode
    """Trading mode at the time the action was proposed."""

    risk_level: RiskLevel
    """Operator-facing severity of this action."""

    requires_human_approval: bool
    """If True, the executor will record and return without executing."""

    idempotency_key: str
    """Stable key derived from (action_type, subject, date). Prevents duplicates."""

    preconditions: tuple[str, ...]
    """Descriptions of conditions that must hold before execution."""

    expected_effect: str
    """One sentence describing the observable effect if executed."""

    rollback_behavior: str
    """How to undo this action; 'N/A — read-only' for non-mutating actions."""

    audit_payload: dict[str, Any] = field(default_factory=dict)
    """Secret-free key/value pairs recorded in the audit log."""

    @classmethod
    def build(
        cls,
        action_type: ActionType,
        scope: ActionScope,
        mode: TradingMode,
        risk_level: RiskLevel,
        requires_human_approval: bool,
        idempotency_key: str,
        preconditions: list[str],
        expected_effect: str,
        rollback_behavior: str,
        audit_payload: dict[str, Any] | None = None,
    ) -> "SafeAction":
        """Construct a SafeAction, generating a fresh action_id."""
        return cls(
            action_id=str(uuid.uuid4()),
            action_type=action_type,
            scope=scope,
            mode=mode,
            risk_level=risk_level,
            requires_human_approval=requires_human_approval,
            idempotency_key=idempotency_key,
            preconditions=tuple(preconditions),
            expected_effect=expected_effect,
            rollback_behavior=rollback_behavior,
            audit_payload=audit_payload or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "scope": self.scope.value,
            "mode": self.mode.value,
            "risk_level": self.risk_level.value,
            "requires_human_approval": self.requires_human_approval,
            "idempotency_key": self.idempotency_key,
            "preconditions": list(self.preconditions),
            "expected_effect": self.expected_effect,
            "rollback_behavior": self.rollback_behavior,
            "audit_payload": self.audit_payload,
        }


@dataclass
class ExecutionResult:
    """Outcome of one SafeActionExecutor.execute() call.

    Always produced — including for blocked and forbidden actions — so the
    caller can unconditionally log or forward it.
    """

    action_id: str
    action_type: ActionType
    executed: bool
    """True only when the action ran to completion without error."""

    blocked: bool
    """True when policy, preconditions, or human-gate prevented execution."""

    blocked_reason: Optional[str] = None
    """Machine-readable reason the action was blocked."""

    idempotency_skipped: bool = False
    """True when this idempotency_key was already executed in this session."""

    precondition_failed: Optional[str] = None
    """Description of the precondition that was not met."""

    error: Optional[str] = None
    """Exception type (never full traceback — avoids leaking stack frames)."""

    stub_not_implemented: bool = False
    """True when the action type is framework-complete but Phase 4 not yet wired."""

    result_payload: dict[str, Any] = field(default_factory=dict)
    """Action-specific output (e.g. runbook command string for GENERATE_RUNBOOK_COMMAND)."""

    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "executed": self.executed,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "idempotency_skipped": self.idempotency_skipped,
            "precondition_failed": self.precondition_failed,
            "error": self.error,
            "stub_not_implemented": self.stub_not_implemented,
            "result_payload": self.result_payload,
            "timestamp": self.timestamp,
        }


@dataclass
class ClassifiedObservation:
    """The classifier's verdict on one Phase 2 finding or raw observation.

    The executor does not act directly on a ClassifiedObservation; the caller
    turns it into a SafeAction (using the proposed_action_types as a guide)
    before calling SafeActionExecutor.execute().
    """

    observation_code: str
    """Phase 2 finding code (e.g. 'broker.feed_very_stale') or raw key."""

    subject: str
    """Component or sub-component being classified (e.g. 'execution_engine')."""

    classification: ClassificationResult
    """Recommended response category."""

    proposed_action_types: list[ActionType]
    """Ordered list of action types to consider, most important first."""

    reasoning: str
    """One sentence explaining why this classification was chosen."""

    suppress_docker_restart: bool = True
    """Always True — confirms the classifier never proposes a Docker restart."""
