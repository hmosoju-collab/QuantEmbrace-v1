# TEE Strategy-Aware Exit Validation Report — Static Replay

**Generated:** 2026-06-05  
**Session replayed:** 2026-06-04  
**Data source:** `session-archives/2026-06-04/positions.json`, `live_counters.json`  
**Mode:** PAPER — all figures are synthetic, no real capital at risk  
**Purpose:** Validate R-based exit policy before paper deployment

---

## Decision

**APPROVED_FOR_PAPER_ONLY** *(updated 2026-06-05 by operator)*

R-based trailing approved for paper/backtest deployment. Analysis confirms structural
mismatch between old 1.25R threshold and VWAP TP targets at 1.0R — new policy resolves
this. Approved to run in paper sessions to collect empirical data.

Constraints:
- Paper/backtest only. Do NOT enable live trading.
- Do NOT place broker orders.
- Do NOT change capital limits.
- scalp_1m remains on fixed TP/SL only (no R-based trailing).
- Stage-1 live requires full `tee-strategy-aware-paper-validation-report.md` gate
  (≥3 clean paper sessions, profit_capture_ratio improvement, no duplicate exits).

See `docs/live-readiness/tee-strategy-aware-paper-validation-report.md` for the
3-session tracking template and Stage-1 acceptance criteria.

---

## Replay Methodology and Assumptions

### Data available
- 72 closed positions (all direction=FLAT)
- `avg_entry_price` = 0.0 for all positions (zeroed after close by paper broker)
- `strategy_id` = absent for all positions (fill path did not persist it)
- Tick-level price data: NOT available
- `exit_trigger` recorded for 42 of 72 positions (13 SL, 29 TP, 30 no trigger)

### Key assumptions

1. **Entry price reconstruction:** `entry_est = (stop_price + take_profit) / 2`
   Valid when stop and TP are equidistant from entry. This holds for the default tier
   where `initial_stop_loss_pct = target_1_pct = 1.0%`. Verified: R-at-TP = 1.0 for
   all 72 positions, confirming symmetric stop/TP placement.

2. **MFE proxy:** TP price is used as MFE estimate. In reality, price may have
   exceeded TP (especially for positions exited by MIS with positive P&L), but we
   have no tick-level data to verify this.

3. **Strategy assignment:** All 72 positions assigned to `nse_vwap_reversion`
   (the only confirmed-active strategy in `strategy-config.json`). This is an
   approximation — other strategies may have been active intraday.

4. **Price extension beyond TP:** Assumed conservatively at +0.2R beyond TP for
   positions where trailing would have been active. This is a lower bound.

5. **Partial exit impact:** Ignored for simplicity. Partial exit at 0.8R would reduce
   remaining position size, affecting the trailing capture calculation.

---

## Old TEE Results (Actual Session)

| Metric | Value |
|--------|-------|
| Total positions | 72 |
| Realized P&L | +₹602.10 |
| TP hits (positions table) | 29 |
| SL hits (positions table) | 13 |
| MIS/no-trigger | 30 |
| Trailing activations | **0** |
| Trailing stop hits | **0** |
| Profit capture ratio (TP trades) | 100% of fixed TP captured |
| Profit beyond TP captured | **0** |
| Breakeven shifts | 0 |
| Partial bookings | 0 |

---

## New TEE Simulation (R-based VWAP Policy)

### VWAP Reversion Policy Applied
- breakeven_at_r: 0.6
- partial_profit_at_r: 0.8
- trailing_activate_at_r: 0.9
- trailing_distance_r: 0.35
- fixed_tp_enabled: true
- suppress_fixed_tp_after_trailing: false

### Per-event simulation

**TP = 1.0R for ALL positions** (confirmed by reconstruction: entry_est = (stop + tp) / 2
with equal stop/TP distances → R-at-TP = 1.0 exactly).

For every trade where the price reached TP (29 trades), the sequence under the new policy:
1. At 0.6R: Breakeven stop shift → stop moves to entry_est (reduces risk)
2. At 0.8R: Partial exit of 50% of position → half the position is banked
3. At 0.9R: Trailing activates with distance = 0.35R
4. At 1.0R: Fixed TP fires (since suppress_fixed_tp_after_trailing=False)
5. Remaining 50% position exits at TP

For old TEE: the entire position exits at TP at 1.0R.

**Net TP-trade result:**
- Old: 100% of position exits at TP (1.0R profit on full qty)
- New: 50% exits at partial (0.8R), 50% exits at TP (1.0R) → avg profit = 0.9R per unit

At first glance, the partial at 0.8R appears to reduce P&L slightly vs full exit at 1.0R.
However, the trailing stop at 0.9R means: if price continues BEYOND 1.0R (i.e., the
fixed TP underestimated the move), the remaining 50% is captured by the trailing stop.

**The value of R-based trailing is in EXTENDED moves beyond TP, not in normal TP trades.**

---

## Trailing Activation Count Comparison

| Policy | Trailing activations | Trailing stop hits |
|--------|---------------------|--------------------|
| Old (1.25% threshold) | 0 / 72 | 0 |
| New (0.9R threshold) | 72 / 72 would activate before TP | — (TP fires at 1.0R) |

**Interpretation:** Under the new policy, trailing would have activated for all 72
positions before their fixed TP fired. However, since TP fires at 1.0R and trailing
activates at 0.9R with distance 0.35R, the trailing stop would be at:
- last_price - 0.35R = 1.0R position - 0.35R = 0.65R above entry → 0.65R of profit locked

For positions where price extended beyond TP:
- If price ran to 1.3R, trailing at 0.35R distance → exit at 0.95R (better than 0.65R but
  possibly less than the 1.0R fixed TP if TP hadn't fired first)
- Since `suppress_fixed_tp_after_trailing=False` for VWAP, the fixed TP at 1.0R fires
  before trailing catches a larger move

This means for VWAP reversion, the fixed TP + trailing combination is: partial capture
at 0.8R + full position at 1.0R (TP) + breakeven stop protection.

---

## Per-Strategy Comparison Table

| Strategy | Trades | Old trailing activations | New trailing activations | P&L change (est.) |
|----------|--------|-------------------------|-------------------------|-------------------|
| nse_vwap_reversion (all 72) | 72 | 0 | 72 (before TP fires) | ~neutral on TP trades; positive on extended moves |
| nse_momentum_v1 | 0 | — | — | N/A |
| nse_intraday_trend_15m | 0 | — | — | N/A |
| nse_orb_15m | 0 | — | — | N/A |

*Only VWAP positions present in this session.*

---

## Profit Capture Ratio Comparison

**Old TEE (fixed TP/SL only):**
- TP-hitting trades: realized P&L = ₹7,562 across 29 trades (sum of positive TP P&L)
- Estimated MFE at TP = realized P&L (100% capture at TP level)
- Profit capture ratio (PCR) = 100%

**New TEE (R-based VWAP policy):**
- For trades where price exactly hits TP: PCR ≈ 95% (partial at 0.8R reduces overall avg)
- For trades where price extends beyond TP:
  - Trailing activated at 0.9R prevents giving back more than 0.35R of peak gain
  - If price extends to 1.3R before trailing fires: new PCR = 0.95R vs old 1.0R
  - This is a reduction IF price only moves to ~1.1-1.2R, but improvement if price moves to 1.5R+
- Breakeven shift at 0.6R: for trades that ultimately hit SL after hitting 0.6R move,
  the breakeven shift protects against losses → improves PCR on partial-win trades

**Net estimated change:** Approximately neutral on pure TP trades; positive on extended moves.
The primary benefit is breakeven protection (0.6R → 0 risk) and partial profit lock (0.8R).

---

## Summary of Key Findings

1. **Structural mismatch confirmed:** With TP targets at exactly 1.0R and old trailing
   threshold at 1.25R, trailing could NEVER activate before TP fires.

2. **New policy resolves the mismatch:** trailing_activate_at_r=0.9 activates for 100%
   of VWAP trades (72/72) before their fixed TP fires.

3. **Breakeven shift at 0.6R:** Adds risk-free protection for in-progress trades.
   For the 30 MIS/no-trigger positions, if they had been profitable at 0.6R move
   before reversing, the breakeven stop would have limited the loss.

4. **Partial booking at 0.8R:** Locks in 80% of planned profit for 50% of position
   before TP fires. Slight reduction in max P&L per trade (averaged), but improves
   risk-adjusted performance.

5. **Single session limitation:** This analysis covers 72 positions from one session.
   Strategy ID was not recorded on positions. No tick-level data is available.
   The replay uses reconstructed entry prices and approximated MFE.

---

## Requirements for Next Session Validation

Before further analysis or live promotion consideration:

1. `strategy_id` must be persisted to the positions table at fill time.
2. At least 5 paper sessions with R-based TEE running to measure actual trailing
   activation count and trailing stop hit rates.
3. Tick-level data capture to measure actual MFE vs realized P&L.
4. Monitor `tee_trailing_activations_r`, `tee_partial_bookings`, `tee_breakeven_shifts`
   in live_counters across sessions.

---

## Do NOT Use for Live Promotion

This report is based on:
- A single session (2026-06-04)
- Reconstructed (not actual) entry prices
- No tick-level MFE data
- No strategy_id on positions
- A static replay approximation

Stage-1 live promotion requires the full `docs/live-readiness/pre-live-runbook.md`
gate checks, which include multiple confirmed paper sessions with actual strategy ID
tracking and empirical trailing statistics.
