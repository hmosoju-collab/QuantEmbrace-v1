#!/usr/bin/env python3
"""
ASG health checker used in CI/CD post-deploy verification.

Polls Auto Scaling Group descriptions and verifies that each named ASG has
at least --min-healthy InService+Healthy instances. Exits non-zero on failure,
causing the GitHub Actions step to fail.

Usage:
    python scripts/deploy/check_asg_health.py \
        --asg quantembrace-prod-risk-engine-asg \
        --min-healthy 1 \
        --timeout 300

    # Pass immediately when the ASG is intentionally scaled to zero:
    python scripts/deploy/check_asg_health.py \
        --asg quantembrace-prod-strategy-engine-asg \
        --allow-zero-desired
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
    asg_name: str,
    min_healthy: int,
    timeout: int,
    allow_zero_desired: bool,
    poll_interval: int = 15,
) -> bool:
    client = boto3.client("autoscaling")
    deadline = time.time() + timeout

    print(f"Checking ASG: {asg_name}")
    print(f"  Min healthy      : {min_healthy}")
    print(f"  Allow zero desired: {allow_zero_desired}")
    print(f"  Timeout          : {timeout}s\n")

    while time.time() < deadline:
        resp = client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        groups = resp.get("AutoScalingGroups", [])

        if not groups:
            print(f"  ERROR: ASG {asg_name!r} not found in this region/account.")
            return False

        asg = groups[0]
        desired = asg.get("DesiredCapacity", 0)
        in_service = sum(
            1
            for i in asg.get("Instances", [])
            if i.get("LifecycleState") == "InService"
            and i.get("HealthStatus") == "Healthy"
        )

        print(f"  desired={desired}  in_service_healthy={in_service}")

        if allow_zero_desired and desired == 0:
            print(f"  desired=0 and --allow-zero-desired — check passes.")
            return True

        if in_service >= min_healthy:
            print(f"\n  {asg_name} is healthy ({in_service} InService).")
            return True

        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        print(
            f"  Not yet healthy. Retrying in {poll_interval}s "
            f"({remaining}s remaining)...\n"
        )
        time.sleep(poll_interval)

    print(
        f"\nTIMEOUT: {asg_name} did not reach {min_healthy} healthy "
        f"instance(s) within {timeout}s."
    )
    return False


def _main() -> None:
    p = argparse.ArgumentParser(description="ASG post-deploy health check")
    p.add_argument("--asg", required=True, help="Auto Scaling Group name")
    p.add_argument(
        "--min-healthy",
        type=int,
        default=1,
        help="Minimum InService+Healthy instances required (default: 1)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Max seconds to wait before failing (default: 300)",
    )
    p.add_argument(
        "--poll-interval",
        type=int,
        default=15,
        help="Seconds between polls (default: 15)",
    )
    p.add_argument(
        "--allow-zero-desired",
        action="store_true",
        help="Pass immediately if ASG desired=0 (intentionally scaled-down ASG)",
    )
    args = p.parse_args()

    ok = _check(
        asg_name=args.asg,
        min_healthy=args.min_healthy,
        timeout=args.timeout,
        allow_zero_desired=args.allow_zero_desired,
        poll_interval=args.poll_interval,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main()
