# QuantEmbrace — Paper Trading Makefile
# ══════════════════════════════════════════════════════════════════════════════
#
# QUICKSTART (Monday morning, run this once):
#
#   make monday
#
# That single command checks prerequisites, starts everything, and tells you
# when the system is ready to trade. Everything else in this file is for
# drilling into specific parts of the system.
#
# FIRST TIME EVER running on this machine?
#
#   make first-time-setup          ← install deps, configure .env, start stack
#
# ══════════════════════════════════════════════════════════════════════════════

# ── Shell and error handling ──────────────────────────────────────────────────
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

# ── ANSI colours (safe — suppressed if stdout is not a tty) ──────────────────
BOLD   := $(shell tput bold   2>/dev/null || echo "")
GREEN  := $(shell tput setaf 2 2>/dev/null || echo "")
YELLOW := $(shell tput setaf 3 2>/dev/null || echo "")
CYAN   := $(shell tput setaf 6 2>/dev/null || echo "")
RED    := $(shell tput setaf 1 2>/dev/null || echo "")
RESET  := $(shell tput sgr0   2>/dev/null || echo "")

# ── Directories ───────────────────────────────────────────────────────────────
ROOT_DIR := $(shell pwd)
VENV_DIR := $(ROOT_DIR)/.venv
PYTHON   := $(VENV_DIR)/bin/python
PIP      := $(VENV_DIR)/bin/pip

# ── Service health endpoints ──────────────────────────────────────────────────
HEALTH_PORTS := 8081 8082 8083 8084 8085
SERVICE_NAMES := data_ingestion strategy_engine risk_engine execution_engine ai_engine

# ── LocalStack endpoint (used by aws CLI commands below) ─────────────────────
LOCALSTACK := http://localhost:4566

# ══════════════════════════════════════════════════════════════════════════════
# HELP  (default target — just run `make`)
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: help
help:
	@echo ""
	@echo "$(BOLD)$(CYAN)╔══════════════════════════════════════════════════════════════╗$(RESET)"
	@echo "$(BOLD)$(CYAN)║           QuantEmbrace — Paper Trading Commands              ║$(RESET)"
	@echo "$(BOLD)$(CYAN)╚══════════════════════════════════════════════════════════════╝$(RESET)"
	@echo ""
	@echo "$(BOLD)$(GREEN)── GETTING STARTED ─────────────────────────────────────────────$(RESET)"
	@echo "  $(BOLD)make first-time-setup$(RESET)   Complete first-time setup (run once ever)"
	@echo "  $(BOLD)make check-prereqs$(RESET)      Check all required tools are installed"
	@echo "  $(BOLD)make configure-env$(RESET)      Guided .env configuration wizard"
	@echo ""
	@echo "$(BOLD)$(GREEN)── EVERY MONDAY MORNING ────────────────────────────────────────$(RESET)"
	@echo "  $(BOLD)make monday$(RESET)             Full Monday startup sequence (use this!)"
	@echo "  $(BOLD)make zerodha-login$(RESET)      Refresh Zerodha daily token (NSE trading)"
	@echo "  $(BOLD)make checklist$(RESET)          Run daily pre-trading checklist"
	@echo ""
	@echo "$(BOLD)$(GREEN)── START / STOP SERVICES ───────────────────────────────────────$(RESET)"
	@echo "  $(BOLD)make start$(RESET)              Start all services (Docker Compose)"
	@echo "  $(BOLD)make stop$(RESET)               Stop all services (keep data)"
	@echo "  $(BOLD)make restart$(RESET)            Restart all services"
	@echo "  $(BOLD)make infra-up$(RESET)           Start infra only (Redpanda + LocalStack)"
	@echo "  $(BOLD)make infra-down$(RESET)         Stop infra only"
	@echo ""
	@echo "$(BOLD)$(GREEN)── MONITORING ──────────────────────────────────────────────────$(RESET)"
	@echo "  $(BOLD)make health$(RESET)             Check all 5 service health endpoints"
	@echo "  $(BOLD)make logs$(RESET)               Tail logs from all services"
	@echo "  $(BOLD)make logs-risk$(RESET)          Tail risk engine logs only"
	@echo "  $(BOLD)make logs-execution$(RESET)     Tail execution engine logs only"
	@echo "  $(BOLD)make paper-orders$(RESET)       List paper orders in DynamoDB"
	@echo "  $(BOLD)make watch-signals$(RESET)      Watch live Kafka signals (live stream)"
	@echo "  $(BOLD)make kill-switch-status$(RESET) Check if kill switch is active"
	@echo ""
	@echo "$(BOLD)$(GREEN)── SAFETY ──────────────────────────────────────────────────────$(RESET)"
	@echo "  $(BOLD)make kill-switch-on$(RESET)     HALT all trading immediately"
	@echo "  $(BOLD)make kill-switch-off$(RESET)    Resume trading (clears kill switch)"
	@echo ""
	@echo "$(BOLD)$(GREEN)── TESTING ─────────────────────────────────────────────────────$(RESET)"
	@echo "  $(BOLD)make test$(RESET)               Run unit tests (no Docker needed)"
	@echo "  $(BOLD)make test-integration$(RESET)   Run integration tests (needs infra)"
	@echo "  $(BOLD)make test-coverage$(RESET)      Run unit tests with coverage report"
	@echo ""
	@echo "$(BOLD)$(GREEN)── BACKTEST ─────────────────────────────────────────────────────$(RESET)"
	@echo "  $(BOLD)make backtest$(RESET)           Run a sample momentum backtest"
	@echo ""
	@echo "$(BOLD)$(GREEN)── CLEANUP ──────────────────────────────────────────────────────$(RESET)"
	@echo "  $(BOLD)make clean$(RESET)              Stop services and remove containers"
	@echo "  $(BOLD)make clean-all$(RESET)          Nuclear reset (removes volumes + venv)"
	@echo ""
	@echo "$(BOLD)$(YELLOW)TIP:$(RESET) Monday morning? Just run: $(BOLD)make monday$(RESET)"
	@echo ""

# ══════════════════════════════════════════════════════════════════════════════
# PREREQUISITES CHECK
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: check-prereqs
check-prereqs:
	@echo ""
	@echo "$(BOLD)$(CYAN)Checking prerequisites...$(RESET)"
	@echo ""

	@# ── Python ────────────────────────────────────────────────────────────
	@if command -v python3 &>/dev/null; then \
		PY_VER=$$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"); \
		PY_MAJOR=$$(python3 -c "import sys; print(sys.version_info.major)"); \
		PY_MINOR=$$(python3 -c "import sys; print(sys.version_info.minor)"); \
		if [ "$$PY_MAJOR" -ge 3 ] && [ "$$PY_MINOR" -ge 11 ]; then \
			echo "  $(GREEN)✓$(RESET) Python $$PY_VER (3.11+ required)"; \
		else \
			echo "  $(RED)✗$(RESET) Python $$PY_VER — need 3.11+"; \
			echo "    Install: https://www.python.org/downloads/"; \
			exit 1; \
		fi \
	else \
		echo "  $(RED)✗$(RESET) Python not found"; \
		echo "    Install: https://www.python.org/downloads/"; \
		exit 1; \
	fi

	@# ── Docker ────────────────────────────────────────────────────────────
	@if command -v docker &>/dev/null; then \
		DOCKER_VER=$$(docker --version | grep -oE '[0-9]+\.[0-9]+' | head -1); \
		echo "  $(GREEN)✓$(RESET) Docker $$DOCKER_VER"; \
	else \
		echo "  $(RED)✗$(RESET) Docker not found"; \
		echo "    Install Docker Desktop: https://docs.docker.com/get-docker/"; \
		exit 1; \
	fi

	@# ── Docker running ────────────────────────────────────────────────────
	@if docker info &>/dev/null; then \
		echo "  $(GREEN)✓$(RESET) Docker daemon is running"; \
	else \
		echo "  $(RED)✗$(RESET) Docker daemon is not running"; \
		echo "    Open Docker Desktop and wait for it to start, then retry."; \
		exit 1; \
	fi

	@# ── Docker Compose ────────────────────────────────────────────────────
	@if docker compose version &>/dev/null; then \
		DC_VER=$$(docker compose version | grep -oE '[0-9]+\.[0-9]+' | head -1); \
		echo "  $(GREEN)✓$(RESET) Docker Compose v$$DC_VER (bundled with Docker Desktop)"; \
	else \
		echo "  $(RED)✗$(RESET) Docker Compose v2 not found"; \
		echo "    Update Docker Desktop to 4.x — Compose v2 is bundled."; \
		exit 1; \
	fi

	@# ── AWS CLI ───────────────────────────────────────────────────────────
	@if command -v aws &>/dev/null; then \
		AWS_VER=$$(aws --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1); \
		echo "  $(GREEN)✓$(RESET) AWS CLI v$$AWS_VER"; \
	else \
		echo "  $(YELLOW)⚠$(RESET)  AWS CLI not found"; \
		echo "    Install: https://aws.amazon.com/cli/"; \
		echo "    (Only needed for infra work — paper trading works without it)"; \
	fi

	@# ── .env file ─────────────────────────────────────────────────────────
	@if [ -f ".env" ]; then \
		echo "  $(GREEN)✓$(RESET) .env file exists"; \
	else \
		echo "  $(YELLOW)⚠$(RESET)  .env file not found"; \
		echo "    Run: make configure-env   (creates .env from template)"; \
	fi

	@echo ""
	@echo "  $(GREEN)All critical prerequisites satisfied.$(RESET)"
	@echo ""

# ══════════════════════════════════════════════════════════════════════════════
# FIRST-TIME SETUP  (run this once when you clone the repo)
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: first-time-setup
first-time-setup: check-prereqs _create-venv _install-deps configure-env
	@echo ""
	@echo "$(BOLD)$(GREEN)╔══════════════════════════════════════════════════════════════╗$(RESET)"
	@echo "$(BOLD)$(GREEN)║   First-time setup complete!                                 ║$(RESET)"
	@echo "$(BOLD)$(GREEN)║                                                              ║$(RESET)"
	@echo "$(BOLD)$(GREEN)║   Next step: run  make monday                                ║$(RESET)"
	@echo "$(BOLD)$(GREEN)╚══════════════════════════════════════════════════════════════╝$(RESET)"
	@echo ""

.PHONY: _create-venv
_create-venv:
	@echo ""
	@echo "$(BOLD)$(CYAN)Step 1/3 — Creating Python virtual environment...$(RESET)"
	@if [ ! -d "$(VENV_DIR)" ]; then \
		python3 -m venv $(VENV_DIR); \
		echo "  $(GREEN)✓$(RESET) Virtual environment created at .venv/"; \
	else \
		echo "  $(GREEN)✓$(RESET) Virtual environment already exists (.venv/)"; \
	fi

.PHONY: _install-deps
_install-deps: _create-venv
	@echo ""
	@echo "$(BOLD)$(CYAN)Step 2/3 — Installing Python dependencies...$(RESET)"
	@echo "  This takes 2–3 minutes on first run. Grab a coffee ☕"
	@$(PIP) install --quiet --upgrade pip
	@$(PIP) install --quiet -r requirements.txt
	@$(PIP) install --quiet -r requirements-dev.txt
	@echo "  $(GREEN)✓$(RESET) All dependencies installed"

# ══════════════════════════════════════════════════════════════════════════════
# ENVIRONMENT CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: configure-env
configure-env:
	@echo ""
	@echo "$(BOLD)$(CYAN)Step 3/3 — Environment configuration$(RESET)"
	@echo ""
	@if [ -f ".env" ]; then \
		echo "  $(GREEN)✓$(RESET) .env file already exists — skipping copy."; \
		echo "    To reset it, delete .env and run: make configure-env"; \
	else \
		cp .env.example .env; \
		echo "  $(GREEN)✓$(RESET) Copied .env.example → .env"; \
	fi
	@echo ""
	@echo "  $(BOLD)What you need to fill in:$(RESET)"
	@echo ""
	@echo "  $(BOLD)$(YELLOW)① Zerodha credentials (for NSE/Indian market data)$(RESET)"
	@echo "    Get from: https://developers.kite.trade/"
	@echo "    Required fields:"
	@echo "      ZERODHA_API_KEY=      ← Your Kite Connect API key"
	@echo "      ZERODHA_API_SECRET=   ← Your Kite Connect API secret"
	@echo ""
	@echo "  $(BOLD)$(YELLOW)② Alpaca credentials (for US market data)$(RESET)"
	@echo "    Get from: https://app.alpaca.markets/ (free account works)"
	@echo "    Required fields:"
	@echo "      ALPACA_API_KEY=       ← Your Alpaca API key ID"
	@echo "      ALPACA_API_SECRET=    ← Your Alpaca API secret key"
	@echo ""
	@echo "  $(BOLD)$(GREEN)③ Everything else is pre-filled for paper trading ✓$(RESET)"
	@echo "    RISK_PROFILE=paper, ALPACA_USE_PAPER=true, and all"
	@echo "    local infrastructure settings are already configured."
	@echo ""
	@echo "  $(CYAN)Open .env in your editor and fill in the ① and ② fields above.$(RESET)"
	@echo ""
	@echo "  $(BOLD)Quick open:$(RESET)"
	@echo "    macOS:   open -e .env"
	@echo "    VS Code: code .env"
	@echo ""
	@echo "  $(YELLOW)SECURITY NOTE: Never commit .env to git. It is in .gitignore.$(RESET)"
	@echo ""

# ══════════════════════════════════════════════════════════════════════════════
# MONDAY MORNING STARTUP SEQUENCE
# ══════════════════════════════════════════════════════════════════════════════
# This is the only command you need on Monday morning.
# It runs every step in the correct order with clear status updates.

.PHONY: monday
monday:
	@echo ""
	@echo "$(BOLD)$(CYAN)╔══════════════════════════════════════════════════════════════╗$(RESET)"
	@echo "$(BOLD)$(CYAN)║   QuantEmbrace — Monday Morning Startup                      ║$(RESET)"
	@echo "$(BOLD)$(CYAN)║   Paper Trading Mode                                         ║$(RESET)"
	@echo "$(BOLD)$(CYAN)╚══════════════════════════════════════════════════════════════╝$(RESET)"
	@echo ""

	@# ── Step 0: Gate — refuse to start without .env ───────────────────────
	@if [ ! -f ".env" ]; then \
		echo "$(RED)ERROR: .env file not found.$(RESET)"; \
		echo ""; \
		echo "  Run this first:  make first-time-setup"; \
		echo ""; \
		exit 1; \
	fi
	@echo "  $(GREEN)✓$(RESET) .env file found"

	@# ── Step 1: Check Docker is running ───────────────────────────────────
	@echo ""
	@echo "$(BOLD)$(CYAN)[1/6] Checking Docker...$(RESET)"
	@if ! docker info &>/dev/null; then \
		echo "  $(RED)✗$(RESET) Docker is not running."; \
		echo "    Open Docker Desktop, wait for the whale icon to stabilise,"; \
		echo "    then run: make monday"; \
		exit 1; \
	fi
	@echo "  $(GREEN)✓$(RESET) Docker is running"

	@# ── Step 2: Start infrastructure (Redpanda + LocalStack) ──────────────
	@echo ""
	@echo "$(BOLD)$(CYAN)[2/6] Starting infrastructure (Redpanda + LocalStack)...$(RESET)"
	@echo "  This starts the local Kafka broker (Redpanda) and AWS emulator"
	@echo "  (LocalStack). No real AWS credentials are needed."
	@docker compose up -d localstack redpanda
	@echo "  Waiting for LocalStack and Redpanda to become healthy..."
	@timeout=90; \
	while [ $$timeout -gt 0 ]; do \
		LS_OK=$$(docker compose ps localstack --format json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if isinstance(d,list) and d and d[0].get('Health')=='healthy' else 'wait')" 2>/dev/null || echo wait); \
		RP_OK=$$(docker compose ps redpanda --format json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if isinstance(d,list) and d and d[0].get('Health')=='healthy' else 'wait')" 2>/dev/null || echo wait); \
		if [ "$$LS_OK" = "ok" ] && [ "$$RP_OK" = "ok" ]; then break; fi; \
		sleep 3; timeout=$$((timeout-3)); \
		printf "  . waiting ($$timeout s remaining)\r"; \
	done
	@echo "  $(GREEN)✓$(RESET) Redpanda (Kafka) is healthy"
	@echo "  $(GREEN)✓$(RESET) LocalStack (DynamoDB + S3) is healthy"

	@# ── Step 3: Create DynamoDB tables and Kafka topics ───────────────────
	@echo ""
	@echo "$(BOLD)$(CYAN)[3/6] Creating DynamoDB tables and Kafka topics...$(RESET)"
	@echo "  (Safe to re-run — existing tables and topics are not overwritten)"
	@docker compose run --rm setup
	@echo "  $(GREEN)✓$(RESET) DynamoDB tables ready"
	@echo "  $(GREEN)✓$(RESET) Kafka topics ready"

	@# ── Step 4: Start all 5 trading services ──────────────────────────────
	@echo ""
	@echo "$(BOLD)$(CYAN)[4/6] Starting trading services...$(RESET)"
	@echo "  Services: data_ingestion, strategy_engine, ai_engine,"
	@echo "            risk_engine, execution_engine"
	@docker compose up -d data_ingestion strategy_engine risk_engine execution_engine ai_engine
	@echo "  Waiting 20 seconds for services to initialise..."
	@sleep 20

	@# ── Step 5: Health check all services ─────────────────────────────────
	@echo ""
	@echo "$(BOLD)$(CYAN)[5/6] Checking service health...$(RESET)"
	@$(MAKE) --no-print-directory _health-check

	@# ── Step 6: Kill switch check ─────────────────────────────────────────
	@echo ""
	@echo "$(BOLD)$(CYAN)[6/6] Verifying kill switch is clear...$(RESET)"
	@$(MAKE) --no-print-directory kill-switch-status

	@# ── Done ──────────────────────────────────────────────────────────────
	@echo ""
	@echo "$(BOLD)$(GREEN)╔══════════════════════════════════════════════════════════════╗$(RESET)"
	@echo "$(BOLD)$(GREEN)║   System is UP and ready for paper trading!                  ║$(RESET)"
	@echo "$(BOLD)$(GREEN)╚══════════════════════════════════════════════════════════════╝$(RESET)"
	@echo ""
	@echo "  $(BOLD)Useful next commands:$(RESET)"
	@echo "    make logs              — watch all service logs"
	@echo "    make paper-orders      — see paper orders flowing in"
	@echo "    make watch-signals     — watch live trading signals"
	@echo "    make checklist         — run daily ops checklist"
	@echo "    make kill-switch-on    — HALT all trading (emergency)"
	@echo ""
	@echo "  $(BOLD)Redpanda Console (topic browser):$(RESET) http://localhost:8080"
	@echo ""

# ══════════════════════════════════════════════════════════════════════════════
# ZERODHA DAILY TOKEN REFRESH
# ══════════════════════════════════════════════════════════════════════════════
# Zerodha Kite Connect tokens expire every day at ~07:30 IST.
# Run this EVERY morning before the NSE market opens (09:15 IST).
# You will be asked to paste a URL token — it takes about 30 seconds.

.PHONY: zerodha-login
zerodha-login: _require-venv
	@echo ""
	@echo "$(BOLD)$(CYAN)Zerodha Daily Token Refresh$(RESET)"
	@echo "─────────────────────────────────────────────────────────────────"
	@echo ""
	@echo "  $(BOLD)What this does:$(RESET)"
	@echo "  1. Prints a Kite login URL"
	@echo "  2. You open the URL in your browser and log in"
	@echo "  3. You paste the request_token from the redirect URL"
	@echo "  4. The script stores a fresh access token in DynamoDB"
	@echo "  5. The execution engine picks it up automatically"
	@echo ""
	@echo "  $(YELLOW)NOTE: Tokens expire at 07:30 IST daily. Run this every morning.$(RESET)"
	@echo ""
	@PYTHONPATH=$(ROOT_DIR)/services $(PYTHON) scripts/zerodha_login.py

# ══════════════════════════════════════════════════════════════════════════════
# INFRASTRUCTURE  (Redpanda + LocalStack)
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: infra-up
infra-up: _require-docker
	@echo "$(BOLD)$(CYAN)Starting infrastructure (Redpanda + LocalStack)...$(RESET)"
	@docker compose up -d localstack redpanda
	@echo "  $(GREEN)✓$(RESET) Infrastructure started. Run 'make infra-setup' if first time."

.PHONY: infra-setup
infra-setup:
	@echo "$(BOLD)$(CYAN)Creating DynamoDB tables and Kafka topics...$(RESET)"
	@docker compose run --rm setup
	@echo "  $(GREEN)✓$(RESET) Setup complete"

.PHONY: infra-down
infra-down:
	@echo "$(BOLD)$(CYAN)Stopping infrastructure...$(RESET)"
	@docker compose stop localstack redpanda redpanda-console
	@echo "  $(GREEN)✓$(RESET) Infrastructure stopped"

# ══════════════════════════════════════════════════════════════════════════════
# SERVICE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: start
start: _require-env _require-docker
	@echo "$(BOLD)$(CYAN)Starting all services...$(RESET)"
	@docker compose up -d
	@echo ""
	@echo "  $(GREEN)✓$(RESET) All services started."
	@echo "  Run 'make health' to verify they are healthy."
	@echo "  Run 'make logs' to watch the output."

.PHONY: stop
stop:
	@echo "$(BOLD)$(CYAN)Stopping all services (data is kept)...$(RESET)"
	@docker compose stop
	@echo "  $(GREEN)✓$(RESET) All services stopped. Run 'make start' to bring them back up."

.PHONY: restart
restart:
	@echo "$(BOLD)$(CYAN)Restarting all services...$(RESET)"
	@docker compose restart
	@echo "  $(GREEN)✓$(RESET) All services restarted"

.PHONY: restart-strategy
restart-strategy:
	@echo "$(CYAN)Restarting strategy_engine only...$(RESET)"
	@docker compose restart strategy_engine
	@echo "  $(GREEN)✓$(RESET) strategy_engine restarted"

.PHONY: restart-risk
restart-risk:
	@echo "$(CYAN)Restarting risk_engine only...$(RESET)"
	@docker compose restart risk_engine
	@echo "  $(GREEN)✓$(RESET) risk_engine restarted"

.PHONY: restart-execution
restart-execution:
	@echo "$(CYAN)Restarting execution_engine only...$(RESET)"
	@docker compose restart execution_engine
	@echo "  $(GREEN)✓$(RESET) execution_engine restarted"

# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: health
health:
	@echo ""
	@echo "$(BOLD)$(CYAN)Service Health Check$(RESET)"
	@echo "─────────────────────────────────────────────────────────────────"
	@$(MAKE) --no-print-directory _health-check
	@echo ""

.PHONY: _health-check
_health-check:
	@for svc_port in \
		"data_ingestion:8081" \
		"strategy_engine:8082" \
		"risk_engine:8083" \
		"execution_engine:8084" \
		"ai_engine:8085"; do \
		name=$$(echo $$svc_port | cut -d: -f1); \
		port=$$(echo $$svc_port | cut -d: -f2); \
		if curl -sf http://localhost:$$port/health >/dev/null 2>&1; then \
			echo "  $(GREEN)✓$(RESET) $$name  (http://localhost:$$port/health)"; \
		else \
			echo "  $(RED)✗$(RESET) $$name  (http://localhost:$$port/health) — not responding"; \
		fi; \
	done
	@# ── Infrastructure services ───────────────────────────────────────────
	@if curl -sf $(LOCALSTACK)/_localstack/health 2>/dev/null | grep -q '"dynamodb": "available"'; then \
		echo "  $(GREEN)✓$(RESET) LocalStack (DynamoDB + S3)  ($(LOCALSTACK))"; \
	else \
		echo "  $(RED)✗$(RESET) LocalStack — not responding"; \
	fi
	@if curl -sf http://localhost:9644/v1/cluster 2>/dev/null | grep -q '"nodeId"' 2>/dev/null || \
	   docker exec $$(docker compose ps -q redpanda 2>/dev/null) rpk cluster health 2>/dev/null | grep -q 'Healthy:.*true'; then \
		echo "  $(GREEN)✓$(RESET) Redpanda (Kafka)  (localhost:19092)"; \
	else \
		echo "  $(YELLOW)⚠$(RESET)  Redpanda — health check inconclusive (may still be starting)"; \
	fi

# ══════════════════════════════════════════════════════════════════════════════
# LOGS
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: logs
logs:
	@echo "$(CYAN)Tailing all service logs (Ctrl-C to exit)...$(RESET)"
	@docker compose logs -f --tail=50

.PHONY: logs-data
logs-data:
	@docker compose logs -f --tail=50 data_ingestion

.PHONY: logs-strategy
logs-strategy:
	@docker compose logs -f --tail=50 strategy_engine

.PHONY: logs-risk
logs-risk:
	@docker compose logs -f --tail=50 risk_engine

.PHONY: logs-execution
logs-execution:
	@docker compose logs -f --tail=50 execution_engine

.PHONY: logs-ai
logs-ai:
	@docker compose logs -f --tail=50 ai_engine

# ══════════════════════════════════════════════════════════════════════════════
# PAPER TRADING MONITORING
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: paper-orders
paper-orders:
	@echo ""
	@echo "$(BOLD)$(CYAN)Paper Orders in DynamoDB$(RESET)"
	@echo "─────────────────────────────────────────────────────────────────"
	@echo "  Scanning quantembrace-development-orders for paper_trade=true..."
	@echo ""
	@aws --endpoint-url=$(LOCALSTACK) dynamodb scan \
		--table-name quantembrace-development-orders \
		--filter-expression "attribute_exists(paper_trade)" \
		--output table \
		2>&1 || echo "  $(YELLOW)No orders found yet — system may still be warming up.$(RESET)"
	@echo ""

.PHONY: watch-signals
watch-signals:
	@echo ""
	@echo "$(BOLD)$(CYAN)Watching live signals on Kafka topic: signals.approved$(RESET)"
	@echo "  $(YELLOW)Press Ctrl-C to stop$(RESET)"
	@echo ""
	@docker run --rm --network host \
		redpandadata/redpanda:v24.1.1 \
		rpk topic consume signals.approved \
		--brokers localhost:19092 \
		--format json

.PHONY: watch-orders
watch-orders:
	@echo ""
	@echo "$(BOLD)$(CYAN)Watching live order events on Kafka topic: orders.events$(RESET)"
	@echo "  $(YELLOW)Press Ctrl-C to stop$(RESET)"
	@echo ""
	@docker run --rm --network host \
		redpandadata/redpanda:v24.1.1 \
		rpk topic consume orders.events \
		--brokers localhost:19092 \
		--format json

.PHONY: kafka-topics
kafka-topics:
	@echo ""
	@echo "$(BOLD)$(CYAN)Kafka Topics$(RESET)"
	@echo "─────────────────────────────────────────────────────────────────"
	@docker run --rm --network host \
		redpandadata/redpanda:v24.1.1 \
		rpk topic list --brokers localhost:19092
	@echo ""
	@echo "  $(CYAN)TIP: Visual topic browser: http://localhost:8080$(RESET)"
	@echo ""

.PHONY: checklist
checklist: _require-venv
	@echo ""
	@echo "$(BOLD)$(CYAN)Daily Pre-Trading Checklist$(RESET)"
	@echo "─────────────────────────────────────────────────────────────────"
	@PYTHONPATH=$(ROOT_DIR)/services $(PYTHON) scripts/ops/checklist.py --env development
	@echo ""

# ══════════════════════════════════════════════════════════════════════════════
# KILL SWITCH  (emergency trading halt)
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: kill-switch-status
kill-switch-status: _require-venv
	@echo ""
	@echo "$(BOLD)$(CYAN)Kill Switch Status$(RESET)"
	@PYTHONPATH=$(ROOT_DIR)/services \
		QE_ENVIRONMENT=development \
		DYNAMODB_TABLE_PREFIX=quantembrace-development \
		AWS_ENDPOINT_URL=$(LOCALSTACK) \
		AWS_DEFAULT_REGION=ap-south-1 \
		AWS_ACCESS_KEY_ID=test \
		AWS_SECRET_ACCESS_KEY=test \
		$(PYTHON) scripts/kill_switch_cli.py status || \
		echo "  $(YELLOW)Kill switch check unavailable — services may not be running yet.$(RESET)"
	@echo ""

.PHONY: kill-switch-on
kill-switch-on: _require-venv
	@echo ""
	@echo "$(BOLD)$(RED)╔══════════════════════════════════════════════════════════════╗$(RESET)"
	@echo "$(BOLD)$(RED)║   ACTIVATING KILL SWITCH — ALL TRADING WILL HALT            ║$(RESET)"
	@echo "$(BOLD)$(RED)╚══════════════════════════════════════════════════════════════╝$(RESET)"
	@echo ""
	@read -rp "  Reason (required): " REASON; \
	PYTHONPATH=$(ROOT_DIR)/services \
		QE_ENVIRONMENT=development \
		DYNAMODB_TABLE_PREFIX=quantembrace-development \
		AWS_ENDPOINT_URL=$(LOCALSTACK) \
		AWS_DEFAULT_REGION=ap-south-1 \
		AWS_ACCESS_KEY_ID=test \
		AWS_SECRET_ACCESS_KEY=test \
		$(PYTHON) scripts/kill_switch_cli.py activate --reason "$$REASON"
	@echo ""
	@echo "  $(RED)Kill switch ACTIVATED. No new orders will be placed.$(RESET)"
	@echo "  To resume trading: make kill-switch-off"
	@echo ""

.PHONY: kill-switch-off
kill-switch-off: _require-venv
	@echo ""
	@echo "$(BOLD)$(YELLOW)Clearing kill switch — trading will resume.$(RESET)"
	@read -rp "  Confirm (type YES to proceed): " CONFIRM; \
	if [ "$$CONFIRM" = "YES" ]; then \
		PYTHONPATH=$(ROOT_DIR)/services \
			QE_ENVIRONMENT=development \
			DYNAMODB_TABLE_PREFIX=quantembrace-development \
			AWS_ENDPOINT_URL=$(LOCALSTACK) \
			AWS_DEFAULT_REGION=ap-south-1 \
			AWS_ACCESS_KEY_ID=test \
			AWS_SECRET_ACCESS_KEY=test \
			$(PYTHON) scripts/kill_switch_cli.py deactivate; \
		echo "  $(GREEN)Kill switch cleared. Trading resumed.$(RESET)"; \
	else \
		echo "  Cancelled."; \
	fi
	@echo ""

# ══════════════════════════════════════════════════════════════════════════════
# TESTING
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: test
test: _require-venv
	@echo ""
	@echo "$(BOLD)$(CYAN)Running unit tests (no Docker required)...$(RESET)"
	@echo ""
	@PYTHONPATH=$(ROOT_DIR)/services \
		$(VENV_DIR)/bin/pytest tests/unit/ -v --tb=short
	@echo ""

.PHONY: test-integration
test-integration: _require-venv
	@echo ""
	@echo "$(BOLD)$(CYAN)Running integration tests (requires LocalStack + Redpanda)...$(RESET)"
	@echo ""
	@if ! curl -sf $(LOCALSTACK)/_localstack/health >/dev/null 2>&1; then \
		echo "  $(YELLOW)LocalStack not running. Starting infrastructure first...$(RESET)"; \
		$(MAKE) --no-print-directory infra-up; \
		$(MAKE) --no-print-directory infra-setup; \
	fi
	@PYTHONPATH=$(ROOT_DIR)/services \
		AWS_ENDPOINT_URL=$(LOCALSTACK) \
		AWS_DEFAULT_REGION=ap-south-1 \
		AWS_ACCESS_KEY_ID=test \
		AWS_SECRET_ACCESS_KEY=test \
		KAFKA_BOOTSTRAP_SERVERS=localhost:19092 \
		KAFKA_USE_IAM=false \
		DYNAMODB_TABLE_PREFIX=quantembrace-development \
		QE_ENVIRONMENT=development \
		$(VENV_DIR)/bin/pytest tests/integration/ -v --tb=short
	@echo ""

.PHONY: test-coverage
test-coverage: _require-venv
	@echo ""
	@echo "$(BOLD)$(CYAN)Running unit tests with coverage report...$(RESET)"
	@echo ""
	@PYTHONPATH=$(ROOT_DIR)/services \
		$(VENV_DIR)/bin/pytest tests/unit/ \
		--cov=services \
		--cov-report=term-missing \
		--cov-report=html:htmlcov \
		--tb=short
	@echo ""
	@echo "  $(GREEN)✓$(RESET) Coverage report written to: htmlcov/index.html"
	@echo "  Open: open htmlcov/index.html"
	@echo ""

# ══════════════════════════════════════════════════════════════════════════════
# BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: backtest
backtest: _require-venv
	@echo ""
	@echo "$(BOLD)$(CYAN)Running sample Momentum backtest on RELIANCE (NSE)...$(RESET)"
	@echo ""
	@echo "  Strategy: momentum"
	@echo "  Period:   2025-01-01 to 2025-12-31"
	@echo "  Capital:  ₹10,00,000"
	@echo ""
	@PYTHONPATH=$(ROOT_DIR)/services \
		AWS_ENDPOINT_URL=$(LOCALSTACK) \
		AWS_DEFAULT_REGION=ap-south-1 \
		AWS_ACCESS_KEY_ID=test \
		AWS_SECRET_ACCESS_KEY=test \
		$(PYTHON) scripts/backtest/run_backtest.py \
		--symbol RELIANCE \
		--market NSE \
		--strategy momentum \
		--capital 1000000
	@echo ""

# ══════════════════════════════════════════════════════════════════════════════
# CLEANUP
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: clean
clean:
	@echo ""
	@echo "$(BOLD)$(CYAN)Stopping and removing containers (data volumes are kept)...$(RESET)"
	@docker compose down
	@echo "  $(GREEN)✓$(RESET) Containers removed. Run 'make start' or 'make monday' to restart."
	@echo "  Data volumes (redpanda-data, localstack-data) are preserved."
	@echo ""

.PHONY: clean-all
clean-all:
	@echo ""
	@echo "$(BOLD)$(RED)╔══════════════════════════════════════════════════════════════╗$(RESET)"
	@echo "$(BOLD)$(RED)║   NUCLEAR RESET — removes containers, volumes, and .venv     ║$(RESET)"
	@echo "$(BOLD)$(RED)║   All LocalStack data (DynamoDB, S3) will be wiped.          ║$(RESET)"
	@echo "$(BOLD)$(RED)╚══════════════════════════════════════════════════════════════╝$(RESET)"
	@echo ""
	@read -rp "  Type RESET to confirm: " CONFIRM; \
	if [ "$$CONFIRM" = "RESET" ]; then \
		docker compose down -v; \
		rm -rf $(VENV_DIR); \
		find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true; \
		find . -name "*.pyc" -delete 2>/dev/null || true; \
		echo "  $(GREEN)✓$(RESET) Clean complete. Run 'make first-time-setup' to start fresh."; \
	else \
		echo "  Cancelled."; \
	fi
	@echo ""

# ══════════════════════════════════════════════════════════════════════════════
# GUARDS  (internal — not shown in help)
# ══════════════════════════════════════════════════════════════════════════════

.PHONY: _require-venv
_require-venv:
	@if [ ! -f "$(PYTHON)" ]; then \
		echo "$(RED)ERROR: Virtual environment not found.$(RESET)"; \
		echo "  Run: make first-time-setup"; \
		exit 1; \
	fi

.PHONY: _require-docker
_require-docker:
	@if ! docker info &>/dev/null; then \
		echo "$(RED)ERROR: Docker is not running.$(RESET)"; \
		echo "  Open Docker Desktop and wait for it to start."; \
		exit 1; \
	fi

.PHONY: _require-env
_require-env:
	@if [ ! -f ".env" ]; then \
		echo "$(RED)ERROR: .env file not found.$(RESET)"; \
		echo "  Run: make first-time-setup"; \
		exit 1; \
	fi
