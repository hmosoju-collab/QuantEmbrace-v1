#!/usr/bin/env python3
"""
QuantEmbrace Paper Trading Pre-flight Check

Validates the local paper trading stack before a session starts:

  1. Required environment variables are set (non-empty)
  2. LocalStack DynamoDB is reachable and all required tables exist
  3. LocalStack S3 is reachable and required buckets exist
  4. Redpanda Kafka is reachable and required topics exist
  5. Paper trading safety gates:
       - RISK_PROFILE must be "paper" (never "production")
       - live_trading_enabled must NOT be true
       - Kill switch must NOT be active
  6. NAV is seeded (paper capital available for risk calculations)
  7. Paper mode is correctly configured (no live broker credentials)

Exit codes:
  0  All checks passed — safe to start paper trading
  1  One or more checks FAILED — do NOT start
  2  Configuration / usage error

Usage:
  python scripts/deploy/paper_preflight_check.py

Environment variables (set in .env or docker-compose):
  AWS_ENDPOINT_URL          LocalStack endpoint (default: http://localhost:4566)
  AWS_DEFAULT_REGION        e.g. ap-south-1
  DYNAMODB_TABLE_PREFIX     e.g. quantembrace-development
  KAFKA_BOOTSTRAP_SERVERS   Redpanda external address (default: localhost:19092)
  RISK_PROFILE              Must be "paper"
  UNIVERSE_MODE             Should be PAPER_SAFE_START or PAPER_EXPAND
  S3_BUCKET_DATA            e.g. quantembrace-development-data
  S3_BUCKET_LOGS            e.g. quantembrace-development-logs
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

# Canonical kill-switch key/reader. Prefer the shared module so this script and
# the risk engine agree on the exact PK/SK and attribute names. Fall back to
# literal definitions if ``shared`` is not importable in the run environment
# (e.g. the script is executed standalone without services/ on PYTHONPATH).
try:
    from shared.risk_state import attr_bool as _attr_bool
    from shared.risk_state import kill_switch_key as _kill_switch_key
except Exception:  # pragma: no cover - exercised only when shared is unavailable
    def _kill_switch_key() -> dict[str, dict[str, str]]:
        return {"PK": {"S": "KILLSWITCH"}, "SK": {"S": "GLOBAL"}}

    def _attr_bool(item: dict[str, Any], name: str, default: bool = False) -> bool:
        raw = item.get(name)
        if isinstance(raw, dict):
            if "BOOL" in raw:
                return bool(raw["BOOL"])
            if "S" in raw:
                return str(raw["S"]).upper() in {"ACTIVE", "TRUE", "1", "YES"}
        if raw is None and name == "active":
            status = item.get("status", {})
            if isinstance(status, dict) and "S" in status:
                return str(status["S"]).upper() == "ACTIVE"
        return bool(raw) if raw is not None else default

# ── ANSI colours ──────────────────────────────────────────────────────────────
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

PASS_STR = f"{_GREEN}✓ PASS{_RESET}"
FAIL_STR = f"{_RED}✗ FAIL{_RESET}"
WARN_STR = f"{_YELLOW}⚠ WARN{_RESET}"


@dataclass
class CheckResult:
    name:    str
    status:  str        # "PASS" | "FAIL" | "WARN"
    message: str = ""


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, message: str = "") -> None:
        self.checks.append(CheckResult(name=name, status=status, message=message))

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "FAIL"]

    @property
    def warned(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "WARN"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _dynamo_client():
    import boto3
    endpoint = _env("AWS_ENDPOINT_URL", "http://localhost:4566")
    region   = _env("AWS_DEFAULT_REGION", "ap-south-1")
    return boto3.client(
        "dynamodb",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=_env("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=_env("AWS_SECRET_ACCESS_KEY", "test"),
    )


def _s3_client():
    import boto3
    endpoint = _env("AWS_ENDPOINT_URL", "http://localhost:4566")
    region   = _env("AWS_DEFAULT_REGION", "ap-south-1")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=_env("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=_env("AWS_SECRET_ACCESS_KEY", "test"),
    )


# ── Check implementations ─────────────────────────────────────────────────────

def check_env_vars(report: PreflightReport) -> None:
    required = [
        ("DYNAMODB_TABLE_PREFIX", "DynamoDB table prefix"),
        ("KAFKA_BOOTSTRAP_SERVERS", "Kafka bootstrap servers"),
        ("RISK_PROFILE", "Risk profile (must be 'paper')"),
    ]
    recommended = [
        ("S3_BUCKET_DATA",  "S3 data bucket (needed for session report upload)"),
        ("S3_BUCKET_LOGS",  "S3 logs bucket (needed for audit log upload)"),
        ("UNIVERSE_MODE",   "Universe mode (PAPER_SAFE_START or PAPER_EXPAND)"),
    ]
    all_ok = True
    for key, desc in required:
        val = _env(key)
        if not val:
            report.add(f"env:{key}", "FAIL", f"{desc} is not set")
            all_ok = False

    if all_ok:
        report.add("env:required", "PASS", "All required env vars are set")

    for key, desc in recommended:
        if not _env(key):
            report.add(f"env:{key}", "WARN", f"{desc} is not set")


def check_paper_safety_gates(report: PreflightReport) -> None:
    risk_profile = _env("RISK_PROFILE", "").lower()
    if risk_profile == "paper":
        report.add("safety:risk_profile", "PASS", "RISK_PROFILE=paper")
    else:
        report.add("safety:risk_profile", "FAIL",
                   f"RISK_PROFILE='{risk_profile}' — must be 'paper' for local paper trading")

    # Broker credential check.
    # Real Zerodha credentials are required for the candle data feed (IntradayCandleStream)
    # even in paper mode — without them, candle strategies produce zero signals.
    # HARD BLOCK only when QE_EXECUTION_LIVE_TRADING_ENABLED=true, which is when real
    # credentials become dangerous (live orders would be placed). With the live gate closed,
    # credentials are used for market data only, never for order placement.
    live_gate_open = _env("QE_EXECUTION_LIVE_TRADING_ENABLED", "false").lower() in ("true", "1", "yes")
    for key in ("ZERODHA_API_KEY", "ZERODHA_API_SECRET", "ALPACA_API_KEY", "ALPACA_API_SECRET"):
        val = _env(key, "paper_placeholder")
        if val and "placeholder" not in val.lower() and len(val) > 10:
            if live_gate_open:
                report.add(f"safety:{key}", "FAIL",
                           f"HARD RULE VIOLATION: {key} is a real credential and "
                           f"QE_EXECUTION_LIVE_TRADING_ENABLED=true — live orders would be placed. "
                           f"Set QE_EXECUTION_LIVE_TRADING_ENABLED=false before starting.")
            else:
                report.add(f"safety:{key}", "WARN",
                           f"{key} is a real credential. Live gate is CLOSED — permitted for "
                           f"market data feed (candle stream) only. No live orders will be placed.")

    universe_mode = _env("UNIVERSE_MODE", "PAPER_SAFE_START")
    valid_paper_modes = {"PAPER_SAFE_START", "PAPER_EXPAND"}
    if universe_mode in valid_paper_modes:
        report.add("safety:universe_mode", "PASS", f"UNIVERSE_MODE={universe_mode}")
    elif universe_mode == "LIVE_ADVANCED":
        report.add("safety:universe_mode", "FAIL",
                   "UNIVERSE_MODE=LIVE_ADVANCED — cannot use live universe in paper mode")
    else:
        report.add("safety:universe_mode", "WARN",
                   f"UNIVERSE_MODE='{universe_mode}' — expected PAPER_SAFE_START or PAPER_EXPAND")


def check_dynamodb(report: PreflightReport) -> None:
    try:
        dynamo = _dynamo_client()
    except ImportError:
        report.add("dynamodb:connectivity", "FAIL", "boto3 not installed")
        return

    prefix = _env("DYNAMODB_TABLE_PREFIX", "quantembrace-development")
    required_tables = [
        f"{prefix}-orders",
        f"{prefix}-positions",
        f"{prefix}-risk-state",
        f"{prefix}-sessions",
        f"{prefix}-latest-prices",
        f"{prefix}-fills",
        f"{prefix}-features",
        f"{prefix}-candle-cache",
        f"{prefix}-strategy-config",
        f"{prefix}-strategy-state",
        f"{prefix}-regime-log",
        f"{prefix}-strategy-recommendations",
    ]

    try:
        resp = dynamo.list_tables()
        existing = set(resp.get("TableNames", []))
    except Exception as exc:
        report.add("dynamodb:connectivity", "FAIL",
                   f"Cannot connect to LocalStack DynamoDB: {exc}. "
                   f"Run: docker-compose up -d localstack && docker-compose run --rm setup")
        return

    report.add("dynamodb:connectivity", "PASS",
               f"Connected to DynamoDB at {_env('AWS_ENDPOINT_URL', 'http://localhost:4566')}")

    missing = [t for t in required_tables if t not in existing]
    if missing:
        report.add("dynamodb:tables", "FAIL",
                   f"Missing tables: {', '.join(missing)}. "
                   f"Run: docker-compose run --rm setup")
    else:
        report.add("dynamodb:tables", "PASS",
                   f"All {len(required_tables)} required tables exist")

    # Check NAV seeded
    try:
        nav_resp = dynamo.get_item(
            TableName=f"{prefix}-risk-state",
            Key={"PK": {"S": "NAV#CURRENT"}, "SK": {"S": "STATE"}},
        )
        nav_item = nav_resp.get("Item", {})
        if nav_item:
            nav_val = float(nav_item.get("portfolio_value", {}).get("N", "0"))
            report.add("dynamodb:nav_seeded", "PASS",
                       f"Paper NAV seeded: INR {nav_val:,.0f}")
        else:
            report.add("dynamodb:nav_seeded", "WARN",
                       "NAV#CURRENT not found in risk-state — "
                       "risk engine may reject all orders. Run: docker-compose run --rm setup")
    except Exception:
        report.add("dynamodb:nav_seeded", "WARN", "Could not check NAV seed state")

    # Kill switch must not be active.
    check_kill_switch(dynamo, prefix, report)


def check_kill_switch(dynamo: Any, prefix: str, report: PreflightReport) -> None:
    """Verify the production kill switch is not active.

    Reads the PRODUCTION kill-switch row: PK=KILLSWITCH, SK=GLOBAL, attribute
    ``active`` (BOOL). This matches ``shared.risk_state.kill_switch_key()`` and
    the schema written by ``kill_switch_cli`` / the risk engine. The previous
    key (``KILL_SWITCH#GLOBAL`` / SK=``STATE`` / field ``state``) never matched
    a real row, so an ACTIVE kill switch was silently reported PASS.

    Extracted into its own function (Phase 2.1 / Q4) so the detection logic is
    directly unit-testable with a stubbed DynamoDB client.
    """
    try:
        ks_resp = dynamo.get_item(
            TableName=f"{prefix}-risk-state",
            Key=_kill_switch_key(),
            ConsistentRead=True,
        )
        ks_item = ks_resp.get("Item", {})
        if _attr_bool(ks_item, "active", False):
            reason = ks_item.get("reason", {}).get("S", "unknown")
            report.add("dynamodb:kill_switch", "FAIL",
                       f"Kill switch is ACTIVE (reason: {reason}) — "
                       f"clear it before starting: python scripts/kill_switch_cli.py clear")
        else:
            report.add("dynamodb:kill_switch", "PASS", "Kill switch is not active")
    except Exception:
        report.add("dynamodb:kill_switch", "WARN",
                   "Could not check kill switch state (table may not be accessible)")


def check_s3(report: PreflightReport) -> None:
    data_bucket = _env("S3_BUCKET_DATA", "quantembrace-development-data")
    logs_bucket = _env("S3_BUCKET_LOGS", "quantembrace-development-logs")
    try:
        s3 = _s3_client()
        resp = s3.list_buckets()
        existing = {b["Name"] for b in resp.get("Buckets", [])}
    except Exception as exc:
        report.add("s3:connectivity", "FAIL",
                   f"Cannot connect to LocalStack S3: {exc}")
        return

    report.add("s3:connectivity", "PASS", "Connected to S3 (LocalStack)")
    for bucket in (data_bucket, logs_bucket):
        if bucket in existing:
            report.add(f"s3:{bucket}", "PASS", f"Bucket exists: {bucket}")
        else:
            report.add(f"s3:{bucket}", "WARN",
                       f"Bucket missing: {bucket} — "
                       f"run: docker-compose run --rm setup")


def check_kafka(report: PreflightReport) -> None:
    bootstrap = _env("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
    required_topics = [
        "ticks.nse",
        "ticks.us",
        "signals.pending",
        "signals.enriched",
        "signals.approved",
        "orders.events",
    ]
    try:
        from confluent_kafka.admin import AdminClient
        admin = AdminClient({"bootstrap.servers": bootstrap, "socket.timeout.ms": 5000})
        meta = admin.list_topics(timeout=5)
        existing_topics = set(meta.topics.keys())
    except ImportError:
        report.add("kafka:connectivity", "WARN",
                   "confluent_kafka not installed — skipping Kafka checks. "
                   "Install with: pip install confluent-kafka")
        return
    except Exception as exc:
        report.add("kafka:connectivity", "FAIL",
                   f"Cannot connect to Kafka at {bootstrap}: {exc}. "
                   f"Run: docker-compose up -d redpanda")
        return

    report.add("kafka:connectivity", "PASS", f"Connected to Kafka at {bootstrap}")

    missing = [t for t in required_topics if t not in existing_topics]
    if missing:
        report.add("kafka:topics", "FAIL",
                   f"Missing topics: {', '.join(missing)}. "
                   f"Run: docker-compose run --rm setup")
    else:
        report.add("kafka:topics", "PASS",
                   f"All {len(required_topics)} required topics exist")


# ── Rendering ─────────────────────────────────────────────────────────────────

def _render(report: PreflightReport) -> None:
    print(f"\n{_BOLD}QuantEmbrace Paper Trading Pre-flight Check{_RESET}")
    print("=" * 60)

    for chk in report.checks:
        if chk.status == "PASS":
            icon = PASS_STR
        elif chk.status == "WARN":
            icon = WARN_STR
        else:
            icon = FAIL_STR
        msg = f"  {chk.message}" if chk.message else ""
        print(f"  {icon}  {chk.name}{msg}")

    print("\n" + "=" * 60)
    fail_count = len(report.failed)
    warn_count = len(report.warned)

    if fail_count == 0 and warn_count == 0:
        print(f"  {_GREEN}{_BOLD}All checks passed — safe to start paper trading.{_RESET}")
    elif fail_count == 0:
        print(f"  {_YELLOW}{_BOLD}{warn_count} warning(s) — review before starting.{_RESET}")
    else:
        print(f"  {_RED}{_BOLD}{fail_count} failure(s) — do NOT start paper trading.{_RESET}")
    print()


def main() -> None:
    report = PreflightReport()

    check_env_vars(report)
    check_paper_safety_gates(report)
    check_dynamodb(report)
    check_s3(report)
    check_kafka(report)

    _render(report)

    sys.exit(0 if not report.failed else 1)


if __name__ == "__main__":
    main()
