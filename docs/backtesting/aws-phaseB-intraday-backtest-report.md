# AWS Backtesting Lab — Phase B Report: Intraday Strategy Backtest (Zerodha Kite)

**Status:** COMPLETE — awaiting human approval
**Date:** 2026-06-14
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

First intraday evaluation of the four session-based strategies that the daily
Bhavcopy lake could not assess. Per-day execution (fresh strategy state per
trading day) models daily reset + MIS EOD flatten.

| Parameter | Value |
|---|---|
| Data source | Zerodha Kite `historical_data` (`zerodha_kite`) |
| Trust | HIGH (provenance) — LIMITED depth (~3 yr, liquid names); advisory only |
| Period | 2022-01-01 → 2024-12-31 |
| Universe | 47 NSE symbols |
| Cost model | `in-eq-delivery-2024.10` (NSE equity, statutory stack) |
| Slippage | 5 bps per leg |
| Code version | `intraday-phaseB-v1` |
| Data version | `zerodha-kite-intraday-v1` |

**Data integrity:** the `Backtester` multi-symbol end-of-day flatten bug (cross-symbol
mark-out, fixed 2026-06-14, regression-pinned by `test_eod_multi_symbol_uses_own_symbol_price`)
is corrected in these results. An earlier uncorrected run produced physically impossible
figures (>4000% win rate, ₹crore P&L) and was discarded. The numbers below are post-fix.

---

## Per-Strategy Results

| Strategy | Interval | Days | Trades | Win % | Expectancy ₹ | Profit Factor | Net P&L ₹ | Cost drag ₹ | Verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| `orb` | 1m | 743 | 2403 | 33.2 | -94.0 | 0.445 | -225,949 | 151,052 | REJECT |
| `vwap_reversion` | 1m | 743 | 131 | 22.9 | -174.0 | 0.253 | -22,791 | 14,174 | REJECT |
| `trend_15m` | 15m | 743 | 0 | 0.0 | 0.0 | 0.000 | 0 | 0 | NO_TRADES |
| `preclose` | 5m | 743 | 10596 | 12.0 | -83.6 | 0.049 | -886,007 | 583,555 | REJECT |

Gates (per strategy): expectancy > 0 · profit factor > 1.2 · net P&L > 0.
`ELIGIBLE_FOR_PAPER_PRIORITIZATION` = all gates pass · `PAPER_OPTIMIZATION` = 
positive but not all gates · `REJECT` = no positive edge · `NO_TRADES` = no signals fired.

---

## Advisory Conclusions

> **Backtesting can recommend. Backtesting cannot promote. A human approves all production changes.**

- Results are advisory and non-authoritative for promotion.
- Zerodha intraday is HIGH trust for provenance but LIMITED depth (~3 yr, liquid
  names). A positive result warrants procuring deeper licensed vendor data
  (TrueData / GlobalDataFeeds) before further validation — per the intraday
  data procurement memo's staged path.
- Per-day capital reset makes Sharpe / annualised return unreliable; expectancy,
  profit factor, and win rate are the valid edge metrics here.
- `trend_15m` = 0 trades is a **real result, not a warm-up artifact**. It runs in warm-start
  mode (persistent instance + `reset_daily()` between days, carrying the OHLCV buffers across
  sessions — like the live ADR-031 warm-start), so its EMAs warm fully (buffer depth 150 ≫ 52
  needed). It still fires 0 signals at its production config because the **ADX≥25 and
  confidence≥0.65 filters are mutually exclusive on NIFTY50 15m data** (each alone admits
  ~46–75 signals; together, 0). With both filters disabled, the raw trend logic trades ~4,857
  times (2023–24) but loses: expectancy −₹76, PF 0.30, net −₹368k → REJECT.

### Bottom line

**No intraday strategy shows edge.** The three that trade at their production config all lose
to the NSE statutory cost stack — orb (PF 0.45), vwap_reversion (PF 0.25), preclose (PF 0.05,
worst); cost drag alone is ₹151k / ₹14k / ₹584k. `trend_15m` either does not trade (default
filters mutually exclusive) or loses with filters off (PF 0.30). This is consistent with the
platform's standing thesis (`strategy_pnl_root_causes`, India cost mandate): naive intraday
entries do not clear round-trip costs. **No intraday strategy is eligible for paper
prioritisation on this evidence.** Momentum-on-daily (Phase 15C, PAPER_OPTIMIZATION) remains
the only strategy with a positive advisory edge.
- Live trading remains BLOCKED — paper session gates (≥5 consecutive passing
  sessions) are unaffected by this advisory backtest.

---

## Next Options (operator selects)

**A. Procure deeper licensed intraday** for any strategy showing edge here
   (targeted symbols/years from TrueData / GlobalDataFeeds), then re-validate.
**B. Intraday walk-forward** — parameter walk-forward (like Phase 15C for momentum)
   on the strategy/strategies with the strongest first-pass edge.
**C. Continue paper sessions** — Session 18 with ADR-030 quality gates.
**D. GenAI analysis** (Phase 10) over the intraday artifacts.

---

## Approval Required

Per governance: **a human must approve this report.**

- [ ] Per-day execution model + limitations understood
- [ ] Zerodha limited-depth / advisory-only nature confirmed
- [ ] Per-strategy verdicts reviewed
- [ ] No production changes will be made based solely on this report
- [ ] Next option selected from A / B / C / D above
