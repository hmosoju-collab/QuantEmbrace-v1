"""AlphaRanker — cross-sectional ranking + publication gating (shadow mode).

Per cycle (ADR-031):
  1. Drop expired forecasts (``cycle_ts - decision_ts > ttl``).
  2. Tag same-symbol opposite-direction forecasts with a shared
     ``conflict_group_id`` — BOTH sides are kept (#7): shadow mode maximizes
     learning, so research can later compute which alpha family wins a conflict.
  3. score = ``confidence * net_edge_bps`` (Phase 1; z-score/percentile/
     sector-neutral are the documented roadmap, not built here).
  4. Publication gate: only forecasts with ``net_edge_bps >= min_net_edge_bps``
     (default 50) and within the top-N by score are published to
     ``alpha.opportunities``. Everything else is still returned (and persisted by
     the caller) with a ``suppressed_by`` reason — all forecasts are stored.

The ranker is pure: it does not know about the kill switch (the service gates
publishing) and performs no I/O. ``cycle_id`` is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib

from shared.models.alpha import AlphaForecast, AlphaOpportunity
from shared.models.signal import Direction

SUPPRESS_EDGE_FLOOR = "edge_floor"
SUPPRESS_TOP_N_CAP = "top_n_cap"


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


@dataclass(frozen=True)
class RankResult:
    """Outcome of one ranking cycle.

    ``opportunities`` contains every surviving (non-expired) forecast wrapped as
    an ``AlphaOpportunity`` with ``rank``/``score``/``published``/``suppressed_by``
    set, sorted by score descending. The caller persists all of them and publishes
    only ``published_opportunities``.
    """

    cycle_id: str
    cycle_ts: datetime
    opportunities: list[AlphaOpportunity]
    expired_count: int

    @property
    def published_opportunities(self) -> list[AlphaOpportunity]:
        return [o for o in self.opportunities if o.published]


class AlphaRanker:
    def __init__(
        self,
        *,
        top_n: int = 10,
        min_net_edge_bps: float = 50.0,
        forecast_ttl_seconds: int = 180,
    ) -> None:
        self._top_n = top_n
        self._min_net_edge_bps = min_net_edge_bps
        self._ttl_seconds = forecast_ttl_seconds

    def rank(self, forecasts: list[AlphaForecast], cycle_ts: datetime) -> RankResult:
        cycle_ts = _aware(cycle_ts)

        # 1. TTL filter
        survivors: list[AlphaForecast] = []
        expired = 0
        for f in forecasts:
            age = (cycle_ts - _aware(f.decision_ts)).total_seconds()
            if age > self._ttl_seconds:
                expired += 1
            else:
                survivors.append(f)

        # 2. Conflict tagging — same (market, symbol) with both BUY and SELL present
        conflict_ids = self._conflict_group_ids(survivors, cycle_ts)

        # cycle_id is deterministic over the surviving forecast ids
        cycle_id = _make_cycle_id(cycle_ts, [f.forecast_id for f in survivors])

        # 3. Score + sort (tie-break on forecast_id for determinism)
        scored = sorted(
            survivors,
            key=lambda f: (-(f.confidence * f.net_edge_bps), f.forecast_id),
        )

        # 4. Publication gate (edge floor + top-N)
        opportunities: list[AlphaOpportunity] = []
        published_count = 0
        for idx, f in enumerate(scored):
            score = f.confidence * f.net_edge_bps
            eligible = f.net_edge_bps >= self._min_net_edge_bps
            if not eligible:
                published, suppressed = False, SUPPRESS_EDGE_FLOOR
            elif published_count < self._top_n:
                published, suppressed = True, ""
                published_count += 1
            else:
                published, suppressed = False, SUPPRESS_TOP_N_CAP
            opportunities.append(
                AlphaOpportunity(
                    forecast=f,
                    rank=idx + 1,
                    score=score,
                    cycle_id=cycle_id,
                    cycle_ts=cycle_ts,
                    conflict_group_id=conflict_ids.get(f.forecast_id),
                    published=published,
                    suppressed_by=suppressed,
                )
            )

        return RankResult(
            cycle_id=cycle_id,
            cycle_ts=cycle_ts,
            opportunities=opportunities,
            expired_count=expired,
        )

    @staticmethod
    def _conflict_group_ids(
        forecasts: list[AlphaForecast], cycle_ts: datetime
    ) -> dict[str, str]:
        """Map forecast_id -> conflict_group_id for symbols showing disagreement.

        A symbol is "in conflict" this cycle when at least one BUY and one SELL
        forecast exist for it (across any model/horizon). Every forecast of such a
        symbol is tagged with the same deterministic group id.
        """
        by_key: dict[tuple[str, str], set[Direction]] = {}
        for f in forecasts:
            by_key.setdefault((f.market, f.symbol), set()).add(f.direction)

        conflicted_keys = {
            key for key, dirs in by_key.items()
            if Direction.BUY in dirs and Direction.SELL in dirs
        }

        result: dict[str, str] = {}
        for f in forecasts:
            key = (f.market, f.symbol)
            if key in conflicted_keys:
                raw = f"{f.market}|{f.symbol}|{cycle_ts.isoformat()}"
                result[f.forecast_id] = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return result


def _make_cycle_id(cycle_ts: datetime, forecast_ids: list[str]) -> str:
    raw = cycle_ts.isoformat() + "|" + "|".join(sorted(forecast_ids))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
