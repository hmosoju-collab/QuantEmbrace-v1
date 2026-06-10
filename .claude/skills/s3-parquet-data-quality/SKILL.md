---
name: s3-parquet-data-quality
description: Validate Parquet market-data quality and point-in-time/no-lookahead correctness for backtests. Use to audit a data snapshot or a completed run for gaps, bad OHLC, missing corporate actions, survivorship, or leakage. Read-only.
---

# S3 Parquet Data Quality

References: `docs/backtesting/aws-data-lake-contract.md` §6, `docs/backtesting/no-lookahead-rules.md`.

## When to use
Before trusting any backtest: audit the snapshot; or audit a run for `lookahead_violations`.

## Checks
- Uniqueness: no duplicate `(symbol,interval,timestamp)`.
- Ordering: monotonic timestamps per partition; gaps vs trading calendar.
- Sanity: `low <= open,close <= high`; volume >= 0.
- Coverage: first/last bar, % expected bars, gap list per symbol.
- Corporate actions: every split/bonus has an `adj_factor`.
- Survivorship: delisted symbols present for their active window.
- Point-in-time: adjustment as-of; reference read from snapshot, not latest.
- Run validity: `lookahead_violations == 0`.

## Rules
Read-only. Report every finding; never auto-fix or hide issues (`no_silent_failures`).
