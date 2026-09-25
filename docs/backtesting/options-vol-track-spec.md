# Options / Volatility Track — Spec & Pre-Registered Gate

**Pre-registered:** 2026-06-19 · **Status:** O-1 tooling built (offline-tested); awaiting operator data pull.
**Governance:** Advisory only. Backtesting can recommend; it cannot promote. A human approves all
production changes. **Live trading remains BLOCKED.** A PASS at any phase only unlocks the *next research
step*, never deployment.

---

## Why this track exists

Every cash-equity avenue we tested dissolved (intraday retired — negative-drift desert per C6; gap,
calendar, factor, combo all shelved — see `strategy-retirement-register-2026-06-19.md`,
`combined-overnight` findings, `forward-factor-validation-gate.md`). The one structurally *different*
return source we have not yet tested on NSE is the **volatility risk premium (VRP)**: index implied vol
(INDIA VIX) tends to exceed subsequently-realised vol, so disciplined, **defined-risk** vol sellers can be
paid. Options have non-linear payoffs and a genuine risk premium — unlike cash intraday, which we proved
is negative-sum after costs.

## The data reality that shapes the plan

Kite `historical_data` needs an `instrument_token`, and **expired weekly-option tokens are purged** from
the instrument master. So 3–5 yr of option *chains* is **not** cheaply available from Kite — that needs a
paid vendor. But the *premium itself* is measurable from **free** underlying inputs (INDIA VIX + NIFTY
spot). Hence a strict **cheapest-first, two-phase** plan: screen for free, pay only if the screen clears.

---

## Phase O-1 — FREE screen of the underlying premium  ✅ tooling built

**Inputs (free):** INDIA VIX (implied vol) + NIFTY 50 spot (→ realised vol), daily, ~5 yr.

**Tooling (offline self-tested, runs locally with a same-day Kite token):**
```bash
# 1. fetch the free underlying inputs (≈4 API calls, ~1.3s)
python scripts/backtest/fetch_zerodha_indices.py --indices niftyvix --intervals 1d \
    --start 2020-01-01 --end 2025-12-31
#    …or drop free NSE history CSVs and use --nifty-csv / --vix-csv on the screener.

# 2. run the pre-registered screen
python scripts/backtest/run_vol_premium_study.py
#    → writes docs/backtesting/vol-premium-study-report.md
```
Operator prerequisite (same as `download_bhavcopy.py`): the fetch needs a live same-day Kite session
(`python scripts/zerodha_login.py`); the sandbox has no egress to api.kite.trade. Confirm the Kite Connect
**historical data API** add-on is active (separate from the live-trading key).

**What it measures:** `VRP = INDIA VIX − NIFTY realised vol over the next 21 trading days` (annualised vol
points). Forward-realised is an *ex-post measurement* of whether sellers got paid — not a tradable signal,
so it is **not lookahead**. Plus a coarse monthly short-straddle-vega ₹ proxy vs an estimated defined-risk
iron-condor cost stack, and a mandatory **tail** report (short vol's whole danger).

### Pre-registered O-1 gate (fixed 2026-06-19 — must NOT be relaxed to force a PASS)
| # | Criterion | Threshold |
|---|---|---|
| G1 | Mean VRP | > **1.0** vol point (implied beats realised by a real margin) |
| G2a | Win frequency | VIX > realised on ≥ **65%** of days |
| G2b | Consistency | positive-mean VRP in ≥ **70%** of calendar years |
| G3 | Economics | median **gross** cycle edge ≥ **2×** median round-trip cost **and** median net > 0 |
| — | Tail (caution, not pass/fail) | worst-cycle loss & cumulative-DD reported; a fat left tail is a hard caution into O-2 sizing |

**PASS ⇒ authorises *buying* option-chain data for O-2 only. It does NOT authorise deployment.**
**SHELVE ⇒ do not pay for chains; the premium isn't there after the cost stack.**

> Integrity rule: same as the Forward Factor Gate — fix the bar before the data arrives. Moving the
> goalposts after seeing the numbers invalidates the screen.

---

## Phase O-2 — PAID option-chain backtest  *(only if O-1 PASSES)*

- **Buy** 3–5 yr NIFTY weekly+monthly option-chain 1-min history — **Algotest export / GDFL / TrueData**
  (Algotest also ships a hosted options backtester that can short-circuit build work).
- **Build:** `IndianCostModel.options()` — Zerodha **₹20 flat/leg** (dominant on small size), **STT 0.1%
  on sell-side premium**, exchange txn ~0.035% premium, stamp 0.003% buy, SEBI ₹10/cr, GST 18%.
- **Backtest defined-risk only** (credit spreads / iron condors — *never naked*): real premiums, bid/ask
  slippage, walk-forward, per-regime, and explicit **tail-day stress** (Mar-2020, budget/election/expiry
  gaps). On a ₹5L account this is 1–2 NIFTY lots — flat per-leg cost is the likeliest edge-killer.
- A PASS here ⇒ **human review for a small gated pilot**, never auto-deploy.

**O-2 HARNESS BUILT (2026-06-19, zero spend) — `scripts/backtest/run_options_vol_backtest.py`:**
- `OptionsCostModel` (₹20/leg flat + STT 0.1% sell + txn 0.035% + GST + stamp; flat fee dominates retail
  size) · Black–Scholes pricer (erf-based, no scipy) · iron-condor build + bounded expiry payoff ·
  per-cycle risk budget ≤2% NAV (hard tail cap) · pre-registered O-2 gate (expectancy>0 · PF>1.3 ·
  ≥60% pos-years · maxDD≤20% · defined-risk cap held).
- **Self-test PASS** (BS parity exact; condor loss bounded at ±wing; +VRP→PF 1.89; crash injected→loss
  capped; zero-VRP→costs turn it negative). **Synthetic-on-real-NIFTY-path** (incl. Mar-2020): 32 cycles,
  PF 1.65, maxDD −5.7%, worst cycle −₹17.6k inside the ₹20k budget — defined-risk cap held through COVID.
- ⚠️ The synthetic PASS is **by construction** (IV set = realised + 3 vp). It validates the ENGINE + cost
  stack + tail cap, NOT a real edge. Real-chain mode is a documented schema contract, not yet wired to
  data — needs the O-2 vendor purchase. `docs/backtesting/options-vol-backtest-report.md`.

**O-2 DATA SOURCE = free NSE F&O Bhavcopy, NOT Zerodha (2026-06-19, operator-approved).** Kite can't
backfill expired option chains (purged instrument tokens). The NSE F&O (derivatives) bhavcopy carries all
NIFTY strikes/expiries 3+ yr, free, same archive family as the equity bhavcopy; EOD suffices for the
held-to-expiry monthly condor. BUILT + self-tested (zero spend): `scripts/backtest/download_fo_bhavcopy.py`
(both legacy + UDiFF NSE formats → `lake/options/underlying=NIFTY/`) and `run_options_vol_backtest.py
--chain` (`backtest_real`: real strikes/premiums/skew, NIFTY50-spot settle, 2.5% slippage haircut; condor
snaps to nearest available strike). Both self-tests PASS. **NEXT (operator runs LOCALLY — no NSE egress in
sandbox):** `download_fo_bhavcopy.py --start 2022-06-01 --end 2025-06-30` (804 td) → `run_options_vol_backtest.py
--chain backtest-data/lake/options` = first REAL O-2 verdict. EOD PASS ⇒ intraday-vendor re-test before any
pilot; never auto-deploy; live BLOCKED.

**O-2 FIRST REAL VERDICT (2026-06-20) → FAIL.** Lake: 760 td, 2022-06→2025-06, 1.24M NIFTY option rows.
31 monthly condor cycles (after fixing long-dated-expiry leakage + sub-1-lot selection bias): net −₹54k,
ann −2.1%, PF 0.67, expectancy −₹1,740, pos-years 50% (maxDD −8.7% & cap held; edge criteria FAIL).
**Decomposition = EDGE problem, not cost:** loses gross −₹35k even at ZERO slippage; win 65% but
credit/max-loss 0.25 ⇒ needs ~81% win to break even; losses cluster in directional 2022–23. The positive
ATM VRP from O-1 does NOT convert to a profitable OTM iron condor (put skew + directional risk eat the
wing premium). Does NOT advance to a pilot. Do not parameter-fish a 31-cycle sample. Report:
`docs/backtesting/options-vol-backtest-report.md`.

**TRACK CLOSED → SHELVE (2026-06-20).** Pre-declared structure sweep (`run_options_vol_sweep.py`, report
`options-vol-sweep-report.md`): OTM {1-5}% × wing {1,2}% = 10 monthly condor configs. **0/10 clear the O-2
gate; only 2/10 gross-positive even at zero slippage (≈<0.5%/yr, negative after costs).** Closer-to-ATM
catastrophic (directional blow-through), far-OTM collects ~nothing — no sweet spot. The FAIL is STRUCTURAL:
the NSE index VRP is real ATM/frictionless (O-1) but NOT harvestable by any retail-affordable static
defined-risk structure after put skew + directional risk + costs. Only untested lever = intraday active
management (needs paid data; not justified when every static structure loses gross). **Return to standing
posture: forward factor books accrue, deploy nothing. Live BLOCKED.** Tooling retained + reusable.

---

## Status log
- **2026-06-19** — Track opened (operator chose options/vol). O-1 tooling built & offline-tested:
  `fetch_zerodha_indices.py` (self-test PASS), `run_vol_premium_study.py` (self-test PASS both
  directions: premium-present → PASS, no-premium → SHELVE).
- **2026-06-19 — O-1 RUN → PASS** (on FREE LOW-trust data: Yahoo `^NSEI` / `^INDIAVIX`, 2020–2025,
  1,449 days, 69 monthly cycles; Kite request_token expired before exchange so the lake wasn't populated
  this run). Result: **mean VRP +2.34 vp · median +3.06 · VIX>realised 79% of days · positive-mean VRP
  in 100% of years** (2020 +1.66 … 2025 +2.56). By regime: bear +4.08 / bull +3.28 / chop +1.98 (premium
  positive in every *classified* regime); the SMA warm-up window (early-COVID Mar-2020 spike) is −23.34 —
  the acute vol-spike onset is the killer, not "bear" per se. ₹ proxy (straddle-vega): median gross
  ₹9,357 vs cost ₹329 → net ₹8,998/lot/cycle. **All 4 gates PASS.** ⚠️ **Fat left tail**: worst cycle
  −₹109,627 (11.7× median gross), cum-DD −₹119,604 — the steamroller. *Fixed a date-parse bug in the CSV
  loader (`dayfirst=True` was NaT-ing ~60% of ISO dates → fake 100% realised vol → spurious SHELVE).*
  **Interpretation:** PASS authorises *buying chain data for O-2*, NOT deployment — the tail makes
  defined-risk structures + explicit crash stress mandatory in O-2. Report:
  `docs/backtesting/vol-premium-study-report.md`.
- **2026-06-19 — O-1 CONFIRMED on HIGH-trust Kite data.** `kite_fetch_with_token.py` (no-DynamoDB
  one-shot) populated the lake (`segment=INDICES`, NIFTY50 + INDIAVIX, 1,492 daily bars each, 2020–25).
  Screen on lake: mean VRP **+2.48 vp** · VIX>realised **80%** days · +VRP **100%** years · worst cycle
  −₹111,424 (10.6× median) · **all 4 gates PASS** — matches the Yahoo run within noise (cross-source
  agreement). PASS stands on broker-grade data. Next = operator O-2 vendor/spend decision.
