"""qe.ai — offline, advisory AI research layer (ADR-043).

TradingAgents-inspired analyst/debate agents that produce typed research
records (``ResearchSignal`` v1) about what the qe engine is doing. This package
has no authority: it cannot place orders, touch positions, risk limits, configs,
book state, or promote anything. The trading engine never imports it, and it
never imports engine / risk / execution / live-gate / broker code — enforced by
``tests/qe/ai/test_ai_boundary.py``.

Entry point: ``python -m qe.ai`` (never through ``python -m qe``).
"""
