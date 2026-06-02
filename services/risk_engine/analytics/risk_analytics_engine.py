"""
RiskAnalyticsEngine — background portfolio analytics loop.

Computes sector exposure breakdown, simplified 5-day historical VaR, and
daily P&L on a phase-aware schedule and persists the snapshot to DynamoDB.
``RiskContextBuilder`` reads this snapshot once per signal (zero additional I/O).

Phase-aware scheduling (ADR-014 Misalignment 2, Zerodha rate capacity alignment):
    PRE_OPEN      30s  — warm up sector/VaR before market opens
    PRE_AUCTION   30s  — continue warm-up through auction
    MARKET_OPEN   30s  — fast refresh during volatile first 15 min
    NORMAL        60s  — standard refresh during main session
    PRE_CLOSE     30s  — pre-compute MIS square-off impact
    CLOSING       60s  — end-of-session wrap-up
    POST_CLOSE   300s  — end-of-day final snapshot only
    OVERNIGHT      ∞   — no computation; resume at PRE_OPEN

Analytics computed per cycle:
    1. Sector breakdown — sum position exposures by sector using InstrumentRegistry
    2. Portfolio total exposure — sum |qty × last_price| across all positions
    3. Portfolio P&L today — sum realized fills (from fills table, today only)
    4. Simplified VaR — 2% historical quantile over last 5 trading days
       using daily P&L records.  Falls back to 0.0 if < 5 days available.

DynamoDB write:
    PK=ANALYTICS#SNAPSHOT, SK=CURRENT
    Fields: sector_exposures (map), portfolio_var_2pct (N),
            portfolio_pnl_today (N), computed_at (S), total_exposure (N)
    Overwrites on each cycle (no version history — use S3 audit log for that).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.utils.helpers import utc_iso, utc_now

logger = get_logger(__name__, service_name="risk_engine")

# Phase-aware analytics interval (seconds).  Import MarketPhase lazily to
# avoid startup failures when shared.zerodha is not yet importable.
try:
    from shared.zerodha.market_phase import MarketPhase, MarketPhaseGovernor

    ANALYTICS_INTERVAL_BY_PHASE: dict[Any, int] = {
        MarketPhase.PRE_OPEN:    30,
        MarketPhase.PRE_AUCTION: 30,
        MarketPhase.MARKET_OPEN: 30,
        MarketPhase.NORMAL:      60,
        MarketPhase.PRE_CLOSE:   30,
        MarketPhase.CLOSING:     60,
        MarketPhase.POST_CLOSE:  300,
    }
    _PHASE_GOVERNOR_AVAILABLE = True
except ImportError:
    MarketPhaseGovernor = None  # type: ignore[assignment, misc]
    ANALYTICS_INTERVAL_BY_PHASE = {}
    _PHASE_GOVERNOR_AVAILABLE = False

_DEFAULT_INTERVAL_SECONDS: int = 60

# VaR config
_VAR_QUANTILE: float = 0.02   # 2% loss quantile
_VAR_LOOKBACK_DAYS: int = 5   # minimum days for a valid VaR estimate


class RiskAnalyticsEngine:
    """
    Background asyncio task that computes portfolio analytics on a schedule.

    Wired into the risk engine's asyncio.gather alongside the Kafka processing
    loop.  Computes analytics and writes to DynamoDB; validators read the
    snapshot via RiskContextBuilder — zero per-signal latency cost.

    Attributes:
        _dynamo:           Low-level DynamoDB client.
        _positions_table:  Table with open position records.
        _orders_table:     Table with fill history (for P&L and VaR).
        _risk_state_table: Table where analytics snapshots are written.
        _registry:         Optional InstrumentRegistry for sector lookup.
        _phase_governor:   Optional MarketPhaseGovernor for phase-aware timing.
        _running:          Set False by stop() to exit the loop.
    """

    def __init__(
        self,
        dynamo_client: Any,
        instrument_registry: Optional[Any] = None,
        positions_table: Optional[str] = None,
        orders_table: Optional[str] = None,
        risk_state_table: Optional[str] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._dynamo = dynamo_client
        self._registry = instrument_registry
        self._settings = settings or get_settings()
        self._positions_table = (
            positions_table or self._settings.aws.dynamodb_table_positions
        )
        self._orders_table = orders_table or self._settings.aws.dynamodb_table_orders
        self._risk_state_table = (
            risk_state_table or self._settings.aws.dynamodb_table_risk_state
        )

        self._phase_governor: Optional[Any] = None
        if _PHASE_GOVERNOR_AVAILABLE and MarketPhaseGovernor is not None:
            try:
                self._phase_governor = MarketPhaseGovernor()
            except Exception:
                logger.warning("risk_analytics.phase_governor_init_failed — using fixed interval")

        self._running: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Run the analytics loop as a long-running coroutine.

        Call this in asyncio.gather alongside the Kafka loops.  Returns when
        stop() is called.
        """
        self._running = True
        logger.info("risk_analytics_engine.started")
        await self._analytics_loop()

    async def stop(self) -> None:
        """Signal the analytics loop to stop on its next iteration."""
        self._running = False
        logger.info("risk_analytics_engine.stopped")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _analytics_loop(self) -> None:
        """
        Phase-aware analytics computation loop.

        Sleeps for the phase-appropriate interval, then runs one computation
        cycle and persists the snapshot.  Errors in any individual computation
        are logged but do NOT break the loop — a missed snapshot is acceptable;
        a crashed analytics loop that prevents signal validation is not.
        """
        while self._running:
            interval = self._current_interval()
            if interval <= 0:
                # OVERNIGHT or unknown phase — long sleep, no computation
                await asyncio.sleep(60)
                continue

            await asyncio.sleep(interval)

            if not self._running:
                break

            try:
                snapshot = await self._compute_snapshot()
                await self._persist_snapshot(snapshot)
            except Exception:
                logger.exception("risk_analytics.cycle_error — snapshot skipped")

        logger.info("risk_analytics_engine.loop_exited")

    # ── Computation ───────────────────────────────────────────────────────────

    async def _compute_snapshot(self) -> dict[str, Any]:
        """Gather all portfolio metrics concurrently and return a snapshot dict."""
        positions, fills_today = await asyncio.gather(
            self._fetch_all_positions(),
            self._fetch_fills_today(),
            return_exceptions=False,
        )

        sector_exposures = self._compute_sector_exposures(positions)
        total_exposure = sum(abs(p["qty"] * p["last_price"]) for p in positions)
        pnl_today = self._compute_pnl_today(fills_today)
        var_2pct = await self._compute_var(fills_today)

        return {
            "sector_exposures": sector_exposures,
            "total_exposure": total_exposure,
            "portfolio_pnl_today": pnl_today,
            "portfolio_var_2pct": var_2pct,
            "computed_at": utc_iso(),
        }

    def _compute_sector_exposures(
        self, positions: list[dict[str, Any]]
    ) -> dict[str, float]:
        """Sum position exposures by sector using InstrumentRegistry."""
        sector_totals: dict[str, float] = {}

        for pos in positions:
            symbol = pos["symbol"]
            exposure = abs(pos["qty"] * pos["last_price"])
            sector = "UNKNOWN"

            if self._registry is not None:
                try:
                    config = self._registry.get(symbol)
                    if config is not None:
                        sector = config.sector
                except Exception:
                    pass

            sector_totals[sector] = sector_totals.get(sector, 0.0) + exposure

        return sector_totals

    def _compute_pnl_today(self, fills: list[dict[str, Any]]) -> float:
        """
        Compute realized P&L from today's fills.

        Note: This is gross turnover-based P&L, not net P&L.  A proper net
        P&L calculation requires cost basis per symbol which is not yet stored.
        Phase 5 (feature store) will add cost basis tracking.
        """
        total = 0.0
        for fill in fills:
            direction = fill.get("direction", "BUY")
            notional = fill.get("notional_value", 0.0)
            # Sells reduce exposure (positive contribution to realized P&L proxy)
            if direction == "SELL":
                total += notional
            else:
                total -= notional
        return total

    async def _compute_var(self, fills_today: list[dict[str, Any]]) -> float:
        """
        Simplified 5-day historical VaR at 2% quantile.

        Reads daily_pnl records from the fills table (one record per calendar
        day written by the loss validator on each fill).  Uses the bottom
        _VAR_QUANTILE percentile of 5-day P&L as a rough loss estimate.

        Returns 0.0 if fewer than _VAR_LOOKBACK_DAYS of records are available.
        """
        if self._dynamo is None:
            return 0.0

        try:
            # Read daily P&L summary records (PK=DAILY_PNL#{YYYY-MM-DD}, SK=TOTAL)
            response = await asyncio.to_thread(
                self._dynamo.query,
                TableName=self._risk_state_table,
                KeyConditionExpression="begins_with(PK, :prefix)",
                ExpressionAttributeValues={":prefix": {"S": "DAILY_PNL#"}},
                ScanIndexForward=False,  # most recent first
                Limit=_VAR_LOOKBACK_DAYS + 2,  # small buffer
                ProjectionExpression="pnl_total",
            )
            items = response.get("Items", [])
            if len(items) < _VAR_LOOKBACK_DAYS:
                return 0.0

            daily_pnls = sorted(
                [float(item.get("pnl_total", {}).get("N", "0")) for item in items]
            )
            # 2nd percentile: take floor(0.02 * N)-th smallest value
            idx = max(0, int(_VAR_QUANTILE * len(daily_pnls)))
            return abs(daily_pnls[idx])

        except Exception:
            logger.debug("risk_analytics.var_compute_failed — returning 0.0")
            return 0.0

    # ── DynamoDB I/O ──────────────────────────────────────────────────────────

    async def _fetch_all_positions(self) -> list[dict[str, Any]]:
        """Scan positions table and return list of {symbol, qty, last_price} dicts."""
        if self._dynamo is None:
            return []

        results: list[dict[str, Any]] = []
        try:
            last_key: Optional[dict[str, Any]] = None
            while True:
                scan_kwargs: dict[str, Any] = {
                    "TableName": self._positions_table,
                    "FilterExpression": "begins_with(PK, :prefix)",
                    "ExpressionAttributeValues": {":prefix": {"S": "POSITION#"}},
                    "ProjectionExpression": "PK, quantity, last_price",
                }
                if last_key:
                    scan_kwargs["ExclusiveStartKey"] = last_key

                response = await asyncio.to_thread(self._dynamo.scan, **scan_kwargs)
                for item in response.get("Items", []):
                    pk = item.get("PK", {}).get("S", "")
                    symbol = pk.replace("POSITION#", "") if pk.startswith("POSITION#") else pk
                    results.append({
                        "symbol": symbol,
                        "qty": float(item.get("quantity", {}).get("N", "0")),
                        "last_price": float(item.get("last_price", {}).get("N", "0")),
                    })
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
        except Exception:
            logger.warning("risk_analytics.positions_fetch_failed")

        return results

    async def _fetch_fills_today(self) -> list[dict[str, Any]]:
        """Query fills table for today's fills. Returns list of fill dicts."""
        if self._dynamo is None:
            return []

        today = utc_now().strftime("%Y-%m-%d")
        results: list[dict[str, Any]] = []

        try:
            # Fills use PK=FILL#{fill_id} — query via date GSI if available,
            # or scan with FilterExpression.  For simplicity: scan with filter.
            response = await asyncio.to_thread(
                self._dynamo.scan,
                TableName=self._orders_table,
                FilterExpression="begins_with(PK, :prefix) AND begins_with(created_at, :today)",
                ExpressionAttributeValues={
                    ":prefix": {"S": "FILL#"},
                    ":today": {"S": today},
                },
                ProjectionExpression="direction, notional_value",
            )
            for item in response.get("Items", []):
                results.append({
                    "direction": item.get("direction", {}).get("S", "BUY"),
                    "notional_value": float(
                        item.get("notional_value", {}).get("N", "0")
                    ),
                })
        except Exception:
            logger.debug("risk_analytics.fills_fetch_failed — P&L will be 0")

        return results

    async def _persist_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Write analytics snapshot to DynamoDB risk-state table."""
        if self._dynamo is None:
            return

        try:
            # Convert sector_exposures dict to DynamoDB map format
            sector_map: dict[str, Any] = {
                k: {"N": str(round(v, 2))}
                for k, v in snapshot["sector_exposures"].items()
            }

            await asyncio.to_thread(
                self._dynamo.put_item,
                TableName=self._risk_state_table,
                Item={
                    "PK": {"S": "ANALYTICS#SNAPSHOT"},
                    "SK": {"S": "CURRENT"},
                    "sector_exposures": {"M": sector_map},
                    "total_exposure": {"N": str(round(snapshot["total_exposure"], 2))},
                    "portfolio_pnl_today": {
                        "N": str(round(snapshot["portfolio_pnl_today"], 2))
                    },
                    "portfolio_var_2pct": {
                        "N": str(round(snapshot["portfolio_var_2pct"], 2))
                    },
                    "computed_at": {"S": snapshot["computed_at"]},
                },
            )

            logger.debug(
                "risk_analytics.snapshot_persisted "
                "sectors=%d total_exposure=%.0f pnl=%.0f var=%.0f",
                len(snapshot["sector_exposures"]),
                snapshot["total_exposure"],
                snapshot["portfolio_pnl_today"],
                snapshot["portfolio_var_2pct"],
            )

        except Exception:
            logger.exception("risk_analytics.persist_failed — snapshot lost")

    # ── Interval helpers ──────────────────────────────────────────────────────

    def _current_interval(self) -> int:
        """
        Return analytics interval in seconds based on current market phase.

        Returns 0 for OVERNIGHT (no computation).
        Returns _DEFAULT_INTERVAL_SECONDS if phase governor is not available.
        """
        if not _PHASE_GOVERNOR_AVAILABLE or self._phase_governor is None:
            return _DEFAULT_INTERVAL_SECONDS

        try:
            phase = self._phase_governor.current_phase()
            interval = ANALYTICS_INTERVAL_BY_PHASE.get(phase)
            if interval is None:
                return 0  # OVERNIGHT or unknown — skip
            return interval
        except Exception:
            return _DEFAULT_INTERVAL_SECONDS
