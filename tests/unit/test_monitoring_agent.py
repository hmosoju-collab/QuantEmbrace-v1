"""Unit tests for the Phase 1 (read-only) monitoring agent.

Scope of what these tests lock down:

  * Safety contract — DRY_RUN/notify_only defaults, config redaction never leaks
    the Slack webhook, and the app logs CRITICAL (but stays harmless) on an unsafe
    config because Phase 1 wires no action layer at all.
  * **Read-only API audit** (the most important test here): every collector module
    is parsed with ``ast`` and asserted to make NO call to a write/mutate method on
    Kafka, DynamoDB, Docker, or a broker — and to open no file for writing. This is
    the mechanical guarantee behind "the agent can reduce risk only; it never acts".
  * Pure decision functions for each collector (service/kafka/docker/dynamodb/broker).
  * Snapshot roll-up (`worst`) + JSON shape, rules loader (fail-soft + tolerant),
    Slack payload secret-safety, and the edge-triggered / restart-safe incident log.

Design choice: all tests are SYNCHRONOUS. Async collector behaviour is driven with
``asyncio.run(...)`` so the suite needs neither pytest-asyncio nor any of the agent's
optional heavy deps (confluent-kafka, docker, boto3) installed to pass.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import monitoring_agent
from monitoring_agent.collectors import build_collectors
from monitoring_agent.collectors.base import Collector
from monitoring_agent.collectors.broker_collector import _parse_iso_age_seconds
from monitoring_agent.collectors.docker_collector import decide_container_status
from monitoring_agent.collectors.dynamodb_collector import (
    DynamoDBCollector,
    decide_table_status,
)
from monitoring_agent.collectors.kafka_collector import KafkaCollector, compute_group_lag
from monitoring_agent.collectors.service_collector import (
    ServiceCollector,
    decide_service_status,
)
from monitoring_agent.config import AgentConfig
from monitoring_agent.incident_log import IncidentLog
from monitoring_agent.notify.slack import SlackNotifier, format_slack_payload
from monitoring_agent.rules import MonitoringRules, ServiceRule, load_rules
from monitoring_agent.snapshot import (
    ALERTING_STATUSES,
    CollectorResult,
    HealthSnapshot,
    Status,
    worst,
)

COLLECTORS_DIR = Path(monitoring_agent.__file__).resolve().parent / "collectors"
RULES_YAML = Path(monitoring_agent.__file__).resolve().parent / "rules.yaml"


# ── helpers ───────────────────────────────────────────────────────────────────


def _cfg(**overrides) -> AgentConfig:
    """An AgentConfig with safe defaults, overridable per-test (frozen → replace)."""
    return dataclasses.replace(AgentConfig(), **overrides)


def _down_snapshot(name: str = "execution_engine") -> HealthSnapshot:
    return HealthSnapshot(
        results=[CollectorResult(name=name, status=Status.DOWN, summary=f"{name} down")]
    )


class _FixedCollector(Collector):
    """A fake collector that returns a preset status (drives base.run without I/O)."""

    def __init__(self, name: str, status: Status, *, boom: bool = False) -> None:
        super().__init__(_cfg(), MonitoringRules())
        self.name = name
        self._status = status
        self._boom = boom

    def _collect_sync(self) -> CollectorResult:
        if self._boom:
            raise RuntimeError("collector exploded")
        return CollectorResult(name=self.name, status=self._status, summary=f"{self.name} {self._status.value}")


# ══════════════════════════════════════════════════════════════════════════════
# 1. READ-ONLY API AUDIT — the core safety guarantee
# ══════════════════════════════════════════════════════════════════════════════

# Methods that mutate the trading platform. If a collector ever calls one of these,
# the read-only contract is broken and this test must fail loudly.
_BANNED_METHODS = frozenset(
    {
        # DynamoDB writes
        "put_item", "update_item", "delete_item", "batch_writer", "batch_write_item",
        "create_table", "delete_table", "update_table", "transact_write_items",
        # Kafka mutations / group membership / offset commits
        "produce", "flush", "commit", "subscribe", "assign", "poll",
        "create_topics", "delete_topics", "create_partitions", "alter_configs",
        "store_offsets", "send_offsets_to_transaction",
        # Docker container mutations
        "run", "start", "stop", "restart", "kill", "remove", "pause", "unpause",
        "exec_run", "prune", "commit",
        # Broker order actions
        "place_order", "modify_order", "cancel_order", "exit_order",
    }
)

_WRITE_OPEN_MODES = ("w", "a", "x", "+")


def _collector_files() -> list[Path]:
    files = sorted(p for p in COLLECTORS_DIR.glob("*.py"))
    assert files, "no collector source files found — wrong path?"
    return files


def _called_attr_methods(tree: ast.AST) -> set[str]:
    """Every method name invoked as ``something.method(...)`` in the tree."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def test_collectors_make_no_write_calls():
    """AST audit: no collector calls any platform-mutating method."""
    offenders: dict[str, set[str]] = {}
    for path in _collector_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = _called_attr_methods(tree) & _BANNED_METHODS
        if bad:
            offenders[path.name] = bad
    assert not offenders, f"read-only contract violated — write calls found: {offenders}"


def test_collectors_open_no_files_for_writing():
    """AST audit: any open() in a collector must be read-mode only."""
    offenders: dict[str, str] = {}
    for path in _collector_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = ""
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    mode = str(node.args[1].value)
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = str(kw.value.value)
                if any(flag in mode for flag in _WRITE_OPEN_MODES):
                    offenders[path.name] = mode
    assert not offenders, f"collector opened a file for writing: {offenders}"


def test_collectors_do_not_reference_place_order():
    """Belt-and-braces: the substring 'place_order' appears nowhere in collectors."""
    for path in _collector_files():
        assert "place_order" not in path.read_text(encoding="utf-8"), path.name


def test_service_collector_uses_get_not_post():
    src = (COLLECTORS_DIR / "service_collector.py").read_text(encoding="utf-8")
    assert 'method="GET"' in src
    for verb in ('method="POST"', 'method="PUT"', 'method="DELETE"', "data="):
        assert verb not in src, f"service collector must not {verb}"


# ══════════════════════════════════════════════════════════════════════════════
# 2. CONFIG — safety defaults + secret redaction
# ══════════════════════════════════════════════════════════════════════════════


def test_default_config_is_phase1_safe():
    cfg = AgentConfig()
    assert cfg.dry_run is True
    assert cfg.action_mode == "notify_only"
    assert cfg.is_phase1_safe is True
    assert cfg.health_port == 8086


def test_from_env_defaults_safe_with_empty_env(monkeypatch):
    for var in (
        "MONITORING_DRY_RUN", "MONITORING_ACTION_MODE", "SLACK_WEBHOOK_URL",
        "KAFKA_BOOTSTRAP_SERVERS", "AWS_ENDPOINT_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = AgentConfig.from_env()
    assert cfg.is_phase1_safe is True
    assert cfg.slack_enabled is False


def test_from_env_invalid_action_mode_coerced_to_notify_only(monkeypatch):
    monkeypatch.setenv("MONITORING_ACTION_MODE", "do_whatever_you_want")
    assert AgentConfig.from_env().action_mode == "notify_only"


def test_from_env_unsafe_flags_are_preserved_not_silently_fixed(monkeypatch):
    # The config faithfully reflects an unsafe request; the *app* refuses to act on it.
    monkeypatch.setenv("MONITORING_DRY_RUN", "false")
    monkeypatch.setenv("MONITORING_ACTION_MODE", "risk_reduce")
    cfg = AgentConfig.from_env()
    assert cfg.dry_run is False
    assert cfg.action_mode == "risk_reduce"
    assert cfg.is_phase1_safe is False


def test_redacted_never_contains_webhook():
    secret = "https://hooks.slack.com/services/T00/B00/SUPERSECRETTOKEN"
    cfg = _cfg(slack_webhook_url=secret)
    red = cfg.redacted()
    assert "slack_webhook_url" not in red
    assert red["slack_enabled"] is True
    assert "SUPERSECRETTOKEN" not in json.dumps(red)


def test_table_name_composition():
    cfg = _cfg(dynamodb_table_prefix="quantembrace-development")
    assert cfg.table("orders") == "quantembrace-development-orders"


# ══════════════════════════════════════════════════════════════════════════════
# 3. SNAPSHOT — worst-wins roll-up + serialisation
# ══════════════════════════════════════════════════════════════════════════════


def test_worst_empty_is_ok():
    assert worst([]) is Status.OK


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([Status.OK, Status.OK], Status.OK),
        ([Status.OK, Status.UNKNOWN], Status.UNKNOWN),
        ([Status.UNKNOWN, Status.DEGRADED], Status.DEGRADED),
        ([Status.DEGRADED, Status.DOWN], Status.DOWN),
        ([Status.OK, Status.DOWN, Status.UNKNOWN], Status.DOWN),
    ],
)
def test_worst_ordering(statuses, expected):
    assert worst(statuses) is expected


def test_alerting_statuses_membership():
    assert Status.OK not in ALERTING_STATUSES
    assert {Status.DEGRADED, Status.DOWN, Status.UNKNOWN} == set(ALERTING_STATUSES)


def test_snapshot_overall_and_json_roundtrip():
    snap = HealthSnapshot(
        results=[
            CollectorResult("services", Status.OK, "ok"),
            CollectorResult("kafka", Status.DEGRADED, "lagging"),
        ]
    )
    assert snap.overall_status is Status.DEGRADED
    assert snap.dry_run is True and snap.action_mode == "notify_only" and snap.phase == 1
    parsed = json.loads(snap.to_json())
    assert parsed["overall_status"] == "degraded"
    assert parsed["phase"] == 1
    assert len(parsed["results"]) == 2


# ══════════════════════════════════════════════════════════════════════════════
# 4. RULES LOADER — fail-soft + tolerant + matches shipped rules.yaml
# ══════════════════════════════════════════════════════════════════════════════


def test_load_rules_missing_file_is_empty(tmp_path):
    rules = load_rules(str(tmp_path / "nope.yaml"))
    assert rules.is_empty is True
    assert rules.summary()["services"] == 0


def test_load_rules_malformed_yaml_is_empty(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("services: [unterminated\n", encoding="utf-8")
    rules = load_rules(str(bad))  # must not raise
    assert rules.is_empty is True


def test_load_rules_non_mapping_is_empty(tmp_path):
    f = tmp_path / "list.yaml"
    f.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert load_rules(str(f)).is_empty is True


def test_shipped_rules_yaml_parses_to_expected_policy():
    assert RULES_YAML.exists(), "rules.yaml should ship with the package"
    rules = load_rules(str(RULES_YAML))
    s = rules.summary()
    assert s["services"] == 5
    assert s["consumer_groups"] == 5
    assert s["topics"] == 8
    assert s["dynamodb_tables"] == 12
    assert rules.docker.enabled is True
    assert rules.logs.enabled is False
    assert rules.notifications.min_status == "degraded"
    # ai_engine is the only non-critical service.
    non_critical = [svc.name for svc in rules.services if not svc.critical]
    assert non_critical == ["ai_engine"]


def test_service_rule_tolerates_junk_types():
    rule = ServiceRule.from_dict({"name": "x", "port": "not-a-number", "critical": "yes"})
    assert rule.port == 0  # bad int coerced to default
    assert rule.critical is True  # "yes" → True


def test_rules_from_dict_ignores_unknown_keys_and_drops_nameless_entries():
    rules = MonitoringRules.from_dict(
        {
            "services": [{"name": "a", "port": 1}, {"port": 2}],  # 2nd has no name → dropped
            "future_section": {"anything": True},  # unknown → ignored, kept in raw
        }
    )
    assert [s.name for s in rules.services] == ["a"]
    assert rules.raw["future_section"] == {"anything": True}


# ══════════════════════════════════════════════════════════════════════════════
# 5. COLLECTOR DECISION FUNCTIONS (pure, no deps)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "reachable,health,ready,expected",
    [
        (False, None, None, Status.DOWN),
        (True, 200, 200, Status.OK),
        (True, 200, 503, Status.DEGRADED),
        (True, 500, 200, Status.DEGRADED),
        (True, None, None, Status.DEGRADED),
    ],
)
def test_decide_service_status(reachable, health, ready, expected):
    assert decide_service_status(reachable, health, ready) is expected


@pytest.mark.parametrize(
    "state,health,expected",
    [
        ("running", None, Status.OK),
        ("running", "healthy", Status.OK),
        ("running", "unhealthy", Status.DEGRADED),
        ("restarting", None, Status.DEGRADED),
        ("exited", None, Status.DOWN),
        ("created", None, Status.DOWN),
        ("", None, Status.DOWN),
    ],
)
def test_decide_container_status(state, health, expected):
    assert decide_container_status(state, health) is expected


@pytest.mark.parametrize(
    "table_status,expected",
    [
        ("ACTIVE", Status.OK),
        ("CREATING", Status.DEGRADED),
        ("UPDATING", Status.DEGRADED),
        ("", Status.UNKNOWN),
        (None, Status.UNKNOWN),
        ("DELETING", Status.DOWN),
        ("INACCESSIBLE_ENCRYPTION_CREDENTIALS", Status.DOWN),
    ],
)
def test_decide_table_status(table_status, expected):
    assert decide_table_status(table_status) is expected


def test_compute_group_lag_basic():
    committed = {("t", 0): 100, ("t", 1): 50}
    highwater = {("t", 0): 150, ("t", 1): 50}
    out = compute_group_lag(committed, highwater)
    assert out["total_lag"] == 50
    assert out["max_lag"] == 50
    assert len(out["partitions"]) == 2


def test_compute_group_lag_skips_sentinels_and_clamps():
    committed = {("t", 0): -1, ("t", 1): 200, ("t", 2): 10}  # -1 = "no offset stored"
    highwater = {("t", 0): 500, ("t", 1): 100, ("t", 2): None}  # p1 committed>high → clamp 0; p2 no high → skip
    out = compute_group_lag(committed, highwater)
    assert out["total_lag"] == 0  # p0 skipped, p1 clamped to 0, p2 skipped
    assert [p["partition"] for p in out["partitions"]] == [1]


def test_parse_iso_age_seconds():
    now = datetime(2026, 5, 29, 12, 0, 30, tzinfo=timezone.utc)
    assert _parse_iso_age_seconds("2026-05-29T12:00:00+00:00", now) == pytest.approx(30.0)
    # 'Z' suffix and naive timestamps (assumed UTC) both supported.
    assert _parse_iso_age_seconds("2026-05-29T12:00:00Z", now) == pytest.approx(30.0)
    assert _parse_iso_age_seconds("2026-05-29T11:59:30", now) == pytest.approx(60.0)
    assert _parse_iso_age_seconds("not-a-timestamp", now) is None
    assert _parse_iso_age_seconds(None, now) is None


# ══════════════════════════════════════════════════════════════════════════════
# 6. KAFKA RESULT ASSEMBLY (pure; no confluent-kafka needed)
# ══════════════════════════════════════════════════════════════════════════════


def _kafka() -> KafkaCollector:
    return KafkaCollector(_cfg(), MonitoringRules())


def test_kafka_unreachable_is_down():
    res = _kafka()._build_result({"reachable": False, "error": "boom"})
    assert res.status is Status.DOWN


def test_kafka_missing_critical_topic_is_degraded():
    res = _kafka()._build_result(
        {"reachable": True, "broker_count": 1, "groups": {}, "missing_critical_topics": ["signals.approved"]}
    )
    assert res.status is Status.DEGRADED


def test_kafka_idle_critical_group_is_degraded():
    res = _kafka()._build_result(
        {
            "reachable": True,
            "broker_count": 1,
            "groups": {"risk-v1": {"critical": True, "found": False}},
        }
    )
    assert res.status is Status.DEGRADED


def test_kafka_offset_read_error_only_is_unknown():
    res = _kafka()._build_result(
        {
            "reachable": True,
            "broker_count": 1,
            "groups": {"execution-v1": {"critical": True, "found": True, "error": "timeout"}},
        }
    )
    assert res.status is Status.UNKNOWN


def test_kafka_all_healthy_is_ok():
    res = _kafka()._build_result(
        {
            "reachable": True,
            "broker_count": 3,
            "groups": {"risk-v1": {"critical": True, "found": True}},
            "missing_critical_topics": [],
        }
    )
    assert res.status is Status.OK


# ══════════════════════════════════════════════════════════════════════════════
# 7. COLLECTOR BEHAVIOUR — graceful degradation + base.run safety net
# ══════════════════════════════════════════════════════════════════════════════


def test_service_collector_no_rules_is_unknown():
    res = asyncio.run(ServiceCollector(_cfg(), MonitoringRules()).run())
    assert res.status is Status.UNKNOWN


def test_dynamodb_collector_no_rules_is_unknown():
    res = asyncio.run(DynamoDBCollector(_cfg(), MonitoringRules()).run())
    assert res.status is Status.UNKNOWN


# ── Kill switch probe tests ───────────────────────────────────────────────────

def _rules_with_kill_switch_probe() -> MonitoringRules:
    from monitoring_agent.rules import DynamoTableRule
    return MonitoringRules(
        dynamodb_tables=(
            DynamoTableRule(suffix="risk-state", critical=True, kill_switch_probe=True),
        )
    )


def _make_dynamo_collector_with_mock(active: bool) -> DynamoDBCollector:
    """Return a DynamoDBCollector whose Table mock reports active/inactive KS."""
    from unittest.mock import MagicMock
    col = DynamoDBCollector(_cfg(), _rules_with_kill_switch_probe())

    table_mock = MagicMock()
    table_mock.table_status = "ACTIVE"
    ks_item = {"active": True, "reason": "data stale"} if active else {}
    table_mock.get_item.return_value = {"Item": ks_item}

    resource_mock = MagicMock()
    resource_mock.Table.return_value = table_mock

    import services.monitoring_agent.collectors.dynamodb_collector as _mod
    col._orig_get_resource = getattr(_mod, "_get_resource", None)

    # Patch get_dynamodb_resource inside the collector module
    import unittest.mock as mock
    col._patch = mock.patch(
        "shared.aws.clients.get_dynamodb_resource", return_value=resource_mock
    )
    col._patch.start()
    col._resource_mock = resource_mock
    return col


def test_kill_switch_probe_active_returns_down(monkeypatch):
    """DynamoDB collector must return Status.DOWN when kill switch active=True."""
    from unittest.mock import MagicMock, patch
    from monitoring_agent.rules import DynamoTableRule

    col = DynamoDBCollector(_cfg(), _rules_with_kill_switch_probe())

    table_mock = MagicMock()
    table_mock.table_status = "ACTIVE"
    table_mock.get_item.return_value = {"Item": {"active": True, "reason": "stale feed"}}
    resource_mock = MagicMock()
    resource_mock.Table.return_value = table_mock

    with patch("shared.aws.clients.get_dynamodb_resource", return_value=resource_mock):
        res = asyncio.run(col.run())

    assert res.status is Status.DOWN, f"expected DOWN when kill switch active, got {res.status}"
    assert res.details is not None
    assert any(
        t.get("kill_switch_active") is True
        for t in res.details.get("tables", [])
    )


def test_kill_switch_probe_inactive_returns_ok(monkeypatch):
    """DynamoDB collector must return Status.OK when kill switch active=False / absent."""
    from unittest.mock import MagicMock, patch

    col = DynamoDBCollector(_cfg(), _rules_with_kill_switch_probe())

    table_mock = MagicMock()
    table_mock.table_status = "ACTIVE"
    # active=False (or item missing entirely) → kill switch is off
    table_mock.get_item.return_value = {"Item": {"active": False}}
    resource_mock = MagicMock()
    resource_mock.Table.return_value = table_mock

    with patch("shared.aws.clients.get_dynamodb_resource", return_value=resource_mock):
        res = asyncio.run(col.run())

    assert res.status is Status.OK


def test_kill_switch_probe_parsed_from_rules_yaml():
    """kill_switch_probe: true in rules.yaml must parse correctly into DynamoTableRule."""
    rules = load_rules(str(RULES_YAML))
    ks_tables = [t for t in rules.dynamodb_tables if t.suffix == "risk-state"]
    assert ks_tables, "risk-state table must be in rules.yaml"
    assert ks_tables[0].kill_switch_probe is True, (
        "risk-state table must have kill_switch_probe=True in rules.yaml"
    )


def test_blocker_pattern_matches_actual_kill_switch_log():
    """The blocker pattern must match the log line killswitch.py actually emits."""
    import yaml
    with open(str(RULES_YAML)) as fh:
        data = yaml.safe_load(fh)
    blocker_pats = data.get("container_logs", {}).get("blocker_patterns", [])
    actual_log_line = (
        '{"level": "CRITICAL", "service": "risk_engine", '
        '"message": "KILL SWITCH ACTIVATED | reason=data stale | by=consumer_lag_monitor"}'
    )
    matched = any(pat in actual_log_line for pat in blocker_pats)
    assert matched, (
        f"No blocker pattern matches the actual kill switch log. "
        f"Patterns: {blocker_pats}. Log: {actual_log_line!r}"
    )


def test_base_run_converts_exceptions_to_unknown():
    res = asyncio.run(_FixedCollector("boom", Status.OK, boom=True).run())
    assert res.status is Status.UNKNOWN
    assert res.error is not None
    assert res.duration_ms >= 0.0


def test_build_collectors_respects_enabled_flags():
    # Defaults: docker enabled, logs disabled → 5 collectors, no 'logs'.
    names = [c.name for c in build_collectors(_cfg(), MonitoringRules())]
    assert "logs" not in names
    assert "docker" in names
    assert set(names) == {"services", "kafka", "dynamodb", "broker", "docker"}


def test_build_collectors_drops_docker_when_disabled():
    rules = MonitoringRules(docker=dataclasses.replace(MonitoringRules().docker, enabled=False))
    names = [c.name for c in build_collectors(_cfg(), rules)]
    assert "docker" not in names


# ══════════════════════════════════════════════════════════════════════════════
# 8. SLACK NOTIFIER — secret-safety + summaries-only
# ══════════════════════════════════════════════════════════════════════════════


def test_slack_payload_is_secret_free_and_summary_only():
    snap = HealthSnapshot(
        results=[CollectorResult("execution_engine", Status.DOWN, "exec down", details={"k": "RAWLOGSECRET"})]
    )
    from monitoring_agent.incident_log import Transition

    payload = format_slack_payload(
        snap, [Transition("execution_engine", "ok", "down", "exec down", False, True)], "#trading-ops"
    )
    text = payload["text"]
    assert "QuantEmbrace platform: DOWN" in text
    assert "execution_engine" in text and "exec down" in text
    assert "RAWLOGSECRET" not in text  # raw collector details are NEVER sent
    assert payload["channel"] == "#trading-ops"


def test_slack_send_without_webhook_returns_false():
    notifier = SlackNotifier(_cfg(slack_webhook_url=None))
    assert notifier.enabled is False
    assert notifier.send({"text": "hello"}) is False


def test_slack_send_failure_never_leaks_webhook(monkeypatch, caplog):
    secret = "https://hooks.slack.com/services/T0/B0/LEAKMENOT"
    notifier = SlackNotifier(_cfg(slack_webhook_url=secret))

    def _boom(*_a, **_k):
        raise RuntimeError(f"connection to {secret} failed")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    with caplog.at_level(logging.WARNING):
        assert notifier.send({"text": "hi"}) is False
    assert "LEAKMENOT" not in caplog.text  # only type(exc).__name__ is logged
    assert "RuntimeError" in caplog.text


# ══════════════════════════════════════════════════════════════════════════════
# 9. INCIDENT LOG — edge-triggered + restart-safe
# ══════════════════════════════════════════════════════════════════════════════


def test_incident_log_edge_triggered(tmp_path):
    log = IncidentLog(str(tmp_path / "inc.jsonl"), min_status="degraded")
    first = log.record(_down_snapshot("kafka"))
    assert len(first) == 1 and first[0].alertworthy is True
    # Same status next cycle → no new transition (edge-triggered, not level-triggered).
    assert log.record(_down_snapshot("kafka")) == []


def test_incident_log_recovery_alerts(tmp_path):
    path = str(tmp_path / "inc.jsonl")
    log = IncidentLog(path, min_status="degraded")
    log.record(_down_snapshot("kafka"))
    recovered = log.record(HealthSnapshot(results=[CollectorResult("kafka", Status.OK, "ok")]))
    assert len(recovered) == 1
    assert recovered[0].is_recovery is True
    assert recovered[0].alertworthy is True  # recovery from an alerting state alerts


def test_incident_log_restart_safe(tmp_path):
    path = str(tmp_path / "inc.jsonl")
    log1 = IncidentLog(path, min_status="degraded")
    log1.record(_down_snapshot("kafka"))  # persisted: kafka → down

    # Simulate a process restart: a fresh log replays the file and restores state.
    log2 = IncidentLog(path, min_status="degraded")
    assert log2.record(_down_snapshot("kafka")) == []  # still down → must NOT re-alert


def test_incident_log_unknown_below_min_status_not_alertworthy(tmp_path):
    log = IncidentLog(str(tmp_path / "inc.jsonl"), min_status="degraded")
    txns = log.record(HealthSnapshot(results=[CollectorResult("ai_engine", Status.UNKNOWN, "blind")]))
    assert len(txns) == 1
    assert txns[0].alertworthy is False  # unknown < degraded threshold


def test_incident_log_unknown_alerts_when_min_status_unknown(tmp_path):
    log = IncidentLog(str(tmp_path / "inc.jsonl"), min_status="unknown")
    txns = log.record(HealthSnapshot(results=[CollectorResult("ai_engine", Status.UNKNOWN, "blind")]))
    assert txns[0].alertworthy is True


# ══════════════════════════════════════════════════════════════════════════════
# 10. APP — Phase 1 safety guard + a full hermetic cycle
# ══════════════════════════════════════════════════════════════════════════════


def _agent(tmp_path, **cfg_overrides):
    from monitoring_agent.app import MonitoringAgent

    cfg = _cfg(
        snapshot_path=str(tmp_path / "snap.json"),
        incident_log_path=str(tmp_path / "inc.jsonl"),
        **cfg_overrides,
    )
    return MonitoringAgent(config=cfg, rules=MonitoringRules())


def test_app_logs_critical_on_unsafe_config_but_constructs(tmp_path, caplog):
    with caplog.at_level(logging.CRITICAL):
        agent = _agent(tmp_path, dry_run=False, action_mode="risk_reduce")
    assert agent is not None
    assert "UNSAFE CONFIG" in caplog.text


def test_app_run_once_writes_snapshot_and_detects_transition(tmp_path):
    agent = _agent(tmp_path)
    # Replace collectors with hermetic fakes so the cycle touches no external system.
    agent.collectors = [_FixedCollector("services", Status.OK), _FixedCollector("kafka", Status.DOWN)]

    snap = asyncio.run(agent.run_once())
    assert snap.overall_status is Status.DOWN

    # Snapshot persisted as valid JSON with the worst-wins overall status.
    written = json.loads(Path(agent.config.snapshot_path).read_text(encoding="utf-8"))
    assert written["overall_status"] == "down"
    assert {r["name"] for r in written["results"]} == {"services", "kafka"}

    # Incident log recorded the kafka transition (edge-triggered) on first cycle.
    lines = [json.loads(x) for x in Path(agent.config.incident_log_path).read_text().splitlines() if x.strip()]
    assert any(rec["component"] == "kafka" and rec["to"] == "down" for rec in lines)
