# Forward Factor Validation Gate (FFG) — positional factor books

**Pre-registered:** 2026-06-19. **Status:** ACTIVE — both books accruing, neither eligible.
**Posture (operator decision 2026-06-19):** *Deploy no capital. Let the forward paper books be the
out-of-sample truth-test. A book must clear this gate FORWARD before capital is even reconsidered.*
**Checker:** `scripts/paper/check_forward_gate.py`. **Live trading remains BLOCKED.**

---

## Why this exists

Across the strategy program, every apparent edge dissolved under proper/out-of-sample testing:
intraday strategies (cost-walled — `strategy-retirement-register-2026-06-19.md`), the gap-reaction
idea (real but thin/year-inconsistent), the delivery factor (Sharpe 1.60→0.61, weak forward), and the
delivery+momentum "diversification" combo (a +0.67-correlated mirage — `combined-book-study-report.md`).
So the bar is set **before** more data arrives, and targets **alpha vs the equal-weight liquid
benchmark**, consistency, and risk — *not* raw return (which is mostly beta).

> **Integrity rule:** these thresholds are fixed as of 2026-06-19 and must **not** be relaxed to make
> a book pass. Moving the goalposts after seeing the data invalidates the whole forward test.

## The gate (ALL criteria must hold)

| # | Criterion | Threshold |
|---|---|---|
| 1 | **Horizon** | ≥ 12 complete forward months (books seeded 2025-12-31 → eligible ~Dec-2026) |
| 2 | **Alpha** | cumulative net return beats the EW liquid benchmark (cumulative alpha > 0) |
| 3 | **Info ratio** | monthly-alpha IR (mean/std·√12) ≥ 0.50 |
| 4 | **Consistency** | positive monthly alpha in ≥ 58% of months **and** no single month > 50% of cumulative alpha |
| 5 | **Risk** | forward max drawdown ≤ the benchmark's over the same window |

**Clearing the gate → human review for a small, gated capital pilot. NEVER auto-deploy.** Costs are
the full NSE delivery stack on actual rebalance turnover — no relaxation. A hard divergence of the
forward record from the backtest is itself informative (delivery already shows this).

## Current status (2026-06-19, 5/12 months)

| Book | Months | Cum alpha | IR | Pos-alpha mo | MaxDD vs bench | Status |
|---|---:|---:|---:|---:|---|---|
| delivery | 5/12 | −8.69% | −1.67 | 40% | −13.3% vs −13.7% | IN PROGRESS (failing early) |
| momentum | 5/12 | +6.58% | +1.73 | 60% | −15.2% vs −13.7% | IN PROGRESS (passing 4/6, higher risk) |

Neither is eligible (horizon). Early reads are noisy (5 months); do not over-read either.

## Monthly cadence (runbook)

After each calendar month closes and the Bhavcopy lake is refreshed:

```bash
# 1. refresh the daily lake to the new month
python scripts/backtest/download_bhavcopy.py --start <YYYY-MM-01> --end <YYYY-MM-DD>

# 2. advance both forward books (deterministic monthly replay; idempotent — rebuilds from inception)
python scripts/paper/replay_delivery_book_forward.py --factor delivery
python scripts/paper/replay_delivery_book_forward.py --factor momentum

# 3. evaluate against the pre-registered gate
python scripts/paper/check_forward_gate.py
```

Reports refresh at `docs/backtesting/{delivery,momentum}-paper-book-forward-report.md`; isolated
state at `backtest-data/paper_book/{delivery,momentum}_book_state.json`. All advisory, no broker.

## Governance

Advisory only. Backtesting can recommend; it cannot promote. A human approves all production changes.
This is a **separate track** from the intraday 5-session paper gate. Clearing the FFG does not enable
live trading — it only unlocks human review of a small gated pilot. Until then: **deploy nothing.**
