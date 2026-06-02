"""Per-collector detectors + the severity engine — Phase 2.

Each detector consumes the secret-free ``details`` a Phase 1 collector already
produced and classifies it into :class:`~monitoring_agent.detectors.base.Finding`
objects on the INFO/WARNING/CRITICAL/BLOCKER ladder, using the warn/critical
thresholds the collector embedded in its details (which come from
``rules.yaml``). Detectors are pure — they do no I/O — so Phase 2 stays strictly
observe-only.

The :class:`SeverityEngine` runs every detector over a :class:`HealthSnapshot`,
collects the findings, and rolls them up into one ``overall_severity``. Its output
(:class:`SeverityReport`) is *additive enrichment*: it is attached to the snapshot
and surfaced in logs/Slack, but it never changes the coarse ``overall_status`` nor
the Status-based incident/alert gating. A detector that misbehaves can never crash
a cycle — the engine wraps every call.

Lag threshold note: per-group lag is compared on ``max_lag`` (the worst single
partition) rather than ``total_lag``. A single partition falling far behind is the
canonical "this consumer is stuck" signal, and using a per-partition figure keeps
thresholds stable regardless of partition count. ``total_lag`` is still reported in
each finding's context for the operator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from monitoring_agent.detectors.base import Detector, Finding
from monitoring_agent.detectors.severity import (
    Severity,
    severity_for_status,
    severity_rank,
    worst_severity,
)
from monitoring_agent.snapshot import CollectorResult, HealthSnapshot, Status

logger = logging.getLogger("monitoring_agent.detectors.engine")

# How many multiples of the configured max age count as "very stale" (CRITICAL).
_VERY_STALE_MULTIPLIER = 3.0


# ── tolerant local coercion (details schema is secret-free but not rigid) ──────


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status_of(value: Any) -> Status:
    """Coerce a status string back into a :class:`Status` (UNKNOWN on junk)."""
    try:
        return Status(str(value).strip().lower())
    except ValueError:
        return Status.UNKNOWN


# ── per-collector detectors ────────────────────────────────────────────────────


class ServiceDetector(Detector):
    """Classify HTTP service health (component ``services``)."""

    component = "services"

    def detect(self, result: CollectorResult) -> list[Finding]:
        services = result.details.get("services")
        if not isinstance(services, list):
            return []
        findings: list[Finding] = []
        for svc in services:
            if not isinstance(svc, dict):
                continue
            status = _status_of(svc.get("status"))
            if status is Status.OK:
                continue
            critical = bool(svc.get("critical", True))
            severity = severity_for_status(status, critical=critical)
            name = str(svc.get("name", "?"))
            findings.append(
                self._finding(
                    severity,
                    code=f"service.{status.value}",
                    message=(
                        f"{name} is {status.value} "
                        f"(health={svc.get('health_code')}, ready={svc.get('ready_code')})"
                    ),
                    subject=name,
                    context={
                        "critical": critical,
                        "reachable": bool(svc.get("reachable", False)),
                        "health_code": svc.get("health_code"),
                        "ready_code": svc.get("ready_code"),
                    },
                )
            )
        return findings


class KafkaDetector(Detector):
    """Classify Kafka reachability, missing topics, and consumer lag (``kafka``)."""

    component = "kafka"

    def detect(self, result: CollectorResult) -> list[Finding]:
        details = result.details or {}
        if not details.get("reachable", False):
            return [
                self._finding(
                    Severity.BLOCKER,
                    code="kafka.unreachable",
                    message="Kafka cluster unreachable — the trading event bus is down",
                    subject="cluster",
                    context={"error_type": _error_type(result.error)},
                )
            ]

        findings: list[Finding] = []

        missing = list(details.get("missing_critical_topics") or [])
        if missing:
            findings.append(
                self._finding(
                    Severity.CRITICAL,
                    code="kafka.missing_topics",
                    message=f"{len(missing)} critical topic(s) missing: {missing}",
                    subject="topics",
                    context={"missing_topics": missing},
                )
            )

        groups = details.get("groups")
        if not isinstance(groups, dict):
            groups = {}
        for gid, info in groups.items():
            if not isinstance(info, dict):
                continue
            critical = bool(info.get("critical", True))

            if info.get("error"):
                findings.append(
                    self._finding(
                        Severity.CRITICAL if critical else Severity.WARNING,
                        code="kafka.group_offset_read_error",
                        message=f"could not read committed offsets for group {gid}",
                        subject=gid,
                        context={"critical": critical},
                    )
                )
                continue

            if critical and not info.get("found", False):
                findings.append(
                    self._finding(
                        Severity.CRITICAL,
                        code="kafka.group_idle",
                        message=f"critical group {gid} has no committed offsets (consumer not running?)",
                        subject=gid,
                        context={"critical": critical},
                    )
                )
                continue

            max_lag = _as_int(info.get("max_lag"))
            warn_lag = _as_int(info.get("warn_lag"), 1000)
            critical_lag = _as_int(info.get("critical_lag"), 10000)
            ctx = {
                "max_lag": max_lag,
                "total_lag": _as_int(info.get("total_lag")),
                "warn_lag": warn_lag,
                "critical_lag": critical_lag,
                "critical": critical,
            }
            if max_lag >= critical_lag:
                findings.append(
                    self._finding(
                        Severity.CRITICAL if critical else Severity.WARNING,
                        code="kafka.group_lag_critical",
                        message=f"group {gid} max partition lag {max_lag} ≥ critical {critical_lag}",
                        subject=gid,
                        context=ctx,
                    )
                )
            elif max_lag >= warn_lag:
                findings.append(
                    self._finding(
                        Severity.WARNING,
                        code="kafka.group_lag_warning",
                        message=f"group {gid} max partition lag {max_lag} ≥ warn {warn_lag}",
                        subject=gid,
                        context=ctx,
                    )
                )
        return findings


class DynamoDBDetector(Detector):
    """Classify DynamoDB table presence/status (component ``dynamodb``)."""

    component = "dynamodb"

    def detect(self, result: CollectorResult) -> list[Finding]:
        tables = result.details.get("tables")
        if not isinstance(tables, list):
            return []
        findings: list[Finding] = []
        for entry in tables:
            if not isinstance(entry, dict):
                continue
            status = _status_of(entry.get("status"))
            if status is Status.OK:
                continue
            critical = bool(entry.get("critical", True))
            severity = severity_for_status(status, critical=critical)
            table = str(entry.get("table") or entry.get("suffix") or "?")
            table_status = entry.get("table_status")
            findings.append(
                self._finding(
                    severity,
                    code=f"dynamodb.{status.value}",
                    message=f"table {table} is {status.value} (TableStatus={table_status})",
                    subject=table,
                    context={"critical": critical, "table_status": table_status},
                )
            )
        return findings


class BrokerDetector(Detector):
    """Classify broker price-feed freshness (component ``broker``).

    Severity is market-hours aware: staleness off-hours is expected and INFO.
    During market hours a stale feed means strategies are trading on old prices,
    which escalates with how stale the newest tick is.
    """

    component = "broker"

    def detect(self, result: CollectorResult) -> list[Finding]:
        if result.status is Status.UNKNOWN:
            return [
                self._finding(
                    Severity.WARNING,
                    code="broker.state_unknown",
                    message="cannot infer price-feed health (latest-prices unreadable)",
                    subject="feed",
                    context={"error_type": _error_type(result.error)},
                )
            ]

        details = result.details or {}
        if not details.get("market_open", False):
            return []  # idle feed off-hours is expected → INFO, no finding

        newest_age = _as_float(details.get("newest_age_seconds"))
        max_age = _as_float(details.get("max_age_seconds")) or 60.0
        sampled = _as_int(details.get("sampled"))
        ctx = {
            "newest_age_seconds": newest_age,
            "max_age_seconds": max_age,
            "sampled": sampled,
            "market_open": True,
        }

        if newest_age is None:
            return [
                self._finding(
                    Severity.CRITICAL,
                    code="broker.no_fresh_prices",
                    message="market open but no parseable recent prices — feed likely down",
                    subject="feed",
                    context=ctx,
                )
            ]
        if newest_age > _VERY_STALE_MULTIPLIER * max_age:
            return [
                self._finding(
                    Severity.CRITICAL,
                    code="broker.feed_very_stale",
                    message=f"price feed very stale — newest tick {newest_age:.0f}s old (> {_VERY_STALE_MULTIPLIER:.0f}× {max_age:.0f}s)",
                    subject="feed",
                    context=ctx,
                )
            ]
        if newest_age > max_age:
            return [
                self._finding(
                    Severity.WARNING,
                    code="broker.feed_stale",
                    message=f"price feed stale — newest tick {newest_age:.0f}s old (> {max_age:.0f}s)",
                    subject="feed",
                    context=ctx,
                )
            ]
        return []


class DockerDetector(Detector):
    """Classify container liveness + restart loops (component ``docker``).

    Docker is the local/dev runtime (production runs on EC2 ASGs), so container
    liveness issues are WARNING rather than BLOCKER. A restart count at/above the
    configured critical threshold is an unambiguous crash loop and escalates to
    CRITICAL.
    """

    component = "docker"

    def detect(self, result: CollectorResult) -> list[Finding]:
        containers = result.details.get("containers")
        if not isinstance(containers, list):
            return []
        findings: list[Finding] = []
        for c in containers:
            if not isinstance(c, dict):
                continue
            status = _status_of(c.get("status"))
            restarts = _as_int(c.get("restart_count"))
            warn = _as_int(c.get("restart_warn"), 1)
            crit = _as_int(c.get("restart_critical"), 3)
            name = str(c.get("name", "?"))

            severity = Severity.INFO
            reasons: list[str] = []
            if restarts >= crit:
                severity = worst_severity([severity, Severity.CRITICAL])
                reasons.append(f"{restarts} restarts ≥ critical {crit}")
            elif restarts >= warn:
                severity = worst_severity([severity, Severity.WARNING])
                reasons.append(f"{restarts} restarts ≥ warn {warn}")
            if status in (Status.DOWN, Status.DEGRADED):
                severity = worst_severity([severity, Severity.WARNING])
                reasons.append(f"state={c.get('state')}")

            if severity is Severity.INFO:
                continue
            code = "docker.restart_loop" if restarts >= crit else (
                "docker.restarting" if restarts >= warn else f"docker.{status.value}"
            )
            findings.append(
                self._finding(
                    severity,
                    code=code,
                    message=f"container {name}: {'; '.join(reasons)}",
                    subject=name,
                    context={
                        "state": c.get("state"),
                        "health": c.get("health"),
                        "restart_count": restarts,
                        "restart_warn": warn,
                        "restart_critical": crit,
                    },
                )
            )
        return findings


class LogDetector(Detector):
    """Classify recent log error-pattern matches (component ``logs``).

    A match on a pattern named ``CRITICAL`` escalates to CRITICAL; any other
    error-pattern match is WARNING. Only counts are surfaced — never sample
    lines — so log contents never leak past the snapshot's secret-free ``details``.
    """

    component = "logs"

    def detect(self, result: CollectorResult) -> list[Finding]:
        details = result.details or {}
        total = _as_int(details.get("total_matches"))
        if total <= 0:
            return []
        by_pattern: dict[str, Any] = details.get("matches_by_pattern") or {}
        has_critical = _as_int(by_pattern.get("CRITICAL")) > 0
        severity = Severity.CRITICAL if has_critical else Severity.WARNING
        scanned = details.get("files_scanned") or []
        return [
            self._finding(
                severity,
                code="logs.errors_present",
                message=f"{total} error-pattern match(es) across {len(scanned)} file(s)",
                subject="logs",
                context={
                    "total_matches": total,
                    "matches_by_pattern": by_pattern,
                    "window_seconds": details.get("window_seconds"),
                },
            )
        ]


class ContainerLogDetector(Detector):
    """Classify container log pattern hits by severity group (component ``container_logs``)."""

    component = "container_logs"

    def detect(self, result: CollectorResult) -> list[Finding]:
        details = result.details or {}
        findings: list[Finding] = []

        blocker_hits = _as_int(details.get("total_blocker_hits"))
        critical_hits = _as_int(details.get("total_critical_hits"))
        warning_hits = _as_int(details.get("total_warning_hits"))
        blocker_samples = details.get("blocker_samples") or []
        critical_samples = details.get("critical_samples") or []
        warning_samples = details.get("warning_samples") or []

        if blocker_hits:
            findings.append(
                self._finding(
                    Severity.BLOCKER,
                    code="container_logs.blocker_pattern",
                    message=f"{blocker_hits} BLOCKER pattern hit(s) in service logs — sample: {blocker_samples[:1]}",
                    subject="logs",
                    context={"hits": blocker_hits, "samples": blocker_samples},
                )
            )
        if critical_hits:
            findings.append(
                self._finding(
                    Severity.CRITICAL,
                    code="container_logs.critical_pattern",
                    message=f"{critical_hits} CRITICAL pattern hit(s) in service logs — sample: {critical_samples[:1]}",
                    subject="logs",
                    context={"hits": critical_hits, "samples": critical_samples},
                )
            )
        if warning_hits:
            findings.append(
                self._finding(
                    Severity.WARNING,
                    code="container_logs.warning_pattern",
                    message=f"{warning_hits} WARNING pattern hit(s) in service logs",
                    subject="logs",
                    context={"hits": warning_hits, "samples": warning_samples},
                )
            )
        return findings


class GenericDetector(Detector):
    """Fallback for any collector without a dedicated detector (forward-compat).

    Treats the component as critical so a future collector is never *under*-
    reported. Only fires when the coarse status is not OK.
    """

    component = ""  # catch-all

    def applies_to(self, result: CollectorResult) -> bool:  # pragma: no cover - trivial
        return True

    def detect(self, result: CollectorResult) -> list[Finding]:
        if result.status is Status.OK:
            return []
        severity = severity_for_status(result.status, critical=True)
        return [
            Finding(
                component=result.name,
                severity=severity,
                code=f"{result.name}.{result.status.value}",
                message=result.summary or f"{result.name} is {result.status.value}",
                subject=result.name,
                context={"unmapped_collector": True},
            )
        ]


def default_detectors() -> list[Detector]:
    """The standard set of dedicated detectors (the generic fallback is implicit)."""
    return [
        ServiceDetector(),
        KafkaDetector(),
        DynamoDBDetector(),
        BrokerDetector(),
        DockerDetector(),
        LogDetector(),
        ContainerLogDetector(),
    ]


# ── aggregation ─────────────────────────────────────────────────────────────--


@dataclass
class SeverityReport:
    """Aggregate Phase 2 classification for one snapshot. Additive enrichment only."""

    findings: list[Finding] = field(default_factory=list)

    @property
    def overall_severity(self) -> Severity:
        return worst_severity([f.severity for f in self.findings])

    def counts(self) -> dict[str, int]:
        """Count of findings per severity (always lists all four levels)."""
        out = {s.value: 0 for s in Severity}
        for f in self.findings:
            out[f.severity.value] += 1
        return out

    def findings_at_or_above(self, severity: Severity) -> list[Finding]:
        floor = severity_rank(severity)
        return [f for f in self.findings if severity_rank(f.severity) >= floor]

    def to_dict(self) -> dict[str, Any]:
        # Sort worst-first so the most urgent findings read at the top.
        ordered = sorted(self.findings, key=lambda f: severity_rank(f.severity), reverse=True)
        return {
            "overall_severity": self.overall_severity.value,
            "counts": self.counts(),
            "findings": [f.to_dict() for f in ordered],
        }


class SeverityEngine:
    """Run detectors over a snapshot and roll their findings up. Never raises."""

    def __init__(self, detectors: list[Detector] | None = None) -> None:
        self._detectors = detectors if detectors is not None else default_detectors()
        self._fallback = GenericDetector()

    def _detector_for(self, result: CollectorResult) -> Detector:
        for det in self._detectors:
            if det.applies_to(result):
                return det
        return self._fallback

    def evaluate(self, snapshot: HealthSnapshot) -> SeverityReport:
        """Classify every collector result into findings. Defensive per-result."""
        findings: list[Finding] = []
        for result in snapshot.results:
            detector = self._detector_for(result)
            try:
                findings.extend(detector.detect(result))
            except Exception:  # noqa: BLE001 — a bad detector must not break enrichment
                logger.exception("detector %s failed on %s", type(detector).__name__, result.name)
        return SeverityReport(findings=findings)


def _error_type(error: Any) -> str | None:
    """Surface only the *kind* of error string a collector recorded, never its body.

    Collectors store ``repr(exc)`` in ``error`` (already secret-free by their own
    contract), but in findings we keep only a short leading token so nothing
    log-line-shaped travels into Slack.
    """
    if not error:
        return None
    text = str(error)
    head = text.split("(", 1)[0].strip()
    return head[:60] or None
