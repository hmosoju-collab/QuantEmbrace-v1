"""Strategy interface: pure functions over point-in-time market state.

A strategy sees a PIT-sliced view of the panel (nothing after "now") and emits
target weights. It knows nothing about brokers, NAV, paper/live mode, or
orders — sizing, netting, risk, and execution are downstream layers.
"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol

import pandas as pd

from qe.data.panel import Panel


@dataclass(frozen=True)
class Context:
    """Point-in-time view handed to strategies. ``now_pos`` is the last valid
    row; the panel frames are sliced so nothing after it is visible."""

    close: pd.DataFrame
    turnover: pd.DataFrame
    delivery: pd.DataFrame
    now_pos: int
    now_date: date

    @classmethod
    def at(cls, panel: Panel, pos: int) -> "Context":
        return cls(
            close=panel.close.iloc[: pos + 1],
            turnover=panel.turnover.iloc[: pos + 1],
            delivery=panel.delivery.iloc[: pos + 1],
            now_pos=pos,
            now_date=panel.date_at(pos),
        )


class Strategy(Protocol):
    """A positional strategy: called at each rebalance point, returns target
    weights (fractions of NAV). Unmentioned symbols mean zero; weights summing
    below 1.0 leave the remainder in cash."""

    name: str

    def rebalance(self, ctx: Context) -> dict[str, float]: ...
