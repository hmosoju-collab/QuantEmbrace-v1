# QuantEmbrace — Minimal Official NSE Bhavcopy Daily Ingestion (Design)

> **Status: PLANNED — design only.** The ingestion + normalization tooling
> (`services/backtesting/{ingestion,s3_data_catalog,data_loader,data_quality}.py`,
> `scripts/backtest/{ingest_nse_history_to_s3,normalize_nse_history,validate_s3_nse_history}.py`)
> is implemented and sample-validated; **no real Bhavcopy has been ingested yet.**
> Backtest-only: reads market data, writes the backtest namespace only — no broker
> APIs, no live/paper access.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md` ·
> Canon: `aws-data-lake-contract.md`, `no-lookahead-rules.md`.

Goal: define the **minimal** official daily-OHLCV path needed to produce the
**first authoritative, non-synthetic backtest**, reusing existing tooling
(`prefer_refactor_over_rewrite`).

**Scope:** daily OHLCV only · free/official data first · no 1-minute vendor
dependency yet · no AWS overbuild unless necessary.

## No-overbuild principle (read first)

This first backbone requires **no AWS**. `DataCatalog` / `data_loader` /
`Normalizer` already accept a **local** base path, and the Hive partition layout
is byte-identical local vs `s3://`. Run the entire first authoritative backbone on
a **local Parquet lake**; lifting to S3 later is a base-path swap, not a redesign.
Provision S3/Terraform only when scale or sharing demands it.

---

## 1. Source acquisition checklist

**Recommended primary (v1):** `sec_bhavdata_full_DDMMYYYY.csv` — the Security-wise
Full Bhavcopy + Deliverable report. One CSV per trading day, EQ OHLCV + delivery +
series, ~310 KB, coverage **2016 → present**. Simplest authoritative path; no zip
parsing; carries the delivery columns for free.

- [ ] Pull from the official archives host only:
      `https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv`.
      Provenance = the exchange itself → this is what earns HIGH trust (§3).
- [ ] Handle the NSE bot-shield: send a real browser `User-Agent`, do a priming
      `GET https://www.nseindia.com` to obtain session cookies, then request the
      archive with those cookies; throttle (~1 req/s) and back off on 401/403.
- [ ] Iterate **trading days only** — skip weekends and NSE holidays via an NSE
      trading calendar (also required by the `missing_trading_days` DQ check). A
      missing file on a real holiday is expected, not a gap.
- [ ] Store each file **verbatim** (no edits) and record its `sha256`. The existing
      `Ingestor` writes `_ingest_manifest.json` + `checksums.sha256`. Re-download is
      idempotent: checksum match ⇒ skip.
- [ ] Register `source_name="bhavcopy"` (classifies HIGH). Any mirror/Kaggle/GitHub
      copy must use a **different** source_name so the classifier auto-quarantines it.
- [ ] ISIN is **not** present in `sec_bhavdata_full`. Fetch the equity master
      `EQUITY_L.csv` (`/content/equities/EQUITY_L.csv`) once for a SYMBOL→ISIN
      reference; store under `reference/symbol_map/`.

**Canonical post-2024 / ISIN-bearing cross-check:** the UDiFF CM bhavcopy
`BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip` replaced the legacy
`cm…bhav.csv.zip` on **2024-07-08** (NSE circular 62424). It carries ISIN natively
but is zipped and mixes all instrument types (filter series ∈ {EQ, BE}). Optional
for v1 — useful to backfill ISIN and cross-validate `sec_bhavdata_full`.

> Note: `sec_bhavdata_full` columns are whitespace-padded in the file (e.g.
> ` SYMBOL`, ` SERIES`, ` OPEN_PRICE`, ` HIGH_PRICE`, ` LOW_PRICE`, ` CLOSE_PRICE`,
> ` PREV_CLOSE`, ` LAST_PRICE`, ` AVG_PRICE`, ` TTL_TRD_QNTY`, ` DELIV_QTY`,
> ` DELIV_PER`, ` DATE1`). Confirm the exact header at parse time; `Normalizer`
> must strip whitespace and map to canonical columns.

## 2. Expected raw file layout

Exactly what `Ingestor.ingest()` produces today (HIGH → `raw/`):

```
<base>/backtest-data/                      # <base> = local dir OR s3://quantembrace-backtest-data
  raw/bhavcopy/{ingest_date=YYYY-MM-DD}/
      sec_bhavdata_full_DDMMYYYY.csv       # bytes preserved verbatim
      _ingest_manifest.json                # source=bhavcopy, trust_level=HIGH, data_version,
                                           #   sha256[], segment=EQ, timeframe=1d, market=NSE
      checksums.sha256
  reference/
      symbol_map/equity_l_{snapshot}.csv   # SYMBOL → ISIN
      calendars/nse_trading_calendar.parquet
```

Mirrors/untrusted copies land in `quarantine/{source}/{ingest_date}/` and never
enter the lake (`Normalizer` raises `QuarantineError`).

## 3. Trust-tier assignment

**HIGH — conditionally.** The discriminator is provenance + integrity, not price.
Contract §1: *"Bhavcopy is HIGH trust despite being free — it is official NSE data."*

| Condition | Tier | Zone |
|---|---|---|
| Fetched **directly** from `nsearchives.nseindia.com`, `sha256` recorded, `source_name=bhavcopy` | **HIGH** | `raw/` → eligible for `lake/` |
| Any third-party mirror (GitHub / Kaggle / scraper), integrity unverified | **LOW** | `quarantine/` only |

`classify_source_trust()` maps `bhavcopy → HIGH`; unknown → LOW (safe default).
`eligible_for_use = passed AND HIGH` — a LOW snapshot can never feed strategy
eligibility or model-training datasets.

## 4. S3 / Parquet partition layout

Unchanged from contract §4/§8; daily simply pins `interval=1d` (same path local or S3):

```
lake/ohlcv/market=NSE/segment=EQ/symbol={SYM}/interval=1d/year={YYYY}/part-*.parquet
```

Parquet + Snappy, ~128 MB target, predicate pushdown on `timestamp`, **one writer
per `(symbol, 1d, year)`** ⇒ idempotent re-writes. Canonical columns
(`data_loader.CANONICAL_COLUMNS`): `timestamp` (tz-aware IST = bar **close**; daily
⇒ 15:30 IST), `symbol`, `isin`, `market`, `segment`, `interval`,
`open/high/low/close` (**unadjusted**), `volume`, `source`, `trust_level`,
`adj_factor`.

## 5. Data quality checks before `data_snapshot_id`

`data_quality.run_quality_checks()` is the snapshot gate — **any ERROR fails the
snapshot**; it publishes only if clean within threshold. Daily-relevant breakdown:

| Check | Severity | Applies to daily? |
|---|---|---|
| `empty_dataset` | ERROR | yes |
| `timezone_errors` (must be IST-aware) | ERROR | yes |
| `future_timestamps` | ERROR | yes |
| `duplicate_timestamps` on `(symbol, 1d, ts)` | ERROR | yes |
| `invalid_ohlc` (`low ≤ open,close ≤ high`) | ERROR | yes |
| `nonpositive_prices` | ERROR | yes |
| `missing_trading_days` vs NSE calendar | WARN | **yes — key completeness signal** |
| `outlier_jumps` (>20%) | WARN | yes — fires on un-split-adjusted days (§7) |
| `corporate_action_gaps` (>20% overnight) | WARN | yes — same cause (§7) |
| `symbol_mapping_gaps` (no ISIN) | WARN | yes — until SYMBOL→ISIN joined |
| `zero_volume` (EQ) | WARN | yes |
| `market_hours_violation` | ERROR | **N/A** (intraday only) |
| `missing_candles` (375/75/25 bars) | WARN | **N/A** (intraday only) |

On pass, publish `_snapshots/{data_snapshot_id}.json` (sources, `data_version`,
`code_version`, checksums, `trust_level`, row counts, date span). **Every backtest
run must reference this `data_snapshot_id`.**

## 6. Acceptance criteria — "real daily backbone ready"

1. ≥1 full real calendar window (suggest the most recent **3 full FYs**) of
   `sec_bhavdata_full`, EQ series, ingested HIGH-trust with checksums.
2. Normalized to `lake/.../interval=1d/`, canonical schema, tz-aware IST,
   `trust_level=HIGH`.
3. DQ gate: **0 ERROR** issues; WARNs triaged and explained (splits / ISIN gaps
   documented).
4. A published `data_snapshot_id` with `eligible_for_use == True`.
5. Row-count reconciliation: trading days in the lake == NSE calendar trading days
   for the span (holiday-aware), per symbol.
6. `run_full_backtest_aws.py` runs against the Parquet `BarSource` (**not** the
   synthetic generator) referencing that `data_snapshot_id`, and `metrics.json`
   shows **`lookahead_violations == 0`**.
7. Re-running ingestion is idempotent (no duplicate partitions; checksums stable).

Meeting 1–7 = the first authoritative non-synthetic daily backtest. It does **not**
imply strategy edge or live-readiness — those remain gated by CLAUDE.md
§ Strategy Performance Live-Readiness Rule.

## 7. What not to solve yet

- **Intraday / 1-minute vendor** (TrueData / GlobalDataFeeds) — pluggable later via
  `BarSource`; the daily backbone proves the lab end-to-end first.
- **Full 15-yr depth / pre-2016** — needs the legacy daily bhavcopy archive (and
  UDiFF for post-2024). Start with `sec_bhavdata_full` (2016→present); backfill later.
- **Corporate-action `adj_factor` pipeline** — ingest **unadjusted** with
  `adj_factor = 1.0` for v1; accept that `outlier_jumps` / `corporate_action_gaps`
  WARN on split days. As-of back-adjustment is a read-time concern
  (`no-lookahead-rules.md`), so it does not block the lake.
- **Point-in-time index membership / perfect survivorship** — delisted names flow
  through naturally (Bhavcopy lists what traded), but as-of universe reconstruction
  from historical constituent lists is later.
- **Indices, BSE, F&O segments** — EQ-only for the first backbone (`ind_close_all`
  is a trivial add later).
- **AWS infra** (buckets / Terraform / worker ASG) — run locally; provision S3 only
  when scale or sharing demands it. Identical layout, no rework.
- **Scheduled / automated daily cron** — a manual backfill run is enough to prove the
  backbone; automate after acceptance.

---

## References

- NSE — All Reports (Equities): https://www.nseindia.com/all-reports
- NSE — Equity Market Data Reports Download: https://www.nseindia.com/products-services/equity-market-data-reports-download
- NSE — UDiFF file-format readme: https://nsearchives.nseindia.com/content/new_bhavcopy_format_readme.xlsx
- NSE Clearing — UDiFF: https://www.nseclearing.in/udiff
- UDiFF change confirmation (Jul 2024): https://tradingqna.com/t/has-nse-changed-bhavcopy-location/169551
- Daily full bhavcopy + deliverable archive: https://github.com/chartiny/nse-sec-bhavdata-full
- Daily CM UDiFF final archive: https://github.com/chartiny/nse-cm-bhavcopy
- Internal canon: `aws-data-lake-contract.md`, `no-lookahead-rules.md`, `data-ingestion-pending-vendor-report.md`
