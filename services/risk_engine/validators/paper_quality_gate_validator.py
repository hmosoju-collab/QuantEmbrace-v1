"""PaperQualityGateValidator — confidence and reward:risk filters for paper trading.

Two filters in one validator:
    1. Confidence threshold: reject signals below per-strategy min_confidence.
    2. Reward:risk ratio: reject signals with R:R below per-strategy minimum.

Both filters only apply to new-entry signals.  Exit signals (is_closeout=True
or signal_id starts with "EXIT-") are always exempt.

Rejection reasons:
    CONFIDENCE_BELOW_THRESHOLD — signal.confidence below min for strategy.
    REWARD_RISK_TOO_LOW        — computed R:R below min for strategy.

Default thresholds (paper_optimization.yaml values):
    min_confidence: vwap_reversion=0.90, orb_15m=0.93
    min_reward_risk: vwap_reversion=1.20, orb_15m=1.30

If stop_loss or take_profit is absent, the R:R check is skipped (can't compute).
This validator is synchronous — no DynamoDB reads.
"""

from __future__ import annotations

import logging
from typing import Optional

from risk_engine.limits.risk_limits import RiskValidationResult
from shared.models.signal import Direction, Signal

logger = logging.getLogger("risk_engine.validators.paper_quality_gate")

VALIDATOR_NAME = "paper_quality_gate_validator"


def _is_exit_signal(signal: Signal) -> bool:
    if signal.metadata.get("is_closeout"):
        return True
    if signal.signal_id.startswith("EXIT-"):
        return True
    return False


def _threshold_for(thresholds: dict[str, float], strategy_name: str) -> float:
    """Resolve a per-strategy threshold, tolerating market-prefixed names.

    Signals carry market-prefixed strategy names ("nse_vwap_reversion") while
    paper_optimization.yaml keys are unprefixed ("vwap_reversion"). The exact
    lookup alone returned 0.0 for every live strategy, silently disabling both
    quality filters from Session 12 through Session 15.
    """
    if strategy_name in thresholds:
        return thresholds[strategy_name]
    for prefix in ("nse_", "us_"):
        if strategy_name.startswith(prefix):
            return thresholds.get(strategy_name[len(prefix):], 0.0)
    return 0.0


class PaperQualityGateValidator:
    """Filter low-confidence and low-R:R entry signals in paper mode.

    Args:
        min_confidence_by_strategy: Dict mapping strategy_name → minimum confidence (0.0–1.0).
            Signals below the threshold are rejected with CONFIDENCE_BELOW_THRESHOLD.
        min_rr_by_strategy: Dict mapping strategy_name → minimum reward:risk ratio.
            Entries where abs(tp - entry) / abs(entry - stop) < threshold are rejected.
    """

    def __init__(
        self,
        min_confidence_by_strategy: Optional[dict[str, float]] = None,
        min_rr_by_strategy: Optional[dict[str, float]] = None,
    ) -> None:
        self._min_confidence: dict[str, float] = min_confidence_by_strategy or {}
        self._min_rr: dict[str, float] = min_rr_by_strategy or {}

    def validate(self, signal: Signal) -> RiskValidationResult:
        """Check confidence and R:R for a new-entry signal.

        Returns:
            RiskValidationResult — approved=True if both checks pass.
        """
        if _is_exit_signal(signal):
            return RiskValidationResult(
                approved=True,
                validator_name=VALIDATOR_NAME,
                reason="exit_signal_exempt",
            )

        # ── Confidence filter ──────────────────────────────────────────────────
        min_conf = _threshold_for(self._min_confidence, signal.strategy_name)
        if min_conf > 0.0 and signal.confidence < min_conf:
            logger.info(
                "paper_quality_gate.confidence_rejected "
                "signal_id=%s symbol=%s strategy=%s confidence=%.3f min=%.3f",
                signal.signal_id, signal.symbol, signal.strategy_name,
                signal.confidence, min_conf,
            )
            return RiskValidationResult(
                approved=False,
                validator_name=VALIDATOR_NAME,
                reason=(
                    f"CONFIDENCE_BELOW_THRESHOLD: {signal.strategy_name} "
                    f"confidence={signal.confidence:.3f} < required {min_conf:.3f}"
                ),
                details={
                    "strategy_name": signal.strategy_name,
                    "signal_confidence": signal.confidence,
                    "min_confidence": min_conf,
                },
            )

        # ── Reward:Risk filter ─────────────────────────────────────────────────
        rr_result = self._check_reward_risk(signal)
        if rr_result is not None:
            return rr_result

        return RiskValidationResult(
            approved=True,
            validator_name=VALIDATOR_NAME,
            reason=(
                f"quality_gate_ok: confidence={signal.confidence:.3f}>={min_conf:.3f}"
            ),
            details={
                "signal_confidence": signal.confidence,
                "min_confidence": min_conf,
            },
        )

    def _check_reward_risk(self, signal: Signal) -> Optional[RiskValidationResult]:
        """Return a rejection result if R:R is below threshold, else None."""
        min_rr = _threshold_for(self._min_rr, signal.strategy_name)
        if min_rr <= 0.0:
            return None

        stop_loss = signal.stop_loss
        take_profit = signal.take_profit
        entry = signal.price_at_signal

        if stop_loss is None or take_profit is None or entry <= 0:
            return None

        if signal.direction == Direction.BUY:
            expected_reward = take_profit - entry
            expected_risk = entry - stop_loss
        else:
            expected_reward = entry - take_profit
            expected_risk = stop_loss - entry

        if expected_risk <= 0 or expected_reward <= 0:
            # Can't compute valid R:R — skip the check (graceful degradation)
            return None

        rr = expected_reward / expected_risk

        if rr < min_rr:
            logger.info(
                "paper_quality_gate.rr_rejected "
                "signal_id=%s symbol=%s strategy=%s rr=%.2f min=%.2f "
                "entry=%.2f stop=%.2f tp=%.2f",
                signal.signal_id, signal.symbol, signal.strategy_name,
                rr, min_rr, entry, stop_loss, take_profit,
            )
            return RiskValidationResult(
                approved=False,
                validator_name=VALIDATOR_NAME,
                reason=(
                    f"REWARD_RISK_TOO_LOW: {signal.strategy_name} "
                    f"R:R={rr:.2f} < required {min_rr:.2f} "
                    f"(reward={expected_reward:.2f}, risk={expected_risk:.2f})"
                ),
                details={
                    "strategy_name": signal.strategy_name,
                    "reward_risk_ratio": rr,
                    "min_reward_risk_ratio": min_rr,
                    "expected_reward": expected_reward,
                    "expected_risk": expected_risk,
                    "entry_price": entry,
                    "stop_price": stop_loss,
                    "take_profit_price": take_profit,
                },
            )

        return None

    @staticmethod
    def compute_reward_risk(
        direction: Direction,
        entry: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
    ) -> Optional[float]:
        """Utility: compute R:R for persistence to orders table metadata.

        Returns the float R:R ratio, or None if it cannot be computed.
        """
        if stop_loss is None or take_profit is None or entry <= 0:
            return None
        if direction == Direction.BUY:
            reward = take_profit - entry
            risk = entry - stop_loss
        else:
            reward = entry - take_profit
            risk = stop_loss - entry
        if risk <= 0 or reward <= 0:
            return None
        return reward / risk
