# Zerodha Intraday Fetch + Backtest (Backtesting Lab — Phase B)

> **Status:** tooling built + self-tested (2026-06-14). **No real intraday data fetched yet** —
> the fetch is operator-run (needs a live Kite session). Backtest-only · advisory ·
> no live trading · no orders · no capital changes.
> Governed by `aws-backtesting-steering.md`, `aws-data-lake-contract.md`,
> `intraday-data-procurement-memo.md`.

---

## Why this exists

Phase 13/15 validated `momentum` on the free daily Bhavcopy lake. The other four
strategies are **session-based** and need intraday bars the daily lake cannot provide:

| Strategy | Interval | Why intraday |
|---|---|---|
| `orb` | 1m | opening-range breakout needs the 09:15–09:30 prints |
| `vwap_reversion` | 1m | intraday VWAP + volume |
| `trend_15m` | 15m | intraday 15-minute trend |
| `preclose` | 5m | pre-close momentum |

(`scalp_1m` is excluded — Stage-1 DISABLED, lowest procurement priority.)

**Source decision (2026-06-14):** GlobalDataFeeds was the requested vendor, but no GDF
data/credentials exist in the workspace and GDFL only retains ~3 months of 1-min history
(too shallow for walk-forward). We use the **Zerodha Kite `historical_data` API** instead —
free with the base Connect subscription, exchange-validated candles, ~3 years of 1-min depth
for liquid names. This is the procurement memo's sanctioned "limited intraday" tier.

**Trust:** Zerodha is **HIGH trust for provenance** (exchange-validated, the operator's own
authorized feed — classified HIGH in `s3_data_catalog.classify_source_trust`, per data-lake
contract §1). But its **depth is LIMITED** (~3 yr, liquid names). Results are **advisory edge
exploration**, not a 15-yr authoritative backbone. A positive result warrants procuring deeper
licensed vendor data (TrueData / GlobalDataFeeds) before further validation.

---

## Step 1 — Fetch (operator-run, local Mac, needs a fresh token)

The sandbox has no egress to `api.kite.trade` and no live token — run this on the trading host,
exactly like `download_bhavcopy.py`.

```bash
# 1. Refresh today's Zerodha token (expires 07:30 IST daily)
python scripts/zerodha_login.py

# 2. Export creds (or rely on .env: ZERODHA_API_KEY / ZERODHA_ACCESS_TOKEN)
export ZERODHA_API_KEY=...
export ZERODHA_ACCESS_TOKEN=...        # printed by zerodha_login.py

# 3. Dry-run to see the request budget / ETA
python scripts/backtest/fetch_zerodha_intraday.py \
    --universe nifty50 --intervals 1m,5m,15m \
    --start 2022-01-01 --end 2024-12-31 --dry-run

# 4. Fetch (idempotent — re-run to resume; ~minutes at 3 req/sec)
python scripts/backtest/fetch_zerodha_intraday.py \
    --universe nifty50 --intervals 1m,5m,15m \
    --start 2022-01-01 --end 2024-12-31
```

Output lands in the same Parquet lake as the daily backbone, just with intraday intervals:

```
backtest-data/lake/ohlcv/market=NSE/segment=EQ/symbol={SYM}/interval={1m|5m|15m}/year={YYYY}/part-0.parquet
backtest-data/raw/zerodha_intraday/_manifest.json   # source=zerodha_kite, trust=HIGH, license, checksums
```

**Tips:** start with `--universe liquid10` and a 1-year window for a cheap first probe.
`minute` data is capped at 60 days/request (the script chunks automatically); `5m`/`15m`
have larger caps. Kite's intraday history typically reaches back ~3 years for liquid names.

---

## Step 2 — Backtest (runs anywhere with the lake; no broker, no network)

```bash
# All four strategies, full universe
python scripts/backtest/run_intraday_backtest.py \
    --universe nifty50 --start 2022-01-01 --end 2024-12-31

# A subset / cheaper probe
python scripts/backtest/run_intraday_backtest.py \
    --strategies orb,vwap_reversion --universe liquid10 --start 2023-01-01
```

Writes `docs/backtesting/aws-phaseB-intraday-backtest-report.md` with a per-strategy table
(trades, win %, expectancy, profit factor, net P&L, cost drag) and a verdict each:
`ELIGIBLE_FOR_PAPER_PRIORITIZATION` / `PAPER_OPTIMIZATION` / `REJECT` / `NO_TRADES`.

### How it executes (and why)

- **Per trading day, one strategy instance across all symbols.** Session-based strategies
  reset daily (ORB opening range, VWAP, per-day signal budgets). The offline `Backtester`
  never calls `reset_daily()` (that is the live MarketPhaseGovernor's job at POST_CLOSE), so a
  fresh instance per day *is* the daily reset. Running per-day-all-symbols also preserves each
  strategy's **global** daily signal budget across the universe.
- **EOD flatten** is the Backtester's close-at-last-bar — correct for MIS intraday.
- **This fixes the Session-16 "ORB blind" problem**: historical bars include the full 09:15
  opening range, so the range always forms (in Session 16 the live start was 10:19 IST and the
  range never formed).

### Honest limitations (also recorded in the report)

- Capital resets each day (no cross-day compounding) → **Sharpe / annualised return unreliable**;
  expectancy / profit factor / win rate are the valid edge metrics.
- `trend_15m` warm-up is intra-day only (~25 15m bars/day) — **indicative, not conclusive**.
- Exits are modeled by the Backtester (per-bar stop/target from the signal + EOD flatten),
  **not** the live TEE / MIS engine.
- Zerodha intraday depth is limited — advisory edge exploration only.

---

## Governance

> **Backtesting can recommend. Backtesting cannot promote. A human approves all production changes.**

- The **fetcher** reads Kite `historical_data` on the separate 3 req/sec budget. It places **no
  orders**, changes **no trading state** (guarded by `test_fetcher_places_no_orders`).
- The **runner** is a pure offline lake read + backtest — **no broker, no Kite, no live state**
  (guarded by `test_runner_has_no_broker_or_kite`).
- Live trading remains **BLOCKED** — paper-session gates (≥5 consecutive passing sessions) are
  unaffected by this advisory backtest.

Tests: `tests/backtest/test_intraday_fetch_and_backtest.py` (12 tests; both scripts ship an
offline `--self-test`).

---

## Next options after a real run

- **A.** Procure deeper licensed intraday (targeted symbols/years) for any strategy showing edge.
- **B.** Intraday parameter walk-forward (like Phase 15C for momentum) on the strongest strategy.
- **C.** Continue paper sessions (Session 18, ADR-030 gates).
- **D.** GenAI analysis (Phase 10) over the intraday artifacts.
