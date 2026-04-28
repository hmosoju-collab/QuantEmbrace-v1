"""Model Registry.

Manages loading, versioning, and caching of ML models.
Models are stored in S3 with versioned keys and loaded into memory for inference.
"""

import pickle
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from services.shared.logging.logger import get_logger

logger = get_logger(__name__)


class ModelMetadata:
    """Metadata for a loaded model."""

    def __init__(
        self,
        name: str,
        version: str,
        loaded_at: datetime,
        s3_key: str,
        features: list[str],
    ) -> None:
        self.name = name
        self.version = version
        self.loaded_at = loaded_at
        self.s3_key = s3_key
        self.features = features


class ModelRegistry:
    """Registry for ML models with S3 backend and in-memory caching.

    Models are stored in S3 as serialized pickle/joblib files with
    versioned keys: models/{name}/{version}/model.pkl

    On load, models are cached in memory for low-latency inference.
    """

    def __init__(self, s3_bucket: str, region: str) -> None:
        self._s3_bucket = s3_bucket
        self._region = region
        self._cache: dict[str, dict[str, Any]] = {}
        self._metadata: dict[str, ModelMetadata] = {}
        # TODO: Initialize boto3 S3 client
        # self._s3_client = boto3.client("s3", region_name=region)

    async def load_model(self, name: str, version: str = "latest") -> None:
        """Load a model from S3 into the in-memory cache.

        Args:
            name: Model name (e.g., 'default', 'regime_detector', 'vol_forecast').
            version: Model version string or 'latest' for most recent.
        """
        logger.info(
            "model_registry.loading",
            model_name=name,
            version=version,
        )

        # Resolve 'latest' to the actual latest version
        if version == "latest":
            version = await self._get_latest_version(name)

        s3_key = f"models/{name}/{version}/model.pkl"

        # TODO: Download model from S3 and deserialize
        # response = self._s3_client.get_object(Bucket=self._s3_bucket, Key=s3_key)
        # model = pickle.loads(response["Body"].read())

        model = None  # Placeholder

        self._cache[name] = {
            "model": model,
            "version": version,
        }

        self._metadata[name] = ModelMetadata(
            name=name,
            version=version,
            loaded_at=datetime.now(timezone.utc),
            s3_key=s3_key,
            features=await self._get_model_features(name, version),
        )

        logger.info(
            "model_registry.loaded",
            model_name=name,
            version=version,
            s3_key=s3_key,
        )

    def get_model(self, name: str) -> Optional[Any]:
        """Retrieve a cached model by name.

        Args:
            name: Model name.

        Returns:
            The model object, or None if not loaded.
        """
        entry = self._cache.get(name)
        return entry["model"] if entry else None

    def get_metadata(self, name: str) -> Optional[ModelMetadata]:
        """Get metadata for a loaded model.

        Args:
            name: Model name.

        Returns:
            ModelMetadata or None if not loaded.
        """
        return self._metadata.get(name)

    def is_loaded(self, name: str) -> bool:
        """Check if a model is loaded in cache."""
        return name in self._cache

    def clear_cache(self) -> None:
        """Clear all cached models from memory."""
        self._cache.clear()
        self._metadata.clear()
        logger.info("model_registry.cache_cleared")

    async def _get_latest_version(self, name: str) -> str:
        """Determine the latest version of a model in S3.

        Args:
            name: Model name.

        Returns:
            Version string (e.g., '2026-04-23-v1').
        """
        # TODO: List S3 objects under models/{name}/ and find latest
        # prefix = f"models/{name}/"
        # response = self._s3_client.list_objects_v2(
        #     Bucket=self._s3_bucket, Prefix=prefix, Delimiter="/"
        # )
        # versions = [p["Prefix"].split("/")[-2] for p in response.get("CommonPrefixes", [])]
        # return sorted(versions)[-1]
        return "v1"

    async def _get_model_features(self, name: str, version: str) -> list[str]:
        """Load the feature list expected by a model.

        Args:
            name: Model name.
            version: Model version.

        Returns:
            List of feature names the model expects as input.
        """
        # TODO: Load features.json from S3: models/{name}/{version}/features.json
        return []
