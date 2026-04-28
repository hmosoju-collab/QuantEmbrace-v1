#!/usr/bin/env python3
"""
Kill Switch CLI — operator tool for manual kill switch control.

Usage::

    # Check current state
    python scripts/kill_switch_cli.py status

    # Activate (halt all trading)
    python scripts/kill_switch_cli.py activate --reason "Suspected runaway strategy"

    # Deactivate (resume trading — requires explicit confirmation)
    python scripts/kill_switch_cli.py deactivate

Environment variables required:
    AWS_REGION          — AWS region (e.g. us-east-1)
    ENVIRONMENT         — Runtime environment (dev / staging / prod)
    DYNAMODB_TABLE_PREFIX — DynamoDB table prefix (default: quantembrace)
    SNS_KILL_SWITCH_TOPIC_ARN — (optional) SNS topic for notifications

Or set AWS credentials via the standard AWS CLI profile / instance role.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Ensure the services directory is on the path when run directly
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from shared.config.settings import get_settings
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="cli")

_REQUIRED_CONFIRM = "I confirm trading should resume"
_BANNER_ON = (
    "\n"
    "╔══════════════════════════════════════════════╗\n"
    "║  ⛔  KILL SWITCH  —  TRADING IS HALTED  ⛔   ║\n"
    "╚══════════════════════════════════════════════╝"
)
_BANNER_OFF = (
    "\n"
    "╔══════════════════════════════════════════════╗\n"
    "║  ✅  KILL SWITCH  —  TRADING IS ACTIVE  ✅   ║\n"
    "╚══════════════════════════════════════════════╝"
)


def _build_kill_switch() -> "KillSwitch":
    """Instantiate a KillSwitch with live AWS clients."""
    import boto3
    from risk_engine.killswitch.killswitch import KillSwitch

    settings = get_settings()
    region = settings.aws.region

    dynamo = boto3.client("dynamodb", region_name=region)
    sns = boto3.client("sns", region_name=region)
    topic_arn = os.environ.get(
        "SNS_KILL_SWITCH_TOPIC_ARN",
        getattr(settings.aws, "sns_kill_switch_topic_arn", ""),
    )

    return KillSwitch(
        dynamo_client=dynamo,
        sns_client=sns,
        sns_topic_arn=topic_arn,
        settings=settings,
    )


async def cmd_status(args: argparse.Namespace) -> int:
    """Print the current kill switch state."""
    ks = _build_kill_switch()
    await ks.load_state()
    status = ks.get_status()

    if status["active"]:
        print(_BANNER_ON)
        print(f"\n  Reason      : {status['reason']}")
        print(f"  Activated at: {status['activated_at']}")
        print(f"  Activated by: {status['activated_by']}")
    else:
        print(_BANNER_OFF)
        print("\n  Trading is currently enabled.")

    print()
    return 0


async def cmd_activate(args: argparse.Namespace) -> int:
    """Activate the kill switch."""
    reason = args.reason or "Manual activation via CLI"
    activated_by = args.by or os.environ.get("USER", "operator")

    ks = _build_kill_switch()
    await ks.load_state()

    if ks.active:
        print(f"\n[WARN] Kill switch is already active.")
        print(f"       Reason: {ks.reason}")
        print(f"       No action taken.\n")
        return 0

    # Confirm with operator before activating
    env = os.environ.get("ENVIRONMENT", "unknown").upper()
    print(f"\n{'='*52}")
    print(f"  ACTIVATING KILL SWITCH — {env} ENVIRONMENT")
    print(f"  This will halt ALL trading immediately.")
    print(f"  Reason: {reason}")
    print(f"{'='*52}")

    if not args.yes:
        confirm = input("\n  Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            print("  Aborted.\n")
            return 1

    await ks.activate(reason=reason, activated_by=activated_by)
    status = ks.get_status()

    print(_BANNER_ON)
    print(f"\n  Kill switch activated successfully.")
    print(f"  Reason      : {status['reason']}")
    print(f"  Activated at: {status['activated_at']}")
    print(f"  Activated by: {status['activated_by']}")
    print()
    return 0


async def cmd_deactivate(args: argparse.Namespace) -> int:
    """Deactivate the kill switch after explicit confirmation."""
    deactivated_by = args.by or os.environ.get("USER", "operator")

    ks = _build_kill_switch()
    await ks.load_state()

    if not ks.active:
        print("\n[INFO] Kill switch is already inactive — trading is enabled.")
        print("       No action taken.\n")
        return 0

    print(f"\n{'='*52}")
    print(f"  DEACTIVATING KILL SWITCH")
    print(f"  Was active since : {ks.activated_at}")
    print(f"  Reason           : {ks.reason}")
    print(f"\n  ⚠  This will RESUME live trading.")
    print(f"{'='*52}")
    print(f"\n  To confirm, type exactly:")
    print(f"  >>> {_REQUIRED_CONFIRM}\n")

    if args.yes:
        confirmation = _REQUIRED_CONFIRM
    else:
        confirmation = input("  Your confirmation: ").strip()

    if confirmation != _REQUIRED_CONFIRM:
        print("\n  Confirmation mismatch — kill switch NOT deactivated.\n")
        return 1

    await ks.deactivate(deactivated_by=deactivated_by)

    print(_BANNER_OFF)
    print(f"\n  Kill switch deactivated by: {deactivated_by}")
    print(f"  Trading has resumed.\n")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kill-switch",
        description="QuantEmbrace Kill Switch CLI — manual operator control",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- status ---
    sub.add_parser("status", help="Show current kill switch state")

    # --- activate ---
    p_activate = sub.add_parser("activate", help="Activate kill switch (halt trading)")
    p_activate.add_argument(
        "--reason",
        default=None,
        help='Reason for activation (e.g. "Suspected runaway strategy")',
    )
    p_activate.add_argument(
        "--by",
        default=None,
        help="Operator identifier (defaults to $USER)",
    )
    p_activate.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompt",
    )

    # --- deactivate ---
    p_deactivate = sub.add_parser("deactivate", help="Deactivate kill switch (resume trading)")
    p_deactivate.add_argument(
        "--by",
        default=None,
        help="Operator identifier (defaults to $USER)",
    )
    p_deactivate.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompt (auto-supplies required string)",
    )

    args = parser.parse_args()

    handlers = {
        "status": cmd_status,
        "activate": cmd_activate,
        "deactivate": cmd_deactivate,
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
