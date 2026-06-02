#!/usr/bin/env python3
"""Create LocalStack resources needed by CI and local unit/integration tests."""

from __future__ import annotations

import os
from typing import Any

import boto3
from botocore.exceptions import ClientError


def _endpoint_url() -> str | None:
    return os.environ.get("AWS_ENDPOINT_URL") or os.environ.get("LOCALSTACK_ENDPOINT_URL")


def _resource(service_name: str) -> Any:
    kwargs: dict[str, str] = {"region_name": os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")}
    endpoint_url = _endpoint_url()
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.resource(service_name, **kwargs)


def _table_exists(table: Any) -> bool:
    try:
        table.load()
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise


def _create_table(
    dynamodb: Any,
    *,
    name: str,
    key_schema: list[dict[str, str]],
    attributes: list[dict[str, str]],
    gsis: list[dict[str, Any]] | None = None,
    ttl_attribute: str | None = None,
) -> None:
    table = dynamodb.Table(name)
    if _table_exists(table):
        print(f"DynamoDB table exists: {name}")
        return

    params: dict[str, Any] = {
        "TableName": name,
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": key_schema,
        "AttributeDefinitions": attributes,
    }
    if gsis:
        params["GlobalSecondaryIndexes"] = gsis

    table = dynamodb.create_table(**params)
    table.wait_until_exists()

    if ttl_attribute:
        dynamodb.meta.client.update_time_to_live(
            TableName=name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": ttl_attribute},
        )

    print(f"DynamoDB table created: {name}")


def _pk_sk_schema() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    return (
        [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
    )


def _create_dynamodb_tables() -> None:
    dynamodb = _resource("dynamodb")
    prefix = os.environ.get("DYNAMODB_TABLE_PREFIX", "quantembrace-test")

    pk_sk_key, pk_sk_attrs = _pk_sk_schema()

    _create_table(
        dynamodb,
        name=f"{prefix}-orders",
        key_schema=pk_sk_key,
        attributes=[
            *pk_sk_attrs,
            {"AttributeName": "signal_id",    "AttributeType": "S"},
            {"AttributeName": "order_status", "AttributeType": "S"},
            {"AttributeName": "created_at",   "AttributeType": "S"},
            {"AttributeName": "account_id",   "AttributeType": "S"},
            {"AttributeName": "trade_date",   "AttributeType": "S"},
            {"AttributeName": "symbol",       "AttributeType": "S"},
        ],
        gsis=[
            {
                "IndexName": "signal-index",
                "KeySchema": [{"AttributeName": "signal_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "status-index",
                "KeySchema": [
                    {"AttributeName": "order_status", "KeyType": "HASH"},
                    {"AttributeName": "created_at",   "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "account-index",
                "KeySchema": [
                    {"AttributeName": "account_id", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "DateIndex",
                "KeySchema": [{"AttributeName": "trade_date", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "symbol-status-index",
                "KeySchema": [
                    {"AttributeName": "symbol",       "KeyType": "HASH"},
                    {"AttributeName": "order_status", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )

    for suffix in ("positions", "risk-state", "sessions"):
        _create_table(
            dynamodb,
            name=f"{prefix}-{suffix}",
            key_schema=pk_sk_key,
            attributes=pk_sk_attrs,
        )

    _create_table(
        dynamodb,
        name=f"{prefix}-latest-prices",
        key_schema=pk_sk_key,
        attributes=pk_sk_attrs,
        ttl_attribute="expires_at",
    )

    _create_table(
        dynamodb,
        name=f"{prefix}-fills",
        key_schema=pk_sk_key,
        attributes=pk_sk_attrs,
        ttl_attribute="TTL",
    )

    _create_table(
        dynamodb,
        name=f"{prefix}-features",
        key_schema=pk_sk_key,
        attributes=pk_sk_attrs,
        ttl_attribute="ttl",
    )

    _create_table(
        dynamodb,
        name=f"{prefix}-candle-cache",
        key_schema=[{"AttributeName": "PK", "KeyType": "HASH"}],
        attributes=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "cache_bucket", "AttributeType": "S"},
            {"AttributeName": "candle_open_time", "AttributeType": "S"},
        ],
        gsis=[
            {
                "IndexName": "candle-open-time-index",
                "KeySchema": [
                    {"AttributeName": "cache_bucket", "KeyType": "HASH"},
                    {"AttributeName": "candle_open_time", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        ttl_attribute="expires_at",
    )

    _create_table(
        dynamodb,
        name=f"{prefix}-strategy-config",
        key_schema=pk_sk_key,
        attributes=pk_sk_attrs,
    )

    # ai_engine: regime audit log — non-fatal if missing, but creates noisy log errors
    _create_table(
        dynamodb,
        name=f"{prefix}-regime-log",
        key_schema=pk_sk_key,
        attributes=pk_sk_attrs,
        ttl_attribute="ttl",
    )

    # ai_engine: strategy recommendations from StrategySelector agent
    _create_table(
        dynamodb,
        name=f"{prefix}-strategy-recommendations",
        key_schema=pk_sk_key,
        attributes=pk_sk_attrs,
        ttl_attribute="ttl",
    )

    _create_table(
        dynamodb,
        name=f"{prefix}-strategy-state",
        key_schema=[
            {"AttributeName": "strategy_name", "KeyType": "HASH"},
            {"AttributeName": "symbol", "KeyType": "RANGE"},
        ],
        attributes=[
            {"AttributeName": "strategy_name", "AttributeType": "S"},
            {"AttributeName": "symbol", "AttributeType": "S"},
        ],
        ttl_attribute="expires_at",
    )


def _create_bucket(s3: Any, bucket_name: str, region: str) -> None:
    bucket = s3.Bucket(bucket_name)
    try:
        bucket.load()
        print(f"S3 bucket exists: {bucket_name}")
        return
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in {"404", "NoSuchBucket"}:
            raise

    if region == "us-east-1":
        bucket.create()
    else:
        bucket.create(CreateBucketConfiguration={"LocationConstraint": region})
    print(f"S3 bucket created: {bucket_name}")


def _create_s3_buckets() -> None:
    s3 = _resource("s3")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
    prefix = os.environ.get("DYNAMODB_TABLE_PREFIX", "quantembrace-development")
    bucket_names = {
        os.environ.get("S3_BUCKET_DATA",  f"{prefix}-data"),
        os.environ.get("S3_BUCKET_LOGS",  f"{prefix}-logs"),
        os.environ.get("AWS_S3_BUCKET_DATA", f"{prefix}-data"),
        os.environ.get("AWS_S3_BUCKET_LOGS", f"{prefix}-logs"),
        "quantembrace-market-data",   # s3_bucket settings default (S3Writer, tick archive)
        "quantembrace-ml-models",     # s3_model_bucket settings default (ai_engine models)
    }
    for bucket_name in sorted(bucket_names):
        _create_bucket(s3, bucket_name, region)


def _seed_paper_nav() -> None:
    """Seed initial NAV and paper trading state so risk checks pass from the start."""
    import time
    from decimal import Decimal
    dynamodb = _resource("dynamodb")
    prefix   = os.environ.get("DYNAMODB_TABLE_PREFIX", "quantembrace-development")
    table    = dynamodb.Table(f"{prefix}-risk-state")
    if not _table_exists(table):
        return

    # Only seed if no row exists (don't overwrite an execution-engine NAV update)
    try:
        existing = table.get_item(Key={"PK": "NAV#CURRENT", "SK": "STATE"}).get("Item")
        if existing and existing.get("source") != "paper_seed":
            print("NAV already set by execution engine — skipping seed")
            return
    except Exception:
        pass

    nav = Decimal(os.environ.get("PAPER_SEED_NAV", "5000000"))
    table.put_item(Item={
        "PK":              "NAV#CURRENT",
        "SK":              "STATE",
        "portfolio_value": nav,
        "updated_at":      str(int(time.time())),
        "source":          "paper_seed",
    })
    print(f"NAV seeded: INR {float(nav):,.0f} (paper account)")


def _seed_strategy_configs() -> None:
    """Seed default strategy configs so strategies aren't capped at 10 signals/day.

    The StrategyConfigLoader falls back to _DEFAULT_CONFIG(max_signals_per_day=10) when
    no DynamoDB row exists. For paper testing we want 0 (unlimited) so a single busy
    session can't silently top out at 10 signals per strategy (60 total).
    """
    from datetime import datetime, timezone
    dynamodb = _resource("dynamodb")
    prefix   = os.environ.get("DYNAMODB_TABLE_PREFIX", "quantembrace-development")
    env      = os.environ.get("QE_ENVIRONMENT", "development")
    table    = dynamodb.Table(f"{prefix}-strategy-config")
    if not _table_exists(table):
        print("strategy-config table not found — skipping strategy seed")
        return

    strategies = [
        "nse_momentum_v1",
        "nse_orb_15m",
        "nse_scalp_1m",
        "nse_vwap_reversion",
        "nse_intraday_trend_15m",
        "nse_preclose_momentum",
    ]
    seeded = 0
    for name in strategies:
        pk = f"STRATEGY_CONFIG#{name}"
        sk = f"ENV#{env}"
        try:
            table.put_item(
                Item={
                    "PK":                                    pk,
                    "SK":                                    sk,
                    "enabled":                               True,
                    "paper_trade":                           True,
                    "max_signals_per_day":                   0,
                    "circuit_breaker_threshold_consecutive": 5,
                    "circuit_breaker_threshold_rate":        10,
                    "circuit_breaker_state":                 "CLOSED",
                    "circuit_breaker_reset":                 False,
                    "updated_at":                            datetime.now(timezone.utc).isoformat(),
                    "updated_by":                            "setup_local_tables",
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            seeded += 1
        except Exception as exc:
            if "ConditionalCheckFailed" in type(exc).__name__:
                pass  # row already exists — leave it
            else:
                print(f"  WARN: strategy config seed failed for {name}: {exc}")
    print(f"Strategy configs seeded: {seeded}/{len(strategies)} (0 = unlimited signals/day)")


def main() -> None:
    _create_dynamodb_tables()
    _create_s3_buckets()
    _seed_paper_nav()
    _seed_strategy_configs()


if __name__ == "__main__":
    main()
