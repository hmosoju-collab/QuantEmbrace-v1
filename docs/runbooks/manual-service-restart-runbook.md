# Manual Service Restart Runbook

_Last updated: 2026-05-31 · Requires: human approval · NOT an autonomous action_

> **This runbook is operator-executed only.** No autonomous process may trigger
> these commands. The monitoring agent generates the command text via
> `GENERATE_RUNBOOK_COMMAND` — a human reads the output and decides whether to run it.

---

## Pre-conditions (ALL must hold before any restart)

- [ ] Kill switch status confirmed: `python scripts/kill_switch_cli.py status`
- [ ] No open positions in an unmanaged state (check reconcile output)
- [ ] Reason for restart identified and documented (log the root cause)
- [ ] Market is closed OR TEE/MIS are confirmed handling active positions
- [ ] Token refresh is current (Zerodha): `python scripts/zerodha_login.py`

---

## ai_engine Restart

**When to restart:** ai_engine DOWN or crash-looping AND EnrichmentWatchdog is
confirmed on fallback path (`risk.kill-switch` topic shows no enrichment).

**Do NOT restart if:**
- You have not identified the root cause (a crash log must exist)
- The EnrichmentWatchdog fallback is not active (signals may gap)
- Market is open and active positions have pending fills

```bash
# 1. Confirm fallback is active
docker-compose logs risk_engine | grep "enrichment_watchdog.fallback_active"

# 2. Check ai_engine crash reason
docker-compose logs ai_engine | tail -50

# 3. Restart
docker-compose stop ai_engine
docker-compose rm -f ai_engine
docker-compose up -d ai_engine

# 4. Post-check: consumer group aiengine-v1 rejoined
docker-compose logs -f ai_engine | grep "aiengine-v1"
# Expected: consumer group assignment message within 30s

# 5. Confirm watchdog disarms
docker-compose logs risk_engine | grep "enrichment_watchdog.fallback_inactive"
```

**Rollback:** If ai_engine fails to start, leave it down — EnrichmentWatchdog
fallback continues trading. Escalate to Anthropic support if image is corrupt.

---

## execution_engine Restart

**When to restart:** execution_engine DOWN and confirmed no open orders in
PLACED/PARTIALLY_FILLED state (pending fills would be orphaned).

**REQUIRES EXPLICIT HUMAN SIGN-OFF** — execution_engine manages open positions.

```bash
# 1. Confirm kill switch is INACTIVE
python scripts/kill_switch_cli.py status

# 2. Confirm no in-flight orders
# Check DynamoDB: orders table, status-index GSI, filter PLACED/PARTIALLY_FILLED
# If any exist: wait for fills or manually cancel via Zerodha terminal

# 3. Restart
docker-compose stop execution_engine
docker-compose rm -f execution_engine
docker-compose up -d execution_engine

# 4. Post-check
docker-compose logs -f execution_engine | grep "execution_service.started"
# Expected within 15s

# 5. Verify TEE restarted
docker-compose logs execution_engine | grep "tee.started"

# 6. Verify MIS restarted (if during market hours)
docker-compose logs execution_engine | grep "mis_square_off.started"
```

**Rollback:** If execution_engine fails to start, positions remain open in
DynamoDB but no new orders will be placed. Manually close positions via Zerodha
terminal (Kite web/app). Activate kill switch to block any leaked signals:
`python scripts/kill_switch_cli.py activate --reason "execution_engine restart failed"`

---

## risk_engine Restart

**When to restart:** risk_engine DOWN and kill switch confirmed ACTIVE (no new
signals should be flowing anyway while risk_engine is down).

**REQUIRES EXPLICIT HUMAN SIGN-OFF**

```bash
# 1. Confirm kill switch is ACTIVE (signals not flowing to execution)
python scripts/kill_switch_cli.py status

# 2. Check restart reason
docker-compose logs risk_engine | tail -50

# 3. Restart
docker-compose stop risk_engine
docker-compose rm -f risk_engine
docker-compose up -d risk_engine

# 4. Post-check
docker-compose logs -f risk_engine | grep "risk_engine_service.started"
# Expected within 20s (DynamoDB warm-up)

# 5. Confirm consumer groups restored
docker-compose logs risk_engine | grep "risk-v1"

# 6. Deactivate kill switch only after risk_engine is confirmed healthy
python scripts/kill_switch_cli.py status  # verify validators active
# Then if safe:
python scripts/kill_switch_cli.py deactivate --reason "risk_engine restarted and healthy"
```

**Rollback:** If risk_engine fails to start, keep kill switch ACTIVE. No new
signals will be approved. Investigate DynamoDB connectivity and MSK auth.

---

## strategy_engine Restart

**Lower risk than risk/execution — no open positions or orders managed here.**

```bash
# 1. Restart
docker-compose stop strategy_engine
docker-compose rm -f strategy_engine
docker-compose up -d strategy_engine

# 2. Post-check
docker-compose logs -f strategy_engine | grep "strategy_engine_service.started"

# 3. Confirm candle consumer active
docker-compose logs strategy_engine | grep "dynamo_candle_consumer.started"
```

---

## data_ingestion Restart

**Lower risk — ticks stop flowing but no positions are managed here.**

```bash
# 1. Restart
docker-compose stop data_ingestion
docker-compose rm -f data_ingestion
docker-compose up -d data_ingestion

# 2. Post-check: WebSocket reconnect
docker-compose logs -f data_ingestion | grep "zerodha.websocket.connected"

# 3. Confirm candle stream active
docker-compose logs data_ingestion | grep "candle_stream_watchdog.started"
```

---

## monitoring_agent Restart

```bash
docker-compose restart monitoring_agent
# The agent's incident log replays on startup — no re-alerts for known issues.
curl -s localhost:8086/health
```

---

## Post-Restart Checklist (all services)

- [ ] `python scripts/deploy/paper_preflight_check.py` exits 0
- [ ] `python scripts/monitoring/paper_trading_monitor.py --counters /tmp/qe_live_counters.json`
       shows GREEN status
- [ ] Kill switch INACTIVE (unless deliberately kept active)
- [ ] No `reconciliation_required=True` in DynamoDB risk-state
- [ ] Zerodha token fresh (check `python scripts/zerodha_login.py` if near 07:30 IST)
