# US EOD Data Lake — Phase 1 Report (ADR-041)

_2026-07-10 · US equities pivot Phase 1 · Plan: `docs/strategy/us-equities-pivot-plan.md`_

**Status: BUILT + VERIFIED — awaiting operator approval before Phase 2 (candidate screening).**

---

## 1. What was built

`scripts/backtest/download_us_eod.py` — the US counterpart of `download_bhavcopy.py`,
implementing the data-lake contract end to end (LOW trust → quarantine → cross-validate →
promote → snapshot):

- **Primary source: Yahoo Finance v8 chart API** — split-adjusted OHLCV + `adjclose`
  (split+dividend adjusted) + dividend/split event lists. Fetched via a curl_cffi
  chrome-impersonated session (Yahoo hard-429s plain HTTP clients; this is the engine
  behind modern yfinance).
- **Independent verifier: Nasdaq exchange website API** — split-adjusted OHLCV. One
  full-range request per symbol per source (~196 calls total).
- **Quarantine**: raw responses verbatim under `backtest-data/quarantine/us_eod/`
  (`source=yahoo/`, `source=nasdaq/`, `_manifest.json` with SHA-256s, trust=LOW).
- **Validation**: pre-registered gates → `_validation_report.json` (details §3).
- **Promotion**: PASS/WARN symbols only → `backtest-data/lake/ohlcv/market=US/segment=EQ/
  symbol={SYM}/interval=1d/year={Y}/part-0.parquet` — NSE-compatible schema + `adj_close`,
  timestamps 16:00 America/New_York tz-aware, `trust_level=MEDIUM`, `source=yahoo`.
  Universe metadata → `backtest-data/reference/us_universe.json`.
- **Snapshot**: `qe.data.snapshot` pinning (idempotent, content-hashed).
- **qe change (minimal)**: `qe/data/panel.py::load_panel` gained a `tz` keyword
  (default `Asia/Kolkata` — NSE behavior byte-identical; qe suite 69/69 green including
  the ₹0.00 parity suites). Two new unit tests in `tests/qe/test_lake_snapshot.py`.

## 2. The lake

| Metric | Value |
|---|---|
| Symbols promoted | **97** (23 ETFs + 74 mega-caps) |
| Range | 2005-01-03 → 2026-07-09 (frontier = last complete session) |
| Union trading calendar | 5,412 days |
| Total rows | 515,691 |
| Parquet files | 2,100 |
| Data snapshot | **`ds-b9b110ac58d57cae`** |
| Coverage post-inception | min 1.00, median 1.00 (zero gaps, all symbols) |
| Excluded | **MMC** (FAIL: Yahoo 404s the ticker across the whole range — "possibly delisted"/re-tickered post-2026; operator may nominate a replacement mega-cap) |

Panel smoke test through the real qe loaders (`resolve_panel_files(market="US")` +
`load_panel(..., tz="America/New_York")`): 5,412 days × 97 symbols.

## 3. Validation gates + results

Pre-registered gates (constants in the script): coverage ≥98% PASS / ≥95% WARN; max
consecutive missing days ≤3 PASS / ≤10 WARN; OHLC-sanity drops ≤0.1%; cross-source
agreement ≥99.5% of days PASS / ≥98% WARN, else FAIL; verifier overlap <250d → WARN.

**One methodological revision, made before promotion and documented here:** the
cross-source comparison was moved from **price levels to daily returns**. First-run
evidence: 17 symbols "failed" level comparison with a *constant* pre-event offset whose
prevalence exactly matched each symbol's spin-off date (ABT→AbbVie 2013 = 63% of window;
T→WBD 2022 = 20%; IBM→Kyndryl 2021 = 21%; MS→Discover 2007; GE→HealthCare/Vernova
2023-24; XLF→XLRE 2016; GOOGL A/C distribution 2014; …). Diagnosis: **Nasdaq adjusts
history for spin-offs/special distributions, Yahoo only for splits** — a convention
difference, not corruption. Returns are invariant to constant historical scaling, so a
convention gap affects only the 1–2 event days while genuine corruption still fails.
Level stats are retained as informational output (`adjustment_convention_gap` flag).
The offline self-test now covers both cases: per-day noise → FAIL; constant spin-off
offset → PASS + flagged.

**Results: 95 PASS · 2 WARN · 1 FAIL (of 98)**

- Cross-validation quality (96 symbols with verifier): median **99.98%** of days agree
  within 15bps in returns; minimum 99.06%.
- **WARN — BRK-B**: no Nasdaq verifier data (class-B chart endpoint 404s under both
  `BRK.B`/`BRK-B`). Promoted on Yahoo alone, flagged.
- **WARN — IWM**: 99.06% agreement; its 51 disagreement days cluster on 2008 crisis
  sessions (2008-09-19/22 = short-ban chaos); median diff otherwise 3e-8. Promoted.
- **FAIL — MMC**: no primary data (excluded; fail-closed path exercised for real).
- 9 symbols carry the informational spin-off convention flag: GE, HON, IBM, MRK, PFE,
  RTX, SPGI, T, XLF.

## 4. Runtime verification (beyond the gates)

- **SPY 2025-01-02 close = 584.64** in the promoted lake — matches the value captured
  directly from Nasdaq's API in an independent session probe, to the cent.
- **AAPL 4:1 split (2020-08-31)**: adjusted series continuous across the split day
  (ratio 1.034 = AAPL's actual +3.4% move that day; no 4x discontinuity).
- **Dividend adjustment sanity**: adj/close ratio 2005 = 0.52 (KO), 0.50 (TLT), 0.68
  (SPY) — 20 years of distributions, sensible magnitudes; exactly 1.0000 at the frontier.
- **Snapshot idempotency**: re-running the snapshot stage returns the identical
  `ds-b9b110ac58d57cae`.
- **Garbage input**: unknown symbol → clean "YAHOO FAILED (primary — symbol excluded)".
- **tz is load-bearing**: loading US files with the IST default mislabels every bar +1
  day and silently drops the frontier bar in a bounded query (4 rows @ NY tz vs 3
  mislabeled rows @ IST for 2026-07-06→09). Phase 3 must dispatch tz per market inside
  the engine; until then US panels must be loaded explicitly with `tz="America/New_York"`.

## 5. Known limitations (carried into Phase 2)

1. **Mega-cap list is survivorship-selected** (current members, by construction). ETF
   strategies — the primary substrate — are unaffected; any single-stock strategy in
   Phase 4 must address this explicitly.
2. **Yahoo restates `adjclose` history on every dividend** (rolling re-adjustment). The
   quarantined raw JSON + content-hashed snapshot pin exactly what this run saw; refreshes
   create new snapshots rather than silently mutating history.
3. **Dividend adjustment is single-source** (Nasdaq verifies the split-adjusted series
   only). Phase 2 adds an aggregate check of SPY/QQQ total returns against published
   benchmark figures before any candidate verdict is trusted.
4. `delivery_pct` is null for US (NSE microstructure concept) — `Panel.delivery` is empty.
5. **Trust level is MEDIUM** (free, cross-validated) vs the NSE lake's HIGH (official
   archives). Per the standing rule, licensed/vendor data is required before any live
   decision built on this lake.
6. MMC missing (74/75 mega-caps until a replacement is nominated).

## 6. Next (awaiting approval)

**Phase 2 — candidate selection + screening**: LEAN CLI workspace, lake→LEAN custom-data
converter, 5–8 QC-library positional candidates, pre-registered screen gate, shortlist
2–3 → `docs/strategy/us-qc-candidate-report.md`. **Stop for approval.**
