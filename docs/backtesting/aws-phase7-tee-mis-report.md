# QuantEmbrace — AWS-BT-7 TEE + MIS Simulator Report

> **Phase 7 — Trade Exit Engine + MIS square-off simulators. Implemented + tested.** Backtest-only: no broker APIs, no live trading; advisory comparison only — never promotes a policy or changes trading behaviour.
> Generated: 2026-06-06 · Governed by `aws-backtesting-steering.md` · Reuses AWS-BT-6 execution simulator for fills.

---

## 1. What was built

- `services/backtesting/tee_simulator.py` — R-based exit engine + per-trade lifecycle + metrics. Models the production exit priority and both the **old global** and **new strategy-aware** policies.
- `services/backtesting/mis_simulator.py` — 15:05 IST square-off (deadline 15:10, broker 15:15), final cleanup only.
- `scripts/backtest/compare_tee_policies_aws.py` — runs identical trades under both policies and reports the old-vs-new delta.

## 2. Old vs new policy

| | Old global (pre-R) | New strategy-aware (R-based) |
|---|---|---|
| stop-loss | fixed | fixed |
| breakeven | — | move stop → entry at `breakeven_at_r` |
| partial booking | — | once at `partial_profit_at_r` (idempotent) |
| trailing | global, activate 1.25R | per-strategy, activate + distance in R |
| fixed TP | 2.5R | 2.5R (suppressed once trailing active) |
| max-hold exit | — | e.g. VWAP 30 min |
| hard time exit | — | e.g. preclose 15:00 IST |
| EOD | rides to MIS 15:05 | usually time-exits before MIS |

Per-strategy overrides live in `NEW_POLICIES` (vwap_reversion has `max_hold_minutes=30`; preclose has `hard_exit_time=15:00`).

## 3. R math + exit priority (production-faithful)

`initial_risk = |entry − stop|`; LONG `R=(price−entry)/risk`, SHORT `R=(entry−price)/risk`.
Per-bar priority: **1** stop/trailing → **2** breakeven → **3** partial → **4** trailing activate/advance → **5** final target/fixed TP → **6** max-hold → **7** hard time. MIS square-off (15:05) is applied last, only to still-open positions.

## 4. Tracked exit states

`STOP_LOSS`, `FINAL_TARGET` (fixed TP), `BREAKEVEN` (stop→entry), `PARTIAL_PROFIT`, `TRAILING`, `TIME_EXIT` (max-hold / hard time), `MIS_CLOSE`, `UNRESOLVED` (still open at end of bars). Trailing activation and trailing-stop hits are distinguished from the initial stop.

## 5. Metrics

Per trade: **MFE** (max favourable excursion, R), **MAE** (max adverse excursion, R), `realized_r`, **profit_capture_ratio** = realized_r / MFE, **giveback_ratio** = (MFE − realized_r) / MFE, **MIS dependency** (final reason == MIS_CLOSE). The compare script aggregates these and the **old-vs-new delta** plus the exit-reason mix.

## 6. Test results

`tests/backtest/test_tee_mis.py` — **11/11 passing** (full lab suite **57/57**).

| Test | Verifies |
|---|---|
| `r_calculation_long` / `_short` | R = 2.0 at 2× risk; −1.0 at the stop |
| `breakeven_shift` | stop pulled to entry at `breakeven_at_r` |
| `partial_booking_idempotency` | booked exactly once, 50% of qty |
| `trailing_never_loosens` | stop history is monotonic (tightens only) |
| `vwap_max_hold_exit` | TIME_EXIT after 30 min |
| `preclose_hard_exit` | TIME_EXIT at 15:00 IST |
| `mis_eod_exit` | open at 15:05 → MIS_CLOSE, mis_dependent |
| `old_vs_new_deterministic_comparison` | identical results across runs |
| `daily_cap_does_not_block_exits` | exit fires with `daily_cap_hit=True` |
| `mis_remains_final_cleanup_only` | no square-off before 15:05; TEE-resolved trades untouched |

## 7. Sample comparison (synthetic self-test)

```
metric                 old       new     delta
avg_realized_r       1.667     1.142    -0.525
avg_capture          0.641     0.391    -0.250
avg_giveback         0.359     0.609    +0.250
mis_dependency       0.333     0.000    -0.333
old reasons {FINAL_TARGET:2, MIS_CLOSE:1}  new {UNRESOLVED:1, TRAILING:1, TIME_EXIT:1}
```
Illustrative only (3 synthetic trades). The signal: the new policy's time exits drive **MIS dependency to zero** (it resolves positions itself rather than relying on the 15:05 broker-cleanup), at the cost of more giveback on strong runners — exactly the trade-off a real comparison surfaces. Real conclusions require real data + many trades.

## 8. Invariants enforced

Trailing only tightens; partial booking is idempotent; **daily caps block entries, never exits**; **MIS is exposure cleanup, never profit booking** and only acts on still-open positions at/after 15:05. Fills route through the AWS-BT-6 execution simulator (costs/slippage). No broker APIs.

## 9. Files

| Artifact | Path |
|---|---|
| TEE simulator | `services/backtesting/tee_simulator.py` |
| MIS simulator | `services/backtesting/mis_simulator.py` |
| Compare script | `scripts/backtest/compare_tee_policies_aws.py` |
| Tests (11) | `tests/backtest/test_tee_mis.py` |

## 10. Recommended next phase

**Phase 8 — Metrics & reports** (`/aws_bt_metrics_reports`): assemble the full `metrics-catalog.md` set per run, render `report.md`, and map results to the live-readiness gates (expectancy > 0, profit factor > 1.2, realized P&L > 0) — advisory.

---

*Implemented + tested. No infra deployed, no live trading, no broker APIs called. Stop for approval before the next phase.*
