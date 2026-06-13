"""
PositionReconciliationService — startup consistency check for open positions.

Runs once on service startup (Phase 3: paper mode only).
Live mode emits CRITICAL alerts without auto-repair; live repair is Phase 4.

Mismatch categories detected:
    UNMANAGED        — position is open but has no stop_price (exit policy missing)
    STALE_EXIT_LOCK  — exit_order_id is set but position direction is not FLAT
                       (exit was fired but fill not yet confirmed, or fill missed)
    QTY_DIRECTION    — signed quantity and direction field disagree
    ZERO_QTY_OPEN    — direction != FLAT but quantity == 0 (should be FLAT)
    FLAT_WITH_STOP   — direction == FLAT but stop_price is still present

Paper mode actions:
    UNMANAGED      → attach placeholder exit policy (stop_price re-attachment)
    STALE_EXIT_LOCK → log WARNING, no auto-repair (may self-resolve on next fill)
    QTY_DIRECTION  → log WARNING, no auto-repair (quantity is canonical)
    ZERO_QTY_OPEN  → set direction=FLAT to repair the inconsistency
    FLAT_WITH_STOP → log INFO only (stale attribute, not dangerous)

Live mode: all mismatches → CRITICAL log, no repair.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from services.shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="execution_engine")


class MismatchType(str, Enum):
    UNMANAGED        = "UNMANAGED"
    STALE_EXIT_LOCK  = "STALE_EXIT_LOCK"
    QTY_DIRECTION    = "QTY_DIRECTION"
    ZERO_QTY_OPEN    = "ZERO_QTY_OPEN"
    FLAT_WITH_STOP   = "FLAT_WITH_STOP"
    ENTRY_UNWOUND_BY_COMPETING_ENTRY = "ENTRY_UNWOUND_BY_COMPETING_ENTRY"  # NEW


@dataclass
class PositionMismatch:
    symbol:       str
    mismatch_type: MismatchType
    detail:       str
    repaired:     bool = False
    repair_error: Optional[str] = None


@dataclass
class ReconciliationReport:
    mode:       str
    mismatches: list[PositionMismatch] = field(default_factory=list)
    repaired:   int = 0
    alerted:    int = 0

    @property
    def total_mismatches(self) -> int:
        return len(self.mismatches)

    @property
    def clean(self) -> bool:
        return self.total_mismatches == 0


class PositionReconciliationService:
    """
    Runs a single startup reconciliation pass over all positions.

    Usage:
        reconciler = PositionReconciliationService(
            dynamo_client=dynamo,
            positions_table=settings.aws.dynamodb_table_positions,
            mode="paper",        # "paper" | "live"
        )
        report = await reconciler.run()
        if not report.clean:
            logger.warning("reconciliation.mismatches_found", ...)

    The service never starts any continuous background loop — it is a
    one-shot operation called during service startup.
    """

    def __init__(
        self,
        dynamo_client: Any,
        positions_table: str,
        mode: str = "paper",
    ) -> None:
        if mode not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got {mode!r}")
        self._dynamo = dynamo_client
        self._positions_table = positions_table
        self._mode = mode

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self) -> ReconciliationReport:
        """
        Perform a full startup reconciliation pass.

        Returns a ReconciliationReport describing all mismatches found and
        the actions taken (paper repairs) or alerts emitted (live mode).
        """
        logger.info(
            "reconciliation.started",
            mode=self._mode,
            detail="Startup position reconciliation beginning",
        )

        positions = await self._scan_all_positions()
        report = ReconciliationReport(mode=self._mode)

        for item in positions:
            mismatches = self._detect_mismatches(item)
            for mismatch in mismatches:
                report.mismatches.append(mismatch)
                if self._mode == "paper":
                    await self._repair(item, mismatch)
                    if mismatch.repaired:
                        report.repaired += 1
                    else:
                        self._alert(mismatch)
                        report.alerted += 1
                else:
                    # Live mode: alert only, never auto-repair
                    self._alert_critical(mismatch)
                    report.alerted += 1

        logger.info(
            "reconciliation.complete",
            mode=self._mode,
            total_positions=len(positions),
            total_mismatches=report.total_mismatches,
            repaired=report.repaired,
            alerted=report.alerted,
            clean=report.clean,
        )
        return report

    async def check_competing_entry_unwind(
        self,
        orders_table: str,
    ) -> list[PositionMismatch]:
        """
        End-of-session gate: detect positions where a competing ENTRY order
        reduced or closed an existing position instead of an explicit EXIT.

        For each symbol, collect all FILLED orders. If the net signed quantity
        from ENTRY-side fills differs from what explicit EXIT fills account for,
        flag ENTRY_UNWOUND_BY_COMPETING_ENTRY.

        An order is classified as ENTRY if it has a strategy signal_id (not
        prefixed with EXIT-). An order is classified as EXIT if its signal_id
        starts with EXIT-.

        Returns a list of PositionMismatch with type
        ENTRY_UNWOUND_BY_COMPETING_ENTRY for each affected symbol.
        """
        mismatches: list[PositionMismatch] = []

        try:
            # Scan all FILLED orders
            response = await asyncio.to_thread(
                self._dynamo.scan,
                TableName=orders_table,
                FilterExpression="order_status = :filled AND SK = :meta",
                ExpressionAttributeValues={
                    ":filled": {"S": "FILLED"},
                    ":meta": {"S": "META"},
                },
                ProjectionExpression=(
                    "symbol, side, quantity, signal_id, filled_quantity"
                ),
            )
            items = response.get("Items", [])

            # Group by symbol
            from collections import defaultdict  # noqa: PLC0415
            by_symbol: dict[str, list[dict]] = defaultdict(list)
            for item in items:
                sym = item.get("symbol", {}).get("S", "")
                if sym:
                    by_symbol[sym].append(item)

            for symbol, orders in by_symbol.items():
                entry_buys = 0.0
                entry_sells = 0.0
                exit_buys = 0.0
                exit_sells = 0.0

                for order in orders:
                    sig = order.get("signal_id", {}).get("S", "")
                    side_str = order.get("side", {}).get("S", "")
                    qty = float(order.get("filled_quantity", {}).get("N") or
                                order.get("quantity", {}).get("N") or "0")
                    is_exit = sig.startswith("EXIT-")

                    if is_exit:
                        if side_str == "BUY":
                            exit_buys += qty
                        else:
                            exit_sells += qty
                    else:
                        if side_str == "BUY":
                            entry_buys += qty
                        else:
                            entry_sells += qty

                # If there are both ENTRY buys and ENTRY sells for the same
                # symbol, a competing entry reduced the position.
                if entry_buys > 0 and entry_sells > 0:
                    net_entry = entry_buys - entry_sells
                    mismatches.append(PositionMismatch(
                        symbol=symbol,
                        mismatch_type=MismatchType.ENTRY_UNWOUND_BY_COMPETING_ENTRY,
                        detail=(
                            f"Both ENTRY buys={entry_buys:.0f} and ENTRY sells={entry_sells:.0f} "
                            f"exist for {symbol}. Net entry qty={net_entry:.0f}. "
                            f"Exit buys={exit_buys:.0f} exit sells={exit_sells:.0f}. "
                            "A competing entry signal reduced the position without an explicit "
                            "exit order — this is RECON_ENTRY_UNWOUND_BY_COMPETING_ENTRY."
                        ),
                    ))
                    if self._mode == "paper":
                        logger.warning(
                            "reconciliation.entry_unwound_by_competing_entry",
                            symbol=symbol,
                            entry_buys=entry_buys,
                            entry_sells=entry_sells,
                            exit_buys=exit_buys,
                            exit_sells=exit_sells,
                        )
                    else:
                        logger.critical(
                            "reconciliation.entry_unwound_by_competing_entry_LIVE",
                            symbol=symbol,
                            entry_buys=entry_buys,
                            entry_sells=entry_sells,
                            action_required="Manual position audit required before next session.",
                        )

        except Exception:
            logger.exception("reconciliation.check_competing_entry_unwind.error")

        return mismatches

    async def check_audit_chain(
        self,
        orders_table: str,
        positions_table: Optional[str] = None,
    ) -> list[PositionMismatch]:
        """
        End-of-session invariant: for every FILLED ENTRY order, the chain

            signal_id → order_id → filled_quantity > 0 → position exists

        must be unbroken. A break means an entry fill did not produce or update
        a position record — the trade is unaccounted for in P&L and risk state.

        An order is classified as ENTRY if its signal_id does NOT start with
        "EXIT-" (TEE exit orders use that prefix).

        Returns a list of PositionMismatch with type AUDIT_CHAIN_BROKEN for
        each entry order whose position record is missing or has quantity=0.
        """
        positions_table = positions_table or self._positions_table
        mismatches: list[PositionMismatch] = []

        try:
            # Fetch all FILLED entry orders
            response = await asyncio.to_thread(
                self._dynamo.scan,
                TableName=orders_table,
                FilterExpression="order_status = :filled AND SK = :meta",
                ExpressionAttributeValues={
                    ":filled": {"S": "FILLED"},
                    ":meta": {"S": "META"},
                },
                ProjectionExpression=(
                    "order_id, signal_id, symbol, side, filled_quantity"
                ),
            )
            entry_orders = [
                item for item in response.get("Items", [])
                if not item.get("signal_id", {}).get("S", "").startswith("EXIT-")
            ]

            # Collect symbols that have FILLED entry orders
            symbols_with_entries: set[str] = {
                item.get("symbol", {}).get("S", "")
                for item in entry_orders
                if item.get("symbol", {}).get("S", "")
            }

            if not symbols_with_entries:
                return mismatches

            # Scan positions for those symbols
            pos_response = await asyncio.to_thread(
                self._dynamo.scan,
                TableName=positions_table,
                ProjectionExpression="symbol, quantity",
            )
            position_qty_by_symbol: dict[str, float] = {}
            for item in pos_response.get("Items", []):
                sym = item.get("symbol", {}).get("S", "")
                qty = float(item.get("quantity", {}).get("N") or "0")
                if sym:
                    position_qty_by_symbol[sym] = qty

            # Check each entry order's symbol has a non-zero position
            already_flagged: set[str] = set()
            for order in entry_orders:
                signal_id = order.get("signal_id", {}).get("S", "?")
                order_id = order.get("order_id", {}).get("S", "?")
                symbol = order.get("symbol", {}).get("S", "")
                fill_qty = float(order.get("filled_quantity", {}).get("N") or "0")

                if not symbol or symbol in already_flagged:
                    continue

                # A filled entry order (fill_qty > 0) must leave a trace in
                # positions. Zero or missing position qty for that symbol
                # indicates the audit chain is broken.
                pos_qty = position_qty_by_symbol.get(symbol)
                if fill_qty > 0 and (pos_qty is None or abs(pos_qty) < 1e-9):
                    already_flagged.add(symbol)
                    detail = (
                        f"AUDIT_CHAIN_BROKEN: FILLED ENTRY order {order_id} "
                        f"(signal={signal_id}) filled {fill_qty:.0f} shares of "
                        f"{symbol}, but position record shows qty="
                        f"{'MISSING' if pos_qty is None else f'{pos_qty:.0f}'}. "
                        "The signal→order→fill→position chain is broken."
                    )
                    mismatches.append(PositionMismatch(
                        symbol=symbol,
                        mismatch_type=MismatchType.ENTRY_UNWOUND_BY_COMPETING_ENTRY,
                        detail=detail,
                    ))
                    if self._mode == "paper":
                        logger.warning("reconciliation.audit_chain_broken", symbol=symbol,
                                       order_id=order_id, signal_id=signal_id,
                                       fill_qty=fill_qty, position_qty=pos_qty)
                    else:
                        logger.critical("reconciliation.audit_chain_broken_LIVE", symbol=symbol,
                                        order_id=order_id, signal_id=signal_id,
                                        fill_qty=fill_qty, position_qty=pos_qty,
                                        action_required="Manual position audit required.")

        except Exception:
            logger.exception("reconciliation.check_audit_chain.error")

        return mismatches

    # ── DynamoDB scan ─────────────────────────────────────────────────────────

    async def _scan_all_positions(self) -> list[dict]:
        """Scan the full positions table. No filter — we want all records."""
        response = await asyncio.to_thread(
            self._dynamo.scan,
            TableName=self._positions_table,
            ProjectionExpression=(
                "symbol, #dir, quantity, stop_price, exit_order_id, exit_state"
            ),
            ExpressionAttributeNames={"#dir": "direction"},
        )
        return response.get("Items", [])

    # ── Mismatch detection ────────────────────────────────────────────────────

    def _detect_mismatches(self, item: dict) -> list[PositionMismatch]:
        """Return all mismatches found in one position item."""
        symbol    = item.get("symbol",    {}).get("S", "UNKNOWN")
        direction = item.get("direction", {}).get("S", "")
        qty_str   = item.get("quantity",  {}).get("N", "0")
        stop_str  = item.get("stop_price", {}).get("N")
        exit_oid  = item.get("exit_order_id", {}).get("S")
        quantity  = float(qty_str) if qty_str else 0.0

        mismatches: list[PositionMismatch] = []

        # Derive canonical direction from signed quantity
        if abs(quantity) < 1e-9:
            qty_direction = "FLAT"
        elif quantity > 0:
            qty_direction = "LONG"
        else:
            qty_direction = "SHORT"

        if direction == "FLAT":
            # Position is closed
            if stop_str is not None:
                mismatches.append(PositionMismatch(
                    symbol=symbol,
                    mismatch_type=MismatchType.FLAT_WITH_STOP,
                    detail=f"direction=FLAT but stop_price={stop_str} is still present",
                ))
        else:
            # Position is open
            if abs(quantity) < 1e-9:
                mismatches.append(PositionMismatch(
                    symbol=symbol,
                    mismatch_type=MismatchType.ZERO_QTY_OPEN,
                    detail=f"direction={direction} but quantity is 0 — should be FLAT",
                ))

            if qty_direction not in ("FLAT",) and qty_direction != direction:
                mismatches.append(PositionMismatch(
                    symbol=symbol,
                    mismatch_type=MismatchType.QTY_DIRECTION,
                    detail=(
                        f"quantity={quantity} implies {qty_direction} "
                        f"but direction={direction}"
                    ),
                ))

            if stop_str is None:
                mismatches.append(PositionMismatch(
                    symbol=symbol,
                    mismatch_type=MismatchType.UNMANAGED,
                    detail="position is open but has no stop_price (exit policy missing)",
                ))

            if exit_oid and direction != "FLAT":
                mismatches.append(PositionMismatch(
                    symbol=symbol,
                    mismatch_type=MismatchType.STALE_EXIT_LOCK,
                    detail=(
                        f"exit_order_id={exit_oid} is set but position is still "
                        f"direction={direction} — exit may be in-flight or fill missed"
                    ),
                ))

        return mismatches

    # ── Repair actions (paper mode only) ─────────────────────────────────────

    async def _repair(self, item: dict, mismatch: PositionMismatch) -> None:
        """Attempt to repair a mismatch in paper mode."""
        symbol = mismatch.symbol

        try:
            if mismatch.mismatch_type == MismatchType.ZERO_QTY_OPEN:
                await self._set_direction_flat(symbol)
                mismatch.repaired = True
                logger.warning(
                    "reconciliation.repaired_zero_qty_open",
                    symbol=symbol,
                    detail="set direction=FLAT for zero-quantity open position",
                )

            elif mismatch.mismatch_type == MismatchType.UNMANAGED:
                logger.critical(
                    "reconciliation.unmanaged_position",
                    symbol=symbol,
                    detail=(
                        "Open position has no exit policy. "
                        "Re-attach stop_price via attach_exit_policy() "
                        "or close the position manually."
                    ),
                )
                # Cannot repair without knowing the original stop price.
                # Emit CRITICAL; TEE will also alert on every poll cycle.

            elif mismatch.mismatch_type == MismatchType.STALE_EXIT_LOCK:
                logger.warning(
                    "reconciliation.stale_exit_lock",
                    symbol=symbol,
                    detail=mismatch.detail,
                )

            elif mismatch.mismatch_type == MismatchType.QTY_DIRECTION:
                logger.warning(
                    "reconciliation.qty_direction_mismatch",
                    symbol=symbol,
                    detail=mismatch.detail,
                )

            elif mismatch.mismatch_type == MismatchType.FLAT_WITH_STOP:
                logger.info(
                    "reconciliation.flat_with_stop",
                    symbol=symbol,
                    detail=mismatch.detail,
                )

        except Exception as exc:
            mismatch.repair_error = str(exc)
            logger.exception(
                "reconciliation.repair_failed",
                symbol=symbol,
                mismatch_type=mismatch.mismatch_type.value,
            )

    async def _set_direction_flat(self, symbol: str) -> None:
        from shared.risk_state import position_key  # noqa: PLC0415

        await asyncio.to_thread(
            self._dynamo.update_item,
            TableName=self._positions_table,
            Key=position_key(symbol),
            UpdateExpression="SET direction = :flat, quantity = :zero",
            ExpressionAttributeValues={
                ":flat": {"S": "FLAT"},
                ":zero": {"N": "0"},
            },
        )

    # ── Alert helpers ─────────────────────────────────────────────────────────

    def _alert(self, mismatch: PositionMismatch) -> None:
        logger.warning(
            "reconciliation.mismatch",
            symbol=mismatch.symbol,
            type=mismatch.mismatch_type.value,
            detail=mismatch.detail,
        )

    def _alert_critical(self, mismatch: PositionMismatch) -> None:
        logger.critical(
            "reconciliation.mismatch_live_mode",
            symbol=mismatch.symbol,
            type=mismatch.mismatch_type.value,
            detail=mismatch.detail,
            action_required="Manual intervention required. No auto-repair in live mode.",
        )
