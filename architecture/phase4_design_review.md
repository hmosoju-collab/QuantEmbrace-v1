# QuantEmbrace Phase 4 — Distributed Risk Engine + Portfolio Layer
## Architecture Design Review — APPROVED

**Version**: 1.1 — Zerodha Rate Capacity Aligned
**Date**: 2026-05-06
**Owner**: Hari
**Status**: ✅ APPROVED 2026-05-06
**Prerequisite**: Phase 3 complete ✅ (2026-05-06)

**Revision history:**
| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-05-06 | Initial draft |
| 1.1 | 2026-05-06 | Aligned with `zerodha_rate_capacity_design.md`: added SpreadGateValidator, MarketPhase-aware analytics, live_spread_bps in RiskContext, CRITICAL-priority force-cancel wiring, candle gap degradation |

---

## 1. The Honest Reframe

The roadmap Phase 4 sketch calls for:
- ElastiCache Redis cluster for kill switch + position hot path
- Active-active dual risk engine instances across two AZs
- Full 252-day historical VaR simulation
- F&O portfolio delta/gamma

**None of these are appropriate for a personal trader running 5–30 signals per day.**

Your actual risk engine profile:

| Metric | Reality | What Redis/dual-instance solves |
|--------|---------|--------------------------------|
| Signals per day | 5–30 | > 50,000 |
| Kill switch read latency needed | < 200ms | < 1ms |
| Current kill switch read latency | 3–8ms (DynamoDB) | N/A |
| Concurrent risk validations | 1–3 | > 500 |
| Cost of Redis (ElastiCache r7g.small) | ~$50/month | — |
| Marginal value at 30 signals/day | $0 | — |

Active-active is solving an availability problem you don't have. A single risk engine
instance with watchdog auto-restart (EC2 ASG desired=1, health check 30s) provides
99.9%+ uptime. NSE trading hours are 9:15–15:30 IST — a 30-second restart has
essentially zero trading impact.

F&O delta/gamma requires a live option chain feed, real-time IV surface, and
non-trivial Greeks computation. The current codebase has no F&O positions.
Skip entirely until you're actively trading options.

**Phase 4 correct definition:**

> Fill the actual gaps in the risk layer. The validators exist; the analytics
> don't. Add the portfolio analytics that make the existing limits meaningful:
> sector concentration (the `max_sector_exposure_pct` field that currently
> does nothing), a liquidity guard, a simplified portfolio VaR, and an
> in-memory kill switch cache that eliminates the per-signal DynamoDB read
> without requiring Redis.

---

## 2. Alignment with Zerodha Full-Capacity Rate Limit Design

`zerodha_rate_capacity_design.md` (v1.1, approved 2026-05-01) is the authoritative
source of truth for how all services interact with the Zerodha broker layer. Every
phase design must be consistent with it. This section documents the four gaps found
in Phase 4 v1.0 and how they are resolved in v1.1.

### Misalignment 1 — No Spread Gate (RESOLVED)

**Zerodha design says**: `LiveQuotePoller` provides real-time bid-ask spread, and the
risk engine must reject signals when spread exceeds a configured threshold. This was
called out explicitly as one of the three outputs of `LiveQuotePoller`.

**Phase 4 v1.0 gap**: The validator pipeline had no spread gate. `SlippageValidator`
estimated slippage from signal price but did not check the live bid-ask spread.

**Resolution (v1.1)**: Added `SpreadGateValidator` as step 7 in the pipeline (before
sector, margin, and slippage). `RiskContextBuilder` reads the most recent quote from
`LiveQuotePoller`'s DynamoDB output table and exposes it as `context.live_spread_bps`.
`SpreadGateValidator` rejects if `live_spread_bps > max_spread_bps` (default: 50 bps,
configurable per instrument class). If LiveQuotePoller data is stale (>30s), validator
approves with a `STALE_SPREAD_DATA` warning — never blocks on data gaps.

### Misalignment 2 — RiskAnalyticsEngine Not MarketPhase-Aware (RESOLVED)

**Zerodha design says**: All polling services must adapt their interval by market phase.
`PositionMonitor`, `BulkOrderPoller`, and `LiveQuotePoller` all have per-phase intervals.

**Phase 4 v1.0 gap**: `RiskAnalyticsEngine` ran every 60 seconds flat regardless of
whether the market was open, in pre-open, or after close.

**Resolution (v1.1)**: `RiskAnalyticsEngine` now reads from `MarketPhaseGovernor` and
uses phase-specific intervals:

```
PRE_OPEN      30s   — warm up sector/VaR before market opens
MARKET_OPEN   30s   — fast refresh during volatile first 15 min
NORMAL        60s   — standard refresh
PRE_CLOSE     30s   — pre-compute MIS square-off impact
POST_CLOSE    300s  — end-of-day final snapshot only
OVERNIGHT     never — no computation; resume at PRE_OPEN
```

### Misalignment 3 — IntradayCandleStream Phase Gap Not Handled (RESOLVED)

**Zerodha design says**: `IntradayCandleStream` is DISABLED during `MARKET_OPEN`
(09:15–09:30) — the 3 req/s historical budget is reallocated to order placement and
fill detection during the opening burst.

**Phase 4 v1.0 gap**: `LiquidityValidator` relies on ADV data from the candle cache.
During `MARKET_OPEN`, no new candles are being written. ADV data from the pre-open
`candle_prefetch.py` run is the authoritative source. Phase 4 did not acknowledge this
gap or its graceful degradation path.

**Resolution (v1.1)**: `RiskContextBuilder` checks the age of the most recent candle
for the signal's symbol. If candle data is > 20 minutes stale (which is normal during
`MARKET_OPEN`), `LiquidityValidator` uses the ADV pre-computed by `candle_prefetch.py`
during the morning pre-open run, which writes to the same candle-cache table with
`interval="day"`. Fall-through: if no daily ADV data exists, validator approves with
a `LOW_ADV_DATA` warning (same graceful-degradation rule as before — data gaps never
block trading).

### Misalignment 4 — Kill Switch Force-Cancel Not on CRITICAL Priority (RESOLVED)

**Zerodha design says**: CRITICAL priority is for "Emergency cancel, stop-loss
protection, kill-switch force-cancel". Kill switch activation must preempt all other
Zerodha API operations immediately.

**Phase 4 v1.0 gap**: `KillSwitchCache.activate()` persisted state to DynamoDB (no
rate limit relevance — DynamoDB is not Zerodha). But kill switch activation also
triggers force-cancel of all open orders via `zerodha.cancel_order()`. Those calls
must use `Priority.CRITICAL` in `ZerodhaRateLimiter`. Phase 4 v1.0 did not specify
this.

**Resolution (v1.1)**: `KillSwitchCache.activate()` explicitly calls
`cancel_all_orders(priority=Priority.CRITICAL)` on `ExecutionService` via an event
bus after persisting the DynamoDB state. Force-cancel calls bypass all queued LOW/MEDIUM
operations and execute immediately. The DynamoDB refresh poll (every 1s) is a pure
DynamoDB call — it does not pass through `ZerodhaRateLimiter` (which governs Zerodha
API calls only, not AWS service calls).

---

## 3. What Phase 3 Left Incomplete

### Gap 1 — Kill Switch DynamoDB Read on Every Signal (Latency Tax)

`RiskEngineService.validate_signal()` reads kill switch state from DynamoDB on every
single signal check. At 5–30 signals/day this is harmless in throughput terms.
The real problem: **the DynamoDB read is synchronous via `asyncio.to_thread`**,
adding 3–8ms of I/O to every validation. More importantly, a DynamoDB brief
throttle event during market hours would cause every signal to time out — even
though the kill switch is inactive.

**Fix**: In-memory KillSwitchCache. Cache state in RAM, refresh every 1 second
via a background loop. Kill switch checks become 0ms (dict lookup).
Kill switch activation still persists to DynamoDB immediately (correctness path).
The 1-second cache lag is acceptable — activation is an operator action, not
a microsecond precision event.

### Gap 2 — Sector Concentration Limit Does Nothing

`RiskLimits.max_sector_exposure_pct = 25.0` is set and logged but never enforced.
There is no validator that checks sector exposure. There is no sector classification
data for NSE instruments.

Result: the risk engine will happily approve RELIANCE + ONGC + BPCL (all Energy)
for 75% of portfolio without any sector concentration check.

**Fix**: Static `instruments.yaml` sector registry (GICS Level 1 for NSE top-200
instruments + all US instruments in scope). New `SectorConcentrationValidator`
that reads current positions, groups by sector, and rejects signals that would
push any sector above `max_sector_exposure_pct`.

### Gap 3 — No Liquidity Check

The position validator caps the quantity of shares held. It does not check whether
the order itself is feasible in the market. A 50,000-share BUY on a stock averaging
10,000 shares/day ADV is a guaranteed adverse fill — the order moves the market
against you and slippage explodes.

**Fix**: `LiquidityValidator` — checks `signal.quantity` against the instrument's
20-day average daily volume (ADV). ADV is read from the existing DynamoDB
`candle-cache` table (already populated by `IntradayCandleStream`).
Configurable threshold: default reject if order > 5% ADV (adjustable per instrument).

### Gap 4 — No Portfolio-Level Loss Analytics

The daily loss validator (`DailyLossValidator`) tracks realized P&L from fills.
It does not track unrealized P&L (mark-to-market open positions). A position can
have a 3% unrealized loss without triggering the daily loss limit.

Additionally, there is no rolling VaR — the system has no estimate of what
a bad day looks like for the current portfolio composition.

**Fix**: `RiskAnalyticsEngine` — a background coroutine (not a separate service)
running every 60 seconds inside `RiskEngineService`. It computes:
  1. Unrealized P&L per position (last price from `LiveQuotePoller` data via DynamoDB)
  2. Total daily P&L (realized + unrealized) → fed to `DailyLossValidator`
  3. Portfolio VaR(95%) — simplified 10-day rolling window using daily close prices
     from the candle cache
  4. Sector exposure breakdown — per-sector notional, used by `SectorConcentrationValidator`

Results written to DynamoDB `risk-analytics` table (TTL 24h). Validators read
from this table with a 60-second stale tolerance.

### Gap 5 — Per-Signal DynamoDB Reads Are Not Batched

Each of the 7 validators currently makes independent DynamoDB reads for positions,
orders, and limits. A signal going through all 7 validators makes approximately
8–12 DynamoDB reads. Even at low signal volume this is wasteful and creates
redundant DynamoDB read units.

**Fix**: `RiskContext` — a per-signal immutable context object assembled once
at the start of validation. All validators read from this pre-fetched context.
Reduces per-signal DynamoDB reads from 8–12 to 2–3.

---

## 3. What We Are NOT Building in Phase 4

| Roadmap Item | Verdict | Reason |
|---|---|---|
| ElastiCache Redis | ❌ Deferred to Phase 6+ | $50/month for 0 throughput gain at 30 signals/day. In-memory cache solves the latency problem. |
| Active-active dual risk engine | ❌ Not needed | Single instance + ASG watchdog restart is sufficient. No meaningful HA gap at personal trader scale. |
| F&O delta/gamma | ❌ Deferred | No F&O positions. Requires option chain feed not yet integrated. Phase 6 scope. |
| Full 252-day historical VaR | ❌ Simplified | 252 days of daily closes from S3 not yet in a queryable format. Phase 5 (Feature Store) prerequisite. Build 10-day rolling VaR now; upgrade to full VaR in Phase 5. |
| Kafka `risk.analytics` topic | ❌ Not needed | Analytics results written to DynamoDB directly. Kafka overhead not justified. |

---

## 4. Architecture Changes

### 4.1 Risk Context (Pre-fetch Pattern)

**New file**: `services/risk_engine/context/risk_context.py`

```
RiskContext (dataclass, immutable after construction)
    signal:              Signal
    confirmed_position:  int          (from positions table — written by PositionMonitor)
    pending_quantity:    int          (from orders table, in-flight)
    current_exposure:    float        (total portfolio exposure, ₹)
    sector_exposures:    dict[str, float]  (sector → notional ₹)
    adv_20d:             float        (20-day ADV for signal.symbol, from candle-cache)
    live_spread_bps:     float | None (bid-ask spread from LiveQuotePoller DynamoDB output;
                                      None if data > 30s stale)
    portfolio_nav:       float        (current NAV from RiskLimits)
    analytics_snapshot:  AnalyticsSnapshot | None  (from risk-analytics table)
    fetched_at:          datetime
```

`RiskContextBuilder.build(signal)` — called once per signal. Makes 3–4 targeted
DynamoDB reads (positions GetItem + orders query + analytics GetItem + quote GetItem).
All 8 validators receive the same `RiskContext` instead of reading DynamoDB independently.

**Data source notes**:
- `confirmed_position` is kept fresh by `PositionMonitor` (writes every 1–2s). `RiskContextBuilder` does a simple `GetItem` on the same table — no independent scan.
- `live_spread_bps` is written by `LiveQuotePoller` to a `quotes-cache` DynamoDB table every 2s. `RiskContextBuilder` reads with `GetItem`. No Zerodha API call on the risk engine side.

### 4.2 In-Memory Kill Switch Cache

**New file**: `services/risk_engine/killswitch/kill_switch_cache.py`

```
KillSwitchCache
    _active: bool                   (in-memory state)
    _activated_reason: str | None
    _last_refresh: float            (monotonic)
    _refresh_interval: float = 1.0  (seconds)

    is_active() → bool              (0ms — pure RAM read)
    activate(reason) → None         (writes DynamoDB + updates RAM)
    deactivate() → None             (writes DynamoDB + updates RAM)
    _refresh_loop()                 (background coroutine, 1s poll DynamoDB)
```

`KafkaKillSwitchListener` already propagates kill switch events via Kafka.
`KillSwitchCache._refresh_loop()` adds a DynamoDB fallback poll (handles restart
scenarios where Kafka message was missed). Both paths update `_active`.

All validators call `cache.is_active()` instead of reading DynamoDB.

### 4.3 Spread Gate Validator (new — Zerodha alignment)

**New file**: `services/risk_engine/validators/spread_gate_validator.py`

`SpreadGateValidator.validate(signal, context: RiskContext)`:
1. Read `context.live_spread_bps` (from `LiveQuotePoller` DynamoDB output)
2. If `live_spread_bps is None` (data stale > 30s): approve with `STALE_SPREAD_DATA` warning — never block on data gaps
3. Reject if `live_spread_bps > max_spread_bps` (default: 50 bps; configurable per instrument class)

Wide spread = your limit order sits at the wrong price or gets an immediate adverse fill.
50 bps (0.5%) is the threshold used in the Zerodha design's scalp strategy gate.

Per-instrument overrides (in `instruments.yaml`):
```yaml
nse:
  RELIANCE: { sector: "Energy", max_spread_bps: 30 }   # liquid — tight gate
  SMALLCAP:  { sector: "...",   max_spread_bps: 100 }  # illiquid — relaxed gate
```

If no override, `RiskLimits.max_spread_bps` applies (default 50).

### 4.5 Sector Concentration Validator

**New file**: `services/risk_engine/validators/sector_validator.py`
**New file**: `services/shared/instruments/registry.py`
**New file**: `configs/instruments.yaml`

`instruments.yaml` structure:
```yaml
nse:
  RELIANCE:  { sector: "Energy",       industry: "Integrated Oil & Gas" }
  ONGC:      { sector: "Energy",       industry: "Oil & Gas Exploration" }
  BPCL:      { sector: "Energy",       industry: "Oil & Gas Refining" }
  INFY:      { sector: "Technology",   industry: "IT Services" }
  TCS:       { sector: "Technology",   industry: "IT Services" }
  HDFCBANK:  { sector: "Financials",   industry: "Banks" }
  # ... NSE top-200 instruments
us:
  AAPL:      { sector: "Technology",   industry: "Consumer Electronics" }
  NVDA:      { sector: "Technology",   industry: "Semiconductors" }
  # ... all US instruments in scope
```

`SectorConcentrationValidator.validate(signal, context: RiskContext)`:
1. Read `context.sector_exposures` (pre-computed by analytics engine)
2. Identify the sector of `signal.symbol` from `InstrumentRegistry`
3. Compute proposed sector exposure after adding this signal's notional
4. Reject if proposed sector exposure > `max_sector_exposure_pct` of NAV

### 4.6 Liquidity Validator

**New file**: `services/risk_engine/validators/liquidity_validator.py`

`LiquidityValidator.validate(signal, context: RiskContext)`:
1. Read `context.adv_20d` (pre-fetched from candle-cache aggregate query)
2. Compute `participation_pct = (signal.quantity * signal.price_at_signal) / adv_20d`
3. Reject if `participation_pct > max_adv_participation_pct` (default: 5%)

ADV computation: `RiskContextBuilder` queries the candle-cache DynamoDB table for
the last 20 daily candles for `signal.symbol` (`interval="day"`), sums volume,
divides by 20. These daily candles are pre-populated by `candle_prefetch.py`
every morning before market open.

**Phase-aware graceful degradation**: `IntradayCandleStream` is disabled during
`MARKET_OPEN` (09:15–09:30 IST). During this window, intraday candles are not
written. However, ADV is computed from daily candles (`interval="day"`), which
`candle_prefetch.py` wrote in pre-open. The daily candle data remains valid throughout
the trading session. If daily candle data is absent (< 5 days), validator approves
with a `LOW_ADV_DATA` warning — never blocks on data gaps.

### 4.7 Risk Analytics Engine (MarketPhase-aware)

**New file**: `services/risk_engine/analytics/risk_analytics_engine.py`

Background coroutine added to `RiskEngineService.start()` via `asyncio.gather`.
Interval adapts to `MarketPhaseGovernor` — aligned with `zerodha_rate_capacity_design.md`
requirement that all polling services be market-phase-aware.

```python
ANALYTICS_INTERVAL_BY_PHASE = {
    MarketPhase.PRE_OPEN:    30,   # warm up before market opens
    MarketPhase.PRE_AUCTION: 30,   # price discovery phase — keep fresh
    MarketPhase.MARKET_OPEN: 30,   # volatile first 15 min — fast refresh
    MarketPhase.NORMAL:      60,   # standard
    MarketPhase.PRE_CLOSE:   30,   # MIS square-off window — fast refresh
    MarketPhase.CLOSING:     60,
    MarketPhase.POST_CLOSE:  300,  # end-of-day snapshot only
}
# Overnight (no phase / outside schedule): no computation
```

```
RiskAnalyticsEngine
    _phase_governor: MarketPhaseGovernor

    async _analytics_loop()
        while True:
            interval = ANALYTICS_INTERVAL_BY_PHASE.get(
                _phase_governor.current_phase(), 60
            )
            await asyncio.sleep(interval)
            snapshot = await _compute_snapshot()
            await _persist_snapshot(snapshot)

    async _compute_snapshot() → AnalyticsSnapshot
        1. Read all open positions from DynamoDB
        2. Read last prices from candle-cache (most recent candle per symbol)
        3. Compute unrealized P&L per position
        4. Compute sector exposures (notional per sector)
        5. Compute total_daily_pnl = realized_pnl + unrealized_pnl
        6. Compute portfolio_var_95 (10-day rolling, explained below)
        7. Return AnalyticsSnapshot
```

**VaR(95%) methodology** (10-day rolling, simplified):
- Source: `candle-cache` table — last 10 available daily close prices per held symbol
- Per-position: weight = (quantity × last_price) / NAV; daily return = (close_t / close_t-1) - 1
- Portfolio daily return = Σ(weight_i × return_i) for each held symbol
- VaR(95%) = -1 × percentile(portfolio_daily_returns_10d, 5%)
- If fewer than 5 days of data: VaR is `None` (not enough history)
- Reported as: `var_95_pct` (% of NAV) and `var_95_value` (₹ absolute)

This is not rigorous for a derivatives book. It is appropriate for a long/short
equity book with 5–30 daily signals and 10–50 simultaneous positions.

`AnalyticsSnapshot` (DynamoDB schema):
```
PK = "ANALYTICS#PORTFOLIO"
SK = "SNAPSHOT"
computed_at:         str (ISO-8601 UTC)
total_daily_pnl:     Decimal (realized + unrealized ₹)
unrealized_pnl:      Decimal
sector_exposures:    str (JSON: {"Energy": 450000.0, "Technology": 320000.0, ...})
var_95_pct:          Decimal | absent (None if < 5 days data)
var_95_value:        Decimal | absent
positions_count:     int
TTL:                 int (24h)
```

### 4.8 Updated Validator Pipeline

New validation order in `RiskEngineService.validate_signal()`:

```
1.  KillSwitchCache.is_active()          [0ms — RAM]
2.  SignalAgeValidator.validate()         [0ms — timestamp math]
3.  RiskContextBuilder.build(signal)      [3–8ms — 3–4 DynamoDB GetItems]
    ↓ RiskContext assembled
    (contains: confirmed_position, pending_qty, current_exposure,
     sector_exposures, adv_20d, live_spread_bps, analytics_snapshot)
4.  PositionValidator.validate(ctx)       [0ms — ctx.confirmed_position + pending]
5.  ExposureValidator.validate(ctx)       [0ms — ctx.current_exposure]
6.  LiquidityValidator.validate(ctx)      [0ms — ctx.adv_20d]
7.  SpreadGateValidator.validate(ctx)     [0ms — ctx.live_spread_bps]  ← NEW
8.  SectorConcentrationValidator(ctx)     [0ms — ctx.sector_exposures]
9.  MarginValidator.validate(ctx)         [optional broker call, cached 5min]
10. SlippageValidator.validate(ctx)       [0ms — price math]
11. DailyLossValidator.validate(ctx)      [0ms — cached P&L + ctx analytics]
```

Total latency target: **< 15ms** (down from current ~50ms with 8+ independent
DynamoDB reads scattered across validators).

**Why SpreadGateValidator before SectorConcentration and Margin?**
Spread check is the cheapest early exit for market-microstructure reasons.
If the spread is 200 bps, the trade is uneconomic regardless of sector or margin.
Reject cheap before the expensive checks.

---

## 5. New Infrastructure

### 5.1 DynamoDB Tables

**`risk-analytics` table** (new):
```
PK: "ANALYTICS#PORTFOLIO"
SK: "SNAPSHOT"
BillingMode: PAY_PER_REQUEST
TTL attribute: TTL
```

Single-row table. Only one snapshot row at any time. Cost: < $0.01/month
(1 write/minute × 60s × 6.5h market hours = 390 writes/day at negligible cost).

### 5.2 IAM Additions

Risk engine EC2 role needs `dynamodb:PutItem` + `dynamodb:GetItem` on
`risk-analytics` table (added to existing `ec2_services/iam.tf`).

### 5.3 Terraform Changes

- Add `risk_analytics` DynamoDB table in `infra/terraform/modules/dynamodb/`
- Add `instruments_config_s3_key` optional variable (for future S3-hosted registry)
- No new EC2 instances, no Redis, no additional services

### 5.4 CloudWatch Alarms (additions)

| Alarm | Metric | Threshold | Action |
|-------|--------|-----------|--------|
| AnalyticsComputeFailure | `RiskEngine/AnalyticsErrors` count > 3 in 5min | CRITICAL | PagerDuty |
| KillSwitchCacheStaleness | `RiskEngine/KillSwitchCacheAge` > 10s | WARNING | Alert SNS |
| VaRBreachWarning | `RiskEngine/VaR95Pct` > 3.0% | WARNING | Alert SNS |
| LiquidityRejectRate | `RiskEngine/LiquidityRejects` > 5 in 1hr | INFO | Alert SNS |
| SectorConcentrationBreach | `RiskEngine/SectorRejects` > 10 in 1hr | INFO | Alert SNS |

---

## 6. New Files Summary

| File | Type | Description |
|------|------|-------------|
| `services/risk_engine/context/risk_context.py` | New | `RiskContext` dataclass + `RiskContextBuilder` |
| `services/risk_engine/context/__init__.py` | New | Package init |
| `services/risk_engine/killswitch/kill_switch_cache.py` | New | In-memory kill switch cache with 1s DynamoDB refresh loop |
| `services/risk_engine/validators/sector_validator.py` | New | `SectorConcentrationValidator` |
| `services/risk_engine/validators/liquidity_validator.py` | New | `LiquidityValidator` (20-day ADV check) |
| `services/risk_engine/analytics/risk_analytics_engine.py` | New | `RiskAnalyticsEngine` background loop |
| `services/risk_engine/analytics/__init__.py` | New | Package init |
| `services/shared/instruments/registry.py` | New | `InstrumentRegistry` (loads `instruments.yaml`) |
| `services/shared/instruments/__init__.py` | New | Package init |
| `configs/instruments.yaml` | New | NSE top-200 + US instruments with sector/industry tags |
| `infra/terraform/modules/dynamodb/risk_analytics.tf` | New | `risk-analytics` DynamoDB table |
| `tests/unit/test_risk_context.py` | New | Unit tests for `RiskContextBuilder` |
| `tests/unit/test_sector_validator.py` | New | Unit tests for `SectorConcentrationValidator` |
| `tests/unit/test_liquidity_validator.py` | New | Unit tests for `LiquidityValidator` |
| `tests/unit/test_risk_analytics_engine.py` | New | Unit tests for `RiskAnalyticsEngine` |
| `tests/unit/test_kill_switch_cache.py` | New | Unit tests for `KillSwitchCache` |

**Modified files:**

| File | Change |
|------|--------|
| `services/risk_engine/service.py` | Wire `RiskContextBuilder`, `KillSwitchCache`, `RiskAnalyticsEngine`; replace validator DynamoDB reads with context reads |
| `services/risk_engine/limits/risk_limits.py` | Add `max_adv_participation_pct`, `var_95_warn_pct` fields |
| `services/risk_engine/validators/exposure_validator.py` | Accept `RiskContext`, remove own DynamoDB reads |
| `services/risk_engine/validators/position_validator.py` | Accept `RiskContext`, remove own DynamoDB reads |
| `services/risk_engine/validators/loss_validator.py` | Accept `RiskContext`, feed `analytics_snapshot.total_daily_pnl` |
| `services/shared/config/settings.py` | Add `risk_analytics_table`, `instruments_config_path`, `max_adv_participation_pct`, `kill_switch_cache_refresh_interval` |
| `infra/terraform/modules/ec2_services/iam.tf` | Add risk-analytics DynamoDB IAM |

---

## 7. What Stays The Same

- **7 existing validators** — logic unchanged; they accept `RiskContext` instead
  of reading DynamoDB themselves. External behaviour (approve/reject decisions)
  does not change.
- **Kafka topics** — no new topics. All signals flow unchanged through
  `signals.pending` → `signals.approved`.
- **Kill switch DynamoDB record** — schema unchanged. `KillSwitchCache` reads
  from the same PK/SK. Existing operator CLI and HTTP API continue to work.
- **EC2 deployment** — single c6g.large instance, same ASG config.
- **MarginValidator** — unchanged. It legitimately needs a live broker call
  (or a 5-minute cached broker call). Not affected by `RiskContext`.

---

## 8. Implementation Task List

| # | Task | File | Complexity |
|---|------|------|-----------|
| PHASE4-001 | `RiskContext` + `RiskContextBuilder` | `context/risk_context.py` | Medium |
| PHASE4-002 | `KillSwitchCache` with 1s refresh loop | `killswitch/kill_switch_cache.py` | Low |
| PHASE4-003 | `InstrumentRegistry` + `instruments.yaml` (NSE top-50 + US scope) | `shared/instruments/` + `configs/` | Low |
| PHASE4-004 | `SectorConcentrationValidator` | `validators/sector_validator.py` | Low |
| PHASE4-005 | `LiquidityValidator` (20-day ADV) | `validators/liquidity_validator.py` | Low |
| PHASE4-006 | `RiskAnalyticsEngine` (60s loop: unrealized P&L, sector exposures, VaR) | `analytics/risk_analytics_engine.py` | High |
| PHASE4-007 | Update `RiskLimits` + `settings.py` with new fields | `limits/risk_limits.py`, `settings.py` | Low |
| PHASE4-008 | Refactor all validators to accept `RiskContext` (remove own DynamoDB reads) | 5 existing validators | Medium |
| PHASE4-009 | Wire everything into `service.py` (RiskContextBuilder, KillSwitchCache, analytics loop) | `service.py` | Medium |
| PHASE4-010 | Terraform: `risk-analytics` DynamoDB table + IAM | `dynamodb/risk_analytics.tf`, `iam.tf` | Low |
| PHASE4-011 | Unit tests for all new components | `tests/unit/test_risk_*.py` | Medium |
| PHASE4-012 | Update `architecture/` docs, `memory/decisions.md`, `memory/open_tasks.md` | Docs | Low |

---

## 9. Open Questions

**Q1 — VaR data source for non-NSE instruments**
`candle-cache` is populated by `IntradayCandleStream` for NSE instruments.
US instruments (Alpaca) currently have no candle cache. VaR for US positions
will be `None` until Phase 5 (Data Platform) adds a US candle feed.

Options:
- (A) Compute NSE VaR only; log warning for US positions with `None` VaR
- (B) Approximate US VaR using intraday price range as a single-day return proxy
- (C) Hardcode historical annualised volatility per US instrument (e.g., AAPL ≈ 30% annualised → σ_daily ≈ 1.9%) for VaR estimation when candle data is absent

**Recommendation**: Option A for now. Simple, honest, no hardcoded constants.
US P&L is fully tracked; only VaR estimate is absent.

**Q2 — `instruments.yaml` maintenance burden**
NSE has ~1,900 listed equities. The registry covers only the instruments
the strategies actually trade (currently ~50). New instruments added to
strategy configs must be manually added to `instruments.yaml`.

Options:
- (A) Manual YAML updates — operators add entries before enabling a new instrument
- (B) Auto-populate from NSE sector data via a `scripts/instruments/refresh_registry.py`
  script (scrapes NSE website or uses Kite master instruments list)
- (C) Fallback sector for unknown instruments: `"Unknown"` — sector concentration
  check is skipped for that instrument with a WARNING log

**Recommendation**: Option C for validator robustness (unknown instruments never
block trading), plus Option B as a maintenance script (run manually to keep
registry current). Option A alone creates an ops dependency before every go-live.

**Q3 — Analytics loop failure handling**
If `RiskAnalyticsEngine._compute_snapshot()` fails (DynamoDB throttle, etc.),
should validators use the last known snapshot or operate without portfolio analytics?

Options:
- (A) Use last known snapshot (up to 5 minutes stale) — sector/VaR checks continue
  with potentially stale data
- (B) Disable sector + VaR checks when snapshot is stale, log WARN — validators
  approve without analytics (safe; only removes extra checks, doesn't block)
- (C) Activate kill switch if analytics are unavailable > 5 minutes (fail-safe)

**Recommendation**: Option B. Portfolio analytics are additive risk guards, not
hard stops. A compute failure should not halt trading. Option C is too aggressive —
an analytics bug would take down the entire platform.

---

## 10. Acceptance Criteria

- [ ] Kill switch check is 0ms (RAM read, no DynamoDB I/O on the signal path)
- [ ] All 7 existing validators pass existing unit tests unchanged
- [ ] Per-signal DynamoDB reads reduced from 8–12 to 2–3 (via `RiskContext`)
- [ ] `SectorConcentrationValidator` correctly rejects a signal that would push Energy > 25% of NAV
- [ ] `LiquidityValidator` correctly rejects a signal for 50,000 shares on a 100,000 ADV instrument (50% participation)
- [ ] `RiskAnalyticsEngine` writes a valid snapshot to DynamoDB within 60s of startup
- [ ] VaR computation produces a reasonable value on 5+ days of candle data
- [ ] `InstrumentRegistry` loads `instruments.yaml` and returns correct sector for RELIANCE, TCS, HDFCBANK, AAPL
- [ ] All new components have ≥ 85% unit test coverage
- [ ] No new AWS services introduced (no Redis, no new EC2 instances)
- [ ] Total signal validation latency < 15ms (end-to-end, measured in unit test)
