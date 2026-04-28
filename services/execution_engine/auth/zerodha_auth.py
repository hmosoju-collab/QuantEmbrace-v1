"""
Zerodha Token Manager — daily authentication lifecycle for Kite Connect.

Zerodha Kite Connect uses a multi-step OAuth-like flow:
    1. Direct the operator's browser to the Kite login URL.
    2. After login, Kite redirects to the configured callback URL with a
       one-time ``request_token`` in the query string.
    3. Exchange the ``request_token`` + ``api_secret`` for an ``access_token``
       via ``KiteConnect.generate_session()``.
    4. The ``access_token`` is valid until ~07:30 IST the following day
       (02:00 UTC). After that, the entire flow must be repeated.

This module manages the full lifecycle:
    - Loading API credentials from AWS Secrets Manager (with env-var fallback).
    - Storing the access token in DynamoDB with a TTL so expired records are
      automatically deleted.
    - Validating token freshness on broker startup.
    - Providing a ``get_login_url()`` helper so the operator knows where to
      point their browser.

Daily operations workflow::

    # Morning, before 07:30 IST — operator runs the CLI
    python scripts/zerodha_login.py

    # CLI prints the login URL, operator opens it, pastes the request_token
    # CLI calls exchange_request_token() → new token stored in DynamoDB
    # Service starts / restarts normally — connect() reads the fresh token
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.utils.helpers import utc_now

logger = get_logger(__name__, service_name="execution_engine")

# DynamoDB keys for the token record
_TOKEN_PK = "ZERODHA#TOKEN"
_TOKEN_SK = "CURRENT"

# Zerodha token expiry: ~07:30 IST = 02:00 UTC daily.
# We use 02:00 UTC as the hard boundary — if current UTC ≥ 02:00, the token
# issued before this time is considered stale.
_TOKEN_EXPIRY_UTC_HOUR = 2   # 02:00 UTC = 07:30 IST

# Secrets Manager secret name template
_SECRET_NAME_TEMPLATE = "quantembrace/{env}/zerodha/api-credentials"


class TokenExpiredError(Exception):
    """
    Raised when no valid Zerodha access token is available.

    The caller should:
        1. Log the error prominently.
        2. Call ``ZerodhaTokenManager.get_login_url()`` and present it to
           the operator so they can complete the daily login.
        3. Do not attempt to place orders until a fresh token is stored.
    """


class ZerodhaTokenManager:
    """
    Manages the Zerodha Kite Connect access token lifecycle.

    Responsibilities:
        - Load API key + secret from Secrets Manager (env-var fallback).
        - Read / write access token in DynamoDB with TTL.
        - Detect token expiry before the broker attempts to use it.
        - Exchange a request_token for a new access_token.
        - Generate the Kite login URL for the operator's daily login.

    The DynamoDB record uses a ``ttl`` attribute (Unix timestamp) so AWS
    automatically purges stale token records — no manual cleanup required.

    DynamoDB schema::

        PK  = "ZERODHA#TOKEN"
        SK  = "CURRENT"
        access_token = <token string>
        api_key      = <api key (for reference)>
        expires_at   = <ISO-8601 UTC timestamp>
        ttl          = <Unix timestamp — DynamoDB auto-expiry>
        created_at   = <ISO-8601 UTC timestamp>
    """

    def __init__(
        self,
        dynamo_client: Any = None,
        secrets_client: Any = None,
        table_name: Optional[str] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._dynamo = dynamo_client
        self._secrets = secrets_client
        self._table_name = table_name or self._settings.aws.dynamodb_table_sessions

        # Cached credentials (loaded lazily)
        self._api_key: Optional[str] = None
        self._api_secret: Optional[str] = None

        # Cached in-memory token (avoids repeated DynamoDB reads within a session)
        self._cached_token: Optional[str] = None
        self._cached_expires_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def load_credentials(self) -> tuple[str, str]:
        """
        Load Zerodha API key and secret.

        Resolution order:
            1. In-memory cache (already loaded this session).
            2. AWS Secrets Manager: ``quantembrace/{env}/zerodha/api-credentials``.
            3. Settings / environment variables: ``ZERODHA_API_KEY`` /
               ``ZERODHA_API_SECRET``.

        Returns:
            ``(api_key, api_secret)`` tuple.

        Raises:
            ValueError: If credentials cannot be resolved.
        """
        if self._api_key and self._api_secret:
            return self._api_key, self._api_secret

        # --- Try Secrets Manager first ---
        if self._secrets is not None:
            try:
                env = getattr(self._settings, "environment", "dev")
                secret_name = _SECRET_NAME_TEMPLATE.format(env=env)
                response = await asyncio.to_thread(
                    self._secrets.get_secret_value, SecretId=secret_name
                )
                secret = json.loads(response["SecretString"])
                self._api_key = secret["api_key"]
                self._api_secret = secret["api_secret"]
                logger.info("Zerodha credentials loaded from Secrets Manager")
                return self._api_key, self._api_secret
            except Exception:
                logger.warning(
                    "Secrets Manager unavailable — falling back to env vars for Zerodha credentials"
                )

        # --- Fall back to settings / env vars ---
        try:
            api_key = self._settings.zerodha.api_key.get_secret_value()
            api_secret = self._settings.zerodha.api_secret.get_secret_value()
            if api_key and api_secret:
                self._api_key = api_key
                self._api_secret = api_secret
                logger.debug("Zerodha credentials loaded from environment settings")
                return self._api_key, self._api_secret
        except Exception:
            pass

        raise ValueError(
            "Zerodha API credentials not found. Set ZERODHA_API_KEY and "
            "ZERODHA_API_SECRET, or store them in Secrets Manager at "
            f"'{_SECRET_NAME_TEMPLATE.format(env=getattr(self._settings, 'environment', 'dev'))}'."
        )

    async def get_login_url(self) -> str:
        """
        Return the Kite Connect login URL for the daily operator login.

        The operator must open this URL in a browser, log in with their
        Zerodha credentials, and paste the ``request_token`` from the
        redirect URL back into the CLI.

        Returns:
            Full Kite login URL string.
        """
        try:
            from kiteconnect import KiteConnect  # type: ignore[import]
        except ImportError:
            raise ImportError("kiteconnect package not installed — pip install kiteconnect")

        api_key, _ = await self.load_credentials()
        kite = KiteConnect(api_key=api_key)
        url: str = kite.login_url()
        return url

    async def get_valid_token(self) -> str:
        """
        Return a valid access token, reading from DynamoDB if needed.

        Checks in-memory cache first, then DynamoDB. If the stored token
        is expired (past 02:00 UTC today), raises ``TokenExpiredError``.

        Returns:
            The current valid access token.

        Raises:
            TokenExpiredError: No valid token — operator must log in.
        """
        # --- In-memory cache hit ---
        if self._cached_token and self._cached_expires_at:
            if utc_now() < self._cached_expires_at:
                return self._cached_token
            # Cache is stale — fall through to DynamoDB check
            self._cached_token = None
            self._cached_expires_at = None

        # --- Read from DynamoDB ---
        token, expires_at = await self._load_token_from_dynamo()

        if token is None:
            raise TokenExpiredError(
                "No Zerodha access token found in DynamoDB. "
                "Run 'python scripts/zerodha_login.py' to authenticate."
            )

        if expires_at is not None and utc_now() >= expires_at:
            raise TokenExpiredError(
                f"Zerodha access token expired at {expires_at.isoformat()} UTC. "
                "Run 'python scripts/zerodha_login.py' to re-authenticate."
            )

        # Cache for the current session
        self._cached_token = token
        self._cached_expires_at = expires_at
        logger.debug("Zerodha access token loaded from DynamoDB (expires %s)", expires_at)
        return token

    async def exchange_request_token(self, request_token: str) -> str:
        """
        Exchange a one-time request_token for a new access_token.

        Called after the operator completes the Kite login flow and provides
        the ``request_token`` from the redirect callback URL.

        Args:
            request_token: The one-time request token from the Kite redirect.

        Returns:
            The new access token (also persisted to DynamoDB).

        Raises:
            Exception: If the exchange fails (invalid token, API error, etc.).
        """
        try:
            from kiteconnect import KiteConnect  # type: ignore[import]
        except ImportError:
            raise ImportError("kiteconnect package not installed — pip install kiteconnect")

        api_key, api_secret = await self.load_credentials()

        logger.info("Exchanging Zerodha request_token for access_token")
        kite = KiteConnect(api_key=api_key)

        try:
            data = await asyncio.to_thread(
                kite.generate_session,
                request_token,
                api_secret=api_secret,
            )
        except Exception as exc:
            logger.exception("Zerodha request_token exchange failed")
            raise RuntimeError(f"Token exchange failed: {exc}") from exc

        access_token: str = data["access_token"]
        expires_at = self._next_expiry_utc()

        # Persist to DynamoDB and update in-memory cache
        await self.store_token(
            access_token=access_token,
            api_key=api_key,
            expires_at=expires_at,
        )

        self._cached_token = access_token
        self._cached_expires_at = expires_at

        logger.info(
            "Zerodha access token exchanged and stored (expires %s UTC)",
            expires_at.strftime("%Y-%m-%d %H:%M"),
        )
        return access_token

    async def store_token(
        self,
        access_token: str,
        api_key: str,
        expires_at: Optional[datetime] = None,
    ) -> None:
        """
        Persist an access token to DynamoDB with a TTL for auto-expiry.

        Args:
            access_token: The Zerodha access token to store.
            api_key: The API key associated with this token (for reference).
            expires_at: Expiry datetime (UTC). Defaults to next 02:00 UTC.
        """
        if self._dynamo is None:
            logger.warning("No DynamoDB client — Zerodha token not persisted")
            return

        expiry = expires_at or self._next_expiry_utc()
        ttl = int(expiry.timestamp())
        now_iso = utc_now().isoformat()

        item: dict[str, Any] = {
            "PK": {"S": _TOKEN_PK},
            "SK": {"S": _TOKEN_SK},
            "access_token": {"S": access_token},
            "api_key": {"S": api_key},
            "expires_at": {"S": expiry.isoformat()},
            "ttl": {"N": str(ttl)},
            "created_at": {"S": now_iso},
        }

        try:
            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._table_name,
                Item=item,
            )
            logger.info(
                "Zerodha access token stored in DynamoDB (ttl=%s, expires=%s)",
                ttl,
                expiry.isoformat(),
            )
        except Exception:
            logger.exception("Failed to persist Zerodha access token to DynamoDB")
            raise

    def is_token_valid(self) -> bool:
        """
        Quick in-memory check: is the cached token still valid?

        Returns:
            True if an unexpired token is in memory.
        """
        if self._cached_token and self._cached_expires_at:
            return utc_now() < self._cached_expires_at
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_token_from_dynamo(self) -> tuple[Optional[str], Optional[datetime]]:
        """
        Read the current token record from DynamoDB.

        Returns:
            ``(access_token, expires_at)`` or ``(None, None)`` if not found.
        """
        if self._dynamo is None:
            return None, None

        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._table_name,
                Key={
                    "PK": {"S": _TOKEN_PK},
                    "SK": {"S": _TOKEN_SK},
                },
            )
            item = response.get("Item")
            if not item:
                return None, None

            access_token = item.get("access_token", {}).get("S")
            expires_at_str = item.get("expires_at", {}).get("S", "")
            expires_at: Optional[datetime] = None
            if expires_at_str:
                expires_at = datetime.fromisoformat(expires_at_str)

            return access_token, expires_at

        except Exception:
            logger.exception("Failed to read Zerodha token from DynamoDB")
            return None, None

    @staticmethod
    def _next_expiry_utc() -> datetime:
        """
        Calculate the next token expiry boundary: 02:00 UTC.

        Zerodha tokens expire at ~07:30 IST = 02:00 UTC. We set the TTL
        to the *next* occurrence of 02:00 UTC after the current moment so
        tokens generated close to midnight are not immediately invalidated.

        Examples:
            - Current UTC  10:00 → expires today at  02:00 UTC (next day)
            - Current UTC  01:00 → expires today at  02:00 UTC (same day)
            - Current UTC  02:30 → expires tomorrow at 02:00 UTC

        Returns:
            datetime (UTC, timezone-aware) of the next expiry boundary.
        """
        now = utc_now()
        # Today's boundary
        today_boundary = now.replace(
            hour=_TOKEN_EXPIRY_UTC_HOUR, minute=0, second=0, microsecond=0
        )
        if now < today_boundary:
            return today_boundary
        # Already past today's boundary → use tomorrow's
        return today_boundary + timedelta(days=1)
