"""EntryBlockValidator — defense-in-depth ENTRY_BLOCK enforcement in risk_engine.

This validator is Phase 6 defense-in-depth.  The primary enforcement point for
ENTRY_BLOCK is strategy_engine (upstream, blocks signal production at source).
This validator provides a second wall: even if a new-entry signal somehow reaches
risk_engine while ENTRY_BLOCK is active, it is rejected here before approval.

Key invariants:
    * Only new-entry signals are rejected.  Closeout and paper signals pass through.
    * signal.metadata["is_closeout"] = True → exempt (same contract as ReconciliationValidator).
    * signal.paper_trade = True → exempt for paper mode; live/live-stage1 still blocked.
    * ENTRY_BLOCK read failure:
        - PAPER profile    → WARN + allow (fail open, configurable via fail_closed_on_error).
        - LIVE / LIVE_STAGE_1 → FAIL CLOSED for new entries.
    * Cache TTL mirrors the shared EntryBlockReader (default 5s).

Wire-up order in RiskEngineService.validate_signal():
    After kill-switch check (step 2), before reconciliation check (step 3).
    This gives the operator a stable override point even when kill switch is inactive.

This validator never:
    * Writes to DynamoDB.
    * Clears the ENTRY_BLOCK flag.
    * Modifies the signal.
    * Blocks exit management.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from shared.models.signal import Signal

from risk_engine.limits.risk_limits import RiskValidationResult
from risk_engine.validators.common import is_paper_signal

# Lazy import — AppSettings/get_settings pull in pydantic_settings which is not
# available in the lightweight test environment. Import only when actually needed.
try:
    from shared.config.settings import AppSettings, get_settings  # type: ignore[import]
except ImportError:  # pragma: no cover
    AppSettings = None  # type: ignore[assignment,misc]
    get_settings = lambda: None  # type: ignore[assignment]

logger = logging.getLogger("risk_engine.validators.entry_block")

VALIDATOR_NAME = "entry_block_validator"

# How stale the cache is allowed to be before we do a fresh DynamoDB read.
_DEFAULT_CACHE_TTL: float = 5.0


class EntryBlockValidator:
    """Reject new-entry signals when ENTRY_BLOCK/GLOBAL is active in DynamoDB.

    Args:
        dynamo_client:        Low-level boto3 DynamoDB client.
        risk_state_table:     Name of the risk-state DynamoDB table.
        risk_profile:         "paper" | "tiny-live" | "live" — controls fail-closed behaviour.
        fail_closed_on_error: Override: True → always fail closed on read error.
                              Defaults to True when risk_profile != "paper".
        cache_ttl_seconds:    How long to cache the last-read state (default 5s).
        settings:             Optional AppSettings (unused; for future config expansion).
    """

    def __init__(
        self,
        dynamo_client: Any,
        risk_state_table: str,
        risk_profile: str = "paper",
        fail_closed_on_error: Optional[bool] = None,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._dynamo = dynamo_client
        self._table = risk_state_table
        self._profile = risk_profile.lower()
        # fail_closed: True for any non-paper profile
        if fail_closed_on_error is not None:
            self._fail_closed = fail_closed_on_error
        else:
            self._fail_closed = self._profile != "paper"
        self._ttl = cache_ttl_seconds
        self._settings = settings or get_settings()

        # Cache state
        self._blocked: bool = False
        self._reason: str = ""
        self._source: str = ""
        self._action_id: str = ""
        self._read_ok: bool = True
        self._cached_at: float = 0.0

    # ── public API ─────────────────────────────────────────────────────────────

    async def validate(self, signal: Signal) -> RiskValidationResult:
        """Check ENTRY_BLOCK flag and approve/reject the signal.

        Closeout and (paper-profile) paper_trade signals bypass this check.

        Returns:
            RiskValidationResult — approved=True if the signal may proceed.
        """
        # Closeout orders are always exempt — blocking them would trap open positions.
        if signal.metadata.get("is_closeout"):
            return RiskValidationResult(
                approved=True,
                validator_name=VALIDATOR_NAME,
                reason="closeout_exempt",
            )

        # Paper signals in paper profile are exempt (they carry no real-money risk).
        # In live profiles, even paper_trade=True signals pass through this check
        # because the universe validator is the canonical paper/live isolation gate.
        if is_paper_signal(signal) and self._profile == "paper":
            return RiskValidationResult(
                approved=True,
                validator_name=VALIDATOR_NAME,
                reason="paper_signal_exempt_in_paper_profile",
            )

        await self._refresh_if_stale()

        if not self._read_ok:
            # Read failure — apply mode-specific behaviour.
            if self._fail_closed:
                logger.error(
                    "entry_block_validator.read_failed_fail_closed "
                    "signal_id=%s symbol=%s profile=%s — rejecting new entry",
                    signal.signal_id, signal.symbol, self._profile,
                )
                return RiskValidationResult(
                    approved=False,
                    validator_name=VALIDATOR_NAME,
                    reason="ENTRY_BLOCK_READ_FAILURE_FAIL_CLOSED: DynamoDB read failed "
                           "in live profile — new entries halted to preserve safety",
                    details={"profile": self._profile, "read_ok": False},
                )
            else:
                logger.warning(
                    "entry_block_validator.read_failed_warn_allow "
                    "signal_id=%s symbol=%s profile=%s — allowing (paper fail-open)",
                    signal.signal_id, signal.symbol, self._profile,
                )
                return RiskValidationResult(
                    approved=True,
                    validator_name=VALIDATOR_NAME,
                    reason="ENTRY_BLOCK_READ_FAILURE_WARN_ALLOW: DynamoDB read failed "
                           "in paper profile — allowing with warning",
                    details={"profile": self._profile, "read_ok": False},
                )

        if self._blocked:
            logger.warning(
                "entry_block_validator.signal_rejected "
                "signal_id=%s symbol=%s reason=%s action_id=%s",
                signal.signal_id, signal.symbol, self._reason, self._action_id,
            )
            return RiskValidationResult(
                approved=False,
                validator_name=VALIDATOR_NAME,
                reason=(
                    f"ENTRY_BLOCK_ACTIVE: new entries halted. "
                    f"reason={self._reason!r} source={self._source!r} "
                    f"action_id={self._action_id!r}. "
                    "Clear ENTRY_BLOCK/GLOBAL in risk-state DynamoDB to resume."
                ),
                details={
                    "entry_block_reason": self._reason,
                    "source": self._source,
                    "action_id": self._action_id,
                },
            )

        return RiskValidationResult(
            approved=True,
            validator_name=VALIDATOR_NAME,
            reason="entry_block_not_active",
        )

    @property
    def entry_block_active(self) -> bool:
        """Cached value — True if ENTRY_BLOCK is currently active."""
        return self._blocked

    @property
    def last_read_ok(self) -> bool:
        """True if the last DynamoDB read succeeded."""
        return self._read_ok

    # ── cache refresh ──────────────────────────────────────────────────────────

    async def _refresh_if_stale(self) -> None:
        """Refresh the in-memory cache from DynamoDB if TTL has expired."""
        if time.monotonic() - self._cached_at < self._ttl:
            return

        try:
            from shared.risk_state import ENTRY_BLOCK_PK, ENTRY_BLOCK_SK, attr_bool, attr_string
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._table,
                Key={"PK": {"S": ENTRY_BLOCK_PK}, "SK": {"S": ENTRY_BLOCK_SK}},
                ProjectionExpression=(
                    "blocked, #st, reason, source, action_id, "
                    "idempotency_key, created_at, schema_version"
                ),
                ExpressionAttributeNames={"#st": "status"},
            )
            item = response.get("Item")
            if item:
                self._blocked = attr_bool(item, "blocked", False)
                self._reason = attr_string(item, "reason")
                self._source = attr_string(item, "source")
                self._action_id = attr_string(item, "action_id")
            else:
                self._blocked = False
                self._reason = ""
                self._source = ""
                self._action_id = ""
            self._read_ok = True
            if self._blocked:
                logger.warning(
                    "entry_block_validator.active reason=%s source=%s action_id=%s",
                    self._reason, self._source, self._action_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "entry_block_validator.dynamo_read_failed error_type=%s profile=%s",
                type(exc).__name__, self._profile,
            )
            self._read_ok = False
            # Do not update cache timestamp — keep retrying on next signal.
            return

        self._cached_at = time.monotonic()

    def invalidate(self) -> None:
        """Force the next validate() call to bypass the cache."""
        self._cached_at = 0.0
