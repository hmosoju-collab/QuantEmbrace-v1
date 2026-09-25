"""ADR-041 Phase 4 — qe RPLITE port parity against the pandas screen harness
that was itself already validated against LEAN (Phase 2b, RMSE 3.4bps). Two
layers, mirroring exactly how the Phase 2b root-cause was done manually:

1. **Weight-target parity** — `RiskParityLiteStrategy.rebalance` must produce
   the identical inverse-vol weights as `w_rplite`, to float precision, at
   every rebalance point (pure logic, no engine mechanics involved).
2. **Zero-cost NAV-path parity** — with both engines' costs zeroed out (so
   only rebalance-timing/weight-drift/share-rounding mechanics are compared,
   exactly as the Phase 2b LEAN root-cause did), qe's simulated NAV path must
   match the pandas `run_weights` reference to a tight tolerance.

Together these transitively establish qe-vs-LEAN parity without re-running
Docker: qe ≈ pandas screen (proven here) and pandas screen ≈ LEAN (proven in
Phase 2b, `docs/strategy/us-lean-crosscheck-p2b-report.md`).
"""

from datetime import date
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

from qe.config import DataConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.costs import USEquityCosts
from qe.data.panel import Panel
from qe.engine.sim import run_sim
from qe.execution import SimBroker
from qe.strategy.base import Context
from qe.strategy.risk_parity import RiskParityLiteStrategy

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS_BT = _REPO / "scripts" / "backtest"
if str(_SCRIPTS_BT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_BT))

v1 = pytest.importorskip("run_us_rotation_study")

ASSETS = ("SPY", "TLT", "GLD")
VOL_LOOKBACK = 63
SEED = 1_000_000.0
INCEPTION = date(2024, 1, 1)


@pytest.fixture(scope="module")
def synthetic_px() -> pd.DataFrame:
    """~2.5y of business days so several post-warmup rebalances exist.

    Tz-NAIVE index — this mirrors `run_us_rotation_study.load_adj_close`,
    whose ``.dt.date`` extraction produces a tz-naive index even though the
    lake timestamps are tz-aware. qe's own Panel is genuinely tz-aware
    (`synthetic_panel` below); the two must represent the *same* calendar
    dates for the parity comparison to mean anything."""
    rng = np.random.default_rng(41)
    dates = pd.date_range("2023-01-02", periods=650, freq="B")
    paths = {
        "SPY": (0.0004, 0.011),
        "TLT": (0.0001, 0.009),
        "GLD": (0.0002, 0.010),
    }
    data = {
        sym: 100 * np.exp(np.cumsum(rng.normal(mu, sigma, len(dates))))
        for sym, (mu, sigma) in paths.items()
    }
    return pd.DataFrame(data, index=dates)


@pytest.fixture(scope="module")
def synthetic_panel(synthetic_px: pd.DataFrame) -> Panel:
    close = synthetic_px.copy()
    close.index = close.index.tz_localize("America/New_York")
    turnover = close * 1e6  # RPLITE doesn't use turnover/delivery; placeholders
    delivery = pd.DataFrame(50.0, index=close.index, columns=close.columns)
    return Panel(close=close, turnover=turnover, delivery=delivery)


# ── 1. weight-target parity ──────────────────────────────────────────────────


def test_weight_target_matches_v1_at_every_rebalance(synthetic_panel, synthetic_px):
    # cash_buffer=0.0: prove the RAW ported math matches w_rplite exactly. The
    # engine's default nonzero buffer (solvency fix, see risk_parity.py) is a
    # separate, additive execution-layer concern tested in test_us_study.py.
    strategy = RiskParityLiteStrategy(assets=ASSETS, vol_lookback=VOL_LOOKBACK, cash_buffer=0.0)
    checked = 0
    for pos in range(VOL_LOOKBACK + 5, len(synthetic_panel.index), 21):  # every ~month
        ctx = Context.at(synthetic_panel, pos)
        qe_w = strategy.rebalance(ctx)
        t_naive = synthetic_panel.index[pos].tz_localize(
            None
        )  # match synthetic_px's tz-naive index
        v1_w = v1.w_rplite(synthetic_px, t_naive, VOL_LOOKBACK)
        assert v1_w, f"v1 produced no weights at {t_naive}"
        assert set(qe_w) == set(v1_w)
        for s in ASSETS:
            assert qe_w[s] == pytest.approx(v1_w[s], abs=1e-10), (t_naive, s)
        checked += 1
    assert checked >= 10  # sanity: the loop actually exercised enough points


# ── 2. zero-cost NAV-path parity (engine mechanics only) ────────────────────

_ZERO_COST = USEquityCosts(sec_fee_pct=0.0, taf_pct=0.0, slippage_frac=0.0)


def _qe_config(tmp_path: Path, panel: Panel) -> RunConfig:
    return RunConfig(
        name="rplite-parity",
        mode="sim",
        start_date=INCEPTION,
        end_date=pd.Timestamp(panel.index[-1]).date(),
        universe=UniverseConfig(market="US", symbols=ASSETS),
        data=DataConfig(lake_root="unused"),
        journal_dir=str(tmp_path / "journals"),
        seed_nav=SEED,
        strategy=StrategyConfig(
            kind="risk_parity_lite", assets=ASSETS, vol_lookback=VOL_LOOKBACK, cash_buffer=0.0
        ),
    )


def test_qe_zero_cost_nav_matches_v1_screen(synthetic_panel, synthetic_px, tmp_path):
    config = _qe_config(tmp_path, synthetic_panel)
    result = run_sim(config, base_dir=tmp_path, panel=synthetic_panel, broker=SimBroker(_ZERO_COST))
    assert result.nav_history, "qe produced no rebalances — check warmup/schedule"

    tgts = v1.build_targets(synthetic_px, "RPLITE", VOL_LOOKBACK)
    v1_net = v1.run_weights(synthetic_px, tgts, pd.Timestamp(INCEPTION), cost_bps=0.0)
    v1_nav = (1 + v1_net).cumprod() * SEED

    qe_dates = [d for d, _ in result.nav_history]
    qe_navs = [n for _, n in result.nav_history]
    v1_at_dates = v1_nav.reindex(pd.DatetimeIndex(qe_dates), method="ffill")

    qe_monthly = pd.Series(qe_navs, index=pd.DatetimeIndex(qe_dates)).pct_change().dropna()
    v1_monthly = (
        pd.Series(v1_at_dates.to_numpy(), index=pd.DatetimeIndex(qe_dates)).pct_change().dropna()
    )
    diff = qe_monthly - v1_monthly
    rmse = float(np.sqrt((diff**2).mean()))
    # Phase 2b established whole-share rounding alone contributes ~0.7bps and
    # full engine-mechanics parity (real fills vs pandas) ~2bps; a generous
    # 15bps tolerance leaves headroom for the synthetic data's different price
    # levels while still catching any real logic divergence.
    assert rmse < 0.0015, f"qe-vs-v1 zero-cost NAV RMSE too high: {rmse:.6f}"
