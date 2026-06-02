# Live Mode Gate Checklist

**Status:** BLOCKED — DO NOT ENABLE  
**Version:** 1.0  
**Date:** 2026-05-25  
**Prerequisite:** [paper-trading-acceptance-checklist.md](paper-trading-acceptance-checklist.md) fully signed off (5 sessions).

---

## Gate Status

```
╔══════════════════════════════════════════════════════════╗
║  LIVE TRADING IS CURRENTLY BLOCKED                       ║
║                                                          ║
║  live_trading_enabled = False  (ExitOrderRouter)         ║
║  QE_LIVE_EXITS_ENABLED = not set                         ║
║                                                          ║
║  To unlock: complete ALL items in this checklist and     ║
║  obtain sign-off from both Operator and Reviewer.        ║
╚══════════════════════════════════════════════════════════╝
```

---

## Section 1 — Prerequisites

| # | Check | Status |
|---|---|---|
| 1.1 | Paper acceptance checklist signed off (5 sessions) | ☐ BLOCKED |
| 1.2 | All 185 automated tests passing on the deployment branch | ☐ |
| 1.3 | No open `CRITICAL` or `HIGH` issues in the issue tracker | ☐ |
| 1.4 | Zerodha API credentials present and valid in Secrets Manager | ☐ |
| 1.5 | Zerodha paper and live accounts are separate (different API keys) | ☐ |
| 1.6 | Live account funded with sufficient margin for 1-share validation trade | ☐ |

---

## Section 2 — Infrastructure

| # | Check | Status |
|---|---|---|
| 2.1 | EC2 instance type matches cost-optimized production spec (c6g.medium minimum) | ☐ |
| 2.2 | DynamoDB table TTL configured on `session_tokens` and `ephemeral_locks` | ☐ |
| 2.3 | S3 audit log bucket exists and write permissions confirmed | ☐ |
| 2.4 | Kafka MSK Serverless bootstrap servers configured for production | ☐ |
| 2.5 | CloudWatch alarms active: `OrderPlacementErrors`, `MissedMISDeadline` | ☐ |
| 2.6 | SNS CRITICAL alert topic confirmed — test message sent and received on PagerDuty/Slack | ☐ |
| 2.7 | CloudWatch dashboard visible with all Phase 4 metrics (trailing, MIS, reconciliation) | ☐ |
| 2.8 | VPC endpoints for S3 and DynamoDB confirmed (no NAT Gateway data charges) | ☐ |

---

## Section 3 — Kill Switch

| # | Check | Status |
|---|---|---|
| 3.1 | Kill switch manual activation tested in staging (`kill_switch_cli.py activate`) | ☐ |
| 3.2 | Kill switch clears all `exit_order_id` fields that block it (unconditional write confirmed) | ☐ |
| 3.3 | Kill switch activation emits SNS CRITICAL alert within 5 seconds | ☐ |
| 3.4 | Kill switch deactivation tested — service resumes signal intake after clear | ☐ |
| 3.5 | MIS deadline escalation (15:10) activates kill switch and alerts PagerDuty | ☐ |
| 3.6 | Kill switch does NOT block risk-reducing (close) orders | ☐ |

---

## Section 4 — Broker-vs-DynamoDB Reconciliation

| # | Check | Status |
|---|---|---|
| 4.1 | `_reconcile_state()` order-level reconciliation tested with simulated PENDING order | ☐ |
| 4.2 | Position drift monitor (PositionMonitor) confirmed active and sending alerts on drift | ☐ |
| 4.3 | Startup position reconciliation runs in `mode=live` and emits CRITICAL (no repair) | ☐ |
| 4.4 | A manually closed Zerodha position (external close) is detected and synced within 1 startup cycle | ☐ |
| 4.5 | Orphan detector (OrphanDetector) alerts on unprotected live positions | ☐ |

---

## Section 5 — One-Share Live Validation Plan

> All steps must be executed in sequence on a single trading day with dedicated operator observation.  
> Validation instrument: **SBIN** or **HDFCBANK** (liquid, low-impact, 1 share = ₹500–₹800).

### 5.1 — Pre-session

| Step | Action | Expected | Done |
|---|---|---|---|
| A | Set `QE_LIVE_EXITS_ENABLED=true` and `live_trading_enabled=True` in ExitOrderRouter constructor | Service starts in live mode | ☐ |
| B | Confirm `execution_service.started` with `mode=live` in logs | Log present | ☐ |
| C | Confirm kill switch is INACTIVE at session start | `kill_switch_state.active = False` in DynamoDB | ☐ |
| D | Confirm `KAFKA_BOOTSTRAP_SERVERS` points to production MSK | Log at startup | ☐ |

### 5.2 — Entry Validation (09:20 IST, 5 minutes after open)

| Step | Action | Expected | Done |
|---|---|---|---|
| E | Submit 1 LONG signal for validation instrument via strategy engine | Signal flows through risk engine | ☐ |
| F | Confirm `execution_service.order_placed` log with Zerodha broker order ID | Real broker order ID (not `paper-*`) | ☐ |
| G | Confirm Zerodha dashboard shows the order placed | Manual check | ☐ |
| H | Confirm `attach_exit_policy` writes `stop_price` to DynamoDB within 5s of fill | DynamoDB check | ☐ |
| I | Confirm `product_type = MIS` on the position in DynamoDB | DynamoDB check | ☐ |

### 5.3 — Stop-Loss Validation (intraday)

| Step | Action | Expected | Done |
|---|---|---|---|
| J | Manually set `stop_price` in DynamoDB to current market price (to trigger SL) | Stop set above current bid | ☐ |
| K | Wait for next TEE poll cycle (≤ 60s) | TEE fires `STOP_LOSS` exit | ☐ |
| L | Confirm `exit_router.live_exit_placed` log with Zerodha order ID | Real broker order ID | ☐ |
| M | Confirm Zerodha dashboard shows MARKET SELL order placed | Manual check | ☐ |
| N | Confirm `direction = FLAT` in DynamoDB after fill confirmation | DynamoDB check | ☐ |
| O | Confirm `orders.events` contains `ORDER_FILLED` exit event | Kafka consumer check | ☐ |

### 5.4 — MIS Validation (if position still open at 15:05)

> Only perform if step K–N did not complete (position remained open past 14:00).

| Step | Action | Expected | Done |
|---|---|---|---|
| P | Observe `mis_square_off.starting` log at 15:05 | Log present | ☐ |
| Q | Confirm MIS places MARKET SELL via Zerodha | `mis_square_off.close_order_placed` log | ☐ |
| R | Confirm `direction = FLAT` by 15:08 | DynamoDB check | ☐ |
| S | Confirm `mis_square_off.all_positions_closed` (not deadline path) | Log present | ☐ |

### 5.5 — Post-session Validation

| Step | Action | Expected | Done |
|---|---|---|---|
| T | Confirm zero open positions in DynamoDB at 15:30 | DynamoDB scan | ☐ |
| U | Confirm audit log written to S3 with all order events | S3 object exists | ☐ |
| V | Confirm `orders.events` P&L accounting records in risk engine | Risk engine logs: `handle_fill.pnl_recorded` | ☐ |
| W | Confirm CloudWatch metrics flushed: `OrdersSubmitted`, `trade_exit.*` | CloudWatch console | ☐ |
| X | Confirm no duplicate orders in orders DynamoDB table | One entry per position | ☐ |

---

## Section 6 — Order Rejection Path

| # | Check | Status |
|---|---|---|
| 6.1 | Order rejection handled: `ORDER_REJECTED` event published to `orders.events` | ☐ |
| 6.2 | Risk engine logs `handle_fill.rejection_recorded` on ORDER_REJECTED | ☐ |
| 6.3 | Rejected order does NOT leave position stuck in `EXIT_PENDING` state | ☐ |
| 6.4 | TEE retries exit on next poll cycle if prior exit was rejected | ☐ |

---

## Section 7 — Audit and Compliance

| # | Check | Status |
|---|---|---|
| 7.1 | All `risk_decision_id` values present on every order in DynamoDB | ☐ |
| 7.2 | S3 execution log bucket with structured JSON lines per order | ☐ |
| 7.3 | S3 lifecycle policy: 90-day hot, archive to Glacier thereafter | ☐ |
| 7.4 | CloudWatch Logs retention set to 30 days | ☐ |
| 7.5 | All live stop-loss orders logged with entry price, stop price, exit price, slippage_bps | ☐ |

---

## Section 8 — Rollback Plan

> In case of unexpected behavior, following steps restore the system to a known-safe state.

### Immediate Rollback (< 30 seconds)

```bash
# Step 1: Activate kill switch
python scripts/kill_switch_cli.py activate --reason "live_validation_rollback"

# Step 2: Verify kill switch active
python scripts/kill_switch_cli.py status

# Step 3: Check Zerodha dashboard for any open positions
# If positions remain open, Zerodha will auto-square at 15:15

# Step 4: Set env var to block live trading
# (restart service with live_trading_enabled=False)
```

### Service-Level Rollback

```bash
# Return to paper mode:
# 1. Update env: QE_LIVE_EXITS_ENABLED=false
# 2. Restart execution_engine service
# 3. Confirm startup log shows mode=paper
# 4. Run startup reconciliation manually if needed
```

### Data Recovery

- Orders table retains all records regardless of mode — no purge needed
- Position records set to `direction = FLAT` after successful close are already safe
- If a position is stuck open at broker but FLAT in DynamoDB: use Zerodha Kite dashboard to manually close

### Contact Escalation

| Situation | Action |
|---|---|
| Open position at market close | Zerodha 15:15 auto-square handles it; verify next day |
| Duplicate fill detected | Activate kill switch; reconcile via `_reconcile_state` on next restart |
| Kill switch won't activate | Log into Zerodha Kite directly and flatten manually |
| SNS alerts not firing | Check CloudWatch alarm state; call broker support if needed |

---

## Final Gate Sign-Off

All sections above checked:

| Role | Name | Date | Signature |
|---|---|---|---|
| Operator | | | |
| Reviewer | | | |
| Architecture Review | | | |

**After sign-off:**
1. Set `QE_LIVE_EXITS_ENABLED=true` in EC2 instance environment
2. Set `live_trading_enabled=True` in ExitOrderRouter constructor
3. Deploy to production EC2 ASG
4. Begin with 1-share validation (Section 5 above)
5. Monitor for 3 full live sessions before increasing position size
