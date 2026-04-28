"""
Kill Switch Auto-Triggers — background monitors that activate the kill switch
when system health thresholds are breached.

Four automatic trigger conditions are monitored here. The fifth (daily portfolio
loss) is already handled by ``DailyLossValidator`` in the risk validation pipeline.

Trigger conditions:
    1. Single-strategy loss exceeds per-strategy threshold.
    2. Order submission rate exceeds runaway threshold (e.g., >20 orders/min).
    3. Broker connectivity lost for longer than the configured timeout (default 30s).
    4. Market data feed stale for longer than the configured timeout (default 60s)
       during market hours.

Each monitor runs as an ``asyncio`` background task. The monitor is started by
``RiskEngineService.start()`` and stopped by ``RiskEngineService.stop()``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.utils.helpers import utc_now

from risk_engine.killswitch.killswitch import KillSwitch

logger = get_logger(__name__, service_name="risk_engine")

# Type alias for the heartbeat-update callback
HeartbeatCallback = Callable[[], None]


class KillSwitchMonitor:
    """
    Background task manager for all automatic kill-switch triggers.

    The monitor aggregates four health checks into independent asyncio tasks.
    Each task runs in a tight poll loop with a configurable interval and calls
    ``KillSwitch.activate()`` when its threshold is breached.

    Usage::

        monitor = KillSwitchMonitor(kill_switch=ks, settings=settings)
        await monitor.start()          # launch background tasks
        # ... service runs ...
        monitor.record_order()         # call this for every order submitted
        monitor.record_broker_ping()   # call this on each successful broker response
        monitor.record_data_tick(market="US")  # call this on each received tick
        await monitor.stop()           # cancel background tasks on shutdown
    """

    def __init__(
        self,
        kill_switch: KillSwitch,
        settings: Optional[AppSettings] = None,
        # Thresholds (override via settings or constructor for tests)
        order_rate_limit: int = 20,          # orders per minute before runaway
        order_rate_window_secs: int = 60,    # sliding window for order rate
        broker_timeout_secs: float = 30.0,   # connectivity lost threshold
        data_stale_secs: float = 60.0,       # data feed stale threshold
        strategy_loss_pct: float = 5.0,      # per-strategy loss % threshold
        poll_interval_secs: float = 5.0,     # how often each monitor checks
    ) -> None:
        self._kill_switch = kill_switch
        self._settings = settings or get_settings()

        # Configurable thresholds
        self._order_rate_limit = order_rate_limit
        self._order_rate_window = order_rate_window_secs
        self._broker_timeout = broker_timeout_secs
        self._data_stale_timeout = data_stale_secs
        self._strategy_loss_pct = strategy_loss_pct
        self._poll_interval = poll_interval_secs

        # --- State tracked by event-report methods ---

        # Sliding window: timestamps (UTC) of each order submitted
        self._order_timestamps: deque[datetime] = deque()

        # Last successful broker heartbeat (REST call or WebSocket frame)
        self._last_broker_ping: Optional[datetime] = None
        self._broker_connected: bool = False  # set True by record_broker_ping()

        # Per-market last tick timestamp
        self._last_tick: dict[str, datetime] = {}

        # Per-strategy cumulative P&L: {strategy_id: pnl_float}
        # Populated by record_strategy_pnl(); negative = loss
        self._strategy_pnl: dict[str, float] = {}
        self._strategy_capital: dict[str, float] = {}  # starting capital per strategy

        # Running background tasks
        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch all monitoring background tasks."""
        if self._running:
            logger.warning("KillSwitchMonitor already running — ignoring start()")
            return

        self._running = True
        self._tasks = [
            asyncio.create_task(self._monitor_order_rate(), name="ks_order_rate"),
            asyncio.create_task(self._monitor_broker_connectivity(), name="ks_broker_conn"),
            asyncio.create_task(self._monitor_data_staleness(), name="ks_data_stale"),
            asyncio.create_task(self._monitor_strategy_loss(), name="ks_strategy_loss"),
        ]
        logger.info("KillSwitchMonitor started (%d monitors active)", len(self._tasks))

    async def stop(self) -> None:
        """Cancel all monitoring tasks gracefully."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("KillSwitchMonitor stopped")

    # ------------------------------------------------------------------
    # Event-report methods  (called by execution engine / broker client)
    # ------------------------------------------------------------------

    def record_order(self) -> None:
        """
        Record a new order submission event.

        Must be called every time an order is submitted to a broker.
        The order-rate monitor uses this to detect runaway loops.
        """
        self._order_timestamps.append(utc_now())

    def record_broker_ping(self) -> None:
        """
        Record a successful broker connectivity event.

        Call this on every successful broker API response (REST or WebSocket
        heartbeat). A gap exceeding ``broker_timeout_secs`` triggers the kill switch.
        """
        self._last_broker_ping = utc_now()
        if not self._broker_connected:
            self._broker_connected = True
            logger.info("Broker connectivity established")

    def record_data_tick(self, market: str = "US") -> None:
        """
        Record that a live market-data tick was received.

        Args:
            market: Market identifier (``"US"`` or ``"NSE"``).
        """
        self._last_tick[market.upper()] = utc_now()

    def record_strategy_pnl(
        self, strategy_id: str, pnl: float, starting_capital: float
    ) -> None:
        """
        Update the running P&L for a strategy.

        Args:
            strategy_id: Unique identifier for the strategy.
            pnl: Current cumulative P&L (negative = loss).
            starting_capital: Capital allocated to this strategy (for % calc).
        """
        self._strategy_pnl[strategy_id] = pnl
        self._strategy_capital[strategy_id] = starting_capital

    # ------------------------------------------------------------------
    # Monitor 1 — Order rate runaway
    # ------------------------------------------------------------------

    async def _monitor_order_rate(self) -> None:
        """
        Trigger kill switch if orders/minute exceeds the runaway threshold.

        Uses a sliding window: only orders in the last ``order_rate_window_secs``
        seconds are counted.
        """
        logger.debug(
            "Order-rate monitor started (limit=%d orders/%ds)",
            self._order_rate_limit,
            self._order_rate_window,
        )
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                if self._kill_switch.active:
                    continue

                now = utc_now()
                cutoff = now.timestamp() - self._order_rate_window

                # Drain expired timestamps from the left
                while self._order_timestamps and (
                    self._order_timestamps[0].timestamp() < cutoff
                ):
                    self._order_timestamps.popleft()

                count = len(self._order_timestamps)
                if count > self._order_rate_limit:
                    reason = (
                        f"Auto-triggered: order rate runaway — "
                        f"{count} orders in {self._order_rate_window}s "
                        f"(limit={self._order_rate_limit})"
                    )
                    logger.critical("ORDER RATE RUNAWAY DETECTED: %s", reason)
                    await self._kill_switch.activate(
                        reason=reason, activated_by="order_rate_monitor"
                    )

            except asyncio.CancelledError:
                logger.debug("Order-rate monitor cancelled")
                return
            except Exception:
                logger.exception("Order-rate monitor error — continuing")

    # ------------------------------------------------------------------
    # Monitor 2 — Broker connectivity
    # ------------------------------------------------------------------

    async def _monitor_broker_connectivity(self) -> None:
        """
        Trigger kill switch if no successful broker response for >30 seconds.

        The monitor only starts checking once the first ``record_broker_ping()``
        call is received (i.e., after the broker has connected at least once).
        This prevents false positives during startup.
        """
        logger.debug(
            "Broker-connectivity monitor started (timeout=%.0fs)", self._broker_timeout
        )
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                if self._kill_switch.active:
                    continue

                # Don't fire before first connection is established
                if not self._broker_connected or self._last_broker_ping is None:
                    continue

                now = utc_now()
                elapsed = (now - self._last_broker_ping).total_seconds()

                if elapsed > self._broker_timeout:
                    reason = (
                        f"Auto-triggered: broker connectivity lost — "
                        f"no response for {elapsed:.0f}s "
                        f"(threshold={self._broker_timeout:.0f}s)"
                    )
                    logger.critical("BROKER CONNECTIVITY LOST: %s", reason)
                    await self._kill_switch.activate(
                        reason=reason, activated_by="broker_connectivity_monitor"
                    )

            except asyncio.CancelledError:
                logger.debug("Broker-connectivity monitor cancelled")
                return
            except Exception:
                logger.exception("Broker-connectivity monitor error — continuing")

    # ------------------------------------------------------------------
    # Monitor 3 — Data feed staleness
    # ------------------------------------------------------------------

    async def _monitor_data_staleness(self) -> None:
        """
        Trigger kill switch if no market-data tick received for >60 seconds.

        Only fires during market hours (08:00–22:00 UTC to cover both NSE
        and US sessions). A gap outside market hours is normal and ignored.
        """
        logger.debug(
            "Data-staleness monitor started (timeout=%.0fs)", self._data_stale_timeout
        )
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                if self._kill_switch.active:
                    continue

                # Only check during broad market hours
                if not self._is_market_hours():
                    continue

                # No ticks recorded yet — nothing to check
                if not self._last_tick:
                    continue

                now = utc_now()
                for market, last_tick_time in list(self._last_tick.items()):
                    elapsed = (now - last_tick_time).total_seconds()
                    if elapsed > self._data_stale_timeout:
                        reason = (
                            f"Auto-triggered: data feed stale — "
                            f"{market} feed silent for {elapsed:.0f}s "
                            f"(threshold={self._data_stale_timeout:.0f}s)"
                        )
                        logger.critical("DATA FEED STALE: %s", reason)
                        await self._kill_switch.activate(
                            reason=reason, activated_by="data_staleness_monitor"
                        )
                        break  # one activation is enough

            except asyncio.CancelledError:
                logger.debug("Data-staleness monitor cancelled")
                return
            except Exception:
                logger.exception("Data-staleness monitor error — continuing")

    # ------------------------------------------------------------------
    # Monitor 4 — Single-strategy loss
    # ------------------------------------------------------------------

    async def _monitor_strategy_loss(self) -> None:
        """
        Trigger kill switch if a single strategy's loss exceeds the threshold.

        Threshold is expressed as a percentage of the strategy's starting capital.
        E.g. strategy_loss_pct=5.0 means halt if any strategy loses >5% of capital.
        """
        logger.debug(
            "Strategy-loss monitor started (threshold=%.1f%%)", self._strategy_loss_pct
        )
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                if self._kill_switch.active:
                    continue

                for strategy_id, pnl in list(self._strategy_pnl.items()):
                    capital = self._strategy_capital.get(strategy_id, 0)
                    if capital <= 0 or pnl >= 0:
                        continue

                    loss_pct = abs(pnl) / capital * 100.0
                    if loss_pct >= self._strategy_loss_pct:
                        reason = (
                            f"Auto-triggered: strategy loss threshold breached — "
                            f"strategy '{strategy_id}' lost {loss_pct:.2f}% "
                            f"(threshold={self._strategy_loss_pct:.1f}%)"
                        )
                        logger.critical("STRATEGY LOSS THRESHOLD BREACHED: %s", reason)
                        await self._kill_switch.activate(
                            reason=reason, activated_by="strategy_loss_monitor"
                        )
                        break

            except asyncio.CancelledError:
                logger.debug("Strategy-loss monitor cancelled")
                return
            except Exception:
                logger.exception("Strategy-loss monitor error — continuing")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_market_hours() -> bool:
        """
        Return True if current UTC time falls within broad market hours.

        Window: 03:30–10:30 UTC (NSE session) and 13:30–20:00 UTC (US session).
        Outside these windows data feed silence is expected and not a trigger.
        """
        now = datetime.now(tz=timezone.utc)
        hour = now.hour + now.minute / 60.0
        # NSE: 03:30–10:30 UTC  |  US: 13:30–20:00 UTC
        return (3.5 <= hour <= 10.5) or (13.5 <= hour <= 20.0)
