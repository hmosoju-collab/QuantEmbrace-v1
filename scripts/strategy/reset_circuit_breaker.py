#!/usr/bin/env python3
"""
scripts/strategy/reset_circuit_breaker.py — Force-reset a strategy's circuit breaker.

Sets ``circuit_breaker_reset=True`` in the strategy-config DynamoDB table.
The running strategy_engine picks this up within 60 seconds (one StrategyConfigLoader
refresh cycle) and transitions the circuit from OPEN → CLOSED.

After the reset is applied, StrategyConfigLoader automatically writes
``circuit_breaker_reset=False`` back to DynamoDB so subsequent refreshes
do not re-reset.

Use this when:
    - A strategy's circuit breaker is OPEN due to a transient error spike
    - The underlying issue has been resolved (e.g., broker API recovered)
    - You need to resume signal generation immediately without restarting the service

Safety guard:
    The script always shows the current circuit state before resetting.
    You must confirm before the reset is written if the circuit is CLOSED
    (resetting a closed circuit is a no-op but may indicate operator confusion).

Usage
-----
  # Reset a specific strategy's circuit in production:
  python scripts/strategy/reset_circuit_breaker.py nse_orb_15m --env production

  # List all circuit breaker states first:
  python scripts/strategy/reset_circuit_breaker.py --list --env production

  # Reset without confirmation prompt (for automation/CI):
  python scripts/strategy/reset_circuit_breaker.py nse_scalp_1m --env staging --yes

Authentication
--------------
  IAM-based via boto3. Requires dynamodb:UpdateItem on {prefix}-strategy-config.

Requirements
------------
  pip install boto3~=1.34
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Optional

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("ERROR: boto3 not installed. Run: pip install boto3~=1.34")
    sys.exit(1)

_DEFAULT_PROJECT_PREFIX = os.environ.get("DYNAMODB_TABLE_PREFIX", "quantembrace")
_PK_PREFIX = "STRATEGY#"
_SK_PREFIX  = "CONFIG#"


# ── DynamoDB helpers ──────────────────────────────────────────────────────────

def _get_table(env: str, prefix: str, profile: Optional[str] = None):
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    dynamodb = session.resource("dynamodb")
    table_name = f"{prefix}-strategy-config"
    return dynamodb.Table(table_name), table_name


def _pk(name: str) -> str:
    return f"{_PK_PREFIX}{name}"


def _sk(env: str) -> str:
    return f"{_SK_PREFIX}{env}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── List circuit breaker states ───────────────────────────────────────────────

def list_circuit_states(table, env: str) -> int:
    """Print circuit breaker state for all configured strategies."""
    response = table.scan()
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    env_items = [i for i in items if i.get("SK") == _sk(env)]
    if not env_items:
        print(f"No config rows found for env={env}")
        return 0

    print(f"\nCircuit breaker states — env={env}")
    print("-" * 80)
    print(f"  {'STRATEGY':<30}  {'STATE':<10}  {'ENABLED':<8}  OPENED_AT")
    print("-" * 80)

    has_open = False
    for item in sorted(env_items, key=lambda x: x.get("PK", "")):
        name  = item["PK"].replace(_PK_PREFIX, "")
        state = item.get("circuit_breaker_state", "CLOSED")
        enabled = "YES" if item.get("enabled", True) else "NO"
        opened_at = item.get("circuit_breaker_opened_at", "—")
        marker = "  ⚠" if state == "OPEN" else ("  ·" if state == "HALF_OPEN" else "  ✓")
        print(f"{marker} {name:<30}  {state:<10}  {enabled:<8}  {opened_at}")
        if state in ("OPEN", "HALF_OPEN"):
            has_open = True

    print()
    if has_open:
        print("  ⚠  Some circuits are OPEN or HALF_OPEN. Use this script to reset if needed.")
    else:
        print("  ✓  All circuits CLOSED — no action needed.")
    print()
    return 0


# ── Reset a single circuit breaker ────────────────────────────────────────────

def reset_circuit_breaker(
    table,
    strategy_name: str,
    env: str,
    operator: str,
    yes: bool = False,
) -> int:
    """Set circuit_breaker_reset=True for the given strategy."""
    # Read current state
    response = table.get_item(Key={"PK": _pk(strategy_name), "SK": _sk(env)})
    item = response.get("Item")

    if item is None:
        print(
            f"No config row for strategy={strategy_name!r} env={env!r}.\n"
            f"Run 'python scripts/strategy/config.py seed --env {env}' to create default rows."
        )
        return 1

    current_state = item.get("circuit_breaker_state", "CLOSED")
    enabled       = item.get("enabled", True)
    paper_trade   = item.get("paper_trade", True)
    reset_pending = item.get("circuit_breaker_reset", False)

    print(f"\nCurrent state for '{strategy_name}' [{env}]:")
    print(f"  circuit_breaker_state   : {current_state}")
    print(f"  enabled                 : {enabled}")
    print(f"  paper_trade             : {paper_trade}")
    if reset_pending:
        print(f"  circuit_breaker_reset   : True  ← reset already pending, not yet applied by service")

    if current_state == "CLOSED" and not yes:
        confirm = input(
            f"\nCircuit is already CLOSED. Are you sure you want to force-reset? [y/N] "
        )
        if confirm.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 0

    if not yes and current_state in ("OPEN", "HALF_OPEN"):
        confirm = input(
            f"\nSet circuit_breaker_reset=True for '{strategy_name}' in {env}?\n"
            f"The service will transition to CLOSED within 60s. [Y/n] "
        )
        if confirm.strip().lower() in ("n", "no"):
            print("Aborted.")
            return 0

    # Write circuit_breaker_reset=True
    try:
        table.update_item(
            Key={"PK": _pk(strategy_name), "SK": _sk(env)},
            UpdateExpression=(
                "SET circuit_breaker_reset = :true, "
                "updated_at = :ts, updated_by = :actor"
            ),
            ExpressionAttributeValues={
                ":true":  True,
                ":ts":    _now_iso(),
                ":actor": operator,
            },
        )
    except ClientError as e:
        print(f"ERROR: DynamoDB update failed: {e.response['Error']['Message']}")
        return 1

    print(f"\n✓  circuit_breaker_reset=True written for '{strategy_name}' [{env}]")
    print(f"   The strategy_engine will apply the reset within 60 seconds.")
    print(f"   After applying, the service automatically clears the reset flag.")
    print(f"   Watch CloudWatch logs for: strategy_runner.circuit_breaker_reset_manual")
    return 0


# ── Argument parsing ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/strategy/reset_circuit_breaker.py",
        description="Force-reset a QuantEmbrace strategy circuit breaker via DynamoDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "strategy",
        nargs="?",
        help="Strategy name to reset (e.g. nse_orb_15m). Omit when using --list.",
    )
    parser.add_argument(
        "--env",
        default=os.environ.get("QE_ENVIRONMENT", "staging"),
        help="Environment (dev/staging/production). Default: QE_ENVIRONMENT env var or 'staging'",
    )
    parser.add_argument(
        "--prefix",
        default=_DEFAULT_PROJECT_PREFIX,
        help=f"DynamoDB table prefix. Default: DYNAMODB_TABLE_PREFIX or '{_DEFAULT_PROJECT_PREFIX}'",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("AWS_PROFILE"),
        help="AWS CLI profile name",
    )
    parser.add_argument(
        "--operator",
        default=os.environ.get("USER", "cli-operator"),
        help="Operator identity written to updated_by",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_mode",
        help="List all circuit breaker states instead of resetting",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompts (for automation)",
    )
    return parser.parse_args()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    args = _parse_args()

    if not args.list_mode and not args.strategy:
        print("ERROR: Provide a strategy name to reset, or use --list to view circuit states.")
        print("Usage: python scripts/strategy/reset_circuit_breaker.py <strategy> --env <env>")
        print("       python scripts/strategy/reset_circuit_breaker.py --list --env <env>")
        return 1

    table, table_name = _get_table(args.env, args.prefix, args.profile)
    print(f"Table: {table_name}  |  env={args.env}  |  operator={args.operator}")

    try:
        if args.list_mode:
            return list_circuit_states(table, args.env)
        else:
            return reset_circuit_breaker(
                table,
                args.strategy,
                args.env,
                args.operator,
                yes=args.yes,
            )
    except ClientError as e:
        print(f"ERROR: AWS error: {e.response['Error']['Code']}: {e.response['Error']['Message']}")
        print(f"  Table: {table_name}")
        print("  Check IAM permissions (dynamodb:UpdateItem required).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
