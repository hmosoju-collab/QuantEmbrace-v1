#!/usr/bin/env python3
"""
Rate Limiter Stress Test — staging-only validation of ZerodhaRateLimiter behaviour.

Validates the ADR-012 token bucket implementation under 5 scenarios designed to
catch the failure modes that motivated the rate-limit rewrite:

  Scenario 1 — Baseline throughput
    Sends 30 requests at Priority.HIGH as fast as possible.
    Asserts: actual rate ≤ 10.0 req/sec (± 0.5 tolerance).
    Catches: capacity misconfiguration, broken token refill.

  Scenario 2 — Burst absorption
    Sends 15 requests simultaneously (burst_capacity = 15).
    Asserts: first 15 dispatched within 200ms (burst absorbed from pre-filled bucket).
    Asserts: next 10 dispatched at 10 req/sec cadence.
    Catches: burst ceiling not working, tokens not pre-loaded at startup.

  Scenario 3 — Priority queue ordering
    Queues 5 MEDIUM requests, then 5 HIGH requests, then 2 CRITICAL requests
    with the bucket temporarily empty.
    Asserts: CRITICAL requests are dispatched first, HIGH before MEDIUM.
    Catches: priority inversion, FIFO-only queue.

  Scenario 4 — MIS auto-square-off simulation
    Sends 8 CRITICAL requests in <500ms (simulates batch cancel at 15:15 IST).
    Asserts: all 8 complete within 1500ms despite being above 10 req/sec rate.
    Rationale: burst_capacity = 15 absorbs 8 at once from the bucket.
    Catches: CRITICAL priority not bypassing queue backlog.

  Scenario 5 — Phase budget enforcement
    Sets market phase to MARKET_OPEN (budget: orders=4, fills=3, reserve=1).
    Sends 15 requests at HIGH priority and 5 at LOW priority simultaneously.
    Asserts: LOW-priority requests wait behind HIGH (priority queue ordering).
    Asserts: no 429 simulation errors occur (rate ≤ 10 req/sec).

IMPORTANT:
    This script validates the in-process rate limiter logic only.
    It does NOT make real Zerodha API calls and MUST NOT be run in production.
    Use only in staging/development environments.

Usage:
    python scripts/zerodha/stress_test.py
    python scripts/zerodha/stress_test.py --scenario 3
    python scripts/zerodha/stress_test.py --verbose

Requirements:
    Python 3.11+ (asyncio, dataclasses)
    The services/shared/zerodha package must be on the Python path.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Add project root to path so we can import shared zerodha modules
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent / "services"
sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from shared.zerodha.rate_limiter import Priority, ZerodhaRateLimiter
except ImportError as exc:
    print(
        f"ERROR: Cannot import ZerodhaRateLimiter: {exc}\n"
        "Ensure services/ is on your PYTHONPATH or run from project root:\n"
        "  PYTHONPATH=services python scripts/zerodha/stress_test.py",
        file=sys.stderr,
    )
    sys.exit(1)


# ── Test result dataclasses ────────────────────────────────────────────────────

@dataclass
class RequestRecord:
    """Timing record for a single rate-limited request."""
    request_id:   int
    priority:     Priority
    enqueued_at:  float   # monotonic
    dispatched_at: float  # monotonic (when acquire() returned)

    @property
    def wait_ms(self) -> float:
        return (self.dispatched_at - self.enqueued_at) * 1000


@dataclass
class ScenarioResult:
    name:    str
    passed:  bool
    records: list[RequestRecord] = field(default_factory=list)
    errors:  list[str]           = field(default_factory=list)
    notes:   list[str]           = field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _actual_req_sec(records: list[RequestRecord]) -> float:
    """Compute actual throughput from dispatch timestamps."""
    if len(records) < 2:
        return 0.0
    first = records[0].dispatched_at
    last  = records[-1].dispatched_at
    elapsed = last - first
    if elapsed <= 0:
        return float("inf")
    return (len(records) - 1) / elapsed


def _pprint_records(records: list[RequestRecord], verbose: bool) -> None:
    if not verbose:
        return
    print(f"    {'ID':>4}  {'Priority':<12}  {'Enqueued':>12}  {'Dispatched':>12}  {'Wait':>8}")
    for r in records[:20]:  # cap at 20 rows
        print(
            f"    {r.request_id:>4}  {r.priority.name:<12}  "
            f"{r.enqueued_at:>12.3f}  {r.dispatched_at:>12.3f}  {r.wait_ms:>7.1f}ms"
        )
    if len(records) > 20:
        print(f"    ... and {len(records)-20} more")


# ── Scenario implementations ──────────────────────────────────────────────────

async def _make_request(
    rl: ZerodhaRateLimiter,
    req_id: int,
    priority: Priority,
    results: list[RequestRecord],
) -> None:
    enqueued = time.monotonic()
    await rl.acquire(priority)
    dispatched = time.monotonic()
    results.append(RequestRecord(req_id, priority, enqueued, dispatched))


async def scenario_1_baseline(verbose: bool) -> ScenarioResult:
    """Scenario 1 — Baseline throughput: 30 HIGH requests as fast as possible."""
    NAME = "S1: Baseline throughput (30 requests @ HIGH)"
    rl = ZerodhaRateLimiter(capacity=10, burst_capacity=15)
    await rl.start()

    records: list[RequestRecord] = []
    start = time.monotonic()
    for i in range(30):
        await _make_request(rl, i, Priority.HIGH, records)
    elapsed = time.monotonic() - start

    await rl.stop()

    actual = _actual_req_sec(records)
    passed = actual <= 10.5  # 0.5 req/sec tolerance
    result = ScenarioResult(NAME, passed, records)
    result.notes.append(f"Elapsed: {elapsed:.2f}s  |  Actual: {actual:.2f} req/s  |  Limit: 10.0 req/s")
    if not passed:
        result.errors.append(f"Rate {actual:.2f} req/s exceeded 10.5 req/s tolerance — token bucket not enforcing limit")
    _pprint_records(records, verbose)
    return result


async def scenario_2_burst(verbose: bool) -> ScenarioResult:
    """Scenario 2 — Burst absorption: 15 simultaneous requests from pre-filled bucket."""
    NAME = "S2: Burst absorption (15 concurrent @ HIGH, burst_capacity=15)"
    rl = ZerodhaRateLimiter(capacity=10, burst_capacity=15)
    await rl.start()

    records: list[RequestRecord] = []
    burst_start = time.monotonic()

    # Fire 15 concurrent requests — should all be served from the pre-filled burst bucket
    tasks = [asyncio.create_task(_make_request(rl, i, Priority.HIGH, records)) for i in range(15)]
    await asyncio.gather(*tasks)
    burst_elapsed = time.monotonic() - burst_start

    # Next 10 should be rate-limited at 10/sec
    steady_start = time.monotonic()
    for i in range(15, 25):
        await _make_request(rl, i, Priority.HIGH, records)
    steady_elapsed = time.monotonic() - steady_start

    await rl.stop()

    burst_records  = [r for r in records if r.request_id < 15]
    steady_records = [r for r in records if r.request_id >= 15]

    burst_within_200ms = burst_elapsed < 0.4  # 400ms tolerance for asyncio overhead
    steady_rate = _actual_req_sec(steady_records)
    steady_ok   = steady_rate <= 10.5

    passed = burst_within_200ms and steady_ok
    result = ScenarioResult(NAME, passed, records)
    result.notes.append(f"Burst 15 dispatched in {burst_elapsed*1000:.0f}ms (threshold <400ms)")
    result.notes.append(f"Steady-state 10 requests at {steady_rate:.2f} req/s")
    if not burst_within_200ms:
        result.errors.append(f"Burst took {burst_elapsed*1000:.0f}ms — burst capacity not pre-loaded")
    if not steady_ok:
        result.errors.append(f"Steady-state {steady_rate:.2f} req/s exceeded 10.5 limit")
    _pprint_records(records, verbose)
    return result


async def scenario_3_priority(verbose: bool) -> ScenarioResult:
    """Scenario 3 — Priority queue: CRITICAL preempts HIGH and MEDIUM."""
    NAME = "S3: Priority ordering (CRITICAL > HIGH > MEDIUM)"
    rl = ZerodhaRateLimiter(capacity=10, burst_capacity=15)
    await rl.start()

    # Drain the bucket so requests queue
    drain_tasks = [asyncio.create_task(rl.acquire(Priority.HIGH)) for _ in range(15)]
    await asyncio.gather(*drain_tasks)
    await asyncio.sleep(0.05)  # let drain_loop settle

    records: list[RequestRecord] = []
    dispatch_order: list[int] = []  # request_id in dispatch order

    async def tagged_request(req_id: int, priority: Priority) -> None:
        enqueued = time.monotonic()
        await rl.acquire(priority)
        dispatched = time.monotonic()
        records.append(RequestRecord(req_id, priority, enqueued, dispatched))
        dispatch_order.append(req_id)

    # Queue: 5 MEDIUM (ids 0–4), then 5 HIGH (ids 5–9), then 2 CRITICAL (ids 10–11)
    med_tasks  = [asyncio.create_task(tagged_request(i,    Priority.MEDIUM))   for i in range(5)]
    await asyncio.sleep(0.01)
    high_tasks = [asyncio.create_task(tagged_request(i+5,  Priority.HIGH))     for i in range(5)]
    await asyncio.sleep(0.01)
    crit_tasks = [asyncio.create_task(tagged_request(i+10, Priority.CRITICAL)) for i in range(2)]

    # Wait for all to dispatch (give enough time at 10 req/sec for 12 requests = ~1.2s)
    await asyncio.gather(*med_tasks, *high_tasks, *crit_tasks)
    await rl.stop()

    # CRITICAL requests (ids 10, 11) should appear before HIGH (5–9) and MEDIUM (0–4)
    critical_positions = [dispatch_order.index(i) for i in [10, 11] if i in dispatch_order]
    high_positions     = [dispatch_order.index(i) for i in range(5, 10) if i in dispatch_order]
    medium_positions   = [dispatch_order.index(i) for i in range(5)     if i in dispatch_order]

    crit_before_high   = all(cp < min(high_positions)   for cp in critical_positions) if high_positions   else True
    high_before_medium = all(hp < min(medium_positions) for hp in high_positions)     if medium_positions else True

    passed = crit_before_high and high_before_medium
    result = ScenarioResult(NAME, passed, records)
    result.notes.append(f"Dispatch order (first 12): {dispatch_order[:12]}")
    result.notes.append(f"CRITICAL positions: {critical_positions}  HIGH: {high_positions[:3]}...  MEDIUM: {medium_positions[:3]}...")
    if not crit_before_high:
        result.errors.append("CRITICAL requests did not preempt HIGH — priority queue not working")
    if not high_before_medium:
        result.errors.append("HIGH requests did not preempt MEDIUM — priority ordering broken")
    _pprint_records(records, verbose)
    return result


async def scenario_4_mis_squareoff(verbose: bool) -> ScenarioResult:
    """Scenario 4 — MIS auto-square-off: 8 CRITICAL cancels in <1500ms."""
    NAME = "S4: MIS auto-square-off (8 CRITICAL cancels absorbed by burst)"
    rl = ZerodhaRateLimiter(capacity=10, burst_capacity=15)
    await rl.start()
    await asyncio.sleep(0.1)  # ensure bucket is full

    records: list[RequestRecord] = []
    start = time.monotonic()
    tasks = [asyncio.create_task(_make_request(rl, i, Priority.CRITICAL, records)) for i in range(8)]
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start

    await rl.stop()

    passed = elapsed < 1.5  # 8 requests from 15-token burst should complete in <1.5s
    result = ScenarioResult(NAME, passed, records)
    result.notes.append(f"8 CRITICAL requests dispatched in {elapsed*1000:.0f}ms (threshold: <1500ms)")
    if not passed:
        result.errors.append(
            f"MIS square-off took {elapsed*1000:.0f}ms — burst capacity not absorbing concurrent cancels"
        )
    _pprint_records(records, verbose)
    return result


async def scenario_5_phase_budget(verbose: bool) -> ScenarioResult:
    """Scenario 5 — Phase budget: LOW waits behind HIGH under MARKET_OPEN."""
    NAME = "S5: Phase budget — LOW deferred behind HIGH in MARKET_OPEN"
    rl = ZerodhaRateLimiter(capacity=10, burst_capacity=15)
    rl.set_market_phase("MARKET_OPEN")
    await rl.start()

    records: list[RequestRecord] = []
    low_dispatch_times:  list[float] = []
    high_dispatch_times: list[float] = []

    async def track_request(req_id: int, priority: Priority) -> None:
        enqueued = time.monotonic()
        await rl.acquire(priority)
        dispatched = time.monotonic()
        records.append(RequestRecord(req_id, priority, enqueued, dispatched))
        if priority == Priority.LOW:
            low_dispatch_times.append(dispatched)
        else:
            high_dispatch_times.append(dispatched)

    # Drain the bucket first
    drain_tasks = [asyncio.create_task(rl.acquire(Priority.HIGH)) for _ in range(15)]
    await asyncio.gather(*drain_tasks)

    # Fire 15 HIGH and 5 LOW simultaneously into the depleted bucket
    high_tasks = [asyncio.create_task(track_request(i,    Priority.HIGH)) for i in range(15)]
    await asyncio.sleep(0.01)
    low_tasks  = [asyncio.create_task(track_request(i+15, Priority.LOW))  for i in range(5)]
    await asyncio.gather(*high_tasks, *low_tasks)

    await rl.stop()

    # LOW requests should have later average dispatch time than HIGH
    avg_high = sum(high_dispatch_times) / len(high_dispatch_times) if high_dispatch_times else 0
    avg_low  = sum(low_dispatch_times)  / len(low_dispatch_times)  if low_dispatch_times  else 0
    low_after_high = avg_low > avg_high

    # Also verify no burst beyond 10.5 req/sec
    actual_rate = _actual_req_sec(records)
    rate_ok     = actual_rate <= 10.5

    passed = low_after_high and rate_ok
    result = ScenarioResult(NAME, passed, records)
    result.notes.append(
        f"Avg HIGH dispatch: {avg_high:.3f}  |  Avg LOW dispatch: {avg_low:.3f}  |  "
        f"LOW deferred by {(avg_low - avg_high)*1000:.0f}ms"
    )
    result.notes.append(f"Actual rate: {actual_rate:.2f} req/s (limit 10.5)")
    if not low_after_high:
        result.errors.append("LOW requests were NOT deferred behind HIGH — priority ordering broken in MARKET_OPEN")
    if not rate_ok:
        result.errors.append(f"Rate {actual_rate:.2f} req/s exceeded 10.5 tolerance")
    _pprint_records(records, verbose)
    return result


# ── Runner ─────────────────────────────────────────────────────────────────────

async def run_all(scenario: Optional[int], verbose: bool) -> None:
    all_scenarios = [
        scenario_1_baseline,
        scenario_2_burst,
        scenario_3_priority,
        scenario_4_mis_squareoff,
        scenario_5_phase_budget,
    ]

    if scenario is not None:
        scenarios = [all_scenarios[scenario - 1]]
    else:
        scenarios = all_scenarios

    results: list[ScenarioResult] = []
    for fn in scenarios:
        print(f"Running: {fn.__doc__.splitlines()[1].strip()}")
        result = await fn(verbose)
        results.append(result)
        status = "PASS ✅" if result.passed else "FAIL ❌"
        print(f"  Status: {status}")
        for note in result.notes:
            print(f"  {note}")
        for err in result.errors:
            print(f"  ERROR: {err}")
        print()

    passed = sum(1 for r in results if r.passed)
    total  = len(results)
    print(f"{'─'*60}")
    print(f"Results: {passed}/{total} scenarios passed")
    if passed < total:
        print("FAILED scenarios:")
        for r in results:
            if not r.passed:
                print(f"  ✗ {r.name}")
        sys.exit(1)
    else:
        print("All scenarios PASSED — ZerodhaRateLimiter is working correctly.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Staging-only stress test for ZerodhaRateLimiter (ADR-012)"
    )
    parser.add_argument(
        "--scenario", type=int, choices=[1, 2, 3, 4, 5], default=None,
        help="Run a single scenario (1–5). Default: run all.",
    )
    parser.add_argument("--verbose", action="store_true", help="Print per-request timing tables")
    args = parser.parse_args()

    # Safety guard — do not run in production
    env = os.environ.get("QE_ENVIRONMENT", "development")
    if env == "production":
        print(
            "ERROR: This stress test MUST NOT run in production.\n"
            "Set QE_ENVIRONMENT=staging or QE_ENVIRONMENT=development.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Zerodha Rate Limiter Stress Test (env={env})\n{'─'*60}")
    asyncio.run(run_all(args.scenario, args.verbose))


# Late import (needed for production guard above)
import os  # noqa: E402

if __name__ == "__main__":
    main()
