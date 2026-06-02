#!/usr/bin/env python3
"""
Position Audit — reconcile DynamoDB positions against Zerodha broker state.

Fetches live positions from Zerodha (kite.positions()) and compares them to
the current DynamoDB positions table.  Reports any discrepancies and optionally
writes the broker's ground-truth quantities back to DynamoDB.

This is the operator counterpart to PositionMonitor (the live service component).
Use it for:
  - End-of-day reconciliation
  - Post-incident audit after a missed fill or auto-square-off
  - Manual investigation when position_monitor.drift_detected appears in logs
  - Pre-market state verification before the trading session starts

Scenarios it catches:
  1. Zerodha auto-square-off (MIS positions closed by broker at ~15:15 IST)
  2. Manual orders placed from Kite app / phone that weren't system-originated
  3. Partial fills that BulkOrderPoller may have missed in a network gap
  4. Stale DynamoDB entries from a previous session that were never cleared

Usage:
    # Read-only audit report (default)
    python scripts/zerodha/position_audit.py

    # Fix mode — write broker quantities to DynamoDB for all drift detected
    python scripts/zerodha/position_audit.py --fix

    # Audit a specific symbol only
    python scripts/zerodha/position_audit.py --symbol NSE:RELIANCE

    # Output as JSON (for alerting pipelines)
    python scripts/zerodha/position_audit.py --format json

Requirements:
    pip install boto3 kiteconnect python-dotenv

Environment variables:
    ZERODHA_API_KEY
    ZERODHA_ACCESS_TOKEN
    AWS_REGION                     (default: ap-south-1)
    AWS_DYNAMODB_TABLE_POSITIONS   (default: quantembrace-positions)
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal
import json
import os
import sys

_POSITION_SK = "CURRENT"

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import boto3
    from boto3.dynamodb.conditions import Attr
except ImportError:
    print("ERROR: boto3 not installed. Run: pip install boto3", file=sys.stderr)
    sys.exit(1)

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("ERROR: kiteconnect not installed. Run: pip install kiteconnect", file=sys.stderr)
    sys.exit(1)


# ── DynamoDB helpers ───────────────────────────────────────────────────────────

def _get_dynamo_positions(dynamo, table_name: str) -> dict[str, dict]:
    """
    Scan DynamoDB positions table and return all open positions.

    Returns dict: { symbol → {quantity, product, avg_price, ...} }
    Filters to items where quantity != 0.
    """
    table    = dynamo.Table(table_name)
    response = table.scan(FilterExpression=Attr("quantity").ne(0))
    items    = response.get("Items", [])

    # Handle DynamoDB pagination
    while "LastEvaluatedKey" in response:
        response = table.scan(
            FilterExpression=Attr("quantity").ne(0),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    return {item["symbol"]: item for item in items if "symbol" in item}


def _get_broker_positions(kite: KiteConnect) -> dict[str, dict]:
    """
    Fetch live positions from Zerodha.

    Returns dict: { "EXCHANGE:SYMBOL" → {quantity, product, average_price, ...} }
    Filters to net day + net positions with non-zero quantity.
    """
    positions = kite.positions()
    result    = {}

    # kite.positions() returns {"net": [...], "day": [...]}
    net_positions = positions.get("net", [])
    for pos in net_positions:
        qty = pos.get("quantity", 0)
        if qty == 0:
            continue
        exchange = pos.get("exchange", "NSE")
        symbol   = pos.get("tradingsymbol", "")
        key      = f"{exchange}:{symbol}"
        result[key] = {
            "quantity":     qty,
            "average_price": pos.get("average_price", 0.0),
            "product":      pos.get("product", ""),
            "buy_quantity": pos.get("buy_quantity", 0),
            "sell_quantity": pos.get("sell_quantity", 0),
            "pnl":          pos.get("pnl", 0.0),
            "unrealised":   pos.get("unrealised", 0.0),
        }

    return result


def _overwrite_position_quantity(
    dynamo,
    table_name: str,
    symbol: str,
    quantity: float,
    source: str,
    now_iso: str,
) -> None:
    """Write broker quantity to DynamoDB as ground truth."""
    table = dynamo.Table(table_name)
    key = {"PK": f"POSITION#{symbol}", "SK": _POSITION_SK}
    quantity_value = Decimal(str(quantity))

    if quantity == 0:
        # If broker says flat, keep the canonical row but mark it flat for audit.
        table.update_item(
            Key=key,
            UpdateExpression=(
                "SET symbol = :sym, quantity = :q, confirmed_quantity = :q, "
                "last_synced_at = :ts, sync_source = :src"
            ),
            ExpressionAttributeValues={
                ":sym": symbol,
                ":q": quantity_value,
                ":ts": now_iso,
                ":src": source,
            },
        )
    else:
        table.update_item(
            Key=key,
            UpdateExpression=(
                "SET symbol = :sym, quantity = :q, confirmed_quantity = :q, "
                "last_synced_at = :ts, sync_source = :src "
                "ADD revision :one"
            ),
            ExpressionAttributeValues={
                ":sym": symbol,
                ":q": quantity_value,
                ":ts": now_iso,
                ":src": source,
                ":one": 1,
            },
        )


def _compare_positions(
    broker_map: dict[str, dict],
    dynamo_map: dict[str, dict],
    filter_symbol: str | None,
) -> list[dict]:
    """
    Compare broker and DynamoDB positions.
    Returns list of drift records: { symbol, broker_qty, dynamo_qty, drift, source }
    """
    drifts = []
    all_symbols = set(broker_map) | set(dynamo_map)

    for symbol in sorted(all_symbols):
        if filter_symbol and filter_symbol != symbol:
            continue

        broker_qty = float(broker_map.get(symbol, {}).get("quantity", 0))
        dynamo_item = dynamo_map.get(symbol, {})
        dynamo_qty  = float(dynamo_item.get("quantity", 0))

        if abs(broker_qty - dynamo_qty) > 0.001:
            source = (
                "broker_only"  if symbol not in dynamo_map else
                "dynamo_only"  if symbol not in broker_map else
                "qty_mismatch"
            )
            drifts.append({
                "symbol":     symbol,
                "broker_qty": broker_qty,
                "dynamo_qty": dynamo_qty,
                "drift":      round(broker_qty - dynamo_qty, 4),
                "source":     source,
                "broker_avg_price": broker_map.get(symbol, {}).get("average_price", 0),
                "broker_product":   broker_map.get(symbol, {}).get("product", ""),
            })

    return drifts


def run(
    symbol_filter: str | None,
    fix: bool,
    output_format: str,
    region: str,
    positions_table: str,
) -> int:
    """
    Run the position audit.
    Returns exit code: 0 = clean, 1 = drift detected.
    """
    api_key      = os.environ.get("ZERODHA_API_KEY",     "")
    access_token = os.environ.get("ZERODHA_ACCESS_TOKEN","")

    if not api_key or not access_token:
        print(
            "ERROR: ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN must be set.",
            file=sys.stderr,
        )
        return 2

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    session = boto3.Session(region_name=region)
    dynamo  = session.resource("dynamodb")

    now_iso = datetime.now(tz=UTC).isoformat()

    # Fetch both sources
    try:
        broker_map = _get_broker_positions(kite)
    except Exception as exc:
        print(f"ERROR: failed to fetch Zerodha positions: {exc}", file=sys.stderr)
        return 2

    try:
        dynamo_map = _get_dynamo_positions(dynamo, positions_table)
    except Exception as exc:
        print(f"ERROR: failed to fetch DynamoDB positions: {exc}", file=sys.stderr)
        return 2

    drifts = _compare_positions(broker_map, dynamo_map, symbol_filter)

    # ── JSON output mode ──────────────────────────────────────────────────────
    if output_format == "json":
        report = {
            "timestamp":      now_iso,
            "broker_count":   len(broker_map),
            "dynamo_count":   len(dynamo_map),
            "drift_count":    len(drifts),
            "drift_fixed":    False,
            "drifts":         drifts,
        }
        if fix and drifts:
            for d in drifts:
                try:
                    _overwrite_position_quantity(
                        dynamo, positions_table, d["symbol"],
                        d["broker_qty"], "position_audit_script", now_iso,
                    )
                except Exception as exc:
                    d["fix_error"] = str(exc)
            report["drift_fixed"] = True
        print(json.dumps(report, indent=2))
        return 0 if not drifts else 1

    # ── Human-readable output mode ────────────────────────────────────────────
    print(f"\n{'─'*65}")
    print(f"Position Audit — {now_iso}")
    print(f"  DynamoDB table: {positions_table}  (region={region})")
    print(f"  Fix mode:       {fix}")
    print(f"{'─'*65}")
    print(f"  Broker positions:  {len(broker_map):3d} symbols with non-zero qty")
    print(f"  DynamoDB positions:{len(dynamo_map):3d} symbols with non-zero qty")
    print()

    if not drifts:
        print("  ✅ No drift detected — broker and DynamoDB are in sync.\n")
        return 0

    print(f"  ⚠️  {len(drifts)} drift(s) detected:\n")
    header = f"  {'Symbol':<25s}  {'Broker Qty':>10s}  {'DynamoDB Qty':>12s}  "
    header += f"{'Drift':>8s}  Source"
    print(header)
    print(f"  {'─'*len(header)}")

    for d in drifts:
        drift_sign = "+" if d["drift"] > 0 else ""
        print(
            f"  {d['symbol']:<25s}  "
            f"{d['broker_qty']:>10.2f}  "
            f"{d['dynamo_qty']:>12.2f}  "
            f"{drift_sign}{d['drift']:>7.2f}  "
            f"{d['source']}"
        )

    print()

    if fix:
        print("  Applying fixes (--fix mode enabled)...")
        fix_ok = 0
        fix_err = 0
        for d in drifts:
            try:
                _overwrite_position_quantity(
                    dynamo, positions_table, d["symbol"],
                    d["broker_qty"], "position_audit_script", now_iso,
                )
                print(f"    ✅ Fixed {d['symbol']}: qty → {d['broker_qty']}")
                fix_ok += 1
            except Exception as exc:
                print(f"    ❌ Failed {d['symbol']}: {exc}", file=sys.stderr)
                fix_err += 1
        print(f"\n  Fixes applied: {fix_ok}  Errors: {fix_err}")
    else:
        print(
            "  Run with --fix to write broker quantities to DynamoDB.\n"
            "  CAUTION: --fix overwrites DynamoDB with broker ground truth.\n"
            "  Review the drift list above before applying."
        )

    print()
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile DynamoDB positions vs Zerodha broker state"
    )
    parser.add_argument("--symbol",  default=None,         help="Audit a single symbol e.g. NSE:RELIANCE")
    parser.add_argument("--fix",     action="store_true",  help="Write broker state to DynamoDB for all drift")
    parser.add_argument("--format",  choices=["text","json"], default="text", help="Output format")
    parser.add_argument("--region",  default=os.environ.get("AWS_REGION", "ap-south-1"))
    parser.add_argument(
        "--table",
        default=os.environ.get("AWS_DYNAMODB_TABLE_POSITIONS", "quantembrace-positions"),
        help="DynamoDB positions table name",
    )
    args = parser.parse_args()
    sys.exit(run(args.symbol, args.fix, args.format, args.region, args.table))


if __name__ == "__main__":
    main()
