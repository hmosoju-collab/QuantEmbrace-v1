"""
RegimeClassifier — HMM-based market regime detector.

Classifies the current market into one of four regimes:
    trending  — strong directional price movement, ADX > 25
    ranging   — sideways price action, ADX < 20, low volatility
    volatile  — large price swings, high ATR relative to average
    crash     — sharp downward movement, RSI < 30, extreme ATR

Phase 6 design (ADR-014 §5.1):
  Model: Hidden Markov Model (hmmlearn) trained offline on NSE daily
  returns + volatility.  Serialised with joblib.  Loaded via ModelRegistry
  at service start.  Inference uses features from Phase 5 FeatureSet.

  In production, the HMM predicts the most likely hidden state and maps it
  to a human-readable regime label.  The posterior probability of the
  predicted state is returned as ``confidence``.

  Stub model behaviour (paper mode):
    When ModelRegistry loads a DummyClassifier stub (no real HMM trained yet),
    regime defaults to ``"unknown"`` with confidence 0.0.

Degradation contract:
  Any failure in feature reads or model inference returns:
    regime      = "unknown"
    confidence  = 0.0
  The enriched signal still flows through — regime is advisory.

Required FeatureSet fields:
    rsi_14, adx_14, atr_14, macd, volume_ratio
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from shared.logging.logger import get_logger
from ai_engine.features.feature_pipeline import FeaturePipeline
from ai_engine.models.model_registry import ModelRegistry

logger = get_logger(__name__, service_name="ai_engine")

MODEL_NAME = "regime_classifier"

# Feature names required for regime classification
REGIME_FEATURES: list[str] = [
    "rsi_14",
    "adx_14",
    "atr_14",
    "macd",
    "volume_ratio",
]

RegimeLabel = Literal["trending", "ranging", "volatile", "crash", "unknown"]

# HMM state → regime label mapping (trained model must match this ordering)
_STATE_TO_REGIME: dict[int, RegimeLabel] = {
    0: "trending",
    1: "ranging",
    2: "volatile",
    3: "crash",
}


@dataclass(frozen=True)
class RegimeOutput:
    """Output from a single regime classification call."""

    regime:      RegimeLabel  # "trending" | "ranging" | "volatile" | "crash" | "unknown"
    confidence:  float        # 0.0–1.0  (posterior probability of predicted state)
    computed_at: datetime     # UTC timestamp when classification ran


# Singleton degradation output for fast path
_DEGRADED_REGIME = RegimeOutput(
    regime="unknown",
    confidence=0.0,
    computed_at=datetime.now(timezone.utc),
)


def _fresh_degraded() -> RegimeOutput:
    """Return a degraded RegimeOutput with the current timestamp."""
    return RegimeOutput(
        regime="unknown",
        confidence=0.0,
        computed_at=datetime.now(timezone.utc),
    )


class RegimeClassifier:
    """
    Wrapper around the HMM regime classification model.

    Loads from ModelRegistry on construction.  Uses FeaturePipeline to
    fetch live features from DynamoDB.

    Args:
        model_registry:   Shared ModelRegistry (must have loaded MODEL_NAME).
        feature_pipeline: FeaturePipeline backed by Phase 5 FeatureReader.
    """

    def __init__(
        self,
        model_registry:   ModelRegistry,
        feature_pipeline: FeaturePipeline,
    ) -> None:
        self._registry = model_registry
        self._pipeline = feature_pipeline

    async def ensure_loaded(self) -> None:
        """Load the regime classifier model if not already cached."""
        if not self._registry.is_loaded(MODEL_NAME):
            await self._registry.load_model(MODEL_NAME)

    async def classify(
        self,
        market:   str,
        symbol:   str,
        interval: str = "1m",
    ) -> RegimeOutput:
        """
        Classify the current market regime for a symbol.

        Fetches live features from DynamoDB and runs HMM inference.
        Returns a degraded output on any failure — never raises.

        Args:
            market:   Market identifier ("NSE" or "US").
            symbol:   Trading symbol e.g. "RELIANCE".
            interval: Candle interval for feature freshness.

        Returns:
            RegimeOutput with regime label and confidence.
        """
        try:
            return await self._classify(market, symbol, interval)
        except Exception as exc:
            logger.error(
                "regime_classifier.unexpected_error",
                market=market,
                symbol=symbol,
                error=str(exc),
            )
            return _fresh_degraded()

    async def _classify(
        self,
        market:   str,
        symbol:   str,
        interval: str,
    ) -> RegimeOutput:
        """Inner classification — raises on errors (caller wraps)."""
        await self.ensure_loaded()
        model, metadata = self._registry.get(MODEL_NAME)

        if model is None:
            logger.warning(
                "regime_classifier.model_not_loaded", market=market, symbol=symbol
            )
            return _fresh_degraded()

        # Fetch live features
        features = await self._pipeline.get_features(
            market=market, symbol=symbol, interval=interval
        )

        if features is None:
            logger.debug(
                "regime_classifier.features_unavailable",
                market=market,
                symbol=symbol,
            )
            return _fresh_degraded()

        if not self._pipeline.has_required_features(features, REGIME_FEATURES):
            logger.debug(
                "regime_classifier.insufficient_features",
                available=list(features.keys()),
                required=REGIME_FEATURES,
            )
            return _fresh_degraded()

        # Build feature vector
        feature_names = metadata.features if metadata and metadata.features else REGIME_FEATURES
        feature_vector = self._pipeline.to_feature_vector(features, feature_names)

        # Run inference
        regime, confidence = await asyncio.to_thread(
            _run_hmm_inference, model, feature_vector, metadata.is_stub if metadata else True
        )

        return RegimeOutput(
            regime=regime,
            confidence=confidence,
            computed_at=datetime.now(timezone.utc),
        )


# ── Inference helper ──────────────────────────────────────────────────────────

def _run_hmm_inference(
    model: Any,
    feature_vector: list[float],
    is_stub: bool,
) -> tuple[RegimeLabel, float]:
    """
    Synchronous inference — runs in asyncio.to_thread.

    Handles both real HMM (hmmlearn) and stub DummyClassifier.
    Returns (regime_label, confidence).
    """
    import numpy as np

    if is_stub or not hasattr(model, "predict"):
        return "unknown", 0.0

    X = np.array(feature_vector, dtype=float).reshape(1, -1)

    try:
        # sklearn-compatible path (DummyClassifier or trained classifier)
        prediction = model.predict(X)
        raw_label = prediction[0]

        # Map integer state to label (HMM returns int state index)
        if isinstance(raw_label, (int, np.integer)):
            regime = _STATE_TO_REGIME.get(int(raw_label), "unknown")
        elif isinstance(raw_label, str):
            regime = raw_label if raw_label in _STATE_TO_REGIME.values() else "unknown"  # type: ignore[assignment]
        else:
            regime = "unknown"

        # Extract posterior confidence if available (predict_proba)
        confidence = 0.0
        if hasattr(model, "predict_proba"):
            proba = model.predict_proba(X)[0]
            confidence = float(max(proba)) if len(proba) > 0 else 0.0

        # Stub DummyClassifier always returns "unknown" constant — set confidence 0
        if regime == "unknown":
            confidence = 0.0

        return regime, confidence  # type: ignore[return-value]

    except Exception as exc:
        logger.error("regime_classifier.inference_error", error=str(exc))
        return "unknown", 0.0
