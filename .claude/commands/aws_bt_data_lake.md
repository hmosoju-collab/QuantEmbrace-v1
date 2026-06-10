---
description: "Phase 1 — design/build the S3 Parquet historical data lake + ingestion + quality checks."
argument-hint: "[source: bhavcopy|vendor|zerodha] [market] [from] [to]"
---

# /aws_bt_data_lake — Phase 1: Historical Data Lake

Build the NSE historical data lake per `docs/backtesting/aws-data-lake-contract.md`. **Backtest-only.** Reads market data; writes only to `quantembrace-backtest-data`.

## Load first
`aws-data-lake-contract.md`, `no-lookahead-rules.md` (§2 point-in-time), `aws-backtesting-steering.md`.

## Do
1. Implement the pluggable `BarSource` interface; start with the **NSE Bhavcopy daily backbone** (free, survivorship-safe). Add vendor/zerodha adapters as pluggable.
2. Normalize into curated Parquet: Hive partitions `market/symbol/interval/year`, unadjusted prices + `adj_factor`, provenance columns.
3. Build `reference/` (corporate actions, instrument master incl. delisted, trading calendar) and write a `data_snapshot_id` manifest.
4. Run data-quality checks (§6 of the contract); fail the snapshot on threshold breach (`no_silent_failures`).

## Safety
No broker order APIs. No live/paper bucket access. No lookahead: store unadjusted + adjust at read time.

## Output / Stop
Lake coverage + data-quality report (per-symbol coverage, gaps, corp-action coverage, snapshot id). **Stop for approval.**
