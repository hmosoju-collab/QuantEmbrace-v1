# QuantEmbrace — Introduction

> **Who is this for?** This guide assumes you are new to this codebase. You may be a developer, data scientist, quant analyst, or even someone curious about how algorithmic trading works under the hood. We start from the very basics and build up to the full system.

---

## Table of Contents

1. [What is Algorithmic Trading?](#what-is-algorithmic-trading)
2. [What is QuantEmbrace?](#what-is-quantembrace)
3. [What Markets Does It Trade?](#what-markets-does-it-trade)
4. [The Big Picture — How a Trade Happens](#the-big-picture--how-a-trade-happens)
5. [Core Design Philosophy](#core-design-philosophy)
6. [What This System Is NOT](#what-this-system-is-not)
7. [How to Navigate the Docs](#how-to-navigate-the-docs)

---

## What is Algorithmic Trading?

In traditional trading, a human watches a screen, sees a price move, and decides to buy or sell. Algorithmic trading replaces the "human watching" part with a computer program.

The program:
1. **Reads market prices** in real time (e.g. "RELIANCE is at ₹2,450 right now")
2. **Applies a strategy** ("if the 10-day average crosses above the 50-day average, that's a buy signal")
3. **Checks risk limits** ("but don't risk more than 3% of my total capital on any single trade")
4. **Places the order automatically** ("buy 50 shares of RELIANCE at market price")
5. **Tracks the position** until it's closed, monitoring profit/loss in real time

The advantages are speed (a computer reacts in milliseconds, a human takes seconds), consistency (a computer follows rules perfectly, a human gets emotional), and scale (one system can monitor hundreds of stocks simultaneously).

---

## What is QuantEmbrace?

QuantEmbrace is a **production-grade algorithmic trading platform** that:

- Monitors live market prices from **two brokers simultaneously** (Zerodha for Indian markets, Alpaca for US markets)
- Runs **pluggable trading strategies** that generate buy/sell signals
- **Validates every signal** through a risk engine before any order is placed
- **Executes orders** with smart routing to the right broker
- **Operates on AWS** in a cost-optimized, always-on configuration
- **Provides full audit trails** — every decision is logged

Think of it as a hedge fund's trading desk, but automated and running on cloud infrastructure.

```
What QuantEmbrace does in plain English:

  "Watch prices from NSE India and US stock markets, 
   run smart strategies to find opportunities, 
   make sure every trade is safe, 
   then place the order automatically — 
   all day, every trading day."
```

---

## Do I Need to Manually Pick Stocks to Trade?

**Short answer:** You set the *universe* (watchlist), the algorithm decides *when* to trade.

This is a two-stage system:

```
Stage 1 — YOU decide the universe (one-time config edit):
  Open configs/instruments.yaml
  Set active: true for every stock you want the system to watch
  Restart the service — done

Stage 2 — ALGORITHM decides when to trade (fully automatic):
  Watches all active stocks in real time
  Runs momentum analysis on every price tick
  Generates a BUY signal only when a golden cross fires
  Generates a SELL signal only when a death cross fires
  You never manually say "buy RELIANCE today"
```

### Example

```yaml
# configs/instruments.yaml — you edit this
nse:
  instruments:
    - symbol: RELIANCE
      active: true     # ← system watches this stock
    - symbol: TCS
      active: true     # ← watches this too
    - symbol: WIPRO
      active: false    # ← ignored, not watched
```

The system continuously watches RELIANCE and TCS. If RELIANCE's 10-tick moving average crosses above its 50-tick moving average today, a BUY signal is auto-generated and flows through risk validation to execution. WIPRO gets no signal because it's inactive.

**To add a new stock:** Set `active: true` and restart. No code changes needed.
**To pause a stock:** Set `active: false`. No code changes needed.
**To change trade timing/sensitivity:** Adjust `short_window` / `long_window` in the config.

The full list of pre-configured NSE and US stocks is in [`configs/instruments.yaml`](../configs/instruments.yaml). It includes Nifty 50 blue-chips for NSE and S&P 500 / NASDAQ top names for US.

---

## What Markets Does It Trade?

### NSE India (National Stock Exchange)
- **Broker:** Zerodha Kite Connect
- **Instruments:** NSE equities (stocks like RELIANCE, TCS, INFY), F&O (Futures & Options)
- **Trading hours:** 09:15 AM – 3:30 PM IST, Monday–Friday
- **Data feed:** Zerodha Kite Ticker (WebSocket, real-time tick data)
- **Key nuance:** Intraday positions (MIS product type) are auto-squared off at ~3:15 PM by Zerodha. Our system handles this proactively.

### US Equities
- **Broker:** Alpaca Markets
- **Instruments:** NYSE, NASDAQ stocks (AAPL, MSFT, GOOGL, AMZN, NVDA, etc.)
- **Trading hours:** 9:30 AM – 4:00 PM ET (= ~7:00 PM – 1:30 AM IST next day during EDT)
- **Data feed:** Alpaca WebSocket, real-time trades and quotes
- **Key nuance:** PDT (Pattern Day Trader) rules apply for accounts under $25,000. The system respects these.

### Why Two Markets?

Diversification. NSE and US markets have very low correlation because:
- They trade at completely different times
- They're driven by different economic factors (RBI policy vs. Fed policy)
- Currencies are different (INR vs. USD)

Running strategies on both gives more opportunities and spreads risk.

---

## The Big Picture — How a Trade Happens

Here is the complete lifecycle of a single trade, from raw market data to a filled order. Every step is handled by a different part of QuantEmbrace.

```
STEP 1: MARKET DATA ARRIVES
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Zerodha sends:  RELIANCE LTP = ₹2,453.50
Alpaca sends:   AAPL bid = $182.10, ask = $182.15

     ↓ (Data Ingestion Service normalizes and stores this)

STEP 2: STRATEGY SEES THE DATA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Momentum strategy calculates:
  "10-day average = 2,420 | 50-day average = 2,400"
  "10-day > 50-day and price > 10-day → BULLISH CROSSOVER"
  
Strategy emits a Signal:
  BUY RELIANCE | qty: 20 shares | confidence: 0.75

     ↓ (Signal sent to Risk Engine — NOT directly to execution)

STEP 3: RISK ENGINE VALIDATES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Checks:
  ✓ Kill switch: OFF (system is active)
  ✓ Position limit: currently 4 open positions, max is 10 → OK
  ✓ Exposure: buying 20 × ₹2,453 = ₹49,060, within 5% portfolio limit → OK
  ✓ Daily loss: today's P&L = +₹8,200, no drawdown → OK
  ✓ Margin: ₹49,060 available in Zerodha account → OK
  
Decision: APPROVED (risk_decision_id: abc-789)

     ↓ (Approved signal sent to Execution Engine)

STEP 4: ORDER PLACED
━━━━━━━━━━━━━━━━━━━━
Execution Engine:
  1. Checks DynamoDB — not a duplicate order
  2. Routes to Zerodha (because market = NSE)
  3. Places: BUY 20 RELIANCE @ MARKET
  4. Zerodha confirms: order_id = "987654321"
  5. Saves to DynamoDB: status = PLACED

     ↓ (Zerodha fills the order a few milliseconds later)

STEP 5: FILL CONFIRMED
━━━━━━━━━━━━━━━━━━━━━━
Zerodha confirms: FILLED @ ₹2,454.00 (20 shares)
DynamoDB updated: status = FILLED, avg_price = 2454.00
Risk engine notified: update open position tracking
```

This entire sequence — from tick arriving to order placed — happens in **under 1 second**.

---

## Core Design Philosophy

These five principles explain every major architecture decision in the system:

### 1. Risk First, Always
The Risk Engine is not optional. It is not a suggestion. Every single signal — no matter how confident the strategy is — must pass through the Risk Engine before touching a broker. This is the most important rule in the entire system.

> *"The worst thing an algo trading system can do is place a bad trade very fast. Speed without risk control is dangerous."*

### 2. Separation of Concerns
Three layers that must never mix:
- **Strategy** = "should I trade?" (signal generation)
- **Risk** = "is it safe to trade?" (validation)  
- **Execution** = "how do I trade?" (broker interaction)

Importing strategy code in the execution service, or checking risk limits inside a strategy, is a critical defect.

### 3. Idempotency (Restart Safety)
If the system crashes and restarts, it must not:
- Place duplicate orders
- Miss any fills
- Lose track of open positions

Every state transition is written to DynamoDB with conditional checks. Every order has a UUID that prevents duplicates.

### 4. Fail Safe, Not Fail Open
If the Risk Engine is down → trading halts (not: trading bypasses risk).  
If a broker connection drops → positions are held (not: random orders placed to close).  
If a strategy throws an exception → that strategy stops (not: the entire system crashes).

### 5. Cost-Conscious Infrastructure
No Lambda functions for streaming (they don't support long-lived WebSocket connections, and they're expensive for always-on workloads). Services run on ECS Fargate during market hours only, scaled to zero outside trading hours.

---

## What This System Is NOT

To set correct expectations:

| This system IS | This system is NOT |
|---|---|
| An execution platform for defined strategies | A strategy research or backtesting-first tool |
| AWS-native and cloud-deployed | A local desktop trading tool |
| Python-based | A low-latency C++/FPGA HFT system |
| Designed for 1-second to minute-level signals | Designed for microsecond HFT |
| Multi-market (NSE + US) | A single-broker wrapper |
| Production-hardened with risk controls | A prototype or toy project |

---

## How to Navigate the Docs

The documentation is organized into chapters. Start with the one that matches your role:

| I am... | Start here |
|---|---|
| Completely new, want the big picture | You're in the right place. Then read [02_architecture.md](02_architecture.md) |
| A developer joining the team | [02_architecture.md](02_architecture.md) → [04_services.md](04_services.md) → [05_local_setup.md](05_local_setup.md) |
| Setting up the system for the first time | [05_local_setup.md](05_local_setup.md) |
| Working on infrastructure / AWS | [06_aws_infrastructure.md](06_aws_infrastructure.md) |
| Adding a new trading strategy | [03_signal_lifecycle.md](03_signal_lifecycle.md) → [07_contributing.md](07_contributing.md) |
| Looking for a glossary or term definition | [07_contributing.md](07_contributing.md#glossary) |

**Full docs index:** [README.md](README.md)

---

*Last updated: 2026-04-24 | Update this document whenever: new markets are added, new brokers are integrated, or the core design philosophy changes.*
