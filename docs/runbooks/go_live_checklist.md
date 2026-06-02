# QuantEmbrace Go-Live Checklist

> **Purpose:** Gate control between paper trading and live capital deployment.  
> All items in Section 1 must be PASS before any live capital is committed.  
> This document is the single source of truth for go-live promotion decisions.

---

## Section 1 — Mandatory Go-Live Criteria

All criteria must be satisfied before **any strategy** is promoted to live capital.

### 1.1 Five-Day Paper Trading Validation

Run `scripts/monitoring/paper_session_report.py --days 5` and verify:

| Metric | Required threshold | How to check |
|---|---|---|
| Enrichment rate | ≥ 80 % signals enriched (not degraded) | `paper_session_report.py` → enrichment_rate |
| Signal approval rate | ≥ 50 % of pending approved | `paper_session_report.py` → signal_approval_rate |
| Order fill rate | ≥ 90 % of approved orders filled | `paper_session_report.py` → order_fill_rate |
| Avg slippage | ≤ 0.15 % vs signal price | `paper_session_report.py` → avg_slippage |
| Fallback activations | ≤ 2 / day | `paper_session_report.py` → fallback_activations |
| Kill switch fires | 0 | `paper_session_report.py` → kill_switch_events |

**All 5 days must show verdict = READY.** A single NOT_READY or BORDERLINE day resets the 5-day counter.

### 1.2 Pre-flight Check

Run `scripts/deploy/preflight_check.py --env production` immediately before each session.  
All checks must be PASS (WARN on enrichment lag is acceptable with confirmation).

### 1.3 Infrastructure Verification

- [ ] Terraform `plan` is clean — no unintended resource changes (`terraform plan -var-file=environments/prod/terraform.tfvars`)
- [ ] All EC2 ASGs have `desired_capacity ≥ 1` (verify via AWS Console or `aws autoscaling describe-auto-scaling-groups`)
- [ ] Kafka topics exist with correct partition counts (`scripts/kafka/create_topics.py --verify`)
- [ ] DynamoDB tables present and accessible (covered by pre-flight check)
- [ ] S3 buckets accessible (covered by pre-flight check)
- [ ] CloudWatch alarms in `OK` state — no pre-existing alarm breaches

### 1.4 Kill Switch Verification

- [ ] Kill switch is NOT active (`scripts/deploy/preflight_check.py` → kill_switch_inactive = PASS)
- [ ] Kill switch reset procedure has been tested in staging (see Section 4.3)
- [ ] On-call engineer knows the kill switch command:
  ```bash
  # Activate kill switch (emergency halt — use only if trading must stop immediately)
  python scripts/trading/kill_switch.py --activate --reason "manual go-live abort"
  ```

### 1.5 Broker Session Verification

**NSE (Zerodha):**
- [ ] Kite Connect access token refreshed for today's session (expires daily ~07:30 IST)
- [ ] Zerodha account balance sufficient for planned position sizes
- [ ] NSE market is in normal trading mode (check NSE website / Zerodha positions)

**US Equities (Alpaca):**
- [ ] Alpaca account status = ACTIVE (`paper_session_report.py` → alpaca_session = PASS)
- [ ] Alpaca base URL set to LIVE endpoint (not paper): `https://api.alpaca.markets`
- [ ] Pattern Day Trader (PDT) rules noted if account < $25k

### 1.6 Risk Limits Configured

Review `configs/risk_limits_production.yaml` and confirm:

- [ ] Max position size per symbol is set and tested in paper trading
- [ ] Daily loss cap is set (recommended: ≤ 2 % of NAV)
- [ ] Max open positions is set
- [ ] Kill switch daily-loss trigger threshold is set (recommended: ≤ 3 % of NAV)
- [ ] Quality filter threshold > 0 in DynamoDB strategy-config for all live strategies

---

## Section 2 — Per-Strategy Promotion Criteria

Each strategy must meet these thresholds **individually** over 5 paper days before live allocation.

| Metric | Required | Preferred |
|---|---|---|
| Annualised Sharpe ratio | ≥ 0.5 | ≥ 1.0 |
| Max drawdown | ≤ 5 % | ≤ 3 % |
| Win rate | ≥ 45 % | ≥ 55 % |
| Fill rate | ≥ 90 % | ≥ 95 % |
| Days since last FAIL | ≥ 5 | ≥ 10 |

**Initial live allocation:** Start at 10 % of planned capital per strategy. Double only after 5 live days meeting the preferred thresholds.

### Strategies and promotion status

| Strategy | Paper start | Paper end | Verdict | Live capital |
|---|---|---|---|---|
| momentum | TBD | TBD | PENDING | — |
| mean_reversion | TBD | TBD | PENDING | — |
| stat_arb | TBD | TBD | PENDING | — |
| breakout | TBD | TBD | PENDING | — |
| trend_following | TBD | TBD | PENDING | — |
| regime_adaptive | TBD | TBD | PENDING | — |

> Update this table in the PR that promotes each strategy to live.

---

## Section 3 — Go-Live Day Procedure

Execute in this exact order on the first live trading day.

### T-60min: Infrastructure
1. `terraform plan` → must be clean
2. Verify all ASGs healthy in AWS Console
3. Confirm Kafka cluster status = ACTIVE in MSK console
4. Run `scripts/kafka/create_topics.py --verify`
5. Check CloudWatch dashboards — no active alarms

### T-30min: Broker sessions
1. Refresh Zerodha access token:
   ```bash
   python scripts/auth/refresh_zerodha_token.py
   ```
2. Verify Alpaca account (live endpoint):
   ```bash
   curl -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
        -H "APCA-API-SECRET-KEY: $ALPACA_API_SECRET" \
        https://api.alpaca.markets/v2/account | jq .status
   ```
3. Confirm `QE_ENVIRONMENT=production` and `ALPACA_BASE_URL=https://api.alpaca.markets`

### T-15min: Pre-flight
1. Run `scripts/deploy/preflight_check.py --env production`
2. All checks must be PASS. Resolve any FAILs before proceeding.
3. Confirm kill switch is inactive.

### T-5min: Service startup
Start services in this order (each must be healthy before starting the next):

```bash
# 1. Data ingestion (market data must flow before strategies run)
systemctl start quantembrace-data-ingestion
# Wait for health check: curl localhost:8080/health

# 2. AI engine (must be consuming signals.pending before risk_engine starts)
systemctl start quantembrace-ai-engine
# Wait for health check: curl localhost:8081/health

# 3. Risk engine (starts both enriched + fallback processing loops)
systemctl start quantembrace-risk-engine
# Wait for health check: curl localhost:8082/health

# 4. Execution engine
systemctl start quantembrace-execution-engine
# Wait for health check: curl localhost:8083/health

# 5. Strategy engine last — signals only flow when downstream is ready
systemctl start quantembrace-strategy-engine
```

### T=0: Session open
1. Confirm first signal appears in CloudWatch → `QuantEmbrace/StrategyEngine/SignalsPublished`
2. Confirm first enriched signal in `QuantEmbrace/AIEngine/SignalsEnriched`
3. Confirm risk decision in `QuantEmbrace/RiskEngine/SignalsApproved`
4. Monitor first order placement via Zerodha/Alpaca console

---

## Section 4 — Emergency Procedures

### 4.1 Immediate Trading Halt

If anything looks wrong during live trading, execute the kill switch immediately:

```bash
python scripts/trading/kill_switch.py --activate --reason "<brief reason>"
```

This will:
- Set the DynamoDB kill-switch table `active=True`
- The risk engine's KafkaKillSwitchListener picks up the change within 1s
- All pending risk approvals are blocked
- All pending orders are cancelled
- Execution engine halts new order placement

The halt propagates within ~1–2 seconds. No restart required.

### 4.2 Resetting the Kill Switch

After investigating and resolving the issue:

```bash
# Verify the issue is resolved before resetting
python scripts/trading/kill_switch.py --status

# Reset (requires confirmation prompt)
python scripts/trading/kill_switch.py --deactivate --reason "resolved: <explanation>"
```

Document the incident in `docs/incidents/` before resetting.

### 4.3 AI Enrichment Fallback

If `EnrichmentWatchdog` activates fallback mode (ai_engine lag detected):

- **Impact:** Signals flow via `signals.pending → risk_engine` directly. Enrichment metadata (regime, quality_score) is unavailable. Trading continues safely.
- **Action:** Check ai_engine ASG health in AWS Console. If the ASG instance is unhealthy, AWS will auto-replace it (min=1 max=2).
- **Recovery:** EnrichmentWatchdog automatically re-activates enriched mode once lag clears. No manual action needed.
- **If enrichment does not recover within 30 minutes:** Investigate ai_engine CloudWatch logs. Check MSK consumer-group offset for `aiengine-v1`.

### 4.4 Kafka MSK Connectivity Loss

If Kafka becomes unreachable:

1. Check AWS MSK console — cluster state should be ACTIVE
2. Check VPC security group rules — port 9098 must be open from EC2 to MSK
3. Check IAM role for each service — token expiry can cause auth failures
4. All services retry Kafka connections with exponential backoff — no restart required unless the process has exited
5. If MSK is in a degraded state, contact AWS Support via console

### 4.5 Broker API Failure

**Zerodha:** If Kite API returns 403/token expired mid-session, the execution engine will reject orders until the token is refreshed. Run:
```bash
python scripts/auth/refresh_zerodha_token.py --update-dynamo
```
The execution engine picks up the new token from DynamoDB on the next order attempt.

**Alpaca:** Alpaca keys do not expire. API errors (503, rate limit) are handled by the execution engine's circuit breaker with retry. If Alpaca is having an outage, monitor https://status.alpaca.markets.

### 4.6 Broker Position Reconciliation

On startup (or after any emergency halt), the execution engine reconciles its DynamoDB order state against the broker. If discrepancies are found:

```bash
# Dry-run reconciliation
python scripts/ops/reconcile_positions.py --broker zerodha --dry-run
python scripts/ops/reconcile_positions.py --broker alpaca  --dry-run

# Apply corrections (after manual review)
python scripts/ops/reconcile_positions.py --broker zerodha --apply
```

---

## Section 5 — Post-Session Checklist

Run after each live session:

- [ ] Run `paper_session_report.py --date <today>` with `QE_ENVIRONMENT=production`
- [ ] Verify all positions are flat (MIS auto-square-off at 15:15 IST for NSE, end-of-day for US)
- [ ] Review CloudWatch dashboards for anomalies
- [ ] Check S3 audit logs for any DLQ messages (`signals.enriched.dlq`, `signals.pending.dlq`)
- [ ] Update strategy promotion table (Section 2) if any thresholds changed
- [ ] File incident report if any emergency procedure was invoked

---

## Section 6 — Rollback Procedure

If live trading results in unexpected P&L or system behaviour:

1. Activate kill switch immediately (Section 4.1)
2. Close any open positions manually via Zerodha/Alpaca console
3. Scale down all EC2 ASGs to 0 to stop all services:
   ```bash
   aws autoscaling set-desired-capacity --auto-scaling-group-name qe-prod-strategy-engine  --desired-capacity 0
   aws autoscaling set-desired-capacity --auto-scaling-group-name qe-prod-risk-engine       --desired-capacity 0
   aws autoscaling set-desired-capacity --auto-scaling-group-name qe-prod-execution-engine  --desired-capacity 0
   aws autoscaling set-desired-capacity --auto-scaling-group-name qe-prod-data-ingestion    --desired-capacity 0
   aws autoscaling set-desired-capacity --auto-scaling-group-name qe-prod-ai-engine         --desired-capacity 0
   ```
4. Investigate root cause. Do not restart until cause is identified and documented.
5. Reset 5-day paper trading counter. All strategies must re-validate from zero.

---

## Appendix A — Key Contacts and Resources

| Resource | Location |
|---|---|
| CloudWatch dashboards | AWS Console → CloudWatch → Dashboards → QuantEmbrace-Prod |
| MSK cluster | AWS Console → Amazon MSK → Clusters → qe-prod-msk |
| EC2 ASGs | AWS Console → EC2 → Auto Scaling Groups → filter: qe-prod |
| Zerodha console | https://kite.zerodha.com |
| Alpaca dashboard | https://app.alpaca.markets |
| S3 audit logs | s3://$S3_BUCKET_LOGS/audit/ |
| S3 DLQ messages | s3://$S3_BUCKET_LOGS/dlq/ |
| Incident log | docs/incidents/ |

## Appendix B — Architecture Reference

Signal flow (Phase 6+):
```
strategy_engine
    └──▶  signals.pending  (v3.0)
              └──▶  ai_engine  (aiengine-v1 consumer group)
                        └──▶  signals.enriched  (v4.0)
                                  └──▶  risk_engine  (risk-v1)
                                            └──▶  signals.approved
                                                      └──▶  execution_engine
```

Fallback path (EnrichmentWatchdog activates if ai_engine lags):
```
strategy_engine
    └──▶  signals.pending
              └──▶  risk_engine  (risk-v1, fallback loop)
                        └──▶  signals.approved
                                  └──▶  execution_engine
```

Retry path (Phase 7):
```
signals.enriched  →  signals.enriched.retry  (after processing failure)
signals.enriched.retry  →  KafkaRetryReplayer  →  signals.enriched  (replay)
signals.enriched  →  signals.enriched.dlq  (after max_retry_attempts=3 or expiry)
```
