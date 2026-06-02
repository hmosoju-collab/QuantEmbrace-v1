"""
Kill Switch — emergency trading halt mechanism.

Provides a persistent kill switch that halts ALL trading when activated.
State is stored in DynamoDB for persistence across service restarts.
Activation publishes to an SNS topic so all services can react within
the propagation SLA (≤5 seconds).

Activation can be triggered:
    - Manually: operator calls activate() via CLI or HTTP API.
    - Automatically: loss validator, order-rate monitor, connectivity
      monitor, and data-staleness monitor all call activate() when their
      respective thresholds are breached.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.risk_state import (
    attr_bool,
    attr_string,
    kill_switch_item,
    kill_switch_key,
)
from shared.utils.helpers import utc_iso, utc_now

logger = get_logger(__name__, service_name="risk_engine")

class KillSwitch:
    """
    Emergency kill switch that halts all trading when active.

    State is persisted in DynamoDB so that a service restart does not
    inadvertently re-enable trading after a halt. On startup the risk
    engine must call ``load_state()`` to restore the kill switch status.

    When activated, an SNS message is published to the configured topic
    so downstream services (execution engine, strategy engine) can react
    without polling DynamoDB.

    Activation can be:
        - Manual: operator calls ``activate()`` via admin API or CLI.
        - Automatic: loss/rate/connectivity/staleness monitors trigger.

    When active, every signal submitted to the risk engine is immediately
    rejected with reason ``KILL_SWITCH_ACTIVE``.
    """

    def __init__(
        self,
        dynamo_client: Any = None,
        sns_client: Any = None,
        table_name: Optional[str] = None,
        sns_topic_arn: Optional[str] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._sns = sns_client
        self._table_name = table_name or getattr(
            self._settings.aws,
            "dynamodb_table_risk_state",
            self._settings.aws.dynamodb_table_orders,
        )
        self._sns_topic_arn = sns_topic_arn or getattr(
            self._settings.aws, "sns_kill_switch_topic_arn", ""
        )
        self._active: bool = False
        self._activated_at: Optional[datetime] = None
        self._reason: str = ""
        self._activated_by: str = "unknown"
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """Whether the kill switch is currently engaged."""
        return self._active

    @property
    def activated_at(self) -> Optional[datetime]:
        """Timestamp when the kill switch was last activated."""
        return self._activated_at

    @property
    def reason(self) -> str:
        """Human-readable reason for activation."""
        return self._reason

    async def is_active(self) -> bool:
        """
        Check if the kill switch is currently active.

        Returns:
            True if trading is halted.
        """
        return self._active

    def get_status(self) -> dict[str, Any]:
        """
        Return a serializable status snapshot for the API and CLI.

        Returns:
            Dict with keys: active, reason, activated_at, activated_by.
        """
        return {
            "active": self._active,
            "reason": self._reason,
            "activated_at": self._activated_at.isoformat() if self._activated_at else None,
            "activated_by": self._activated_by if self._active else None,
        }

    async def activate(
        self,
        reason: str = "Manual activation",
        activated_by: str = "operator",
    ) -> None:
        """
        Activate the kill switch, halting all trading.

        Persists state to DynamoDB and publishes an SNS notification so
        all services can react within the propagation SLA (≤5 seconds).
        Idempotent: duplicate activations while already active are ignored.

        Args:
            reason: Human-readable reason for the activation.
            activated_by: Identifier of the actor (e.g. "operator", "loss_validator").
        """
        async with self._lock:
            if self._active:
                logger.warning(
                    "Kill switch already active (reason=%s) — ignoring duplicate activation",
                    self._reason,
                )
                return

            self._active = True
            self._activated_at = utc_now()
            self._reason = reason
            self._activated_by = activated_by

            logger.critical(
                "KILL SWITCH ACTIVATED | reason=%s | by=%s | at=%s",
                reason,
                activated_by,
                self._activated_at.isoformat(),
            )

            # Persist and notify concurrently for minimum propagation latency
            await asyncio.gather(
                self._persist_state(),
                self._publish_sns_event("ACTIVATED"),
                return_exceptions=True,
            )

    async def deactivate(self, deactivated_by: str = "operator") -> None:
        """
        Deactivate the kill switch, resuming normal trading.

        Should only be called by an operator after manual review.
        Idempotent: no-op if already inactive.

        Args:
            deactivated_by: Identifier of the actor deactivating the switch.
        """
        async with self._lock:
            if not self._active:
                logger.warning("Kill switch already inactive — ignoring deactivation")
                return

            logger.critical(
                "KILL SWITCH DEACTIVATED | by=%s | was_active_since=%s | was_reason=%s",
                deactivated_by,
                self._activated_at.isoformat() if self._activated_at else "unknown",
                self._reason,
            )

            self._active = False
            self._activated_at = None
            self._reason = ""
            self._activated_by = ""

            await asyncio.gather(
                self._persist_state(),
                self._publish_sns_event("DEACTIVATED"),
                return_exceptions=True,
            )

    async def load_state(self) -> None:
        """
        Restore kill switch state from DynamoDB on service startup.

        If the record is missing or the read fails, the switch keeps the
        in-memory state. This avoids clearing an already-active halt because
        DynamoDB had a transient read problem.
        """
        if self._dynamo is None:
            logger.warning("No DynamoDB client — kill switch state not loaded from persistence")
            return

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._table_name,
                Key=kill_switch_key(),
            )

            item = response.get("Item")
            if item:
                self._active = attr_bool(item, "active", False)
                activated_at_str = (
                    attr_string(item, "activated_at", "")
                    or attr_string(item, "activation_time", "")
                )
                if activated_at_str:
                    self._activated_at = datetime.fromisoformat(activated_at_str)
                elif not self._active:
                    self._activated_at = None
                self._reason = attr_string(item, "reason", "")
                self._activated_by = attr_string(item, "activated_by", "unknown")
                if self._active:
                    logger.info(
                        "Kill switch state loaded | active=%s | reason=%s",
                        self._active,
                        self._reason,
                    )
                else:
                    logger.debug(
                        "Kill switch state loaded | active=%s | reason=%s",
                        self._active,
                        self._reason,
                    )
            else:
                logger.debug("No kill switch record in DynamoDB — defaulting to inactive")

        except Exception:
            logger.exception(
                "Failed to load kill switch state — keeping in-memory state active=%s",
                self._active,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _persist_state(self) -> None:
        """Write the current kill switch state to DynamoDB."""
        if self._dynamo is None:
            logger.warning("No DynamoDB client — kill switch state not persisted")
            return

        try:
            now = utc_iso()
            item: dict[str, Any] = kill_switch_item(
                active=self._active,
                reason=self._reason,
                activated_by=self._activated_by,
                activated_at=self._activated_at.isoformat() if self._activated_at else None,
                deactivated_at=now if not self._active else None,
                updated_at=now,
            )

            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._table_name,
                Item=item,
            )
            logger.debug("Kill switch state persisted | active=%s", self._active)

        except Exception:
            logger.exception("Failed to persist kill switch state to DynamoDB")

    async def _publish_sns_event(self, event_type: str) -> None:
        """
        Publish a kill switch event to the SNS topic.

        Args:
            event_type: ``"ACTIVATED"`` or ``"DEACTIVATED"``.
        """
        if self._sns is None or not self._sns_topic_arn:
            logger.debug("SNS not configured — skipping kill switch notification")
            return

        payload = {
            "event": f"KILL_SWITCH_{event_type}",
            "active": self._active,
            "reason": self._reason,
            "activated_by": self._activated_by,
            "timestamp": utc_iso(),
        }
        if self._activated_at:
            payload["activated_at"] = self._activated_at.isoformat()

        try:
            await asyncio.to_thread(
                self._sns.publish,
                TopicArn=self._sns_topic_arn,
                Subject=f"QuantEmbrace Kill Switch {event_type}",
                Message=json.dumps(payload),
                MessageAttributes={
                    "event_type": {
                        "DataType": "String",
                        "StringValue": f"KILL_SWITCH_{event_type}",
                    }
                },
            )
            logger.info(
                "Kill switch SNS notification published | event=%s | topic=%s",
                event_type,
                self._sns_topic_arn,
            )
        except Exception:
            # SNS failure must not prevent the kill switch from activating
            logger.exception(
                "Failed to publish kill switch SNS notification — state is still persisted"
            )
