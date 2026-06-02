"""
SignalQualityScorer — GBT-based signal quality scorer.

Scores each signal on a 0.0–1.0 scale based on historical signal outcomes:
  1.0 → historically high probability of hitting take-profit within N bars
  0.0 → historically low probability (signal likely to be stopped out)
  0.5 → degraded / unknown (features unavailable — safe neutral)

Phase 6 design (ADR-014 §5.2):
  Model: LightGBM / sklearn GradientBoostingClassifier, trained offline on
  historical signal outcomes.  Serialised with joblib.  Loaded via
  ModelRegistry at service start.

  The quality_score feeds the soft-filter mechanism in SignalEnricher:
    if quality_score < threshold:  filtered = True
  Risk engine respects filtered=True as a rejection when the quality filter
  is enabled (threshold > 0.0 in strategy-config DynamoDB).

  Default threshold is 0.0 — no filtering occurs in Phase 6 by default.
  Operator raises the threshold after observing the score distribution
  over >= 10 paper trading sessions.

  Stub model behaviour (paper mode):
    DummyClassifier stub returns class 0, which maps to quality_score=0.5
    (neutral, no filtering).  The enrichment pipeline runs end-to-end.

Degradation contract:
  Any failure returns quality_score=0.5, filtered=False.  Signal flows.

Required FeatureSet fields:
    rsi_14, ema_9, ema_21, vwap, atr_14, adx_14, macd, macd_signal,
    macd_hist, volume_ratio
Plus signal metadata:
    direction (encoded: BUY=1, SELL=-1)
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, Optional

from shared.logging.logger import get_logger
from shared.models.signal import Direction
from ai_engine.features.feature_pipeline import FeaturePipeline
from ai_engine.models.model_registry import ModelRegistry

logger = get_logger(__name__, service_name="ai_engine")

MODEL_NAME = "signal_quality_scorer"

# Features passed to the quality scoring model
QUALITY_FEATURES: list[str] = [
    "rsi_14",
    "ema_9",
    "ema_21",
    "vwap",
    "atr_14",
    "adx_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "volume_ratio",
]

# Neutral score returned on degradation
_DEGRADED_SCORE: float = 0.5


class SignalQualityScorer:
    """
    Wrapper around the GBT signal quality model.

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
        """Load the quality scorer model if not already cached."""
        if not self._registry.is_loaded(MODEL_NAME):
            await self._registry.load_model(MODEL_NAME)

    async def score(
        self,
        market:    str,
        symbol:    str,
        direction: Direction,
        interval:  str = "1m",
        *,
        threshold: float = 0.0,
    ) -> tuple[float, bool]:
        """
        Score signal quality and determine if the soft filter trips.

        Returns:
            (quality_score, filtered)
            quality_score: float 0.0–1.0
            filtered:      True if quality_score < threshold

        Args:
            market:    Market identifier ("NSE" or "US").
            symbol:    Trading symbol.
            direction: Signal direction (BUY or SELL).
            interval:  Candle interval for feature freshness.
            threshold: Soft-filter threshold (default 0.0 = no filtering).
        """
        try:
            return await self._score(market, symbol, direction, interval, threshold)
        except Exception as exc:
            logger.error(
                "signal_quality_scorer.unexpected_error",
                market=market,
                symbol=symbol,
                error=str(exc),
            )
            return _DEGRADED_SCORE, False

    async def _score(
        self,
        market:    str,
        symbol:    str,
        direction: Direction,
        interval:  str,
        threshold: float,
    ) -> tuple[float, bool]:
        """Inner scoring — raises on errors (caller wraps)."""
        await self.ensure_loaded()
        model, metadata = self._registry.get(MODEL_NAME)

        if model is None:
            logger.warning(
                "signal_quality_scorer.model_not_loaded",
                market=market,
                symbol=symbol,
            )
            return _DEGRADED_SCORE, False

        features = await self._pipeline.get_features(
            market=market, symbol=symbol, interval=interval
        )

        if features is None:
            logger.debug(
                "signal_quality_scorer.features_unavailable",
                market=market,
                symbol=symbol,
            )
            return _DEGRADED_SCORE, False

        # Add direction encoding as a feature
        direction_encoded = 1.0 if direction == Direction.BUY else -1.0
        features_with_dir = {**features, "direction": direction_encoded}

        feature_names = metadata.features if metadata and metadata.features else QUALITY_FEATURES
        feature_vector = self._pipeline.to_feature_vector(features_with_dir, feature_names)

        quality_score = await asyncio.to_thread(
            _run_quality_inference,
            model,
            feature_vector,
            metadata.is_stub if metadata else True,
        )

        filtered = quality_score < threshold
        if filtered:
            logger.info(
                "signal_quality_scorer.filtered",
                market=market,
                symbol=symbol,
                quality_score=quality_score,
                threshold=threshold,
            )

        return quality_score, filtered


# ── Inference helper ──────────────────────────────────────────────────────────

def _run_quality_inference(
    model:          Any,
    feature_vector: list[float],
    is_stub:        bool,
) -> float:
    """
    Synchronous inference — runs in asyncio.to_thread.

    Returns quality_score as a float in [0.0, 1.0].
    Stub model always returns 0.5 (neutral).
    """
    import numpy as np

    if is_stub or not hasattr(model, "predict"):
        return _DEGRADED_SCORE

    X = np.array(feature_vector, dtype=float).reshape(1, -1)

    try:
        # Prefer predict_proba (probability of class=1 = "good signal")
        if hasattr(model, "predict_proba"):
            proba = model.predict_proba(X)[0]
            # Binary classifier: proba[1] = P(signal is good)
            if len(proba) == 2:
                return float(np.clip(proba[1], 0.0, 1.0))
            # Multi-class: use max probability as a quality proxy
            return float(np.clip(max(proba), 0.0, 1.0))

        # Fallback: predict() returning a float regression output
        raw = model.predict(X)[0]
        return float(np.clip(raw, 0.0, 1.0))

    except Exception as exc:
        logger.error("signal_quality_scorer.inference_error", error=str(exc))
        return _DEGRADED_SCORE
