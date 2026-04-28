# QuantEmbrace — Contributing Guide, Glossary & FAQ

---

## Table of Contents

1. [How to Add a New Strategy](#how-to-add-a-new-strategy)
2. [How to Add a New Broker](#how-to-add-a-new-broker)
3. [Risk Rules Checklist — Before Any PR](#risk-rules-checklist--before-any-pr)
4. [Git Workflow and Standards](#git-workflow-and-standards)
5. [Code Style Standards](#code-style-standards)
6. [Common Beginner Mistakes](#common-beginner-mistakes)
7. [Glossary — Trading Terms](#glossary--trading-terms)
8. [Glossary — Technical Terms](#glossary--technical-terms)
9. [FAQ for Newcomers](#faq-for-newcomers)

---

## How to Add a New Strategy

Strategies are the most common contribution to this codebase. Here is the exact process.

### Step 1: Create the Strategy File

```python
# services/strategy_engine/strategies/mean_reversion_strategy.py

from decimal import Decimal
from collections import deque
from statistics import mean, stdev
from typing import Optional
from uuid import uuid4

from strategy_engine.strategies.base_strategy import BaseStrategy
from strategy_engine.signals.signal import Signal, Direction, OrderType
from shared.utils.helpers import utc_now
from shared.logging.logger import get_logger

logger = get_logger(__name__)


class MeanReversionStrategy(BaseStrategy):
    """
    Z-score mean reversion strategy.
    
    Logic:
      - Track a rolling window of prices (default: 50 ticks)
      - Compute z-score = (current_price - mean) / stdev
      - BUY when z-score < -2.0 (price is 2 standard deviations BELOW mean)
      - SELL when z-score > +2.0 (price is 2 standard deviations ABOVE mean)
    
    Theory: prices tend to "revert to the mean". If a stock is unusually
    cheap (low z-score), it's likely to recover. If unusually expensive
    (high z-score), it's likely to fall.
    
    Args:
        name: Unique strategy identifier for logging and tracking.
        symbols: List of symbols this strategy watches.
        market: "NSE" or "US".
        window: Number of ticks for rolling statistics (default 50).
        z_threshold: Z-score threshold for signal (default 2.0).
    """

    def __init__(
        self,
        name: str,
        symbols: list[str],
        market: str,
        window: int = 50,
        z_threshold: float = 2.0,
    ) -> None:
        self.name = name
        self.symbols = symbols
        self.market = market
        self._window = window
        self._z_threshold = z_threshold
        self._prices: dict[str, deque] = {s: deque(maxlen=window) for s in symbols}

    async def initialize(self, saved_state: Optional[dict]) -> None:
        """Restore state from DynamoDB if available (called on startup)."""
        if saved_state and "prices" in saved_state:
            for symbol, prices in saved_state["prices"].items():
                if symbol in self._prices:
                    self._prices[symbol].extend(prices)
        logger.info("Strategy %s initialized with %d symbols", self.name, len(self.symbols))

    async def on_tick(self, symbol: str, price: float, volume: int, timestamp) -> None:
        """Called for every price tick. Updates internal price buffer."""
        if symbol in self._prices:
            self._prices[symbol].append(Decimal(str(price)))

    async def generate_signal(self) -> Optional[Signal]:
        """Check all symbols and return a signal if conditions are met."""
        for symbol in self.symbols:
            prices = self._prices[symbol]
            
            # Need enough data for statistics
            if len(prices) < self._window:
                continue
            
            current_price = prices[-1]
            price_mean = Decimal(str(mean(prices)))
            price_stdev = Decimal(str(stdev(prices)))
            
            if price_stdev == 0:
                continue  # All prices are identical, can't compute z-score
            
            z_score = (current_price - price_mean) / price_stdev
            
            if z_score < -self._z_threshold:
                # Price is unusually low → expect reversion → BUY
                return self._create_signal(
                    symbol=symbol,
                    direction=Direction.BUY,
                    price=current_price,
                    z_score=float(z_score),
                )
            elif z_score > self._z_threshold:
                # Price is unusually high → expect reversion → SELL
                return self._create_signal(
                    symbol=symbol,
                    direction=Direction.SELL,
                    price=current_price,
                    z_score=float(z_score),
                )
        
        return None

    def _create_signal(
        self, symbol: str, direction: Direction, price: Decimal, z_score: float
    ) -> Signal:
        stop_pct = Decimal("0.02")  # 2% stop-loss
        
        if direction == Direction.BUY:
            stop_price = price * (1 - stop_pct)
        else:
            stop_price = price * (1 + stop_pct)
        
        # Confidence is proportional to how extreme the z-score is
        confidence = min(abs(z_score) / (self._z_threshold * 2), 1.0)
        
        return Signal(
            signal_id=str(uuid4()),
            strategy_name=self.name,
            market=self.market,
            instrument=f"{self.market}:{symbol}",
            direction=direction,
            quantity=10,  # TODO: use position sizer based on confidence and volatility
            order_type=OrderType.MARKET,
            limit_price=None,
            stop_price=stop_price,
            confidence=confidence,
            metadata={"z_score": z_score, "window": self._window},
            created_at=utc_now(),
        )

    def get_state(self) -> dict:
        """Return current state for DynamoDB persistence on shutdown."""
        return {
            "prices": {
                symbol: [str(p) for p in prices]
                for symbol, prices in self._prices.items()
            }
        }

    def get_parameters(self) -> dict:
        """Return parameters for logging and auditing."""
        return {
            "window": self._window,
            "z_threshold": self._z_threshold,
            "symbols": self.symbols,
        }
```

### Step 2: Register the Strategy

```python
# services/strategy_engine/service.py → _register_default_strategies()

def _register_default_strategies(self) -> None:
    """Register the default set of strategies."""
    # Existing strategies
    nse_momentum = MomentumStrategy(...)
    us_momentum = MomentumStrategy(...)
    
    # ADD YOUR NEW STRATEGY:
    from strategy_engine.strategies.mean_reversion_strategy import MeanReversionStrategy
    nse_mean_rev = MeanReversionStrategy(
        name="nse_mean_reversion_v1",
        symbols=["RELIANCE", "TCS", "INFY"],
        market="NSE",
        window=50,
        z_threshold=2.0,
    )
    
    self.register_strategy(nse_momentum)
    self.register_strategy(us_momentum)
    self.register_strategy(nse_mean_rev)  # ← Add this line
```

### Step 3: Write Tests

```python
# tests/unit/strategy_engine/test_mean_reversion_strategy.py

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock

from strategy_engine.strategies.mean_reversion_strategy import MeanReversionStrategy
from strategy_engine.signals.signal import Direction


@pytest.mark.asyncio
async def test_no_signal_with_insufficient_data():
    """Strategy should not generate signals until window is full."""
    strategy = MeanReversionStrategy(
        name="test_mr", symbols=["RELIANCE"], market="NSE", window=50
    )
    
    # Feed only 10 ticks — not enough for window=50
    for price in range(10):
        await strategy.on_tick("RELIANCE", 2400.0 + price, 1000, None)
    
    signal = await strategy.generate_signal()
    assert signal is None


@pytest.mark.asyncio
async def test_buy_signal_on_low_z_score():
    """Strategy should emit BUY when price is 2+ std devs below mean."""
    strategy = MeanReversionStrategy(
        name="test_mr", symbols=["RELIANCE"], market="NSE", window=10, z_threshold=2.0
    )
    
    # Feed 9 ticks at 2400 (establishing the mean)
    for _ in range(9):
        await strategy.on_tick("RELIANCE", 2400.0, 1000, None)
    
    # Feed 1 tick that's very low (should trigger z-score < -2.0)
    await strategy.on_tick("RELIANCE", 2340.0, 1000, None)  # ~3 std devs below mean
    
    signal = await strategy.generate_signal()
    assert signal is not None
    assert signal.direction == Direction.BUY
    assert signal.confidence > 0.5


@pytest.mark.asyncio  
async def test_stop_loss_set_correctly():
    """Stop-loss should be 2% below entry for BUY signals."""
    strategy = MeanReversionStrategy(
        name="test_mr", symbols=["RELIANCE"], market="NSE", window=10, z_threshold=2.0
    )
    
    for _ in range(9):
        await strategy.on_tick("RELIANCE", 2400.0, 1000, None)
    await strategy.on_tick("RELIANCE", 2340.0, 1000, None)
    
    signal = await strategy.generate_signal()
    
    assert signal is not None
    # Stop loss should be 2% below the entry price of 2340
    expected_stop = Decimal("2340.0") * Decimal("0.98")
    assert abs(signal.stop_price - expected_stop) < Decimal("1.0")
```

### Step 4: Backtest Before Deploying

```bash
# Test your strategy against historical data before going live
python scripts/backtest/run.py \
    --strategy mean_reversion \
    --symbols RELIANCE,TCS \
    --market NSE \
    --from 2025-01-01 \
    --to 2025-12-31

# Minimum acceptable backtest metrics:
# - Sharpe ratio > 0.8
# - Max drawdown < 15%
# - Win rate > 45%
```

---

## How to Add a New Broker

If you want to integrate a new broker (e.g., Interactive Brokers, Fyers):

### Step 1: Create the Broker Adapter

```python
# services/execution_engine/brokers/fyers_broker.py

from execution_engine.brokers.base_broker import BrokerClient
from execution_engine.orders.order import OrderRequest, OrderResponse, OrderStatus, CancelResponse, Position

class FyersBroker(BrokerClient):
    """Fyers API adapter for NSE/BSE trading."""
    
    def __init__(self, client_id: str, access_token: str) -> None:
        self._client_id = client_id
        self._access_token = access_token
        # Initialize Fyers SDK here
    
    async def place_order(self, order: OrderRequest) -> OrderResponse:
        # Map internal order to Fyers API format
        # Call Fyers API
        # Map response back to OrderResponse
        ...
    
    async def cancel_order(self, order_id: str) -> CancelResponse:
        ...
    
    async def get_positions(self) -> list[Position]:
        ...
    
    async def get_order_status(self, order_id: str) -> OrderStatus:
        ...
```

### Step 2: Add Routing in Execution Engine

```python
# services/execution_engine/service.py → _get_broker()

def _get_broker(self, market: Market) -> BrokerClient:
    if market == Market.NSE:
        return self._zerodha      # or self._fyers if using Fyers for NSE
    elif market == Market.US:
        return self._alpaca
    elif market == Market.NSE_FYERS:  # New market enum value
        return self._fyers
    else:
        raise ValueError(f"Unsupported market: {market}")
```

### Step 3: Add credentials to Secrets Manager and environment config

---

## Risk Rules Checklist — Before Any PR

Before merging any code that touches trading logic, verify:

### Separation of Concerns
- [ ] Strategy code does NOT call any broker API
- [ ] Strategy code does NOT check margins or positions
- [ ] Risk code does NOT modify signals (only approve/reject)
- [ ] Execution code does NOT generate signals
- [ ] There is no import of `execution_engine` inside `strategy_engine`
- [ ] There is no import of `strategy_engine` inside `execution_engine`

### Risk Engine Integrity
- [ ] All signals arrive at the execution engine with a `risk_decision_id` attached
- [ ] The execution engine rejects signals without `risk_decision_id`
- [ ] No "emergency bypass" flag or skip condition exists in the risk validation pipeline

### Idempotency
- [ ] Every order placement checks DynamoDB for an existing order with the same `signal_id`
- [ ] Retries use the same `order_id` — not a new one
- [ ] Service startup reconciles open orders with broker before processing new signals

### No Silent Failures
- [ ] All exceptions in the trading path are caught, logged, AND raise an alert
- [ ] No bare `except: pass` in execution or risk code
- [ ] Failed orders update DynamoDB status to `FAILED` before raising

### Restart Safety
- [ ] Strategy state is saved to DynamoDB in `stop()` and restored in `initialize()`
- [ ] Kill switch state persists in DynamoDB (not in memory only)
- [ ] Daily P&L counters persist in DynamoDB

---

## Git Workflow and Standards

### Branch Naming

```
feature/TICK-123-add-mean-reversion-strategy
fix/TICK-456-fix-alpaca-token-expiry-handling
infra/add-cloudwatch-alarm-dlq
refactor/TICK-789-split-risk-validator-classes
```

### Commit Messages

Use conventional commits format:
```
feat: add mean reversion strategy with z-score signals
fix: handle Alpaca 429 rate limit with backoff retry
refactor: extract position sizing into separate class
docs: update architecture doc with AI engine changes
test: add hypothesis tests for risk limit edge cases
infra: add CloudWatch alarm for kill switch activation
```

### Pull Request Rules

| PR Type | Reviewers Required | Must Pass |
|---|---|---|
| Trading logic (strategy, risk, execution) | 2 reviewers | Tests + backtest |
| Infrastructure changes | 1 reviewer | Terraform plan review |
| Documentation | 1 reviewer | None (but must be accurate) |
| Tests only | 1 reviewer | CI tests |

### Squash Merge Policy

All PRs are squash-merged to keep main history clean. The PR description becomes the commit message.

---

## Code Style Standards

### Python Standards

```python
# ✅ CORRECT: Type hints everywhere
async def validate_signal(self, signal: Signal) -> RiskDecision:
    ...

# ❌ WRONG: No type hints
async def validate(self, s):
    ...

# ✅ CORRECT: Pydantic for all data models
class Signal(BaseModel):
    signal_id: str
    direction: Direction
    quantity: int

# ❌ WRONG: Plain dicts or dataclasses for models passed between services
signal = {"signal_id": "...", "direction": "BUY"}

# ✅ CORRECT: Google-style docstrings for all public functions
def compute_z_score(prices: list[Decimal]) -> float:
    """Compute the z-score of the latest price in the series.
    
    Args:
        prices: Rolling window of prices, most recent last.
        
    Returns:
        Z-score of the last price relative to the window mean and stdev.
        
    Raises:
        ValueError: If prices list has fewer than 2 elements.
    """

# ✅ CORRECT: Async/await for all I/O
async def get_positions(self) -> list[Position]:
    response = await asyncio.to_thread(self._dynamo.scan, TableName="positions")
    
# ❌ WRONG: Synchronous I/O in async context (blocks the event loop)
def get_positions(self) -> list[Position]:
    response = self._dynamo.scan(TableName="positions")  # Blocks!
```

### Line Length and Formatting

```bash
# Auto-format all code (must pass with zero diffs in CI)
black services/ tests/ --line-length 100

# Lint (must pass with zero warnings in CI)
ruff check services/ tests/

# Sort imports (must match black)
isort services/ tests/ --profile black
```

---

## Common Beginner Mistakes

### Mistake 1: Importing across service boundaries

```python
# ❌ WRONG: Execution engine importing from strategy engine
# services/execution_engine/service.py
from strategy_engine.signals.signal import Signal  # DON'T DO THIS

# ✅ CORRECT: Use shared models or SQS message deserialization
# services/execution_engine/service.py
from shared.models.signal import ApprovedSignal  # Use shared models
```

**Why:** Cross-imports create tight coupling. Services should only share data through SQS messages and DynamoDB — never through Python imports.

### Mistake 2: Placing orders without risk_decision_id

```python
# ❌ WRONG: Creating an order request without checking risk
order = OrderRequest(symbol="RELIANCE", side=BUY, quantity=50)
broker.place_order(order)

# ✅ CORRECT: Only process signals that came through the risk engine
if not signal_data.get("risk_decision_id"):
    raise ValueError("Cannot execute — signal has no risk_decision_id")
```

### Mistake 3: Using synchronous sleep in async code

```python
# ❌ WRONG: This blocks the entire event loop — no other coroutines run during this sleep
import time
time.sleep(5)

# ✅ CORRECT: Async sleep yields control to the event loop
import asyncio
await asyncio.sleep(5)
```

### Mistake 4: Swallowing exceptions

```python
# ❌ WRONG: Exception is eaten silently — you'll never know the order failed
try:
    await broker.place_order(order)
except Exception:
    pass  # NEVER DO THIS IN TRADING CODE

# ✅ CORRECT: Log it, update state, alert
try:
    await broker.place_order(order)
except BrokerError as exc:
    logger.error("Order placement failed: %s", exc, signal_id=signal.signal_id)
    await order_manager.update_order_status(order.order_id, OrderStatus.FAILED)
    raise  # Re-raise so the caller knows it failed
```

### Mistake 5: Using `datetime.now()` instead of UTC

```python
# ❌ WRONG: Returns local machine time — wrong on an AWS server in UTC
from datetime import datetime
ts = datetime.now()

# ✅ CORRECT: Always UTC-aware timestamps
from shared.utils.helpers import utc_now
ts = utc_now()  # returns datetime(2026, 4, 24, tzinfo=UTC)
```

### Mistake 6: Hardcoded credentials

```python
# ❌ WRONG: NEVER put credentials in code
kite = KiteConnect(api_key="abc123", api_secret="xyz789")

# ✅ CORRECT: Read from Secrets Manager or AppSettings
from shared.config.settings import get_settings
settings = get_settings()
kite = KiteConnect(api_key=settings.kite_api_key, api_secret=settings.kite_api_secret)
```

---

## Glossary — Trading Terms

| Term | Definition |
|---|---|
| **Algo Trading** | Automated trading using computer algorithms instead of human decisions |
| **Backtest** | Testing a strategy against historical data to see how it would have performed |
| **Bid** | The highest price a buyer is currently willing to pay |
| **Ask** | The lowest price a seller is currently willing to sell for |
| **Bid-Ask Spread** | The difference between bid and ask prices. Wider spread = less liquid market |
| **Bull Market** | A market trending upward over time |
| **Bear Market** | A market trending downward over time |
| **CNC** | Cash and Carry — Zerodha product type for delivery/overnight positions |
| **Confidence Score** | 0.0–1.0 score indicating how strongly the strategy believes in a signal |
| **Crossover** | When one moving average crosses another (e.g., short MA crosses above long MA) |
| **Drawdown** | Decline from a peak to a trough in portfolio value. "Max drawdown" = worst historical decline |
| **Equity Curve** | Chart of portfolio value over time — shows the trajectory of a strategy |
| **Exposure** | Total market value of open positions. Gross exposure = |longs| + |shorts| |
| **F&O** | Futures and Options — derivative instruments on NSE |
| **Fill** | When an order is executed by the broker (a "filled" order is completed) |
| **Golden Cross** | When 50-day MA crosses above 200-day MA — traditionally bullish signal |
| **Intraday** | Positions opened and closed within the same trading day |
| **Kill Switch** | Emergency stop that immediately halts all trading activity |
| **Limit Order** | Order to buy/sell at a specific price or better (won't execute at a worse price) |
| **Liquidity** | How easily an asset can be bought or sold without affecting its price |
| **LTP** | Last Traded Price — the most recent price at which a trade occurred |
| **Market Order** | Order to buy/sell immediately at whatever the current market price is |
| **MIS** | Margin Intraday Square-off — Zerodha product type for intraday positions |
| **Moving Average** | Average of the last N prices. "Smooths out" price noise |
| **NRML** | Normal — Zerodha product type for overnight F&O positions |
| **NSE** | National Stock Exchange of India |
| **OHLCV** | Open, High, Low, Close, Volume — the standard candlestick data format |
| **P&L** | Profit and Loss — difference between current value and entry cost |
| **PDT Rule** | Pattern Day Trader rule (US) — accounts under $25k can only make 3 day trades per 5 days |
| **Position** | An open holding of an asset (long = bought, short = sold) |
| **Position Sizing** | Deciding how many shares to buy/sell based on risk rules and conviction |
| **Regime** | The "mode" a market is in — trending, ranging (sideways), or highly volatile |
| **Sharpe Ratio** | Risk-adjusted return: (return − risk-free rate) / volatility. Above 1.0 is generally good |
| **Signal** | A structured recommendation to buy or sell an instrument |
| **Slippage** | Difference between expected fill price and actual fill price |
| **Stop-Loss** | A pre-defined price level where a losing position is automatically closed |
| **Swing Trade** | Position held for days to weeks (not intraday, not long-term) |
| **Tick** | A single price update event from the exchange |
| **VWAP** | Volume-Weighted Average Price — average price weighted by trading volume |
| **WebSocket** | A persistent two-way connection — used for real-time data streams |
| **Win Rate** | Percentage of trades that were profitable |
| **Z-score** | How many standard deviations a value is from its mean. Z = 2.0 means 2 stdev above average |

---

## Glossary — Technical Terms

| Term | Definition |
|---|---|
| **Adapter Pattern** | Design pattern where a wrapper class translates one interface into another (e.g., ZerodhaAdapter) |
| **ADR** | Architecture Decision Record — a document explaining why an architectural decision was made |
| **Async/Await** | Python pattern for non-blocking concurrent code — lets one coroutine run while another waits for I/O |
| **ONNX** | Open Neural Network Exchange — a standard format for ML model files |
| **Conditional Write** | DynamoDB feature — only update a record if it's currently in a specific state (prevents race conditions) |
| **Correlation ID** | A UUID that tags all log entries for a single transaction across multiple services |
| **Dead Letter Queue (DLQ)** | A SQS queue that receives messages that failed processing too many times |
| **ECS Fargate** | AWS managed container service — runs Docker containers without managing servers |
| **ECR** | Elastic Container Registry — AWS's Docker image storage |
| **Exponential Backoff** | Retry strategy where wait time doubles between each retry (1s, 2s, 4s...) |
| **FIFO Queue** | First-In-First-Out queue — guarantees messages are processed in the order they arrive |
| **Hypothesis** | Python library for property-based testing — generates thousands of random test inputs |
| **IAM** | Identity and Access Management — AWS service for controlling who can access what |
| **Idempotent** | An operation that produces the same result whether called once or many times |
| **IaC** | Infrastructure as Code — defining cloud resources in code (Terraform) rather than manual clicks |
| **LocalStack** | A local AWS simulator — runs S3, SQS, DynamoDB etc. on your laptop for development |
| **Parquet** | A columnar storage file format — efficient for analytics queries on large datasets |
| **Protocol (Python)** | Python's structural subtyping — defines an interface without requiring explicit inheritance |
| **Pydantic** | Python library for data validation using type hints — all models in this system use it |
| **ruff** | A fast Python linter — checks for code quality issues |
| **SQS** | Simple Queue Service — AWS managed message queue for decoupling services |
| **SNS** | Simple Notification Service — AWS managed pub/sub for sending alerts (email, SMS, webhooks) |
| **Structured Logging** | Logging in JSON format instead of plain text — makes logs easily queryable |
| **Task Role (ECS)** | An IAM role assigned to a specific ECS task — defines what AWS services it can access |
| **Terraform** | Infrastructure as Code tool — defines AWS resources in `.tf` files |
| **TTL** | Time To Live — DynamoDB feature that automatically deletes records after a specified time |
| **VPC** | Virtual Private Cloud — an isolated network within AWS |
| **VPC Endpoint** | A private connection from your VPC to an AWS service, bypassing the public internet |

---

## FAQ for Newcomers

**Q: Why is the Risk Engine a separate service and not just a function inside the Strategy Engine?**

A: Because services can fail independently. If the risk checks were inside the strategy engine and the strategy engine crashed, they'd both go down together. As a separate service, if the strategy engine crashes, the risk engine still runs. When strategies restart, their signals still pass through risk validation. More importantly, it enforces the architecture: physically separating them makes it impossible (by accident) to skip risk.

**Q: What happens to my positions if the system crashes during market hours?**

A: Positions are held at the broker. They don't close automatically just because our system went down. On restart, the execution engine reconciles with the broker to find out what filled and what didn't. Existing positions are safe — they live in the broker's system, not ours.

**Q: Can the system trade in paper mode (no real money)?**

A: Yes. Set `ALPACA_BASE_URL=https://paper-api.alpaca.markets` for US paper trading. For Zerodha NSE, there's no official paper trading API — you'd typically use a backtest to simulate instead.

**Q: Why do we use Python instead of a faster language like C++ or Java?**

A: Our signal latency target is seconds-to-minutes, not microseconds. Python's quant/ML ecosystem (numpy, pandas, scikit-learn, PyTorch) is unmatched. The broker API latency (50–200ms) dominates anyway — shaving microseconds off our code doesn't matter. If we needed HFT (sub-millisecond), we'd use C++. We don't.

**Q: What's the difference between `stop_price` in a signal and the stop-loss order at the broker?**

A: The `stop_price` in the signal is the **price level** at which we want to exit if the trade goes wrong. The execution engine is responsible for converting that into an actual broker stop-loss order (SL-M on Zerodha, stop on Alpaca). The risk engine also reads this price to validate that it exists before approving.

**Q: Why SQS instead of Kafka for messaging?**

A: Our message volumes are relatively low (<1000 signals/day). SQS is serverless — no cluster to manage. Kafka requires a cluster (MSK on AWS is expensive). SQS's at-least-once delivery with deduplication (FIFO) is sufficient for our needs. If we ever reach millions of messages/day, we'd revisit Kafka.

**Q: How does the system handle the Zerodha daily token expiry?**

A: Zerodha issues a new access token each trading day after login. The `zerodha_refresh_token.py` script handles this — it opens a browser for login, exchanges the authorization code for a token, and saves it to AWS Secrets Manager. The data_ingestion and execution services read from Secrets Manager, so they automatically get the new token on next load. This process should be run once per day before market hours (it can be automated with a scheduled ECS task that runs at 08:00 IST).

**Q: What happens if the drawdown limit is hit? Can I override it?**

A: When the daily loss limit is hit, the kill switch activates automatically. There is no override. This is by design — in a panic situation, you don't want human override capability; you want the system to stop. The kill switch deactivates at the start of the next trading day. If you genuinely need to change the limit (e.g., you've recapitalized), update the `MAX_DAILY_LOSS_PCT` value in DynamoDB — the risk engine reads it at runtime without requiring a restart.

**Q: How do I know if my new strategy is actually working in production?**

A: Check the CloudWatch dashboard for your strategy's signal rate, and check the S3 risk audit logs to see what % of your signals are being approved. If signals are being generated but rejected, the risk logs will explain why. The execution logs show all filled orders with strategy attribution.

---

*Last updated: 2026-04-24 | Update the glossary whenever new technical or trading concepts are introduced. Update the FAQ whenever newcomers repeatedly ask the same questions.*
