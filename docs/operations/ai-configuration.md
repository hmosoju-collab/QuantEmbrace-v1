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
| `backend` | `fake` | `fake` (deterministic, free), `bedrock` (Claude on Amazon Bedrock) or `anthropic` (first-party Anthropic API). Both real backends **require `--allow-llm-spend`**. |
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

### 2.2 Enabling a real backend (P10 — code built; account access is the operator's step)

Two real backends sit behind the same `LLMClient` protocol and share one Messages-API implementation
(`qe/ai/llm/messages.py`), so the Opus 5.x rules below, the spend flag, the budget/breaker, the probe and the
contamination rule are identical for both. Pick one with `backend:` in the research config.

| | `bedrock` | `anthropic` (first-party) |
|---|---|---|
| Config | `configs/qe_ai_research_bedrock.yaml` | `configs/qe_ai_research_anthropic.yaml` |
| Client | SDK `AnthropicBedrockMantle` (Messages endpoint) | SDK `Anthropic` |
| Model ID | `anthropic.claude-opus-5-5` | `claude-opus-5-5` (no `anthropic.` prefix) |
| Credential | SigV4 from the normal AWS chain (no API key); IAM `bedrock-mantle:CreateInference` on the model ARN only | Resolved **by the SDK**: `ANTHROPIC_API_KEY` in the shell that runs `qe.ai` (or `ant auth login`). `qe.ai` never reads the environment and never handles or logs the key; the secret scanner would refuse a prompt containing one. |
| Egress | the Bedrock regional endpoint | `api.anthropic.com` |
| `region` field | used | ignored |

Steps (both):

1. `pip install -r requirements-ai.txt` (optional dependency; not needed for the fake backend, tests or CI).
2. Use the matching config above (Opus 5.5, `effort: low`, `max_tokens_per_call: 4096`,
   `knowledge_cutoff: 2026-06-30` = the "training data cutoff Jun 2026" of Anthropic's models overview,
   guard 90 d ⇒ **first uncontaminated decision date is 2026-09-29**). Opus 5.x removed sampling parameters and
   cannot disable thinking, so the adapter sends none and relies on `effort`.
3. Bedrock only: grant the run's IAM identity `bedrock-mantle:CreateInference` on the model ARN only. First-party:
   `export ANTHROPIC_API_KEY=...` in your shell (never in a config file or a prompt).
4. **Probe before spending:** `python -m qe.ai probe --config <config> --allow-llm-spend` (a few tokens). It prints an
   actionable, backend-specific hint per HTTP status (Bedrock: 403 = account not entitled, 404 = endpoint does not
   serve that model/region; first-party: 401 = no credential, 403 = key/org not permitted, 404 = unknown model ID;
   both: 400 = bad parameter, 429 = throttled / out of credit).
5. Run with `--allow-llm-spend`. Without the flag the CLI refuses (exit 2) before writing anything. If every
   LLM call fails, `research` exits **3** with a loud warning (it does not pretend to have run).

**State on 2026-09-26 (this account): BLOCKED.** The Messages endpoint returned `404 model does not exist` for
every model in `ap-south-1` and `403 not available for this account` in `us-east-1` — also on the classic
`bedrock-runtime` path, and also for models the docs list as open to all customers — while
`aws bedrock get-foundation-model-availability` shows `AUTHORIZED`. That message directs the account to AWS
Sales / Bedrock model access. Spend: $0 (0 tokens). Alternatives that need an operator decision: enable
Anthropic model access for the AWS account, **or use the first-party backend** (`backend: anthropic`, built and
tested against a fake runtime the same day; no real call has been made through it yet — it needs an API key).

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

## 5. New commands (P7–P10)

```bash
python -m qe.ai post-trade --engine-journal journals/<sim-or-paper>.jsonl [--max-trades 20]   # P7
python -m qe.ai dashboard                                                                   # P8 -> reports/qe-ai/dashboard/index.html
python scripts/backtest/download_nse_announcements.py --from 2026-08-01 --to 2026-09-25     # P9 raw zone (network)
python -m qe.ai corpus ingest        # P9 sanitise -> screen -> validate -> promote / hold / reject
python -m qe.ai corpus status
python -m qe.ai probe --config configs/qe_ai_research_bedrock.yaml --allow-llm-spend        # P10 access check (Bedrock)
python -m qe.ai probe --config configs/qe_ai_research_anthropic.yaml --allow-llm-spend      # P10 access check (first-party; needs ANTHROPIC_API_KEY)
```

- **Held documents** (`backtest-data/ai_corpus/held/`) are text the screen flagged as injection, secret-like or
  forbidden-action language. They are never promoted; a human reads them and, if a false positive, edits the
  screen — never the file.
- **NSE announcements**: `knowledge_ts` is the later of exchange dissemination and announcement time; a date-only
  item counts as known at 23:59:59 IST. Only sanitised headlines reach prompts.
