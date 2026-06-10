# QuantEmbrace — AWS Historical Backtesting Lab: Specification

> **Status: PLANNED — design only.** Backtest-only. No live trading, no broker orders, no capital change, no live/paper table mutation.
> Version: AWS-BT-1 · Last updated: 2026-06-06 · Built on `aws-phase0-discovery-report.md`. Governed by `aws-backtesting-steering.md`.
> Deep detail lives in the companion contracts (`aws-data-lake-contract.md`, `aws-backtest-run-registry.md`, `no-lookahead-rules.md`, `cost-slippage-model.md`, `metrics-catalog.md`, `walk-forward-validation.md`, `model-dataset-spec.md`, `aws-serverless-genai-backtesting-design.md`); this document is the master spec that ties them together.

---

## 1. Objective

Provide a reliable, reproducible AWS lab to backtest QuantEmbrace strategies over **10–15 years of NSE history**. It exists to validate strategy edge fast (vs. one paper day at a time), run **TEE old-vs-new** and **walk-forward** validation, and generate **AI quality-scorer training datasets**. It is **advisory only** — it never promotes a strategy, changes capital, or alters trading behavior. Live trading remains gated behind paper sessions + manual sign-off (CLAUDE.md).

Reuse-first: the existing engine `services/strategy_engine/backtesting/backtester.py` is the core; this lab wraps it (`prefer_refactor_over_rewrite`).

## 2. Supported segments

| Segment | v1 scope | Notes |
|---|---|---|
| **NSE EQ cash** | ✅ in scope | Primary. Equity series `EQ`/`BE`; includes delisted names for survivorship. |
| **NSE indices** | ✅ in scope | NIFTY 50/100/200/500 + sector indices — for index-relative strategies and point-in-time universe membership (§7). |
| **NSE F&O** | 🔭 optional later | Design hooks only (instrument keys, expiry/strike, lot size, rollover). **Not built in v1.** No options Greeks/IV modeling in v1. |

US/Alpaca equities remain supported by the engine but are out of scope for this NSE-focused spec.

## 3. Supported timeframes

`1m`, `5m`, `15m`, `1d`. Stored as distinct partitions (`interval=`) in the lake.

- `1d` (daily) is the **backbone** — 15 yr available free/official (NSE Bhavcopy) and survivorship-safe. Acquisition + DQ design: `daily-bhavcopy-ingestion-design.md`.
- `1m/5m/15m` (intraday) require a **licensed/vendor** feed (§8); depth varies and is layered in once procured.
- Higher intervals (5m/15m) may be **derived** from 1m by resampling, recorded with `source=derived` and provenance to the base interval.

## 4. S3 raw / processed data layout

Canonical (supersedes the conflicting legacy paths catalogued in Phase 0 §2). Full contract: `aws-data-lake-contract.md`.

```
s3://quantembrace-backtest-data/
  raw/{source}/{ingest_date}/...                 # immutable original drops (HIGH or quarantined LOW)
  quarantine/{source}/{ingest_date}/...          # LOW-trust data awaiting validation (§8)
  lake/ohlcv/market={NSE}/segment={EQ|INDEX}/symbol={SYM}/interval={1m|5m|15m|1d}/year={YYYY}/part-*.parquet
  reference/
    corporate_actions/{symbol}.parquet           # splits, bonuses, dividends (§6)
    symbol_map/isin_map.parquet                  # ISIN ↔ symbol, rename history (§6)
    instruments/instruments_{snapshot}.parquet   # master incl. delisted
    index_membership/{index}/{effective_date}.parquet   # point-in-time constituents (§7)
    calendars/nse_trading_calendar.parquet
  _snapshots/{data_snapshot_id}.json             # manifest: sources, versions, checksums, trust_level

s3://quantembrace-backtest-results/
  runs/{run_id}/{config.json, trades.parquet, metrics.json, equity_curve.parquet, labels.parquet, logs/, report.md}
  walkforward/{study_id}/...
  datasets/{dataset_id}/...
```

Raw is immutable; the curated `lake/` is the only surface the replay engine reads. Lifecycle: `lake`/`reference` Standard→IA 30d→Glacier IR 365d; `results`/`datasets`/`walkforward` retained.

## 5. Candle schema (curated OHLCV Parquet)

| Column | Type | Notes |
|---|---|---|
| `timestamp` | timestamp (UTC stored; IST in metadata) | bar **close** time |
| `symbol` | string | NSE trading symbol (as-of) |
| `isin` | string | **stable join key** across renames (§6) |
| `market` | string | `NSE` |
| `segment` | string | `EQ` / `INDEX` (`FNO` later) |
| `interval` | string | `1m`/`5m`/`15m`/`1d` |
| `open,high,low,close` | double | **unadjusted** raw prices |
| `volume` | long | traded qty (0 for indices) |
| `adj_factor` | double | cumulative corp-action factor; 1.0 if none — applied at read time |
| `source` | string | `bhavcopy`/vendor/`derived` |
| `trust_level` | string | `HIGH` / `LOW` (§8) |
| `ingested_at` | timestamp | lineage |

Invariant: `low ≤ open,close ≤ high`; monotonic timestamps per partition; no duplicate `(isin, interval, timestamp)`.

## 6. Corporate actions & symbol mapping

- **Corporate actions** (`reference/corporate_actions/`): splits, bonuses, dividends, face-value changes, each with an **effective date** and a derived `adj_factor`. Prices stored **unadjusted**; back-adjustment is computed **as-of the simulated date** so future actions never leak into past bars (`no-lookahead-rules.md`).
- **Symbol mapping** (`reference/symbol_map/`): NSE symbols change over 15 yr (renames, mergers). **ISIN is the stable primary key**; the map records `isin → symbol` with effective-date ranges. All lake joins and universe reconstruction key on ISIN, never on the raw ticker.
- **Survivorship**: delisted/merged symbols are retained in the instrument master for their active window; they must be present in backtests of those periods.

## 7. Historical index membership

Point-in-time constituent sets are **required** for survivorship-correct universes and index-relative strategies.

- `reference/index_membership/{index}/{effective_date}.parquet` holds the constituent ISIN set for NIFTY 50/100/200/500 (+ sector indices) at each rebalance.
- The as-of universe for a simulated date is the constituent set **effective on or before that date** — never today's membership.
- Sourcing: NSE historical index data / licensed vendor (HIGH trust); free/scraped membership is **LOW trust** and quarantined until validated (§8). Absent membership for a date ⇒ that date is excluded from index-dependent runs (logged, not silently filled).

## 8. Data trust levels

Two tiers, recorded per source in the snapshot manifest, per dataset in the registry, and per row (`trust_level`).

| Tier | Sources | Handling |
|---|---|---|
| **HIGH** | Official **NSE Bhavcopy**, licensed vendors (TrueData / GlobalDataFeeds), exchange feeds | Land in `raw/`, validate (§9), promote to `lake/`. **Required for production strategy validation.** |
| **LOW** | GitHub / Kaggle / scraped / free third-party | Land in `quarantine/`. **Never** read by the engine until they pass full validation **and** are reconciled against a HIGH source; only then promoted to `lake/` with provenance. Used for exploration only, never for go/no-go evidence. |

Promotion path: `raw|quarantine → validate → reconcile (LOW only) → lake`. A run's `report.md` states the trust level of every input; a run that touched any unreconciled LOW data is flagged **non-authoritative**. (Note: NSE Bhavcopy is official → **HIGH**, despite being free.)

## 9. Data quality rules (snapshot gate)

A `data_snapshot_id` is only published if it passes:
- **Uniqueness**: no duplicate `(isin, interval, timestamp)`.
- **Ordering/continuity**: monotonic timestamps; gaps logged vs the trading calendar.
- **OHLC sanity**: `low ≤ open,close ≤ high`; volume ≥ 0; no negative/zero prices.
- **Coverage**: per-symbol first/last bar + % expected bars present; coverage below threshold blocks the symbol.
- **Corporate-action coverage**: every split/bonus in the window has an `adj_factor`.
- **Survivorship**: delisted symbols present for their active window.
- **Index membership coverage** for any index-dependent run.
- **Trust reconciliation**: LOW data matches a HIGH source within tolerance before promotion.

Failures are logged and **block** the snapshot — never silently dropped (`no_silent_failures`). Full detail: `s3-parquet-data-quality` skill + `aws-data-lake-contract.md §6`.

## 10. Backtest modes

| Mode | What runs | Use | Speed |
|---|---|---|---|
| **Strategy-only fast** | bars → strategy → `Backtester` (existing engine), no services | Rapid edge iteration / parameter sweeps | Fastest |
| **Platform replay** | bars → strategy → **risk validators** (deterministic subset) → **execution simulator** | Test platform-realistic signal→approval→fill behavior offline | Medium |
| **TEE replay** | replay signals through **old vs new** `TradeExitEngine` + **MIS square-off** simulation | Old-vs-new exit comparison; intraday square-off validation | Medium |
| **Walk-forward** | IS-optimize → OOS-validate over rolling/anchored folds; each fold a registered run | Overfitting guard; honest OOS edge | Slowest |

All modes: backtest-only, costs+slippage on, `lookahead_violations == 0`, results to the registry + S3. No mode calls a broker, mutates trading state, or enables live. Platform/TEE replay reuse the live logic where deterministic but execute against the simulator, never a broker.

## 11. Execution simulator design

Deterministic fill model (extends `backtester.py`):
- **Next-bar execution** only; signal stamped with producing bar; fill iff `fill_ts > generated_at` (else counted as a lookahead violation).
- **Mandatory costs + slippage** every fill (§12).
- **Gap-through stops** fill at the worse bar open; **stops/TP** checked on subsequent bars.
- **Liquidity cap**: reject orders > `max_order_bar_volume_pct` of bar volume (counted in `rejected_orders`).
- **Determinism**: fixed seeds, no wall-clock in logic, deterministic same-timestamp ordering.
- Emits per-trade records (entry/exit, costs, slippage, reason) → `trades.parquet` and per-signal outcomes → `labels.parquet`.
- **No broker SDK** in the simulator path (CI-enforced).

## 12. Cost / slippage model

Mandatory and on by default. Full detail: `cost-slippage-model.md`.
- **Indian statutory costs** (`IndianCostModel`): STT (sell), exchange txn, SEBI turnover, stamp (buy), GST on (brokerage+exchange+SEBI); plus brokerage per leg.
- **Slippage**: `slippage_bps` + half of `spread_bps` adverse on each fill.
- **Models** (from `commands/run_backtest.yaml`): `slippage_model ∈ {fixed, percentage, volume_based}`, `commission_model ∈ {zerodha, alpaca, zero}`. (Phase 0: only `fixed`+IndianCostModel implemented today; `percentage`/`volume_based` to be added.)
- **`cost_model_version`** stamped on every run. Disabling any component requires an explicit flag, is logged, and is flagged in `report.md`.

## 13. No-lookahead rules

Enforced and asserted (`lookahead_violations == 0` per valid run). Full list: `no-lookahead-rules.md`.
- Next-bar fills; trailing-window indicators only; stops/TP on subsequent bars; gap honesty.
- Point-in-time data: corp-action adjustment as-of; **as-of index membership & instrument master** (no future constituents); ISIN-keyed joins.
- Survivorship: delisted symbols included for their window.
- Train/test separation for walk-forward (IS strictly precedes OOS) and datasets (time split + embargo).
- Determinism so leakage cannot hide behind randomness.

## 14. Run registry design

DynamoDB, `qe-bt-` prefix. Full schema: `aws-backtest-run-registry.md`.
- **`qe-bt-runs`** (PK `run_id` = `bt_{date}_{config_hash[:8]}_…`): `config_hash`, `status` (`PENDING→RUNNING→CHECKPOINTED→COMPLETED/FAILED/CANCELLED`), strategy/symbols/interval/date range, `engine_version`/`cost_model_version`/`data_snapshot_id`/`git_sha`, `metrics_summary`, `s3_prefix`, timestamps, `error`. GSIs: status, strategy, config_hash (idempotent lookup).
- **Idempotency**: deterministic `run_id` from config; conditional-write claim prevents duplicate compute (mirrors live `orders` pattern).
- Registry is the **index of record**; advisory only — it records results, never triggers trades.

## 15. Checkpoint / resume design

`qe-bt-checkpoints` (PK `run_id`, SK `shard_id`). Full detail: registry doc §4.
- Shards by `(symbol|ISIN, year)` (or date-chunk). Each shard checkpoints `cursor` (last bar ts) + `partial_state` every N bars / M seconds, with `heartbeat_at` + `worker_id`.
- **Resume**: an interrupted worker's stale heartbeat lets another reclaim the shard (conditional write); resume from `cursor` (exclusive).
- **Guarantee**: a resumed run produces **identical** metrics to an uninterrupted one (acceptance check, later phase). Every long run is resumable (`restart_safety`).

## 16. Metrics & reports

Full catalog: `metrics-catalog.md`. Per run → `metrics.json` + summary in `qe-bt-runs`.
- Returns (total, annualised), risk-adjusted (Sharpe, Sortino, Calmar), drawdown (% / abs / duration / stress), trade stats (win rate, profit factor, **expectancy**, avg/largest win-loss), exposure/turnover, costs/slippage, `rejected_orders`, `lookahead_violations`, signal counts.
- **Reports**: deterministic `report.md` per run; aggregate reports for walk-forward studies and TEE comparisons. Each report maps results to the **live-readiness gates** (expectancy > 0, profit factor > 1.2, realized P&L > 0) — clearly labeled **advisory** (a passing backtest never auto-promotes).

## 17. Model-training label dataset

Full spec: `model-dataset-spec.md`. Generates **data only** — never trains/deploys models or touches live `ai_engine` artifacts.
- Products: `signal_quality` (label = net-of-cost profitable?) for the GBT quality scorer; `regime` for the HMM classifier.
- Features: point-in-time, matching the live `shared/features/feature_reader.py` set (RSI/EMA/VWAP/ATR/ADX/MACD/vol_ratio) + signal metadata.
- Splits: time-based train/val/test with an embargo gap (no feature/label time overlap). Registered in `qe-bt-datasets` with `schema.json`/`manifest.json`, class balance, and versions.

## 18. AWS serverless GenAI analysis layer

Full design: `aws-serverless-genai-backtesting-design.md`. **Advisory only; on-demand/event-driven; no SQS, no polling.**
- On run `COMPLETED`: EventBridge → Step Functions → context builder (redacted) → LLM → persist `report.md` + `genai_summary`; plus operator RAG Q&A over registry/S3 artifacts.
- **Open decision (Phase 0 §9):** the repo already uses the **Anthropic SDK (Claude Haiku)** in `ai_engine/agents/strategy_selector.py`, not **Bedrock**. Resolve Bedrock-vs-Anthropic-SDK before building (this spec keeps the layer abstract behind an `LLMProvider` interface).
- Guardrails: no live/paper access, no secrets/PII in prompts, no auto-apply, per-run token cap + SNS budget alarm; GenAI failure never blocks a run (deterministic report remains).

## 19. Safety rules

Non-negotiable (CLAUDE.md § AWS Historical Backtesting Protocol):
- AWS backtesting only · no live trading · no broker orders · no capital changes · no live table mutation · no paper/live table mixing.
- Use existing AWS architecture (EC2 ARM64 workers, S3, DynamoDB, CloudWatch, SNS). No Fargate/EKS/Lambda-for-compute/SQS/Kinesis unless explicitly approved.
- S3 is the source of historical data; DynamoDB is run registry + metadata only; large outputs to S3.
- Every long run resumable; every run persists config, code version, data version, trades, metrics, equity curve, reports, labels, logs.
- Costs & slippage mandatory; no lookahead leakage.
- GitHub/free = LOW trust → quarantine; licensed/vendor/NSE = HIGH, required for production validation.
- Every phase writes a report and **stops for approval**. Isolation: separate `backtest` env, `qe-bt-` tables, `quantembrace-backtest-*` buckets, lab IAM with **no** broker creds and **no** live/paper access.

## 20. Implementation phases

Detailed gating in `aws-backtesting-implementation-plan.md`. Summary (each phase = one `/aws_bt_*` command, ends with a report + approval stop):

| Phase | Deliverable |
|---|---|
| 0 ✅ | Discovery (`aws-phase0-discovery-report.md`) |
| **1 (next)** | **Data lake**: Parquet lake + bhavcopy daily backbone + reference data + quality gate + `data_snapshot_id` |
| 2 | Run registry + checkpoints (Terraform `environments/backtest/` + client) |
| 3 | Replay engine extension (Parquet `BarSource`, shard runner, checkpoint hooks) |
| 4 | Strategy adapters for all 6 strategies (+ fix stale `run_backtest.yaml` refs) |
| 5 | Execution simulator wired + realism/no-lookahead asserted |
| 6 | TEE old-vs-new + MIS square-off simulator |
| 7 | Metrics + report generator |
| 8 | Walk-forward harness |
| 9 | Model-dataset generator |
| 10 | Serverless GenAI layer (after Bedrock-vs-Anthropic decision) |
| 11 | Full end-to-end multi-year run |

**Pre-Phase-1 housekeeping (advisory):** add the trust-tier/quarantine model to `aws-data-lake-contract.md`, fix `run_backtest.yaml` stale refs (`scripts/backtest/run.py`, `services/strategy_engine/registry.py`), and register the lab in governance + ADR-029.

---

*Specification only. No code implemented, no infra deployed, no live trading enabled. Stop for approval before Phase 1.*
