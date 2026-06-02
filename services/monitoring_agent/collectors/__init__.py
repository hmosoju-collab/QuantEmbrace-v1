"""Read-only collectors for the Phase 1 monitoring agent.

Each collector observes one slice of the platform and returns a coarse
:class:`~monitoring_agent.snapshot.CollectorResult`. They are strictly read-only
(see ``base.Collector`` and the test-suite API audit).

``build_collectors`` wires the standard set in a sensible reporting order and
drops any collector that is disabled by the loaded rules (Docker and logs are
opt-in).
"""

from __future__ import annotations

from monitoring_agent.collectors.base import Collector
from monitoring_agent.collectors.broker_collector import BrokerCollector
from monitoring_agent.collectors.container_log_collector import ContainerLogCollector
from monitoring_agent.collectors.docker_collector import DockerCollector
from monitoring_agent.collectors.dynamodb_collector import DynamoDBCollector
from monitoring_agent.collectors.kafka_collector import KafkaCollector
from monitoring_agent.collectors.log_collector import LogCollector
from monitoring_agent.collectors.service_collector import ServiceCollector

# Reporting order for the snapshot (most operationally important first).
ALL_COLLECTOR_CLASSES: tuple[type[Collector], ...] = (
    ServiceCollector,
    KafkaCollector,
    DynamoDBCollector,
    BrokerCollector,
    DockerCollector,
    LogCollector,
    ContainerLogCollector,
)

__all__ = [
    "Collector",
    "ServiceCollector",
    "KafkaCollector",
    "DynamoDBCollector",
    "BrokerCollector",
    "DockerCollector",
    "LogCollector",
    "ContainerLogCollector",
    "ALL_COLLECTOR_CLASSES",
    "build_collectors",
]


def build_collectors(config, rules) -> list[Collector]:
    """Instantiate the standard collector set, keeping only enabled ones."""
    collectors = [cls(config, rules) for cls in ALL_COLLECTOR_CLASSES]
    return [c for c in collectors if c.enabled()]
