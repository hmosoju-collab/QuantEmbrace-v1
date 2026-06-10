# QuantEmbrace — AWS-BT-6 Execution Simulator & Cost/Slippage Report

> **Phase 6 — execution simulator + NSE cost/slippage model. Implemented + tested.** Paper/backtest-only: no broker APIs, no live trading, no order placement.
> Generated: 2026-06-06 · Governed by `aws-backtesting-steering.md` · Builds on the AWS-BT-4 replay engine and AWS-BT-5 adapters.

---

## 1. What was built

`services/backtesting/execution_simulator.py` — a deterministic fill simulator with the full NSE intraday cost stack, tiered slippage, signed-position accounting, partial fills, turnover, a net-edge gate, and risk-based sizing. It **reuses the statutory rates** from the production `IndianCostModel` and the canonical `Direction` enum, so economics match the production `Backtester`.

`docs/backtesting/cost-slippage-model.md` updated to the implemented model.

## 2. Cost components & configurable assumptions

- **Components**: brokerage, STT, exchange transaction charges, SEBI fees, GST, stamp duty, spread, slippage. Statutory fees are explicit; spread + slippage are baked into a worse fill price.
- **Configurable** (`ExecutionConfig`): `intraday_round_trip_cost_bps`, `slippage_bps_liquid` / `slippage_bps_mid` / `slippage_bps_illiquid`, `spread_bps`, `min_net_edge_pct`, `brokerage_pct`, `enable_statutory_costs`, `allow_partial_fills`. Formulas in `cost-slippage-model.md`.

## 3. Simulator requirements coverage

| Requirement | How |
|---|---|
| long/short fills | `submit(BUY/SELL, …)`; signed position accounting |
| signed quantity invariant | `Position.quantity` signed; net always = net of signed fills |
| partial fills optional | `allow_partial_fills` + `fill_ratio` |
| position sizing | `position_size(nav, risk_pct, entry, stop)` |
| turnover tracking | cumulative traded value (`turnover`) |
| net P&L after costs | `realized_pnl` (fees + price-embedded slippage) |
| paper/backtest only | no broker imports; verified by test |
| no broker APIs | module scanned for broker tokens (test) |

## 4. Test results

`tests/backtest/test_execution_simulator.py` — **9/9 passing** (8 required + 1 end-to-end). Full lab suite **46/46** (12 data + 9 registry + 8 replay + 8 adapters + 9 execution).

| Test | Verifies |
|---|---|
| `long_pnl_correct` | frictionless long round-trip realises (exit−entry)·qty |
| `short_pnl_correct` | frictionless short round-trip realises (entry−exit)·qty |
| `costs_applied` | full NSE stack > 0; P&L reduced; STT on sell leg |
| `slippage_applied` | buyer pays up / seller receives less; slippage > 0 |
| `signed_quantity_invariant` | 10 → 6 → 0; position == net signed fills |
| `partial_fill_accounting` | `fill_ratio=0.4` → 4 filled; turnover = 4×price |
| `rejected_if_net_edge_too_small` | tight target rejected (no position); wide target fills |
| `no_broker_call_possible` | no broker tokens in the module |
| `end_to_end_costs_and_no_lookahead` | adapter → replay → `Backtester`: `lookahead_violations == 0`, ≥1 trade, costs > 0, slippage > 0 |

## 5. End-to-end assertion (the phase goal)

`run_with_backtester` was extended to forward `backtester_kwargs` (e.g. `slippage_bps`, `commission_pct`) so cost/slippage flow through the whole chain. The end-to-end test runs the **momentum adapter** over a 5m crossover via the **replay engine** into the existing **`Backtester`** and asserts `lookahead_violations == 0` with costs and slippage applied to the resulting trade.

## 6. Files

| Artifact | Path |
|---|---|
| Execution simulator | `services/backtesting/execution_simulator.py` |
| Cost/slippage model doc | `docs/backtesting/cost-slippage-model.md` |
| Tests (9) | `tests/backtest/test_execution_simulator.py` |
| Engine hook | `services/backtesting/replay_engine.py` (`run_with_backtester(..., backtester_kwargs=)`) |

## 7. Recommended next phase

**Phase 7 — TEE / MIS simulator** (`/aws_bt_tee_mis`): replay identical signals through the old vs new `TradeExitEngine` (R-based exits + partial profit booking) and simulate the 15:05/15:15 IST MIS square-off, producing a side-by-side comparison — reusing this execution simulator for fills.

---

*Implemented + tested. No infra deployed, no live trading, no broker APIs called. Stop for approval before the next phase.*
