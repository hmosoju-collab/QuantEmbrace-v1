# NSE Options & Futures — Hedge-Level Strategy Design & Plan

**Created:** 2026-06-20 · **Status:** design/plan (no build yet) · **Live trading: BLOCKED**
Advisory research program. Backtesting can recommend; it cannot promote. A human approves all production
changes. Every strategy here must clear a **pre-registered** gate FORWARD before any capital is reconsidered.

---

## 1. Mandate & hard constraints (these kill most "hedge-fund" ideas immediately)

- **Account ≈ ₹5L.** Leverage on futures/short-options = **ruin risk**, not just drawdown. Capital
  protection > trade count > profit (CLAUDE.md). One NIFTY futures lot ≈ ₹19L notional on ~₹2L margin —
  a single 10% gap can exceed the account. **Position sizing and defined risk are existential, not optional.**
- **Costs are large vs capital.** Futures: low % cost but lot-notional is huge. Options: flat ₹20/leg
  dominates at small size (proven in O-2). Every design assumes the full NSE statutory stack.
- **No overfitting.** ~3 yr of data is hypothesis-screening, not proof. Pre-register gates; report trade
  counts + confidence; walk-forward; crash-stress (Mar-2020, budget/election/RBI gap days).
- **What we've already proven (do not re-litigate):** intraday *cash equity* is a negative-drift desert
  (C6); static index short-vol (condors) fails structurally net of skew+costs (O-2 sweep, 0/10). The
  ATM VRP is real but not harvestable by a naive static structure. **These rule out two whole families.**
- **What carries forward (evidence-grounded):** the NSE equity premium accrues **overnight** (C6: cash
  overnight Sharpe 3.2) — futures are the capital-efficient way to *test* capturing it. Momentum has a
  pre-cost tilt. Event IV is elevated before scheduled events. These are the seeds below.

## 2. Data reality map (this gates everything — be honest before designing)

| Instrument / resolution | Source | Cost | Status |
|---|---|---|---|
| Index/stock **futures, EOD** (price, OI, basis) | NSE F&O bhavcopy | free | extend existing downloader (FUTIDX/FUTSTK) — small |
| Index **futures, intraday** 1m (~3 yr) | Kite continuous futures | free | `fetch_zerodha_intraday`-style; testable now |
| Index **options, EOD chains** (3 yr) | NSE F&O bhavcopy | free | **already downloaded** (NIFTY, 760 days) |
| Index **options, intraday** chains | GDFL / TrueData / Algotest | **paid** | data-blocked; or collect forward via Kite |
| India VIX, NIFTY spot (daily) | Kite / Yahoo | free | **already in lake** |

**Consequence:** *futures* strategies (intraday + positional) and *EOD/event-based options* strategies are
testable **now, free**. *Intraday options* strategies (gamma scalping, 0DTE) are **data-blocked** — defer
until a paid/forward-collected dataset justifies itself.

## 3. Strategy taxonomy — by return source (why it makes money), with honest priors

### TIER 1 — evidence-grounded, testable free, account-survivable (design these first)

**F1 · Overnight index-futures premium.** *Thesis:* C6 proved the equity premium is overnight; capture it
via NIFTY/BANKNIFTY futures (buy near close, exit near open) with capital efficiency. *Data:* Kite futures
1m + bhavcopy basis — free. *Risk:* leveraged overnight gap = the tail; **defined via small size + optional
protective put (turns it into a risk-defined overnight carry)**. *Prior:* cautiously positive — it's our
single most evidence-grounded idea — BUT C6 also showed cash-overnight isn't retail-harvestable via daily
round-trips; futures change the cost/carry structure, so it must be **re-tested on futures with real roll +
financing + the gap tail**, not assumed.

**O1 · Scheduled-event IV-crush (defined-risk short vol, event-timed).** *Thesis:* IV is systematically
elevated before known events (RBI policy, Union Budget, Fed, monthly expiry, big-cap earnings) and crushes
after; sell **defined-risk** structures before, close after — distinct from always-on condors. *Data:*
partially testable on the EOD options lake (ATM-IV / term-structure change across event vs non-event dates),
full version needs intraday. *Prior:* moderate — event VRP is real and concentrated, but skew + the
occasional event that moves big are the risks; defined-risk only.

**F2 · Positional index-futures trend/momentum.** *Thesis:* the momentum tilt we found, expressed on
NIFTY/BANKNIFTY futures with leverage + low cost and a hard stop. *Data:* Kite + bhavcopy — free. *Prior:*
mixed — momentum was "mostly beta" in the factor work; a futures-specific, risk-managed trend is a distinct
test, but expect modest results.

### TIER 2 — testable but thinner or harder

**F3 · Futures calendar / cash-futures basis & roll.** Low-risk relative value (capture premium/discount +
roll). *Prior:* thin edge, capital-heavy for ₹5L; likely fails the cost gate — test cheaply, expect KILL.
**O2 · Volatility term-structure / calendar spreads.** Sell front-month vol, buy back. Partially EOD-testable.
*Prior:* low — related to the VRP that already failed at the wings.

### TIER 3 — data-blocked or not account-appropriate (defer / decline)

**O3 · Intraday options** (gamma scalping, intraday short vol, 0DTE-style) — needs PAID intraday chains;
defer. **X · Dispersion, index arbitrage** — institutional, capital-heavy; decline for ₹5L.

### HEDGING / RISK OVERLAY (the literal "hedge" layer — design regardless of which alpha we pick)

**H1 · Protective-put / collar overlay** on any deployed positional book (incl. the forward factor books):
caps tail at a known cost. **H2 · Tail hedge** (cheap far-OTM puts) for crash protection. These are *risk
management*, not alpha — they drag return and only matter once capital is deployed, but a hedge-level system
must specify them up front.

## 4. Ranked roadmap (robustness × testability × account-fit)

1. **F1 overnight futures** — build EOD-futures + Kite-futures-intraday loaders, test the overnight capture
   net of roll/financing/gap, defined-risk sized. (Most evidence-grounded; free data.)
   - **SCREEN DONE 2026-06-20 → PASS w/ caveats** (`run_overnight_futures_study.py`, NIFTY50 spot proxy
     2020-25): overnight +11.3 bps/night SURVIVES futures cost (~0.023% vs cash 0.22%), net Sharpe 1.98,
     positive all 6 years — but NAKED form has maxDD −41% (COVID) and an index-scaled gap tail (same −9%
     COVID gap at today's ~26000 = −35% NAV in one night; gate G3 passed on a low-index historical accident
     + lacked a max-DD limit). ⇒ naked leveraged carry is account-inappropriate. **Deployable = F1-hedged
     (long future + protective OTM put).** NEXT: F1-hedged test (data in hand) + F1-full real futures +
     DD-aware gate. Report: `docs/backtesting/overnight-futures-study-report.md`.
   - **F1-FULL (real futures) 2026-06-20 → FAIL; F1 DEAD.** Real near-month NIFTY futures (733 nights,
     basis+roll in prices): overnight only **+3.2 bps/night, Sharpe 0.34, ann +6.1% → FAIL G4**; 2025
     negative. Decomposition (same 2022-25 window): spot +8.9 bps → futures +3.2 bps = **−5.7 bps lost to
     basis decay + the non-tradable index "open"** — C6's overnight is a real index property but ~2/3 is NOT
     harvestable on the tradable instrument. **F1-hedged also dead** (binding failure is RETURN not tail; a
     put adds cost). Lesson: the spot/index proxy overstated the harvestable edge ~3.5× — always validate on
     the real tradable instrument.
2. **O1 event IV-crush** — measure IV behaviour around event dates on the EOD options lake first (cheap
   screen, like O-1 VRP); only build the trade if the crush is real & large net of costs.
   - **SCREEN 2026-06-20 → SHELVE** (`run_event_vol_study.py`, 28 events). Edge over random-day baseline
     **1.03× = nothing.** Where the crush is real (Budget +1.9vp, Election +2.1vp) the realised MOVE is also
     real and short vol LOSES (Budget net −₹9k, Election −₹27k); where short vol wins (RBI) there's no crush
     (+0.3vp) — just ordinary VRP on calm days. Pre-event IV is fairly-priced risk compensation, not excess.
3. **F2 futures trend** — positional momentum on index futures with hard stops.
   - **SCREEN 2026-06-20 → SHELVE** (`run_futures_trend_study.py`, pre-declared 8-cell grid). Drawdown
     reduction, NOT alpha: long-only trend ann ~12% = buy-hold (no return added), Sharpe 0.54-0.59 vs B&H
     0.42; cuts the −72% leveraged buy-hold DD to ~−25% but hinges on dodging ONE crash (2020, n≈1 trend);
     isolated/threshold-marginal pass. Risk-overlay property, not standalone alpha for ₹5L.

> **PROGRAM OUTCOME (2026-06-20): all Tier-1 eliminated — F1 dead, O1 shelved, F2 shelved.** Tier-2 low
> prior, Tier-3 data-blocked. NSE large-cap is efficient at the horizons/instruments a ₹5L account can
> reach. Return to the standing posture (forward factor books accrue vs the pre-registered gate; deploy
> nothing). Tooling retained + reusable. Live trading remains BLOCKED.

4. **F3 basis / O2 calendars** — cheap KILL-or-keep screens.
5. **H1/H2 overlays** — design alongside whatever reaches a forward book.
6. **O3 intraday options** — only if a paid/forward dataset is justified by a Tier-1 success.

## 5. Validation discipline (non-negotiable, same as every prior track)

- **Pre-register** each strategy's hypothesis + gate BEFORE seeing results; never relax to force a pass.
- Full NSE cost stack; **futures: model margin, roll, financing, the gap tail**; options: flat-fee + STT/skew/slippage.
- Walk-forward / per-year / per-regime; explicit **crash-day stress**; report **trade counts + confidence**.
- **Defined risk always** (sizing caps, protective legs); a single cycle must never threaten the account.
- A backtest PASS ⇒ a **forward paper book** (like the factor books) → only after a forward gate clears does
  a human review a **small gated pilot**. **Never auto-deploy. Live BLOCKED.**

## 6. Reconciliation with prior findings
Cash intraday (dead), static index short-vol (dead), positional equity factors (forward-testing, deploy
nothing). This plan does NOT revive those — it tests *different instruments and return sources* (futures
leverage/overnight, event-timed vol, futures trend) where the prior failures don't directly apply, while
inheriting their hard-won discipline. The default expectation remains skeptical: most of these will also be
shelved. That's the process working.

---
*Next: operator picks the first 1–2 strategies to develop into detailed designs + pre-registered backtests.
No code until a choice is made. Live trading remains BLOCKED throughout.*
