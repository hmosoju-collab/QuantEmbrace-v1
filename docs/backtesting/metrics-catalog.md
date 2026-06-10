# QuantEmbrace — Backtest Metrics Catalog

> **Status: PLANNED — design only.** Source of truth = `BacktestResult` in `services/strategy_engine/backtesting/backtester.py` + `commands/run_backtest.yaml`; this doc consolidates and extends.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md`.

Every run writes these to `metrics.json`; a small summary is mirrored to `qe-bt-runs.metrics_summary`.

---

## 1. Returns
| Metric | Definition |
|---|---|
| `total_return_pct` | (final − initial) / initial × 100 |
| `annualised_return_pct` | CAGR, years = calendar_days / 365.25 |
| `monthly_returns` | series of monthly return fractions (extension) |

## 2. Risk-adjusted
| Metric | Definition |
|---|---|
| `sharpe_ratio` | annualised, daily excess returns, RF default 6% |
| `sortino_ratio` | annualised, downside deviation only |
| `calmar_ratio` | annualised return / max drawdown (extension) |

## 3. Drawdown
| Metric | Definition |
|---|---|
| `max_drawdown_pct`, `max_drawdown_abs` | largest peak-to-trough |
| `max_drawdown_duration_days` | longest underwater period (extension) |
| `drawdown_stress` | observed + 1.5× / 2× stressed scenarios |

## 4. Trade statistics
| Metric | Definition |
|---|---|
| `total_trades` | completed round-trips |
| `win_rate` | % profitable |
| `profit_factor` | gross profit / gross loss |
| `expectancy` | mean P&L per trade (after costs) — **live-gate metric** |
| `avg_win`, `avg_loss`, `largest_win`, `largest_loss` | trade P&L distribution |
| `avg_win_loss_ratio`, `max_consecutive_losses`, `avg_holding_period` | extensions |

## 5. Exposure / activity
`average_exposure_pct`, `max_exposure_pct`, `time_in_market_pct`, `turnover` (extensions).

## 6. Cost & integrity (mandatory)
| Metric | Definition |
|---|---|
| `total_costs`, `total_commission`, `total_slippage` | realism accounting |
| `rejected_orders` | liquidity/capital/short rejections |
| `lookahead_violations` | **must be 0** for a valid run |
| `signals_generated`, `buy_signals`, `sell_signals` | strategy activity |

## 7. Mapping to live-readiness gates

The lab reports each run against the CLAUDE.md *Strategy Performance Live-Readiness Rule* so backtest evidence is directly comparable to paper-session gates:

| Live gate | Backtest metric |
|---|---|
| Strategy expectancy > 0 | `expectancy > 0` |
| Profit factor > 1.2 | `profit_factor > 1.2` |
| Realized P&L > 0 | `total_return_pct > 0` (net of costs) |
| Section 17 health | composite of the above + drawdown + consistency |

> A passing **backtest** is necessary but **not sufficient** for live: promotion still requires ≥5 valid paper sessions and manual operator sign-off. The lab is advisory.

## 8. Reproducibility header
`metrics.json` includes `engine_version`, `cost_model_version`, `data_snapshot_id`, `git_sha`, and the resolved config hash.
