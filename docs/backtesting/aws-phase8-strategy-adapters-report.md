# AWS Backtesting Lab — Phase 8 Report: Strategy Adapters

**Status:** COMPLETE — awaiting human approval before Phase 9  
**Date:** 2026-06-14  
**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.

---

## Scope

Phase 8 verifies all six production strategy adapters, closes coverage gaps, and confirms the
`scalp_1m` paper-only constraint enforced by the adapter layer. The `strategy_adapter.py`
module was pre-existing and complete. Phase 8 work: 6 gap tests added (8→14 tests), 0 regressions.

---

## What Was Already Implemented

| File | Status |
|---|---|
| `services/backtesting/strategy_adapter.py` | Pre-existing — complete (6 adapters) |
| `tests/backtest/test_strategy_adapter.py` (8 tests) | Pre-existing — all passing |

---

## Adapter Registry

| Adapter | Interval | Strategy Version | paper_only | Production Class |
|---|---|---|---|---|
| `vwap_reversion` | 1m | `vwap_reversion@1.0` | False | `VWAPReversionStrategy` |
| `momentum` | 5m | `momentum@2.0` | False | `MomentumStrategy` |
| `orb` | 1m | `orb_15m@1.0` | False | `ORBStrategy` |
| `trend_15m` | 15m | `intraday_trend_15m@1.0` | False | `IntradayTrend15mStrategy` |
| `preclose` | 5m | `preclose_momentum@1.0` | False | `PreCloseMomentumStrategy` |
| `scalp_1m` | 1m | `scalp_1m@2.0` | **True** | `Scalp1mStrategy` |

5 adapters are eligible for live comparison. `scalp_1m` is excluded by `paper_only=True` — the
filter `[n for n in list_adapters() if not get_adapter(n).paper_only]` gives exactly 5.

---

## `StrategyAdapter` Interface

### `build_strategy(symbols, *, market, nav, **overrides) → BaseStrategy`

Each builder receives the production strategy class. Builders strip `paper_trade=False` from
overrides and pass `paper_trade=True` — strategies under backtest never touch live broker paths.
`scalp_1m` additionally hard-codes edge floors (`rr_ratio=1.5`, `atr_stop_multiplier=0.5`, etc.)
and cannot be unlocked to `paper_trade=False` by any caller.

### `enrich(signal, *, data_version, last_bar=None) → dict`

Converts a production `Signal` to an enriched dict with:

| Field | Description |
|---|---|
| `strategy_version` | Adapter's pinned version string |
| `data_version` | Caller-supplied lake snapshot ID |
| `paper_trade` | Forced `True` for `paper_only` adapters; preserved otherwise |
| `metadata.backtest` | Always `True` |
| `metadata.tee.strategy` | Adapter name |
| `metadata.tee.entry_price` | `price_at_signal` (or `last_bar.close` if missing) |
| `metadata.tee.stop_loss` | From `Signal.stop_loss` |
| `metadata.tee.take_profit` | From `Signal.take_profit` |
| `metadata.tee.risk_per_unit` | `\|entry − stop_loss\|` |
| `metadata.tee.rr_target` | `\|take_profit − entry\| / risk_per_unit` |
| `metadata.tee.product_type` | `"MIS"` (always) |
| `metadata.tee.exit_policy_version` | `"tee@1.0"` |

### `collect_signals(candles, symbols, *, data_version, ...) → list[dict]`

Fast-mode run: sorts candles chronologically, feeds each to `on_bar` / `generate_signal`,
stamps each emitted signal to the bar that produced it (`generated_at = bar.timestamp`).
No signal is ever stamped after the last fed candle — enforced by the stamping assignment
before `enrich()` is called.

---

## Tests

Phase 8 added 6 tests; combined total is 14 in `tests/backtest/test_strategy_adapter.py`.

**Pre-existing (8):**

| Test | Covers |
|---|---|
| `test_vwap_emits_only_with_valid_setup` | Flat series → no signal (VWAP) |
| `test_orb_uses_first_15m_opening_range_only` | ORB formation window → no trade |
| `test_momentum_uses_5m_candles` | V-shape crossover → BUY with enrichment metadata |
| `test_trend_15m_uses_15m_candles` | Interval assert + runs without error |
| `test_preclose_respects_window` | 10:00 bar outside 14:45–15:10 window → no signal |
| `test_scalp_v2_rejects_low_edge_trades` | Flat series → fails edge floors → no signal |
| `test_no_strategy_sees_future_data` | All 6 adapters: no signal stamped after last candle |
| `test_scalp_remains_paper_only` | `paper_only=True` + `enrich()` forces `paper_trade=True` |

**New (6):**

| Test | Covers |
|---|---|
| `test_get_adapter_raises_for_unknown_name` | `get_adapter("doesnotexist")` raises `ValueError` |
| `test_list_adapters_returns_all_six` | `list_adapters()` returns exactly the 6 expected names |
| `test_paper_only_adapters_filterable` | Only `scalp_1m` is `paper_only`; 5 eligible for comparison |
| `test_enrich_non_scalp_preserves_paper_trade_flag` | Non-paper adapter does not override `paper_trade` |
| `test_enrich_tee_metadata_completeness` | All 8 TEE fields correct: strategy, prices, risk, R:R, product_type, version |
| `test_no_broker_calls_in_strategy_adapter` | `strategy_adapter.py` contains no broker API references |

Full suite: **149 passed, 0 failed, 0 warnings** (`tests/backtest/` — all phases).

---

## File Manifest

```
# Python (modified — tests extended)
tests/backtest/test_strategy_adapter.py    (+6 tests, 8→14; added pytest import)
```

---

## Safety Invariants Verified

| Invariant | How verified |
|---|---|
| No broker API in adapter | `test_no_broker_calls_in_strategy_adapter` |
| `paper_trade=True` never reaches live broker in backtest | All builders pass `paper_trade=True` to strategy class |
| `scalp_1m` cannot be used in live comparison | `test_paper_only_adapters_filterable` |
| `paper_only` adapter forces `paper_trade=True` on enrich | `test_scalp_remains_paper_only` |
| Non-paper adapter does not override caller's `paper_trade` | `test_enrich_non_scalp_preserves_paper_trade_flag` |
| No signal stamped after last fed candle | `test_no_strategy_sees_future_data` (all 6 adapters) |
| TEE metadata complete for exit engine | `test_enrich_tee_metadata_completeness` |
| `get_adapter` raises on bad name | `test_get_adapter_raises_for_unknown_name` |
| Advisory only — adapter never touches broker | No broker import in `strategy_adapter.py` |
| 149 tests passing, 0 warnings | ✅ Confirmed: `python -m pytest tests/backtest/ -q` |

---

## Phase 9 Preview (NOT STARTED)

Phase 9 scope: **Model Dataset Generation**

`services/backtesting/model_dataset_agent.py` (if it exists) or a new module would:
- Consume the backtest trade log and enriched signals from Phase 8 adapters
- Generate leakage-free training labels (outcome labels: winner/loser/breakeven) for the AI engine quality scorer
- Point-in-time correct: labels derived only from trade outcomes, never from future price
- Output: S3 Parquet datasets in `quantembrace-backtest-data/model-datasets/`
- No model training, no deployment — dataset generation only

Phase 9 does **not** begin until this report is approved.

---

## Approval Required

Per governance: **a human must approve this report before Phase 9 begins.**

Checklist for approver:
- [ ] All 6 adapter names, intervals, and strategy versions accepted
- [ ] `scalp_1m` paper-only exclusion from live comparison accepted
- [ ] `enrich()` TEE metadata field set accepted (strategy, prices, risk_per_unit, rr_target, MIS, tee@1.0)
- [ ] `collect_signals()` bar-stamping invariant accepted (no future data)
- [ ] No-broker invariant accepted for `strategy_adapter.py`
- [ ] 149 tests passing, 0 warnings (`python -m pytest tests/backtest/ -q`)
- [ ] Phase 9 scope (Model Dataset Generation) understood and approved
