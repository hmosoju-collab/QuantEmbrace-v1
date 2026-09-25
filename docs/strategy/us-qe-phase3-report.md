# Phase 3 — qe US Market Support (ADR-041)

_2026-07-14 · US equities pivot Phase 3 · Plan: `docs/strategy/us-equities-pivot-plan.md`_

**Status: BUILT. Infrastructure only — no US strategy is wired into `mode=sim`
yet (that is Phase 4, porting the shortlist into `qe/strategy/`). NSE ₹0.00
parity re-verified green. Awaiting operator approval → Phase 4.**

---

## 1. Scope

Phase 3 gives the `qe` backtest engine the ability to run a `market=US` book at
all: cost model, trading-date timezone/calendar, a single-symbol benchmark, and
currency-aware reporting. It deliberately does **not** touch:

- The paper/live path (`qe/engine/paper.py`, `qe/livecheck/drills.py`) — still
  NSE/IST-only by design. US paper trading is Phase 5.
- Strategy code (`qe/strategy/`) — RPLITE itself is ported in Phase 4.
- `qe/research/wf_v1.py` — the v1-cross-check walk-forward path is
  intentionally NSE-only (it exists to reproduce v1's own NSE behavior).

## 2. What changed

| Component | Change |
|---|---|
| `qe/costs.py` | Added `USEquityCosts` (SEC Section-31 fee + FINRA TAF, both SELL-side only; 5bps/leg slippage; $0 commission) and `cost_model_for_market()` dispatcher. Same interface shape as `EquityDeliveryCosts` (`leg_cost_frac`/`round_trip_frac`/`version`) — `SimBroker` needs no changes. |
| `qe/clock.py` | Added `NY` (America/New_York) and `market_tz()` dispatcher, additive only — `SimClock`/`WallClock` (paper/live) untouched. |
| `qe/data/panel.py` | `Panel.date_at()` no longer force-converts to IST — it reads the index's own tz-aware value, which `load_panel(tz=...)` already normalizes correctly per market. Fixes the "+1 day mislabel" gap flagged in the P1 report. |
| `qe/engine/sim.py` | `run_sim()` now dispatches `tz=market_tz(config.universe.market)` to `load_panel()` and `cost_model_for_market(config.universe.market)` to `SimBroker`. `month_end_positions`/`rebalance_schedule` no longer force-convert to IST — they trust the panel's own (already-correct) tz, so they work for any market without threading a tz parameter through. Unrecognized markets fail closed (`ValueError`) rather than silently defaulting to NSE costs/timezone. |
| `qe/research/benchmark.py` | Added `buy_hold_benchmark_return()` — simple single-symbol buy-and-hold return, for the US book's SPY benchmark (the existing `ew_benchmark_return` cross-sectional-liquid-universe construction is NSE-specific and unchanged). |
| `qe/reporting/session_report.py` | `render_session_report()` now prints `$` for `market=US` and `₹` for `market=NSE` (read from the journaled config), instead of a hardcoded `₹`. |

**"NYSE calendar"**: no separate holiday-calendar library was added. Trading
days come directly from which dates exist in the panel (same as NSE), and the
US lake (Phase 1) already contains only real NYSE trading days — verified
gapless during the Phase 2b root-cause (5412/5412 trading days present, no
holiday-driven gaps across SPY/TLT/GLD). The tz fix above is what makes that
calendar line up correctly instead of being IST-shifted.

## 3. Tests

`tests/qe/test_us_market.py` (13 new tests): cost/tz dispatch, `USEquityCosts`
BUY-vs-SELL cost split and magnitude sanity vs NSE, `date_at`/month-end
correctness on a synthetic NY-tz panel (including a leap-year Feb 2024
month-end and partial-final-month exclusion), the SPY buy-and-hold benchmark,
and NAV currency symbol switching — each paired with an explicit "NSE
unchanged" counterpart test.

**Full `tests/qe/` suite: 83/83 passing**, including every pre-existing parity
test (`test_costs_parity.py`, `test_parity_delivery_book.py`,
`test_wf_v1_parity.py`, `test_walkforward_study.py`) — the ₹0.00 parity path is
provably untouched.

## 4. Remaining before Phase 4

- Operator approval of this report.
- XSMOM survivorship falsification (unrelated, still open from Phase 2).
- Phase 4 itself: port RPLITE into `qe/strategy/` as a new strategy kind
  (`StrategyConfig` currently only models `factor_book`/delivery/momentum —
  RPLITE's inverse-vol risk-parity logic needs a new kind), qe-vs-LEAN parity
  re-check inside qe, walk-forward OOS, and the pre-registered US Forward Gate.
