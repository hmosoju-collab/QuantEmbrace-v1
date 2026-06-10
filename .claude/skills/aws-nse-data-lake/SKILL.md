---
name: aws-nse-data-lake
description: Build and query the QuantEmbrace S3 Parquet historical NSE data lake. Use when ingesting bhavcopy/vendor/Zerodha history, normalizing to partitioned Parquet, handling corporate actions and survivorship, or producing a data_snapshot_id. Backtest-only; never touches live/paper data or broker order APIs.
---

# AWS NSE Data Lake

Authoritative contract: `docs/backtesting/aws-data-lake-contract.md`.

## When to use
Ingesting or curating historical OHLCV for backtests; building `reference/` (corporate actions, instrument master, calendar); creating a reproducible `data_snapshot_id`.

## Procedure
1. Pick a `BarSource`: bhavcopy (daily backbone, free, survivorship-safe) -> vendor (intraday) -> zerodha (gap-fill only).
2. Normalize -> Parquet partitioned `market/symbol/interval/year`; store **unadjusted** prices + `adj_factor`; add provenance.
3. Build `reference/` and write the snapshot manifest with source versions + checksums.
4. Run quality checks (contract §6); fail on threshold breach.

## Rules
- Read market data only; write only to `quantembrace-backtest-data`.
- Adjust at read time, never bake future corp-actions into past bars (`no-lookahead-rules.md`).
- No live/paper buckets; no broker order calls; no silent data drops.
