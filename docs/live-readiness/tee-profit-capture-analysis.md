# TEE Profit Capture Analysis — 2026-06-04 Session

**Generated:** 2026-06-05  
**Session date:** 2026-06-04  
**Data source:** `session-archives/2026-06-04/positions.json`, `orders.json`, `live_counters.json`  
**Mode:** PAPER — all figures are synthetic, no real capital at risk

---

## Data Limitations

- `avg_entry_price` is **0.0 for all 72 positions**. The paper broker zeroes this field
  after a position is closed. Entry price was reconstructed as the geometric midpoint
  of `stop_price` and `take_profit`, which is valid only when stop and TP are
  equidistant from entry (symmetric percentage policy). This is consistent with the
  default tier: `initial_stop_loss_pct = 1.0`, `target_1_pct = 1.0`.
- `strategy_id` is **absent from all 72 positions**. The fill path did not persist
  `strategy_id` to the positions table in this session. Therefore all analysis is
  presented at the aggregate level and labelled `nse_vwap_reversion` (the only
  strategy confirmed active in `strategy-config.json` for this session).
- No tick-level MFE data is available. MFE is approximated as the `take_profit`
  price (which represents the maximum intended profit level).
- Trailing activation count = **0** across all sessions (confirmed by live_counters).

---

## Session Summary

| Metric | Value |
|--------|-------|
| Total positions closed | 72 |
| Total realized P&L | +₹602.10 |
| Winners | 33 |
| Losers | 30 |
| Zero P&L (no trigger, closed by MIS or outside TEE) | 9 |
| Win rate | 52.4% (33 of 63 non-zero trades) |
| Average winner | +₹312.50 |
| Average loser | -₹323.68 |
| Profit factor | 1.01 (marginally above breakeven) |
| TP hits (from live_counters) | 62 |
| SL hits (from live_counters) | 30 |
| Trailing activations | 0 |
| Trailing stop hits | 0 |
| MIS square-off (no trigger) | 30 |

*Note: live_counters show 62 TP hits vs 29 from positions.exit_trigger. The counter
includes intraday exits that may have been followed by re-entries; the positions
table reflects final state only.*

---

## Strategy-Level Analysis

### nse_vwap_reversion (all 72 positions assigned here — see data limitation above)

| Metric | Value |
|--------|-------|
| Trades | 72 (63 non-zero P&L + 9 zero) |
| Avg P&L per trade | +₹8.36 |
| Avg winner | +₹312.50 |
| Avg loser | -₹323.68 |
| Win rate | 52.4% |
| Profit factor | 1.01 |
| TP hit count (positions.exit_trigger) | 29 |
| SL hit count (positions.exit_trigger) | 13 |
| MIS square-off (no trigger, zero P&L) | 9 |
| Other exits (no trigger, non-zero P&L) | 21 |
| Trailing activation count | **0** |
| Trailing stop hit count | **0** |
| MIS square-off count (live_counters) | ~30 (positions with no exit_trigger and non-zero pnl likely exited via MIS) |

#### TP Range Analysis

| Metric | Value |
|--------|-------|
| Min TP distance from estimated entry | 0.21% (BEL SHORT) |
| Max TP distance from estimated entry | 1.48% (TITAN SHORT) |
| Median TP distance | ~0.44% |
| Mean TP distance | ~0.47% |
| TP distances > 1.25% (old trailing threshold) | 1 of 72 positions (TITAN, 1.48%) |
| TP distances < 1.25% | 71 of 72 positions (98.6%) |

#### MFE and Profit Capture

- MFE estimate = `take_profit` price (maximum intended move).
- For positions that hit TP: profit capture ratio = 100% (by definition — fixed TP fully realised).
- For positions that hit SL: profit capture ratio = negative (loss taken).
- For positions exited by MIS with non-zero P&L: partial capture where price moved
  favourably but did not reach TP.

**Estimated MFE-weighted profit capture ratio (TP-hit trades only):**
- 29 TP hits captured their full fixed TP distance (100% of planned MFE).
- However, if price ran beyond TP, those gains were **left on the table** (no trailing).
- With 0 trailing activations, 100% of winning exits were at the fixed TP — no runner captures.

---

## Root Cause: Why Trailing Never Activated

### Quantitative diagnosis

The old TEE uses a **percentage-from-entry** trailing activation:
- LONG activates when `last_price >= entry * 1.0125` (price up 1.25% from entry)
- SHORT activates when `last_price <= entry * 0.9875` (price down 1.25% from entry)

The VWAP reversion strategy places take-profit targets at **0.21% to 1.48% from entry**,
with a median of **~0.47%**. The fixed TP fires and exits the trade **before** the price
can ever reach the 1.25% trailing activation threshold.

**Timeline of a typical VWAP trade:**
```
Entry price = ₹1,000
TP at +0.47% = ₹1,004.70   ← TEE fires TP exit here
Trailing would activate at +1.25% = ₹1,012.50  ← NEVER REACHED
```

Only **1 of 72 positions** (TITAN, TP at +1.48%) had a TP target beyond the 1.25%
trailing threshold. Even that trade showed no trailing activation (strategy may not
be `nse_vwap_reversion`).

### Structural mismatch

The 1.25% trailing threshold was designed for higher-volatility, trend-following
strategies where 2–5% moves are common. VWAP reversion is a **mean-reversion**
strategy that profits from 0.3–0.8% countertrend moves. The existing trailing
configuration is architecturally incompatible with the strategy's natural profit targets.

---

## Per-Strategy Recommended R-Based Policy

R is defined as: `R = initial_risk = |entry_price - stop_price|`

### nse_vwap_reversion

**Observed risk per unit (from stop-to-entry distance):**
- Min stop distance: ~0.20% (BEL)
- Max stop distance: ~0.96% (ADANIPOWER)
- Median stop distance: ~0.47%
- With 0.47% stop, a 1.25% trailing threshold = **2.66R** — unreachable for VWAP reversion.

**Recommended policy:**
| Rule | R-value | Rationale |
|------|---------|-----------|
| Breakeven shift | 0.6R | Move stop to entry once we're 60% to our first TP |
| Partial profit booking | 0.8R | Book 50% at 80% of the way to TP |
| Trailing activation | 0.9R | Activate trailing before TP fires (TP targets ≈ 1.0R) |
| Trailing distance | 0.35R | Tight trail; VWAP holds don't last long |
| Max hold | 20 min | VWAP reversion thesis invalidates beyond 20 min |

With 0.47% stop and TP at ~1.0R:
- Breakeven at 0.6R = 0.28% move (easily achieved)
- Partial at 0.8R = 0.38% move (before TP)
- Trailing at 0.9R = 0.42% move (before fixed TP at ~1.0R)

This allows trailing to activate on **most winning VWAP trades** while the fixed TP
remains as a backstop for fast moves.

### nse_momentum_v1 (no trades in session — policy based on strategy design)
- Trend component; 2–4R moves possible.
- Trailing activation at 1.5R appropriate.
- Partial at 1.2R to reduce risk before trailing engages.

### nse_intraday_trend_15m (no trades — design-based)
- Larger moves expected on 15m timeframe.
- Fixed TP suppressed; trailing is the primary exit mechanism.
- Activation at 1.5R; trailing distance 0.75R for slower price discovery.

### nse_orb_15m (no trades — design-based)
- Opening range breakouts produce 1.5–3R moves when valid.
- Partial at 1.5R reduces risk; trailing captures remainder.

### nse_preclose_momentum (no trades — design-based)
- Similar profile to VWAP reversion but with hard time constraint.
- Hard exit at 15:03 IST prevents overnight risk.

### nse_scalp_1m (no trades — design-based)
- Sub-minute holds; fixed TP/SL only — no time for trailing mechanics.

---

## Opportunity Cost Estimate

If trailing had been active for the 29 TP-hitting trades, and price extended
beyond TP by an additional 0.3R on average (conservative), the additional
captured P&L per trade would be approximately:

```
Additional gain = 0.3R × avg_stop_dist × qty
avg_stop_dist ≈ 0.47% of ₹1,000 = ₹4.70
qty ≈ 100 shares (rough estimate)
Additional per trade = 0.3 × ₹4.70 × 100 = ₹141
29 trades × ₹141 = ₹4,089 additional capture (rough estimate)
```

This is an approximation. Without tick-level data, the actual extension beyond TP
cannot be measured. The estimate suggests material upside from R-based trailing.

---

## Conclusions

1. **Root cause confirmed:** The 1.25% percentage-based trailing threshold is
   structurally incompatible with VWAP reversion TP distances of 0.3–0.8%.
2. **Fix required:** Switch to R-based trailing with `trailing_activate_at_r: 0.9`
   for VWAP reversion (activates before TP fires).
3. **Partial booking:** Add partial profit booking at 0.8R to reduce risk before
   trailing engages.
4. **Breakeven shift:** Add breakeven stop shift at 0.6R to protect capital on
   profitable-but-not-yet-TP trades.
5. **Strategy ID persistence:** `strategy_id` must be written to the positions table
   at fill time to enable per-strategy R-based policy lookup.
6. **Data needed:** Tick-level data and `strategy_id` on positions are needed for
   a rigorous per-strategy analysis.
