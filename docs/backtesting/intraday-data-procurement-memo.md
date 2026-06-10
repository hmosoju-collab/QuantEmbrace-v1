# QuantEmbrace — 1-Minute NSE Historical Data: Procurement Decision Memo

> **Status: advisory decision memo (design only).** No purchase implied, no
> implementation. Retail-practical guidance for a solo operator applying
> institutional discipline.
> Last updated: 2026-06-06 · Governed by `aws-backtesting-steering.md` ·
> Relates: `aws-data-lake-contract.md` (trust tiers/licensing),
> `daily-bhavcopy-ingestion-design.md` (the free daily backbone),
> `daily-data-backtest-protocol.md` (what to prove first).

**Question:** is deep 10–15 yr 1-minute data necessary now, what might it cost,
and what cheaper staged alternatives exist? **Bottom line: deep 1-min is not
necessary now — it is the most expensive and least available tier, and should be
last.** Daily (free) is sufficient to prove the first edge; buy limited intraday
only when a strategy earns it.

---

## 1. Data requirements by strategy type

| Strategy | Interval needed | History to *start* | 1-min required? |
|---|---|---|---|
| `momentum` (daily/positional) | Daily OHLCV | 10–15 yr (free Bhavcopy) | **No** |
| `intraday_trend_15m` | 15-min | 2–3 yr | No — 15m direct or resample |
| `preclose_momentum` | 5-min | 2–3 yr | No — 5m direct or resample |
| `orb` (opening-range breakout) | **1-min** (opening prints) | 1–2 yr liquid universe | Yes (limited) |
| `vwap_reversion` | **1-min** (intraday volume) | 1–2 yr liquid universe | Yes (limited) |
| `scalp_1m` | **1-min / tick** | most data-hungry, microstructure-sensitive | Yes — but Stage-1 DISABLED → lowest priority |

1-min is needed for only ~3 strategies, and **1–3 yr of liquid-universe 1-min is
enough to first test for edge.** Deep 10–15 yr 1-min is not required now.

## 2. Vendor / source options

- **Free / official daily:** NSE Bhavcopy (the backbone) — daily only, HIGH trust, ₹0.
- **Broker historical APIs (cheap, shallow):** Zerodha Kite Connect (1-min ≈ 3 yr,
  60-day/request, 3 req/s; ₹500/mo or free with base Connect sub); Fyers / Upstox /
  Dhan / Angel SmartAPI equivalents. Personal-use ToS.
- **Authorized retail vendors:** TrueData (NSE/BSE/MCX authorized; historical 1-min
  IEOD CSV via Velocity plans); Global Datafeeds / GDFL (NSE Data & Analytics–authorized
  L1 vendor; sold via resellers).
- **Official / institutional (expensive):** NSE Data & Analytics (DotEx) paid historical;
  Refinitiv/LSEG, Bloomberg, AlgoSeek — not retail-economical.
- **Free mirrors (avoid for authoritative use):** GitHub/Kaggle 1-min dumps — unknown
  provenance/license → LOW trust → quarantine only per the data-lake contract.

## 3. Likely availability constraints

- **Deep 1-min is the hard part.** GDFL retains only ~3 calendar months of 1–4-min
  history (5–12 min ~4.5 mo; 15–30 min/hourly ~6 mo; tick ~1 week); its EOD goes back
  to 2010 but intraday does not. Broker APIs cap 1-min at ~3 years. So 10–15 yr 1-min
  for the full universe is **not a standard retail product**.
- **Survivorship:** retail intraday sets often omit delisted symbols → biased backtests.
- **Corporate actions:** adjustment quality varies; may need self-adjustment.
- **F&O complexity:** contract rollover/expiry, continuous-future construction.
- **Operational:** broker-API rate limits make multi-year minute backfills slow;
  gaps / bad ticks / holiday handling.

## 4. Licensing risks

- **NSE owns the data; vendors are licensed redistributors.** A retail license is
  typically personal, non-redistribution — publishing or reselling raw data (and
  sometimes derived outputs) can breach terms. Record the exact license
  (`license`, `source`, `data_version`) per the data-lake contract.
- **Broker-API data:** ToS generally restrict to personal use; bulk-extracting years
  into an external data lake is a grey area and may violate terms.
- **Free GitHub/Kaggle mirrors:** provenance/license unknown → IP risk + LOW trust →
  quarantine, never authoritative.
- **Display vs non-display** real-time licensing differs from historical bulk; keep an
  audit trail of terms.

## 5. Cost-risk ranking (best value → worst)

| Option | Approx. retail cost* | Licensing risk | Depth / quality | Verdict |
|---|---|---|---|---|
| Daily Bhavcopy | ₹0 | Low | 10–15 yr, HIGH | **Best value — do now** |
| Broker API 1-min (Zerodha etc.) | ~₹0–6k/yr | Medium (personal ToS) | ~3 yr, decent | Good for limited intraday |
| TrueData / GDFL retail 1-min | ~low tens of thousands ₹/yr* | Low–Med | better breadth; GDFL intraday shallow | Step up only if needed |
| Deep 10–15 yr 1-min, full universe | **₹ lakhs / institutional** | High | rare; survivorship/adjust extra | **Defer** |
| NSE DotEx official historical | high / licensed | High | authoritative | Not retail-economical |
| Free GitHub/Kaggle 1-min | ₹0 | **High (IP + quality)** | unverified | **Avoid for authoritative use** |

*Exact figures change — confirm on each vendor's current pricing page before purchase.

## 6. Recommended staged path

1. **Daily first (now, ₹0):** Bhavcopy backbone + the daily-data protocol; prove the
   daily `momentum` edge after delivery costs. No intraday spend.
2. **Limited intraday second (only if warranted):** 1–3 yr of 1-min/5m/15m for a liquid
   universe (~NIFTY 100) from **one** affordable licensed source (broker API or
   TrueData), for the 2–3 most promising intraday strategies (ORB, VWAP-reversion;
   trend_15m/preclose on resampled bars). Test for edge cheaply.
3. **Deep intraday last (only if an intraday strategy proves edge on limited data and
   needs multi-regime depth):** buy **targeted** history (specific symbols/years), not
   full-universe-15-yr. Premium/NSE-official bulk is a deliberate, scoped expense —
   never the default.

## 7. Decision checklist before purchase

- [ ] A specific intraday strategy showed **edge on free/limited data** first.
- [ ] Exact scope defined: symbols, interval, **years**, segment (EQ/F&O).
- [ ] License **permits storage + research + publishing results**; no redistribution required.
- [ ] **Survivorship:** delisted names included.
- [ ] **Corporate-action** adjustment provided or computable (unadjusted + factors).
- [ ] **Sample validated** through the DQ gate before bulk buy; vendor is NSE-authorized.
- [ ] Delivery format + reproducibility (`data_version`, checksums); trust-tier = HIGH
      only if licensed + authorized.
- [ ] Cost weighed against expected research value; refund/exit terms; no auto-renew surprises.

---

## References

- TrueData — price plans: https://www.truedata.in/price · IEOD 1-min: https://newweb.truedata.in/products/ieod
- Global Datafeeds — authorized vendor: https://globaldatafeeds.in/global-datafeeds-nsebse-mcx-authorized-data-vendor/ · data types & intraday-history limits: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/introduction/type-of-data-available/
- Zerodha Kite Connect — historical API (3 yr / 60-day): https://kite.trade/docs/connect/v3/historical/ · free with base Connect sub: https://kite.trade/forum/discussion/14806/historical-data-is-now-free-with-base-kite-connect-subscription
- NSE — paid EOD/historical data subscription: https://www.nseindia.com/static/market-data/eod-historical-data-subscription
- Internal: `aws-data-lake-contract.md`, `daily-bhavcopy-ingestion-design.md`, `daily-data-backtest-protocol.md`
