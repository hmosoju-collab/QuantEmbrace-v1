#!/usr/bin/env python3
"""
Zerodha Daily Login CLI — operator tool for the daily Kite Connect token refresh.

Zerodha Kite Connect tokens expire at ~07:30 IST (~02:00 UTC) every day.
This script must be run each morning before market open to obtain a fresh
access token that is stored in DynamoDB and picked up by the execution engine.

Workflow::

    python scripts/zerodha_login.py

    1. The script prints the Kite login URL.
    2. Open the URL in your browser and log in with your Zerodha credentials.
    3. After login, Zerodha redirects to your configured callback URL with a
       ``request_token`` parameter. Paste it into the prompt.
    4. The script exchanges the token, stores it in DynamoDB, and confirms.
    5. The execution engine's ZerodhaBrokerClient will pick up the new token
       on its next connect() call (or immediately on restart).

Environment variables required:
    AWS_REGION              — AWS region (e.g. ap-south-1)
    ENVIRONMENT             — Runtime environment (dev / staging / prod)
    DYNAMODB_TABLE_SESSIONS — DynamoDB table for session tokens
                              (default: quantembrace-sessions)

AWS credentials via the standard profile / instance role.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from shared.config.settings import get_settings
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="cli")

_SEPARATOR = "=" * 60


def _build_token_manager() -> "ZerodhaTokenManager":
    """Instantiate a ZerodhaTokenManager with live AWS clients."""
    import boto3
    from execution_engine.auth.zerodha_auth import ZerodhaTokenManager

    settings = get_settings()
    region = settings.aws.region
    table = settings.aws.dynamodb_table_sessions

    dynamo = boto3.client("dynamodb", region_name=region)
    secrets = boto3.client("secretsmanager", region_name=region)

    return ZerodhaTokenManager(
        dynamo_client=dynamo,
        secrets_client=secrets,
        table_name=table,
        settings=settings,
    )


async def cmd_login(args: argparse.Namespace) -> int:
    """Full interactive daily login flow."""
    tm = _build_token_manager()

    print(f"\n{_SEPARATOR}")
    print("  Zerodha Kite Connect — Daily Token Refresh")
    print(_SEPARATOR)

    env = os.environ.get("ENVIRONMENT", "dev").upper()
    print(f"\n  Environment : {env}")
    print(f"  Region      : {get_settings().aws.region}")
    print(f"  Table       : {get_settings().aws.dynamodb_table_sessions}")

    # Step 1: Generate and display the login URL
    try:
        login_url = await tm.get_login_url()
    except Exception as exc:
        print(f"\n[ERROR] Could not generate login URL: {exc}")
        print("  Check that ZERODHA_API_KEY is set correctly.\n")
        return 1

    print(f"\n{'─'*60}")
    print("  STEP 1 — Open this URL in your browser and log in:")
    print(f"{'─'*60}")
    print(f"\n  {login_url}\n")
    print(f"{'─'*60}")
    print(
        "  STEP 2 — After login, Kite redirects to your callback URL.\n"
        "  The URL will look like:\n"
        "    https://your-callback.example.com/callback?request_token=XXXX&action=login&status=success\n"
        "  Copy ONLY the request_token value (the XXXX part)."
    )
    print(f"{'─'*60}\n")

    # Step 2: Accept the request_token
    if args.request_token:
        request_token = args.request_token.strip()
        print(f"  Using provided request_token: {request_token[:8]}...")
    else:
        try:
            request_token = input("  Paste the request_token here: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Aborted.\n")
            return 1

    if not request_token:
        print("\n[ERROR] No request_token provided.\n")
        return 1

    # Step 3: Exchange the token
    print(f"\n  Exchanging request_token for access_token...")
    try:
        access_token = await tm.exchange_request_token(request_token)
    except Exception as exc:
        print(f"\n[ERROR] Token exchange failed: {exc}")
        print(
            "  Common causes:\n"
            "    - The request_token has already been used (one-time use only).\n"
            "    - The request_token has expired (valid for ~5 minutes).\n"
            "    - API key / secret mismatch.\n"
        )
        return 1

    from execution_engine.auth.zerodha_auth import ZerodhaTokenManager
    expiry = ZerodhaTokenManager._next_expiry_utc()

    print(f"\n{'─'*60}")
    print("  ✅  Login successful!")
    print(f"{'─'*60}")
    print(f"  Token stored in DynamoDB.")
    print(f"  Valid until : {expiry.strftime('%Y-%m-%d %H:%M')} UTC (~07:30 IST)")
    print(f"  Token prefix: {access_token[:8]}...")
    print(
        "\n  The execution engine will pick up the new token automatically.\n"
        "  If the service is already running, restart it to apply the token.\n"
    )
    print(f"{_SEPARATOR}\n")
    return 0


async def cmd_status(args: argparse.Namespace) -> int:
    """Show the current token status from DynamoDB."""
    tm = _build_token_manager()

    print(f"\n{_SEPARATOR}")
    print("  Zerodha Token Status")
    print(_SEPARATOR)

    try:
        token = await tm.get_valid_token()
        expiry = tm._cached_expires_at
        print(f"\n  Status      : ✅ VALID")
        print(f"  Token prefix: {token[:8]}...")
        print(f"  Expires at  : {expiry.strftime('%Y-%m-%d %H:%M') if expiry else 'unknown'} UTC")
    except Exception as exc:
        print(f"\n  Status      : ❌ EXPIRED / MISSING")
        print(f"  Reason      : {exc}")
        print(f"\n  Run 'python scripts/zerodha_login.py login' to authenticate.")

    print(f"\n{_SEPARATOR}\n")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="zerodha-login",
        description="Zerodha daily token refresh CLI",
    )
    sub = parser.add_subparsers(dest="command")

    # Default: no subcommand → run interactive login
    p_login = sub.add_parser("login", help="Perform interactive daily login (default)")
    p_login.add_argument(
        "--request-token",
        default=None,
        help="Provide request_token directly (skip interactive prompt)",
    )

    sub.add_parser("status", help="Check current token status in DynamoDB")

    args = parser.parse_args()

    # Default to 'login' if no subcommand given
    if args.command is None:
        args.command = "login"
        args.request_token = None

    handlers = {
        "login": cmd_login,
        "status": cmd_status,
    }

    try:
        exit_code = asyncio.run(handlers[args.command](args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
    except Exception as exc:
        print(f"\n[ERROR] {exc}")
        sys.exit(2)


if __name__ == "__main__":
    main()
