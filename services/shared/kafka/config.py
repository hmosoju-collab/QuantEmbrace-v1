"""Shared Kafka authentication config builder.

Returns security config for MSK Serverless (SASL_SSL + IAM OAUTHBEARER) by default.
Set KAFKA_USE_IAM=false to switch to PLAINTEXT for local dev (Redpanda / local Kafka).
"""

from __future__ import annotations

import os
from typing import Any

try:
    from aws_msk_iam_sasl_signer import MSKAuthTokenProvider
    _IAM_SIGNER_AVAILABLE = True
except ImportError:
    _IAM_SIGNER_AVAILABLE = False


def get_kafka_auth_config(aws_region: str) -> dict[str, Any]:
    """Return Kafka security config keyed for confluent-kafka.

    When KAFKA_USE_IAM=false uses PLAINTEXT — for local Redpanda / plain Kafka.
    All other values (including the default) use SASL_SSL + IAM OAUTHBEARER for MSK.
    """
    use_iam = os.environ.get("KAFKA_USE_IAM", "true").lower() not in ("false", "0", "no")

    if not use_iam:
        return {"security.protocol": "PLAINTEXT"}

    if not _IAM_SIGNER_AVAILABLE:
        raise RuntimeError(
            "aws-msk-iam-sasl-signer-python is required for IAM auth. "
            "Install: pip install aws-msk-iam-sasl-signer-python~=1.0  "
            "Or set KAFKA_USE_IAM=false for local development."
        )

    region = aws_region

    def oauth_callback(config: dict) -> tuple[str, float]:
        token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(region)
        return token, expiry_ms / 1000.0

    return {
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": "OAUTHBEARER",
        "oauth_cb": oauth_callback,
    }
