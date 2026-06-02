"""Detector + severity engine — **Phase 2**.

Phase 1 collectors emit a coarse per-component :class:`~monitoring_agent.snapshot.Status`
(ok / degraded / down / unknown). Phase 2 adds *detectors* that consume the raw
collector ``details`` (consumer lag, restart counts, data age, log error rates) and
classify them into fine-grained severities (INFO / WARNING / CRITICAL / BLOCKER)
using the thresholds already parsed in ``rules.py`` and embedded in those details.

Phase 2 is strictly *additive enrichment*: the :class:`SeverityEngine` output is
attached to the snapshot and surfaced in logs/Slack, but it never changes the
coarse ``overall_status`` nor the proven Status-based incident/alert gating, and it
wires NO action layer. Detectors are pure (no I/O), so the agent stays observe-only.
"""

from __future__ import annotations

from monitoring_agent.detectors.base import Detector, Finding
from monitoring_agent.detectors.engine import (
    BrokerDetector,
    DockerDetector,
    DynamoDBDetector,
    GenericDetector,
    KafkaDetector,
    LogDetector,
    ServiceDetector,
    SeverityEngine,
    SeverityReport,
    default_detectors,
)
from monitoring_agent.detectors.severity import (
    Severity,
    severity_for_status,
    severity_rank,
    worst_severity,
)

__all__: list[str] = [
    "Detector",
    "Finding",
    "Severity",
    "severity_rank",
    "worst_severity",
    "severity_for_status",
    "SeverityEngine",
    "SeverityReport",
    "default_detectors",
    "ServiceDetector",
    "KafkaDetector",
    "DynamoDBDetector",
    "BrokerDetector",
    "DockerDetector",
    "LogDetector",
    "GenericDetector",
]
