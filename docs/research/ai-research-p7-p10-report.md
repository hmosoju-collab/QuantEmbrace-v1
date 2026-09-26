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
5. **The NSE downloader is untested against the live site**, and NSE actively blocks scripted access.
6. **The screen is heuristic.** Held documents need a human, and the injection patterns will need tuning on real data.
7. **The CI `test-qe` job has still never run on GitHub**, and nothing is pushed.

## 8. Recommended next steps

1. **Unblock the model** (operator): enable Anthropic access on the AWS account, then run `python -m qe.ai probe … --allow-llm-spend`. Tell me if you'd rather use a first-party API key instead.
2. Run the downloader once for a small window (`--symbols INFY,TCS`), then `corpus ingest` and inspect `held/` and `rejected/`.
3. Once a probe passes, run the capped smoke run, then a STANDARD run on **2026-09-30** (the first clean date) to start accruing.
4. Only after that, review and sign off the shadow gate.
5. Push and open the two PRs (`fix/findings-triage`, `feature/hybrid-ai-research`) so CI runs the new `test-qe` job.
