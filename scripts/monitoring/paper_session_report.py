#!/usr/bin/env python3
"""
QuantEmbrace Paper Trading Session Report

Generates a daily report of paper trading performance covering:
  - Signal counts: pending → enriched → approved → executed
  - Enrichment funnel: enriched vs degraded (fallback) signals
  - Risk rejections by validator
  - Order fill rates and slippage
  - Circuit-breaker events (kill switch activations, fallback activations)
  - Per-strategy performance
  - Readiness assessment: pass/warn/fail against go-live thresholds

Output:
  - Console (coloured summary)
  - S3 (JSON detail report) — if S3_BUCKET_LOGS is set
  - Local file: /tmp/paper_session_report_YYYYMMDD.json

Usage:
  python scripts/monitoring/paper_session_report.py [--date YYYY-MM-DD] [--days N]

Environment variables:
  AWS_REGION             e.g. ap-south-1
  DYNAMODB_TABLE_PREFIX  e.g. quantembrace-prod
  S3_BUCKET_LOGS         S3 bucket for reports
  QE_ENVIRONMENT         production | staging | development
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta, date
from typing import Any, Optional


# ── ANSI colours ──────────────────────────────────────────────────────────────
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

PASS = f"{_GREEN}PASS{_RESET}"
FAIL = f"{_RED}FAIL{_RESET}"
WARN = f"{_YELLOW}WARN{_RESET}"


# ── Go-live thresholds ────────────────────────────────────────────────────────
# Minimum 5 consecutive days of paper trading must exceed all PASS thresholds
# before live capital is deployed (see go_live_checklist.md).

THRESHOLDS = {
    # Signal pipeline health
    "enrichment_rate_pct":         {"pass": 80.0,  "warn": 60.0},  # % signals enriched (not degraded)
    "signal_approval_rate_pct":    {"pass": 50.0,  "warn": 30.0},  # % pending signals approved
    "order_fill_rate_pct":         {"pass": 90.0,  "warn": 75.0},  # % approved signals filled
    # Risk quality
    "false_positive_rejection_pct":{"pass": 10.0,  "warn": 20.0},  # % rejections that were age-only
    # Execution quality
    "avg_slippage_pct":            {"pass": 0.15,  "warn": 0.30},  # % slippage vs signal price
    # System reliability
    "fallback_activations_per_day":{"pass": 2,     "warn": 5},     # EnrichmentWatchdog fallbacks
    "kill_switch_activations":     {"pass": 0,     "warn": 1},     # Kill switch fires
    # Strategy health (per strategy)
    "strategy_sharpe_ratio":       {"pass": 0.5,   "warn": 0.0},   # Annualised Sharpe (paper P&L)
    "max_drawdown_pct":            {"pass": 5.0,   "warn": 10.0},  # Max drawdown %
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SignalMetrics:
    total_pending:      int = 0
    total_enriched:     int = 0
    total_degraded:     int = 0
    total_approved:     int = 0
    total_rejected:     int = 0
    total_executed:     int = 0
    total_filled:       int = 0

    @property
    def enrichment_rate_pct(self) -> float:
        if self.total_enriched + self.total_degraded == 0:
            return 0.0
        return 100.0 * self.total_enriched / (self.total_enriched + self.total_degraded)

    @property
    def approval_rate_pct(self) -> float:
        if self.total_pending == 0:
            return 0.0
        return 100.0 * self.total_approved / self.total_pending

    @property
    def fill_rate_pct(self) -> float:
        if self.total_executed == 0:
            return 0.0
        return 100.0 * self.total_filled / self.total_executed


@dataclass
class RejectionBreakdown:
    kill_switch:    int = 0
    position_limit: int = 0
    daily_loss:     int = 0
    margin:         int = 0
    signal_age:     int = 0
    spread_gate:    int = 0
    sector_limit:   int = 0
    liquidity:      int = 0
    other:          int = 0

    @property
    def total(self) -> int:
        return sum(vars(self).values())


@dataclass
class ExecutionMetrics:
    total_orders:       int   = 0
    filled:             int   = 0
    partially_filled:   int   = 0
    rejected:           int   = 0
    cancelled:          int   = 0
    avg_slippage_pct:   float = 0.0
    max_slippage_pct:   float = 0.0
    avg_fill_latency_ms:float = 0.0


@dataclass
class StrategyMetrics:
    name:           str
    signals:        int   = 0
    approved:       int   = 0
    filled:         int   = 0
    gross_pnl:      float = 0.0
    net_pnl:        float = 0.0
    sharpe:         float = 0.0
    max_drawdown:   float = 0.0
    win_rate:       float = 0.0


@dataclass
class CircuitBreakerEvents:
    kill_switch_activations:     int = 0
    fallback_activations:        int = 0
    fallback_recovery_events:    int = 0
    avg_fallback_duration_sec:   float = 0.0


@dataclass
class SessionReport:
    report_date:         str
    environment:         str
    generated_at:        str
    session_start:       str
    session_end:         str
    signals:             SignalMetrics
    rejections:          RejectionBreakdown
    execution:           ExecutionMetrics
    strategies:          list[StrategyMetrics]
    circuit_breakers:    CircuitBreakerEvents
    readiness_checks:    dict[str, str]   # metric_name → PASS|WARN|FAIL
    readiness_verdict:   str              # READY|NOT_READY|BORDERLINE
    notes:               list[str]


# ── Data fetching from DynamoDB ───────────────────────────────────────────────

def _fetch_signal_metrics(
    dynamo,
    table_prefix: str,
    session_date: date,
) -> SignalMetrics:
    """
    Scan the risk-state table for RISK_DECISION# entries created on session_date.

    Risk decisions are stored in {prefix}-risk-state with PK=RISK_DECISION#{signal_id},
    SK=DECISION.  Fields: status (APPROVED|REJECTED), created_at (ISO8601 UTC).
    Falls back to zeroed metrics if DynamoDB is unavailable.
    """
    metrics = SignalMetrics()
    try:
        table_name = f"{table_prefix}-risk-state"
        date_prefix = session_date.isoformat()  # "YYYY-MM-DD"

        paginator = dynamo.get_paginator("scan")
        pages = paginator.paginate(
            TableName=table_name,
            FilterExpression=(
                "begins_with(PK, :pk_prefix) AND begins_with(created_at, :date)"
            ),
            ExpressionAttributeValues={
                ":pk_prefix": {"S": "RISK_DECISION#"},
                ":date":      {"S": date_prefix},
            },
            ProjectionExpression="PK,#s,decision_json",
            ExpressionAttributeNames={"#s": "status"},
        )
        for page in pages:
            for item in page.get("Items", []):
                metrics.total_pending += 1
                # Enrichment type lives inside the serialized decision_json blob.
                decision_json = item.get("decision_json", {}).get("S", "")
                if decision_json:
                    try:
                        import json as _json  # noqa: PLC0415
                        blob = _json.loads(decision_json)
                        # RiskDecision.to_dict() now writes "enriched": True/False
                        # directly.  Older records (pre-fix) lack the field — treat
                        # them as degraded so the count is conservative, not inflated.
                        if blob.get("enriched", False):
                            metrics.total_enriched += 1
                        else:
                            metrics.total_degraded += 1
                    except Exception:
                        metrics.total_degraded += 1
                else:
                    metrics.total_degraded += 1

                rs = item.get("status", {}).get("S", "")
                if rs == "APPROVED":
                    metrics.total_approved += 1
                elif rs == "REJECTED":
                    metrics.total_rejected += 1

    except Exception:
        pass  # DynamoDB unavailable — return zeroed metrics
    return metrics


def _fetch_execution_metrics(
    dynamo,
    table_prefix: str,
    session_date: date,
) -> tuple[ExecutionMetrics, RejectionBreakdown]:
    """
    Query the orders table for the session date using the DateIndex GSI.

    The orders table ({prefix}-orders) uses 'trade_date' as the hash key of
    the DateIndex GSI and 'order_status' as the status field.  This matches
    the schema in setup_local_tables.py.
    """
    exec_m = ExecutionMetrics()
    rej    = RejectionBreakdown()
    try:
        table_name = f"{table_prefix}-orders"
        date_str   = session_date.isoformat()

        # Use the DateIndex GSI (trade_date is the GSI hash key).
        paginator = dynamo.get_paginator("query")
        pages = paginator.paginate(
            TableName=table_name,
            IndexName="DateIndex",
            KeyConditionExpression="trade_date = :d",
            ExpressionAttributeValues={":d": {"S": date_str}},
        )
        slippages = []
        for page in pages:
            for item in page.get("Items", []):
                exec_m.total_orders += 1
                # The status attribute is named 'order_status' in this table schema.
                status = item.get("order_status", {}).get("S", "")
                if status in ("FILLED", "PAPER_FILLED"):
                    exec_m.filled += 1
                    slip = float(item.get("slippage_pct", {}).get("N", "0"))
                    if slip:
                        slippages.append(slip)
                elif status in ("PARTIALLY_FILLED", "PAPER_PARTIAL"):
                    exec_m.partially_filled += 1
                elif status in ("REJECTED", "PAPER_REJECTED"):
                    exec_m.rejected += 1
                    reject_reason = item.get("reject_reason", {}).get("S", "other")
                    broker_message = item.get("broker_message", {}).get("S", "")
                    combined = f"{reject_reason} {broker_message}".lower()
                    if "kill_switch" in combined:      rej.kill_switch    += 1
                    elif "position_limit" in combined: rej.position_limit += 1
                    elif "daily_loss" in combined:     rej.daily_loss     += 1
                    elif "margin" in combined:         rej.margin         += 1
                    elif "signal_age" in combined:     rej.signal_age     += 1
                    elif "spread" in combined:         rej.spread_gate    += 1
                    elif "sector" in combined:         rej.sector_limit   += 1
                    elif "liquidity" in combined:      rej.liquidity      += 1
                    else:                              rej.other          += 1
                elif status == "CANCELLED":
                    exec_m.cancelled += 1

        if slippages:
            exec_m.avg_slippage_pct = sum(slippages) / len(slippages)
            exec_m.max_slippage_pct = max(slippages)

    except Exception:
        pass
    return exec_m, rej


def _fetch_strategy_metrics(
    dynamo,
    table_prefix: str,
    session_date: date,
) -> list[StrategyMetrics]:
    """
    Per-strategy P&L aggregation is not stored to DynamoDB in the current architecture
    (orders table has no strategy_name field; no strategy-performance table exists).
    Returns empty list — strategy-level breakdown requires a future dedicated table.
    """
    return []


def _fetch_circuit_breaker_events(
    dynamo,
    table_prefix: str,
    session_date: date,
) -> CircuitBreakerEvents:
    """
    Query risk-state for kill switch activations on session_date.

    Kill switch state is stored in {prefix}-risk-state as
    PK=KILL_SWITCH#GLOBAL, SK=STATE, with field activated_at (ISO8601 UTC).
    Checks whether a kill switch activation occurred on the given date.
    Fallback activations are not persisted to DynamoDB — returned as 0.
    """
    events = CircuitBreakerEvents()
    try:
        table_name  = f"{table_prefix}-risk-state"
        date_prefix = session_date.isoformat()
        resp = dynamo.get_item(
            TableName=table_name,
            Key={"PK": {"S": "KILL_SWITCH#GLOBAL"}, "SK": {"S": "STATE"}},
            ProjectionExpression="activated_at",
        )
        item         = resp.get("Item", {})
        activated_at = item.get("activated_at", {}).get("S", "")
        if activated_at.startswith(date_prefix):
            events.kill_switch_activations = 1
    except Exception:
        pass
    return events


# ── Readiness assessment ──────────────────────────────────────────────────────

def _assess_readiness(
    signals:  SignalMetrics,
    exec_m:   ExecutionMetrics,
    rejections: RejectionBreakdown,
    cb_events: CircuitBreakerEvents,
    strategies: list[StrategyMetrics],
) -> tuple[dict[str, str], str]:
    """
    Compare session metrics against go-live thresholds.
    Returns (readiness_checks, verdict).
    """
    checks: dict[str, str] = {}

    def _grade(metric: float, key: str, higher_is_better: bool = True) -> str:
        t = THRESHOLDS.get(key)
        if t is None:
            return "SKIP"
        if higher_is_better:
            if metric >= t["pass"]: return "PASS"
            if metric >= t["warn"]: return "WARN"
        else:
            if metric <= t["pass"]: return "PASS"
            if metric <= t["warn"]: return "WARN"
        return "FAIL"

    checks["enrichment_rate"]       = _grade(signals.enrichment_rate_pct,      "enrichment_rate_pct")
    checks["signal_approval_rate"]  = _grade(signals.approval_rate_pct,        "signal_approval_rate_pct")
    exec_fill_rate_pct = (100.0 * exec_m.filled / exec_m.total_orders) if exec_m.total_orders > 0 else 0.0
    checks["order_fill_rate"]       = _grade(exec_fill_rate_pct,               "order_fill_rate_pct")
    checks["avg_slippage"]          = _grade(exec_m.avg_slippage_pct,          "avg_slippage_pct",             higher_is_better=False)
    checks["fallback_activations"]  = _grade(cb_events.fallback_activations,   "fallback_activations_per_day", higher_is_better=False)
    checks["kill_switch_events"]    = _grade(cb_events.kill_switch_activations,"kill_switch_activations",      higher_is_better=False)

    if signals.total_rejected > 0:
        false_pos_pct = 100.0 * rejections.signal_age / signals.total_rejected
        checks["false_positive_rejections"] = _grade(false_pos_pct, "false_positive_rejection_pct", higher_is_better=False)

    for strat in strategies:
        checks[f"sharpe_{strat.name}"]   = _grade(strat.sharpe,       "strategy_sharpe_ratio")
        checks[f"drawdown_{strat.name}"] = _grade(strat.max_drawdown, "max_drawdown_pct", higher_is_better=False)

    fail_count = sum(1 for v in checks.values() if v == "FAIL")
    warn_count = sum(1 for v in checks.values() if v == "WARN")

    if fail_count > 0:
        verdict = "NOT_READY"
    elif warn_count > 2:
        verdict = "BORDERLINE"
    else:
        verdict = "READY"

    return checks, verdict


# ── Report rendering ──────────────────────────────────────────────────────────

def _render_console(report: SessionReport) -> None:
    v = report.readiness_verdict
    v_colour = _GREEN if v == "READY" else (_YELLOW if v == "BORDERLINE" else _RED)

    print(f"\n{_BOLD}QuantEmbrace Paper Session Report{_RESET}")
    print(f"Date: {report.report_date}  |  Env: {report.environment.upper()}")
    print(f"Session: {report.session_start} → {report.session_end}")
    print("=" * 65)

    print(f"\n{_CYAN}{_BOLD}Signal Pipeline{_RESET}")
    s = report.signals
    print(f"  Pending   →  {s.total_pending}")
    print(f"  Enriched  →  {s.total_enriched}  ({s.enrichment_rate_pct:.1f}% enrichment rate)")
    print(f"  Degraded  →  {s.total_degraded}  (fallback path)")
    print(f"  Approved  →  {s.total_approved}  ({s.approval_rate_pct:.1f}% approval rate)")
    print(f"  Rejected  →  {s.total_rejected}")
    print(f"  Filled    →  {s.total_filled}  ({s.fill_rate_pct:.1f}% fill rate)")

    print(f"\n{_CYAN}{_BOLD}Risk Rejection Breakdown{_RESET}")
    r = report.rejections
    print(f"  Kill switch:    {r.kill_switch}")
    print(f"  Position limit: {r.position_limit}")
    print(f"  Daily loss cap: {r.daily_loss}")
    print(f"  Signal age:     {r.signal_age}  (false positives)")
    print(f"  Spread gate:    {r.spread_gate}")
    print(f"  Sector limit:   {r.sector_limit}")
    print(f"  Liquidity:      {r.liquidity}")
    print(f"  Margin:         {r.margin}")
    print(f"  Other:          {r.other}")

    print(f"\n{_CYAN}{_BOLD}Execution Quality{_RESET}")
    e = report.execution
    print(f"  Orders:          {e.total_orders}")
    print(f"  Filled:          {e.filled}")
    print(f"  Avg slippage:    {e.avg_slippage_pct:.3f}%")
    print(f"  Max slippage:    {e.max_slippage_pct:.3f}%")

    print(f"\n{_CYAN}{_BOLD}Circuit Breaker Events{_RESET}")
    cb = report.circuit_breakers
    print(f"  Kill switch activations:    {cb.kill_switch_activations}")
    print(f"  Fallback activations:       {cb.fallback_activations}")
    print(f"  Fallback recovery events:   {cb.fallback_recovery_events}")
    if cb.avg_fallback_duration_sec > 0:
        print(f"  Avg fallback duration:      {cb.avg_fallback_duration_sec:.1f}s")

    if report.strategies:
        print(f"\n{_CYAN}{_BOLD}Per-Strategy Performance{_RESET}")
        print(f"  {'Strategy':<20} {'Signals':>7} {'Fills':>6} {'Net P&L':>10} {'Sharpe':>7} {'MaxDD':>7}")
        print(f"  {'-'*60}")
        for st in report.strategies:
            pnl_str  = f"${st.net_pnl:+.2f}" if st.net_pnl != 0 else "—"
            sh_str   = f"{st.sharpe:.2f}"   if st.sharpe   != 0 else "—"
            dd_str   = f"{st.max_drawdown:.2f}%" if st.max_drawdown != 0 else "—"
            print(f"  {st.name:<20} {st.signals:>7} {st.filled:>6} {pnl_str:>10} {sh_str:>7} {dd_str:>7}")

    print(f"\n{_CYAN}{_BOLD}Go-Live Readiness Checks{_RESET}")
    for check, verdict in report.readiness_checks.items():
        icon = (f"{_GREEN}{PASS}{_RESET}" if verdict == "PASS"
                else (f"{_YELLOW}{WARN}{_RESET}" if verdict == "WARN"
                else  f"{_RED}{FAIL}{_RESET}"))
        print(f"  {icon}  {check}")

    print(f"\n{'=' * 65}")
    print(f"Go-live verdict: {v_colour}{_BOLD}{v}{_RESET}")
    if report.notes:
        print("\nNotes:")
        for note in report.notes:
            print(f"  • {note}")
    print()


def _save_json(report: SessionReport, s3_bucket: Optional[str], aws_region: str) -> None:
    report_dict  = asdict(report)
    json_payload = json.dumps(report_dict, indent=2)
    local_path   = f"/tmp/paper_session_report_{report.report_date}.json"

    with open(local_path, "w") as fh:
        fh.write(json_payload)
    print(f"Report saved locally: {local_path}")

    if s3_bucket:
        try:
            import boto3
            s3  = boto3.client("s3", region_name=aws_region)
            key = f"reports/paper/{report.report_date}/session_report.json"
            s3.put_object(
                Bucket=s3_bucket,
                Key=key,
                Body=json_payload.encode(),
                ContentType="application/json",
            )
            print(f"Report uploaded: s3://{s3_bucket}/{key}")
        except Exception as exc:
            print(f"S3 upload failed (non-critical): {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def generate_report(session_date: date, n_days: int, env: str) -> int:
    aws_region   = os.environ.get("AWS_REGION",            "ap-south-1")
    tbl_prefix   = os.environ.get("DYNAMODB_TABLE_PREFIX", "quantembrace-development")
    s3_logs      = os.environ.get("S3_BUCKET_LOGS",        "")

    try:
        import boto3
        dynamo = boto3.client("dynamodb", region_name=aws_region)
    except ImportError:
        print(f"{_RED}boto3 not installed — cannot fetch DynamoDB data{_RESET}")
        dynamo = None

    # Build a report for each requested day
    all_verdicts = []
    for day_offset in range(n_days - 1, -1, -1):
        target_date = session_date - timedelta(days=day_offset)

        signals       = _fetch_signal_metrics(dynamo, tbl_prefix, target_date)   if dynamo else SignalMetrics()
        exec_m, rej   = _fetch_execution_metrics(dynamo, tbl_prefix, target_date) if dynamo else (ExecutionMetrics(), RejectionBreakdown())
        strategies    = _fetch_strategy_metrics(dynamo, tbl_prefix, target_date)  if dynamo else []
        cb_events     = _fetch_circuit_breaker_events(dynamo, tbl_prefix, target_date) if dynamo else CircuitBreakerEvents()

        checks, verdict = _assess_readiness(signals, exec_m, rej, cb_events, strategies)
        all_verdicts.append(verdict)

        # Derive session window from date (NSE: 09:15–15:30 IST, US: 09:30–16:00 ET)
        session_start = f"{target_date.isoformat()}T09:15:00+05:30"
        session_end   = f"{target_date.isoformat()}T15:30:00+05:30"

        notes = []
        if signals.total_pending == 0:
            notes.append("No signals found — check data ingestion and Kafka connectivity.")
        if cb_events.kill_switch_activations > 0:
            notes.append(
                f"Kill switch fired {cb_events.kill_switch_activations}x — "
                "review risk logs before promotion."
            )
        if verdict == "BORDERLINE":
            notes.append("Multiple WARNs present — continue paper trading before going live.")
        if verdict == "NOT_READY":
            notes.append("FAIL thresholds exceeded — do NOT promote to live capital.")

        report = SessionReport(
            report_date      = target_date.isoformat(),
            environment      = env,
            generated_at     = datetime.now(timezone.utc).isoformat(),
            session_start    = session_start,
            session_end      = session_end,
            signals          = signals,
            rejections       = rej,
            execution        = exec_m,
            strategies       = strategies,
            circuit_breakers = cb_events,
            readiness_checks = checks,
            readiness_verdict= verdict,
            notes            = notes,
        )

        _render_console(report)
        _save_json(report, s3_logs or None, aws_region)

    # Multi-day summary (if n_days > 1)
    if n_days > 1:
        consecutive_ready = sum(1 for v in all_verdicts if v == "READY")
        print(f"\n{_BOLD}Multi-day summary ({n_days} days){_RESET}")
        print(f"  Consecutive READY days: {consecutive_ready} / {n_days}")
        if consecutive_ready >= 5:
            print(f"  {_GREEN}✓ 5+ consecutive READY days — go-live criteria met.{_RESET}")
        else:
            remaining = 5 - consecutive_ready
            print(f"  {_YELLOW}Continue paper trading — need {remaining} more READY day(s).{_RESET}")

    # Return 0 if all days READY, 1 otherwise
    all_ready = all(v == "READY" for v in all_verdicts)
    return 0 if all_ready else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantEmbrace paper trading session report")
    parser.add_argument(
        "--date",
        default=None,
        help="Session date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Number of days to report (default: 1, use 5 for consecutive-day assessment)",
    )
    args = parser.parse_args()

    if args.date:
        try:
            session_date = date.fromisoformat(args.date)
        except ValueError:
            print(f"Invalid date format: {args.date} — expected YYYY-MM-DD")
            sys.exit(2)
    else:
        session_date = date.today()

    env = os.environ.get("QE_ENVIRONMENT", "development")
    sys.exit(generate_report(session_date, args.days, env))


if __name__ == "__main__":
    main()
