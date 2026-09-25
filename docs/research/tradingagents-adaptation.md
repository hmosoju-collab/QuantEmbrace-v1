# TradingAgents Adaptation (pointer)

The canonical analysis is **[docs/tradingagents/adaptation-analysis.md](../tradingagents/adaptation-analysis.md)**. It covers what is reused, adapted and rejected, the conflicts with QuantEmbrace, and the security and dependency implications.

Short version:
- **Reused as ideas:** specialist analysts, bull/bear debate, critic/judge, quick/deep model tiers, bounded rounds.
- **Replaced:**
  - free-text decisions → the typed `ResearchSignal` v1;
  - LLM tool-calling → code-called point-in-time tools;
  - LLM portfolio-manager authority → deterministic fusion with AI weight 0 by default.
- **Rejected:**
  - trader and portfolio-manager authority;
  - LangGraph/LangChain;
  - reflection memory (a look-ahead leak);
  - unvetted news and social tools;
  - historical backtests of LLM decisions as evidence (training-data contamination).
- **No code copied.** TradingAgents is Apache-2.0 and was used as a design reference only.
