# QuantEmbrace — Version 3: Alpha Economics, Execution Realism & Governance

> **VERSION SCOPE: v3 — DO NOT IMPLEMENT**  
> All changes in this document are scoped to **QuantEmbrace Version 3**.  
> Implementation begins only after Version 1 (current) has been fully tested, paper-validated, and gone live with real capital.  
> This document is a forward-planning reference. No code, config, or infrastructure changes should be made from this document until v3 is formally initiated.

_Version: v3 — Future Scope_  
_Status: DRAFT v1.0 — Held pending v1 go-live_  
_Review basis: External platform review "Refining QuantEmbrace for Stronger Alpha, Simpler Operations, Execution Realism, and Proven Profitability"_  
_Prerequisite: QuantEmbrace v1 fully live with real capital; Phase 8 (Production Hardening) complete_  
_Author role: Chief Architect_

---

---

## Version Roadmap

| Version | Phases | Key Deliverables | Gate to next version |
|---------|--------|-----------------|----------------------|
| **v1** _(current)_ | 1–7 | Kafka-native backbone · MSK Serverless · EC2 ARM64 ASGs · 6 alpha strategies (paper) · AI/ML signal enrichment (HMM + GBT) · retry infrastructure · 5-day paper validation protocol · go-live runbook | v1 live with real capital for ≥ 5 sessions; all Phase 7 checklist items green |
| **v2** | 8 | Production hardening: ACK_UNKNOWN order state · SQLite durable outbox · per-endpoint Zerodha rate budgets · startup reconciliation gate · 3-tier stop-loss + orphan detector · signal inbox/outbox · 158+ fault-tolerance tests | Phase 8 P0 tasks all complete; zero-defect reconciliation for ≥ 5 sessions; kill-switch fanout CI test passing |
| **v3** _(this doc)_ | 9+ | Alpha economics (cost-aware signals v5.0) · alpha sleeve architecture · portfolio construction layer · broker capability matrix · fill-state realism (3-price IS tracking) · 4-mode simulation stack · 6-stage formal promotion ladder · decision-quality observability (5 dashboards) · graduated kill-switch degradation | Initiated by Hari explicitly after v2 complete |

### v3 Internal Phases

| v3 Stage | Focus | Builds on |
|----------|-------|-----------|
| **A** — Blockers | Risk limits config · broker capability matrix · position reconciliation gate · fill-state realism | v2 complete |
| **B** — Signal Economics | signal_event v5.0 · CostModel · AlphaForecast DTO · momentum scorer · PortfolioBuilder · suppression gate | Stage A complete |
| **C** — Simulation & Validation | Identity map · cost-aware backtest · scorecard (DSR/PBO) · replay test suite | Stage B complete |
| **D** — Governance & Observability | Promotion ladder CLI · 5 dashboards · shadow trading harness · degradation hierarchy | Stage C complete |
| **E** — Alpha Quality | Quality overlay sleeve · regime filter wrapper · ensemble combiner · vol scaling overlay | Stage D complete |
| **F** — Advanced Execution | Per-broker rate limiters · order slicer · capability router · reconciler | Stage A complete (parallel with B–E) |

---

## 1. Executive Summary

Phases 1–7 delivered a structurally sound Kafka-first trading platform. The external review confirms the architecture is correct and does not require a rewrite. The highest-return work is now **economic**, not infrastructural:

| Theme | Current State | Gap |
|-------|--------------|-----|
| **Signals** | Direction-only (price, stop, qty) | No cost awareness — net edge never computed |
| **Alpha** | 6 isolated strategy classes | No shared forecast interface, no cost filter |
| **Portfolio** | Each strategy sizes independently | No unified construction, no turnover penalty |
| **Execution** | Broker conditionals scattered in code | No capability matrix, no fill-state tracking |
| **Simulation** | Simple backtester (mid-price fills) | No arrival-price, no implementation shortfall |
| **Governance** | 5-day paper gate (binary) | No formal promotion ladder, no scorecard |
| **Observability** | System-health metrics | No decision-quality metrics (IC, edge decay) |
| **Risk Limits** | Scattered across validator files | Not config-driven, no per-strategy overrides |

**The three blockers to real capital confidence (per review):**
1. No demonstrated, cost-adjusted, out-of-sample validation results
2. No broker-specific capability matrix
3. No hard position reconciliation gate before order entry

v3 addresses all three blockers and the wider economic gaps.  
v3 does **not** change the Kafka topology, service count, EC2 architecture, or the 6-layer boundary model.

---

## 2. Architecture Invariants (Nothing Changes Below)

The following are **explicitly confirmed unchanged** by Phase 9:

- **Kafka-first backbone**: All real-time inter-service flows remain Kafka. No SQS, no HTTP for trading data paths.
- **6-layer boundary model**: Layer 2 (Strategy) never places orders. Layer 4 (Risk) never generates signals. Layer 3 (Execution) never generates signals or overrides risk. These rules are non-negotiable.
- **Signal flow sequence**: `strategy_engine → [ai_engine] → risk_engine → execution_engine`. No bypass path.
- **EC2 ARM64 ASGs**: No change to compute layer. No ECS Fargate re-introduction.
- **MSK Serverless**: No change to messaging infrastructure.
- **kill.switch topic**: No change to kill switch propagation mechanism.
- **trace_id propagation**: Still a uuid4 set at tick origin, propagated unchanged to fill.
- **Idempotency contract**: `attribute_not_exists(signal_id)` + `attribute_not_exists(order_id)` conditional writes remain.
- **paper_trade pipeline**: DynamoDB strategy-config still controls paper_trade flag.
- **Existing Kafka topics**: No topics are removed or renamed. `signals.pending`, `signals.enriched`, `signals.approved`, `orders.events`, `kill.switch`, `ops.audit` are unchanged.
- **Audit trail (v3 expanded)**: Every signal, decision, validation result, and order at every hop MUST be recorded. v3 adds the following new event types to the `ops.audit` topic (existing event types unchanged):

  | New v3 Event | Published by | When |
  |-------------|-------------|------|
  | `SIGNAL_SUPPRESSED_NEGATIVE_EDGE` | strategy_engine (CostModel) | forecast dropped: net_edge_bps ≤ 0 |
  | `SIGNAL_CLIPPED_FOR_LIQUIDITY` | execution_engine (order_policy.py step 3.5) | target_qty reduced for ADV constraint |
  | `SIGNAL_STALE_NET_EDGE` | execution_engine (order_policy.py step 4) | net_edge rechecked at arrival; turned negative |
  | `PARTIAL_FILL` | execution_engine | partial fill received from broker |
  | `PARTIAL_FILL_EXPIRED` | execution_engine | max_partial_fill_age_ms exceeded; remainder cancelled |
  | `PORTFOLIO_BUILDER_DECISION` | strategy_engine (PortfolioBuilder) | solve cycle complete; target_qty per symbol emitted |
  | `SCORECARD_APPROVED` / `SCORECARD_REJECTED` | ops CLI (promote.py) | Chief Architect promotion decision |
  | `PROMOTION_STAGE_CHANGE` | ops CLI | strategy advances or rolls back a stage |
  | `RECONCILIATION_STATUS_CHANGE` | execution_engine (Reconciler) | CLEAN → DRIFT_WARN → DRIFT_HALT → CLEAN |
  | `DEGRADATION_LEVEL_CHANGE` | risk_engine | level 0↔1↔2↔3↔4 transitions |
  | `PAPER_FILL_RECORDED` | execution_engine | paper fill confirmed; paper_trade=True flag set |
  | `PAPER_CONFIG_MISSING` | execution_engine | paper_trade=True arrived but ALPACA_PAPER_BASE_URL not set |
  | `KILL_SWITCH_ABORTED_SUBMISSION` | execution_engine | kill switch activated between risk approval and broker submit |
  | `MARKET_CLOSED` | execution_engine | order rejected because calendar.is_trading_day returned False |
  | `CALENDAR_LIMIT_OVERRIDE` | risk_engine | expiry/rebalance day limit override applied |
  | `CALENDARREFRESH_STALE` | scripts/calendar | holiday list not refreshed in > 30 days |

  All events carry: `trace_id`, `signal_id` (where applicable), `event_type`, `timestamp`, `service`, `schema_version`. Retained in S3 trading-logs for compliance audit.

---

## 3. v3 Architectural Changes

### 3.1 Signal Schema Evolution — v5.0 (Cost-Aware Signals)

**Problem:** Signals currently carry direction, price, stop_loss, take_profit, and quantity. The strategy engine knows nothing about the cost of executing the trade. The risk engine cannot distinguish a 20bps edge from a 2bps edge after costs.

**Change:** Evolve `signals.pending` to schema v5.0. The strategy layer must emit a forecast with economic context, not just a direction.

#### New fields in signal_event v5.0

```
signal_event v5.0 (extends v3.0 — backward-compatible new fields)
─────────────────────────────────────────────────────────────────
schema_version        : "5.0"
signal_id             : sha256(strategy_id|symbol|direction|tick_seq_id|timeframe)[:32]
trace_id              : uuid4 (unchanged propagation)
strategy_id           : string (replaces strategy_name — versioned identifier)
strategy_version      : "YYYY-MM-DD" (date of strategy config in production)
alpha_family          : "momentum" | "quality" | "reversal" | "regime_filter"
symbol                : string
instrument_id         : string
market                : "NSE" | "US"
timeframe             : "1m" | "5m" | "15m" | "daily"
direction             : "BUY" | "SELL" (unchanged)

── Cost fields (NEW in v5.0) ─────────────────────────────────────
forecast_return_bps   : float  -- raw expected alpha in basis points
expected_spread_bps   : float  -- half-spread × 2, pre-trade estimate
expected_slippage_bps : float  -- market-impact estimate at target_qty
fees_taxes_bps        : float  -- STT + exchange fees + GST (NSE); SEC + FINRA (US)
net_edge_bps          : float  -- forecast_return - spread - slippage - fees
risk_penalty_bps      : float  -- vol-adjusted cost of position risk

── Position intent (NEW in v5.0) ─────────────────────────────────
target_qty            : int    -- desired position size
target_notional       : float  -- target_qty × decision_price
vol_target_pct        : float  -- fraction of daily vol budget allocated
risk_budget_bps       : float  -- maximum acceptable loss in bps NAV

── Timing (existing + evolved) ────────────────────────────────────
decision_price        : float  -- price at signal creation (renamed from price_at_signal)
decision_ts           : datetime
expires_at            : datetime (existing — unchanged: +30s)
source_tick_seq_id    : int    -- monotonic sequence_id of originating tick

── Regime context (from ai_engine, pass-through) ─────────────────
regime_tags           : list[str]  -- ["trend_on", "vol_ok"] etc.
confidence            : float      (existing — unchanged)
```

**Gateway rule (enforced in strategy_engine before publishing):**
```
if net_edge_bps <= 0:
    do NOT publish to signals.pending
    emit metric: StrategyEngine/SignalSuppressedNegativeEdge
    log: { strategy_id, symbol, direction, net_edge_bps, reason="negative_net_edge" }
```

This rule must live in the strategy layer, not the risk layer. It is an economic filter, not a risk filter.

**Backward compatibility:** The risk engine must handle schema v3.0, v4.0, and v5.0 envelopes during the transition. Existing fallback path (signals.pending → risk_engine direct) accepts schema v3.0. `KafkaEnrichedConsumer` already handles dual-schema parsing — same pattern extends to v5.0. Note: "schema v5.0" refers to the Kafka event schema version, not the platform version.

---

### 3.2 Alpha Sleeve Architecture (Inside Strategy Layer)

**Problem:** The 6 current strategies are isolated classes with no shared interface. Each independently decides forecast, sizing, and timing. There is no mechanism to combine sleeves or enforce consistent economic output format.

**Change:** Introduce a shared `AlphaForecast` DTO and a processing pipeline inside strategy_engine. Each strategy produces an `AlphaForecast`; a `PortfolioBuilder` converts these into v5.0 signals with net_edge_bps computed.

**This is a structural redesign of the strategy layer internals. Service boundary (signals.pending) and all external interfaces remain unchanged.**

#### New internal pipeline (inside strategy_engine service)

```
Market data (tick / candle)
        │
        ▼
  Alpha Scorer (per sleeve)
  ┌──────────────────────────────────────────────────────┐
  │  momentum.py  → AlphaForecast(symbol, raw_bps, ...)  │
  │  quality.py   → AlphaForecast(symbol, raw_bps, ...)  │
  │  reversal.py  → AlphaForecast(symbol, raw_bps, ...)  │
  └──────────────────────────────────────────────────────┘
        │  list[AlphaForecast]
        ▼
  RegimeFilter (risk gate)
  • If regime="volatile" or "crash": suppress non-liquidating signals
  • Uses regime from ai_engine signals.enriched (or fallback DynamoDB regime-log)
        │
        ▼
  CostModel (per instrument, per broker)
  • Reads spread from DynamoDB latest-prices (bid-ask)
  • Reads slippage curve from instruments.yaml (ADV-based)
  • Computes: expected_spread_bps, expected_slippage_bps, fees_taxes_bps
  • Computes: net_edge_bps = raw_bps - spread - slippage - fees
  • Drops forecast if net_edge_bps <= 0
        │
        ▼
  PortfolioBuilder (single instance, portfolio-aware)
  • Receives surviving AlphaForecast list
  • Solves constrained target-position problem:
    maximize Σ weight_i × net_edge_i
             - λ_turnover × turnover
             - λ_concentration × concentration_penalty
  subject to: per_instrument_pct_nav, per_sector_pct_nav,
              gross_exposure, daily_turnover, liquidity_clip
  • Outputs: target_qty per symbol, target_notional, vol_target_pct
        │
        ▼
  KafkaSignalPublisher
  → signals.pending (v5.0 envelope)
```

#### Alpha Sleeve Definitions

| Sleeve | Cadence | Universe | Role | Target version |
|--------|---------|----------|------|----------------|
| `momentum` | Daily / 15m | Liquid NSE large-cap + US ETFs | Core sleeve | v3 Stage B |
| `quality` | Daily/weekly | NSE equities, sector baskets | Core overlay | v3 Stage E |
| `regime_filter` | Daily / intraday vol | Portfolio-level | Risk gate only (not alpha source) | v3 Stage E (wraps existing HMM) |
| `reversal` | Daily / multi-day | Liquid large-cap only | Selective sleeve — research first | v3 Stage E+ |
| `short_horizon` | 5m / 15m | Highest-turnover names only | Phase-two research sleeve | v4 (future) |
| `volatility_scaling` | Portfolio overlay | All sleeves | Risk overlay only (not alpha) | v3 Stage E |

**Design principle: few sleeves, high discipline, explicit net edge.**

#### New modules required inside services/strategy_engine/

```
services/strategy_engine/
  alpha/
    base.py              — AlphaForecast DTO (typed, frozen dataclass)
    momentum.py          — medium-frequency momentum scorer
    quality.py           — quality/profitability ranker  
    reversal.py          — liquid-universe reversal scorer (phase 9+ research)
    regime_filter.py     — regime gating wrapper (reads HMM output)
  cost/
    cost_model.py        — pre-trade cost estimator (spread + slippage + fees)
  portfolio/
    portfolio_builder.py — unified forecast → target position solver
    ensemble.py          — combine sleeve outputs, confidence weighting
  models/
    forecast.py          — AlphaForecast typed DTO
```

#### AlphaForecast DTO

```python
@dataclass(frozen=True)
class AlphaForecast:
    strategy_id: str
    alpha_family: str             # "momentum" | "quality" | "reversal"
    symbol: str
    market: str
    timeframe: str
    direction: Direction
    raw_forecast_bps: float       # raw alpha before cost
    confidence: float
    decision_price: float
    decision_ts: datetime
    source_tick_seq_id: int
    regime_tags: list[str]        # forwarded from ai_engine or []
    # set by CostModel
    expected_spread_bps: float = 0.0
    expected_slippage_bps: float = 0.0
    fees_taxes_bps: float = 0.0
    net_edge_bps: float = 0.0
    # set by PortfolioBuilder
    target_qty: int = 0
    target_notional: float = 0.0
    vol_target_pct: float = 0.0
    risk_budget_bps: float = 0.0
```

---

### 3.3 Broker Capability Matrix (Execution Layer)

**Problem:** Broker-specific order-type restrictions, TIF compatibility rules, and rate limits are currently handled with conditionals scattered in execution_engine code. There is no pre-submission validation. Unsupported order types reach the broker API and fail silently or with opaque errors.

**Change:** Introduce a config-driven broker capability matrix and an order policy engine that validates every order before any broker call is made.

#### New config file: configs/execution.yaml

```yaml
brokers:
  zerodha:
    supports:
      market: true
      limit: true
      stop_loss: true           # Zerodha SL order
      stop_loss_market: true    # Zerodha SL-M order
      cover_order: false        # CO — Phase 8 scope
      bracket_order: false      # BO — Phase 8 scope
      amo: true                 # After-market orders
    tif:
      day: true
      ioc: true
      gtc: false                # Zerodha does not support GTC
    limits:
      orders_per_second: 10
      orders_per_minute: 400
      orders_per_day: 5000
      modifications_per_order: 25
    fill_updates:
      websocket: false
      postback: true            # Kite Postback API (Phase 9 addition)
      poll_fallback: true       # existing BulkOrderPoller
      poll_interval_ms: 300
    default_entry_policy: marketable_limit
    default_exit_policy: stop_loss_market

  alpaca:
    supports:
      market: true
      limit: true
      stop: true
      stop_limit: true
      trailing_stop: true
      oco: true
      bracket: false            # not yet wired
      extended_hours: true
    tif:
      day: true
      gtc: true
      ioc: true
      fok: true
      opg: true
      cls: true
    limits:
      orders_per_minute: 200
      modifications_per_order: unlimited
    fill_updates:
      websocket: true           # Alpaca streaming order events
      poll_fallback: true
      poll_interval_ms: 500
    default_entry_policy: marketable_limit
    default_exit_policy: stop_limit

execution:
  stale_signal_ttl_ms: 750
  cancel_on_ttl_expiry: true
  slicing:
    enabled: true
    max_participation_rate: 0.08   # max 8% of ADV per clip
    min_clip_qty: 1
    max_child_orders: 3
    inter_clip_delay_ms: 50
  retry:
    max_attempts: 3
    backoff_ms: [50, 150, 500]
    retryable_errors:
      - gateway_timeout
      - network_disconnect
      - temporary_unavailable
    non_retryable_errors:
      - invalid_order_type
      - invalid_tif
      - insufficient_margin
      - precision_rejected
  reconciliation:
    poll_if_no_update_ms: 1500
    hard_reconcile_ms: 5000
```

#### New modules inside services/execution_engine/order_manager/

```
services/execution_engine/order_manager/
  broker_capabilities.py   — typed capability matrix loader from execution.yaml
  order_policy.py          — validates order type + TIF + TTL before broker submit
  rate_limiter.py          — per-broker token bucket (replaces global Zerodha limiter)
  router.py                — selects route/order type from policy + capability matrix
  slicer.py                — splits large qty by participation/clip rules
  fill_state.py            — stateful partial-fill tracker (new, see §3.4)
  reconciler.py            — fetches order/fill history on divergence or timeout
```

#### Order validation sequence (pre-submit, inside order_policy.py)

```
Receive approved signal (v5.0)
        │
        ▼
1. TTL check: signal.expires_at > now? → reject + log stale_signal
        │
        ▼
2. Capability check: broker.supports[order_type] AND broker.tif[tif]? → reject + emit policy_violation
        │
        ▼
3. Rate-limit check: token bucket has capacity? → queue briefly or partial-halt based on TTL
        │
        ▼
4. Net-edge re-check at arrival: signal.net_edge_bps > min_threshold? → reject if edge decayed
        │
        ▼
5. Build broker order from policy + capability matrix + slicer
        │
        ▼
6. Submit with idempotent client_order_id
```

**Default execution policies:**
- Liquid entry with positive edge → `marketable_limit` (better price protection than market order)
- Thin / fragile instrument → `passive` or no trade
- Protective exit → `stop_limit` (if broker supports reliably)
- Urgent risk liquidation (kill switch / limit breach) → `market` or aggressive limit

#### Market Order Protection Contracts (v3 non-negotiable)

```
RULE: Market orders for new position entries are PROHIBITED in v3.

Entry         → always marketable_limit (limit at ask ± tolerance for BUY;
                 bid ± tolerance for SELL). Limit price set to arrival_price ±
                 configs/risk_limits.yaml:execution.marketable_limit_tolerance_bps.
Exit          → stop_limit if broker supports; stop_loss_market as fallback.
Emergency     → market or aggressive limit ONLY for Level 4 kill-switch liquidation
                 or forced risk reduction (not discretionary exit).

Rationale: market orders have unbounded slippage in thin NSE names. Marketable
limits cap worst-case fill price and remain consistent with the net_edge_bps model
(which assumes spread cost, not unlimited slippage). A marketable limit that misses
a fill is preferable to a market fill that destroys the net edge.
```

#### Liquidity Gate (step 3.5 — added to order_policy.py validation sequence)

```
Receive approved signal (v5.0)
        │
        ▼
1. TTL check: signal.expires_at > now?              → reject + log stale_signal
2. Capability check: order_type + TIF supported?    → reject + emit policy_violation
3. Rate-limit check: token bucket has capacity?     → queue briefly or partial-halt
3.5. Liquidity check:
     adv_estimate = instruments.yaml[symbol].avg_daily_volume
     max_clip_qty = floor(adv_estimate × execution.slicing.max_participation_rate)
     if target_qty > max_clip_qty:
       target_qty = max_clip_qty
       log: SIGNAL_CLIPPED_FOR_LIQUIDITY { original_qty, clipped_qty, adv_estimate }
       emit to ops.audit: SIGNAL_CLIPPED_FOR_LIQUIDITY
     if target_qty < instruments.yaml[symbol].min_tradeable_qty:
       reject + emit LIQUIDITY_REJECT (do not submit order)
4. Net-edge re-check at arrival: recalculate net_edge_bps with current spread.
   If net_edge_bps ≤ 0 after spread widening at arrival → cancel, emit
   SIGNAL_STALE_NET_EDGE { original_net_edge_bps, recalculated_net_edge_bps }
5. Build broker order from policy + capability matrix + slicer
6. Submit with idempotent client_order_id
```

#### Partial-Fill Handling Contract

```
All partial-fill lifecycle managed via DynamoDB fill-state table (§3.4):

ON partial_fill event received from broker:
  → update fill-state: cum_fill_qty += fill_qty; leaves_qty -= fill_qty
  → publish fill_event v2.0 (partial) to orders.events immediately
  → risk_engine consumes orders.events: updates real-time position to cum_fill_qty
  → emit to ops.audit: PARTIAL_FILL { client_order_id, cum_fill_qty, leaves_qty }

ON max_partial_fill_age_ms exceeded (from configs/risk_limits.yaml):
  → cancel remainder via broker cancel API
  → publish fill_event v2.0 (expired) to orders.events
  → risk_engine updates position to cum_fill_qty only (treats leaves_qty as zero)
  → emit to ops.audit: PARTIAL_FILL_EXPIRED { client_order_id, cum_fill_qty }

ON spread widening detected at arrival (order_policy step 4):
  → if recalculated net_edge_bps ≤ 0: cancel before any broker submit
  → emit SIGNAL_STALE_NET_EDGE; no fill-state entry created (order never placed)
```

---

### 3.4 Fill State Realism + fill_event v2.0

**Problem:** Current `orders.events` schema tracks final fill state only. Partial fills, replace events, and expired orders are not modeled. The execution engine cannot compute implementation shortfall (arrival price vs fill price vs decision price). P&L attribution is incomplete.

**Change:** Expand `orders.events` to carry three price points and stateful partial-fill sequencing. Model order state as an event stream, not a single final status.

#### Three prices per trade (mandatory from v3 forward)

| Price | When set | Purpose |
|-------|---------|---------|
| `decision_price` | At signal generation | Reference alpha was computed against |
| `arrival_price` | When execution_engine begins submitting | Tradable benchmark; base for IS calculation |
| `fill_price` | When broker confirms fill | Actual executed price |

```
implementation_shortfall_bps = (fill_price - arrival_price) / arrival_price × 10000 × direction_sign
cost_decomposition:
  spread_cost_bps       = (arrival_price - decision_price) / decision_price × 10000
  slippage_bps          = (fill_price - arrival_price) / arrival_price × 10000
  expected_slippage_bps = from signal.expected_slippage_bps
  slippage_error_bps    = slippage_bps - expected_slippage_bps
```

#### fill_event schema v2.0 (orders.events topic — backward-compatible evolution)

```json
{
  "schema_version": "2.0",
  "trace_id": "uuid",
  "signal_id": "hash",
  "strategy_id": "momentum_v1",
  "client_order_id": "uuid",
  "broker_order_id": "string",
  "broker_event_type": "partial_fill | fill | canceled | expired | replaced | done_for_day",
  "order_type": "limit | market | stop_limit",
  "time_in_force": "day | ioc | gtc",
  "venue": "NSE | NASDAQ",
  "symbol": "string",
  "instrument_id": "string",
  "event_ts": "ISO8601",
  "fill_seq": 1,
  "fill_price": 1524.10,
  "fill_qty": 40,
  "cum_fill_qty": 80,
  "leaves_qty": 40,
  "decision_price": 1523.45,
  "arrival_price": 1523.72,
  "fee_amount": 11.8,
  "slippage_bps": 2.49,
  "implementation_shortfall_bps": 4.21,
  "net_edge_bps_at_signal": 12.2,
  "paper_trade": false
}
```

#### New fill_state.py — stateful partial-fill tracker

```
OrderFillState (per client_order_id):
  status:        PENDING → PLACED → PARTIALLY_FILLED → FILLED | CANCELED | EXPIRED | REPLACED
  cum_fill_qty:  cumulative filled quantity
  leaves_qty:    outstanding quantity
  fills:         list[FillEvent] (each partial fill)
  decision_price: from signal
  arrival_price:  set when order is submitted to broker
  final_is_bps:   computed on terminal event (FILLED / CANCELED)
```

**Risk engine consumption (orders.events):** The risk engine already consumes orders.events for P&L. v3 evolves this consumption to:
1. Track `cum_fill_qty` for real-time position updates (not just terminal fill)
2. Compute `implementation_shortfall_bps` per trade
3. Publish `TradingSystem/ImplementationShortfallBps` to CloudWatch per strategy

---

### 3.5 Simulation Stack — Four Modes

**Problem:** The current backtester uses mid-price fills with no cost model. This produces optimistic performance estimates. Paper trading alone is insufficient validation — Alpaca's own documentation states it omits market impact, information leakage, latency slippage, queue position, and dividends.

**Change:** Build a four-mode simulation stack. Each mode uses a progressively more realistic fill model.

#### Simulation Modes

| Mode | Fill Rule | Use | When |
|------|----------|-----|------|
| **Fast Research** | Mid / last price ± spread penalty | Early idea triage | Local dev |
| **Cost-Aware Backtest** | Arrival price + spread curve + slippage curve + TTL expiration + fees | Standard research | Before paper |
| **Replay Simulation** | Historical quotes/trades with event timing and order book depth | Pre-promotion | After cost-aware passes |
| **Paper Trading** | Broker paper environment (existing) | API + order-state validation | Carried over from v1 |
| **Shadow Trading** | Live signal generation, zero capital, slippage and latency measured | Final production rehearsal | Before seed live |

**Promotion gate requires all modes to pass in sequence.** A strategy cannot skip from fast research to paper.

#### Backtest data quality requirements

```
Instrument identity key: "{exchange}#{tradingsymbol}"   (not instrument_token — tokens reuse)
Required offline components:

tools/backtest/
  build_identity_map.py          — symbol/token mapping history, prevent token-reuse errors
  corporate_actions_sync.py      — pull splits/dividends/actions, maintain adjustment ledger
  data_quality_audit.py          — check missing bars, duplicate ticks, bad timestamps; fail dataset before research
  cost_model_calibration.py      — calibrate slippage curve vs realized fills

tests/replay/
  test_strategy_replay_determinism.py   — same event stream → same signals (determinism proof)
  test_restart_mid_session.py           — restart does not duplicate orders
  test_stale_signal_drop.py             — old signals not executed during replay lag
  test_corporate_action_replay.py       — re-run sessions across split/rename boundaries
```

#### Validation Scorecard (required fields for promotion decision)

| Metric | Required value | Notes |
|--------|---------------|-------|
| In-sample Sharpe | > 1.5 | After all explicit costs |
| Out-of-sample Sharpe | > 1.0 | After all explicit costs |
| Deflated Sharpe Ratio (DSR) | Positive, comfortably above zero | Corrects for multiple testing |
| Probability of Backtest Overfitting (PBO) | < 20% | Via combinatorial cross-validation |
| Turnover | Within declared range | Strategy-specific |
| Implementation Shortfall (avg) | < 25% of model expectation | i.e. model must be calibrated |
| Capacity Estimate | > 3× intended capital | Liquidity headroom |
| Max Drawdown in validation | Within predeclared tolerance | Not hard threshold |
| Trade count | > 100 OOS trades | Statistical significance |
| Regime stability | Edge positive across ≥ 3 of 4 regimes | Not regime-dependent alpha |

#### Data Quality Contracts (v3 additions)

**Missing candle detection:**
```
DynamoCandleConsumer computes expected candles per window:
  1m strategy: 3min lookback → expect 3 candles per symbol per interval
  5m strategy: 10min lookback → expect 2 candles per symbol per interval
  15m strategy: 30min lookback → expect 2 candles per symbol per interval

On gap detection (expected > received for any symbol/interval):
  1. Log: DataQuality/CandleGapDetected { market, symbol, interval, expected, received }
  2. Emit CloudWatch metric: DataIngestion/CandleGapCount (per symbol, per interval)
  3. Suppress signal generation for the affected symbol (do not call strategy.on_bar)
  4. If gap persists > 5min for a symbol → set symbol-level PARTIAL_HALT (Level 1)
  5. If gap persists > 15min for ≥ 3 symbols → escalate to ops alert (data_ingestion failure suspected)
```

**CostModel API failure fallbacks:**
```
CostModel reads live spread from DynamoDB latest-prices.
On read failure OR stale record (age > 30s):
  → use instruments.yaml[symbol].default_spread_bps (static conservative estimate)
  → set signal.expected_spread_bps = default_spread_bps
  → log: CostModel/SpreadFallback { symbol, reason, fallback_spread_bps }
  → recalculate net_edge_bps with fallback spread; if ≤ 0 → suppress signal
  → CloudWatch metric: CostModel/SpreadFallbackCount

On instruments.yaml missing a symbol (new instrument not yet configured):
  → reject AlphaForecast with INSTRUMENT_NOT_CONFIGURED
  → do not publish to signals.pending
```

**Corporate action / bad price guard:**
```
data_quality_audit.py runs pre-session (before market open each day):
  → check for prices > 2× or < 0.5× previous close (corporate action signal)
  → check for zero-volume candles during normal market hours
  → check for duplicate timestamps in tick stream
  → check instrument_token is in current identity map (not stale token from reuse)
  → FAIL dataset if any check fails: strategy for that symbol NOT dispatched until clean
  → log: DataQualityAudit/Failed { symbol, check_failed, value }
  → alert ops if > 5 symbols fail the same day (likely data feed problem)

build_identity_map.py (run weekly offline):
  → maps {exchange}#{tradingsymbol} → token history (prevents token-reuse errors)

corporate_actions_sync.py (run daily before 08:00 IST):
  → pulls splits, dividends, bonus issues from NSE corporate actions API
  → maintains adjustment ledger in S3: ohlcv-data/corporate-actions/{symbol}/{date}.json
  → strategy uses adjusted prices; unadjusted price retained for order placement
```

#### Live = Backtest Alignment Contract

**Rule:** The strategy engine, CostModel, and feature computation MUST use the SAME code path for live trading and backtesting. No parallel implementations.

```
Alignment invariants:
  1. CostModel.compute(forecast, data_provider) is identical in live and backtest.
     Only the DataProvider implementation differs (DynamoDB live vs S3 historical).
  2. AlphaForecast produced from tick X in live == AlphaForecast from same tick X in replay.
  3. PortfolioBuilder.solve(forecasts, constraints) is deterministic:
       same inputs → same target_qty → same signal_event v5.0 (zero tolerance).
  4. Signal suppression gate (net_edge_bps ≤ 0) applies identically in both modes.
  5. Adjusted price computation uses the same corporate_actions_sync ledger in both modes.

DataProvider interface (enforces alignment by construction):
  class DataProvider(Protocol):
      def get_spread_bps(self, symbol: str, ts: datetime) -> float: ...
      def get_adv(self, symbol: str, date: date) -> float: ...
      def get_regime_tags(self, symbol: str, ts: datetime) -> list[str]: ...

  LiveDataProvider    → DynamoDB latest-prices, regime-log (real-time)
  BacktestDataProvider → S3 Parquet snapshots at simulation timestamp

  Same CostModel code. Same PortfolioBuilder code. Same suppression gate.
  Alignment is structural, not a test-only assertion.

CI enforcement:
  test_live_backtest_alignment.py:
    Given: identical sequence of ticks + fixture DataProvider responses
    Assert: identical AlphaForecast list, identical CostModel output,
            identical PortfolioBuilder target_qty, identical signal_event v5.0
    Tolerance: zero (deterministic; no floating-point divergence permitted)
```

---

### 3.6 Position Reconciliation Gate — BLOCKER

**Problem (confirmed BLOCKER by review):** The execution engine currently places orders without a hard gate on whether internal positions match broker-reported positions. A position mismatch is logged but trading continues. This makes capital scaling unsafe.

**Change:** Add a pre-order-entry position reconciliation check. If internal vs broker position diverges beyond tolerance, freeze new signal entry until an operator explicitly acks or reconciliation succeeds.

#### Reconciliation architecture

```
Five-point intraday reconciliation (runs every 5 minutes + on every service startup):
  1. internal_positions     = DynamoDB positions table (QuantEmbrace state)
  2. broker_positions       = Zerodha kite.positions() / Alpaca get_all_positions()
  3. cumulative_fills       = DynamoDB orders table (sum of fill quantities)
  4. pending_open_orders    = Zerodha/Alpaca open orders API
  5. cash_margin_delta      = broker account balance vs expected (based on fills)

Reconciliation result → DynamoDB risk-state table:
  reconciliation_status:  CLEAN | DRIFT_WARN | DRIFT_HALT
  last_reconciled_at:     timestamp
  drift_details:          list[{ symbol, internal_qty, broker_qty, delta }]
```

#### Reconciliation gate enforcement

```
Before any order placement (inside execution_engine order_policy.py):
  if risk_state.reconciliation_status == DRIFT_HALT:
    → halt ALL new position-opening orders
    → allow position-closing (risk-reducing) orders only
    → publish RECONCILIATION_HALT to ops.audit
    → require operator ack via scripts/ops/reconcile.py --ack
```

#### New config: configs/risk_limits.yaml

```yaml
reconciliation:
  tolerance_qty_absolute: 0        # zero tolerance for integer quantity drift
  tolerance_notional_pct: 0.001    # 0.1% notional tolerance for FX rounding
  auto_halt_on_drift: true
  halt_requires_operator_ack: true
  drift_warn_threshold_pct: 0.01   # log WARNING but don't halt

global:
  max_gross_exposure_pct_nav: 130
  max_net_exposure_pct_nav: 60
  max_open_orders: 120
  intraday_loss_limit_pct_nav: 1.00
  daily_loss_limit_pct_nav: 1.75
  max_child_order_retries: 3
  kill_switch_on_reconciliation_break: true

universe:
  per_instrument_pct_nav: 4.0
  per_sector_pct_nav: 18.0
  per_strategy_pct_nav: 35.0
  max_turnover_pct_nav_day: 20.0

execution:
  max_slippage_bps_per_order: 15
  max_arrival_delay_ms: 500
  max_partial_fill_age_ms: 30000
  min_net_edge_bps_at_arrival: 2.0   # re-check at arrival; cancel if edge gone

strategy_overrides:
  momentum_v1:
    per_instrument_pct_nav: 3.0
    max_turnover_pct_nav_day: 12.0
  quality_overlay_v1:
    per_instrument_pct_nav: 2.0
    max_turnover_pct_nav_day: 8.0
```

All existing validators in risk_engine must be refactored to read limits from this file, not hard-coded values. No per-strategy limit should live in strategy code.

#### Risk Validator Sequence (all active in v3 — enumerated explicitly)

All 10 validators run in this sequence for every signal. No validator may be skipped or reordered. All limits read from `configs/risk_limits.yaml` (not hardcoded).

```
Receive signal from signals.pending OR signals.enriched (fallback path)
        │
        ▼
Validator 1: kill_switch_check
  → reads DynamoDB risk-state.kill_switch (ConsistentRead=True — never stale)
  → if ACTIVE: reject immediately, publish KILL_SWITCH_REJECTED to ops.audit

Validator 2: degradation_level_check  [NEW in v3]
  → reads DynamoDB risk-state.degradation_level (ConsistentRead=True)
  → if Level 2 (ENTRY_BLOCKED): reject new position-opening signals; allow exits
  → if Level 3 (RECONCILIATION_HALT): reject ALL new signals (even exits)
  → if Level 4 (KILL_SWITCH): handled by Validator 1 above

Validator 3: reconciliation_status_check  [NEW in v3]
  → reads DynamoDB risk-state.reconciliation_status
  → if DRIFT_HALT: reject any signal that would open or increase a position
  → allow position-closing signals (risk-reducing only)

Validator 4: position_limit_check
  → reads positions table (ConsistentRead=True for the signal's symbol)
  → checks per_instrument_pct_nav against risk_limits.yaml (global + strategy override)

Validator 5: exposure_check
  → checks current_gross_exposure_pct_nav ≤ max_gross_exposure_pct_nav
  → checks current_net_exposure_pct_nav ≤ max_net_exposure_pct_nav
  → checks per_sector_pct_nav for signal's sector

Validator 6: net_edge_check  [NEW in v3]
  → reads signal.net_edge_bps (v5.0 field; absent/null in v3.0/v4.0 → skip)
  → if net_edge_bps ≤ 0.0: reject with NEGATIVE_NET_EDGE
  → purpose: safety net; strategy layer should have suppressed, but risk is the backstop

Validator 7: stop_loss_check
  → validates stop_loss and take_profit are present, rational (stop > 0, TP > SL for BUY)
  → validates risk_budget_bps is present (v5.0) and positive

Validator 8: drawdown_check
  → reads risk-state.daily_pnl_{date}
  → if |daily_pnl| >= intraday_loss_limit_pct_nav × NAV: reject + emit LOSS_LIMIT_BREACH
  → if |daily_pnl| >= daily_loss_limit_pct_nav × NAV: reject + trigger Level 4

Validator 9: instrument_limit_check
  → checks per_instrument_pct_nav (strategy-specific override if present)
  → checks per_sector_pct_nav for signal's GICS sector
  → checks per_strategy_pct_nav (total exposure under this strategy_id)

Validator 10: margin_check
  → validates signal.target_notional ≤ available_margin (from broker account snapshot)
  → uses cached broker balance (refreshed every 60s); if cache stale > 5min → reject

All rejections:
  → event published to ops.audit topic: SIGNAL_REJECTED
    { validator_id, reason, signal_id, strategy_id, symbol, net_edge_bps }
  → signal is NOT published to signals.approved
  → logged with full signal context for compliance audit
```

---

### 3.7 Formal Promotion Ladder — 6-Stage Governance

**Problem:** The current go-live process is a single gate: 5-day paper validation + operator CLI command. This is insufficient for capital scaling. There is no shadow trading stage, no cost-adjusted out-of-sample validation requirement, and no formal scorecard.

**Change:** Introduce a 6-stage formal promotion ladder. Every strategy must pass each stage before advancing. No stage can be skipped.

#### Promotion Stages

```
Stage 0: RESEARCH
  Entry: Any new alpha idea
  Evidence required:
    • Cost-aware backtest with DSR + PBO scorecard completed
    • OOS Sharpe > 1.0 after all explicit costs
    • PBO < 20%
    • Implementation shortfall model calibrated vs historical data
  Exit gate: Chief Architect + Quant review of scorecard
  
Stage 1: REPLAY
  Entry: Research scorecard passed
  Evidence required:
    • Deterministic replay test: same event stream → same signals (100%)
    • Restart-mid-session test: no duplicate orders
    • Stale-signal-drop test: signals expired during lag not executed
    • Corporate-action replay: sessions across split/rename boundaries pass
  Exit gate: Automated CI test suite (all replay tests green)

Stage 2: PAPER
  Entry: Replay stage passed
  Evidence required:
    • ≥ 5 consecutive paper trading sessions
    • No P0/P1 alarms during any session
    • CandleBarsProcessed > 0 for all candle strategies (no silent failures)
    • PaperSignalsByStrategy within expected range (not zero, not runaway)
    • No CircuitBreakerOpen alarms
    • Broker API lifecycle validated: partial fills, replacements, cancellations handled
  Exit gate: paper_session_report.py READY for 5 consecutive sessions

Stage 3: SHADOW
  Entry: Paper stage passed
  Evidence required:
    • ≥ 5 trading days of live signal generation (zero capital)
    • Paper-to-shadow fill-rate gap < 10 percentage points
    • Shadow vs paper signal divergence < 5% (same market conditions)
    • Implementation shortfall measured: expected vs realized slippage error < 25%
    • Latency measurements within model assumptions (arrival_delay < max_arrival_delay_ms)
  Exit gate: Shadow session report for 5 days, reviewed by operator

Stage 4: SEED LIVE
  Entry: Shadow stage passed
  Evidence required:
    • Tiny capital deployment (predefined minimum position size only)
    • Daily reconciliation: zero-defect intraday and EOD
    • Order reject rate < 1-2% under normal conditions
    • Strategy drawdown within predeclared tolerance
    • Slippage thresholds not breached for 5 sessions
  Exit gate: Daily operator review; kill-switch drills completed

Stage 5: RAMP
  Entry: Seed live stage stable for ≥ 5 sessions
  Evidence required:
    • Live metrics consistent with seed window (no regression)
    • Gradual capital increase schedule defined (not more than 25% increase per week)
    • P&L attribution computed at trade + strategy + day level
  Exit gate: Weekly review vs scorecard metrics
```

#### DynamoDB strategy-config evolution (new fields)

```
{prefix}-strategy-config table additions (v3):
  promotion_stage:          RESEARCH | REPLAY | PAPER | SHADOW | SEED_LIVE | RAMP | LIVE
  scorecard_approved:       bool (set by Chief Architect after scorecard review)
  oos_sharpe:               float (from backtest scorecard)
  deflated_sharpe:          float
  pbo_pct:                  float
  paper_days_passed:        int
  shadow_days_passed:       int
  seed_live_days_passed:    int
  last_scorecard_reviewed:  datetime
  scorecard_reviewer:       string
```

---

### 3.8 Decision-Quality Observability — 5 New Dashboards

**Problem:** Current monitoring tracks system health (CPU, memory, Kafka lag, WebSocket uptime). It does not show whether the platform is making money for the right reasons. Edge decay, implementation drag, and alpha decay are invisible until P&L collapses.

**Change:** Add five decision-quality dashboards to the existing Grafana infrastructure.

#### Dashboard 1: Strategy Efficacy (infra/monitoring/grafana/strategy.json)

| Panel | Metric | Alert threshold |
|-------|--------|----------------|
| Hit rate per strategy | % signals that resulted in profitable trades (after costs) | < 50% for 2 consecutive days |
| Information Coefficient | Correlation of forecast direction with next-bar return | IC < 0.05 for 5 sessions |
| Average net edge at signal | avg(net_edge_bps) across published signals | < 3 bps avg |
| Realized edge at fill | avg(fill_pnl_bps - expected_cost_bps) | Consistently below expected |
| Turnover per strategy | Daily notional traded / NAV | Above declared strategy limit |
| Signal suppression rate | % signals dropped due to net_edge_bps ≤ 0 | > 80% suppression rate (alpha gone) |
| Edge decay by time-of-day | Realized edge vs time bucket (09:15/11:00/13:00/15:00) | Edge negative in any bucket |

**New CloudWatch metrics required:**
- `StrategyEngine/SignalNetEdgeBps` (per strategy_id)
- `StrategyEngine/SignalSuppressedNegativeEdge` (count, per strategy_id)
- `ExecutionEngine/RealizedEdgeBps` (per strategy_id, computed at fill)

#### Dashboard 2: Execution Quality (infra/monitoring/grafana/execution.json)

| Panel | Metric | Alert threshold |
|-------|--------|----------------|
| Fill rate by order type | % orders filled within TTL (limit vs market) | < 85% fill rate for limit orders |
| Order reject rate | % orders rejected by broker | > 2% |
| Slippage vs arrival price | fill_price - arrival_price in bps | > max_slippage_bps_per_order |
| Implementation shortfall | IS_bps = slippage + spread + fees vs modeled | Realized IS > 125% of model |
| Partial fill ratio | % orders that were partially filled | > 30% (indicates sizing too large) |
| Cancel/replace counts | Orders modified after submission | Spike alert |
| Hourly fill rate | % filled by hour (09:15, 09:30, 10:00…) | — (pattern monitoring) |

**New CloudWatch metrics required:**
- `ExecutionEngine/ImplementationShortfallBps` (per strategy_id)
- `ExecutionEngine/FillRateByOrderType` (per order_type)
- `ExecutionEngine/SlippageBpsVsModel` (realized vs expected)

#### Dashboard 3: Risk (infra/monitoring/grafana/risk.json)

Extends existing risk monitoring to add:

| Panel | Metric | Alert threshold |
|-------|--------|----------------|
| Gross/net exposure % NAV | — | Any hard limit breach |
| Per-strategy exposure | — | Per-strategy limit breach |
| Sector concentration | — | Per-sector limit breach |
| Unreconciled positions | Count of symbols with position mismatch | > 0 → DRIFT_HALT |
| Implementation shortfall vs risk budget | IS_bps vs signal.risk_budget_bps | IS > risk budget |
| Daily drawdown vs kill switch | — | Existing alarm (unchanged) |

#### Dashboard 4: Kafka / Platform (infra/monitoring/grafana/platform.json)

Existing system-health metrics — no new panels required. Confirm existing alarms cover:
- Consumer lag per topic/group
- Hot partition > 30%
- Write failure (immediate)
- WebSocket gap (kill switch trigger)

#### Dashboard 5: Promotion Governance (infra/monitoring/grafana/promotion.json)

| Panel | Metric | Alert threshold |
|-------|--------|----------------|
| Strategy promotion stage | RESEARCH / REPLAY / PAPER / SHADOW / SEED / RAMP | — (status panel) |
| OOS Sharpe by strategy | From scorecard | Below 1.0 |
| DSR by strategy | From scorecard | Negative |
| PBO % by strategy | From scorecard | > 20% |
| Paper-to-shadow fill gap | abs(paper_fill_rate - shadow_fill_rate) | > 10 pp |
| Slippage model accuracy | (realized IS - modeled IS) / modeled IS | > 25% |
| Days at current stage | Time in stage without advancing | > 10 days (stalled review) |
| Scorecard review status | approved / pending / blocked | — (status panel) |

---

### 3.9 Graduated Kill-Switch Degradation Hierarchy

**Problem:** Current kill switch is binary (ACTIVE / INACTIVE). There is no graduated response to partial system degradation (e.g., Kafka lag spike, exposure breach, reconciliation drift, single-broker failure).

**Change:** Introduce a 4-level degradation hierarchy with distinct operational modes.

#### Degradation Levels

```
Level 0: TRADING (normal)
  All systems green. Full signal processing, all order types allowed.

Level 1: PARTIAL_HALT (Kafka lag OR stale ticks)
  Trigger: Kafka consumer lag > policy threshold
  Action:  Drop stale ticks (age > max_tick_age_ms)
           Suppress signals for instruments with stale data
           Allow existing open positions to fill or expire
  Operator notification: WARNING alert

Level 2: ENTRY_BLOCKED (Exposure breach)
  Trigger: gross_exposure > max_gross OR net_exposure > max_net
           OR per_instrument > per_instrument_pct_nav
  Action:  Block all new position-opening orders
           Allow risk-reducing exits (position-closing) only
           Alerts ops immediately
  Recovery: Automatic when exposure returns within limits

Level 3: RECONCILIATION_HALT (Broker drift)
  Trigger: reconciliation_status == DRIFT_HALT
           OR broker heartbeat / API connectivity lost > 30s
  Action:  Block ALL new orders (including exits)
           Enter reconciliation mode (poll broker until clean)
           Alert ops; require manual ack to resume
  Recovery: Operator ack via scripts/ops/reconcile.py --ack

Level 4: KILL_SWITCH (Global halt)
  Trigger: Daily loss breach OR manual activation
           OR Kafka write failures (SQLite outbox overflow)
  Action:  Cancel ALL open orders
           Halt ALL new order placement
           Propagate via DynamoDB + Kafka kill.switch topic (existing)
  Recovery: Manual operator deactivation (existing CLI)
```

#### DynamoDB risk-state additions

```
degradation_level:    0 | 1 | 2 | 3 | 4
degradation_reason:   string (human-readable)
degradation_since:    datetime
entry_blocked:        bool (Level 2+)
reconciliation_halt:  bool (Level 3+)
kill_switch:          ACTIVE | INACTIVE (existing — Level 4)
```

#### Kill Switch Coverage for All New v3 Code Paths

v3 introduces new components (PortfolioBuilder, CostModel, shadow harness, OrderSlicer, capability router). Every new component that produces orders or signals MUST respect the kill switch. Explicit contract:

```
PortfolioBuilder.solve()
  → check degradation_level before producing any target positions
  → if degradation_level >= 3 (RECONCILIATION_HALT): return empty position list
  → if kill_switch == ACTIVE: return empty position list
  → PortfolioBuilder MUST NOT produce target_qty for suppressed instruments

OrderSlicer (when slicing large orders into child orders):
  → re-check kill_switch status before submitting EACH child order
  → if kill_switch activated mid-slice: cancel all remaining child orders immediately
  → do not wait for the current child to fill before checking

ShadowTradingHarness:
  → check kill_switch before publishing to signals.shadow topic
  → if ACTIVE: suppress shadow signals (shadow mode must not create a false sense
     of ongoing signal generation when the live system is halted)

CostModel.compute():
  → pure computation; no kill switch check needed (no side effects, no I/O)

BrokerCapabilityRouter / order_policy.py:
  → kill switch check is Validator 1 (upstream in risk_engine)
  → additionally: execution_engine re-checks DynamoDB risk-state.kill_switch
     (ConsistentRead=True) immediately before every broker API call as a last-resort gate
  → if kill_switch turns ACTIVE between risk approval and broker submit:
     abort submit, publish KILL_SWITCH_ABORTED_SUBMISSION to ops.audit

Reconciler.reconcile():
  → not blocked by kill switch (reconciliation is the recovery path for Level 3)
  → IS blocked by network/broker API outage (handled by Level 3 trigger itself)

NON-NEGOTIABLE RULE:
  No new v3 code may route around order_policy.py.
  All orders must enter execution through order_policy.py step 1 (TTL) through step 6 (submit).
  Adding any "fast path" that bypasses TTL, capability, or kill-switch check is a critical defect.
```

---

### 3.10 Paper Trading Isolation Contract (v3 Non-Negotiable)

**Problem:** The paper_trade flag is carried on signals and controls broker routing. Without an explicit isolation contract, a misconfiguration or code defect could cause a paper-mode signal to reach a live broker. This is a capital-safety defect.

**v3 isolation guarantee:**

```
RULE: paper_trade=True signals MUST route ONLY to the Alpaca paper API endpoint.
      paper_trade=True signals MUST NEVER touch Zerodha live API or Alpaca live API.

Enforcement architecture (inside execution_engine):
  Startup: execution_engine instantiates TWO separate BrokerClient instances:
    paper_broker = AlpacaBrokerClient(
        base_url  = env: ALPACA_PAPER_BASE_URL,      # e.g. https://paper-api.alpaca.markets
        api_key   = Secrets Manager: /quantembrace/{env}/alpaca/paper/api_key,
        api_secret= Secrets Manager: /quantembrace/{env}/alpaca/paper/api_secret,
    )
    live_nse_broker  = ZerodhaBrokerClient(credentials from /zerodha/*)
    live_us_broker   = AlpacaBrokerClient(credentials from /alpaca/live/*)

  Routing decision in execution_engine (after order_policy.py step 6):
    if signal.paper_trade == True:
        submit to paper_broker (Alpaca paper endpoint)
    else:
        route via broker capability matrix (live_nse_broker or live_us_broker)

  Fail-safe rule:
    If paper_trade=True arrives but ALPACA_PAPER_BASE_URL is not configured:
      → REJECT the signal with PAPER_CONFIG_MISSING
      → emit to ops.audit: PAPER_CONFIG_MISSING { signal_id, strategy_id }
      → alert ops immediately
      → DO NOT fall through to live_broker under any circumstances

  Kill switch applies equally to paper signals:
    → paper fills respect kill switch (no new paper orders if ACTIVE)
    → paper reconciliation is separate from live position reconciliation

  P&L isolation:
    → risk_engine does NOT count paper fills toward real NAV
    → paper fills stored in DynamoDB with paper_trade=True flag
    → paper P&L tracked in a separate risk-state key: daily_paper_pnl_{date}
    → live P&L tracking (daily_pnl_{date}) is never updated by paper fills

  Credential separation:
    → paper credentials (/alpaca/paper/*) and live credentials (/alpaca/live/*) are
       separate Secrets Manager paths; they cannot be the same value
    → CI test: assert ALPACA_PAPER_BASE_URL != ALPACA_LIVE_BASE_URL
```

---

### 3.11 NSE/US Market Calendar

**Problem:** Signal generation, order routing, and reconciliation all depend on market hours and trading calendars. Orders sent outside market hours, on NSE holidays, or on expiry day without posture adjustment cause unnecessary rejections and capital risk.

**v3 requirements:**

#### Calendar configuration: configs/calendar.yaml

```yaml
nse:
  session_open_ist:           "09:15"
  session_close_ist:          "15:30"
  pre_open_start_ist:         "09:00"
  amo_window_start_ist:       "17:00"    # after-market order window opens
  amo_window_end_ist:         "08:59"    # closes before pre-open
  holiday_source:             "NSE_OFFICIAL"   # authoritative: NSE bhavcopy + NSEpy
  holiday_cache_table:        "{prefix}-calendar-cache"
  holiday_refresh_days:       7          # re-fetch every 7 days
  monthly_expiry:             "last_thursday"  # NSE F&O monthly expiry
  weekly_expiry_banknifty:    "wednesday"      # Bank Nifty weekly expiry
  rebalance_lookback_days:    2          # Nifty 50 index rebalance: tighten limits ±2d

us:
  session_open_et:            "09:30"
  session_close_et:           "16:00"
  holiday_source:             "NYSE_CALENDAR"      # via pandas_market_calendars
  half_day_source:            "NYSE_EARLY_CLOSE"   # handle early-close dates

safety:
  block_orders_before_open_ms:    60000   # suppress signals until 60s after session open
  block_orders_after_close_ms:    60000   # suppress signals from 60s before session close
  expiry_day_position_limit_pct:  50      # halve per-instrument NAV limit on expiry day
  rebalance_day_position_limit_pct: 75    # reduce limit in rebalance window
  expiry_new_position_cutoff_ist: "14:30" # no new F&O positions in expiring contract after this
```

#### TradingCalendar interface (shared utility — used by strategy_engine, execution_engine, risk_engine)

```python
class TradingCalendar(Protocol):
    def is_trading_day(self, market: str, date: date) -> bool: ...
    def is_open_now(self, market: str, ts: datetime) -> bool: ...
    def next_trading_day(self, market: str, date: date) -> date: ...
    def is_expiry_day(self, market: str, instrument: str, date: date) -> bool: ...
    def is_rebalance_window(self, market: str, date: date) -> bool: ...
    def get_session_hours(self, market: str, date: date) -> tuple[datetime, datetime]: ...
    def is_amo_window(self, market: str, ts: datetime) -> bool: ...
```

#### Calendar enforcement per service

```
strategy_engine — before dispatching any tick or candle event:
  if not calendar.is_open_now(market, utcnow()):
    suppress tick/candle dispatch (do not call strategy.on_tick / on_bar)
    emit CloudWatch metric: TradingCalendar/OutsideMarketHours { market }
    (normal — happens at session open/close; only alert if during expected trading hours)

execution_engine — before order submission (step 0 in order_policy.py):
  if not calendar.is_trading_day(market, today):
    reject with MARKET_CLOSED
    route to AMO only if: broker.supports.amo == true AND strategy is AMO-eligible
  if calendar.is_expiry_day(market, instrument, today):
    apply expiry_day_position_limit_pct override from calendar.yaml
    if time_ist > expiry_new_position_cutoff_ist (14:30):
      block new F&O positions in the expiring contract
  if calendar.is_open_now returns False but signal.expires_at > now:
    reject immediately (signal will expire before market opens)

risk_engine — limit overrides on special days:
  if calendar.is_expiry_day: apply reduced per_instrument_pct_nav
  if calendar.is_rebalance_window: apply reduced per_instrument_pct_nav
  Both overrides read from configs/calendar.yaml (not hardcoded)
```

#### Holiday list refresh (offline script)

```
scripts/calendar/refresh_holidays.py --market NSE --env production
  → fetches NSE holiday list from official NSE bhavcopy endpoint
  → cross-validates against pandas_market_calendars NSE calendar
  → writes to DynamoDB: {prefix}-calendar-cache
      PK = MARKET#NSE, SK = HOLIDAYS#{year}
      TTL = 400 days (auto-expire after 13 months)
  → emits CloudWatch metric: CalendarCacheRefreshed { market, year, holiday_count }
  → if holiday list unchanged for > 30 days (possible fetch failure):
      alert ops: CalendarRefreshStale
```

---

### 3.12 Secrets Management (v3 Non-Negotiable Invariant)

**Rule:** All broker API keys, secrets, tokens, and credentials MUST be sourced from AWS Secrets Manager at service startup. They MUST NEVER appear in:

- Source code (any `.py`, `.yaml`, `.json`, `.env`, `.sh` file)
- Config files (`execution.yaml`, `risk_limits.yaml`, `instruments.yaml`, etc.)
- Environment variable defaults in `Dockerfile` or `docker-compose.yaml`
- Git history (commits, branches, stashes)
- CloudWatch Logs or any other log output

#### Secrets layout in AWS Secrets Manager

```
/quantembrace/{env}/zerodha/api_key
/quantembrace/{env}/zerodha/api_secret
/quantembrace/{env}/zerodha/access_token        # refreshed daily before 03:45 UTC
/quantembrace/{env}/alpaca/live/api_key
/quantembrace/{env}/alpaca/live/api_secret
/quantembrace/{env}/alpaca/paper/api_key
/quantembrace/{env}/alpaca/paper/api_secret
/quantembrace/{env}/kafka/sasl_password         # if applicable for MSK auth rotation
```

`{env}` = `development` | `staging` | `production`. Each environment uses strictly separate secrets.

#### Service startup contract

```
Every service MUST:
  1. Load required credentials from AWS Secrets Manager at startup via boto3 secrets client
  2. Fail fast (exit 1) if any required secret is missing, unparseable, or access denied
  3. NEVER accept credentials from environment variables directly for broker secrets
     (env vars may carry AWS_REGION, DYNAMODB_TABLE_PREFIX etc. — not broker keys)
  4. Never log credential values — mask in all log output:
       log.info("Loaded Zerodha credentials", api_key=api_key[:4] + "****")
  5. Refresh Zerodha access_token daily before 03:45 UTC via scripts/zerodha_login.py
     → token_refresh script writes new token to Secrets Manager path above
     → execution_engine polls for token refresh every 60s via a dedicated task

Zerodha daily token refresh flow:
  scripts/zerodha_login.py (runs as a scheduled task on the execution_engine host):
    → OAuth login via Zerodha Kite Connect (automated with stored TOTP seed)
    → writes new access_token to /quantembrace/{env}/zerodha/access_token
    → execution_engine picks up new token on next 60s poll cycle
    → old token becomes invalid automatically (Zerodha single-session model)
```

#### CI/CD enforcement

```
Every PR must pass:
  trufflehog scan:   trufflehog git --since-commit HEAD~1 --no-update
                     → fails CI on any credential pattern detected in changed files
  git-secrets check: git secrets --scan-history (enforced in pre-commit hook)
  Pattern blocklist: AKIA*, ZerodhaAPIKey, alpaca_api_key, api_secret=, password= in code
```

---

## 4. New Kafka Schemas Summary

| Schema | Topic | Key Changes |
|--------|-------|------------|
| signal_event **v5.0** | signals.pending | +net_edge_bps, +forecast_return_bps, +expected_cost_bps, +target_notional, +vol_target_pct, +risk_budget_bps, +alpha_family, +strategy_version, +source_tick_seq_id |
| fill_event **v2.0** | orders.events | +broker_event_type, +fill_seq, +cum_fill_qty, +leaves_qty, +decision_price, +arrival_price, +slippage_bps, +implementation_shortfall_bps, +net_edge_bps_at_signal |

Backward compatibility rule: all consumers must handle previous schema versions during transition. The `schema_version` field gates parsing logic.

---

## 5. New Config Files

| File | Purpose | Owner |
|------|---------|-------|
| `configs/execution.yaml` | Broker capability matrix, order policies, rate limits, retry config | execution_engine (read-only) |
| `configs/risk_limits.yaml` | Global + per-strategy risk limits, reconciliation thresholds | risk_engine (read-only), ops CLI (write) |
| `configs/calendar.yaml` | NSE/US session hours, holiday sources, expiry rules, safety windows | strategy_engine, execution_engine, risk_engine (all read-only) |

Both files are loaded at service startup and on 60s hot-reload (matching existing strategy-config pattern). Changes to these files should trigger a CloudWatch metric (`ConfigReloaded`) for audit.

---

## 6. New DynamoDB Tables

| Table | Owner | Purpose |
|-------|-------|---------|
| `{prefix}-fill-state` | execution_engine | Partial-fill event stream per client_order_id; TTL 7d; PK=ORDER#{client_order_id}, SK=FILL#{fill_seq} |
| `{prefix}-scorecard` | ops CLI, Chief Architect | Strategy validation scorecard; PK=STRATEGY#{id}, SK=SCORECARD#{version}; no TTL (retained forever) |
| `{prefix}-shadow-log` | shadow trading harness | Live signals during shadow mode, zero capital; PK=SIGNAL#{signal_id}, SK=SESSION#{date}; TTL 30d |
| `{prefix}-promotion-state` | ops CLI | Current promotion stage per strategy; PK=STRATEGY#{id}, SK=STAGE; no TTL |
| `{prefix}-calendar-cache` | scripts/calendar/refresh_holidays.py (weekly) | NSE/US holiday lists and session hours; PK=MARKET#{market}, SK=HOLIDAYS#{year}; TTL 400d |

Existing `strategy-config` table gains the new promotion_stage and scorecard fields (additive, backward-compatible).

#### Idempotency and Consistency Contracts for New v3 Tables

| Table | Write pattern | Idempotency contract | Consistency mode |
|-------|--------------|---------------------|-----------------|
| `{prefix}-fill-state` | execution_engine appends fill events per child order | `condition_expression: attribute_not_exists(PK) AND attribute_not_exists(SK)` — each `FILL#{fill_seq}` written exactly once | Eventual for P&L updates; **ConsistentRead=True** for terminal status reads (FILLED / CANCELED) before position update |
| `{prefix}-scorecard` | ops CLI writes scorecard post-review | Versioned SK `SCORECARD#{review_ts}` — immutable once written; new review = new SK row | Eventual (scorecard reads are non-time-critical; read before promotion gate → ConsistentRead=True) |
| `{prefix}-shadow-log` | shadow harness writes signals (zero capital) | `condition_expression: attribute_not_exists(signal_id)` — same contract as live signal idempotency | Eventual (shadow only; no capital consequences) |
| `{prefix}-promotion-state` | ops CLI / promote.py; stage machine transitions only | `condition_expression: stage = :expected_current_stage` — transition only if currently in the expected stage; prevents concurrent promotion race | **ConsistentRead=True** — promotion gating is a decision-critical read |
| `{prefix}-calendar-cache` | refresh_holidays.py script (weekly) | `put_item` with full replacement; no partial update | Eventual (read at service startup; cached in memory for session) |

Existing tables (`positions`, `orders`, `risk-state`, `strategy-config`, `strategy-state`, `candle-cache`, `latest-prices`) retain their v1/v2 idempotency contracts unchanged.

---

## 7. Updated Service Boundaries

| Service | Adds in Phase 9 | Removes / Changes |
|---------|----------------|------------------|
| **strategy_engine** | Alpha sleeve pipeline (AlphaForecast DTO, CostModel, PortfolioBuilder, RegimeFilter); publishes v5.0 signals | Strategies no longer emit raw direction signals directly — they emit AlphaForecast to internal pipeline |
| **execution_engine** | BrokerCapabilityMatrix loader; OrderPolicy validator; per-broker RateLimiter; OrderSlicer; FillState tracker; Reconciler; arrival_price capture | Scattered broker conditionals replaced by policy engine |
| **risk_engine** | Reconciliation gate (pre-order check); degradation level manager; reads v5.0 signal fields for net_edge validation; consumes fill_event v2.0 for IS tracking | No topology changes; existing validator sequence unchanged |
| **ai_engine** | Regime output forwarded to strategy_engine cost model as `regime_tags` on AlphaForecast | No other changes |
| **data_ingestion** | No changes | — |
| **infra** | 5 new Grafana dashboards; configs/execution.yaml + configs/risk_limits.yaml + configs/calendar.yaml; 5 new DynamoDB tables (fill-state, scorecard, shadow-log, promotion-state, calendar-cache); configs reloaded via CloudWatch Events; scripts/calendar/refresh_holidays.py (weekly schedule) | — |

---

## 8. v3 Implementation Sequence

v3 must be executed in this order. Items marked **[BLOCKER]** block all subsequent items within v3.

### Stage A — Blockers (first sprint of v3, before any capital scaling)

| Order | Item | Effort | Why first |
|-------|------|--------|-----------|
| A-1 | `configs/risk_limits.yaml` + refactor validators to read from it | 1–2 days | Unblocks all limit changes without code deploys |
| A-2 | `order_manager/broker_capabilities.py` + `order_policy.py` | 2–3 days | **BLOCKER**: prevents unsupported order types reaching broker |
| A-3 | Position reconciliation gate (five-point, DRIFT_HALT in risk-state) | 3–4 days | **BLOCKER**: required before any capital scaling |
| A-4 | fill_event v2.0 + fill_state.py + three prices (decision/arrival/fill) | 2–3 days | **BLOCKER**: required for IS tracking and P&L attribution |

### Stage B — Signal Economics (highest impact, 1–2 weeks)

| Order | Item | Effort |
|-------|------|--------|
| B-1 | signal_event v5.0 schema + cost fields (schema only, no alpha yet) | 1 day |
| B-2 | `cost/cost_model.py` — spread + slippage + fees calculation | 2–3 days |
| B-3 | `alpha/base.py` + `models/forecast.py` — AlphaForecast DTO | 1 day |
| B-4 | `alpha/momentum.py` — medium-frequency momentum scorer | 3–5 days |
| B-5 | `portfolio/portfolio_builder.py` — unified target-position solver | 3–4 days |
| B-6 | Signal suppression gate (net_edge_bps ≤ 0 → no publish) | 1 day |

### Stage C — Simulation & Validation (before shadow trading)

| Order | Item | Effort |
|-------|------|--------|
| C-1 | `tools/backtest/build_identity_map.py` + `corporate_actions_sync.py` | 2–3 days |
| C-2 | Cost-aware backtest mode (arrival price + slippage curve + fees) | 3–4 days |
| C-3 | Validation scorecard output (DSR, PBO, IS, turnover, regime stability) | 3–4 days |
| C-4 | Replay test suite (determinism, restart, stale-signal, corp-action) | 3–4 days |

### Stage D — Promotion Governance & Observability (before capital ramp)

| Order | Item | Effort |
|-------|------|--------|
| D-1 | `{prefix}-promotion-state` + `{prefix}-scorecard` DynamoDB tables | 1 day |
| D-2 | Strategy promotion ladder CLI (`scripts/strategy/promote.py`) | 2–3 days |
| D-3 | 5 Grafana dashboards + new CloudWatch metrics | 3–5 days |
| D-4 | Shadow trading harness (live signals, zero capital, IS measurement) | 3–5 days |
| D-5 | Graduated kill-switch degradation levels in risk-state | 2–3 days |

### Stage E — Alpha Quality (medium-term, 2–4 weeks)

| Order | Item | Effort |
|-------|------|--------|
| E-1 | `alpha/quality.py` — quality/profitability ranker | 3–5 days |
| E-2 | `alpha/regime_filter.py` — regime gating wrapper | 1–2 days |
| E-3 | `portfolio/ensemble.py` — multi-sleeve combination | 2–3 days |
| E-4 | Volatility scaling overlay (risk overlay, not alpha) | 2–3 days |

### Stage F — Advanced Execution (after Stage A complete)

| Order | Item | Effort |
|-------|------|--------|
| F-1 | `order_manager/rate_limiter.py` — per-broker + per-endpoint token buckets | 2–3 days |
| F-2 | `order_manager/slicer.py` — participation-rate based order clipping | 2–3 days |
| F-3 | `order_manager/router.py` — capability-matrix-driven routing | 1–2 days |
| F-4 | `order_manager/reconciler.py` — broker-state reconciliation on divergence | 2–3 days |

---

## 9. v3 Capital Readiness Gate

This gate applies to any strategy being considered for capital ramp under v3. It is stricter than the v1 paper→live promotion because capital is now real and being scaled.

Before any capital ramp beyond seed-live size, **all four conditions must be true simultaneously:**

1. **Cost-adjusted profitable out-of-sample** — OOS Sharpe > 1.0, DSR positive, PBO < 20%, for the specific strategies to be traded, on the specific instruments, with the specific brokers.
2. **Replay-deterministic** — identical event stream produces identical signals, restart does not create duplicate orders, all replay tests green in CI.
3. **Paper/shadow fills match** — paper-to-shadow fill-rate gap < 10 pp; slippage model error < 25% of expectation.
4. **Zero-defect reconciliation** — intraday and EOD position reconciliation resolves to zero drift, every session, for ≥ 5 consecutive sessions.

Until all four are true: platform status is **WARNING**. No capital ramp permitted.

_Note: The v1 capital readiness gate (5-day paper validation) remains in force for initial go-live. This v3 gate applies to scaling capital after v3 promotion infrastructure is in place._

---

## 10. Open Questions (Deferred — Answer When v3 Is Initiated)

| # | Question | Why it matters | Options |
|---|---------|---------------|---------|
| Q1 | Should signal schema v5.0 be a **strict schema bump** (reject v3.0/v4.0 on new consumers) or **additive** (old consumers tolerate missing fields with defaults)? | Determines whether strategy_engine and risk_engine upgrade must be atomic or can be rolled independently | Recommend: additive with schema_version guard — allows staged rollout |
| Q2 | Should the **CostModel** pull live spread from DynamoDB `latest-prices` or from the `features` table (which has ATR, volume_ratio already computed)? | Determines data dependency and freshness of spread estimates | Recommend: `latest-prices` for real-time spread, `features.volume_ratio` for slippage curve |
| Q3 | Should the **PortfolioBuilder** target absolute qty (# shares) or % NAV weights? | Absolute qty requires NAV as input; % weights are more portable across capital sizes | Recommend: % NAV internally, converted to qty at publish time using latest NAV from risk-state |
| Q4 | Should **shadow trading** publish to a separate Kafka topic (`signals.shadow`) or reuse `signals.pending` with a `shadow=True` flag? | Separate topic is cleaner but adds infra; flag is simpler but pollutes production topic | Recommend: separate `signals.shadow` topic (2 partitions, 7d retention) — no risk of shadow signals reaching execution |
| Q5 | Should the **degradation level** (PARTIAL_HALT, ENTRY_BLOCKED, etc.) live in DynamoDB `risk-state` as a single field, or as a separate `degradation-state` table? | Single field is simpler; separate table allows history of degradation events | Recommend: add `degradation_level` field to existing `risk-state` table; log state transitions to `ops.audit` |

---

## 11. Relationship Between v2 and v3

v2 (Phase 8 Production Hardening) and v3 are complementary, not competing. The correct sequencing is:

```
v1 live with real capital
    → v2 Phase 8 P0 tasks complete
        → v3 Stage A (blockers)
            → v3 Stage B (signal economics)
                → v2 Phase 8 P1 tasks (parallel or sequential)
                    → v3 Stages C–F
```

**Where v2 work feeds directly into v3:**

| v2 task | v3 dependency | Relationship |
|---------|--------------|-------------|
| PHASE8-001 — ACK_UNKNOWN order state | v3 F-4 `reconciler.py` | v2 creates the state model; v3 F-4 builds the full reconciliation loop on top of it |
| PHASE8-002 — per-endpoint Zerodha rate budgets | v3 A-2 `broker_capabilities.py` + `rate_limiter.py` | v2 is a targeted fix; v3 A-2 supersedes it with a full capability-matrix-driven approach |
| PHASE8-004 — startup reconciliation gate | v3 A-3 position reconciliation | v2 wires the startup gate; v3 A-3 extends it to continuous intraday five-point reconciliation |
| PHASE8-007 — reconciliation hard stop + three-way drift tool | v3 A-3 full reconciliation gate | v2 is the blunt instrument (halt on drift); v3 A-3 adds graduated DRIFT_WARN / DRIFT_HALT levels and the ops ack flow |

If v2 Phase 8 is fully implemented before v3 begins, v3 Stage A-3 is a natural evolution of the v2 reconciliation work — not a duplicate or a conflict.

---

---

## 12. v3 Activation Criteria

This document becomes actionable only when **all of the following are true**:

| # | Criterion | Who verifies |
|---|-----------|-------------|
| 1 | QuantEmbrace v1 has been live with real capital for ≥ 5 consecutive trading days | Hari (operator) |
| 2 | v2 / Phase 8 is fully implemented — all P0 tasks complete, CI kill-switch fanout test passing | Chief Architect |
| 3 | Position reconciliation is zero-defect for ≥ 5 sessions under v2 | Risk Manager |
| 4 | At least 2 strategies have been promoted from paper → live (real capital, not just paper_trade=False) | Hari (operator) |
| 5 | Hari explicitly initiates v3 ("start v3") | Hari |

Until all 5 criteria are met, this document is **read-only forward-planning material**. No task, ticket, code branch, or infrastructure change should reference this document as a delivery target.

---

## 13. What v3 Does NOT Change

As a reference guard against scope creep during v3 implementation:

| Item | Status in v3 |
|------|-------------|
| Kafka topics (names, partitions, retention) | Unchanged — only schemas evolve |
| MSK Serverless cluster | Unchanged |
| EC2 ARM64 ASGs and instance types | Unchanged |
| 6-layer architectural boundary model | Unchanged — non-negotiable |
| Signal flow: strategy → risk → execution | Unchanged — no bypass added |
| kill.switch Kafka topic and DynamoDB mechanism | Unchanged — only gains additional levels |
| KAFKA_BOOTSTRAP_SERVERS fail-fast at startup | Unchanged |
| Idempotency contracts (signal_id, order_id conditional writes) | Unchanged |
| paper_trade flag controlled by DynamoDB strategy-config | Unchanged |
| SQS permanently banned | Unchanged |
| ECS Fargate permanently removed | Unchanged |

---

---

## 14. CI/CD Requirements for v3

v3 introduces new code paths (alpha sleeves, CostModel, PortfolioBuilder, simulation stack, promotion ladder, calendar service) that require additional CI gates beyond what v1/v2 enforce. All existing CI gates (unit tests, integration tests, linting, type checks, kill-switch fanout) remain active and are not weakened.

### New CI gates required for v3

| Gate | Test / Tool | What it checks | Blocks merge if |
|------|------------|---------------|----------------|
| **Replay determinism** | `tests/replay/test_strategy_replay_determinism.py` | Same event stream → identical AlphaForecast, CostModel output, PortfolioBuilder target_qty, signal_event v5.0 for all registered sleeves | Any sleeve produces different output from same input |
| **Live = backtest alignment** | `tests/replay/test_live_backtest_alignment.py` | `DataProvider` interface (DynamoDB live vs S3 historical) produces identical AlphaForecast + signal for fixture inputs | Any divergence (zero tolerance) |
| **Cost model calibration** | `tests/unit/test_cost_model_calibration.py` | CostModel output within 15% of historical realized fill data (fixture dataset) | Cost model drift > 15% vs calibration fixture |
| **Restart idempotency** | `tests/replay/test_restart_mid_session.py` | Simulated service restart mid-session produces zero duplicate signals or orders | Any duplicate detected |
| **Stale signal drop** | `tests/replay/test_stale_signal_drop.py` | Signals expired during replay lag are NOT executed | Any stale signal reaches execution |
| **Corporate action replay** | `tests/replay/test_corporate_action_replay.py` | Sessions across split/rename boundaries produce correct adjusted prices and quantities | Any price or qty error across boundary |
| **Secrets scan** | `trufflehog git --since-commit HEAD~1 --no-update` | No credential patterns in changed files or config | Any credential pattern detected |
| **Security scan** | `bandit -r services/ common/ -ll` | OWASP top 10 checks on all service code | Any HIGH severity finding |
| **Schema compatibility** | `tests/unit/test_kafka_schema_compat.py` | signal_event v3.0, v4.0, v5.0 all parse correctly on all consumers (no missing required fields, correct defaults for optional fields) | Any schema parse failure |
| **Risk limits config validation** | `pytest tests/unit/test_risk_limits_config.py` | `configs/risk_limits.yaml` parses against pydantic model; all required limits present and within sane bounds | Config fails pydantic validation |
| **Execution config validation** | `pytest tests/unit/test_execution_config.py` | `configs/execution.yaml` parses correctly; capability matrix consistent across both brokers | Config fails validation |
| **Calendar config validation** | `pytest tests/unit/test_calendar_config.py` | `configs/calendar.yaml` parses correctly; NSE/US session times are consistent with known market hours | Config fails validation |
| **Promotion ladder dry-run** | `scripts/strategy/promote.py --dry-run --env staging` | Promotion CLI connects, reads DynamoDB, computes valid stage transitions | Any CLI error or unexpected state |
| **Paper isolation test** | `tests/unit/test_paper_isolation.py` | `execution_engine` instantiates separate paper/live broker clients; paper client URL ≠ live URL; paper_trade=True signal never reaches live_broker | Routing test fails |
| **Market order protection** | `tests/unit/test_order_policy.py::test_no_market_order_for_entry` | order_policy.py never produces a market order for a non-emergency new entry signal | Market order type produced for entry |

### Existing CI gates (remain active, not weakened in v3)

- `pytest tests/unit/` — min 85% coverage for risk, execution, strategy core logic
- `pytest tests/integration/` — LocalStack (S3, DynamoDB) + local Kafka (Redpanda)
- Kill-switch fanout test — Kafka kill.switch triggers halt in all 3 services (v2 gate)
- `black --check`, `ruff check`, `isort --check` — formatting and linting
- `mypy --strict` on `services/` and `common/` — type checking
- ARM64 Docker build: `docker build --platform linux/arm64` — ensures build succeeds on target compute

### Pre-deployment dry-run checklist (v3 production deployments)

```
Before any v3 production deployment:

1. scripts/strategy/promote.py --dry-run --env production
   → verify promotion state machine connects and transitions are valid

2. scripts/ops/reconcile.py --dry-run --env production
   → verify reconciliation tool connects to broker APIs and DynamoDB

3. scripts/calendar/refresh_holidays.py --dry-run --env production
   → verify holiday list is fresh (< 7 days old) or refresh succeeds

4. docker build --platform linux/arm64 --no-cache
   → verify clean ARM64 build (catches any x86-only native dependency issues)

5. terraform plan -var-file=environments/production/variables.tf
   → verify no unintended infra drift

6. trufflehog git --since-commit v2.0.0 --no-update
   → one-time full scan from v2 tag to ensure no secrets in v3 commits

All 6 checks must pass before any v3 deployment to production proceeds.
```

---

_End of QuantEmbrace v3 Architecture Design Document (DRAFT v1.1)_  
_Tagged: VERSION 3 — FUTURE SCOPE — DO NOT IMPLEMENT_  
_Current focus: v1 live with real capital → v2 Phase 8 hardening_  
_v3 initiation: Hari's explicit call after v2 complete and v1 proven live_  
_Open questions (§10): Answer when v3 is formally started_  
_v1.1 update: 12-concern safety/quality review complete — gaps closed in §2, §3.3, §3.5, §3.6, §3.9, §3.10–3.12, §6, §14_
