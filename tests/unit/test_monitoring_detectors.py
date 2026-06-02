"""Phase 2 detector + severity-engine tests for the monitoring agent.

These tests are pure and synchronous (no I/O, no event loop): detectors only
read a :class:`CollectorResult`, and the engine/report are in-memory. They lock
in the Phase 2 contract:

    * the severity ladder and the Status→Severity mapping,
    * each detector's classification of secret-free collector ``details``,
    * the engine's defensive aggregation (a bad detector can never crash a cycle),
    * the *additive-enrichment* invariant: attaching a SeverityReport to a
      snapshot never changes its coarse ``overall_status``,
    * the Slack payload surfaces severity findings (messages only) and never
      leaks raw collector ``details``.
"""

from __future__ import annotations

from typing import Any

import pytest

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
from monitoring_agent.notify.slack import format_slack_payload
from monitoring_agent.snapshot import CollectorResult, HealthSnapshot, Status


# ── helpers ──────────────────────────────────────────────────────────────────


def _result(name: str, status: Status, *, summary: str = "", **details: Any) -> CollectorResult:
    """Build a CollectorResult with arbitrary secret-free details."""
    return CollectorResult(name=name, status=status, summary=summary or name, details=dict(details))


def _only(findings: list[Finding]) -> Finding:
    """Assert exactly one finding and return it."""
    assert len(findings) == 1, f"expected exactly one finding, got {findings}"
    return findings[0]


# ── severity ladder ──────────────────────────────────────────────────────────


def test_severity_ladder_is_strictly_ordered() -> None:
    assert (
        severity_rank(Severity.INFO)
        < severity_rank(Severity.WARNING)
        < severity_rank(Severity.CRITICAL)
        < severity_rank(Severity.BLOCKER)
    )


def test_worst_severity_empty_is_info() -> None:
    assert worst_severity([]) is Severity.INFO


def test_worst_severity_picks_highest() -> None:
    assert worst_severity([Severity.INFO, Severity.BLOCKER, Severity.WARNING]) is Severity.BLOCKER


@pytest.mark.parametrize(
    ("status", "critical", "expected"),
    [
        (Status.OK, True, Severity.INFO),
        (Status.OK, False, Severity.INFO),
        (Status.DOWN, True, Severity.BLOCKER),
        (Status.DOWN, False, Severity.WARNING),
        (Status.DEGRADED, True, Severity.CRITICAL),
        (Status.DEGRADED, False, Severity.WARNING),
        (Status.UNKNOWN, True, Severity.CRITICAL),
        (Status.UNKNOWN, False, Severity.INFO),
    ],
)
def test_severity_for_status_table(status: Status, critical: bool, expected: Severity) -> None:
    assert severity_for_status(status, critical=critical) is expected


# ── Finding model ────────────────────────────────────────────────────────────


def test_finding_to_dict_shape() -> None:
    f = Finding(
        component="kafka",
        severity=Severity.CRITICAL,
        code="kafka.group_lag_critical",
        message="group risk-v1 lag high",
        subject="risk-v1",
        context={"max_lag": 15000},
    )
    d = f.to_dict()
    assert set(d) == {"component", "subject", "severity", "rank", "code", "message", "context"}
    assert d["severity"] == "critical"
    assert d["rank"] == severity_rank(Severity.CRITICAL)
    assert d["context"] == {"max_lag": 15000}


# ── ServiceDetector ──────────────────────────────────────────────────────────


def test_service_critical_down_is_blocker() -> None:
    det = ServiceDetector()
    res = _result(
        "services",
        Status.DOWN,
        services=[{"name": "execution_engine", "status": "down", "critical": True}],
    )
    f = _only(det.detect(res))
    assert f.severity is Severity.BLOCKER
    assert f.code == "service.down"
    assert f.subject == "execution_engine"


def test_service_critical_degraded_is_critical() -> None:
    det = ServiceDetector()
    res = _result(
        "services",
        Status.DEGRADED,
        services=[{"name": "risk_engine", "status": "degraded", "critical": True}],
    )
    f = _only(det.detect(res))
    assert f.severity is Severity.CRITICAL
    assert f.code == "service.degraded"


def test_service_noncritical_down_is_warning() -> None:
    det = ServiceDetector()
    res = _result(
        "services",
        Status.DOWN,
        services=[{"name": "monitoring_agent", "status": "down", "critical": False}],
    )
    assert _only(det.detect(res)).severity is Severity.WARNING


def test_service_ok_yields_no_finding() -> None:
    det = ServiceDetector()
    res = _result("services", Status.OK, services=[{"name": "x", "status": "ok", "critical": True}])
    assert det.detect(res) == []


def test_service_missing_key_is_tolerated() -> None:
    assert ServiceDetector().detect(_result("services", Status.OK)) == []


# ── KafkaDetector ────────────────────────────────────────────────────────────


def test_kafka_unreachable_is_blocker() -> None:
    f = _only(KafkaDetector().detect(_result("kafka", Status.DOWN, reachable=False)))
    assert f.severity is Severity.BLOCKER
    assert f.code == "kafka.unreachable"


def test_kafka_missing_topics_is_critical() -> None:
    res = _result("kafka", Status.DEGRADED, reachable=True, missing_critical_topics=["signals.approved"])
    f = _only(KafkaDetector().detect(res))
    assert f.severity is Severity.CRITICAL
    assert f.code == "kafka.missing_topics"


def test_kafka_critical_group_idle() -> None:
    res = _result(
        "kafka",
        Status.DEGRADED,
        reachable=True,
        groups={"risk-v1": {"critical": True, "found": False}},
    )
    f = _only(KafkaDetector().detect(res))
    assert f.severity is Severity.CRITICAL
    assert f.code == "kafka.group_idle"


def test_kafka_critical_group_lag_critical() -> None:
    res = _result(
        "kafka",
        Status.DEGRADED,
        reachable=True,
        groups={
            "risk-v1": {
                "critical": True,
                "found": True,
                "max_lag": 15000,
                "warn_lag": 1000,
                "critical_lag": 10000,
            }
        },
    )
    f = _only(KafkaDetector().detect(res))
    assert f.severity is Severity.CRITICAL
    assert f.code == "kafka.group_lag_critical"


def test_kafka_group_lag_warning() -> None:
    res = _result(
        "kafka",
        Status.OK,
        reachable=True,
        groups={
            "risk-v1": {
                "critical": True,
                "found": True,
                "max_lag": 2000,
                "warn_lag": 1000,
                "critical_lag": 10000,
            }
        },
    )
    f = _only(KafkaDetector().detect(res))
    assert f.severity is Severity.WARNING
    assert f.code == "kafka.group_lag_warning"


def test_kafka_noncritical_group_at_critical_lag_is_warning() -> None:
    res = _result(
        "kafka",
        Status.OK,
        reachable=True,
        groups={
            "aiengine-v1": {
                "critical": False,
                "found": True,
                "max_lag": 50000,
                "warn_lag": 1000,
                "critical_lag": 10000,
            }
        },
    )
    f = _only(KafkaDetector().detect(res))
    assert f.severity is Severity.WARNING
    assert f.code == "kafka.group_lag_critical"


def test_kafka_offset_read_error_is_critical_for_critical_group() -> None:
    res = _result(
        "kafka",
        Status.DEGRADED,
        reachable=True,
        groups={"risk-v1": {"critical": True, "error": "timeout"}},
    )
    f = _only(KafkaDetector().detect(res))
    assert f.severity is Severity.CRITICAL
    assert f.code == "kafka.group_offset_read_error"


def test_kafka_healthy_group_yields_no_finding() -> None:
    res = _result(
        "kafka",
        Status.OK,
        reachable=True,
        groups={
            "risk-v1": {
                "critical": True,
                "found": True,
                "max_lag": 5,
                "warn_lag": 1000,
                "critical_lag": 10000,
            }
        },
    )
    assert KafkaDetector().detect(res) == []


# ── DynamoDBDetector ─────────────────────────────────────────────────────────


def test_dynamodb_critical_down_is_blocker() -> None:
    res = _result(
        "dynamodb",
        Status.DOWN,
        tables=[{"table": "qe-orders", "status": "down", "critical": True, "table_status": None}],
    )
    f = _only(DynamoDBDetector().detect(res))
    assert f.severity is Severity.BLOCKER
    assert f.code == "dynamodb.down"
    assert f.subject == "qe-orders"


def test_dynamodb_critical_degraded_is_critical() -> None:
    res = _result(
        "dynamodb",
        Status.DEGRADED,
        tables=[{"table": "qe-orders", "status": "degraded", "critical": True}],
    )
    assert _only(DynamoDBDetector().detect(res)).severity is Severity.CRITICAL


def test_dynamodb_noncritical_down_is_warning() -> None:
    res = _result(
        "dynamodb",
        Status.DOWN,
        tables=[{"table": "qe-candle-cache", "status": "down", "critical": False}],
    )
    assert _only(DynamoDBDetector().detect(res)).severity is Severity.WARNING


def test_dynamodb_ok_yields_no_finding() -> None:
    res = _result("dynamodb", Status.OK, tables=[{"table": "qe-orders", "status": "ok"}])
    assert DynamoDBDetector().detect(res) == []


# ── BrokerDetector ───────────────────────────────────────────────────────────


def test_broker_market_closed_yields_no_finding() -> None:
    assert BrokerDetector().detect(_result("broker", Status.OK, market_open=False)) == []


def test_broker_state_unknown_is_warning() -> None:
    f = _only(BrokerDetector().detect(_result("broker", Status.UNKNOWN)))
    assert f.severity is Severity.WARNING
    assert f.code == "broker.state_unknown"


def test_broker_no_fresh_prices_is_critical() -> None:
    res = _result("broker", Status.DEGRADED, market_open=True, newest_age_seconds=None, max_age_seconds=60)
    f = _only(BrokerDetector().detect(res))
    assert f.severity is Severity.CRITICAL
    assert f.code == "broker.no_fresh_prices"


def test_broker_feed_very_stale_is_critical() -> None:
    res = _result("broker", Status.DEGRADED, market_open=True, newest_age_seconds=400, max_age_seconds=60)
    f = _only(BrokerDetector().detect(res))
    assert f.severity is Severity.CRITICAL
    assert f.code == "broker.feed_very_stale"


def test_broker_feed_stale_is_warning() -> None:
    res = _result("broker", Status.OK, market_open=True, newest_age_seconds=90, max_age_seconds=60)
    f = _only(BrokerDetector().detect(res))
    assert f.severity is Severity.WARNING
    assert f.code == "broker.feed_stale"


def test_broker_fresh_feed_yields_no_finding() -> None:
    res = _result("broker", Status.OK, market_open=True, newest_age_seconds=5, max_age_seconds=60)
    assert BrokerDetector().detect(res) == []


# ── DockerDetector ───────────────────────────────────────────────────────────


def test_docker_restart_loop_is_critical() -> None:
    res = _result(
        "docker",
        Status.DEGRADED,
        containers=[{"name": "risk", "status": "degraded", "restart_count": 5, "restart_warn": 1, "restart_critical": 3}],
    )
    f = _only(DockerDetector().detect(res))
    assert f.severity is Severity.CRITICAL
    assert f.code == "docker.restart_loop"


def test_docker_restarting_is_warning() -> None:
    res = _result(
        "docker",
        Status.OK,
        containers=[{"name": "risk", "status": "ok", "restart_count": 2, "restart_warn": 1, "restart_critical": 3}],
    )
    f = _only(DockerDetector().detect(res))
    assert f.severity is Severity.WARNING
    assert f.code == "docker.restarting"


def test_docker_down_is_warning() -> None:
    res = _result(
        "docker",
        Status.DOWN,
        containers=[{"name": "risk", "status": "down", "restart_count": 0, "state": "exited"}],
    )
    f = _only(DockerDetector().detect(res))
    assert f.severity is Severity.WARNING
    assert f.code == "docker.down"


def test_docker_healthy_yields_no_finding() -> None:
    res = _result(
        "docker",
        Status.OK,
        containers=[{"name": "risk", "status": "ok", "restart_count": 0}],
    )
    assert DockerDetector().detect(res) == []


# ── LogDetector ──────────────────────────────────────────────────────────────


def test_logs_no_matches_yields_no_finding() -> None:
    assert LogDetector().detect(_result("logs", Status.OK, total_matches=0)) == []


def test_logs_critical_pattern_is_critical() -> None:
    res = _result(
        "logs",
        Status.DEGRADED,
        total_matches=2,
        matches_by_pattern={"CRITICAL": 1, "ERROR": 1},
        files_scanned=["risk.log"],
    )
    f = _only(LogDetector().detect(res))
    assert f.severity is Severity.CRITICAL
    assert f.code == "logs.errors_present"


def test_logs_error_only_is_warning() -> None:
    res = _result(
        "logs",
        Status.DEGRADED,
        total_matches=3,
        matches_by_pattern={"ERROR": 3},
        files_scanned=["risk.log"],
    )
    assert _only(LogDetector().detect(res)).severity is Severity.WARNING


# ── GenericDetector (fallback) ───────────────────────────────────────────────


def test_generic_unmapped_down_is_blocker_with_context() -> None:
    f = _only(GenericDetector().detect(_result("newthing", Status.DOWN, summary="newthing is down")))
    assert f.severity is Severity.BLOCKER
    assert f.code == "newthing.down"
    assert f.context.get("unmapped_collector") is True


def test_generic_ok_yields_no_finding() -> None:
    assert GenericDetector().detect(_result("newthing", Status.OK)) == []


# ── SeverityReport ───────────────────────────────────────────────────────────


def _report() -> SeverityReport:
    return SeverityReport(
        findings=[
            Finding("a", Severity.WARNING, "a.warn", "warn"),
            Finding("b", Severity.BLOCKER, "b.block", "block"),
            Finding("c", Severity.INFO, "c.info", "info"),
            Finding("d", Severity.CRITICAL, "d.crit", "crit"),
        ]
    )


def test_report_overall_severity_is_worst() -> None:
    assert _report().overall_severity is Severity.BLOCKER


def test_report_counts_lists_all_levels() -> None:
    counts = _report().counts()
    assert counts == {"info": 1, "warning": 1, "critical": 1, "blocker": 1}


def test_report_to_dict_is_sorted_worst_first() -> None:
    d = _report().to_dict()
    severities = [f["severity"] for f in d["findings"]]
    assert severities == ["blocker", "critical", "warning", "info"]
    assert d["overall_severity"] == "blocker"
    assert d["counts"]["blocker"] == 1


def test_report_findings_at_or_above_critical() -> None:
    above = _report().findings_at_or_above(Severity.CRITICAL)
    assert {f.severity for f in above} == {Severity.CRITICAL, Severity.BLOCKER}


# ── SeverityEngine ───────────────────────────────────────────────────────────


def test_default_detectors_count() -> None:
    assert len(default_detectors()) == 6


def test_engine_aggregates_and_rolls_up() -> None:
    snap = HealthSnapshot(
        results=[
            _result("services", Status.DOWN, services=[{"name": "execution_engine", "status": "down", "critical": True}]),
            _result(
                "kafka",
                Status.DEGRADED,
                reachable=True,
                groups={"risk-v1": {"critical": True, "found": True, "max_lag": 15000, "warn_lag": 1000, "critical_lag": 10000}},
            ),
            _result("broker", Status.OK, market_open=True, newest_age_seconds=5, max_age_seconds=60),
        ]
    )
    report = SeverityEngine().evaluate(snap)
    assert report.overall_severity is Severity.BLOCKER
    assert report.counts() == {"info": 0, "warning": 0, "critical": 1, "blocker": 1}


def test_engine_never_raises_on_bad_detector() -> None:
    class _BoomDetector(Detector):
        component = "services"

        def detect(self, result: CollectorResult) -> list[Finding]:
            raise ValueError("boom")

    engine = SeverityEngine(detectors=[_BoomDetector()])
    snap = HealthSnapshot(
        results=[
            _result("services", Status.DOWN, services=[{"name": "x", "status": "down", "critical": True}]),
            _result("kafka", Status.DOWN),  # no dedicated detector → generic fallback
        ]
    )
    report = engine.evaluate(snap)  # must not raise
    # the boom detector is skipped; the generic fallback still classifies kafka
    f = _only(report.findings)
    assert f.code == "kafka.down"
    assert f.severity is Severity.BLOCKER


def test_engine_tolerates_malformed_details() -> None:
    snap = HealthSnapshot(
        results=[
            _result("services", Status.OK, services="notalist"),
            _result("kafka", Status.OK, reachable=True, groups="nope"),
            _result("dynamodb", Status.OK, tables=123),
            _result("docker", Status.OK, containers={}),
            _result("logs", Status.OK, total_matches="x"),
        ]
    )
    report = SeverityEngine().evaluate(snap)  # must not raise
    assert report.findings == []
    assert report.overall_severity is Severity.INFO


def test_engine_uses_generic_fallback_for_unknown_collector() -> None:
    snap = HealthSnapshot(results=[_result("brand_new", Status.DEGRADED, summary="weird")])
    f = _only(SeverityEngine().evaluate(snap).findings)
    assert f.severity is Severity.CRITICAL
    assert f.context.get("unmapped_collector") is True


# ── enrichment invariant (the contract app.run_once relies on) ───────────────


def test_attaching_severity_does_not_change_overall_status() -> None:
    """Mirrors app.run_once: evaluate → attach severity + bump phase, additively."""
    snap = HealthSnapshot(
        results=[
            _result("services", Status.DOWN, services=[{"name": "execution_engine", "status": "down", "critical": True}]),
            _result("kafka", Status.OK, reachable=True),
        ]
    )
    before = snap.overall_status
    assert before is Status.DOWN

    report = SeverityEngine().evaluate(snap)
    snap.severity = report.to_dict()
    snap.phase = 2

    # Status is unchanged by enrichment; severity is purely additive.
    assert snap.overall_status is Status.DOWN
    assert snap.phase == 2
    assert snap.severity["overall_severity"] == "blocker"
    assert "counts" in snap.severity
    assert isinstance(snap.severity["findings"], list)
    # and it survives JSON serialisation
    assert "severity" in snap.to_dict()


# ── Slack rendering ──────────────────────────────────────────────────────────


def _snapshot_with_issue(secret_token: str) -> HealthSnapshot:
    return HealthSnapshot(
        results=[
            CollectorResult(
                name="services",
                status=Status.DOWN,
                summary="execution_engine unreachable",
                details={"raw": secret_token},  # raw details must never be rendered
            )
        ]
    )


def test_slack_payload_includes_severity_and_findings() -> None:
    snap = _snapshot_with_issue("DETAIL-DO-NOT-LEAK")
    report = SeverityReport(
        findings=[Finding("services", Severity.BLOCKER, "service.down", "execution_engine is down", subject="execution_engine")]
    )
    text = format_slack_payload(snap, [], report=report)["text"]
    assert "Severity: BLOCKER" in text
    assert "execution_engine is down" in text


def test_slack_payload_has_no_severity_line_without_report() -> None:
    snap = _snapshot_with_issue("x")
    text = format_slack_payload(snap, [], report=None)["text"]
    assert "Severity:" not in text


def test_slack_payload_does_not_leak_raw_details() -> None:
    snap = _snapshot_with_issue("DETAIL-DO-NOT-LEAK")
    report = SeverityReport(
        findings=[Finding("services", Severity.BLOCKER, "service.down", "execution_engine is down", subject="execution_engine")]
    )
    text = format_slack_payload(snap, [], report=report)["text"]
    assert "DETAIL-DO-NOT-LEAK" not in text
