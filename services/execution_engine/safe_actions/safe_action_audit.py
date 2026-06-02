"""Append-only audit log for every safe action attempt.

Every call to SafeActionExecutor.execute() — including blocked and forbidden
actions — writes one record here. The audit log is the authoritative history
of what the safe_actions layer attempted, approved, and refused.

Format: newline-delimited JSON (JSONL), one record per line.
Fields written: timestamp, action_id, action_type, scope, mode, risk_level,
    requires_human_approval, idempotency_key, executed, blocked, blocked_reason,
    idempotency_skipped, precondition_failed, error, stub_not_implemented,
    requested_by, reason, result_payload.

Secrets are never written here. The audit_payload from SafeAction is included
verbatim — the caller is responsible for keeping it secret-free (the same
contract as Phase 1 collector details).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("safe_actions.audit")

_DEFAULT_PATH = "/app/data/safe_actions_audit.jsonl"


class SafeActionAudit:
    """Append-only JSONL writer for safe action records.

    Thread-safe for the single-loop monitoring agent use case (one write per
    cycle). For concurrent use, wrap writes with an external lock.

    The audit log is written atomically per-record (one ``write + flush``).
    If the file cannot be opened, audit records are emitted at WARNING level
    to the process logger so they still appear in CloudWatch Logs.
    """

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def record(
        self,
        *,
        action_id: str,
        action_type: str,
        scope: str,
        mode: str,
        risk_level: str,
        requires_human_approval: bool,
        idempotency_key: str,
        executed: bool,
        blocked: bool,
        blocked_reason: Optional[str] = None,
        idempotency_skipped: bool = False,
        precondition_failed: Optional[str] = None,
        error: Optional[str] = None,
        stub_not_implemented: bool = False,
        requested_by: str = "monitoring_agent",
        reason: str = "",
        audit_payload: dict[str, Any] | None = None,
        result_payload: dict[str, Any] | None = None,
    ) -> None:
        """Write one audit record. Never raises — failure falls back to log."""
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action_id": action_id,
            "action_type": action_type,
            "scope": scope,
            "mode": mode,
            "risk_level": risk_level,
            "requires_human_approval": requires_human_approval,
            "idempotency_key": idempotency_key,
            "executed": executed,
            "blocked": blocked,
            "blocked_reason": blocked_reason,
            "idempotency_skipped": idempotency_skipped,
            "precondition_failed": precondition_failed,
            "error": error,
            "stub_not_implemented": stub_not_implemented,
            "requested_by": requested_by,
            "reason": reason,
            "audit_payload": audit_payload or {},
            "result_payload": result_payload or {},
        }
        line = json.dumps(entry, default=str, ensure_ascii=False)
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
        except OSError as exc:
            # Fall back to structured log so the record appears in CloudWatch.
            logger.warning(
                "safe_action_audit.write_failed path=%s error_type=%s audit_record=%s",
                self._path,
                type(exc).__name__,
                line,
            )

    def read_all(self) -> list[dict[str, Any]]:
        """Return all records from the audit log (for inspection/testing)."""
        try:
            with open(self._path, encoding="utf-8") as fh:
                return [
                    json.loads(line)
                    for line in fh
                    if line.strip()
                ]
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("safe_action_audit.read_failed error_type=%s", type(exc).__name__)
            return []
