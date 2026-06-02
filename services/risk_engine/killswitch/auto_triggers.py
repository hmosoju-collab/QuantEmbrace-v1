"""
Kill Switch Auto-Triggers — background monitors that activate the kill switch
when system health thresholds are breached.

Five automatic trigger conditions are monitored here. The sixth (daily portfolio
loss) is handled by ``DailyLossValidator`` in the risk validation pipeline.

Trigger conditions:
    1. Single-strategy loss exceeds per-strategy threshold.
    2. Order submission rate exceeds runaway threshold (e.g., >20 orders/min).
    3. Broker connectivity lost for longer than the configured timeout (default 30s).
    4. Signal consumer lag: no signals received from Kafka for >``consumer_lag_stale_secs``
       (default 300s) during market hours.  Tolerates Kafka consumer rebalancing.
    5. WebSocket producer heartbeat stale: data_ingestion has not written a DynamoDB
       heartbeat key for >``producer_heartbeat_stale_secs`` (default 60s) during market
       hours.  Detects a dead WebSocket feed independently of consumer lag.

ADR-021 Phase 1: triggers 4 and 5 replace the former single ``data_stale_secs``
threshold (which was 3600s as a workaround for consumer rebalancing false fires).
The workaround ``RISK_DATA_FEED_STALE_SECONDS=3600`` in ``.env`` can now be removed
or set to 300 (the consumer-lag timeout).

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

# DynamoDB key written by data_ingestion's producer heartbeat loop.
# risk_engine reads this to detect a dead WebSocket feed independently of
# Kafka consumer lag (ADR-021 Phase 1 split).
_HEARTBEAT_PK_PREFIX = "HEARTBEAT#"
_HEARTBEAT_SK = "CURRENT"

logger = get_logger(__name__, service_name="risk_engine")

# Type alias for the heartbeat-update callback
HeartbeatCallback = Callable[[], None]


class KillSwitchMonitor:
    """
    Background task manager for all automatic kill-switch triggers.

    The monitor aggregates four independent health checks as asyncio tasks.
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
        order_rate_limit: int = 20,                    # orders per minute before runaway
        order_rate_window_secs: int = 60,              # sliding window for order rate
        broker_timeout_secs: float = 30.0,             # connectivity lost threshold
        # ADR-021 Phase 1: split data feed staleness into two independent thresholds.
        # consumer_lag_stale_secs: how long risk_engine can go without receiving any
        #   signal from strategy_engine via Kafka (consumer lag / rebalancing tolerance).
        #   Default 300s — generous enough to survive consumer rebalancing.
        consumer_lag_stale_secs: float = 300.0,
        # producer_heartbeat_stale_secs: how long data_ingestion's DynamoDB heartbeat
        #   key can be absent/stale before declaring the WebSocket producer dead.
        #   Default 60s — tight because a dead WebSocket means no live prices.
        producer_heartbeat_stale_secs: float = 60.0,
        # Legacy alias kept for callers that set data_stale_secs directly.
        # If provided, overrides consumer_lag_stale_secs (the consumer monitor is
        # the closer analogue to the old unified check).
        data_stale_secs: Optional[float] = None,
        strategy_loss_pct: float = 5.0,               # per-strategy loss % threshold
        poll_interval_secs: float = 5.0,              # how often each monitor checks
        # DynamoDB client + table for producer heartbeat monitor (monitor 5).
        # When None, monitor 5 is disabled and only consumer lag is tracked.
        dynamo_client: Optional[Any] = None,
        prices_table: Optional[str] = None,
    ) -> None:
        self._kill_switch = kill_switch
        self._settings = settings or get_settings()

        # Configurable thresholds
        self._order_rate_limit = order_rate_limit
        self._order_rate_window = order_rate_window_secs
        self._broker_timeout = broker_timeout_secs
        # Consumer lag monitor (replaces old unified data_stale_secs).
        self._consumer_lag_timeout = data_stale_secs if data_stale_secs is not None else consumer_lag_stale_secs
        self._producer_heartbeat_timeout = producer_heartbeat_stale_secs
        self._strategy_loss_pct = strategy_loss_pct
        self._poll_interval = poll_interval_secs

        # Producer heartbeat monitor (monitor 5) — only active when DynamoDB is wired.
        self._dynamo = dynamo_client
        self._prices_table = prices_table

        # Grace period after startup: candle strategies need 5–10 minutes of warmup
        # before generating signals.  Do not fire the data-staleness trigger during
        # this window even if no ticks have been recorded yet.
        self._start_time: datetime = utc_now()
        self._startup_grace_secs: float = 600.0  # 10 minutes

        # --- State tracked by event-report methods ---

        # Sliding window: timestamps (UTC) of each order submitted
        self._order_timestamps: deque[datetime] = deque()

        # Last successful broker heartbeat (REST call or WebSocket frame)
        self._last_broker_ping: Optional[datetime] = None
        self._broker_connected: bool = False  # set True by record_broker_ping()

        # Per-market last tick timestamp
        self._last_tick: dict[str, datetime] = {}
        # Track kill-switch active state so we can reset stale clocks on deactivation.
        # Without this, clearing a kill switch that fired due to data staleness would
        # immediately re-fire because _last_tick is frozen for the entire active period.
        self._ks_was_active: bool = False

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
            asyncio.create_task(self._monitor_consumer_lag(), name="ks_consumer_lag"),
            asyncio.create_task(self._monitor_strategy_loss(), name="ks_strategy_loss"),
        ]
        if self._dynamo is not None and self._prices_table:
            self._tasks.append(
                asyncio.create_task(self._monitor_producer_heartbeat(), name="ks_producer_heartbeat")
            )
        logger.info(
            "KillSwitchMonitor started (%d monitors active; producer_heartbeat=%s)",
            len(self._tasks),
            "enabled" if self._dynamo is not None else "disabled",
        )

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
    # Monitor 3 — Kafka signal consumer lag  (ADR-021 Phase 1 split)
    # ------------------------------------------------------------------

    async def _monitor_consumer_lag(self) -> None:
        """
        Trigger kill switch if risk_engine receives no signals from Kafka for
        >consumer_lag_stale_secs (default 300s) during market hours.

        Replaces the old unified "data_stale" monitor.  The 300s default tolerates
        Kafka consumer rebalancing without false positives.  The WebSocket producer
        health is monitored separately by _monitor_producer_heartbeat() (monitor 5).

        ``record_data_tick(market)`` is called by validate_signal() on every signal
        arrival — this clock reflects signal consumer throughput, not raw tick rate.
        """
        logger.debug(
            "Consumer-lag monitor started (timeout=%.0fs)", self._consumer_lag_timeout
        )
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                ks_active_now = self._kill_switch.active

                # When the kill switch transitions active → inactive, reset all clocks
                # so the monitor doesn't immediately re-fire after deactivation.
                if self._ks_was_active and not ks_active_now:
                    now = utc_now()
                    for market in list(self._last_tick):
                        self._last_tick[market] = now
                    logger.info("Consumer-lag clocks reset after kill-switch deactivation")
                self._ks_was_active = ks_active_now

                if ks_active_now:
                    continue
                if not self._is_market_hours():
                    continue
                if not self._last_tick:
                    continue

                uptime = (utc_now() - self._start_time).total_seconds()
                if uptime < self._startup_grace_secs:
                    continue

                now = utc_now()
                for market, last_tick_time in list(self._last_tick.items()):
                    elapsed = (now - last_tick_time).total_seconds()
                    if elapsed > self._consumer_lag_timeout:
                        reason = (
                            f"Auto-triggered: Kafka consumer lag — "
                            f"no {market} signals received for {elapsed:.0f}s "
                            f"(threshold={self._consumer_lag_timeout:.0f}s)"
                        )
                        logger.critical("CONSUMER LAG THRESHOLD EXCEEDED: %s", reason)
                        await self._kill_switch.activate(
                            reason=reason, activated_by="consumer_lag_monitor"
                        )
                        break

            except asyncio.CancelledError:
                logger.debug("Consumer-lag monitor cancelled")
                return
            except Exception:
                logger.exception("Consumer-lag monitor error — continuing")

    # ------------------------------------------------------------------
    # Monitor 5 — WebSocket producer heartbeat  (ADR-021 Phase 1 split)
    # ------------------------------------------------------------------

    async def _monitor_producer_heartbeat(self) -> None:
        """
        Trigger kill switch if data_ingestion's DynamoDB heartbeat key is absent
        or older than producer_heartbeat_stale_secs (default 60s).

        data_ingestion writes ``PK=HEARTBEAT#{market} SK=CURRENT updated_at=<iso>``
        to the latest-prices table every 10 seconds while connectors are live.
        Absence or staleness means the WebSocket feed is dead — independent of
        Kafka consumer lag (which monitor 3 tracks).

        This monitor is only started when ``dynamo_client`` is provided to the
        KillSwitchMonitor constructor.
        """
        logger.debug(
            "Producer-heartbeat monitor started (timeout=%.0fs table=%s)",
            self._producer_heartbeat_timeout,
            self._prices_table,
        )
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                if self._kill_switch.active:
                    continue
                if not self._is_market_hours():
                    continue

                uptime = (utc_now() - self._start_time).total_seconds()
                if uptime < self._startup_grace_secs:
                    continue

                now = utc_now()
                for market in ("NSE", "US"):
                    try:
                        response = await asyncio.to_thread(
                            self._dynamo.get_item,
                            TableName=self._prices_table,
                            Key={
                                "PK": {"S": f"{_HEARTBEAT_PK_PREFIX}{market}"},
                                "SK": {"S": _HEARTBEAT_SK},
                            },
                            ProjectionExpression="updated_at",
                            ConsistentRead=False,
                        )
                    except Exception:
                        logger.warning(
                            "producer_heartbeat_monitor.dynamo_read_failed market=%s — skipping check",
                            market,
                        )
                        continue

                    item = response.get("Item")
                    if not item:
                        # Heartbeat key never written (data_ingestion not yet started
                        # or market not active) — skip, not an error.
                        continue

                    updated_at_str = (item.get("updated_at") or {}).get("S", "")
                    if not updated_at_str:
                        continue

                    try:
                        from datetime import datetime, timezone as _tz  # noqa: PLC0415
                        updated_at = datetime.fromisoformat(updated_at_str)
                        if updated_at.tzinfo is None:
                            updated_at = updated_at.replace(tzinfo=_tz.utc)
                    except ValueError:
                        continue

                    elapsed = (now - updated_at).total_seconds()
                    if elapsed > self._producer_heartbeat_timeout:
                        reason = (
                            f"Auto-triggered: WebSocket producer heartbeat stale — "
                            f"data_ingestion {market} heartbeat is {elapsed:.0f}s old "
                            f"(threshold={self._producer_heartbeat_timeout:.0f}s); "
                            f"WebSocket feed likely dead"
                        )
                        logger.critical("PRODUCER HEARTBEAT STALE: %s", reason)
                        await self._kill_switch.activate(
                            reason=reason, activated_by="producer_heartbeat_monitor"
                        )
                        break  # one activation is enough

            except asyncio.CancelledError:
                logger.debug("Producer-heartbeat monitor cancelled")
                return
            except Exception:
                logger.exception("Producer-heartbeat monitor error — continuing")

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
