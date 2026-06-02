"""
Signal Age Validator — the FIRST gate in the risk validation chain.

Rejects any signal whose ``generated_at`` timestamp is older than
``max_signal_age_seconds``. This prevents stale signals — queued during a
restart, a processing backlog, or a WebSocket reconnect — from being
executed against current market prices.

Why first?
    Age validation is O(1) (a single datetime comparison). Running it before
    the DynamoDB-backed validators (position, exposure, loss) means we avoid
    unnecessary database reads on signals that are already invalid.

Typical failure scenarios caught by this validator:
    - Risk engine restarts and drains a 60-second Kafka lag.
    - Strategy engine Kafka consumer falls behind and a 12-second-old tick
      message produces a signal 14 seconds after the tick was generated.
    - WebSocket reconnection gap: strategy resumes from a stale buffer.
"""

from __future__ import annotations

from typing import Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.models.signal import Signal
from shared.utils.helpers import utc_now

from risk_engine.limits.risk_limits import RiskValidationResult

logger = get_logger(__name__, service_name="risk_engine")

# Hard ceiling regardless of configuration — protects against a misconfigured
# very large value that would defeat the purpose of this validator.
_ABSOLUTE_MAX_AGE_SECONDS: float = 30.0


class SignalAgeValidator:
    """
    Validates that a signal is not older than ``max_signal_age_seconds``.

    This is the first validator in the risk chain. All stale signals are
    rejected immediately without touching DynamoDB.

    Configuration:
        ``RISK_MAX_SIGNAL_AGE_SECONDS`` environment variable (default 5.0).
        Capped at _ABSOLUTE_MAX_AGE_SECONDS (30s) regardless of config.
    """

    VALIDATOR_NAME = "signal_age_validator"

    def __init__(self, settings: Optional[AppSettings] = None) -> None:
        self._settings = settings or get_settings()
        raw_max = self._settings.risk.max_signal_age_seconds
        # Enforce the hard ceiling
        self._max_age_seconds: float = min(raw_max, _ABSOLUTE_MAX_AGE_SECONDS)

        logger.info(
            "SignalAgeValidator initialized — max_age=%.1fs (configured=%.1fs, ceiling=%.1fs)",
            self._max_age_seconds,
            raw_max,
            _ABSOLUTE_MAX_AGE_SECONDS,
        )

    async def validate(self, signal: Signal) -> RiskValidationResult:
        """
        Reject the signal if its age exceeds ``max_signal_age_seconds``.

        Args:
            signal: The trading signal to validate.

        Returns:
            RiskValidationResult.approved=True if signal is fresh,
            False if stale.
        """
        now = utc_now()

        # generated_at may be tz-aware or tz-naive depending on the producer.
        # Normalise: if generated_at is tz-naive, treat it as UTC.
        generated_at = signal.generated_at
        if generated_at.tzinfo is None:
            from datetime import timezone
            generated_at = generated_at.replace(tzinfo=timezone.utc)

        if now.tzinfo is None:
            from datetime import timezone
            now = now.replace(tzinfo=timezone.utc)

        age_seconds = (now - generated_at).total_seconds()

        if age_seconds < 0:
            # Clock skew: signal appears to be from the future.
            # Allow with a warning — reject only if egregiously wrong (> 2s).
            if abs(age_seconds) > 2.0:
                return RiskValidationResult(
                    approved=False,
                    validator_name=self.VALIDATOR_NAME,
                    reason=(
                        f"Signal {signal.signal_id} has a future timestamp "
                        f"({abs(age_seconds):.2f}s ahead of system clock). "
                        "Possible clock skew — rejecting to prevent mis-execution."
                    ),
                    details={
                        "signal_generated_at": generated_at.isoformat(),
                        "validator_now": now.isoformat(),
                        "age_seconds": age_seconds,
                        "max_age_seconds": self._max_age_seconds,
                    },
                )
            # Minor future skew (< 2s) — accept
            return RiskValidationResult(
                approved=True,
                validator_name=self.VALIDATOR_NAME,
                reason=f"Signal age OK (minor clock skew {abs(age_seconds):.3f}s)",
                details={"age_seconds": age_seconds},
            )

        if age_seconds > self._max_age_seconds:
            logger.warning(
                "STALE SIGNAL %s rejected: age=%.2fs > max=%.1fs | "
                "symbol=%s strategy=%s generated_at=%s",
                signal.signal_id,
                age_seconds,
                self._max_age_seconds,
                signal.symbol,
                signal.strategy_name,
                generated_at.isoformat(),
            )
            return RiskValidationResult(
                approved=False,
                validator_name=self.VALIDATOR_NAME,
                reason=(
                    f"Signal is stale: age {age_seconds:.2f}s exceeds "
                    f"max {self._max_age_seconds:.1f}s. "
                    f"Generated at {generated_at.isoformat()}, "
                    f"validated at {now.isoformat()}."
                ),
                details={
                    "signal_generated_at": generated_at.isoformat(),
                    "validator_now": now.isoformat(),
                    "age_seconds": age_seconds,
                    "max_age_seconds": self._max_age_seconds,
                },
            )

        return RiskValidationResult(
            approved=True,
            validator_name=self.VALIDATOR_NAME,
            reason=f"Signal age {age_seconds:.3f}s within limit {self._max_age_seconds:.1f}s",
            details={"age_seconds": age_seconds},
        )
