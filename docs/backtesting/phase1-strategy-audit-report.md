# Phase 1 Audit — Intraday Strategies by Year and Regime (Retire Candidates)

**Status:** COMPLETE — advisory. Live trading remains BLOCKED.
**Date:** 2026-06-19   **Period:** 2022-01-01 → 2024-12-31 (Zerodha Kite intraday, NIFTY50)
**Capital basis:** fixed ₹10L (no cross-day compounding). **Costs:** NSE **intraday/MIS** statutory stack (~0.035% round-trip — correct for EOD-flat strategies; Phase B used delivery ~0.222%, ~6× harsher) + 5 bps/leg slippage.

> Per-day capital reset makes compounded annualised return unreliable; the valid edge
> metrics are trades/win%/PF/expectancy/net. Sharpe/Sortino/DD below are on the
> fixed-capital DAILY P&L series. Overnight exposure = 0 (MIS flat by EOD).

## Overall (full period)

| Strategy | Trades | Win% | PF | Exp ₹ | Net ₹ | Sharpe(d) | Sortino(d) | MaxDD | DD days | Turn×/day | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `orb` | 2403 | 36.8 | 0.696 | -41.1 | -98,843 | -3.65 | -6.36 | -10.2% | 660 | 0.1 | REJECT |
| `vwap_reversion` | 131 | 29.0 | 0.498 | -82.9 | -10,864 | -4.03 | -6.83 | -1.2% | 94 | 0.07 | REJECT |
| `trend_15m` | 15 | 26.7 | 0.36 | -72.6 | -1,089 | -6.18 | -9.81 | -0.1% | 13 | 0.03 | REJECT |
| `preclose` | 10596 | 21.9 | 0.246 | -37.3 | -394,959 | -19.77 | -20.82 | -39.5% | 731 | 0.36 | REJECT |

## By calendar year (PF / expectancy / net ₹ / trades / daily-Sharpe)

### `orb`

| Year | Trades | Win% | PF | Exp ₹ | Net ₹ | Sharpe(d) |
|---|---:|---:|---:|---:|---:|---:|
| 2022 | 781 | 38.7 | 0.774 | -30.7 | -24,014 | -2.72 |
| 2023 | 836 | 37.9 | 0.727 | -34.8 | -29,071 | -3.13 |
| 2024 | 786 | 33.8 | 0.594 | -58.2 | -45,758 | -5.1 |

### `vwap_reversion`

| Year | Trades | Win% | PF | Exp ₹ | Net ₹ | Sharpe(d) |
|---|---:|---:|---:|---:|---:|---:|
| 2022 | 43 | 32.6 | 0.473 | -80.2 | -3,450 | -5.15 |
| 2023 | 45 | 20.0 | 0.23 | -139.7 | -6,286 | -7.94 |
| 2024 | 43 | 34.9 | 0.838 | -26.2 | -1,127 | -1.0 |

### `trend_15m`

| Year | Trades | Win% | PF | Exp ₹ | Net ₹ | Sharpe(d) |
|---|---:|---:|---:|---:|---:|---:|
| 2022 | 8 | 50.0 | 0.84 | -14.6 | -117 | -1.0 |
| 2023 | 4 | 0.0 | 0.0 | -120.1 | -480 | -19.83 |
| 2024 | 3 | 0.0 | 0.0 | -164.1 | -492 | -35.29 |

### `preclose`

| Year | Trades | Win% | PF | Exp ₹ | Net ₹ | Sharpe(d) |
|---|---:|---:|---:|---:|---:|---:|
| 2022 | 3719 | 25.2 | 0.285 | -35.8 | -133,030 | -17.38 |
| 2023 | 3465 | 21.0 | 0.236 | -36.3 | -125,732 | -22.37 |
| 2024 | 3412 | 19.4 | 0.215 | -39.9 | -136,197 | -20.73 |

## By market regime (equal-weight NIFTY50 vs 50d SMA)

### `orb`

| Regime | Trades | Win% | PF | Exp ₹ | Net ₹ |
|---|---:|---:|---:|---:|---:|
| UPTREND | 1718 | 37.4 | 0.684 | -42.4 | -72,901 |
| DOWNTREND | 683 | 35.4 | 0.727 | -37.5 | -25,597 |
| UNKNOWN | 2 | 0.0 | 0.0 | -172.6 | -345 |

### `vwap_reversion`

| Regime | Trades | Win% | PF | Exp ₹ | Net ₹ |
|---|---:|---:|---:|---:|---:|
| UPTREND | 81 | 28.4 | 0.503 | -78.2 | -6,337 |
| DOWNTREND | 50 | 30.0 | 0.492 | -90.5 | -4,527 |

### `trend_15m`

| Regime | Trades | Win% | PF | Exp ₹ | Net ₹ |
|---|---:|---:|---:|---:|---:|
| UPTREND | 8 | 12.5 | 0.135 | -112.1 | -897 |
| DOWNTREND | 7 | 42.9 | 0.71 | -27.5 | -193 |

### `preclose`

| Regime | Trades | Win% | PF | Exp ₹ | Net ₹ |
|---|---:|---:|---:|---:|---:|
| UPTREND | 7271 | 21.7 | 0.242 | -36.7 | -267,019 |
| DOWNTREND | 3320 | 22.4 | 0.256 | -38.4 | -127,439 |
| UNKNOWN | 5 | 20.0 | 0.084 | -100.3 | -502 |

## Read

- The diagnostic question: do these lose **universally** (costs > edge) or only in a
  particular year/regime (regime-dependence / alpha-decay)? Compare the by-year and
  by-regime PF columns — PF < 1 in *every* cell ⇒ structural cost-bleed, not bad luck.

> Backtesting can recommend. It cannot promote. A human approves all production changes.
