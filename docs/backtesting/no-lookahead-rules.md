# QuantEmbrace — No-Lookahead Rules

> **Status: PLANNED — design only.** Documents the rules the lab enforces; partially already implemented in `services/strategy_engine/backtesting/backtester.py`.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md`.

Lookahead (future-data leakage) is the single most common cause of backtests that look great and fail live. These rules are mandatory and **asserted per run**: a valid run has `lookahead_violations == 0`.

---

## 1. Execution timing (already enforced)

- **Next-bar execution.** A signal generated on bar *t* may only fill on bar *t+1* or later. The engine stamps `signal.generated_at = bar.timestamp` of the producing bar and rejects any fill where `fill_timestamp <= generated_at`, incrementing `lookahead_violations` (see `backtester.py::_open_position`).
- **No same-bar peeking.** Entry decisions for bar *t* use only data available at *t*'s close. Stops/take-profits are checked against **subsequent** bars' high/low, never the entry bar.
- **Gap-through honesty.** If price gaps past a stop, fill at the worse bar **open**, not the stop level (`gap_stop_behavior`). Optimistic stop fills are a hidden lookahead.

## 2. Point-in-time data

- **Corporate actions applied as-of.** The lake stores **unadjusted** prices + `adj_factor`; back-adjustment is computed for the as-of date only. A split next year must not alter this year's prices (`aws-data-lake-contract.md` §4).
- **Survivorship.** Universe and symbol lists are reconstructed as-of the trading date, including **delisted** names. Never backtest only on today's survivors.
- **Indicators/features use trailing windows only.** RSI/EMA/VWAP/ATR/ADX/MACD computed from bars ≤ current bar. No centered windows, no full-series normalization using future statistics.
- **No forward-fill across the boundary.** Gaps are not filled with values that postdate the bar; missing data is flagged, not invented (`no_silent_failures`).

## 3. Universe & reference timing

- Trading calendar, instrument master, and any exclusion/surveillance lists are read **as-of** the simulated date, from `reference/` snapshots — never the latest version.

## 4. Train/test separation (walk-forward & datasets)

- In-sample optimization may only see data strictly before the out-of-sample window (`walk-forward-validation.md`).
- Model-dataset labels are computed from **future-relative-to-signal** outcomes but datasets are **split by time**; no row's features may include information from its own or later label window (`model-dataset-spec.md`).

## 5. Determinism

- Fixed random seeds; no wall-clock or `now()` in simulation logic; ordering of same-timestamp events is deterministic. Non-determinism can mask lookahead.

## 6. Enforcement checklist (per run / per PR)

- [ ] `lookahead_violations == 0` in `metrics.json`.
- [ ] Adjustment applied as-of; no future `adj_factor` used.
- [ ] Universe/reference read from `data_snapshot_id`, not latest.
- [ ] Indicators use trailing windows only.
- [ ] Walk-forward IS window strictly precedes OOS window.
- [ ] Dataset split boundaries documented; no feature/label time overlap.
- [ ] Stops/TP checked on subsequent bars; gap fills at worse open.

The `safety-review-agent` and `data-quality-agent` verify this list before any phase report.
