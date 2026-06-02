"""Finding data model + Detector base class — Phase 2.

A *detector* is the Phase 2 counterpart to a Phase 1 *collector*. Where a
collector answers "is this component reachable?" and returns a coarse
:class:`~monitoring_agent.snapshot.CollectorResult`, a detector consumes that
result's already-collected ``details`` and classifies it into zero or more
:class:`Finding` objects on the INFO/WARNING/CRITICAL/BLOCKER ladder.

Two invariants, mirroring the collector contract:

    1. **Pure / read-only.** A detector performs *no* I/O. It only reads the
       ``CollectorResult`` it is handed (status, details, error) and the rules
       thresholds already baked into those details. It never touches Kafka,
       DynamoDB, Docker, the broker, the network, or the filesystem. This is what
       keeps Phase 2 strictly observe-only.
    2. **Never throw.** A detector that hits unexpected data must degrade, not
       raise — the :class:`~monitoring_agent.detectors.engine.SeverityEngine`
       also wraps every call defensively, but detectors should be written to
       tolerate missing/oddly-typed keys (collectors already guarantee secret-free
       details, but not a rigid schema across versions).

Findings are deliberately small and secret-free: a stable machine ``code``, a
human ``message`` built only from non-sensitive fields, and a numeric ``context``
map. They are safe to serialise into the snapshot, structured logs, and Slack.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from monitoring_agent.detectors.severity import Severity, severity_rank
from monitoring_agent.snapshot import CollectorResult


@dataclass(frozen=True)
class Finding:
    """One classified observation about a single component or sub-component.

    Attributes:
        component: Owning collector name (e.g. ``"kafka"``, ``"services"``).
        severity:  Where this sits on the Phase 2 ladder.
        code:      Stable, machine-readable identifier (e.g. ``"kafka.group_lag_critical"``).
        message:   One-line human-readable explanation — secret-free.
        subject:   The specific sub-item this is about (group id / table / service),
                   empty when the finding is about the component as a whole.
        context:   Small, secret-free numeric/string context (lag counts, ages…).
    """

    component: str
    severity: Severity
    code: str
    message: str
    subject: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "subject": self.subject,
            "severity": self.severity.value,
            "rank": severity_rank(self.severity),
            "code": self.code,
            "message": self.message,
            "context": self.context,
        }


class Detector(abc.ABC):
    """Abstract, pure classifier from a :class:`CollectorResult` to findings.

    Attributes:
        component: The collector name this detector handles. ``applies_to`` matches
            a result by this name; a detector with an empty ``component`` is a
            catch-all (used by the engine as a fallback for unknown collectors).
    """

    component: str = ""

    def applies_to(self, result: CollectorResult) -> bool:
        """Whether this detector should classify the given collector result."""
        return result.name == self.component

    @abc.abstractmethod
    def detect(self, result: CollectorResult) -> list[Finding]:
        """Classify a result into zero or more findings. Must not perform I/O."""
        raise NotImplementedError

    # ── convenience ──────────────────────────────────────────────────────────

    def _finding(
        self,
        severity: Severity,
        code: str,
        message: str,
        *,
        subject: str = "",
        context: dict[str, Any] | None = None,
    ) -> Finding:
        """Build a :class:`Finding` tagged with this detector's component name."""
        return Finding(
            component=self.component or "unknown",
            severity=severity,
            code=code,
            message=message,
            subject=subject,
            context=context or {},
        )
