"""Slack notifier — posts health alerts to an incoming webhook (read-only HTTP).

Safety / privacy:
    * The webhook URL is a secret. It is NEVER logged, never put in a snapshot,
      and never included in an exception message we emit (we log only the
      exception *type*, never the full error string which could echo the URL).
    * Only component *summaries*, status transitions, and Phase 2 severity
      *findings* (a stable code + a secret-free message + numeric context) are
      sent — never the raw collector ``details`` (which may contain log lines or
      metrics). This keeps potentially sensitive payloads out of the chat channel.
    * If no webhook is configured the notifier degrades to logging the alert text
      at INFO, so the agent is still useful in local/dev runs.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import TYPE_CHECKING, Any, Optional

from monitoring_agent.detectors.severity import Severity, severity_rank
from monitoring_agent.snapshot import HealthSnapshot, Status

if TYPE_CHECKING:  # avoid a runtime import cycle
    from monitoring_agent.config import AgentConfig
    from monitoring_agent.detectors.engine import SeverityReport
    from monitoring_agent.incident_log import Transition

logger = logging.getLogger("monitoring_agent.notify.slack")

_STATUS_EMOJI = {
    Status.OK.value: ":white_check_mark:",
    Status.DEGRADED.value: ":warning:",
    Status.DOWN.value: ":red_circle:",
    Status.UNKNOWN.value: ":grey_question:",
}

_SEVERITY_EMOJI = {
    Severity.INFO.value: ":large_blue_circle:",
    Severity.WARNING.value: ":warning:",
    Severity.CRITICAL.value: ":red_circle:",
    Severity.BLOCKER.value: ":rotating_light:",
}

# Cap on how many top findings to list so an alert never becomes a wall of text.
_MAX_FINDINGS_IN_ALERT = 8
_POST_TIMEOUT = 5.0


def format_slack_payload(
    snapshot: HealthSnapshot,
    transitions: list["Transition"],
    channel_hint: str = "",
    report: Optional["SeverityReport"] = None,
) -> dict[str, Any]:
    """Build a secret-free Slack message payload from a snapshot + transitions.

    When a Phase 2 :class:`SeverityReport` is supplied, a one-line severity summary
    and the CRITICAL/BLOCKER findings (worst-first) are appended. Only each
    finding's ``message`` is rendered — never raw collector ``details``.
    """
    overall = snapshot.overall_status.value
    header_emoji = _STATUS_EMOJI.get(overall, "")
    lines = [
        f"{header_emoji} *QuantEmbrace platform: {overall.upper()}* "
        f"(phase {snapshot.phase}, dry_run={snapshot.dry_run}, mode={snapshot.action_mode})",
        f"_generated {snapshot.generated_at}_",
    ]

    if report is not None:
        counts = report.counts()
        sev = report.overall_severity.value
        lines.append(
            f"{_SEVERITY_EMOJI.get(sev, '')} *Severity: {sev.upper()}* "
            f"(blocker={counts['blocker']}, critical={counts['critical']}, "
            f"warning={counts['warning']}, info={counts['info']})"
        )
        top = sorted(
            report.findings_at_or_above(Severity.CRITICAL),
            key=lambda f: severity_rank(f.severity),
            reverse=True,
        )
        if top:
            lines.append("*Top severity findings:*")
            for f in top[:_MAX_FINDINGS_IN_ALERT]:
                femoji = _SEVERITY_EMOJI.get(f.severity.value, "")
                label = f"{f.component}/{f.subject}" if f.subject else f.component
                lines.append(f"{femoji} `{label}`: {f.message}")

    if transitions:
        lines.append("*Changes this cycle:*")
        for t in transitions:
            emoji = _STATUS_EMOJI.get(t.current, "")
            arrow = f"{t.previous or 'new'} → {t.current}"
            lines.append(f"{emoji} `{t.component}`: {arrow} — {t.summary}")

    issues = [r for r in snapshot.results if r.status != Status.OK]
    if issues:
        lines.append("*Current issues:*")
        for r in issues:
            emoji = _STATUS_EMOJI.get(r.status.value, "")
            lines.append(f"{emoji} `{r.name}`: {r.summary}")

    payload: dict[str, Any] = {"text": "\n".join(lines)}
    if channel_hint:
        payload["channel"] = channel_hint  # honoured only if the webhook allows it; otherwise ignored
    return payload


class SlackNotifier:
    """Post alerts to a Slack incoming webhook, or log them if none is set."""

    def __init__(self, config: "AgentConfig") -> None:
        self._webhook = config.slack_webhook_url
        self._channel = config.slack_channel_hint

    @property
    def enabled(self) -> bool:
        return bool(self._webhook)

    def notify(
        self,
        snapshot: HealthSnapshot,
        transitions: list["Transition"],
        report: Optional["SeverityReport"] = None,
    ) -> bool:
        """Format and send an alert. Returns True if delivered to Slack."""
        return self.send(format_slack_payload(snapshot, transitions, self._channel, report))

    def send(self, payload: dict[str, Any]) -> bool:
        """POST a payload to the webhook. Never raises; never leaks the URL."""
        if not self._webhook:
            logger.info("slack disabled (no webhook). Alert summary:\n%s", payload.get("text", ""))
            return False
        try:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                self._webhook,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=_POST_TIMEOUT) as resp:  # noqa: S310
                code = resp.getcode()
                if 200 <= code < 300:
                    return True
                logger.warning("slack webhook returned HTTP %s", code)
                return False
        except Exception as exc:  # noqa: BLE001 — log type only, never the URL-bearing message
            logger.warning("slack post failed: %s", type(exc).__name__)
            return False
