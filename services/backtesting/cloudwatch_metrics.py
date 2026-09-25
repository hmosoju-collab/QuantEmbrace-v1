"""CloudWatch metric emitter for the QuantEmbrace backtesting lab.

Emits custom metrics under the ``QuantEmbrace/Backtest`` namespace. All emits
are no-ops (debug log only) when no CW client is provided — so unit tests and
local runs never require AWS credentials.

Backtest-only: no broker APIs, no live trading, no live table access.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

_NAMESPACE = "QuantEmbrace/Backtest"
logger = logging.getLogger(__name__)


class BacktestCloudWatchEmitter:
    """Thin wrapper around ``cloudwatch.put_metric_data``.

    All methods degrade gracefully to a warning log on emit failure so a CW
    outage never crashes a running backtest.
    """

    def __init__(self, cw_client: Any = None) -> None:
        """
        Args:
            cw_client: A boto3 CloudWatch client (or compatible mock/fake).
                       When ``None``, all emits are no-ops (test / local mode).
        """
        self._cw = cw_client

    @classmethod
    def from_aws(cls) -> "BacktestCloudWatchEmitter":
        """Construct with the shared sanctioned CloudWatch client."""
        from shared.aws.clients import get_cloudwatch_client

        return cls(get_cloudwatch_client())

    # ── lifecycle metrics ────────────────────────────────────────────────────────

    def run_completed(self) -> None:
        """Emitted once per run that reaches COMPLETED."""
        self._emit("RunCompleted", 1)

    def run_failed(self) -> None:
        """Emitted on any run failure (including Spot interruption)."""
        self._emit("RunFailed", 1)

    def checkpoint_written(self) -> None:
        """Emitted each time a partition checkpoint is durably written."""
        self._emit("CheckpointWritten", 1)

    def dq_gate_failed(self) -> None:
        """Emitted when the data-quality pre-run gate rejects a snapshot."""
        self._emit("DQGateFailed", 1)

    def active_run_count(self, count: int) -> None:
        """Gauge: number of runs currently in CREATED or RUNNING state."""
        self._emit("ActiveRunCount", count)

    def candles_replayed(self, count: int) -> None:
        """Counter: total candles delivered to the strategy in one run."""
        self._emit("CandlesReplayed", count)

    # ── internals ────────────────────────────────────────────────────────────────

    def _emit(self, metric_name: str, value: float, *, unit: str = "Count") -> None:
        if self._cw is None:
            logger.debug("CW no-op (no client): %s = %s", metric_name, value)
            return
        try:
            self._cw.put_metric_data(
                Namespace=_NAMESPACE,
                MetricData=[
                    {
                        "MetricName": metric_name,
                        "Value": value,
                        "Unit": unit,
                        "Timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ],
            )
        except Exception as exc:
            logger.warning("CloudWatch emit non-fatal: %s — %s", metric_name, exc)
