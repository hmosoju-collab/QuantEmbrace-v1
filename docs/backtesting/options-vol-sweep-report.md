# Options-Vol Structure Robustness Sweep (O-2 follow-up)

**Pre-registered grid & rule:** 2026-06-20 · **Data:** free NSE F&O bhavcopy (EOD, 2022-06→2025-06) ·
**Live trading: BLOCKED.** Advisory only — backtesting cannot promote.

Purpose: confirm whether the naive-condor O-2 FAIL is **structural** or a single-config artifact, by
running a pre-declared OTM×wing grid on the same data and reporting **every** cell (no cherry-picking).

## Results (monthly held-to-expiry, 2.5% slippage, full cost stack, NAV ₹10L, ≥1 lot)
| OTM | wing | cycles | net | gross (0-slip) | PF | exp/cycle | pos-yrs | maxDD | O-2 gate |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1% | 1% | 37 | ₹-317,956 | ₹-267,891 | 0.27 | ₹-8,593 | 0% | -30.5% | fail |
| 2% | 1% | 37 | ₹-156,809 | ₹-23,819 | 0.53 | ₹-4,238 | 50% | -19.2% | fail |
| 3% | 1% | 36 | ₹-108,203 | ₹-93,221 | 0.49 | ₹-3,006 | 25% | -11.1% | fail |
| 4% | 1% | 33 | ₹-88,780 | ₹-72,343 | 0.40 | ₹-2,690 | 25% | -9.8% | fail |
| 5% | 1% | 32 | ₹-7,902 | ₹7,655 | 0.91 | ₹-247 | 50% | -5.4% | fail |
| 1% | 2% | 37 | ₹-208,522 | ₹-170,430 | 0.39 | ₹-5,636 | 0% | -19.5% | fail |
| 2% | 2% | 36 | ₹-115,072 | ₹-81,154 | 0.61 | ₹-3,196 | 25% | -13.5% | fail |
| 3% | 2% | 36 | ₹-124,555 | ₹-100,798 | 0.58 | ₹-3,460 | 50% | -15.2% | fail |
| 4% | 2% | 31 | ₹-53,953 | ₹-38,954 | 0.67 | ₹-1,740 | 50% | -8.7% | fail |
| 5% | 2% | 37 | ₹770 | ₹12,697 | 1.01 | ₹21 | 25% | -4.4% | fail |

- **Gross-positive (zero-slip) configs:** 2 — if 0, no structure captures the premium even
  before costs (an edge problem, not a friction problem).
- **Configs clearing the O-2 gate:** 0.

## VERDICT
**SHELVE — no monthly condor structure clears the O-2 gate; the naive short-vol harvest is structural, not a single-config artifact.**

---
*Decision rule (pre-declared): a pass counts only against the unchanged O-2 gate, with margin AND a passing
neighbour (contiguous island) — guarding the ~0.5 false passes expected from 10 configs × ~31 cycles.
Even a genuine pass here is EOD / held-to-expiry and would still need intraday + active-management testing
before any human-reviewed pilot. Live trading remains BLOCKED.*
