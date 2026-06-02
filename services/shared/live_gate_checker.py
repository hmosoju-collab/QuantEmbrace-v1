"""
LiveGateChecker — production live-trading promotion gate.

Returns one of three verdicts:

    APPROVED  — all 26 checks pass; live trading may be enabled by the operator.
    BLOCKED   — one or more checks failed; live trading MUST NOT be enabled.
    DEGRADED  — all checks pass or warned; some runtime checks could not be
                verified (e.g. no CloudWatch client). Live trading is NOT
                recommended until warnings are resolved.

Design principles
─────────────────
- Read-only: never writes to DynamoDB, Kafka, or any broker API.
- No live-trading side effects: the checker itself does not enable trading.
- All 26 checks run and are reported individually — no early exit on first FAIL.
- Checks 11/16/18/20 degrade gracefully (WARN) when optional clients are absent.
- Time-based checks (21/22) accept an injectable clock for deterministic tests.

Approval record (DynamoDB, risk-state table)
────────────────────────────────────────────
    PK : "LIVE_GATE#APPROVAL"
    SK : "CURRENT"
    Fields:
        approval_token        str   — non-empty = record exists and is active
        live_stage            str   — must be STAGE_1_ONE_SHARE (or approved stage)
        max_capital           N     — must be ≤ stage portfolio limit
        approved_by           str   — operator identity (name or email)
        approved_at           str   — ISO timestamp of approval
        release_tag           str   — git SHA / semver of deployment being approved
        rollback_plan_confirmed  BOOL — True = operator confirmed rollback exists
        sns_alert_tested      BOOL  — True = SNS test delivery confirmed

    Write this record via the operator tool (scripts/ops/approve_live_gate.py)
    before running the checker. Never included in automated CI.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_IST = timezone(timedelta(hours=5, minutes=30))

# NSE trading window
_NSE_OPEN = time(9, 15, 0)
_NSE_CLOSE = time(15, 30, 0)
_NO_NEW_ENTRY = time(15, 0, 0)   # no new position entries after 15:00 IST

# Stage-1 hard limits — immutable; operator cannot override via config
STAGE_1_ONE_SHARE = "STAGE_1_ONE_SHARE"
APPROVED_STAGES: frozenset[str] = frozenset({STAGE_1_ONE_SHARE})

STAGE_1_MAX_PORTFOLIO_VALUE: float = 1_000_000.0   # ₹10L
STAGE_1_MAX_ORDER_VALUE: float     = 5_000.0        # ₹5k
STAGE_1_MAX_CONCURRENT_POSITIONS: int = 1

# LTP freshness threshold
_LTP_MAX_AGE_SECONDS: float = 120.0  # 2 minutes

# Strategies that are approved for paper trading only and must never appear in
# a live strategy-config entry.  Check 26 enforces this at gate time.
# See: docs/live-readiness/stage1-strategy-eligibility.md
PAPER_ONLY_STRATEGIES: frozenset[str] = frozenset({"scalp_1m"})

# DynamoDB key constants
_APPROVAL_PK = "LIVE_GATE#APPROVAL"
_APPROVAL_SK = "CURRENT"
_KS_PK       = "KILLSWITCH"
_KS_SK       = "GLOBAL"
_RECON_PK    = "RECONCILIATION#STATE"
_RECON_SK    = "GLOBAL"


# ── Result types ───────────────────────────────────────────────────────────────

CheckStatus = Literal["PASS", "FAIL", "WARN", "SKIP"]
GateStatus  = Literal["APPROVED", "BLOCKED", "DEGRADED"]


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    reason: str

    def __repr__(self) -> str:
        return f"CheckResult({self.name!r}, {self.status}, {self.reason!r})"


@dataclass
class LiveGateResult:
    status: GateStatus
    checks: list[CheckResult] = field(default_factory=list)
    summary: str = ""

    @property
    def blocked_by(self) -> list[str]:
        return [c.name for c in self.checks if c.status == "FAIL"]

    @property
    def warnings(self) -> list[str]:
        return [c.name for c in self.checks if c.status == "WARN"]

    @property
    def passed(self) -> list[str]:
        return [c.name for c in self.checks if c.status == "PASS"]

    def __repr__(self) -> str:
        return (
            f"LiveGateResult(status={self.status!r}, "
            f"blocked_by={self.blocked_by}, warnings={self.warnings})"
        )


# ── Checker ────────────────────────────────────────────────────────────────────

class LiveGateChecker:
    """
    Evaluates all 25 live-trading promotion gates and returns a single verdict.

    Parameters
    ----------
    settings : AppSettings
        Full application settings (risk profile, portfolio value, watchlist, …).
    dynamo_client : boto3 DynamoDB client
        Low-level `boto3.client("dynamodb")` for risk-state, strategy-config,
        sessions, orders, and latest-prices tables.
    risk_state_table : str
        Fully-qualified DynamoDB table name for risk state (e.g.
        "quantembrace-prod-risk-state").
    orders_table : str
        Fully-qualified DynamoDB table name for orders.
    strategy_config_table : str
        Fully-qualified DynamoDB table name for strategy config.
    sessions_table : str
        Fully-qualified DynamoDB table name for Zerodha sessions.
    prices_table : str
        Fully-qualified DynamoDB table name for latest prices (LTP source).
    cw_client : optional boto3 CloudWatch client
        When provided, checks 18 and 20 query alarm state. When absent those
        checks degrade to WARN ("RUNTIME_VERIFICATION_REQUIRED").
    zerodha : optional ZerodhaBrokerClient
        When provided, check 16 (margins readable) calls get_margins(). When
        absent, check 16 degrades to WARN.
    _now_ist : optional callable → datetime
        Injectable clock for testing. Defaults to `datetime.now(_IST)`.
    """

    def __init__(
        self,
        settings: Any,
        dynamo_client: Any,
        risk_state_table: str,
        orders_table: str,
        strategy_config_table: str,
        sessions_table: str,
        prices_table: str,
        cw_client: Optional[Any] = None,
        zerodha: Optional[Any] = None,
        _now_ist: Optional[Any] = None,
    ) -> None:
        self._settings = settings
        self._dynamo = dynamo_client
        self._risk_state_table = risk_state_table
        self._orders_table = orders_table
        self._strategy_config_table = strategy_config_table
        self._sessions_table = sessions_table
        self._prices_table = prices_table
        self._cw = cw_client
        self._zerodha = zerodha
        self._now_ist = _now_ist or (lambda: datetime.now(_IST))

    # ── Public API ─────────────────────────────────────────────────────────────

    async def check_all(self) -> LiveGateResult:
        """
        Run all 25 gates concurrently and return a LiveGateResult.

        Never raises — all exceptions are caught inside individual check
        methods and surfaced as FAIL with the exception message as reason.
        """
        approval_record = await self._fetch_approval_record()

        check_fns = [
            self._check_trading_mode_live(approval_record),
            self._check_live_trading_enabled(),
            self._check_manual_approval_token(approval_record),
            self._check_live_stage_approved(approval_record),
            self._check_max_capital_within_stage_limit(approval_record),
            self._check_max_order_value_configured(),
            self._check_max_daily_loss_configured(),
            self._check_allowed_symbols_configured(),
            self._check_allowed_strategies_configured(),
            self._check_kill_switch_off(),
            self._check_trade_exit_engine_running(),
            self._check_mis_square_off_armed(),
            self._check_reconciliation_clean(),
            self._check_ltp_fresh(),
            self._check_broker_session_valid(),
            self._check_margins_readable(),
            self._check_dynamodb_live_table_reachable(),
            self._check_cloudwatch_alarms_active(),
            self._check_sns_alert_tested(approval_record),
            self._check_no_unresolved_critical_alerts(),
            self._check_trading_window(),
            self._check_no_new_entry_cutoff(),
            self._check_rollback_plan_exists(approval_record),
            self._check_release_tag_recorded(approval_record),
            self._check_human_approval_recorded(approval_record),
            self._check_no_paper_only_strategy_in_live(),
        ]

        results: list[CheckResult] = list(
            await asyncio.gather(*check_fns, return_exceptions=True)
        )

        # Any gather exception means the check itself crashed — treat as FAIL
        safe: list[CheckResult] = []
        for i, r in enumerate(results):
            if isinstance(r, BaseException):
                safe.append(CheckResult(
                    name=f"check_{i+1}_internal_error",
                    status="FAIL",
                    reason=f"Check raised exception: {r!r}",
                ))
            else:
                safe.append(r)  # type: ignore[arg-type]

        fails  = [c for c in safe if c.status == "FAIL"]
        warns  = [c for c in safe if c.status == "WARN"]

        if fails:
            gate_status: GateStatus = "BLOCKED"
            summary = f"BLOCKED — {len(fails)} check(s) failed: {[c.name for c in fails]}"
        elif warns:
            gate_status = "DEGRADED"
            summary = (
                f"DEGRADED — all hard checks pass but "
                f"{len(warns)} runtime check(s) could not be verified: "
                f"{[c.name for c in warns]}"
            )
        else:
            gate_status = "APPROVED"
            summary = "APPROVED — all 26 gates passed"

        logger.info(
            "live_gate_checker.result status=%s fails=%d warns=%d",
            gate_status, len(fails), len(warns),
        )
        return LiveGateResult(status=gate_status, checks=safe, summary=summary)

    # ── Approval record ────────────────────────────────────────────────────────

    async def _fetch_approval_record(self) -> dict:
        """Read the live-gate approval record from DynamoDB. Returns {} on miss."""
        try:
            resp = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._risk_state_table,
                Key={
                    "PK": {"S": _APPROVAL_PK},
                    "SK": {"S": _APPROVAL_SK},
                },
                ConsistentRead=True,
            )
            return resp.get("Item", {})
        except Exception as exc:
            logger.warning("live_gate_checker.approval_record_fetch_failed error=%r", exc)
            return {}

    # ── Check 1: trading mode == LIVE ─────────────────────────────────────────

    async def _check_trading_mode_live(self, rec: dict) -> CheckResult:
        name = "trading_mode_live"
        env = getattr(self._settings, "environment", None)
        # Accept either production environment setting or an explicit
        # live_stage in the approval record (checked in check 4 as well;
        # this check gates on *intent*, not configuration consistency).
        from_env = str(getattr(env, "value", env or "")).lower() == "production"
        from_rec = bool(rec.get("live_stage", {}).get("S", ""))
        if from_env or from_rec:
            return CheckResult(name, "PASS", "Environment=production or live_stage set in approval record")
        return CheckResult(
            name, "FAIL",
            "QE_ENVIRONMENT is not 'production' and no live_stage in approval record. "
            "Set QE_ENVIRONMENT=production in EC2 env file and write an approval record.",
        )

    # ── Check 2: live_trading_enabled == true ──────────────────────────────────

    async def _check_live_trading_enabled(self) -> CheckResult:
        name = "live_trading_enabled"
        raw = os.environ.get("QE_EXECUTION_LIVE_TRADING_ENABLED", "").strip().lower()
        # Also check settings.execution.live_trading_enabled if it exists
        from_settings = bool(
            getattr(getattr(self._settings, "execution", None), "live_trading_enabled", False)
        )
        enabled = raw in ("true", "1") or from_settings
        if enabled:
            return CheckResult(name, "PASS", "QE_EXECUTION_LIVE_TRADING_ENABLED=true")
        return CheckResult(
            name, "FAIL",
            "QE_EXECUTION_LIVE_TRADING_ENABLED is absent or false. "
            "Uncomment it in execution_engine.sh userdata after full gate sign-off.",
        )

    # ── Check 3: manual approval token present ────────────────────────────────

    async def _check_manual_approval_token(self, rec: dict) -> CheckResult:
        name = "manual_approval_token"
        token = rec.get("approval_token", {}).get("S", "").strip()
        if token:
            return CheckResult(name, "PASS", "Approval token present in live-gate record")
        return CheckResult(
            name, "FAIL",
            "No approval_token in LIVE_GATE#APPROVAL/CURRENT DynamoDB record. "
            "Run scripts/ops/approve_live_gate.py to create the record.",
        )

    # ── Check 4: live stage == STAGE_1_ONE_SHARE or approved stage ────────────

    async def _check_live_stage_approved(self, rec: dict) -> CheckResult:
        name = "live_stage_approved"
        stage = rec.get("live_stage", {}).get("S", "").strip()
        if stage in APPROVED_STAGES:
            return CheckResult(name, "PASS", f"live_stage={stage!r} is an approved stage")
        if stage:
            return CheckResult(
                name, "FAIL",
                f"live_stage={stage!r} is not in approved stages {sorted(APPROVED_STAGES)}. "
                "Only STAGE_1_ONE_SHARE is currently approved.",
            )
        return CheckResult(
            name, "FAIL",
            "live_stage field absent from approval record. "
            "Set live_stage=STAGE_1_ONE_SHARE in the approval record.",
        )

    # ── Check 5: max capital within stage limit ────────────────────────────────

    async def _check_max_capital_within_stage_limit(self, rec: dict) -> CheckResult:
        name = "max_capital_within_stage_limit"
        # Check approval record max_capital first, then settings.portfolio_value
        rec_capital_raw = rec.get("max_capital", {})
        portfolio_value = float(getattr(self._settings, "portfolio_value", 0) or 0)

        if rec_capital_raw:
            try:
                rec_capital = float(str(rec_capital_raw.get("N", rec_capital_raw)))
            except (ValueError, TypeError):
                rec_capital = 0.0
            if rec_capital > STAGE_1_MAX_PORTFOLIO_VALUE:
                return CheckResult(
                    name, "FAIL",
                    f"Approval record max_capital={rec_capital:.0f} exceeds "
                    f"Stage-1 limit of ₹{STAGE_1_MAX_PORTFOLIO_VALUE:.0f}. "
                    "₹1,000,000 is BLOCKED for Stage-1.",
                )
        # Also check configured portfolio_value against the stage limit
        if portfolio_value > STAGE_1_MAX_PORTFOLIO_VALUE:
            return CheckResult(
                name, "FAIL",
                f"settings.portfolio_value={portfolio_value:.0f} exceeds "
                f"Stage-1 limit of ₹{STAGE_1_MAX_PORTFOLIO_VALUE:.0f}. "
                "Reduce portfolio_value before enabling Stage-1 live trading.",
            )
        if portfolio_value <= 0:
            return CheckResult(
                name, "FAIL",
                "portfolio_value is 0 or not set. Configure a positive portfolio_value.",
            )
        return CheckResult(
            name, "PASS",
            f"portfolio_value={portfolio_value:.0f} ≤ Stage-1 limit "
            f"₹{STAGE_1_MAX_PORTFOLIO_VALUE:.0f}",
        )

    # ── Check 6: max order value configured ──────────────────────────────────

    async def _check_max_order_value_configured(self) -> CheckResult:
        name = "max_order_value_configured"
        val = float(
            getattr(getattr(self._settings, "risk", None), "max_single_order_value", 0) or 0
        )
        if val <= 0:
            return CheckResult(
                name, "FAIL",
                "RISK_MAX_SINGLE_ORDER_VALUE is 0 or unset. Configure a positive value.",
            )
        if val > STAGE_1_MAX_ORDER_VALUE:
            return CheckResult(
                name, "FAIL",
                f"max_single_order_value={val:.0f} exceeds Stage-1 cap of "
                f"₹{STAGE_1_MAX_ORDER_VALUE:.0f}. Reduce to ≤ ₹5,000.",
            )
        return CheckResult(
            name, "PASS",
            f"max_single_order_value=₹{val:.0f} ≤ Stage-1 cap ₹{STAGE_1_MAX_ORDER_VALUE:.0f}",
        )

    # ── Check 7: max daily loss configured ───────────────────────────────────

    async def _check_max_daily_loss_configured(self) -> CheckResult:
        name = "max_daily_loss_configured"
        pct = float(
            getattr(getattr(self._settings, "risk", None), "max_daily_loss_pct", 0) or 0
        )
        if pct <= 0:
            return CheckResult(
                name, "FAIL",
                "RISK_MAX_DAILY_LOSS_PCT is 0 or unset. Configure a positive daily loss limit.",
            )
        return CheckResult(
            name, "PASS",
            f"max_daily_loss_pct={pct:.2f}% is configured",
        )

    # ── Check 8: allowed symbols configured ──────────────────────────────────

    async def _check_allowed_symbols_configured(self) -> CheckResult:
        name = "allowed_symbols_configured"
        watchlist = list(
            getattr(getattr(self._settings, "strategy", None), "watchlist_nse", []) or []
        )
        if not watchlist:
            return CheckResult(
                name, "FAIL",
                "STRATEGY_WATCHLIST_NSE is empty. Set comma-separated NSE symbols "
                "in the EC2 execution/strategy env file via strategy_watchlist_nse "
                "Terraform variable.",
            )
        return CheckResult(
            name, "PASS",
            f"{len(watchlist)} symbol(s) configured in watchlist_nse: "
            f"{watchlist[:3]}{'…' if len(watchlist) > 3 else ''}",
        )

    # ── Check 9: allowed strategies configured ────────────────────────────────

    async def _check_allowed_strategies_configured(self) -> CheckResult:
        name = "allowed_strategies_configured"
        try:
            resp = await asyncio.to_thread(
                self._dynamo.scan,
                TableName=self._strategy_config_table,
                FilterExpression="paper_trade = :false",
                ExpressionAttributeValues={":false": {"BOOL": False}},
                ProjectionExpression="PK, SK, enabled",
            )
            live_strategies = [
                item for item in resp.get("Items", [])
                if item.get("enabled", {}).get("BOOL", False)
            ]
            if live_strategies:
                names = [
                    item.get("PK", {}).get("S", "?").removeprefix("STRATEGY_CONFIG#")
                    for item in live_strategies
                ]
                return CheckResult(
                    name, "PASS",
                    f"{len(live_strategies)} live strategy(ies) configured: {names}",
                )
            return CheckResult(
                name, "FAIL",
                "No strategies have paper_trade=false and enabled=true in DynamoDB "
                "strategy-config table. Use scripts/strategy/config.py to promote "
                "a strategy to live.",
            )
        except Exception as exc:
            return CheckResult(name, "FAIL", f"Failed to read strategy-config table: {exc!r}")

    # ── Check 26: no paper-only strategy in live config ───────────────────────

    async def _check_no_paper_only_strategy_in_live(self) -> CheckResult:
        """Block Stage-1 if any PAPER_ONLY_STRATEGIES has paper_trade=false in DynamoDB.

        scalp_1m is approved for paper only (APPROVED_FOR_PAPER_ONLY, 2026-06-02).
        Letting it reach a live broker path would bypass the net-edge viability
        filter that was the sole reason for the paper-only approval.
        """
        name = "no_paper_only_strategy_in_live"
        try:
            resp = await asyncio.to_thread(
                self._dynamo.scan,
                TableName=self._strategy_config_table,
                FilterExpression="paper_trade = :false AND attribute_exists(PK)",
                ExpressionAttributeValues={":false": {"BOOL": False}},
                ProjectionExpression="PK, paper_trade, enabled",
            )
            offending: list[str] = []
            for item in resp.get("Items", []):
                raw_name = item.get("PK", {}).get("S", "")
                strategy_name = raw_name.removeprefix("STRATEGY_CONFIG#")
                if strategy_name in PAPER_ONLY_STRATEGIES:
                    offending.append(strategy_name)
            if offending:
                return CheckResult(
                    name, "FAIL",
                    f"Strategy(ies) {offending} are APPROVED_FOR_PAPER_ONLY but have "
                    "paper_trade=false in strategy-config. Remove them from the live "
                    "config before enabling live trading. "
                    "See docs/live-readiness/stage1-strategy-eligibility.md",
                )
            return CheckResult(
                name, "PASS",
                f"No paper-only strategies found in live config. "
                f"(Paper-only list: {sorted(PAPER_ONLY_STRATEGIES)})",
            )
        except Exception as exc:
            return CheckResult(name, "FAIL", f"Failed to scan strategy-config table: {exc!r}")

    # ── Check 10: kill switch OFF ─────────────────────────────────────────────

    async def _check_kill_switch_off(self) -> CheckResult:
        name = "kill_switch_off"
        try:
            resp = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._risk_state_table,
                Key={"PK": {"S": _KS_PK}, "SK": {"S": _KS_SK}},
                ConsistentRead=True,
            )
            item = resp.get("Item", {})
            if not item:
                return CheckResult(
                    name, "PASS",
                    "Kill switch record absent → interpreted as INACTIVE (safe default)",
                )
            active = item.get("active", {}).get("BOOL", False)
            if active:
                reason = item.get("reason", {}).get("S", "unknown reason")
                activated_by = item.get("activated_by", {}).get("S", "unknown")
                return CheckResult(
                    name, "FAIL",
                    f"Kill switch is ACTIVE (activated_by={activated_by!r}, reason={reason!r}). "
                    "Deactivate with: python scripts/kill_switch_cli.py deactivate",
                )
            return CheckResult(name, "PASS", "Kill switch is INACTIVE")
        except Exception as exc:
            return CheckResult(name, "FAIL", f"Failed to read kill switch state: {exc!r}")

    # ── Check 11: TradeExitEngine running ─────────────────────────────────────

    async def _check_trade_exit_engine_running(self) -> CheckResult:
        name = "trade_exit_engine_running"
        # TEE writes a heartbeat to prices table: PK=HEARTBEAT#TEE, SK=CURRENT
        try:
            resp = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._prices_table,
                Key={"PK": {"S": "HEARTBEAT#TEE"}, "SK": {"S": "CURRENT"}},
            )
            item = resp.get("Item", {})
            if not item:
                # No heartbeat key — TEE may not write one (depends on implementation)
                return CheckResult(
                    name, "WARN",
                    "No TEE heartbeat found at HEARTBEAT#TEE/CURRENT. "
                    "RUNTIME_VERIFICATION_REQUIRED — confirm TradeExitEngine is "
                    "running in execution_engine logs before enabling live trading.",
                )
            updated_at_str = item.get("updated_at", {}).get("S", "")
            if updated_at_str:
                try:
                    updated_at = datetime.fromisoformat(updated_at_str)
                    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
                    if age > _LTP_MAX_AGE_SECONDS:
                        return CheckResult(
                            name, "FAIL",
                            f"TEE heartbeat is stale: {age:.0f}s ago "
                            f"(threshold {_LTP_MAX_AGE_SECONDS:.0f}s). "
                            "Verify TradeExitEngine is running in execution_engine.",
                        )
                    return CheckResult(
                        name, "PASS",
                        f"TEE heartbeat fresh: {age:.0f}s ago",
                    )
                except ValueError:
                    pass
            return CheckResult(
                name, "WARN",
                "TEE heartbeat record found but updated_at is unreadable. "
                "RUNTIME_VERIFICATION_REQUIRED.",
            )
        except Exception as exc:
            return CheckResult(
                name, "WARN",
                f"Could not read TEE heartbeat: {exc!r}. RUNTIME_VERIFICATION_REQUIRED.",
            )

    # ── Check 12: MIS Square-Off Manager armed ────────────────────────────────

    async def _check_mis_square_off_armed(self) -> CheckResult:
        name = "mis_square_off_armed"
        now = self._now_ist()
        now_time = now.time()

        # If we're after the no-new-entry cutoff, MIS has already fired or
        # should be past its daily window. Check is not applicable after market.
        if now_time >= _NSE_CLOSE:
            return CheckResult(
                name, "PASS",
                "After NSE close — MIS square-off window has passed for today. "
                "Arm will be re-evaluated at next market open.",
            )

        # During market hours (before 15:00): verify service is running by
        # checking execution_engine heartbeat (the service that hosts MIS)
        try:
            resp = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._prices_table,
                Key={"PK": {"S": "HEARTBEAT#EXECUTION_ENGINE"}, "SK": {"S": "CURRENT"}},
            )
            item = resp.get("Item", {})
            if not item:
                return CheckResult(
                    name, "WARN",
                    "No execution_engine heartbeat found. "
                    "RUNTIME_VERIFICATION_REQUIRED — confirm MISSquareOffManager is "
                    "running inside execution_engine before enabling live trading.",
                )
            updated_at_str = item.get("updated_at", {}).get("S", "")
            if updated_at_str:
                try:
                    updated_at = datetime.fromisoformat(updated_at_str)
                    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
                    if age > _LTP_MAX_AGE_SECONDS:
                        return CheckResult(
                            name, "FAIL",
                            f"execution_engine heartbeat stale by {age:.0f}s — "
                            "MIS square-off manager may not be running.",
                        )
                    return CheckResult(
                        name, "PASS",
                        f"execution_engine heartbeat fresh ({age:.0f}s). "
                        "MIS square-off manager is expected to be armed.",
                    )
                except ValueError:
                    pass
            return CheckResult(
                name, "WARN",
                "execution_engine heartbeat found but timestamp unreadable. "
                "RUNTIME_VERIFICATION_REQUIRED.",
            )
        except Exception as exc:
            return CheckResult(
                name, "WARN",
                f"Could not verify MIS armed state: {exc!r}. RUNTIME_VERIFICATION_REQUIRED.",
            )

    # ── Check 13: reconciliation clean ────────────────────────────────────────

    async def _check_reconciliation_clean(self) -> CheckResult:
        name = "reconciliation_clean"
        try:
            resp = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._risk_state_table,
                Key={"PK": {"S": _RECON_PK}, "SK": {"S": _RECON_SK}},
                ConsistentRead=True,
            )
            item = resp.get("Item", {})
            if not item:
                return CheckResult(
                    name, "PASS",
                    "Reconciliation state record absent → CLEAR (no halt flag set)",
                )
            required = item.get("reconciliation_required", {}).get("BOOL", False)
            if required:
                return CheckResult(
                    name, "FAIL",
                    "reconciliation_required=True in DynamoDB risk-state. "
                    "Run: python scripts/ops/reconcile.py --environment prod --fix "
                    "and confirm all positions are clean before going live.",
                )
            return CheckResult(name, "PASS", "Reconciliation state is CLEAR")
        except Exception as exc:
            return CheckResult(name, "FAIL", f"Failed to read reconciliation state: {exc!r}")

    # ── Check 14: LTP fresh ───────────────────────────────────────────────────

    async def _check_ltp_fresh(self) -> CheckResult:
        name = "ltp_fresh"
        watchlist = list(
            getattr(getattr(self._settings, "strategy", None), "watchlist_nse", []) or []
        )
        if not watchlist:
            return CheckResult(
                name, "FAIL",
                "Cannot verify LTP freshness — watchlist_nse is empty (check 8 also fails).",
            )

        # Check the first symbol in the watchlist as representative
        symbol = watchlist[0]
        pk = f"QUOTE#NSE#{symbol}"
        try:
            resp = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._prices_table,
                Key={"PK": {"S": pk}, "SK": {"S": "LATEST"}},
            )
            item = resp.get("Item", {})
            if not item:
                return CheckResult(
                    name, "FAIL",
                    f"No LTP record for {symbol} (PK={pk!r}) in latest-prices table. "
                    "LiveQuotePoller may not be running or symbol not in watchlist.",
                )
            captured_at = (
                item.get("captured_at_utc", {}).get("S", "")
                or item.get("updated_at", {}).get("S", "")
            )
            if captured_at:
                try:
                    ts = datetime.fromisoformat(captured_at)
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    if age > _LTP_MAX_AGE_SECONDS:
                        return CheckResult(
                            name, "FAIL",
                            f"LTP for {symbol} is stale: {age:.0f}s old "
                            f"(threshold {_LTP_MAX_AGE_SECONDS:.0f}s). "
                            "LiveQuotePoller may be down or symbol is not actively quoted.",
                        )
                    return CheckResult(
                        name, "PASS",
                        f"LTP for {symbol} is fresh: {age:.0f}s old",
                    )
                except ValueError:
                    pass
            return CheckResult(
                name, "WARN",
                f"LTP record for {symbol} found but timestamp unreadable. "
                "RUNTIME_VERIFICATION_REQUIRED.",
            )
        except Exception as exc:
            return CheckResult(name, "FAIL", f"Failed to read LTP for {symbol}: {exc!r}")

    # ── Check 15: broker session valid ────────────────────────────────────────

    async def _check_broker_session_valid(self) -> CheckResult:
        name = "broker_session_valid"
        today_ist = self._now_ist().date().isoformat()
        pk = f"SESSION#{today_ist}"
        try:
            resp = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._sessions_table,
                Key={"PK": {"S": pk}, "SK": {"S": "ZERODHA"}},
                ConsistentRead=True,
            )
            item = resp.get("Item", {})
            if not item:
                return CheckResult(
                    name, "FAIL",
                    f"No Zerodha session record for today ({today_ist}) in sessions table. "
                    "Run: python scripts/zerodha_login.py  before market open (08:30 IST).",
                )
            expires_at = (
                item.get("expires_at", {}).get("S", "")
                or item.get("expires_at_epoch", {}).get("N", "")
            )
            # Presence of the record for today is the primary signal; expiry is advisory
            access_token = item.get("access_token", {}).get("S", "")
            if not access_token:
                return CheckResult(
                    name, "FAIL",
                    "Session record exists for today but access_token field is empty. "
                    "Re-run: python scripts/zerodha_login.py",
                )
            return CheckResult(
                name, "PASS",
                f"Zerodha session found for {today_ist}. "
                f"Token present (expires_at={expires_at or 'not recorded'})",
            )
        except Exception as exc:
            return CheckResult(name, "FAIL", f"Failed to read sessions table: {exc!r}")

    # ── Check 16: margins readable ────────────────────────────────────────────

    async def _check_margins_readable(self) -> CheckResult:
        name = "margins_readable"
        if self._zerodha is None:
            return CheckResult(
                name, "WARN",
                "No Zerodha client injected — RUNTIME_VERIFICATION_REQUIRED. "
                "Confirm margin data is accessible before enabling live trading: "
                "python scripts/zerodha/position_audit.py --check-margins",
            )
        try:
            margins = await self._zerodha.get_margins()
            cash = float(margins.get("available_cash", 0) or 0)
            return CheckResult(
                name, "PASS",
                f"Margins readable. available_cash=₹{cash:.2f}",
            )
        except Exception as exc:
            return CheckResult(
                name, "FAIL",
                f"get_margins() raised {exc!r}. "
                "Broker API is unreachable or session is expired.",
            )

    # ── Check 17: DynamoDB live table reachable ───────────────────────────────

    async def _check_dynamodb_live_table_reachable(self) -> CheckResult:
        name = "dynamodb_live_table_reachable"
        try:
            # Use a get_item on a non-existent key — just verifies table is accessible
            await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._orders_table,
                Key={"PK": {"S": "HEALTH_CHECK"}, "SK": {"S": "PROBE"}},
                ConsistentRead=True,
            )
            return CheckResult(
                name, "PASS",
                f"Live orders table {self._orders_table!r} is reachable",
            )
        except Exception as exc:
            return CheckResult(
                name, "FAIL",
                f"Orders table {self._orders_table!r} is unreachable: {exc!r}. "
                "Verify DynamoDB endpoint and IAM permissions.",
            )

    # ── Check 18: CloudWatch alarms active ───────────────────────────────────

    async def _check_cloudwatch_alarms_active(self) -> CheckResult:
        name = "cloudwatch_alarms_active"
        if self._cw is None:
            return CheckResult(
                name, "WARN",
                "No CloudWatch client injected — RUNTIME_VERIFICATION_REQUIRED. "
                "Verify all production alarms are in OK or ALARM (not INSUFFICIENT_DATA) "
                "state in the AWS console before enabling live trading.",
            )
        try:
            resp = await asyncio.to_thread(
                self._cw.describe_alarms,
                StateValue="INSUFFICIENT_DATA",
                AlarmTypes=["MetricAlarm"],
            )
            insufficient = resp.get("MetricAlarms", [])
            trading_alarms_insufficient = [
                a["AlarmName"]
                for a in insufficient
                if "quantembrace" in a.get("AlarmName", "").lower()
                and "ecs" not in a.get("AlarmName", "").lower()  # ECS alarms intentionally empty
            ]
            if trading_alarms_insufficient:
                return CheckResult(
                    name, "FAIL",
                    f"{len(trading_alarms_insufficient)} trading alarm(s) in "
                    f"INSUFFICIENT_DATA state: {trading_alarms_insufficient[:5]}. "
                    "Alarms must receive metric data before going live.",
                )
            return CheckResult(
                name, "PASS",
                "All QuantEmbrace trading alarms have received metric data (not INSUFFICIENT_DATA)",
            )
        except Exception as exc:
            return CheckResult(
                name, "WARN",
                f"CloudWatch describe_alarms failed: {exc!r}. RUNTIME_VERIFICATION_REQUIRED.",
            )

    # ── Check 19: SNS alert tested ────────────────────────────────────────────

    async def _check_sns_alert_tested(self, rec: dict) -> CheckResult:
        name = "sns_alert_tested"
        tested = rec.get("sns_alert_tested", {}).get("BOOL", False)
        if tested:
            return CheckResult(
                name, "PASS",
                "sns_alert_tested=True in approval record",
            )
        return CheckResult(
            name, "FAIL",
            "sns_alert_tested is not True in approval record. "
            "Run: aws sns publish --topic-arn <alerts-arn> --message 'Stage-1 test' "
            "then set sns_alert_tested=true in the approval record.",
        )

    # ── Check 20: no unresolved critical alerts ───────────────────────────────

    async def _check_no_unresolved_critical_alerts(self) -> CheckResult:
        name = "no_unresolved_critical_alerts"
        if self._cw is None:
            return CheckResult(
                name, "WARN",
                "No CloudWatch client — RUNTIME_VERIFICATION_REQUIRED. "
                "Manually verify no P0 alarms are in ALARM state in the AWS console.",
            )
        # P0 alarms are those that route to the kill-switch SNS topic
        p0_names = [
            "*kill-switch-activated*",
            "*daily-pnl-loss-halt*",
            "*websocket-disconnected*",
            "*data-feed-stale*",
            "*risk-engine-unhealthy*",
            "*zerodha-rate-limit-errors*",
        ]
        try:
            resp = await asyncio.to_thread(
                self._cw.describe_alarms,
                StateValue="ALARM",
                AlarmTypes=["MetricAlarm"],
            )
            active = resp.get("MetricAlarms", [])
            p0_active = [
                a["AlarmName"]
                for a in active
                if any(
                    fragment.strip("*") in a.get("AlarmName", "").lower()
                    for fragment in p0_names
                )
            ]
            if p0_active:
                return CheckResult(
                    name, "FAIL",
                    f"P0 trading alarm(s) currently in ALARM state: {p0_active}. "
                    "Resolve all active alerts before enabling live trading.",
                )
            return CheckResult(
                name, "PASS",
                "No P0 trading alarms in ALARM state",
            )
        except Exception as exc:
            return CheckResult(
                name, "WARN",
                f"CloudWatch describe_alarms failed: {exc!r}. RUNTIME_VERIFICATION_REQUIRED.",
            )

    # ── Check 21: current time inside trading window ──────────────────────────

    async def _check_trading_window(self) -> CheckResult:
        name = "trading_window"
        now = self._now_ist()
        t = now.time()
        weekday = now.weekday()   # 0=Mon … 6=Sun
        if weekday >= 5:
            return CheckResult(
                name, "FAIL",
                f"Current day is {'Saturday' if weekday == 5 else 'Sunday'} IST. "
                "NSE is closed on weekends. Do not enable live trading outside market days.",
            )
        if t < _NSE_OPEN or t >= _NSE_CLOSE:
            return CheckResult(
                name, "FAIL",
                f"Current IST time {t.strftime('%H:%M:%S')} is outside NSE trading hours "
                f"({_NSE_OPEN.strftime('%H:%M')}–{_NSE_CLOSE.strftime('%H:%M')}). "
                "Enable live trading only during market hours.",
            )
        return CheckResult(
            name, "PASS",
            f"Current IST time {t.strftime('%H:%M:%S')} is within NSE trading hours",
        )

    # ── Check 22: no-new-entry cutoff respected ───────────────────────────────

    async def _check_no_new_entry_cutoff(self) -> CheckResult:
        name = "no_new_entry_cutoff"
        now = self._now_ist()
        t = now.time()
        if t >= _NO_NEW_ENTRY:
            return CheckResult(
                name, "FAIL",
                f"Current IST time {t.strftime('%H:%M:%S')} is at or after the "
                f"no-new-entry cutoff ({_NO_NEW_ENTRY.strftime('%H:%M')} IST). "
                "Do not open live positions this close to NSE close.",
            )
        return CheckResult(
            name, "PASS",
            f"Current IST time {t.strftime('%H:%M:%S')} is before the "
            f"no-new-entry cutoff ({_NO_NEW_ENTRY.strftime('%H:%M')} IST)",
        )

    # ── Check 23: rollback plan exists ────────────────────────────────────────

    async def _check_rollback_plan_exists(self, rec: dict) -> CheckResult:
        name = "rollback_plan_exists"
        confirmed = rec.get("rollback_plan_confirmed", {}).get("BOOL", False)
        if confirmed:
            return CheckResult(
                name, "PASS",
                "rollback_plan_confirmed=True in approval record",
            )
        return CheckResult(
            name, "FAIL",
            "rollback_plan_confirmed is not True in approval record. "
            "Confirm the rollback procedure in docs/live-readiness/pre-live-runbook.md "
            "and set rollback_plan_confirmed=true in the approval record.",
        )

    # ── Check 24: release tag recorded ───────────────────────────────────────

    async def _check_release_tag_recorded(self, rec: dict) -> CheckResult:
        name = "release_tag_recorded"
        tag = rec.get("release_tag", {}).get("S", "").strip()
        if tag:
            return CheckResult(
                name, "PASS",
                f"release_tag={tag!r} recorded in approval record",
            )
        return CheckResult(
            name, "FAIL",
            "release_tag is empty or missing in approval record. "
            "Record the git SHA or semver tag of the deployment being approved.",
        )

    # ── Check 25: human approval recorded ────────────────────────────────────

    async def _check_human_approval_recorded(self, rec: dict) -> CheckResult:
        name = "human_approval_recorded"
        approved_by = rec.get("approved_by", {}).get("S", "").strip()
        approved_at = rec.get("approved_at", {}).get("S", "").strip()
        if approved_by and approved_at:
            return CheckResult(
                name, "PASS",
                f"Human approval by {approved_by!r} at {approved_at}",
            )
        missing = []
        if not approved_by:
            missing.append("approved_by")
        if not approved_at:
            missing.append("approved_at")
        return CheckResult(
            name, "FAIL",
            f"Missing fields in approval record: {missing}. "
            "The approval record must include the operator identity and timestamp "
            "of their explicit sign-off before live trading is enabled.",
        )
