"""
ModelRegistry — S3-backed ML model loader with in-memory cache and hot-reload.

Models are stored in S3 under the model_artifacts bucket:

    models/
      {name}/
        v1/
          model.joblib      # Serialised sklearn / hmmlearn / lightgbm model
          features.json     # ["rsi_14", "adx_14", ...]  feature names expected
          metadata.json     # {"trained_at": "...", "accuracy": 0.78}
        latest_version      # Plain text file containing the active version tag

Phase 6 design (ADR-014 §5.3):
  • Real S3 download with joblib deserialization.
  • Stub model bootstrap: when no real model exists in S3, a DummyClassifier
    or DummyRegressor is serialised locally and used so the full enrichment
    pipeline can run in paper mode without any trained models.
  • Hot-reload: background task polls DynamoDB ``models/{name}/latest_version``
    record every 60 seconds.  When the version changes the new model is loaded
    in the background and swapped atomically via ``_active_model`` pointer.

Thread safety:
  ``_active_model`` is a plain attribute replaced atomically under the GIL.
  asyncio single-threaded model + asyncio.to_thread isolation means no explicit
  lock is required.  No concurrent writes to ``_active_model`` can occur.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="ai_engine")


# ── Availability guards ───────────────────────────────────────────────────────

try:
    import joblib
    _JOBLIB_AVAILABLE = True
except ImportError:
    joblib = None  # type: ignore[assignment]
    _JOBLIB_AVAILABLE = False
    logger.warning("joblib not installed — stub models only. pip install joblib")

try:
    import boto3
    _BOTO3_AVAILABLE = True
except ImportError:
    boto3 = None  # type: ignore[assignment]
    _BOTO3_AVAILABLE = False
    logger.warning("boto3 not installed — S3 model loading unavailable.")

try:
    from sklearn.dummy import DummyClassifier, DummyRegressor
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False
    logger.warning("scikit-learn not installed — stub model bootstrap unavailable.")


# ── Hot-reload interval ───────────────────────────────────────────────────────
_HOT_RELOAD_INTERVAL_SECONDS: int = 60


@dataclass
class ModelMetadata:
    """Runtime metadata for a loaded model."""

    name:      str
    version:   str
    loaded_at: datetime
    s3_key:    str
    features:  list[str]
    is_stub:   bool = False  # True when running on a DummyClassifier/DummyRegressor


@dataclass
class _ModelEntry:
    """Internal cache entry — model object + metadata."""

    model:    Any
    metadata: ModelMetadata


class ModelRegistry:
    """
    Registry for ML models with S3 backend, in-memory cache, and background
    hot-reload.

    Usage:
        registry = ModelRegistry(
            s3_bucket    = settings.aws.s3_model_bucket,
            region       = settings.aws.region,
            dynamo_client = boto3.client("dynamodb", ...),
            version_table = settings.aws.dynamodb_table_strategy_config,
        )
        await registry.start()
        ...
        model, meta = registry.get("regime_classifier")
        await registry.stop()
    """

    def __init__(
        self,
        s3_bucket:     str,
        region:        str,
        dynamo_client: Optional[Any] = None,
        version_table: Optional[str] = None,
    ) -> None:
        self._s3_bucket     = s3_bucket
        self._region        = region
        self._dynamo        = dynamo_client
        self._version_table = version_table

        self._cache: dict[str, _ModelEntry] = {}
        self._reload_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._running: bool = False

        # boto3 S3 client — lazily created on first use
        self._s3_client: Optional[Any] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the registry and launch the background hot-reload task."""
        self._running = True
        if _BOTO3_AVAILABLE and boto3 is not None:
            self._s3_client = boto3.client("s3", region_name=self._region)
        self._reload_task = asyncio.create_task(
            self._hot_reload_loop(), name="model_registry.hot_reload"
        )
        logger.info("model_registry.started", s3_bucket=self._s3_bucket)

    async def stop(self) -> None:
        """Stop the hot-reload loop and release resources."""
        self._running = False
        if self._reload_task and not self._reload_task.done():
            self._reload_task.cancel()
            try:
                await self._reload_task
            except asyncio.CancelledError:
                pass
        self._cache.clear()
        logger.info("model_registry.stopped")

    # ── Public API ────────────────────────────────────────────────────────────

    async def load_model(self, name: str, version: str = "latest") -> None:
        """
        Load a model from S3 into cache (or bootstrap a stub if S3 unavailable).

        Args:
            name:    Model name e.g. ``"regime_classifier"``.
            version: Version tag or ``"latest"`` (resolves via DynamoDB pointer).
        """
        resolved_version = await self._resolve_version(name, version)
        entry = await self._download_and_cache(name, resolved_version)
        self._cache[name] = entry
        logger.info(
            "model_registry.loaded",
            model=name,
            version=resolved_version,
            is_stub=entry.metadata.is_stub,
        )

    def get(self, name: str) -> tuple[Optional[Any], Optional[ModelMetadata]]:
        """
        Return ``(model, metadata)`` for a cached model.

        Returns ``(None, None)`` if the model has not been loaded yet.
        """
        entry = self._cache.get(name)
        if entry is None:
            return None, None
        return entry.model, entry.metadata

    def get_model(self, name: str) -> Optional[Any]:
        """Return the cached model object, or None."""
        model, _ = self.get(name)
        return model

    def get_metadata(self, name: str) -> Optional[ModelMetadata]:
        """Return the cached model metadata, or None."""
        _, meta = self.get(name)
        return meta

    def is_loaded(self, name: str) -> bool:
        """True if the model is present in cache."""
        return name in self._cache

    def clear_cache(self) -> None:
        """Evict all cached models."""
        self._cache.clear()
        logger.info("model_registry.cache_cleared")

    def loaded_versions(self) -> dict[str, str]:
        """Return {model_name: version} for all loaded models."""
        return {
            name: entry.metadata.version
            for name, entry in self._cache.items()
        }

    # ── Internal: version resolution ──────────────────────────────────────────

    async def _resolve_version(self, name: str, version: str) -> str:
        """Resolve ``"latest"`` to the actual version string."""
        if version != "latest":
            return version

        # Prefer DynamoDB pointer (hot-reload-compatible)
        if self._dynamo and self._version_table:
            dynamo_version = await self._get_dynamo_version(name)
            if dynamo_version:
                return dynamo_version

        # Fallback: read S3 latest_version file
        s3_version = await self._get_s3_version(name)
        if s3_version:
            return s3_version

        # Last resort: use "v1"
        return "v1"

    async def _get_dynamo_version(self, name: str) -> Optional[str]:
        """Read the latest_version for a model from DynamoDB strategy-config table."""
        if not self._dynamo or not self._version_table:
            return None
        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._version_table,
                Key={
                    "PK": {"S": f"MODEL#{name}"},
                    "SK": {"S": "LATEST_VERSION"},
                },
            )
            item = response.get("Item", {})
            v = item.get("version", {}).get("S")
            return v if v else None
        except Exception:
            logger.warning("model_registry.dynamo_version_read_failed", model=name)
            return None

    async def _get_s3_version(self, name: str) -> Optional[str]:
        """Read the plain-text ``models/{name}/latest_version`` file from S3."""
        if not self._s3_client:
            return None
        try:
            key = f"models/{name}/latest_version"
            response = await asyncio.to_thread(
                self._s3_client.get_object,
                Bucket=self._s3_bucket,
                Key=key,
            )
            body = response["Body"].read().decode("utf-8").strip()
            return body if body else None
        except Exception:
            return None

    # ── Internal: model download ───────────────────────────────────────────────

    async def _download_and_cache(self, name: str, version: str) -> _ModelEntry:
        """Download model from S3 or bootstrap a stub if unavailable."""
        model = await self._download_model(name, version)
        features = await self._load_features(name, version)
        is_stub = False

        if model is None:
            model, is_stub = _make_stub(name), True
            if model is None:
                # sklearn unavailable — use a sentinel dict so callers can check
                model = {"stub": True, "type": "none"}
                is_stub = True
            logger.warning(
                "model_registry.stub_loaded",
                model=name,
                version=version,
                reason="S3 model unavailable — using stub model for paper mode",
            )

        return _ModelEntry(
            model=model,
            metadata=ModelMetadata(
                name=name,
                version=version,
                loaded_at=datetime.now(timezone.utc),
                s3_key=f"models/{name}/{version}/model.joblib",
                features=features,
                is_stub=is_stub,
            ),
        )

    async def _download_model(self, name: str, version: str) -> Optional[Any]:
        """Download and deserialise a joblib model from S3."""
        if not self._s3_client or not _JOBLIB_AVAILABLE:
            return None

        s3_key = f"models/{name}/{version}/model.joblib"
        try:
            response = await asyncio.to_thread(
                self._s3_client.get_object,
                Bucket=self._s3_bucket,
                Key=s3_key,
            )
            raw_bytes = response["Body"].read()
            model = await asyncio.to_thread(
                joblib.load, io.BytesIO(raw_bytes)
            )
            logger.info(
                "model_registry.s3_download_ok",
                model=name,
                version=version,
                bytes=len(raw_bytes),
            )
            return model
        except Exception as exc:
            logger.warning(
                "model_registry.s3_download_failed",
                model=name,
                version=version,
                s3_key=s3_key,
                error=str(exc),
            )
            return None

    async def _load_features(self, name: str, version: str) -> list[str]:
        """Load the feature list from S3 ``features.json``."""
        if not self._s3_client:
            return []
        s3_key = f"models/{name}/{version}/features.json"
        try:
            response = await asyncio.to_thread(
                self._s3_client.get_object,
                Bucket=self._s3_bucket,
                Key=s3_key,
            )
            return json.loads(response["Body"].read().decode("utf-8"))
        except Exception:
            return []

    # ── Internal: hot-reload loop ─────────────────────────────────────────────

    async def _hot_reload_loop(self) -> None:
        """
        Background task: check DynamoDB every 60 seconds for version changes.
        If the active version differs from the loaded version, reload atomically.
        """
        while self._running:
            await asyncio.sleep(_HOT_RELOAD_INTERVAL_SECONDS)
            if not self._running:
                break
            for name in list(self._cache.keys()):
                await self._check_and_reload(name)

    async def _check_and_reload(self, name: str) -> None:
        """Check DynamoDB version pointer; reload if it changed."""
        try:
            latest = await self._get_dynamo_version(name) or await self._get_s3_version(name)
            if not latest:
                return
            current_entry = self._cache.get(name)
            if current_entry and current_entry.metadata.version == latest:
                return
            logger.info(
                "model_registry.hot_reload",
                model=name,
                old_version=current_entry.metadata.version if current_entry else "none",
                new_version=latest,
            )
            new_entry = await self._download_and_cache(name, latest)
            self._cache[name] = new_entry  # atomic pointer swap
            logger.info(
                "model_registry.hot_reload_complete",
                model=name,
                version=latest,
                is_stub=new_entry.metadata.is_stub,
            )
        except Exception as exc:
            logger.error(
                "model_registry.hot_reload_error",
                model=name,
                error=str(exc),
            )


# ── Stub model factory ────────────────────────────────────────────────────────

def _make_stub(name: str) -> Optional[Any]:
    """
    Create a fitted stub model for paper mode bootstrap.

    regime_classifier  → DummyClassifier(strategy="constant", constant="unknown")
                         pre-fitted on a tiny synthetic dataset.
    signal_quality_scorer → DummyClassifier(strategy="constant", constant=0)
                         (quality_score will be overridden to 0.5 by scorer wrapper)
    Everything else    → DummyRegressor(strategy="constant", constant=0.5)
    """
    if not _SKLEARN_AVAILABLE:
        return None

    import numpy as np

    X_dummy = np.zeros((4, 1))  # minimal fit data

    if "regime" in name:
        # DummyClassifier requires constant to be in training labels.
        # Use "ranging" as the stub constant; the RegimeClassifier wrapper
        # detects is_stub=True and overrides the output to "unknown" anyway.
        labels = ["trending", "ranging", "volatile", "crash"]
        y_dummy = labels[:4]
        stub = DummyClassifier(strategy="constant", constant="ranging")
        stub.fit(X_dummy, y_dummy)
        return stub

    if "quality" in name or "scorer" in name:
        y_dummy = [0, 1, 0, 1]
        stub = DummyClassifier(strategy="constant", constant=0)
        stub.fit(X_dummy, y_dummy)
        return stub

    stub = DummyRegressor(strategy="constant", constant=0.5)
    stub.fit(X_dummy, [0.5, 0.5, 0.5, 0.5])
    return stub
