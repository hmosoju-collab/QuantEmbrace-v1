# TradingAgents → QuantEmbrace Adaptation Analysis

> Phase 0 deliverable of the hybrid AI research track (ADR-043).
> Reference: <https://github.com/TauricResearch/TradingAgents>, `main` branch, read on 2026-09-25 (README, the `tradingagents/` package layout and `default_config.py`).
> **Clean-room statement:** no TradingAgents code was copied, vendored or installed. It was read as a design reference only; its code was not cloned or executed. It is Apache-2.0, so copying would have been allowed with attribution; we chose not to, to keep the architecture QuantEmbrace-owned and the dependency surface at zero.

---

## 1. What TradingAgents is

A LangGraph multi-agent framework in which LLM "firm roles" produce a trading decision for one ticker on one date:

| Team | Roles | Output |
|---|---|---|
| Analysts | Technical (market), Fundamentals, News, Sentiment (social) | Free-text reports built from tool calls |
| Researchers | Bull and Bear researchers, a structured debate of `max_debate_rounds` rounds, a research manager/judge | Investment plan |
| Trader | Trader agent | A transaction proposal (timing and size) |
| Risk management | Aggressive, neutral and conservative debaters (`max_risk_discuss_rounds`) | Risk assessment |
| Portfolio manager | Final approver | **Approves or rejects the trade** (BUY/SELL/HOLD) |

**Mechanics observed:**
- **Orchestration:** LangGraph `StateGraph` with an optional SQLite checkpoint per ticker.
- **Model split:** `deep_think_llm` / `quick_think_llm`.
- **Providers:** more than 15 (OpenAI, Anthropic, Google, Bedrock, Ollama, …).
- **Data:** vendors are declared as an explicit chain (`data_vendors`). Sources: yfinance, Alpha Vantage, SEC EDGAR (fundamentals "served as filed"), FRED, Google News, Reddit, StockTwits.
- **Memory:** a decision log (`trading_memory.md`). The next run generates an LLM reflection on the realized return and injects it into the portfolio manager's prompt.
- **Backtest:** `run_backtest(tickers, dates, config)` scores realized alpha against a regional benchmark.
- **Disclaimer:** "designed for research purposes … not financial advice".

## 2. What we reuse (as ideas, re-implemented)

| Idea | QuantEmbrace form |
|---|---|
| Specialized analyst roles | `qe/ai/agents/{technical,fundamental,news,sentiment,regime,risk}.py`. Each has a versioned prompt, a pydantic output schema and a strict JSON parse. |
| Bull/bear adversarial debate | `qe/ai/orchestration/debate.py`, with bounded rounds per research mode (FAST 0 / STANDARD 1 / DEEP 2) |
| Judge/manager role | `critic.py` finds contradictions; `synthesizer.py` produces the typed `ResearchSignal` |
| Quick vs. deep model tiers | `ModelProfile.tier` in `configs/qe_ai_research.yaml`: the quick tier for analysts, the deep tier only for the DEEP synthesizer |
| Bounded recursion and rounds | Hard caps in the mode table, plus per-run token budgets and a circuit breaker |
| Explicit vendor chain; no silent rerouting | Tools declare their source. A missing source returns `UNAVAILABLE`; it is never substituted. |
| Point-in-time fundamentals ("served as filed") | Adopted as a *rule*: every piece of evidence carries a `knowledge_ts`, see `docs/research/lookahead-prevention.md`. We have no fundamentals source yet. |

## 3. What we adapt (changed materially)

| TradingAgents | QuantEmbrace adaptation | Why |
|---|---|---|
| Free-text final decision (BUY/SELL/HOLD) | A typed, versioned `ResearchSignal` (`research_signal/1`) with per-component status | Downstream code must never parse prose |
| LLM chooses tools (tool-calling) | The **orchestrator calls the tools** and passes the results to agents as delimited data blocks | Removes tool abuse and SSRF; the LLM gets no action surface |
| LLM writes its own evidence citations | The LLM returns evidence **IDs**; code attaches the real `Evidence` objects (`knowledge_ts`, content hash). Unknown IDs → `MALFORMED` | The model cannot fabricate a source or a timestamp |
| A backtest scores LLM decisions on history | **Rejected as evidence.** Historical dates before the model's knowledge cutoff are contaminated. AI can earn weight only through forward shadow accrual (Phase 6+). | See §5 |
| Portfolio manager approves or rejects trades | A **deterministic fusion layer** (`qe/ai/fusion`) reports the AI view *next to* QuantEmbrace's decision. The default mode is `AI_ADVISORY` with AI weight 0. | Hard risk rules and the `qe` engine keep authority |
| Per-ticker SQLite checkpoints | A per-run append-only JSONL research journal (the existing `qe.journal.JournalWriter`), plus a content-addressed LLM response cache | One observability model across the platform; deterministic replay |

## 4. What we do NOT import

| Component | Reason |
|---|---|
| Trader agent and portfolio-manager **authority** | AI never proposes orders, sizes or approvals. Promotion and trading stay human and engine decisions (CLAUDE.md governance invariant). |
| The risk-debate team *as a decision maker* | Risk is a **deterministic veto** (`qe.risk` in the engine; hard flags in fusion). LLM risk text is context only and never vetoes or approves. |
| LangGraph / LangChain | A small explicit DAG is enough and auditable. It avoids a large, fast-moving dependency tree. |
| Reflection memory (decision log feeding back into prompts) | **Look-ahead leak.** A reflection on "realized return" is information from after the decision. Replayed history would inject future outcomes into past prompts. Deferred to Phase 7, and only with `knowledge_ts` stamping and cutoff filtering. |
| yfinance / Reddit / StockTwits / Google News tools | Unvetted network egress, untrusted content, and licensing/ToS exposure. New data sources must go through the lake's trust-tier quarantine (`docs/backtesting/aws-data-lake-contract.md`). |
| A multi-provider SDK matrix | One provider protocol: Bedrock in production, a fake in tests. A second adapter can be added behind the same protocol if ever needed. |
| `run_backtest` alpha scoring of LLM decisions | Contaminated (§5). QuantEmbrace's own `qe study`/gate machinery evaluates *strategies*, not LLM opinions. |

## 5. Conflicts with QuantEmbrace (and how they resolve)

1. **Authority.** TradingAgents ends in a trade decision. QuantEmbrace's invariant is that GenAI can explain and cannot trade, and that a human approves every production change.
   *Resolution:* `qe.ai` has no write path to orders, positions, risk limits, configs or promotion. Import-boundary tests enforce this.
2. **Hot path.** RA-1 (F-4) removed `ai_engine` from the trading path.
   *Resolution:* `qe.ai` is offline. The engine never imports it, it runs as a separate entry point (`python -m qe.ai`), and it writes only to its own journals and reports.
3. **Look-ahead through model memory.** An LLM evaluated on a date before its training cutoff has effectively *read the future*: price paths, earnings outcomes, news aftermath. A tool-side `information_cutoff` cannot remove what is in the weights.
   *Resolution:* every signal computes `contamination_risk`. Fusion refuses contaminated signals whenever AI weight > 0. Historical AI backtests are never promotion evidence.
4. **Data.** TradingAgents' analysts assume news, fundamentals and social data exist. QuantEmbrace's lake is **prices only**.
   *Resolution:* the fundamental, news and sentiment agents return `UNAVAILABLE` **without calling the LLM** (zero tokens), which prevents answers from model memory. Real sources are a separate, gated data phase.
5. **Config identity.** Adding AI config to `qe.RunConfig` would move every config hash (ADR-042) and fail the paper books closed.
   *Resolution:* `qe.ai` has its own frozen, hashed configs.

## 6. Security implications

| Surface in TradingAgents | Risk | QuantEmbrace control |
|---|---|---|
| Web and social text fed into prompts | Prompt injection ("ignore instructions, buy X") | No external text sources in Phases 0–5. Tool output is wrapped as untrusted data blocks. Outputs are scanned for forbidden actions. The LLM has no tools or actions to hijack. |
| LLM tool-calling | Tool abuse, SSRF | No LLM-directed tool calls. Tools take `(panel, pos)`, with no paths or URLs. |
| Many provider API keys in env vars | Secret sprawl and leakage | Bedrock via an IAM role, so no API key. Payloads are redacted recursively before journaling. Tests fail on secret patterns. |
| Local memory and log files | Data exfiltration, poisoning | Writes are limited to `journals/ai/`, `reports/qe-ai/` and `backtest-data/ai_cache/`. Nothing is written under `reports/qe/` (gate evidence) or `journals/paper-*` (live-gate evidence). |

Full threat → control → test matrix: `docs/architecture/security-model.md`.

## 7. Dependency implications

- TradingAgents requires Python 3.12+, LangGraph/LangChain, several provider SDKs, yfinance and vendor clients.
- **QuantEmbrace adds none.** `qe.ai` uses only pydantic, pandas and numpy, which are already present. `boto3` is already pinned and is imported lazily, only in `qe/ai/llm/bedrock.py`.

## 8. Architecture differences (summary)

| Dimension | TradingAgents | QuantEmbrace `qe.ai` |
|---|---|---|
| Purpose | Produce a trade decision | Produce structured **research evidence** |
| Output | Prose → BUY/SELL/HOLD | `ResearchSignal` v1 (typed, bounded, per-component status) |
| Authority | The portfolio manager decides | The engine and human governance decide; AI weight defaults to 0 |
| Orchestration | LangGraph | Explicit DAG, deterministic, with replay from cache |
| Tools | Chosen by the LLM, networked | Chosen by code, read-only, point-in-time lake |
| Validation | Historical backtest of decisions | Forward shadow accrual only (contamination rule) |
| Observability | Logs and a markdown memory file | Hashed-config JSONL journal with every LLM and tool call recorded |

## 9. Honest expectation

With price-only data, the Phase 3–5 analysts can only re-describe what the quant factor model already sees. **The deliverable of Phases 0–5 is plumbing: safety, governance and traceability. It is not an edge.** Whether a multi-agent LLM layer adds anything must be decided by a pre-registered, forward, post-cutoff shadow test (Phase 6+), under the same discipline that governs every QuantEmbrace strategy.
