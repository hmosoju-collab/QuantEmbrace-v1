# Monitoring Agent Runbook

> **Purpose:** Operate and triage the QuantEmbrace monitoring agent — a standalone,
> **read-only** observer of the trading platform.
> **Safety guarantee:** The agent only observes, records, and alerts. In Phase 1 it
> wires **no action layer** — it cannot place a trade, change exposure, override a
> risk rejection, or touch the kill switch. Every escalation below is a **human**
> action; the agent never performs them.

---

## 1 — What this service is (and is not)

| | |
|---|---|
| **Is** | A sidecar that polls the 5 trading services, Kafka, DynamoDB, the inferred broker feed, and Docker every ~30s; writes a health snapshot; alerts Slack on status *changes*. |
| **Is not** | A trading component. It is never on the capital path. It must never be made to act on the platform in Phase 1. |
| **Port** | `8086` (`/health`, `/ready`). The trading services own 8081–8085. |
| **Outputs** | `/app/data/health_snapshot.json` (latest), `/app/data/monitoring_incidents.jsonl` (append-only transitions). Both on the `monitoring-data` volume. |
| **Policy** | `services/monitoring_agent/rules.yaml` (what to watch). |
| **Secrets** | Slack webhook via `SLACK_WEBHOOK_URL` only. Never logged, never in the snapshot. |

---

## 2 — Start / stop / status

```bash
# Start (local stack)
docker-compose up -d monitoring_agent

# Is the agent itself alive / ready?
curl -s localhost:8086/health      # 200 = process up
curl -s localhost:8086/ready       # 200 = at least one cycle completed

# Live logs
docker-compose logs -f monitoring_agent

# One-off cycle without the loop (prints the snapshot, exit 1 if overall DOWN)
docker-compose run --rm monitoring_agent --once

# Stop
docker-compose stop monitoring_agent
```

The agent depends on `setup` completing (tables + topics created). It is
`restart: unless-stopped`; a crash-restart replays the incident log and will **not**
re-alert components that are still in their last-known bad state.

---

## 3 — Read the current health snapshot

```bash
docker-compose exec monitoring_agent cat /app/data/health_snapshot.json | python -m json.tool
```

Top-level fields: `overall_status` (worst-wins), `dry_run` (always `true` in P1),
`action_mode` (always `notify_only`), and `results[]` — one entry per collector with
`status`, a one-line `summary`, and secret-free `details`.

Status meanings:

| Status | Meaning | Operator urgency |
|---|---|---|
| `ok` | Reachable and behaving. | none |
| `degraded` | Reachable but impaired (lag, stale feed, non-critical down, unhealthy container). | investigate |
| `down` | A **critical** component is unreachable / not running. | act now |
| `unknown` | The agent is **blind** here (collector error or optional dep missing). | check the agent, not necessarily the platform |

> `unknown` means *we cannot tell*, not *it is broken*. Treat it as a gap in
> visibility — confirm the agent's own dependencies (socket mounted, bootstrap set)
> before assuming a platform fault.

---

## 4 — Alert triage by collector

Slack alerts are **edge-triggered** (sent on a status change, plus on recovery to
`ok`). The alert carries the component, the transition (`from → to`), and a summary
— never raw log lines or metrics. For each alert, read the snapshot `details` for
that component, then follow the matching row.

### 4.1 `services` (HTTP health)

| Symptom | Likely cause | Check |
|---|---|---|
| one service `down` | container crashed / not serving | `docker-compose ps`; `docker-compose logs <svc>`; restart the service if appropriate (operator action) |
| `degraded` (health 200, ready 503) | service up but not ready (warming, waiting on a dependency) | give it `start_period`; if persistent, inspect that service's readiness deps |
| `ai_engine` down only | **expected to degrade, not page** — it is non-critical; risk falls back to `risk-v1-fallback` | confirm the enrichment fallback is active in `risk_engine` logs |

### 4.2 `kafka`

| Symptom | Likely cause | Check |
|---|---|---|
| cluster `down` | Redpanda/MSK unreachable | `docker-compose ps redpanda`; bootstrap/network/IAM config |
| missing critical topic | topic not created | re-run `scripts/kafka/create_topics.py`; verify against `rules.yaml` topic list |
| idle critical group (`no committed offsets`) | a consumer (e.g. `risk-v1`, `execution-v1`) is not running/consuming | check that service's logs and health |
| `unknown` (offset read errors) | transient broker timeout or `confluent-kafka` missing in the image | re-check next cycle; confirm the dependency is installed |

> Lag *numbers* are recorded in `details` but Phase 1 does **not** alert on lag
> thresholds — that is the Phase 2 severity engine. Phase 1 flags only a missing
> critical topic or a critical group with no committed offsets at all.

### 4.3 `dynamodb`

| Symptom | Likely cause | Check |
|---|---|---|
| critical table missing (`down`) | table not created | re-run `scripts/setup_local_tables.py`; confirm `DYNAMODB_TABLE_PREFIX` matches |
| table non-ACTIVE (`degraded`) | CREATING/UPDATING | transient — confirm it reaches ACTIVE |
| `unknown` | LocalStack/endpoint unreachable or boto3 missing | check `AWS_ENDPOINT_URL` and LocalStack health |

### 4.4 `broker` (inferred from `latest-prices` freshness)

| Symptom | Likely cause | Check |
|---|---|---|
| `degraded` — feed stale while market open | data_ingestion not writing ticks, or broker WebSocket dropped | `data_ingestion` health + logs; for Zerodha, token may have expired (~07:30 IST daily) → `python scripts/zerodha_login.py` |
| `degraded` — no prices while market open | feed never started this session | confirm `STRATEGY_WATCHLIST_NSE` set + data_ingestion running |
| `ok` — "market closed" | expected outside market hours | none — the agent is market-hours aware |

> The agent infers the broker from persisted state on purpose; it never calls the
> broker API and never reads the access-token item. A `broker` alert is a signal to
> look at **data_ingestion**, not to log into the broker.

### 4.5 `docker`

| Symptom | Likely cause | Check |
|---|---|---|
| container `down` (exited/dead) | crash | `docker-compose logs <name>`; restart (operator action) |
| `degraded` (restarting / unhealthy) | crash-loop or failing healthcheck | inspect logs; rising `restart_count` in `details` |
| `unknown` — cannot reach daemon | Docker socket not mounted | confirm the `:ro` socket mount on the agent |

### 4.6 `logs` (opt-in)

Disabled by default. When enabled in `rules.yaml`, an error-pattern match →
`degraded`. Sample lines live only in the snapshot `details` (never sent to Slack).
Open the snapshot to see which file and pattern matched.

---

## 5 — Slack setup

1. Create an incoming webhook for the target channel.
2. Set `SLACK_WEBHOOK_URL` in the agent's environment (and optionally `SLACK_CHANNEL`).
3. Restart the agent.

With no webhook configured the agent logs alert summaries at `INFO` instead — it
stays useful in dev. Delivery failures are swallowed (logged by exception *type*
only); the webhook URL is never written to logs.

---

## 6 — Safety boundaries (do not cross in Phase 1)

- **Never** add an action call to a collector. Collectors are read-only; the test
  `tests/unit/test_monitoring_agent.py::test_collectors_make_no_write_calls`
  fails the build if any write/mutate method appears.
- **Never** set `MONITORING_ACTION_MODE` to `safe_actions` or `risk_reduce`
  expecting action — Phase 1 has no action layer; the app logs `CRITICAL` and does
  nothing. Those modes belong to Phases 3/4 and require explicit operator sign-off.
- **Never** give the agent broker credentials or point it at the trading event
  stream as a real consumer. It must never join a consumer group.
- The agent **complements** the platform's own kill switch and risk controls; it
  does not replace, bypass, or weaken them.

---

## 7 — Health of the agent itself

| Check | Command | Healthy result |
|---|---|---|
| Process up | `curl -s localhost:8086/health` | 200 |
| Completed a cycle | `curl -s localhost:8086/ready` | 200 |
| Cycle cadence | `docker-compose logs monitoring_agent \| grep "health cycle"` | a line ≈ every poll interval |
| Snapshot fresh | `stat` the snapshot file's mtime | within ~1 poll interval |
| No silent crash-loop | `docker-compose ps monitoring_agent` | `Up`, low restart count |

If the agent is `unknown` across the board, the fault is usually the **agent's**
environment (missing deps, unmounted socket, wrong bootstrap/endpoint) rather than
the trading platform.
