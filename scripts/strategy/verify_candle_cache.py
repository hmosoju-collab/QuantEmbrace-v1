#!/usr/bin/env python3
"""
Phase 3 Prerequisite P1 — Verify Candle Cache is Populated.

Queries DynamoDB ``{prefix}-candle-cache`` and confirms that
``IntradayCandleStream`` in data_ingestion is writing candles.

Usage:
    python scripts/strategy/verify_candle_cache.py --env staging
    python scripts/strategy/verify_candle_cache.py --env production --min-items 5

Exit codes:
    0 — candle cache has fresh items (Phase 3 can proceed)
    1 — candle cache empty or stale (Phase 3 blocked — investigate data_ingestion)
    2 — DynamoDB / AWS error
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import sys
import time

import boto3
from boto3.dynamodb.conditions import Attr, Key

_CANDLE_OPEN_TIME_INDEX = "candle-open-time-index"
_CANDLE_CACHE_BUCKET = "ACTIVE"


def _table_name(prefix: str) -> str:
    return f"{prefix}-candle-cache"


def _run(env: str, min_items: int, lookback_minutes: int, profile: str | None) -> int:
    prefix_map = {
        "development": "quantembrace-dev",
        "staging": "quantembrace-staging",
        "production": "quantembrace-prod",
    }
    if env not in prefix_map:
        print(f"[ERROR] Unknown environment '{env}'. Choose: development, staging, production")
        return 2

    prefix = prefix_map[env]
    table_name = _table_name(prefix)

    session_kwargs: dict = {}
    if profile:
        session_kwargs["profile_name"] = profile

    try:
        session = boto3.Session(**session_kwargs)
        dynamodb = session.resource("dynamodb")
        table = dynamodb.Table(table_name)
    except Exception as exc:
        print(f"[ERROR] Failed to connect to DynamoDB: {exc}")
        return 2

    cutoff_dt = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
    cutoff_iso = cutoff_dt.isoformat()

    print(f"[INFO] Querying table : {table_name}")
    print(f"[INFO] Lookback window: {lookback_minutes} minutes (candles after {cutoff_iso})")
    print()

    try:
        response = table.query(
            IndexName=_CANDLE_OPEN_TIME_INDEX,
            KeyConditionExpression=(
                Key("cache_bucket").eq(_CANDLE_CACHE_BUCKET)
                & Key("candle_open_time").gte(cutoff_iso)
            ),
            FilterExpression=Attr("expires_at").gt(int(time.time())),
            Limit=500,
        )
    except Exception as exc:
        print(f"[ERROR] DynamoDB query failed: {exc}")
        return 2

    items = response.get("Items", [])

    # Group by market+instrument for human-readable output
    by_instrument: dict[str, list[dict]] = {}
    for item in items:
        pk = item.get("PK", "?")
        by_instrument.setdefault(pk, []).append(item)

    instrument_count = len(by_instrument)
    item_count = len(items)

    print(f"Found {item_count} candle items across {instrument_count} instruments in the last {lookback_minutes} minutes:")
    print()

    if by_instrument:
        # Print table: instrument | count | freshest candle time
        header = f"{'Instrument':<40} {'Candles':>7} {'Freshest candle_open_time':<30}"
        print(header)
        print("-" * len(header))
        for pk in sorted(by_instrument.keys()):
            inst_items = by_instrument[pk]
            # Find freshest candle_open_time
            open_times = [i.get("candle_open_time", "") for i in inst_items if i.get("candle_open_time")]
            freshest = max(open_times) if open_times else "n/a"
            print(f"{pk:<40} {len(inst_items):>7} {freshest:<30}")
    else:
        print("  (no items found)")

    print()

    if item_count < min_items:
        print(
            f"[FAIL] Only {item_count} items found (required >= {min_items}). "
            f"candle-cache is empty or stale."
        )
        print()
        print("Troubleshooting steps:")
        print("  1. Confirm data_ingestion service is running and healthy")
        print("  2. Check market hours — IntradayCandleStream only runs during MARKET hours (IST)")
        print("  3. Check CloudWatch logs: data_ingestion log group → 'candle_stream'")
        print("  4. Verify DynamoDB table exists: aws dynamodb describe-table --table-name", table_name)
        print("  5. Check Zerodha historical API credentials are valid")
        return 1

    print(
        f"[PASS] {item_count} items found across {instrument_count} instruments "
        f"in the last {lookback_minutes} minutes. Candle cache is active."
    )
    print("       Phase 3 prerequisite P1 satisfied — proceed with implementation.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 3 P1 prerequisite: verify candle-cache is being written by data_ingestion.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["development", "staging", "production"],
        help="Target environment",
    )
    parser.add_argument(
        "--min-items",
        type=int,
        default=3,
        help="Minimum number of fresh items required to PASS (default: 3)",
    )
    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=30,
        help="How many minutes back to scan for fresh items (default: 30)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS CLI profile name (optional)",
    )
    args = parser.parse_args()
    sys.exit(_run(args.env, args.min_items, args.lookback_minutes, args.profile))


if __name__ == "__main__":
    main()
