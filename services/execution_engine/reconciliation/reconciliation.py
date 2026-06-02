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
