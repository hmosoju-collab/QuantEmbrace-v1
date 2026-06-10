# QuantEmbrace — Data Ingestion Report (PENDING VENDOR)

> ## ⚠️ NO REAL LICENSED DATA WAS INGESTED
> The `Source:` block in the request was **empty** (no vendor, license, data range, segment, timeframe, file format, or delivery path), and **no vendor files, license notes, data dictionary, or sample files were found** in the workspace or uploads. **Nothing was ingested into any trusted production zone.** The ingestion + normalization **tooling is built and validated on a sample**; real ingestion awaits the vendor delivery + license.
>
> Backtest-only. No broker APIs. No live trading. Generated: 2026-06-06.

---

## 1. Inputs read

| Required input | Status |
|---|---|
| `docs/backtesting/aws-data-lake-contract.md` | ✅ read (zones, schema, trust tiers) |
| vendor license notes | ❌ not provided |
| data dictionary | ❌ not provided |
| sample files | ❌ not provided |
| delivery path | ❌ not provided |

## 2. Source (to be completed by operator)

```
vendor:        <UNSPECIFIED>      # e.g. TrueData / GlobalDataFeeds / NSE-Bhavcopy
license:       <UNSPECIFIED>      # license id / terms
data_range:    <UNSPECIFIED>      # e.g. 2010-01-01 .. 2024-12-31
segment:       <UNSPECIFIED>      # EQ | INDEX | (FNO later)
timeframe:     <UNSPECIFIED>      # 1m | 5m | 15m | 1d
file_format:   <UNSPECIFIED>      # CSV | Parquet
delivery_path: <UNSPECIFIED>      # local dir or s3://...
```

## 3. Tooling built (ready for a real drop)

| Artifact | Purpose |
|---|---|
| `services/backtesting/ingestion.py` | `Ingestor` (raw-preserve + checksums + metadata, trust routing) + `Normalizer` (raw→Parquet lake + quality + quarantine block) |
| `scripts/backtest/ingest_nse_history_to_s3.py` | CLI: copy raw files unchanged → raw/quarantine zone + manifest |
| `scripts/backtest/normalize_nse_history.py` | CLI: normalize raw → curated Parquet lake + data-quality report |
| `tests/backtest/test_data_ingestion.py` | 5 tests (below) |

## 4. Rules — how each is enforced

| Rule | Mechanism |
|---|---|
| Raw files preserved unchanged | bytes copied verbatim; test asserts `read_bytes() == source` |
| Processed files are Parquet | `Normalizer` writes `part-0.parquet` per `year` partition |
| Record source/vendor/license/data_version | `_ingest_manifest.json` (vendor, license, data_version, source, trust_level, segment, timeframe, market, ingest_date) |
| GitHub/free → quarantine only | LOW-trust sources land in `quarantine/`; `Normalizer` refuses to promote them |
| Quarantine ≠ trusted (never mixed) | separate `raw/` vs `quarantine/` prefixes; lake reads only the trusted lake |
| Checksum manifest | per-file `sha256` in the manifest + a `checksums.sha256` file |
| Data-quality report | `Normalizer` runs the Phase-2 quality battery and emits a markdown report |
| No broker APIs / no live | ingestion only reads/writes data files; no broker, no live flags |

## 5. S3 path layout

```
s3://quantembrace-backtest-data/
  raw/{source}/{ingest_date}/<files...>        + _ingest_manifest.json + checksums.sha256   # HIGH trust
  quarantine/{source}/{ingest_date}/<files...> + _ingest_manifest.json + checksums.sha256   # LOW trust
  lake/ohlcv/market=NSE/segment={EQ|INDEX}/symbol={SYM}/interval={1m|5m|15m|1d}/year={YYYY}/part-*.parquet
```

## 6. Sample validation (self-test — NOT real data)

- **Ingest:** HIGH-trust `bhavcopy` sample → `raw/bhavcopy/2026-06-06/` (manifest + checksums). LOW-trust `github` sample → `quarantine/github/2026-06-06/` (separate zone).
- **Normalize:** HIGH-trust sample → `lake/.../year=2020/part-0.parquet` (60 rows, tz-aware IST, eligible). LOW-trust → **blocked** (`QuarantineError`).
- Reproduce: `python scripts/backtest/ingest_nse_history_to_s3.py --self-test` and `python scripts/backtest/normalize_nse_history.py --self-test`.

## 7. Test results

`tests/backtest/test_data_ingestion.py` — **5/5 passing** (full lab suite **86/86**).

| Test | Verifies |
|---|---|
| `raw_to_processed_conversion` | raw preserved byte-for-byte; processed Parquet has canonical IST schema |
| `checksum_generated` | sha256 per file in manifest + `checksums.sha256` |
| `source_metadata_persisted` | vendor/license/data_version/source/trust_level in manifest |
| `s3_path_layout_correct` | raw/quarantine/lake layout; S3 base routes via stub |
| `quarantine_source_blocked_from_model_training` | LOW → quarantine; promotion refused; override flags non-authoritative |

## 8. To ingest real licensed data

1. Fill the §2 Source block; confirm license permits storage + research use.
2. `python scripts/backtest/ingest_nse_history_to_s3.py --delivery-path <path> --vendor <V> --license "<L>" --data-version <id> --source-name <truedata|globaldatafeeds|bhavcopy> --segment EQ --timeframe 1d --base s3://quantembrace-backtest-data`
3. `python scripts/backtest/normalize_nse_history.py --raw-path <raw s3 uri> --symbol <SYM> --interval 1d --source-name <...> --vendor <V> --license "<L>" --data-version <id> --base s3://quantembrace-backtest-data`
4. Review the data-quality report; if PASS, the snapshot becomes the lake's `data_version` for authoritative backtests (re-run `run_full_backtest_aws.py` wired to the Parquet `BarSource`).

> **Pre-req still open:** the `backtest` AWS environment (buckets/tables/worker ASG) is not yet provisioned. Provision it (Terraform `environments/backtest/`) before pushing real data to S3.

---

*No real vendor data supplied → no trusted-zone ingestion performed. Tooling built + sample-validated. No live trading, no broker APIs. Stop for the vendor delivery + license.*
