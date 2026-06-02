# Monitoring Agent

A standalone, **read-only** observability service for the QuantEmbrace trading
platform. It runs as its own container alongside the five trading services, polls
the platform's health on a fixed interval, writes a health snapshot, records an
edge-triggered incident log, and posts Slack alerts on status changes.

> **Phase 1 status — READ-ONLY / NOTIFY-ONLY.** This agent observes, records, and
> notifies. It wires **no action layer**. There is no code path from anything it
> observes to a trade, an exposure change, a risk override, or the kill switch.

---

## Safety contract (non-negotiable)

The agent's whole reason to exist is to *reduce* the need for a human to stare at
dashboards — never to act on the platform. The contract, enforced by design and by
the test suite:

- **It can reduce risk only — and in Phase 1 it takes no actions at all.** It never
  places trades, increases exposure, overrides a risk rejection, changes NAV, or
  disables the kill switch.
- **It only reads.** Collectors call read-only APIs exclusively: HTTP `GET /health`
  + `/ready`, Kafka metadata + committed-offset reads, DynamoDB `DescribeTable` +
  point reads, Docker `list`/`inspect`. The test suite parses every collector with
  `ast` and fails the build if any write/mutate method appears (`tests/unit/test_monitoring_agent.py::test_collectors_make_no_write_calls`).
- **It never joins the trading event stream.** Kafka lag is measured without
  subscribing, polling, or committing — a throwaway consumer reads watermarks only.
- **It never touches secrets.** The broker is monitored by *inferring* feed
  freshness from the `latest-prices` table, never by calling the broker API or
  reading the access-token item. The Slack webhook URL is never logged or snapshotted.
- **Defaults are safe and mandatory:** `MONITORING_DRY_RUN=true`,
  `MONITORING_ACTION_MODE=notify_only`. If an operator sets unsafe values the app
  logs `CRITICAL` and still does nothing — because no action layer exists to run.

If any of these need to change, that is a Phase 3/4 decision gated by an explicit
operator sign-off — see the roadmap below.

---

## What it watches

Policy lives in [`rules.yaml`](./rules.yaml) (safe to commit — no secrets). Six
collectors run concurrently each cycle; each returns a coarse status
(`ok` / `degraded` / `down` / `unknown`) plus secret-free detail:

| Collector | Reads | Coarse signal |
|---|---|---|
| `services` | `GET /health` + `/ready` on the 5 trading services (ports 8081–8085) | unreachable → `down`; 200/200 → `ok`; else `degraded` |
| `kafka` | `AdminClient` metadata + committed offsets; throwaway watermark reads | cluster unreachable → `down`; missing critical topic / idle critical group → `degraded` |
| `dynamodb` | `DescribeTable` (+ optional point read) on the 12 platform tables | missing critical table → `down`; non-ACTIVE → `degraded` |
| `broker` | Freshness of the `latest-prices` table (market-hours aware) | open + stale/empty feed → `degraded`; closed → `ok` |
| `docker` | Container liveness + restart counts via the daemon socket | not running → `down`; restarting / unhealthy → `degraded` |
| `logs` | Error-pattern scan over recent log files (**opt-in**, disabled by default) | matches → `degraded` |

The overall status is **worst-wins**, with one nuance: a *non-critical* service or
table that is `down` only degrades the overall result rather than painting the whole
platform `down`.

---

## Architecture

```
                          ┌──────────────── rules.yaml (policy: what to watch) ───────────────┐
                          │                                                                   │
  env (config + secrets)  ▼                                                                   │
        │        ┌─────────────────┐   concurrent, read-only, never-raise                     │
        └───────▶│  collectors[]   │── services · kafka · dynamodb · broker · docker · logs ──┘
                 └───────┬─────────┘
                         ▼
                 ┌─────────────────┐   worst-wins roll-up
                 │  HealthSnapshot │──────────────┬───────────────┐
                 └─────────────────┘              ▼               ▼
                                          health_snapshot.json   structured JSON log
                         │
                         ▼
                 ┌─────────────────┐   edge-triggered (alert on transition, restart-safe)
                 │  IncidentLog    │── monitoring_incidents.jsonl
                 └───────┬─────────┘
                         ▼
                 ┌─────────────────┐   summaries only — never raw details, never the webhook URL
                 │  SlackNotifier  │── Slack incoming webhook (or log-only when unset)
                 └─────────────────┘
```

Two separated inputs:

- **`config.py`** — runtime knobs + **secrets** from the environment (poll cadence,
  AWS endpoint, table prefix, Kafka bootstrap, Slack webhook, the `DRY_RUN` /
  `ACTION_MODE` safety flags). Never serialised to the snapshot.
- **`rules.yaml`** — declarative **policy** (which services/ports, which consumer
  groups + topics, which tables, thresholds for later phases). Safe to log/commit.

The agent writes only its **own** artifacts — `health_snapshot.json` and
`monitoring_incidents.jsonl` under `/app/data`. Writing those is not a
trading-platform write; the incident log is replayed on startup so a restart does
not re-alert a still-broken component (**restart-safe**).

---

## Running it

### With docker-compose (recommended)

The `monitoring_agent` service is wired into `docker-compose.yml` (port **8086**,
Docker socket mounted `:ro`, `monitoring-data` volume, depends on `setup`):

```bash
docker-compose up -d monitoring_agent
docker-compose logs -f monitoring_agent
curl -s localhost:8086/health        # liveness
curl -s localhost:8086/ready         # readiness (ready after first successful cycle)
```

### Standalone

```bash
# long-running poll loop
python -m monitoring_agent.app

# a single cycle then exit (cron / CI / smoke test) — prints the snapshot as JSON,
# exit code 1 if overall status is DOWN
python -m monitoring_agent.app --once
```

### Slack

Set `SLACK_WEBHOOK_URL` (and optionally `SLACK_CHANNEL`). With no webhook the agent
degrades gracefully to logging alert summaries at `INFO` — still useful in dev.

---

## Configuration reference

| Env var | Default | Meaning |
|---|---|---|
| `MONITORING_DRY_RUN` | `true` | Safety flag. Phase 1 takes no action regardless. |
| `MONITORING_ACTION_MODE` | `notify_only` | `notify_only` \| `safe_actions` (P3) \| `risk_reduce` (P4). |
| `MONITORING_POLL_INTERVAL_SECONDS` | `30` | Seconds between cycles. |
| `QE_HEALTH_CHECK_PORT` | `8086` | Port for the agent's own `/health` + `/ready`. |
| `MONITORING_RULES_PATH` | `/app/services/monitoring_agent/rules.yaml` | Policy file. |
| `MONITORING_SNAPSHOT_PATH` | `/app/data/health_snapshot.json` | Latest snapshot. |
| `MONITORING_INCIDENT_LOG_PATH` | `/app/data/monitoring_incidents.jsonl` | Append-only transitions. |
| `KAFKA_BOOTSTRAP_SERVERS` | — | Redpanda (`redpanda:9092`) locally; MSK in prod. |
| `KAFKA_USE_IAM` | `true` | `false` locally (PLAINTEXT); IAM/SASL for MSK. |
| `AWS_ENDPOINT_URL` | — | LocalStack URL locally; unset in prod. |
| `DYNAMODB_TABLE_PREFIX` | `quantembrace-development` | Joined with each table suffix. |
| `SLACK_WEBHOOK_URL` | — | **Secret.** Unset → log-only fallback. |
| `SLACK_CHANNEL` | `#trading-ops` | Optional channel hint. |

---

## Tests

```bash
python -m pytest tests/unit/test_monitoring_agent.py -q
```

Covers the read-only API audit, config redaction, the worst-wins roll-up, the
fail-soft rules loader (validated against the shipped `rules.yaml`), every collector
decision function, Slack secret-safety, and the edge-triggered / restart-safe
incident log. All tests are synchronous (async paths driven via `asyncio.run`) so
the suite needs none of the agent's optional heavy dependencies installed.

---

## Roadmap (later phases — NOT implemented here)

Phase 1 deliberately stops at observe + record + notify. Each later phase is a
separate, gated change.

| Phase | Adds | Gate |
|---|---|---|
| 2 | Detector + INFO/WARNING/CRITICAL/BLOCKER severity engine (consumes the lag/restart/staleness thresholds already parsed from `rules.yaml`) | — |
| 3 | `safe_actions` — restart a *non-critical* container, re-run reconciliation. Never touches capital path. | operator enables `ACTION_MODE=safe_actions` |
| 4 | `risk_reduce` — trigger the kill switch / pause intake when risk is clearly breached. **Reduce-risk only, never increase exposure.** | operator enables `ACTION_MODE=risk_reduce` + sign-off |
| 5 | Daily health report artifact | — |
| 6 | Optional AI-written incident summary | — |

The `detectors/`, `actions/`, and `reports/` packages exist as documented,
empty placeholders so later phases slot in without restructuring.
