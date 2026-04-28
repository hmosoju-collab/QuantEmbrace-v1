"""Predictor.

Runs model inference on extracted features and returns predictions
with confidence scores. Orchestrates the feature pipeline and model registry.
"""

from typing import Any, Optional

import structlog

from services.ai_engine.features.feature_pipeline import FeaturePipeline
from services.ai_engine.models.model_registry import ModelRegistry
from services.shared.logging.logger import get_logger

logger = get_logger(__name__)


class Predictor:
    """Runs ML model inference for signal enhancement.

    Orchestrates:
        1. Feature computation (via FeaturePipeline)
        2. Model retrieval (via ModelRegistry)
        3. Inference execution
        4. Confidence calibration
    """

    def __init__(
        self,
        feature_pipeline: FeaturePipeline,
        model_registry: ModelRegistry,
    ) -> None:
        self._feature_pipeline = feature_pipeline
        self._model_registry = model_registry

    async def predict(
        self,
        symbol: str,
        model_name: str = "default",
        precomputed_features: Optional[dict[str, float]] = None,
    ) -> dict[str, Any]:
        """Generate a prediction for a symbol using the specified model.

        Args:
            symbol: Trading symbol.
            model_name: Which model to use for inference.
            precomputed_features: Pre-computed features to skip pipeline computation.

        Returns:
            Dictionary with keys: prediction, confidence, model_version, features_used.

        Raises:
            ValueError: If the requested model is not loaded.
        """
        # Ensure model is loaded
        if not self._model_registry.is_loaded(model_name):
            await self._model_registry.load_model(model_name)

        model = self._model_registry.get_model(model_name)
        metadata = self._model_registry.get_metadata(model_name)

        if model is None or metadata is None:
            raise ValueError(f"Model '{model_name}' could not be loaded.")

        # Compute or use provided features
        if precomputed_features:
            features = precomputed_features
            features_used = list(precomputed_features.keys())
        else:
            features = await self._feature_pipeline.compute_features(
                symbol=symbol,
                feature_set=metadata.features if metadata.features else None,
            )
            features_used = list(features.keys())

        if not features:
            logger.warning(
                "predictor.no_features",
                symbol=symbol,
                model_name=model_name,
            )
            return {
                "prediction": 0.0,
                "confidence": 0.0,
                "model_version": metadata.version,
                "features_used": [],
            }

        # Run inference
        # TODO: Replace with actual model.predict() call
        # feature_vector = np.array([features[f] for f in metadata.features])
        # raw_prediction = model.predict(feature_vector.reshape(1, -1))[0]
        raw_prediction = 0.0  # Placeholder
        confidence = 0.0  # Placeholder

        logger.info(
            "predictor.prediction_complete",
            symbol=symbol,
            model_name=model_name,
            model_version=metadata.version,
            prediction=raw_prediction,
            confidence=confidence,
            num_features=len(features_used),
        )

        return {
            "prediction": raw_prediction,
            "confidence": confidence,
            "model_version": metadata.version,
            "features_used": features_used,
        }
