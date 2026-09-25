"""Read-only loader over the existing Parquet OHLCV lake.

Layout (produced by scripts/backtest/download_bhavcopy.py and friends):

    {lake_root}/ohlcv/market={M}/segment={S}/symbol={SYM}/interval={I}/year={Y}/part-*.parquet

Fail-closed by default: a symbol with no files in the requested range is an
error, not a silent gap. Timestamps in the lake are tz-aware Asia/Kolkata;
date filtering happens on the IST trading date.
"""

from datetime import date
from pathlib import Path

import pandas as pd

IST = "Asia/Kolkata"


class LakeError(RuntimeError):
    pass


class OhlcvLake:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        if not (self.root / "ohlcv").is_dir():
            raise LakeError(f"not an OHLCV lake (no ohlcv/ dir): {self.root}")

    def _symbol_dir(self, market: str, segment: str, symbol: str, interval: str) -> Path:
        return (
            self.root
            / "ohlcv"
            / f"market={market}"
            / f"segment={segment}"
            / f"symbol={symbol}"
            / f"interval={interval}"
        )

    def resolve_files(
        self,
        symbols: tuple[str, ...] | list[str],
        start: date,
        end: date,
        *,
        market: str = "NSE",
        segment: str = "EQ",
        interval: str = "1d",
    ) -> list[Path]:
        """Return every Parquet file the query touches; fail-closed per symbol.

        This exact file list is what a data snapshot pins — the snapshot covers
        precisely the bytes the run can read.
        """
        years = range(start.year, end.year + 1)
        files: list[Path] = []
        missing: list[str] = []
        for symbol in symbols:
            base = self._symbol_dir(market, segment, symbol, interval)
            symbol_files = [
                f for year in years for f in sorted((base / f"year={year}").glob("part-*.parquet"))
            ]
            if not symbol_files:
                missing.append(symbol)
            files.extend(symbol_files)
        if missing:
            raise LakeError(
                f"no lake data for {len(missing)} symbol(s) in {start}..{end} "
                f"({market}/{segment}/{interval}): {', '.join(sorted(missing))}"
            )
        return files

    def load_bars(
        self,
        symbols: tuple[str, ...] | list[str],
        start: date,
        end: date,
        *,
        market: str = "NSE",
        segment: str = "EQ",
        interval: str = "1d",
        files: list[Path] | None = None,
    ) -> pd.DataFrame:
        """Load bars for symbols in [start, end] (inclusive, IST dates),
        sorted by (timestamp, symbol). Pass ``files`` to reuse a resolved
        (snapshot-pinned) file list."""
        if files is None:
            files = self.resolve_files(
                symbols, start, end, market=market, segment=segment, interval=interval
            )
        frames = [pd.read_parquet(f) for f in files]
        df = pd.concat(frames, ignore_index=True)
        ist_date = df["timestamp"].dt.tz_convert(IST).dt.date
        df = df[(ist_date >= start) & (ist_date <= end)]
        return df.sort_values(["timestamp", "symbol"], ignore_index=True)
