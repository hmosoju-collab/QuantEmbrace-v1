"""
StrategyConfigLoader — hot-reload strategy config from DynamoDB every 60s.

Reads per-strategy configuration from the ``{prefix}-strategy-config`` table
(schema defined in ADR-013 §9.1) and applies updates to all registered
StrategyRunner instances without requiring a service restart.

Table schema:
    PK:  "STRATEGY#{strategy_name}"
    SK:  "CONFIG#{env}"          e.g. "CONFIG#production"
    enabled:                     bool
    paper_trade:                 bool
    max_signals_per_day:         int   (0 = unlimited)
    circuit_breaker_threshold_consecutive: int
    circuit_breaker_threshold_rate:        int
    circuit_breaker_state:       str  (CLOSED/OPEN/HALF_OPEN — written by runner, read-only here)
    circuit_breaker_reset:       bool (operator sets True to force reset)
    updated_at:                  str  (ISO-8601 UTC)
    updated_by:                  str  (operator identity)

Hot-reload behaviour:
    Every 60 seconds, _config_refresh_loop() in StrategyEngineService calls
    refresh_all(). This method reads the table and calls runner.apply_config()
    for each registered strategy. The runner updates its live configuration
    atomically — in-progress dispatches are not interrupted.

    If circuit_breaker_reset=True is found:
        apply_config(reset_cb=True) is called, which forces the circuit to CLOSED.
        After applying the reset, this loader writes circuit_breaker_reset=False
        back to DynamoDB so subsequent refreshes don't re-reset.

Defaults (if a strategy has no row in strategy-config):
    All strategies fall back to DEFAULT_CONFIG. This is safe:
    paper_trade=True ensures no live orders are placed for un-configured strategies.

Usage (from StrategyEngineService):
    loader = StrategyConfigLoader(
        dynamo_table=boto3_resource.Table("quantembrace-prod-strategy-config"),
        env="production",
    )
    loader.register(momentum_runner)
    loader.register(orb_runner)
    ...
    await loader.refresh_all()   # called by _config_refresh_loop() every 60s
"""

from __future__ import annotations

from datetime import UTC, datetime
import time
from typing import Any

from shared.logging.logger import get_logger
from strategy_engine.runners.strategy_runner import StrategyConfig, StrategyRunner

logger = get_logger(__name__, service_name="strategy_engine")


# Default config used when a strategy has no row in strategy-config.
# paper_trade=True is intentional — unconfigured strategies never trade live.
_DEFAULT_CONFIG = StrategyConfig(
    enabled=True,
    paper_trade=True,
    max_signals_per_day=10,
    circuit_breaker_threshold_consecutive=5,
    circuit_breaker_threshold_rate=10,
)

# DynamoDB PK/SK key format — must match setup_local_tables.py schema
_PK_PREFIX = "STRATEGY_CONFIG#"
_SK_PREFIX = "ENV#"


def _normalize_environment(env: Any) -> str:
    """
    Return the canonical environment token used in strategy-config SK values.

    Pydantic settings pass shared.config.settings.Environment enum instances,
    while operator scripts write literal rows such as CONFIG#production.  Using
    str(enum) would produce CONFIG#Environment.PRODUCTION and silently miss the
    live-control row, so normalize at the loader boundary.
    """
    raw_env = getattr(env, "value", env)
    env_text = str(raw_env).strip()
    if env_text.lower().startswith("environment."):
        env_text = env_text.split(".", 1)[1]

    env_text = env_text.lower()
    aliases = {
        "dev": "development",
        "prod": "production",
        "stage": "staging",
    }
    return aliases.get(env_text, env_text)


class StrategyConfigLoader:
    """
    Polls DynamoDB strategy-config and applies hot-reload updates to StrategyRunners.

    Args:
        dynamo_table: boto3 DynamoDB Table resource for the strategy-config table.
        env:          Environment string (e.g. "production", "staging").
    """

    def __init__(
        self,
        dynamo_table: Any,  # boto3 DynamoDB Table resource
        env: Any = "production",
    ) -> None:
        self._table = dynamo_table
        self._env = _normalize_environment(env)
        self._runners: dict[str, StrategyRunner] = {}
        self._last_refresh: float | None = None

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, runner: StrategyRunner) -> None:
        """Register a StrategyRunner to be managed by this loader."""
        self._runners[runner.name] = runner
        logger.info("strategy_config_loader.registered strategy=%s interface=%s", runner.name, runner.interface_type.value)

    # ── Refresh ───────────────────────────────────────────────────────────────

    async def refresh_all(self) -> None:
        """
        Read all strategy configs from DynamoDB and apply to registered runners.

        Should be called every 60s from StrategyEngineService._config_refresh_loop().
        This method is async-compatible but uses asyncio.to_thread() internally
        for the DynamoDB GetItem calls (blocking boto3 operations).
        """
        import asyncio

        t0 = time.monotonic()
        updated = 0
        errors  = 0

        for strategy_name, runner in self._runners.items():
            try:
                config, reset_cb = await asyncio.to_thread(
                    self._load_config, strategy_name
                )
                did_reset = runner.apply_config(config, reset_cb=reset_cb)

                if reset_cb and did_reset:
                    # Clear the reset flag in DynamoDB so next refresh doesn't re-reset
                    await asyncio.to_thread(
                        self._clear_reset_flag, strategy_name
                    )

                updated += 1

            except Exception:
                errors += 1
                logger.exception("strategy_config_loader.refresh_error strategy=%s", strategy_name)

        elapsed_ms = (time.monotonic() - t0) * 1000
        self._last_refresh = time.monotonic()

        logger.info("strategy_config_loader.refresh_complete updated=%d errors=%d latency_ms=%.1f total=%d", updated, errors, round(elapsed_ms, 1), len(self._runners))

    # ── DynamoDB operations ───────────────────────────────────────────────────

    def _load_config(self, strategy_name: str) -> tuple[StrategyConfig, bool]:
        """
        Read a single strategy's config row from DynamoDB.

        Returns:
            Tuple of (StrategyConfig, reset_cb).
            reset_cb is True if circuit_breaker_reset=True was found in the row.
        """
        pk = f"{_PK_PREFIX}{strategy_name}"
        sk = f"{_SK_PREFIX}{self._env}"

        response = self._table.get_item(Key={"PK": pk, "SK": sk})
        item = response.get("Item")

        if item is None:
            # No row — use defaults. This is normal for new strategies.
            logger.debug("strategy_config_loader.no_config_row strategy=%s env=%s (using defaults)", strategy_name, self._env)
            return _DEFAULT_CONFIG, False

        config = StrategyConfig(
            enabled=bool(item.get("enabled", True)),
            paper_trade=bool(item.get("paper_trade", True)),
            max_signals_per_day=int(item.get("max_signals_per_day", 0)),
            circuit_breaker_threshold_consecutive=int(
                item.get("circuit_breaker_threshold_consecutive", 5)
            ),
            circuit_breaker_threshold_rate=int(
                item.get("circuit_breaker_threshold_rate", 10)
            ),
        )

        reset_cb = bool(item.get("circuit_breaker_reset", False))

        return config, reset_cb

    def _clear_reset_flag(self, strategy_name: str) -> None:
        """
        Write circuit_breaker_reset=False to DynamoDB after applying a manual reset.

        Prevents subsequent config refreshes from re-resetting the circuit breaker.
        """
        pk = f"{_PK_PREFIX}{strategy_name}"
        sk = f"{_SK_PREFIX}{self._env}"

        try:
            self._table.update_item(
                Key={"PK": pk, "SK": sk},
                UpdateExpression=(
                    "SET circuit_breaker_reset = :false, "
                    "updated_at = :ts, updated_by = :actor"
                ),
                ExpressionAttributeValues={
                    ":false": False,
                    ":ts":    datetime.now(UTC).isoformat(),
                    ":actor": "strategy_engine:auto_clear_reset",
                },
            )
            logger.info("strategy_config_loader.reset_flag_cleared strategy=%s env=%s", strategy_name, self._env)
        except Exception:
            logger.exception("strategy_config_loader.clear_reset_flag_error strategy=%s", strategy_name)

    # ── Seed helper (used by scripts/strategy/config.py) ─────────────────────

    def seed_defaults(
        self,
        strategies: list[tuple[str, int]],  # [(strategy_name, max_signals_per_day), ...]
    ) -> None:
        """
        Write default config rows for all strategies if no row exists.

        Called by scripts/strategy/config.py --seed-from-yaml.
        Only writes rows that don't already exist (conditional put).

        Args:
            strategies: List of (strategy_name, max_signals_per_day) tuples.
        """
        for strategy_name, max_signals in strategies:
            pk = f"{_PK_PREFIX}{strategy_name}"
            sk = f"{_SK_PREFIX}{self._env}"
            try:
                self._table.put_item(
                    Item={
                        "PK":                                    pk,
                        "SK":                                    sk,
                        "enabled":                               True,
                        "paper_trade":                           True,
                        "max_signals_per_day":                   max_signals,
                        "circuit_breaker_threshold_consecutive": 5,
                        "circuit_breaker_threshold_rate":        10,
                        "circuit_breaker_state":                 "CLOSED",
                        # circuit_breaker_opened_at is intentionally omitted from the
                        # seed item — DynamoDB resource API rejects Python None values.
                        # The attribute is written only when the circuit actually opens
                        # (with an ISO-8601 timestamp string).
                        "circuit_breaker_reset":                 False,
                        "updated_at":                            datetime.now(UTC).isoformat(),
                        "updated_by":                            "seed_defaults",
                    },
                    ConditionExpression="attribute_not_exists(PK)",
                )
                logger.info("strategy_config_loader.seeded strategy=%s env=%s max_signals=%d", strategy_name, self._env, max_signals)
            except self._table.meta.client.exceptions.ConditionalCheckFailedException:
                logger.debug("strategy_config_loader.seed_skipped_exists strategy=%s env=%s", strategy_name, self._env)
            except Exception:
                logger.exception("strategy_config_loader.seed_error strategy=%s env=%s", strategy_name, self._env)
