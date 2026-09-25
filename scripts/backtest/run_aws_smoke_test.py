#!/usr/bin/env python3
"""AWS live smoke test — validates real provisioned infrastructure without real data.

Backtest-only. No broker APIs. No live/paper trading. No real NSE data required.
All backtest results are SYNTHETIC and NON-AUTHORITATIVE.

Checks (always run):
  1. AWS identity  — sts:GetCallerIdentity confirms valid credentials
  2. DynamoDB      — qe-bt-runs / qe-bt-checkpoints / qe-bt-datasets all ACTIVE
  3. S3            — quantembrace-backtest-data / quantembrace-backtest-results accessible
  4. ASG           — quantembrace-backtest-worker exists (min=0 scale-from-zero)
  5. VPC           — backtest VPC exists, CIDR disjoint from live

Optional full run (--run):
  6. Creates a real run in qe-bt-runs (tiny synthetic dataset, momentum adapter)
  7. Verifies the run appears in DynamoDB after completion
  8. Verifies the report was uploaded to S3

Usage:
    # Connectivity check only (safe, no writes)
    python scripts/backtest/run_aws_smoke_test.py

    # Full live run against real DynamoDB + S3 (creates real records)
    python scripts/backtest/run_aws_smoke_test.py --run

    # Full run, skip S3 upload (writes only to local reports/)
    python scripts/backtest/run_aws_smoke_test.py --run --no-s3
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

# ── constants ─────────────────────────────────────────────────────────────────

REGION = "ap-south-1"
RUNS_TABLE = "qe-bt-runs"
CHECKPOINTS_TABLE = "qe-bt-checkpoints"
DATASETS_TABLE = "qe-bt-datasets"
DATA_BUCKET = "quantembrace-backtest-data"
RESULTS_BUCKET = "quantembrace-backtest-results"
ASG_NAME = "quantembrace-backtest-worker"
BACKTEST_VPC_CIDR = "10.40.0.0/16"
LIVE_VPC_CIDR_PREFIX = "10.0."  # live VPC — must NOT match backtest

EXPECTED_TABLES = [RUNS_TABLE, CHECKPOINTS_TABLE, DATASETS_TABLE]
EXPECTED_BUCKETS = [DATA_BUCKET, RESULTS_BUCKET]

SMOKE_SYMBOLS = ["RELIANCE", "INFY"]
SMOKE_START = date(2020, 1, 1)
SMOKE_END = date(2020, 2, 1)  # 1 month only — minimal cost


# ── helpers ───────────────────────────────────────────────────────────────────

def _boto3():
    try:
        import boto3
        return boto3
    except ImportError:
        print("FAIL  boto3 not installed — run: pip install boto3")
        sys.exit(1)


def _ok(msg: str) -> None:
    print(f"  PASS  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}")


def _warn(msg: str) -> None:
    print(f"  WARN  {msg}")


# ── check functions ────────────────────────────────────────────────────────────

def check_identity(boto3) -> dict:
    """1. Verify AWS credentials are valid."""
    print("\n[1] AWS Identity")
    client = boto3.client("sts", region_name=REGION)
    try:
        resp = client.get_caller_identity()
        account = resp["Account"]
        arn = resp["Arn"]
        _ok(f"Account={account}  ARN={arn}")
        return {"account": account, "arn": arn}
    except Exception as e:
        _fail(f"sts:GetCallerIdentity failed: {e}")
        print("\n  Fix: run 'aws configure' or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY")
        sys.exit(1)


def check_dynamodb(boto3) -> bool:
    """2. Verify all DynamoDB backtest tables exist and are ACTIVE."""
    print("\n[2] DynamoDB tables")
    client = boto3.client("dynamodb", region_name=REGION)
    all_ok = True
    for table in EXPECTED_TABLES:
        try:
            resp = client.describe_table(TableName=table)
            status = resp["Table"]["TableStatus"]
            if status == "ACTIVE":
                _ok(f"{table}  status=ACTIVE")
            else:
                _fail(f"{table}  status={status} (expected ACTIVE)")
                all_ok = False
        except client.exceptions.ResourceNotFoundException:
            _fail(f"{table}  NOT FOUND — was terraform apply successful?")
            all_ok = False
        except Exception as e:
            _fail(f"{table}  error: {e}")
            all_ok = False
    return all_ok


def check_s3(boto3) -> bool:
    """3. Verify both S3 backtest buckets exist and are in ap-south-1."""
    print("\n[3] S3 buckets")
    client = boto3.client("s3", region_name=REGION)
    all_ok = True
    for bucket in EXPECTED_BUCKETS:
        try:
            resp = client.get_bucket_location(Bucket=bucket)
            loc = resp.get("LocationConstraint") or "us-east-1"
            if loc == REGION:
                _ok(f"s3://{bucket}  region={loc}")
            else:
                _warn(f"s3://{bucket}  region={loc} (expected {REGION}) — check Terraform config")
        except client.exceptions.NoSuchBucket:
            _fail(f"s3://{bucket}  NOT FOUND — was terraform apply successful?")
            all_ok = False
        except Exception as e:
            _fail(f"s3://{bucket}  error: {e}")
            all_ok = False
    return all_ok


def check_asg(boto3) -> bool:
    """4. Verify EC2 ASG exists and is scale-from-zero."""
    print("\n[4] EC2 Auto Scaling Group")
    client = boto3.client("autoscaling", region_name=REGION)
    try:
        resp = client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[ASG_NAME],
        )
        groups = resp.get("AutoScalingGroups", [])
        if not groups:
            _fail(f"{ASG_NAME}  NOT FOUND")
            return False
        g = groups[0]
        mn, mx, desired = g["MinSize"], g["MaxSize"], g["DesiredCapacity"]
        _ok(f"{ASG_NAME}  min={mn} desired={desired} max={mx}")
        if mn != 0:
            _warn(f"  Min={mn} — expected 0 (scale-from-zero)")
        return True
    except Exception as e:
        _fail(f"{ASG_NAME}  error: {e}")
        return False


def check_vpc(boto3) -> bool:
    """5. Verify backtest VPC exists with CIDR disjoint from live."""
    print("\n[5] VPC isolation")
    client = boto3.client("ec2", region_name=REGION)
    try:
        resp = client.describe_vpcs(
            Filters=[
                {"Name": "cidr", "Values": [BACKTEST_VPC_CIDR]},
                {"Name": "tag:Project", "Values": ["QuantEmbrace"]},
                {"Name": "tag:Environment", "Values": ["backtest"]},
            ]
        )
        vpcs = resp.get("Vpcs", [])
        if not vpcs:
            _fail(f"No VPC with CIDR={BACKTEST_VPC_CIDR} and Environment=backtest found")
            return False
        vpc_id = vpcs[0]["VpcId"]
        cidr = vpcs[0]["CidrBlock"]
        _ok(f"vpc_id={vpc_id}  cidr={cidr}")
        if cidr.startswith(LIVE_VPC_CIDR_PREFIX):
            _fail(f"CIDR {cidr} overlaps with live VPC range {LIVE_VPC_CIDR_PREFIX}* — ISOLATION BREACH")
            return False
        _ok(f"CIDR {cidr} is disjoint from live range {LIVE_VPC_CIDR_PREFIX}* — isolation OK")
        return True
    except Exception as e:
        _fail(f"VPC check error: {e}")
        return False


def run_full_backtest(boto3, *, upload_s3: bool = True) -> bool:
    """6–8. Create a real run in DynamoDB + S3 using synthetic data."""
    print("\n[6] Full backtest run against real AWS (synthetic data)")
    print("    Backtest-only. SYNTHETIC. NON-AUTHORITATIVE. No broker APIs.\n")

    import pandas as pd

    from backtesting.replay_engine import CandleReplayEngine, DataFrameBarSource, ReplayConfig
    from backtesting.report_writer import ReportWriter
    from backtesting.run_registry import RunSpec
    from backtesting.runner import BacktestRunner
    from backtesting.strategy_adapter import get_adapter

    # Build synthetic daily candles (30 trading days, 2 symbols)
    def _synth(symbol: str, seed: int) -> pd.DataFrame:
        rng = random.Random(seed)
        rows = []
        ts = pd.Timestamp(SMOKE_START.isoformat()).tz_localize("Asia/Kolkata")
        px = 500.0 + rng.uniform(-100, 100)
        for _ in range(30):
            o = px
            h = o * (1 + rng.uniform(0, 0.02))
            l = o * (1 - rng.uniform(0, 0.02))
            c = o * (1 + rng.uniform(-0.015, 0.015))
            rows.append({
                "timestamp": ts, "symbol": symbol, "market": "NSE",
                "segment": "EQ", "interval": "1d",
                "open": round(o, 2), "high": round(h, 2),
                "low": round(l, 2), "close": round(c, 2),
                "volume": rng.randint(100_000, 1_000_000),
            })
            ts += pd.Timedelta(days=1)
            px = c
        return pd.DataFrame(rows)

    candles = pd.concat([_synth(s, i + 7) for i, s in enumerate(SMOKE_SYMBOLS)], ignore_index=True)

    import time as _time
    _run_ts = str(int(_time.time()))
    spec = RunSpec(
        strategy="momentum",
        symbols=SMOKE_SYMBOLS,
        timeframe="1d",
        start_date=SMOKE_START,
        end_date=SMOKE_END,
        config_s3_path=f"s3://{RESULTS_BUCKET}/configs/smoke-test-{_run_ts}.json",
        data_version=f"SYNTHETIC-smoke-test-{_run_ts}",
        code_version="aws-smoke-test-v1",
        cost_model_version="v1",
        exit_policy_version="v1",
    )

    # Build ReportWriter — optionally skip S3 upload
    writer = ReportWriter(
        base_dir="reports/backtests",
        s3_bucket=RESULTS_BUCKET if upload_s3 else None,
        s3_prefix="runs",
    )

    try:
        runner = BacktestRunner.from_aws(
            runs_table=RUNS_TABLE,
            checkpoints_table=CHECKPOINTS_TABLE,
            results_bucket=RESULTS_BUCKET,
            emit_cw=False,  # skip CW metrics on smoke test
        )
        # Inject the report writer (avoids constructing a second boto3 S3 client)
        runner._report_writer = writer
    except Exception as e:
        _fail(f"BacktestRunner.from_aws() failed: {e}")
        return False

    adapter = get_adapter("momentum")
    source = DataFrameBarSource.from_dataframe(candles)
    config = ReplayConfig(timeframes=["1d"], partition_by="symbol", market_hours_filter=False)

    print(f"    run_id  = {spec.run_id()}")
    print(f"    symbols = {SMOKE_SYMBOLS}")
    print(f"    period  = {SMOKE_START} → {SMOKE_END}")
    print()

    try:
        summary = runner.run(
            spec,
            source=source,
            config=config,
            strategy_factory=lambda: adapter.build_strategy(
                SMOKE_SYMBOLS, short_window=5, long_window=15, min_confidence=0.0
            ),
            backtester_kwargs={"slippage_bps": 2.0, "spread_bps": 4.0, "commission_pct": 0.03},
        )
    except Exception as e:
        _fail(f"runner.run() raised: {e}")
        import traceback; traceback.print_exc()
        return False

    run_id = summary.run_id
    status = summary.status

    print(f"\n[7] Verify DynamoDB record")
    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(RUNS_TABLE)
    item = table.get_item(Key={"run_id": run_id}, ConsistentRead=True).get("Item")
    if item is None:
        _fail(f"run_id={run_id} NOT FOUND in {RUNS_TABLE} — DynamoDB write failed")
        return False
    _ok(f"run_id={run_id}  status={item.get('status')}  strategy={item.get('strategy')}")

    if status != "COMPLETED":
        _fail(f"Run status={status}  error={summary.error}")
        return False

    _ok(f"status=COMPLETED  partitions={summary.partitions_processed}  trades={summary.total_trades}")

    print(f"\n[8] Verify S3 report")
    if upload_s3:
        s3 = boto3.client("s3", region_name=REGION)
        try:
            resp = s3.list_objects_v2(
                Bucket=RESULTS_BUCKET,
                Prefix=f"runs/{run_id}/",
                MaxKeys=10,
            )
            keys = [o["Key"] for o in resp.get("Contents", [])]
            if not keys:
                _fail(f"No S3 objects found at s3://{RESULTS_BUCKET}/runs/{run_id}/")
                return False
            _ok(f"s3://{RESULTS_BUCKET}/runs/{run_id}/ — {len(keys)} objects uploaded")
            for k in keys:
                print(f"        {k}")
        except Exception as e:
            _fail(f"S3 list failed: {e}")
            return False
    else:
        local_dir = Path(summary.local_report_dir)
        if local_dir.exists():
            files = list(local_dir.rglob("*"))
            _ok(f"Local report at {local_dir} ({len(files)} files) — S3 upload skipped (--no-s3)")
        else:
            _warn(f"Local report dir {local_dir} not found — may still be OK")

    return True


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="AWS live smoke test — connectivity + optional synthetic run")
    ap.add_argument("--run", action="store_true", help="Run a real synthetic backtest (creates DynamoDB + S3 records)")
    ap.add_argument("--no-s3", action="store_true", help="With --run: skip S3 upload, write locally only")
    args = ap.parse_args()

    boto3 = _boto3()

    print("=" * 60)
    print("QuantEmbrace AWS Backtesting Lab — Live Smoke Test")
    print("BACKTEST-ONLY. No broker. No live trading. SYNTHETIC DATA.")
    print("=" * 60)

    identity = check_identity(boto3)
    ddb_ok = check_dynamodb(boto3)
    s3_ok = check_s3(boto3)
    asg_ok = check_asg(boto3)
    vpc_ok = check_vpc(boto3)

    connectivity_ok = ddb_ok and s3_ok and asg_ok and vpc_ok

    print("\n" + "=" * 60)
    if connectivity_ok:
        print("CONNECTIVITY  PASS — all 5 infra checks passed")
    else:
        print("CONNECTIVITY  FAIL — one or more checks failed (see above)")

    run_ok = True
    if args.run:
        run_ok = run_full_backtest(boto3, upload_s3=not args.no_s3)
        print()
        if run_ok:
            print("SYNTHETIC RUN  PASS — real DynamoDB + S3 wiring confirmed")
        else:
            print("SYNTHETIC RUN  FAIL — see errors above")
    else:
        print()
        print("Tip: add --run to also execute a real synthetic backtest against DynamoDB + S3")

    print("=" * 60)

    if not connectivity_ok or not run_ok:
        print("\nNSE data ingestion blocked until all checks pass.")
        return 1

    print("\nNext step: download NSE Bhavcopy data and ingest to S3.")
    print("  python scripts/backtest/download_bhavcopy.py --start 2020-01-01 --end 2024-12-31")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
