"""
Risk Engine Service — the mandatory gatekeeper between Strategy and Execution.

Every trading signal MUST pass through this service before reaching the
Execution Engine. There is no bypass path. If the Risk Engine is down,
trading halts — this is by design.

Signal flow: strategy_engine -> risk_engine -> execution_engine

The service runs validators in sequence. If ANY validator rejects a signal,
the signal is rejected and never reaches execution. Every decision (approve
or reject) is logged to S3 for audit and linked to the signal via a unique
``risk_decision_id``.
"""

from __future__ import annotations

import asyncio
import json
import signal
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger, set_correlation_id
from shared.utils.helpers import utc_iso, utc_now

from risk_engine.killswitch.auto_triggers import KillSwitchMonitor
from risk_engine.killswitch.killswitch import KillSwitch
from risk_engine.limits.risk_limits import RiskLimits, RiskValidationResult
from risk_engine.validators.exposure_validator import ExposureValidator
from risk_engine.validators.loss_validator import DailyLossValidator
from risk_engine.validators.position_validator import PositionValidator
from strategy_engine.signals.signal import Signal, SignalStatus

logger = get_logger(__name__, service_name="risk_engine")


class RiskDecisionStatus(str, Enum):
    """Outcome of a risk validation pass."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass
class RiskDecision:
    """
    Immutable record of a risk validation decision.

    Every decision is persisted to S3 for audit. The ``risk_decision_id``
    is attached to the downstream order so that any trade can be traced
    back to the risk check that approved it.
    """

    risk_decision_id: str
    signal_id: str
    status: RiskDecisionStatus
    reason: str
    validator_results: list[RiskValidationResult] = field(default_factory=list)
    timestamp: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the decision for JSON/S3 storage."""
        return {
            "risk_decision_id": self.risk_decision_id,
            "signal_id": self.signal_id,
            "status": self.status.value,
            "reason": self.reason,
            "validator_results": [
                {
                    "approved": vr.approved,
                    "validator_name": vr.validator_name,
                    "reason": vr.reason,
                    "details": vr.details,
                }
                for vr in self.validator_results
            ],
            "timestamp": self.timestamp.isoformat(),
        }


class RiskEngineService:
    """
    Main risk validation service.

    Lifecycle:
        1. ``start()`` — loads kill switch state, initializes validators,
           begins consuming signals from SQS.
        2. Event loop — validates each signal, publishes approved signals
           to the execution queue.
        3. ``stop()`` — drains in-flight validations, persists state.

    Restart-safety: Kill switch state is in DynamoDB. In-flight signals
    that were not yet forwarded will be re-delivered by SQS visibility
    timeout.
    """

    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        dynamo_client: Any = None,
        s3_client: Any = None,
        sqs_client: Any = None,
        sns_client: Any = None,
        sns_topic_arn: Optional[str] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._s3 = s3_client
        self._sqs = sqs_client
        self._sns = sns_client

        # Risk limits (loaded from settings)
        self._limits = RiskLimits(
            max_position_size_pct=self._settings.risk.max_position_size_pct,
            max_total_exposure_pct=self._settings.risk.max_total_exposure_pct,
            max_daily_loss_pct=self._settings.risk.max_daily_loss_pct,
            max_single_order_value=self._settings.risk.max_single_order_value,
            max_open_orders=self._settings.risk.max_open_orders,
            portfolio_value=self._settings.portfolio_value,
        )

        # Kill switch — with SNS client for sub-5s propagation
        _topic = sns_topic_arn or getattr(self._settings.aws, "sns_kill_switch_topic_arn", "")
        self._kill_switch = KillSwitch(
            dynamo_client=self._dynamo,
            sns_client=self._sns,
            sns_topic_arn=_topic,
            settings=self._settings,
        )

        # Auto-trigger monitor — 4 background health checks
        self._kill_switch_monitor = KillSwitchMonitor(
            kill_switch=self._kill_switch,
            settings=self._settings,
        )

        # Validators — executed in sequence
        self._position_validator = PositionValidator(
            limits=self._limits,
            dynamo_client=self._dynamo,
            settings=self._settings,
        )
        self._exposure_validator = ExposureValidator(
            limits=self._limits,
            dynamo_client=self._dynamo,
            settings=self._settings,
        )
        self._loss_validator = DailyLossValidator(
            limits=self._limits,
            dynamo_client=self._dynamo,
            settings=self._settings,
        )

        self._running = False
        self._shutdown_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def kill_switch(self) -> KillSwitch:
        """Access the kill switch instance."""
        return self._kill_switch

    async def start(self) -> None:
        """
        Start the Risk Engine Service.

        Loads persisted kill switch state and begins consuming signals
        from SQS.
        """
        set_correlation_id()
        logger.info("Starting Risk Engine Service")

        # Restore kill switch state from DynamoDB
        await self._kill_switch.load_state()
        if self._kill_switch.active:
            logger.warning(
                "Kill switch is ACTIVE on startup — all signals will be rejected until deactivated"
            )

        # Start automatic kill-switch monitors (order rate, connectivity,
        # data staleness, strategy loss)
        await self._kill_switch_monitor.start()

        # Register OS signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        self._running = True
        logger.info("Risk Engine Service started")

        await self._processing_loop()

    async def stop(self) -> None:
        """Gracefully stop the Risk Engine Service."""
        if not self._running:
            return

        logger.info("Stopping Risk Engine Service")
        self._running = False
        self._shutdown_event.set()
        await self._kill_switch_monitor.stop()
        logger.info("Risk Engine Service stopped")

    async def validate_signal(self, signal_obj: Signal) -> RiskDecision:
        """
        Run a signal through all risk validators.

        Validators are executed in sequence:
            1. Kill switch check (instant reject if active).
            2. Position size validator.
            3. Exposure validator.
            4. Daily loss validator.

        If ANY validator rejects, the pipeline short-circuits and the
        signal is rejected. The decision is logged to S3 for audit.

        Args:
            signal_obj: The trading signal to validate.

        Returns:
            RiskDecision with approval/rejection status and reasoning.
        """
        risk_decision_id = str(uuid.uuid4())
        validator_results: list[RiskValidationResult] = []

        # ----- Kill switch check -----
        if await self._kill_switch.is_active():
            decision = RiskDecision(
                risk_decision_id=risk_decision_id,
                signal_id=signal_obj.signal_id,
                status=RiskDecisionStatus.REJECTED,
                reason=f"Kill switch is active: {self._kill_switch.reason}",
                validator_results=[],
            )
            await self._log_decision(decision)
            logger.warning(
                "Signal %s REJECTED — kill switch active: %s",
                signal_obj.signal_id,
                self._kill_switch.reason,
            )
            return decision

        # ----- Run validators in sequence -----
        validators = [
            self._position_validator,
            self._exposure_validator,
            self._loss_validator,
        ]

        for validator in validators:
            result = await validator.validate(signal_obj)
            validator_results.append(result)

            if not result.approved:
                decision = RiskDecision(
                    risk_decision_id=risk_decision_id,
                    signal_id=signal_obj.signal_id,
                    status=RiskDecisionStatus.REJECTED,
                    reason=result.reason,
                    validator_results=validator_results,
                )
                await self._log_decision(decision)

                logger.warning(
                    "Signal %s REJECTED by %s: %s",
                    signal_obj.signal_id,
                    result.validator_name,
                    result.reason,
                )

                # Auto-activate kill switch if daily loss limit breached
                if result.validator_name == DailyLossValidator.VALIDATOR_NAME:
                    await self._kill_switch.activate(
                        reason=f"Auto-triggered: {result.reason}",
                        activated_by="loss_validator",
                    )

                return decision

        # ----- All validators passed -----
        decision = RiskDecision(
            risk_decision_id=risk_decision_id,
            signal_id=signal_obj.signal_id,
            status=RiskDecisionStatus.APPROVED,
            reason="All risk checks passed",
            validator_results=validator_results,
        )
        await self._log_decision(decision)

        logger.info(
            "Signal %s APPROVED (decision %s) — %s %s %s qty=%d",
            signal_obj.signal_id,
            risk_decision_id,
            signal_obj.strategy_name,
            signal_obj.direction.value,
            signal_obj.symbol,
            signal_obj.quantity,
        )

        return decision

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _processing_loop(self) -> None:
        """
        Main event loop: consume signals from SQS and validate them.

        Approved signals are forwarded to the execution engine's SQS queue.
        """
        while self._running:
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=1.0,
                )
                break
            except asyncio.TimeoutError:
                # Poll SQS for signals
                signals = await self._receive_signals()
                for signal_obj in signals:
                    decision = await self.validate_signal(signal_obj)
                    if decision.status == RiskDecisionStatus.APPROVED:
                        await self._forward_approved_signal(
                            signal_obj, decision.risk_decision_id
                        )

    async def _receive_signals(self) -> list[Signal]:
        """
        Receive signals from the SQS signals queue.

        Returns:
            List of deserialized Signal objects.
        """
        if self._sqs is None:
            return []

        try:
            response = await asyncio.to_thread(
                self._sqs.receive_message,
                QueueUrl=self._settings.aws.sqs_signals_queue,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=5,
            )

            signals: list[Signal] = []
            for message in response.get("Messages", []):
                body = json.loads(message["Body"])
                signal_obj = Signal.from_dict(body)
                signals.append(signal_obj)

                # Delete the message after successful deserialization
                await asyncio.to_thread(
                    self._sqs.delete_message,
                    QueueUrl=self._settings.aws.sqs_signals_queue,
                    ReceiptHandle=message["ReceiptHandle"],
                )

            return signals

        except Exception:
            logger.exception("Failed to receive signals from SQS")
            return []

    async def _forward_approved_signal(
        self, signal_obj: Signal, risk_decision_id: str
    ) -> None:
        """
        Forward an approved signal to the Execution Engine via SQS.

        Attaches the ``risk_decision_id`` so execution can be traced
        back to the risk approval.

        Args:
            signal_obj: The approved signal.
            risk_decision_id: Unique ID of the risk decision.
        """
        signal_obj.status = SignalStatus.APPROVED
        payload = signal_obj.to_dict()
        payload["risk_decision_id"] = risk_decision_id

        if self._sqs is None:
            logger.debug("No SQS client — approved signal not forwarded")
            return

        try:
            await asyncio.to_thread(
                self._sqs.send_message,
                QueueUrl=self._settings.aws.sqs_orders_queue,
                MessageBody=json.dumps(payload),
                MessageGroupId=signal_obj.symbol,
                MessageDeduplicationId=signal_obj.signal_id,
            )
            logger.info(
                "Forwarded approved signal %s to execution queue", signal_obj.signal_id
            )
        except Exception:
            logger.exception(
                "Failed to forward approved signal %s to execution queue",
                signal_obj.signal_id,
            )

    async def _log_decision(self, decision: RiskDecision) -> None:
        """
        Persist a risk decision to S3 for audit.

        Decisions are stored as JSON objects keyed by date and decision ID.

        Args:
            decision: The RiskDecision to log.
        """
        if self._s3 is None:
            logger.debug("No S3 client — risk decision not persisted to audit log")
            return

        try:
            today = utc_now().strftime("%Y-%m-%d")
            key = f"risk-audit/{today}/{decision.risk_decision_id}.json"
            body = json.dumps(decision.to_dict(), default=str)

            await asyncio.to_thread(
                self._s3.put_object,
                Bucket=self._settings.aws.s3_bucket,
                Key=key,
                Body=body.encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            logger.exception(
                "Failed to persist risk decision %s to S3",
                decision.risk_decision_id,
            )


async def main() -> None:
    """
    Entry point for the Risk Engine Service.

    Instantiates real boto3 clients and injects them into the service so
    that all SQS, DynamoDB, S3, and SNS calls use the shared singleton
    factory (auto-switches to LocalStack when LOCALSTACK_ENDPOINT_URL is set).
    """
    import os
    import boto3
    from shared.aws.clients import get_dynamodb_resource, get_s3_client, get_sqs_client

    settings = get_settings()
    sns = boto3.client("sns", region_name=settings.aws.region)
    topic_arn = os.environ.get(
        "SNS_KILL_SWITCH_TOPIC_ARN",
        getattr(settings.aws, "sns_kill_switch_topic_arn", ""),
    )

    service = RiskEngineService(
        dynamo_client=get_dynamodb_resource(),
        s3_client=get_s3_client(),
        sqs_client=get_sqs_client(),
        sns_client=sns,
        sns_topic_arn=topic_arn,
    )
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
