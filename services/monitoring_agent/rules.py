"""Declarative monitoring policy loaded from ``rules.yaml``.

This module is the *typed, tolerant* parser that turns the YAML policy file into
immutable dataclasses the collectors can consume. Design goals:

    * **YAML-driven.** All "what to watch" knobs (which services/ports/health
      paths, which Kafka consumer groups + topics, which DynamoDB tables, Docker
      and log policy, notification thresholds) live in ``rules.yaml`` — never
      hard-coded in collectors. ``config.py`` holds *runtime/secrets*; this holds
      *policy*.
    * **Forward-compatible.** Unknown keys are ignored, every field has a safe
      default, and the original parsed mapping is preserved on ``.raw`` so later
      phases (detectors, actions) can read new sections without a parser change.
    * **Fail-soft.** A missing file yields empty rules + a warning (the agent
      still starts, reports "no rules", and stays harmless). A malformed file is
      logged and also degrades to empty rules rather than crashing a read-only
      observability process.

Thresholds for lag/staleness/restarts are parsed and stored now but are only
*acted on* by the Phase 2 detector/severity engine. Phase 1 collectors record
raw measurements; they do not classify INFO/WARNING/CRITICAL/BLOCKER here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("monitoring_agent.rules")


# ── Tolerant coercion helpers ────────────────────────────────────────────────
# Every helper accepts whatever YAML produced and returns a well-typed value,
# falling back to the default on anything unexpected. This is what makes the
# loader robust to hand-edited YAML.


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_opt_str(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a scalar or list into a tuple of non-empty strings."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (str(value),)
    try:
        return tuple(str(v) for v in value if str(v) != "")
    except TypeError:
        return ()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# ── Typed rule records (immutable) ───────────────────────────────────────────


@dataclass(frozen=True)
class ServiceRule:
    """A trading service whose HTTP /health and /ready endpoints we probe.

    ``critical`` marks services whose outage should drive the worst overall
    status (execution/risk are critical; an optional sidecar may not be).
    """

    name: str
    host: str = "localhost"
    port: int = 0
    health_path: str = "/health"
    ready_path: str = "/ready"
    critical: bool = True
    timeout_seconds: float = 3.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ServiceRule":
        return cls(
            name=_as_str(d.get("name")),
            host=_as_str(d.get("host"), "localhost"),
            port=_as_int(d.get("port"), 0),
            health_path=_as_str(d.get("health_path"), "/health"),
            ready_path=_as_str(d.get("ready_path"), "/ready"),
            critical=_as_bool(d.get("critical"), True),
            timeout_seconds=_as_float(d.get("timeout_seconds"), 3.0),
        )


@dataclass(frozen=True)
class ConsumerGroupRule:
    """A Kafka consumer group whose committed-offset lag we measure read-only.

    Lag thresholds are stored for the Phase 2 severity engine; Phase 1 only
    records the measured lag per topic/partition.
    """

    group_id: str
    topics: tuple[str, ...] = ()
    warn_lag: int = 1000
    critical_lag: int = 10000
    critical: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ConsumerGroupRule":
        return cls(
            group_id=_as_str(d.get("group_id") or d.get("group")),
            topics=_as_str_tuple(d.get("topics")),
            warn_lag=_as_int(d.get("warn_lag"), 1000),
            critical_lag=_as_int(d.get("critical_lag"), 10000),
            critical=_as_bool(d.get("critical"), True),
        )


@dataclass(frozen=True)
class TopicRule:
    """A Kafka topic we expect to exist (metadata-only existence/partition check)."""

    name: str
    min_partitions: int = 1

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TopicRule":
        return cls(
            name=_as_str(d.get("name")),
            min_partitions=_as_int(d.get("min_partitions"), 1),
        )


@dataclass(frozen=True)
class DynamoTableRule:
    """A DynamoDB table to confirm reachable via a read-only describe/get.

    ``suffix`` is appended to ``config.dynamodb_table_prefix`` to form the real
    table name, so the same rules.yaml works across dev/staging/prod prefixes.
    ``max_age_seconds`` / ``probe_key`` support Phase 2 staleness detection; they
    are parsed now and unused by Phase 1 collectors.
    """

    suffix: str
    critical: bool = True
    max_age_seconds: Optional[float] = None
    timestamp_attr: Optional[str] = None
    probe_key: dict[str, Any] = field(default_factory=dict)
    kill_switch_probe: bool = False  # if True, read KILLSWITCH/GLOBAL and DOWN if active=True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DynamoTableRule":
        return cls(
            suffix=_as_str(d.get("suffix") or d.get("name")),
            critical=_as_bool(d.get("critical"), True),
            max_age_seconds=_as_opt_float(d.get("max_age_seconds")),
            timestamp_attr=_as_opt_str(d.get("timestamp_attr")),
            probe_key=_as_dict(d.get("probe_key")),
            kill_switch_probe=_as_bool(d.get("kill_switch_probe"), False),
        )


@dataclass(frozen=True)
class DockerRule:
    """Policy for the Docker collector (container liveness + restart counts)."""

    enabled: bool = True
    name_contains: tuple[str, ...] = ()
    restart_warn: int = 1
    restart_critical: int = 3

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DockerRule":
        if not d:
            return cls()
        return cls(
            enabled=_as_bool(d.get("enabled"), True),
            name_contains=_as_str_tuple(d.get("name_contains")),
            restart_warn=_as_int(d.get("restart_warn"), 1),
            restart_critical=_as_int(d.get("restart_critical"), 3),
        )


@dataclass(frozen=True)
class LogRule:
    """Policy for the log collector (error-pattern scan over recent log files).

    Disabled by default — log scanning is opt-in via rules.yaml because paths are
    deployment-specific.
    """

    enabled: bool = False
    paths: tuple[str, ...] = ()
    error_patterns: tuple[str, ...] = ("ERROR", "CRITICAL", "Traceback")
    window_seconds: float = 300.0
    max_bytes_per_file: int = 2_000_000

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LogRule":
        if not d:
            return cls()
        patterns = _as_str_tuple(d.get("error_patterns"))
        return cls(
            enabled=_as_bool(d.get("enabled"), False),
            paths=_as_str_tuple(d.get("paths")),
            error_patterns=patterns or ("ERROR", "CRITICAL", "Traceback"),
            window_seconds=_as_float(d.get("window_seconds"), 300.0),
            max_bytes_per_file=_as_int(d.get("max_bytes_per_file"), 2_000_000),
        )


@dataclass(frozen=True)
class NotificationRule:
    """Notification policy (Slack only in Phase 1).

    ``min_status`` gates which overall statuses warrant an alert.
    ``repeat_suppression_seconds`` throttles re-alerting on an unchanged problem.
    """

    slack_enabled: bool = True
    min_status: str = "degraded"
    repeat_suppression_seconds: float = 900.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NotificationRule":
        if not d:
            return cls()
        slack = d.get("slack")
        slack_enabled = _as_bool(slack.get("enabled"), True) if isinstance(slack, dict) else True
        return cls(
            slack_enabled=slack_enabled,
            min_status=_as_str(d.get("min_status"), "degraded").strip().lower(),
            repeat_suppression_seconds=_as_float(d.get("repeat_suppression_seconds"), 900.0),
        )


@dataclass(frozen=True)
class MonitoringRules:
    """The fully-parsed monitoring policy. Immutable; safe to log."""

    services: tuple[ServiceRule, ...] = ()
    consumer_groups: tuple[ConsumerGroupRule, ...] = ()
    topics: tuple[TopicRule, ...] = ()
    dynamodb_tables: tuple[DynamoTableRule, ...] = ()
    docker: DockerRule = DockerRule()
    logs: LogRule = LogRule()
    notifications: NotificationRule = NotificationRule()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """True when no probes are configured (e.g. rules file failed to load)."""
        return not (
            self.services
            or self.consumer_groups
            or self.topics
            or self.dynamodb_tables
        )

    def summary(self) -> dict[str, int]:
        """Counts for logs/snapshots — confirms what policy is actually loaded."""
        return {
            "services": len(self.services),
            "consumer_groups": len(self.consumer_groups),
            "topics": len(self.topics),
            "dynamodb_tables": len(self.dynamodb_tables),
            "docker_enabled": int(self.docker.enabled),
            "logs_enabled": int(self.logs.enabled),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "MonitoringRules":
        """Build rules from a parsed mapping, ignoring unknown keys."""
        data = data or {}
        return cls(
            services=tuple(
                ServiceRule.from_dict(item)
                for item in _as_list_of_dicts(data.get("services"))
                if item.get("name")
            ),
            consumer_groups=tuple(
                ConsumerGroupRule.from_dict(item)
                for item in _as_list_of_dicts(data.get("consumer_groups"))
                if (item.get("group_id") or item.get("group"))
            ),
            topics=tuple(
                TopicRule.from_dict(item)
                for item in _as_list_of_dicts(data.get("topics"))
                if item.get("name")
            ),
            dynamodb_tables=tuple(
                DynamoTableRule.from_dict(item)
                for item in _as_list_of_dicts(data.get("dynamodb_tables"))
                if (item.get("suffix") or item.get("name"))
            ),
            docker=DockerRule.from_dict(_as_dict(data.get("docker"))),
            logs=LogRule.from_dict(_as_dict(data.get("logs"))),
            notifications=NotificationRule.from_dict(_as_dict(data.get("notifications"))),
            raw=dict(data),
        )


def load_rules(path: str) -> MonitoringRules:
    """Load and parse ``rules.yaml`` into a :class:`MonitoringRules`.

    Fail-soft contract:
        * Missing file       → empty rules + WARNING (agent still starts).
        * Unparseable YAML    → empty rules + ERROR (never crash a read-only agent).
        * PyYAML not installed → empty rules + ERROR (deps misconfigured).
    """
    try:
        import yaml  # lazy: keep module import safe even without PyYAML
    except ImportError:
        logger.error("PyYAML not installed; monitoring rules cannot be parsed. Running with empty rules.")
        return MonitoringRules()

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        logger.warning("Rules file not found at %s; running with empty rules.", path)
        return MonitoringRules()
    except OSError as exc:
        logger.error("Could not read rules file %s: %s; running with empty rules.", path, exc)
        return MonitoringRules()
    except yaml.YAMLError as exc:
        logger.error("Malformed YAML in %s: %s; running with empty rules.", path, exc)
        return MonitoringRules()

    if data is None:
        logger.warning("Rules file %s is empty; running with empty rules.", path)
        return MonitoringRules()
    if not isinstance(data, dict):
        logger.error("Rules file %s did not parse to a mapping (got %s); running with empty rules.", path, type(data).__name__)
        return MonitoringRules()

    rules = MonitoringRules.from_dict(data)
    logger.info("Loaded monitoring rules from %s: %s", path, rules.summary())
    return rules
