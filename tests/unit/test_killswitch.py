"""
Unit tests for the Kill Switch system (TASK-007).

Covers:
    - KillSwitch core: manual activate/deactivate, idempotency, state persistence
    - KillSwitch core: SNS publish on activation and deactivation
    - KillSwitch core: load_state() restores from DynamoDB
    - KillSwitch core: get_status() returns correct snapshot
    - KillSwitchMonitor: order rate runaway trigger
    - KillSwitchMonitor: broker connectivity lost trigger
    - KillSwitchMonitor: data feed staleness trigger
    - KillSwitchMonitor: single-strategy loss trigger
    - KillSwitchMonitor: no false positive during non-market hours (data staleness)
    - KillSwitchMonitor: no trigger before first broker ping (connectivity)
    - HTTP API: GET /risk/kill-switch/status
    - HTTP API: POST /risk/kill-switch/activate
    - HTTP API: POST /risk/kill-switch/deactivate with correct/wrong confirmation
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap: tests run from the project root; services/ must be on path
# ---------------------------------------------------------------------------
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)


# ---------------------------------------------------------------------------
# Minimal stub for shared.* so we don't need the full service stack
# ---------------------------------------------------------------------------

def _install_shared_stubs() -> None:
    """Install minimal stubs for shared.* imports used by the kill switch."""

    def _make_pkg(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    # shared
    shared = _make_pkg("shared")
    shared_config = _make_pkg("shared.config")
    shared_config_settings = _make_pkg("shared.config.settings")
    shared_logging = _make_pkg("shared.logging")
    shared_logging_logger = _make_pkg("shared.logging.logger")
    shared_utils = _make_pkg("shared.utils")
    shared_utils_helpers = _make_pkg("shared.utils.helpers")

    # AppSettings stub
    class _AWSConfig:
        dynamodb_table_orders = "quantembrace-dev-orders"
        sns_kill_switch_topic_arn = "arn:aws:sns:us-east-1:123456789:qs-kill-switch"
        region = "us-east-1"

    class _AppSettings:
        aws = _AWSConfig()

    def get_settings() -> _AppSettings:
        return _AppSettings()

    shared_config_settings.AppSettings = _AppSettings
    shared_config_settings.get_settings = get_settings
    shared_config.settings = shared_config_settings

    # logger stub
    def get_logger(name: str, **_kw):  # type: ignore[return]
        import logging
        return logging.getLogger(name)

    shared_logging_logger.get_logger = get_logger

    # helpers stubs
    _utc_now_value = datetime(2025, 10, 1, 9, 0, 0, tzinfo=timezone.utc)

    def utc_now() -> datetime:
        return _utc_now_value

    def utc_iso() -> str:
        return _utc_now_value.isoformat()

    shared_utils_helpers.utc_now = utc_now
    shared_utils_helpers.utc_iso = utc_iso

    # Link packages
    shared.config = shared_config
    shared.logging = shared_logging
    shared.utils = shared_utils
    shared_config.settings = shared_config_settings
    shared_logging.logger = shared_logging_logger
    shared_utils.helpers = shared_utils_helpers


_install_shared_stubs()

# Now we can import the real modules
from risk_engine.killswitch.killswitch import KillSwitch  # noqa: E402
from risk_engine.killswitch.auto_triggers import KillSwitchMonitor  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dynamo_mock() -> MagicMock:
    """DynamoDB client mock that returns empty get_item by default."""
    m = MagicMock()
    m.get_item.return_value = {"Item": None}
    m.put_item.return_value = {}
    m.update_item.return_value = {}
    # ConditionalCheckFailedException
    m.exceptions = MagicMock()
    m.exceptions.ConditionalCheckFailedException = Exception
    return m


def _sns_mock() -> MagicMock:
    m = MagicMock()
    m.publish.return_value = {"MessageId": "test-message-id"}
    return m


def _make_ks(
    dynamo: MagicMock | None = None,
    sns: MagicMock | None = None,
    topic_arn: str = "arn:aws:sns:us-east-1:123456789:qs-kill-switch",
) -> KillSwitch:
    return KillSwitch(
        dynamo_client=dynamo or _dynamo_mock(),
        sns_client=sns or _sns_mock(),
        sns_topic_arn=topic_arn,
    )


# ---------------------------------------------------------------------------
# KillSwitch core tests
# ---------------------------------------------------------------------------


class TestKillSwitchActivate:
    """Manual activation path."""

    @pytest.mark.asyncio
    async def test_activate_sets_active_true(self):
        ks = _make_ks()
        assert not ks.active
        await ks.activate(reason="Test halt")
        assert ks.active

    @pytest.mark.asyncio
    async def test_activate_sets_reason(self):
        ks = _make_ks()
        await ks.activate(reason="Daily loss limit")
        assert ks.reason == "Daily loss limit"

    @pytest.mark.asyncio
    async def test_activate_sets_timestamp(self):
        ks = _make_ks()
        await ks.activate()
        assert ks.activated_at is not None
        assert isinstance(ks.activated_at, datetime)

    @pytest.mark.asyncio
    async def test_activate_idempotent_when_already_active(self):
        sns = _sns_mock()
        ks = _make_ks(sns=sns)
        await ks.activate(reason="First reason")
        sns.publish.reset_mock()

        # Second activation with different reason — should be ignored
        await ks.activate(reason="Second reason")
        assert ks.reason == "First reason"           # unchanged
        sns.publish.assert_not_called()              # no duplicate SNS

    @pytest.mark.asyncio
    async def test_activate_persists_to_dynamodb(self):
        dynamo = _dynamo_mock()
        ks = _make_ks(dynamo=dynamo)
        await ks.activate(reason="DB test")
        dynamo.put_item.assert_called_once()
        call_kwargs = dynamo.put_item.call_args[1]
        item = call_kwargs["Item"]
        assert item["active"]["BOOL"] is True
        assert item["reason"]["S"] == "DB test"

    @pytest.mark.asyncio
    async def test_activate_publishes_sns(self):
        sns = _sns_mock()
        ks = _make_ks(sns=sns)
        await ks.activate(reason="SNS test")
        sns.publish.assert_called_once()
        publish_kwargs = sns.publish.call_args[1]
        assert publish_kwargs["TopicArn"] == "arn:aws:sns:us-east-1:123456789:qs-kill-switch"
        payload = json.loads(publish_kwargs["Message"])
        assert payload["event"] == "KILL_SWITCH_ACTIVATED"
        assert payload["active"] is True

    @pytest.mark.asyncio
    async def test_activate_sns_failure_does_not_raise(self):
        sns = _sns_mock()
        sns.publish.side_effect = Exception("SNS unavailable")
        ks = _make_ks(sns=sns)
        # Should not raise even if SNS publish fails
        await ks.activate(reason="SNS failure test")
        assert ks.active  # still activated despite SNS failure


class TestKillSwitchDeactivate:
    """Manual deactivation path."""

    @pytest.mark.asyncio
    async def test_deactivate_clears_active(self):
        ks = _make_ks()
        await ks.activate(reason="halt")
        await ks.deactivate()
        assert not ks.active

    @pytest.mark.asyncio
    async def test_deactivate_clears_reason(self):
        ks = _make_ks()
        await ks.activate(reason="halt")
        await ks.deactivate()
        assert ks.reason == ""
        assert ks.activated_at is None

    @pytest.mark.asyncio
    async def test_deactivate_idempotent_when_already_inactive(self):
        sns = _sns_mock()
        ks = _make_ks(sns=sns)
        # Switch is already inactive; deactivate should be a no-op
        await ks.deactivate()
        sns.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_deactivate_publishes_sns(self):
        sns = _sns_mock()
        ks = _make_ks(sns=sns)
        await ks.activate(reason="halt")
        sns.publish.reset_mock()
        await ks.deactivate()
        sns.publish.assert_called_once()
        payload = json.loads(sns.publish.call_args[1]["Message"])
        assert payload["event"] == "KILL_SWITCH_DEACTIVATED"
        assert payload["active"] is False

    @pytest.mark.asyncio
    async def test_deactivate_persists_to_dynamodb(self):
        dynamo = _dynamo_mock()
        ks = _make_ks(dynamo=dynamo)
        await ks.activate(reason="halt")
        dynamo.put_item.reset_mock()
        await ks.deactivate()
        dynamo.put_item.assert_called_once()
        item = dynamo.put_item.call_args[1]["Item"]
        assert item["active"]["BOOL"] is False


class TestKillSwitchLoadState:
    """State restoration from DynamoDB."""

    @pytest.mark.asyncio
    async def test_load_state_restores_active(self):
        dynamo = _dynamo_mock()
        dynamo.get_item.return_value = {
            "Item": {
                "active": {"BOOL": True},
                "reason": {"S": "Persisted halt"},
                "activated_by": {"S": "loss_validator"},
                "activated_at": {"S": "2025-10-01T09:00:00+00:00"},
            }
        }
        ks = _make_ks(dynamo=dynamo)
        await ks.load_state()
        assert ks.active is True
        assert ks.reason == "Persisted halt"

    @pytest.mark.asyncio
    async def test_load_state_defaults_to_inactive_when_no_record(self):
        dynamo = _dynamo_mock()
        dynamo.get_item.return_value = {"Item": None}
        ks = _make_ks(dynamo=dynamo)
        await ks.load_state()
        assert ks.active is False

    @pytest.mark.asyncio
    async def test_load_state_defaults_to_inactive_on_dynamo_error(self):
        dynamo = _dynamo_mock()
        dynamo.get_item.side_effect = Exception("DynamoDB unavailable")
        ks = _make_ks(dynamo=dynamo)
        await ks.load_state()  # must not raise
        assert ks.active is False

    @pytest.mark.asyncio
    async def test_load_state_without_dynamo_client(self):
        ks = KillSwitch(dynamo_client=None)
        await ks.load_state()  # must not raise
        assert ks.active is False


class TestKillSwitchGetStatus:
    """get_status() snapshot."""

    @pytest.mark.asyncio
    async def test_get_status_inactive(self):
        ks = _make_ks()
        status = ks.get_status()
        assert status["active"] is False
        assert status["reason"] == ""
        assert status["activated_at"] is None
        assert status["activated_by"] is None

    @pytest.mark.asyncio
    async def test_get_status_active(self):
        ks = _make_ks()
        await ks.activate(reason="Test", activated_by="pytest")
        status = ks.get_status()
        assert status["active"] is True
        assert status["reason"] == "Test"
        assert status["activated_at"] is not None
        assert status["activated_by"] == "pytest"


# ---------------------------------------------------------------------------
# KillSwitchMonitor auto-trigger tests
# ---------------------------------------------------------------------------


def _make_monitor(
    kill_switch: KillSwitch | None = None,
    **kwargs,
) -> KillSwitchMonitor:
    ks = kill_switch or _make_ks()
    return KillSwitchMonitor(
        kill_switch=ks,
        poll_interval_secs=0.01,  # fast for tests
        **kwargs,
    )


class TestOrderRateTrigger:
    """Monitor 1 — order rate runaway."""

    @pytest.mark.asyncio
    async def test_activates_when_rate_exceeded(self):
        ks = _make_ks()
        monitor = _make_monitor(
            kill_switch=ks,
            order_rate_limit=5,
            order_rate_window_secs=60,
        )

        # Simulate 6 orders in the window
        for _ in range(6):
            monitor.record_order()

        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

        assert ks.active
        assert "order rate runaway" in ks.reason

    @pytest.mark.asyncio
    async def test_no_activation_when_rate_within_limit(self):
        ks = _make_ks()
        monitor = _make_monitor(
            kill_switch=ks,
            order_rate_limit=10,
            order_rate_window_secs=60,
        )

        # Only 5 orders — under the limit of 10
        for _ in range(5):
            monitor.record_order()

        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

        assert not ks.active

    @pytest.mark.asyncio
    async def test_no_activation_when_already_active(self):
        """If kill switch is already active, monitor should not re-trigger."""
        ks = _make_ks()
        await ks.activate(reason="Pre-existing halt")
        sns_mock = ks._sns
        sns_mock.publish.reset_mock()

        monitor = _make_monitor(kill_switch=ks, order_rate_limit=5)
        for _ in range(10):
            monitor.record_order()

        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

        # SNS should not be called again — already active
        sns_mock.publish.assert_not_called()


class TestBrokerConnectivityTrigger:
    """Monitor 2 — broker connectivity lost."""

    @pytest.mark.asyncio
    async def test_activates_when_broker_silent_too_long(self):
        ks = _make_ks()
        monitor = _make_monitor(
            kill_switch=ks,
            broker_timeout_secs=0.05,  # very short for tests
        )

        # Simulate an old ping (2 seconds ago relative to "now")
        old_time = datetime(2025, 10, 1, 8, 59, 0, tzinfo=timezone.utc)  # 60s before utc_now stub
        monitor._broker_connected = True
        monitor._last_broker_ping = old_time

        await monitor.start()
        await asyncio.sleep(0.1)
        await monitor.stop()

        assert ks.active
        assert "broker connectivity lost" in ks.reason

    @pytest.mark.asyncio
    async def test_no_activation_before_first_ping(self):
        """Monitor should not fire before any broker connection is established."""
        ks = _make_ks()
        monitor = _make_monitor(
            kill_switch=ks,
            broker_timeout_secs=0.01,
        )
        # _broker_connected is False — no ping yet
        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()
        assert not ks.active

    @pytest.mark.asyncio
    async def test_no_activation_when_recent_ping(self):
        """No trigger when broker pinged recently."""
        from shared.utils.helpers import utc_now as _utc_now
        ks = _make_ks()
        monitor = _make_monitor(
            kill_switch=ks,
            broker_timeout_secs=60.0,
        )
        monitor.record_broker_ping()  # sets _last_broker_ping to utc_now() stub value
        # utc_now stub returns 2025-10-01T09:00:00 — same as "now", so elapsed=0
        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()
        assert not ks.active


class TestDataStalenessTrigger:
    """Monitor 3 — data feed stale."""

    @pytest.mark.asyncio
    async def test_activates_when_data_stale_during_market_hours(self):
        ks = _make_ks()
        monitor = _make_monitor(
            kill_switch=ks,
            data_stale_secs=0.05,
        )

        # Record a very old tick (60s before the stub utc_now)
        old_time = datetime(2025, 10, 1, 8, 59, 0, tzinfo=timezone.utc)
        monitor._last_tick["US"] = old_time

        # Patch _is_market_hours to return True
        with patch.object(KillSwitchMonitor, "_is_market_hours", return_value=True):
            await monitor.start()
            await asyncio.sleep(0.15)
            await monitor.stop()

        assert ks.active
        assert "data feed stale" in ks.reason

    @pytest.mark.asyncio
    async def test_no_activation_outside_market_hours(self):
        """Data staleness should not trigger outside of market hours."""
        ks = _make_ks()
        monitor = _make_monitor(kill_switch=ks, data_stale_secs=0.01)

        old_time = datetime(2025, 10, 1, 8, 59, 0, tzinfo=timezone.utc)
        monitor._last_tick["US"] = old_time

        # Override _is_market_hours to return False
        with patch.object(KillSwitchMonitor, "_is_market_hours", return_value=False):
            await monitor.start()
            await asyncio.sleep(0.05)
            await monitor.stop()

        assert not ks.active

    @pytest.mark.asyncio
    async def test_no_activation_when_no_ticks_recorded(self):
        """If no ticks have ever been received, don't trigger (service just started)."""
        ks = _make_ks()
        monitor = _make_monitor(kill_switch=ks, data_stale_secs=0.01)
        # _last_tick is empty

        with patch.object(KillSwitchMonitor, "_is_market_hours", return_value=True):
            await monitor.start()
            await asyncio.sleep(0.05)
            await monitor.stop()

        assert not ks.active


class TestStrategyLossTrigger:
    """Monitor 4 — single-strategy loss."""

    @pytest.mark.asyncio
    async def test_activates_when_strategy_loss_exceeds_threshold(self):
        ks = _make_ks()
        monitor = _make_monitor(kill_switch=ks, strategy_loss_pct=5.0)

        # Record a strategy that lost 6% of its capital
        monitor.record_strategy_pnl(
            strategy_id="momentum_v1",
            pnl=-6000.0,
            starting_capital=100_000.0,  # 6% loss
        )

        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

        assert ks.active
        assert "strategy loss threshold" in ks.reason
        assert "momentum_v1" in ks.reason

    @pytest.mark.asyncio
    async def test_no_activation_when_loss_under_threshold(self):
        ks = _make_ks()
        monitor = _make_monitor(kill_switch=ks, strategy_loss_pct=5.0)

        # 4% loss — below threshold
        monitor.record_strategy_pnl(
            strategy_id="mean_reversion_v1",
            pnl=-4000.0,
            starting_capital=100_000.0,
        )

        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

        assert not ks.active

    @pytest.mark.asyncio
    async def test_no_activation_on_positive_pnl(self):
        ks = _make_ks()
        monitor = _make_monitor(kill_switch=ks, strategy_loss_pct=5.0)

        monitor.record_strategy_pnl(
            strategy_id="stat_arb_v1",
            pnl=+5000.0,
            starting_capital=100_000.0,
        )

        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

        assert not ks.active

    @pytest.mark.asyncio
    async def test_no_activation_when_capital_is_zero(self):
        """Avoid division by zero when capital not yet set."""
        ks = _make_ks()
        monitor = _make_monitor(kill_switch=ks, strategy_loss_pct=5.0)

        monitor.record_strategy_pnl(
            strategy_id="edge_case",
            pnl=-999.0,
            starting_capital=0.0,  # zero capital
        )

        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

        assert not ks.active


# ---------------------------------------------------------------------------
# HTTP API tests
# ---------------------------------------------------------------------------


class TestKillSwitchAPI:
    """aiohttp handler tests using mock requests."""

    def _make_request(self, app: dict, body: dict | None = None) -> MagicMock:
        req = MagicMock()
        req.app = app
        if body is not None:
            async def _json():
                return body
            req.json = _json
        else:
            async def _json_err():
                raise ValueError("no body")
            req.json = _json_err
        return req

    @pytest.mark.asyncio
    async def test_status_returns_inactive_state(self):
        from risk_engine.api.killswitch_api import handle_status
        ks = _make_ks()
        req = self._make_request({"kill_switch": ks, "monitor": None})
        resp = await handle_status(req)
        data = json.loads(resp.text)
        assert data["active"] is False

    @pytest.mark.asyncio
    async def test_status_returns_active_state(self):
        from risk_engine.api.killswitch_api import handle_status
        ks = _make_ks()
        await ks.activate(reason="Test halt", activated_by="pytest")
        req = self._make_request({"kill_switch": ks, "monitor": None})
        resp = await handle_status(req)
        data = json.loads(resp.text)
        assert data["active"] is True
        assert data["reason"] == "Test halt"

    @pytest.mark.asyncio
    async def test_activate_activates_kill_switch(self):
        from risk_engine.api.killswitch_api import handle_activate
        ks = _make_ks()
        req = self._make_request(
            {"kill_switch": ks, "monitor": None},
            body={"reason": "API halt", "activated_by": "operator"},
        )
        resp = await handle_activate(req)
        data = json.loads(resp.text)
        assert data["status"] == "activated"
        assert ks.active

    @pytest.mark.asyncio
    async def test_activate_returns_already_active_when_duplicate(self):
        from risk_engine.api.killswitch_api import handle_activate
        ks = _make_ks()
        await ks.activate(reason="Pre-existing")
        req = self._make_request(
            {"kill_switch": ks, "monitor": None},
            body={"reason": "Duplicate"},
        )
        resp = await handle_activate(req)
        data = json.loads(resp.text)
        assert data["status"] == "already_active"

    @pytest.mark.asyncio
    async def test_deactivate_with_correct_confirmation(self):
        from risk_engine.api.killswitch_api import (
            handle_deactivate,
            _REQUIRED_DEACTIVATION_CONFIRM,
        )
        ks = _make_ks()
        await ks.activate(reason="halt")
        req = self._make_request(
            {"kill_switch": ks, "monitor": None},
            body={"confirmation": _REQUIRED_DEACTIVATION_CONFIRM, "deactivated_by": "operator"},
        )
        resp = await handle_deactivate(req)
        data = json.loads(resp.text)
        assert data["status"] == "deactivated"
        assert not ks.active

    @pytest.mark.asyncio
    async def test_deactivate_with_wrong_confirmation_returns_400(self):
        from risk_engine.api.killswitch_api import handle_deactivate
        ks = _make_ks()
        await ks.activate(reason="halt")
        req = self._make_request(
            {"kill_switch": ks, "monitor": None},
            body={"confirmation": "wrong string"},
        )
        resp = await handle_deactivate(req)
        assert resp.status == 400
        data = json.loads(resp.text)
        assert data["status"] == "error"
        assert ks.active  # still active

    @pytest.mark.asyncio
    async def test_deactivate_returns_already_inactive(self):
        from risk_engine.api.killswitch_api import (
            handle_deactivate,
            _REQUIRED_DEACTIVATION_CONFIRM,
        )
        ks = _make_ks()
        # Not active — should return already_inactive
        req = self._make_request(
            {"kill_switch": ks, "monitor": None},
            body={"confirmation": _REQUIRED_DEACTIVATION_CONFIRM},
        )
        resp = await handle_deactivate(req)
        data = json.loads(resp.text)
        assert data["status"] == "already_inactive"
