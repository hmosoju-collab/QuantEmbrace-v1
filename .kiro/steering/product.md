# QuantEmbrace — Product Overview

QuantEmbrace is a production-grade algorithmic trading platform that monitors live market prices, runs pluggable trading strategies, validates every signal through a mandatory risk engine, and executes orders automatically across two markets:

- **NSE India** via Zerodha Kite Connect (09:15–15:30 IST)
- **US Equities** via Alpaca Markets (09:30–16:00 ET)

## Core Value Proposition

Automated, risk-controlled trading across two uncorrelated markets. The system watches a configured instrument universe, generates signals from strategy logic, enforces risk limits before any order touches a broker, and maintains full audit trails of every decision.

## Non-Negotiable Design Principles

1. **Risk is mandatory.** Every signal passes through the Risk Engine. No bypass exists. If the Risk Engine is down, trading halts — this is by design.
2. **Strict layer separation.** Strategy = "should I trade?", Risk = "is it safe?", Execution = "how do I trade?". These layers never mix.
3. **Idempotent everything.** Restarts must not produce duplicate orders or lose position state.
4. **Fail safe, not fail open.** Component failures halt trading; they do not pass through unchecked orders.
5. **Kafka-only inter-service messaging.** SQS is permanently removed and banned by CI (ruff TID251). All real-time data flows through MSK Serverless Kafka topics.

## Signal Flow (Canonical)

```
Broker WebSocket → data_ingestion → [ticks.nse / ticks.us]
  → strategy_engine → [signals.pending]
  → risk_engine → [signals.approved]
  → execution_engine → Broker API → [orders.events]
  → risk_engine (P&L update)
```

## Current State

Phase 3 complete. Six strategies active in paper-trade mode (MomentumStrategy + 5 candle-based strategies). All strategies start with `paper_trade=True` in DynamoDB and require 5-day paper validation before live promotion.

## Broker Integrations

### Zerodha Kite Connect (NSE India)
- REST for orders/positions, WebSocket (Kite Ticker) for streaming quotes
- OAuth2 tokens expire daily ~07:30 IST — `scripts/zerodha_login.py` handles refresh
- Rate limits: 10 req/s orders, 3 req/s historical data
- MIS positions auto square-off at ~15:15 IST — system handles proactively
- Order types: MARKET, LIMIT, SL, SL-M; products: CNC, MIS, NRML

### Alpaca (US Equities)
- REST for orders/account, WebSocket for streaming quotes and trade updates
- API key + secret (no expiry); rate limit: 200 req/min
- Paper trading via separate endpoint — used for staging
- Supports fractional shares, extended hours, TRAILING_STOP orders
- PDT rules apply to accounts under $25k

## AWS Cost Rules

- **No Lambda** for streaming workloads (cold start + 15-min timeout incompatible)
- **No ECS Fargate** — permanently removed; use EC2 ARM64 ASGs
- S3 for all logs and historical data; DynamoDB for low-latency hot state only
- VPC endpoints for S3/DynamoDB to avoid NAT Gateway charges
- Spot instances for backtesting/batch analytics (up to 70% savings)
