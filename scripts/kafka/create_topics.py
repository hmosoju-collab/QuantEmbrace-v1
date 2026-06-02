#!/usr/bin/env python3
"""
scripts/kafka/create_topics.py — QuantEmbrace Kafka topic setup (Phase 2 + Phase 6).

Creates all required Kafka topics on MSK Serverless using IAM authentication.
Idempotent: topics that already exist are left untouched (no error).

Topics (Phase 2 from phase2_final_approved.md §2, Phase 6 from ADR-014 §5):
  ticks.nse          4 parts · 24h  · instrument_id key
  ticks.us           2 parts · 24h  · instrument_id key
  signals.pending    2 parts ·  1h  · instrument_id key
  signals.enriched   2 parts ·  1h  · symbol key         [Phase 6 — ai_engine → risk_engine]
  signals.approved   2 parts · 30m  · instrument_id key
  orders.events      4 parts ·  7d  · instrument_id key
  risk.kill-switch   1 part  · 30d  · GLOBAL key
  ops.audit          2 parts · 90d  · trace_id key
  <each topic>.retry same partitions/retention as source
  <each topic>.dlq   same partitions/retention as source

Usage:
  # From an EC2 instance with the ops-admin IAM role attached:
  python scripts/kafka/create_topics.py --bootstrap-servers <MSK_BOOTSTRAP_BROKERS>

  # Dry run (print config, do not create):
  python scripts/kafka/create_topics.py --bootstrap-servers <BROKERS> --dry-run

  # Override retention for dev/staging (shorter retention to save cost):
  python scripts/kafka/create_topics.py --bootstrap-servers <BROKERS> --short-retention

Environment variables:
  KAFKA_BOOTSTRAP_SERVERS   Bootstrap broker string (alternative to --bootstrap-servers)
  AWS_DEFAULT_REGION        AWS region (defaults to ap-south-1)

Requirements:
  pip install confluent-kafka~=2.3 boto3~=1.34 aws-msk-iam-sasl-signer-python~=1.0

Authentication:
  IAM-based. The script signs Kafka auth tokens using the EC2 instance role or
  the local AWS credentials (for local development via assumed role).
  The EC2 role must have the QuantEmbrace-{env}-KafkaOpsAdmin IAM policy attached.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# ── Import guard ──────────────────────────────────────────────────────────────
try:
    from confluent_kafka.admin import AdminClient, NewTopic, ConfigResource, ConfigSource
    from confluent_kafka import KafkaException
except ImportError:
    print("ERROR: confluent-kafka not installed.")
    print("       pip install confluent-kafka~=2.3")
    sys.exit(1)

try:
    from aws_msk_iam_sasl_signer import MSKAuthTokenProvider
except ImportError:
    print("ERROR: aws-msk-iam-sasl-signer-python not installed.")
    print("       pip install aws-msk-iam-sasl-signer-python~=1.0")
    sys.exit(1)

# ── Topic definitions ─────────────────────────────────────────────────────────

@dataclass
class TopicDef:
    name:         str
    partitions:   int
    retention_ms: int            # milliseconds
    key_field:    str            # documentation only — Kafka doesn't enforce key type
    purpose:      str
    # Computed short-retention override (staging/dev)
    short_retention_ms: Optional[int] = field(default=None, repr=False)


# Canonical topic definitions from phase2_final_approved.md §2
_PRIMARY_TOPICS: list[TopicDef] = [
    TopicDef(
        name="ticks.nse",
        partitions=4,
        retention_ms=86_400_000,        # 24h
        short_retention_ms=3_600_000,   # 1h for dev
        key_field="instrument_id",
        purpose="NSE real-time tick stream from data_ingestion",
    ),
    TopicDef(
        name="ticks.us",
        partitions=2,
        retention_ms=86_400_000,        # 24h
        short_retention_ms=3_600_000,   # 1h for dev
        key_field="instrument_id",
        purpose="US equity tick stream from data_ingestion",
    ),
    TopicDef(
        name="signals.pending",
        partitions=2,
        retention_ms=3_600_000,         # 1h
        short_retention_ms=600_000,     # 10m for dev
        key_field="instrument_id",
        purpose="Unvalidated signals awaiting AI enrichment + risk check",
    ),
    TopicDef(
        name="signals.enriched",
        partitions=2,
        retention_ms=3_600_000,         # 1h — matches signals.pending; signals expire fast
        short_retention_ms=600_000,     # 10m for dev
        key_field="symbol",
        purpose="[Phase 6] AI-enriched signals (v4.0 schema): regime + quality score. "
                "Published by ai_engine (aiengine-v1), consumed by risk_engine (risk-v1). "
                "EnrichmentWatchdog falls back to signals.pending if this topic goes silent.",
    ),
    TopicDef(
        name="signals.approved",
        partitions=2,
        retention_ms=1_800_000,         # 30m
        short_retention_ms=300_000,     # 5m for dev
        key_field="instrument_id",
        purpose="Risk-validated signals ready for execution",
    ),
    TopicDef(
        name="orders.events",
        partitions=4,
        retention_ms=604_800_000,       # 7d
        short_retention_ms=86_400_000,  # 24h for dev
        key_field="instrument_id",
        purpose="All order lifecycle events: SUBMITTED, FILLED, CANCELLED, REJECTED",
    ),
    TopicDef(
        name="risk.kill-switch",
        partitions=1,
        retention_ms=2_592_000_000,     # 30d
        short_retention_ms=86_400_000,  # 24h for dev
        key_field="GLOBAL",
        purpose="Kill switch state changes (ACTIVATE/DEACTIVATE). Single partition for ordering.",
    ),
    TopicDef(
        name="ops.audit",
        partitions=2,
        retention_ms=7_776_000_000,     # 90d
        short_retention_ms=604_800_000, # 7d for dev
        key_field="trace_id",
        purpose="Full audit trail: every signal decision, risk decision, order event",
    ),
]


def _failure_topics(topics: list[TopicDef]) -> list[TopicDef]:
    """Return retry and DLQ topic definitions for every primary event stream."""
    derived: list[TopicDef] = []
    for topic in topics:
        for suffix, label in (("retry", "Retry"), ("dlq", "Dead-letter")):
            derived.append(
                TopicDef(
                    name=f"{topic.name}.{suffix}",
                    partitions=topic.partitions,
                    retention_ms=topic.retention_ms,
                    short_retention_ms=topic.short_retention_ms,
                    key_field=topic.key_field,
                    purpose=f"{label} stream for {topic.name}: {topic.purpose}",
                )
            )
    return derived


TOPICS: list[TopicDef] = _PRIMARY_TOPICS + _failure_topics(_PRIMARY_TOPICS)


# ── IAM auth callback ─────────────────────────────────────────────────────────

def _make_oauth_callback(region: str):
    """
    Returns a token-refresh callback compatible with confluent-kafka's
    oauth_cb configuration for MSK IAM auth.

    The callback is called by the Kafka client when a new token is needed.
    MSK IAM tokens expire every 15 minutes; confluent-kafka handles refresh
    automatically when this callback is configured.
    """
    def oauth_callback(config: dict) -> tuple[str, float]:
        token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(region)
        return token, expiry_ms / 1000.0   # confluent-kafka expects seconds

    return oauth_callback


# ── Admin client factory ──────────────────────────────────────────────────────

def _build_admin_client(bootstrap_servers: str, region: str, use_iam: bool = True) -> AdminClient:
    """
    Build a confluent-kafka AdminClient.

    When use_iam=True (production/MSK Serverless): SASL_SSL + OAUTHBEARER IAM tokens.
    When use_iam=False (local Redpanda / dev): plain PLAINTEXT — no auth, no TLS.
    """
    if use_iam:
        conf = {
            "bootstrap.servers":  bootstrap_servers,
            "security.protocol":  "SASL_SSL",
            "sasl.mechanism":     "OAUTHBEARER",
            "oauth_cb":           _make_oauth_callback(region),
            "socket.connection.setup.timeout.ms": 30_000,
            "metadata.request.timeout.ms":        30_000,
            "log.connection.close": False,
        }
    else:
        conf = {
            "bootstrap.servers":  bootstrap_servers,
            "security.protocol":  "PLAINTEXT",
            "socket.connection.setup.timeout.ms": 10_000,
            "metadata.request.timeout.ms":        10_000,
            "log.connection.close": False,
        }
    return AdminClient(conf)


# ── Topic creation ────────────────────────────────────────────────────────────

def _retention_str(ms: int) -> str:
    """Human-readable retention description for logging."""
    if ms >= 86_400_000:
        return f"{ms // 86_400_000}d"
    if ms >= 3_600_000:
        return f"{ms // 3_600_000}h"
    return f"{ms // 60_000}m"


def create_topics(
    admin: AdminClient,
    topics: list[TopicDef],
    short_retention: bool = False,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """
    Create topics on the MSK cluster. Idempotent — existing topics are skipped.

    Returns:
        (created, skipped, failed) counts
    """
    new_topics = []
    for t in topics:
        ret = t.short_retention_ms if (short_retention and t.short_retention_ms) else t.retention_ms
        config = {
            "retention.ms":       str(ret),
            # min.insync.replicas: MSK Serverless manages replication internally.
            # Setting this is not required, but 1 is the floor and keeps behavior explicit.
            "min.insync.replicas": "1",
            # cleanup.policy: delete (not compact). Ticks / signals are time-windowed, not keyed state.
            "cleanup.policy": "delete",
        }
        new_topics.append(
            NewTopic(
                topic=t.name,
                num_partitions=t.partitions,
                replication_factor=-1,   # MSK Serverless: use -1 (managed by AWS)
                config=config,
            )
        )
        ret_str = _retention_str(ret) + (" (short)" if short_retention and t.short_retention_ms else "")
        print(
            f"  {'[DRY RUN] ' if dry_run else ''}topic={t.name:<25}  "
            f"partitions={t.partitions}  retention={ret_str:<12}  key={t.key_field}"
        )

    if dry_run:
        print("\nDry run complete — no topics created.")
        return 0, 0, 0

    result = admin.create_topics(new_topics, request_timeout=30)
    created = skipped = failed = 0

    for topic_name, future in result.items():
        try:
            future.result()
            print(f"  ✓  Created  {topic_name}")
            created += 1
        except KafkaException as e:
            if "TOPIC_ALREADY_EXISTS" in str(e) or e.args[0].code().name == "TOPIC_ALREADY_EXISTS":
                print(f"  ·  Exists   {topic_name}  (no change)")
                skipped += 1
            else:
                print(f"  ✗  FAILED   {topic_name}: {e}")
                failed += 1

    return created, skipped, failed


# ── Describe topics (verification) ───────────────────────────────────────────

def verify_topics(admin: AdminClient, expected: list[TopicDef]) -> bool:
    """
    Describe each expected topic and verify partitions match.
    Returns True if all topics exist with the correct partition count.
    """
    topic_names = [t.name for t in expected]
    metadata = admin.list_topics(timeout=15)

    print("\nVerification:")
    all_ok = True
    for t in expected:
        if t.name not in metadata.topics:
            print(f"  ✗  MISSING   {t.name}")
            all_ok = False
        else:
            actual_parts = len(metadata.topics[t.name].partitions)
            ok = actual_parts == t.partitions
            if ok:
                print(f"  ✓  {t.name:<25}  partitions={actual_parts}/{t.partitions}")
            else:
                print(f"  ✗  {t.name:<25}  partitions={actual_parts} (expected {t.partitions})")
                all_ok = False

    return all_ok


# ── Main ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="QuantEmbrace Phase 2 — Kafka topic setup for MSK Serverless",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--bootstrap-servers",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", ""),
        help="MSK bootstrap broker string (port 9098). "
             "Also reads from KAFKA_BOOTSTRAP_SERVERS env var.",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_DEFAULT_REGION", "ap-south-1"),
        help="AWS region (default: ap-south-1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print topic configs but do not create anything",
    )
    parser.add_argument(
        "--short-retention",
        action="store_true",
        help="Use shortened retention periods (for dev/staging cost reduction)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip creation — only verify topics exist with correct partitions",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if not args.bootstrap_servers:
        print(
            "ERROR: --bootstrap-servers is required (or set KAFKA_BOOTSTRAP_SERVERS).\n"
            "       Get the value from Terraform output: bootstrap_brokers_sasl_iam\n"
            "       Or via: aws kafka get-bootstrap-brokers --cluster-arn <ARN>"
        )
        return 1

    mode = "DRY RUN" if args.dry_run else ("VERIFY ONLY" if args.verify_only else "CREATE")
    print(f"\nQuantEmbrace Kafka Topic Setup — {mode}")
    print(f"  Bootstrap servers : {args.bootstrap_servers}")
    print(f"  Region            : {args.region}")
    print(f"  Short retention   : {args.short_retention}")
    print()

    use_iam = os.environ.get("KAFKA_USE_IAM", "true").lower() not in ("false", "0", "no")
    auth_mode = "IAM (SASL_SSL)" if use_iam else "PLAINTEXT (local dev)"
    print(f"Building AdminClient — auth mode: {auth_mode}")
    admin = _build_admin_client(args.bootstrap_servers, args.region, use_iam=use_iam)

    # Quick connectivity check
    print("Connecting to MSK cluster...")
    try:
        md = admin.list_topics(timeout=20)
        print(f"Connected. Cluster has {len(md.topics)} existing topics.")
    except KafkaException as e:
        print(f"ERROR: Cannot connect to MSK cluster: {e}")
        print(
            "Ensure:\n"
            "  1. EC2 instance is in the same VPC as MSK Serverless\n"
            "  2. Security group allows port 9098 from this instance\n"
            "  3. IAM role has kafka-cluster:Connect permission\n"
        )
        return 1

    if args.verify_only:
        ok = verify_topics(admin, TOPICS)
        return 0 if ok else 1

    print(f"\nPlanning {len(TOPICS)} topics:")
    print("-" * 70)

    created, skipped, failed = create_topics(
        admin, TOPICS, short_retention=args.short_retention, dry_run=args.dry_run
    )

    if not args.dry_run:
        print()
        # Give MSK a moment to propagate topic metadata
        if created > 0:
            print("Waiting 5s for topic metadata propagation...")
            time.sleep(5)
        ok = verify_topics(admin, TOPICS)
        print()
        print("=" * 70)
        print(f"  Created : {created}")
        print(f"  Existed : {skipped}")
        print(f"  Failed  : {failed}")
        print(f"  Status  : {'✓ OK' if ok and failed == 0 else '✗ ISSUES DETECTED'}")
        print("=" * 70)

        if failed > 0 or not ok:
            print(
                "\nNext steps:\n"
                "  - Check IAM policy has kafka-cluster:CreateTopic permission\n"
                "  - Verify MSK cluster ARN in KafkaOpsAdmin IAM policy\n"
                "  - Re-run this script after resolving failures\n"
            )
            return 1

    print("\nTopic setup complete. Next step:")
    print("  Deploy services with KAFKA_BOOTSTRAP_SERVERS set to:")
    print(f"    {args.bootstrap_servers}")
    print("\nConsumer groups to expect:")
    print("  strategy-v1   — strategy_engine consuming ticks.nse, ticks.us")
    print("  aiengine-v1   — ai_engine consuming signals.pending → producing signals.enriched  [Phase 6]")
    print("  risk-v1       — risk_engine consuming signals.enriched (primary) or signals.pending (fallback)")
    print("  execution-v1  — execution_engine consuming signals.approved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
