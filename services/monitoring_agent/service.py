"""Fleet-parity service entrypoint for the monitoring agent.

Every QuantEmbrace service exposes a ``service.py`` so the shared multi-stage
image can launch it with ``python -m services.<name>.service`` (see
``infra/deployment/Dockerfile``). This module exists purely so the monitoring
agent obeys that same contract; all real logic lives in :mod:`monitoring_agent.app`.

The agent's own dedicated image (``Dockerfile.monitoring_agent``) invokes
``python -m monitoring_agent.app`` directly — both paths run the identical
read-only Phase 1 poll loop. Running this module with no arguments starts the
long-running loop; pass ``--once`` for a single cycle (cron / CI / smoke test).
"""

from __future__ import annotations

from monitoring_agent.app import main

if __name__ == "__main__":
    raise SystemExit(main())
