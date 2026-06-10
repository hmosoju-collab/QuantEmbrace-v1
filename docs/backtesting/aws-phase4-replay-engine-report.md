# QuantEmbrace — AWS-BT-4 Replay Engine Report

> **Phase 4 — candle replay engine. Implemented + tested.** Backtest-only: no broker APIs imported or called, no live trading, no live-table mutation.
> Generated: 2026-06-06 · Governed by `aws-backtesting-steering.md` · Builds on AWS-BT-2 (data layer) and AWS-BT-3 (registry/checkpoints).

---

## 1. What was built

`services/backtesting/replay_engine.py` — a candle replay engine that **reuses the existing engine** (`services/strategy_engine/backtesting/backtester.py`) rather than replacing it (`prefer_refactor_over_rewrite`):

- `Candle` dataclass + `Candle.to_bar()` → the existing engine's `Bar` (lazy import, no hard dependency).
- `BarSource` protocol with two implementations: `DataFrameBarSource` (in-memory / single-process, used in tests) and `ParquetBarSource` (lake/S3 via `data_loader`).
- `ReplayConfig` (symbols, timeframes, date range, market-hours filter, trading calendar, missing-candle policy, `as_of` cutoff, partition strategy).
- `CandleReplayEngine` with:
  - `plan()` — partition/shard list (`symbol` | `symbol_year` | `symbol_date`) for AWS-scale batching.
  - `replay_stream()` — global, chronological, no-lookahead candle iterator (portfolio view).
  - `replay(consumer, run_id, registry, checkpoint)` — per-partition replay with **checkpoint/resume** and **RunRegistry lifecycle** wiring (RUNNING→COMPLETED/FAILED).
  - `run_with_backtester(strategy_factory, …)` — reuses the existing `Backtester` per partition, checkpointing each shard.

## 2. Requirements coverage

| Requirement | How |
|---|---|
| replay candle-by-candle | `replay_stream()` / `replay()` deliver one `Candle` at a time |
| multiple symbols | sources + global merge; `multi_symbol` test |
| multiple timeframes | `timeframes` filter + interval-aware ordering; `timeframe_alignment` test |
| preserve chronological order | stable sort key `(timestamp, interval_minutes, symbol)` + monotonic guard |
| enforce no-lookahead | monotonic delivery (`_iter_monotonic` raises on inversion) + `as_of` cutoff; strategy sees only current/past |
| batching by date/symbol (AWS scale) | `partition_by = symbol \| symbol_year \| symbol_date`; `plan()` |
| checkpoint resume | `replay(..., checkpoint=CheckpointManager)` skips completed partitions, checkpoints each |
| deterministic replay | full stable sort key; no set/dict-order reliance; `deterministic_output` test |
| market-hours filter | 09:15–15:30 IST filter for intraday; `market_hours_filter` test |
| trading calendar support | `ReplayConfig.calendar` (allowed trading dates) |
| reuse `backtester.py` | `Candle.to_bar()` + `run_with_backtester()` drive the existing `Backtester` |

## 3. Test results

`tests/backtest/test_replay_engine.py` — **8/8 passing** (full lab suite **29/29**: 12 data-layer + 9 registry/checkpoint + 8 replay).

| Test | Verifies |
|---|---|
| `future_candle_not_visible` | `as_of` hides future; streaming delivery strictly past-only |
| `multi_symbol_chronological_replay` | two symbols interleave in global time order |
| `timeframe_alignment` | 1m + 5m ordered by close; finer interval first at equal timestamp |
| `missing_candle_handled_per_config` | ERROR policy raises; SKIP records the gap (4 missing) and continues |
| `checkpoint_resume_continues_correctly` | completed partition skipped; only pending replayed; checkpoint updated |
| `deterministic_output_for_same_input` | identical ordered output across runs |
| `market_hours_filter_works` | out-of-hours candles excluded (on) / included (off) |
| `no_broker_calls_possible` | module source contains no broker tokens (kite/alpaca/place_order/boto3/…) |

**Integration smoke (manual, not a unit test):** `run_with_backtester()` ran the existing `Backtester` over an 80-bar daily series and returned a `BacktestResult` per partition with `lookahead_violations = 0` — confirming the engine genuinely extends `backtester.py`.

## 4. Safety notes

- **No broker path.** The engine imports no broker SDK; it only reads candles and hands them to a consumer. Enforced by the `no_broker_calls_possible` test.
- **No-lookahead** is enforced two ways: chronological monotonic delivery (raises on any inversion) and the optional `as_of` cutoff. Corporate-action read-time adjustment and point-in-time membership remain the data layer's responsibility (`no-lookahead-rules.md`).
- **Resumability** uses the AWS-BT-3 `CheckpointManager` (metadata-only); large partial outputs stay in S3.
- Backtest-only; no live trading, no capital change, no live/paper table access.

## 5. Files

| Artifact | Path |
|---|---|
| Replay engine | `services/backtesting/replay_engine.py` |
| Tests (8) | `tests/backtest/test_replay_engine.py` |
| Reused engine | `services/strategy_engine/backtesting/backtester.py` (unchanged) |

## 6. Recommended next phase

**Phase 5 — Strategy adapters** (`/aws_bt_strategy_adapters`): present all six production strategies to the replay engine via thin adapters (correct interval + params, deterministic `signal_id`/`generated_at`), and fix the two stale references in `commands/run_backtest.yaml` (`scripts/backtest/run.py`, `services/strategy_engine/registry.py`) noted in the Phase 0 report.

---

*Implemented + tested. No infra deployed, no live trading enabled, no broker APIs called. Stop for approval before the next phase.*
