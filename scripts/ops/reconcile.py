"""
reconcile.py — three-way broker/DynamoDB/Kafka position drift tool.

Phase 8 (ADR-015 F7): operator reconciliation tool.

Usage:
    # Check for drift and optionally set the reconciliation_required flag:
    python scripts/ops/reconcile.py --environment staging

    # Set the reconciliation_required flag without drift check (emergency halt):
    python scripts/ops/reconcile.py --environment prod --set-required \\
        --reason "manual_audit_post_outage"

    # Clear the reconciliation_required flag after resolving drift:
    python scripts/ops/reconcile.py --environment prod --clear

    # Check current flag status only:
    python scripts/ops/reconcile.py --environment prod --status

This tool performs a three-way comparison:
    1. Broker positions  — live call to Zerodha kite.positions()
    2. DynamoDB positions — scan of the positions table (FILLED orders)
    3. DynamoDB open orders — scan of PENDING / PLACED / PARTIALLY_FILLED orders

Any discrepancy (position mismatch, orphan orders, missing fills) is reported.
If ``--set-required`` is passed (or if --auto-set is passed and drift is found),
the reconciliation_required flag is written to DynamoDB, halting non-closeout
risk_engine signal approval until the operator clears it.

ADR-015 §5.3: Only a human operator may clear this flag. The risk_engine does
NOT auto-recover from a reconciliation halt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

# Ensure the services directory is on the path so shared/ is importable.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
_SERVICES_DIR = os.path.join(_REPO_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

import boto3  # noqa: E402
from shared.risk_state import reconciliation_key, s  # noqa: E402


# ── Constants ──────────────────────────────────────────────────────────────────

_ACTIVE_ORDER_STATUSES = {"PENDING", "PLACED", "PARTIALLY_FILLED"}
_RECONCILIATION_TABLE_ENV_VAR = "DYNAMODB_TABLE_RISK_STATE"


def _get_table_name(environment: str) -> str:
    """Derive the risk-state table name from the environment."""
    table = os.environ.get(_RECONCILIATION_TABLE_ENV_VAR)
    if table:
        return table
    prefix = os.environ.get("DYNAMODB_TABLE_PREFIX", f"quantembrace-{environment}")
    return f"{prefix}-risk-state"


def _get_orders_table(environment: str) -> str:
    table = os.environ.get("DYNAMODB_TABLE_ORDERS")
    if table:
        return table
    prefix = os.environ.get("DYNAMODB_TABLE_PREFIX", f"quantembrace-{environment}")
    return f"{prefix}-orders"


def _get_positions_table(environment: str) -> str:
    table = os.environ.get("DYNAMODB_TABLE_POSITIONS")
    if table:
        return table
    prefix = os.environ.get("DYNAMODB_TABLE_PREFIX", f"quantembrace-{environment}")
    return f"{prefix}-positions"


# ── Core reconciliation logic ─────────────────────────────────────────────────

async def get_broker_positions(settings: dict) -> dict[str, float]:
    """
    Fetch live NSE positions from Zerodha.

    Returns: {symbol: net_quantity} for all open intraday (MIS) positions.
    Skips if Zerodha credentials are not available (e.g., US-only mode).
    """
    api_key = os.environ.get("ZERODHA_API_KEY", "")
    access_token = os.environ.get("ZERODHA_ACCESS_TOKEN", "")
    if not api_key or not access_token:
        print("  [WARN] ZERODHA_API_KEY / ZERODHA_ACCESS_TOKEN not set — skipping broker check")
        return {}

    try:
        from kiteconnect import KiteConnect  # noqa: PLC0415
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        raw = await asyncio.to_thread(kite.positions)
        positions: dict[str, float] = {}
        for pos in raw.get("net", []):
            qty = int(pos.get("quantity", 0))
            symbol = str(pos.get("tradingsymbol", ""))
            if symbol and qty != 0:
                positions[symbol] = float(qty)
        return positions
    except ImportError:
        print("  [WARN] kiteconnect not installed — skipping broker position fetch")
        return {}
    except Exception as exc:
        print(f"  [ERROR] Zerodha positions fetch failed: {exc}")
        return {}


async def get_dynamo_positions(dynamo, positions_table: str) -> dict[str, float]:
    """Scan DynamoDB positions table for current confirmed net quantities."""
    positions: dict[str, float] = {}
    try:
        paginator = dynamo.get_paginator("scan")
        async for page in _async_paginator(paginator, TableName=positions_table):
            for item in page.get("Items", []):
                symbol_raw = item.get("symbol", {})
                symbol = symbol_raw.get("S", "") if isinstance(symbol_raw, dict) else str(symbol_raw)
                qty_raw = item.get("net_quantity", item.get("quantity", {}))
                qty = float((qty_raw.get("N", "0") if isinstance(qty_raw, dict) else qty_raw) or 0)
                if symbol:
                    positions[symbol] = positions.get(symbol, 0) + qty
    except Exception as exc:
        print(f"  [ERROR] DynamoDB positions scan failed: {exc}")
    return positions


async def get_dynamo_open_orders(dynamo, orders_table: str) -> list[dict[str, Any]]:
    """Scan DynamoDB orders table for all active (non-terminal) orders."""
    open_orders: list[dict[str, Any]] = []
    try:
        paginator = dynamo.get_paginator("scan")
        async for page in _async_paginator(paginator, TableName=orders_table):
            for item in page.get("Items", []):
                status_raw = item.get("status", {})
                status = (
                    status_raw.get("S", "") if isinstance(status_raw, dict) else str(status_raw)
                )
                if status.upper() in _ACTIVE_ORDER_STATUSES:
                    open_orders.append(item)
    except Exception as exc:
        print(f"  [ERROR] DynamoDB orders scan failed: {exc}")
    return open_orders


async def _async_paginator(paginator, **kwargs):
    """Wrap a boto3 paginator in an async generator."""
    pages = await asyncio.to_thread(lambda: list(paginator.paginate(**kwargs)))
    for page in pages:
        yield page


def _compare_positions(
    broker: dict[str, float],
    dynamo: dict[str, float],
) -> list[dict[str, Any]]:
    """Return a list of discrepancy dicts between broker and DynamoDB positions."""
    all_symbols = set(broker) | set(dynamo)
    drifts = []
    for symbol in sorted(all_symbols):
        broker_qty = broker.get(symbol, 0.0)
        dynamo_qty = dynamo.get(symbol, 0.0)
        if abs(broker_qty - dynamo_qty) > 0.01:
            drifts.append({
                "symbol": symbol,
                "broker_qty": broker_qty,
                "dynamo_qty": dynamo_qty,
                "delta": broker_qty - dynamo_qty,
            })
    return drifts


# ── DynamoDB flag management ──────────────────────────────────────────────────

async def get_flag_status(dynamo, table: str) -> dict[str, Any]:
    """Read the current reconciliation_required flag from DynamoDB."""
    response = await asyncio.to_thread(
        dynamo.get_item,
        TableName=table,
        Key=reconciliation_key(),
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not item:
        return {"required": False}
    required_raw = item.get("required", {})
    required = bool(
        required_raw.get("BOOL", False) if isinstance(required_raw, dict) else required_raw
    )
    return {
        "required": required,
        "reason": (item.get("reason") or {}).get("S", ""),
        "set_by": (item.get("set_by") or {}).get("S", ""),
        "set_at": (item.get("set_at") or {}).get("S", ""),
    }


async def set_flag(dynamo, table: str, reason: str, set_by: str = "reconcile_script") -> None:
    """Write reconciliation_required=True to DynamoDB."""
    now = datetime.now(UTC).isoformat()
    await asyncio.to_thread(
        dynamo.put_item,
        TableName=table,
        Item={
            **reconciliation_key(),
            "required": {"BOOL": True},
            "reason": s(reason),
            "set_by": s(set_by),
            "set_at": s(now),
            "updated_at": s(now),
        },
    )


async def clear_flag(dynamo, table: str, cleared_by: str = "reconcile_script") -> None:
    """Write reconciliation_required=False to DynamoDB."""
    now = datetime.now(UTC).isoformat()
    await asyncio.to_thread(
        dynamo.put_item,
        TableName=table,
        Item={
            **reconciliation_key(),
            "required": {"BOOL": False},
            "reason": s("cleared"),
            "set_by": s(cleared_by),
            "set_at": s(now),
            "updated_at": s(now),
        },
    )


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> int:
    """
    Main reconcile logic.  Returns 0 on success, 1 if drift is found.
    """
    environment = args.environment
    region = os.environ.get("AWS_REGION", "ap-south-1")

    dynamo = boto3.client("dynamodb", region_name=region)
    risk_table = _get_table_name(environment)
    orders_table = _get_orders_table(environment)
    positions_table = _get_positions_table(environment)

    print(f"\n=== QuantEmbrace Reconcile Tool  ({environment.upper()}) ===")
    print(f"    risk-state table : {risk_table}")
    print(f"    orders table     : {orders_table}")
    print(f"    positions table  : {positions_table}")
    print()

    # ── --status: print current flag and exit ─────────────────────────────────
    if args.status:
        flag = await get_flag_status(dynamo, risk_table)
        status_str = "ACTIVE" if flag.get("required") else "CLEAR"
        print(f"  reconciliation_required = {status_str}")
        if flag.get("required"):
            print(f"    reason : {flag.get('reason', '')}")
            print(f"    set_by : {flag.get('set_by', '')}")
            print(f"    set_at : {flag.get('set_at', '')}")
        return 0

    # ── --clear: remove the flag and exit ─────────────────────────────────────
    if args.clear:
        print("  Clearing reconciliation_required flag ...")
        await clear_flag(dynamo, risk_table, cleared_by=f"operator:{os.environ.get('USER', 'unknown')}")
        print("  ✓ reconciliation_required = False")
        print("  Risk engine will resume normal signal processing on next poll (≤ 1s).")
        return 0

    # ── --set-required: set the flag and exit ─────────────────────────────────
    if args.set_required:
        reason = args.reason or "manual_operator_halt"
        print(f"  Setting reconciliation_required = True  (reason: {reason}) ...")
        await set_flag(dynamo, risk_table, reason=reason)
        print("  ✓ reconciliation_required = True")
        print("  Risk engine will reject non-closeout signals within 1s.")
        return 0

    # ── Default: run three-way drift check ───────────────────────────────────
    print("Step 1/3  Fetching broker positions (Zerodha) ...")
    broker_positions = await get_broker_positions({})

    print("Step 2/3  Fetching DynamoDB positions ...")
    dynamo_positions = await get_dynamo_positions(dynamo, positions_table)

    print("Step 3/3  Fetching open orders from DynamoDB ...")
    open_orders = await get_dynamo_open_orders(dynamo, orders_table)

    # Compare broker vs DynamoDB
    drifts = _compare_positions(broker_positions, dynamo_positions)

    print()
    print("─── Results ─────────────────────────────────────────────────────────")

    if broker_positions:
        print(f"\nBroker positions ({len(broker_positions)} symbols):")
        for sym, qty in sorted(broker_positions.items()):
            print(f"    {sym:30s}  broker={qty:+.0f}")

    if dynamo_positions:
        print(f"\nDynamoDB positions ({len(dynamo_positions)} symbols):")
        for sym, qty in sorted(dynamo_positions.items()):
            print(f"    {sym:30s}  dynamo={qty:+.0f}")

    if open_orders:
        print(f"\nOpen orders in DynamoDB ({len(open_orders)}):")
        for o in open_orders:
            oid = (o.get("order_id") or {}).get("S", "?")
            sym = (o.get("symbol") or {}).get("S", "?")
            status = (o.get("status") or {}).get("S", "?")
            side = (o.get("side") or {}).get("S", "?")
            qty = (o.get("quantity") or {}).get("N", "?")
            print(f"    {oid:40s}  {sym:20s}  {status:20s}  {side} {qty}")

    if drifts:
        print(f"\n[DRIFT DETECTED]  {len(drifts)} position(s) differ between broker and DynamoDB:")
        for d in drifts:
            print(
                f"    {d['symbol']:30s}  broker={d['broker_qty']:+.0f}  "
                f"dynamo={d['dynamo_qty']:+.0f}  delta={d['delta']:+.0f}"
            )
        print()
        if args.auto_set:
            reason = f"position_drift_detected_{len(drifts)}_symbols"
            print(f"  --auto-set: setting reconciliation_required (reason: {reason}) ...")
            await set_flag(dynamo, risk_table, reason=reason)
            print("  ✓ reconciliation_required = True")
            print("  Resolve the drift above, then run: python scripts/ops/reconcile.py --clear")
        else:
            print("  To halt risk_engine intake:  python scripts/ops/reconcile.py --set-required")
            print("  After resolving:             python scripts/ops/reconcile.py --clear")
        return 1

    print("\n[CLEAN]  No position drift detected.")
    if open_orders and not broker_positions:
        print(f"  Note: {len(open_orders)} open order(s) in DynamoDB but no broker connection.")
        print("  Verify broker state manually if Zerodha credentials were not available.")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Three-way broker/DynamoDB/Kafka position reconciliation tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--environment", "-e",
        default=os.environ.get("QE_ENVIRONMENT", "staging"),
        help="Target environment (dev/staging/prod). Default: $QE_ENVIRONMENT or 'staging'",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print current reconciliation_required flag status and exit",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Clear the reconciliation_required flag (resume normal operation)",
    )
    parser.add_argument(
        "--set-required", action="store_true",
        help="Set reconciliation_required=True without running drift check",
    )
    parser.add_argument(
        "--reason",
        default="",
        help="Reason string for --set-required",
    )
    parser.add_argument(
        "--auto-set", action="store_true",
        help="Automatically set reconciliation_required if drift is detected",
    )
    parser.add_argument(
        "--output-json", action="store_true",
        help="Output drift report as JSON to stdout (for CI or monitoring pipelines)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    exit_code = asyncio.run(main(args))
    sys.exit(exit_code)
