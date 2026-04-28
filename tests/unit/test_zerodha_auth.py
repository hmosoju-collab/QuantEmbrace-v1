"""
Unit tests for ZerodhaTokenManager (TASK-001).

Covers:
    - load_credentials: Secrets Manager success, SM failure → env fallback,
      no credentials at all raises ValueError
    - get_valid_token: found + unexpired, found + expired, not found
    - exchange_request_token: success path persists token + updates cache,
      KiteConnect failure raises RuntimeError
    - store_token: DynamoDB put_item called with correct schema + TTL
    - is_token_valid: in-memory cache states
    - _next_expiry_utc: before boundary → same day, after boundary → next day
    - ZerodhaBrokerClient.connect(): valid token, expired token enters
      needs_authentication mode, env-var fallback
    - ZerodhaBrokerClient.place_order(): rejects when needs_authentication
    - ZerodhaBrokerClient.refresh_access_token(): delegates and updates state
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)


# ---------------------------------------------------------------------------
# Shared stubs (same pattern as test_killswitch.py)
# ---------------------------------------------------------------------------

def _install_shared_stubs() -> None:
    for name in [
        "shared", "shared.config", "shared.config.settings",
        "shared.logging", "shared.logging.logger",
        "shared.utils", "shared.utils.helpers",
    ]:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    class _AWSConfig:
        dynamodb_table_sessions = "quantembrace-sessions"
        dynamodb_table_orders = "quantembrace-orders"
        region = "ap-south-1"

    class _ZerodhaConfig:
        def api_key(self): ...
        def api_secret(self): ...
        def access_token(self): ...

        class _Secret:
            def __init__(self, val): self._val = val
            def get_secret_value(self): return self._val

        api_key = _Secret("test_api_key")
        api_secret = _Secret("test_api_secret")
        access_token = _Secret("")

    class _AppSettings:
        aws = _AWSConfig()
        zerodha = _ZerodhaConfig()
        environment = "test"

    def get_settings():
        return _AppSettings()

    sys.modules["shared.config.settings"].get_settings = get_settings
    sys.modules["shared.config.settings"].AppSettings = _AppSettings
    sys.modules["shared.logging.logger"].get_logger = lambda *a, **k: __import__("logging").getLogger("test")

    _NOW = datetime(2025, 10, 1, 10, 0, 0, tzinfo=timezone.utc)  # after 02:00 UTC
    sys.modules["shared.utils.helpers"].utc_now = lambda: _NOW
    sys.modules["shared.utils.helpers"].utc_iso = lambda: _NOW.isoformat()

    # Wire sub-packages
    sys.modules["shared"].config = sys.modules["shared.config"]
    sys.modules["shared"].logging = sys.modules["shared.logging"]
    sys.modules["shared"].utils = sys.modules["shared.utils"]
    sys.modules["shared.config"].settings = sys.modules["shared.config.settings"]
    sys.modules["shared.logging"].logger = sys.modules["shared.logging.logger"]
    sys.modules["shared.utils"].helpers = sys.modules["shared.utils.helpers"]


_install_shared_stubs()

# Stub kiteconnect before importing auth module
_kite_mod = types.ModuleType("kiteconnect")
_MockKiteConnect = MagicMock()
_kite_mod.KiteConnect = _MockKiteConnect
sys.modules["kiteconnect"] = _kite_mod

from execution_engine.auth.zerodha_auth import (  # noqa: E402
    TokenExpiredError,
    ZerodhaTokenManager,
    _TOKEN_EXPIRY_UTC_HOUR,
    _TOKEN_PK,
    _TOKEN_SK,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW_UTC = datetime(2025, 10, 1, 10, 0, 0, tzinfo=timezone.utc)   # 10:00 UTC = after 02:00
_BEFORE_BOUNDARY = datetime(2025, 10, 1, 1, 0, 0, tzinfo=timezone.utc)  # 01:00 UTC = before 02:00
_EXPIRES_AT = datetime(2025, 10, 2, 2, 0, 0, tzinfo=timezone.utc)  # tomorrow at 02:00 UTC


def _dynamo_with_token(
    token: str = "valid_token_abc",
    expires_at: str = "2025-10-02T02:00:00+00:00",
) -> MagicMock:
    m = MagicMock()
    m.get_item.return_value = {
        "Item": {
            "access_token": {"S": token},
            "api_key": {"S": "test_api_key"},
            "expires_at": {"S": expires_at},
        }
    }
    m.put_item.return_value = {}
    return m


def _dynamo_empty() -> MagicMock:
    m = MagicMock()
    m.get_item.return_value = {"Item": None}
    m.put_item.return_value = {}
    return m


def _secrets_with_creds(
    api_key: str = "sm_api_key",
    api_secret: str = "sm_api_secret",
) -> MagicMock:
    m = MagicMock()
    m.get_secret_value.return_value = {
        "SecretString": json.dumps({"api_key": api_key, "api_secret": api_secret})
    }
    return m


def _make_tm(dynamo=None, secrets=None) -> ZerodhaTokenManager:
    return ZerodhaTokenManager(
        dynamo_client=dynamo or _dynamo_empty(),
        secrets_client=secrets,
    )


# ---------------------------------------------------------------------------
# TestLoadCredentials
# ---------------------------------------------------------------------------


class TestLoadCredentials:

    @pytest.mark.asyncio
    async def test_loads_from_secrets_manager(self):
        secrets = _secrets_with_creds("sm_key", "sm_secret")
        tm = _make_tm(secrets=secrets)
        key, secret = await tm.load_credentials()
        assert key == "sm_key"
        assert secret == "sm_secret"

    @pytest.mark.asyncio
    async def test_falls_back_to_env_when_sm_unavailable(self):
        secrets = MagicMock()
        secrets.get_secret_value.side_effect = Exception("SM unavailable")
        tm = _make_tm(secrets=secrets)
        key, secret = await tm.load_credentials()
        # Falls back to settings stubs: "test_api_key" / "test_api_secret"
        assert key == "test_api_key"
        assert secret == "test_api_secret"

    @pytest.mark.asyncio
    async def test_caches_in_memory_after_first_load(self):
        secrets = _secrets_with_creds("cached_key", "cached_secret")
        tm = _make_tm(secrets=secrets)
        await tm.load_credentials()
        secrets.get_secret_value.reset_mock()
        # Second call should use cache — SM not called again
        key, _ = await tm.load_credentials()
        assert key == "cached_key"
        secrets.get_secret_value.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_when_no_credentials(self):
        secrets = MagicMock()
        secrets.get_secret_value.side_effect = Exception("SM down")

        # Patch settings to return empty key/secret
        from shared.config.settings import get_settings
        orig_settings = get_settings()

        class _EmptyZerodha:
            class _Empty:
                def get_secret_value(self): return ""
            api_key = _Empty()
            api_secret = _Empty()
            access_token = _Empty()

        class _BadSettings:
            aws = orig_settings.aws
            zerodha = _EmptyZerodha()
            environment = "test"

        tm = ZerodhaTokenManager(
            dynamo_client=_dynamo_empty(),
            secrets_client=secrets,
            settings=_BadSettings(),
        )
        with pytest.raises(ValueError, match="credentials not found"):
            await tm.load_credentials()


# ---------------------------------------------------------------------------
# TestGetValidToken
# ---------------------------------------------------------------------------


class TestGetValidToken:

    @pytest.mark.asyncio
    async def test_returns_token_when_valid(self):
        dynamo = _dynamo_with_token("good_token", "2025-10-02T02:00:00+00:00")
        tm = _make_tm(dynamo=dynamo)
        token = await tm.get_valid_token()
        assert token == "good_token"

    @pytest.mark.asyncio
    async def test_raises_when_token_expired(self):
        # expires_at is in the past relative to _NOW_UTC (10:00 UTC 2025-10-01)
        expired_at = "2025-09-30T02:00:00+00:00"
        dynamo = _dynamo_with_token("stale_token", expired_at)
        tm = _make_tm(dynamo=dynamo)
        with pytest.raises(TokenExpiredError, match="expired"):
            await tm.get_valid_token()

    @pytest.mark.asyncio
    async def test_raises_when_no_token_in_dynamo(self):
        tm = _make_tm(dynamo=_dynamo_empty())
        with pytest.raises(TokenExpiredError, match="No Zerodha access token"):
            await tm.get_valid_token()

    @pytest.mark.asyncio
    async def test_returns_cached_token_without_dynamo_call(self):
        dynamo = _dynamo_with_token("cached_token", "2025-10-02T02:00:00+00:00")
        tm = _make_tm(dynamo=dynamo)
        # Populate the in-memory cache manually
        tm._cached_token = "in_memory_token"
        tm._cached_expires_at = datetime(2025, 10, 2, 2, 0, 0, tzinfo=timezone.utc)
        token = await tm.get_valid_token()
        assert token == "in_memory_token"
        dynamo.get_item.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_through_cache_when_expired(self):
        # Cache is stale → should fall through to DynamoDB
        dynamo = _dynamo_with_token("fresh_db_token", "2025-10-02T02:00:00+00:00")
        tm = _make_tm(dynamo=dynamo)
        tm._cached_token = "stale_cache"
        tm._cached_expires_at = datetime(2025, 9, 30, 2, 0, 0, tzinfo=timezone.utc)  # past
        token = await tm.get_valid_token()
        assert token == "fresh_db_token"


# ---------------------------------------------------------------------------
# TestExchangeRequestToken
# ---------------------------------------------------------------------------


class TestExchangeRequestToken:

    @pytest.mark.asyncio
    async def test_success_stores_token_and_updates_cache(self):
        dynamo = _dynamo_empty()
        secrets = _secrets_with_creds()

        # Mock kiteconnect.KiteConnect.generate_session
        mock_kite_instance = MagicMock()
        mock_kite_instance.generate_session.return_value = {"access_token": "new_access_token"}
        _MockKiteConnect.return_value = mock_kite_instance

        tm = _make_tm(dynamo=dynamo, secrets=secrets)
        token = await tm.exchange_request_token("req_token_123")

        assert token == "new_access_token"
        assert tm._cached_token == "new_access_token"
        assert tm._cached_expires_at is not None
        dynamo.put_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_dynamo_item_has_correct_keys(self):
        dynamo = _dynamo_empty()
        secrets = _secrets_with_creds()

        mock_kite_instance = MagicMock()
        mock_kite_instance.generate_session.return_value = {"access_token": "stored_token"}
        _MockKiteConnect.return_value = mock_kite_instance

        tm = _make_tm(dynamo=dynamo, secrets=secrets)
        await tm.exchange_request_token("req_tok")

        item = dynamo.put_item.call_args[1]["Item"]
        assert item["PK"]["S"] == _TOKEN_PK
        assert item["SK"]["S"] == _TOKEN_SK
        assert item["access_token"]["S"] == "stored_token"
        assert "ttl" in item
        assert "expires_at" in item
        assert int(item["ttl"]["N"]) > 0

    @pytest.mark.asyncio
    async def test_raises_runtime_error_on_kite_failure(self):
        secrets = _secrets_with_creds()
        mock_kite_instance = MagicMock()
        mock_kite_instance.generate_session.side_effect = Exception("Invalid request_token")
        _MockKiteConnect.return_value = mock_kite_instance

        tm = _make_tm(secrets=secrets)
        with pytest.raises(RuntimeError, match="Token exchange failed"):
            await tm.exchange_request_token("bad_token")


# ---------------------------------------------------------------------------
# TestStoreToken
# ---------------------------------------------------------------------------


class TestStoreToken:

    @pytest.mark.asyncio
    async def test_persists_to_dynamodb(self):
        dynamo = _dynamo_empty()
        tm = _make_tm(dynamo=dynamo)
        expires = datetime(2025, 10, 2, 2, 0, 0, tzinfo=timezone.utc)
        await tm.store_token("tok_123", "api_key_x", expires)

        dynamo.put_item.assert_called_once()
        item = dynamo.put_item.call_args[1]["Item"]
        assert item["access_token"]["S"] == "tok_123"
        assert item["api_key"]["S"] == "api_key_x"
        ttl_val = int(item["ttl"]["N"])
        assert ttl_val == int(expires.timestamp())

    @pytest.mark.asyncio
    async def test_no_op_without_dynamo_client(self):
        tm = ZerodhaTokenManager(dynamo_client=None)
        # Must not raise
        await tm.store_token("tok", "key")


# ---------------------------------------------------------------------------
# TestIsTokenValid
# ---------------------------------------------------------------------------


class TestIsTokenValid:

    def test_returns_false_with_no_cache(self):
        tm = _make_tm()
        assert tm.is_token_valid() is False

    def test_returns_true_with_valid_cache(self):
        tm = _make_tm()
        tm._cached_token = "tok"
        tm._cached_expires_at = datetime(2025, 10, 2, 2, 0, tzinfo=timezone.utc)  # future
        assert tm.is_token_valid() is True

    def test_returns_false_with_expired_cache(self):
        tm = _make_tm()
        tm._cached_token = "tok"
        tm._cached_expires_at = datetime(2025, 9, 30, 2, 0, tzinfo=timezone.utc)  # past
        assert tm.is_token_valid() is False


# ---------------------------------------------------------------------------
# TestNextExpiryUtc
# ---------------------------------------------------------------------------


class TestNextExpiryUtc:

    def test_before_boundary_returns_same_day(self):
        # Current time 01:00 UTC → boundary is today at 02:00 UTC
        with patch("execution_engine.auth.zerodha_auth.utc_now",
                   return_value=_BEFORE_BOUNDARY):
            expiry = ZerodhaTokenManager._next_expiry_utc()
        assert expiry.hour == _TOKEN_EXPIRY_UTC_HOUR
        assert expiry.date() == _BEFORE_BOUNDARY.date()

    def test_after_boundary_returns_next_day(self):
        # Current time 10:00 UTC → boundary is tomorrow at 02:00 UTC
        with patch("execution_engine.auth.zerodha_auth.utc_now",
                   return_value=_NOW_UTC):
            expiry = ZerodhaTokenManager._next_expiry_utc()
        assert expiry.hour == _TOKEN_EXPIRY_UTC_HOUR
        expected_date = (_NOW_UTC + timedelta(days=1)).date()
        assert expiry.date() == expected_date

    def test_expiry_is_timezone_aware(self):
        with patch("execution_engine.auth.zerodha_auth.utc_now",
                   return_value=_NOW_UTC):
            expiry = ZerodhaTokenManager._next_expiry_utc()
        assert expiry.tzinfo is not None


# ---------------------------------------------------------------------------
# TestZerodhaBrokerClientConnect
# ---------------------------------------------------------------------------


class TestZerodhaBrokerClientConnect:
    """Integration-style tests for ZerodhaBrokerClient.connect() with the token manager."""

    def _make_client(self, dynamo=None, secrets=None):
        from execution_engine.brokers.zerodha_broker import ZerodhaBrokerClient
        return ZerodhaBrokerClient(
            dynamo_client=dynamo or _dynamo_empty(),
            secrets_client=secrets,
        )

    @pytest.mark.asyncio
    async def test_connect_with_valid_token_sets_connected(self):
        dynamo = _dynamo_with_token("live_token", "2025-10-02T02:00:00+00:00")
        secrets = _secrets_with_creds()
        mock_kite_inst = MagicMock()
        _MockKiteConnect.return_value = mock_kite_inst

        client = self._make_client(dynamo=dynamo, secrets=secrets)
        await client.connect()

        assert client._connected is True
        assert client.needs_authentication is False
        mock_kite_inst.set_access_token.assert_called_once_with("live_token")

    @pytest.mark.asyncio
    async def test_connect_with_expired_token_enters_needs_auth_mode(self):
        expired_dynamo = _dynamo_with_token("old_token", "2025-09-30T02:00:00+00:00")
        secrets = _secrets_with_creds()
        mock_kite_inst = MagicMock()
        mock_kite_inst.login_url.return_value = "https://kite.zerodha.com/connect/login"
        _MockKiteConnect.return_value = mock_kite_inst

        client = self._make_client(dynamo=expired_dynamo, secrets=secrets)
        await client.connect()

        assert client._connected is False
        assert client.needs_authentication is True

    @pytest.mark.asyncio
    async def test_place_order_raises_when_needs_authentication(self):
        from execution_engine.brokers.base_broker import BrokerAPIError
        from execution_engine.orders.order import OrderRequest, OrderSide

        client = self._make_client()
        client._needs_authentication = True

        order = OrderRequest(
            signal_id="sig1",
            risk_decision_id="risk1",
            symbol="RELIANCE",
            market="NSE",
            side=OrderSide.BUY,
            quantity=10,
        )
        with pytest.raises(BrokerAPIError, match="No valid access token"):
            await client.place_order(order)

    @pytest.mark.asyncio
    async def test_refresh_access_token_updates_connected_state(self):
        secrets = _secrets_with_creds()
        mock_kite_inst = MagicMock()
        mock_kite_inst.generate_session.return_value = {"access_token": "refreshed_token"}
        _MockKiteConnect.return_value = mock_kite_inst

        dynamo = _dynamo_empty()
        client = self._make_client(dynamo=dynamo, secrets=secrets)
        client._kite = mock_kite_inst   # pre-init kite instance
        client._needs_authentication = True

        new_token = await client.refresh_access_token("fresh_req_token")

        assert new_token == "refreshed_token"
        assert client._connected is True
        assert client.needs_authentication is False
        mock_kite_inst.set_access_token.assert_called_with("refreshed_token")
