# AI / Quant Boundary

> ADR-043. This document states the rules that keep `qe.ai` advisory. **Each rule names the test that enforces it.** The tests are `[PLANNED — not yet implemented]` until Phase 2 lands (see [hybrid-ai-system.md §6](hybrid-ai-system.md)).

## 1. Layering

```
AI Layer (qe.ai.agents, qe.ai.orchestration)
   ↓ structured observations only
Research API (qe.ai.tools.pit.ResearchDataAPI — read-only, point-in-time)
   ↓ reads
Quant Engine data (qe.data, qe.universe, qe.strategy.base.Context)
   ✗ no edge to ↓
Risk Engine (qe.risk) → Execution (qe.engine.core, qe.execution) → Broker port (Sim/Paper/LiveBroker)
```

**There is no edge from `qe.ai` to `qe.risk`, `qe.engine`, `qe.execution`, `qe.killswitch`, `qe.live_gate` or any broker.** An "AI → Broker" dependency cannot exist, because the only broker surface (`qe.execution`) is not importable from `qe.ai`.

## 2. Import rules

| Rule | Enforced by |
|---|---|
| `qe.ai` may import from `qe` **only**: `qe.config`, `qe.journal`, `qe.data.{lake,panel,snapshot,feed}`, `qe.strategy.base`, `qe.universe`, `qe.clock`, `qe.version`, and `qe.ai.*`. It is an allowlist, not a denylist. | `tests/qe/ai/test_ai_boundary.py` (AST walk of every `qe/ai/**/*.py`) |
| `qe.ai` must not import `qe.research` (its `__init__` pulls `study → engine → paper → execution`), `qe.engine`, `qe.execution`, `qe.risk`, `qe.portfolio`, `qe.killswitch`, `qe.live_gate`, `qe.cli`, `qe.strategy.factor_book`/`risk_parity` (strategy objects), or `services.*` | same |
| `qe.ai` source must not import `subprocess`, `socket`, `requests`, `urllib`, `httpx`, `http.client`, `kiteconnect`, `alpaca*`, `anthropic`, `openai`, `langchain*` or `langgraph*` | same (AST; checked on source because pandas and boto3 load some of these transitively) |
| `boto3` / `botocore` may appear **only** in `qe/ai/llm/bedrock.py`, imported lazily inside a function | same |
| No trading module imports `qe.ai`. Scanned: `qe/engine`, `qe/execution.py`, `qe/risk.py`, `qe/portfolio.py`, `qe/killswitch.py`, `qe/live_gate.py`, `qe/strategy`, `qe/cli.py`, `qe/research`. | same (AST walk of the reverse direction) |
| Importing every `qe.ai` module in a **fresh interpreter** leaves `qe.execution`, `qe.engine`, `qe.risk`, `qe.live_gate`, `kiteconnect` and `alpaca` absent from `sys.modules` | same (subprocess-isolated check) |

## 3. Write rules

| `qe.ai` may write | `qe.ai` must never write |
|---|---|
| `journals/ai/<run_id>.jsonl` (research journal) | `journals/paper-*.jsonl` (read by `qe.live_gate` as clean-session evidence) |
| `reports/qe-ai/<run_id>/…` (derived views) | anything under `reports/qe/` (read by `qe.live_gate` and `check_forward_gate.py` as gate evidence) |
| `backtest-data/ai_cache/…` (LLM response cache) | `backtest-data/paper_book/*` (book state, kill flag), `governance/live-gate/*`, `governance/experiment-registry.jsonl` (P6 will register hypotheses there only through `qe.research.registry`, called by a human-run study) |
| `backtest-data/lake/_snapshots/` **only through** `qe.data.snapshot.create_snapshot` (provenance, the same mechanism the engine uses) | orders, positions, risk limits, configs, broker credentials, account settings |

All `qe.ai` writes go through `qe.ai.paths.safe_write_path`, which refuses the forbidden locations. Enforced by `test_ai_boundary.py`, `test_ai_cli.py` (an end-to-end run asserts nothing new under `reports/qe/` or `journals/paper-*`), and `test_ai_engine_untouched.py`.

## 4. Decision rules

| Rule | Enforced by |
|---|---|
| `qe.ai` exposes no function or method named or behaving like `place_order`, `submit_order`, `cancel_order`, `set_position`, `set_risk_limit`, `activate`/`deactivate` (kill), `promote`, `go_live` | `test_ai_boundary.py` (public-API scan) |
| In `AI_DISABLED` and `AI_ADVISORY` modes, the fused decision and score are **independent of every AI input** | `test_ai_fusion.py` (hypothesis property test) |
| In ADVISORY mode, `SELECT` exactly equals the engine's own pick (`FactorBookStrategy.rebalance`) at every rebalance | `test_ai_fusion_parity.py` |
| A hard risk flag always yields `REJECT`, whatever the AI score, confidence or mode | `test_ai_fusion.py` |
| `AI_WEIGHTED` / `AI_EXPERIMENTAL` are refused outside the `study` context. AI weight is capped in code (0.20 / 0.50). | `test_ai_fusion.py` |
| Contaminated signals (decision date ≤ model knowledge cutoff + guard) never carry weight | `test_ai_fusion.py`, `test_ai_schemas.py` |
| AI alone never decides: no quant score means `NO_DECISION` | `test_ai_fusion.py` |

## 5. Engine invariance

`qe.ai` does not modify `qe/config.py`, `qe/strategy/*`, `qe/engine/*`, `qe/execution.py`, `qe/risk.py`, `qe/portfolio.py`, `qe/killswitch.py`, `qe/live_gate.py` or `qe/cli.py`.

`test_ai_engine_untouched.py` asserts:
- The paper book configs' `config_hash` values are unchanged: delivery `7c95f33da911…` and momentum `d377a933b653…`, matching the stored book states (ADR-042).
- The full `tests/qe` suite still passes, including the ₹0.00 parity tests.
