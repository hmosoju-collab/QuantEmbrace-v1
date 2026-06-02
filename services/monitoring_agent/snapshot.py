"""Health snapshot data model for the monitoring agent.

A :class:`HealthSnapshot` is the canonical Phase 1 output: an immutable,
serialisable picture of platform health at one point in time, assembled from the
results of every enabled collector. It is written to disk, logged as structured
JSON, and used to decide whether a Slack alert is warranted.

Collectors populate only the *coarse* per-component ``Status``
(reachability/liveness). Fine-grained INFO/WARNING/CRITICAL/BLOCKER severity is a
Phase 2 concern owned by the detector + severity engine; it is attached here as an
optional, additive ``severity`` enrichment (see :attr:`HealthSnapshot.severity`)
and never replaces ``Status`` nor changes how alerts are gated.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class Status(str, enum.Enum):
    """Coarse Phase 1 health status for a single collector result.

    ``str`` subclass so it serialises directly to JSON as its value.
    """

    OK = "ok"            # Component reachable and behaving as expected.
    DEGRADED = "degraded"  # Reachable but impaired (e.g. /ready 503, lag elevated).
    DOWN = "down"        # Unreachable / not running — operator attention required.
    UNKNOWN = "unknown"  # Could not determine (collector error, optional dep missing).


# Worst-wins ordering used to roll component statuses up into an overall status.
# OK is best; DOWN is worst. UNKNOWN sits above OK (it means "we are blind here")
# but below an actively-impaired or actively-down component.
_SEVERITY_ORDER: dict[Status, int] = {
    Status.OK: 0,
    Status.UNKNOWN: 1,
    Status.DEGRADED: 2,
    Status.DOWN: 3,
}

# Statuses that warrant a Phase 1 alert when a component transitions into them.
ALERTING_STATUSES: frozenset[Status] = frozenset({Status.DEGRADED, Status.DOWN, Status.UNKNOWN})


def worst(statuses: list[Status]) -> Status:
    """Return the worst (highest-severity) status in the list, OK if empty."""
    if not statuses:
        return Status.OK
    return max(statuses, key=lambda s: _SEVERITY_ORDER[s])


@dataclass
class CollectorResult:
    """The outcome of a single collector run.

    Attributes:
        name:        Stable collector identifier (e.g. ``"kafka"``, ``"services"``).
        status:      Coarse :class:`Status` for this component.
        summary:     One-line human-readable summary (safe to put in a Slack alert).
        details:     Structured, secret-free metrics for the snapshot file/logs.
        error:       Populated only when the collector itself failed to run.
        collected_at: ISO-8601 UTC timestamp of when this result was produced.
        duration_ms: Wall-clock time the collector took, for self-observability.
    """

    name: str
    status: Status
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    collected_at: str = field(default_factory=_utc_now_iso)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
            "details": self.details,
            "error": self.error,
            "collected_at": self.collected_at,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class HealthSnapshot:
    """Aggregate health of the platform at one collection cycle.

    Attributes:
        results:     One :class:`CollectorResult` per enabled collector.
        dry_run:     Echo of the agent's DRY_RUN flag (always True in Phase 1).
        action_mode: Echo of ACTION_MODE (always ``notify_only`` in Phase 1).
        generated_at: ISO-8601 UTC timestamp for the whole snapshot.
        phase:       Agent phase that produced this snapshot.
        severity:    Optional Phase 2 enrichment (the SeverityEngine's
                     :meth:`SeverityReport.to_dict`). ``None`` until the engine
                     runs; serialised only when present. Purely additive — it
                     never affects :attr:`overall_status`.
    """

    results: list[CollectorResult]
    dry_run: bool = True
    action_mode: str = "notify_only"
    generated_at: str = field(default_factory=_utc_now_iso)
    phase: int = 1
    severity: Optional[dict[str, Any]] = None

    @property
    def overall_status(self) -> Status:
        """Worst component status across all results."""
        return worst([r.status for r in self.results])

    def results_by_status(self, status: Status) -> list[CollectorResult]:
        return [r for r in self.results if r.status == status]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "generated_at": self.generated_at,
            "phase": self.phase,
            "dry_run": self.dry_run,
            "action_mode": self.action_mode,
            "overall_status": self.overall_status.value,
            "results": [r.to_dict() for r in self.results],
        }
        if self.severity is not None:
            out["severity"] = self.severity
        return out

    def to_json(self, *, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str, ensure_ascii=False)
