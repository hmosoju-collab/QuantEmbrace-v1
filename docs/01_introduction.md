# QuantEmbrace — Introduction

> **Who is this for?** Anyone reading about QuantEmbrace for the first time. You may be a developer, a data scientist, a quant analyst, or someone who has never traded before and is simply curious how algorithmic trading works. We start from absolute basics and build up to the full system.

---

## Table of Contents

1. [What is Algorithmic Trading?](#what-is-algorithmic-trading)
2. [What is QuantEmbrace?](#what-is-quantembrace)
3. [What Markets Does It Trade?](#what-markets-does-it-trade)
4. [Do I Need to Manually Pick Stocks?](#do-i-need-to-manually-pick-stocks)
5. [The Big Picture — How a Trade Happens](#the-big-picture--how-a-trade-happens)
6. [Core Design Philosophy](#core-design-philosophy)
7. [What This System Is NOT](#what-this-system-is-not)
8. [How to Navigate the Docs](#how-to-navigate-the-docs)

---

## What is Algorithmic Trading?

In traditional trading, a human watches a screen, sees a price move, and decides to buy or sell. **Algorithmic trading** replaces the "human watching" part with a computer program.

The computer:
1. **Reads market prices** in real time (e.g., "RELIANCE is at ₹2,450 right now")
2. **Applies a strategy** (e.g., "if the short moving average crosses above the long moving average, that's a buy signal")
3. **Checks risk limits** ("don't risk more than 3% of total capital on any one trade")
4. **Places the order automatically** ("buy 20 shares of RELIANCE at market price")
5. **Tracks the position** until it's closed, monitoring profit and loss in real time

The advantages are:
- **Speed** — a computer reacts in milliseconds; a human takes seconds
- **Consistency** — a computer follows rules exactly every time; a human gets emotional
- **Scale** — one system can monitor hundreds of stocks simultaneously

---

## What is QuantEmbrace?

QuantEmbrace is a **production-grade algorithmic trading platform** that:

- Monitors live market prices from **two brokers simultaneously** — Zerodha for Indian markets, Alpaca for US markets
- Runs **6 pluggable trading strategies** that generate buy/sell signals
- **Enriches every signal** with machine-learning predictions before risk evaluation
- **Validates every signal** through a risk engine before any order is placed — this gate has no bypass
- **Executes orders** with smart routing to the right broker
- Operates on **AWS** using cost-optimised ARM64 servers and serverless Kafka messaging
- Maintains **full audit trails** — every signal, every risk decision, every order fill is logged permanently

```
What QuantEmbrace does in plain English:

  "Watch prices from NSE India and US stock markets,
   run 6 strategies to find trading opportunities,
   enrich each opportunity with ML predictions,
   verify every trade is safe,
   then place the order automatically —
   all day, every trading day, without human intervention."
```

### Paper Trading vs. Live Trading

The system starts in **paper trading mode** by default. In paper mode:
- All the real code runs — signals are generated, risk is validated, orders are "placed"
- The only difference is the execution engine simulates the fill instead of calling the real broker
- You see exactly what would have happened with real money, without any financial risk

This lets you verify the system is working correctly before committing real capital.

---

## What Markets Does It Trade?

### NSE India (National Stock Exchange)

- **Broker:** Zerodha Kite Connect
- **Instruments:** NSE equities (RELIANCE, TCS, INFY, HDFCBANK, etc.) and F&O derivatives
- **Trading hours:** 09:15 – 15:30 IST, Monday–Friday
- **Data source:** Zerodha Kite Ticker — real-time WebSocket stream, ticks every fraction of a second
- **Key detail:** Intraday (MIS) positions are auto-squared off at 15:15 IST by Zerodha. Our system closes them proactively at 15:10 IST.

### US Equities

- **Broker:** Alpaca Markets
- **Instruments:** NYSE, NASDAQ stocks (AAPL, MSFT, GOOGL, AMZN, NVDA, etc.)
- **Trading hours:** 09:30 – 16:00 ET (= ~19:00 – 01:30 IST during EDT / 20:00 – 02:30 IST during EST)
- **Data source:** Alpaca WebSocket, real-time trades and quotes
- **Key detail:** US and India trade at different times of day with no overlap, making them complementary markets for a 24-hour strategy.

### Why Two Markets?

Markets operating in different time zones, currencies, and economic environments have very low correlation with each other. When NSE is having a bad day due to RBI policy, US markets may be trending independently due to Fed decisions. Running strategies across both gives more opportunities and spreads risk across uncorrelated environments.

---

## Do I Need to Manually Pick Stocks?

**Short answer: You configure a watchlist once. The algorithm decides when to trade.**

```
What YOU do (one-time configuration):
  Open configs/instruments.yaml
  Set active: true for every stock you want watched
  Restart the service — done

What the ALGORITHM does (fully automatic, every trading day):
  Watches all active stocks in real time
  Runs all 6 strategies on every price update
  Generates a BUY or SELL signal when strategy conditions are met
  Sends the signal through AI enrichment → risk validation → execution
  You never manually say "buy RELIANCE today"
```

### Example: Controlling Your Watchlist

```yaml
# configs/instruments.yaml — you edit this file, the system reads it at startup
nse:
  instruments:
    - symbol: RELIANCE
      active: true     # ← system watches this stock and trades it
    - symbol: TCS
      active: true     # ← watches this too
    - symbol: WIPRO
      active: false    # ← completely ignored — no data, no signals
```

The system continuously watches RELIANCE and TCS. When strategy conditions are met (e.g., the short moving average crosses above the long moving average on RELIANCE), a BUY signal is generated automatically and flows through the full pipeline.

**To add a stock:** Set `active: true` and restart. No code changes needed.  
**To pause a stock:** Set `active: false` and restart.  
**To tune signal sensitivity:** Adjust `short_window` / `long_window` for that symbol.

The pre-built watchlist includes 20+ Nifty 50 blue-chips for NSE and 10+ S&P 500 names for US.

---

## The Big Picture — How a Trade Happens

Here is the complete journey of a single trade, from a raw price tick arriving to a filled broker order. Each step is handled by a different service.

```
STEP 1: MARKET DATA ARRIVES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Zerodha sends:  RELIANCE last traded price = ₹2,453.50
Alpaca sends:   AAPL bid = $182.10, ask = $182.15

     ↓  data_ingestion normalises and stores this tick

STEP 2: STRATEGY FINDS AN OPPORTUNITY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Momentum strategy calculates:
  "Short moving average (10 ticks) = ₹2,420"
  "Long moving average (50 ticks)  = ₹2,400"
  "Short MA is now above Long MA — bullish crossover!"

Strategy emits a signal:
  BUY RELIANCE | qty: 20 shares | confidence: 0.75

     ↓  signal published to Kafka topic: signals.pending

STEP 3: AI ENGINE ENRICHES THE SIGNAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AI engine reads the signal and adds:
  market_regime:  "trending"       ← is the market in trend or ranging mode?
  quality_score:  0.82             ← how reliable is this signal given current conditions?
  volatility_est: 0.018            ← expected volatility in the next hour

     ↓  enriched signal published to Kafka topic: signals.enriched

STEP 4: RISK ENGINE VALIDATES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
11 validators check in sequence. Every check must pass:
  ✓ Signal age: generated 0.3s ago (< 30s limit)
  ✓ Kill switch: OFF (system is active)
  ✓ Position limit: 4 open positions, max is 8 → OK
  ✓ Exposure check: buying ₹49,060 worth, within 5% portfolio limit → OK
  ✓ Daily loss: today's P&L = +₹8,200, no drawdown → OK
  ✓ Margin: sufficient balance in Zerodha account → OK
  ✓ Slippage: market has not moved more than 0.15% since signal → OK
  ✓ Spread: bid-ask spread is 8 bps, under 50 bps limit → OK
  ✓ Sector limit: Energy sector exposure within 20% limit → OK
  ✓ Liquidity: order is < 1% of average daily volume → OK
  ✓ Quality score: 0.82 exceeds minimum threshold of 0.30 → OK

Decision: APPROVED (risk_decision_id: abc-789)

     ↓  approved signal published to Kafka topic: signals.approved

STEP 5: ORDER PLACED
━━━━━━━━━━━━━━━━━━━━
Execution engine:
  1. Checks DynamoDB — not a duplicate order
  2. Routes to Zerodha (because market = NSE)
  3. Places: BUY 20 RELIANCE @ MARKET
  4. Zerodha confirms: order_id = "987654321"
  5. Saves to DynamoDB: status = PLACED

     ↓  Zerodha fills the order milliseconds later

STEP 6: FILL CONFIRMED
━━━━━━━━━━━━━━━━━━━━━━
Zerodha confirms: FILLED @ ₹2,454.00 (all 20 shares)
DynamoDB updated: status = FILLED, avg_price = ₹2,454.00
Risk engine notified: update position tracking
Kafka: fill event published to orders.events
```

This entire sequence — from tick arriving to order placed — typically takes **under 1 second**. The broker fill time (Zerodha matching the order on the exchange) is usually another 100–500ms.

---

## Core Design Philosophy

These five principles explain every major architecture decision in the system.

### 1. Risk First, Always

The Risk Engine is not optional. It is not a suggestion. Every signal — no matter how confident the strategy is — must pass through the Risk Engine and the AI enrichment pipeline before touching a broker. This is the most important rule in the entire system.

> *"The worst thing an algo trading system can do is place a bad trade very fast. Speed without risk control is dangerous."*

### 2. Separation of Concerns

Three layers that must never mix:
- **Strategy** = "should I trade?" — computes signals from price data
- **Risk** = "is it safe to trade?" — validates signals against limits
- **Execution** = "how do I trade?" — places orders with the broker

Importing strategy code in the execution service, or checking risk limits inside a strategy, is a critical defect. These boundaries are enforced structurally — services communicate only through Kafka topics, never through direct Python imports.

### 3. Idempotency (Restart Safety)

If the system crashes and restarts, it must not:
- Place duplicate orders
- Miss any fills
- Lose track of open positions

Every order has a UUID (`signal_id`) created when the strategy generates the signal. Before placing any order, the execution engine checks DynamoDB: "has an order with this signal_id already been placed?" This prevents duplicates regardless of how many times the service restarts.

### 4. Fail Safe, Not Fail Open

If the Risk Engine is down → trading halts (not: signals bypass risk and go straight to execution).  
If a broker connection drops → positions are held safely (not: random orders placed to close).  
If a strategy throws an exception → that strategy stops (not: the entire system crashes).  
If the AI engine lags → signals flow through a fallback path (not: trading halts entirely).

### 5. Cost-Conscious Infrastructure

Services run on AWS EC2 ARM64 Graviton instances (c6g, t4g) — up to 40% cheaper than equivalent x86 instances. Messaging uses Kafka MSK Serverless with no idle cluster cost when markets are closed. There is no ECS Fargate overhead, no Lambda cold start latency, and no SQS polling waste. Every AWS service choice has been made with both correctness and cost in mind.

---

## What This System Is NOT

To set correct expectations:

| This system IS | This system is NOT |
|---|---|
| A production execution platform | A strategy research tool (that's backtesting) |
| AWS-native and cloud-deployed | A local desktop trading app |
| Python 3.11+, async | A low-latency C++/FPGA HFT system |
| Designed for 1-second to minute-level signals | Designed for microsecond HFT |
| Multi-market (NSE India + US equities) | A single-broker wrapper |
| Production-hardened with risk controls | A prototype |
| Paper trading safe by default | Ready for live capital without validation |

---

## How to Navigate the Docs

Start with the document that matches your goal:

| I want to... | Go to |
|---|---|
| Understand the full system architecture | [02_architecture.md](02_architecture.md) |
| See exactly how a signal travels from price to order | [03_signal_lifecycle.md](03_signal_lifecycle.md) |
| Look up a specific service (what it does, how to debug it) | [04_services.md](04_services.md) |
| Get the system running locally on my machine | [05_local_setup.md](05_local_setup.md) |
| Understand the AWS infrastructure and costs | [06_aws_infrastructure.md](06_aws_infrastructure.md) |
| Add a new trading strategy or broker | [07_contributing.md](07_contributing.md) |
| Check what needs to be done before going live | [runbooks/go_live_checklist.md](runbooks/go_live_checklist.md) |
| Learn the daily operating procedures | [../runbooks/daily_operations.md](../runbooks/daily_operations.md) |
| Look up a trading or technical term | [07_contributing.md#glossary--trading-terms](07_contributing.md#glossary--trading-terms) |

**Full docs index:** [README.md](README.md)

---

*Last updated: 2026-05-15 | Update this document when: new markets are added, new brokers are integrated, or the core design philosophy changes.*
