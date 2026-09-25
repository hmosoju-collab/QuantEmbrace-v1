"""Bull/bear debate: bounded rounds, each side answering the other's last turn."""

from collections.abc import Sequence
from typing import Any

from qe.ai.agents import DEBATERS, AgentContext, run_agent
from qe.ai.models import ComponentStatus, Evidence
from qe.ai.orchestration.state import DebateTurn, observation_block


def run_debate(
    ctx: AgentContext,
    *,
    symbol: str,
    evidence: Sequence[Evidence],
    analyst_blocks: Sequence[tuple[str, Any]],
    rounds: int,
) -> tuple[DebateTurn, ...]:
    turns: list[DebateTurn] = []
    for rnd in range(1, rounds + 1):
        for side in ("bull", "bear"):
            history = [
                (f"debate/{t.side}/{t.round}", observation_block(t.run.observation))
                for t in turns
                if t.run.observation.status is ComponentStatus.OK
            ]
            run = run_agent(
                DEBATERS[side],
                ctx,
                symbol=symbol,
                evidence=evidence,
                context_blocks=[*analyst_blocks, *history],
            )
            turns.append(DebateTurn(side, rnd, run))
    return tuple(turns)


def debate_status(turns: Sequence[DebateTurn]) -> ComponentStatus:
    """OK only when both sides produced at least one OK turn."""
    if not turns:
        return ComponentStatus.SKIPPED
    sides_ok = {t.side for t in turns if t.run.observation.status is ComponentStatus.OK}
    if sides_ok == {"bull", "bear"}:
        return ComponentStatus.OK
    failed = [
        t.run.observation.status
        for t in turns
        if t.run.observation.status is not ComponentStatus.OK
    ]
    return failed[0] if failed else ComponentStatus.ERROR
