"""F-10: the research regime overlay must be point-in-time.

wf_v1.regime_series (verbatim v1 parity anchor) ranks its market proxy on
total-period turnover, so rewriting FUTURE turnover changes PAST regime values —
proven here to document the bug. pit_regime_series must be invariant to it.
"""

import numpy as np
import pandas as pd
import pytest

from qe.research import wf_v1
from qe.research.regime import pit_regime_series

K = 250  # rows [0, K] are "the past"; everything after is rewritten
SMA = 50


@pytest.fixture(scope="module")
def panel():
    rng = np.random.default_rng(11)
    dates = pd.date_range("2022-01-03", periods=420, freq="B", tz="Asia/Kolkata")
    syms = [f"S{i:03d}" for i in range(150)]  # > 100 so the top-100 proxy is selective
    # heterogeneous drifts so proxy membership actually matters
    drift = rng.normal(0.0, 0.002, len(syms))
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(drift + rng.normal(0, 0.02, (len(dates), len(syms))), 0)),
        index=dates,
        columns=syms,
    )
    turn = pd.DataFrame(rng.uniform(1e5, 1e6, close.shape), index=dates, columns=syms)
    return close, turn


def _future_rewritten(turn: pd.DataFrame) -> pd.DataFrame:
    """Make the currently least-liquid names dominate turnover AFTER row K."""
    out = turn.copy()
    quiet = turn.iloc[: K + 1].sum().nsmallest(60).index
    out.iloc[K + 1 :, out.columns.get_indexer(quiet)] *= 1_000.0
    return out


def test_v1_regime_leaks_future_turnover_into_the_past(panel):
    close, turn = panel
    before = wf_v1.regime_series(close, turn, SMA).iloc[: K + 1]
    after = wf_v1.regime_series(close, _future_rewritten(turn), SMA).iloc[: K + 1]
    assert not before.equals(after), "expected the verbatim v1 proxy to leak (F-10)"


def test_pit_regime_is_invariant_to_future_turnover(panel):
    close, turn = panel
    before = pit_regime_series(close, turn, SMA).iloc[: K + 1]
    after = pit_regime_series(close, _future_rewritten(turn), SMA).iloc[: K + 1]
    assert before.equals(after)


def test_pit_regime_is_invariant_to_future_prices(panel):
    close, turn = panel
    shocked = close.copy()
    shocked.iloc[K + 1 :] *= 0.2
    a = pit_regime_series(close, turn, SMA).iloc[: K + 1]
    b = pit_regime_series(shocked, turn, SMA).iloc[: K + 1]
    assert a.equals(b)


def test_pit_regime_shape(panel):
    close, turn = panel
    r = pit_regime_series(close, turn, SMA)
    assert r.index.equals(close.index) and r.dtype == bool
    assert not r.iloc[:SMA].any()  # no SMA yet ⇒ never "risk on"
    assert r.iloc[SMA + 70 :].any()  # it does switch on once history exists
