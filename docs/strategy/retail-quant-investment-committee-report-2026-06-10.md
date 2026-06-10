# Retail Quant Investment Committee Report — QuantEmbrace Strategy Book

**Date:** 2026-06-10
**Account basis:** ₹10,00,000 paper NAV
**Venue:** India — NSE cash equities, intraday MIS, Zerodha Kite Connect (primary broker API)
**Evidence base:** Session archives 2026-06-01 → 2026-06-09 (`session-archives/`), live Session 15 LocalStack data (2026-06-10), full source review of all 6 strategies, quality gates, position sizer, and TEE exit policies
**Status:** RECOMMENDATION ONLY — no code, config, or trading behavior changed. Implementation requires operator approval (CLAUDE.md governance).

---

## Standing Evaluation Rules (operator mandate, 2026-06-10)

> Assume the trading environment is India (NSE/BSE) unless specified otherwise. Include STT,
> exchange transaction charges, GST, stamp duty, SEBI charges, lot-size constraints, broker APIs
> (e.g., Zerodha, Angel One, Upstox), and margin rules in all evaluations. **A strategy that is
> profitable before these costs but not after them must be REJECTED.**

- **R1 (after-cost rejection):** Every strategy evaluation in this repo computes expectancy NET of the full Indian statutory + brokerage + spread + slippage stack (§0). Pre-cost profitability is irrelevant.
- **R2 (cost floor):** Minimum viable profit target ≈ 2.5× round-trip cost. At the §0 baseline this means **target ≥ 0.5%, stop ≥ 0.4%** of entry price for intraday cash equity.
- **R3 (trade-count budget):** Trade frequency is itself a cost decision. The book-wide entry cap is part of risk management, not an afterthought.

---

## Committee Verdict (summary)

| Strategy | Class | Status 2026-06-10 | After-cost verdict (R1) | Decision | Priority |
|---|---|---|---|---|---|
| nse_intraday_trend_15m | Trend Following | Structurally dead (warm-up bug) | Viable by construction (targets 3–6× cost floor) — unvalidated | **IMPROVE & ACTIVATE** | **1** |
| nse_vwap_reversion | Mean Reversion | Trading; PF 0.37, WR 43% | FAILS as configured (RR ≈ 1.0 vs 0.2% cost); passes with RR ≥ 1.5 gate | **IMPROVE** | **2** |
| nse_orb_15m | Breakout | Trading; PF 0.17, WR 22% | FAILS as configured (cost > 1R); plausibly passes after rebuild | **REBUILD** | **3** |
| nse_momentum_v1 / us_momentum_v1 | Trend/Momentum | Structurally dead (interface bug) | n/a — duplicate of trend_15m if fixed | **MERGE** into trend_15m | 4 |
| nse_preclose_momentum | Momentum/Event | Structurally dead (config conflict) | **REJECTED under R1** (≤13-min hold vs 0.15–0.20% cost) | **RETIRE** (intraday form) | 5 |
| nse_scalp_1m | Scalping/Momentum | Self-gated; rejects 100% of own signals | **REJECTED under R1** — its own viability gate proves it | **RETIRE** (promote its filter to shared infra) | — |

The portfolio's core problem is not signal quality — it is that **the book trades 90+ times a day
with profit targets smaller than the round-trip cost.** Fix the economics first; everything else
is second-order.

---

## 0. Execution Reality Baseline — India (NSE/BSE) Full Cost Stack

This is the hurdle every trade must clear. All evaluations in this report use it (Rule R1).

### 0.1 Statutory + brokerage stack — intraday equity (NSE, Zerodha)

| Component | Rate (intraday equity) | Notes |
|---|---|---|
| Brokerage (Zerodha) | min(0.03%, ₹20) per executed order, both legs | ₹20 cap binds only above ~₹66,700/order notional |
| STT | 0.025% on **sell** side only | Delivery (CNC) is 0.1% on **both** sides — relevant if overnight variants are ever researched |
| Exchange transaction charges | NSE ~0.00297% of turnover (both sides); BSE ~0.00375% | |
| SEBI turnover fee | 0.0001% (₹10/crore) both sides | |
| Stamp duty | 0.003% on **buy** side | |
| GST | 18% on (brokerage + exchange txn + SEBI fee) | |
| DP charges | Nil for intraday | Applies only to delivery sells |

### 0.2 Worked examples (round trip, both legs)

| One-side notional | Brokerage | STT | Txn+SEBI | Stamp | GST | **Total statutory+brokerage** | As % |
|---:|---:|---:|---:|---:|---:|---:|---:|
| ₹25,000 | ₹15.00 | ₹6.25 | ₹1.54 | ₹0.75 | ₹2.98 | **≈ ₹26.5** | **0.106%** |
| ₹50,000 | ₹30.00 | ₹12.50 | ₹3.07 | ₹1.50 | ₹5.95 | **≈ ₹53** | **0.106%** |

On top of statutory + brokerage, add market microstructure costs:

| Component | Magnitude |
|---|---|
| Bid-ask spread | NIFTY-50 names ~0.03–0.05%; NIFTY-100/midcaps 0.05–0.15% |
| Slippage on market orders (normal conditions) | ~0.05%; worse on fresh breakouts (momentum-adverse fills) |
| **Total round-trip cost** | **≈ 0.15–0.30% of notional** |

The paper simulator charges 5 bps slippage + 5 bps half-spread per side ≈ **0.20% round trip**
(`services/execution_engine/service.py:2284`). This is **realistic for the statutory + spread +
slippage stack at this clip size — do not soften it.** If anything it is optimistic for midcap
breakout entries.

### 0.3 Broker comparison at this book's clip size (₹25–50k/order)

| Broker | Intraday brokerage model | Round-trip brokerage on ₹25k | Implication |
|---|---|---|---|
| Zerodha | min(0.03%, ₹20)/order | ₹15 (0.06%) | Best fit for small clips — percentage model scales down |
| Angel One | ₹20 flat/order (or 0.25% if lower) | up to ₹40 (0.16%) | Flat fee punishes small notionals |
| Upstox | min(0.05%, ₹20)/order | ₹25 (0.10%) | Between the two |

**Zerodha's percentage-capped model is the right primary broker for 2.5–5% NAV position sizes.**
Flat-₹20 brokers raise the cost floor ~10 bps at this clip size and would tighten Rule R2 further.

### 0.4 Margin, product, and lot-size rules

- **SEBI peak margin regime:** intraday leverage capped at VAR+ELM-based margins — effectively ~5× on liquid large caps, less on midcaps. No broker may offer more. The book's recommended gross exposure (≤1.5× NAV) sits far inside this; **leverage is not the binding constraint, edge is.**
- **MIS product:** broker force-square-off ~15:15–15:20 IST (platform squares off itself at 15:05 with 15:10 deadline — correct, keeps the broker fallback as last resort). No position survives the day; trend capture is capped at ~5 hours.
- **Margin shortfall penalties** (0.5–1% of shortfall/day) apply if peak margin is breached intraday — another reason the 5% NAV per-position hard cap must stay aligned between sizer and risk engine (it currently is).
- **Lot sizes:** cash equity trades at 1-share granularity — no lot constraint. Lot sizes become binding only if F&O strategies are added (e.g., NIFTY options); none exist in this book today. Any future options-income strategy must be evaluated per-lot (notional = lot × price), which changes minimum capital per trade materially.
- **Circuit limits:** 5/10/20% price bands on cash equities can trap intraday positions (no exit liquidity at band). Mitigation: liquid NIFTY-100 universe + MIS sizing keeps this tail small, but it is a real gap-risk analogue for intraday.

### 0.5 Broker API operational constraints (already partially handled by platform)

- Zerodha Kite: access token expires daily ~07:30 IST (handled — `ZerodhaTokenManager` + DynamoDB sessions table); order rate limits (~10/s, 200/min, ~3,000/day) — far above the recommended ≤15 entries/day; historical candle API rate limits already governed by the token-bucket limiter (ADR-012).
- Occasional WebSocket drops and API timeouts are assumed (retail internet) — the candle-stream watchdog, fill poller, and kill-switch staleness triggers already cover the failure modes that matter for these strategies' minute-scale holding periods.
- Partial fills at NIFTY-100 liquidity and ₹25–50k clips are second-order; market orders into fresh breakouts are the one place fills degrade meaningfully (addressed in the ORB rebuild spec).

### 0.6 Implications the current book violates

1. **Minimum viable target ≈ 0.5%** (2.5× cost). Observed median targets: ORB 0.33%, VWAP ~0.36%.
2. **Minimum viable stop ≈ 0.4%.** ORB's live median stop is 0.163% — round-trip cost alone exceeds 1R, so even winners are coin flips against the spread.
3. **Trade count is a cost decision.** 92 trades/day × ₹50–100 all-in ≈ ₹5,000–9,000 daily drag — almost exactly the observed daily loss. At ≤15 trades/day the same gross signal quality would have been near breakeven.

---

## 1. Audit Evidence (what the sessions actually show)

P&L reconstructed from archived `positions.json` (the text session reports show zeros — generated post-teardown):

| Session | Date | Realized P&L | SL / TP hits | Notes |
|---|---|---:|---|---|
| 7 | 2026-06-01 | +₹520 | 19 / 23 | pre-quality-gate |
| 8 | 2026-06-02 | ₹0 | 0 / 0 | no trades (candle stream issue) |
| 11 | 2026-06-04 | +₹602 | 30 / 62 | stale image, gates inactive — best day, still PF 1.01 |
| 12 | 2026-06-05 | **−₹14,063** | 68 / 63 | first valid quality-gate session |
| 13 | 2026-06-08 | −₹4,206 | 32 / 29 | |
| 14 | 2026-06-09 | −₹3,649 | 53 / 27 | ORB −₹2,626, VWAP −₹1,022 |
| 15 | 2026-06-10 | −₹8,180 (13:00 IST) | 59 / 23 | ORB −₹5,515, VWAP −₹2,664 |

Session 15 per-strategy economics (live data):

| Strategy | Trades | Win rate | Avg winner | Avg loser | Profit factor | Expectancy |
|---|---:|---:|---:|---:|---:|---:|
| nse_orb_15m | 54 | 22.2% | +₹94 | −₹158 | 0.17 | −₹102/trade |
| nse_vwap_reversion | 30 | 43.3% | +₹122 | −₹250 | 0.37 | −₹89/trade |

**The system never had edge.** Even the best session was breakeven gross. Losses are structural
(cost-blind targets + noise-scale stops + worst-trade selection), not a degradation of something
that once worked.

Root causes (verified in code + data; full detail in `memory/strategy_pnl_root_causes_2026_06_10`):

1. Cost-blind micro targets — targets below the §0 cost floor on both active strategies.
2. ATR(14) computed on 1-minute bars → noise-level stops (ORB takes the *tighter* of OR-mid vs ATR).
3. Only 2 of 6 strategies trade; 3 are structurally dead (interface bug, warm-up bug, config conflict).
4. `MAX_TRADES_PER_SYMBOL=1` admits the **first** signal of the day per symbol (09:30–10:00 chop); confidence formulas saturate ≈1.0, so conf/RR quality gates reject nothing AND saturated confidence unlocks 5% NAV "high conviction" sizing on every trade.
5. Strategy-aware TEE R-ladder cannot engage (0 breakeven shifts, 0 partials on 92 trades; 30s poll vs minutes-long trades; gross R rarely reaches 0.8 net of costs). Trailing fired 2× for +₹410 — the only net-positive exit mechanism.
6. Ops: 8 open positions with LTP source = `fill` (TEE price-blind until MIS); ENTRY_BLOCK DynamoDB read error (paper fail-open); `strategy_id` not persisted to positions.

---

## 2. nse_intraday_trend_15m — Trend Following — IMPROVE & ACTIVATE (Priority 1)

**A. Executive summary.** The only strategy whose design is cost-friendly by construction (few
trades, wide ATR(15m)-scale targets). It has never produced a single trade due to a warm-up bug.
Fix the plumbing, add an index regime gate, and it becomes the book's workhorse.

**B. Existing logic.** EMA(9/21) crossover on 15-minute candles; ADX(14) > 25 chop filter; price
on the correct side of EMA(50); 1.5×ATR stop, 2.5:1 RR; one signal per symbol per day; fires
09:30–14:15 IST.

**C. Weaknesses found.**
- Fatal: EMA(50) guard needs 52 fifteen-minute bars (`intraday_trend_15m_strategy.py:146`); a session has ~25, buffers are in-memory and wiped nightly (`docker-compose down -v`; warm-up replay `lookback_minutes=0`). **Zero trades ever — all expectations unvalidated.**
- ADX(14) on 15m needs ~28 bars to stabilize — same warm-up dependency.
- Hidden assumption: a 13:30 crossover still has runway to 2.5R before the 14:55 time-exit. It does not.
- 214-symbol scan can fire 10+ correlated signals on a strong index day — concentration, not diversification.

**D/E. Revised retail specification.**
1. **Warm up buffers from 2 days of 15m history at startup** (candle-cache / Kite historical via the existing `startup_warmup_replay` hook). The single change that takes it from 0 trades to functional.
2. Entry window 09:45–13:30 (skip late crosses with insufficient runway).
3. Stop = 1.5×ATR(14, 15m) — lands ~0.5–1.2%, above the R2 floor. Keep fixed TP suppressed; let the TEE R-ladder trail (`configs/exit_policy.yaml` already configures this).
4. Index regime gate: longs only when NIFTY > its day VWAP; shorts only below. One comparison, data already flows.
5. Max 5 concurrent, max 8 entries/day; prefer highest-ADX setups when oversubscribed.

**F. Expected impact (targets to validate, not promises).** WR 35–45%, PF 1.3–1.6, expectancy
positive net of §0 costs (targets 3–6× cost floor); drawdown driver = chop-day 1R-loss sequences
(capped by ADX + index gates). CAGR/Sharpe estimates deferred until walk-forward (§9) produces
real distributions — quoting numbers now would be curve-fit theater.

**G. Difficulty:** Easy–Moderate (warm-up plumbing only). **H. Confidence:** Medium (sound
design, zero live evidence). **I. Priority: 1.**

---

## 3. nse_vwap_reversion — Mean Reversion — IMPROVE (Priority 2)

**A. Executive summary.** The only strategy showing signs of life (43% WR in broken form). Edge
is real (intraday overextension snap-back in liquid large caps on range days) but the current
RR ≈ 1.0 against a 0.2% cost makes it a structural loser. An RR gate + trend-day stand-down is
the shortest path to the first profitable session.

**B. Existing logic.** Rolling intraday VWAP with ±2σ volume-weighted bands on 1m candles; entry
on close beyond band + wick rejection toward VWAP; stop = max(1×ATR(1m), half band-width); TP =
VWAP; 5-bar cooldown; fires 09:45–14:30.

**C. Weaknesses found (from live data).**
- Inverted asymmetry: stop ≈ 1–2σ wide, target = remaining distance to VWAP → observed median RR 1.10, min 1.00 (0.73 passed on 06-09). Mean reversion at RR ≈ 1 needs ~65% WR after costs; it runs 43%. Avg winner ₹122 vs avg loser −₹250. **Fails Rule R1 as configured.**
- Signal flooding: cooldown is not direction-aware → re-fired SELL on SHREECEM 16× into a trend (the classic MR failure mode: fighting a trend day; the per-symbol cap converts it into "take the first knife-catch, then spam rejections").
- Confidence saturates ≈0.97 → useless as filter and triggers 5% NAV high-conviction sizing on every trade — conviction scaling inverted.
- No regime awareness; fires from 09:45 on only 30 minutes of VWAP history (bands thinnest when volatility is highest).

**D/E. Revised retail specification.**
1. **Hard RR gate: skip unless distance-to-VWAP ≥ 1.5× stop distance, net of 0.2% cost** (Rule R2). Stop floor 0.4%. This one line removes the majority of observed losers.
2. Bands at 2.5σ; entries from 10:15 (60 bars of VWAP history).
3. Trend-day kill switch: stand down per-symbol after 2 consecutive same-direction stop-outs; stand down book-wide when NIFTY 15m ADX > 30. **Mean reversion must be allowed to not trade.**
4. Universe: NIFTY-50 only (tightest spreads; thin margins cannot afford midcap spreads).
5. Max 6 entries/day total. Time exit 45 min (verify the TEE `max_hold_minutes` path actually fires — 0 max-hold exits recorded to date).
6. Sizing fixed at 2.5% NAV until the confidence score is recalibrated (§8).

**F. Expected impact.** WR 50–58% at net RR ~1.2, PF 1.2–1.4; low per-trade variance; worst
regime = sustained trend days (gated). **G. Difficulty:** Easy (parameters/filters in existing
paths). **H. Confidence:** Medium-High. **I. Priority: 2** — likeliest first green session.

---

## 4. nse_orb_15m — Breakout — REBUILD (Priority 3)

**A. Executive summary.** Opening-range breakout is a genuine, documented Indian-equities edge —
but only with disciplined range/volume filters and wide stops. The current implementation (WR
22%, PF 0.17) fails Rule R1 outright: its median stop (0.163%) is smaller than the round-trip
cost. Rebuild on the same skeleton; smallest initial risk budget until validated.

**B. Existing logic.** 09:15–09:30 opening range; trade close beyond OR high/low; volume ≥ 1.5×
10-bar average; stop = *tighter* of OR-midpoint vs 1.5×ATR(14, 1m); TP = 2R; one signal per
symbol per direction per day; breakout watch until 14:45.

**C. Weaknesses found.**
- **The stop is the bug:** 1m-ATR stops land at median 0.163% — breakouts normally retest the boundary, and this design guarantees the retest kills the trade. Realized RR 0.6 vs designed 2.0.
- Confidence = 0.5 + overshoot/(2×tiny-ATR) → pins at 1.00 and **rewards chasing** — the worse the entry price, the higher the "confidence" and the bigger the size. Backwards on both axes.
- Volume confirmation silently passes when average volume is 0 (`orb_strategy.py:199` fallback).
- Breakout window to 14:45 — afternoon "breakouts" of a 15-minute morning range are noise; 54 of Session 15's 92 book-wide entries fired in 09:30–10:00 chop for −₹4,676.
- 56 trades/day vs the 3–8 best setups a retail ORB should take.
- Execution: market orders into fresh breakouts is where slippage is worst — the 5 bps assumption is optimistic *for this strategy specifically*.

**D/E. Revised retail specification.**
1. Stop = **wider** of OR-midpoint or 1.5×ATR(14, **5m**), floor 0.45%. TP stays 2R; TEE trails past 1.5R (config already supports).
2. Entry requires close > OR-high + 0.15×OR-range (buffer kills 1-tick fakeouts); breakout-bar volume ≥ 1.5× average with the zero-average fallback changed to **reject**; minimum OR range ≥ 0.5% of price (skip dead opens).
3. Window 09:30–11:30 only. Strategy cap 6 entries/day; select by relative volume when oversubscribed.
4. Confidence: closeness-to-boundary + volume ratio (entries *near* the boundary on strong volume are the good ones; stop rewarding chase distance).
5. Index filter: single-name longs only when NIFTY broke its own opening range up; shorts down.

**F. Expected impact.** WR 35–42% at realized net RR ~1.7, PF 1.2–1.5 — with a lumpy equity
curve (trend-day clusters carry the month; psychologically the hardest of the three).
**G. Difficulty:** Moderate. **H. Confidence:** Medium-Low — most validation-dependent.
**I. Priority: 3.**

---

## 5. nse_momentum_v1 / us_momentum_v1 — Trend/Momentum — MERGE

SMA(10/50) crossover with ATR(14) 2:1 stops — registered on the **TICK** interface while all
signal logic lives in `on_bar()`, which the tick path never calls. **It has never fired and
cannot fire.** Re-homed to candles (the only sensible fix) it becomes a slightly worse duplicate
of intraday_trend_15m: same crossover family, no ADX filter, no trend filter. A retail book
should not maintain two near-identical trend followers — that is maintenance burden plus
correlated risk masquerading as diversification.

**Decision:** fold its ATR/2:1 parameters into the trend_15m validation grid (§9) and retire the
separate registration. us_momentum_v1 (Alpaca) inherits the same dead interface — park it; US
trading is out of scope for this book. **Difficulty:** trivial. **Confidence:** High.

---

## 6. nse_preclose_momentum — Momentum/Event — RETIRE (current form)

Directional bias from the last 30 minutes of 5m candles, entering 14:45–15:10 IST — a window
that sits **entirely inside the platform's own `no_new_entry_after_ist: "14:45"` block**
(`configs/exit_policy.yaml:36`). Every signal it has ever produced was rejected before reaching
the broker path.

Even if unblocked, it **fails Rule R1 on arithmetic**: enter ~14:50, hard exit 15:03 → ≤13
minutes of hold, in which liquid NSE names typically move 0.1–0.25%, against a 0.15–0.20% cost
floor. The edge would need to be enormous; nothing in the data suggests it is.

The *real* version of this edge is overnight close-to-open drift — which requires CNC product
(0.1% STT both sides, DP charges, overnight gap risk, different margin treatment) and a
different risk framework. **Retire the intraday form; log the overnight variant as a
backtest-lab research item. Do not "fix" the config conflict just to activate a structurally
cost-trapped strategy.** **Confidence:** High.

---

## 7. nse_scalp_1m — Scalping — RETIRE (promote its filter)

EMA(9/21) on 1-minute candles with a hardened viability gate (Session 8 post-mortem): spread
estimate, TP ≥ 2× spread, net edge ≥ 0.12% after round-trip costs. **In Session 15 it rejected
110 of 110 of its own signals — all `net_edge_too_small`.** That is not a malfunction; it is
the only honest component in the strategy book, empirically proving every session that
1-minute scalping in NSE cash equities cannot clear retail costs (Rule R1). Scalping at this
frequency is an HFT/colocation game — exactly what the retail constraint set excludes.

**Decision:** retire the strategy; **extract `_check_viability` into shared code and make it a
mandatory pre-trade gate for every strategy.** This is the highest-ROI change in this entire
report. **Confidence:** High.

---

## 8. Portfolio Construction, Capital Allocation, Risk Framework

Three survivors, deliberately regime-complementary:

| Strategy | Regime it earns in | Risk budget share | Max concurrent | Max entries/day |
|---|---|---|---|---|
| intraday_trend_15m | Trend days | 40% | 5 | 8 |
| vwap_reversion (improved) | Range days | 35% | 3 | 6 |
| orb_15m (rebuilt) | Trend-day opens | 25% | 3 | 6 |

- **Per-trade risk: 0.25% NAV (₹2,500)** — the sizer's existing `MAX_LOSS_PCT`; with R2-compliant stop widths it now actually binds. **Freeze the 5% high-conviction upsize** until a confidence score demonstrates calibration (today it max-sizes the worst trades). Default position notional 2.5% NAV.
- **Book-wide:** max 15 entries/day (Rule R3; cost drag falls from ~₹6–8k/day to <₹1.5k), max 8 concurrent positions, gross exposure ≤ 1.5× NAV (well inside SEBI peak-margin MIS limits — leverage is not the bottleneck, edge is).
- **Daily kill:** −1.5% NAV (₹15,000) halts new entries (exits always run — existing invariant). **Weekly:** −3% NAV stands the book down for the week. Both evaluated by existing kill-switch/cap machinery, report-only until approved.
- **Highest-value portfolio alpha add:** a one-signal regime gate — NIFTY 15m ADX (or day-range vs 10-day ATR). Trend regime → ORB + trend_15m active, VWAP throttled; range regime → inverse. No ML required to start.
- MIS reality: everything flat by 15:05 (platform) / 15:15 (broker fallback). Accepted constraint; caps trend capture at ~5 hours.

---

## 9. Validation Standards (before any size increase)

Per modified strategy, using the existing `services/strategy_engine/backtesting/backtester.py`
with the §0 cost model (and the AWS backtesting lab when built):

1. In-sample on ~2 years of 15m/5m candle data.
2. Out-of-sample on the most recent 6 months.
3. Walk-forward: 3-month train / 1-month test rolls.
4. Parameter sensitivity: ±50% on every parameter; **reject anything whose PF flips sign inside the band** (the current ORB would have failed this instantly).
5. Monte Carlo resequencing of trades for drawdown distributions.
6. Regime split: trend vs range days (NIFTY ADX) scored separately.

**Gate to keep a paper slot: OOS PF > 1.2 net of §0 costs.** Live paper gates unchanged from
CLAUDE.md: expectancy > 0, PF > 1.2, ≥5 consecutive valid sessions; verdict stays
`PAPER_OPTIMIZATION` until then. **No strategy is promoted on pre-cost performance (Rule R1).**

---

## 10. Machine Learning Verdict

**Not yet — with one exception.** Meta-labeling / GBT quality scoring (the ai_engine plan)
learns *which signals to skip*; trained on the current book it would learn exactly one thing —
"skip everything" — because gross edge ≈ 0. ML filters amplify an edge; they do not create one.

Sequence: fix economics → accumulate 20–30 sessions of attributable trades (persist
`strategy_id` on positions) → then train the quality scorer via the planned Phase 9 leakage-free
dataset. The exception worth doing sooner: the **regime classifier** — start as the rule-based
NIFTY ADX gate (§8); upgrade to the planned HMM / simple GBT only if the rule demonstrably lags
regime turns. Reject anything beyond that — it fails the "one person maintains this for years"
test.

---

## 11. Roadmap

1. **Week 1 — stop the bleed (config + small code):** universal viability gate (extracted from scalp_1m); VWAP RR ≥ 1.5 + entry caps; ORB stop/window/volume fixes; global 15-entry/day cap; retire scalp_1m, preclose, momentum registrations. Ops fixes: LiveQuotePoller coverage for all traded symbols; ENTRY_BLOCK DynamoDB read error.
2. **Week 1–2 — activate the workhorse:** trend_15m warm-up replay + index gate; persist `strategy_id` on positions; gross-vs-net cost line in `paper_session_report.py`.
3. **Weeks 2–4 — validate:** backtest revised specs per §9; 5 paper sessions; per-strategy PF/SL-TP gates, not just book-level.
4. **Month 2+:** regime-gate upgrade; conviction-sizing recalibration; overnight close-to-open drift research in the backtest lab (CNC cost stack: 0.1% STT both sides + DP charges + gap risk); quality-scorer dataset accumulation.

---

## 12. Bottom Line

This book loses money for boring, fixable reasons — cost-blind targets, noise-scale stops, and
three strategies that never actually ran. The committee's bet: VWAP-reversion (improved)
delivers the first profitable sessions within two weeks of changes; trend_15m becomes the
long-run P&L engine; ORB earns its slot back only through validation; scalp and preclose are
rejected under the after-cost rule — the former by its own viability gate, the latter by
arithmetic. Everything stays paper until the existing five-session gates pass. No exceptions.

---

*Related records: `memory/strategy_pnl_root_causes_2026_06_10` (auto-memory),
`docs/live-readiness/tee-profit-capture-analysis.md`,
`docs/live-readiness/tee-strategy-aware-paper-validation-report.md`,
`memory/open_tasks.md` §5-Session Live Gate Progress.*
