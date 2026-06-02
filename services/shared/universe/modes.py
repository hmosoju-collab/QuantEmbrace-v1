"""
Universe modes — define the three supported trading universe stages.

Usage:
    from shared.universe.modes import UniverseMode

    mode = UniverseMode.PAPER_SAFE_START
    if mode.is_paper:
        ...  # safe to use dummy credentials
"""

from __future__ import annotations

from enum import Enum


class UniverseMode(str, Enum):
    """
    Trading universe stage.

    The three modes define which symbols may be traded and how strict
    the liquidity/risk filters are.

    Promotion path (requires gate evaluation):
        PAPER_SAFE_START  →  PAPER_EXPAND  →  LIVE_ADVANCED

    Rules:
    - Paper modes use PAPER_SAFE_START or PAPER_EXPAND snapshots.
    - Live mode uses LIVE_ADVANCED snapshots exclusively.
    - Paper and live snapshots are stored under different namespaces in DynamoDB.
    - Mode must be explicitly set in environment — there is no automatic promotion.
    """

    PAPER_SAFE_START = "PAPER_SAFE_START"
    PAPER_EXPAND = "PAPER_EXPAND"
    LIVE_ADVANCED = "LIVE_ADVANCED"

    @property
    def is_paper(self) -> bool:
        """True for paper trading modes (PAPER_SAFE_START, PAPER_EXPAND)."""
        return self in (UniverseMode.PAPER_SAFE_START, UniverseMode.PAPER_EXPAND)

    @property
    def is_live(self) -> bool:
        """True only for live trading mode (LIVE_ADVANCED)."""
        return self is UniverseMode.LIVE_ADVANCED

    @property
    def dynamo_namespace(self) -> str:
        """DynamoDB key namespace — paper and live are always separate."""
        return "PAPER" if self.is_paper else "LIVE"

    @property
    def can_promote_to(self) -> "UniverseMode | None":
        """Next mode in the promotion path, or None if already at LIVE_ADVANCED."""
        _next = {
            UniverseMode.PAPER_SAFE_START: UniverseMode.PAPER_EXPAND,
            UniverseMode.PAPER_EXPAND: UniverseMode.LIVE_ADVANCED,
        }
        return _next.get(self)

    @classmethod
    def from_string(cls, value: str) -> "UniverseMode":
        """Parse a mode string (case-insensitive).

        Raises:
            ValueError: If the string does not match any mode.
        """
        try:
            return cls(value.upper())
        except ValueError:
            valid = [m.value for m in cls]
            raise ValueError(
                f"Unknown UniverseMode '{value}'. Valid values: {valid}"
            )
