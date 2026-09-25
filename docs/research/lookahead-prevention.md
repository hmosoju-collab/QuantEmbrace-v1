# Look-Ahead Prevention for AI Research

> ADR-043. This extends the platform's point-in-time rules (`docs/backtesting/no-lookahead-rules.md`, the data-lake contract) to `qe.ai`. The mechanisms are `[PLANNED — not yet implemented]` until P2–P5 land.

AI research has **two** independent look-ahead channels. The first can be fully closed with engineering. The second cannot, so it is handled by governance.

## 1. Channel A — data look-ahead (closed by construction)

### 1.1 information_cutoff

Every research run has one decision date `D`, the last panel row at or before the requested as-of date. It has one cutoff:

```
information_cutoff = D @ qe.clock.market_close_time(market)   (in qe.clock.market_tz(market))
                     NSE: D 15:30 IST · US: D 16:00 America/New_York
```

This matches the engine exactly: `qe` rebalances at the close of `D` using `close[D]` (`Context.at(panel, pos)`).

### 1.2 Knowledge time for daily bars

**A daily bar dated `D` becomes knowable only at `D`'s market close.** Its `knowledge_ts` is `D @ close`, not the bar's raw timestamp:
- Bhavcopy bars are stamped at 15:30 IST.
- Kite index candles are believed to be stamped at the start of the day (unconfirmed).

Either way, a raw `timestamp ≤ cutoff` comparison would let the same day's close leak into a decision taken before the close. Stamping `knowledge_ts` at the close closes that gap.

- `ResearchDataAPI.at(panel, as_of: datetime)` picks the last row whose `knowledge_ts ≤ as_of`. A request at `D 11:00` therefore sees `D−1`.
- Every `Evidence` carries `knowledge_ts`.
- `ResearchSignal` validation **rejects the whole signal** if any attached evidence has `knowledge_ts > information_cutoff`. This is fail-closed.

### 1.3 Tools see only the point-in-time slice

- Tools receive `ResearchDataAPI`, which exposes only `Context.at(panel, pos)`: frames sliced `.iloc[:pos+1]`.
- Rolling windows are strictly backward-looking.
- The liquidity universe uses `[pos−60, pos)` via `qe.universe.liquid_universe`, exactly as the engine does.
- **Do not reuse `qe.research.wf_v1.regime_series`.** It selects its market proxy from **total-period** turnover (`turn.sum()`), which is look-ahead ([current-state F-10](../architecture/current-state.md)). `qe.ai.tools.regime` builds its proxy from trailing `[pos−60, pos)` turnover instead.
- Missing index coverage (INDIAVIX in the lake ends in 2025) means **UNAVAILABLE, never a forward fill**.
- Tools must not mutate the panel. A test hashes the panel before and after a full run.

### 1.4 Tests

`tests/qe/ai/test_ai_tools_pit.py` checks five things:
- Multiplying every row after `pos` by 10 leaves every tool output unchanged.
- Evidence stamped after the cutoff makes the signal invalid.
- A same-day close is invisible to an as-of before the close.
- The panel hash is unchanged after a run.
- The quant tool matches the engine's `select_basket` at every rebalance row.

### 1.5 Other information types (future)

| Type | Rule when a source is added (P9+ data phase) |
|---|---|
| News | `knowledge_ts` = publisher timestamp, plus ingestion lag. Never use the article's "updated" time. Store raw immutable text with fetch time. |
| Fundamentals | **As filed**: `knowledge_ts` = filing or announcement time. Restated figures get a new `knowledge_ts`; never overwrite history. |
| Sentiment | `knowledge_ts` = post time. Aggregate only posts with `knowledge_ts ≤ cutoff`. |
| AI observations / reflections | `knowledge_ts` = when the observation was produced. A reflection on a trade's outcome is knowable only after the outcome was realized (P7). |

## 2. Channel B — model-memory look-ahead ("contamination")

An LLM was trained on text that reaches up to its **knowledge cutoff**. Ask it on a historical date `D` before that cutoff whether a stock looks attractive, and its answer can draw on what happened after `D`: price paths, earnings surprises, scandals, index changes. **No `information_cutoff` on the tools can remove what is in the weights.** Masking tickers or dates reduces the leak but cannot be shown to remove it.

### 2.1 Rule

```
contaminated = (model.knowledge_cutoff is None) or (D <= model.knowledge_cutoff + guard_days)
```

- Evaluated per signal against **every** model used (quick and deep).
- `guard_days` defaults to 90, because published cutoffs are approximate and training corpora lag.
- **Computed, never claimed.** `ResearchSignal` stores the inputs (`knowledge_cutoffs`, `guard_days`) and its validator recomputes `contamination_risk`. A mismatch is invalid.
- An unknown cutoff counts as contaminated.

### 2.2 Consequences

1. **Historical backtests of AI scores are not evidence.** They may be run for plumbing tests or curiosity, but their results can never support promotion or a non-zero AI weight.
2. Fusion never gives weight to a contaminated signal, in any mode or context.
3. The **only** valid way for an AI score to earn weight is **forward accrual**:
   - Signals are recorded at decision dates *after* `knowledge_cutoff + guard`.
   - They are scored later against realized outcomes.
   - A pre-registered gate, fixed before accrual starts, decides the result, with the same discipline as `check_forward_gate.py`.
   
   This is the P6/P10 shadow ledger `[PLANNED — not yet implemented]`.
4. Changing models (a new `model_id` or cutoff) starts a new forward series. Accrued evidence does not transfer between models.
