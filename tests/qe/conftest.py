"""Fixture lake matching the real bhavcopy lake schema/layout exactly."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from qe.config import DataConfig, RunConfig, UniverseConfig


def _make_bars(symbol: str, year: int, n: int = 10) -> pd.DataFrame:
    ts = pd.date_range(f"{year}-01-01 15:30", periods=n, freq="B", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "symbol": symbol,
            "isin": f"INE{abs(hash(symbol)) % 10**6:06d}A0000",
            "market": "NSE",
            "segment": "EQ",
            "interval": "1d",
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1000 + i for i in range(n)],
            "prev_close": [100.0 + i for i in range(n)],
            "delivery_qty": None,
            "delivery_pct": None,
            "source": "bhavcopy",
            "trust_level": "HIGH",
        }
    )


@pytest.fixture()
def fixture_lake(tmp_path: Path) -> Path:
    """Lake with TESTA (2023+2024) and TESTB (2024 only)."""
    lake = tmp_path / "lake"
    for symbol, years in {"TESTA": (2023, 2024), "TESTB": (2024,)}.items():
        for year in years:
            part_dir = (
                lake
                / "ohlcv"
                / "market=NSE"
                / "segment=EQ"
                / f"symbol={symbol}"
                / "interval=1d"
                / f"year={year}"
            )
            part_dir.mkdir(parents=True)
            _make_bars(symbol, year).to_parquet(part_dir / "part-0.parquet", index=False)
    return lake


@pytest.fixture()
def run_config(fixture_lake: Path) -> RunConfig:
    return RunConfig(
        name="m1-test",
        mode="null",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        universe=UniverseConfig(symbols=("TESTA", "TESTB")),
        data=DataConfig(lake_root=str(fixture_lake)),
        journal_dir=str(fixture_lake.parent / "journals"),
    )
