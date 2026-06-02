#!/usr/bin/env python3
"""Seed LocalStack DynamoDB with realistic paper trading state for local monitoring.

Creates 4 open positions (3 LONG, 1 SHORT with trailing) and sets the kill switch
OFF in the risk-state table. Run this after setup_local_tables.py and before
paper_trading_monitor.py.

Usage:
    AWS_ENDPOINT_URL=http://localhost:4566 DYNAMODB_TABLE_PREFIX=quantembrace-test \
        python scripts/monitoring/seed_local_positions.py

    # Clear existing positions before seeding:
    python scripts/monitoring/seed_local_positions.py --clear

    # Custom prefix:
    DYNAMODB_TABLE_PREFIX=quantembrace-dev \
    AWS_ENDPOINT_URL=http://localhost:4566 \
        python scripts/monitoring/seed_local_positions.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError


# Realistic intraday positions reflecting a mid-session paper trading state.
# RELIANCE/INFY/TCS: LONG positions from momentum signals.
# HDFCBANK: SHORT with trailing stop active (moving in our favour).
# TCS: exit order already placed by TEE (exit_order_id set).
_POSITIONS = [
    {
        "symbol":          "RELIANCE",
        "direction":       "LONG",
        "quantity":        "50",
        "avg_entry_price": "2450.00",
        "last_price":      "2510.50",
        "stop_price":      "2400.00",
        "take_profit":     "2550.00",
        "exit_state":      "STOP_LOSS_ACTIVE",
        "exit_order_id":   None,
    },
    {
        "symbol":          "INFY",
        "direction":       "LONG",
        "quantity":        "100",
        "avg_entry_price": "1580.00",
        "last_price":      "1605.00",
        "stop_price":      "1550.00",
        "take_profit":     "1650.00",
        "exit_state":      "STOP_LOSS_ACTIVE",
        "exit_order_id":   None,
    },
    {
        "symbol":          "HDFCBANK",
        "direction":       "SHORT",
        "quantity":        "-25",
        "avg_entry_price": "1620.00",
        "last_price":      "1608.00",
        "stop_price":      "1660.00",
        "take_profit":     "1570.00",
        "exit_state":      "TRAILING_ACTIVE",
        "exit_order_id":   None,
    },
    {
        "symbol":          "TCS",
        "direction":       "LONG",
        "quantity":        "30",
        "avg_entry_price": "3750.00",
        "last_price":      "3800.00",
        "stop_price":      "3700.00",
        "take_profit":     "3900.00",
        "exit_state":      "TAKE_PROFIT_ACTIVE",
        "exit_order_id":   "ORD-TCS-20260525-001",
    },
]


def _make_client(endpoint: str | None, region: str) -> Any:
    kwargs: dict[str, Any] = {"region_name": region}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("dynamodb", **kwargs)


def _clear_positions(dynamo: Any, table: str) -> None:
    resp = dynamo.scan(
        TableName=table,
        FilterExpression="SK = :cur",
        ExpressionAttributeValues={":cur": {"S": "CURRENT"}},
        ProjectionExpression="PK, SK",
    )
    deleted = 0
    for item in resp.get("Items", []):
        pk = item["PK"]["S"]
        if pk.startswith("POSITION#"):
            dynamo.delete_item(TableName=table, Key={"PK": item["PK"], "SK": item["SK"]})
            print(f"  Deleted {pk}")
            deleted += 1
    if deleted == 0:
        print("  No existing positions to clear")


def _seed_positions(dynamo: Any, table: str) -> None:
    for pos in _POSITIONS:
        sym = pos["symbol"]
        qty = float(pos["quantity"])
        item: dict[str, Any] = {
            "PK":              {"S": f"POSITION#{sym}"},
            "SK":              {"S": "CURRENT"},
            "symbol":          {"S": sym},
            "direction":       {"S": pos["direction"]},
            "quantity":        {"N": pos["quantity"]},
            "avg_entry_price": {"N": pos["avg_entry_price"]},
            "last_price":      {"N": pos["last_price"]},
            "stop_price":      {"N": pos["stop_price"]},
            "take_profit":     {"N": pos["take_profit"]},
            "exit_state":      {"S": pos["exit_state"]},
        }
        if pos["exit_order_id"]:
            item["exit_order_id"] = {"S": pos["exit_order_id"]}

        dynamo.put_item(TableName=table, Item=item)

        side = "LONG " if qty >= 0 else "SHORT"
        entry = float(pos["avg_entry_price"])
        ltp   = float(pos["last_price"])
        pnl   = (ltp - entry) * abs(qty) if qty >= 0 else (entry - ltp) * abs(qty)
        print(
            f"  {sym:<12}  {side}  qty={pos['quantity']:>4}  "
            f"entry={pos['avg_entry_price']}  ltp={pos['last_price']}  "
            f"pnl={pnl:+.2f}  state={pos['exit_state']}"
        )


def _seed_kill_switch(dynamo: Any, table: str) -> None:
    ts = str(int(time.time()))
    dynamo.put_item(
        TableName=table,
        Item={
            "PK":             {"S": "KILLSWITCH"},
            "SK":             {"S": "GLOBAL"},
            "active":         {"BOOL": False},
            "status":         {"S": "INACTIVE"},
            "scope":          {"S": "GLOBAL"},
            "reason":         {"S": "paper_seed"},
            "activated_by":   {"S": "seed_local_positions"},
            "updated_at":     {"S": ts},
            "schema_version": {"S": "1.0"},
            "deactivated_at": {"S": ts},
        },
    )
    print("  Kill switch: INACTIVE (safe for paper trading)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed LocalStack DynamoDB with paper trading state for monitoring"
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete existing CURRENT positions before seeding",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="DynamoDB table prefix (overrides $DYNAMODB_TABLE_PREFIX)",
    )
    args = parser.parse_args()

    endpoint = (
        os.environ.get("AWS_ENDPOINT_URL")
        or os.environ.get("LOCALSTACK_ENDPOINT_URL")
    )
    region  = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "ap-south-1"))
    prefix  = args.prefix or os.environ.get("DYNAMODB_TABLE_PREFIX", "quantembrace-test")

    positions_table  = f"{prefix}-positions"
    risk_state_table = f"{prefix}-risk-state"

    print(f"Endpoint  : {endpoint or 'real AWS (no endpoint override)'}")
    print(f"Region    : {region}")
    print(f"Prefix    : {prefix}")
    print()

    try:
        dynamo = _make_client(endpoint, region)
    except Exception as exc:
        print(f"Failed to create DynamoDB client: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Positions table  → {positions_table}")
    try:
        if args.clear:
            print("Clearing existing positions:")
            _clear_positions(dynamo, positions_table)
        print("Seeding positions:")
        _seed_positions(dynamo, positions_table)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            print(
                f"\n  Table '{positions_table}' does not exist.\n"
                "  Run setup_local_tables.py first:\n"
                "    AWS_ENDPOINT_URL=http://localhost:4566 python scripts/setup_local_tables.py",
                file=sys.stderr,
            )
        else:
            print(f"\n  DynamoDB error: {exc}", file=sys.stderr)
            print("  Is LocalStack running? docker-compose up localstack", file=sys.stderr)
        sys.exit(1)

    print()
    print(f"Risk-state table → {risk_state_table}")
    try:
        _seed_kill_switch(dynamo, risk_state_table)
    except ClientError as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print()
    print("Seed complete.")
    print()
    print("Next step:")
    print("  python scripts/monitoring/paper_trading_monitor.py \\")
    print("    --counters scripts/monitoring/sample_counters.json")


if __name__ == "__main__":
    main()
