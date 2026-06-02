"""
Position Monitor — Ground-truth position state via ``kite.positions()``.

Polls ``kite.positions()`` at a phase-adaptive interval (1–10s) to provide
a source of truth for NSE positions that is independent of fill detection.

Why this is needed even with BulkOrderPoller:
---------------------------------------------
``BulkOrderPoller`` detects fills by watching ``kite.orders()``.  It works
well for orders placed through QuantEmbrace.  But positions can change from
outside the system:

1. **Zerodha auto-square-off (MIS)**: At ~15:15 IST Zerodha force-closes all
   MIS (intraday) positions.  This happens broker-side without any order being
   visible to us at the time.
2. **Manual orders from Kite app**: Operator places an order from phone/web.
   The order appears in ``kite.orders()`` but has no matching DynamoDB record
   — the system will miss the fill unless PositionMonitor catches the drift.
3. **Partial fill gaps**: ``BulkOrderPoller`` tracks cumulative filled_quantity
   across polling cycles.  In rare cases (network gap during bulk fetch) an
   intermediate partial fill can be missed.  ``PositionMonitor`` reconciles
   actual broker quantities with our DynamoDB state.

Detection mechanism:
    For each position returned by ``kite.positions()``:
      - Compare broker quantity with DynamoDB quantity
      - If drift detected: log CRITICAL + activate the kill switch by default
      - If configured (position_monitor_auto_sync=True): write broker state
        to DynamoDB as ground truth

Polling interval (phase-adaptive):
    MARKET_OPEN   1.0s   — fast fill detection needed at the open
    NORMAL        2.0s   — balanced; 0.5 req/sec on rate budget
    PRE_CLOSE     1.0s   — watch for MIS auto-square-offs
    CLOSING       2.0s
    POST_CLOSE   10.0s   — end-of-day check only
    PRE_OPEN      5.0s   — pre-market reconciliation
    PRE_AUCTION   2.0s

Rate cost: 0.5–1.0 req/sec (within MEDIUM priority budget in PHASE_BUDGET).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.utils.helpers import utc_iso
from shared.zerodha.market_phase import MarketPhase, MarketPhaseGovernor
from shared.zerodha.rate_limiter import EndpointClass, Priority, ZerodhaRateLimiter

from execution_engine.brokers.zerodha_broker import ZerodhaBrokerClient
from execution_engine.orders.order_manager import OrderManager

logger = get_logger(__name__, service_name="execution_engine")

# ── Phase-adaptive poll intervals (seconds) ───────────────────────────────────

_POLL_INTERVAL: dict[MarketPhase, float] = {
    MarketPhase.PRE_OPEN:    5.0,
    MarketPhase.PRE_AUCTION: 2.0,
    MarketPhase.MARKET_OPEN: 1.0,
    MarketPhase.NORMAL:      2.0,
    MarketPhase.PRE_CLOSE:   1.0,   # MIS auto-square-off window
    MarketPhase.CLOSING:     2.0,
    MarketPhase.POST_CLOSE:  10.0,
}

_MAX_CONSECUTIVE_ERRORS: int = 10


class PositionMonitor:
    """
    Ground-truth position state monitor via ``kite.positions()`` bulk call.

    Runs as a background asyncio task inside ``ExecutionService``.  Polls
    Zerodha for live position data and reconciles with DynamoDB.

    On drift detection:
        - Logs a structured CRITICAL event with symbol, broker qty, and DynamoDB qty.
        - Activates the kill switch by default. Unknown live exposure is treated
          as P0 because risk checks are no longer operating on known state.
        - If ``auto_sync=True``: writes broker quantity to DynamoDB as the
          authoritative value.  Default False — operator reviews first.

    Args:
        zerodha:        Connected Zerodha broker client.
        order_manager:  Order manager with DynamoDB access.
        rate_limiter:   Shared ``ZerodhaRateLimiter`` (MEDIUM priority).
        phase_governor: Optional ``MarketPhaseGovernor`` for interval adaptation.
        auto_sync:      If True, write broker positions to DynamoDB on drift.
                        Default False — log-only mode.
        on_drift:       Optional callback called with (symbol, broker_qty,
                        dynamo_qty) when drift is detected.  Useful for
                        alerting and testing.
        settings:       App settings.
    """

    def __init__(
        self,
        zerodha: ZerodhaBrokerClient,
        order_manager: OrderManager,
        rate_limiter: ZerodhaRateLimiter,
        phase_governor: Optional[MarketPhaseGovernor] = None,
        auto_sync: bool = False,
        on_drift: Optional[Callable[[str, float, float], None]] = None,
        kill_switch: Optional[Any] = None,
        activate_kill_switch_on_drift: bool = True,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._zerodha       = zerodha
        self._order_manager = order_manager
        self._rate_limiter  = rate_limiter
        self._auto_sync     = auto_sync
        self._on_drift      = on_drift
        self._kill_switch   = kill_switch
        self._activate_kill_switch_on_drift = activate_kill_switch_on_drift
        self._settings      = settings or get_settings()
        self._phase         = MarketPhase.POST_CLOSE
        self._running       = False
        self._consecutive_errors = 0

        # Last known broker positions: {symbol: quantity}
        # Used to emit drift metrics only when the discrepancy changes.
        self._last_broker_positions: dict[str, float] = {}

        if phase_governor is not None:
            phase_governor.add_listener(self.on_phase_change)

    # ── Phase awareness ────────────────────────────────────────────────────────

    def on_phase_change(self, phase_name: str) -> None:
        """Called by ``MarketPhaseGovernor`` on phase transitions."""
        try:
            self._phase = MarketPhase(phase_name)
            logger.info(
                "position_monitor.phase_changed",
                phase=phase_name,
                new_interval_s=_POLL_INTERVAL.get(self._phase, 2.0),
            )
        except ValueError:
            logger.warning("position_monitor.unknown_phase", phase_name=phase_name)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the position monitoring loop."""
        self._running = True
        logger.info(
            "position_monitor.started",
            auto_sync=self._auto_sync,
            activate_kill_switch_on_drift=self._activate_kill_switch_on_drift,
        )
        await self._poll_loop()

    async def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        logger.info("position_monitor.stopped")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            interval   = _POLL_INTERVAL.get(self._phase, 2.0)
            cycle_start = asyncio.get_event_loop().time()

            try:
                await self._poll_cycle()
                self._consecutive_errors = 0
            except Exception:
                self._consecutive_errors += 1
                logger.exception(
                    "position_monitor.cycle_error",
                    consecutive_errors=self._consecutive_errors,
                )
                if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    logger.critical(
                        "position_monitor.stale_state",
                        detail=(
                            "Position monitor offline — DynamoDB positions "
                            "may not reflect broker reality."
                        ),
                    )

            elapsed    = asyncio.get_event_loop().time() - cycle_start
            sleep_time = max(0.0, interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    # ── Single poll cycle ─────────────────────────────────────────────────────

    async def _poll_cycle(self) -> None:
        """
        Fetch broker positions and reconcile with DynamoDB.

        One ``kite.positions()`` call returns all NSE net positions.
        For each position, compare with DynamoDB and flag any drift.
        """
        if not getattr(self._zerodha, "_connected", False):
            return
        await self._rate_limiter.acquire(Priority.MEDIUM, EndpointClass.OTHER)
        broker_positions = await self._zerodha.get_positions()

        if not broker_positions:
            # No positions — verify DynamoDB agrees
            dynamo_positions = await self._order_manager.get_all_open_positions()
            if dynamo_positions:
                logger.critical(
                    "position_monitor.unexpected_dynamo_positions",
                    dynamo_count=len(dynamo_positions),
                    detail=(
                        "DynamoDB has open positions but broker reports none. "
                        "Possible auto-square-off or manual close."
                    ),
                )
                now_iso = utc_iso()
                for position in dynamo_positions:
                    await self._handle_drift(
                        symbol=position.symbol,
                        broker_qty=0.0,
                        dynamo_qty=float(position.quantity),
                        source="broker_empty_dynamo_open",
                        now_iso=now_iso,
                    )
            self._last_broker_positions = {}
            return

        # Build broker position map: {symbol: quantity}
        broker_map: dict[str, float] = {
            pos["symbol"]: float(pos["quantity"])
            for pos in broker_positions
        }
        self._last_broker_positions = broker_map

        # Fetch our DynamoDB positions for comparison
        dynamo_positions = await self._order_manager.get_all_open_positions()
        dynamo_map: dict[str, float] = {
            pos.symbol: float(pos.quantity)
            for pos in (dynamo_positions or [])
        }

        now_iso = utc_iso()

        # Check broker → DynamoDB drift
        for symbol, broker_qty in broker_map.items():
            dynamo_qty = dynamo_map.get(symbol, 0.0)
            if abs(broker_qty - dynamo_qty) > 0.001:
                await self._handle_drift(
                    symbol=symbol,
                    broker_qty=broker_qty,
                    dynamo_qty=dynamo_qty,
                    source="broker_vs_dynamo",
                    now_iso=now_iso,
                )

        # Check DynamoDB → broker drift (positions we think we have but broker doesn't)
        for symbol, dynamo_qty in dynamo_map.items():
            if symbol not in broker_map and abs(dynamo_qty) > 0.001:
                await self._handle_drift(
                    symbol=symbol,
                    broker_qty=0.0,
                    dynamo_qty=dynamo_qty,
                    source="dynamo_only",
                    now_iso=now_iso,
                )

    # ── Drift handling ────────────────────────────────────────────────────────

    async def _handle_drift(
        self,
        symbol: str,
        broker_qty: float,
        dynamo_qty: float,
        source: str,
        now_iso: str,
    ) -> None:
        """
        Handle a detected position discrepancy.

        Always logs.  If auto_sync=True, writes broker quantity to DynamoDB.
        Calls the on_drift callback if registered (useful for testing/alerting).
        """
        logger.critical(
            "position_monitor.drift_detected",
            symbol=symbol,
            broker_qty=broker_qty,
            dynamo_qty=dynamo_qty,
            drift=round(broker_qty - dynamo_qty, 4),
            source=source,
            timestamp=now_iso,
            action="kill_switch_and_auto_sync" if self._auto_sync else "kill_switch",
        )

        if self._on_drift is not None:
            try:
                maybe_result = self._on_drift(symbol, broker_qty, dynamo_qty)
                if asyncio.iscoroutine(maybe_result):
                    await maybe_result
            except Exception:
                logger.exception("position_monitor.on_drift_callback_error")

        if self._activate_kill_switch_on_drift:
            await self._activate_kill_switch_for_drift(
                symbol=symbol,
                broker_qty=broker_qty,
                dynamo_qty=dynamo_qty,
                source=source,
                now_iso=now_iso,
            )

        if self._auto_sync:
            await self._sync_position_to_broker(symbol, broker_qty, now_iso)

    async def _activate_kill_switch_for_drift(
        self,
        *,
        symbol: str,
        broker_qty: float,
        dynamo_qty: float,
        source: str,
        now_iso: str,
    ) -> None:
        """Activate the durable kill switch when position ground truth diverges."""
        reason = (
            "POSITION_DRIFT_DETECTED "
            f"symbol={symbol} broker_qty={broker_qty} dynamo_qty={dynamo_qty} "
            f"source={source} detected_at={now_iso}"
        )
        if self._kill_switch is None:
            logger.critical(
                "position_monitor.kill_switch_unavailable",
                symbol=symbol,
                reason=reason,
            )
            return
        try:
            result = self._kill_switch.activate(
                reason=reason,
                activated_by="position_monitor",
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception(
                "position_monitor.kill_switch_activation_failed",
                symbol=symbol,
                source=source,
            )

    async def _sync_position_to_broker(
        self,
        symbol: str,
        broker_qty: float,
        now_iso: str,
    ) -> None:
        """
        Write broker quantity to DynamoDB as the authoritative position.

        Only called when auto_sync=True.  This overwrites our tracked position
        with the ground truth from Zerodha — use with care.
        """
        try:
            await self._order_manager.overwrite_position_quantity(
                symbol=symbol,
                quantity=broker_qty,
                source="position_monitor_sync",
                timestamp=now_iso,
            )
            logger.info(
                "position_monitor.synced",
                symbol=symbol,
                new_qty=broker_qty,
            )
        except Exception:
            logger.exception(
                "position_monitor.sync_failed",
                symbol=symbol,
            )

    # ── Read access for metrics exporters ─────────────────────────────────────

    def get_last_positions(self) -> dict[str, float]:
        """Return the most recently fetched broker positions snapshot."""
        return dict(self._last_broker_positions)
