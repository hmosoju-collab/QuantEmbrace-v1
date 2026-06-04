"""
Trade Exit Engine (TEE) — monitors open positions and triggers exits.

Runs as a long-lived async task alongside the execution service.

Responsibilities (Phase 3 + strategy-aware R-based exits):
    - Poll DynamoDB positions every TEE_POLL_INTERVAL_SECONDS (default 30).
    - For each managed position (direction <> FLAT, stop_price present):
        * Resolve last known price (prices table → position.last_price fallback).
        * Compute current R-multiple relative to initial risk.
        * Evaluate exit conditions in priority order:
            1. Stop-loss / trailing stop (highest priority, always checked first).
            2. Breakeven shift: move stop to entry at breakeven_at_r.
            3. Partial profit booking at partial_profit_at_r (idempotent).
            4. Trailing activation at trailing_activate_at_r (R-based distance).
            5. Trailing advance (ratchet — only tightens stop).
            6. Fixed TP (suppressed per strategy policy when trailing active).
            7. Max hold time exit (max_hold_minutes).
            8. Hard time exit (hard_exit_time IST).
        * Manage trailing stop: activate, advance, and write updated stop_price to DynamoDB.
        * Stop-loss takes priority when both conditions are met in the same cycle.
    - Alert CRITICAL on positions that are OPEN but have no stop_price (unmanaged).
    - Route exit via ExitOrderRouter — NEVER touches the signal pipeline.

Side-aware exit rules:
    LONG:  stop fires when last_price <= stop_price  (price fell to/below stop)
           tp   fires when last_price >= take_profit  (price rose to/above target)
    SHORT: stop fires when last_price >= stop_price  (price rose to/above stop)
           tp   fires when last_price <= take_profit  (price fell to/below target)

R-multiple calculation:
    initial_risk = abs(entry_price - stop_price)
    LONG:  R = (last_price - entry_price) / initial_risk
    SHORT: R = (entry_price - last_price) / initial_risk
    R > 0 = in profit; R < 0 = in loss; R = 0 = at entry

Trailing stop rules (R-based):
    Activates when current_r >= policy.trailing_activate_at_r.
    Trailing stop is placed at: last_price - (trailing_distance_r * initial_risk)  [LONG]
                                last_price + (trailing_distance_r * initial_risk)  [SHORT]
    Ratchet invariant: stop only moves in your favor.

Partial exit rules:
    Fires once when current_r >= policy.partial_profit_at_r.
    Idempotent: uses DynamoDB conditional write on partial_exit_order_id field.
    Does NOT set the main exit_order_id (full exit remains available later).

Breakeven shift:
    Moves stop to entry when current_r >= policy.breakeven_at_r and
    stop has not already been shifted past entry.

Not in scope:
    - Startup reconciliation (separate service)
    - Kafka signal pipeline (exits bypass it entirely)
    - Daily cap (exits bypass the cap check)

Exit priority hierarchy (full system):
    1. Kill switch     — unconditional flatten, not managed here
    2. TEE             — stop-loss / take-profit / trailing / R-based (this module)
    3. MIS at 15:05    — time-based close (MISSquareOffManager)
    4. Zerodha 15:15   — broker auto-square (no code path)
"""
from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from services.shared.logging.logger import get_logger
from services.execution_engine.exit.exit_models import (
    ExitOrderRequest,
    ExitTriggerType,
)
from services.execution_engine.exit.exit_order_router import ExitOrderRouter
from services.shared.monitoring import LiveCounters
from services.shared.monitoring.ltp_resolver import LtpResolver

logger = get_logger(__name__, service_name="execution_engine")

_DEFAULT_POLL_INTERVAL: int = int(os.environ.get("TEE_POLL_INTERVAL_SECONDS", "30"))
_IST = timezone(timedelta(seconds=19800))


def _session_date_ist() -> str:
    """Return today's session date string in IST (UTC+05:30)."""
    return datetime.now(_IST).strftime("%Y-%m-%d")


def _now_ist() -> datetime:
    """Return current datetime in IST."""
    return datetime.now(_IST)


# ── Exit policy dataclass ─────────────────────────────────────────────────────


@dataclass
class ExitPolicy:
    """
    Strategy-aware exit policy loaded from configs/exit_policy.yaml strategies section.

    All R-values are multiples of initial_risk = abs(entry_price - stop_price).
    None values disable the corresponding rule for a strategy.

    Fields:
        strategy_id:                    Strategy identifier from positions table.
        breakeven_at_r:                 Move stop to entry when R reaches this value.
        partial_profit_at_r:            Book partial exit when R reaches this value.
        partial_qty_pct:                Percentage of remaining qty to book as partial (0-100).
        trailing_activate_at_r:         Activate trailing stop when R reaches this value.
        trailing_distance_r:            Trailing stop distance expressed as R multiples.
        max_hold_minutes:               Exit full position after this many minutes (None = disabled).
        hard_exit_time:                 Force full exit at this IST time string "HH:MM" (None = disabled).
        fixed_tp_enabled:               Whether the fixed take_profit level is active.
        suppress_fixed_tp_after_trailing: If True, suppress fixed TP once trailing is active.
    """

    strategy_id: str
    breakeven_at_r: Optional[float]
    partial_profit_at_r: Optional[float]
    partial_qty_pct: float              # 0–100
    trailing_activate_at_r: Optional[float]
    trailing_distance_r: Optional[float]
    max_hold_minutes: Optional[int]
    hard_exit_time: Optional[str]       # "HH:MM" IST
    fixed_tp_enabled: bool
    suppress_fixed_tp_after_trailing: bool


# ── Exit policy loader ────────────────────────────────────────────────────────


class ExitPolicyLoader:
    """
    Loads and provides per-strategy exit policies from configs/exit_policy.yaml.

    The loader reads the ``strategies`` section of exit_policy.yaml. This section
    coexists with the tier-based section (which drives stop/TP placement at entry).

    The loader supports hot-reload: call reload() to pick up file changes without
    restarting the service. The existing in-memory policies continue serving during
    the reload.

    Args:
        policy_path: Path to exit_policy.yaml. Defaults to configs/exit_policy.yaml.
    """

    _DEFAULT_PATH = "configs/exit_policy.yaml"

    def __init__(self, policy_path: str = _DEFAULT_PATH) -> None:
        self._path = Path(policy_path)
        self._policies: dict[str, ExitPolicy] = {}
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, strategy_id: str) -> ExitPolicy:
        """
        Return the ExitPolicy for a strategy_id.

        Falls back to _default policy if the strategy_id is not found.
        If even _default is absent, returns a conservative built-in fallback
        that preserves pre-R-based behaviour (trailing_activate_at_r=1.25).
        """
        policy = self._policies.get(strategy_id)
        if policy is not None:
            return policy
        default = self._policies.get("_default")
        if default is not None:
            logger.debug(
                "tee.exit_policy.fallback_to_default",
                strategy_id=strategy_id,
            )
            # Return a copy with the strategy_id set to the requested one
            return ExitPolicy(
                strategy_id=strategy_id,
                breakeven_at_r=default.breakeven_at_r,
                partial_profit_at_r=default.partial_profit_at_r,
                partial_qty_pct=default.partial_qty_pct,
                trailing_activate_at_r=default.trailing_activate_at_r,
                trailing_distance_r=default.trailing_distance_r,
                max_hold_minutes=default.max_hold_minutes,
                hard_exit_time=default.hard_exit_time,
                fixed_tp_enabled=default.fixed_tp_enabled,
                suppress_fixed_tp_after_trailing=default.suppress_fixed_tp_after_trailing,
            )
        # Built-in fallback: preserve old percentage-based behaviour
        logger.warning(
            "tee.exit_policy.no_default_found",
            strategy_id=strategy_id,
            detail="Using built-in fallback policy (trailing_activate_at_r=1.25)",
        )
        return ExitPolicy(
            strategy_id=strategy_id,
            breakeven_at_r=None,
            partial_profit_at_r=None,
            partial_qty_pct=50.0,
            trailing_activate_at_r=1.25,
            trailing_distance_r=0.6,
            max_hold_minutes=None,
            hard_exit_time=None,
            fixed_tp_enabled=True,
            suppress_fixed_tp_after_trailing=True,
        )

    def reload(self) -> None:
        """
        Hot-reload the policy file. Thread-safe: swap is atomic (dict assignment).

        Logs a warning if the file cannot be read; retains existing policies in that case.
        """
        try:
            self._load()
            logger.info(
                "tee.exit_policy.reloaded",
                path=str(self._path),
                strategies=list(self._policies.keys()),
            )
        except Exception:
            logger.exception(
                "tee.exit_policy.reload_failed",
                path=str(self._path),
                detail="Retaining existing policies. Investigate YAML syntax.",
            )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load(self) -> None:
        """Parse strategies section of exit_policy.yaml into ExitPolicy objects."""
        resolved = self._resolve_path()
        if not resolved.exists():
            logger.warning(
                "tee.exit_policy.file_not_found",
                path=str(resolved),
                detail="Using built-in fallback policies for all strategies.",
            )
            return

        with open(resolved, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        strategies_raw = raw.get("strategies", {})
        if not strategies_raw:
            logger.warning(
                "tee.exit_policy.no_strategies_section",
                path=str(resolved),
                detail="exit_policy.yaml has no 'strategies:' section.",
            )
            return

        new_policies: dict[str, ExitPolicy] = {}
        for name, cfg in strategies_raw.items():
            if not isinstance(cfg, dict):
                continue
            policy = ExitPolicy(
                strategy_id=name,
                breakeven_at_r=cfg.get("breakeven_at_r"),
                partial_profit_at_r=cfg.get("partial_profit_at_r"),
                partial_qty_pct=float(cfg.get("partial_qty_pct", 50)),
                trailing_activate_at_r=cfg.get("trailing_activate_at_r"),
                trailing_distance_r=cfg.get("trailing_distance_r"),
                max_hold_minutes=cfg.get("max_hold_minutes"),
                hard_exit_time=cfg.get("hard_exit_time"),
                fixed_tp_enabled=bool(cfg.get("fixed_tp_enabled", True)),
                suppress_fixed_tp_after_trailing=bool(
                    cfg.get("suppress_fixed_tp_after_trailing", True)
                ),
            )
            new_policies[name] = policy

        # Atomic swap — replaces entire dict so partial state is never visible
        self._policies = new_policies
        logger.info(
            "tee.exit_policy.loaded",
            path=str(resolved),
            strategies=list(new_policies.keys()),
        )

    def _resolve_path(self) -> Path:
        """Resolve path: try as-is, then walk up from this file to find repo root."""
        if self._path.exists():
            return self._path
        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / self._path
            if candidate.exists():
                return candidate
        return self._path  # return original so the error message is useful


# ── Trade Exit Engine ─────────────────────────────────────────────────────────


class TradeExitEngine:
    """
    Monitors all managed open positions and fires exit orders on trigger.

    Never touches the Kafka signal pipeline. Never increments signals_today.
    The daily strategy cap cannot block exits.

    Strategy-aware mode: loads ExitPolicyLoader to apply per-strategy R-based
    exit rules (breakeven shift, partial profit, trailing activation, max hold,
    hard time exit). The old percentage-based trailing is preserved as the
    ``_default`` policy for strategies not listed in exit_policy.yaml.

    Args:
        dynamo_client:           boto3 DynamoDB client.
        positions_table:         DynamoDB positions table name.
        router:                  ExitOrderRouter (paper / live / backtest).
        prices_table:            Optional DynamoDB prices table (QUOTE#NSE/{symbol}/LATEST).
        poll_interval:           Seconds between position scans. Default: 30.
        trailing_enabled:        Whether to manage trailing stops. Default True.
        trailing_activation_pct: Legacy pct-based activation (used by _default fallback).
        trailing_stop_pct:       Legacy pct-based trail distance (used by _default fallback).
        exit_policy_path:        Path to exit_policy.yaml for ExitPolicyLoader.
        live_counters:           LiveCounters for monitoring metrics.
    """

    def __init__(
        self,
        dynamo_client: Any,
        positions_table: str,
        router: ExitOrderRouter,
        *,
        prices_table: Optional[str] = None,
        poll_interval: int = _DEFAULT_POLL_INTERVAL,
        trailing_enabled: bool = True,
        trailing_activation_pct: float = 1.25,
        trailing_stop_pct: float = 0.6,
        exit_policy_path: str = ExitPolicyLoader._DEFAULT_PATH,
        live_counters: Optional[LiveCounters] = None,
    ) -> None:
        self._dynamo = dynamo_client
        self._positions_table = positions_table
        self._router = router
        self._prices_table = prices_table
        self._poll_interval = poll_interval
        self._running = True
        self._trailing_enabled = trailing_enabled
        self._trailing_activation_pct = trailing_activation_pct
        self._trailing_stop_pct = trailing_stop_pct
        self._live_counters = live_counters
        self._ltp_resolver = LtpResolver(
            dynamo_client=dynamo_client,
            prices_table=prices_table,
            freshness_seconds=float(os.environ.get("TEE_LTP_FRESHNESS_SECONDS", "5.0")),
        )
        # In LIVE mode, block exit evaluation when LTP age exceeds this threshold.
        # Paper mode only warns. Controlled by TEE_MAX_STALE_LTP_LIVE_SECONDS (default 3 s).
        self._max_stale_ltp_live = float(os.environ.get("TEE_MAX_STALE_LTP_LIVE_SECONDS", "3.0"))
        # Strategy-aware exit policy loader
        self._exit_policy_loader = ExitPolicyLoader(exit_policy_path)

    async def run(self) -> None:
        """Main loop — poll and evaluate exit conditions continuously."""
        logger.info(
            "tee.started",
            poll_interval_seconds=self._poll_interval,
            mode=self._router.mode.value,
        )
        if self._live_counters is not None:
            self._live_counters.tee_running = True
            self._live_counters.tee_poll_interval = self._poll_interval
        while self._running:
            try:
                await self._check_all_positions()
            except Exception:
                logger.exception("tee.cycle_error")
            await asyncio.sleep(self._poll_interval)
        logger.info("tee.stopped")

    def stop(self) -> None:
        """Signal the engine to stop after the current cycle completes."""
        self._running = False

    # ── Position scan ─────────────────────────────────────────────────────────

    async def _check_all_positions(self) -> None:
        """Single poll cycle: scan positions, alert unmanaged, evaluate exits."""
        managed = await self._get_open_managed_positions()
        await self._alert_unmanaged_positions()

        if not managed:
            logger.debug("tee.no_managed_positions")
            return

        logger.info(
            "tee.cycle_start",
            managed_positions=len(managed),
            symbols=[p["symbol"] for p in managed],
        )

        for position in managed:
            try:
                await self._evaluate_exit_conditions(position)
            except Exception:
                logger.exception(
                    "tee.position_evaluation_error",
                    symbol=position.get("symbol"),
                )

    async def _get_open_managed_positions(self) -> list[dict]:
        """
        Scan DynamoDB for positions that are open AND have an exit policy.

        Filter: direction <> FLAT  AND  attribute_exists(stop_price)

        Returns a list of parsed position dicts ready for exit evaluation.
        """
        response = await asyncio.to_thread(
            self._dynamo.scan,
            TableName=self._positions_table,
            FilterExpression=(
                "#dir <> :flat AND attribute_exists(stop_price)"
            ),
            ExpressionAttributeValues={
                ":flat": {"S": "FLAT"},
            },
            ProjectionExpression=(
                "symbol, #dir, quantity, avg_price, avg_entry_price, "
                "stop_price, take_profit, last_price, product, exit_order_id, "
                "exit_state, strategy_id, partial_exit_order_id, entry_time"
            ),
            ExpressionAttributeNames={"#dir": "direction"},
        )
        items = response.get("Items", [])
        resolved = []
        for item in items:
            pos = self._parse_position(item)
            if pos is not None:
                resolved.append(pos)
        return resolved

    async def _alert_unmanaged_positions(self) -> None:
        """
        Scan for open positions that have NO stop_price.

        These positions are unprotected. Each one gets a CRITICAL log on every
        poll cycle until the condition is resolved.
        """
        response = await asyncio.to_thread(
            self._dynamo.scan,
            TableName=self._positions_table,
            FilterExpression=(
                "#dir <> :flat AND attribute_not_exists(stop_price)"
            ),
            ExpressionAttributeValues={
                ":flat": {"S": "FLAT"},
            },
            ProjectionExpression="symbol, #dir, quantity",
            ExpressionAttributeNames={"#dir": "direction"},
        )
        for item in response.get("Items", []):
            symbol = item.get("symbol", {}).get("S", "UNKNOWN")
            logger.critical(
                "tee.unmanaged_open_position",
                symbol=symbol,
                detail=(
                    "Position is OPEN but has no exit policy (stop_price absent). "
                    "Risk is unprotected. Investigate immediately."
                ),
            )
            if self._live_counters is not None:
                self._live_counters.tee_unmanaged_detections += 1

    def _parse_position(self, item: dict) -> Optional[dict]:
        """Parse a DynamoDB attribute-typed position item into a plain dict."""
        symbol    = item.get("symbol",    {}).get("S", "UNKNOWN")
        direction = item.get("direction", {}).get("S", "")
        qty_str   = item.get("quantity",  {}).get("N")
        stop_str  = item.get("stop_price", {}).get("N")
        tp_str    = item.get("take_profit", {}).get("N")
        # avg_price may appear under either name depending on fill path
        avg_str   = (
            item.get("avg_price", {}).get("N")
            or item.get("avg_entry_price", {}).get("N")
        )
        last_str         = item.get("last_price", {}).get("N")
        exit_oid         = item.get("exit_order_id", {}).get("S")
        exit_state       = item.get("exit_state", {}).get("S")
        strategy_id      = item.get("strategy_id", {}).get("S") or "_default"
        partial_exit_oid = item.get("partial_exit_order_id", {}).get("S")
        entry_time_str   = item.get("entry_time", {}).get("S")

        if qty_str is None or stop_str is None:
            return None

        quantity = float(qty_str)
        if abs(quantity) < 1e-9:
            return None

        return {
            "symbol":               symbol,
            "direction":            direction,
            "quantity":             quantity,
            "avg_price":            float(avg_str) if avg_str else 0.0,
            "stop_price":           float(stop_str),
            "take_profit":          float(tp_str) if tp_str else None,
            "last_price":           float(last_str) if last_str else None,
            "exit_order_id":        exit_oid,
            "exit_state":           exit_state,
            "strategy_id":          strategy_id,
            "partial_exit_order_id": partial_exit_oid,
            "entry_time":           entry_time_str,
        }

    # ── R-multiple helpers (pure functions, easy to test) ─────────────────────

    @staticmethod
    def compute_r(
        direction: str,
        entry_price: float,
        stop_price: float,
        last_price: float,
    ) -> float:
        """
        Compute the current R-multiple for a position.

        R = (favourable_move) / initial_risk
        initial_risk = abs(entry_price - stop_price)

        Returns 0.0 when initial_risk is zero (zero-risk guard, no divide-by-zero).

        LONG:  R = (last_price - entry_price) / initial_risk
        SHORT: R = (entry_price - last_price) / initial_risk

        R > 0 = in profit territory; R < 0 = in loss territory; R = 0 = at entry.
        """
        initial_risk = abs(entry_price - stop_price)
        if initial_risk < 1e-9:
            return 0.0
        if direction == "LONG":
            return (last_price - entry_price) / initial_risk
        if direction == "SHORT":
            return (entry_price - last_price) / initial_risk
        return 0.0

    @staticmethod
    def compute_trailing_stop_from_r(
        direction: str,
        entry_price: float,
        stop_price: float,
        last_price: float,
        trailing_distance_r: float,
    ) -> float:
        """
        Compute trailing stop price from R-distance.

        trail_amount = trailing_distance_r * initial_risk
        LONG:  trailing_stop = last_price - trail_amount
        SHORT: trailing_stop = last_price + trail_amount

        The ratchet invariant is enforced by the caller (stop only moves in favor).
        """
        initial_risk = abs(entry_price - stop_price)
        trail_amount = trailing_distance_r * initial_risk
        if direction == "LONG":
            return last_price - trail_amount
        if direction == "SHORT":
            return last_price + trail_amount
        return stop_price

    # ── Exit condition evaluation ─────────────────────────────────────────────

    async def _evaluate_exit_conditions(self, position: dict) -> None:
        """
        Check all exit conditions for one managed position in priority order.

        Priority:
            1. Stop-loss / trailing stop (unconditional — never blocked)
            2. Breakeven shift (DynamoDB write only, no exit order)
            3. Partial profit (fires once per position; idempotent via DynamoDB CW)
            4. Trailing activation / advance (DynamoDB write only)
            5. Fixed TP (conditional on policy.fixed_tp_enabled + suppress rules)
            6. Max hold exit
            7. Hard time exit

        Exits bypass all daily caps — this is enforced by ExitOrderRouter.
        """
        if position["exit_order_id"] is not None:
            return  # full exit already in-flight

        symbol     = position["symbol"]
        last_price = await self._get_last_price(symbol, position)

        if last_price is None:
            logger.warning("tee.no_price_available", symbol=symbol)
            return

        direction   = position["direction"]
        stop_price  = position["stop_price"]
        take_profit = position["take_profit"]
        exit_state  = position.get("exit_state")
        entry_price = position["avg_price"]
        strategy_id = position.get("strategy_id", "_default")

        # Load strategy-aware exit policy
        policy = self._exit_policy_loader.get(strategy_id)

        # ── 1. Stop-loss / trailing stop: highest priority, always checked first ─
        if self.stop_loss_triggered(direction, last_price, stop_price):
            trigger = (
                ExitTriggerType.TRAILING
                if exit_state == "TRAILING_ACTIVE"
                else ExitTriggerType.STOP_LOSS
            )
            logger.warning(
                "tee.stop_loss_triggered",
                symbol=symbol,
                direction=direction,
                last_price=last_price,
                stop_price=stop_price,
                trigger=trigger.value,
            )
            await self._fire_exit(position, trigger, last_price)
            return

        # Compute current R-multiple (requires valid entry_price)
        current_r: Optional[float] = None
        if entry_price > 0:
            current_r = self.compute_r(direction, entry_price, stop_price, last_price)

        # ── 2. Breakeven shift (stop → entry price) ───────────────────────────
        if (
            current_r is not None
            and policy.breakeven_at_r is not None
            and current_r >= policy.breakeven_at_r
            and entry_price > 0
        ):
            improved = (
                (direction == "LONG" and stop_price < entry_price)
                or (direction == "SHORT" and stop_price > entry_price)
            )
            if improved:
                logger.info(
                    "tee.breakeven_shift",
                    symbol=symbol,
                    direction=direction,
                    current_r=round(current_r, 3),
                    breakeven_at_r=policy.breakeven_at_r,
                    old_stop=stop_price,
                    new_stop=entry_price,
                )
                await self._write_position_update(
                    symbol=symbol,
                    updates={"stop_price": entry_price},
                )
                if self._live_counters is not None:
                    self._live_counters.tee_breakeven_shifts += 1
                # Update local position dict so subsequent checks use new stop
                position["stop_price"] = entry_price
                stop_price = entry_price

        # ── 3. Partial profit booking ─────────────────────────────────────────
        if (
            current_r is not None
            and policy.partial_profit_at_r is not None
            and current_r >= policy.partial_profit_at_r
            and position.get("partial_exit_order_id") is None  # idempotency check
        ):
            qty = position["quantity"]
            partial_qty = math.floor(abs(qty) * policy.partial_qty_pct / 100.0)
            if partial_qty >= 1:
                partial_oid = (
                    f"EXIT-{symbol}-{strategy_id}-PARTIAL-{_session_date_ist()}"
                )
                booked = await self._book_partial_exit(
                    position=position,
                    partial_qty=partial_qty,
                    partial_exit_order_id=partial_oid,
                    last_price=last_price,
                )
                if booked:
                    logger.info(
                        "tee.partial_profit_booked",
                        symbol=symbol,
                        direction=direction,
                        current_r=round(current_r, 3),
                        partial_profit_at_r=policy.partial_profit_at_r,
                        partial_qty=partial_qty,
                        partial_oid=partial_oid,
                    )
                    if self._live_counters is not None:
                        self._live_counters.tee_partial_bookings += 1
                    # Mark position locally to skip duplicate partial checks
                    position["partial_exit_order_id"] = partial_oid

        # ── 4 & 5. Trailing stop management ──────────────────────────────────
        if self._trailing_enabled and current_r is not None and entry_price > 0:
            await self._manage_trailing_stop_r(
                position=position,
                last_price=last_price,
                current_r=current_r,
                policy=policy,
                entry_price=entry_price,
            )
        elif self._trailing_enabled and entry_price == 0:
            # Legacy pct-based fallback for positions without entry_price
            await self._manage_trailing_stop(position, last_price)

        # ── 6. Fixed TP ───────────────────────────────────────────────────────
        # Re-read exit_state as trailing activation may have updated it
        exit_state = position.get("exit_state")
        trailing_now_active = exit_state == "TRAILING_ACTIVE"

        fixed_tp_blocked = (
            not policy.fixed_tp_enabled
            or (policy.suppress_fixed_tp_after_trailing and trailing_now_active)
        )

        if (
            take_profit is not None
            and not fixed_tp_blocked
            and self.take_profit_triggered(direction, last_price, take_profit)
        ):
            logger.info(
                "tee.take_profit_triggered",
                symbol=symbol,
                direction=direction,
                last_price=last_price,
                take_profit=take_profit,
            )
            await self._fire_exit(position, ExitTriggerType.TAKE_PROFIT, take_profit)
            return

        # ── 7. Max hold exit ──────────────────────────────────────────────────
        if policy.max_hold_minutes is not None and position.get("entry_time"):
            hold_minutes = self._hold_minutes(position["entry_time"])
            if hold_minutes is not None and hold_minutes > policy.max_hold_minutes:
                logger.info(
                    "tee.max_hold_exit",
                    symbol=symbol,
                    direction=direction,
                    hold_minutes=round(hold_minutes, 1),
                    max_hold_minutes=policy.max_hold_minutes,
                )
                await self._fire_exit(position, ExitTriggerType.TIME_EXIT, last_price)
                if self._live_counters is not None:
                    self._live_counters.tee_max_hold_exits += 1
                return

        # ── 8. Hard exit time ─────────────────────────────────────────────────
        if policy.hard_exit_time is not None:
            if self._past_hard_exit_time(policy.hard_exit_time):
                logger.info(
                    "tee.hard_time_exit",
                    symbol=symbol,
                    direction=direction,
                    hard_exit_time=policy.hard_exit_time,
                )
                await self._fire_exit(position, ExitTriggerType.TIME_EXIT, last_price)
                if self._live_counters is not None:
                    self._live_counters.tee_hard_time_exits += 1
                return

    # ── Trigger logic (public for testability) ────────────────────────────────

    @staticmethod
    def stop_loss_triggered(direction: str, last_price: float, stop_price: float) -> bool:
        """
        Return True when the stop-loss condition is met.

        LONG:  price fell to or below stop_price  (last_price <= stop_price)
        SHORT: price rose to or above stop_price  (last_price >= stop_price)
        """
        if direction == "LONG":
            return last_price <= stop_price
        if direction == "SHORT":
            return last_price >= stop_price
        return False

    @staticmethod
    def take_profit_triggered(direction: str, last_price: float, take_profit: float) -> bool:
        """
        Return True when the take-profit condition is met.

        LONG:  price rose to or above take_profit  (last_price >= take_profit)
        SHORT: price fell to or below take_profit  (last_price <= take_profit)
        """
        if direction == "LONG":
            return last_price >= take_profit
        if direction == "SHORT":
            return last_price <= take_profit
        return False

    @staticmethod
    def trailing_activation_triggered(
        direction: str,
        last_price: float,
        avg_entry_price: float,
        activation_pct: float,
    ) -> bool:
        """
        Return True when trailing stop should activate (legacy pct-based).

        LONG:  price rose >= entry * (1 + activation_pct/100)
        SHORT: price fell <= entry * (1 - activation_pct/100)
        """
        if direction == "LONG":
            return last_price >= avg_entry_price * (1.0 + activation_pct / 100.0)
        if direction == "SHORT":
            return last_price <= avg_entry_price * (1.0 - activation_pct / 100.0)
        return False

    @staticmethod
    def compute_trailing_stop(
        direction: str,
        last_price: float,
        current_stop: float,
        trail_pct: float,
    ) -> float:
        """
        Compute the new trailing stop price (legacy pct-based).

        LONG:  new_stop = max(current_stop, last_price * (1 - trail_pct/100))
               Stop only moves upward — never loosens risk.
        SHORT: new_stop = min(current_stop, last_price * (1 + trail_pct/100))
               Stop only moves downward — never loosens risk.
        """
        if direction == "LONG":
            candidate = last_price * (1.0 - trail_pct / 100.0)
            return max(current_stop, candidate)
        if direction == "SHORT":
            candidate = last_price * (1.0 + trail_pct / 100.0)
            return min(current_stop, candidate)
        return current_stop

    # ── R-based trailing stop management ─────────────────────────────────────

    async def _manage_trailing_stop_r(
        self,
        position: dict,
        last_price: float,
        current_r: float,
        policy: ExitPolicy,
        entry_price: float,
    ) -> None:
        """
        Activate or advance the trailing stop using R-based distance.

        Activation: when current_r >= policy.trailing_activate_at_r and not yet TRAILING_ACTIVE.
        Advance: when TRAILING_ACTIVE, compute new stop from R-distance, apply ratchet.
        """
        if policy.trailing_activate_at_r is None or policy.trailing_distance_r is None:
            return  # trailing disabled for this strategy (e.g., nse_scalp_1m)

        exit_state = position.get("exit_state")
        direction  = position["direction"]
        stop_price = position["stop_price"]

        if exit_state == "TRAILING_ACTIVE":
            # Advance trailing stop using R-distance
            candidate = self.compute_trailing_stop_from_r(
                direction=direction,
                entry_price=entry_price,
                stop_price=stop_price,
                last_price=last_price,
                trailing_distance_r=policy.trailing_distance_r,
            )
            # Apply ratchet: stop only moves in your favor
            if direction == "LONG":
                new_stop = max(stop_price, candidate)
            else:
                new_stop = min(stop_price, candidate)

            if abs(new_stop - stop_price) < 1e-6:
                return  # no improvement — skip DynamoDB write

            logger.info(
                "tee.trailing_stop_advanced_r",
                symbol=position["symbol"],
                direction=direction,
                last_price=last_price,
                current_r=round(current_r, 3),
                new_stop=new_stop,
                prev_stop=stop_price,
            )
            await self._write_trailing_stop(position["symbol"], new_stop, state="TRAILING_ACTIVE")
            position["stop_price"] = new_stop

        elif current_r >= policy.trailing_activate_at_r:
            # Activate trailing stop for the first time
            candidate = self.compute_trailing_stop_from_r(
                direction=direction,
                entry_price=entry_price,
                stop_price=stop_price,
                last_price=last_price,
                trailing_distance_r=policy.trailing_distance_r,
            )
            # Apply ratchet: new stop must be at least as good as current stop
            if direction == "LONG":
                new_stop = max(stop_price, candidate)
            else:
                new_stop = min(stop_price, candidate)

            logger.info(
                "tee.trailing_stop_activated_r",
                symbol=position["symbol"],
                direction=direction,
                last_price=last_price,
                current_r=round(current_r, 3),
                activation_r=policy.trailing_activate_at_r,
                initial_trailing_stop=new_stop,
                prev_stop=stop_price,
            )
            if self._live_counters is not None:
                self._live_counters.tee_trailing_activated += 1
                self._live_counters.tee_trailing_activations_r += 1

            await self._write_trailing_stop(position["symbol"], new_stop, state="TRAILING_ACTIVE")
            position["exit_state"] = "TRAILING_ACTIVE"
            position["stop_price"] = new_stop

    # ── Legacy pct-based trailing stop management ─────────────────────────────

    async def _manage_trailing_stop(self, position: dict, last_price: float) -> None:
        """Activate or advance the trailing stop using legacy pct-based distance."""
        exit_state = position.get("exit_state")
        direction  = position["direction"]

        if exit_state == "TRAILING_ACTIVE":
            await self._advance_trailing_stop(position, last_price)
        else:
            if self.trailing_activation_triggered(
                direction, last_price, position["avg_price"],
                self._trailing_activation_pct,
            ):
                await self._activate_trailing_stop(position, last_price)

    async def _activate_trailing_stop(self, position: dict, last_price: float) -> None:
        """
        Transition exit_state → TRAILING_ACTIVE and write initial trailing stop.
        The trailing stop is always at least as tight as the existing stop_price.
        """
        symbol    = position["symbol"]
        direction = position["direction"]
        new_stop  = self.compute_trailing_stop(
            direction, last_price, position["stop_price"], self._trailing_stop_pct
        )

        logger.info(
            "tee.trailing_stop_activated",
            symbol=symbol,
            direction=direction,
            last_price=last_price,
            initial_trailing_stop=new_stop,
            prev_stop=position["stop_price"],
        )

        if self._live_counters is not None:
            self._live_counters.tee_trailing_activated += 1

        await self._write_trailing_stop(symbol, new_stop, state="TRAILING_ACTIVE")

    async def _advance_trailing_stop(self, position: dict, last_price: float) -> None:
        """
        Advance the trailing stop if price moved favorably. No-op if stop would loosen.
        """
        symbol    = position["symbol"]
        direction = position["direction"]
        new_stop  = self.compute_trailing_stop(
            direction, last_price, position["stop_price"], self._trailing_stop_pct
        )

        if new_stop == position["stop_price"]:
            return  # no improvement — skip DynamoDB write

        logger.info(
            "tee.trailing_stop_advanced",
            symbol=symbol,
            direction=direction,
            last_price=last_price,
            new_stop=new_stop,
            prev_stop=position["stop_price"],
        )

        await self._write_trailing_stop(symbol, new_stop, state="TRAILING_ACTIVE")

    async def _write_trailing_stop(self, symbol: str, new_stop: float, state: str) -> None:
        """Persist the new trailing stop price and exit_state to DynamoDB."""
        from shared.risk_state import position_key  # noqa: PLC0415

        try:
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._positions_table,
                Key=position_key(symbol),
                UpdateExpression="SET stop_price = :new_stop, exit_state = :state",
                ConditionExpression="direction <> :flat",
                ExpressionAttributeValues={
                    ":new_stop": {"N": str(new_stop)},
                    ":state":    {"S": state},
                    ":flat":     {"S": "FLAT"},
                },
            )
        except Exception:
            logger.exception("tee.trailing_stop_write_failed", symbol=symbol)

    # ── Partial exit ──────────────────────────────────────────────────────────

    async def _book_partial_exit(
        self,
        position: dict,
        partial_qty: int,
        partial_exit_order_id: str,
        last_price: float,
    ) -> bool:
        """
        Book a partial exit order and mark the position as partially exited.

        Idempotency: uses a conditional DynamoDB write on partial_exit_order_id.
        If the field already exists (race or duplicate cycle), skips without
        firing another exit order.

        Does NOT set the main exit_order_id — the full exit remains available later.

        Returns True if the partial was booked, False if already booked (idempotent skip).
        """
        from shared.risk_state import position_key  # noqa: PLC0415

        symbol    = position["symbol"]
        direction = position["direction"]

        # Conditional write: only proceed if partial_exit_order_id is not yet set
        try:
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._positions_table,
                Key=position_key(symbol),
                UpdateExpression="SET partial_exit_order_id = :poid, exit_state = :state",
                ConditionExpression=(
                    "direction <> :flat AND attribute_not_exists(partial_exit_order_id)"
                ),
                ExpressionAttributeValues={
                    ":poid":  {"S": partial_exit_order_id},
                    ":state": {"S": "PARTIAL_PROFIT_BOOKED"},
                    ":flat":  {"S": "FLAT"},
                },
            )
        except self._dynamo.exceptions.ConditionalCheckFailedException:
            # Already partially exited (idempotent skip)
            logger.debug(
                "tee.partial_exit.already_booked",
                symbol=symbol,
                partial_exit_order_id=partial_exit_order_id,
            )
            return False
        except Exception:
            logger.exception("tee.partial_exit.dynamo_write_failed", symbol=symbol)
            return False

        # Fire partial exit via the standard router path
        request = ExitOrderRequest(
            exit_id=partial_exit_order_id,
            symbol=symbol,
            market="NSE",
            position_direction=direction,
            close_side="SELL" if direction == "LONG" else "BUY",
            close_qty=float(partial_qty),
            trigger_type=ExitTriggerType.PARTIAL_PROFIT,
            order_type="LIMIT" if last_price else "MARKET",
            exit_price=last_price,
            avg_entry_price=position["avg_price"],
        )

        logger.info(
            "tee.firing_partial_exit",
            symbol=symbol,
            direction=direction,
            partial_qty=partial_qty,
            exit_price=last_price,
            partial_oid=partial_exit_order_id,
        )
        await self._router.route(request)
        return True

    # ── DynamoDB write helpers ────────────────────────────────────────────────

    async def _write_position_update(
        self,
        symbol: str,
        updates: dict[str, Any],
    ) -> None:
        """
        Write arbitrary field updates to a position using UpdateExpression.

        All writes are conditional on direction <> FLAT (prevents writes to
        already-closed positions).
        """
        from shared.risk_state import position_key  # noqa: PLC0415

        set_clauses = []
        expr_values: dict[str, Any] = {":flat": {"S": "FLAT"}}
        for i, (field, value) in enumerate(updates.items()):
            placeholder = f":v{i}"
            set_clauses.append(f"{field} = {placeholder}")
            if isinstance(value, str):
                expr_values[placeholder] = {"S": value}
            elif isinstance(value, bool):
                expr_values[placeholder] = {"BOOL": value}
            elif isinstance(value, (int, float)):
                expr_values[placeholder] = {"N": str(value)}
            else:
                expr_values[placeholder] = {"S": str(value)}

        if not set_clauses:
            return

        try:
            await asyncio.to_thread(
                self._dynamo.update_item,
                TableName=self._positions_table,
                Key=position_key(symbol),
                UpdateExpression=f"SET {', '.join(set_clauses)}",
                ConditionExpression="direction <> :flat",
                ExpressionAttributeValues=expr_values,
            )
        except self._dynamo.exceptions.ConditionalCheckFailedException:
            logger.debug("tee.position_update.already_flat", symbol=symbol)
        except Exception:
            logger.exception("tee.position_update.failed", symbol=symbol, updates=updates)

    # ── Time helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _hold_minutes(entry_time_str: str) -> Optional[float]:
        """
        Compute how many minutes a position has been held.

        entry_time_str is expected to be an ISO 8601 datetime string with timezone.
        Returns None if parsing fails.
        """
        try:
            entry_dt = datetime.fromisoformat(entry_time_str)
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return (now - entry_dt).total_seconds() / 60.0
        except Exception:
            return None

    @staticmethod
    def _past_hard_exit_time(hard_exit_time: str) -> bool:
        """
        Return True if the current IST time is at or past the hard_exit_time.

        hard_exit_time must be "HH:MM" format in IST (UTC+05:30).
        Returns False on parse failure (fail-safe: do not exit unexpectedly).
        """
        try:
            hh, mm = hard_exit_time.split(":")
            now_ist = datetime.now(_IST)
            exit_time = now_ist.replace(
                hour=int(hh), minute=int(mm), second=0, microsecond=0
            )
            return now_ist >= exit_time
        except Exception:
            return False

    # ── Exit dispatch ─────────────────────────────────────────────────────────

    async def _fire_exit(
        self,
        position: dict,
        trigger: ExitTriggerType,
        exit_price: Optional[float],
    ) -> None:
        """Build and route an ExitOrderRequest via ExitOrderRouter."""
        direction = position["direction"]
        request = ExitOrderRequest.from_position(
            symbol=position["symbol"],
            market="NSE",
            direction=direction,
            signed_quantity=position["quantity"],
            trigger_type=trigger,
            session_date_ist=_session_date_ist(),
            avg_entry_price=position["avg_price"],
            exit_price=exit_price,
        )
        logger.info(
            "tee.firing_exit",
            symbol=position["symbol"],
            direction=direction,
            trigger=trigger.value,
            close_side=request.close_side,
            close_qty=request.close_qty,
            product_type=request.product_type,
            exit_id=request.exit_id,
            exit_state=position.get("exit_state"),
        )

        if self._live_counters is not None:
            c = self._live_counters
            if trigger == ExitTriggerType.STOP_LOSS:
                c.tee_stop_loss_hits += 1
            elif trigger == ExitTriggerType.TAKE_PROFIT:
                c.tee_take_profit_hits += 1
            elif trigger == ExitTriggerType.TRAILING:
                c.tee_trailing_hits += 1

            from datetime import timedelta as _td  # noqa: PLC0415
            now_ist = datetime.now(timezone(_td(seconds=19800))).strftime("%Y-%m-%d %H:%M:%S IST")
            signed_qty = int(request.close_qty) if request.close_side == "BUY" else -int(request.close_qty)
            event_str = (
                f"{now_ist} | {position['symbol']:<10} | {trigger.value:<18} | "
                f"exit={exit_price or position['avg_price']:.2f}  | "
                f"qty={signed_qty:+d}"
            )
            c.tee_latest_events = (c.tee_latest_events or [])[-9:] + [event_str]

        await self._router.route(request)

    # ── Price source ──────────────────────────────────────────────────────────

    async def _get_last_price(self, symbol: str, position: dict) -> Optional[float]:
        """
        Resolve the current LTP for *symbol* via LtpResolver.

        Priority (handled by LtpResolver):
            1. prices table (QUOTE#NSE/{symbol}/LATEST) — checked for freshness via
               captured_at field written by LiveQuotePoller.
            2. position's last_price (entry fill price) — always is_stale=True.

        Logs a warning when the price is stale so the operator can see that exit
        decisions are being made against non-current prices.
        """
        result = await self._ltp_resolver.resolve(
            symbol,
            position_fill_price=position.get("last_price"),
        )
        if result is None:
            return None

        if result.is_stale:
            age = result.age_seconds or float("inf")
            # In LIVE mode: block exit evaluation when LTP is too stale.
            # Executing a stop-loss against a 30-second-old price is dangerous.
            # In PAPER mode: warn only — stale exits are non-monetary.
            if (
                self._router.mode.value == "live"
                and age > self._max_stale_ltp_live
            ):
                logger.critical(
                    "tee.stale_ltp_blocked_live",
                    symbol=symbol,
                    source=result.source,
                    age_seconds=age,
                    max_allowed_seconds=self._max_stale_ltp_live,
                    detail=(
                        "Exit evaluation BLOCKED: LTP is stale beyond live threshold. "
                        "LiveQuotePoller may be offline. Check execution_engine logs."
                    ),
                )
                if self._live_counters is not None:
                    self._live_counters.tee_stale_ltp_blocks += 1
                return None
            logger.warning(
                "tee.stale_ltp",
                symbol=symbol,
                source=result.source,
                age_seconds=age,
                price=result.price,
            )
        else:
            logger.debug(
                "tee.ltp_resolved",
                symbol=symbol,
                source=result.source,
                age_seconds=result.age_seconds,
                price=result.price,
            )
        return result.price
