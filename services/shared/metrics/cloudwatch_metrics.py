"""
CloudWatch Metrics — structured latency and trading metric emission.

Provides a thin, fire-and-forget wrapper around boto3's put_metric_data
so every service can emit named metrics without boilerplate.

Design decisions:
  - Batched: metrics accumulate in a buffer and flush every 20 items
    (CloudWatch max per call) or when flush() is called explicitly.
  - Fire-and-forget: metric emission NEVER blocks or raises in the trading
    hot path. A CloudWatch failure logs a warning and drops the metric.
    Latency of the trading path is more important than metric completeness.
  - Thread-safe: asyncio.to_thread() wraps the blocking boto3 call.

Usage (in execution_engine):

    from shared.metrics.cloudwatch_metrics import get_metrics_client

    _metrics = get_metrics_client(namespace="QuantEmbrace/Trading")

    t0 = time.perf_counter()
    # ... place order ...
    _metrics.record_latency("OrderPlacementLatencyMs", t0, dimensions={"Market": "NSE"})

Available helpers:
    record_latency(name, start_time, dimensions)   — ms since perf_counter start
    record_count(name, value, dimensions)          — increment a counter
    record_gauge(name, value, unit, dimensions)    — set an absolute value
    flush()                                        — force-send buffered metrics

Namespace convention:
    "QuantEmbrace/Trading"    — live trading metrics
    "QuantEmbrace/Risk"       — risk engine decisions
    "QuantEmbrace/Data"       — data ingestion stats
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# CloudWatch allows max 20 metric data points per PutMetricData call.
_BATCH_SIZE: int = 20

# Maximum number of buffered (unflushed) metrics before oldest are dropped.
# Prevents unbounded memory use if flush thread falls behind.
_MAX_BUFFER: int = 200


@dataclass
class _MetricDatum:
    """One metric data point ready for CloudWatch."""

    metric_name: str
    value: float
    unit: str
    dimensions: dict[str, str]
    timestamp: float = field(default_factory=time.time)


class CloudWatchMetrics:
    """
    Batched, fire-and-forget CloudWatch metric emitter.

    All public methods are synchronous (no await needed in hot paths).
    The flush is async and is called in background tasks or on service stop.
    """

    def __init__(
        self,
        namespace: str,
        cloudwatch_client: Any = None,
        region: str = "ap-south-1",
    ) -> None:
        """
        Args:
            namespace: CloudWatch metric namespace (e.g. "QuantEmbrace/Trading").
            cloudwatch_client: boto3 CloudWatch client. If None, metrics are
                               logged as DEBUG (useful for local dev without AWS).
            region: AWS region for auto-creating the client if not supplied.
        """
        self._namespace = namespace
        self._cw = cloudwatch_client
        self._region = region
        self._buffer: list[_MetricDatum] = []
        self._lock = asyncio.Lock()
        self._dry_run = cloudwatch_client is None

        if self._dry_run:
            logger.debug(
                "CloudWatchMetrics initialised in DRY-RUN mode "
                "(no cloudwatch_client — metrics will be logged only)"
            )

    # ── Public recording methods (synchronous, safe to call in hot path) ──────

    def record_latency(
        self,
        metric_name: str,
        start_time: float,
        dimensions: Optional[dict[str, str]] = None,
    ) -> None:
        """
        Record elapsed time since ``start_time`` (from time.perf_counter()).

        Args:
            metric_name: CloudWatch metric name.
            start_time:  Start time from time.perf_counter().
            dimensions:  Optional dict of CloudWatch dimension name → value.
        """
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        self._buffer_metric(metric_name, elapsed_ms, "Milliseconds", dimensions or {})

    def record_count(
        self,
        metric_name: str,
        value: float = 1.0,
        dimensions: Optional[dict[str, str]] = None,
    ) -> None:
        """
        Increment a counter metric.

        Args:
            metric_name: CloudWatch metric name.
            value:        Amount to add (default 1).
            dimensions:   Optional dimension dict.
        """
        self._buffer_metric(metric_name, value, "Count", dimensions or {})

    def record_gauge(
        self,
        metric_name: str,
        value: float,
        unit: str = "None",
        dimensions: Optional[dict[str, str]] = None,
    ) -> None:
        """
        Record an absolute gauge value.

        Args:
            metric_name: CloudWatch metric name.
            value:        Current gauge reading.
            unit:         CloudWatch unit string (e.g. "Milliseconds", "Count",
                          "Percent", "Bytes", "None").
            dimensions:   Optional dimension dict.
        """
        self._buffer_metric(metric_name, value, unit, dimensions or {})

    # ── Async flush ───────────────────────────────────────────────────────────

    async def flush(self) -> None:
        """
        Send all buffered metrics to CloudWatch.

        Safe to call frequently — no-ops if buffer is empty.
        Never raises — a CloudWatch API failure is logged and dropped.
        """
        if not self._buffer:
            return

        async with self._lock:
            batch, self._buffer = self._buffer[:_BATCH_SIZE], self._buffer[_BATCH_SIZE:]

        if not batch:
            return

        if self._dry_run:
            for m in batch:
                logger.debug(
                    "DRY-RUN metric: namespace=%s name=%s value=%.3f unit=%s dims=%s",
                    self._namespace,
                    m.metric_name,
                    m.value,
                    m.unit,
                    m.dimensions,
                )
            return

        metric_data = [
            {
                "MetricName": m.metric_name,
                "Value": m.value,
                "Unit": m.unit,
                "Dimensions": [
                    {"Name": k, "Value": v} for k, v in m.dimensions.items()
                ],
            }
            for m in batch
        ]

        try:
            await asyncio.to_thread(
                self._cw.put_metric_data,
                Namespace=self._namespace,
                MetricData=metric_data,
            )
        except Exception:
            logger.warning(
                "CloudWatch metric flush failed — %d metric(s) dropped",
                len(batch),
                exc_info=True,
            )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _buffer_metric(
        self,
        metric_name: str,
        value: float,
        unit: str,
        dimensions: dict[str, str],
    ) -> None:
        """Buffer a metric datum. Drops oldest if buffer is full."""
        datum = _MetricDatum(
            metric_name=metric_name,
            value=value,
            unit=unit,
            dimensions=dimensions,
        )
        if len(self._buffer) >= _MAX_BUFFER:
            self._buffer.pop(0)  # Drop oldest to prevent unbounded growth
            logger.warning(
                "CloudWatch metric buffer full (%d) — oldest metric dropped",
                _MAX_BUFFER,
            )
        self._buffer.append(datum)


# ── Module-level singleton factory ────────────────────────────────────────────

_instances: dict[str, CloudWatchMetrics] = {}


def get_metrics_client(
    namespace: str = "QuantEmbrace/Trading",
    cloudwatch_client: Any = None,
) -> CloudWatchMetrics:
    """
    Get or create a CloudWatchMetrics instance for a namespace.

    Reuses existing instances — safe to call at module level.

    Args:
        namespace:          CloudWatch namespace.
        cloudwatch_client:  boto3 CloudWatch client. If None the first time,
                            attempts to create one via boto3. Subsequent calls
                            with the same namespace reuse the cached instance.

    Returns:
        CloudWatchMetrics instance for the namespace.
    """
    if namespace in _instances:
        return _instances[namespace]

    client = cloudwatch_client
    if client is None:
        try:
            import boto3
            from shared.config.settings import get_settings
            _settings = get_settings()
            client = boto3.client("cloudwatch", region_name=_settings.aws.region)
        except Exception:
            logger.warning(
                "Could not create CloudWatch client — metrics will be dry-run only",
                exc_info=True,
            )
            client = None

    instance = CloudWatchMetrics(namespace=namespace, cloudwatch_client=client)
    _instances[namespace] = instance
    return instance
