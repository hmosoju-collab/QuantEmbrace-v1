"""Wide cross-sectional panel loader (date x symbol matrices) over the lake.

Semantics ported from `scripts/backtest/run_factor_study._load_panel` (the
validated loader behind every 2026 factor study): IST-normalized daily index,
turnover = close * volume, pivot-table wide frames.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from qe.data.lake import IST


@dataclass(frozen=True)
class Panel:
    """Point-in-time cross-sectional daily panel."""

    close: pd.DataFrame
    turnover: pd.DataFrame
    delivery: pd.DataFrame

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.close.index

    def date_at(self, pos: int) -> date:
        # The index is already normalized to the market's own trading-date tz
        # by load_panel(tz=...) — re-converting to IST here would mislabel
        # non-IST panels (ADR-041 P3), so just read the tz-aware value as-is.
        return pd.Timestamp(self.index[pos]).date()


def resolve_panel_files(
    lake_root: str | Path,
    start: date,
    end: date,
    *,
    market: str = "NSE",
    segment: str = "EQ",
    interval: str = "1d",
) -> list[Path]:
    """Every Parquet file a whole-segment panel query touches (snapshot scope)."""
    base = Path(lake_root) / "ohlcv" / f"market={market}" / f"segment={segment}"
    files = [
        f
        for year in range(start.year, end.year + 1)
        for f in sorted(base.glob(f"symbol=*/interval={interval}/year={year}/part-*.parquet"))
    ]
    if not files:
        raise FileNotFoundError(f"no panel data under {base} for {start.year}..{end.year}")
    return files


def load_panel(files: list[Path], start: date, end: date, *, tz: str = IST) -> Panel:
    """Load wide close/turnover/delivery matrices for [start, end].

    ``tz`` is the market's trading-date timezone (default IST for the NSE lake;
    pass "America/New_York" for market=US files — ADR-041).
    """
    import pyarrow.dataset as ds

    table = ds.dataset([str(f) for f in files], format="parquet").to_table(
        columns=["timestamp", "symbol", "close", "volume", "delivery_pct"]
    )
    df = table.to_pandas()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(tz).dt.normalize()
    df = df[(df["date"] >= pd.Timestamp(start, tz=tz)) & (df["date"] <= pd.Timestamp(end, tz=tz))]
    df["turnover"] = df["close"] * df["volume"]

    close = df.pivot_table(index="date", columns="symbol", values="close")
    turn = df.pivot_table(index="date", columns="symbol", values="turnover")
    deliv = df.pivot_table(index="date", columns="symbol", values="delivery_pct")
    return Panel(close.sort_index(), turn.sort_index(), deliv.sort_index())
