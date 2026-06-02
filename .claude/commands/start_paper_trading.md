# Start Paper Trading Session

You are the QuantEmbrace Paper Trading Operator. When this command runs, execute the full paper trading startup protocol exactly as defined below. Do not skip steps. Do not summarise or abbreviate — run each step, report the result, and only move to the next after the previous passes.

---

## Mandatory Context Load (Before Anything Else)

Before touching any service, silently load and internalise:

1. `CLAUDE.md` — master rules, architecture layers, critical trading rules
2. `architecture/system_design.md` — current phase, signal flow, universe model
3. `architecture/data_flow.md` — Kafka topic map, data ownership
4. `memory/open_tasks.md` — blockers, known issues, Day 7+ items
5. `memory/decisions.md` — ADR-018 (live tightening), ADR-019 (universe model), ADR-020 (paper readiness sweep)
6. `memory/paper_trading_fixes.md` — FIX-A through FIX-F in effect
7. `docs/operations/paper-trading-acceptance-checklist.md` — current session gate status

Do NOT proceed past this point if any of these files is missing or unreadable. Report the missing file and stop.

---

## Hard Rules — Enforce Throughout the Entire Session

These rules are non-negotiable. They apply from startup to session close. Never deviate.

| Rule | Enforcement |
|------|-------------|
| `paper_trade=True` on every signal | Reject any signal without `paper_trade=True`. Never route to live broker. |
| `RISK_MAX_SIGNAL_AGE_SECONDS=30` | Candle signals are 7-12s old at risk_engine. Do not lower this. |
| No real Zerodha credentials when paper_trade=True | **HARD BLOCK**: If `ZERODHA_API_KEY` resolves to a real production key (not a test stub), stop immediately and warn the user. |
| `QE_EXECUTION_LIVE_TRADING_ENABLED` must be absent or `false` | Verify before starting services. |
| `RISK_PROFILE=paper` | Must be set in `.env` or docker-compose. |
| `UNIVERSE_MODE=PAPER_SAFE_START` | Default for new sessions. Do not change to LIVE_ADVANCED. |
| `PAPER_SEED_NAV=1000000` | Setup service must seed ₹10L NAV. 5x mismatch (₹50L) causes silent over-permission. |
| Strategy configs must have `max_signals_per_day=0` | Seeded by `docker-compose run --rm setup`. |

---

## Pre-Session Startup Sequence

Execute every step in order. Print the step number, action, and PASS/FAIL result.

### Step 1 — Archive Previous Session + Wipe LocalStack

This step is always two phases: **archive first, then wipe**. Never wipe without archiving.

#### Phase A — Archive Previous Session Data

If any previous session exists (i.e., LocalStack has data from yesterday), run the archive script
first to preserve all session data before the volume is destroyed:

```bash
bash scripts/monitoring/archive_session.sh
```

This saves to `session-archives/YYYY-MM-DD/` (today's IST date by default):
- `session_report.txt` — full P&L, signal funnel, enrichment rate, go-live readiness
- `orders.json`, `positions.json`, `fills.json`, `risk-state.json` — DynamoDB table dumps
- `strategy-config.json`, `sessions.json`, `regime-log.json` — supporting tables
- `live_counters.json` — TEE/Router/MIS counters from `/tmp/qe_live_counters.json`
- `*.log` — full logs for all 5 services (data_ingestion, strategy_engine, risk_engine, execution_engine, ai_engine)
- `MANIFEST.txt` — archive summary

PASS when script prints `Archive complete`. FAIL on any error — fix the error before wiping.

> **If this is the very first session ever** (no prior LocalStack data): skip Phase A and go to Phase B.

#### Phase B — Wipe LocalStack

Only run after Phase A completes (or confirmed first-ever session):

```bash
docker-compose down -v
```

PASS when all containers and volumes are removed. This is required to pick up the correct
NAV seed (₹10L) and fresh strategy configs for the new session.

### Step 2 — Start Infrastructure

```bash
docker-compose up -d localstack redpanda
```

Wait for health. Poll every 5s, max 60s:
```bash
docker-compose ps localstack redpanda
```

PASS when both show status `healthy`. FAIL if either is `unhealthy` after 60s — print logs and stop.

### Step 3 — Seed Tables

```bash
docker-compose run --rm setup
```

This seeds:
- Paper NAV: ₹10,00,000 (aligned with `risk_limits_production.yaml`)
- All 6 strategy configs: `max_signals_per_day=0`, `paper_trade=True`
- DynamoDB tables: orders, positions, risk-state, candle-cache, strategy-config, sessions

PASS when exit code = 0. FAIL otherwise — print full output and stop.

### Step 4 — Pre-Flight Check

```bash
python scripts/deploy/paper_preflight_check.py
```

PASS when exit code = 0 (prints `ALL CHECKS PASSED — GO`).
FAIL when exit code = 1 — print the specific check that failed and stop. Do not start services until the underlying issue is fixed.

If the preflight script itself is missing: stop and alert the user — do not proceed.

### Step 5 — Zerodha Token Refresh

```bash
python scripts/zerodha_login.py status
```

If token is expired or missing:
```bash
python scripts/zerodha_login.py
```

Follow the interactive prompts. PASS when token is valid and stored in DynamoDB.

> Note: For pure paper trading, a valid Zerodha token is needed for the candle data feed (IntradayCandleStream) even though no orders are placed. Without a valid token, candle strategies produce zero signals.

### Step 6 — Start All Services

```bash
docker-compose up -d
```

Poll health endpoints every 5s, max 90s:

| Service | Health URL |
|---------|-----------|
| data_ingestion | http://localhost:8081/health |
| strategy_engine | http://localhost:8082/health |
| risk_engine | http://localhost:8083/health |
| execution_engine | http://localhost:8084/health |
| ai_engine | http://localhost:8085/health |

PASS when all 5 return HTTP 200. If any remain unhealthy after 90s, print that service's logs and stop.

### Step 7 — Verify Safety Gates

Run these checks and report PASS/FAIL for each:

```bash
# Check kill switch is INACTIVE
python scripts/kill_switch_cli.py status

# Check RISK_PROFILE
docker-compose exec risk_engine printenv RISK_PROFILE

# Check live trading gate is closed
docker-compose exec execution_engine printenv QE_EXECUTION_LIVE_TRADING_ENABLED

# Check signal age limit
docker-compose exec risk_engine printenv RISK_MAX_SIGNAL_AGE_SECONDS
```

Expected values:
- Kill switch: `INACTIVE`
- `RISK_PROFILE`: `paper`
- `QE_EXECUTION_LIVE_TRADING_ENABLED`: absent or `false`
- `RISK_MAX_SIGNAL_AGE_SECONDS`: `30`

Any deviation = FAIL. Stop and report. Do not allow the session to proceed with a live gate open or signal age < 20.

---

## Market Open Monitoring (09:15 IST / 03:45 UTC)

Once services are running, watch for these events at market open:

### Candle Stream Diagnostic

```bash
docker logs -f data_ingestion | grep "diag_fetch\|candles_fetched\|candle_stream"
```

Expected within 3 minutes of market open:
- `candle_stream.diag_fetch` WARNING log showing `raw_count > 0` for each instrument+interval
- `candle_stream.candles_fetched` showing candles being written to candle-cache

If `raw_count=0` at any instrument: Zerodha token issue — re-run `python scripts/zerodha_login.py`.
If `raw_count > 0` but no `candles_fetched`: DynamoDB write error — check `dynamo_write_error` logs.
If both fire normally: candle stream is healthy. Note: remove diagnostic logs after Day 7 confirms healthy.

### Signal Flow Verification

```bash
# Watch for signals on signals.pending
docker run --rm --network host redpandadata/redpanda:v24.1.1 \
  rpk topic consume signals.pending --brokers localhost:19092 --num 5

# Watch for enriched signals
docker run --rm --network host redpandadata/redpanda:v24.1.1 \
  rpk topic consume signals.enriched --brokers localhost:19092 --num 5

# Watch for approvals
docker run --rm --network host redpandadata/redpanda:v24.1.1 \
  rpk topic consume signals.approved --brokers localhost:19092 --num 5
```

If signals.pending is empty 30 minutes after market open: check candle-cache DynamoDB table for candle rows.
If signals.pending has messages but signals.enriched is empty: ai_engine consumer lag issue.
If signals.enriched has messages but signals.approved is empty: risk_engine rejection — check risk-audit S3 logs.

### First Fill Verification

```bash
# Check for paper orders in DynamoDB
aws --endpoint-url=http://localhost:4566 dynamodb scan \
  --table-name quantembrace-development-orders \
  --filter-expression "attribute_exists(paper_trade)"
```

First fill should appear within ~15 minutes of market open if strategies have qualifying conditions. Zero fills after 45 minutes = investigate.

---

## Monitoring Status

When the user asks for monitoring status during the session, always use the full 15-section monitoring report format:

```bash
python scripts/monitoring/paper_trading_monitor.py \
  --counters /tmp/qe_live_counters.json
```

Or in watch mode:
```bash
python scripts/monitoring/paper_trading_monitor.py \
  --counters /tmp/qe_live_counters.json --watch 60
```

The monitoring report must cover all 15 sections as defined in `docs/operations/monitoring-status-template.md`. Never give a free-form summary — always use the structured report.

---

## MIS Square-Off (15:05 IST / 09:35 UTC)

The execution engine's `MISSquareOffManager` automatically fires at `MIS_CLOSE_TIME_IST` (default 15:05). Watch for:

```bash
docker logs -f execution_engine | grep "mis_square_off"
```

Expected sequence:
1. `mis_square_off.starting` — fires at 15:05
2. `mis_square_off.positions_found_total=N` — all open positions found
3. `mis_square_off.close_order_placed` — one per position
4. `mis_square_off.all_positions_closed` — confirms clean close

If `mis_square_off.positions_still_open_at_deadline` appears: investigate immediately — positions not closed means overnight risk.

---

## Session Close

After 15:30 IST, execute all steps in order:

1. Run the session report and confirm MIS square-off completed:
```bash
python scripts/monitoring/paper_session_report.py --date $(TZ=Asia/Kolkata date +%Y-%m-%d)
```

2. **Archive all session data** (mandatory — must run before any future wipe):
```bash
bash scripts/monitoring/archive_session.sh
```
PASS when it prints `Archive complete`. Do not proceed to next day without this.
Output lands in `session-archives/YYYY-MM-DD/` and is permanent — survives `docker-compose down -v`.

3. Record results in `docs/operations/paper-trading-acceptance-checklist.md` — mark criteria S1–S7 for this session.

4. Check if the session qualifies as PASSED on the acceptance checklist. A session PASSES if:
   - All S1–S7 criteria met
   - No P0/P1 alarms fired
   - MIS square-off completed cleanly
   - No live broker calls (S7 clean)

5. Report session outcome to the user with: fills count, P&L, enrichment rate, kill switch events, and whether the session counts toward the 5-session live-mode gate.

---

## What NEVER Happens in This Mode

- No changes to `paper_trade=False` on any strategy during the session
- No enabling of `QE_EXECUTION_LIVE_TRADING_ENABLED`
- No direct Zerodha order placement (all paper)
- No changes to risk limits during an active session
- No kill switch deactivation without operator confirmation
- No skip of preflight check
- No restart of services during market hours without reconciliation
