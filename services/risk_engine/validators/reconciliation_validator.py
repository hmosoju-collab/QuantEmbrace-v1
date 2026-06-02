"""
ReconciliationValidator — blocks new order intake when position drift is detected.

Phase 8 (ADR-015 F7): three-way reconciliation halt.

Root cause this validator addresses:
    After a service crash or network partition, the broker's position state may
    differ from DynamoDB and from what the risk engine believes.  Approving new
    signals on top of stale position state can create overlapping or doubled
    positions.

Mechanism:
    An operator (or the ``scripts/ops/reconcile.py`` tool) sets a
    ``reconciliation_required`` flag in DynamoDB (PK=RECONCILIATION#STATE,
    SK=GLOBAL).  This validator reads that flag on every signal and rejects
    non-closeout signals until an operator explicitly clears the flag.

    Paper-trade signals pass through — they do not open real broker positions
    and do not interact with the reconciliation-sensitive live book.

    Closeout signals pass through — they are identified by ``metadata["is_closeout"]=True``
    set by the strategy or execution engine to indicate a position-reducing order.
    Blocking closeouts would trap open positions, which is worse than the drift.

Recovery:
    1. Run ``python scripts/ops/reconcile.py --environment <env>``
       to detect and report broker/DynamoDB/Kafka drift.
    2. Manually resolve any discrepancies (cancel orphan orders, update DynamoDB).
    3. Clear the flag: ``python scripts/ops/reconcile.py --clear``.
    4. Risk engine resumes normal operation on the next signal.

ADR-015 §5.3: operator-only reconciliation clear — no automatic recovery.
    Auto-recovery would re-enable trading on stale state, which defeats the
    purpose of the halt. A human must verify position accuracy first.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.models.signal import Signal
from shared.risk_state import attr_bool, attr_string, reconciliation_key

from risk_engine.limits.risk_limits import RiskValidationResult

logger = get_logger(__name__, service_name="risk_engine")

VALIDATOR_NAME = "reconciliation_validator"

# TTL on the in-memory cache to avoid hitting DynamoDB on every signal (1s).
_CACHE_TTL_SECONDS: float = 1.0


class ReconciliationValidator:
    """
    Rejects new trading signals when the reconciliation_required flag is set.

    Reads the flag from DynamoDB at most once per second (cached). Safe to
    call synchronously (validate()) since the cache refresh is async and the
    hot path returns the cached value in O(1).

    Args:
        dynamo_client: Low-level boto3 DynamoDB client.
        risk_state_table: Name of the DynamoDB risk-state table.
        settings: AppSettings (used for table name fallback).
    """

    def __init__(
        self,
        dynamo_client: Any,
        risk_state_table: str,
        settings: Optional[AppSettings] = None,
        metrics_client: Optional[Any] = None,
    ) -> None:
        self._dynamo = dynamo_client
        self._table = risk_state_table
        self._settings = settings or get_settings()
        # Optional boto3 CloudWatch client used to emit an alarmable metric when
        # the reconciliation-flag read fails. When None, read failures are still
        # surfaced via a CRITICAL log line; only the CloudWatch metric is skipped.
        self._metrics = metrics_client

        # In-memory cache for the flag (avoids DynamoDB read on every signal)
        self._required: bool = False
        self._reason: str = ""
        self._set_by: str = ""
        self._last_refresh: float = 0.0
        # Observability (Phase 2.1 Q1): count consecutive read failures so a
        # persistent DynamoDB outage is visible even though we fail open.
        self._consecutive_read_failures: int = 0

    # ── Public interface ──────────────────────────────────────────────────────

    async def validate(self, signal: Signal) -> RiskValidationResult:
        """
        Check the reconciliation flag and approve/reject the signal.

        Paper-trade and closeout signals are always approved regardless of the
        flag (they do not create new net exposure).

        Args:
            signal: The trading signal to validate.

        Returns:
            RiskValidationResult — approved=True if safe to proceed.
        """
        # Paper-trade and closeout signals bypass reconciliation halt.
        if getattr(signal, "paper_trade", False):
            return RiskValidationResult(
                approved=True,
                validator_name=VALIDATOR_NAME,
                reason="paper_trade_exempt",
            )
        if signal.metadata.get("is_closeout"):
            return RiskValidationResult(
                approved=True,
                validator_name=VALIDATOR_NAME,
                reason="closeout_exempt",
            )

        await self._refresh_if_stale()

        if self._required:
            logger.warning(
                "reconciliation_validator.signal_rejected "
                "signal_id=%s symbol=%s reason=%s set_by=%s",
                signal.signal_id,
                signal.symbol,
                self._reason,
                self._set_by,
            )
            return RiskValidationResult(
                approved=False,
                validator_name=VALIDATOR_NAME,
                reason=(
                    f"RECONCILIATION_REQUIRED: trading halted pending operator reconciliation. "
                    f"reason={self._reason!r} set_by={self._set_by!r}. "
                    f"Run scripts/ops/reconcile.py --clear after verifying position accuracy."
                ),
                details={
                    "reconciliation_reason": self._reason,
                    "set_by": self._set_by,
                },
            )

        return RiskValidationResult(
            approved=True,
            validator_name=VALIDATOR_NAME,
            reason="reconciliation_not_required",
        )

    @property
    def reconciliation_required(self) -> bool:
        """Cached value — True if trading is currently halted for reconciliation."""
        return self._required

    # ── Cache refresh ─────────────────────────────────────────────────────────

    async def _refresh_if_stale(self) -> None:
        """Refresh the in-memory cache from DynamoDB if the TTL has expired."""
        if time.monotonic() - self._last_refresh < _CACHE_TTL_SECONDS:
            return

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._table,
                Key=reconciliation_key(),
                ConsistentRead=True,
            )
            item = response.get("Item")
            if item:
                self._required = attr_bool(item, "required", False)
                self._reason   = attr_string(item, "reason", "")
                self._set_by   = attr_string(item, "set_by", "")
            else:
                self._required = False
                self._reason   = ""
                self._set_by   = ""
            # Successful read clears the failure streak.
            self._consecutive_read_failures = 0
        except Exception as exc:
            self._consecutive_read_failures += 1
            # Distinct, alertable CRITICAL log line — separate from a generic
            # exception trace so dashboards/alarms can key on this event.
            logger.critical(
                "reconciliation_validator.dynamo_read_failed "
                "consecutive_failures=%d error=%s — FAILING OPEN (treating as not required)",
                self._consecutive_read_failures,
                str(exc),
            )
            self._emit_read_failure_metric()
            # On DynamoDB read failure, default to NOT required so a transient
            # outage doesn't block all trading. The kill switch provides a harder
            # safety backstop if DynamoDB is persistently unavailable.
            # NOTE: fail-open here is intentional and UNCHANGED by Phase 2.1 Q1 —
            # only observability (log + metric) was added.
            self._required = False

        self._last_refresh = time.monotonic()

    def _emit_read_failure_metric(self) -> None:
        """Emit a CloudWatch alarm metric on reconciliation-flag read failure.

        Best-effort and never raises: when no metrics client is configured the
        failure is still surfaced via the CRITICAL log above.
        """
        if not self._metrics:
            return
        try:
            self._metrics.put_metric_data(
                Namespace="QuantEmbrace/RiskEngine",
                MetricData=[{
                    "MetricName": "ReconciliationFlagReadFailure",
                    "Value":      1.0,
                    "Unit":       "Count",
                    "Dimensions": [{"Name": "Service", "Value": "risk_engine"}],
                }],
            )
        except Exception:
            logger.exception(
                "reconciliation_validator.read_failure_metric_emit_failed"
            )
