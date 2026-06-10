# QuantEmbrace — Cost & Slippage Model

> **Status (AWS-BT-6): implemented.** `services/backtesting/execution_simulator.py` + the existing `Backtester` (`IndianCostModel`). Costs & slippage are **mandatory** and on by default; disabling any component requires an explicit flag and is surfaced in the run report.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md`.

A backtest without realistic costs is misleading. The lab applies the full NSE intraday cost stack plus spread and slippage to every fill. The execution simulator reuses the statutory **rates** from the production `IndianCostModel` (single source of truth) so simulated economics match the production `Backtester`.

---

## 1. Cost components (per leg)

| Component | Default rate | Applied on |
|---|---|---|
| Brokerage | `0.03%` (`brokerage_pct`) | both legs |
| STT | `0.025%` | **sell** leg |
| Exchange transaction charge | `0.00345%` | both legs |
| SEBI turnover fee | `0.0001%` | both legs |
| Stamp duty | `0.003%` | **buy** leg |
| GST | `18%` | on (brokerage + exchange + SEBI) |
| Spread | `spread_bps` (default 5) | half-spread per fill (in price) |
| Slippage | tiered bps (below) | adverse, per fill (in price) |

`statutory_total = brokerage + STT + exchange + SEBI + GST + stamp` (explicit fees). **Spread and slippage are not added as fees — they are baked into a worse fill price**, so they reduce P&L automatically and are reported separately (`CostBreakdown.slippage`, `.spread`).

## 2. Configurable assumptions (`ExecutionConfig`)

| Knob | Default | Meaning |
|---|---|---|
| `intraday_round_trip_cost_bps` | `0.0` | optional flat round-trip cost estimate (bps) used by the edge gate when > 0 |
| `slippage_bps_liquid` | `1.0` | slippage for liquid names |
| `slippage_bps_mid` | `3.0` | slippage for mid-liquidity names |
| `slippage_bps_illiquid` | `8.0` | slippage for illiquid names |
| `spread_bps` | `5.0` | bid/ask spread; half paid per fill |
| `min_net_edge_pct` | `0.0` | reject trades whose net edge (%) is below this |
| `brokerage_pct` | `0.03` | per-leg brokerage |
| `enable_statutory_costs` | `True` | toggle the NSE statutory stack |
| `allow_partial_fills` | `False` | enable `fill_ratio` partial fills |

## 3. Slippage application

```
adverse_bps = slippage_bps_for(tier) + spread_bps/2
fill_price  = reference * (1 ± adverse_bps/10_000)        # + for BUY, − for SELL
slippage_per_unit = |fill_price − reference|
```
Buyers pay up, sellers receive less. Gap-through stops (in the `Backtester`) fill at the worse bar open.

## 4. Net-edge gate (`min_net_edge_pct`)

```
gross_edge_pct      = |target − entry| / entry × 100
round_trip_cost_pct = intraday_round_trip_cost_bps/100         # if set, else…
                    = statutory(buy+sell)/entry×100 + (2·slippage_bps + spread_bps)/100
net_edge_pct        = gross_edge_pct − round_trip_cost_pct
accept              = net_edge_pct ≥ min_net_edge_pct
```
A `submit(..., target=…)` with `min_net_edge_pct > 0` is **rejected** when the expected net edge is too small (no position change, `Fill.rejected=True`). This mirrors the scalp_1m v2 edge floor.

## 5. Simulator semantics

- **Signed positions**: `Position.quantity` is signed (+ long, − short); buys add, sells subtract. Net position always equals the net of signed fills.
- **Long/short P&L**: realised on the closed portion using average price — long `(exit−avg)·qty`, short `(avg−exit)·qty`; statutory fees subtracted; slippage already in fill prices.
- **Partial fills**: optional via `fill_ratio` when `allow_partial_fills=True`.
- **Turnover**: cumulative traded value across fills.
- **Position sizing**: `position_size(nav, risk_pct, entry, stop) = floor(nav·risk_pct / |entry−stop|)`.
- **Net P&L after costs**: `realized_pnl` reflects both explicit fees and price-embedded slippage.

## 6. Mandatory-on policy & versioning

Costs and slippage are on by default. Disabling (e.g. `enable_statutory_costs=False`, zero slippage) is for controlled experiments only and must be logged + flagged in the run report. A `cost_model_version` is recorded on every run (registry + `config.json`).

## 7. End-to-end guarantee

Through replay engine → strategy adapters → `Backtester`, every executed trade incurs costs and slippage and the run reports `lookahead_violations == 0`. Verified by `tests/backtest/test_execution_simulator.py::test_end_to_end_costs_and_no_lookahead`.

## 8. Safety

Paper/backtest only. No broker APIs, no live trading, no order placement. Enforced by `tests/backtest/test_execution_simulator.py::test_no_broker_call_possible`.
