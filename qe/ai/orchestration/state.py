"""Frozen per-symbol research state accumulated by the graph."""

from dataclasses import dataclass, field
from typing import Any

from qe.ai.agents.base import AgentRun
from qe.ai.models import AgentObservation, ComponentStatus, Evidence
from qe.ai.tools.pit import ToolResult


@dataclass(frozen=True)
class DebateTurn:
    side: str  # bull | bear
    round: int
    run: AgentRun


@dataclass(frozen=True)
class SymbolState:
    symbol: str
    trace_id: str
    tools: dict[str, tuple[ToolResult, ...]]
    market_evidence: tuple[Evidence, ...] = ()  # regime facts shared by every symbol
    analysts: dict[str, AgentRun] = field(default_factory=dict)
    debate: tuple[DebateTurn, ...] = ()
    critic: AgentRun | None = None
    synthesis: AgentRun | None = None

    def evidence(self) -> tuple[Evidence, ...]:
        """Every OK tool fact for this symbol plus market-level evidence, unique by id."""
        seen: dict[str, Evidence] = {}
        for results in self.tools.values():
            for r in results:
                if r.ok:
                    for e in r.evidence:
                        seen.setdefault(e.evidence_id, e)
        for e in self.market_evidence:
            seen.setdefault(e.evidence_id, e)
        return tuple(seen.values())


def observation_block(obs: AgentObservation) -> dict[str, Any]:
    """An agent's view as passed to later agents — LLM text, so it is always
    wrapped as untrusted data by ``render_prompt``."""
    return {
        "agent": obs.agent_id,
        "status": obs.status,
        "score": obs.score,
        "risk_score": obs.risk_score,
        "confidence": obs.confidence,
        "summary": obs.summary,
        "points": list(obs.points),
    }


def ok(run: AgentRun | None) -> bool:
    return run is not None and run.observation.status is ComponentStatus.OK
