"""qe.ai test guards: no test may reach a network or construct an AWS client.

Every qe.ai test runs against the deterministic FakeLLM. These autouse patches
make an accidental real call fail loudly instead of spending money.
"""

from pathlib import Path
import socket

import numpy as np
import pandas as pd
import pytest

from qe.data.panel import Panel


def make_panel(n_days: int = 520, n_syms: int = 60, seed: int = 5) -> Panel:
    """Same construction as the engine parity fixture (tests/qe/test_parity_*)."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-06-01", periods=n_days, freq="B", tz="Asia/Kolkata")
    syms = [f"S{i:03d}" for i in range(n_syms)]
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0.0003, 0.018, (n_days, n_syms)), 0)),
        index=dates,
        columns=syms,
    )
    turn = close * pd.DataFrame(rng.uniform(1e5, 1e6, close.shape), index=dates, columns=syms)
    deliv = pd.DataFrame(rng.uniform(20, 80, close.shape), index=dates, columns=syms)
    return Panel(close=close, turnover=turn, delivery=deliv)


@pytest.fixture()
def synthetic_panel() -> Panel:
    return make_panel()


def write_lake(root: Path, panel: Panel) -> Path:
    """Materialize a panel as an on-disk lake in the real bhavcopy layout."""
    for sym in panel.close.columns:
        close = panel.close[sym]
        df = pd.DataFrame(
            {
                "timestamp": close.index + pd.Timedelta(hours=15, minutes=30),
                "symbol": sym,
                "market": "NSE",
                "segment": "EQ",
                "interval": "1d",
                "close": close.to_numpy(),
                "volume": (panel.turnover[sym] / close).to_numpy(),
                "delivery_pct": panel.delivery[sym].to_numpy(),
                "source": "synthetic",
                "trust_level": "TEST",
            }
        )
        for year, part in df.groupby(df["timestamp"].dt.year):
            d = (
                root / "ohlcv" / "market=NSE" / "segment=EQ" / f"symbol={sym}" / "interval=1d"
                / f"year={year}"
            )  # fmt: skip
            d.mkdir(parents=True, exist_ok=True)
            part.to_parquet(d / "part-0.parquet", index=False)
    return root


def _refuse(*_args, **_kwargs):
    raise RuntimeError("network / AWS access is forbidden in qe.ai tests")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    try:
        import boto3
    except ImportError:
        return
    monkeypatch.setattr(boto3, "client", _refuse)


@pytest.fixture()
def synthetic_lake(tmp_path, synthetic_panel) -> Path:
    return write_lake(tmp_path / "lake", synthetic_panel)
