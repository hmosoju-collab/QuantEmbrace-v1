"""
Phase 2.1 / Q5 — committed coverage for the three live-readiness test gaps.

This file closes the Q5 gaps the Phase-2 audit flagged. All three exercise REAL
production modules (no reimplemented copies):

  Gap 1 — Read-only DynamoDB proxy verification  (PROMOTED to a committed test)
      scripts/read_only_live_readiness_runtime_check.py :: _ReadOnlyDynamo
      Proves the proxy delegates read calls and STRUCTURALLY blocks every write
      (put/update/delete/batch_write/transact_write/create/delete/update table),
      plus anything off the read allow-list. This is the guarantee that the
      runtime check can never mutate a live table even by accident.

  Gap 2 — live_trading_enabled absent/false keeps live exits DISARMED
      services/execution_engine/exit/exit_order_router.py :: ExitOrderRouter.route
      Proves a LIVE-mode exit is blocked pre-lock when the flag is False (and
      that the flag defaults to False when omitted), so no DynamoDB lock is
      taken and no broker call is made. A control case shows the gate is keyed
      on the flag (True -> the lock IS attempted), not on the mode alone.

  Gap 3 — Zerodha token freshness
      services/execution_engine/auth/zerodha_auth.py :: ZerodhaTokenManager
      Token-freshness logic EXISTS, so this is a real test (not
      RUNTIME_VERIFICATION_REQUIRED): a stale stored token is rejected with
      TokenExpiredError, a fresh token is accepted, a missing row raises, and
      the in-memory is_token_valid() honours the cached expiry.

Standalone:
    python tests/unit/test_read_only_runtime_check.py
"""

from __future__ import annotations

import asyncio
import importlib.util as _ilu
import logging
import os
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

# ── Paths ───────────────────────────────────────────────────────────────────────
_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
_SCRIPTS_DIR  = os.path.join(_PROJECT_ROOT, "scripts")


# ── Minimal logger stub (mirrors the other unit suites) ─────────────────────────
class _Logger:
    def __init__(self, name=""):
        self._log = logging.getLogger(name)

    def info(self, msg, *a, **kw):      self._log.info(msg)
    def warning(self, msg, *a, **kw):   self._log.warning(msg)
    def debug(self, msg, *a, **kw):     self._log.debug(msg)
    def error(self, msg, *a, **kw):     self._log.error(msg)
    def exception(self, msg, *a, **kw): self._log.exception(msg)
    def critical(self, msg, *a, **kw):  self._log.critical(msg)


@dataclass
class _LiveCounters:
    """Stand-in for services.shared.monitoring.LiveCounters (only the fields the
    router touches; all default to zero)."""
    router_live_attempts: int = 0
    router_live_blocked: int = 0
    router_idempotency_skips: int = 0
    router_idempotency_successes: int = 0
    router_paper_exits: int = 0
    router_failed_routes: int = 0
    realized_pnl: float = 0.0


# ── Stub installer (non-clobbering: never overwrites a real module/attr) ────────
def _ensure(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        if "." in name:
            parent, _, child = name.rpartition(".")
            p = sys.modules.get(parent)
            if p is not None:
                setattr(p, child, mod)
    return mod


def _ensure_attr(mod: types.ModuleType, key: str, val) -> None:
    if not hasattr(mod, key):
        setattr(mod, key, val)


def _install_stubs() -> None:
    # shared.* (used lazily by exit_order_router and at import by zerodha_auth)
    _ensure("shared")
    _ensure("shared.logging")
    log_mod = _ensure("shared.logging.logger")
    _ensure_attr(log_mod, "get_logger", lambda *a, **k: _Logger())
    _ensure_attr(log_mod, "set_correlation_id", lambda *a, **k: None)

    _ensure("shared.utils")
    helpers = _ensure("shared.utils.helpers")
    _ensure_attr(helpers, "utc_now", lambda: datetime.now(timezone.utc))

    _ensure("shared.config")
    settings_mod = _ensure("shared.config.settings")
    _ensure_attr(settings_mod, "AppSettings", object)
    _ensure_attr(
        settings_mod, "get_settings",
        lambda: SimpleNamespace(
            aws=SimpleNamespace(dynamodb_table_sessions="t-sessions"),
            environment="dev",
        ),
    )

    risk_state = _ensure("shared.risk_state")
    _ensure_attr(
        risk_state, "position_key",
        lambda symbol: {"PK": {"S": f"POSITION#{symbol}"}, "SK": {"S": "CURRENT"}},
    )
    # Phase 7.1: add symbols needed by test_paper_preflight_check when tests run in
    # the same process (stub is non-clobbering but must be complete enough).
    _ensure_attr(
        risk_state, "kill_switch_item",
        lambda *, active, reason, activated_by, updated_at, activated_at=None,
               deactivated_at=None, detail="": {
            "PK": {"S": "KILLSWITCH"}, "SK": {"S": "GLOBAL"},
            "active": {"BOOL": active},
            "status": {"S": "ACTIVE" if active else "INACTIVE"},
            "reason": {"S": reason},
            "activated_by": {"S": activated_by},
            "updated_at": {"S": updated_at},
        },
    )
    _ensure_attr(
        risk_state, "kill_switch_key",
        lambda: {"PK": {"S": "KILLSWITCH"}, "SK": {"S": "GLOBAL"}},
    )
    _ensure_attr(risk_state, "attr_bool", lambda item, name, default=False: default)
    _ensure_attr(risk_state, "attr_string", lambda item, name, default="": default)

    # services.* (exit_order_router imports these with the `services.` prefix)
    _ensure("services")
    _ensure("services.shared")
    _ensure("services.shared.logging")
    s_log = _ensure("services.shared.logging.logger")
    _ensure_attr(s_log, "get_logger", lambda *a, **k: _Logger())
    _ensure_attr(s_log, "set_correlation_id", lambda *a, **k: None)

    s_mon = _ensure("services.shared.monitoring")
    _ensure_attr(s_mon, "LiveCounters", _LiveCounters)

    _ensure("services.execution_engine")
    _ensure("services.execution_engine.exit")
    s_models = _ensure("services.execution_engine.exit.exit_models")
    _ensure_attr(s_models, "ExitOrderRequest", object)


def _load_from_file(path: str, name: str):
    """Load a module from an explicit file path. Registers it in sys.modules
    before exec so module-level @dataclass decorators work on Python 3.10."""
    spec = _ilu.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_install_stubs()

rocheck = _load_from_file(
    os.path.join(_SCRIPTS_DIR, "read_only_live_readiness_runtime_check.py"),
    "read_only_live_readiness_runtime_check",
)
exitmod = _load_from_file(
    os.path.join(_SERVICES_DIR, "execution_engine", "exit", "exit_order_router.py"),
    "exit_order_router_under_test",
)
authmod = _load_from_file(
    os.path.join(_SERVICES_DIR, "execution_engine", "auth", "zerodha_auth.py"),
    "zerodha_auth_under_test",
)


# ═══════════════════════════════════════════════════════════════════════════════
# Gap 1 — Read-only DynamoDB proxy verification (PROMOTED)
# ═══════════════════════════════════════════════════════════════════════════════
class TestReadOnlyDynamoProxy(unittest.TestCase):
    """_ReadOnlyDynamo must delegate reads and block ALL writes."""

    _WRITES = ["put_item", "update_item", "delete_item", "batch_write_item",
               "transact_write_items", "create_table", "delete_table", "update_table"]
    _READS  = ["get_item", "query", "scan", "list_tables", "describe_table",
               "batch_get_item"]

    def test_read_methods_delegate_to_client(self):
        client = MagicMock()
        client.get_item.return_value = {"Item": {"x": {"S": "1"}}}
        proxy = rocheck._ReadOnlyDynamo(client)
        out = proxy.get_item(TableName="t", Key={"PK": {"S": "k"}})
        self.assertEqual(out, {"Item": {"x": {"S": "1"}}})
        client.get_item.assert_called_once()

    def test_all_read_methods_are_allowed(self):
        client = MagicMock()
        proxy = rocheck._ReadOnlyDynamo(client)
        for m in self._READS:
            getattr(proxy, m)(TableName="t")  # must not raise
            getattr(client, m).assert_called_once()

    def test_every_write_method_raises_blocked(self):
        client = MagicMock()
        proxy = rocheck._ReadOnlyDynamo(client)
        for w in self._WRITES:
            with self.assertRaises(RuntimeError) as cm:
                getattr(proxy, w)          # attribute access itself must raise
            self.assertIn("BLOCKED", str(cm.exception))
        # The underlying client's write methods were never even reached.
        for w in self._WRITES:
            getattr(client, w).assert_not_called()

    def test_unknown_method_is_denied_by_default(self):
        client = MagicMock()
        proxy = rocheck._ReadOnlyDynamo(client)
        with self.assertRaises(RuntimeError):
            getattr(proxy, "execute_statement")


class TestRuntimeVerdict(unittest.TestCase):
    """Report.verdict() — unreachable runtime must demand verification, not GO."""

    def test_unreachable_runtime_is_verification_required(self):
        r = rocheck.Report()
        r.runtime_reachable = False
        r.add("connectivity", "UNKNOWN", "down")
        self.assertEqual(r.verdict(), rocheck.VERIFICATION_REQ)

    def test_reachable_with_fail_is_unsafe(self):
        r = rocheck.Report()
        r.runtime_reachable = True
        r.add("x", "FAIL", "bad")
        self.assertEqual(r.verdict(), rocheck.UNSAFE)

    def test_reachable_all_pass_is_paper_safe(self):
        r = rocheck.Report()
        r.runtime_reachable = True
        r.add("x", "PASS", "ok")
        self.assertEqual(r.verdict(), rocheck.PAPER_SAFE)


# ═══════════════════════════════════════════════════════════════════════════════
# Gap 2 — live_trading_enabled absent/false keeps live exits DISARMED
# ═══════════════════════════════════════════════════════════════════════════════
class TestExitRouterLiveGate(unittest.TestCase):
    """ExitOrderRouter.route — LIVE exits are gated by live_trading_enabled."""

    def _request(self):
        return SimpleNamespace(
            symbol="RELIANCE",
            trigger_type=SimpleNamespace(value="STOP_LOSS"),
            exit_id="exit-1",
            is_kill_switch=False,
        )

    def test_live_blocked_when_flag_false(self):
        dynamo = MagicMock()
        broker = MagicMock()
        counters = _LiveCounters()
        router = exitmod.ExitOrderRouter(
            mode=exitmod.TradingMode.LIVE,
            dynamo_client=dynamo,
            positions_table="t-positions",
            zerodha_broker=broker,
            live_trading_enabled=False,
            live_counters=counters,
        )
        result = asyncio.run(router.route(self._request()))

        self.assertFalse(result)                       # exit refused
        dynamo.update_item.assert_not_called()         # no idempotency lock taken
        broker.place_order.assert_not_called()         # no broker order
        self.assertEqual(counters.router_live_blocked, 1)
        self.assertEqual(counters.router_live_attempts, 1)

    def test_live_blocked_by_default_when_flag_absent(self):
        # live_trading_enabled omitted entirely -> constructor default must be False.
        dynamo = MagicMock()
        broker = MagicMock()
        router = exitmod.ExitOrderRouter(
            mode=exitmod.TradingMode.LIVE,
            dynamo_client=dynamo,
            positions_table="t-positions",
            zerodha_broker=broker,
        )
        result = asyncio.run(router.route(self._request()))
        self.assertFalse(result)
        dynamo.update_item.assert_not_called()
        broker.place_order.assert_not_called()

    def test_gate_is_keyed_on_flag_not_mode(self):
        # Control: with the flag True the gate is PASSED and the DynamoDB
        # idempotency lock is attempted (here it fails the conditional write,
        # so the exit still does not complete — but update_item WAS called,
        # which never happens in the disarmed cases above).
        dynamo = MagicMock()
        dynamo.update_item.side_effect = Exception("ConditionalCheckFailedException")
        router = exitmod.ExitOrderRouter(
            mode=exitmod.TradingMode.LIVE,
            dynamo_client=dynamo,
            positions_table="t-positions",
            zerodha_broker=MagicMock(),
            live_trading_enabled=True,
            live_counters=_LiveCounters(),
        )
        result = asyncio.run(router.route(self._request()))
        self.assertFalse(result)
        dynamo.update_item.assert_called_once()        # gate passed -> lock attempted


# ═══════════════════════════════════════════════════════════════════════════════
# Gap 3 — Zerodha token freshness
# ═══════════════════════════════════════════════════════════════════════════════
class TestZerodhaTokenFreshness(unittest.TestCase):
    """ZerodhaTokenManager — stale tokens are rejected, fresh tokens accepted."""

    def _mgr(self, dynamo=None):
        settings = SimpleNamespace(aws=SimpleNamespace(dynamodb_table_sessions="t-sessions"))
        return authmod.ZerodhaTokenManager(
            dynamo_client=dynamo, table_name="t-sessions", settings=settings
        )

    @staticmethod
    def _token_item(expires_at: datetime, token: str = "tok"):
        return {"Item": {
            "access_token": {"S": token},
            "expires_at":   {"S": expires_at.isoformat()},
        }}

    # ── in-memory freshness ────────────────────────────────────────────────────
    def test_is_token_valid_false_without_cache(self):
        self.assertFalse(self._mgr().is_token_valid())

    def test_is_token_valid_true_for_future_expiry(self):
        m = self._mgr()
        m._cached_token = "tok"
        m._cached_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        self.assertTrue(m.is_token_valid())

    def test_is_token_valid_false_for_past_expiry(self):
        m = self._mgr()
        m._cached_token = "tok"
        m._cached_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        self.assertFalse(m.is_token_valid())

    # ── DynamoDB-backed freshness ───────────────────────────────────────────────
    def test_get_valid_token_rejects_stale_token(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        dynamo = MagicMock()
        dynamo.get_item.return_value = self._token_item(past, "stale-tok")
        with self.assertRaises(authmod.TokenExpiredError):
            asyncio.run(self._mgr(dynamo).get_valid_token())

    def test_get_valid_token_accepts_fresh_token(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        dynamo = MagicMock()
        dynamo.get_item.return_value = self._token_item(future, "fresh-tok")
        tok = asyncio.run(self._mgr(dynamo).get_valid_token())
        self.assertEqual(tok, "fresh-tok")

    def test_get_valid_token_raises_when_no_row(self):
        dynamo = MagicMock()
        dynamo.get_item.return_value = {}
        with self.assertRaises(authmod.TokenExpiredError):
            asyncio.run(self._mgr(dynamo).get_valid_token())

    def test_next_expiry_is_0200_utc_in_future(self):
        exp = authmod.ZerodhaTokenManager._next_expiry_utc()
        self.assertEqual((exp.hour, exp.minute, exp.second), (2, 0, 0))
        self.assertGreater(exp, datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
