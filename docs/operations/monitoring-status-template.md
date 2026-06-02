# Monitoring Status Template

**Version:** 1.0  
**Date:** 2026-05-25  
**Scope:** Paper trading monitoring report — 15-section structured output

---

## Overview

The monitoring status is a structured 15-section report that captures the complete
health of the paper trading session. It is produced by `MonitoringStatusRenderer`
using data from `MonitoringStatusService.build_snapshot()`.

**Trigger phrases** (Claude responds with the live template when you say any of these):

- `Give monitoring status`
- `Daily monitoring status`
- `Paper trading monitoring status`
- `Today's monitoring status`
- `Give status`
- `Monitoring status now`

**Implementation:**
- Service: `services/shared/monitoring/monitoring_status.py`
- Package: `services/shared/monitoring/__init__.py`
- Tests: `tests/unit/test_monitoring_status.py` (111 tests)
- CLI runner: `scripts/monitoring/paper_trading_monitor.py`
- LocalStack seeder: `scripts/monitoring/seed_local_positions.py`
- Sample counters stub: `scripts/monitoring/sample_counters.json`
- LiveCounters wire-up: `services/execution_engine/service.py` (`_monitoring_flush_loop`)
- ExitOrderRouter counters: `services/execution_engine/exit/exit_order_router.py`
- TradeExitEngine counters: `services/execution_engine/monitors/trade_exit_engine.py`
- MISSquareOffManager counters: `services/execution_engine/mis_square_off.py`

---

## Template

The template below shows the exact structure produced by `MonitoringStatusRenderer.render()`.
Values in `{{...}}` are substituted at render time from `MonitoringStatusSnapshot`.

```
# Paper Trading Monitoring Status

## 1. Overall Status

Status: {{GREEN / AMBER / RED}}
Timestamp IST: {{YYYY-MM-DD HH:MM:SS IST}}
Trading Mode: {{PAPER / BACKTEST / LIVE}}
Live Trading Enabled: {{true / false}}
Broker Live Order Calls: {{ENABLED / DISABLED}}
Kill Switch: {{ON / OFF}}
Session Safety: {{SAFE / UNSAFE / DEGRADED}}

One-line verdict:
{{verdict_line}}

---

## 2. Service Health

| Component | Status | Notes |
|---|---:|---|
| strategy_engine | {{UP/DOWN/UNKNOWN}} | |
| risk_engine | {{UP/DOWN/UNKNOWN}} | |
| execution_engine | {{UP/DOWN/UNKNOWN}} | |
| TradeExitEngine | {{UP/UNKNOWN}} | poll_interval={{N}}s |
| ExitOrderRouter | UP | mode={{PAPER/LIVE}} |
| MISSquareOffManager | {{UP/UNKNOWN}} | fires at {{time}} IST |
| PositionReconciliationService | {{RAN/UNKNOWN}} | mode={{mode}} mismatches={{N}} |
| paper broker ledger | OK | Synthetic fills via apply_fill_to_position |
| DynamoDB positions table | OK | {{table_name}} |
| Kafka/events path | {{OK/UNKNOWN}} | |
| candle_cache | UNKNOWN | No direct health check available |
| data_ingestion | {{UP/UNKNOWN}} | |

---

## 3. Trading Mode and Safety Gates

| Check | Expected | Actual | Status |
|---|---:|---:|---|
| Trading mode | PAPER | {{actual}} | {{PASS/FAIL}} |
| live_trading_enabled | false | {{actual}} | {{PASS/FAIL}} |
| Live broker order placement | DISABLED | {{actual}} | {{PASS/FAIL}} |
| Exit orders bypass signal pipeline | YES | YES | PASS |
| Daily cap blocks exits | NO | NO | PASS |
| Signed quantity invariant active | YES | YES | PASS |
| MIS product_type | MIS | MIS | PASS |
| Duplicate exit prevention | ENABLED | ENABLED | PASS |

---

## 4. Position Summary

| Metric | Count |
|---|---:|
| Total open positions | {{N}} |
| LONG positions | {{N}} |
| SHORT positions | {{N}} |
| FLAT positions ignored | {{N}} |
| Positions with exit policy | {{N}} |
| Positions missing exit policy | {{N}} |
| Positions with exit_order_id | {{N}} |
| Positions with trailing active | {{N}} |
| Direction/quantity mismatches | {{N}} |
| Unmanaged positions | {{N}} |

Position safety verdict:
{{SAFE / UNSAFE — N unmanaged position(s). / DEGRADED — N direction/quantity mismatch(es).}}

---

## 5. Open Positions Detail

| Symbol | Qty | Direction | Entry | LTP | P&L | Stop | Target | Trailing | Exit State | Exit Order ID | LTP Source | LTP Age |
|---|---:|---|---:|---:|---:|---:|---:|---|---|---|---|---|
| {{symbol}} | {{+N.0 / -N.0}} | {{LONG/SHORT}} | {{₹entry}} | {{₹ltp}} | {{±₹pnl}} | {{₹stop}} | {{₹target}} | {{ACTIVE/NO}} | {{exit_state}} | {{id/none}} | {{live/stale/fill/—}} | {{N.Ns/N.Nm/—}} |

Rules:
- Qty must be signed. Positive qty means LONG. Negative qty means SHORT. Zero means FLAT.
- If direction disagrees with signed qty, mark WARNING.
- LTP Source: `live`=prices_table fresh (< 5 s), `stale`=prices_table expired, `fill`=entry fill price (LiveQuotePoller not running), `—`=unavailable.

---

## 6. Trade Exit Engine Status

| Metric | Value |
|---|---:|
| TEE running | {{true/false}} |
| Poll interval | {{N}}s |
| Stop-loss active positions | {{N}} |
| Take-profit active positions | {{N}} |
| Trailing active positions | {{N}} |
| Trailing activated today | {{N}} |
| Trailing stop hit today | {{N}} |
| Stop-loss hit today | {{N}} |
| Take-profit hit today | {{N}} |
| Duplicate exits prevented | {{N}} |
| Unmanaged position detections | {{N}} |

Latest TEE events:
- {{event 1}}
- {{event 2}}
...

---

## 7. ExitOrderRouter Status

| Metric | Value |
|---|---:|
| Router mode | {{PAPER/LIVE}} |
| Paper exits routed | {{N}} |
| Backtest exits routed | {{N}} |
| Live exits attempted | {{N}} |
| Live exits blocked | {{N}} |
| Idempotency successes | {{N}} |
| Idempotency skips | {{N}} |
| Failed exit routes | {{N}} |

Safety note:
{{No live orders were placed. All exits routed through paper path. live_trading_enabled=False is confirmed.
   OR: WARNING: N live exit attempt(s) detected. Investigate immediately.}}

---

## 8. MIS Square-Off Status

| Field | Value |
|---|---|
| MIS square-off armed | {{true/false}} |
| MIS square-off time IST | 15:05 |
| Square-off deadline IST | 15:10 |
| Broker fallback time IST | 15:15 |
| Positions discovered at last scan | {{N / NA}} |
| LONG discovered | {{N / NA}} |
| SHORT discovered | {{N / NA}} |
| Orders placed | {{N / NA}} |
| Orders rejected | {{N / NA}} |
| Positions confirmed flat | {{N / NA}} |
| Positions still open at deadline | {{N / NA}} |
| Kill switch activated by MIS | {{true/false}} |

MIS safety checks:

| Check | Status |
|---|---|
| Uses quantity != 0 / abs(quantity) > 0 | PASS |
| LONG closes with SELL | PASS |
| SHORT closes with BUY | PASS |
| close_qty uses abs(quantity) | PASS |
| product_type=ProductType.MIS | PASS |
| skips positions with exit_order_id | PASS |

---

## 9. Reconciliation Status

| Metric | Value |
|---|---:|
| Reconciliation ran on startup | {{true/false}} |
| Mode | {{paper/live}} |
| Mismatches detected | {{N}} |
| Paper repairs completed | {{N}} |
| Live critical alerts | {{N}} |
| ZERO_QTY_OPEN found | {{N}} |
| STALE_EXIT_LOCK found | {{N}} |
| direction/quantity mismatch found | {{N}} |
| open position without exit policy found | {{N}} |

Reconciliation verdict:
{{SAFE — Reconciliation clean. All positions consistent at startup.
  / WARNING — Reconciliation did not run on startup.
  / WARNING — N mismatch(es) found; N repaired, N alerted.
  / CRITICAL — N open position(s) had no exit policy. Operator action required.}}

---

## 10. Risk and Daily Cap Status

| Metric | Value |
|---|---:|
| Daily cap reached | {{true/false}} |
| Daily loss limit reached | {{true/false}} |
| Daily profit lock reached | {{true/false}} |
| New entries allowed | {{true/false}} |
| Exit management allowed | true |
| Max open positions reached | {{true/false}} |
| Max fills per strategy reached | {{true/false}} |

Important:
Daily cap must block new entries only.
Daily cap must not block exits.

---

## 10a. Entry Block Status (Phase 6)

| Field | Value |
|---|---|
| Entry block active | {{YES ⚠ / no}} |
| DynamoDB read status | {{OK / ERROR}} |
| Source | {{source or —}} |
| Reason | {{reason or —}} |
| Action ID | {{action_id or —}} |
| Idempotency key | {{idempotency_key or —}} |
| Created at | {{ISO-8601 UTC or —}} |

Rules:
- Entry block active → new entry signals from strategy_engine AND risk_engine are suppressed (defense-in-depth).
- Exit management (TradeExitEngine, MIS, ExitOrderRouter) is NEVER blocked by entry block.
- Kill switch activation is NEVER blocked by entry block.
- Closeout signals (`metadata.is_closeout=True`) bypass the risk_engine entry block check.
- To clear: **human-only** — `aws dynamodb delete-item --table-name <risk-state-table> --key '{"PK":{"S":"ENTRY_BLOCK"},"SK":{"S":"GLOBAL"}}'`

GREEN conditions: Entry block absent (active=false) — or — active=true and exits are healthy (status is AMBER).
RED condition: DynamoDB read failure with fail-closed active (live profile).

---

## 10b. Safe Actions Status (Phase 6)

| Field | Value |
|---|---|
| ACTION_MODE | {{notify_only / safe_actions}} |
| Executor active | {{true / false}} |
| Actions proposed | {{N}} |
| Actions executed | {{N}} |
| Actions blocked | {{N}} |
| Idempotency skips | {{N}} |
| Last action type | {{action_type or —}} |
| Last blocked reason | {{reason or —}} |

Rules:
- If `ACTION_MODE=safe_actions` and `executor_active=false` → AMBER warning.
- If `ACTION_MODE=safe_actions` and metrics/audit unavailable → status cannot be GREEN.
- All safe_action executions are permanently recorded in the audit log (`safe_actions_audit.jsonl`).
- Idempotency skips (both memory and DynamoDB) are audited with `idempotency_skipped=true`.

---

## 11. Strategy Status

| Strategy | Status | Signals | Fills | Open Pos | Exits | Cap Status | Notes |
|---|---|---:|---:|---:|---:|---|---|
| {{name}} | {{ACTIVE/CAPPED/STOPPED}} | {{N}} | {{N}} | {{N}} | {{N}} | {{OK/CAPPED}} | |

---

## 12. Paper P&L and Risk

| Metric | Value |
|---|---:|
| Realized P&L | {{±₹N}} |
| Unrealized P&L | {{±₹N}} |
| Total Paper P&L | {{±₹N}} |
| Largest winner | {{symbol ±₹N / —}} |
| Largest loser | {{symbol ±₹N / —}} |
| Max intraday drawdown | {{₹-N / —}} |
| Win rate today | {{N.N% / —}} |
| Average winner | {{±₹N / —}} |
| Average loser | {{±₹N / —}} |

---

## 13. Alerts and Warnings

Critical alerts:
- {{alert text / None}}

Warnings:
- {{warning text / None}}

Data quality issues:
- {{issue text / None}}

Operational issues:
- {{issue text / None}}

---

## 14. Action Required

Action required: {{YES / NO}}

If YES:
1. {{action 1}}
2. {{action 2}}

If NO:
No manual action required. Continue monitoring.

---

## 15. Final Verdict

Final Status: {{GREEN / AMBER / RED}}

Summary:
{{summary paragraph}}
```

---

## Status Color Logic

| Status | Condition |
|---|---|
| **RED** | `live_trading_enabled=True` — live broker orders could be placed |
| **RED** | Any open position has no stop_price (unmanaged) |
| **RED** | Kill switch ACTIVE with open positions |
| **RED** | Any live route attempt recorded (`router_live_attempts > 0`) |
| **RED** | MIS activated kill switch (15:10 deadline escalation) |
| **RED** | Trading mode is LIVE (not PAPER) |
| **AMBER** | Direction/quantity mismatches detected |
| **AMBER** | Startup reconciliation found mismatches (repaired or alerted) |
| **AMBER** | Startup reconciliation status unknown (did not run) |
| **GREEN** | All checks pass — no critical alerts, no warnings |

### Key invariants encoded in the report

1. **`exit_management_allowed` is always `true`** — the daily cap does not and must not block exits.
2. **MIS safety checks are always PASS** — they are code invariants verified by the test suite, not runtime checks.
3. **`live_trading_enabled=False`** must always show as `false` / PASS until the live-mode gate checklist is signed off and `QE_EXECUTION_LIVE_TRADING_ENABLED=true` is explicitly set. See `docs/operations/live-mode-gate-checklist.md`.
4. **Signed quantity is canonical** — direction mismatches are flagged in §4 and §5; the qty_direction property overrides the stored direction string.

---

## Integration

### Standalone (DynamoDB only)

```python
from services.shared.monitoring import MonitoringStatusService, MonitoringStatusRenderer

svc = MonitoringStatusService(
    dynamo_client=dynamo,
    positions_table="qe-paper-positions",
    risk_state_table="qe-risk-state",
)
snap = await svc.build_snapshot()
print(MonitoringStatusRenderer().render(snap))
```

### In-process (with live counters from execution service)

```python
from services.shared.monitoring import (
    MonitoringStatusService, MonitoringStatusRenderer, LiveCounters,
)

counters = LiveCounters(
    tee_running=True,
    tee_poll_interval=60,
    recon_ran=True,
    recon_mode="paper",
    recon_mismatches=0,
    router_mode="PAPER",
    router_paper_exits=7,
    mis_armed=True,
)
svc = MonitoringStatusService(
    dynamo_client=dynamo,
    positions_table="qe-paper-positions",
    risk_state_table="qe-risk-state",
    trading_mode="paper",
    live_trading_enabled=False,
    live_counters=counters,
)
snap = await svc.build_snapshot()
print(MonitoringStatusRenderer().render(snap))
```

---

## Files

| File | Purpose |
|---|---|
| `services/shared/monitoring/monitoring_status.py` | Full implementation (~650 lines) |
| `services/shared/monitoring/ltp_resolver.py` | `LtpResolver` + `LtpResult` — shared LTP lookup with freshness metadata |
| `services/shared/monitoring/__init__.py` | Package init — public exports |
| `tests/unit/test_monitoring_status.py` | 111 unit tests |
| `scripts/monitoring/paper_trading_monitor.py` | Offline CLI — runs 15-section report from JSON + DynamoDB |
| `scripts/monitoring/seed_local_positions.py` | Seeds LocalStack with sample positions for offline testing |
| `scripts/monitoring/sample_counters.json` | Static `LiveCounters` stub (no execution service required) |
| `services/execution_engine/service.py` | `_monitoring_flush_loop()` — writes `LiveCounters` JSON every 60 s |
| `services/execution_engine/exit/exit_order_router.py` | Writes router/P&L counters |
| `services/execution_engine/monitors/trade_exit_engine.py` | Writes TEE running/event counters |
| `services/execution_engine/mis_square_off.py` | Writes MIS square-off counters |
| `docs/operations/monitoring-status-template.md` | This file |
