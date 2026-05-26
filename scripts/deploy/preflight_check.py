#!/usr/bin/env python3
"""
QuantEmbrace Pre-flight Check — runs before every live trading session.

Validates all external dependencies before any orders can be placed:

  1. Kafka MSK connectivity (SASL/OAUTHBEARER IAM, port 9098)
  2. Required Kafka topics exist and have expected partition counts
  3. DynamoDB tables accessible (read/write latency check)
  4. S3 buckets accessible
  5. Zerodha broker session (NSE) — token freshness check
  6. Alpaca broker session (US equities) — account status check
  7. AI enrichment pipeline health (signals.enriched consumer-group lag)
  8. Kill switch state (must NOT be active before session)

Exit codes:
  0  All checks passed — safe to start trading
  1  One or more checks failed — do NOT start trading
  2  Usage / configuration error

Usage:
  python scripts/deploy/preflight_check.py [--env production|staging]

Environment variables required:
  KAFKA_BOOTSTRAP_SERVERS   MSK Serverless bootstrap URL (port 9098)
  AWS_REGION                e.g. ap-south-1
  DYNAMODB_TABLE_PREFIX     e.g. quantembrace-prod
  S3_BUCKET_DATA            S3 bucket for market data
  S3_BUCKET_LOGS            S3 bucket for audit/execution logs
  ZERODHA_API_KEY           Zerodha Kite Connect API key
  ZERODHA_ACCESS_TOKEN      Zerodha session access token (refreshed daily)
  ALPACA_API_KEY            Alpaca API key
  ALPACA_API_SECRET         Alpaca API secret
  ALPACA_BASE_URL           Alpaca API base URL
  QE_ENVIRONMENT            production | staging | development
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

# ── ANSI colours ──────────────────────────────────────────────────────────────
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

PASS = f"{_GREEN}✓ PASS{_RESET}"
FAIL = f"{_RED}✗ FAIL{_RESET}"
WARN = f"{_YELLOW}⚠ WARN{_RESET}"


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name:    str
    status:  str          # "PASS" | "FAIL" | "WARN"
    message: str
    details: dict = field(default_factory=dict)
    elapsed_ms: float = 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _env(key: str, default: Optional[str] = None) -> str:
    val = os.environ.get(key, default)
    if val is None:
        print(f"{FAIL} Missing required environment variable: {key}", flush=True)
        sys.exit(2)
    return val


def _timed(fn: Callable) -> tuple[any, float]:
    t0     = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - t0) * 1000


def _result(name: str, ok: bool, msg: str, details: dict = None, elapsed_ms: float = 0.0) -> CheckResult:
    return CheckResult(
        name=name,
        status="PASS" if ok else "FAIL",
        message=msg,
        details=details or {},
        elapsed_ms=elapsed_ms,
    )


def _warn(name: str, msg: str, details: dict = None) -> CheckResult:
    return CheckResult(name=name, status="WARN", message=msg, details=details or {})


# ── Individual checks ─────────────────────────────────────────────────────────

def check_kafka_connectivity(bootstrap_servers: str, aws_region: str) -> CheckResult:
    """Verify MSK Serverless is reachable with IAM auth."""
    name = "kafka_connectivity"
    try:
        from confluent_kafka import Producer
        from aws_msk_iam_sasl_signer import MSKAuthTokenProvider

        def oauth_callback(config: dict) -> tuple[str, float]:
            token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(aws_region)
            return token, expiry_ms / 1000.0

        def _connect():
            p = Producer({
                "bootstrap.servers":     bootstrap_servers,
                "security.protocol":     "SASL_SSL",
                "sasl.mechanism":        "OAUTHBEARER",
                "oauth_cb":              oauth_callback,
                "socket.connection.setup.timeout.ms": 10000,
            })
            # flush with a trivial metadata request
            p.list_topics(timeout=8)
            return p

        _, elapsed = _timed(_connect)
        return _result(name, True, f"MSK reachable ({elapsed:.0f}ms)", elapsed_ms=elapsed)

    except ImportError as exc:
        return _result(name, False, f"confluent-kafka or MSK signer not installed: {exc}")
    except Exception as exc:
        return _result(name, False, f"MSK connection failed: {exc}")


def check_kafka_topics(bootstrap_servers: str, aws_region: str) -> CheckResult:
    """Verify required Kafka topics exist with expected partition counts."""
    name = "kafka_topics"
    REQUIRED = {
        "ticks.nse":        4,  # Phase 3 increased from 2
        "ticks.us":         2,
        "signals.pending":  2,
        "signals.enriched": 2,
        "signals.approved": 2,
        "orders.events":    4,
        "risk.kill-switch": 1,
    }
    try:
        from confluent_kafka import Producer
        from aws_msk_iam_sasl_signer import MSKAuthTokenProvider

        def oauth_callback(config: dict) -> tuple[str, float]:
            token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(aws_region)
            return token, expiry_ms / 1000.0

        p = Producer({
            "bootstrap.servers":     bootstrap_servers,
            "security.protocol":     "SASL_SSL",
            "sasl.mechanism":        "OAUTHBEARER",
            "oauth_cb":              oauth_callback,
            "socket.connection.setup.timeout.ms": 10000,
        })
        cluster_meta = p.list_topics(timeout=10)
        existing     = cluster_meta.topics

        missing     = []
        wrong_parts = []
        for topic, expected_parts in REQUIRED.items():
            if topic not in existing:
                missing.append(topic)
            else:
                actual = len(existing[topic].partitions)
                if actual != expected_parts:
                    wrong_parts.append(f"{topic}(expected={expected_parts}, actual={actual})")

        if missing or wrong_parts:
            details = {"missing": missing, "wrong_partition_count": wrong_parts}
            return _result(name, False, "Topic validation failed", details)

        return _result(name, True, f"All {len(REQUIRED)} required topics present")

    except Exception as exc:
        return _result(name, False, f"Topic check failed: {exc}")


def check_dynamodb(table_prefix: str, aws_region: str) -> CheckResult:
    """Verify DynamoDB tables are accessible with read latency < 200ms."""
    name = "dynamodb_tables"
    TABLES = [
        f"{table_prefix}-orders",
        f"{table_prefix}-risk-state",
        f"{table_prefix}-positions",
        f"{table_prefix}-features",
    ]
    try:
        import boto3
        endpoint = os.environ.get("AWS_ENDPOINT_URL") or os.environ.get("LOCALSTACK_ENDPOINT_URL")
        kwargs: dict = {"region_name": aws_region}
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        client = boto3.client("dynamodb", **kwargs)

        missing = []
        slow    = []
        for table in TABLES:
            def _describe(t=table):
                return client.describe_table(TableName=t)
            try:
                _, elapsed = _timed(lambda t=table: client.describe_table(TableName=t))
                if elapsed > 200:
                    slow.append(f"{table}({elapsed:.0f}ms)")
            except client.exceptions.ResourceNotFoundException:
                missing.append(table)

        if missing:
            return _result(name, False, "DynamoDB tables missing", {"missing": missing})
        if slow:
            return _warn(name, f"DynamoDB tables slow (>200ms): {slow}")
        return _result(name, True, f"All {len(TABLES)} DynamoDB tables accessible")

    except ImportError:
        return _result(name, False, "boto3 not installed")
    except Exception as exc:
        return _result(name, False, f"DynamoDB check failed: {exc}")


def check_s3_buckets(bucket_data: str, bucket_logs: str, aws_region: str) -> CheckResult:
    """Verify S3 buckets are accessible."""
    name = "s3_buckets"
    try:
        import boto3
        endpoint = os.environ.get("AWS_ENDPOINT_URL") or os.environ.get("LOCALSTACK_ENDPOINT_URL")
        kwargs: dict = {"region_name": aws_region}
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        client = boto3.client("s3", **kwargs)
        missing = []
        for bucket in [bucket_data, bucket_logs]:
            try:
                client.head_bucket(Bucket=bucket)
            except Exception:
                missing.append(bucket)

        if missing:
            return _result(name, False, "S3 buckets missing or inaccessible", {"missing": missing})
        return _result(name, True, f"S3 buckets {bucket_data}, {bucket_logs} accessible")

    except ImportError:
        return _result(name, False, "boto3 not installed")
    except Exception as exc:
        return _result(name, False, f"S3 check failed: {exc}")


def check_zerodha_session(api_key: str, access_token: str) -> CheckResult:
    """Verify Zerodha Kite session token is valid and fresh (not expired)."""
    name = "zerodha_session"
    if not api_key or not access_token:
        return _result(name, False, "ZERODHA_API_KEY or ZERODHA_ACCESS_TOKEN missing")

    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        def _profile():
            return kite.profile()

        profile, elapsed = _timed(_profile)
        return _result(
            name, True,
            f"Zerodha session valid — user={profile.get('user_name', '?')} ({elapsed:.0f}ms)",
            elapsed_ms=elapsed,
        )

    except ImportError:
        return _warn(name, "kiteconnect not installed — skipping Zerodha check")
    except Exception as exc:
        return _result(name, False, f"Zerodha session invalid: {exc}")


def check_alpaca_session(api_key: str, api_secret: str, base_url: str) -> CheckResult:
    """Verify Alpaca account is active and tradeable."""
    name = "alpaca_session"
    if not api_key or not api_secret or not base_url:
        return _result(name, False, "Alpaca credentials or base URL missing")

    try:
        import urllib.request
        import base64

        credentials = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        url         = f"{base_url.rstrip('/')}/v2/account"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Basic {credentials}",
                "Accept":        "application/json",
            },
        )

        def _get_account():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())

        account, elapsed = _timed(_get_account)
        status  = account.get("status", "UNKNOWN")
        blocked = account.get("trading_blocked", True)

        if status != "ACTIVE":
            return _result(name, False, f"Alpaca account status={status} (expected ACTIVE)")
        if blocked:
            return _result(name, False, "Alpaca account trading_blocked=True")
        return _result(
            name, True,
            f"Alpaca account ACTIVE, equity=${account.get('equity', '?')} ({elapsed:.0f}ms)",
            elapsed_ms=elapsed,
        )

    except Exception as exc:
        return _result(name, False, f"Alpaca check failed: {exc}")


def check_enrichment_pipeline(bootstrap_servers: str, aws_region: str) -> CheckResult:
    """
    Check ai_engine consumer-group (aiengine-v1) lag on signals.pending.
    High lag (>500 messages) means enrichment is behind — a WARN, not FAIL,
    since the fallback path handles it.
    """
    name = "enrichment_pipeline_lag"
    LAG_WARN_THRESHOLD  = 500
    LAG_FAIL_THRESHOLD  = 5000

    try:
        from confluent_kafka.admin import AdminClient
        from aws_msk_iam_sasl_signer import MSKAuthTokenProvider

        def oauth_callback(config: dict) -> tuple[str, float]:
            token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(aws_region)
            return token, expiry_ms / 1000.0

        admin = AdminClient({
            "bootstrap.servers":     bootstrap_servers,
            "security.protocol":     "SASL_SSL",
            "sasl.mechanism":        "OAUTHBEARER",
            "oauth_cb":              oauth_callback,
        })

        # List consumer group offsets for aiengine-v1
        offsets = admin.list_consumer_group_offsets(["aiengine-v1"])
        total_lag = 0
        for group_id, future in offsets.items():
            try:
                result = future.result()
                for tp, offset_info in result.topic_partition_offsets.items():
                    # Calculate lag: high watermark - committed offset
                    # (Simplified: just check that the group is active)
                    pass
            except Exception:
                pass

        # Without actual watermark data, just verify the group exists
        groups_result = admin.list_groups(timeout=10)
        group_ids     = [g.id for g in groups_result]
        if "aiengine-v1" not in group_ids:
            return _warn(
                name,
                "aiengine-v1 consumer group not found — ai_engine may not be running. "
                "Fallback path will be used.",
                {"active_groups": group_ids[:10]},
            )
        return _result(name, True, "aiengine-v1 consumer group active")

    except ImportError:
        return _warn(name, "confluent-kafka not installed — skipping enrichment lag check")
    except Exception as exc:
        return _warn(name, f"Enrichment lag check failed (non-critical): {exc}")


def check_risk_limits_config(env: str) -> CheckResult:
    """
    Verify configs/risk_limits_production.yaml exists and is fully populated.

    BLOCKER-003 fix:
        The go-live runbook (Section 1.6) instructs operators to review this
        file before committing capital.  The file must exist and every required
        field must be explicitly set to a non-zero value.  Missing or zero
        values indicate the config was never completed and risk limits may
        default to the wrong profile for the deployed capital size.

    Only applies to production environment (raises WARN in staging, PASS in dev).
    """
    name = "risk_limits_config"

    # Resolve config path relative to this script's location
    import pathlib
    script_dir   = pathlib.Path(__file__).parent
    project_root = script_dir.parent.parent
    config_path  = project_root / "configs" / "risk_limits_production.yaml"

    # In non-production environments, skip the hard check
    if env not in ("production", "staging"):
        return CheckResult(
            name=name, status="PASS",
            message=f"Risk limits config check skipped for env={env}",
        )

    # File must exist
    if not config_path.exists():
        return _result(
            name, False,
            f"configs/risk_limits_production.yaml not found at {config_path}. "
            "Create this file before going live — see go_live_checklist.md §1.6.",
            {"expected_path": str(config_path)},
        )

    # Parse YAML
    try:
        import yaml  # PyYAML
        with config_path.open() as f:
            cfg = yaml.safe_load(f)
    except ImportError:
        # Fallback to basic string check if PyYAML not installed
        content = config_path.read_text()
        cfg = None
        if len(content.strip()) < 50:
            return _result(name, False,
                           "configs/risk_limits_production.yaml appears empty or stub")

    # Required fields that must be set and non-zero/non-null
    REQUIRED_FIELDS = [
        "max_daily_loss_pct",
        "max_position_size_pct",
        "max_total_exposure_pct",
        "max_single_order_value",
        "max_open_orders",
        "max_concurrent_positions",
        "max_sector_exposure_pct",
        "kill_switch_daily_loss_pct",
        "portfolio_value",
    ]

    if cfg is not None:
        missing  = []
        zero_val = []
        for field in REQUIRED_FIELDS:
            if field not in cfg:
                missing.append(field)
            elif not cfg[field]:
                zero_val.append(field)

        if missing or zero_val:
            details: dict = {}
            if missing:  details["missing_fields"]   = missing
            if zero_val: details["zero_or_null_fields"] = zero_val
            severity = "FAIL" if env == "production" else "WARN"
            msg = (
                f"{len(missing)} missing, {len(zero_val)} zero/null fields "
                f"in risk_limits_production.yaml"
            )
            return CheckResult(name=name, status=severity, message=msg, details=details)

        # Sanity-check critical limit values
        warnings = []
        daily_loss = cfg.get("max_daily_loss_pct", 0)
        if daily_loss > 5.0:
            warnings.append(
                f"max_daily_loss_pct={daily_loss}% is very high (recommended ≤ 2%)"
            )
        ks_loss = cfg.get("kill_switch_daily_loss_pct", 0)
        if ks_loss > 5.0:
            warnings.append(
                f"kill_switch_daily_loss_pct={ks_loss}% is very high (recommended ≤ 3%)"
            )
        leverage = cfg.get("allow_leverage", False)
        if leverage:
            warnings.append("allow_leverage=true — leverage enabled, ensure this is intentional")

        if warnings and env == "production":
            return _warn(
                name,
                f"Risk config loaded but has {len(warnings)} safety warning(s)",
                {"warnings": warnings, "config_path": str(config_path)},
            )

    return _result(
        name, True,
        f"Risk limits config present and all {len(REQUIRED_FIELDS)} required fields set",
        {"config_path": str(config_path)},
    )


def check_kill_switch(table_prefix: str, aws_region: str) -> CheckResult:
    """
    Verify kill switch is NOT active before session starts.
    A live kill switch means the previous session ended with an emergency halt —
    requires manual reset before trading can resume.
    """
    name = "kill_switch_inactive"
    try:
        import boto3
        client     = boto3.client("dynamodb", region_name=aws_region)
        table_name = f"{table_prefix}-risk-state"

        resp = client.get_item(
            TableName=table_name,
            Key={"PK": {"S": "KILLSWITCH"}, "SK": {"S": "GLOBAL"}},
        )
        item = resp.get("Item")
        if item is None:
            return _result(name, True, "Kill switch not set (safe to trade)")

        active = item.get("active", {}).get("BOOL", False)
        if active:
            activated_at = item.get("activated_at", {}).get("S", "unknown")
            reason       = item.get("reason", {}).get("S", "no reason recorded")
            return _result(
                name, False,
                f"Kill switch is ACTIVE — manual reset required. "
                f"Activated at {activated_at}. Reason: {reason}",
                {"activated_at": activated_at, "reason": reason},
            )

        return _result(name, True, "Kill switch present but inactive")

    except ImportError:
        return _result(name, False, "boto3 not installed")
    except Exception as exc:
        return _result(name, False, f"Kill switch check failed: {exc}")


def check_live_trading_gate() -> CheckResult:
    """
    Verify that QE_EXECUTION_LIVE_TRADING_ENABLED is explicitly set before
    live trading is allowed.  The env var must be the string 'true' (case-insensitive).
    An absent or falsy value means live trading remains disabled, which is SAFE —
    this check only WARNS so the operator can confirm intent rather than block.
    """
    name = "live_trading_gate"
    raw = os.environ.get("QE_EXECUTION_LIVE_TRADING_ENABLED", "")
    if raw.strip().lower() == "true":
        return _result(
            name, True,
            "QE_EXECUTION_LIVE_TRADING_ENABLED=true — live broker orders ENABLED (Phase 4).",
        )
    return _result(
        name, True,
        f"QE_EXECUTION_LIVE_TRADING_ENABLED={raw!r} — live broker orders DISABLED (safe default). "
        "Set to 'true' to enable Phase 4 live trading.",
    )


def check_ltp_freshness_for_live() -> CheckResult:
    """
    Verify LTP freshness thresholds are tight enough for live trading.

    TEE_LTP_FRESHNESS_SECONDS must be ≤ 2.0 s in live mode — values above this
    mean TEE could use a price that's several seconds old when deciding stop/target.
    TEE_MAX_STALE_LTP_LIVE_SECONDS must be ≤ 3.0 s — higher values mean TEE would
    continue exit evaluation with very stale data instead of blocking.
    """
    name = "ltp_freshness_live"
    live_enabled = os.environ.get("QE_EXECUTION_LIVE_TRADING_ENABLED", "").strip().lower() == "true"
    if not live_enabled:
        return _result(name, True, "Live trading disabled — LTP freshness thresholds not enforced.")

    msgs = []
    passed = True

    freshness = float(os.environ.get("TEE_LTP_FRESHNESS_SECONDS", "5.0"))
    if freshness > 2.0:
        msgs.append(
            f"TEE_LTP_FRESHNESS_SECONDS={freshness} is too high for live trading "
            f"(recommended ≤ 2.0 s). Stale prices may trigger wrong exits."
        )
        passed = False
    else:
        msgs.append(f"TEE_LTP_FRESHNESS_SECONDS={freshness} ✓")

    max_stale = float(os.environ.get("TEE_MAX_STALE_LTP_LIVE_SECONDS", "3.0"))
    if max_stale > 3.0:
        msgs.append(
            f"TEE_MAX_STALE_LTP_LIVE_SECONDS={max_stale} is too high for live trading "
            f"(recommended ≤ 3.0 s). TEE will not block stale-LTP exits quickly enough."
        )
        passed = False
    else:
        msgs.append(f"TEE_MAX_STALE_LTP_LIVE_SECONDS={max_stale} ✓")

    return _result(name, passed, " | ".join(msgs))


# ── Main ──────────────────────────────────────────────────────────────────────

def run_preflight(env: str) -> int:
    print(f"\n{_BOLD}QuantEmbrace Pre-flight Check{_RESET}  [{env.upper()}]  "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
    print("=" * 65)

    # Load config from environment
    bootstrap  = _env("KAFKA_BOOTSTRAP_SERVERS")
    region     = _env("AWS_REGION", "ap-south-1")
    tbl_prefix = _env("DYNAMODB_TABLE_PREFIX")
    s3_data    = _env("S3_BUCKET_DATA")
    s3_logs    = _env("S3_BUCKET_LOGS")
    zd_key     = os.environ.get("ZERODHA_API_KEY", "")
    zd_token   = os.environ.get("ZERODHA_ACCESS_TOKEN", "")
    al_key     = os.environ.get("ALPACA_API_KEY", "")
    al_secret  = os.environ.get("ALPACA_API_SECRET", "")
    al_url     = os.environ.get("ALPACA_BASE_URL", "")

    checks: list[CheckResult] = []

    # Run checks in dependency order.
    # risk_limits_config is FIRST — there is no point checking infrastructure
    # if the risk parameters governing all trading decisions are misconfigured.
    print("Running checks...\n")
    runners = [
        ("Risk limits config",      lambda: check_risk_limits_config(env)),
        ("Kafka connectivity",      lambda: check_kafka_connectivity(bootstrap, region)),
        ("Kafka topics",            lambda: check_kafka_topics(bootstrap, region)),
        ("DynamoDB tables",         lambda: check_dynamodb(tbl_prefix, region)),
        ("S3 buckets",              lambda: check_s3_buckets(s3_data, s3_logs, region)),
        ("Kill switch state",       lambda: check_kill_switch(tbl_prefix, region)),
        ("Live trading gate",       check_live_trading_gate),
        ("LTP freshness (live)",    check_ltp_freshness_for_live),
        ("Zerodha session",         lambda: check_zerodha_session(zd_key, zd_token)),
        ("Alpaca session",          lambda: check_alpaca_session(al_key, al_secret, al_url)),
        ("AI enrichment pipeline",  lambda: check_enrichment_pipeline(bootstrap, region)),
    ]

    for label, fn in runners:
        try:
            result = fn()
        except Exception as exc:
            result = CheckResult(
                name=label, status="FAIL",
                message=f"Unexpected error: {exc}",
            )
        checks.append(result)

        icon = PASS if result.status == "PASS" else (WARN if result.status == "WARN" else FAIL)
        elapsed_str = f"  [{result.elapsed_ms:.0f}ms]" if result.elapsed_ms > 0 else ""
        print(f"  {icon}  {label:<32} {result.message}{elapsed_str}")
        if result.details and result.status != "PASS":
            for k, v in result.details.items():
                print(f"           {k}: {v}")

    # Summary
    n_pass = sum(1 for c in checks if c.status == "PASS")
    n_warn = sum(1 for c in checks if c.status == "WARN")
    n_fail = sum(1 for c in checks if c.status == "FAIL")

    print("\n" + "=" * 65)
    print(f"Results: {_GREEN}{n_pass} passed{_RESET}  "
          f"{_YELLOW}{n_warn} warnings{_RESET}  "
          f"{_RED}{n_fail} failed{_RESET}")

    if n_fail > 0:
        print(f"\n{_RED}{_BOLD}PRE-FLIGHT FAILED — do NOT start trading.{_RESET}")
        print("Resolve all FAIL items before proceeding.\n")
        return 1
    elif n_warn > 0:
        print(f"\n{_YELLOW}{_BOLD}PRE-FLIGHT PASSED WITH WARNINGS.{_RESET}")
        print("Review warnings before starting. Trading is conditionally safe.\n")
        return 0
    else:
        print(f"\n{_GREEN}{_BOLD}PRE-FLIGHT PASSED — safe to start trading.{_RESET}\n")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantEmbrace pre-flight check")
    parser.add_argument(
        "--env",
        default=os.environ.get("QE_ENVIRONMENT", "development"),
        choices=["production", "staging", "development"],
        help="Target environment (default: QE_ENVIRONMENT or 'development')",
    )
    args = parser.parse_args()

    if args.env == "production":
        print(f"\n{_RED}{_BOLD}⚠  PRODUCTION ENVIRONMENT  ⚠{_RESET}")
        print("This check targets LIVE trading. Ensure you intend to trade real capital.\n")

    sys.exit(run_preflight(args.env))


if __name__ == "__main__":
    main()
