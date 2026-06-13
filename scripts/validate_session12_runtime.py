#!/usr/bin/env python3
"""
Session 12 Runtime Validation Script — QuantEmbrace

Verifies that the risk_engine Docker image is up-to-date and that
quality-gate validators are active before starting a paper trading session.

Session 12 is the FIRST VALID quality-gate test session. Sessions 10 and 11
ran on a stale image that was missing the quality-gate validators.

Usage:
    python scripts/validate_session12_runtime.py
    python scripts/validate_session12_runtime.py --prefix quantembrace-development --endpoint http://localhost:4566

Exit codes:
    0 — all critical checks PASS
    1 — one or more critical checks FAIL
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────

# Container name produced by docker-compose for the risk_engine service
_RISK_CONTAINER = "quantembrace-ahedgelevelalgotradingsystem-risk_engine-1"

# Paths inside the container
_CONTAINER_QUALITY_GATE_PATH = "/app/services/risk_engine/validators/paper_quality_gate_validator.py"
_CONTAINER_SYMBOL_COUNT_PATH = "/app/services/risk_engine/validators/symbol_trade_count_validator.py"
_CONTAINER_SERVICE_PATH      = "/app/services/risk_engine/service.py"
_CONTAINER_YAML_PATH         = "/app/services/strategy_engine/config/paper_optimization.yaml"

# Host-side bind-mounted path alternatives (for when container is not running)
_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOST_QUALITY_GATE_PATH = _REPO_ROOT / "services/risk_engine/validators/paper_quality_gate_validator.py"
_HOST_SYMBOL_COUNT_PATH = _REPO_ROOT / "services/risk_engine/validators/symbol_trade_count_validator.py"
_HOST_SERVICE_PATH      = _REPO_ROOT / "services/risk_engine/service.py"
_HOST_YAML_PATH         = _REPO_ROOT / "services/strategy_engine/config/paper_optimization.yaml"

_IST = timezone(timedelta(hours=5, minutes=30))

# Confidence thresholds used in synthetic signal injection test
_LOW_QUALITY_CONFIDENCE  = 0.88
_HIGH_QUALITY_CONFIDENCE = 0.91
_LOW_QUALITY_RR  = 0.95   # below vwap_reversion threshold of 1.20
_HIGH_QUALITY_RR = 1.25   # above vwap_reversion threshold of 1.20


# ── Helpers ────────────────────────────────────────────────────────────────────


def _ok(label: str, detail: str = "") -> None:
    suffix = f"  ({detail})" if detail else ""
    print(f"[PASS] {label}{suffix}")


def _fail(label: str, detail: str = "") -> None:
    suffix = f"  ({detail})" if detail else ""
    print(f"[FAIL] {label}{suffix}")


def _check_container_file(container: str, path: str) -> bool:
    """Return True if the file exists in the running container."""
    try:
        result = subprocess.run(
            ["docker", "exec", container, "test", "-f", path],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _check_container_running(container: str) -> bool:
    """Return True if the named container is currently running."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _grep_container_file(container: str, path: str, pattern: str) -> bool:
    """Return True if the pattern is found in the container file."""
    try:
        result = subprocess.run(
            ["docker", "exec", container, "grep", "-q", pattern, path],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _grep_host_file(path: Path, pattern: str) -> bool:
    """Return True if the pattern is found in a host-side file."""
    try:
        text = path.read_text(encoding="utf-8")
        return pattern in text
    except (OSError, UnicodeDecodeError):
        return False


def _get_container_logs(container: str, tail: int = 200) -> str:
    """Return the last N lines of container stdout logs."""
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(tail), container],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout + result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _inject_synthetic_signal(
    container: str,
    signal_id: str,
    confidence: float,
    stop_loss: float,
    take_profit: float,
    price: float = 2500.0,
    symbol: str = "VALTEST",
) -> bool:
    """Inject a synthetic signal into the running risk_engine container via confluent_kafka.

    Produces directly to signals.enriched (the primary risk_engine consumption topic)
    using the same pattern validated during post-rebuild testing.

    Returns True if the injection subprocess exited 0, False otherwise.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    expires_iso = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()

    payload_dict = {
        # ── Event envelope (required by validate_event) ──
        "event_id":      str(uuid.uuid4()),
        "event_type":    "SIGNAL_ENRICHED",
        "schema_version": "4.0",
        "source":        "ai_engine",
        "published_time": now_iso,
        # ── Signal fields ──
        "signal_id":        signal_id,
        "strategy_id":      "vwap_reversion",
        "strategy_name":    "vwap_reversion",
        "instrument_id":    f"NSE:{symbol}",
        "symbol":           symbol,
        "market":           "NSE",
        "direction":        "BUY",
        "quantity":         1,
        "price_at_signal":  price,
        "confidence":       confidence,
        "stop_loss":        stop_loss,
        "take_profit":      take_profit,
        "paper_trade":      True,
        "generated_at":     now_iso,
        "signal_time":      now_iso,
        "expires_at":       expires_iso,
        "product_type":     "MIS",
        "trace_id":         f"validate-{signal_id}",
        "metadata":         {"injected_by": "validate_session12_runtime"},
        # ── Enrichment fields ──
        "regime":                "ranging",
        "regime_confidence":     0.75,
        "quality_score":         confidence,
        "filtered":              False,
        "enriched_at":           now_iso,
        "enrichment_latency_ms": 1.0,
        "model_versions":        {},
    }
    # Embed the dict literal directly — avoids double-encoding via json.dumps
    payload_repr = repr(payload_dict)

    python_snippet = f"""
import json
from confluent_kafka import Producer

conf = {{'bootstrap.servers': 'redpanda:9092'}}
p = Producer(conf)
payload = {payload_repr}
p.produce('signals.enriched', value=json.dumps(payload).encode('utf-8'), key={repr(signal_id)}.encode('utf-8'))
p.flush(timeout=5)
print('produced signal {signal_id}')
"""

    try:
        result = subprocess.run(
            ["docker", "exec", container, "python3", "-c", python_snippet],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _check_dynamodb_counters(dynamo, risk_state_table: str) -> dict:
    """Read quality gate rejection counters from DynamoDB for today.

    Returns a dict mapping reason_code → count. Returns {} on error.
    """
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    for code in ("CONFIDENCE_BELOW_THRESHOLD", "REWARD_RISK_TOO_LOW", "MAX_TRADES_PER_SYMBOL_REACHED"):
        try:
            resp = dynamo.get_item(
                TableName=risk_state_table,
                Key={
                    "PK": {"S": f"QUALITY_GATE_REJECT#{code}"},
                    "SK": {"S": f"DAY#{today}"},
                },
            )
            item = resp.get("Item")
            if item:
                raw = item.get("count", {})
                n = raw.get("N", "0") if isinstance(raw, dict) else "0"
                counts[code] = int(float(n))
            else:
                counts[code] = 0
        except Exception:
            pass
    return counts


# ── Main validation logic ──────────────────────────────────────────────────────


def run_checks(prefix: str, endpoint: str) -> list[tuple[str, bool]]:
    """Run all validation checks. Returns a list of (label, passed) tuples."""

    results: list[tuple[str, bool]] = []

    container_running = _check_container_running(_RISK_CONTAINER)

    # ── Check 1: paper_quality_gate_validator.py exists ───────────────────────
    label = "paper_quality_gate_validator.py exists in container"
    if container_running:
        passed = _check_container_file(_RISK_CONTAINER, _CONTAINER_QUALITY_GATE_PATH)
    else:
        # Fallback: verify on host (bind-mounted source)
        passed = _HOST_QUALITY_GATE_PATH.exists()
    results.append((label, passed))

    # ── Check 2: symbol_trade_count_validator.py exists ───────────────────────
    label = "symbol_trade_count_validator.py exists in container"
    if container_running:
        passed = _check_container_file(_RISK_CONTAINER, _CONTAINER_SYMBOL_COUNT_PATH)
    else:
        passed = _HOST_SYMBOL_COUNT_PATH.exists()
    results.append((label, passed))

    # ── Check 3: risk_engine/service.py contains quality-gate wiring ─────────
    label = "risk_engine/service.py contains quality-gate wiring"
    wiring_pattern = "PaperQualityGateValidator"
    if container_running:
        passed = _grep_container_file(_RISK_CONTAINER, _CONTAINER_SERVICE_PATH, wiring_pattern)
    else:
        passed = _grep_host_file(_HOST_SERVICE_PATH, wiring_pattern)
    results.append((label, passed))

    # ── Check 4: paper_optimization.yaml exists ───────────────────────────────
    label = "paper_optimization.yaml exists at expected path"
    if container_running:
        passed = _check_container_file(_RISK_CONTAINER, _CONTAINER_YAML_PATH)
    else:
        passed = _HOST_YAML_PATH.exists()
    results.append((label, passed))

    # ── Check 5: Section 17 counters readable from DynamoDB ──────────────────
    label = "Section 17 counters readable from DynamoDB"
    dynamo = None
    risk_state_table = f"{prefix}-risk-state"
    try:
        import boto3
        boto_kwargs: dict = {}
        if endpoint:
            boto_kwargs["endpoint_url"] = endpoint
        dynamo = boto3.client(
            "dynamodb",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-south-1"),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
            **boto_kwargs,
        )
        # Attempt a read — any error (table missing, connectivity) fails the check
        dynamo.get_item(
            TableName=risk_state_table,
            Key={
                "PK": {"S": "QUALITY_GATE_REJECT#CONFIDENCE_BELOW_THRESHOLD"},
                "SK": {"S": f"DAY#{datetime.now(_IST).strftime('%Y-%m-%d')}"},
            },
        )
        passed = True
    except Exception as exc:
        passed = False
        dynamo = None
    results.append((label, passed))

    # ── Check 6: orders_table configured for MonitoringStatusService ─────────
    label = "orders_table configured for MonitoringStatusService"
    orders_table = f"{prefix}-orders"
    try:
        if dynamo is not None:
            # A successful describe_table confirms the table exists and is reachable
            dynamo.describe_table(TableName=orders_table)
            passed = True
        else:
            passed = False
    except Exception:
        passed = False
    results.append((label, passed))

    # ── Check 7: live trading disabled (env check) ────────────────────────────
    label = "live trading disabled (env check)"
    live_enabled_raw = os.environ.get("QE_EXECUTION_LIVE_TRADING_ENABLED", "").strip().lower()
    # Must be absent or explicitly "false" — any truthy value is a failure
    passed = live_enabled_raw in ("", "false", "0", "no")
    results.append((label, passed))

    # ── Checks 8 & 9: Synthetic signal injection ──────────────────────────────
    # Only possible when the container is running.
    if container_running:
        low_signal_id  = f"validate-low-{uuid.uuid4().hex[:8]}"
        high_signal_id = f"validate-high-{uuid.uuid4().hex[:8]}"

        # Synthetic symbols (VALTEST_L / VALTEST_H) are not real NSE instruments.
        # They will never appear in the DynamoDB orders table from a real session,
        # so SymbolTradeCountValidator always starts at count=0 for them.
        # entry=2500, stop=2480 → risk=20; tp=2519 → reward=19 → R:R≈0.95 (below 1.20)
        low_injected = _inject_synthetic_signal(
            _RISK_CONTAINER,
            signal_id=low_signal_id,
            confidence=_LOW_QUALITY_CONFIDENCE,
            stop_loss=2480.0,
            take_profit=2519.0,
            symbol="VALTEST_L",
        )
        # entry=2500, stop=2450 → risk=50; tp=2575.0 → reward=75 → R:R=1.5 (above ADR-030 vwap_reversion floor 1.40)
        high_injected = _inject_synthetic_signal(
            _RISK_CONTAINER,
            signal_id=high_signal_id,
            confidence=_HIGH_QUALITY_CONFIDENCE,
            stop_loss=2450.0,
            take_profit=2575.0,
            symbol="VALTEST_H",
        )

        if low_injected or high_injected:
            # Give the risk_engine time to process both signals
            print(f"  Injected synthetic signals; waiting 5s for processing ...")
            time.sleep(5)

        # Check rejection of low-quality signal
        label = "Synthetic low-quality signal rejected (confidence=0.88, R:R=0.95)"
        if low_injected:
            logs = _get_container_logs(_RISK_CONTAINER, tail=300)
            # risk_engine logs "REJECTED" along with signal_id for rejected signals
            passed = (
                low_signal_id in logs
                and ("REJECTED" in logs or "CONFIDENCE_BELOW_THRESHOLD" in logs or "REWARD_RISK_TOO_LOW" in logs)
            )
        else:
            passed = False
        results.append((label, passed))

        # Check approval of high-quality signal
        label = "Synthetic high-quality signal approved (confidence=0.91, R:R=1.5)"
        if high_injected:
            logs = _get_container_logs(_RISK_CONTAINER, tail=300)
            passed = high_signal_id in logs and "APPROVED" in logs
        else:
            passed = False
        results.append((label, passed))
    else:
        results.append((
            "Synthetic low-quality signal rejected (confidence=0.88, R:R=0.95)",
            False,
        ))
        results.append((
            "Synthetic high-quality signal approved (confidence=0.91, R:R=1.5)",
            False,
        ))

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Session 12 runtime validation — confirm quality gates are active"
    )
    parser.add_argument(
        "--prefix",
        default="quantembrace-development",
        help="DynamoDB table name prefix (default: quantembrace-development)",
    )
    parser.add_argument(
        "--endpoint",
        default="http://localhost:4566",
        help="AWS endpoint URL for LocalStack (default: http://localhost:4566)",
    )
    args = parser.parse_args()

    print()
    print("=" * 72)
    print("  QuantEmbrace — Session 12 Runtime Validation")
    print(f"  prefix={args.prefix}  endpoint={args.endpoint}")
    print(f"  {datetime.now(_IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 72)
    print()

    results = run_checks(prefix=args.prefix, endpoint=args.endpoint)

    all_pass = True
    for label, passed in results:
        if passed:
            _ok(label)
        else:
            _fail(label)
            all_pass = False

    print()
    print("=" * 72)
    if all_pass:
        print("  Session 12 runtime validation: PASS")
        print("  All quality-gate checks passed. Safe to start paper session.")
    else:
        failed = sum(1 for _, p in results if not p)
        print(f"  Session 12 runtime validation: FAIL  ({failed} check(s) failed)")
        print("  Do NOT start a paper session until all checks pass.")
        print("  Common fix: docker-compose build risk_engine && docker-compose up -d risk_engine")
    print("=" * 72)
    print()

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
