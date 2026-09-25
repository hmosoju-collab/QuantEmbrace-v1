# AI Research — Configuration and Fusion Methodology

> ADR-043. `qe.ai` is offline and advisory: nothing here is read by `qe study`, `qe paper`, or `qe live`.
> Related: [observability.md](observability.md) · [failure-handling.md](failure-handling.md) · [hybrid-ai-system.md](../architecture/hybrid-ai-system.md)

## 1. Commands

```bash
# research one decision date (fake backend: zero spend); writes journals/ai/<run_id>.jsonl + reports/qe-ai/<run_id>/
python -m qe.ai research --config configs/qe_ai_research.yaml --as-of 2026-07-31 [--symbols INFY,TCS]

# rebuild the derived report from a research journal
python -m qe.ai report --journal journals/ai/<run_id>.jsonl

# research view: AI recommendation next to QuantEmbrace's decision (default AI_ADVISORY, weight 0)
python -m qe.ai fuse --research journals/ai/<run_id>.jsonl \
    [--engine-journal journals/paper-delivery-book-paper-<...>.jsonl] [--context shadow|study]
```

```bash
# P6: draft testable hypotheses from a research run (CANDIDATE drafts for human review only)
python -m qe.ai hypothesize --research journals/ai/<run_id>.jsonl

# P6: evaluate the pre-registered forward AI shadow gate (DRAFT ⇒ counts only, no verdict)
python -m qe.ai shadow [--as-of YYYY-MM-DD]
python -m qe.ai shadow --show-binding          # the values a human fills in to sign off

# P6: strategy lifecycle ledger (engine-side governance; qe.ai cannot call it)
python -m qe lifecycle status
python -m qe lifecycle transition --strategy <id> --to CANDIDATE --family <f> \
    --hypothesis-ref qe.ai:<draft_id> --approved-by <your name>
```

`python -m qe.ai` is intentionally separate from `python -m qe`. The engine CLI loads the paper engine at import time, and the two must never share a process.

## 2. Research config — `configs/qe_ai_research.yaml`

| Key | Default | Meaning |
|---|---|---|
| `schema_version` | `qe_ai_research/1` | Bump on any non-optional schema change |
| `book_config` | `configs/qe_delivery_book_paper.yaml` | The engine book to annotate (read-only). Supplies factor params, market and lake. |
| `research_mode` | `FAST` | `FAST` / `STANDARD` / `DEEP` (see §2.1) |
| `backend` | `fake` | `fake` (deterministic, free) or `bedrock` (**requires `--allow-llm-spend`**) |
| `quick_model` / `deep_model` | `fake-quick` / `fake-deep` | `model_id`, `tier`, `knowledge_cutoff`. An **unknown cutoff (`null`) marks every signal contaminated.** |
| `budget.max_run_tokens` | 200000 | Hard per-run token budget (the worst case is reserved before each call) |
| `budget.max_tokens_per_call` | 1024 | Per-call output cap |
| `budget.timeout_s` | 30 | Provider read timeout |
| `budget.max_retries` | 1 | Retries per agent on timeout/error and on malformed output |
| `budget.breaker_threshold` | 3 | Consecutive failures before every remaining component becomes UNAVAILABLE |
| `guard_days` | 90 | Added to each knowledge cutoff for the contamination rule |
| `max_symbols` | 20 | The engine basket first, then `symbols`/`--symbols`, capped at this |
| `mask_identifiers` | `true` | Withhold tickers from prompts (a contamination mitigation, not a cure) |
| `temperature`, `seed`, `use_cache`, `region` | 0.0, 0, true, ap-south-1 | `seed` drives the fake backend. The cache keys on the prompt content. |

The config is hashed on its own (`exclude_none`) and echoed into the journal header. **It is not part of `qe.RunConfig`**, so no engine config hash moves (ADR-042).

### 2.1 Modes

| Mode | LLM calls per symbol (today) | What runs |
|---|---|---|
| FAST | ≤ 2 | technical, risk (+ regime once per run); deterministic synthesis |
| STANDARD | ≤ 6 | + bull/bear (1 round), critic, synthesizer (quick model). Fundamental, news and sentiment run and return UNAVAILABLE with 0 calls. |
| DEEP | ≤ 8 | 2 debate rounds, and the synthesizer on the deep model |

### 2.2 Enabling a real backend (P10 — `[PLANNED — not yet implemented]` as an operational step)

1. Set `backend: bedrock`. Set `quick_model.model_id` and `deep_model.model_id` to Bedrock model or inference-profile IDs enabled in the account.
2. Set each `knowledge_cutoff` from the model card. If it is left `null`, every signal is contaminated and can never carry weight.
3. Grant the run's IAM role `bedrock:InvokeModel` / `bedrock:Converse` on those model ARNs only.
4. Run with `--allow-llm-spend`. Without the flag the CLI refuses (exit code 2) before writing anything.

## 3. Fusion — `configs/research_fusion.yaml`

### 3.1 Formula (per symbol *s*, decision date *t*)

```
q  = 2·rank_pct(book factor score | PIT liquid universe) - 1        ∈ (-1, 1]; None if no score
a  = ResearchSignal.ai_score   (mean of OK directional analysts)    ∈ [-1, 1]
c  = ResearchSignal.ai_confidence                                   ∈ [0, 1]
w  = ai_weight if AI view USABLE else 0
     USABLE ⇔ mode ∈ {AI_WEIGHTED, AI_EXPERIMENTAL} ∧ context = study ∧ signal for exactly this cutoff
              ∧ not contaminated ∧ a, c present
S  = q                    if w = 0        (identity)
     (1-w)·q + w·c·a      otherwise
decision = REJECT       if any hard flag (no price, stale data > max_data_age_days,
                                          outside liquid universe, optional vol/drawdown caps)
           NO_DECISION  if q is None     (AI alone never decides)
           DISABLED / ADVISORY  : SELECT ⇔ engine's own pick (FactorBookStrategy basket)
           WEIGHTED / EXPERIMENTAL : SELECT ⇔ top-k by (S desc, q desc, symbol)
AI recommendation = POSITIVE / NEGATIVE / NEUTRAL by sign(a) with a ±0.20 dead zone
                     — reported next to the decision, never merged into it
```

### 3.2 Why only one weight

The brief sketched four configurable weights (quant, regime, AI, risk). Only `ai_weight` is free, and the quant weight is `1 − ai_weight`:

- **Regime is not blended.** It is a market-level value, identical for every symbol, so adding it cannot change a cross-sectional ranking; it would only make a score look informative. It is reported instead. Applying regime to *exposure* would be a strategy change and needs its own pre-registered study.
- **Risk is not blended.** A hard limit that can be traded off against a score is not a hard limit. Risk is a deterministic veto. LLM risk narrative never vetoes and never approves.

### 3.3 Modes and ceilings (enforced in code)

| Mode | `ai_weight` ceiling | Contexts | Effect |
|---|---|---|---|
| `AI_DISABLED` | 0 | study, shadow | AI inputs are not read at all |
| **`AI_ADVISORY` (default)** | 0 | study, shadow | Decision equals the engine's pick; the AI view is displayed beside it |
| `AI_WEIGHTED` | 0.20 | **study only** | Clean signals can re-rank names. At 0.20, a 70th-percentile name can overtake a 90th. |
| `AI_EXPERIMENTAL` | 0.50 | **study only** | Research on the formula itself |

**Why the default weight is 0.** There is no validated AI edge. Historical evaluation cannot provide one, because of training-data contamination ([lookahead-prevention §2](../research/lookahead-prevention.md)). AI can earn weight only through a pre-registered forward shadow gate (P6/P10 `[PLANNED]`). Even then a pass means a human review and a new ADR, not an automatic change.

### 3.4 Guards

- A fusion run recomputes the quant view on the **same data snapshot** the research run used. If the lake changed since, the run is refused.
- Signals researched for a different `information_cutoff` are ignored and listed in `ignored_signals`.
- `--engine-journal` is read-only. It adds what the engine actually did that day (risk verdict, rebalance or skip, kill-blocked) to the view.

## 4. Signing off the forward AI shadow gate (P6 → P10)

`configs/qe_ai_shadow_gate.yaml` is committed as **DRAFT**. Do **not** sign it off while the research
config uses `backend: fake` or an unknown `knowledge_cutoff` — `--show-binding` warns about both.

1. Complete §2.2 (real Bedrock model IDs + their knowledge cutoffs) and commit the research config.
2. `python -m qe.ai shadow --show-binding` → copy `research_config_hash` and `model_id`.
3. Set `status: SIGNED_OFF`, `signed_off_by`, `signed_off_on` (today) in the gate and **commit before the
   first counted month-end**. Thresholds are never relaxed afterwards; changing the research config or
   model starts a new series (a new sign-off).
4. Monthly: `python -m qe.ai research …` at the book's month-end, then `python -m qe.ai shadow`.
5. A PASS means human review and a new ADR before any `ai_weight` change — never automatic.
