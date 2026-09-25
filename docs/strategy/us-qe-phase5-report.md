# Phase 5 — US Forward Paper Book: paper==sim, Fold into Cadence (ADR-041)

_2026-07-14 · US equities pivot Phase 5 · Plan: `docs/strategy/us-equities-pivot-plan.md`_

**Status: BUILT + TESTED. The paper engine now has full US market support and
paper==sim is proven for RPLITE exactly as it is for the NSE books. The book
is NOT YET ACTIVATED — no real paper session has been run, no persisted book
state exists. Awaiting operator approval before the first real session.**

---

## 1. Scope and a deliberate boundary

This phase extends `qe/engine/paper.py` (the real-time WallClock engine) to
support `market=US`, and wires the US RPLITE book into the paper CLI and the
monthly cadence documentation. It does **not** run a real paper session — that
is the operator's activation decision, and creating persisted book state
(`backtest-data/paper_book/qe_rplite-book-paper_state.json`) is exactly the
kind of "affects paper trading behavior" action that requires explicit
approval, not something to do unilaterally while building the capability.

## 2. What changed

| Component | Change |
|---|---|
| `qe/clock.py` | `WallClock` gained an optional `tz` constructor arg (default IST — no existing NSE caller changes). **Found and fixed a real bug**: `SimClock.today()` hard-converted to IST regardless of the clock's own timezone. For an NSE clock this is a no-op (already IST), but for a US clock pinned at NYSE close (16:00 ET = 20:00 UTC = 01:30 IST *the next day*), it silently returned the wrong calendar date — every US paper session would have looked "one day ahead." Fixed to read `self._t.date()` directly (the same class of fix as `Panel.date_at()` in Phase 3). Added `market_close_time()` (NSE 15:30 IST / US 16:00 ET) alongside the existing `market_tz()`. |
| `qe/data/feed.py` | Added `reference_symbol_for_market()` (NSE→RELIANCE, US→SPY) — `LiveLakeFeed.latest_date()` needs a symbol that actually exists under that market's lake path; the old hardcoded "RELIANCE" default would have raised `FileNotFoundError` for a US feed. |
| `qe/engine/paper.py` | `_build_strategy` dispatches `risk_parity_lite` (mirrors `qe/engine/sim.py`'s Phase 4 dispatch); `run_paper()` now builds `LiveLakeFeed` with the right reference symbol, `PaperBroker(cost_model_for_market(...))`, and `WallClock(tz=market_tz(...))`; `_panel_dates`/`_row_for_date` no longer force-convert to IST (same fix pattern as Phase 3's `month_end_positions`); `sim_clock_at()` takes an optional `market=` (default `"NSE"`, backward compatible) and uses `market_close_time`/`market_tz`. |
| `configs/qe_us_rplite_book_paper.yaml` | The paper-mode config, parallel to `qe_delivery_book_paper.yaml`. `max_data_age_days: 10` (vs NSE's 7) since the US EOD lake refreshes less frequently. Not yet activated. |
| `docs/runbooks/qe-operator-runbook.md` | US book added to both the monthly cadence and real-time paper session sections, clearly marked **NOT YET ACTIVATED** pending this report's approval. |

Nothing in `qe/engine/paper.py`'s risk pipeline, kill switch, `PaperBroker`
isolation, or the `LiveBroker`/`LiveGateToken` double-gate was touched — this
phase only fixes *which market's clock/costs/data-feed a paper session uses*,
not any safety property.

## 3. paper==sim proof for the US book

`tests/qe/test_us_paper_engine.py::test_us_paper_reproduces_sim_exactly` —
mirrors the NSE M4 acceptance test exactly: drives successive `run_paper()`
sessions at each of `run_sim()`'s rebalance dates (via `sim_clock_at(d,
market="US")`), resuming persisted state each time, and asserts the final NAV
history matches sim's **to the cent**. Passes. A second test
(`test_sim_clock_at_us_reports_correct_ny_date`) is a direct regression guard
for the `SimClock.today()` bug found above.

## 4. Verification done, and deliberately not done

- **Done**: full `tests/qe/` suite — **89/89 passing** (7 new: 2 in
  `test_us_paper_engine.py`, plus the clock fix didn't break any of the 87
  from Phases 3/4). Both new YAML configs parse cleanly via
  `RunConfig.from_yaml`. Smoke-tested `python -m qe study` end-to-end against
  the real US lake with historical dates (a temp copy of the config, not the
  committed one) to confirm the CLI's `$`-formatted output path — output
  matched the Phase 4 numbers.
- **Deliberately not done**: running `python -m qe paper --config
  configs/qe_us_rplite_book_paper.yaml` for real. Because `start_date:
  2026-07-31` is still in the future relative to the current lake frontier,
  this would currently just seed an empty book and report "not due" — mostly
  harmless, but it **would** create the persisted state file that marks the
  book's official inception date, which is the actual activation event this
  report is asking approval for. That run belongs to the operator, at or
  after 2026-07-31, once this phase is approved.

## 5. Remaining before the book is live (forward-accruing)

- Operator approval of this report.
- Operator runs the monthly cadence (lake refresh → `qe study` → `qe paper`)
  for the US book starting 2026-07-31, alongside the existing NSE cadence.
- XSMOM survivorship falsification (unrelated, still open since Phase 2).
- The US Forward Gate (`check_us_forward_gate.py`, Phase 4) needs ≥12 complete
  forward months before it can report anything but "IN PROGRESS" — per Phase
  4's honest finding (mean monthly alpha vs SPY was −0.218% over the full
  2006-2026 backtest), the operator should expect this gate to be a genuinely
  hard bar to clear, not a formality.
