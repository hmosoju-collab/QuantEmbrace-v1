# QuantEmbrace — Local Setup Guide

> **Who is this for?** Anyone running QuantEmbrace for the first time on a local machine in paper-trading mode.
> All signals default to `paper_trade=True`. No real broker credentials are required to start.

---

## Prerequisites

| Tool | Minimum version | Install |
|------|----------------|---------|
| Python | 3.11+ | [python.org](https://www.python.org/downloads/) |
| Docker Desktop | 4.x | [docs.docker.com](https://docs.docker.com/get-docker/) |
| Docker Compose | v2 (bundled with Docker Desktop) | bundled |
| AWS CLI v2 | 2.x | [aws.amazon.com/cli](https://aws.amazon.com/cli/) |
| Terraform | 1.5+ | [terraform.io](https://www.terraform.io/downloads) |

AWS CLI is only needed for infrastructure work (Terraform). Local paper trading uses LocalStack and does not require real AWS credentials.

---

## Table of Contents

1. [Repository Setup](#1-repository-setup)
2. [Environment Configuration](#2-environment-configuration)
3. [Option A — Docker Compose (recommended)](#3-option-a--docker-compose-recommended)
4. [Option B — Run Services Directly](#4-option-b--run-services-directly)
5. [Running Tests](#5-running-tests)
6. [Running a Backtest](#6-running-a-backtest)
7. [Verifying Paper Trades](#7-verifying-paper-trades)
8. [Common Problems](#8-common-problems)
9. [Developer Workflow](#9-developer-workflow)

---

## 1. Repository Setup

```bash
# Clone
git clone <repo-url>
cd "QuantEmbrace - A Hedge Level Algo Trading System"

# Python virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

---

## 2. Environment Configuration

```bash
cp .env.example .env
```

Edit `.env`. The minimum fields for local paper trading:

| Variable | Value for local dev | Notes |
|----------|--------------------|----|
| `QE_ENVIRONMENT` | `development` | Already set in template |
| `KAFKA_BOOTSTRAP_SERVERS` | *(leave empty)* | docker-compose overrides to `redpanda:9092` |
| `KAFKA_USE_IAM` | `false` | Local Redpanda uses PLAINTEXT, not MSK IAM |
| `RISK_PROFILE` | `paper` | Already set in template |
| `ZERODHA_API_KEY` | *(optional for paper)* | Only needed for live signals |
| `ALPACA_API_KEY` | *(optional for paper)* | Only needed for live signals |

> **Note:** For pure paper trading, broker credentials are not required. The execution engine routes paper signals to the built-in simulator, which never calls Zerodha or Alpaca.

---

## 3. Option A — Docker Compose (recommended)

This starts LocalStack (DynamoDB + S3), Redpanda (Kafka), and all 5 trading services.

### First-time setup

```bash
# Start infrastructure only
docker-compose up -d localstack redpanda

# Wait ~20s for health checks, then create tables and Kafka topics
# This also: seeds paper NAV at ₹10L and strategy configs (max_signals=0 per strategy)
docker-compose run --rm setup

# Run pre-flight check before starting services
python scripts/deploy/paper_preflight_check.py  # must exit 0

# Start all trading services
docker-compose up -d
```

> **Important (2026-05-27):** After any `docker-compose down -v`, you must re-run `setup` before starting services. The setup job seeds:
> - Paper NAV: ₹10,00,000 (aligned with `risk_limits_production.yaml`)
> - Strategy configs: all 6 strategies with `max_signals_per_day=0` (unlimited), `paper_trade=True`
> 
> Without this, risk engine uses a ₹50L NAV default (5× mismatch) and strategies are silently capped at 10 signals/day each.

### Subsequent starts

```bash
docker-compose up -d
```

### View logs

```bash
docker-compose logs -f                     # all services
docker-compose logs -f risk_engine         # single service
```

### Stop

```bash
docker-compose down                        # keep volumes
docker-compose down -v                     # also wipe LocalStack + Redpanda data
```

### Service health endpoints

| Service | Port | URL |
|---------|------|-----|
| Redpanda Console (topic browser) | 8080 | http://localhost:8080 |
| data_ingestion | 8081 | http://localhost:8081/health |
| strategy_engine | 8082 | http://localhost:8082/health |
| risk_engine | 8083 | http://localhost:8083/health |
| execution_engine | 8084 | http://localhost:8084/health |
| ai_engine | 8085 | http://localhost:8085/health |
| LocalStack | 4566 | http://localhost:4566/_localstack/health |
| Redpanda Kafka | 19092 | `localhost:19092` (Kafka protocol) |

---

## 4. Option B — Run Services Directly

Use this when iterating quickly on a single service without rebuilding Docker images.

### Start infrastructure

```bash
docker-compose up -d localstack redpanda
docker-compose run --rm setup
```

### Set local environment

```bash
export KAFKA_BOOTSTRAP_SERVERS=localhost:19092
export KAFKA_USE_IAM=false
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=ap-south-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export DYNAMODB_TABLE_PREFIX=quantembrace-development
export QE_ENVIRONMENT=development
export RISK_PROFILE=paper
export PYTHONPATH=$(pwd)/services
```

### Run individual services

```bash
python -m data_ingestion.service
python -m strategy_engine.service
python -m risk_engine.service
python -m execution_engine.service
python -m ai_engine.service
```

Each service reads `.env` via pydantic-settings. The `PYTHONPATH` above makes `services/` importable without package installation.

---

## 5. Running Tests

```bash
# Unit tests (no Docker required)
pytest tests/unit/ -v

# Unit tests with coverage
pytest tests/unit/ --cov=services --cov-report=term-missing

# Integration tests (require LocalStack + Redpanda running)
docker-compose up -d localstack redpanda
docker-compose run --rm setup
pytest tests/integration/ -v
```

---

## 6. Running a Backtest

```bash
# Download historical data (NSE example)
python scripts/backtest/fetch_historical.py \
  --symbol RELIANCE \
  --from 2025-01-01 \
  --to 2025-12-31

# Run a backtest
python scripts/backtest/run_backtest.py \
  --strategy momentum \
  --config configs/backtest_momentum.yaml
```

Backtest artifacts are written to `S3` (LocalStack in dev) under the `quantembrace-development-data` bucket.

---

## 7. Verifying Paper Trades

Paper trades are processed through the full signal pipeline with `paper_trade=True` stamped on each signal. They produce real DynamoDB order records and Kafka `orders.events` messages — only the broker call is simulated.

```bash
# List paper orders in DynamoDB (LocalStack)
aws --endpoint-url=http://localhost:4566 dynamodb scan \
  --table-name quantembrace-development-orders \
  --filter-expression "attribute_exists(paper_trade)"

# Watch live Kafka signals
docker run --rm --network host \
  redpandadata/redpanda:v24.1.1 \
  rpk topic consume signals.approved --brokers localhost:19092

# Check risk engine decisions in S3
aws --endpoint-url=http://localhost:4566 s3 ls \
  s3://quantembrace-development-logs/risk/
```

---

## 8. Common Problems

### `KAFKA_BOOTSTRAP_SERVERS` not set

```
ERROR: KAFKA_BOOTSTRAP_SERVERS is required
```

**Fix:** Set `KAFKA_BOOTSTRAP_SERVERS=localhost:19092` (Option B) or use docker-compose (overrides automatically).

---

### LocalStack DynamoDB table not found

```
ResourceNotFoundException: Requested resource not found
```

**Fix:** Run setup: `docker-compose run --rm setup`

---

### Redpanda health check fails on first `docker-compose up`

The Redpanda `rpk cluster health` check takes ~20s on first start. Run:

```bash
docker-compose up -d redpanda
docker-compose ps redpanda    # wait until status is "healthy"
```

---

### `ssl.SSLError` or `SASL_SSL` connection refused to Redpanda

Local Redpanda uses PLAINTEXT. Ensure `KAFKA_USE_IAM=false` is set. The MSK IAM path (`SASL_SSL`) is for production MSK Serverless only.

---

### Port conflicts

If ports 8080–8085 or 4566 are in use, override in `docker-compose.override.yml`:

```yaml
services:
  localstack:
    ports:
      - "4567:4566"
```

---

## 9. Developer Workflow

### Adding a new strategy

1. Create `services/strategy_engine/strategies/<name>.py` implementing the `BaseStrategy` protocol.
2. Register it in `services/strategy_engine/registry.py`.
3. Add a DynamoDB entry in `quantembrace-development-strategy-config` with `paper_trade=True`.
4. Restart `strategy_engine`: `docker-compose restart strategy_engine`.

### Promoting a paper signal to live

1. Complete 5 consecutive profitable paper-trading days.
2. Update the strategy config in DynamoDB: set `paper_trade=False`.
3. Ensure `configs/risk_limits_production.yaml` checklist items are all `true`.
4. Run `python scripts/deploy/preflight_check.py` — must produce zero warnings.

### Kafka topic inspection

```bash
# List topics
docker run --rm --network host redpandadata/redpanda:v24.1.1 \
  rpk topic list --brokers localhost:19092

# Tail a topic
docker run --rm --network host redpandadata/redpanda:v24.1.1 \
  rpk topic consume ticks.nse --brokers localhost:19092 --num 10
```

Or open the Redpanda Console at http://localhost:8080 for a visual topic browser.

---

## Architecture Reference

| Layer | Service | Kafka topics |
|-------|---------|--------------|
| Data Ingestion | `data_ingestion` | → `ticks.nse`, `ticks.us` |
| Strategy | `strategy_engine` | `ticks.*` → `signals.pending` |
| AI Enrichment | `ai_engine` | `signals.pending` → `signals.enriched` |
| Risk | `risk_engine` | `signals.enriched` → `signals.approved` |
| Execution | `execution_engine` | `signals.approved` → `orders.events` |

For full architecture details: [architecture/system_design.md](../architecture/system_design.md)

Compute: EC2 ARM64 ASGs (c6g/t4g) — one ASG per service.
Messaging: Kafka MSK Serverless, port 9098, SASL/OAUTHBEARER IAM auth.
State: DynamoDB (orders, positions, sessions, risk state).
Storage: S3 (raw ticks, execution logs, backtest artifacts, ML model artifacts).

---

## 10. Paper Trading Monitor

The paper trading monitor produces the 15-section monitoring report without connecting to a running service. It reads counters from a JSON file written by the execution engine and queries DynamoDB for position/risk state.

### Quick start — services not running (offline mode)

```bash
# Seed LocalStack with sample positions
python scripts/monitoring/seed_local_positions.py

# Run the monitor using the sample counters stub
python scripts/monitoring/paper_trading_monitor.py \
  --counters scripts/monitoring/sample_counters.json
```

### Quick start — services running

```bash
# Run the monitor using live counters from execution engine
python scripts/monitoring/paper_trading_monitor.py \
  --counters /tmp/qe_live_counters.json

# Watch mode: auto-refresh every 60 seconds
python scripts/monitoring/paper_trading_monitor.py \
  --counters /tmp/qe_live_counters.json --watch 60
```

The execution engine writes `LiveCounters` JSON to `/tmp/qe_live_counters.json` (configurable via `QE_MONITORING_COUNTERS_PATH`) every 60 seconds. If this file doesn't exist, all service-side counters show as `UNKNOWN` in the report.

### Seeder options

```bash
# Seed default 4 positions (RELIANCE, INFY, HDFCBANK, TCS)
python scripts/monitoring/seed_local_positions.py

# Wipe and re-seed (clean slate)
python scripts/monitoring/seed_local_positions.py --reset
```

### Files

| File | Purpose |
|---|---|
| `scripts/monitoring/paper_trading_monitor.py` | CLI runner — 15-section monitoring report |
| `scripts/monitoring/seed_local_positions.py` | Seed LocalStack with sample positions for offline testing |
| `scripts/monitoring/sample_counters.json` | Static `LiveCounters` stub (no services required) |
| `services/shared/monitoring/monitoring_status.py` | Core monitoring service and renderer |
| `docs/operations/monitoring-status-template.md` | Full template specification |
