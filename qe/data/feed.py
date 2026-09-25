"""Live data feed for paper/live mode — the WallClock counterpart to the
snapshot-pinned historical panel used in backtest.

For a monthly EOD factor book, "live" legitimately means "the freshest EOD data
in the lake" — there is no intraday tick to react to. Each paper session still
pins a snapshot of exactly what it saw (provenance), so a paper decision is as
reproducible as a backtest one.
"""

from datetime import date
from pathlib import Path

import pandas as pd

from qe.data.panel import Panel, load_panel, resolve_panel_files
from qe.data.snapshot import create_snapshot

# Reference symbol used only to find the lake's freshest trading date
# (`LiveLakeFeed.latest_date`) — must exist under that market's segment.
_MARKET_REFERENCE_SYMBOL = {"NSE": "RELIANCE", "US": "SPY"}


def reference_symbol_for_market(market: str) -> str:
    """Fail closed on an unregistered market rather than silently defaulting
    to an NSE symbol that won't exist under a different market's lake path
    (ADR-041 P5)."""
    try:
        return _MARKET_REFERENCE_SYMBOL[market]
    except KeyError:
        raise ValueError(
            f"no reference symbol registered for market={market!r} "
            f"(known: {sorted(_MARKET_REFERENCE_SYMBOL)})"
        ) from None


class LiveLakeFeed:
    """Reads the current lake state up to a given (or latest) trading date."""

    def __init__(
        self,
        lake_root: str | Path,
        *,
        market: str = "NSE",
        segment: str = "EQ",
        interval: str = "1d",
        reference_symbol: str = "RELIANCE",
    ):
        self.lake_root = Path(lake_root)
        self.market = market
        self.segment = segment
        self.interval = interval
        self.reference_symbol = reference_symbol

    def latest_date(self) -> date:
        base = (
            self.lake_root
            / "ohlcv"
            / f"market={self.market}"
            / f"segment={self.segment}"
            / f"symbol={self.reference_symbol}"
            / f"interval={self.interval}"
        )
        files = sorted(base.glob("year=*/part-*.parquet"))
        if not files:
            raise FileNotFoundError(f"no data for reference symbol under {base}")
        ts = pd.read_parquet(files[-1], columns=["timestamp"])["timestamp"].max()
        return pd.Timestamp(ts).date()

    def load(self, warmup_start: date, as_of: date) -> tuple[Panel, str]:
        files = resolve_panel_files(
            self.lake_root,
            warmup_start,
            as_of,
            market=self.market,
            segment=self.segment,
            interval=self.interval,
        )
        manifest = create_snapshot(
            self.lake_root,
            files,
            {
                "feed": "live_lake",
                "market": self.market,
                "segment": self.segment,
                "interval": self.interval,
                "warmup_start": str(warmup_start),
                "as_of": str(as_of),
            },
        )
        panel = load_panel(files, warmup_start, as_of)
        return panel, manifest["snapshot_id"]
