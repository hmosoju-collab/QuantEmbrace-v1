# QuantEmbrace — AWS Backtest Data Lake Contract

> **Status (AWS-BT-2):** the **validation tooling is implemented** (`services/backtesting/{s3_data_catalog,data_loader,data_quality}.py`, `scripts/backtest/validate_s3_nse_history.py`, `tests/backtest/test_data_layer.py`). The **lake itself — real 10–15 yr data, S3 buckets, AWS infra — is `[PLANNED — not yet implemented]`** (sourcing is the Phase 0 §16 blocker). Backtest-only: read market data + write backtest namespace; no broker APIs, no live/paper access.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md`.

This contract is the **single source of truth** for backtest data layout. It supersedes the conflicting paths in `architecture/system_design.md` (`backtest/results/{run_id}/`) and `commands/run_backtest.yaml` (`backtests/{strategy}/{timestamp}/`).

---

## 1. Sources & sourcing strategy

| Tier | Source | Coverage | Cost | Trust | Role |
|---|---|---|---|---|---|
| Daily backbone | **NSE Bhavcopy** (official archives) | 15+ yr EOD OHLCV, **incl. delisted** | Free | **HIGH** | Primary backbone; survivorship-safe |
| Intraday | **Vendor** (TrueData / GlobalDataFeeds / user-supplied) | 1m/5m/15m, depth varies | Paid | **HIGH** | Added when procured (pluggable) |
| Gap-fill | **Zerodha `historical_data`** (3 req/s budget) | limited intraday depth | API | **HIGH** | Fill gaps only — *not* a 15-yr intraday source |
| Exploration | **GitHub / Kaggle / scraped / free** | varies, unverified | Free | **LOW** | Quarantine only; never eligibility/training without review |

A **pluggable `BarSource`** maps each source into the curated lake without engine changes. The daily backbone proves the full lab end-to-end; intraday is layered in later. **Bhavcopy is HIGH trust despite being free — it is official NSE data.** The minimal acquisition path and pre-snapshot DQ gate for the daily backbone are detailed in `daily-bhavcopy-ingestion-design.md`. The end-to-end run protocol that consumes a HIGH-trust snapshot is `daily-data-backtest-protocol.md`. Intraday-data procurement (vendors, cost, licensing, staged path) is assessed in `intraday-data-procurement-memo.md`.

## 2. Source trust tiers & quarantine (mandatory)

Two tiers, recorded in the snapshot manifest, the dataset registry, and per row (`trust_level`). Implemented in `s3_data_catalog.classify_source_trust()` — **unknown sources default to LOW** (safe default).

| Tier | Sources | Zone | Eligibility |
|---|---|---|---|
| **HIGH** | official NSE (Bhavcopy), licensed vendors, exchange feeds | `lake/` (engine-readable) | Usable for strategy validation **and** model-training datasets |
| **LOW** | GitHub / Kaggle / scraped / free / unknown | `quarantine/` (NOT engine-readable) | **Never** used for strategy eligibility or model training until it passes quality + license review and is **explicitly approved**, then reconciled against a HIGH source and promoted to `lake/` |

Promotion path: `raw|quarantine → validate (§6) → (LOW only) reconcile vs HIGH → promote to lake/`. Every run's `report.md` states the trust level of each input; a run touching unreconciled LOW data is flagged **non-authoritative** (`data_quality.QualityResult.eligible_for_use == False` whenever quarantined).

## 3. Buckets

- `quantembrace-backtest-data` — raw drops, quarantine, curated lake, reference data.
- `quantembrace-backtest-results` — run outputs, walk-forward studies, datasets, reports.

## 4. Key layout

```
s3://quantembrace-backtest-data/
  raw/{source}/{ingest_date}/...                       # immutable original drops
  quarantine/{source}/{ingest_date}/...                # LOW-trust landing zone (§2)
  lake/ohlcv/market={NSE}/segment={EQ|INDEX}/symbol={SYM}/interval={1m|5m|15m|1d}/year={YYYY}/part-*.parquet
  reference/
    corporate_actions/{symbol}.parquet                 # splits, bonuses, dividends → adj_factor
    symbol_map/isin_map.parquet                        # ISIN ↔ symbol, rename history (stable key)
    instruments/instruments_{snapshot}.parquet         # symbol master incl. delisted
    index_membership/{index}/{effective_date}.parquet  # point-in-time constituents
    calendars/nse_trading_calendar.parquet
  _snapshots/{data_snapshot_id}.json                   # manifest: sources, versions, checksums, trust_level

s3://quantembrace-backtest-results/
  runs/{run_id}/{config.json, trades.parquet, metrics.json, equity_curve.parquet, labels.parquet, logs/, report.md}
  walkforward/{study_id}/folds/{fold_id}/ -> run_id, aggregate.json
  datasets/{dataset_id}/{train,val,test}.parquet, schema.json, manifest.json
```

Path construction is implemented in `s3_data_catalog.DataCatalog` (works for both local and `s3://` bases).

## 5. Curated OHLCV schema (canonical)

Matches `data_loader.CANONICAL_COLUMNS`:

| Column | Type | Notes |
|---|---|---|
| `timestamp` | timestamp, **tz-aware IST** (Asia/Kolkata) | bar **close** time |
| `symbol` | string | NSE trading symbol (as-of) |
| `isin` | string | **stable join key** across renames |
| `market` | string | `NSE` |
| `segment` | string | `EQ` / `INDEX` (`FNO` later) |
| `interval` | string | `1m` / `5m` / `15m` / `1d` |
| `open,high,low,close` | double | **unadjusted** raw prices |
| `volume` | long | traded quantity (0 for indices) |
| `source` | string | provenance |
| `trust_level` | string | `HIGH` / `LOW` |
| `adj_factor` | double | (lake-stored) cumulative corp-action factor; applied at read time |

**Adjustment policy:** the lake stores **unadjusted** prices + `adj_factor`; back-adjustment is applied at **read time** for the as-of date, so future corporate actions never leak into past bars (`no-lookahead-rules.md`). **Symbols change over 15 yr → ISIN is the stable key** for all joins and universe reconstruction.

## 6. Data quality checks (Phase-1 snapshot gate)

Implemented in `data_quality.run_quality_checks()`. ERROR-severity issues **fail** the dataset; a LOW-trust dataset is never eligible regardless of outcome (`no_silent_failures`).

| Check | Severity | Detects |
|---|---|---|
| `duplicate_timestamps` | ERROR | duplicate `(symbol, interval, timestamp)` |
| `invalid_ohlc` | ERROR | not `low ≤ open,close ≤ high` |
| `nonpositive_prices` | ERROR | zero/negative OHLC |
| `future_timestamps` | ERROR | timestamp > now |
| `timezone_errors` | ERROR | naive/unparseable timestamps (expect IST) |
| `market_hours_violation` | ERROR | intraday bars outside **09:15–15:30 IST** |
| `missing_candles` | WARN | fewer than expected bars/day (1m=375, 5m=75, 15m=25) |
| `missing_trading_days` | WARN | absent trading days vs calendar |
| `outlier_jumps` | WARN | >20% bar-to-bar move (bad tick / unadjusted split) |
| `zero_volume` | WARN | zero-volume EQ bars (skipped for indices) |
| `symbol_mapping_gaps` | WARN | rows lacking an ISIN mapping |
| `corporate_action_gaps` | WARN | >20% overnight gap with no corp-action record |

A run **must reference a `data_snapshot_id`**; a snapshot is published only if it clears these checks within threshold.

## 7. Loader capabilities (implemented)

`data_loader.load_candles()` supports: **local path** & **`s3://` prefix** (partitioned reads via `list_objects_v2`); **CSV** & **Parquet** (auto-detected); intervals **1m/5m/15m/1d**; **IST** normalisation (naive→IST localise, aware→convert; mixed timestamp formats handled); **multi-symbol** frames; inclusive **date-range** filtering; trust tagging. S3 uses the sanctioned `shared.aws.clients.get_s3_client` (or an injected client for tests / LocalStack) — never a raw `boto3.client` (TID251 banned-api).

## 8. Partitioning & format

- Hive-style partitions: `market / segment / symbol / interval / year`.
- Parquet, Snappy, ~128 MB target file size, predicate pushdown on `timestamp`.
- One writer per `(symbol, interval, year)` partition → idempotent writes.

## 9. Lifecycle & cost

- `lake/` & `reference/`: Standard → IA 30d → Glacier IR 365d.
- `quarantine/`: short TTL; promote or purge after review.
- `results/runs/`, `datasets/`, `walkforward/`: **never auto-deleted** (audit/repro).
- VPC Gateway Endpoint for S3 (no NAT data charges), per `docs/06_aws_infrastructure.md`.

## 10. Access

Worker IAM role: `s3:GetObject` on `*-backtest-data/*`, `s3:PutObject` on `*-backtest-results/*`. **No** access to live/paper buckets. **No** Secrets Manager broker credentials.

## 11. Validation tooling (implemented — AWS-BT-2)

| Artifact | Path |
|---|---|
| Catalog / trust tiers / zones | `services/backtesting/s3_data_catalog.py` |
| Loader (local/S3, CSV/Parquet, IST) | `services/backtesting/data_loader.py` |
| Quality checks + report renderer | `services/backtesting/data_quality.py` |
| CLI validator | `scripts/backtest/validate_s3_nse_history.py` |
| Tests | `tests/backtest/test_data_layer.py` |
| Report output | `reports/data-quality/nse-10-15y-data-quality-report.md` |

```bash
# Validate a real dataset (local or S3)
python scripts/backtest/validate_s3_nse_history.py \
  --source-path <path|s3://...> --symbol RELIANCE --interval 1m --source-name bhavcopy
# Self-test the harness (synthetic HIGH + LOW datasets)
python scripts/backtest/validate_s3_nse_history.py --self-test
```
