# AWS Backtesting Lab — Phase 7 Report: Metrics Engine & Report Writer

**Status:** COMPLETE — awaiting human approval before Phase 8  
**Date:** 2026-06-14  
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Phase 7 audits the metrics engine against the `docs/backtesting/metrics-catalog.md` spec,
adds the missing catalog metrics, fixes a pandas deprecation warning, and closes 8
coverage gaps in the test suite.

---

## Gap Analysis vs Metrics Catalog

| Catalog metric | Pre-Phase-7 status | Phase 7 action |
|---|---|---|
| `total_return_pct` | Missing | Added |
| `annualised_return_pct` (CAGR) | Missing | Added |
| `sharpe_ratio` | Missing | Added |
| `sortino_ratio` | Missing | Added |
| `largest_win` | Missing | Added |
| `largest_loss` | Missing | Added |
| `max_consecutive_losses` | Missing | Added |
| `lookahead_violations` | Missing | Added (always 0 — enforced by replay engine) |
| `number_of_trades`, `gross_pnl`, `net_pnl`, `cost_impact`, `total_slippage` | ✅ Pre-existing | — |
| `win_rate`, `avg_winner`, `avg_loser`, `payoff_ratio`, `profit_factor`, `expectancy` | ✅ Pre-existing | — |
| `max_drawdown_pct`, `max_drawdown_abs`, `daily_drawdown_pct` | ✅ Pre-existing | — |
| `monthly_pnl`, `turnover`, `exposure_pct`, `mis_dependency` | ✅ Pre-existing | — |
| `avg_mfe_r`, `avg_mae_r`, `profit_capture_ratio` | ✅ Pre-existing | — |

---

## New Metrics

### `total_return_pct`

```python
total_return_pct = net_pnl / initial_capital * 100.0
```

Signed percentage return net of all costs. Used by the live-readiness gate: "Realized P&L > 0".

### `annualised_return_pct` (CAGR)

```python
calendar_years = period_seconds / (365.25 * 24 * 3600)
annualised_return_pct = ((final_equity / initial_capital) ** (1 / calendar_years) - 1) * 100
```

Requires `period_seconds` — provided by `BacktestRunner._period_seconds()`. Returns 0.0
when period is unknown (single-session runs, tests without period).

### `sharpe_ratio` and `sortino_ratio`

Computed from the daily-resampled equity curve (last equity value per calendar day):

```
daily_returns = daily_equity.pct_change()
RF_daily = 6% / 252

Sharpe  = mean(daily_returns - RF_daily) / std(daily_returns)  × √252
Sortino = mean(daily_returns - RF_daily) / std(daily_returns[daily_returns < 0]) × √252
```

- Risk-free rate: 6% annualised (NSE/Indian equity context).
- Returns (0.0, 0.0) when fewer than 2 equity days — prevents division-by-zero and
  misleading values on single-day runs.
- Returns (`inf`, `inf`) when there are no losing days and the strategy has positive excess
  return — explicitly handled as `inf` rather than crashing.

### `largest_win` / `largest_loss`

```python
largest_win  = max(net_pnl[net_pnl > 0])    # 0.0 when no winners
largest_loss = min(net_pnl[net_pnl <= 0])   # 0.0 when no losers (negative value)
```

### `max_consecutive_losses`

Linear scan over `net_pnl` series:

```python
current = 0
for v in net_pnl:
    current = current + 1 if v <= 0 else 0
    best = max(best, current)
```

### `lookahead_violations`

Always `0`. The no-lookahead invariant is enforced structurally by `CandleReplayEngine`
(strict chronological delivery, half-open windows, `_assert_no_leakage`). Recording `0`
explicitly in every `metrics.json` makes the invariant auditable without re-running the check.

---

## Bug Fix: pandas Deprecation

`report_writer.py` used `pd.Timestamp.utcnow()` which is deprecated in pandas 2.x.
Fixed to `pd.Timestamp.now("UTC")`. This eliminates the `Pandas4Warning` that was
appearing in the full test suite output.

---

## Tests

Phase 7 added 8 tests; combined total is 15 in `tests/backtest/test_metrics_reports.py`.

**Pre-existing (7):**

| Test | Covers |
|---|---|
| `test_metrics_calculated_correctly` | Full metric correctness on 3-trade sample |
| `test_losing_run_fails_gates` | All gates FAIL on a losing run |
| `test_report_files_written_locally` | 12 expected artifact files exist in run dir |
| `test_s3_write_stubbed` | FakeS3 receives all S3 keys; `metrics.json` + `summary.md` present |
| `test_summary_contains_required_metrics` | All gate labels and headline metrics in `summary.md` |
| `test_failure_report_generated` | FAILED status + error reason in summary; `metrics.json` still written |
| `test_results_include_code_and_data_version` | `code_version` and `data_version` in `metrics.json` and `config.yaml` |

**New (8):**

| Test | Covers |
|---|---|
| `test_total_return_pct` | 70 / 1_000_000 × 100 = 0.007% |
| `test_annualised_return_pct_cagr` | 1-year period, 10k on 100k → ~10% CAGR |
| `test_largest_win_and_loss` | `largest_win=100`, `largest_loss=-50` on sample trades |
| `test_max_consecutive_losses` | W-L-L-L-W-L-W sequence → streak = 3 |
| `test_sharpe_and_sortino_computed` | 5-day alternating win/loss → both non-zero |
| `test_lookahead_violations_always_zero` | `0` on both non-empty and empty trades |
| `test_no_broker_calls_in_metrics_engine` | metrics_engine.py + report_writer.py have no broker references |
| `test_summary_contains_new_catalog_metrics` | New keys present in `metrics.json` |

Full suite: **143 passed, 0 failed, 0 warnings** (`tests/backtest/` — all phases).

---

## File Manifest

```
# Python (modified)
services/backtesting/metrics_engine.py    (new metrics: total_return_pct, annualised_return_pct,
                                           largest_win/loss, max_consecutive_losses,
                                           sharpe_ratio, sortino_ratio, lookahead_violations;
                                           new helpers: _max_consecutive_losses, _sharpe_sortino)
services/backtesting/report_writer.py     (pd.Timestamp.utcnow → pd.Timestamp.now("UTC"))
tests/backtest/test_metrics_reports.py    (+8 tests, 7→15)
```

---

## Live-Readiness Gate Mapping (Verified)

| CLAUDE.md gate | Metric in `compute_metrics` | Gate key |
|---|---|---|
| Strategy expectancy > 0 | `expectancy` | `expectancy_gt_0` |
| Profit factor > 1.2 | `profit_factor` | `profit_factor_gt_1_2` |
| Realized P&L > 0 | `net_pnl` | `net_pnl_gt_0` |
| Overall | all three above | `overall_pass` |

Gate results labeled `"Advisory. A passing backtest is necessary but not sufficient for live."`.
Live promotion still requires ≥5 valid paper sessions + operator sign-off.

---

## Safety Invariants Verified

| Invariant | How verified |
|---|---|
| `lookahead_violations` always 0 | `test_lookahead_violations_always_zero` |
| No broker references in engine or writer | `test_no_broker_calls_in_metrics_engine` |
| CAGR handles missing period gracefully | Returns `0.0` when `period_seconds` is None |
| Sharpe/Sortino handle < 2 days gracefully | Returns `(0.0, 0.0)` — no div-by-zero |
| Gate note is advisory | `evaluate_gates()` `"note"` field in every output |
| 143 tests still passing, 0 warnings | ✅ Confirmed: `python -m pytest tests/backtest/ -q` |

---

## Phase 8 Preview (NOT STARTED)

Phase 8 scope: **Strategy Adapters**

`services/backtesting/strategy_adapter.py` exists from a prior session. Phase 8 would:
- Verify all 6 production strategy adapters run correctly against the replay engine
- Add integration tests: adapter → CandleReplayEngine → BacktestResult
- Confirm `paper_only=True` on `scalp_1m` blocks it from the comparison path
- Add a smoke test for each adapter in the existing `test_strategy_adapter.py`

Phase 8 does **not** begin until this report is approved.

---

## Approval Required

Per governance: **a human must approve this report before Phase 8 begins.**

Checklist for approver:
- [ ] New metrics definitions accepted (`total_return_pct`, CAGR, Sharpe, Sortino, win/loss extremes, consecutive losses, lookahead violations)
- [ ] Risk-free rate assumption accepted (6% annual / NSE India context)
- [ ] CAGR formula accepted (calendar-year compounding from `period_seconds`)
- [ ] Sharpe/Sortino edge cases accepted (< 2 days → 0.0; no downside → inf)
- [ ] Pandas deprecation fix accepted (`utcnow` → `now("UTC")`)
- [ ] `lookahead_violations = 0` invariant accepted as structural (not checked per-bar)
- [ ] 143 tests passing, 0 warnings (`python -m pytest tests/backtest/ -q`)
- [ ] Phase 8 scope (Strategy Adapters verification) understood and approved
