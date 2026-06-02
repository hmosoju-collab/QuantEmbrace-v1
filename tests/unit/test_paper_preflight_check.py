"""
Regression tests for the paper-trading pre-flight kill-switch check.

Phase 2.1 / Q4: ``scripts/deploy/paper_preflight_check.py`` previously read the
kill switch from a key that never matched a real row
(``PK=KILL_SWITCH#GLOBAL, SK=STATE, field "state"``). As a result an ACTIVE
kill switch was silently reported PASS, so a paper session could start while
trading was supposed to be halted.

These tests prove the fixed check:

  1. Reads the PRODUCTION kill-switch key: PK=KILLSWITCH, SK=GLOBAL.
  2. Detects an active kill switch (``active``=True) and reports FAIL.
  3. Reports PASS when the kill switch is inactive or the row is absent.
  4. Reports WARN (not a false PASS) when the DynamoDB read raises.

The check logic was extracted into ``check_kill_switch(dynamo, prefix, report)``
so it can be exercised directly with a stubbed DynamoDB client — no LocalStack,
no boto3, no network.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest

# ── Load the pre-flight script as a module ──────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
_SERVICES = os.path.join(_ROOT, "services")
# Put services/ on the path so the script's `from shared.risk_state import ...`
# resolves to the REAL canonical key/reader (this is what we want to verify).
if _SERVICES not in sys.path:
    sys.path.insert(0, _SERVICES)

_SCRIPT_PATH = os.path.join(_ROOT, "scripts", "deploy", "paper_preflight_check.py")


def _load_preflight():
    spec = importlib.util.spec_from_file_location("paper_preflight_check", _SCRIPT_PATH)
    assert spec and spec.loader, f"cannot load {_SCRIPT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the @dataclass decorator introspects
    # sys.modules[cls.__module__] on Python 3.10, which fails if the module
    # is not yet registered.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


preflight = _load_preflight()

# Canonical key the fixed check MUST use.
_CANONICAL_KEY = {"PK": {"S": "KILLSWITCH"}, "SK": {"S": "GLOBAL"}}
# The OLD, wrong key the bug used — must NOT be what the check queries.
_OLD_WRONG_KEY = {"PK": {"S": "KILL_SWITCH#GLOBAL"}, "SK": {"S": "STATE"}}


class _FakeDynamo:
    """Minimal DynamoDB stub keyed on (PK, SK).

    Only returns an Item when queried with a key that exactly matches one that
    was seeded. This is what makes the regression meaningful: the active
    kill-switch row is seeded ONLY under the canonical key, so a check that
    queries the old key gets ``{}`` (and would wrongly PASS).
    """

    def __init__(self, items: dict[tuple[str, str], dict] | None = None,
                 raise_on_get: bool = False) -> None:
        self._items = items or {}
        self._raise = raise_on_get
        self.calls: list[dict] = []

    def get_item(self, TableName: str, Key: dict, **kwargs):  # noqa: N803 (boto3 casing)
        self.calls.append({"TableName": TableName, "Key": Key, "kwargs": kwargs})
        if self._raise:
            raise RuntimeError("simulated DynamoDB outage")
        pk = Key["PK"]["S"]
        sk = Key["SK"]["S"]
        item = self._items.get((pk, sk))
        return {"Item": item} if item is not None else {}


def _status_of(report, name: str) -> str:
    for c in report.checks:
        if c.name == name:
            return c.status
    raise AssertionError(f"no check named {name!r} in report")


def _message_of(report, name: str) -> str:
    for c in report.checks:
        if c.name == name:
            return c.message
    raise AssertionError(f"no check named {name!r} in report")


class TestKillSwitchKeyFix(unittest.TestCase):
    """The check must query the canonical KILLSWITCH/GLOBAL key."""

    def test_queries_canonical_key_not_old_key(self):
        dynamo = _FakeDynamo()
        report = preflight.PreflightReport()
        preflight.check_kill_switch(dynamo, "quantembrace-development", report)

        self.assertEqual(len(dynamo.calls), 1)
        key = dynamo.calls[0]["Key"]
        self.assertEqual(key, _CANONICAL_KEY,
                         "kill-switch check must read PK=KILLSWITCH, SK=GLOBAL")
        self.assertNotEqual(key, _OLD_WRONG_KEY,
                            "kill-switch check must NOT use the old KILL_SWITCH#GLOBAL/STATE key")

    def test_reads_against_risk_state_table(self):
        dynamo = _FakeDynamo()
        report = preflight.PreflightReport()
        preflight.check_kill_switch(dynamo, "quantembrace-development", report)
        self.assertEqual(dynamo.calls[0]["TableName"], "quantembrace-development-risk-state")


class TestKillSwitchOnDetected(unittest.TestCase):
    """An ACTIVE kill switch must be detected and reported FAIL."""

    def test_active_bool_true_is_fail(self):
        dynamo = _FakeDynamo(items={
            ("KILLSWITCH", "GLOBAL"): {
                "PK": {"S": "KILLSWITCH"}, "SK": {"S": "GLOBAL"},
                "active": {"BOOL": True},
                "reason": {"S": "manual halt"},
            },
        })
        report = preflight.PreflightReport()
        preflight.check_kill_switch(dynamo, "quantembrace-development", report)

        self.assertEqual(_status_of(report, "dynamodb:kill_switch"), "FAIL")
        self.assertIn("manual halt", _message_of(report, "dynamodb:kill_switch"))

    def test_active_via_status_string_is_fail(self):
        # Some writers set status="ACTIVE" without an explicit `active` BOOL.
        # shared.risk_state.attr_bool resolves this to True for name="active".
        dynamo = _FakeDynamo(items={
            ("KILLSWITCH", "GLOBAL"): {
                "PK": {"S": "KILLSWITCH"}, "SK": {"S": "GLOBAL"},
                "status": {"S": "ACTIVE"},
                "reason": {"S": "auto trip"},
            },
        })
        report = preflight.PreflightReport()
        preflight.check_kill_switch(dynamo, "quantembrace-development", report)
        self.assertEqual(_status_of(report, "dynamodb:kill_switch"), "FAIL")

    def test_canonical_kill_switch_item_active_is_fail(self):
        # Build the item exactly as the production schema writes it.
        from shared.risk_state import kill_switch_item

        item = kill_switch_item(
            active=True,
            reason="circuit breaker",
            activated_by="ops",
            updated_at="2026-05-30T10:00:00Z",
        )
        dynamo = _FakeDynamo(items={("KILLSWITCH", "GLOBAL"): item})
        report = preflight.PreflightReport()
        preflight.check_kill_switch(dynamo, "quantembrace-development", report)
        self.assertEqual(_status_of(report, "dynamodb:kill_switch"), "FAIL")
        self.assertIn("circuit breaker", _message_of(report, "dynamodb:kill_switch"))

    def test_regression_old_key_would_have_missed_active_switch(self):
        # The active row exists ONLY under the canonical key. Prove that the
        # fixed check still detects it (FAIL). With the old key the same
        # FakeDynamo returns {} -> the bug would have reported PASS.
        items = {("KILLSWITCH", "GLOBAL"): {"active": {"BOOL": True}}}
        dynamo = _FakeDynamo(items=items)
        report = preflight.PreflightReport()
        preflight.check_kill_switch(dynamo, "quantembrace-development", report)
        self.assertEqual(_status_of(report, "dynamodb:kill_switch"), "FAIL")

        # Sanity: the old key truly does not match the seeded row.
        self.assertEqual(dynamo.get_item(TableName="t", Key=_OLD_WRONG_KEY), {})


class TestKillSwitchOffOrAbsent(unittest.TestCase):
    """Inactive / absent kill switch must report PASS (safe to start)."""

    def test_active_bool_false_is_pass(self):
        dynamo = _FakeDynamo(items={
            ("KILLSWITCH", "GLOBAL"): {"active": {"BOOL": False}, "status": {"S": "INACTIVE"}},
        })
        report = preflight.PreflightReport()
        preflight.check_kill_switch(dynamo, "quantembrace-development", report)
        self.assertEqual(_status_of(report, "dynamodb:kill_switch"), "PASS")

    def test_absent_row_is_pass(self):
        dynamo = _FakeDynamo(items={})  # no kill-switch row at all
        report = preflight.PreflightReport()
        preflight.check_kill_switch(dynamo, "quantembrace-development", report)
        self.assertEqual(_status_of(report, "dynamodb:kill_switch"), "PASS")


class TestKillSwitchReadError(unittest.TestCase):
    """A read error must WARN (never a false PASS that hides an active switch)."""

    def test_read_exception_is_warn(self):
        dynamo = _FakeDynamo(raise_on_get=True)
        report = preflight.PreflightReport()
        preflight.check_kill_switch(dynamo, "quantembrace-development", report)
        self.assertEqual(_status_of(report, "dynamodb:kill_switch"), "WARN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
