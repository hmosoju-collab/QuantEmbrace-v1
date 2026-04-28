#!/usr/bin/env python3
"""
ECS health checker used in CI/CD post-deploy verification.

Polls ECS service descriptions and verifies that each named service has
at least `--min-running` tasks in RUNNING state. Exits non-zero if any
service is below threshold, causing the GitHub Actions step to fail.

Usage:
    python scripts/deploy/check_ecs_health.py \
        --cluster quantembrace-prod \
        --services data_ingestion strategy_engine risk_engine \
        --min-running 1 \
        --timeout 300
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    import boto3
except ImportError:
    print("boto3 is required: pip install boto3")
    sys.exit(1)


def _check(
    cluster: str,
    service_names: list[str],
    min_running: int,
    timeout: int,
    poll_interval: int = 15,
) -> bool:
    """
    Poll ECS until all services meet the minimum running task count.

    Args:
        cluster:       ECS cluster name or ARN.
        service_names: List of ECS service names (without cluster prefix).
        min_running:   Minimum RUNNING tasks required per service.
        timeout:       Total seconds to wait before giving up.
        poll_interval: Seconds between polls.

    Returns:
        True if all services healthy within timeout, False otherwise.
    """
    ecs = boto3.client("ecs")
    deadline = time.time() + timeout
    # ECS DescribeServices accepts up to 10 services per call
    full_names = [f"{cluster.split('/')[-1]}-{s}" if "/" not in s else s for s in service_names]

    print(f"Checking {len(service_names)} service(s) on cluster '{cluster}'")
    print(f"  Services : {', '.join(full_names)}")
    print(f"  Min tasks: {min_running}")
    print(f"  Timeout  : {timeout}s\n")

    while time.time() < deadline:
        unhealthy: list[str] = []

        # Batch into chunks of 10 (ECS API limit)
        for i in range(0, len(full_names), 10):
            batch = full_names[i : i + 10]
            resp = ecs.describe_services(cluster=cluster, services=batch)

            failures = resp.get("failures", [])
            if failures:
                for f in failures:
                    print(f"  ERROR describing service: {f.get('arn', '?')} — {f.get('reason', '?')}")
                    unhealthy.append(f.get("arn", "unknown"))

            for svc in resp.get("services", []):
                name = svc.get("serviceName", "?")
                running = svc.get("runningCount", 0)
                desired = svc.get("desiredCount", 0)
                status = svc.get("status", "?")

                healthy = running >= min_running and status == "ACTIVE"
                marker = "✓" if healthy else "✗"
                print(f"  {marker} {name:<35}  running={running}  desired={desired}  status={status}")

                if not healthy:
                    unhealthy.append(name)

        if not unhealthy:
            print(f"\nAll {len(service_names)} service(s) healthy.")
            return True

        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        print(f"\n  {len(unhealthy)} service(s) not yet healthy. Retrying in {poll_interval}s "
              f"({remaining}s remaining)...\n")
        time.sleep(poll_interval)

    print(f"\nTIMEOUT: The following services did not reach {min_running} running task(s):")
    for s in unhealthy:
        print(f"  • {s}")
    return False


def _main() -> None:
    p = argparse.ArgumentParser(description="ECS post-deploy health check")
    p.add_argument("--cluster", required=True, help="ECS cluster name or ARN")
    p.add_argument("--services", nargs="+", required=True, help="ECS service names")
    p.add_argument("--min-running", type=int, default=1, help="Minimum running tasks per service")
    p.add_argument("--timeout", type=int, default=300, help="Max seconds to wait")
    p.add_argument("--poll-interval", type=int, default=15, help="Seconds between polls")
    args = p.parse_args()

    ok = _check(
        cluster=args.cluster,
        service_names=args.services,
        min_running=args.min_running,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main()
