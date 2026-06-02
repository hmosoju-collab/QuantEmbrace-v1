#!/usr/bin/env python3
"""QuantEmbrace operational checklist CLI."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ChecklistItem:
    check: str
    dashboard: str
    cli: str
    fail_action: str


CHECKLISTS: dict[str, list[ChecklistItem]] = {
    "daily": [
        ChecklistItem("Kill switch is CLEAR", "Kill Switch Activations", "python scripts/kill_switch_cli.py status", "Do not start trading."),
        ChecklistItem("Kafka consumer lag is near zero", "Kafka Lag by Consumer Group", "python scripts/kafka/validate_phase2.py --check lag", "Hold new strategies until lag is drained."),
        ChecklistItem("Broker latency and 429 metrics are normal", "Broker Latency and Rate Limits", "python scripts/zerodha/rate_monitor.py --once", "Keep all strategies in paper/shadow."),
        ChecklistItem("Position drift is zero", "Position Drift", "python scripts/zerodha/position_audit.py --once", "Run reconciliation before market open."),
    ],
    "market-open": [
        ChecklistItem("Candle cache has fresh minute bars", "Stale Ticks and Candles", "python scripts/strategy/verify_candle_cache.py --interval minute", "Disable ORB and candle strategies."),
        ChecklistItem("Quote poller has fresh spreads", "Quote Spread and Circuit Lock", "python scripts/zerodha/rate_monitor.py --quotes-once", "Fail live approvals closed."),
        ChecklistItem("No open unknown orders", "Order Lifecycle", "python scripts/zerodha/position_audit.py --orders", "Activate kill switch if unknown live exposure exists."),
        ChecklistItem("Daily loss starts at zero", "PnL and NAV", "python scripts/kill_switch_cli.py status", "Investigate NAV/daily PnL state before trading."),
    ],
    "post-market": [
        ChecklistItem("All orders are terminal", "Order Lifecycle", "python scripts/zerodha/position_audit.py --orders", "Reconcile before next session."),
        ChecklistItem("Broker positions match DynamoDB", "Position Drift", "python scripts/zerodha/position_audit.py --once", "Correct state and document incident."),
        ChecklistItem("Daily PnL/NAV is updated", "PnL and NAV", "python scripts/kill_switch_cli.py status", "Backfill fills and NAV snapshot."),
        ChecklistItem("DLQ/retry topics are empty or triaged", "Kafka Retry and DLQ", "python scripts/kafka/validate_phase2.py --check dlq", "Triage every message before replay."),
    ],
    "weekly": [
        ChecklistItem("Replay/chaos suite passes", "Release Readiness", "PYTHONPATH=services pytest tests/unit -q --forked", "Block live promotion."),
        ChecklistItem("Backtest report includes costs/slippage/rejects", "Backtest Validation", "python scripts/backtest/run_backtest.py --help", "Do not trust strategy metrics."),
        ChecklistItem("Runbooks reflect current dashboards", "Ops Checklist", "python scripts/ops/checklist.py daily", "Update runbook before next scale step."),
    ],
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print executable QuantEmbrace operating checklists.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "checklist",
        choices=sorted(CHECKLISTS),
        help="Checklist to print.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser


def _print_text(name: str, items: list[ChecklistItem]) -> None:
    print(f"QuantEmbrace {name} checklist")
    print("=" * (len(name) + 24))
    for idx, item in enumerate(items, 1):
        print(f"{idx}. {item.check}")
        print(f"   Dashboard: {item.dashboard}")
        print(f"   CLI      : {item.cli}")
        print(f"   If fail  : {item.fail_action}")


def main() -> None:
    args = _build_parser().parse_args()
    items = CHECKLISTS[args.checklist]
    if args.json:
        print(json.dumps([asdict(item) for item in items], indent=2))
    else:
        _print_text(args.checklist, items)


if __name__ == "__main__":
    main()
