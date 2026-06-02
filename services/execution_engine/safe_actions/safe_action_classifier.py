"""Phase 3 classifier: maps Phase 2 findings and raw observations to
ClassifiedObservation, proposing appropriate safe action types.

Design rules:
    * The classifier is pure (no I/O) — it only reads the finding/observation
      it is handed and the static rule tables below.
    * It NEVER proposes a Docker container restart (suppress_docker_restart
      is always True on every ClassifiedObservation).
    * ai_engine DOWN → ALERT_ONLY + BLOCK_NEW_ENTRIES (AI-dependent entries
      blocked). Never KILL_SWITCH. Never Docker restart.
    * Any critical component (execution_engine / risk_engine) DOWN →
      KILL_SWITCH as last resort.
    * Stale LTP → BLOCK_ENTRIES only; exit management continues.
    * All proposals are advisory; SafeActionPolicy enforces the final gate.
"""

from __future__ import annotations

from typing import Any

from execution_engine.safe_actions.safe_action_models import (
    ActionType,
    ClassificationResult,
    ClassifiedObservation,
)

# ── finding-code classification table ─────────────────────────────────────────
# Keys are Phase 2 finding codes from detectors/engine.py.
# Tuple: (ClassificationResult, [proposed ActionTypes], reasoning)

_CODE_TABLE: dict[
    str,
    tuple[ClassificationResult, list[ActionType], str],
] = {
    # ── service findings ──────────────────────────────────────────────────────
    "service.down": (
        ClassificationResult.KILL_SWITCH,
        [ActionType.ACTIVATE_KILL_SWITCH, ActionType.SEND_ALERT],
        "Critical service unreachable — unmanaged exposure risk",
    ),
    "service.degraded": (
        ClassificationResult.ALERT_ONLY,
        [ActionType.SEND_ALERT],
        "Service degraded but reachable — alert and monitor",
    ),
    # ── kafka findings ────────────────────────────────────────────────────────
    "kafka.unreachable": (
        ClassificationResult.KILL_SWITCH,
        [ActionType.ACTIVATE_KILL_SWITCH, ActionType.SEND_ALERT],
        "Kafka event bus down — signals cannot flow; trading must pause",
    ),
    "kafka.missing_topics": (
        ClassificationResult.BLOCK_ENTRIES,
        [ActionType.BLOCK_NEW_ENTRIES, ActionType.SEND_ALERT],
        "Critical topic missing — signal path broken; block new entries",
    ),
    "kafka.group_idle": (
        ClassificationResult.ALERT_ONLY,
        [ActionType.SEND_ALERT, ActionType.READ_RUNTIME_STATE],
        "Consumer group has no committed offsets — consumer not running",
    ),
    "kafka.group_lag_critical": (
        ClassificationResult.BLOCK_ENTRIES,
        [ActionType.BLOCK_NEW_ENTRIES, ActionType.SEND_ALERT],
        "Consumer lag at critical threshold — signals backed up",
    ),
    "kafka.group_lag_warning": (
        ClassificationResult.ALERT_ONLY,
        [ActionType.SEND_ALERT],
        "Consumer lag elevated — monitor closely",
    ),
    "kafka.group_offset_read_error": (
        ClassificationResult.ALERT_ONLY,
        [ActionType.SEND_ALERT],
        "Could not read committed offsets — state may be stale",
    ),
    # ── dynamodb findings ─────────────────────────────────────────────────────
    "dynamodb.down": (
        ClassificationResult.KILL_SWITCH,
        [ActionType.ACTIVATE_KILL_SWITCH, ActionType.SEND_ALERT],
        "Critical DynamoDB table unavailable — risk/execution state unreachable",
    ),
    "dynamodb.degraded": (
        ClassificationResult.BLOCK_ENTRIES,
        [ActionType.BLOCK_NEW_ENTRIES, ActionType.SEND_ALERT],
        "DynamoDB table degraded — block entries until fully active",
    ),
    # ── broker findings ───────────────────────────────────────────────────────
    "broker.state_unknown": (
        ClassificationResult.ALERT_ONLY,
        [ActionType.SEND_ALERT, ActionType.READ_RUNTIME_STATE],
        "Cannot infer price-feed health — alert and verify manually",
    ),
    "broker.no_fresh_prices": (
        ClassificationResult.BLOCK_ENTRIES,
        [ActionType.BLOCK_NEW_ENTRIES, ActionType.SEND_ALERT],
        "No parseable prices during market hours — block new entries",
    ),
    "broker.feed_very_stale": (
        ClassificationResult.BLOCK_ENTRIES,
        [ActionType.BLOCK_NEW_ENTRIES, ActionType.SEND_ALERT],
        "Price feed very stale (>3× max age) — strategies would trade on stale data",
    ),
    "broker.feed_stale": (
        ClassificationResult.ALERT_ONLY,
        [ActionType.SEND_ALERT],
        "Price feed stale (>max age) — alert; exits unaffected",
    ),
    # ── docker findings ───────────────────────────────────────────────────────
    "docker.restart_loop": (
        ClassificationResult.ALERT_ONLY,
        [ActionType.SEND_ALERT, ActionType.GENERATE_RUNBOOK_COMMAND],
        "Container crash loop — alert and generate restart runbook (human executes)",
    ),
    "docker.restarting": (
        ClassificationResult.ALERT_ONLY,
        [ActionType.SEND_ALERT],
        "Container restarting — monitor; alert if escalates",
    ),
    "docker.down": (
        ClassificationResult.ALERT_ONLY,
        [ActionType.SEND_ALERT, ActionType.GENERATE_RUNBOOK_COMMAND],
        "Container down — alert and generate restart runbook (human executes)",
    ),
    # ── log findings ──────────────────────────────────────────────────────────
    "logs.errors_present": (
        ClassificationResult.ALERT_ONLY,
        [ActionType.SEND_ALERT],
        "Error-pattern matches in recent logs — alert for operator review",
    ),
}

# ── subject-specific overrides ────────────────────────────────────────────────
# Some finding codes have different responses depending on the subject.
# (subject, finding_code) → override tuple.

_SUBJECT_OVERRIDE: dict[
    tuple[str, str],
    tuple[ClassificationResult, list[ActionType], str],
] = {
    # ai_engine is non-critical (EnrichmentWatchdog provides fallback).
    # service.down on ai_engine → ALERT_ONLY + block AI-dependent entries.
    # NEVER KILL_SWITCH. NEVER Docker restart.
    ("ai_engine", "service.down"): (
        ClassificationResult.ALERT_ONLY,
        [ActionType.BLOCK_NEW_ENTRIES, ActionType.SEND_ALERT],
        "ai_engine DOWN — block AI-dependent entries; fallback path active; "
        "alert operator for manual restart (see runbook)",
    ),
    ("ai_engine", "docker.down"): (
        ClassificationResult.ALERT_ONLY,
        [ActionType.BLOCK_NEW_ENTRIES, ActionType.SEND_ALERT, ActionType.GENERATE_RUNBOOK_COMMAND],
        "ai_engine container down — block AI-dependent entries; "
        "generate manual restart runbook; do not auto-restart",
    ),
    ("ai_engine", "docker.restart_loop"): (
        ClassificationResult.ALERT_ONLY,
        [ActionType.BLOCK_NEW_ENTRIES, ActionType.SEND_ALERT, ActionType.GENERATE_RUNBOOK_COMMAND],
        "ai_engine crash loop — block AI-dependent entries; generate runbook; "
        "human must diagnose root cause before restart",
    ),
}

# ── raw observation keys (not from Phase 2 findings) ─────────────────────────
# These map context observations that the agent infers from non-finding sources.

_RAW_OBSERVATION_TABLE: dict[
    str,
    tuple[ClassificationResult, list[ActionType], str],
] = {
    "reconciliation_required_live": (
        ClassificationResult.BLOCK_ENTRIES,
        [ActionType.BLOCK_NEW_ENTRIES, ActionType.SEND_ALERT, ActionType.MARK_LIVE_READINESS_BLOCKED],
        "reconciliation_required=True in live — position state uncertain; block entries",
    ),
    "reconciliation_required_paper": (
        ClassificationResult.RISK_REDUCTION,
        [ActionType.RUN_RECONCILIATION, ActionType.SEND_ALERT],
        "reconciliation_required=True in paper mode — run paper reconciliation",
    ),
    "kill_switch_active": (
        ClassificationResult.ALERT_ONLY,
        [ActionType.SEND_ALERT],
        "Kill switch already ACTIVE — alert and confirm exits are processing",
    ),
    "paper_position_missing_exit_policy": (
        ClassificationResult.PAPER_REPAIR,
        [ActionType.ATTACH_EXIT_POLICY_PAPER, ActionType.SEND_ALERT],
        "Open paper position has no exit policy — attach default SL/TP",
    ),
    "paper_position_zero_qty_open": (
        ClassificationResult.PAPER_REPAIR,
        [ActionType.REPAIR_PAPER_ZERO_QTY_OPEN, ActionType.SEND_ALERT],
        "Paper position has zero quantity but status=OPEN — mark as closed",
    ),
    "paper_position_direction_mismatch": (
        ClassificationResult.PAPER_REPAIR,
        [ActionType.UPDATE_PAPER_DIRECTION_FROM_QUANTITY, ActionType.SEND_ALERT],
        "Paper position direction disagrees with signed quantity — repair direction",
    ),
    "unmanaged_live_position": (
        ClassificationResult.KILL_SWITCH,
        [ActionType.ACTIVATE_KILL_SWITCH, ActionType.SEND_ALERT],
        "Live position with no exit management — activate kill switch immediately",
    ),
    "stale_ltp_market_hours": (
        ClassificationResult.BLOCK_ENTRIES,
        [ActionType.BLOCK_NEW_ENTRIES, ActionType.SEND_ALERT],
        "LTP stale during market hours — block new entries; exits continue",
    ),
}


class SafeActionClassifier:
    """Pure classifier: finding/observation → ClassifiedObservation.

    Never performs I/O. Never proposes a Docker restart. Never proposes
    anything that bypasses risk management or increases exposure.

    Usage::

        clf = SafeActionClassifier()
        obs = clf.classify_finding(code="broker.feed_very_stale", subject="feed")
        # obs.classification == ClassificationResult.BLOCK_ENTRIES
        # obs.proposed_action_types == [ActionType.BLOCK_NEW_ENTRIES, ActionType.SEND_ALERT]
        # obs.suppress_docker_restart == True (always)
    """

    def classify_finding(
        self,
        code: str,
        subject: str = "",
        context: dict[str, Any] | None = None,
    ) -> ClassifiedObservation:
        """Classify a Phase 2 finding by its code and subject.

        Subject-specific overrides take precedence over the code-level default.
        Unknown codes default to ALERT_ONLY.
        """
        context = context or {}

        # Subject override has highest priority
        key = (subject, code)
        if key in _SUBJECT_OVERRIDE:
            cls_result, action_types, reasoning = _SUBJECT_OVERRIDE[key]
        elif code in _CODE_TABLE:
            cls_result, action_types, reasoning = _CODE_TABLE[code]
        else:
            # Unknown finding code — default to alert-only (safe)
            cls_result = ClassificationResult.ALERT_ONLY
            action_types = [ActionType.SEND_ALERT]
            reasoning = f"unknown finding code '{code}' — defaulting to ALERT_ONLY"

        return ClassifiedObservation(
            observation_code=code,
            subject=subject,
            classification=cls_result,
            proposed_action_types=list(action_types),
            reasoning=reasoning,
            suppress_docker_restart=True,  # always
        )

    def classify_raw(self, observation_key: str, subject: str = "") -> ClassifiedObservation:
        """Classify a raw (non-finding) observation by its key.

        Used for observations that come from context state rather than the
        Phase 2 severity engine (e.g. reconciliation_required flag,
        kill switch state, paper position anomalies).

        Unknown keys default to ALERT_ONLY.
        """
        if observation_key in _RAW_OBSERVATION_TABLE:
            cls_result, action_types, reasoning = _RAW_OBSERVATION_TABLE[observation_key]
        else:
            cls_result = ClassificationResult.ALERT_ONLY
            action_types = [ActionType.SEND_ALERT]
            reasoning = f"unknown observation '{observation_key}' — defaulting to ALERT_ONLY"

        return ClassifiedObservation(
            observation_code=observation_key,
            subject=subject,
            classification=cls_result,
            proposed_action_types=list(action_types),
            reasoning=reasoning,
            suppress_docker_restart=True,
        )

    def classify_forbidden_context(self, context_label: str) -> ClassifiedObservation:
        """Return a FORBIDDEN classification for a categorically disallowed context."""
        return ClassifiedObservation(
            observation_code=context_label,
            subject="",
            classification=ClassificationResult.FORBIDDEN,
            proposed_action_types=[ActionType.FORBIDDEN],
            reasoning=f"'{context_label}' is in the forbidden-context list — never execute",
            suppress_docker_restart=True,
        )
