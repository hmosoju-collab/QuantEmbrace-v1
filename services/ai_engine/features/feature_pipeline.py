"""
FeaturePipeline — reads live features from the Phase 5 DynamoDB feature store.

Replaces the old S3-based placeholder with the real ``FeatureReader`` from
``shared/features/feature_reader.py``.  The FeatureReader provides
interval-aware staleness checking and graceful ``None`` degradation.

Phase 6 design (ADR-014 §5.4):
  Reading features from DynamoDB (online store) instead of S3 keeps enrichment
  within the 6–8ms latency budget.  S3 reads (historical data) are only used
  for model training (offline, not in production inference path).

Feature set provided to models (from Phase 5 FeatureSet):
    rsi_14, ema_9, ema_21, vwap, atr_14, adx_14,
    macd, macd_signal, macd_hist, volume_ratio

Degradation contract:
  ``get_features`` returns ``None`` when:
    - FeatureReader.get_latest returns None (stale or missing)
    - Any DynamoDB error
  The caller (RegimeClassifier, SignalQualityScorer) must handle None gracefully.
"""

from __future__ import annotations

from typing import Any, Optional

from shared.features.feature_reader import FeatureReader
from shared.logging.logger import get_logger
from shared.models.feature_set import FeatureSet

logger = get_logger(__name__, service_name="ai_engine")


class FeaturePipeline:
    """
    Thin wrapper around FeatureReader for the AI engine inference path.

    Converts ``FeatureSet`` into a plain ``dict[str, float]`` suitable for
    passing to sklearn / hmmlearn model ``.predict()`` calls.

    Args:
        feature_reader: Phase 5 FeatureReader (DynamoDB online store).
    """

    # Ordered feature list — governs the feature vector passed to models.
    # Matches Phase 5 FeatureSet fields.  Order must be consistent with
    # the ``features.json`` used during model training.
    MODEL_FEATURES: list[str] = [
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

    def __init__(self, feature_reader: FeatureReader) -> None:
        self._reader = feature_reader

    async def get_features(
        self,
        market:   str,
        symbol:   str,
        interval: str = "1m",
    ) -> Optional[dict[str, float]]:
        """
        Return the latest features for a symbol as a float dict.

        Returns ``None`` if no fresh feature set is available.  All callers must
        degrade gracefully when this returns ``None``.

        Args:
            market:   Market identifier e.g. ``"NSE"`` or ``"US"``.
            symbol:   Trading symbol e.g. ``"RELIANCE"``.
            interval: Candle interval e.g. ``"1m"``, ``"5m"``, ``"15m"``.

        Returns:
            ``dict[str, float]`` with feature values, or ``None`` on degradation.
        """
        try:
            feature_set: Optional[FeatureSet] = await self._reader.get_latest(
                market=market,
                symbol=symbol,
                interval=interval,
            )
        except Exception as exc:
            logger.error(
                "feature_pipeline.reader_error",
                market=market,
                symbol=symbol,
                interval=interval,
                error=str(exc),
            )
            return None

        if feature_set is None:
            logger.debug(
                "feature_pipeline.no_features",
                market=market,
                symbol=symbol,
                interval=interval,
            )
            return None

        return self._feature_set_to_dict(feature_set)

    def _feature_set_to_dict(self, fs: FeatureSet) -> dict[str, float]:
        """
        Convert a FeatureSet to a float dict, omitting None-valued features.

        Only features in ``MODEL_FEATURES`` are included.  None values are
        dropped — callers check completeness via ``has_required_features``.
        """
        result: dict[str, float] = {}
        for name in self.MODEL_FEATURES:
            value: Optional[float] = getattr(fs, name, None)
            if value is not None:
                result[name] = value
        return result

    def has_required_features(
        self,
        features: dict[str, float],
        required: list[str],
    ) -> bool:
        """
        Return True if all ``required`` feature names are present and non-None
        in ``features``.

        Args:
            features: Dict from ``get_features``.
            required: Feature names the model needs.
        """
        return all(name in features for name in required)

    def to_feature_vector(
        self,
        features: dict[str, float],
        feature_names: list[str],
    ) -> list[float]:
        """
        Build an ordered feature vector for model.predict().

        Args:
            features:      Dict of feature name → value.
            feature_names: Ordered list of feature names the model expects.

        Returns:
            List of float values in the order specified by ``feature_names``.
            Missing features are filled with 0.0.
        """
        return [features.get(name, 0.0) for name in feature_names]
