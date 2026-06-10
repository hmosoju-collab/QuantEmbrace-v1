# QuantEmbrace — AWS-BT-5 Strategy Adapters Report

> **Phase 5 — strategy adapters. Implemented + tested.** Backtest-only: reuses production strategy logic, no broker calls, no live dependencies, no Kafka.
> Generated: 2026-06-06 · Governed by `aws-backtesting-steering.md` · Builds on the AWS-BT-4 replay engine.

---

## 1. What was built

`services/backtesting/strategy_adapter.py` — thin adapters that wire the **six production strategies** to the replay engine **without modifying their logic** (`prefer_refactor_over_rewrite`). Each adapter builds the real strategy class, declares the interval the engine should feed it, and converts its `Signal` into an enriched, production-compatible output.

| Adapter | Production class (reused) | Replay interval | strategy_version | paper_only |
|---|---|---|---|---|
| `vwap_reversion` | `VWAPReversionStrategy` | 1m | `vwap_reversion@1.0` | no |
| `momentum` | `MomentumStrategy` | **5m** | `momentum@2.0` | no |
| `orb` | `ORBStrategy` | 1m (forms 09:15–09:30 OR) | `orb_15m@1.0` | no |
| `trend_15m` | `IntradayTrend15mStrategy` | 15m | `intraday_trend_15m@1.0` | no |
| `preclose` | `PreCloseMomentumStrategy` | 5m | `preclose_momentum@1.0` | no |
| `scalp_1m` | `Scalp1mStrategy` (v2) | 1m | `scalp_1m@2.0` | **yes** |

## 2. Enriched signal schema (production-compatible)

`adapter.enrich(signal, data_version=…)` returns the canonical `Signal.to_dict()` **plus** additive fields (no breaking changes):

- `strategy_version`, `data_version`
- `timestamp` / `generated_at` — **bar-stamped** to the candle that produced the signal (never future)
- `metadata.tee` — what the TradeExitEngine needs: `entry_price`, `stop_loss`, `take_profit`, `risk_per_unit`, `rr_target` (R multiple), `product_type=MIS`, `exit_policy_version`, `strategy`
- `metadata.backtest=True`, `metadata.paper_trade`

`collect_signals(candles, …)` is the **fast-mode** runner (no Kafka/broker): it feeds candles oldest-first and stamps every signal to its bar.

## 3. Requirements coverage

| Requirement | How |
|---|---|
| reuse production strategy logic | adapters construct the real classes; zero strategy edits |
| no live deps / no broker / no Kafka in fast mode | `collect_signals` only calls `on_bar`/`generate_signal` |
| production-compatible signal schema | `Signal.to_dict()` + additive fields |
| include strategy_version | per-adapter version constant |
| TEE metadata | `metadata.tee` block (R basis, product type, exit policy) |
| timestamp + data_version | bar-stamped `generated_at` + `data_version` |
| no future data | signals stamped to producing bar; candles fed oldest-first |
| scalp paper-only | `paper_only=True`, `paper_trade` forced True, live spread/LTP rejects off |

## 4. Code-vs-spec findings (code is truth)

- **Pre-close window is `14:45–15:10 IST`** in production (`_IST_FIRE_START = 14*60+45`), **not** the `14:00–15:05` stated in the phase brief. The adapter and test follow the production window. → consider reconciling the brief or the strategy if `14:00–15:05` is the intended policy.
- **VWAP reversion runs on 1m** (`candle_interval="minute"`), though the earlier spec table listed 5min. Adapter uses the production interval (1m).
- **Momentum has no `candle_interval`** (it is TICK-based in production); for backtest it is assigned **5m** per this phase. Note: it warms up only when `long_window ≥ atr_period` (the close buffer is capped at `long_window`); the adapter passes this through and the test uses `long_window=14`.
- **scalp_1m v2 in backtest**: live `reject_if_spread_unavailable` / `reject_if_ltp_stale` are **disabled** (no live spread/LTP feed in an OHLCV backtest), but every edge floor is kept (`min_net_edge_pct=0.12`, `min_stop_pct`, `min_target_pct`, `min_body_atr_pct`, …) so low-edge trades are still rejected. `paper_trade` is forced `True` and cannot be overridden.

## 5. `run_backtest.yaml` stale refs fixed (Phase 0 finding)

- `engine: scripts/backtest/run.py` → `scripts/backtest/run_backtest.py` (the file that exists).
- `must_exist_in: services/strategy_engine/registry.py` (never existed) → `services/backtesting/strategy_adapter.py` (the backtest strategy registry).

## 6. Test results

`tests/backtest/test_strategy_adapter.py` — **8/8 passing** (full lab suite **37/37**: 12 data + 9 registry + 8 replay + 8 adapters).

| Test | Verifies |
|---|---|
| `vwap_emits_only_with_valid_setup` | flat series (no deviation) → no signal |
| `orb_uses_first_15m_opening_range_only` | no trade while forming the 09:15–09:30 range |
| `momentum_uses_5m_candles` | interval 5m; emits BUY on a 5m crossover; full enriched schema |
| `trend_15m_uses_15m_candles` | interval 15m; runs on 15m candles |
| `preclose_respects_window` | 10:00 bar (outside 14:45–15:10) → no signal |
| `scalp_v2_rejects_low_edge_trades` | hardened v2; flat/low-edge → no signal |
| `no_strategy_sees_future_data` | emitted signals stamped to a past bar for every adapter |
| `scalp_remains_paper_only` | `paper_only=True`; enriched scalp signals always `paper_trade=True` |

## 7. Files

| Artifact | Path |
|---|---|
| Adapters | `services/backtesting/strategy_adapter.py` |
| Tests (8) | `tests/backtest/test_strategy_adapter.py` |
| Command fix | `commands/run_backtest.yaml` (2 stale refs) |
| Reused strategies | `services/strategy_engine/strategies/*` (unchanged) |

## 8. Recommended next phase

**Phase 6 — Execution simulator** (`/aws_bt_execution_simulator`): assert mandatory costs/slippage and `lookahead_violations == 0` end-to-end through the replay engine + adapters + `Backtester`, and surface the `cost_model_version` on each run.

---

*Implemented + tested. No infra deployed, no live trading, no broker APIs called. Stop for approval before the next phase.*
