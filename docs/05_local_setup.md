# QuantEmbrace — Local Setup Guide

> **Who is this for?** Anyone setting up QuantEmbrace for the first time on their local machine. This guide starts from absolute zero.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Repository Setup](#repository-setup)
3. [Environment Configuration](#environment-configuration)
4. [Start Local Infrastructure](#start-local-infrastructure)
5. [Running Individual Services](#running-individual-services)
6. [Running the Full Stack](#running-the-full-stack)
7. [Running Tests](#running-tests)
8. [Running a Backtest](#running-a-backtest)
9. [Common Setup Problems](#common-setup-problems)
10. [Developer Workflow](#developer-workflow)

---

## Prerequisites

Install these before starting:

### 1. Python 3.11+

```bash
# Check your Python version
python --version   # Must be 3.11 or higher

# macOS (via Homebrew)
brew install python@3.11

# Ubuntu/Debian
sudo apt install python3.11 python3.11-venv

# Windows: Download from python.org
```

### 2. Docker Desktop

Required for running LocalStack (local AWS simulation) and all services together.

- macOS/Windows: https://www.docker.com/products/docker-desktop/
- Linux: https://docs.docker.com/engine/install/

```bash
docker --version   # Should print Docker version
docker compose version  # Should print Docker Compose version
```

### 3. AWS CLI v2

Even for local development, the AWS CLI is needed for interacting with LocalStack.

```bash
# macOS
brew install awscli

# Linux
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip && sudo ./aws/install

# Verify
aws --version
```

### 4. Terraform 1.5+

For deploying to real AWS (not needed for local-only development):

```bash
# macOS
brew tap hashicorp/tap
brew install hashicorp/tap/terraform

# Linux
# Follow https://developer.hashicorp.com/terraform/install

terraform --version
```

### 5. Broker API Credentials (for live data / paper trading)

- **Zerodha Kite Connect:** Register at https://kite.trade and create an app. You'll get an API key and secret.
- **Alpaca:** Sign up at https://alpaca.markets. The free account gives paper trading access. For live trading, complete KYC.

**Important:** For local development, you can use paper trading credentials. Never use live trading credentials in a local dev environment.

---

## Repository Setup

```bash
# 1. Clone the repository
git clone <repo-url>
cd "QuantEmbrace - A Hedge Level Algo Trading System"

# 2. Create a Python virtual environment
python3.11 -m venv .venv

# 3. Activate the virtual environment
# macOS / Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# You should see (.venv) in your terminal prompt now

# 4. Install all dependencies
pip install -r services/requirements.txt

# 5. Verify installation
python -c "import pydantic; print('OK')"
```

---

## Environment Configuration

### Create your .env file

```bash
# Copy the template
cp .env.example .env

# Open in your editor
nano .env  # or: code .env, vim .env, etc.
```

### Fill in these values

```bash
# ─── BROKER: ZERODHA (NSE India) ───────────────────────────────────────────
KITE_API_KEY=your_kite_api_key_here
KITE_API_SECRET=your_kite_api_secret_here
KITE_ACCESS_TOKEN=                   # Leave blank — set at runtime after login

# ─── BROKER: ALPACA (US Equities) ──────────────────────────────────────────
ALPACA_API_KEY=your_alpaca_api_key_here
ALPACA_API_SECRET=your_alpaca_api_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets   # Use paper for development

# ─── AWS (use fake values for LocalStack, real values for prod) ────────────
AWS_REGION=ap-south-1
AWS_ACCESS_KEY_ID=test               # LocalStack accepts any value
AWS_SECRET_ACCESS_KEY=test           # LocalStack accepts any value

# ─── APPLICATION CONFIG ─────────────────────────────────────────────────────
ENVIRONMENT=dev
LOG_LEVEL=DEBUG
DYNAMODB_TABLE_PREFIX=qe-dev
S3_BUCKET_DATA=quantembrace-market-data-history
S3_BUCKET_LOGS=quantembrace-trading-logs

# ─── SQS QUEUES (LocalStack URLs for local dev) ────────────────────────────
SQS_MARKET_DATA_QUEUE_URL=http://localhost:4566/000000000000/qe-dev-market-data
SQS_SIGNALS_QUEUE_URL=http://localhost:4566/000000000000/qe-dev-signals.fifo
SQS_ORDERS_QUEUE_URL=http://localhost:4566/000000000000/qe-dev-orders.fifo

# ─── RISK PARAMETERS ────────────────────────────────────────────────────────
PORTFOLIO_VALUE=1000000              # 10 lakh INR or $10,000 USD (adjust to your capital)
MAX_POSITION_SIZE_PCT=5.0
MAX_TOTAL_EXPOSURE_PCT=80.0
MAX_DAILY_LOSS_PCT=3.0
```

---

## Start Local Infrastructure

LocalStack simulates AWS services (SQS, DynamoDB, S3) on your local machine. This means you can develop and test without any AWS costs.

### Start LocalStack and DynamoDB Local

```bash
# From the project root
docker compose up -d localstack dynamodb-local

# Wait ~10 seconds for services to initialize, then verify:
curl http://localhost:4566/_localstack/health
# Should see: {"services": {"s3": "running", "sqs": "running", ...}}
```

### Create required tables and queues

```bash
# Run the setup script to create all DynamoDB tables and SQS queues locally
python scripts/setup_local_tables.py

# Expected output:
# ✓ Created DynamoDB table: qe-dev-latest-prices
# ✓ Created DynamoDB table: qe-dev-positions
# ✓ Created DynamoDB table: qe-dev-orders
# ✓ Created DynamoDB table: qe-dev-risk-state
# ✓ Created SQS queue: qe-dev-market-data
# ✓ Created SQS FIFO queue: qe-dev-signals.fifo
# ✓ Created SQS FIFO queue: qe-dev-orders.fifo
# ✓ Created S3 bucket: quantembrace-market-data-history
# ✓ Created S3 bucket: quantembrace-trading-logs
# Setup complete!
```

---

## Running Individual Services

You can run each service individually for focused development:

```bash
# Activate virtual environment first
source .venv/bin/activate

# Load environment variables
export $(cat .env | xargs)

# Run a specific service
python -m services.data_ingestion.main
python -m services.strategy_engine.main
python -m services.risk_engine.main
python -m services.execution_engine.main
python -m services.ai_engine.main
```

### What you'll see when each service starts

**data_ingestion:**
```
INFO  Starting Data Ingestion Service
INFO  Connecting to Zerodha Kite Ticker...
INFO  WebSocket connected — subscribing to 5 instruments
INFO  Connecting to Alpaca WebSocket...
INFO  WebSocket connected — subscribing to 5 symbols
INFO  Data Ingestion Service started
```

**risk_engine:**
```
INFO  Starting Risk Engine Service
INFO  Loading kill switch state from DynamoDB...
INFO  Kill switch state: active=False
INFO  Risk Engine Service started
```

---

## Running the Full Stack

To run all services together:

```bash
# This starts all services + local infrastructure
docker compose up

# To run in background
docker compose up -d

# View logs for a specific service
docker compose logs -f risk_engine
docker compose logs -f strategy_engine

# Stop everything
docker compose down
```

### Service Health Check

After starting, verify all services are healthy:

```bash
# Check ECS task status (local simulation)
docker compose ps

# Should show all services as "Up":
# NAME                STATUS
# data_ingestion_nse  Up
# data_ingestion_us   Up
# strategy_engine     Up
# risk_engine         Up
# execution_engine    Up
# localstack          Up
# dynamodb_local      Up
```

---

## Running Tests

### Run all unit tests

```bash
cd "QuantEmbrace - A Hedge Level Algo Trading System"
source .venv/bin/activate

pytest tests/unit/ -v
```

### Run tests for a specific service

```bash
pytest tests/unit/risk_engine/ -v
pytest tests/unit/strategy_engine/ -v
pytest tests/unit/execution_engine/ -v
```

### Run integration tests (requires LocalStack running)

```bash
# Start LocalStack first
docker compose up -d localstack dynamodb-local
python scripts/setup_local_tables.py

# Run integration tests
pytest tests/integration/ -v
```

### Run with coverage report

```bash
pytest tests/unit/ --cov=services --cov-report=html
open htmlcov/index.html  # View coverage in browser
```

**Minimum coverage target:** 85% for core trading logic (risk, execution, order management).

### Running property-based tests (hypothesis)

The risk engine uses hypothesis for property-based testing — it generates thousands of random inputs to find edge cases:

```bash
pytest tests/unit/risk_engine/ -v -k "hypothesis"
# This may take 30–60 seconds — hypothesis runs many iterations
```

---

## Running a Backtest

Backtesting lets you test a strategy against historical data without risking real money.

### Step 1: Download historical data

```bash
# Download 1 year of RELIANCE tick data
python scripts/data_download/fetch_historical.py \
    --symbol RELIANCE \
    --market NSE \
    --from 2025-01-01 \
    --to 2025-12-31

# Download US equity data
python scripts/data_download/fetch_historical.py \
    --symbol AAPL \
    --market US \
    --from 2025-01-01 \
    --to 2025-12-31

# Data is saved to: s3://quantembrace-market-data-history/ (or local cache)
```

### Step 2: Run the backtest

```bash
python scripts/backtest/run.py \
    --strategy momentum \
    --config configs/backtest_momentum.yaml \
    --from 2025-01-01 \
    --to 2025-12-31

# With specific symbols:
python scripts/backtest/run.py \
    --strategy momentum \
    --symbols RELIANCE,TCS,INFY \
    --market NSE \
    --from 2025-06-01 \
    --to 2025-12-31
```

### Step 3: Interpret results

```
Backtest Results: MomentumStrategy (NSE, 2025-01-01 to 2025-12-31)
═══════════════════════════════════════════════════════════════════
Capital:          ₹10,00,000
Final Value:      ₹11,43,250
Total Return:     +14.3%
Sharpe Ratio:     1.42            ← Above 1.0 is generally good
Max Drawdown:     -6.8%           ← Worst peak-to-trough decline
Win Rate:         54.2%           ← 54% of trades were profitable
Total Trades:     187
Avg Trade P&L:    ₹765
Avg Hold Time:    2h 14m
═══════════════════════════════════════════════════════════════════
```

**Note:** Past backtest performance does not guarantee future results. Backtesting has survivorship bias and look-ahead bias risks. Always paper trade before going live.

---

## Common Setup Problems

### Problem: `LocalStack not ready` when running setup script

```
Error: Connection refused to localhost:4566
```

**Fix:** LocalStack takes ~15 seconds to start. Wait and retry.
```bash
sleep 15 && python scripts/setup_local_tables.py
```

### Problem: `ModuleNotFoundError: No module named 'kiteconnect'`

```
ModuleNotFoundError: No module named 'kiteconnect'
```

**Fix:** Virtual environment not activated, or requirements not installed.
```bash
source .venv/bin/activate
pip install -r services/requirements.txt
```

### Problem: Zerodha authentication fails

```
Error: Invalid API key or access token
```

**Fix:** Zerodha access tokens expire daily. Generate a fresh one:
```bash
python scripts/zerodha_login.py  # Opens browser for login, saves token to .env
```

### Problem: DynamoDB errors with `ResourceNotFoundException`

```
botocore.errorfactory.ResourceNotFoundException: Requested resource not found
```

**Fix:** Tables not created yet. Run setup:
```bash
python scripts/setup_local_tables.py
```

### Problem: Tests failing with import errors

```
ImportError: attempted relative import beyond top-level package
```

**Fix:** Run tests from the project root, not from a subdirectory:
```bash
# ❌ Wrong
cd services/risk_engine && pytest

# ✅ Correct
cd "QuantEmbrace - A Hedge Level Algo Trading System"
pytest tests/
```

### Problem: `Port already in use` for LocalStack

```
Error: port 4566 is already allocated
```

**Fix:** Another LocalStack instance is running. Stop it:
```bash
docker compose down
docker ps  # Check for any lingering containers
docker stop $(docker ps -q)  # Stop all containers if needed
docker compose up -d localstack
```

---

## Developer Workflow

A typical development session:

```bash
# 1. Start a new feature branch
git checkout main && git pull
git checkout -b feature/TICK-123-add-mean-reversion-strategy

# 2. Start local infrastructure
docker compose up -d localstack dynamodb-local
python scripts/setup_local_tables.py

# 3. Activate virtual environment
source .venv/bin/activate

# 4. Make your changes

# 5. Run type checking
mypy services/strategy_engine/

# 6. Run linting and formatting
ruff check services/
black services/ --line-length 100

# 7. Run unit tests for the service you changed
pytest tests/unit/strategy_engine/ -v

# 8. Run integration tests
pytest tests/integration/strategy_engine/ -v

# 9. If you changed a strategy, run a backtest
python scripts/backtest/run.py --strategy your_strategy --from 2025-01-01 --to 2025-12-31

# 10. Commit and push
git add -A
git commit -m "feat: add mean reversion strategy with z-score signals"
git push origin feature/TICK-123-add-mean-reversion-strategy

# 11. Open a Pull Request on GitHub
# PRs for trading logic require TWO reviewers
```

### Code Quality Checks (must pass before merging)

```bash
# Formatter (auto-fixes)
black services/ tests/ --line-length 100

# Linter (must have zero errors)
ruff check services/ tests/

# Type checker
mypy services/ --strict

# Test coverage (must be ≥ 85% for trading logic)
pytest tests/unit/ --cov=services --cov-fail-under=85
```

---

*Last updated: 2026-04-24 | Update this document whenever: new prerequisites are needed, the setup script changes, new environment variables are required, or the development workflow changes.*
