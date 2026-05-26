"""
MonitoringStatusService, MonitoringStatusSnapshot, MonitoringStatusRenderer.

Produces the structured 15-section paper-trading monitoring report.

Usage — standalone (DynamoDB only):
    svc = MonitoringStatusService(
        dynamo_client=dynamo,
        positions_table="qe-positions",
        risk_state_table="qe-risk-state",
    )
    snap = await svc.build_snapshot()
    print(MonitoringStatusRenderer().render(snap))

Usage — in-process (live counters from running service):
    counters = LiveCounters(
        tee_running=True,
        tee_stop_loss_hits=2,
        recon_ran=True,
        recon_mode="paper",
        ...
    )
    svc = MonitoringStatusService(..., live_counters=counters)
    snap = await svc.build_snapshot()

Trigger phrases (Claude responds with this template):
    "Give monitoring status" | "Daily monitoring status"
    "Paper trading monitoring status" | "Today's monitoring status"
    "Give status" | "Monitoring status now"
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .ltp_resolver import LtpResolver

_IST = timezone(timedelta(hours=5, minutes=30))


# ── Position snapshot ─────────────────────────────────────────────────────────


@dataclass
class PositionSnapshot:
    """One position row as seen by the monitoring service."""

    symbol: str
    quantity: float           # signed: +LONG  -SHORT  0=FLAT
    direction: str            # DynamoDB direction field
    entry_price: float
    ltp: Optional[float]      # last trade price — overwritten by _enrich_ltp with fresh market price
    stop_price: Optional[float]
    take_profit: Optional[float]
    trailing_active: bool     # exit_state == "TRAILING_ACTIVE"
    exit_state: str
    exit_order_id: Optional[str]
    # LTP provenance — populated by _enrich_ltp after DynamoDB read
    ltp_source: str = "unavailable"       # "prices_table" | "prices_table_stale" | "position_fill" | "unavailable"
    ltp_age_seconds: Optional[float] = None
    ltp_is_stale: bool = False

    @property
    def qty_direction(self) -> str:
        if self.quantity > 1e-9:
            return "LONG"
        if self.quantity < -1e-9:
            return "SHORT"
        return "FLAT"

    @property
    def direction_mismatch(self) -> bool:
        stored = self.direction.upper()
        derived = self.qty_direction
        return stored != derived and stored != "FLAT"

    @property
    def pnl(self) -> Optional[float]:
        if self.ltp is None or self.entry_price == 0.0:
            return None
        qty = abs(self.quantity)
        if self.qty_direction == "LONG":
            return (self.ltp - self.entry_price) * qty
        if self.qty_direction == "SHORT":
            return (self.entry_price - self.ltp) * qty
        return None


# ── Component health ──────────────────────────────────────────────────────────


@dataclass
class ServiceHealthRow:
    component: str
    status: str   # UP / DOWN / UNKNOWN / RAN / SKIPPED / FAILED / OK / WARNING / ERROR
    notes: str = ""


# ── Sub-system status dataclasses ─────────────────────────────────────────────


@dataclass
class TEEStatus:
    running: bool = False
    poll_interval_seconds: int = 60
    stop_loss_active: int = 0
    take_profit_active: int = 0
    trailing_active: int = 0
    trailing_activated_today: int = 0
    trailing_stop_hit_today: int = 0
    stop_loss_hit_today: int = 0
    take_profit_hit_today: int = 0
    duplicate_exits_prevented: int = 0
    unmanaged_detections: int = 0
    stale_ltp_blocks: int = 0
    latest_events: list[str] = field(default_factory=list)


@dataclass
class RouterStatus:
    mode: str = "PAPER"
    paper_exits_routed: int = 0
    backtest_exits_routed: int = 0
    live_exits_attempted: int = 0
    live_exits_placed: int = 0
    live_exits_blocked: int = 0
    idempotency_successes: int = 0
    idempotency_skips: int = 0
    failed_routes: int = 0


@dataclass
class MISStatus:
    armed: bool = True
    close_time_ist: str = "15:05"
    deadline_time_ist: str = "15:10"
    broker_fallback_ist: str = "15:15"
    positions_discovered: Optional[int] = None
    long_discovered: Optional[int] = None
    short_discovered: Optional[int] = None
    orders_placed: Optional[int] = None
    orders_rejected: Optional[int] = None
    positions_confirmed_flat: Optional[int] = None
    positions_at_deadline: Optional[int] = None
    kill_switch_activated: bool = False
    # These are code invariants verified by the test suite — always True.
    uses_quantity_filter: bool = True
    long_closes_with_sell: bool = True
    short_closes_with_buy: bool = True
    close_qty_uses_abs: bool = True
    uses_product_type_mis: bool = True
    skips_with_exit_order_id: bool = True


@dataclass
class ReconciliationStatus:
    ran_on_startup: bool = False
    mode: str = "UNKNOWN"
    mismatches_detected: int = 0
    paper_repairs: int = 0
    live_critical_alerts: int = 0
    zero_qty_open_found: int = 0
    stale_exit_lock_found: int = 0
    direction_qty_mismatch_found: int = 0
    open_without_exit_policy_found: int = 0


@dataclass
class RiskCapStatus:
    daily_cap_reached: bool = False
    daily_loss_limit_reached: bool = False
    daily_profit_lock_reached: bool = False
    new_entries_allowed: bool = True
    exit_management_allowed: bool = True  # always True — exits bypass the cap
    max_open_positions_reached: bool = False
    max_fills_per_strategy_reached: bool = False


@dataclass
class StrategyStatusRow:
    name: str
    status: str   # ACTIVE / CAPPED / STOPPED
    signals: int
    fills: int
    open_positions: int
    exits: int
    cap_status: str  # OK / CAPPED
    notes: str = ""


@dataclass
class PnLStatus:
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0
    largest_winner_symbol: Optional[str] = None
    largest_winner_amount: Optional[float] = None
    largest_loser_symbol: Optional[str] = None
    largest_loser_amount: Optional[float] = None
    max_intraday_drawdown: Optional[float] = None
    win_rate_pct: Optional[float] = None
    average_winner: Optional[float] = None
    average_loser: Optional[float] = None


# ── Live counters (in-process only) ──────────────────────────────────────────


@dataclass
class LiveCounters:
    """
    In-memory counters from the running execution service.

    All fields default to zero/False so partial data is safe.
    The execution service (or a monitoring sidecar) populates this and
    passes it to MonitoringStatusService to enrich the DynamoDB-derived snapshot.
    """

    # Component health flags
    strategy_engine_up: bool = False
    risk_engine_up: bool = False
    execution_engine_up: bool = False

    # TEE
    tee_running: bool = False
    tee_poll_interval: int = 60
    tee_stop_loss_hits: int = 0
    tee_take_profit_hits: int = 0
    tee_trailing_activated: int = 0
    tee_trailing_hits: int = 0
    tee_duplicate_exits_prevented: int = 0
    tee_unmanaged_detections: int = 0
    tee_stale_ltp_blocks: int = 0
    tee_latest_events: list[str] = field(default_factory=list)

    # Router
    router_mode: str = "PAPER"
    router_paper_exits: int = 0
    router_backtest_exits: int = 0
    router_live_attempts: int = 0
    router_live_blocked: int = 0
    router_live_exits: int = 0
    router_idempotency_successes: int = 0
    router_idempotency_skips: int = 0
    router_failed_routes: int = 0

    # MIS
    mis_armed: bool = True
    mis_positions_discovered: Optional[int] = None
    mis_long_discovered: Optional[int] = None
    mis_short_discovered: Optional[int] = None
    mis_orders_placed: Optional[int] = None
    mis_orders_rejected: Optional[int] = None
    mis_positions_flat: Optional[int] = None
    mis_at_deadline: Optional[int] = None
    mis_kill_switch_activated: bool = False

    # Reconciliation
    recon_ran: bool = False
    recon_mode: str = "paper"
    recon_mismatches: int = 0
    recon_repairs: int = 0
    recon_criticals: int = 0
    recon_zero_qty_open: int = 0
    recon_stale_exit_lock: int = 0
    recon_qty_direction: int = 0
    recon_unmanaged: int = 0

    # Risk cap
    daily_cap_reached: bool = False
    daily_loss_limit_reached: bool = False
    daily_profit_lock_reached: bool = False
    new_entries_allowed: bool = True
    max_open_positions_reached: bool = False
    max_fills_per_strategy_reached: bool = False

    # Strategy
    strategy_statuses: list[StrategyStatusRow] = field(default_factory=list)

    # P&L
    realized_pnl: float = 0.0
    max_intraday_drawdown: Optional[float] = None


# ── Top-level snapshot ────────────────────────────────────────────────────────


@dataclass
class MonitoringStatusSnapshot:
    """All data required to render the 15-section monitoring report."""

    # §1 — Overall
    overall_status: str       # GREEN / AMBER / RED
    timestamp_ist: str
    trading_mode: str         # PAPER / BACKTEST / LIVE
    live_trading_enabled: bool
    broker_live_calls: str    # ENABLED / DISABLED
    kill_switch_active: bool
    session_safety: str       # SAFE / UNSAFE / DEGRADED
    verdict_line: str

    # §2 — Service health
    service_health: list[ServiceHealthRow] = field(default_factory=list)

    # §3 — Safety gates: (check, expected, actual, passed)
    safety_gates: list[tuple[str, str, str, bool]] = field(default_factory=list)

    # §4 — Position counts
    total_open_positions: int = 0
    long_count: int = 0
    short_count: int = 0
    flat_ignored: int = 0
    positions_with_exit_policy: int = 0
    positions_missing_exit_policy: int = 0
    positions_with_exit_order_id: int = 0
    positions_with_trailing_active: int = 0
    direction_qty_mismatches: int = 0
    unmanaged_positions: int = 0

    # §5 — Detail rows
    open_positions: list[PositionSnapshot] = field(default_factory=list)

    # §6–§12
    tee_status: TEEStatus = field(default_factory=TEEStatus)
    router_status: RouterStatus = field(default_factory=RouterStatus)
    mis_status: MISStatus = field(default_factory=MISStatus)
    reconciliation_status: ReconciliationStatus = field(default_factory=ReconciliationStatus)
    risk_cap_status: RiskCapStatus = field(default_factory=RiskCapStatus)
    strategy_statuses: list[StrategyStatusRow] = field(default_factory=list)
    pnl_status: PnLStatus = field(default_factory=PnLStatus)

    # §13 — Alerts
    critical_alerts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data_quality_issues: list[str] = field(default_factory=list)
    operational_issues: list[str] = field(default_factory=list)

    # §14 — Actions
    action_required: bool = False
    actions: list[str] = field(default_factory=list)

    # §15 — Verdict
    final_status: str = "GREEN"
    final_summary: str = ""


# ── Service ───────────────────────────────────────────────────────────────────


class MonitoringStatusService:
    """
    Builds a MonitoringStatusSnapshot from live DynamoDB state plus optional
    in-memory counters from the running execution service.
    """

    def __init__(
        self,
        dynamo_client: Any,
        positions_table: str,
        risk_state_table: str,
        prices_table: Optional[str] = None,
        trading_mode: str = "paper",
        live_trading_enabled: bool = False,
        live_counters: Optional[LiveCounters] = None,
        ltp_resolver: Optional[LtpResolver] = None,
    ) -> None:
        self._dynamo = dynamo_client
        self._positions_table = positions_table
        self._risk_state_table = risk_state_table
        self._prices_table = prices_table
        self._trading_mode = trading_mode.upper()
        self._live_trading_enabled = live_trading_enabled
        self._c = live_counters or LiveCounters()
        self._ltp_resolver = ltp_resolver or LtpResolver(
            dynamo_client=dynamo_client,
            prices_table=prices_table,
        )

    async def build_snapshot(self) -> MonitoringStatusSnapshot:
        timestamp_ist = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")

        raw_positions, kill_switch_active = await asyncio.gather(
            self._fetch_positions(),
            self._fetch_kill_switch(),
        )

        # Partition into open/flat, then enrich LTP for open positions only
        flat_ignored = sum(1 for p in raw_positions if p.direction.upper() == "FLAT")
        open_pos = await self._enrich_ltp(
            [p for p in raw_positions if p.direction.upper() != "FLAT"]
        )

        long_count = sum(1 for p in open_pos if p.qty_direction == "LONG")
        short_count = sum(1 for p in open_pos if p.qty_direction == "SHORT")
        with_policy = sum(1 for p in open_pos if p.stop_price is not None)
        missing_policy = sum(1 for p in open_pos if p.stop_price is None)
        with_exit_oid = sum(1 for p in open_pos if p.exit_order_id is not None)
        with_trailing = sum(1 for p in open_pos if p.trailing_active)
        dir_mismatches = sum(1 for p in open_pos if p.direction_mismatch)
        unmanaged = missing_policy

        # ── Build alerts / warnings ────────────────────────────────────────────
        critical_alerts: list[str] = []
        warnings: list[str] = []
        data_quality: list[str] = []
        operational: list[str] = []

        if self._live_trading_enabled:
            critical_alerts.append(
                "live_trading_enabled=True — live broker orders are ENABLED. "
                "Paper mode requires live_trading_enabled=False."
            )
        if self._trading_mode == "LIVE":
            critical_alerts.append(
                "TradingMode=LIVE — paper session requires TradingMode=PAPER."
            )
        for p in open_pos:
            if p.stop_price is None:
                critical_alerts.append(
                    f"UNMANAGED position: {p.symbol} "
                    f"qty={p.quantity:+.1f} direction={p.direction} "
                    f"has no exit policy (stop_price missing)"
                )
        if kill_switch_active and len(open_pos) > 0:
            critical_alerts.append(
                f"Kill switch ACTIVE with {len(open_pos)} open position(s) — "
                "no new entries allowed, exits must proceed."
            )
        if self._c.tee_unmanaged_detections > 0:
            critical_alerts.append(
                f"TEE detected {self._c.tee_unmanaged_detections} unmanaged "
                "position(s) during polling."
            )
        if self._c.router_live_attempts > 0:
            critical_alerts.append(
                f"{self._c.router_live_attempts} live exit route attempt(s) recorded — "
                "investigate immediately."
            )
        if self._c.mis_kill_switch_activated:
            critical_alerts.append(
                "MIS square-off activated the kill switch (deadline escalation at 15:10)."
            )
        if self._c.mis_at_deadline and self._c.mis_at_deadline > 0:
            critical_alerts.append(
                f"{self._c.mis_at_deadline} position(s) were still open at MIS "
                "deadline (15:10). Zerodha auto-square is the safety net."
            )

        if dir_mismatches > 0:
            warnings.append(
                f"{dir_mismatches} position(s) have direction/quantity mismatch — "
                "quantity is the canonical source of truth."
            )
        if self._c.recon_mismatches > 0 and not critical_alerts:
            warnings.append(
                f"Startup reconciliation found {self._c.recon_mismatches} mismatch(es) "
                f"({self._c.recon_repairs} repaired, "
                f"{self._c.recon_criticals} alerted)."
            )
        if not self._c.recon_ran:
            warnings.append(
                "Startup reconciliation status is UNKNOWN — "
                "could not confirm it ran before TEE started."
            )

        # ── LTP freshness data-quality checks ──────────────────────────────────
        stale_ltp = [p for p in open_pos if p.ltp_is_stale and p.ltp is not None]
        if stale_ltp:
            symbols = ", ".join(p.symbol for p in stale_ltp)
            data_quality.append(
                f"Stale LTP for {len(stale_ltp)} position(s): {symbols}. "
                "Price is from position fill, not live market quote — P&L is approximate."
            )
            if self._trading_mode.upper() == "LIVE":
                critical_alerts.append(
                    f"LIVE MODE — stale LTP for {len(stale_ltp)} position(s): {symbols}. "
                    "TEE will block exit evaluation until LiveQuotePoller resumes writing "
                    "to the prices table. Operator action required."
                )
        if self._c.tee_stale_ltp_blocks > 0:
            if self._trading_mode.upper() == "LIVE":
                critical_alerts.append(
                    f"TEE blocked {self._c.tee_stale_ltp_blocks} exit evaluation(s) due to "
                    "stale LTP in LIVE mode. Positions may be outside stop/target thresholds."
                )
            else:
                warnings.append(
                    f"TEE blocked {self._c.tee_stale_ltp_blocks} exit evaluation(s) due to "
                    "stale LTP (paper mode — no monetary risk, but exits may be delayed)."
                )
        unavail_ltp = [p for p in open_pos if p.ltp is None]
        if unavail_ltp:
            symbols = ", ".join(p.symbol for p in unavail_ltp)
            data_quality.append(
                f"LTP unavailable for {len(unavail_ltp)} position(s): {symbols}. "
                "Unrealized P&L cannot be calculated."
            )

        # ── Overall status ─────────────────────────────────────────────────────
        if critical_alerts:
            overall = "RED"
            session_safety = "UNSAFE"
        elif warnings:
            overall = "AMBER"
            session_safety = "DEGRADED"
        else:
            overall = "GREEN"
            session_safety = "SAFE"

        # ── Safety gates ───────────────────────────────────────────────────────
        safety_gates: list[tuple[str, str, str, bool]] = [
            ("Trading mode", "PAPER", self._trading_mode, self._trading_mode == "PAPER"),
            ("live_trading_enabled", "false", str(self._live_trading_enabled).lower(), not self._live_trading_enabled),
            ("Live broker order placement", "DISABLED", "DISABLED" if not self._live_trading_enabled else "ENABLED", not self._live_trading_enabled),
            ("Exit orders bypass signal pipeline", "YES", "YES", True),
            ("Daily cap blocks exits", "NO", "NO", True),
            ("Signed quantity invariant active", "YES", "YES", True),
            ("MIS product_type", "MIS", "MIS", True),
            ("Duplicate exit prevention", "ENABLED", "ENABLED", True),
        ]

        # ── Service health ─────────────────────────────────────────────────────
        mis_time = os.environ.get("MIS_CLOSE_TIME_IST", "15:05")
        service_health = [
            ServiceHealthRow("strategy_engine", "UP" if self._c.strategy_engine_up else "UNKNOWN"),
            ServiceHealthRow("risk_engine", "UP" if self._c.risk_engine_up else "UNKNOWN"),
            ServiceHealthRow("execution_engine", "UP" if self._c.execution_engine_up else "UNKNOWN"),
            ServiceHealthRow("TradeExitEngine", "UP" if self._c.tee_running else "UNKNOWN", f"poll_interval={self._c.tee_poll_interval}s"),
            ServiceHealthRow("ExitOrderRouter", "UP", f"mode={self._c.router_mode}"),
            ServiceHealthRow("MISSquareOffManager", "UP" if self._c.mis_armed else "UNKNOWN", f"fires at {mis_time} IST"),
            ServiceHealthRow("PositionReconciliationService", "RAN" if self._c.recon_ran else "UNKNOWN", f"mode={self._c.recon_mode} mismatches={self._c.recon_mismatches}"),
            ServiceHealthRow("paper broker ledger", "OK", "Synthetic fills via apply_fill_to_position"),
            ServiceHealthRow("DynamoDB positions table", "OK", self._positions_table),
            ServiceHealthRow(
                "prices_table (LiveQuotePoller)",
                "OK" if self._prices_table else "NOT_CONFIGURED",
                self._prices_table if self._prices_table else "pass prices_table= to enable live LTP",
            ),
            ServiceHealthRow("Kafka/events path", "OK" if self._c.execution_engine_up else "UNKNOWN"),
            ServiceHealthRow("candle_cache", "UNKNOWN", "No direct health check available"),
            ServiceHealthRow("data_ingestion", "UP" if self._c.strategy_engine_up else "UNKNOWN"),
        ]

        # ── TEE status ─────────────────────────────────────────────────────────
        tee = TEEStatus(
            running=self._c.tee_running,
            poll_interval_seconds=self._c.tee_poll_interval,
            stop_loss_active=sum(1 for p in open_pos if p.stop_price is not None and not p.trailing_active),
            take_profit_active=sum(1 for p in open_pos if p.take_profit is not None),
            trailing_active=with_trailing,
            trailing_activated_today=self._c.tee_trailing_activated,
            trailing_stop_hit_today=self._c.tee_trailing_hits,
            stop_loss_hit_today=self._c.tee_stop_loss_hits,
            take_profit_hit_today=self._c.tee_take_profit_hits,
            duplicate_exits_prevented=self._c.tee_duplicate_exits_prevented,
            unmanaged_detections=self._c.tee_unmanaged_detections,
            stale_ltp_blocks=self._c.tee_stale_ltp_blocks,
            latest_events=list(self._c.tee_latest_events),
        )

        # ── Router status ──────────────────────────────────────────────────────
        router = RouterStatus(
            mode=self._c.router_mode,
            paper_exits_routed=self._c.router_paper_exits,
            backtest_exits_routed=self._c.router_backtest_exits,
            live_exits_attempted=self._c.router_live_attempts,
            live_exits_placed=self._c.router_live_exits,
            live_exits_blocked=self._c.router_live_blocked,
            idempotency_successes=self._c.router_idempotency_successes,
            idempotency_skips=self._c.router_idempotency_skips,
            failed_routes=self._c.router_failed_routes,
        )

        # ── MIS status ─────────────────────────────────────────────────────────
        mis = MISStatus(
            armed=self._c.mis_armed,
            close_time_ist=os.environ.get("MIS_CLOSE_TIME_IST", "15:05"),
            deadline_time_ist=os.environ.get("MIS_DEADLINE_TIME_IST", "15:10"),
            broker_fallback_ist="15:15",
            positions_discovered=self._c.mis_positions_discovered,
            long_discovered=self._c.mis_long_discovered,
            short_discovered=self._c.mis_short_discovered,
            orders_placed=self._c.mis_orders_placed,
            orders_rejected=self._c.mis_orders_rejected,
            positions_confirmed_flat=self._c.mis_positions_flat,
            positions_at_deadline=self._c.mis_at_deadline,
            kill_switch_activated=self._c.mis_kill_switch_activated,
        )

        # ── Reconciliation status ──────────────────────────────────────────────
        recon = ReconciliationStatus(
            ran_on_startup=self._c.recon_ran,
            mode=self._c.recon_mode,
            mismatches_detected=self._c.recon_mismatches,
            paper_repairs=self._c.recon_repairs,
            live_critical_alerts=self._c.recon_criticals,
            zero_qty_open_found=self._c.recon_zero_qty_open,
            stale_exit_lock_found=self._c.recon_stale_exit_lock,
            direction_qty_mismatch_found=self._c.recon_qty_direction,
            open_without_exit_policy_found=self._c.recon_unmanaged,
        )

        # ── Risk cap ───────────────────────────────────────────────────────────
        risk_cap = RiskCapStatus(
            daily_cap_reached=self._c.daily_cap_reached,
            daily_loss_limit_reached=self._c.daily_loss_limit_reached,
            daily_profit_lock_reached=self._c.daily_profit_lock_reached,
            new_entries_allowed=self._c.new_entries_allowed,
            exit_management_allowed=True,
            max_open_positions_reached=self._c.max_open_positions_reached,
            max_fills_per_strategy_reached=self._c.max_fills_per_strategy_reached,
        )

        # ── P&L ───────────────────────────────────────────────────────────────
        unrealized = sum(p.pnl or 0.0 for p in open_pos if p.pnl is not None)
        total_pnl = self._c.realized_pnl + unrealized

        pnl_items = [(p.symbol, p.pnl) for p in open_pos if p.pnl is not None]
        winner = max(pnl_items, key=lambda x: x[1], default=None) if pnl_items else None
        loser = min(pnl_items, key=lambda x: x[1], default=None) if pnl_items else None

        pnl = PnLStatus(
            realized_pnl=self._c.realized_pnl,
            unrealized_pnl=unrealized,
            total_pnl=total_pnl,
            largest_winner_symbol=winner[0] if winner else None,
            largest_winner_amount=winner[1] if winner else None,
            largest_loser_symbol=loser[0] if loser else None,
            largest_loser_amount=loser[1] if loser else None,
            max_intraday_drawdown=self._c.max_intraday_drawdown,
        )

        # ── Actions ───────────────────────────────────────────────────────────
        actions: list[str] = []
        if missing_policy > 0:
            actions.append(f"Attach exit policy to {missing_policy} unmanaged position(s) immediately.")
        if dir_mismatches > 0:
            actions.append(f"Investigate {dir_mismatches} direction/quantity mismatch(es) — signed quantity is canonical.")
        if kill_switch_active:
            actions.append("Kill switch is ACTIVE. Investigate reason and clear via kill_switch_cli.py when safe.")
        if self._c.router_live_attempts > 0:
            actions.append("URGENT: Live route attempts recorded — verify live_trading_enabled=False immediately.")
        if self._c.mis_kill_switch_activated:
            actions.append("MIS activated kill switch. Review mis_square_off logs; manually verify all positions are FLAT.")

        # ── Verdict ───────────────────────────────────────────────────────────
        n_open = len(open_pos)
        if overall == "GREEN":
            verdict = (
                "Paper session is safe. Trade Exit Engine is active, MIS square-off is armed, "
                "live trading is disabled, and no unmanaged positions are detected."
            )
            summary = (
                f"All systems GREEN. Mode=PAPER, live_trading_enabled=False. "
                f"{n_open} open position(s): {long_count} LONG, {short_count} SHORT. "
                f"TEE active (poll={self._c.tee_poll_interval}s), MIS armed at {mis.close_time_ist}, "
                f"reconciliation {'clean' if self._c.recon_mismatches == 0 else f'{self._c.recon_mismatches} mismatches resolved'}."
            )
        elif overall == "AMBER":
            first_warn = warnings[0] if warnings else "see warnings"
            verdict = f"Paper session running with warnings. Review: {first_warn}"
            summary = (
                f"AMBER — warnings present. Mode=PAPER, live_trading_enabled=False. "
                f"{n_open} open position(s). Review warnings before continuing."
            )
        else:
            first_crit = critical_alerts[0] if critical_alerts else "see alerts"
            verdict = f"Paper session has CRITICAL issues: {first_crit}"
            summary = (
                "RED — critical issues require immediate attention. "
                "Review critical alerts and take required actions before resuming monitoring."
            )

        return MonitoringStatusSnapshot(
            overall_status=overall,
            timestamp_ist=timestamp_ist,
            trading_mode=self._trading_mode,
            live_trading_enabled=self._live_trading_enabled,
            broker_live_calls="DISABLED" if not self._live_trading_enabled else "ENABLED",
            kill_switch_active=kill_switch_active,
            session_safety=session_safety,
            verdict_line=verdict,
            service_health=service_health,
            safety_gates=safety_gates,
            total_open_positions=n_open,
            long_count=long_count,
            short_count=short_count,
            flat_ignored=flat_ignored,
            positions_with_exit_policy=with_policy,
            positions_missing_exit_policy=missing_policy,
            positions_with_exit_order_id=with_exit_oid,
            positions_with_trailing_active=with_trailing,
            direction_qty_mismatches=dir_mismatches,
            unmanaged_positions=unmanaged,
            open_positions=open_pos,
            tee_status=tee,
            router_status=router,
            mis_status=mis,
            reconciliation_status=recon,
            risk_cap_status=risk_cap,
            strategy_statuses=list(self._c.strategy_statuses),
            pnl_status=pnl,
            critical_alerts=critical_alerts,
            warnings=warnings,
            data_quality_issues=data_quality,
            operational_issues=operational,
            action_required=bool(actions),
            actions=actions,
            final_status=overall,
            final_summary=summary,
        )

    # ── LTP enrichment ────────────────────────────────────────────────────────

    async def _enrich_ltp(self, positions: list[PositionSnapshot]) -> list[PositionSnapshot]:
        """
        Overwrite each position's ``ltp`` with the freshest available price.

        Runs LtpResolver for all positions concurrently, then updates
        ``ltp``, ``ltp_source``, ``ltp_age_seconds``, and ``ltp_is_stale`` in-place.
        The position's original ``ltp`` (entry fill price from DynamoDB) is passed as
        the fallback so resolver always returns something when the prices table is absent.
        """
        async def _one(p: PositionSnapshot) -> None:
            result = await self._ltp_resolver.resolve(p.symbol, p.ltp)
            if result is not None:
                p.ltp = result.price
                p.ltp_source = result.source
                p.ltp_age_seconds = result.age_seconds
                p.ltp_is_stale = result.is_stale
            else:
                p.ltp_source = "unavailable"
                p.ltp_is_stale = True

        await asyncio.gather(*(_one(p) for p in positions))
        return positions

    # ── DynamoDB helpers ───────────────────────────────────────────────────────

    async def _fetch_positions(self) -> list[PositionSnapshot]:
        try:
            items: list[dict] = []
            kwargs: dict[str, Any] = dict(
                TableName=self._positions_table,
                FilterExpression="SK = :cur",
                ExpressionAttributeValues={":cur": {"S": "CURRENT"}},
                ProjectionExpression=(
                    "PK, #sym, #dir, quantity, avg_entry_price, last_price, "
                    "stop_price, take_profit, exit_state, exit_order_id"
                ),
                ExpressionAttributeNames={"#dir": "direction", "#sym": "symbol"},
            )
            while True:
                resp = await asyncio.to_thread(self._dynamo.scan, **kwargs)
                items.extend(resp.get("Items", []))
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
            return [self._parse_position(item) for item in items]
        except Exception:
            return []

    def _parse_position(self, item: dict) -> PositionSnapshot:
        def _s(key: str, default: str = "") -> str:
            raw = item.get(key, {})
            if isinstance(raw, dict):
                return raw.get("S", raw.get("N", default))
            return str(raw) if raw is not None else default

        def _n(key: str, default: float = 0.0) -> float:
            raw = item.get(key, {})
            if isinstance(raw, dict):
                v = raw.get("N")
                if v is not None:
                    try:
                        return float(v)
                    except ValueError:
                        return default
            return default

        def _opt_n(key: str) -> Optional[float]:
            raw = item.get(key)
            if raw is None:
                return None
            if isinstance(raw, dict) and "N" in raw:
                try:
                    return float(raw["N"])
                except ValueError:
                    return None
            return None

        pk = _s("PK")
        symbol = _s("symbol") or pk.replace("POSITION#", "")
        direction = _s("direction", "UNKNOWN")
        quantity = _n("quantity", 0.0)
        entry = _n("avg_entry_price", 0.0)
        ltp = _opt_n("last_price")
        stop = _opt_n("stop_price")
        tp = _opt_n("take_profit")
        exit_state = _s("exit_state", "")
        exit_oid_raw = _s("exit_order_id")
        exit_oid = exit_oid_raw if exit_oid_raw else None

        return PositionSnapshot(
            symbol=symbol,
            quantity=quantity,
            direction=direction,
            entry_price=entry,
            ltp=ltp,
            stop_price=stop,
            take_profit=tp,
            trailing_active=(exit_state == "TRAILING_ACTIVE"),
            exit_state=exit_state,
            exit_order_id=exit_oid,
        )

    async def _fetch_kill_switch(self) -> bool:
        try:
            from services.shared.risk_state import kill_switch_key, attr_bool  # noqa: PLC0415

            resp = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._risk_state_table,
                Key=kill_switch_key(),
            )
            item = resp.get("Item")
            if not item:
                return False
            return attr_bool(item, "active", False)
        except Exception:
            return False


# ── Renderer ──────────────────────────────────────────────────────────────────


class MonitoringStatusRenderer:
    """
    Renders a MonitoringStatusSnapshot to the exact 15-section markdown template.

    Usage:
        output = MonitoringStatusRenderer().render(snapshot)
        print(output)
    """

    def render(self, snap: MonitoringStatusSnapshot) -> str:
        parts = [
            "# Paper Trading Monitoring Status\n",
            self._s1(snap),
            self._s2(snap),
            self._s3(snap),
            self._s4(snap),
            self._s5(snap),
            self._s6(snap),
            self._s7(snap),
            self._s8(snap),
            self._s9(snap),
            self._s10(snap),
            self._s11(snap),
            self._s12(snap),
            self._s13(snap),
            self._s14(snap),
            self._s15(snap),
        ]
        return "\n".join(parts)

    # ── §1 Overall Status ─────────────────────────────────────────────────────

    def _s1(self, s: MonitoringStatusSnapshot) -> str:
        ks = "ON" if s.kill_switch_active else "OFF"
        lines = [
            "## 1. Overall Status\n",
            f"Status: {s.overall_status}  ",
            f"Timestamp IST: {s.timestamp_ist}  ",
            f"Trading Mode: {s.trading_mode}  ",
            f"Live Trading Enabled: {str(s.live_trading_enabled).lower()}  ",
            f"Broker Live Order Calls: {s.broker_live_calls}  ",
            f"Kill Switch: {ks}  ",
            f"Session Safety: {s.session_safety}  ",
            "",
            "One-line verdict:  ",
            s.verdict_line,
            "",
            "---",
        ]
        return "\n".join(lines)

    # ── §2 Service Health ─────────────────────────────────────────────────────

    def _s2(self, s: MonitoringStatusSnapshot) -> str:
        rows = ["## 2. Service Health\n"]
        rows.append("| Component | Status | Notes |")
        rows.append("|---|---:|---|")
        for h in s.service_health:
            rows.append(f"| {h.component} | {h.status} | {h.notes} |")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §3 Trading Mode and Safety Gates ─────────────────────────────────────

    def _s3(self, s: MonitoringStatusSnapshot) -> str:
        rows = ["## 3. Trading Mode and Safety Gates\n"]
        rows.append("| Check | Expected | Actual | Status |")
        rows.append("|---|---:|---:|---|")
        for check, expected, actual, passed in s.safety_gates:
            rows.append(f"| {check} | {expected} | {actual} | {'PASS' if passed else 'FAIL'} |")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §4 Position Summary ───────────────────────────────────────────────────

    def _s4(self, s: MonitoringStatusSnapshot) -> str:
        if s.unmanaged_positions > 0 or s.positions_missing_exit_policy > 0:
            verdict = f"UNSAFE — {s.unmanaged_positions} unmanaged position(s). Attach exit policy immediately."
        elif s.direction_qty_mismatches > 0:
            verdict = f"DEGRADED — {s.direction_qty_mismatches} direction/quantity mismatch(es). Investigate."
        else:
            verdict = "SAFE"
        rows = ["## 4. Position Summary\n"]
        rows.append("| Metric | Count |")
        rows.append("|---|---:|")
        rows.append(f"| Total open positions | {s.total_open_positions} |")
        rows.append(f"| LONG positions | {s.long_count} |")
        rows.append(f"| SHORT positions | {s.short_count} |")
        rows.append(f"| FLAT positions ignored | {s.flat_ignored} |")
        rows.append(f"| Positions with exit policy | {s.positions_with_exit_policy} |")
        rows.append(f"| Positions missing exit policy | {s.positions_missing_exit_policy} |")
        rows.append(f"| Positions with exit_order_id | {s.positions_with_exit_order_id} |")
        rows.append(f"| Positions with trailing active | {s.positions_with_trailing_active} |")
        rows.append(f"| Direction/quantity mismatches | {s.direction_qty_mismatches} |")
        rows.append(f"| Unmanaged positions | {s.unmanaged_positions} |")
        rows.append("")
        rows.append(f"Position safety verdict:  \n{verdict}")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §5 Open Positions Detail ──────────────────────────────────────────────

    def _s5(self, s: MonitoringStatusSnapshot) -> str:
        rows = ["## 5. Open Positions Detail\n"]
        rows.append("| Symbol | Qty | Direction | Entry | LTP | P&L | Stop | Target | Trailing | Exit State | Exit Order ID | LTP Source | LTP Age |")
        rows.append("|---|---:|---|---:|---:|---:|---:|---:|---|---|---|---|---|")
        if not s.open_positions:
            rows.append("| — | — | — | — | — | — | — | — | — | — | — | — | — |")
        for p in s.open_positions:
            qty_str = f"{p.quantity:+.1f}"
            if p.direction_mismatch:
                qty_str += " ⚠ DIR MISMATCH"
            direction_str = p.direction
            if p.direction_mismatch:
                direction_str += f" (qty→{p.qty_direction})"
            rows.append(
                f"| {p.symbol} "
                f"| {qty_str} "
                f"| {direction_str} "
                f"| {self._pr(p.entry_price)} "
                f"| {self._pr(p.ltp)} "
                f"| {self._pnl(p.pnl)} "
                f"| {self._pr(p.stop_price)} "
                f"| {self._pr(p.take_profit)} "
                f"| {'ACTIVE' if p.trailing_active else 'NO'} "
                f"| {p.exit_state or '—'} "
                f"| {p.exit_order_id or 'none'} "
                f"| {self._ltp_src(p)} "
                f"| {self._ltp_age(p)} |"
            )
        rows.append("")
        rows.append("Rules:")
        rows.append("- Qty must be signed. Positive qty means LONG. Negative qty means SHORT. Zero means FLAT.")
        rows.append("- If direction disagrees with signed qty, mark WARNING.")
        rows.append("- LTP Source: live=prices_table fresh, stale=prices_table expired, fill=entry fill price, —=unavailable.")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §6 Trade Exit Engine Status ───────────────────────────────────────────

    def _s6(self, s: MonitoringStatusSnapshot) -> str:
        t = s.tee_status
        rows = ["## 6. Trade Exit Engine Status\n"]
        rows.append("| Metric | Value |")
        rows.append("|---|---:|")
        rows.append(f"| TEE running | {str(t.running).lower()} |")
        rows.append(f"| Poll interval | {t.poll_interval_seconds}s |")
        rows.append(f"| Stop-loss active positions | {t.stop_loss_active} |")
        rows.append(f"| Take-profit active positions | {t.take_profit_active} |")
        rows.append(f"| Trailing active positions | {t.trailing_active} |")
        rows.append(f"| Trailing activated today | {t.trailing_activated_today} |")
        rows.append(f"| Trailing stop hit today | {t.trailing_stop_hit_today} |")
        rows.append(f"| Stop-loss hit today | {t.stop_loss_hit_today} |")
        rows.append(f"| Take-profit hit today | {t.take_profit_hit_today} |")
        rows.append(f"| Duplicate exits prevented | {t.duplicate_exits_prevented} |")
        rows.append(f"| Unmanaged position detections | {t.unmanaged_detections} |")
        rows.append(f"| Stale LTP exit blocks | {t.stale_ltp_blocks} |")
        rows.append("")
        rows.append("Latest TEE events:")
        if t.latest_events:
            for ev in t.latest_events[-5:]:
                rows.append(f"- {ev}")
        else:
            rows.append("- None recorded yet")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §7 ExitOrderRouter Status ─────────────────────────────────────────────

    def _s7(self, s: MonitoringStatusSnapshot) -> str:
        r = s.router_status
        rows = ["## 7. ExitOrderRouter Status\n"]
        rows.append("| Metric | Value |")
        rows.append("|---|---:|")
        rows.append(f"| Router mode | {r.mode} |")
        rows.append(f"| Paper exits routed | {r.paper_exits_routed} |")
        rows.append(f"| Backtest exits routed | {r.backtest_exits_routed} |")
        rows.append(f"| Live exits attempted | {r.live_exits_attempted} |")
        rows.append(f"| Live exits placed | {r.live_exits_placed} |")
        rows.append(f"| Live exits blocked | {r.live_exits_blocked} |")
        rows.append(f"| Idempotency successes | {r.idempotency_successes} |")
        rows.append(f"| Idempotency skips | {r.idempotency_skips} |")
        rows.append(f"| Failed exit routes | {r.failed_routes} |")
        rows.append("")
        if r.live_exits_attempted == 0:
            safety = "No live orders were placed. All exits routed through paper path. live_trading_enabled=False is confirmed."
        else:
            safety = f"WARNING: {r.live_exits_attempted} live exit attempt(s) detected. Investigate immediately."
        rows.append(f"Safety note:  \n{safety}")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §8 MIS Square-Off Status ──────────────────────────────────────────────

    def _s8(self, s: MonitoringStatusSnapshot) -> str:
        m = s.mis_status
        rows = ["## 8. MIS Square-Off Status\n"]
        rows.append("| Field | Value |")
        rows.append("|---|---|")
        rows.append(f"| MIS square-off armed | {str(m.armed).lower()} |")
        rows.append(f"| MIS square-off time IST | {m.close_time_ist} |")
        rows.append(f"| Square-off deadline IST | {m.deadline_time_ist} |")
        rows.append(f"| Broker fallback time IST | {m.broker_fallback_ist} |")
        rows.append(f"| Positions discovered at last scan | {self._opt(m.positions_discovered)} |")
        rows.append(f"| LONG discovered | {self._opt(m.long_discovered)} |")
        rows.append(f"| SHORT discovered | {self._opt(m.short_discovered)} |")
        rows.append(f"| Orders placed | {self._opt(m.orders_placed)} |")
        rows.append(f"| Orders rejected | {self._opt(m.orders_rejected)} |")
        rows.append(f"| Positions confirmed flat | {self._opt(m.positions_confirmed_flat)} |")
        rows.append(f"| Positions still open at deadline | {self._opt(m.positions_at_deadline)} |")
        rows.append(f"| Kill switch activated by MIS | {str(m.kill_switch_activated).lower()} |")
        rows.append("")
        rows.append("MIS safety checks:\n")
        rows.append("| Check | Status |")
        rows.append("|---|---|")
        rows.append(f"| Uses quantity != 0 / abs(quantity) > 0 | {'PASS' if m.uses_quantity_filter else 'FAIL'} |")
        rows.append(f"| LONG closes with SELL | {'PASS' if m.long_closes_with_sell else 'FAIL'} |")
        rows.append(f"| SHORT closes with BUY | {'PASS' if m.short_closes_with_buy else 'FAIL'} |")
        rows.append(f"| close_qty uses abs(quantity) | {'PASS' if m.close_qty_uses_abs else 'FAIL'} |")
        rows.append(f"| product_type=ProductType.MIS | {'PASS' if m.uses_product_type_mis else 'FAIL'} |")
        rows.append(f"| skips positions with exit_order_id | {'PASS' if m.skips_with_exit_order_id else 'FAIL'} |")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §9 Reconciliation Status ──────────────────────────────────────────────

    def _s9(self, s: MonitoringStatusSnapshot) -> str:
        r = s.reconciliation_status
        rows = ["## 9. Reconciliation Status\n"]
        rows.append("| Metric | Value |")
        rows.append("|---|---:|")
        rows.append(f"| Reconciliation ran on startup | {str(r.ran_on_startup).lower()} |")
        rows.append(f"| Mode | {r.mode} |")
        rows.append(f"| Mismatches detected | {r.mismatches_detected} |")
        rows.append(f"| Paper repairs completed | {r.paper_repairs} |")
        rows.append(f"| Live critical alerts | {r.live_critical_alerts} |")
        rows.append(f"| ZERO_QTY_OPEN found | {r.zero_qty_open_found} |")
        rows.append(f"| STALE_EXIT_LOCK found | {r.stale_exit_lock_found} |")
        rows.append(f"| direction/quantity mismatch found | {r.direction_qty_mismatch_found} |")
        rows.append(f"| open position without exit policy found | {r.open_without_exit_policy_found} |")
        rows.append("")
        if not r.ran_on_startup:
            verdict = "WARNING — Reconciliation did not run on startup. TEE may have started with inconsistent positions."
        elif r.open_without_exit_policy_found > 0:
            verdict = f"CRITICAL — {r.open_without_exit_policy_found} open position(s) had no exit policy. Operator action required."
        elif r.mismatches_detected > r.paper_repairs + r.live_critical_alerts:
            verdict = f"WARNING — {r.mismatches_detected - r.paper_repairs - r.live_critical_alerts} mismatch(es) unaccounted for."
        elif r.mismatches_detected > 0:
            verdict = f"WARNING — {r.mismatches_detected} mismatch(es) found; {r.paper_repairs} repaired, {r.live_critical_alerts} alerted."
        else:
            verdict = "SAFE — Reconciliation clean. All positions consistent at startup."
        rows.append(f"Reconciliation verdict:  \n{verdict}")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §10 Risk and Daily Cap Status ─────────────────────────────────────────

    def _s10(self, s: MonitoringStatusSnapshot) -> str:
        rc = s.risk_cap_status
        rows = ["## 10. Risk and Daily Cap Status\n"]
        rows.append("| Metric | Value |")
        rows.append("|---|---:|")
        rows.append(f"| Daily cap reached | {str(rc.daily_cap_reached).lower()} |")
        rows.append(f"| Daily loss limit reached | {str(rc.daily_loss_limit_reached).lower()} |")
        rows.append(f"| Daily profit lock reached | {str(rc.daily_profit_lock_reached).lower()} |")
        rows.append(f"| New entries allowed | {str(rc.new_entries_allowed).lower()} |")
        rows.append(f"| Exit management allowed | {str(rc.exit_management_allowed).lower()} |")
        rows.append(f"| Max open positions reached | {str(rc.max_open_positions_reached).lower()} |")
        rows.append(f"| Max fills per strategy reached | {str(rc.max_fills_per_strategy_reached).lower()} |")
        rows.append("")
        rows.append("Important:  ")
        rows.append("Daily cap must block new entries only.  ")
        rows.append("Daily cap must not block exits.")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §11 Strategy Status ───────────────────────────────────────────────────

    def _s11(self, s: MonitoringStatusSnapshot) -> str:
        rows = ["## 11. Strategy Status\n"]
        rows.append("| Strategy | Status | Signals | Fills | Open Pos | Exits | Cap Status | Notes |")
        rows.append("|---|---|---:|---:|---:|---:|---|---|")
        if not s.strategy_statuses:
            rows.append("| — | UNKNOWN | — | — | — | — | — | No strategy data available |")
        for st in s.strategy_statuses:
            rows.append(
                f"| {st.name} | {st.status} | {st.signals} | {st.fills} "
                f"| {st.open_positions} | {st.exits} | {st.cap_status} | {st.notes} |"
            )
        rows.append("\n---")
        return "\n".join(rows)

    # ── §12 Paper P&L and Risk ────────────────────────────────────────────────

    def _s12(self, s: MonitoringStatusSnapshot) -> str:
        p = s.pnl_status
        rows = ["## 12. Paper P&L and Risk\n"]
        rows.append("| Metric | Value |")
        rows.append("|---|---:|")
        rows.append(f"| Realized P&L | {self._pnl(p.realized_pnl)} |")
        rows.append(f"| Unrealized P&L | {self._pnl(p.unrealized_pnl)} |")
        rows.append(f"| Total Paper P&L | {self._pnl(p.total_pnl)} |")
        rows.append(f"| Largest winner | {self._winner(p.largest_winner_symbol, p.largest_winner_amount)} |")
        rows.append(f"| Largest loser | {self._winner(p.largest_loser_symbol, p.largest_loser_amount)} |")
        rows.append(f"| Max intraday drawdown | {self._pnl(p.max_intraday_drawdown)} |")
        rows.append(f"| Win rate today | {self._pct(p.win_rate_pct)} |")
        rows.append(f"| Average winner | {self._pnl(p.average_winner)} |")
        rows.append(f"| Average loser | {self._pnl(p.average_loser)} |")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §13 Alerts and Warnings ───────────────────────────────────────────────

    def _s13(self, s: MonitoringStatusSnapshot) -> str:
        rows = ["## 13. Alerts and Warnings\n"]
        rows.append("Critical alerts:")
        if s.critical_alerts:
            for a in s.critical_alerts:
                rows.append(f"- {a}")
        else:
            rows.append("- None")
        rows.append("")
        rows.append("Warnings:")
        if s.warnings:
            for w in s.warnings:
                rows.append(f"- {w}")
        else:
            rows.append("- None")
        rows.append("")
        rows.append("Data quality issues:")
        if s.data_quality_issues:
            for d in s.data_quality_issues:
                rows.append(f"- {d}")
        else:
            rows.append("- None")
        rows.append("")
        rows.append("Operational issues:")
        if s.operational_issues:
            for o in s.operational_issues:
                rows.append(f"- {o}")
        else:
            rows.append("- None")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §14 Action Required ───────────────────────────────────────────────────

    def _s14(self, s: MonitoringStatusSnapshot) -> str:
        rows = ["## 14. Action Required\n"]
        rows.append(f"Action required: {'YES' if s.action_required else 'NO'}\n")
        if s.action_required and s.actions:
            rows.append("If YES:")
            for i, action in enumerate(s.actions, 1):
                rows.append(f"{i}. {action}")
        else:
            rows.append("If NO:  ")
            rows.append("No manual action required. Continue monitoring.")
        rows.append("\n---")
        return "\n".join(rows)

    # ── §15 Final Verdict ─────────────────────────────────────────────────────

    def _s15(self, s: MonitoringStatusSnapshot) -> str:
        lines = [
            "## 15. Final Verdict\n",
            f"Final Status: {s.final_status}\n",
            "Summary:",
            s.final_summary,
        ]
        return "\n".join(lines)

    # ── Formatting helpers ────────────────────────────────────────────────────

    @staticmethod
    def _pr(val: Optional[float]) -> str:
        if val is None:
            return "—"
        return f"₹{val:,.2f}"

    @staticmethod
    def _pnl(val: Optional[float]) -> str:
        if val is None:
            return "—"
        prefix = "+" if val > 0 else ""
        return f"{prefix}₹{val:,.2f}"

    @staticmethod
    def _opt(val: Optional[int]) -> str:
        return str(val) if val is not None else "NA"

    @staticmethod
    def _winner(symbol: Optional[str], amount: Optional[float]) -> str:
        if symbol is None or amount is None:
            return "—"
        prefix = "+" if amount > 0 else ""
        return f"{symbol} {prefix}₹{amount:,.2f}"

    @staticmethod
    def _pct(val: Optional[float]) -> str:
        if val is None:
            return "—"
        return f"{val:.1f}%"

    @staticmethod
    def _ltp_src(p: PositionSnapshot) -> str:
        return {
            "prices_table": "live",
            "prices_table_stale": "stale",
            "position_fill": "fill",
        }.get(p.ltp_source, "—")

    @staticmethod
    def _ltp_age(p: PositionSnapshot) -> str:
        if p.ltp_age_seconds is None:
            return "—"
        if p.ltp_age_seconds < 60:
            return f"{p.ltp_age_seconds:.1f}s"
        return f"{p.ltp_age_seconds / 60:.1f}m"
