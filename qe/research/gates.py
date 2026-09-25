"""Pre-registered gate evaluation: metric OP threshold, fail-closed.

Gates are declared in the study config BEFORE the run and never relaxed to
force a pass (the methodology that held through 2026 — consolidation memo §4).
A gate naming a metric the study didn't compute FAILS, it doesn't skip.
"""

import operator

from qe.config import GateSpec

_OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}


def evaluate_gates(gates: tuple[GateSpec, ...], metrics: dict) -> list[dict]:
    results = []
    for g in gates:
        actual = metrics.get(g.metric)
        passed = bool(_OPS[g.op](actual, g.value)) if actual is not None else False
        results.append(
            {
                "name": g.name,
                "metric": g.metric,
                "op": g.op,
                "value": g.value,
                "actual": actual,
                "passed": passed,
            }
        )
    return results


def all_passed(results: list[dict]) -> bool:
    return bool(results) and all(r["passed"] for r in results)
