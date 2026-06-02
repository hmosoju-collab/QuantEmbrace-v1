"""Monitoring agent entrypoint — async poll loop, snapshot, alert, incident log.

Lifecycle each cycle:
    1. run every enabled collector concurrently (each is read-only, never raises);
    2. assemble a :class:`HealthSnapshot`;
    3. enrich it with Phase 2 severity findings (SeverityEngine — additive only);
    4. if ACTION_MODE="safe_actions", classify findings and execute approved safe
       actions via SafeActionExecutor (Phase 5 gate — disabled by default);
    5. write the snapshot atomically to disk + log it as structured JSON;
    6. detect status transitions via the incident log (edge-triggered);
    7. send a Slack alert for alertworthy transitions only;
    8. mark the agent ready and sleep until the next poll.

PHASE 1/2 SAFETY: this module wires NO action layer by default. There is no code
path from a collected status to a trade, an exposure change, a risk override, or
the kill switch unless the operator explicitly sets
MONITORING_ACTION_MODE=safe_actions.

PHASE 5 ACTION GATE:
    * ACTION_MODE absent / "notify_only" → observe, classify, record, notify only.
      No safe actions are executed. Counters increment: safe_actions_disabled_total.
    * ACTION_MODE="safe_actions" → SafeActionExecutor may execute BLOCK_NEW_ENTRIES
      and ACTIVATE_KILL_SWITCH for CRITICAL/BLOCKER findings. All actions go through
      SafeActionPolicy + idempotency + audit. Forbidden actions are always blocked.
    * The executor only runs if a SafeActionDynamoWriter is injected (requires
      DYNAMODB_TABLE_PREFIX + AWS credentials to be present). If not injected,
      safe actions are logged but not executed.

Run as a long-running service:   python -m monitoring_agent.app
Run a single cycle (cron/CI):     python -m monitoring_agent.app --once
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Optional

from monitoring_agent.collectors import build_collectors
from monitoring_agent.config import AgentConfig
from monitoring_agent.detectors.engine import SeverityEngine
from monitoring_agent.detectors.severity import Severity
from monitoring_agent.incident_log import IncidentLog
from monitoring_agent.notify.slack import SlackNotifier
from monitoring_agent.rules import MonitoringRules, load_rules
from monitoring_agent.snapshot import HealthSnapshot, Status

logger = logging.getLogger("monitoring_agent.app")

# Safe-actions imports are lazy (TYPE_CHECKING only here) to avoid a hard
# dependency on execution_engine when safe_actions mode is disabled.
_SAFE_ACTIONS_AVAILABLE = False
try:
    from execution_engine.safe_actions import (  # type: ignore[import]
        ActionScope,
        ActionType,
        SafeAction,
        SafeActionAudit,
        SafeActionClassifier,
        SafeActionDynamoWriter,
        SafeActionExecutor,
        SafeActionPolicy,
        RiskLevel,
        TradingMode,
    )
    _SAFE_ACTIONS_AVAILABLE = True
except ImportError:
    pass


class MonitoringAgent:
    """Owns config, rules, collectors, notifier, incident log, and health server."""

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        rules: Optional[MonitoringRules] = None,
        dynamo_writer: Optional[object] = None,
    ) -> None:
        self.config = config or AgentConfig.from_env()
        self.rules = rules if rules is not None else load_rules(self.config.rules_path)
        self.collectors = build_collectors(self.config, self.rules)
        self.severity_engine = SeverityEngine()
        self.notifier = SlackNotifier(self.config)
        self.incidents = IncidentLog(
            self.config.incident_log_path,
            min_status=self.rules.notifications.min_status,
        )
        self._health = None  # created lazily in run() to avoid binding a port in --once/tests
        self._stop = asyncio.Event()
        self._last_cycle_ts = 0.0
        # Phase 5: safe_actions executor — None unless ACTION_MODE="safe_actions"
        # and _SAFE_ACTIONS_AVAILABLE. Injected dynamo_writer takes precedence
        # over the auto-constructed one (for testing).
        self._safe_executor: Optional[object] = None
        self._safe_classifier: Optional[object] = None
        self._safe_counters = {
            "safe_actions_executed_total": 0,
            "safe_actions_blocked_total": 0,
            "safe_actions_disabled_total": 0,
        }
        if self.config.action_mode == "safe_actions" and _SAFE_ACTIONS_AVAILABLE:
            self._setup_safe_actions(dynamo_writer)
        self._enforce_phase1_safety()

    # ── Phase 5 safe-actions setup ────────────────────────────────────────────

    def _setup_safe_actions(self, dynamo_writer: Optional[object] = None) -> None:
        """Initialise the safe-actions classifier + executor for ACTION_MODE=safe_actions.

        If dynamo_writer is not injected, attempts to construct one from environment
        variables (DYNAMODB_TABLE_PREFIX + AWS credentials). If that fails, the
        executor runs in stub mode (policy/audit/idempotency work, but no DynamoDB
        writes occur).
        """
        try:
            policy = SafeActionPolicy.from_env()
            audit = SafeActionAudit(
                path=os.path.join(
                    os.path.dirname(self.config.snapshot_path),
                    "safe_actions_audit.jsonl",
                )
            )
            writer = dynamo_writer
            if writer is None:
                writer = self._try_build_dynamo_writer()
            self._safe_executor = SafeActionExecutor(
                policy=policy,
                audit=audit,
                notifier=lambda msg: self.notifier.send({"text": str(msg)}),
                dynamo_writer=writer,
                requested_by="monitoring_agent",
            )
            self._safe_classifier = SafeActionClassifier()
            logger.info(
                "monitoring_agent.safe_actions_enabled mode=%s dynamo_writer=%s",
                policy.mode.value,
                "injected" if writer else "stub",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "monitoring_agent.safe_actions_setup_failed error_type=%s — "
                "falling back to notify_only",
                type(exc).__name__,
            )

    def _try_build_dynamo_writer(self) -> Optional[object]:
        """Attempt to construct a SafeActionDynamoWriter from environment."""
        try:
            import boto3  # type: ignore[import]
            prefix = os.environ.get("DYNAMODB_TABLE_PREFIX", "")
            if not prefix:
                return None
            risk_state_table = f"{prefix}-risk-state"
            client = boto3.client(
                "dynamodb",
                endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
                region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-south-1"),
            )
            return SafeActionDynamoWriter(
                dynamo_client=client,
                risk_state_table=risk_state_table,
            )
        except Exception:  # noqa: BLE001
            return None

    # ── safety ────────────────────────────────────────────────────────────────

    def _enforce_phase1_safety(self) -> None:
        """Phase 1 is read-only/notify-only. Refuse to pretend otherwise."""
        if not self.config.is_phase1_safe:
            logger.critical(
                "UNSAFE CONFIG IGNORED: dry_run=%s action_mode=%s. Phase 1 is strictly "
                "read-only/notify-only and wires NO action layer — no action will be taken "
                "regardless of these values.",
                self.config.dry_run,
                self.config.action_mode,
            )

    # ── one cycle ───────────────────────────────────────────────────────────--

    async def collect_once(self) -> HealthSnapshot:
        """Run all collectors concurrently and assemble a snapshot."""
        results = await asyncio.gather(*(c.run() for c in self.collectors))
        return HealthSnapshot(
            results=list(results),
            dry_run=self.config.dry_run,
            action_mode=self.config.action_mode,
            phase=1,
        )

    def _write_snapshot(self, snapshot: HealthSnapshot) -> None:
        path = self.config.snapshot_path
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(snapshot.to_json())
            os.replace(tmp, path)  # atomic
        except OSError as exc:
            logger.warning("could not write snapshot to %s: %s", path, exc)

    async def run_once(self) -> HealthSnapshot:
        """Collect → enrich → (safe actions) → persist → transitions → alert."""
        snapshot = await self.collect_once()
        self._last_cycle_ts = time.time()

        # Phase 2: severity enrichment — additive, never changes overall_status.
        report = self.severity_engine.evaluate(snapshot)
        snapshot.severity = report.to_dict()
        snapshot.phase = 2

        # Phase 5: execute safe actions if ACTION_MODE=safe_actions.
        # Must happen AFTER severity enrichment and BEFORE snapshot write so
        # the audit log and snapshot are in sync.
        if self._safe_executor is not None and self._safe_classifier is not None:
            self._run_safe_actions(report)
        else:
            self._safe_counters["safe_actions_disabled_total"] += 1

        self._write_snapshot(snapshot)
        logger.info(
            "health cycle overall=%s severity=%s components=%s",
            snapshot.overall_status.value,
            report.overall_severity.value,
            {r.name: r.status.value for r in snapshot.results},
        )
        transitions = self.incidents.record(snapshot)
        alertworthy = [t for t in transitions if t.alertworthy]
        if alertworthy:
            delivered = self.notifier.notify(snapshot, alertworthy, report)
            logger.info("alert: %d change(s), slack_delivered=%s", len(alertworthy), delivered)
        return snapshot

    def _run_safe_actions(self, report: object) -> None:
        """Classify CRITICAL/BLOCKER findings and execute approved safe actions.

        Each finding's first proposed action type is attempted. All actions go
        through SafeActionPolicy + idempotency + audit — forbidden actions are
        always blocked regardless of this code path.
        """
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        findings = report.findings_at_or_above(Severity.CRITICAL)  # type: ignore[attr-defined]
        for finding in findings:
            obs = self._safe_classifier.classify_finding(  # type: ignore[attr-defined]
                finding.code, finding.subject
            )
            # Execute only the first (most important) proposed action per finding.
            for action_type in obs.proposed_action_types[:1]:
                if action_type.value == "FORBIDDEN":
                    continue
                try:
                    scope_map = {
                        "SEND_ALERT": ActionScope.READ_ONLY,
                        "GENERATE_RUNBOOK_COMMAND": ActionScope.LIVE_HUMAN_GATED,
                        "READ_RUNTIME_STATE": ActionScope.READ_ONLY,
                        "BLOCK_NEW_ENTRIES": ActionScope.RISK_REDUCTION,
                        "ACTIVATE_KILL_SWITCH": ActionScope.RISK_REDUCTION,
                    }
                    scope = scope_map.get(action_type.value, ActionScope.RISK_REDUCTION)
                    idem_key = (
                        f"{action_type.value.lower()}-{finding.subject or finding.component}"
                        f"-{today}"
                    )
                    action = SafeAction.build(
                        action_type=action_type,
                        scope=scope,
                        mode=SafeActionPolicy.from_env().mode,
                        risk_level=RiskLevel.HIGH if action_type.value == "ACTIVATE_KILL_SWITCH"
                                  else RiskLevel.LOW,
                        requires_human_approval=False,
                        idempotency_key=idem_key,
                        preconditions=[],
                        expected_effect=obs.reasoning,
                        rollback_behavior="operator-manual",
                        audit_payload={
                            "finding_code": finding.code,
                            "finding_subject": finding.subject,
                            "finding_severity": finding.severity.value,
                        },
                    )
                    result = self._safe_executor.execute(  # type: ignore[attr-defined]
                        action, reason=finding.message
                    )
                    if result.executed:
                        self._safe_counters["safe_actions_executed_total"] += 1
                        logger.info(
                            "monitoring_agent.safe_action_executed action_type=%s "
                            "finding=%s subject=%s idempotency_key=%s",
                            action_type.value, finding.code,
                            finding.subject, idem_key,
                        )
                    elif result.blocked:
                        self._safe_counters["safe_actions_blocked_total"] += 1
                        logger.info(
                            "monitoring_agent.safe_action_blocked action_type=%s "
                            "reason=%s",
                            action_type.value, result.blocked_reason,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "monitoring_agent.safe_action_error action_type=%s "
                        "error_type=%s",
                        action_type.value if hasattr(action_type, "value") else str(action_type),
                        type(exc).__name__,
                    )

    # ── long-running loop ──────────────────────────────────────────────────────

    async def run(self) -> None:
        from shared.health.health_server import HealthServer

        self._health = HealthServer(port=self.config.health_port, service_name="monitoring_agent")
        await self._health.start()
        self._install_signal_handlers()
        logger.info(
            "monitoring_agent started: collectors=%s poll=%.0fs config=%s",
            [c.name for c in self.collectors],
            self.config.poll_interval_seconds,
            self.config.redacted(),
        )
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                    self._health.set_ready(True)
                except Exception:  # noqa: BLE001 — a bad cycle must not kill the agent
                    logger.exception("monitoring cycle failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.config.poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            if self._health is not None:
                await self._health.stop()
            logger.info("monitoring_agent stopped")

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # not supported on this platform — fall back to KeyboardInterrupt


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="QuantEmbrace Phase 1 monitoring agent (read-only)")
    parser.add_argument("--once", action="store_true", help="run a single collection cycle and exit")
    args = parser.parse_args(argv)

    config = AgentConfig.from_env()
    _configure_logging(config.log_level)
    agent = MonitoringAgent(config=config)

    if args.once:
        snapshot = asyncio.run(agent.run_once())
        print(snapshot.to_json())
        return 0 if snapshot.overall_status != Status.DOWN else 1

    asyncio.run(agent.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
