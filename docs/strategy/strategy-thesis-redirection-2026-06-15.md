# QuantEmbrace — Strategy Thesis Redirection

**Date:** 2026-06-15
**Status:** Proposed — advisory. Live trading remains BLOCKED. No capital moves on this memo.
**Decision record:** ADR-034 (`memory/decisions.md`).
**Relates:** `docs/backtesting/aws-phaseB-intraday-backtest-report.md`,
`docs/backtesting/aws-phase15c-walk-forward-corrected-report.md`,
`docs/strategy/retail-quant-investment-committee-report-2026-06-10.md`,
[[strategy_pnl_root_causes_2026_06_10]], [[feedback_india_cost_mandate]].

---

## 1. Why we are reconsidering

The platform's working thesis has been **short-horizon technical patterns (breakout, VWAP
reversion, intraday trend, pre-close momentum) on liquid NIFTY-50 names, intraday / MIS.**
Three independent lines of evidence now agree this thesis has no edge after costs:

| Evidence | Result |
|---|---|
| Phase B backtest (3 yr real Kite 1m/5m/15m, 46 NIFTY50 names) | orb, vwap_reversion, trend_15m, preclose **all REJECT** (PF 0.05–0.45) |
| Paper sessions 16–17 (ADR-030 strategies) | S16 0 trades; S17 PF 0.47, net −₹238 |
| First-principles cost stack ([[strategy_pnl_root_causes_2026_06_10]]) | ~0.20% round-trip cost > 0.16–0.36% intraday edge |

When the backtest, the live paper sessions, and the cost arithmetic all point the same way,
it is a structural finding, not a tuning problem.

## 2. The actual lesson (not "pick a better indicator")

Edge per trade must exceed cost per trade. We have been trading the single hardest cell on
the board: **small expected moves** (the most efficient, most-arbitraged large-caps) at the
**highest cost frequency** (intraday round-trips). No indicator fixes a sign problem.

The one strategy that clears costs — **daily momentum** (Phase 15C: +₹90/trade OOS, PF 1.52) —
wins for *structural* reasons, not signal cleverness:
- **Horizon:** holding days→weeks means the move (₹100s–1000s) dwarfs the cost.
- **Product:** CNC delivery amortizes cost over the hold instead of paying it intraday.
- **Source:** cross-sectional momentum is a documented anomaly, not a chart pattern.

So the axes that matter are **(1) holding horizon** and **(2) edge source** — not which
intraday pattern. Every viable thesis below moves along one of those axes.

## 3. The redirected thesis

> Stop hunting intraday technical edge on liquid names. Reorient to **horizon-appropriate,
> edge-source-driven strategies where expected move ≫ cost**, and let the (now-real) backtest
> lab find edge *before* any capital is staged in front of the live gate.

**Beachhead — daily cross-sectional equity factors, positional / CNC.** Momentum already
clears costs at daily frequency; generalise it into a small factor book (momentum, short-term
reversal, low-volatility, and the India-specific **delivery-% conviction** factor), ranked
cross-sectionally, rebalanced weekly/monthly, held days→weeks. This is the *least speculative*
path because one factor is already half-proven, and **the data already exists**: the daily
Bhavcopy lake holds **2,983 NSE symbols, 2019–2025, survivorship-robust, with delivery_qty /
delivery_pct fully populated.**

**Adjacent, later:** event/structural signals on the same lake (delivery spikes, earnings
drift, index inclusion/exclusion, F&O OI). Genuine information content, multi-day horizon.

**alpha_engine = search harness, not a strategy.** It currently shadow-forecasts the three
dead intraday strategies; it cannot conjure edge from signals that have none. Its real value
is to *systematically score* many candidate signals per horizon and surface what clears costs.
Repurpose it to search, not to trade.

**Options = a different risk surface, NOT a safety upgrade.** Premium-selling carries
catastrophic tail risk; buying bleeds theta. Under *capital protection > trade count > profit*,
options are deferred and, if ever attempted, only as defined-risk, tiny-size experiments —
never the default.

## 4. The discipline point

The platform's real asset today is the **execution + risk + research harness**, not a
strategy. The capital-protection-consistent stance is: keep the harness parked in paper, point
the lab at factors/events, and only stand a strategy in front of the live gate once the
**backtest shows edge with margin** — so we stop discovering "no edge" one paper session at a
time. Nothing here is urgent; live is blocked and no capital is at risk.

## 5. Plan (gated, advisory)

1. **Factor study (this memo's first evidence):** cross-sectional long-only factor backtest on
   the daily lake — momentum / reversal / low-vol / delivery-% / combo vs an equal-weight
   benchmark, monthly rebalance, full statutory cost model on turnover, gross vs net. → §6.
2. If a factor clears costs with margin: **walk-forward** it (like Phase 15C) and, only then,
   paper-validate it (positional/CNC) against the live gate it can actually clear.
3. Repurpose alpha_engine into the candidate-signal scorer.
4. Shelve intraday equity; treat options as a separate, later, carefully-gated question.

## 6. Findings (factor study, 2026-06-15)

Full report: `docs/backtesting/factor-study-report.md`. Long-only top-20, monthly, top-200
liquid universe, full NSE delivery cost stack, 2020–2025.

**The pivot is supported — and it is night-and-day vs intraday.** Every factor clears costs
net (cost drag ~3–8% of CAGR); positional/CNC factor investing clears the delivery cost stack
with wide margin where all four intraday strategies were negative.

**But most of the raw return is beta** — the equal-weight liquid benchmark itself did 16–20%
CAGR in this bull regime; `lowvol` underperformed it. The honest signal is *risk-adjusted
outperformance*, and only one factor delivers it stably:

- **`delivery-%` (India-specific conviction) is the robust standout.** It beats the benchmark
  Sharpe in **both** sub-periods (1.88 vs 0.96 in 2020–22; 1.20 vs 1.09 in 2023–25) with the
  lowest drawdowns (−10.5%, −20.3%) and highest hit rate (72%). High delivery % = real
  accumulation, not churn — genuine information already in the daily lake.
- The full-period **combo (Sharpe 1.43) is flattered by 2020–22** (Sharpe 2.11) and only
  matched the benchmark in 2023–25 (1.07). Not a stable standalone edge.
- `momentum`/`reversal` beat on return but not clearly on Sharpe — mostly beta.

**Caveats:** one broad regime (no sustained bear in sample); equity-sized drawdowns (−10% to
−35%, real overnight risk); ffill/trade-at-close mild optimism; no walk-forward yet.

**Conclusion:** carry forward **`delivery-%` (and a delivery-tilted combo)** to the next
research gate — walk-forward + out-of-regime stress + a trend/regime overlay — then paper
validation (positional/CNC). The other factors and the kitchen-sink combo do **not** earn it.
Do not deploy capital on this study; it establishes direction, not readiness.

### 6b. Walk-forward + regime overlay (delivery-%, 2026-06-15)

Report: `docs/backtesting/delivery-walkforward-report.md`. `scripts/backtest/run_delivery_walkforward.py`.

- **Walk-forward = strong.** Delivery-% was **positive in 5/5 calendar years OOS** — 2021 +49%,
  **2022 +2.3%** (the hard year), 2023 +40%, 2024 +11%, 2025 +5%. A non-parametric factor that
  never has a down year across five independent years is genuine robustness (cannot be curve-fit).
  Return concentrates in the bull years; per-year drawdowns stay −2.7% to −15.2%.
- **The 200d regime overlay HURTS on this data** (−6.9 pts CAGR, Sharpe 1.41→1.13, for ~4 pts
  of drawdown relief; in 2022 it turned +2.3% into −8.9%). Trend filters whipsaw on the V-shaped
  dips that are in sample. **Do NOT adopt the overlay on this evidence.** Its real purpose —
  sustained-bear protection — is **untestable** (lake starts Oct 2019; no 2008/2011/2018-style
  bear). Better risk management = position sizing / portfolio drawdown limit, not a market overlay.
- **Key open caveat: the major-bear case is unvalidated** (data limit). Drawdowns are equity-
  sized (−20%). Next gate = **paper-validate delivery-% (no overlay) as a positional/CNC book**;
  acquire pre-2019 daily history if the bear question must be answered before scaling.

### 6c. Paper book stood up (2026-06-15, ADR-035)

Isolated advisory harness `scripts/paper/run_delivery_paper_book.py` (own JSON state, no broker,
no MIS, no live tables; monthly rebalance, top-200 liquid universe, long-only top-20, full
delivery costs). Inaugural basket as of 2025-12-31 = 20 high-delivery defensive-quality large-caps.

- **ETF/fund contamination found + fixed during standup:** the first basket held `LIQUIDBEES`/
  `LIQUIDCASE` (cash funds), `GOLDBEES`, `NIFTYBEES` — NSE ETFs trade in the EQ segment with
  ~100% delivery %, so the factor ranked them top (cash/index, not stock conviction). Added
  `_drop_funds` to the shared harness. **The delivery edge survives the exclusion unchanged
  (Sharpe 1.40 either way)** — genuine equity selection, not an ETF artifact.
- This harness is a **separate track** from the intraday 5-session gate (a monthly strategy can't
  be validated in 5 sessions). Real paper-broker/CNC integration is deferred + approval-gated.
  Data staleness: lake ends 2025-12-31; refresh Bhavcopy to run forward.

### 6d. First forward MTM + rebalance (2026-06-15)

Lake refreshed: 117 trading days downloaded and normalized (2026-01-01 → 2026-06-12,
2,665 EQ symbols). Paper book MTM'd and rebalanced as of 2026-06-12.

**Corporate action dislocation found:** KOTAKBANK demerger ex-date 2026-01-14 caused a
single-day −80.3% price drop (₹2,105→₹421). The paper book's inception basket bought KOTAKBANK
at ₹2,201 (Dec 31); the simulation shows −81.7% on that position because the harness has no
corporate-action adjustment (it cannot credit the spun-off entity shares). **This is a known
harness limitation** documented in ADR-035 as a pre-requisite for real CNC integration.

**Fix applied — `_drop_corp_action_dislocations` filter** (`scripts/backtest/run_factor_study.py`):
Excludes any symbol where any 5-day window in the past 126 trading days had a ≥35% price drop.
Catches demerger/reverse-split ex-dates. Lookback = 126 days (6 months) in the paper book;
63 days in the historical backtest engine (where such events are rarer and a tighter window
avoids excluding stocks recovering from macro crashes). KOTAKBANK is now excluded from the
Jun-12 basket; SBILIFE replaces it.

**Paper book state as of 2026-06-12 (post filter-aware rebalance):**

| Metric | Value |
|---|---|
| NAV | ₹9,21,077 |
| Positions (20 holdings) | ₹8,82,782 |
| Cash | ₹38,295 |
| Since inception (2025-12-31) | **−7.89%** |
| Rebalance cost | ₹144 |

The −7.89% since inception is dominated by the KOTAKBANK demerger loss (simulated −₹35,000,
not real — in a real CNC holding the spun-off shares partially offset this). Excluding that
artifact, the portfolio has been broadly flat-to-negative in a weak market period (NSE broadly
declined ~5–10% Jan–Jun 2026). The delivery-% factor thesis is not invalidated by this period;
the walk-forward study already showed 2022 (+2.3% OOS) as the hard year and 2025 (+5%) as a
slow year. **One 5.5-month sample in a flat/down market is not a verdict.**

**Jun-12 basket (20 holdings, no KOTAKBANK):**
TATACONSUM · MARICO · ITC · POWERGRID · HDFCLIFE · NTPC · AXISBANK · UPL · UNITDSPR ·
PIDILITIND · ULTRACEMCO · GODREJCP · SBILIFE · NESTLEIND · BHARTIARTL · SUNPHARMA ·
HINDUNILVR · TVSMOTOR · ICICIAMC · BRITANNIA

**Next gate:** refresh lake monthly (around each month-end) and run
`python scripts/paper/run_delivery_paper_book.py --rebalance` to track the paper book forward.
The first meaningful OOS read will come after 3–6 monthly rebalances.

### 6e. H5 PEAD study: price-implied proxy REJECTED (2026-06-15)

Built `scripts/backtest/run_pead_study.py` reusing the H4 event harness (EventStudy, event_study,
per_trade_net, calendar_portfolio). H5b mode: price-implied positive-surprise proxy (|return| > 3σ
AND volume > 2× median) on the top-200 liquid universe, 2020–2025. Entry at close[t+1] (no
lookahead). 1,907 positive-surprise and 1,013 negative-surprise events. Report: `docs/backtesting/pead-study-report.md`.

**Same anti-predictive pattern as H4.** Nominal forward returns are positive (beta) but abnormal
returns vs equal-weight market are **significantly negative at every horizon beyond T+10**:

| Horizon | Nominal | Abnormal | t |
|---------|---------|----------|---|
| T+15 | +1.32% | −0.45% | −2.01 |
| T+21 | +2.03% | −0.67% | −2.49 |
| T+42 | +3.81% | −1.20% | −3.02 |
| T+63 | +5.65% | −1.92% | −3.98 |

Calendar portfolio Sharpe 0.74 vs market Sharpe 1.07. **REJECTED.** NSE large-cap event
stocks underperform the market in the weeks following a discrete trigger event — whether that
trigger is a delivery spike (H4) or a large price move (H5b).

**Structural interpretation:** delivery information lives in the *persistent monthly level*
(the cross-sectional factor, which works) rather than in *discrete events* (which don't).
The event harness confirmed it works correctly (self-test: planted +6% surprise + 2bp/day drift →
T+21 mean_ret +3.44%, as expected). The market is the signal, not the event.

**H5a (real earnings calendar) status:** NSE corporate announcements API returns 404 in automated
sessions (bot-shield). BSE `ResultCalendar` API returns empty body. Both require a real browser
session or a paid earnings data provider. H5a is **not blocked on the code** — it's blocked on
data access. Given H5b's strongly negative result, a positive H5a would be a surprise requiring
its own economic rationale (different surprise proxy, different mechanism).

**Consequence:** No new event-driven different-driver candidates remain in scope on the current
lake. The active Tier-1 priority is **regime-expansion** — downloading pre-2019 Bhavcopy (2016–2018)
to stress-test delivery-% through the IL&FS/NBFC crisis and extend the bear-regime test coverage
the current lake (Oct 2019 start) cannot provide.

**Pre-2019 data limitation (discovered 2026-06-15):** NSE archives only provides delivery data
(`DELIV_QTY`, `DELIV_PER`) in the `sec_bhavdata_full` format which starts from 2019. The legacy
`cm{date}bhav.csv.zip` format (pre-2019) has full OHLCV + volume but **no delivery columns**.
Therefore:
- Pre-2019 lake extension (2016–2018): achievable with `download_bhavcopy.py` (now updated
  to handle both URL formats); yields OHLCV-only Parquet with `delivery_pct=NULL`.
- **Delivery-% factor can only be stress-tested on 2019–2025** using free NSE archives.
- Pre-2019 data is useful for non-delivery factors (momentum, vol, reversal) and for extending
  the lake for other factor hypotheses later.
- IL&FS crisis (Sept–Nov 2018) and 2016 demonetization are NOT testable for delivery % with
  free data. Requires a paid data provider or NSE subscription for historical delivery data.
- The current 2019–2025 sample does include: COVID crash (Feb–Mar 2020, −40%), 2022 bear
  (Jan–Oct, +2.3% OOS for delivery %), which are the available bear-regime stress tests.

**`download_bhavcopy.py` updated:** now auto-dispatches to legacy ZIP URL for pre-2019 dates.
Run `python scripts/backtest/download_bhavcopy.py --start 2016-01-01 --end 2018-12-31` to
extend the lake with OHLCV history (delivery columns will be NULL).

### 6f. Regime-expansion factor study: 2016–2025 (2026-06-15)

Extended factor study on the full lake (2016–2025, 2,358 days × 3,271 symbols) after downloading
pre-2019 OHLCV. Script: `scripts/backtest/run_factor_study.py --start 2016-01-01 --end 2025-12-31`.
Report: `docs/backtesting/factor-study-extended-2016-2025-report.md`.

**Full-period results (net of costs, 10 years):**

| Factor | Net CAGR | Sharpe | MaxDD | Note |
|--------|---------|--------|-------|------|
| **momentum** | 13.2% | **0.63** | −38.0% | Beats market; full 10yr price data |
| **combo** | 12.2% | **0.72** | −29.4% | Beats market; delivery missing 2016–18 → runs 3-factor |
| **lowvol** | 8.6% | 0.59 | −31.0% | = market Sharpe; much lower drawdown |
| **delivery** | 5.5% | 0.38 | −42.9% | ⚠️ Misleading — see interpretation |
| **reversal** | 0.5% | 0.17 | −53.8% | Killed by 89% monthly turnover + cost drag |
| _benchmark (gross)_ | — | 0.59 | −49.3% | |

**Critical interpretation — delivery result is an artifact, not a finding:**

Pre-2019 `delivery_pct=NULL` → delivery factor holds zero positions (sits in cash) for 2016–2018.
The benchmark earned ~15% CAGR in this bull period. Three years of 0% cash return (vs 15%
benchmark) mechanically collapses the 10-year delivery Sharpe from 1.40 to 0.38. This is a
**data gap penalty, not a signal quality verdict**. The clean delivery alpha result remains the
2019–2025 study (Sharpe 1.40, 5/5 OOS years positive, from §6 and §6b).

**Genuine regime-expansion findings:**

1. **Momentum is pre-2019 bear-robust.** Sharpe 0.63 vs market 0.59 across the full 10 years
   including the 2016 demonetization (NIFTY −10% in 6 weeks) and 2018 IL&FS crisis (NIFTY −15%
   over 6 months). Momentum's MaxDD (−38%) is meaningfully better than the market's (−49.3%,
   dominated by the COVID crash). This is the key regime-expansion confirmation: momentum
   survives pre-2019 bear episodes with positive net alpha.

2. **Combo (3-factor during 2016–18) still beats market.** When delivery data is absent, the
   combo runs on momentum + reversal + lowvol. Even so it achieves Sharpe 0.72 vs 0.59 for the
   market — genuine multi-factor diversification value. With full delivery data (2019+) it was
   Sharpe 1.43 in the original study.

3. **LowVol is a risk reducer, not an alpha source.** Sharpe exactly matches the market (0.59)
   but MaxDD only −31% vs −49.3% for the market. Its value is capital protection in bear
   regimes, not return generation. Relevant for position sizing / portfolio construction, not as
   a standalone strategy.

4. **Reversal is unviable at monthly frequency.** 89% monthly turnover → 18.6% cost drag →
   0.5% net CAGR over 10 years. Not carrying forward.

**Updated regime picture for delivery-%:**

The one bear regime directly testable for delivery is the **COVID crash (Feb–Mar 2020, −40%)**
— which IS in the 2019–2025 delivery window, and delivery still achieved +5% OOS return in
2020 (walk-forward). The IL&FS crisis (2018) remains untestable for delivery with free data.
The momentum evidence above suggests factors with pre-crisis momentum rankings tend to be
defensive (lower MaxDD than market), which is consistent with the delivery factor's structure
(high-conviction accumulation tends to be quality/defensive names).

**Conclusion:** regime expansion confirms momentum as the second verified factor with pre-2019
bear evidence. Delivery-% bear-regime validation for IL&FS (2018) remains open (paid data).
The 2016–2025 combo Sharpe (0.72) supports a momentum-first, delivery-second blended approach
when delivery data is available; momentum-only as a fallback when it is not.

## 7. Governance

Advisory only. Backtesting can **recommend**, never **promote**. A human approves all
production changes. Live trading remains **BLOCKED**; the 5-session paper gate is unaffected.
No live/paper table is touched by this work.
