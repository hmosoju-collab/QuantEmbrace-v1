"""
Shared boto3 client factory for QuantEmbrace services.

All AWS clients are created through this module so that:
  - LocalStack (local dev) and real AWS (staging/prod) use identical code paths.
  - Endpoint, region, and credentials are configured in one place.
  - Clients are cheap singletons — one per service process, not one per call.

LocalStack auto-detection:
  Set LOCALSTACK_ENDPOINT_URL=http://localhost:4566 in your .env file.
  When present, all clients point to LocalStack instead of real AWS.
  When absent, clients use the standard boto3 credential chain (IAM task role on ECS).

Usage:
    from shared.aws.clients import get_sqs_client, get_dynamodb_resource, get_s3_client

    sqs = get_sqs_client()
    sqs.send_message(QueueUrl="...", MessageBody="...")

    dynamodb = get_dynamodb_resource()
    table = dynamodb.Table("my-table")
    table.put_item(Item={...})

    s3 = get_s3_client()
    s3.put_object(Bucket="...", Key="...", Body=b"...")
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

# ── LocalStack detection ──────────────────────────────────────────────────────
# Set LOCALSTACK_ENDPOINT_URL=http://localhost:4566 in .env for local dev.
# Leave unset (or empty) for real AWS on ECS.
_LOCALSTACK_URL: str | None = os.environ.get("LOCALSTACK_ENDPOINT_URL") or None

# ── Region ───────────────────────────────────────────────────────────────────
_REGION: str = os.environ.get("AWS_REGION", "ap-south-1")

# ── Retry config ─────────────────────────────────────────────────────────────
# Standard retry config: up to 3 retries, exponential backoff.
# Trading services use short timeouts — we'd rather fail fast than hang.
_BOTO_CONFIG = Config(
    region_name=_REGION,
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)

# ── Module-level singletons ───────────────────────────────────────────────────
# One client per type per process. boto3 clients are thread-safe.
_sqs_client: Any = None
_s3_client: Any = None
_dynamodb_resource: Any = None
_dynamodb_client: Any = None  # Low-level client (for conditional writes)
_secretsmanager_client: Any = None


def _endpoint_kwargs() -> dict[str, Any]:
    """Return endpoint_url kwarg dict when LocalStack is configured."""
    if _LOCALSTACK_URL:
        return {"endpoint_url": _LOCALSTACK_URL}
    return {}


def get_sqs_client() -> Any:
    """
    Return a shared boto3 SQS client.

    Automatically points to LocalStack when LOCALSTACK_ENDPOINT_URL is set.

    Returns:
        boto3 SQS client (botocore.client.SQS).
    """
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client(
            "sqs",
            config=_BOTO_CONFIG,
            **_endpoint_kwargs(),
        )
        _log_client_init("SQS")
    return _sqs_client


def get_s3_client() -> Any:
    """
    Return a shared boto3 S3 client.

    Returns:
        boto3 S3 client (botocore.client.S3).
    """
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            config=_BOTO_CONFIG,
            **_endpoint_kwargs(),
        )
        _log_client_init("S3")
    return _s3_client


def get_dynamodb_resource() -> Any:
    """
    Return a shared boto3 DynamoDB resource (high-level Table API).

    Use this for standard put_item / get_item / query / batch_writer operations.

    Returns:
        boto3 DynamoDB ServiceResource.
    """
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource(
            "dynamodb",
            config=_BOTO_CONFIG,
            **_endpoint_kwargs(),
        )
        _log_client_init("DynamoDB resource")
    return _dynamodb_resource


def get_dynamodb_client() -> Any:
    """
    Return a shared boto3 DynamoDB low-level client.

    Use this for conditional writes with ConditionExpression or
    transactions (transact_write_items).

    Returns:
        boto3 DynamoDB client (botocore.client.DynamoDB).
    """
    global _dynamodb_client
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client(
            "dynamodb",
            config=_BOTO_CONFIG,
            **_endpoint_kwargs(),
        )
        _log_client_init("DynamoDB client")
    return _dynamodb_client


def get_secretsmanager_client() -> Any:
    """
    Return a shared boto3 Secrets Manager client.

    Returns:
        boto3 SecretsManager client.
    """
    global _secretsmanager_client
    if _secretsmanager_client is None:
        _secretsmanager_client = boto3.client(
            "secretsmanager",
            config=_BOTO_CONFIG,
            **_endpoint_kwargs(),
        )
        _log_client_init("SecretsManager")
    return _secretsmanager_client


def reset_clients() -> None:
    """
    Reset all singleton clients.

    Used in tests to force fresh client creation with different configs,
    e.g., when patching LOCALSTACK_ENDPOINT_URL between tests.
    """
    global _sqs_client, _s3_client, _dynamodb_resource, _dynamodb_client, _secretsmanager_client
    _sqs_client = None
    _s3_client = None
    _dynamodb_resource = None
    _dynamodb_client = None
    _secretsmanager_client = None


def is_localstack() -> bool:
    """Return True if currently targeting LocalStack (local dev mode)."""
    return _LOCALSTACK_URL is not None


def _log_client_init(service_name: str) -> None:
    """Log client initialisation with endpoint info."""
    if _LOCALSTACK_URL:
        logger.info(
            "boto3 %s client → LocalStack at %s (dev mode)",
            service_name,
            _LOCALSTACK_URL,
        )
    else:
        logger.info(
            "boto3 %s client → AWS %s (region=%s)",
            service_name,
            service_name,
            _REGION,
        )
