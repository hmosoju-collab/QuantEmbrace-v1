# Stage-1 Strategy Eligibility

**Last updated:** 2026-06-02  
**Stage:** STAGE_1_ONE_SHARE  
**Live trading:** NOT enabled. This document is a gate reference, not an approval.

> Any change to strategy eligibility requires:
> 1. Updated validation report in `docs/live-readiness/`
> 2. Updated ADR in `memory/decisions.md`
> 3. LiveGateChecker check 26 must pass (no PAPER_ONLY_STRATEGIES in live config)
> 4. Manual operator sign-off

---

## Eligibility Table

| Strategy | Stage-1 Live Eligible | Paper Allowed | Notes |
|---|---|---|---|
| `nse_vwap_reversion` (VWAP) | **ELIGIBLE CANDIDATE** | Yes | Requires spread/LTP staleness checks before promotion. Primary Stage-1 candidate per `stage1-config-pack.md`. |
| `nse_momentum` (momentum) | **ELIGIBLE CANDIDATE** | Yes | Trend-following; well-tested across sessions. Needs R:R gate before live. |
| `nse_orb` (ORB) | **ELIGIBLE CANDIDATE** | Yes | Opening range breakout; operates in low-activity window. Time-gated. |
| `nse_trend_15m` (trend_15m) | **ELIGIBLE CANDIDATE** | Yes | 15-minute trend; lower signal frequency, lower adverse selection. |
| `nse_preclose` (preclose) | **ELIGIBLE CANDIDATE** | Yes | Only with time-risk controls (no new entries after 14:45 IST). |
| `nse_scalp_1m` (scalp_1m) | **DISABLED_FOR_STAGE1_LIVE** | Yes | See below. |

---

## scalp_1m — DISABLED_FOR_STAGE1_LIVE

**Decision date:** 2026-06-02  
**Decision:** `APPROVED_FOR_PAPER_ONLY` / `DISABLED_FOR_STAGE1_LIVE`  
**Validation report:** `docs/live-readiness/scalp-1m-v2-validation-report.md`

### Reasons for disabling

1. **Insufficient session sample** — At production `min_confidence=0.55`, zero live-quality signals were generated in Session 9 (choppy session). The A/B comparison required lowering confidence to 0.50. Statistical validation requires 5+ sessions with ≥10 signals each in trending conditions.

2. **Monitoring gap** — Rejection counters (`rejected_net_edge`, `rejected_spread_wide`, `rejected_stale`, `rejected_spread_unavailable`) are in structured logs only. They are not surfaced in `paper_trading_monitor.py` or `/tmp/qe_live_counters.json`. Operators cannot monitor filter behavior in real time during a live session. (PHASE8-011 — open.)

3. **Net-edge filter not battle-tested** — The viability filter (`net_edge_too_small`) correctly rejected all signals in the A/B test, but on a single-day sample of a choppy session. Five sessions of trending conditions are required to confirm the filter passes truly good trades while blocking borderline ones.

### Path to re-evaluation

All five conditions must be met before scalp_1m can be reconsidered for Stage-1:

| Condition | Status |
|---|---|
| 5+ paper sessions with ≥10 signals per session (trending conditions) | NOT MET |
| Win rate ≥ 45% and profit factor ≥ 1.2 across those sessions | NOT MET |
| Zero stop distances inside ATR across all signals | NOT MET |
| Rejection counters in `paper_trading_monitor.py` (PHASE8-011) | NOT MET |
| Session P&L from scalp_1m non-negative over 5-session sample | NOT MET |

When all five are met: create a new validation report and propose a strategy eligibility update for operator approval.

### Gate enforcement

`LiveGateChecker` check 26 (`no_paper_only_strategy_in_live`) blocks Stage-1 approval if `scalp_1m` appears in the strategy-config table with `paper_trade=false`.

Source: `services/shared/live_gate_checker.py` → `PAPER_ONLY_STRATEGIES` constant and `_check_no_paper_only_strategy_in_live()`.

---

## Notes on Eligible Candidates

"ELIGIBLE CANDIDATE" means the strategy may be proposed for Stage-1 after passing its own validation cycle. It does not mean the strategy is currently approved for live. All Stage-1 live promotions require:

1. Paper validation gate (5 sessions, pass criteria per `pre-live-runbook.md`)
2. LiveGateChecker all 26 checks pass
3. Manual operator sign-off (`scripts/ops/approve_live_gate.py`)
4. `QE_EXECUTION_LIVE_TRADING_ENABLED=true` explicitly set by operator (never automatic)

Current Stage-1 primary candidate: `nse_vwap_reversion` on `HDFCBANK` (1 share, ₹50k capital, ₹2k max order). See `stage1-config-pack.md`.
