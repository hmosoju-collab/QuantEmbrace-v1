#!/usr/bin/env python3
"""
scripts/strategy/config.py — Operator CLI for QuantEmbrace strategy-config DynamoDB table.

Manages the ``{prefix}-strategy-config`` table (ADR-013 §9.1).
Changes are picked up by the running strategy_engine within 60 seconds
(one StrategyConfigLoader refresh cycle) — no restarts required.

Commands
--------
  list        Print all strategy config rows for the environment.
  get         Print a single strategy's config row.
  set         Update one or more config fields for a strategy.
  seed        Write default config rows for all strategies (if no row exists).
  enable      Shortcut: set enabled=True for a strategy.
  disable     Shortcut: set enabled=False for a strategy.
  go-live     Shortcut: set paper_trade=False for a strategy.
  paper       Shortcut: set paper_trade=True for a strategy.

Usage examples
--------------
  # List all strategies in production:
  python scripts/strategy/config.py list --env production

  # Get a single strategy's config:
  python scripts/strategy/config.py get nse_orb_15m --env staging

  # Disable a strategy immediately (takes effect ≤60s):
  python scripts/strategy/config.py disable nse_scalp_1m --env production

  # Promote orb to live (paper_trade=False):
  python scripts/strategy/config.py go-live nse_orb_15m --env production

  # Update multiple fields at once:
  python scripts/strategy/config.py set nse_vwap_reversion \\
      --max-signals-per-day 5 \\
      --cb-consecutive 3 \\
      --env production

  # Seed default rows for all 6 strategies (safe — skips if row already exists):
  python scripts/strategy/config.py seed --env staging

Authentication
--------------
  IAM-based via boto3. Uses instance profile on EC2 or local AWS profile for dev.
  Required permissions: dynamodb:GetItem, dynamodb:PutItem, dynamodb:UpdateItem,
                        dynamodb:Query, dynamodb:Scan on {prefix}-strategy-config.

Requirements
------------
  pip install boto3~=1.34
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import boto3
    from boto3.dynamodb.conditions import Key
    from botocore.exceptions import ClientError
except ImportError:
    print("ERROR: boto3 not installed. Run: pip install boto3~=1.34")
    sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────

_DEFAULT_PROJECT_PREFIX = os.environ.get("DYNAMODB_TABLE_PREFIX", "quantembrace")

# Default strategies registered in StrategyEngineService._register_strategies_from_config()
_KNOWN_STRATEGIES: list[tuple[str, int]] = [
    ("nse_momentum_v1",       20),
    ("us_momentum_v1",        10),
    ("nse_orb_15m",           4),
    ("nse_scalp_1m",          6),
    ("nse_vwap_reversion",    5),
    ("nse_intraday_trend_15m",4),
    ("nse_preclose_momentum", 3),
]

_PK_PREFIX = "STRATEGY#"
_SK_PREFIX  = "CONFIG#"


# ── DynamoDB helpers ──────────────────────────────────────────────────────────

def _get_table(env: str, prefix: str, profile: Optional[str] = None):
    """Return a boto3 DynamoDB Table resource for the strategy-config table."""
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    dynamodb = session.resource("dynamodb")
    table_name = f"{prefix}-strategy-config"
    return dynamodb.Table(table_name), table_name


def _pk(strategy_name: str) -> str:
    return f"{_PK_PREFIX}{strategy_name}"


def _sk(env: str) -> str:
    return f"{_SK_PREFIX}{env}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Command implementations ───────────────────────────────────────────────────

def cmd_list(table, env: str) -> int:
    """List all strategy config rows for the environment."""
    response = table.scan()
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    env_items = [i for i in items if i.get("SK") == _sk(env)]
    if not env_items:
        print(f"No config rows found for env={env}")
        return 0

    print(f"\nStrategy config — env={env}   ({len(env_items)} rows)")
    print("-" * 100)
    print(
        f"{'STRATEGY':<30}  {'ENABLED':<8}  {'PAPER':<7}  "
        f"{'MAX_SIG':<8}  {'CB_CONSEC':<10}  {'CB_RATE':<9}  "
        f"{'CB_STATE':<10}  UPDATED_BY"
    )
    print("-" * 100)

    for item in sorted(env_items, key=lambda x: x.get("PK", "")):
        name = item["PK"].replace(_PK_PREFIX, "")
        print(
            f"{name:<30}  "
            f"{'YES' if item.get('enabled', True) else 'NO':<8}  "
            f"{'YES' if item.get('paper_trade', True) else 'NO':<7}  "
            f"{str(item.get('max_signals_per_day', 0)):<8}  "
            f"{str(item.get('circuit_breaker_threshold_consecutive', 5)):<10}  "
            f"{str(item.get('circuit_breaker_threshold_rate', 10)):<9}  "
            f"{str(item.get('circuit_breaker_state', 'CLOSED')):<10}  "
            f"{item.get('updated_by', '?')}"
        )
    print()
    return 0


def cmd_get(table, strategy_name: str, env: str) -> int:
    """Print a single strategy's full config row."""
    response = table.get_item(Key={"PK": _pk(strategy_name), "SK": _sk(env)})
    item = response.get("Item")
    if item is None:
        print(f"No config row for strategy={strategy_name!r} env={env!r}")
        print("(Strategy will use default config: paper_trade=True, enabled=True)")
        return 0

    print(f"\nStrategy config — {strategy_name}  [{env}]")
    print("-" * 50)
    fields_order = [
        "enabled", "paper_trade", "max_signals_per_day",
        "circuit_breaker_threshold_consecutive", "circuit_breaker_threshold_rate",
        "circuit_breaker_state", "circuit_breaker_opened_at", "circuit_breaker_reset",
        "updated_at", "updated_by",
    ]
    for key in fields_order:
        if key in item:
            print(f"  {key:<45} {item[key]}")
    # Print any remaining fields not in the ordered list
    for key, val in item.items():
        if key not in fields_order and key not in ("PK", "SK"):
            print(f"  {key:<45} {val}")
    print()
    return 0


def cmd_set(
    table,
    strategy_name: str,
    env: str,
    operator: str,
    enabled: Optional[bool] = None,
    paper_trade: Optional[bool] = None,
    max_signals_per_day: Optional[int] = None,
    cb_consecutive: Optional[int] = None,
    cb_rate: Optional[int] = None,
) -> int:
    """Update specified fields. Performs a conditional UpdateItem."""
    updates: dict[str, Any] = {}
    if enabled is not None:
        updates["enabled"] = enabled
    if paper_trade is not None:
        updates["paper_trade"] = paper_trade
    if max_signals_per_day is not None:
        updates["max_signals_per_day"] = max_signals_per_day
    if cb_consecutive is not None:
        updates["circuit_breaker_threshold_consecutive"] = cb_consecutive
    if cb_rate is not None:
        updates["circuit_breaker_threshold_rate"] = cb_rate

    if not updates:
        print("ERROR: No fields specified to update.")
        return 1

    updates["updated_at"] = _now_iso()
    updates["updated_by"] = operator

    # Build UpdateExpression
    set_parts = [f"#{k} = :{k}" for k in updates]
    update_expr = "SET " + ", ".join(set_parts)
    expr_names  = {f"#{k}": k for k in updates}
    expr_values = {f":{k}": v for k, v in updates.items()}

    try:
        table.update_item(
            Key={"PK": _pk(strategy_name), "SK": _sk(env)},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        print(f"ERROR: DynamoDB update failed: {e.response['Error']['Message']}")
        return 1

    print(f"✓  Updated {strategy_name} [{env}]:")
    for k, v in updates.items():
        if k not in ("updated_at", "updated_by"):
            print(f"   {k} = {v}")
    print(f"   (change takes effect within 60s)")
    return 0


def cmd_seed(table, env: str, operator: str) -> int:
    """Write default config rows. Uses ConditionExpression to skip existing rows."""
    created = 0
    skipped = 0

    for strategy_name, max_signals in _KNOWN_STRATEGIES:
        try:
            table.put_item(
                Item={
                    "PK":    _pk(strategy_name),
                    "SK":    _sk(env),
                    "enabled":                               True,
                    "paper_trade":                           True,
                    "max_signals_per_day":                   max_signals,
                    "circuit_breaker_threshold_consecutive": 5,
                    "circuit_breaker_threshold_rate":        10,
                    "circuit_breaker_state":                 "CLOSED",
                    "circuit_breaker_opened_at":             None,
                    "circuit_breaker_reset":                 False,
                    "updated_at":                            _now_iso(),
                    "updated_by":                            operator,
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            print(f"  ✓  Created  {strategy_name}  (max_signals_per_day={max_signals})")
            created += 1
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                print(f"  ·  Exists   {strategy_name}  (no change)")
                skipped += 1
            else:
                print(f"  ✗  FAILED   {strategy_name}: {e.response['Error']['Message']}")

    print(f"\nSeed complete — created={created}  skipped={skipped}")
    return 0


# ── Argument parsing ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/strategy/config.py",
        description="QuantEmbrace strategy-config operator CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--env",
        default=os.environ.get("QE_ENVIRONMENT", "staging"),
        help="Environment (dev/staging/production). Default: QE_ENVIRONMENT env var or 'staging'",
    )
    parser.add_argument(
        "--prefix",
        default=_DEFAULT_PROJECT_PREFIX,
        help=f"DynamoDB table prefix. Default: DYNAMODB_TABLE_PREFIX env var or '{_DEFAULT_PROJECT_PREFIX}'",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("AWS_PROFILE"),
        help="AWS CLI profile name (optional, uses instance profile if not set)",
    )
    parser.add_argument(
        "--operator",
        default=os.environ.get("USER", "cli-operator"),
        help="Operator identity written to updated_by field",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # list
    sub.add_parser("list", help="List all strategy config rows")

    # get
    p_get = sub.add_parser("get", help="Print a single strategy's config")
    p_get.add_argument("strategy", help="Strategy name (e.g. nse_orb_15m)")

    # set
    p_set = sub.add_parser("set", help="Update config fields for a strategy")
    p_set.add_argument("strategy", help="Strategy name")
    p_set.add_argument("--enabled", type=lambda x: x.lower() == "true",
                       help="true / false")
    p_set.add_argument("--paper-trade", type=lambda x: x.lower() == "true",
                       dest="paper_trade", help="true / false")
    p_set.add_argument("--max-signals-per-day", type=int, dest="max_signals_per_day",
                       help="Daily signal cap (0 = unlimited)")
    p_set.add_argument("--cb-consecutive", type=int, dest="cb_consecutive",
                       help="Circuit breaker consecutive error threshold")
    p_set.add_argument("--cb-rate", type=int, dest="cb_rate",
                       help="Circuit breaker rate error threshold (per 5 min)")

    # seed
    sub.add_parser("seed", help="Write default config rows for all known strategies")

    # enable / disable shortcuts
    p_enable  = sub.add_parser("enable",  help="Shortcut: set enabled=True")
    p_enable.add_argument("strategy")
    p_disable = sub.add_parser("disable", help="Shortcut: set enabled=False")
    p_disable.add_argument("strategy")

    # go-live / paper shortcuts
    p_golive = sub.add_parser("go-live", help="Shortcut: set paper_trade=False")
    p_golive.add_argument("strategy")
    p_paper  = sub.add_parser("paper",   help="Shortcut: set paper_trade=True")
    p_paper.add_argument("strategy")

    return parser.parse_args()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> int:
    args = _parse_args()
    table, table_name = _get_table(args.env, args.prefix, args.profile)
    print(f"Table: {table_name}  |  env={args.env}  |  operator={args.operator}")

    cmd = args.command
    try:
        if cmd == "list":
            return cmd_list(table, args.env)

        elif cmd == "get":
            return cmd_get(table, args.strategy, args.env)

        elif cmd == "set":
            return cmd_set(
                table, args.strategy, args.env, args.operator,
                enabled=args.enabled,
                paper_trade=args.paper_trade,
                max_signals_per_day=args.max_signals_per_day,
                cb_consecutive=args.cb_consecutive,
                cb_rate=args.cb_rate,
            )

        elif cmd == "seed":
            return cmd_seed(table, args.env, args.operator)

        elif cmd == "enable":
            return cmd_set(table, args.strategy, args.env, args.operator, enabled=True)

        elif cmd == "disable":
            return cmd_set(table, args.strategy, args.env, args.operator, enabled=False)

        elif cmd == "go-live":
            confirm = input(
                f"\n⚠  This will set paper_trade=False for '{args.strategy}' in {args.env}.\n"
                f"   Live orders WILL be placed. Type the strategy name to confirm: "
            )
            if confirm.strip() != args.strategy:
                print("Aborted — strategy name did not match.")
                return 1
            return cmd_set(table, args.strategy, args.env, args.operator, paper_trade=False)

        elif cmd == "paper":
            return cmd_set(table, args.strategy, args.env, args.operator, paper_trade=True)

    except ClientError as e:
        print(f"ERROR: AWS error: {e.response['Error']['Code']}: {e.response['Error']['Message']}")
        print(f"  Table: {table_name}")
        print("  Check IAM permissions and table name.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
