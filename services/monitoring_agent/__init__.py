"""QuantEmbrace Monitoring Agent — a separate, read-only observability service.

The monitoring agent runs as its own container/process. It continuously observes
the trading platform (Kafka, services, Docker, DynamoDB, broker state, logs) and
surfaces health snapshots, Slack alerts, and an incident log.

SAFETY CONTRACT (enforced platform-wide, across every phase):
    The agent may only ever *reduce* risk. It must NEVER place trades, increase
    exposure, override a risk rejection, change NAV, or disable the kill switch.

    Phase 1 (this package's current scope) is strictly READ-ONLY. It performs no
    actions of any kind — no DynamoDB writes, no Kafka produces, no broker calls.
    Risk-reducing automation (kill-switch trigger, pause-entries, etc.) is
    introduced only in Phase 4 and is gated behind DRY_RUN / ACTION_MODE.

See README.md for the phase roadmap and docs/runbooks/monitoring_agent.md to operate it.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"  # Phase 1 — read-only monitoring
