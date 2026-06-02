"""
Unit tests for StrategyConfigLoader (Phase 3 — ADR-013 §9).

Coverage:
    register()
        - Adds runner to internal registry

    refresh_all()
        - Calls _load_config for each registered runner
        - Calls runner.apply_config with loaded config
        - When reset_cb=True: calls apply_config(reset_cb=True) then _clear_reset_flag
        - When reset_cb=True but apply_config returns False: does NOT call _clear_reset_flag
        - Exception in _load_config does not abort other runners
        - Logs refresh_complete with correct counts

    _load_config()
        - Returns DEFAULT_CONFIG when no row exists in DynamoDB
        - Parses all fields correctly from DynamoDB item
        - reset_cb=True when circuit_breaker_reset=True in item
        - reset_cb=False when circuit_breaker_reset=False in item

    _clear_reset_flag()
        - Writes circuit_breaker_reset=False and updated timestamps
        - Logs error but does not raise on DynamoDB failure

    seed_defaults()
        - Writes default config rows for all provided strategies
        - Skips existing rows via ConditionalCheckFailedException
        - Logs error but continues on unexpected exception
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from strategy_engine.config.strategy_config_loader import StrategyConfigLoader  # noqa: E402
from strategy_engine.runners.strategy_runner import (  # noqa: E402
    InterfaceType,
    StrategyConfig,
    StrategyRunner,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_table(item: dict | None = None, raises: Exception | None = None) -> MagicMock:
    """Build a mock boto3 DynamoDB Table resource."""
    table = MagicMock()
    table.name = "test-strategy-config"

    if raises is not None:
        table.get_item.side_effect = raises
    else:
        table.get_item.return_value = {"Item": item} if item else {}

    table.update_item.return_value = {}
    table.put_item.return_value = {}

    # For seed_defaults — provide the meta.client.exceptions.ConditionalCheckFailedException
    exc_class = type(
        "ConditionalCheckFailedException",
        (Exception,),
        {},
    )
    table.meta = MagicMock()
    table.meta.client.exceptions.ConditionalCheckFailedException = exc_class

    return table


def _make_runner(name: str = "nse_orb_15m") -> StrategyRunner:
    """Build a StrategyRunner with a mocked strategy."""
    strategy = MagicMock()
    strategy.name = name
    strategy.symbols = ["RELIANCE"]
    runner = StrategyRunner(strategy, InterfaceType.CANDLE, StrategyConfig())
    return runner


def _dynamo_item(
    strategy_name: str = "nse_orb_15m",
    env: str = "test",
    enabled: bool = True,
    paper_trade: bool = True,
    max_signals: int = 5,
    cb_consec: int = 4,
    cb_rate: int = 8,
    cb_reset: bool = False,
) -> dict:
    return {
        "PK":  f"STRATEGY#{strategy_name}",
        "SK":  f"CONFIG#{env}",
        "enabled":                               enabled,
        "paper_trade":                           paper_trade,
        "max_signals_per_day":                   max_signals,
        "circuit_breaker_threshold_consecutive": cb_consec,
        "circuit_breaker_threshold_rate":        cb_rate,
        "circuit_breaker_reset":                 cb_reset,
        "updated_at":                            datetime.now(UTC).isoformat(),
        "updated_by":                            "test",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# register()
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegister:

    def test_register_adds_runner(self):
        table = _make_table()
        loader = StrategyConfigLoader(dynamo_table=table, env="test")
        runner = _make_runner("nse_orb_15m")
        loader.register(runner)
        assert "nse_orb_15m" in loader._runners

    def test_register_multiple_runners(self):
        table = _make_table()
        loader = StrategyConfigLoader(dynamo_table=table, env="test")
        for name in ("a", "b", "c"):
            loader.register(_make_runner(name))
        assert len(loader._runners) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# _load_config()
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadConfig:

    def test_no_row_returns_default_config(self):
        table = _make_table(item=None)   # no item in DynamoDB
        loader = StrategyConfigLoader(dynamo_table=table, env="test")
        config, reset_cb = loader._load_config("nse_orb_15m")

        assert config.paper_trade is True    # default
        assert config.enabled is True        # default
        assert reset_cb is False

    def test_row_parsed_correctly(self):
        item = _dynamo_item(
            enabled=False, paper_trade=False,
            max_signals=7, cb_consec=3, cb_rate=6,
        )
        table = _make_table(item=item)
        loader = StrategyConfigLoader(dynamo_table=table, env="test")
        config, _ = loader._load_config("nse_orb_15m")

        assert config.enabled is False
        assert config.paper_trade is False
        assert config.max_signals_per_day == 7
        assert config.circuit_breaker_threshold_consecutive == 3
        assert config.circuit_breaker_threshold_rate == 6

    def test_reset_cb_true_when_circuit_breaker_reset_true(self):
        item = _dynamo_item(cb_reset=True)
        table = _make_table(item=item)
        loader = StrategyConfigLoader(dynamo_table=table, env="test")
        _, reset_cb = loader._load_config("nse_orb_15m")
        assert reset_cb is True

    def test_reset_cb_false_when_circuit_breaker_reset_false(self):
        item = _dynamo_item(cb_reset=False)
        table = _make_table(item=item)
        loader = StrategyConfigLoader(dynamo_table=table, env="test")
        _, reset_cb = loader._load_config("nse_orb_15m")
        assert reset_cb is False

    def test_enum_environment_uses_operator_cli_sort_key(self):
        class Environment(Enum):
            PRODUCTION = "production"

        item = _dynamo_item(env="production", paper_trade=False)
        table = _make_table(item=item)
        loader = StrategyConfigLoader(dynamo_table=table, env=Environment.PRODUCTION)

        config, _ = loader._load_config("nse_orb_15m")

        assert config.paper_trade is False
        assert table.get_item.call_args[1]["Key"] == {
            "PK": "STRATEGY#nse_orb_15m",
            "SK": "CONFIG#production",
        }

    def test_stringified_enum_environment_uses_operator_cli_sort_key(self):
        table = _make_table(item=_dynamo_item(env="production"))
        loader = StrategyConfigLoader(dynamo_table=table, env="Environment.PRODUCTION")

        loader._load_config("nse_orb_15m")

        assert table.get_item.call_args[1]["Key"]["SK"] == "CONFIG#production"


# ═══════════════════════════════════════════════════════════════════════════════
# refresh_all()
# ═══════════════════════════════════════════════════════════════════════════════

class TestRefreshAll:

    @pytest.mark.asyncio
    async def test_refresh_calls_apply_config(self):
        item = _dynamo_item(enabled=True, paper_trade=True, max_signals=5)
        table = _make_table(item=item)
        loader = StrategyConfigLoader(dynamo_table=table, env="test")

        runner = _make_runner("nse_orb_15m")
        runner.apply_config = MagicMock(return_value=False)
        loader.register(runner)

        await loader.refresh_all()

        runner.apply_config.assert_called_once()
        call_args = runner.apply_config.call_args
        config_arg = call_args[0][0]
        assert isinstance(config_arg, StrategyConfig)
        assert config_arg.max_signals_per_day == 5

    @pytest.mark.asyncio
    async def test_refresh_with_reset_cb_calls_clear_flag(self):
        item = _dynamo_item(cb_reset=True)
        table = _make_table(item=item)
        loader = StrategyConfigLoader(dynamo_table=table, env="test")

        runner = _make_runner("nse_orb_15m")
        runner.apply_config = MagicMock(return_value=True)   # simulates reset applied
        loader.register(runner)

        with patch.object(loader, "_clear_reset_flag") as mock_clear:
            await loader.refresh_all()

        # apply_config was called with reset_cb=True
        runner.apply_config.assert_called_once()
        assert runner.apply_config.call_args[1].get("reset_cb") is True or \
               runner.apply_config.call_args[0][1] is True

        # _clear_reset_flag called after successful reset
        mock_clear.assert_called_once_with("nse_orb_15m")

    @pytest.mark.asyncio
    async def test_refresh_reset_cb_true_but_apply_returns_false_no_clear(self):
        """If apply_config returns False (reset not actually applied), don't clear."""
        item = _dynamo_item(cb_reset=True)
        table = _make_table(item=item)
        loader = StrategyConfigLoader(dynamo_table=table, env="test")

        runner = _make_runner("nse_orb_15m")
        runner.apply_config = MagicMock(return_value=False)   # reset NOT applied
        loader.register(runner)

        with patch.object(loader, "_clear_reset_flag") as mock_clear:
            await loader.refresh_all()

        mock_clear.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_exception_does_not_abort_other_runners(self):
        table = MagicMock()
        table.name = "test"
        table.get_item.side_effect = [
            Exception("DynamoDB error"),   # first runner fails
            {"Item": _dynamo_item()},       # second runner succeeds
        ]
        loader = StrategyConfigLoader(dynamo_table=table, env="test")

        runner_a = _make_runner("strategy_a")
        runner_b = _make_runner("strategy_b")
        runner_a.apply_config = MagicMock(return_value=False)
        runner_b.apply_config = MagicMock(return_value=False)

        loader.register(runner_a)
        loader.register(runner_b)

        await loader.refresh_all()

        # runner_a failed — apply_config not called
        runner_a.apply_config.assert_not_called()
        # runner_b succeeded — apply_config was called
        runner_b.apply_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_no_runners_completes_cleanly(self):
        table = _make_table()
        loader = StrategyConfigLoader(dynamo_table=table, env="test")
        await loader.refresh_all()   # should not raise

    @pytest.mark.asyncio
    async def test_refresh_sets_last_refresh_timestamp(self):
        table = _make_table()
        loader = StrategyConfigLoader(dynamo_table=table, env="test")
        assert loader._last_refresh is None
        await loader.refresh_all()
        assert loader._last_refresh is not None


# ═══════════════════════════════════════════════════════════════════════════════
# _clear_reset_flag()
# ═══════════════════════════════════════════════════════════════════════════════

class TestClearResetFlag:

    def test_writes_circuit_breaker_reset_false(self):
        table = _make_table()
        loader = StrategyConfigLoader(dynamo_table=table, env="production")
        loader._clear_reset_flag("nse_orb_15m")

        table.update_item.assert_called_once()
        call_kwargs = table.update_item.call_args[1]

        # Key should be PK=STRATEGY#nse_orb_15m, SK=CONFIG#production
        assert call_kwargs["Key"]["PK"] == "STRATEGY#nse_orb_15m"
        assert call_kwargs["Key"]["SK"] == "CONFIG#production"

        # ExpressionAttributeValues should include :false = False
        attr_values = call_kwargs["ExpressionAttributeValues"]
        assert ":false" in attr_values
        assert attr_values[":false"] is False

    def test_clear_reset_flag_dynamodb_error_does_not_raise(self):
        table = _make_table()
        table.update_item.side_effect = Exception("DynamoDB unavailable")
        loader = StrategyConfigLoader(dynamo_table=table, env="test")
        # Should log error but not raise
        loader._clear_reset_flag("nse_orb_15m")   # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# seed_defaults()
# ═══════════════════════════════════════════════════════════════════════════════

class TestSeedDefaults:

    def test_writes_rows_for_all_strategies(self):
        table = _make_table()
        loader = StrategyConfigLoader(dynamo_table=table, env="staging")
        strategies = [("strat_a", 5), ("strat_b", 10)]
        loader.seed_defaults(strategies)
        assert table.put_item.call_count == 2

    def test_seed_writes_paper_trade_true(self):
        table = _make_table()
        loader = StrategyConfigLoader(dynamo_table=table, env="staging")
        loader.seed_defaults([("nse_orb_15m", 4)])

        call_kwargs = table.put_item.call_args[1]
        item = call_kwargs["Item"]
        assert item["paper_trade"] is True
        assert item["enabled"] is True
        assert item["max_signals_per_day"] == 4

    def test_seed_skips_existing_row(self):
        # Simulate ConditionalCheckFailedException on first put, success on second
        exc_class = type("ConditionalCheckFailedException", (Exception,), {})
        table = MagicMock()
        table.name = "test"
        table.put_item.side_effect = [
            exc_class(),   # first strategy already exists
            None,          # second strategy written successfully
        ]
        table.meta.client.exceptions.ConditionalCheckFailedException = exc_class

        loader = StrategyConfigLoader(dynamo_table=table, env="staging")
        loader.seed_defaults([("strat_a", 5), ("strat_b", 10)])

        # put_item called for both; first one raised ConditionalCheckFailedException (skipped)
        assert table.put_item.call_count == 2

    def test_seed_continues_after_unexpected_error(self):
        exc_class = type("ConditionalCheckFailedException", (Exception,), {})
        table = MagicMock()
        table.name = "test"
        table.put_item.side_effect = [
            Exception("Unexpected DynamoDB error"),  # first fails unexpectedly
            None,                                     # second succeeds
        ]
        table.meta.client.exceptions.ConditionalCheckFailedException = exc_class

        loader = StrategyConfigLoader(dynamo_table=table, env="staging")
        loader.seed_defaults([("strat_a", 5), ("strat_b", 10)])
        # must not raise; second put still called
        assert table.put_item.call_count == 2

    def test_seed_sets_correct_pk_sk(self):
        table = _make_table()
        loader = StrategyConfigLoader(dynamo_table=table, env="production")
        loader.seed_defaults([("nse_orb_15m", 4)])

        call_kwargs = table.put_item.call_args[1]
        item = call_kwargs["Item"]
        assert item["PK"] == "STRATEGY#nse_orb_15m"
        assert item["SK"] == "CONFIG#production"
