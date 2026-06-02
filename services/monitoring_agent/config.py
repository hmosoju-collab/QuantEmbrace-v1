"""Runtime configuration for the monitoring agent (environment-driven).

This is deliberately separate from ``rules.yaml``:

    * ``config.py``  → operational/runtime knobs and *secrets* sourced from the
      environment (Slack webhook, AWS endpoint, table prefix, poll cadence,
      DRY_RUN / ACTION_MODE safety flags). Never serialised to the snapshot.
    * ``rules.yaml`` → declarative monitoring *policy* (which services/ports to
      probe, which Kafka groups to watch, thresholds for later phases). Safe to
      commit and to log.

Safety defaults (mandatory): ``DRY_RUN=true`` and ``ACTION_MODE=notify_only``.
Phase 1 takes no actions regardless, but these defaults define the contract that
later phases must honour before any risk-reducing automation can run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# ── Safety enums (string values match env input) ────────────────────────────────

# notify_only  → only emit alerts / write incident log (Phase 1 + safe default)
# safe_actions → permit Phase 3 non-trading actions (restart non-critical, reconcile)
# risk_reduce  → permit Phase 4 risk-reducing actions (trigger kill switch, pause)
ACTION_MODES = ("notify_only", "safe_actions", "risk_reduce")

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in _TRUE


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw is not None and raw != "" else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AgentConfig:
    """Immutable runtime configuration assembled from the environment."""

    # ── Safety (mandatory defaults) ─────────────────────────────────────────────
    dry_run: bool = True
    action_mode: str = "notify_only"

    # ── Loop / lifecycle ────────────────────────────────────────────────────────
    poll_interval_seconds: float = 30.0
    health_port: int = 8086
    log_level: str = "INFO"

    # ── Paths ───────────────────────────────────────────────────────────────────
    rules_path: str = "/app/services/monitoring_agent/rules.yaml"
    snapshot_path: str = "/app/data/health_snapshot.json"
    incident_log_path: str = "/app/data/monitoring_incidents.jsonl"

    # ── AWS / DynamoDB ──────────────────────────────────────────────────────────
    aws_region: str = "ap-south-1"
    aws_endpoint_url: Optional[str] = None      # LocalStack URL when set
    dynamodb_table_prefix: str = "quantembrace-development"

    # ── Kafka ───────────────────────────────────────────────────────────────────
    kafka_bootstrap_servers: str = ""
    kafka_use_iam: bool = True

    # ── Notifications (Slack only this phase) ───────────────────────────────────
    # Secret — resolved from env, NEVER serialised or logged.
    slack_webhook_url: Optional[str] = None
    slack_channel_hint: str = "#trading-ops"

    # ── Derived helpers ─────────────────────────────────────────────────────────

    @property
    def slack_enabled(self) -> bool:
        """True only when a webhook is configured; otherwise we log-only."""
        return bool(self.slack_webhook_url)

    @property
    def is_phase1_safe(self) -> bool:
        """Phase 1 contract: read-only, notify-only, dry-run."""
        return self.dry_run and self.action_mode == "notify_only"

    def table(self, suffix: str) -> str:
        """Return a fully-qualified DynamoDB table name for a suffix."""
        return f"{self.dynamodb_table_prefix}-{suffix}"

    def redacted(self) -> dict[str, object]:
        """Config view safe for logs/snapshots — secrets removed."""
        return {
            "dry_run": self.dry_run,
            "action_mode": self.action_mode,
            "poll_interval_seconds": self.poll_interval_seconds,
            "health_port": self.health_port,
            "log_level": self.log_level,
            "rules_path": self.rules_path,
            "snapshot_path": self.snapshot_path,
            "incident_log_path": self.incident_log_path,
            "aws_region": self.aws_region,
            "aws_endpoint_url": self.aws_endpoint_url,
            "dynamodb_table_prefix": self.dynamodb_table_prefix,
            "kafka_bootstrap_servers": self.kafka_bootstrap_servers,
            "kafka_use_iam": self.kafka_use_iam,
            "slack_enabled": self.slack_enabled,  # presence only, never the URL
        }

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """Build the config from environment variables, applying safe defaults."""
        action_mode = _env_str("MONITORING_ACTION_MODE", "notify_only").strip().lower()
        if action_mode not in ACTION_MODES:
            action_mode = "notify_only"

        endpoint = (
            os.environ.get("AWS_ENDPOINT_URL")
            or os.environ.get("LOCALSTACK_ENDPOINT_URL")
            or None
        )

        return cls(
            dry_run=_env_bool("MONITORING_DRY_RUN", True),
            action_mode=action_mode,
            poll_interval_seconds=_env_float("MONITORING_POLL_INTERVAL_SECONDS", 30.0),
            health_port=_env_int("QE_HEALTH_CHECK_PORT", 8086),
            log_level=_env_str("LOG_LEVEL", "INFO"),
            rules_path=_env_str(
                "MONITORING_RULES_PATH",
                "/app/services/monitoring_agent/rules.yaml",
            ),
            snapshot_path=_env_str("MONITORING_SNAPSHOT_PATH", "/app/data/health_snapshot.json"),
            incident_log_path=_env_str(
                "MONITORING_INCIDENT_LOG_PATH",
                "/app/data/monitoring_incidents.jsonl",
            ),
            aws_region=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")),
            aws_endpoint_url=endpoint,
            dynamodb_table_prefix=_env_str("DYNAMODB_TABLE_PREFIX", "quantembrace-development"),
            kafka_bootstrap_servers=_env_str("KAFKA_BOOTSTRAP_SERVERS", ""),
            kafka_use_iam=_env_bool("KAFKA_USE_IAM", True),
            slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL") or None,
            slack_channel_hint=_env_str("SLACK_CHANNEL", "#trading-ops"),
        )
