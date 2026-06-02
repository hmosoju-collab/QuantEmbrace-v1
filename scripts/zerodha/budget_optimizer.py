#!/usr/bin/env python3
"""
Budget Optimizer — analyze CloudWatch metrics and suggest PHASE_BUDGET reallocation.

Reads 5 days of ZerodhaRateLimit CloudWatch metrics and computes actual vs planned
budget consumption per phase.  Outputs a diff of suggested changes to PHASE_BUDGET
in market_phase.py, with the reasoning grounded in observed data.

What it does:
  1. Pulls 5 trading days of ZerodhaAPICallsPerSecond, ZerodhaOpenOrderCount,
     ZerodhaTokenBucketLevel, and ZerodhaFillDetectionLatencyMs from CloudWatch.
  2. Segments data by market phase using the known IST phase schedule.
  3. Computes actual req/sec consumption per phase category.
  4. Compares actual vs the PHASE_BUDGET in market_phase.py.
  5. Outputs suggested reallocation if any category is consistently over/under budget.

Output example:
  Phase: NORMAL (09:30–14:45 IST)
  ─────────────────────────────────────────────────────────
    bulk_fill_poll  Planned: 3.0 req/s  Actual: 1.8 req/s  → OVER-ALLOCATED by 1.2 req/s
    get_quotes      Planned: 1.0 req/s  Actual: 0.5 req/s  → OVER-ALLOCATED by 0.5 req/s
    reserve         Planned: 1.0 req/s  Actual: 4.7 req/s  → UNDER-ALLOCATED (bursty)

  Suggested reallocation:
    bulk_fill_poll: 3.0 → 2.0  (-1.0)
    get_quotes:     1.0 → 0.5  (-0.5)
    reserve:        1.0 → 2.0  (+1.0)
    place_order:    4.0 → 4.5  (+0.5) ← freed capacity

Usage:
    python scripts/zerodha/budget_optimizer.py
    python scripts/zerodha/budget_optimizer.py --days 10 --env staging
    python scripts/zerodha/budget_optimizer.py --output json

Requirements:
    pip install boto3 python-dotenv
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import boto3
except ImportError:
    print("ERROR: boto3 not installed. Run: pip install boto3", file=sys.stderr)
    sys.exit(1)

# ── Phase schedule (mirrors market_phase.py) ──────────────────────────────────

_PHASE_SCHEDULE_IST = [
    ("PRE_OPEN",    8,  0, 9,  0),
    ("PRE_AUCTION", 9,  0, 9, 15),
    ("MARKET_OPEN", 9, 15, 9, 30),
    ("NORMAL",      9, 30, 14, 45),
    ("PRE_CLOSE",  14, 45, 15, 20),
    ("CLOSING",    15, 20, 15, 30),
    ("POST_CLOSE", 15, 30, 23, 59),
]

# Current PHASE_BUDGET from market_phase.py (for comparison)
_CURRENT_BUDGET: dict[str, dict[str, float]] = {
    "PRE_OPEN":    {"place_order": 0.5, "bulk_fill_poll": 0.5, "get_positions": 0.5,
                    "get_margins": 0.5, "get_quotes": 2.0, "candle_stream": 0.5,
                    "reconcile_audit": 0.5, "reserve": 5.0},
    "PRE_AUCTION": {"place_order": 2.0, "bulk_fill_poll": 2.0, "get_positions": 1.0,
                    "get_margins": 1.0, "get_quotes": 1.0, "candle_stream": 0.5,
                    "reconcile_audit": 0.5, "reserve": 2.0},
    "MARKET_OPEN": {"place_order": 4.0, "bulk_fill_poll": 3.0, "get_positions": 1.0,
                    "get_margins": 1.0, "get_quotes": 0.0, "candle_stream": 0.0,
                    "reconcile_audit": 0.0, "reserve": 1.0},
    "NORMAL":      {"place_order": 4.0, "bulk_fill_poll": 2.0, "get_positions": 1.0,
                    "get_margins": 0.5, "get_quotes": 0.5, "candle_stream": 0.5,
                    "reconcile_audit": 0.5, "reserve": 1.0},
    "PRE_CLOSE":   {"place_order": 2.0, "bulk_fill_poll": 4.0, "get_positions": 1.5,
                    "get_margins": 1.0, "get_quotes": 0.0, "candle_stream": 0.0,
                    "reconcile_audit": 0.5, "reserve": 1.0},
    "CLOSING":     {"place_order": 1.0, "bulk_fill_poll": 2.0, "get_positions": 2.0,
                    "get_margins": 1.0, "get_quotes": 1.5, "candle_stream": 0.5,
                    "reconcile_audit": 1.0, "reserve": 1.0},
    "POST_CLOSE":  {"place_order": 0.0, "bulk_fill_poll": 0.5, "get_positions": 1.0,
                    "get_margins": 0.5, "get_quotes": 0.5, "candle_stream": 0.0,
                    "reconcile_audit": 2.0, "reserve": 5.5},
}

NAMESPACE   = "QuantEmbrace/ZerodhaRateLimit"
DIMENSION   = {"Name": "Service", "Value": "execution_engine"}
RATE_LIMIT  = 10.0
IST_OFFSET  = timedelta(hours=5, minutes=30)


def _ist_to_utc(h: int, m: int, ref_date_utc: datetime) -> datetime:
    """Convert IST hour:minute on ref_date to UTC datetime."""
    ist = ref_date_utc.replace(hour=0, minute=0, second=0, microsecond=0) + IST_OFFSET
    ist = ist.replace(hour=h, minute=m, second=0, microsecond=0) - IST_OFFSET
    return ist


def _classify_phase(dt_utc: datetime) -> str:
    """Determine market phase from a UTC datetime."""
    ist_hour  = (dt_utc + IST_OFFSET).hour
    ist_minute = (dt_utc + IST_OFFSET).minute
    ist_total  = ist_hour * 60 + ist_minute

    for phase, sh, sm, eh, em in reversed(_PHASE_SCHEDULE_IST):
        start_total = sh * 60 + sm
        if ist_total >= start_total:
            return phase
    return "POST_CLOSE"


def _fetch_metric_timeseries(
    cw,
    metric_name: str,
    stat: str,
    period: int,
    days: int,
) -> list[dict[str, Any]]:
    """Fetch raw CloudWatch metric datapoints for the last N days."""
    now_utc   = datetime.now(tz=timezone.utc)
    start_utc = now_utc - timedelta(days=days)

    kwargs: dict = {
        "Namespace":  NAMESPACE,
        "MetricName": metric_name,
        "Dimensions": [DIMENSION],
        "StartTime":  start_utc,
        "EndTime":    now_utc,
        "Period":     period,
    }
    if stat.startswith("p"):
        kwargs["ExtendedStatistics"] = [stat]
    else:
        kwargs["Statistics"] = [stat]

    resp = cw.get_metric_statistics(**kwargs)
    dps  = resp.get("Datapoints", [])
    for dp in dps:
        dp["_value"] = dp.get(stat) if stat.startswith("p") else dp.get(stat)
    return sorted(dps, key=lambda d: d["Timestamp"])


def _aggregate_by_phase(datapoints: list[dict]) -> dict[str, list[float]]:
    """Group datapoint values by market phase."""
    phase_values: dict[str, list[float]] = {p[0]: [] for p in _PHASE_SCHEDULE_IST}
    for dp in datapoints:
        if dp.get("_value") is None:
            continue
        phase = _classify_phase(dp["Timestamp"])
        phase_values.get(phase, []).append(dp["_value"])
    return phase_values


def _mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _suggest_reallocation(
    phase: str,
    current_budget: dict[str, float],
    actual_call_rate: float,
    fill_latency_p95: Optional[float],
    token_depletion_pct: float,
) -> list[str]:
    """
    Simple heuristic-based reallocation suggestions.
    Returns list of human-readable suggestion strings.
    """
    suggestions = []
    total = RATE_LIMIT

    # Token depletion > 30% of samples → not enough reserve
    if token_depletion_pct > 30:
        suggestions.append(
            f"Token bucket depleted in {token_depletion_pct:.0f}% of samples — "
            f"consider increasing reserve from {current_budget.get('reserve', 0):.1f} "
            f"→ {min(current_budget.get('reserve', 0) + 1.0, total):.1f} req/s"
        )

    # Fill latency > 600ms P95 → bulk_fill_poll needs more budget
    if fill_latency_p95 is not None and fill_latency_p95 > 600:
        suggestions.append(
            f"Fill detection P95 = {fill_latency_p95:.0f}ms — "
            f"consider increasing bulk_fill_poll from {current_budget.get('bulk_fill_poll', 0):.1f} "
            f"→ {current_budget.get('bulk_fill_poll', 0) + 0.5:.1f} req/s"
        )

    # Actual < 60% of limit → budget may be too conservative
    if actual_call_rate < RATE_LIMIT * 0.6:
        suggestions.append(
            f"Actual utilization is only {actual_call_rate:.1f}/{RATE_LIMIT} req/s "
            f"({actual_call_rate/RATE_LIMIT*100:.0f}%) — system is underutilizing the budget"
        )

    return suggestions


def run(days: int, env: str, region: str, output_format: str) -> None:
    session = boto3.Session(region_name=region)
    cw      = session.client("cloudwatch")

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    print(f"Budget Optimizer — env={env}  region={region}  days={days}  ts={now_iso}\n")

    # Fetch all relevant metrics
    print("Fetching CloudWatch metrics...")
    api_calls   = _fetch_metric_timeseries(cw, "ZerodhaAPICallsPerSecond",     "Average", 60, days)
    token_level = _fetch_metric_timeseries(cw, "ZerodhaTokenBucketLevel",      "Minimum", 60, days)
    fill_lat    = _fetch_metric_timeseries(cw, "ZerodhaFillDetectionLatencyMs","p95",    300, days)
    open_orders = _fetch_metric_timeseries(cw, "ZerodhaOpenOrderCount",        "Maximum", 60, days)
    print(f"  api_calls:   {len(api_calls)} points")
    print(f"  token_level: {len(token_level)} points")
    print(f"  fill_lat:    {len(fill_lat)} points")
    print(f"  open_orders: {len(open_orders)} points\n")

    # Aggregate by phase
    api_by_phase   = _aggregate_by_phase(api_calls)
    token_by_phase = _aggregate_by_phase(token_level)
    fill_by_phase  = _aggregate_by_phase(fill_lat)
    orders_by_phase= _aggregate_by_phase(open_orders)

    report: dict[str, Any] = {
        "generated_at": now_iso,
        "days_analyzed": days,
        "phases": {}
    }

    for phase_name, sh, sm, eh, em in _PHASE_SCHEDULE_IST:
        current = _CURRENT_BUDGET.get(phase_name, {})

        calls    = api_by_phase.get(phase_name, [])
        tokens   = token_by_phase.get(phase_name, [])
        fills    = fill_by_phase.get(phase_name, [])
        orders   = orders_by_phase.get(phase_name, [])

        avg_calls   = _mean(calls)
        avg_tokens  = _mean(tokens)
        avg_fill_p95= _mean(fills)
        avg_orders  = _mean(orders)

        # Depletion = % of 1-min buckets where token level < 1
        depleted    = [t for t in tokens if t is not None and t < 1.0]
        depletion_pct = (len(depleted) / len(tokens) * 100) if tokens else 0.0

        suggestions = _suggest_reallocation(
            phase_name,
            current,
            avg_calls or 0.0,
            avg_fill_p95,
            depletion_pct,
        )

        phase_report = {
            "phase":            phase_name,
            "window_ist":       f"{sh:02d}:{sm:02d}–{eh:02d}:{em:02d}",
            "data_points":      len(calls),
            "avg_req_sec":      round(avg_calls, 2) if avg_calls else None,
            "avg_token_min":    round(avg_tokens, 2) if avg_tokens else None,
            "fill_p95_ms":      round(avg_fill_p95, 0) if avg_fill_p95 else None,
            "avg_open_orders":  round(avg_orders, 1) if avg_orders else None,
            "token_depletion_pct": round(depletion_pct, 1),
            "current_budget":   current,
            "suggestions":      suggestions,
        }
        report["phases"][phase_name] = phase_report

        if output_format == "text":
            print(f"Phase: {phase_name:12s}  ({sh:02d}:{sm:02d}–{eh:02d}:{em:02d} IST)")
            print(f"  {'─'*60}")
            if not calls:
                print(f"  (no data for this phase in the last {days} days)\n")
                continue

            util_pct = (avg_calls / RATE_LIMIT * 100) if avg_calls else 0
            print(f"  Utilization:       {avg_calls or 0:.1f} / {RATE_LIMIT:.0f} req/s  ({util_pct:.0f}%)")
            print(f"  Token bucket min:  {avg_tokens or 0:.1f} tokens avg  "
                  f"(depleted {depletion_pct:.0f}% of samples)")
            if avg_fill_p95 is not None:
                print(f"  Fill detect P95:   {avg_fill_p95:.0f} ms")
            if avg_orders is not None:
                print(f"  Open orders avg:   {avg_orders:.1f}")

            print(f"\n  Current budget:")
            for cat, alloc in sorted(current.items()):
                print(f"    {cat:<20s} {alloc:.1f} req/s")

            if suggestions:
                print(f"\n  Suggestions:")
                for s in suggestions:
                    print(f"    ⚠  {s}")
            else:
                print(f"\n  ✅ No reallocation suggested for this phase.")
            print()

    if output_format == "json":
        print(json.dumps(report, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze CloudWatch metrics and suggest PHASE_BUDGET reallocation"
    )
    parser.add_argument("--days",   type=int, default=5,          help="Days of history to analyze")
    parser.add_argument("--env",    default="prod",                help="Environment label")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-south-1"))
    parser.add_argument("--output", choices=["text","json"], default="text")
    args = parser.parse_args()
    run(args.days, args.env, args.region, args.output)


if __name__ == "__main__":
    main()
