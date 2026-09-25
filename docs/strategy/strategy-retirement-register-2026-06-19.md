# Strategy Retirement Register — Intraday Equity Set

**Date:** 2026-06-19
**Status:** RETIRED (advisory record). Live trading remains BLOCKED throughout.
**Evidence:** `docs/backtesting/phase1-strategy-audit-report.md`, `backtest-data/phase1_audit.json`,
`scripts/backtest/phase1_strategy_audit.py`, `docs/backtesting/aws-phaseB-intraday-backtest-report.md`.
**Relates:** ADR-033 (intraday REJECT), ADR-034 (thesis pivot).

---

## Cost-model correction (applied to this audit)

The Phase B backtest charged `IndianCostModel.delivery()` (**0.222% round-trip**) on what are
**MIS intraday** strategies. The correct cost is `IndianCostModel.intraday()` (**0.035% round-trip**,
~6× cheaper; ~0.14% incl. 5 bps/leg slippage, where slippage now dominates). This audit re-runs at
the **correct MIS cost**. The correction roughly halved per-trade losses but **flipped none to
positive** — the retirement is robust to the cost model. (Phase B's delivery-cost figures remain the
conservative bound.)

## Audit summary (NIFTY50, Zerodha Kite 1m/5m/15m, 2022–2024, fixed ₹10L, MIS costs)

| Strategy | Trades | Win% | PF | Exp ₹ | Net ₹ | Sharpe(d) | MaxDD | Loses every year? | Loses both regimes? | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|:--:|:--:|---|
| `orb` | 2,403 | 36.8 | 0.70 | −41 | −98,843 | −3.65 | −10.2% | ✅ (0.77/0.73/0.59) | ✅ (up 0.68 / dn 0.73) | **RETIRE** |
| `vwap_reversion` | 131 | 29.0 | 0.50 | −83 | −10,864 | −4.03 | −1.2% | ✅ (0.47/0.23/0.84) | ✅ (0.50 / 0.49) | **RETIRE** |
| `intraday_trend_15m` | 15 | 26.7 | 0.36 | −73 | −1,089 | −6.18 | −0.1% | ✅ (0.84/0/0) | ✅ (0.14 / 0.71) | **RETIRE** |
| `preclose_momentum` | 10,596 | 21.9 | 0.25 | −37 | −394,959 | −19.77 | −39.5% | ✅ (0.29/0.24/0.22) | ✅ (0.24 / 0.26) | **RETIRE** |

Gate to survive: expectancy > 0 · PF > 1.2 · net > 0. **None passes any gate in any cell.**

---

## Memo 1 — `orb` (opening-range breakout, 1m) → RETIRE

**Cause of death: no gross edge after costs (cost-bleed), with a hint of decay.** PF < 1 in all
three years *and* both market regimes — the failure is universal, not regime-specific. Even at the
correct MIS cost (−₹41/trade), the 09:15–09:30 breakout does not follow through enough on liquid
NIFTY50 names to clear ~0.14% round-trip. PF erodes by year (2022 0.77 → 2024 0.59), consistent with
an already-absent edge decaying further as opening-range breakout became more algo-saturated.

**Evidence:** 2,403 trades, PF 0.696, exp −₹41, net −₹98,843, Sharpe(d) −3.65, max DD −10.2% across
660 underwater days. The live "ORB blind" excuse (Session 16 started 10:19, missing the range) is
*disproved here* — the backtest has the full opening range and still fails.

**Salvageable lesson:** opening-range breakout on the most efficient large-caps is a saturated,
no-edge signal at retail latency. If revisited at all, only **event-conditioned** (large overnight
gap, where the move is big enough to clear cost — Phase 2 C1).

**Verdict: RETIRE (2026-06-19)** — no gross edge in any year or regime, even at corrected costs.

## Memo 2 — `vwap_reversion` (VWAP mean-reversion, 1m) → RETIRE

**Cause of death: structural — geometrically capped reward:risk + cost-bleed.** Reversion to VWAP on
liquid names has a profit target bounded by the entry's distance from VWAP → R:R pinned near ~1:1
(Session 16: median R:R 1.05, 0/184 passed the gate), which cannot survive any cost. PF < 1 in every
year and both regimes. It barely fires (131 trades in 3 yr) because the quality gate correctly
starves it — and the trades it does take lose (exp −₹83).

**Evidence:** 131 trades, PF 0.498, exp −₹83, net −₹10,864. By year 0.47 / 0.23 / 0.84 — noisy,
small-n, never > 1.

**Salvageable lesson:** a reversion target capped at the dislocation size is structurally
unprofitable after costs. Don't rebuild VWAP reversion on liquid equity; mean-reversion needs much
larger dislocations (wider bands, less efficient instruments) to pay.

**Verdict: RETIRE (2026-06-19)** — geometrically capped, no edge; the low trade count confirms the
gate already strangled it.

## Memo 3 — `intraday_trend_15m` (15m intraday trend) → RETIRE

**Cause of death: internally contradictory filters → near-zero trades, and no edge in the few it
takes.** Even with cross-day warm-start (EMAs fully warmed, like the live ADR-031 path) it fires
only 15 trades in 3 years at production config, because ADX ≥ 25 ∧ confidence ≥ 0.65 are mutually
exclusive on NIFTY50 15m data. Those 15 lose (PF 0.36). With both filters removed (prior run) it
trades ~4,857 times and still loses (PF 0.30). Dead both ways.

**Evidence:** 15 trades, PF 0.36, exp −₹73, net −₹1,089. The "0 trades at production config" is a
*real result, not a warm-up artifact* (warm-start confirmed).

**Salvageable lesson:** a 15m trend filter cannot warm up within a session and finds no follow-through
on large-caps when it can; its production thresholds contradict each other. Trend works at the
**daily** horizon (the verified momentum factor) — not intraday on large-caps.

**Verdict: RETIRE (2026-06-19)** — no trades at production config; loses when forced to trade.

## Memo 4 — `preclose_momentum` (pre-close momentum, 5m) → RETIRE (worst of the set)

**Cause of death: high-frequency negative-edge — frequency × cost = catastrophic.** PF 0.246 (lowest),
14.5 trades/day, Sharpe(d) −19.77, max DD −39.5% (essentially the entire 731-day period underwater).
PF stable ~0.22–0.29 across every year and both regimes. The pre-close window carries no persistent
directional signal on liquid names; the strategy pays spread+cost ~14× a day for noise.

**Evidence:** 10,596 trades, exp −₹37, net −₹394,959, max DD −39.5%. By year 0.285 / 0.236 / 0.215.

**Salvageable lesson:** trade frequency is a **cost multiplier, not an edge multiplier**. Any future
intraday work must be low-frequency and large-move (few trades, each clearing cost with margin).

**Verdict: RETIRE (2026-06-19)** — worst of the set; high-frequency cost-bleed, negative in every cell.

---

## Other current strategies (disposition)

- **`scalp_1m` — PARKED, do NOT activate.** Stage-1 disabled, never validated. No backtest evidence
  to retire *on*, but it is the highest-frequency design of all and the preclose result is a direct
  warning. Leave disabled; do not activate without a full cost-correct backtest first.
- **`momentum` (daily) — KEEP.** Not an intraday strategy and not in scope to retire. It is the one
  verified positive-edge factor (bear-robust through 2016 demonetization + 2018 IL&FS; Phase 15C
  PAPER_OPTIMIZATION). Retained.

## Cross-cutting conclusion (cause of death, one line)

All four intraday strategies show **PF < 1 in every year and every regime, even at correct MIS
costs.** Not regime-dependence (they fail up *and* down), not one bad year (they fail all three),
not leakage (the test is if anything conservative). The uniform cause is **no persistent gross edge
on liquid NIFTY50 at intraday horizons — realistic costs (now mostly slippage) finish them off.**
This is the structural disproof that retired the intraday thesis and motivated the pivot to
positional/factor work (ADR-034).

> Backtesting can recommend. It cannot promote. A human approves all production changes.
> Live trading remains BLOCKED.
