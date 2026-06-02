"""
QuantEmbrace Configuration Settings.

Centralized configuration using Pydantic BaseSettings. All settings are loaded
from environment variables or .env files, suitable for AWS ECS Fargate deployments
where secrets are injected via environment variables or AWS Secrets Manager.
"""

from __future__ import annotations

import json
import logging
import os
from enum import Enum
from typing import Annotated, Optional

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

_log = logging.getLogger(__name__)


class Environment(str, Enum):
    """Deployment environment."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class ZerodhaConfig(BaseSettings):
    """Zerodha Kite Connect broker configuration."""

    model_config = {"env_prefix": "ZERODHA_"}

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
    base_url: str = Field(
        default="https://api.kite.trade",
        description="Kite Connect API base URL",
    )


class AlpacaConfig(BaseSettings):
    """Alpaca broker configuration for US equities."""

    model_config = {"env_prefix": "ALPACA_"}

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
    dynamodb_table_sessions: str = Field(
        default="quantembrace-sessions",
        description="DynamoDB table for broker session tokens (Zerodha access token + TTL)",
    )
    dynamodb_table_risk_state: str = Field(
        default="quantembrace-risk-state",
        description=(
            "DynamoDB table for risk engine state: margin snapshots, NAV, kill switch flags. "
            "Written by execution_engine (margin refresh, NAV snapshots) and risk_engine. "
            "Low-latency reads replace broker API calls in the risk validation hot path."
        ),
    )
    dynamodb_table_fills: str = Field(
        default="quantembrace-fills",
        description=(
            "DynamoDB table for fill idempotency tracking (TTL 24h). "
            "PK=FILL#{fill_id}, SK=META. attribute_not_exists(PK) gate prevents "
            "double-counting when Zerodha polling and postback both detect the same fill, "
            "and guards against Kafka re-delivery of orders.events fills."
        ),
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
    dynamodb_table_features: str = Field(
        default="quantembrace-features",
        description=(
            "DynamoDB table for the Phase 5 feature store (online layer). "
            "Written by data_ingestion (FeatureWriter) — dual rows per candle: "
            "SK=LATEST (online reads, TTL 24h) and SK=CANDLE#{ts} (intraday "
            "archive source, TTL 7d). Read by strategy_engine, risk_engine, "
            "and ai_engine via shared/features/feature_reader.py."
        ),
    )
    dynamodb_table_regime_log: str = Field(
        default="quantembrace-regime-log",
        description=(
            "Phase 6 — DynamoDB table for regime classification audit log. "
            "Written by ai_engine SignalEnricher per enriched signal. "
            "PK=REGIME#{market}#{symbol}, SK=SESSION#{date}T{signal_time}. "
            "TTL 30 days. Used by StrategySelector agent and model evaluation."
        ),
    )
    dynamodb_table_strategy_config: str = Field(
        default="quantembrace-strategy-config",
        description=(
            "Phase 3+ — DynamoDB table for strategy runtime configuration. "
            "Stores circuit-breaker state, paper_trade flag, quality_filter_threshold "
            "(Phase 6), enabled flag. Hot-reloaded by strategy_engine every 60s."
        ),
    )
    dynamodb_table_strategy_recommendations: str = Field(
        default="quantembrace-strategy-recommendations",
        description=(
            "Phase 6 — DynamoDB table for StrategySelector agent recommendations. "
            "PK=RECOMMENDATION, SK=SESSION#{date}-NSE. TTL 30 days. "
            "Read by scripts/strategy/config.py status."
        ),
    )

    @model_validator(mode="after")
    def _apply_table_prefix_defaults(self) -> "AWSConfig":
        """
        Derive default table names from the deployment prefix when explicit
        table names are not supplied.

        EC2/userdata and CI historically set ``DYNAMODB_TABLE_PREFIX`` while
        this settings class only listened to ``AWS_DYNAMODB_TABLE_PREFIX``.
        Supporting both keeps service startup config minimal and prevents
        accidental writes to the unqualified ``quantembrace-*`` defaults.
        """
        prefix = (
            os.environ.get("AWS_DYNAMODB_TABLE_PREFIX")
            or os.environ.get("DYNAMODB_TABLE_PREFIX")
            or self.dynamodb_table_prefix
        )
        self.dynamodb_table_prefix = prefix

        table_defaults = {
            "dynamodb_table_prices":                  "quantembrace-latest-prices",
            "dynamodb_table_orders":                  "quantembrace-orders",
            "dynamodb_table_positions":               "quantembrace-positions",
            "dynamodb_table_sessions":                "quantembrace-sessions",
            "dynamodb_table_risk_state":              "quantembrace-risk-state",
            "dynamodb_table_fills":                   "quantembrace-fills",
            "dynamodb_table_features":                "quantembrace-features",
            "dynamodb_table_regime_log":              "quantembrace-regime-log",
            "dynamodb_table_strategy_config":         "quantembrace-strategy-config",
            "dynamodb_table_strategy_recommendations": "quantembrace-strategy-recommendations",
        }
        table_suffixes = {
            "dynamodb_table_prices":                  "latest-prices",
            "dynamodb_table_orders":                  "orders",
            "dynamodb_table_positions":               "positions",
            "dynamodb_table_sessions":                "sessions",
            "dynamodb_table_risk_state":              "risk-state",
            "dynamodb_table_fills":                   "fills",
            "dynamodb_table_features":                "features",
            "dynamodb_table_regime_log":              "regime-log",
            "dynamodb_table_strategy_config":         "strategy-config",
            "dynamodb_table_strategy_recommendations": "strategy-recommendations",
        }

        for field_name, default_value in table_defaults.items():
            env_name = f"AWS_{field_name.upper()}"
            if os.environ.get(env_name):
                continue
            if getattr(self, field_name) == default_value:
                setattr(self, field_name, f"{prefix}-{table_suffixes[field_name]}")

        return self


class RiskConfig(BaseSettings):
    """Risk management configuration."""

    model_config = {"env_prefix": "RISK_"}

    profile: str = Field(
        default="tiny-live",
        description="Risk profile: paper, shadow, tiny-live, or medium-live",
    )
    max_position_size_pct: float = Field(
        default=5.0,
        description="Max position size as percentage of portfolio",
    )
    max_total_exposure_pct: float = Field(
        default=20.0,
        description="Max total exposure as percentage of portfolio",
    )
    max_daily_loss_pct: float = Field(
        default=0.5,
        description="Max daily loss as percentage of portfolio to trigger kill switch",
    )
    max_single_order_value: float = Field(
        default=5_000.0,
        description="Max value for a single order in base currency",
    )
    max_open_orders: int = Field(
        default=1,
        description="Maximum number of concurrent open orders",
    )
    max_position_per_symbol: int = Field(
        default=100,
        description="Maximum shares/contracts per symbol for tiny-live safety",
    )
    max_concurrent_positions: int = Field(
        default=1,
        description="Maximum concurrent open positions across the portfolio",
    )
    max_sector_exposure_pct: float = Field(
        default=20.0,
        description="Max exposure to a single sector as percentage of portfolio",
    )
    allow_leverage: bool = Field(
        default=False,
        description="If false, profile-level exposure remains at or below cash NAV",
    )
    max_spread_bps: float = Field(
        default=50.0,
        description="Maximum allowed live bid-ask spread in basis points",
    )
    max_order_adv_pct: float = Field(
        default=1.0,
        description="Maximum order size as a percentage of 20-day ADV",
    )
    max_signal_age_seconds: float = Field(
        default=5.0,
        description=(
            "Maximum age in seconds for a signal to be accepted by the risk engine. "
            "Signals older than this threshold are rejected as stale — they were "
            "generated from market data that no longer reflects current conditions. "
            "Set env RISK_MAX_SIGNAL_AGE_SECONDS to override. "
            "5s suits intraday equity strategies; increase for slower strategies."
        ),
    )
    kill_switch_poll_interval_seconds: float = Field(
        default=1.0,
        description=(
            "How often (seconds) the strategy engine polls DynamoDB for kill switch "
            "state in its processing loop. This is a safety net independent of SNS — "
            "ensures the strategy engine halts within 1s of a kill switch event even "
            "if the SNS message is delayed or dropped. Set env "
            "RISK_KILL_SWITCH_POLL_INTERVAL_SECONDS to override."
        ),
    )
    data_feed_stale_seconds: float = Field(
        default=300.0,
        description=(
            "Seconds of silence on any market's signal feed before the data-staleness "
            "auto-trigger fires. Candle strategies only emit signals when conditions "
            "are met (e.g. VWAP crossing bands), so gaps of 2–3 minutes are normal "
            "during quiet markets. Set env RISK_DATA_FEED_STALE_SECONDS to override. "
            "Use 60 for tick-based strategies; 300 for candle strategies."
        ),
    )
    kafka_lag_halt_threshold_messages: int = Field(
        default=250,
        ge=0,
        description=(
            "Activate kill switch when any trading consumer group stays above "
            "this lag. Set to 0 to disable the lag-triggered halt."
        ),
    )
    kafka_lag_halt_consecutive_checks: int = Field(
        default=3,
        ge=1,
        description="Consecutive lag breaches required before activating kill switch.",
    )
    kafka_lag_check_interval_seconds: float = Field(
        default=1.0,
        ge=0.1,
        description="How often services check Kafka consumer lag.",
    )
    cooldown_after_loss_seconds: int = Field(
        default=300,
        description="Cooldown period in seconds after hitting loss limit",
    )


class ZerodhaRateLimitConfig(BaseSettings):
    """
    Zerodha API rate limit and polling interval configuration.

    All values are runtime-tunable via environment variables — no redeploy
    needed to adjust polling aggressiveness.

    ADR-012: endpoint-aware token buckets matching Kite Connect documented
    limits: quote 1 req/sec, historical 3 req/sec, order placement 10 req/sec,
    other endpoints 10 req/sec, plus order-count caps.
    ``bulk_fill_poll_*`` controls the adaptive interval of ``BulkOrderPoller``.
    """

    model_config = {"env_prefix": "ZERODHA_RATELIMIT_"}

    capacity_per_second: int = Field(
        default=10,
        description=(
            "Zerodha order API hard limit (tokens/sec). "
            "Do not exceed 10 — Zerodha will return 429 and activate circuit breaker."
        ),
    )
    burst_capacity: int = Field(
        default=10,
        description=(
            "General endpoint token bucket burst ceiling. Keep at or below 10 "
            "to avoid violating Kite's per-endpoint request limits."
        ),
    )
    quote_capacity_per_second: int = Field(
        default=1,
        description="kite.quote() hard limit in requests/sec.",
    )
    historical_capacity_per_second: int = Field(
        default=3,
        description="kite.historical_data() hard limit in requests/sec.",
    )
    order_capacity_per_second: int = Field(
        default=10,
        description="kite.place_order() hard limit in requests/sec.",
    )
    other_capacity_per_second: int = Field(
        default=10,
        description="All other Kite REST endpoints hard limit in requests/sec.",
    )
    critical_reserved_tokens: int = Field(
        default=2,
        description=(
            "Reserved endpoint tokens for CRITICAL cancel/square-off traffic. "
            "Non-critical order/other calls cannot consume below this floor."
        ),
    )
    max_orders_per_second: int = Field(
        default=10,
        description="Kite order placement cap per second.",
    )
    max_orders_per_minute: int = Field(
        default=400,
        description="Kite order placement cap per minute.",
    )
    max_orders_per_day: int = Field(
        default=5000,
        description="Kite order placement cap per trading day.",
    )
    fill_poll_min_interval_ms: int = Field(
        default=300,
        description=(
            "Fastest BulkOrderPoller interval (ms). Used during PRE_CLOSE phase "
            "and when 6+ orders are open. "
            "Rate cost at this interval: 3.3 req/sec."
        ),
    )
    fill_poll_max_interval_ms: int = Field(
        default=2000,
        description=(
            "Slowest BulkOrderPoller interval (ms). Used when no orders are open. "
            "Rate cost: 0.5 req/sec."
        ),
    )
    quote_poll_interval_ms: int = Field(
        default=2000,
        description=(
            "LiveQuotePoller batch quote refresh interval (ms). "
            "A single kite.quote([50 instruments]) call per cycle = 0.5 req/sec."
        ),
    )
    position_poll_interval_ms: int = Field(
        default=2000,
        description=(
            "PositionMonitor kite.positions() refresh interval (ms). "
            "Reduced to 1000ms during MARKET_OPEN and PRE_CLOSE phases."
        ),
    )
    margin_poll_interval_ms: int = Field(
        default=1000,
        description="Margin state refresh interval (ms). Was 5000ms in Phase 1.",
    )
    candle_stream_enabled: bool = Field(
        default=True,
        description=(
            "Enable IntradayCandleStream. Uses SEPARATE 3 req/sec historical "
            "data budget — does NOT consume order API tokens."
        ),
    )
    live_quote_enabled: bool = Field(
        default=True,
        description=(
            "Enable LiveQuotePoller. Disabled automatically during MARKET_OPEN "
            "and PRE_CLOSE phases (budget reserved for orders/fills)."
        ),
    )
    position_monitor_enabled: bool = Field(
        default=True,
        description=(
            "Enable PositionMonitor. Provides ground-truth position state from "
            "kite.positions(), catching fills missed by BulkOrderPoller."
        ),
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
    ack_unknown_recheck_delay_seconds: float = Field(
        default=1.0,
        ge=0.0,
        description=(
            "Delay before scanning broker order history after a placement timeout. "
            "During this window the order is persisted as ACK_UNKNOWN and must not "
            "be blindly placed again."
        ),
    )
    broker_degraded_failure_threshold: int = Field(
        default=1,
        ge=1,
        description=(
            "Transient broker placement failures before new entries are blocked. "
            "Risk-reducing orders such as protective stops and square-offs remain allowed."
        ),
    )
    broker_degraded_cooldown_seconds: float = Field(
        default=30.0,
        ge=1.0,
        description="How long to block new entries after broker degradation is detected.",
    )
    zerodha_protective_order_mode: str = Field(
        default="application",
        description=(
            "Protective-stop mode for Zerodha entries: application, "
            "broker-native-preferred, or broker-native-required."
        ),
    )
    zerodha_native_protection_live_tested: bool = Field(
        default=False,
        description=(
            "Operator attestation that Zerodha broker-native protection has been "
            "live-tested for the account/product before it is used."
        ),
    )
    circuit_breaker_failure_threshold: int = Field(
        default=5,
        description="Consecutive failures before opening the circuit breaker",
    )
    circuit_breaker_reset_timeout: float = Field(
        default=60.0,
        description="Seconds before the circuit breaker moves from OPEN to HALF-OPEN",
    )
    paper_slippage_bps: float = Field(
        default=5.0,
        ge=0.0,
        description="Paper-trading adverse slippage in basis points.",
    )
    paper_spread_bps: float = Field(
        default=10.0,
        ge=0.0,
        description="Paper-trading simulated bid/ask spread in basis points.",
    )
    paper_latency_ms: int = Field(
        default=250,
        ge=0,
        description="Paper-trading broker acknowledgement/fill latency in milliseconds.",
    )
    paper_partial_fill_probability: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Deterministic probability that a paper order partially fills.",
    )
    paper_partial_fill_min_pct: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Minimum fill percentage when paper partial-fill simulation triggers.",
    )
    paper_reject_probability: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Deterministic probability that a paper order is rejected.",
    )
    paper_market_open_gap_bps: float = Field(
        default=0.0,
        ge=0.0,
        description="Additional adverse gap applied to paper fills during gap simulation.",
    )
    paper_circuit_lock_probability: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Deterministic probability that paper execution rejects due to circuit lock.",
    )
    paper_random_seed: str = Field(
        default="quantembrace-paper-v1",
        description="Stable seed namespace for deterministic paper execution outcomes.",
    )
    paper_trading: bool = Field(
        default=True,
        description="When True the service runs in paper mode (no live broker calls).",
    )
    backtest_mode: bool = Field(
        default=False,
        description="When True the service runs in backtest mode (reconciliation skipped).",
    )

    # ── Startup position reconciliation ─────────────────────────────────────
    reconciliation_enabled: bool = Field(
        default=True,
        description="Master switch — set False to skip position reconciliation entirely.",
    )
    reconciliation_run_on_startup: bool = Field(
        default=True,
        description="Run PositionReconciliationService once during ExecutionService.start().",
    )
    reconciliation_strict_startup: bool = Field(
        default=False,
        description=(
            "When True, any reconciliation failure raises RuntimeError and "
            "prevents the service from starting. "
            "When False (default), failures emit ERROR logs but allow startup."
        ),
    )
    reconciliation_paper_auto_repair: bool = Field(
        default=True,
        description="Paper mode: allow PositionReconciliationService to auto-repair safe mismatches.",
    )
    reconciliation_live_auto_repair: bool = Field(
        default=False,
        description="Live mode: never auto-repair (CRITICAL alert only). Must remain False.",
    )


class StrategyConfig(BaseSettings):
    """Strategy runtime configuration shared by strategy and execution services."""

    model_config = {"env_prefix": "STRATEGY_"}

    watchlist_nse: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "NSE symbol watchlist used by quote polling and strategy bootstrap. "
            "Accepts JSON list or comma-separated env value."
        ),
    )
    watchlist_us: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "US symbol watchlist used by strategy bootstrap. "
            "Accepts JSON list or comma-separated env value."
        ),
    )
    candle_intervals: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["minute", "5minute", "15minute"],
        description=(
            "Confirmed Zerodha candle intervals produced by data_ingestion. "
            "Must cover every registered candle strategy interval."
        ),
    )

    @field_validator("watchlist_nse", "watchlist_us", "candle_intervals", mode="before")
    @classmethod
    def _parse_csv_or_json_list(cls, value: object) -> object:
        """Accept comma-separated values in addition to JSON arrays."""
        if isinstance(value, str):
            raw = value.strip()
            if raw.startswith("["):
                return [str(item).strip() for item in json.loads(raw) if str(item).strip()]
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("watchlist_nse", "watchlist_us")
    @classmethod
    def _uppercase_watchlist(cls, value: list[str]) -> list[str]:
        return [item.upper() for item in value]

    @field_validator("candle_intervals")
    @classmethod
    def _normalise_candle_intervals(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        intervals: list[str] = []
        aliases = {
            "1m": "minute",
            "1min": "minute",
            "5m": "5minute",
            "5min": "5minute",
            "15m": "15minute",
            "15min": "15minute",
        }
        for raw in value:
            interval = aliases.get(str(raw).strip().lower(), str(raw).strip())
            if interval and interval not in seen:
                seen.add(interval)
                intervals.append(interval)
        return intervals or ["minute"]


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
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    zerodha_rate_limit: ZerodhaRateLimitConfig = Field(
        default_factory=ZerodhaRateLimitConfig,
        description=(
            "Zerodha token bucket rate limiter + polling interval config. "
            "ADR-012. All fields tunable via ZERODHA_RATELIMIT_* env vars."
        ),
    )

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
