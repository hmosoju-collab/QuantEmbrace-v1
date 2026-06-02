#!/usr/bin/env python3
"""Render the QuantEmbrace 15-section paper trading monitoring status report.

Connects to DynamoDB (LocalStack or real AWS) and optionally loads a LiveCounters
JSON stub to fill in service counters that would normally come from running services.

Quick start (LocalStack):
    # 1. Ensure LocalStack is running:
    #    docker-compose up localstack
    #
    # 2. Create tables (if not done already):
    #    AWS_ENDPOINT_URL=http://localhost:4566 python scripts/setup_local_tables.py
    #
    # 3. Seed positions:
    #    AWS_ENDPOINT_URL=http://localhost:4566 python scripts/monitoring/seed_local_positions.py
    #
    # 4. Run monitoring status:
    AWS_ENDPOINT_URL=http://localhost:4566 DYNAMODB_TABLE_PREFIX=quantembrace-test \
        python scripts/monitoring/paper_trading_monitor.py \
        --counters scripts/monitoring/sample_counters.json

Watch mode (refresh every 30s):
    python scripts/monitoring/paper_trading_monitor.py --watch 30

Real AWS (dev environment):
    AWS_PROFILE=quantembrace-dev DYNAMODB_TABLE_PREFIX=quantembrace-dev \
    AWS_REGION=ap-south-1 \
        python scripts/monitoring/paper_trading_monitor.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import fields
from typing import Any, Optional

# Add project root so `services.shared.*` imports resolve.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from services.shared.monitoring import (
        LiveCounters,
        MonitoringStatusRenderer,
        MonitoringStatusService,
        StrategyStatusRow,
    )
except ImportError as exc:
    print(f"Import error: {exc}")
    print("Run from the project root with the virtualenv active.")
    sys.exit(1)

_RENDERER = MonitoringStatusRenderer()

_GREEN = "\033[92m"
_AMBER = "\033[93m"
_RED   = "\033[91m"
_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"

_STATUS_COLOUR = {"GREEN": _GREEN, "AMBER": _AMBER, "RED": _RED}


# ── DynamoDB client ────────────────────────────────────────────────────────────


def _make_dynamo(endpoint: Optional[str], region: str) -> Any:
    import boto3
    kwargs: dict[str, Any] = {"region_name": region}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("dynamodb", **kwargs)


# ── LiveCounters loader ────────────────────────────────────────────────────────


def _load_counters(path: str) -> LiveCounters:
    """Deserialise a JSON file into a LiveCounters instance.

    Unknown keys in the JSON are silently ignored so partial stubs work fine.
    StrategyStatusRow objects are reconstructed from the 'strategy_statuses' list.
    """
    with open(path) as fh:
        data: dict[str, Any] = json.load(fh)

    strategy_data = data.pop("strategy_statuses", [])
    strategies = [
        StrategyStatusRow(
            name=str(s["name"]),
            status=s.get("status", "ACTIVE"),
            signals=int(s.get("signals", 0)),
            fills=int(s.get("fills", 0)),
            open_positions=int(s.get("open_positions", 0)),
            exits=int(s.get("exits", 0)),
            cap_status=s.get("cap_status", "OK"),
            notes=s.get("notes", ""),
        )
        for s in strategy_data
    ]

    valid_fields = {f.name for f in fields(LiveCounters)} - {"strategy_statuses"}
    filtered = {k: v for k, v in data.items() if k in valid_fields}

    counters = LiveCounters(**filtered)
    counters.strategy_statuses = strategies
    return counters


# ── Core render ────────────────────────────────────────────────────────────────


async def _build_and_render(
    dynamo: Any,
    positions_table: str,
    risk_state_table: str,
    prices_table: str,
    trading_mode: str,
    counters: Optional[LiveCounters],
) -> tuple[str, str]:
    svc = MonitoringStatusService(
        dynamo_client=dynamo,
        positions_table=positions_table,
        risk_state_table=risk_state_table,
        prices_table=prices_table,
        trading_mode=trading_mode,
        live_trading_enabled=False,
        live_counters=counters,
    )
    snap = await svc.build_snapshot()
    return _RENDERER.render(snap), snap.overall_status


def _print_render(
    output: str,
    status: str,
    prefix: str,
    endpoint: Optional[str],
    counters_path: Optional[str],
) -> None:
    colour = _STATUS_COLOUR.get(status, "")
    status_badge = f"{_BOLD}{colour}[ {status} ]{_RESET}"
    source_note = f"{_DIM}prefix={prefix}  endpoint={endpoint or 'real AWS'}  " \
                  f"counters={counters_path or 'none'}{_RESET}"
    print(f"{status_badge}  {source_note}")
    print(output)


# ── CLI ────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="QuantEmbrace paper trading monitoring status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--prefix",
        default=None,
        metavar="PREFIX",
        help="DynamoDB table prefix (default: $DYNAMODB_TABLE_PREFIX or quantembrace-test)",
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        metavar="URL",
        help="DynamoDB endpoint URL (default: $AWS_ENDPOINT_URL or real AWS)",
    )
    parser.add_argument(
        "--counters",
        default=None,
        metavar="FILE",
        help="LiveCounters JSON stub (e.g. scripts/monitoring/sample_counters.json)",
    )
    parser.add_argument(
        "--watch",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Refresh every N seconds (0 = run once and exit)",
    )
    parser.add_argument(
        "--trading-mode",
        default="PAPER",
        choices=["PAPER", "BACKTEST", "LIVE"],
        help="Trading mode reported in the status header (default: PAPER)",
    )
    args = parser.parse_args()

    endpoint = (
        args.endpoint
        or os.environ.get("AWS_ENDPOINT_URL")
        or os.environ.get("LOCALSTACK_ENDPOINT_URL")
    )
    region = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "ap-south-1"))
    prefix = args.prefix or os.environ.get("DYNAMODB_TABLE_PREFIX", "quantembrace-test")

    positions_table  = f"{prefix}-positions"
    risk_state_table = f"{prefix}-risk-state"
    prices_table     = f"{prefix}-prices"

    counters: Optional[LiveCounters] = None
    if args.counters:
        try:
            counters = _load_counters(args.counters)
        except FileNotFoundError:
            print(f"Counters file not found: {args.counters}", file=sys.stderr)
            sys.exit(1)
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            print(f"Failed to parse counters file: {exc}", file=sys.stderr)
            sys.exit(1)

    try:
        import boto3  # noqa: F401
    except ImportError:
        print("boto3 not installed. Run: pip install boto3", file=sys.stderr)
        sys.exit(1)

    dynamo = _make_dynamo(endpoint, region)

    async def _run_once() -> None:
        output, status = await _build_and_render(
            dynamo, positions_table, risk_state_table, prices_table, args.trading_mode, counters
        )
        _print_render(output, status, prefix, endpoint, args.counters)

    if args.watch > 0:
        async def _watch_loop() -> None:
            try:
                while True:
                    os.system("clear" if os.name != "nt" else "cls")
                    await _run_once()
                    print(f"\n{_DIM}Refreshing in {args.watch}s — Ctrl+C to stop{_RESET}")
                    await asyncio.sleep(args.watch)
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("\nStopped.")

        asyncio.run(_watch_loop())
    else:
        asyncio.run(_run_once())


if __name__ == "__main__":
    main()
