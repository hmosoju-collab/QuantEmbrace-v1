"""Research modes — the main cost lever (docs/architecture/hybrid-ai-system.md §5).

The regime analyst always runs once per run (market level). FAST skips the
debate and synthesizes deterministically (no LLM); STANDARD/DEEP add the
no-data analysts (which cost nothing today), a bull/bear debate, the critic,
and an LLM synthesizer.
"""

from dataclasses import dataclass
from typing import Literal

SYMBOL_ANALYSTS = ("technical", "risk", "fundamental", "news", "sentiment")


@dataclass(frozen=True)
class ModeSpec:
    analysts: tuple[str, ...]
    debate_rounds: int
    critic: bool
    synthesizer_tier: Literal["quick", "deep"] | None  # None ⇒ deterministic synthesis


MODES: dict[str, ModeSpec] = {
    "FAST": ModeSpec(("technical", "risk"), 0, False, None),
    "STANDARD": ModeSpec(SYMBOL_ANALYSTS, 1, True, "quick"),
    "DEEP": ModeSpec(SYMBOL_ANALYSTS, 2, True, "deep"),
}
