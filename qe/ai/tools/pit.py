"""ResearchDataAPI — the only door from qe.ai to market data (point-in-time).

Tools never see a file path, URL, or anything past the decision row: they get
a ``ResearchDataAPI`` wrapping ``Context.at(panel, pos)`` — the exact PIT slice
the engine's strategies see. Every fact they emit is stamped with
``knowledge_ts = decision_date @ market close`` (docs/research/
lookahead-prevention.md §1.2), so a bar is never "known" before its close.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from qe.ai.models import ComponentStatus, Evidence
from qe.clock import market_close_time, market_tz
from qe.config import RunConfig
from qe.data.lake import LakeError, OhlcvLake
from qe.data.panel import Panel, load_panel, resolve_panel_files
from qe.data.snapshot import create_snapshot
from qe.strategy.base import Context

TOOLS_VERSION = "qe_ai_tools/1"
INDEX_SYMBOLS = {"NSE": ("INDIAVIX",)}
WARMUP_DAYS = 730  # 2y: covers 252d momentum + SMA200 regime proxy


def knowledge_ts(d: date, market: str) -> datetime:
    return datetime.combine(d, market_close_time(market), tzinfo=market_tz(market))


@dataclass(frozen=True)
class ResearchDataAPI:
    panel: Panel
    pos: int
    market: str
    # Market-level series (e.g. INDIAVIX close) indexed by trading date. Read
    # through ``index_series`` only, which truncates at the decision date.
    index_bars: dict[str, pd.Series] = field(default_factory=dict)

    @classmethod
    def at(
        cls,
        panel: Panel,
        as_of: datetime,
        market: str,
        index_bars: dict[str, pd.Series] | None = None,
    ) -> "ResearchDataAPI":
        """Last row knowable at ``as_of``: a request before today's close sees
        yesterday — the same-day bar is not knowable until the close."""
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        pos = None
        for i, ts in enumerate(panel.index):
            if knowledge_ts(pd.Timestamp(ts).date(), market) <= as_of:
                pos = i
        if pos is None:
            raise ValueError(f"no panel row is knowable at {as_of.isoformat()}")
        return cls(panel, pos, market, dict(index_bars or {}))

    @property
    def decision_date(self) -> date:
        return self.panel.date_at(self.pos)

    @property
    def cutoff(self) -> datetime:
        return knowledge_ts(self.decision_date, self.market)

    def context(self) -> Context:
        return Context.at(self.panel, self.pos)

    def index_series(self, name: str) -> pd.Series | None:
        """PIT series ending exactly at the decision date, else None — a stale
        index value is UNAVAILABLE, never forward-filled."""
        s = self.index_bars.get(name)
        if s is None:
            return None
        s = s[s.index <= self.decision_date]
        if s.empty or s.index[-1] != self.decision_date:
            return None
        return s


@dataclass(frozen=True)
class ToolResult:
    tool: str
    symbol: str | None
    status: ComponentStatus  # OK | UNAVAILABLE
    evidence: tuple[Evidence, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status is ComponentStatus.OK

    def journal_view(self) -> dict:
        return {
            "tool": self.tool,
            "symbol": self.symbol,
            "status": self.status,
            "reason": self.reason,
            "evidence": [
                {"id": e.evidence_id, "value": e.value, "hash": e.content_hash}
                for e in self.evidence
            ],
        }


def evidence(
    api: ResearchDataAPI, tool: str, name: str, value, summary: str, symbol: str | None
) -> Evidence:
    if isinstance(value, float):
        value = round(value, 6)
    return Evidence(
        evidence_id=f"{tool}.{name}",
        tool=tool,
        symbol=symbol,
        knowledge_ts=api.cutoff,
        value=value,
        summary=summary,
    )


def unavailable(tool: str, symbol: str | None, reason: str) -> ToolResult:
    return ToolResult(tool, symbol, ComponentStatus.UNAVAILABLE, (), reason)


@dataclass(frozen=True)
class ResearchData:
    panel: Panel
    snapshot_id: str
    index_bars: dict[str, pd.Series]


def load_research_data(
    book: RunConfig, as_of: date, base_dir: str | Path = ".", *, start: date | None = None
) -> ResearchData:
    """Load the PIT panel for [start or as_of - 2y, as_of] exactly as the engine
    does, plus market-level index closes when the lake has them, and pin one data
    snapshot covering every file read (provenance for the research journal)."""
    base_dir = Path(base_dir)
    lake_root = base_dir / book.data.lake_root
    market = book.universe.market
    start = start or as_of - timedelta(days=WARMUP_DAYS)
    files = resolve_panel_files(
        lake_root,
        start,
        as_of,
        market=market,
        segment=book.universe.segment,
        interval=book.data.interval,
    )
    index_bars: dict[str, pd.Series] = {}
    index_files = []
    for sym in INDEX_SYMBOLS.get(market, ()):
        try:
            lake = OhlcvLake(lake_root)
            sym_files = lake.resolve_files([sym], start, as_of, market=market, segment="INDICES")
            bars = lake.load_bars(
                [sym], start, as_of, market=market, segment="INDICES", files=sym_files
            )
        except (LakeError, FileNotFoundError):
            continue
        dates = pd.to_datetime(bars["timestamp"]).dt.tz_convert(str(market_tz(market))).dt.date
        index_bars[sym] = pd.Series(bars["close"].to_numpy(), index=dates).groupby(level=0).last()
        index_files.extend(sym_files)
    manifest = create_snapshot(
        lake_root,
        files + index_files,
        {
            "feed": "qe_ai_research",
            "market": market,
            "segment": book.universe.segment,
            "interval": book.data.interval,
            "panel_start": str(start),
            "as_of": str(as_of),
            "index_symbols": sorted(index_bars),
        },
    )
    panel = load_panel(files, start, as_of, tz=str(market_tz(market)))
    return ResearchData(panel, manifest["snapshot_id"], index_bars)
