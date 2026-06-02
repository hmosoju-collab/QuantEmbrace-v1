#!/usr/bin/env python3
"""
Read-Only Live-Readiness Runtime Check  (Phase 2 / Q4)
======================================================

Verifies the *runtime state* that a repo-only audit CANNOT see, using ONLY
read operations against DynamoDB and the process environment.

This script is the answer to Phase-2 open question Q4: a repo audit can prove
what the code *does*, but it cannot prove what the live tables currently
*contain* (token freshness, per-strategy paper_trade flags, kill-switch state,
reconciliation flag, mode env vars). This script reads that state and reports
it without ever mutating anything.

──────────────────────────────────────────────────────────────────────────────
HARD SAFETY GUARANTEES (by construction)
──────────────────────────────────────────────────────────────────────────────
  * READ-ONLY: DynamoDB access goes through ``_ReadOnlyDynamo``, a proxy that
    exposes ONLY get_item / query / scan / list_tables / describe_table and
    raises RuntimeError on ANY write call (put_item, update_item, delete_item,
    batch_write_item, transact_write_items, …). The script literally cannot
    mutate a table even by accident.
  * NO LIVE ENABLE: never sets, writes, or toggles any live-trading flag.
  * NO ORDERS: never constructs a broker client, never calls place_order.
  * NO SECRETS: reads the Zerodha token row for metadata only (existence,
    expires_at, freshness). The access_token VALUE is never read into a
    variable that is printed, logged, or emitted. (CLAUDE.md secrets rule.)

──────────────────────────────────────────────────────────────────────────────
WHAT IT CHECKS
──────────────────────────────────────────────────────────────────────────────
  1. Connectivity              — can we reach DynamoDB at all?
  2. Resolved table names      — paper/live table names actually in use
  3. Zerodha broker token      — exists? expires_at? fresh vs 02:00 UTC?
  4. strategy-config flags     — per-strategy enabled + paper_trade
  5. Kill-switch state         — active / inactive (production read path)
  6. Reconciliation flag       — required=True halts new entries (HARD_HALT)
  7. Mode / risk-stage env      — live_trading_enabled, RISK_PROFILE,
                                  UNIVERSE_MODE, EXECUTION_PAPER_TRADING

──────────────────────────────────────────────────────────────────────────────
EXIT CODES
──────────────────────────────────────────────────────────────────────────────
  0  RUNTIME_STATE_PAPER_SAFE        all checks ran, no FAIL. NOTE: this is
                                     NOT a live-GO authorization — it only
                                     confirms the runtime is in a paper-safe
                                     state.
  1  RUNTIME_STATE_UNSAFE            runtime reachable, but >=1 FAIL (e.g. an
                                     enabled strategy has paper_trade=False, or
                                     live_trading_enabled=true).
  2  RUNTIME_VERIFICATION_REQUIRED   runtime UNREACHABLE — state could not be
                                     verified. Do NOT mark GO. (per Q4)
  3  USAGE_ERROR                     bad invocation / missing boto3.

USAGE
─────
  python scripts/read_only_live_readiness_runtime_check.py
  python scripts/read_only_live_readiness_runtime_check.py --json
  python scripts/read_only_live_readiness_runtime_check.py --env production

Environment (same resolution the services use):
  AWS_REGION / AWS_DEFAULT_REGION
  AWS_ENDPOINT_URL                 set for LocalStack (paper); leave unset for AWS
  AWS_DYNAMODB_TABLE_PREFIX / DYNAMODB_TABLE_PREFIX
  QE_EXECUTION_LIVE_TRADING_ENABLED, RISK_PROFILE, UNIVERSE_MODE,
  EXECUTION_PAPER_TRADING, STRATEGY_WATCHLIST_NSE
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ── Path / .env bootstrap (mirrors scripts/kill_switch_cli.py) ─────────────────
_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

_env_file = os.path.join(_PROJECT_ROOT, ".env")
if os.path.exists(_env_file):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        with open(_env_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    os.environ.setdefault(_k.strip(), _v.strip())


# ── ANSI colours ───────────────────────────────────────────────────────────────
_GREEN, _RED, _YELLOW, _BLUE, _RESET, _BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
PASS_STR    = f"{_GREEN}✓ PASS{_RESET}"
FAIL_STR    = f"{_RED}✗ FAIL{_RESET}"
WARN_STR    = f"{_YELLOW}⚠ WARN{_RESET}"
UNKNOWN_STR = f"{_BLUE}? UNKNOWN{_RESET}"


# ── Verdict constants ───────────────────────────────────────────────────────────
PAPER_SAFE          = "RUNTIME_STATE_PAPER_SAFE"
UNSAFE              = "RUNTIME_STATE_UNSAFE"
VERIFICATION_REQ    = "RUNTIME_VERIFICATION_REQUIRED"
USAGE_ERROR         = "USAGE_ERROR"

_EXIT = {PAPER_SAFE: 0, UNSAFE: 1, VERIFICATION_REQ: 2, USAGE_ERROR: 3}


# ── Result model ────────────────────────────────────────────────────────────────
@dataclass
class CheckResult:
    name:    str
    status:  str            # PASS | FAIL | WARN | UNKNOWN
    message: str = ""
    detail:  dict = field(default_factory=dict)


@dataclass
class Report:
    checks: list = field(default_factory=list)
    runtime_reachable: bool = False

    def add(self, name: str, status: str, message: str = "", **detail: Any) -> None:
        self.checks.append(CheckResult(name, status, message, detail or {}))

    def _by(self, status: str) -> list:
        return [c for c in self.checks if c.status == status]

    @property
    def failed(self):  return self._by("FAIL")
    @property
    def warned(self):  return self._by("WARN")
    @property
    def unknown(self): return self._by("UNKNOWN")

    def verdict(self) -> str:
        if not self.runtime_reachable:
            return VERIFICATION_REQ
        if self.failed:
            return UNSAFE
        return PAPER_SAFE


# ── Read-only DynamoDB proxy ─────────────────────────────────────────────────────
_READ_METHODS  = {"get_item", "query", "scan", "list_tables", "describe_table",
                  "batch_get_item"}
_WRITE_METHODS = {"put_item", "update_item", "delete_item", "batch_write_item",
                  "transact_write_items", "create_table", "delete_table",
                  "update_table", "update_item", "transact_get_items"}


class _ReadOnlyDynamo:
    """
    Wraps a boto3 DynamoDB client and permits ONLY read operations.

    Any attempt to call a mutating method raises RuntimeError. This makes the
    script's read-only guarantee structural, not just a matter of discipline.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if name in _WRITE_METHODS:
            raise RuntimeError(
                f"BLOCKED: read-only runtime check attempted a write call "
                f"'{name}'. This script must never mutate DynamoDB."
            )
        if name in _READ_METHODS:
            return getattr(self._client, name)
        # Anything else (paginators, meta, etc.) is denied by default — the
        # script only needs the explicit read methods above.
        raise RuntimeError(
            f"BLOCKED: '{name}' is not on the read-only allow-list "
            f"{sorted(_READ_METHODS)}."
        )


# ── Helpers ──────────────────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _bool_env(key: str) -> bool:
    return _env(key).lower() in ("true", "1", "yes", "on")


def _attr_s(item: dict, name: str, default: str = "") -> str:
    raw = item.get(name)
    if isinstance(raw, dict):
        return str(raw.get("S", raw.get("N", default)))
    return str(raw) if raw is not None else default


def _attr_bool(item: dict, name: str, default: bool = False) -> bool:
    raw = item.get(name)
    if isinstance(raw, dict):
        if "BOOL" in raw:
            return bool(raw["BOOL"])
        if "S" in raw:
            return str(raw["S"]).upper() in {"ACTIVE", "TRUE", "1", "YES"}
    if isinstance(raw, bool):
        return raw
    return default


def _resolve_settings():
    """Return AppSettings if importable, else None (degrade to env-prefix)."""
    try:
        from shared.config.settings import get_settings  # type: ignore
        return get_settings()
    except Exception:
        return None


def _resolve_table_names(settings) -> dict:
    """
    Resolve the DynamoDB table names the services actually use. Prefer the
    project's settings (authoritative); fall back to the documented prefix
    convention so the script still works without a clean settings import.
    """
    if settings is not None:
        aws = settings.aws
        return {
            "sessions":        aws.dynamodb_table_sessions,
            "risk_state":      aws.dynamodb_table_risk_state,
            "strategy_config": aws.dynamodb_table_strategy_config,
            "orders":          aws.dynamodb_table_orders,
            "positions":       aws.dynamodb_table_positions,
            "_source":         "settings",
        }
    prefix = (_env("AWS_DYNAMODB_TABLE_PREFIX")
              or _env("DYNAMODB_TABLE_PREFIX")
              or "quantembrace")
    return {
        "sessions":        f"{prefix}-sessions",
        "risk_state":      f"{prefix}-risk-state",
        "strategy_config": f"{prefix}-strategy-config",
        "orders":          f"{prefix}-orders",
        "positions":       f"{prefix}-positions",
        "_source":         f"env-prefix:{prefix}",
    }


def _make_readonly_client() -> Optional[_ReadOnlyDynamo]:
    try:
        import boto3  # noqa: F401
    except ImportError:
        return None
    import boto3
    region   = _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or "ap-south-1"
    endpoint = _env("AWS_ENDPOINT_URL")  # set for LocalStack; empty for real AWS
    kwargs: dict = {"region_name": region}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
        # LocalStack accepts dummy creds; real AWS uses the instance role / profile
        kwargs["aws_access_key_id"]     = _env("AWS_ACCESS_KEY_ID", "test")
        kwargs["aws_secret_access_key"] = _env("AWS_SECRET_ACCESS_KEY", "test")
    return _ReadOnlyDynamo(boto3.client("dynamodb", **kwargs))


# ── Checks ────────────────────────────────────────────────────────────────────────
def check_connectivity(report: Report, dynamo: Optional[_ReadOnlyDynamo]) -> bool:
    if dynamo is None:
        report.add("connectivity", "UNKNOWN",
                   "boto3 not installed — cannot reach DynamoDB. "
                   "pip install boto3 and re-run on the trading host.")
        return False
    try:
        dynamo.list_tables()
    except Exception as exc:
        report.add("connectivity", "UNKNOWN",
                   f"DynamoDB unreachable: {type(exc).__name__}: {exc}. "
                   "Run this on the trading host / with AWS creds + region.")
        return False
    report.runtime_reachable = True
    report.add("connectivity", "PASS",
               f"DynamoDB reachable (endpoint={_env('AWS_ENDPOINT_URL') or 'AWS default'})")
    return True


def check_table_names(report: Report, tables: dict) -> None:
    report.add("table_names", "PASS",
               f"Resolved via {tables['_source']}: "
               f"sessions={tables['sessions']}, risk_state={tables['risk_state']}, "
               f"strategy_config={tables['strategy_config']}",
               **{k: v for k, v in tables.items() if not k.startswith("_")})


def check_zerodha_token(report: Report, dynamo: _ReadOnlyDynamo, sessions_table: str) -> None:
    """
    Read ONLY the token row metadata. The access_token VALUE is deliberately
    never extracted — only its presence (bool) and the expires_at timestamp.
    """
    try:
        resp = dynamo.get_item(
            TableName=sessions_table,
            Key={"PK": {"S": "ZERODHA#TOKEN"}, "SK": {"S": "CURRENT"}},
        )
    except Exception as exc:
        report.add("zerodha_token", "UNKNOWN",
                   f"Could not read token row from {sessions_table}: {exc}")
        return

    item = resp.get("Item")
    if not item:
        report.add("zerodha_token", "WARN",
                   "No Zerodha token row (ZERODHA#TOKEN/CURRENT) found. "
                   "Run scripts/zerodha_login.py before any session. "
                   "Live entry would fail-closed (broker not authenticated).")
        return

    token_present = bool(item.get("access_token", {}).get("S"))  # presence only
    expires_at_str = _attr_s(item, "expires_at", "")
    created_at_str = _attr_s(item, "created_at", "")
    fresh: Optional[bool] = None
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            fresh = _utc_now() < expires_at
        except ValueError:
            fresh = None

    detail = {"token_present": token_present,
              "expires_at": expires_at_str or "UNKNOWN",
              "created_at": created_at_str or "UNKNOWN",
              "fresh": fresh}

    if not token_present:
        report.add("zerodha_token", "WARN",
                   "Token row exists but access_token field is empty.", **detail)
    elif fresh is True:
        report.add("zerodha_token", "PASS",
                   f"Token present and FRESH (expires {expires_at_str} UTC). "
                   "[value not read]", **detail)
    elif fresh is False:
        report.add("zerodha_token", "WARN",
                   f"Token present but EXPIRED (expired {expires_at_str} UTC). "
                   "Re-run scripts/zerodha_login.py. Live entry fails-closed until refreshed.",
                   **detail)
    else:
        report.add("zerodha_token", "WARN",
                   "Token present but expires_at missing/unparseable — freshness UNKNOWN.",
                   **detail)


def check_strategy_config(report: Report, dynamo: _ReadOnlyDynamo,
                          strategy_table: str) -> None:
    """
    Scan strategy-config for PK begins_with 'STRATEGY_CONFIG#'. For each row,
    report enabled + paper_trade. An ENABLED strategy with paper_trade=False
    means it would route LIVE — a FAIL for a paper-safe runtime.
    """
    try:
        rows: list = []
        kwargs: dict = {
            "TableName": strategy_table,
            "FilterExpression": "begins_with(PK, :p)",
            "ExpressionAttributeValues": {":p": {"S": "STRATEGY_CONFIG#"}},
        }
        while True:
            resp = dynamo.scan(**kwargs)
            rows.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
    except Exception as exc:
        report.add("strategy_config", "UNKNOWN",
                   f"Could not scan {strategy_table}: {exc}")
        return

    if not rows:
        report.add("strategy_config", "WARN",
                   f"No STRATEGY_CONFIG# rows in {strategy_table}. Strategies fall "
                   "back to default config (enabled=True, paper_trade=True). "
                   "Confirm this is intended before a session.")
        return

    live_enabled, summary = [], []
    for it in rows:
        name = _attr_s(it, "PK").replace("STRATEGY_CONFIG#", "")
        sk   = _attr_s(it, "SK")
        enabled     = _attr_bool(it, "enabled", True)
        paper_trade = _attr_bool(it, "paper_trade", True)
        summary.append({"strategy": name, "sk": sk,
                        "enabled": enabled, "paper_trade": paper_trade})
        if enabled and not paper_trade:
            live_enabled.append(f"{name} ({sk})")

    if live_enabled:
        report.add("strategy_config", "FAIL",
                   f"{len(live_enabled)} ENABLED strategy(ies) have paper_trade=False "
                   f"→ would route LIVE: {', '.join(live_enabled)}. "
                   "For a paper-safe runtime every enabled strategy must be paper_trade=True.",
                   strategies=summary)
    else:
        report.add("strategy_config", "PASS",
                   f"All {len(rows)} strategy-config row(s) are paper-safe "
                   "(no enabled strategy has paper_trade=False).",
                   strategies=summary)


def check_kill_switch(report: Report, dynamo: _ReadOnlyDynamo,
                      risk_state_table: str) -> None:
    """
    Prefer the production read path (KillSwitch.load_state → get_status) so we
    observe exactly what the risk engine observes. Fall back to a direct,
    read-only get_item on the canonical key if the import is unavailable.
    """
    status: Optional[dict] = None
    try:
        import asyncio
        from risk_engine.killswitch.killswitch import KillSwitch  # type: ignore
        ks = KillSwitch(dynamo_client=dynamo, table_name=risk_state_table)
        asyncio.run(ks.load_state())          # read-only (get_item only)
        status = ks.get_status()
    except Exception:
        status = None

    if status is None:
        # Direct read-only fallback using the canonical key/fields.
        try:
            resp = dynamo.get_item(
                TableName=risk_state_table,
                Key={"PK": {"S": "KILLSWITCH"}, "SK": {"S": "GLOBAL"}},
            )
            item = resp.get("Item")
            if not item:
                report.add("kill_switch", "PASS",
                           "No kill-switch row — defaults to INACTIVE (trading allowed).")
                return
            active = _attr_bool(item, "active", False)
            status = {"active": active, "reason": _attr_s(item, "reason")}
        except Exception as exc:
            report.add("kill_switch", "UNKNOWN",
                       f"Could not read kill-switch from {risk_state_table}: {exc}")
            return

    if status.get("active"):
        report.add("kill_switch", "WARN",
                   f"Kill switch ACTIVE — ALL trading halted "
                   f"(reason: {status.get('reason') or 'unknown'}). "
                   "Clear with scripts/kill_switch_cli.py deactivate before a session.",
                   **status)
    else:
        report.add("kill_switch", "PASS", "Kill switch INACTIVE (trading allowed).",
                   **status)


def check_reconciliation(report: Report, dynamo: _ReadOnlyDynamo,
                         risk_state_table: str) -> None:
    try:
        resp = dynamo.get_item(
            TableName=risk_state_table,
            Key={"PK": {"S": "RECONCILIATION#STATE"}, "SK": {"S": "GLOBAL"}},
        )
    except Exception as exc:
        report.add("reconciliation", "UNKNOWN",
                   f"Could not read reconciliation flag from {risk_state_table}: {exc}")
        return
    item = resp.get("Item")
    if not item:
        report.add("reconciliation", "PASS",
                   "No reconciliation row — defaults to NOT required (entries allowed).")
        return
    required = _attr_bool(item, "required", False)
    if required:
        report.add("reconciliation", "WARN",
                   "reconciliation_required=True — risk engine HARD-HALTS new entries "
                   "(exits/risk-reduction still allowed). Resolve the mismatch first.",
                   required=True)
    else:
        report.add("reconciliation", "PASS",
                   "reconciliation_required=False (entries allowed).", required=False)


def check_mode_env(report: Report) -> None:
    """Mode + risk-stage env flags. These gate paper vs live behaviour."""
    live_enabled = _bool_env("QE_EXECUTION_LIVE_TRADING_ENABLED")
    risk_profile = _env("RISK_PROFILE", "").lower()
    universe     = _env("UNIVERSE_MODE", "")
    paper_exec   = _env("EXECUTION_PAPER_TRADING", "")   # default True in code if absent
    watchlist    = _env("STRATEGY_WATCHLIST_NSE", "")

    # live_trading_enabled — the single most dangerous flag for Stage-0/paper.
    if live_enabled:
        report.add("env:live_trading_enabled", "FAIL",
                   "QE_EXECUTION_LIVE_TRADING_ENABLED=true — live EXIT routing is armed. "
                   "Must be absent/false for paper sessions and Stage-0.")
    else:
        report.add("env:live_trading_enabled", "PASS",
                   "QE_EXECUTION_LIVE_TRADING_ENABLED is absent/false (live exits disarmed).")

    if risk_profile == "paper":
        report.add("env:risk_profile", "PASS", "RISK_PROFILE=paper")
    elif risk_profile in ("tiny-live", "medium-live", "shadow", ""):
        report.add("env:risk_profile",
                   "WARN" if risk_profile else "UNKNOWN",
                   f"RISK_PROFILE='{risk_profile or '<unset>'}' — expected 'paper' for a "
                   "paper-safe runtime. Confirm risk stage before a session.")
    else:
        report.add("env:risk_profile", "WARN",
                   f"RISK_PROFILE='{risk_profile}' — unrecognised stage.")

    if universe in ("PAPER_SAFE_START", "PAPER_EXPAND"):
        report.add("env:universe_mode", "PASS", f"UNIVERSE_MODE={universe}")
    elif universe == "LIVE_ADVANCED":
        report.add("env:universe_mode", "WARN",
                   "UNIVERSE_MODE=LIVE_ADVANCED — live universe selected; "
                   "ensure this is intended and gated.")
    else:
        report.add("env:universe_mode",
                   "WARN" if universe else "UNKNOWN",
                   f"UNIVERSE_MODE='{universe or '<unset>'}' — expected PAPER_SAFE_START/PAPER_EXPAND.")

    if paper_exec:
        report.add("env:execution_paper_trading",
                   "PASS" if paper_exec.lower() in ("true", "1", "yes") else "WARN",
                   f"EXECUTION_PAPER_TRADING={paper_exec}")
    else:
        report.add("env:execution_paper_trading", "UNKNOWN",
                   "EXECUTION_PAPER_TRADING unset — code default is True (paper sim).")

    report.add("env:watchlist", "PASS" if watchlist else "WARN",
               f"STRATEGY_WATCHLIST_NSE={'set ('+str(len(watchlist.split(',')))+' symbols)' if watchlist else 'UNSET — candle strategies emit no signals'}")


# ── Rendering ─────────────────────────────────────────────────────────────────────
_ICON = {"PASS": PASS_STR, "FAIL": FAIL_STR, "WARN": WARN_STR, "UNKNOWN": UNKNOWN_STR}


def _render_text(report: Report) -> None:
    print(f"\n{_BOLD}QuantEmbrace — Read-Only Live-Readiness Runtime Check{_RESET}")
    print(f"{'='*68}")
    print(f"  UTC now : {_utc_now().isoformat()}")
    print(f"  Mode    : READ-ONLY (no writes, no live-enable, no orders)\n")
    for c in report.checks:
        msg = f"  {c.message}" if c.message else ""
        print(f"  {_ICON.get(c.status, c.status)}  {c.name}{msg}")
    print(f"\n{'='*68}")
    verdict = report.verdict()
    counts = (f"{len(report.failed)} FAIL · {len(report.warned)} WARN · "
              f"{len(report.unknown)} UNKNOWN")
    if verdict == PAPER_SAFE:
        print(f"  {_GREEN}{_BOLD}VERDICT: {verdict}{_RESET}  ({counts})")
        print(f"  {_YELLOW}NOTE: paper-safe runtime confirmed — this is NOT a live-GO "
              f"authorization.{_RESET}")
    elif verdict == UNSAFE:
        print(f"  {_RED}{_BOLD}VERDICT: {verdict}{_RESET}  ({counts})")
        print(f"  {_RED}Resolve every FAIL before any session.{_RESET}")
    else:
        print(f"  {_BLUE}{_BOLD}VERDICT: {verdict}{_RESET}  ({counts})")
        print(f"  {_BLUE}Runtime state could NOT be verified — DO NOT mark GO. "
              f"Re-run on the trading host with AWS creds + region.{_RESET}")
        if report.failed:
            names = ", ".join(c.name for c in report.failed)
            print(f"  {_RED}Also: {len(report.failed)} definitive FAIL "
                  f"already detected and must be fixed regardless: {names}.{_RESET}")
    print()


def _render_json(report: Report) -> None:
    print(json.dumps({
        "utc_now": _utc_now().isoformat(),
        "mode": "READ_ONLY",
        "runtime_reachable": report.runtime_reachable,
        "verdict": report.verdict(),
        "summary": {"fail": len(report.failed), "warn": len(report.warned),
                    "unknown": len(report.unknown), "total": len(report.checks)},
        "checks": [{"name": c.name, "status": c.status,
                    "message": c.message, "detail": c.detail}
                   for c in report.checks],
    }, indent=2, default=str))


# ── Main ──────────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Read-only live-readiness runtime check (Phase 2 / Q4).")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    ap.add_argument("--env", default=None,
                    help="Override environment token for table-name resolution (informational).")
    args = ap.parse_args()

    report = Report()
    settings = _resolve_settings()
    tables   = _resolve_table_names(settings)
    dynamo   = _make_readonly_client()

    if dynamo is None and not _env("AWS_REGION") and not _env("AWS_DEFAULT_REGION") \
            and not _env("AWS_ENDPOINT_URL"):
        # boto3 missing AND no AWS context at all — pure usage error.
        report.add("connectivity", "UNKNOWN",
                   "boto3 not installed and no AWS context. Cannot verify runtime.")
        check_mode_env(report)            # env checks still meaningful
        (_render_json if args.json else _render_text)(report)
        sys.exit(_EXIT[VERIFICATION_REQ])

    reachable = check_connectivity(report, dynamo)
    check_table_names(report, tables)

    if reachable and dynamo is not None:
        check_zerodha_token(report, dynamo, tables["sessions"])
        check_strategy_config(report, dynamo, tables["strategy_config"])
        check_kill_switch(report, dynamo, tables["risk_state"])
        check_reconciliation(report, dynamo, tables["risk_state"])
    else:
        for n in ("zerodha_token", "strategy_config", "kill_switch", "reconciliation"):
            report.add(n, "UNKNOWN", "Skipped — DynamoDB unreachable.")

    # Env-mode checks never need DynamoDB.
    check_mode_env(report)

    (_render_json if args.json else _render_text)(report)
    sys.exit(_EXIT[report.verdict()])


if __name__ == "__main__":
    main()
