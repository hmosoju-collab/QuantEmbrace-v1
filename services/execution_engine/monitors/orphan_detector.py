"""
OrphanDetector — detects FILLED entry positions that lack an active stop-loss order.

Phase 8 (ADR-015 F8): orphan position detection.

An "orphan" is a FILLED entry order whose linked protective child (stop-loss) is
either missing from DynamoDB, in a terminal state (CANCELLED / REJECTED / FILLED),
or was never placed.  This situation leaves the position unprotected against
adverse moves.

Detection mechanism:
    1. Fetch all FILLED orders from DynamoDB that are NOT themselves protective
       children (i.e., entry orders).
    2. For each entry order, look up the linked protective_stop_order_id.
    3. If no child is linked, or the child is in a terminal state, log CRITICAL
       and fire the on_orphan callback.

Policy (ADR-015 §5.4):
    This detector ALERTS ONLY.  It does NOT auto-flatten or place new orders.
    Auto-flattening on orphan detection was rejected because:
      a. The protective stop may be in-flight at the broker but not yet reflected
         in DynamoDB (race condition on high-throughput fill bursts).
      b. Market orders at unknown prices during a partial-data window could create
         additional exposure rather than reducing it.
    The operator must decide whether to place a new stop, flatten manually, or
    accept the risk.  CloudWatch metric ``OrphanPositionDetected`` is emitted on
    every cycle where orphans exist so the alarm fires within 1 minute.

Recovery:
    1. Investigate why the protective stop is missing (check execution logs).
    2. Place a manual stop via the Zerodha Kite app or via the CLI.
    3. OR run: python scripts/ops/reconcile.py --set-required --reason orphan_detected
       to halt new signal intake while you investigate.

Usage (wired via ExecutionService.start()):
    detector = OrphanDetector(
        order_manager=self._order_manager,
        metrics=get_metrics_client(),
        on_orphan=self._handle_orphan_detected,
    )
    await asyncio.gather(..., detector.run())
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.metrics.cloudwatch_metrics import CloudWatchMetrics as CloudWatchMetricsClient

from execution_engine.orders.order import OrderStatus, StoredOrder
from execution_engine.orders.order_manager import OrderManager

logger = get_logger(__name__, service_name="execution_engine")

# ── Constants ──────────────────────────────────────────────────────────────────

_CHECK_INTERVAL_SECONDS: float = 30.0
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value, OrderStatus.FILLED.value}
)
_OPEN_STATUSES: frozenset[str] = frozenset(
    {
        OrderStatus.PENDING.value,
        OrderStatus.PLACED.value,
        OrderStatus.PARTIALLY_FILLED.value,
        "ACK_UNKNOWN",
    }
)

METRIC_NAMESPACE = "QuantEmbrace/ExecutionEngine"
METRIC_ORPHAN_COUNT = "OrphanPositionDetected"


class OrphanDetector:
    """
    Background task that scans FILLED entry orders for missing protective stops.

    Runs in a 30-second polling loop.  On each cycle:
      1. Fetches all FILLED orders that are entry orders (not protective children).
      2. For each entry, checks whether a live protective stop child exists.
      3. Logs CRITICAL + emits CloudWatch metric for any orphan found.
      4. Calls the optional ``on_orphan`` callback (used for testing / alerting).

    This detector is intentionally read-only.  No orders are placed or cancelled.
    ADR-015 §5.4 forbids auto-flattening on orphan detection.

    Args:
        order_manager:      DynamoDB-backed order state store.
        metrics:            CloudWatch metrics client for ``OrphanPositionDetected``.
        on_orphan:          Optional callback invoked with (orphan_order, reason)
                            for each detected orphan. May be a coroutine function.
        check_interval_s:   Seconds between scans. Default 30.
        settings:           App settings.
    """

    def __init__(
        self,
        order_manager: OrderManager,
        metrics: Optional[CloudWatchMetricsClient] = None,
        on_orphan: Optional[Callable[[StoredOrder, str], Any]] = None,
        check_interval_s: float = _CHECK_INTERVAL_SECONDS,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._order_manager   = order_manager
        self._metrics         = metrics
        self._on_orphan       = on_orphan
        self._interval        = check_interval_s
        self._settings        = settings or get_settings()
        self._running         = False
        self._consecutive_errors: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the orphan detection loop (runs until stop() is called)."""
        self._running = True
        logger.info(
            "orphan_detector.started",
            check_interval_s=self._interval,
        )
        await self._loop()

    async def stop(self) -> None:
        """Signal the loop to exit after the current sleep or cycle completes."""
        self._running = False
        logger.info("orphan_detector.stopped")

    async def run(self) -> None:
        """Alias for start(); allows use with asyncio.gather()."""
        await self.start()

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            cycle_start = asyncio.get_event_loop().time()
            try:
                await self._check_cycle()
                self._consecutive_errors = 0
            except Exception:
                self._consecutive_errors += 1
                logger.exception(
                    "orphan_detector.cycle_error",
                    consecutive_errors=self._consecutive_errors,
                )

            elapsed    = asyncio.get_event_loop().time() - cycle_start
            sleep_time = max(0.0, self._interval - elapsed)
            if sleep_time > 0 and self._running:
                await asyncio.sleep(sleep_time)

    # ── Detection cycle ────────────────────────────────────────────────────────

    async def _check_cycle(self) -> None:
        """
        Single orphan-detection pass.

        Fetches all FILLED entry orders from DynamoDB, then checks each one for
        a live protective stop child.  Emits metrics and logs for any orphans.
        """
        entry_orders = await self._get_filled_entry_orders()
        if not entry_orders:
            return

        orphan_count = 0

        for order in entry_orders:
            reason = await self._classify_orphan(order)
            if reason is None:
                continue

            orphan_count += 1
            logger.critical(
                "orphan_detector.orphan_found",
                order_id=order.order_id,
                symbol=order.symbol,
                quantity=order.filled_quantity,
                side=order.side.value if hasattr(order.side, "value") else str(order.side),
                protective_stop_id=getattr(order, "protective_stop_order_id", ""),
                reason=reason,
                action="alert_only_no_auto_flatten",
                remediation=(
                    "Place a manual stop via Kite app or run: "
                    "python scripts/ops/reconcile.py --set-required --reason orphan_detected"
                ),
            )

            if self._on_orphan is not None:
                try:
                    maybe_coro = self._on_orphan(order, reason)
                    if asyncio.iscoroutine(maybe_coro):
                        await maybe_coro
                except Exception:
                    logger.exception(
                        "orphan_detector.callback_error",
                        order_id=order.order_id,
                    )

        if orphan_count > 0:
            self._emit_metric(orphan_count)
            logger.warning(
                "orphan_detector.cycle_summary",
                entry_orders_checked=len(entry_orders),
                orphans_found=orphan_count,
            )

    # ── Classification helpers ────────────────────────────────────────────────

    async def _get_filled_entry_orders(self) -> list[StoredOrder]:
        """
        Return all FILLED orders that are entry orders (not protective children).

        Uses get_open_orders() for active states only, but we need FILLED.
        Falls back to a DynamoDB query for FILLED status via the status-index GSI.
        """
        try:
            filled = await self._order_manager.get_orders_by_status(
                OrderStatus.FILLED.value
            )
        except AttributeError:
            # Fallback: get_orders_by_status may not exist on older OrderManager.
            # In that case return empty list and let the operator upgrade.
            logger.warning(
                "orphan_detector.missing_get_orders_by_status",
                message="OrderManager.get_orders_by_status not available — orphan scan skipped",
            )
            return []
        except Exception:
            logger.exception("orphan_detector.filled_orders_fetch_error")
            return []

        return [
            o for o in filled
            if not o.is_protective
            and not o.order_id.startswith("PAPER-")
        ]

    async def _classify_orphan(self, order: StoredOrder) -> Optional[str]:
        """
        Determine if ``order`` is an orphan and return a reason string, or None.

        Checks:
          1. No ``protective_stop_order_id`` recorded on the entry order.
          2. The recorded protective stop is in a terminal state.
        """
        child_id: str = getattr(order, "protective_stop_order_id", "") or ""

        # No protective child ever linked — always an orphan.
        if not child_id:
            return "no_protective_stop_linked"

        # Look up the child order to check its current status.
        try:
            child = await self._order_manager.get_stored_order(child_id)
        except Exception:
            logger.exception(
                "orphan_detector.child_lookup_error",
                order_id=order.order_id,
                child_id=child_id,
            )
            # Can't confirm child state — don't raise a false alarm.
            return None

        if child is None:
            return f"protective_stop_missing_from_dynamo child_id={child_id}"

        child_status = (
            child.status.value
            if hasattr(child.status, "value")
            else str(child.status)
        ).upper()

        if child_status in _TERMINAL_STATUSES:
            return f"protective_stop_terminal status={child_status} child_id={child_id}"

        # Child is in an open/active state — position is protected.
        return None

    # ── Metrics ──────────────────────────────────────────────────────────────

    def _emit_metric(self, count: int) -> None:
        """Emit ``OrphanPositionDetected`` count to CloudWatch."""
        if self._metrics is None:
            return
        try:
            self._metrics.record_count(
                METRIC_ORPHAN_COUNT,
                value=float(count),
                dimensions={"Service": "execution_engine"},
            )
        except Exception:
            logger.exception("orphan_detector.metric_emit_error")
