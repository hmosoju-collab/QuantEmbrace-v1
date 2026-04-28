"""AI Engine Service.

Lightweight HTTP service for model inference. Hosts trained ML models and serves
predictions via internal API. Model training happens offline (SageMaker or local).

This service does NOT:
    - Make trading decisions (that's strategy_engine)
    - Validate risk (that's risk_engine)
    - Place orders (that's execution_engine)
"""

import asyncio
from typing import Optional

import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.ai_engine.features.feature_pipeline import FeaturePipeline
from services.ai_engine.models.model_registry import ModelRegistry
from services.ai_engine.inference.predictor import Predictor
from services.shared.config.settings import AppSettings, get_settings
from services.shared.logging.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(title="QuantEmbrace AI Engine", version="1.0.0")


class PredictionRequest(BaseModel):
    """Request body for prediction endpoint."""

    symbol: str = Field(..., description="Trading symbol (e.g., RELIANCE, AAPL)")
    model_name: str = Field(default="default", description="Model to use for prediction")
    features: Optional[dict] = Field(
        default=None, description="Pre-computed features. If None, pipeline computes them."
    )


class PredictionResponse(BaseModel):
    """Response body from prediction endpoint."""

    symbol: str
    prediction: float = Field(..., description="Model prediction value")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Prediction confidence score")
    model_name: str
    model_version: str
    features_used: list[str]


class AIEngineService:
    """Main AI engine service.

    Manages model loading, feature computation, and prediction serving.
    Designed to be lightweight — inference only, no training in production.
    """

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._feature_pipeline: Optional[FeaturePipeline] = None
        self._model_registry: Optional[ModelRegistry] = None
        self._predictor: Optional[Predictor] = None

    async def start(self) -> None:
        """Initialize the AI engine components."""
        logger.info("ai_engine.starting")

        self._feature_pipeline = FeaturePipeline(
            s3_bucket=self._settings.aws.s3_bucket,
            region=self._settings.aws.region,
        )

        self._model_registry = ModelRegistry(
            s3_bucket=self._settings.aws.s3_model_bucket,
            region=self._settings.aws.region,
        )

        # Pre-load default model into memory
        await self._model_registry.load_model("default")

        self._predictor = Predictor(
            feature_pipeline=self._feature_pipeline,
            model_registry=self._model_registry,
        )

        logger.info("ai_engine.started")

    async def stop(self) -> None:
        """Gracefully shut down the AI engine."""
        logger.info("ai_engine.stopping")
        if self._model_registry:
            self._model_registry.clear_cache()
        logger.info("ai_engine.stopped")

    async def predict(self, request: PredictionRequest) -> PredictionResponse:
        """Generate a prediction for the given symbol.

        Args:
            request: Prediction request with symbol and optional features.

        Returns:
            PredictionResponse with prediction value and confidence.
        """
        if not self._predictor:
            raise RuntimeError("AI engine not initialized. Call start() first.")

        result = await self._predictor.predict(
            symbol=request.symbol,
            model_name=request.model_name,
            precomputed_features=request.features,
        )

        return PredictionResponse(
            symbol=request.symbol,
            prediction=result["prediction"],
            confidence=result["confidence"],
            model_name=request.model_name,
            model_version=result["model_version"],
            features_used=result["features_used"],
        )


# --- FastAPI endpoints ---

_service: Optional[AIEngineService] = None


@app.on_event("startup")
async def startup_event() -> None:
    """Initialize AI engine on FastAPI startup."""
    global _service
    settings = get_settings()
    _service = AIEngineService(settings)
    await _service.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Cleanup on FastAPI shutdown."""
    if _service:
        await _service.stop()


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint for ECS health probes."""
    return {"status": "healthy", "service": "ai_engine"}


@app.post("/predict", response_model=PredictionResponse)
async def predict(request: PredictionRequest) -> PredictionResponse:
    """Generate a model prediction for a symbol."""
    if not _service:
        raise HTTPException(status_code=503, detail="AI engine not ready")
    return await _service.predict(request)
