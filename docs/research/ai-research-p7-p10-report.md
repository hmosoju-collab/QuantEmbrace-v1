# Hybrid AI Research — Phases 7–10 Report

> ADR-043 (Phase 7–10 addendum) · 2026-09-26 · branch `feature/hybrid-ai-research`.
> **Status: P7, P8 and P9 built and tested. P10 is built and tested against a fake runtime, but the real Bedrock run is BLOCKED by the AWS account.** No real model has produced any output; **$0 was spent.** Nothing is pushed, and the forward AI shadow gate is still an unsigned DRAFT.

## 1. Result at a glance

| Phase | What now exists | Verified how |
|---|---|---|
| **P7** Post-trade analyst | `python -m qe.ai post-trade`: completed trades rebuilt from an engine journal (read-only), deterministic reviews, an LLM lesson, `lessons_known_at(cutoff)` | 7 tests + a real run: 20 delivery-walk-forward trades in 9 s (factor pick beat the benchmark in 11/20) |
| **P8** Dashboard | `python -m qe.ai dashboard`: one static HTML page, AI recommendation **next to** QuantEmbrace's decision, everything §24 asked for | 2 tests incl. a hostile-model XSS attempt |
| **P9** External data hardened | NSE announcements: raw-zone downloader (network stays in `scripts/`), quarantine-first curation, point-in-time `news` tool | 35 tests. **The downloader has not been run against live NSE.** |
| **P10** Real LLM | Bedrock adapter on the official SDK, `probe`, loud failure reporting, smoke config | 22 + 9 tests; **real run blocked** (§5) |

`tests/qe`: **591 passed, 0 skipped**, and the same 591 pass **without** the optional `anthropic` SDK (as CI runs). Ruff is clean. No engine, v1 or dependency file changed (`requirements-ai.txt` is new and optional).

## 2. P7 — post-trade analyst

- **Trades** are position episodes rebuilt from the engine journal: the rebalance where a name goes 0 → held, to the one where it returns to 0. Costs are allocated pro rata by notional. Positions still open are reported, not reviewed. An exact-arithmetic test checks the reconstruction.
- **Every classification is computed by code**, with documented thresholds: factor thesis vs the equal-weight benchmark; AI thesis vs the *first* research signal at entry; entry and exit quality; regime call vs the benchmark's direction; execution cost bucket; a large-adverse-excursion risk event. The model writes only a lesson and an "unexpected event" note.
- **The engine journal is never rewritten**; a test asserts its bytes are unchanged.
- **Knowledge time.** Each review is stamped knowable at the trade's exit close. `lessons_known_at(cutoff)` returns only earlier reviews, and hypothesis generation now uses it. This is the look-ahead-safe form of TradingAgents' reflection memory, which I had deferred in Phase 0.

## 3. P8 — dashboard

The page shows: market regime, quant score, strategy signal, AI research score and confidence, risk score and flags, the AI recommendation beside the final QuantEmbrace decision (and whether they agree), bull / bear / consensus / conflicting evidence, the shadow gate, post-trade reviews, hypothesis drafts and the lifecycle ledger. It computes no decision. Much of its text is untrusted LLM output, so every value is HTML-escaped, a `default-src 'none'` Content-Security-Policy is set, and the page has no JavaScript. A test injects `<script>` and `<img onerror>` through a hostile model and confirms both render only as text.

## 4. P9 — external data

- **Design.** `qe.ai` is forbidden from any network code. The only network component is `scripts/backtest/download_nse_announcements.py`, which stores what NSE returned verbatim in an immutable raw zone, with a tz-aware fetch time. `python -m qe.ai corpus ingest` does everything else, and each raw record ends in **exactly one bucket**: curated, held for human review (with a redacted excerpt), or rejected with a reason.
- **Sanitising.** NFKC, tag and entity stripping, and removal of every control, format, private-use and unassigned character (zero-width, bidi overrides).
- **Screening.** A heuristic screen for injection phrasing, secret-like strings and forbidden-action language. It runs on both the sanitised text and the markup-preserving form. *A test caught a real hole here:* the tag-stripper was deleting `<<<END_UNTRUSTED_DATA>>>` before the screen ever saw it. There is now one `screen_raw()` entry point that checks both forms.
- **Point-in-time rules.** `knowledge_ts` is the later of exchange dissemination and announcement time; a date-only item counts as known at 23:59:59 IST; an item dated after its own fetch time is rejected. The curated file is re-validated and re-screened on **every load**, so tampering is dropped and counted.
- **The `news` tool** uses a 30-day window, stamps each evidence item at its **own** publication time, exposes only sanitised **headlines** (bodies never enter prompts), and returns UNAVAILABLE (zero LLM calls) when there is nothing to say. The corpus hash is journaled in each run manifest.
- **Still unavailable:** fundamentals and sentiment. There is no structured source, and announcement PDFs are not parsed.
- **Not verified live.** I did not run the downloader against NSE (the plan said it runs only when you run it). NSE's endpoint shape, cookie priming and bot-blocking are unproven in this session.

## 5. P10 — real Bedrock: built, and blocked by the AWS account

**Built.** The backend uses the official Anthropic SDK's Bedrock Mantle client with `anthropic.claude-opus-5-5` in both tiers, as you chose. It encodes Opus 5.x facts I checked against Anthropic's docs and the installed SDK 1.8.0:
- sampling parameters are never sent (removed on this model family; sending one is a 400);
- no tools and no thinking configuration are sent, and an explicit `effort` is;
- a provider refusal becomes BLOCKED with no retry;
- errors cross the adapter as `type:status` only.

New optional config fields (`effort`, `budget.retry_backoff_s`) default to `None`, so **no existing config hash moved** (ADR-042 lesson). `boto3` and `botocore` are now banned in `qe.ai` altogether.

**Cost estimate for the approved smoke run.** At $4/$20 per MTok, the worst case (7 calls × 4,096 output tokens, plus about 10k input) is roughly $0.60, under the $1 cap.

**Real run, 2026-09-26.** The smoke run failed on every call, and the safeguards behaved exactly as designed (breaker opened after 3 failures, the run finished, every component honestly UNAVAILABLE):

| Where | Result |
|---|---|
| Messages endpoint, `ap-south-1` | `404 model does not exist` — for **every** model tried (Opus 5.5, Opus 5, Sonnet 5) |
| Messages endpoint, `us-east-1` | `403 … not available for this account` — Opus 5.5, Sonnet 5, Haiku 4.5, Opus 4.8, Opus 5 (Fable 5.1: 404) |
| Classic `bedrock-runtime`, `ap-south-1` | the same `403` for Opus 5.5 and Sonnet 5 |
| `aws bedrock get-foundation-model-availability` | `AUTHORIZED / AVAILABLE`, but `agreementAvailability: NOT_AVAILABLE` |
| First-party Anthropic credentials | none on this machine |

The 403 text ends "contact AWS Sales", which points to an account-level entitlement gate on AWS's side. **Spend: $0** (0 tokens; failures happen before billing). A handful of "hi" probes (8 tokens each) were made to diagnose this; none succeeded.

**Two tooling gaps found and fixed along the way:**
- `research` printed "3 calls" and **exited 0** while every call had failed. It now reports failures, and **exits 3** with a loud warning when no call succeeded.
- There was no cheap access check. `python -m qe.ai probe` now sends a few-token request and prints an actionable hint per status.

## 6. Test results (actual)

| Measurement | Result |
|---|---|
| `pytest tests/qe -rs` | **591 passed, 0 skipped** (was 490 at the end of Phase 6) |
| Same suite, optional `anthropic` uninstalled (CI condition) | **591 passed** |
| `ruff check qe tests/qe` | Clean |
| Engine files (`qe/engine`, `execution`, `risk`, `strategy`, `config`, `portfolio`, `killswitch`, `live_gate`) and `services/` | Unchanged since Phase 6 |
| New tests | post-trade 7 · dashboard 2 · corpus 35 · probe 9 · additions to llm, agents, boundary |

## 7. Remaining risks and open items

1. **No real model output exists.** Every result in this program still comes from the deterministic fake. Whether an LLM adds anything is unmeasured.
2. **The AWS account is not entitled to Claude on Bedrock**, or at least not on the endpoints tried. Your options: enable Anthropic model access for the account (Bedrock console, or AWS Sales), or provide a first-party Anthropic credential and I add a second adapter behind the same `LLMClient` protocol (about a day's work, not built).
3. **Signals dated before 2026-09-29 are contaminated** under the Jun-2026 knowledge cutoff plus 90-day guard. The first date that can ever count toward the shadow gate is the **2026-09-30 month-end**. Missing it costs one month of the 12-month series, not the program.
4. **The shadow gate must not be signed off** until a real model has run and been probed. `--show-binding` warns about this.
5. ~~The NSE downloader is untested against the live site~~ — verified live 2026-09-26 (addendum); NSE still blocks scripted access at will, so expect intermittent failures.
6. **The screen is heuristic.** Held documents need a human, and the injection patterns will need tuning on real data.
7. **The CI `test-qe` job has still never run on GitHub**, and nothing is pushed.

## 8. Recommended next steps

> **Update (2026-09-26, later): the first-party Anthropic API backend is built** — see the addendum 2 at the end of this report. Step 1 is now satisfied by *either* enabling Bedrock access *or* exporting `ANTHROPIC_API_KEY` and running the probe with `configs/qe_ai_research_anthropic.yaml`.

1. **Unblock the model** (operator): enable Anthropic access on the AWS account, then run `python -m qe.ai probe … --allow-llm-spend`. Tell me if you'd rather use a first-party API key instead.
2. ~~Run the downloader once~~ — done (see the addendum). Widen it (all 217 universe names, a longer history) once the screen has seen more text.
3. Once a probe passes, run the capped smoke run, then a STANDARD run on **2026-09-30** (the first clean date) to start accruing.
4. Only after that, review and sign off the shadow gate.
5. Push and open the two PRs (`fix/findings-triage`, `feature/hybrid-ai-research`) so CI runs the new `test-qe` job.

---

## Addendum — first live NSE run and real-data end to end (2026-09-26, later the same day)

Operator said "proceed next". The AI-model blocker is unchanged (the probe still returns `404`; no first-party credential), so I did the part that
did not depend on it: the **first live run of the NSE downloader** and a news-enabled research run on the real lake.

| Step | Result |
|---|---|
| Downloader, 2 symbols, 18 days | 13 records, 0 failed requests — endpoint, session priming and record shape all matched the parser |
| Downloader, the real 20-name delivery basket, 2026-06-10 → 07-14 | 295 records, 0 failed requests |
| `corpus ingest` (308 records seen) | **307 promoted, 1 duplicate, 0 held, 0 rejected** |
| STANDARD research run, real lake, fake backend, as-of 2026-07-14 | 20 signals, 141 calls, 0 failures, 7.6 s; the news agent produced evidence for all 20 names; corpus hash journaled |

**What the live data changed** (three fixes, tested):
- Every announcement opens with the same boilerplate ("X Limited has informed the Exchange regarding…"). It is now stripped from the headline.
- NSE's own subject label (`desc`, e.g. "Shareholders meeting") is now kept and leads each evidence line. 11 of the first 13 records had landed in `OTHER`.
- Routine filings (newspaper copies, trading-window notices, ESOP allotments, investor-call notices; 81 of 307 here) are tagged `ROUTINE_FILING` and yield no news evidence, so they cost no LLM call. A subject that merely repeats the headline is shown once.

**Honest limits.**
- **0 held / 0 rejected on genuine text is what we want, but it does not prove the screen would catch a real attack.** Exchange feeds rarely contain injection attempts, and the detection evidence is still the synthetic test set. Expect to tune the patterns if the volume grows.
- The window is one month and 22 issuers. Category coverage will need widening (about 31% of documents are `OTHER`; NSE's "General Updates" hides the real subject in the text, which the model does read).
- **Cost projection for a real STANDARD run** (20 symbols, 141 calls): about 155k input tokens (about $0.62 at Opus 5.5's $4/MTok) plus output. Output is unmeasured, since Opus 5.x always thinks and those tokens are billed, so budget roughly **$2–5 per monthly STANDARD run** and about $0.6–1 for FAST. This is an estimate from prompt size, not a measurement.

`tests/qe`: **601 passed, 0 skipped.**

---

## Addendum 2 — first-party Anthropic API backend (2026-09-26, later still)

The operator approved building the second backend, so an Anthropic API key can unblock P10 without waiting on the AWS account.

| Item | Result |
|---|---|
| What was built | `backend: anthropic` (`qe/ai/llm/anthropic_api.py`) beside `bedrock`, behind the same `LLMClient` protocol. The shared request/response/error mapping moved into `qe/ai/llm/messages.py` (no SDK import); `BedrockLLM` and `AnthropicLLM` are thin subclasses that build their SDK client lazily. |
| Same rules for both | No sampling params, explicit `effort`, no tools, `max_retries=0` on the SDK (the gateway owns retries), errors cross as `type:status` only (a test feeds a secret-shaped provider message and confirms it never surfaces), refusal → BLOCKED, `--allow-llm-spend`, budget, breaker, cache, contamination rule. |
| Credentials | Resolved by the SDK (`ANTHROPIC_API_KEY` or `ant auth login`). `qe.ai` bans `os.environ`, so it never reads, logs or forwards the key. A construction failure (for example no credential) now surfaces as a sanitized `LLMError`, not a raw exception. |
| Config / probe | `configs/qe_ai_research_anthropic.yaml` (`claude-opus-5-5`, cutoff 2026-06-30, same 60k-token smoke budget). The probe has first-party hints (401 = set the key, 404 = model ID has no `anthropic.` prefix) and no longer prints `region=` for a non-Bedrock backend. |
| Boundary | `anthropic` may now be imported in exactly two files (`bedrock.py`, `anthropic_api.py`); the shared `messages.py` must import none; `boto3`/`botocore` stay banned. No config hash moved (the `backend` literal only gained a value). |
| Verified | `tests/qe`: **621 passed, 0 skipped** with the SDK, and the same 621 **without** it. Ruff clean. The request-mapping, error, no-network and client-construction tests run against **both** backends; two tests check how each SDK client is constructed (first-party: no key argument, `max_retries=0`; Bedrock: the configured region). |
| Not verified | **No real call has been made through the first-party backend** (no key on this machine); its live behaviour is exactly as unproven as Bedrock's was before the account block. Spend: **$0**. |
