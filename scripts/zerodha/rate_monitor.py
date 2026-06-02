#!/usr/bin/env python3
"""
Zerodha Rate Monitor — live terminal dashboard for the ADR-012 rate-limit system.

Pulls the last 60 minutes of CloudWatch metrics from the QuantEmbrace/ZerodhaRateLimit
namespace and renders a continuously refreshing ASCII dashboard in the terminal.

Usage:
    python scripts/zerodha/rate_monitor.py
    python scripts/zerodha/rate_monitor.py --env staging
    python scripts/zerodha/rate_monitor.py --refresh 5      # seconds between refreshes
    python scripts/zerodha/rate_monitor.py --window 30      # lookback window in minutes

What it shows:
    ┌─ Utilization ──────────────────────────────────────────────────────────────┐
    │  Actual req/sec  (avg last 1m):  7.3 / 10.0  [███████░░░░░] 73%           │
    │  Token bucket    (current min):  2.1 / 15.0  [██░░░░░░░░░░]  14%          │
    │  Rate-limit errors (last 1h):    0                                         │
    ├─ Fill Detection ───────────────────────────────────────────────────────────┤
    │  Latency P50:  210ms   P95:  480ms   P99:  890ms   [NORMAL]               │
    │  Open orders (current max):  3  → poll interval: 500ms                    │
    ├─ Quote Spreads ─────────────────────────────────────────────────────────────┤
    │  P95 spread: 32 bps   Max spread: 91 bps   Gate threshold: 50 bps         │
    ├─ Alarm Status ──────────────────────────────────────────────────────────────┤
    │  zerodha-rate-limit-errors:         OK                                     │
    │  zerodha-token-bucket-depleted:     OK                                     │
    │  zerodha-fill-latency-high:         OK                                     │
    └─────────────────────────────────────────────────────────────────────────────┘

Requirements:
    pip install boto3 rich

AWS permissions needed:
    cloudwatch:GetMetricStatistics
    cloudwatch:DescribeAlarms
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import boto3
except ImportError:
    print("ERROR: boto3 not installed. Run: pip install boto3", file=sys.stderr)
    sys.exit(1)

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    _RICH = True
except ImportError:
    _RICH = False

# ── Constants ──────────────────────────────────────────────────────────────────

NAMESPACE = "QuantEmbrace/ZerodhaRateLimit"
DIMENSION = {"Name": "Service", "Value": "execution_engine"}
ALARM_PREFIX_TEMPLATE = "{project}-{env}"

METRICS = [
    ("ZerodhaAPICallsPerSecond",     "Average",  "req/sec"),
    ("ZerodhaTokenBucketLevel",      "Minimum",  "tokens"),
    ("ZerodhaRateLimitErrors",       "Sum",      "errors"),
    ("ZerodhaFillDetectionLatencyMs","p50",       "ms"),
    ("ZerodhaFillDetectionLatencyMs","p95",       "ms"),
    ("ZerodhaFillDetectionLatencyMs","p99",       "ms"),
    ("ZerodhaOpenOrderCount",        "Maximum",  "orders"),
    ("ZerodhaQuoteSpreadBps",        "p95",       "bps"),
    ("ZerodhaQuoteSpreadBps",        "Maximum",  "bps"),
]

ALARMS = [
    "zerodha-rate-limit-errors",
    "zerodha-token-bucket-depleted",
    "zerodha-fill-latency-high",
]

RATE_LIMIT = 10.0
BURST_CAP  = 15.0
SPREAD_GATE_BPS = 50.0

POLL_INTERVAL_THRESHOLDS = [
    (0,  "2000ms (idle)"),
    (2,  "1000ms"),
    (5,  "500ms"),
    (6,  "300ms (max)"),
]


def get_metric_value(
    cw_client,
    metric_name: str,
    stat: str,
    period_seconds: int,
    window_minutes: int,
) -> Optional[float]:
    """Fetch the latest datapoint for a CloudWatch metric."""
    now_utc = datetime.now(tz=timezone.utc)
    start   = now_utc - timedelta(minutes=window_minutes)

    kwargs: dict = {
        "Namespace":  NAMESPACE,
        "MetricName": metric_name,
        "Dimensions": [DIMENSION],
        "StartTime":  start,
        "EndTime":    now_utc,
        "Period":     period_seconds,
    }
    if stat.startswith("p"):
        kwargs["ExtendedStatistics"] = [stat]
    else:
        kwargs["Statistics"] = [stat]

    resp = cw_client.get_metric_statistics(**kwargs)
    dps  = resp.get("Datapoints", [])
    if not dps:
        return None

    dps.sort(key=lambda d: d["Timestamp"])
    latest = dps[-1]
    return latest.get(stat) if stat.startswith("p") else latest.get(stat)


def get_alarm_states(cw_client, project: str, env: str) -> dict[str, str]:
    """Fetch CloudWatch alarm states for the 3 Zerodha rate-limit alarms."""
    prefix = f"{project}-{env}"
    alarm_names = [f"{prefix}-{suffix}" for suffix in ALARMS]
    resp = cw_client.describe_alarms(AlarmNames=alarm_names, AlarmTypes=["MetricAlarm"])
    states: dict[str, str] = {}
    for alarm in resp.get("MetricAlarms", []):
        short_name = alarm["AlarmName"].replace(f"{prefix}-", "")
        states[short_name] = alarm["StateValue"]  # OK | ALARM | INSUFFICIENT_DATA
    return states


def bar(value: float, maximum: float, width: int = 12) -> str:
    """Return an ASCII progress bar."""
    filled = int(round(min(value, maximum) / maximum * width))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def poll_interval_label(open_orders: int) -> str:
    label = "2000ms (idle)"
    for threshold, lbl in POLL_INTERVAL_THRESHOLDS:
        if open_orders > threshold:
            label = lbl
    return label


def render_plain(data: dict, alarm_states: dict[str, str]) -> None:
    """Render dashboard to stdout without Rich."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n─── Zerodha Rate Monitor [{ts}] ─────────────────────────────────")

    req_sec  = data.get("req_sec", 0.0)  or 0.0
    tok_min  = data.get("tok_min", 0.0)  or 0.0
    errors   = data.get("errors",  0.0)  or 0.0
    fp50     = data.get("fill_p50", 0.0) or 0.0
    fp95     = data.get("fill_p95", 0.0) or 0.0
    fp99     = data.get("fill_p99", 0.0) or 0.0
    orders   = data.get("orders",   0.0) or 0.0
    sp95     = data.get("spread_p95", 0.0) or 0.0
    sp_max   = data.get("spread_max", 0.0) or 0.0

    print(f"  Utilization")
    print(f"    req/sec  avg: {req_sec:5.1f}/{RATE_LIMIT}  "
          f"{bar(req_sec, RATE_LIMIT)}  {req_sec/RATE_LIMIT*100:3.0f}%")
    print(f"    token bucket min: {tok_min:4.1f}/{BURST_CAP}  "
          f"{bar(tok_min, BURST_CAP)}  {tok_min/BURST_CAP*100:3.0f}%")
    err_flag = " ← P0 ALARM" if errors > 0 else ""
    print(f"    rate-limit errors (1h): {int(errors)}{err_flag}")

    print(f"\n  Fill Detection")
    latency_status = "HIGH" if fp95 > 1000 else "NORMAL"
    print(f"    P50: {fp50:.0f}ms   P95: {fp95:.0f}ms   P99: {fp99:.0f}ms  [{latency_status}]")
    print(f"    Open orders: {int(orders)}  → poll interval: {poll_interval_label(int(orders))}")

    print(f"\n  Quote Spreads")
    gate_flag = " ← above gate" if sp_max > SPREAD_GATE_BPS else ""
    print(f"    P95: {sp95:.0f} bps   Max: {sp_max:.0f} bps   Gate: {SPREAD_GATE_BPS:.0f} bps{gate_flag}")

    print(f"\n  Alarm Status")
    alarm_map = {
        "zerodha-rate-limit-errors":     "rate-limit-errors (P0)",
        "zerodha-token-bucket-depleted": "token-bucket-depleted (P1)",
        "zerodha-fill-latency-high":     "fill-latency-high (P1)",
    }
    for key, label in alarm_map.items():
        state = alarm_states.get(key, "UNKNOWN")
        marker = "🔴 ALARM" if state == "ALARM" else ("⚠️  INSUFF" if state == "INSUFFICIENT_DATA" else "✅ OK")
        print(f"    {label:40s}  {marker}")

    print("─" * 65)


def run(project: str, env: str, refresh_sec: int, window_min: int, region: str) -> None:
    """Main monitoring loop."""
    session  = boto3.Session(region_name=region)
    cw       = session.client("cloudwatch")

    print(f"Zerodha Rate Monitor — {project}/{env}  (region={region}, "
          f"refresh={refresh_sec}s, window={window_min}m)")
    print("Press Ctrl+C to exit.\n")

    def fetch() -> tuple[dict, dict]:
        vals = {}
        vals["req_sec"]    = get_metric_value(cw, "ZerodhaAPICallsPerSecond",     "Average", 10, window_min)
        vals["tok_min"]    = get_metric_value(cw, "ZerodhaTokenBucketLevel",      "Minimum", 10, window_min)
        vals["errors"]     = get_metric_value(cw, "ZerodhaRateLimitErrors",       "Sum",     60, window_min)
        vals["fill_p50"]   = get_metric_value(cw, "ZerodhaFillDetectionLatencyMs","p50",     60, window_min)
        vals["fill_p95"]   = get_metric_value(cw, "ZerodhaFillDetectionLatencyMs","p95",     60, window_min)
        vals["fill_p99"]   = get_metric_value(cw, "ZerodhaFillDetectionLatencyMs","p99",     60, window_min)
        vals["orders"]     = get_metric_value(cw, "ZerodhaOpenOrderCount",        "Maximum", 10, window_min)
        vals["spread_p95"] = get_metric_value(cw, "ZerodhaQuoteSpreadBps",        "p95",     60, window_min)
        vals["spread_max"] = get_metric_value(cw, "ZerodhaQuoteSpreadBps",        "Maximum", 60, window_min)
        alarms = get_alarm_states(cw, project, env)
        return vals, alarms

    try:
        while True:
            try:
                data, alarm_states = fetch()
                render_plain(data, alarm_states)
            except Exception as exc:
                print(f"[ERROR] fetch failed: {exc}", file=sys.stderr)
            time.sleep(refresh_sec)
    except KeyboardInterrupt:
        print("\nExiting rate monitor.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Zerodha rate-limit live dashboard")
    parser.add_argument("--project", default="quantembrace",  help="Project name prefix")
    parser.add_argument("--env",     default="prod",          help="Environment (dev/staging/prod)")
    parser.add_argument("--region",  default="ap-south-1",    help="AWS region")
    parser.add_argument("--refresh", type=int, default=10,    help="Refresh interval (seconds)")
    parser.add_argument("--window",  type=int, default=60,    help="Metrics lookback window (minutes)")
    args = parser.parse_args()
    run(args.project, args.env, args.refresh, args.window, args.region)


if __name__ == "__main__":
    main()
