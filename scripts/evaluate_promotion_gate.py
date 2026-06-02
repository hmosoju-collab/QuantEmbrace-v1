#!/usr/bin/env python3
"""
Promotion gate evaluator CLI.

Reads collected metrics and evaluates whether the trading universe is ready
to be promoted to the next mode. Promotion is ALWAYS a manual operator decision —
this tool only reports pass/fail. It never changes the active mode.

Promotion path:
    PAPER_SAFE_START  →  PAPER_EXPAND  →  LIVE_ADVANCED

Usage:
    # Evaluate using a metrics JSON file
    python scripts/evaluate_promotion_gate.py \\
        --gate PAPER_SAFE_START_TO_EXPAND \\
        --metrics path/to/metrics.json

    # Evaluate using inline JSON
    python scripts/evaluate_promotion_gate.py \\
        --gate PAPER_EXPAND_TO_LIVE \\
        --metrics-json '{"data_quality.min_tick_data_pass_rate_pct": 97.5, ...}'

    # Read metrics from stdin (piped from CloudWatch/Prometheus query)
    python scripts/evaluate_promotion_gate.py --gate PAPER_SAFE_START_TO_EXPAND --stdin

Metrics format:
    Flat dict with dotted keys matching criteria in configs/promotion_gates.yaml.
    Example:
      {
        "data_quality.min_tick_data_pass_rate_pct": 98.2,
        "risk_engine.max_kill_switch_false_positive_rate_pct": 0.5,
        "paper_trading.min_fill_rate_pct": 95.1,
        ...
      }

Exit codes:
    0 — All criteria passed (operator may promote if desired)
    1 — One or more criteria failed
    2 — Configuration or usage error
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from shared.universe.promotion import PromotionGateEvaluator

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)

_VALID_GATES = [
    "PAPER_SAFE_START_TO_EXPAND",
    "PAPER_EXPAND_TO_LIVE",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate a QuantEmbrace universe promotion gate.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--gate",
        required=True,
        choices=_VALID_GATES,
        help="Gate to evaluate.",
    )

    metrics_group = parser.add_mutually_exclusive_group(required=True)
    metrics_group.add_argument(
        "--metrics",
        metavar="PATH",
        help="Path to a JSON file containing the metrics dict.",
    )
    metrics_group.add_argument(
        "--metrics-json",
        metavar="JSON",
        help="Inline JSON string of the metrics dict.",
    )
    metrics_group.add_argument(
        "--stdin",
        action="store_true",
        help="Read metrics JSON from stdin.",
    )

    parser.add_argument(
        "--gates-config",
        metavar="PATH",
        default=None,
        help="Override path to promotion_gates.yaml (default: configs/promotion_gates.yaml).",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        help="Output machine-readable JSON instead of a human-readable report.",
    )
    return parser.parse_args()


def _load_metrics(args: argparse.Namespace) -> dict:
    if args.stdin:
        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(f"ERROR: Failed to parse metrics JSON from stdin: {exc}", file=sys.stderr)
            sys.exit(2)

    if args.metrics_json:
        try:
            return json.loads(args.metrics_json)
        except json.JSONDecodeError as exc:
            print(f"ERROR: Failed to parse --metrics-json: {exc}", file=sys.stderr)
            sys.exit(2)

    path = Path(args.metrics)
    if not path.exists():
        print(f"ERROR: Metrics file not found: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: Failed to parse metrics file {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def _output_json(result) -> None:
    data = {
        "gate": result.gate_name,
        "from_mode": result.from_mode.value,
        "to_mode": result.to_mode.value,
        "evaluated_at": result.evaluated_at.isoformat(),
        "overall_passed": result.overall_passed,
        "passed_count": len(result.passed_criteria),
        "failed_count": len(result.failed_criteria),
        "criteria": [
            {
                "criterion": c.criterion,
                "required": c.required,
                "actual": c.actual,
                "passed": c.passed,
                "notes": c.notes,
            }
            for c in result.criteria_results
        ],
    }
    print(json.dumps(data, indent=2))


def main() -> None:
    args = _parse_args()
    metrics = _load_metrics(args)

    try:
        evaluator = PromotionGateEvaluator.from_yaml_config(
            gates_config_path=args.gates_config
        )
    except Exception as exc:
        print(f"ERROR: Failed to load promotion gates config: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        result = evaluator.evaluate(gate=args.gate, metrics=metrics)
    except Exception as exc:
        print(f"ERROR: Gate evaluation failed: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.json_output:
        _output_json(result)
    else:
        print()
        print(result.summary())
        print()
        if result.overall_passed:
            print(
                "OPERATOR ACTION REQUIRED: All criteria passed. "
                "To promote, set UNIVERSE_MODE=" + result.to_mode.value + " and restart services."
            )
        else:
            print(
                f"ACTION REQUIRED: {len(result.failed_criteria)} criterion/criteria failed. "
                "Resolve the issues above before re-evaluating."
            )
        print()

    sys.exit(0 if result.overall_passed else 1)


if __name__ == "__main__":
    main()
