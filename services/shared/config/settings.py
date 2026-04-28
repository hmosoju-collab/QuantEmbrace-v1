"""
QuantEmbrace Configuration Settings.

Centralized configuration using Pydantic BaseSettings. All settings are loaded
from environment variables or .env files, suitable for AWS ECS Fargate deployments
where secrets are injected via environment variables or AWS Secrets Manager.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Optional

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings

_log = logging.getLogger(__name__)


class Environment(str, Enum):
    """Deployment environment."""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class ZerodhaConfig(BaseSettings):
    """Zerodha Kite Connect broker configuration."""

    model_config = {"env_prefix": "ZERODHA_"}

    # TODO: Obtain API key and secret from https://developers.kite.trade/
    api_key: SecretStr = Field(..., description="Zerodha Kite Connect API key")
    api_secret: SecretStr = Field(..., description="Zerodha Kite Connect API secret")
    access_token: SecretStr = Field(
        default=SecretStr(""),
        description="Zerodha access token (refreshed daily via login flow)",
    )
    request_token: str = Field(
        default="",
        description="Request token from Kite login redirect",
    )
    # TODO: Implement token refresh mechanism — Zerodha tokens expire daily
    base_url: str = Field(
        default="https://api.kite.trade",
        description="Kite Connect API base URL",
    )


class AlpacaConfig(BaseSettings):
    """Alpaca broker configuration for US equities."""

    model_config = {"env_prefix": "ALPACA_"}

    # TODO: Obtain API keys from https://app.alpaca.markets/
    api_key: SecretStr = Field(..., description="Alpaca API key ID")
    api_secret: SecretStr = Field(..., description="Alpaca API secret key")
    base_url: str = Field(
        default="https://paper-api.alpaca.markets",
        description="Alpaca API base URL (paper or live)",
    )
    data_url: str = Field(
        default="https://data.alpaca.markets",
        description="Alpaca market data URL",
    )
    use_paper: bool = Field(
        default=True,
        description="Use paper trading (set False for live trading)",
    )


class AWSConfig(BaseSettings):
    """AWS infrastructure configuration."""

    model_config = {"env_prefix": "AWS_"}

    region: str = Field(default="ap-south-1", description="AWS region")
    s3_bucket: str = Field(
        default="quantembrace-market-data",
        description="S3 bucket for historical market data",
    )
    dynamodb_table_prices: str = Field(
        default="quantembrace-latest-prices",
        description="DynamoDB table for latest price snapshots",
    )
    dynamodb_table_orders: str = Field(
        default="quantembrace-orders",
        description="DynamoDB table for order tracking",
    )
    dynamodb_table_positions: str = Field(
        default="quantembrace-positions",
        description="DynamoDB table for position tracking",
    )
    sqs_market_data_queue: str = Field(
        default="quantembrace-market-data.fifo",
        description="SQS FIFO queue: DataIngestionService → StrategyEngine (tick feed)",
    )
    sqs_signals_queue: str = Field(
        default="quantembrace-signals.fifo",
        description="SQS FIFO queue: StrategyEngine → RiskEngine (trading signals)",
    )
    sqs_orders_queue: str = Field(
        default="quantembrace-orders.fifo",
        description="SQS FIFO queue: RiskEngine → ExecutionEngine (approved signals)",
    )
    dynamodb_table_sessions: str = Field(
        default="quantembrace-sessions",
        description="DynamoDB table for broker session tokens (Zerodha access token + TTL)",
    )
    sns_alerts_topic: str = Field(
        default="",
        description="SNS topic ARN for alerts and kill switch notifications",
    )
    sns_kill_switch_topic_arn: str = Field(
        default="",
        description="SNS topic ARN for kill switch activation notifications",
    )
    dynamodb_table_prefix: str = Field(
        default="quantembrace",
        description="Prefix for all DynamoDB table names",
    )
    s3_model_bucket: str = Field(
        default="quantembrace-ml-models",
        description="S3 bucket for ML model artifacts (AI engine)",
    )


class RiskConfig(BaseSettings):
    """Risk management configuration."""

    model_config = {"env_prefix": "RISK_"}

    max_position_size_pct: float = Field(
        default=5.0,
        description="Max position size as percentage of portfolio",
    )
    max_total_exposure_pct: float = Field(
        default=80.0,
        description="Max total exposure as percentage of portfolio",
    )
    max_daily_loss_pct: float = Field(
        default=2.0,
        description="Max daily loss as percentage of portfolio to trigger kill switch",
    )
    max_single_order_value: float = Field(
        default=500_000.0,
        description="Max value for a single order in base currency",
    )
    max_open_orders: int = Field(
        default=50,
        description="Maximum number of concurrent open orders",
    )
    cooldown_after_loss_seconds: int = Field(
        default=300,
        description="Cooldown period in seconds after hitting loss limit",
    )


class ExecutionConfig(BaseSettings):
    """Execution engine retry and circuit-breaker configuration."""

    model_config = {"env_prefix": "EXECUTION_"}

    max_retries: int = Field(
        default=3,
        description="Maximum broker API retry attempts per order",
    )
    retry_base_delay: float = Field(
        default=1.0,
        description="Initial retry back-off delay in seconds",
    )
    retry_max_delay: float = Field(
        default=30.0,
        description="Maximum retry back-off delay in seconds",
    )
    circuit_breaker_failure_threshold: int = Field(
        default=5,
        description="Consecutive failures before opening the circuit breaker",
    )
    circuit_breaker_reset_timeout: float = Field(
        default=60.0,
        description="Seconds before the circuit breaker moves from OPEN to HALF-OPEN",
    )


class AppSettings(BaseSettings):
    """
    Master application settings aggregating all configuration sections.

    Loads from environment variables. On ECS Fargate, inject via task definition
    environment or AWS Secrets Manager integration.
    """

    model_config = {
        "env_prefix": "QE_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    app_name: str = Field(default="QuantEmbrace")
    environment: Environment = Field(default=Environment.DEVELOPMENT)
    log_level: str = Field(default="INFO")
    service_name: str = Field(
        default="unknown",
        description="Name of the current service (data_ingestion, strategy_engine, etc.)",
    )

    # Broker sub-configurations — Optional so non-broker services start without
    # broker secrets.  Populated by the model_validator below when the required
    # env vars (ZERODHA_API_KEY / ALPACA_API_KEY) are present.
    zerodha: Optional[ZerodhaConfig] = Field(
        default=None,
        description="Zerodha config — auto-populated when ZERODHA_API_KEY is set",
    )
    alpaca: Optional[AlpacaConfig] = Field(
        default=None,
        description="Alpaca config — auto-populated when ALPACA_API_KEY is set",
    )

    # Always-present sub-configurations
    aws: AWSConfig = Field(default_factory=AWSConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)

    # Portfolio
    portfolio_value: float = Field(
        default=1_000_000.0,
        description="Total portfolio value in base currency (INR for NSE, USD for US)",
    )

    # Health check
    health_check_port: int = Field(
        default=8080,
        description="Port for ECS health check endpoint",
    )

    @model_validator(mode="after")
    def _lazy_broker_configs(self) -> "AppSettings":
        """
        Construct broker configs only when their secrets are present in the
        environment.  This prevents services that don't need broker access
        (risk_engine, data_ingestion processing path) from failing startup
        with a missing-secret validation error.
        """
        if self.zerodha is None and os.environ.get("ZERODHA_API_KEY"):
            try:
                self.zerodha = ZerodhaConfig()
            except ValidationError as exc:
                # ZERODHA_API_KEY is set but one or more other required secrets
                # (e.g. ZERODHA_API_SECRET) are missing.  Log the specific
                # field errors so the operator knows exactly what to fix
                # instead of seeing a generic "zerodha=None" at call time.
                _log.warning(
                    "ZERODHA_API_KEY is present but Zerodha config is incomplete "
                    "— broker will be unavailable.  Missing fields: %s",
                    [e["loc"] for e in exc.errors()],
                )

        if self.alpaca is None and os.environ.get("ALPACA_API_KEY"):
            try:
                self.alpaca = AlpacaConfig()
            except ValidationError as exc:
                _log.warning(
                    "ALPACA_API_KEY is present but Alpaca config is incomplete "
                    "— broker will be unavailable.  Missing fields: %s",
                    [e["loc"] for e in exc.errors()],
                )

        return self


# Singleton settings instance — import this across services
_settings: Optional[AppSettings] = None


def get_settings() -> AppSettings:
    """
    Return the singleton AppSettings instance.

    Lazily initializes settings on first call. Thread-safe in CPython due to GIL.
    """
    global _settings
    if _settings is None:
        _settings = AppSettings()
    return _settings
