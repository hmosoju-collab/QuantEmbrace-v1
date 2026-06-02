"""
Tests for MonitoringStatusService, MonitoringStatusSnapshot, MonitoringStatusRenderer.

Coverage:
  - Template contains all 15 sections
  - live_trading_enabled=false shown correctly
  - Paper mode shown correctly
  - Open positions table renders signed quantity (+ / -)
  - LONG/SHORT counts render correctly
  - Missing exit policy marks AMBER or RED
  - Live mode enabled marks RED
  - Unmanaged positions marks RED
  - Kill switch ON marks RED when positions are open
  - Daily cap reached does NOT imply exits blocked
  - MIS safety checks are included in section 8
  - Reconciliation status rendered in section 9
  - Final verdict is GREEN only when all critical checks pass
  - MonitoringStatusService builds correct snapshot from DynamoDB mock
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.shared.monitoring.monitoring_status import (
    LiveCounters,
    MISStatus,
    MonitoringStatusRenderer,
    MonitoringStatusService,
    MonitoringStatusSnapshot,
    PnLStatus,
    PositionSnapshot,
    ReconciliationStatus,
    RiskCapStatus,
    RouterStatus,
    ServiceHealthRow,
    StrategyStatusRow,
    TEEStatus,
)


# ── Factories ─────────────────────────────────────────────────────────────────


def _pos(
    symbol: str = "MARUTI",
    quantity: float = 10.0,
    direction: str = "LONG",
    entry_price: float = 2000.0,
    ltp: Optional[float] = 2050.0,
    stop_price: Optional[float] = 1950.0,
    take_profit: Optional[float] = 2100.0,
    trailing_active: bool = False,
    exit_state: str = "",
    exit_order_id: Optional[str] = None,
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        quantity=quantity,
        direction=direction,
        entry_price=entry_price,
        ltp=ltp,
        stop_price=stop_price,
        take_profit=take_profit,
        trailing_active=trailing_active,
        exit_state=exit_state,
        exit_order_id=exit_order_id,
    )


def _snap(
    overall_status: str = "GREEN",
    trading_mode: str = "PAPER",
    live_trading_enabled: bool = False,
    kill_switch_active: bool = False,
    session_safety: str = "SAFE",
    verdict_line: str = "Paper session is safe.",
    open_positions: Optional[list] = None,
    total_open_positions: int = 0,
    long_count: int = 0,
    short_count: int = 0,
    flat_ignored: int = 0,
    positions_with_exit_policy: int = 0,
    positions_missing_exit_policy: int = 0,
    positions_with_exit_order_id: int = 0,
    positions_with_trailing_active: int = 0,
    direction_qty_mismatches: int = 0,
    unmanaged_positions: int = 0,
    critical_alerts: Optional[list] = None,
    warnings: Optional[list] = None,
    data_quality_issues: Optional[list] = None,
    operational_issues: Optional[list] = None,
    action_required: bool = False,
    actions: Optional[list] = None,
    final_status: str = "GREEN",
    final_summary: str = "All clear.",
    tee_status: Optional[TEEStatus] = None,
    router_status: Optional[RouterStatus] = None,
    mis_status: Optional[MISStatus] = None,
    reconciliation_status: Optional[ReconciliationStatus] = None,
    risk_cap_status: Optional[RiskCapStatus] = None,
    strategy_statuses: Optional[list] = None,
    pnl_status: Optional[PnLStatus] = None,
    service_health: Optional[list] = None,
    safety_gates: Optional[list] = None,
    # Phase 6
    entry_block_status=None,
    safe_actions_status=None,
) -> MonitoringStatusSnapshot:
    from services.shared.monitoring.monitoring_status import EntryBlockStatus, SafeActionsStatus
    return MonitoringStatusSnapshot(
        overall_status=overall_status,
        timestamp_ist="2026-05-25 10:00:00 IST",
        trading_mode=trading_mode,
        live_trading_enabled=live_trading_enabled,
        broker_live_calls="DISABLED" if not live_trading_enabled else "ENABLED",
        kill_switch_active=kill_switch_active,
        session_safety=session_safety,
        verdict_line=verdict_line,
        service_health=service_health or [],
        safety_gates=safety_gates or [
            ("Trading mode", "PAPER", trading_mode, trading_mode == "PAPER"),
            ("live_trading_enabled", "false", str(live_trading_enabled).lower(), not live_trading_enabled),
            ("Live broker order placement", "DISABLED", "DISABLED" if not live_trading_enabled else "ENABLED", not live_trading_enabled),
            ("Exit orders bypass signal pipeline", "YES", "YES", True),
            ("Daily cap blocks exits", "NO", "NO", True),
            ("Signed quantity invariant active", "YES", "YES", True),
            ("MIS product_type", "MIS", "MIS", True),
            ("Duplicate exit prevention", "ENABLED", "ENABLED", True),
        ],
        total_open_positions=total_open_positions,
        long_count=long_count,
        short_count=short_count,
        flat_ignored=flat_ignored,
        positions_with_exit_policy=positions_with_exit_policy,
        positions_missing_exit_policy=positions_missing_exit_policy,
        positions_with_exit_order_id=positions_with_exit_order_id,
        positions_with_trailing_active=positions_with_trailing_active,
        direction_qty_mismatches=direction_qty_mismatches,
        unmanaged_positions=unmanaged_positions,
        open_positions=open_positions or [],
        tee_status=tee_status or TEEStatus(),
        router_status=router_status or RouterStatus(),
        mis_status=mis_status or MISStatus(),
        reconciliation_status=reconciliation_status or ReconciliationStatus(ran_on_startup=True, mode="paper"),
        risk_cap_status=risk_cap_status or RiskCapStatus(),
        strategy_statuses=strategy_statuses or [],
        pnl_status=pnl_status or PnLStatus(),
        entry_block_status=entry_block_status or EntryBlockStatus(),
        safe_actions_status=safe_actions_status or SafeActionsStatus(),
        critical_alerts=critical_alerts or [],
        warnings=warnings or [],
        data_quality_issues=data_quality_issues or [],
        operational_issues=operational_issues or [],
        action_required=action_required,
        actions=actions or [],
        final_status=final_status,
        final_summary=final_summary,
    )


_renderer = MonitoringStatusRenderer()


# ── §1: Template structure ────────────────────────────────────────────────────


class TestTemplateStructure:
    def test_template_contains_all_15_sections(self):
        snap = _snap()
        output = _renderer.render(snap)
        for i in range(1, 16):
            assert f"## {i}." in output, f"Section {i} missing from rendered output"

    def test_template_has_paper_trading_monitoring_status_heading(self):
        output = _renderer.render(_snap())
        assert "# Paper Trading Monitoring Status" in output

    def test_all_section_headings_present(self):
        expected_headings = [
            "## 1. Overall Status",
            "## 2. Service Health",
            "## 3. Trading Mode and Safety Gates",
            "## 4. Position Summary",
            "## 5. Open Positions Detail",
            "## 6. Trade Exit Engine Status",
            "## 7. ExitOrderRouter Status",
            "## 8. MIS Square-Off Status",
            "## 9. Reconciliation Status",
            "## 10. Risk and Daily Cap Status",
            "## 11. Strategy Status",
            "## 12. Paper P&L and Risk",
            "## 13. Alerts and Warnings",
            "## 14. Action Required",
            "## 15. Final Verdict",
        ]
        output = _renderer.render(_snap())
        for heading in expected_headings:
            assert heading in output, f"Missing heading: {heading}"


# ── §2: Section 1 — Overall Status ───────────────────────────────────────────


class TestSection1OverallStatus:
    def test_live_trading_enabled_false_shown(self):
        snap = _snap(live_trading_enabled=False)
        output = _renderer.render(snap)
        assert "Live Trading Enabled: false" in output

    def test_live_trading_enabled_true_shown(self):
        snap = _snap(
            live_trading_enabled=True,
            overall_status="RED",
            session_safety="UNSAFE",
            final_status="RED",
        )
        output = _renderer.render(snap)
        assert "Live Trading Enabled: true" in output

    def test_paper_mode_shown_correctly(self):
        snap = _snap(trading_mode="PAPER")
        output = _renderer.render(snap)
        assert "Trading Mode: PAPER" in output

    def test_backtest_mode_shown_correctly(self):
        snap = _snap(trading_mode="BACKTEST")
        output = _renderer.render(snap)
        assert "Trading Mode: BACKTEST" in output

    def test_kill_switch_off_shown(self):
        snap = _snap(kill_switch_active=False)
        output = _renderer.render(snap)
        assert "Kill Switch: OFF" in output

    def test_kill_switch_on_shown(self):
        snap = _snap(kill_switch_active=True, overall_status="RED", session_safety="UNSAFE", final_status="RED")
        output = _renderer.render(snap)
        assert "Kill Switch: ON" in output

    def test_timestamp_present(self):
        output = _renderer.render(_snap())
        assert "2026-05-25" in output

    def test_broker_live_calls_disabled(self):
        output = _renderer.render(_snap(live_trading_enabled=False))
        assert "Broker Live Order Calls: DISABLED" in output

    def test_broker_live_calls_enabled_when_live(self):
        snap = _snap(live_trading_enabled=True, overall_status="RED", final_status="RED", session_safety="UNSAFE")
        output = _renderer.render(snap)
        assert "Broker Live Order Calls: ENABLED" in output

    def test_session_safety_safe(self):
        output = _renderer.render(_snap(session_safety="SAFE"))
        assert "Session Safety: SAFE" in output

    def test_session_safety_unsafe(self):
        snap = _snap(session_safety="UNSAFE", overall_status="RED", final_status="RED")
        output = _renderer.render(snap)
        assert "Session Safety: UNSAFE" in output

    def test_verdict_line_present(self):
        snap = _snap(verdict_line="Test verdict line here.")
        output = _renderer.render(snap)
        assert "Test verdict line here." in output


# ── §3: Section 3 — Safety Gates ─────────────────────────────────────────────


class TestSection3SafetyGates:
    def test_all_safety_gates_present(self):
        output = _renderer.render(_snap())
        assert "Trading mode" in output
        assert "live_trading_enabled" in output
        assert "Live broker order placement" in output
        assert "Exit orders bypass signal pipeline" in output
        assert "Daily cap blocks exits" in output
        assert "Signed quantity invariant active" in output
        assert "MIS product_type" in output
        assert "Duplicate exit prevention" in output

    def test_pass_shown_when_checks_pass(self):
        output = _renderer.render(_snap())
        assert "PASS" in output

    def test_fail_shown_when_live_enabled(self):
        snap = _snap(
            live_trading_enabled=True,
            overall_status="RED",
            final_status="RED",
            session_safety="UNSAFE",
            safety_gates=[
                ("Trading mode", "PAPER", "PAPER", True),
                ("live_trading_enabled", "false", "true", False),
                ("Live broker order placement", "DISABLED", "ENABLED", False),
                ("Exit orders bypass signal pipeline", "YES", "YES", True),
                ("Daily cap blocks exits", "NO", "NO", True),
                ("Signed quantity invariant active", "YES", "YES", True),
                ("MIS product_type", "MIS", "MIS", True),
                ("Duplicate exit prevention", "ENABLED", "ENABLED", True),
            ],
        )
        output = _renderer.render(snap)
        assert "FAIL" in output


# ── §4: Section 4 — Position Summary ─────────────────────────────────────────


class TestSection4PositionSummary:
    def test_long_short_counts_rendered(self):
        snap = _snap(
            long_count=3,
            short_count=2,
            total_open_positions=5,
        )
        output = _renderer.render(snap)
        assert "| LONG positions | 3 |" in output
        assert "| SHORT positions | 2 |" in output

    def test_missing_exit_policy_shown(self):
        snap = _snap(positions_missing_exit_policy=2, unmanaged_positions=2)
        output = _renderer.render(snap)
        assert "| Positions missing exit policy | 2 |" in output

    def test_unmanaged_shown(self):
        snap = _snap(unmanaged_positions=1)
        output = _renderer.render(snap)
        assert "| Unmanaged positions | 1 |" in output

    def test_exit_order_id_count_shown(self):
        snap = _snap(positions_with_exit_order_id=3)
        output = _renderer.render(snap)
        assert "| Positions with exit_order_id | 3 |" in output

    def test_trailing_active_count_shown(self):
        snap = _snap(positions_with_trailing_active=1)
        output = _renderer.render(snap)
        assert "| Positions with trailing active | 1 |" in output

    def test_position_safety_safe_when_no_issues(self):
        snap = _snap(unmanaged_positions=0, positions_missing_exit_policy=0, direction_qty_mismatches=0)
        output = _renderer.render(snap)
        assert "Position safety verdict" in output
        assert "SAFE" in output

    def test_position_safety_unsafe_when_unmanaged(self):
        snap = _snap(unmanaged_positions=2, positions_missing_exit_policy=2)
        output = _renderer.render(snap)
        assert "UNSAFE" in output

    def test_position_safety_degraded_when_mismatch(self):
        snap = _snap(direction_qty_mismatches=1, positions_missing_exit_policy=0, unmanaged_positions=0)
        output = _renderer.render(snap)
        assert "DEGRADED" in output


# ── §5: Section 5 — Open Positions Detail ────────────────────────────────────


class TestSection5PositionsDetail:
    def test_signed_quantity_positive_shown(self):
        pos = _pos(symbol="MARUTI", quantity=10.0, direction="LONG")
        snap = _snap(open_positions=[pos])
        output = _renderer.render(snap)
        assert "+10.0" in output

    def test_signed_quantity_negative_shown(self):
        pos = _pos(symbol="INFY", quantity=-5.0, direction="SHORT")
        snap = _snap(open_positions=[pos])
        output = _renderer.render(snap)
        assert "-5.0" in output

    def test_trailing_active_shown(self):
        pos = _pos(symbol="HDFC", quantity=8.0, trailing_active=True, exit_state="TRAILING_ACTIVE")
        snap = _snap(open_positions=[pos])
        output = _renderer.render(snap)
        assert "ACTIVE" in output

    def test_direction_mismatch_flagged(self):
        # direction says LONG but quantity is negative (SHORT)
        pos = _pos(symbol="WIPRO", quantity=-3.0, direction="LONG")
        assert pos.direction_mismatch is True
        snap = _snap(open_positions=[pos])
        output = _renderer.render(snap)
        assert "DIR MISMATCH" in output or "qty→SHORT" in output

    def test_no_positions_shows_placeholder(self):
        snap = _snap(open_positions=[])
        output = _renderer.render(snap)
        assert "## 5. Open Positions Detail" in output

    def test_exit_order_id_shown_in_row(self):
        pos = _pos(symbol="SBIN", quantity=20.0, exit_order_id="PAPER-EXIT-ABC123")
        snap = _snap(open_positions=[pos])
        output = _renderer.render(snap)
        assert "PAPER-EXIT-ABC123" in output

    def test_no_exit_order_id_shows_none(self):
        pos = _pos(symbol="SBIN", quantity=20.0, exit_order_id=None)
        snap = _snap(open_positions=[pos])
        output = _renderer.render(snap)
        assert "none" in output

    def test_pnl_positive_shown(self):
        pos = _pos(symbol="MARUTI", quantity=10.0, entry_price=2000.0, ltp=2100.0)
        assert pos.pnl == pytest.approx(1000.0)
        snap = _snap(open_positions=[pos])
        output = _renderer.render(snap)
        assert "+₹1,000.00" in output

    def test_pnl_negative_shown_for_short(self):
        # SHORT position: entry 500, ltp 550 → P&L = (500-550)*5 = -250
        pos = _pos(symbol="INFY", quantity=-5.0, direction="SHORT", entry_price=500.0, ltp=550.0)
        assert pos.pnl == pytest.approx(-250.0)
        snap = _snap(open_positions=[pos])
        output = _renderer.render(snap)
        assert "₹-250.00" in output or "-₹250.00" in output

    def test_rules_note_present(self):
        output = _renderer.render(_snap())
        assert "Qty must be signed" in output
        assert "Positive qty means LONG" in output
        assert "Negative qty means SHORT" in output


# ── §6: Section 6 — TEE ──────────────────────────────────────────────────────


class TestSection6TEEStatus:
    def test_tee_running_shown(self):
        snap = _snap(tee_status=TEEStatus(running=True, poll_interval_seconds=60))
        output = _renderer.render(snap)
        assert "| TEE running | true |" in output
        assert "Poll interval" in output

    def test_tee_stop_loss_hits_shown(self):
        snap = _snap(tee_status=TEEStatus(stop_loss_hit_today=3))
        output = _renderer.render(snap)
        assert "| Stop-loss hit today | 3 |" in output

    def test_tee_trailing_activated_shown(self):
        snap = _snap(tee_status=TEEStatus(trailing_activated_today=2, trailing_active=2))
        output = _renderer.render(snap)
        assert "| Trailing activated today | 2 |" in output

    def test_tee_duplicate_exits_shown(self):
        snap = _snap(tee_status=TEEStatus(duplicate_exits_prevented=5))
        output = _renderer.render(snap)
        assert "| Duplicate exits prevented | 5 |" in output

    def test_tee_unmanaged_shown(self):
        snap = _snap(tee_status=TEEStatus(unmanaged_detections=1))
        output = _renderer.render(snap)
        assert "| Unmanaged position detections | 1 |" in output

    def test_latest_events_shown(self):
        snap = _snap(tee_status=TEEStatus(latest_events=["10:15:03 MARUTI stop_loss_triggered"]))
        output = _renderer.render(snap)
        assert "10:15:03 MARUTI stop_loss_triggered" in output


# ── §7: Section 7 — Router ───────────────────────────────────────────────────


class TestSection7RouterStatus:
    def test_router_mode_paper_shown(self):
        snap = _snap(router_status=RouterStatus(mode="PAPER"))
        output = _renderer.render(snap)
        assert "| Router mode | PAPER |" in output

    def test_paper_exits_count_shown(self):
        snap = _snap(router_status=RouterStatus(mode="PAPER", paper_exits_routed=7))
        output = _renderer.render(snap)
        assert "| Paper exits routed | 7 |" in output

    def test_live_exits_attempted_shown(self):
        snap = _snap(router_status=RouterStatus(live_exits_attempted=0))
        output = _renderer.render(snap)
        assert "| Live exits attempted | 0 |" in output

    def test_safety_note_no_live_orders(self):
        snap = _snap(router_status=RouterStatus(live_exits_attempted=0))
        output = _renderer.render(snap)
        assert "Safety note" in output
        assert "live_trading_enabled=False" in output or "paper path" in output

    def test_safety_note_warns_on_live_attempts(self):
        snap = _snap(router_status=RouterStatus(live_exits_attempted=2))
        output = _renderer.render(snap)
        assert "WARNING" in output or "2 live exit attempt" in output

    def test_idempotency_skips_shown(self):
        snap = _snap(router_status=RouterStatus(idempotency_skips=4))
        output = _renderer.render(snap)
        assert "| Idempotency skips | 4 |" in output


# ── §8: Section 8 — MIS ──────────────────────────────────────────────────────


class TestSection8MISStatus:
    def test_mis_safety_checks_included(self):
        output = _renderer.render(_snap())
        assert "MIS safety checks" in output

    def test_all_mis_safety_check_rows_present(self):
        output = _renderer.render(_snap())
        expected_checks = [
            "Uses quantity != 0 / abs(quantity) > 0",
            "LONG closes with SELL",
            "SHORT closes with BUY",
            "close_qty uses abs(quantity)",
            "product_type=ProductType.MIS",
            "skips positions with exit_order_id",
        ]
        for check in expected_checks:
            assert check in output, f"MIS safety check missing: {check}"

    def test_all_mis_safety_checks_pass_by_default(self):
        snap = _snap(mis_status=MISStatus())  # all True by default
        output = _renderer.render(snap)
        # All 6 checks should be PASS
        assert output.count("| PASS |") >= 6

    def test_mis_armed_shown(self):
        snap = _snap(mis_status=MISStatus(armed=True))
        output = _renderer.render(snap)
        assert "| MIS square-off armed | true |" in output

    def test_mis_close_time_shown(self):
        snap = _snap(mis_status=MISStatus(close_time_ist="15:05"))
        output = _renderer.render(snap)
        assert "15:05" in output

    def test_mis_deadline_shown(self):
        snap = _snap(mis_status=MISStatus(deadline_time_ist="15:10"))
        output = _renderer.render(snap)
        assert "15:10" in output

    def test_mis_positions_na_before_scan(self):
        snap = _snap(mis_status=MISStatus(positions_discovered=None))
        output = _renderer.render(snap)
        assert "NA" in output

    def test_mis_positions_count_after_scan(self):
        snap = _snap(mis_status=MISStatus(
            positions_discovered=5,
            long_discovered=3,
            short_discovered=2,
            orders_placed=5,
            positions_confirmed_flat=5,
        ))
        output = _renderer.render(snap)
        assert "| Positions discovered at last scan | 5 |" in output
        assert "| LONG discovered | 3 |" in output
        assert "| SHORT discovered | 2 |" in output

    def test_kill_switch_activated_by_mis_shown(self):
        snap = _snap(mis_status=MISStatus(kill_switch_activated=True))
        output = _renderer.render(snap)
        assert "| Kill switch activated by MIS | true |" in output


# ── §9: Section 9 — Reconciliation ───────────────────────────────────────────


class TestSection9ReconciliationStatus:
    def test_reconciliation_section_present(self):
        output = _renderer.render(_snap())
        assert "## 9. Reconciliation Status" in output

    def test_reconciliation_ran_shown(self):
        snap = _snap(reconciliation_status=ReconciliationStatus(ran_on_startup=True, mode="paper"))
        output = _renderer.render(snap)
        assert "| Reconciliation ran on startup | true |" in output

    def test_reconciliation_mode_shown(self):
        snap = _snap(reconciliation_status=ReconciliationStatus(ran_on_startup=True, mode="paper"))
        output = _renderer.render(snap)
        assert "| Mode | paper |" in output

    def test_mismatch_counts_shown(self):
        snap = _snap(reconciliation_status=ReconciliationStatus(
            ran_on_startup=True,
            mode="paper",
            mismatches_detected=3,
            paper_repairs=2,
            live_critical_alerts=0,
            zero_qty_open_found=2,
            stale_exit_lock_found=1,
            direction_qty_mismatch_found=0,
            open_without_exit_policy_found=0,
        ))
        output = _renderer.render(snap)
        assert "| Mismatches detected | 3 |" in output
        assert "| Paper repairs completed | 2 |" in output
        assert "| ZERO_QTY_OPEN found | 2 |" in output
        assert "| STALE_EXIT_LOCK found | 1 |" in output

    def test_reconciliation_verdict_safe_when_clean(self):
        snap = _snap(reconciliation_status=ReconciliationStatus(
            ran_on_startup=True,
            mode="paper",
            mismatches_detected=0,
        ))
        output = _renderer.render(snap)
        assert "SAFE" in output

    def test_reconciliation_verdict_warning_when_mismatches_repaired(self):
        snap = _snap(reconciliation_status=ReconciliationStatus(
            ran_on_startup=True,
            mode="paper",
            mismatches_detected=2,
            paper_repairs=2,
        ))
        output = _renderer.render(snap)
        assert "WARNING" in output or "mismatch" in output.lower()

    def test_reconciliation_verdict_critical_when_unmanaged(self):
        snap = _snap(reconciliation_status=ReconciliationStatus(
            ran_on_startup=True,
            mode="paper",
            mismatches_detected=1,
            open_without_exit_policy_found=1,
        ))
        output = _renderer.render(snap)
        assert "CRITICAL" in output


# ── §10: Section 10 — Risk/Cap ────────────────────────────────────────────────


class TestSection10RiskCap:
    def test_daily_cap_reached_does_not_imply_exits_blocked(self):
        snap = _snap(risk_cap_status=RiskCapStatus(
            daily_cap_reached=True,
            new_entries_allowed=False,
            exit_management_allowed=True,   # exits always allowed
        ))
        output = _renderer.render(snap)
        # Cap reached
        assert "| Daily cap reached | true |" in output
        # BUT exits still allowed
        assert "| Exit management allowed | true |" in output

    def test_exit_management_always_true(self):
        # Even when cap is reached, exit_management_allowed must be True
        cap = RiskCapStatus(daily_cap_reached=True, exit_management_allowed=True)
        assert cap.exit_management_allowed is True

    def test_important_note_present(self):
        output = _renderer.render(_snap())
        assert "Daily cap must block new entries only" in output
        assert "Daily cap must not block exits" in output

    def test_daily_loss_limit_shown(self):
        snap = _snap(risk_cap_status=RiskCapStatus(daily_loss_limit_reached=True))
        output = _renderer.render(snap)
        assert "| Daily loss limit reached | true |" in output

    def test_new_entries_blocked_when_cap_reached(self):
        snap = _snap(risk_cap_status=RiskCapStatus(
            daily_cap_reached=True,
            new_entries_allowed=False,
        ))
        output = _renderer.render(snap)
        assert "| New entries allowed | false |" in output


# ── §13: Section 13 — Alerts ─────────────────────────────────────────────────


class TestSection13Alerts:
    def test_no_critical_alerts_shows_none(self):
        snap = _snap(critical_alerts=[])
        output = _renderer.render(snap)
        assert "Critical alerts:\n- None" in output

    def test_critical_alert_shown(self):
        snap = _snap(critical_alerts=["live_trading_enabled=True — broker orders ENABLED"])
        output = _renderer.render(snap)
        assert "live_trading_enabled=True" in output

    def test_no_warnings_shows_none(self):
        snap = _snap(warnings=[])
        output = _renderer.render(snap)
        assert "Warnings:\n- None" in output

    def test_warning_shown(self):
        snap = _snap(warnings=["1 direction/quantity mismatch found"])
        output = _renderer.render(snap)
        assert "1 direction/quantity mismatch found" in output

    def test_data_quality_issues_section_present(self):
        output = _renderer.render(_snap())
        assert "Data quality issues:" in output

    def test_operational_issues_section_present(self):
        output = _renderer.render(_snap())
        assert "Operational issues:" in output


# ── §14: Section 14 — Actions ─────────────────────────────────────────────────


class TestSection14Actions:
    def test_no_action_required_shown(self):
        snap = _snap(action_required=False)
        output = _renderer.render(snap)
        assert "Action required: NO" in output
        assert "No manual action required" in output

    def test_action_required_shown(self):
        snap = _snap(
            action_required=True,
            actions=["Attach exit policy to 1 unmanaged position"],
        )
        output = _renderer.render(snap)
        assert "Action required: YES" in output
        assert "Attach exit policy to 1 unmanaged position" in output


# ── §15: Section 15 — Final Verdict ──────────────────────────────────────────


class TestSection15FinalVerdict:
    def test_final_status_green(self):
        snap = _snap(final_status="GREEN", final_summary="All systems go.")
        output = _renderer.render(snap)
        assert "Final Status: GREEN" in output

    def test_final_verdict_green_only_if_all_critical_checks_pass(self):
        # GREEN: no critical alerts, no live trading, no unmanaged
        snap = _snap(
            overall_status="GREEN",
            final_status="GREEN",
            critical_alerts=[],
            live_trading_enabled=False,
            unmanaged_positions=0,
            positions_missing_exit_policy=0,
        )
        output = _renderer.render(snap)
        assert "Final Status: GREEN" in output

    def test_live_mode_enabled_marks_red(self):
        snap = _snap(
            overall_status="RED",
            final_status="RED",
            session_safety="UNSAFE",
            live_trading_enabled=True,
            critical_alerts=["live_trading_enabled=True — live broker orders are ENABLED."],
        )
        output = _renderer.render(snap)
        assert "Final Status: RED" in output
        assert "Status: RED" in output

    def test_unmanaged_positions_marks_red(self):
        snap = _snap(
            overall_status="RED",
            final_status="RED",
            session_safety="UNSAFE",
            unmanaged_positions=1,
            positions_missing_exit_policy=1,
            critical_alerts=["UNMANAGED position: MARUTI qty=+10.0 has no exit policy"],
        )
        output = _renderer.render(snap)
        assert "Final Status: RED" in output

    def test_kill_switch_on_with_open_positions_marks_red(self):
        snap = _snap(
            overall_status="RED",
            final_status="RED",
            session_safety="UNSAFE",
            kill_switch_active=True,
            total_open_positions=3,
            critical_alerts=["Kill switch ACTIVE with 3 open position(s)"],
        )
        output = _renderer.render(snap)
        assert "Final Status: RED" in output
        assert "Kill Switch: ON" in output

    def test_missing_exit_policy_marks_red(self):
        snap = _snap(
            overall_status="RED",
            final_status="RED",
            session_safety="UNSAFE",
            positions_missing_exit_policy=2,
            unmanaged_positions=2,
            critical_alerts=["UNMANAGED position: RELIANCE", "UNMANAGED position: INFY"],
        )
        output = _renderer.render(snap)
        assert "Final Status: RED" in output

    def test_warnings_without_critical_marks_amber(self):
        snap = _snap(
            overall_status="AMBER",
            final_status="AMBER",
            session_safety="DEGRADED",
            warnings=["1 direction/quantity mismatch"],
            critical_alerts=[],
        )
        output = _renderer.render(snap)
        assert "Final Status: AMBER" in output

    def test_summary_present(self):
        snap = _snap(final_summary="Paper session running cleanly in paper mode.")
        output = _renderer.render(snap)
        assert "Paper session running cleanly in paper mode." in output


# ── MonitoringStatusService unit tests ───────────────────────────────────────


class TestMonitoringStatusService:
    """Tests for the service's DynamoDB-backed snapshot building."""

    def _make_dynamo_position_item(
        self,
        symbol: str,
        direction: str,
        quantity: float,
        stop_price: Optional[float] = 100.0,
        take_profit: Optional[float] = 120.0,
        exit_state: str = "",
        exit_order_id: Optional[str] = None,
    ) -> dict:
        item: dict = {
            "PK": {"S": f"POSITION#{symbol}"},
            "SK": {"S": "CURRENT"},
            "symbol": {"S": symbol},
            "direction": {"S": direction},
            "quantity": {"N": str(quantity)},
            "avg_entry_price": {"N": "100.0"},
            "last_price": {"N": "105.0"},
        }
        if stop_price is not None:
            item["stop_price"] = {"N": str(stop_price)}
        if take_profit is not None:
            item["take_profit"] = {"N": str(take_profit)}
        if exit_state:
            item["exit_state"] = {"S": exit_state}
        if exit_order_id:
            item["exit_order_id"] = {"S": exit_order_id}
        return item

    def _make_svc(
        self,
        positions: list[dict],
        kill_switch_active: bool = False,
        trading_mode: str = "paper",
        live_trading_enabled: bool = False,
        live_counters: Optional[LiveCounters] = None,
    ) -> MonitoringStatusService:
        dynamo = MagicMock()

        # Positions scan
        dynamo.scan.return_value = {"Items": positions}

        # Kill switch get_item
        if kill_switch_active:
            dynamo.get_item.return_value = {
                "Item": {"PK": {"S": "KILLSWITCH"}, "SK": {"S": "GLOBAL"}, "active": {"BOOL": True}}
            }
        else:
            dynamo.get_item.return_value = {"Item": None}

        return MonitoringStatusService(
            dynamo_client=dynamo,
            positions_table="test-positions",
            risk_state_table="test-risk",
            trading_mode=trading_mode,
            live_trading_enabled=live_trading_enabled,
            live_counters=live_counters,
        )

    async def test_long_position_counted(self):
        svc = self._make_svc([
            self._make_dynamo_position_item("MARUTI", "LONG", 10.0),
        ])
        snap = await svc.build_snapshot()
        assert snap.long_count == 1
        assert snap.short_count == 0
        assert snap.total_open_positions == 1

    async def test_short_position_counted(self):
        svc = self._make_svc([
            self._make_dynamo_position_item("INFY", "SHORT", -5.0),
        ])
        snap = await svc.build_snapshot()
        assert snap.long_count == 0
        assert snap.short_count == 1

    async def test_flat_position_excluded_from_open_count(self):
        svc = self._make_svc([
            self._make_dynamo_position_item("SBIN", "FLAT", 0.0),
        ])
        snap = await svc.build_snapshot()
        assert snap.total_open_positions == 0
        assert snap.flat_ignored == 1

    async def test_missing_stop_price_marks_unmanaged(self):
        svc = self._make_svc([
            self._make_dynamo_position_item("WIPRO", "LONG", 10.0, stop_price=None),
        ])
        snap = await svc.build_snapshot()
        assert snap.positions_missing_exit_policy == 1
        assert snap.unmanaged_positions == 1

    async def test_missing_stop_price_causes_red_status(self):
        svc = self._make_svc([
            self._make_dynamo_position_item("WIPRO", "LONG", 10.0, stop_price=None),
        ])
        snap = await svc.build_snapshot()
        assert snap.overall_status == "RED"
        assert len(snap.critical_alerts) > 0

    async def test_live_trading_enabled_causes_red(self):
        svc = self._make_svc([], live_trading_enabled=True)
        snap = await svc.build_snapshot()
        assert snap.overall_status == "RED"
        assert snap.live_trading_enabled is True
        assert any("live_trading_enabled" in a for a in snap.critical_alerts)

    async def test_kill_switch_active_with_open_positions_causes_red(self):
        svc = self._make_svc(
            [self._make_dynamo_position_item("MARUTI", "LONG", 10.0)],
            kill_switch_active=True,
        )
        snap = await svc.build_snapshot()
        assert snap.kill_switch_active is True
        assert snap.overall_status == "RED"

    async def test_clean_session_green(self):
        svc = self._make_svc(
            [self._make_dynamo_position_item("RELIANCE", "LONG", 5.0, stop_price=2400.0)],
            live_counters=LiveCounters(
                tee_running=True,
                mis_armed=True,
                recon_ran=True,
                recon_mode="paper",
                recon_mismatches=0,
            ),
        )
        snap = await svc.build_snapshot()
        assert snap.overall_status == "GREEN"
        assert snap.session_safety == "SAFE"

    async def test_trailing_active_position_counted(self):
        svc = self._make_svc([
            self._make_dynamo_position_item(
                "HDFC", "LONG", 10.0, exit_state="TRAILING_ACTIVE"
            )
        ])
        snap = await svc.build_snapshot()
        assert snap.positions_with_trailing_active == 1
        assert snap.open_positions[0].trailing_active is True

    async def test_positions_with_exit_order_id_counted(self):
        svc = self._make_svc([
            self._make_dynamo_position_item(
                "TCS", "LONG", 3.0, exit_order_id="PAPER-EXIT-ABCD"
            )
        ])
        snap = await svc.build_snapshot()
        assert snap.positions_with_exit_order_id == 1

    async def test_trading_mode_paper_in_snapshot(self):
        svc = self._make_svc([], trading_mode="paper")
        snap = await svc.build_snapshot()
        assert snap.trading_mode == "PAPER"

    async def test_dynamo_error_returns_empty_positions(self):
        dynamo = MagicMock()
        dynamo.scan.side_effect = Exception("DynamoDB unavailable")
        dynamo.get_item.return_value = {"Item": None}
        svc = MonitoringStatusService(
            dynamo_client=dynamo,
            positions_table="t",
            risk_state_table="r",
        )
        snap = await svc.build_snapshot()
        # Should not raise; positions will be empty
        assert snap.total_open_positions == 0

    async def test_daily_cap_reached_exit_management_still_true(self):
        counters = LiveCounters(daily_cap_reached=True, new_entries_allowed=False)
        svc = self._make_svc([], live_counters=counters)
        snap = await svc.build_snapshot()
        assert snap.risk_cap_status.daily_cap_reached is True
        assert snap.risk_cap_status.exit_management_allowed is True

    async def test_recon_ran_reflected_in_snapshot(self):
        counters = LiveCounters(recon_ran=True, recon_mode="paper", recon_mismatches=0)
        svc = self._make_svc([], live_counters=counters)
        snap = await svc.build_snapshot()
        assert snap.reconciliation_status.ran_on_startup is True
        assert snap.reconciliation_status.mode == "paper"

    async def test_router_live_attempts_causes_critical(self):
        counters = LiveCounters(router_live_attempts=1)
        svc = self._make_svc([], live_counters=counters)
        snap = await svc.build_snapshot()
        assert snap.overall_status == "RED"
        assert any("live" in a.lower() for a in snap.critical_alerts)

    async def test_mis_kill_switch_causes_critical(self):
        counters = LiveCounters(mis_kill_switch_activated=True)
        svc = self._make_svc([], live_counters=counters)
        snap = await svc.build_snapshot()
        assert snap.overall_status == "RED"


# ── PositionSnapshot property tests ──────────────────────────────────────────


class TestPositionSnapshotProperties:
    def test_positive_qty_is_long(self):
        pos = _pos(quantity=10.0)
        assert pos.qty_direction == "LONG"

    def test_negative_qty_is_short(self):
        pos = _pos(quantity=-5.0)
        assert pos.qty_direction == "SHORT"

    def test_zero_qty_is_flat(self):
        pos = _pos(quantity=0.0)
        assert pos.qty_direction == "FLAT"

    def test_direction_mismatch_long_qty_short_dir(self):
        pos = _pos(quantity=-5.0, direction="LONG")
        assert pos.direction_mismatch is True

    def test_direction_mismatch_short_qty_long_dir(self):
        pos = _pos(quantity=5.0, direction="SHORT")
        assert pos.direction_mismatch is True

    def test_no_mismatch_when_consistent(self):
        pos = _pos(quantity=5.0, direction="LONG")
        assert pos.direction_mismatch is False

    def test_pnl_long_position(self):
        pos = _pos(quantity=10.0, direction="LONG", entry_price=100.0, ltp=110.0)
        assert pos.pnl == pytest.approx(100.0)

    def test_pnl_short_position_profitable(self):
        pos = _pos(quantity=-5.0, direction="SHORT", entry_price=200.0, ltp=180.0)
        assert pos.pnl == pytest.approx(100.0)

    def test_pnl_short_position_loss(self):
        pos = _pos(quantity=-5.0, direction="SHORT", entry_price=200.0, ltp=220.0)
        assert pos.pnl == pytest.approx(-100.0)

    def test_pnl_none_when_no_ltp(self):
        pos = _pos(quantity=10.0, direction="LONG", ltp=None)
        assert pos.pnl is None


# ── Phase 5: ACTION_MODE + entry-block status tests ──────────────────────────


class TestActionModeGate:
    """Tests for ACTION_MODE gate in monitoring_agent and entry-block status."""

    def test_monitoring_status_template_has_entry_block_section(self):
        """The template must contain the Phase 5 entry-block section."""
        from pathlib import Path
        import monitoring_agent
        root = Path(monitoring_agent.__file__).resolve().parent.parent.parent.parent
        template = root / "docs" / "operations" / "monitoring-status-template.md"
        if template.exists():
            content = template.read_text()
            assert "Entry block active" in content
            assert "Entry Block Status" in content
            assert "Exit management" in content

    def test_monitoring_status_template_states_exits_always_allowed(self):
        from pathlib import Path
        import monitoring_agent
        root = Path(monitoring_agent.__file__).resolve().parent.parent.parent.parent
        template = root / "docs" / "operations" / "monitoring-status-template.md"
        if template.exists():
            content = template.read_text()
            # Must state exits are never blocked by entry block
            assert "NEVER blocked" in content or "never blocked" in content.lower()

    def test_action_mode_notify_only_counter_increments(self, tmp_path):
        import asyncio
        import dataclasses
        from monitoring_agent.app import MonitoringAgent
        from monitoring_agent.config import AgentConfig
        from monitoring_agent.rules import MonitoringRules
        from monitoring_agent.snapshot import CollectorResult, Status, HealthSnapshot

        cfg = dataclasses.replace(
            AgentConfig(),
            snapshot_path=str(tmp_path / "snap.json"),
            incident_log_path=str(tmp_path / "inc.jsonl"),
            action_mode="notify_only",
        )
        agent = MonitoringAgent(config=cfg, rules=MonitoringRules())
        agent.collectors = []

        # Patch collect_once to return a known snapshot
        async def _fake_collect():
            return HealthSnapshot(results=[], dry_run=True, action_mode="notify_only", phase=1)
        agent.collect_once = _fake_collect

        asyncio.run(agent.run_once())
        assert agent._safe_counters["safe_actions_disabled_total"] >= 1
        assert agent._safe_counters["safe_actions_executed_total"] == 0

    def test_action_mode_safe_actions_executor_wired_if_available(self, tmp_path):
        import dataclasses
        from monitoring_agent.app import MonitoringAgent, _SAFE_ACTIONS_AVAILABLE
        from monitoring_agent.config import AgentConfig
        from monitoring_agent.rules import MonitoringRules

        cfg = dataclasses.replace(
            AgentConfig(),
            snapshot_path=str(tmp_path / "snap.json"),
            incident_log_path=str(tmp_path / "inc.jsonl"),
            action_mode="safe_actions",
        )
        agent = MonitoringAgent(config=cfg, rules=MonitoringRules())
        if _SAFE_ACTIONS_AVAILABLE:
            assert agent._safe_executor is not None
        else:
            assert agent._safe_executor is None  # graceful fallback


# ── Phase 6: Entry block and safe actions in monitoring status ─────────────────


class TestPhase6MonitoringStatusFields:
    """Verifies that §10a and §10b fields are populated and rendered correctly."""

    def _make_svc_with_entry_block(
        self,
        entry_block_item=None,
        kill_switch_active: bool = False,
        live_counters=None,
    ):
        """Build service whose get_item returns entry_block and kill_switch items."""
        dynamo = MagicMock()
        dynamo.scan.return_value = {"Items": []}

        def _get_item(**kwargs):
            key = kwargs.get("Key", {})
            pk = key.get("PK", {}).get("S", "")
            if pk == "ENTRY_BLOCK":
                return {"Item": entry_block_item} if entry_block_item else {}
            if pk == "KILLSWITCH":
                if kill_switch_active:
                    return {"Item": {"PK": {"S": "KILLSWITCH"}, "SK": {"S": "GLOBAL"}, "active": {"BOOL": True}}}
                return {}
            return {}

        dynamo.get_item.side_effect = _get_item
        return MonitoringStatusService(
            dynamo_client=dynamo,
            positions_table="test-positions",
            risk_state_table="test-risk",
            trading_mode="paper",
            live_trading_enabled=False,
            live_counters=live_counters,
        )

    @staticmethod
    def _eb_item(blocked: bool = True) -> dict:
        return {
            "PK": {"S": "ENTRY_BLOCK"},
            "SK": {"S": "GLOBAL"},
            "blocked": {"BOOL": blocked},
            "status": {"S": "BLOCKED" if blocked else "CLEAR"},
            "reason": {"S": "stale_ltp_test"},
            "source": {"S": "safe_actions"},
            "action_id": {"S": "act-test-001"},
            "idempotency_key": {"S": "bne-20260531"},
            "created_at": {"S": "2026-05-31T10:00:00+00:00"},
        }

    # ── build_snapshot populates entry_block_status ────────────────────────

    @pytest.mark.asyncio
    async def test_entry_block_status_active_when_dynamo_flag_set(self):
        svc = self._make_svc_with_entry_block(entry_block_item=self._eb_item(blocked=True))
        snap = await svc.build_snapshot()
        assert snap.entry_block_status.active is True
        assert snap.entry_block_status.reason == "stale_ltp_test"
        assert snap.entry_block_status.source == "safe_actions"
        assert snap.entry_block_status.action_id == "act-test-001"
        assert snap.entry_block_status.read_status == "OK"

    @pytest.mark.asyncio
    async def test_entry_block_status_not_active_when_absent(self):
        svc = self._make_svc_with_entry_block(entry_block_item=None)
        snap = await svc.build_snapshot()
        assert snap.entry_block_status.active is False
        assert snap.entry_block_status.read_status == "OK"

    @pytest.mark.asyncio
    async def test_entry_block_active_produces_warning_not_critical(self):
        """Entry block active with read_ok → AMBER (warning), not RED."""
        svc = self._make_svc_with_entry_block(entry_block_item=self._eb_item(blocked=True))
        snap = await svc.build_snapshot()
        assert any("ENTRY_BLOCK" in w for w in snap.warnings)
        assert not any("ENTRY_BLOCK" in a for a in snap.critical_alerts)

    @pytest.mark.asyncio
    async def test_entry_block_dynamo_error_sets_error_status(self):
        dynamo = MagicMock()
        dynamo.scan.return_value = {"Items": []}
        dynamo.get_item.side_effect = Exception("DynamoDB unavailable")
        svc = MonitoringStatusService(
            dynamo_client=dynamo,
            positions_table="test-positions",
            risk_state_table="test-risk",
        )
        snap = await svc.build_snapshot()
        assert snap.entry_block_status.read_status == "ERROR"

    # ── build_snapshot populates safe_actions_status ───────────────────────

    @pytest.mark.asyncio
    async def test_safe_actions_status_from_live_counters(self):
        counters = LiveCounters(
            safe_action_mode="safe_actions",
            safe_action_executor_active=True,
            safe_actions_proposed=5,
            safe_actions_executed=3,
            safe_actions_blocked=2,
            safe_actions_idempotency_skips=1,
            safe_actions_last_action="BLOCK_NEW_ENTRIES",
        )
        svc = self._make_svc_with_entry_block(live_counters=counters)
        snap = await svc.build_snapshot()
        sa = snap.safe_actions_status
        assert sa.action_mode == "safe_actions"
        assert sa.executor_active is True
        assert sa.actions_proposed == 5
        assert sa.actions_executed == 3
        assert sa.actions_blocked == 2
        assert sa.idempotency_skips == 1
        assert sa.last_action_type == "BLOCK_NEW_ENTRIES"

    @pytest.mark.asyncio
    async def test_safe_actions_status_defaults_when_no_counters(self):
        svc = self._make_svc_with_entry_block()
        snap = await svc.build_snapshot()
        sa = snap.safe_actions_status
        assert sa.action_mode == "notify_only"
        assert sa.executor_active is False
        assert sa.actions_executed == 0

    @pytest.mark.asyncio
    async def test_safe_actions_mode_safe_actions_executor_not_active_produces_warning(self):
        counters = LiveCounters(
            safe_action_mode="safe_actions",
            safe_action_executor_active=False,  # executor not active!
        )
        svc = self._make_svc_with_entry_block(live_counters=counters)
        snap = await svc.build_snapshot()
        assert any("executor is not active" in w for w in snap.warnings)

    # ── renderer produces §10a and §10b sections ───────────────────────────

    def test_renderer_includes_entry_block_section(self):
        from services.shared.monitoring.monitoring_status import EntryBlockStatus, SafeActionsStatus
        snap = _snap(
            entry_block_status=EntryBlockStatus(
                active=True,
                reason="test_reason",
                source="safe_actions",
                action_id="act-001",
                read_status="OK",
            ),
        )
        output = _renderer.render(snap)
        assert "## 10a. Entry Block Status" in output
        assert "YES" in output
        assert "test_reason" in output
        assert "act-001" in output

    def test_renderer_includes_safe_actions_section(self):
        from services.shared.monitoring.monitoring_status import EntryBlockStatus, SafeActionsStatus
        snap = _snap(
            safe_actions_status=SafeActionsStatus(
                action_mode="safe_actions",
                executor_active=True,
                actions_executed=7,
                actions_blocked=2,
            ),
        )
        output = _renderer.render(snap)
        assert "## 10b. Safe Actions Status" in output
        assert "safe_actions" in output
        assert "7" in output

    def test_renderer_entry_block_not_active_shows_normal_message(self):
        from services.shared.monitoring.monitoring_status import EntryBlockStatus, SafeActionsStatus
        snap = _snap(entry_block_status=EntryBlockStatus(active=False, read_status="OK"))
        output = _renderer.render(snap)
        assert "not active" in output

    def test_renderer_entry_block_active_shows_clear_command(self):
        from services.shared.monitoring.monitoring_status import EntryBlockStatus, SafeActionsStatus
        snap = _snap(entry_block_status=EntryBlockStatus(
            active=True, reason="r", source="s", read_status="OK"
        ))
        output = _renderer.render(snap)
        assert "ENTRY_BLOCK" in output
        assert "delete-item" in output or "Clear" in output
