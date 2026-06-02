"""Notification layer for the monitoring agent (Slack only in Phase 1).

Notifications are *outbound observability*, not trading actions — emitting a Slack
alert is the agent's whole purpose and is fully compatible with the read-only
safety contract. Phase 1 ships a single Slack webhook notifier with a stdout/log
fallback when no webhook is configured. A pluggable multi-channel design
(Telegram, PagerDuty, etc.) is intentionally deferred.
"""

from __future__ import annotations

from monitoring_agent.notify.slack import SlackNotifier, format_slack_payload

__all__ = ["SlackNotifier", "format_slack_payload"]
