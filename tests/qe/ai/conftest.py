"""qe.ai test guards: no test may reach a network or construct an AWS client.

Every qe.ai test runs against the deterministic FakeLLM. These autouse patches
make an accidental real call fail loudly instead of spending money.
"""

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
