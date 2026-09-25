"""Risk-parity-lite: inverse-N-day-vol weights across a small static asset set
(ADR-041 P4 — the RPLITE shortlist candidate).

Ported verbatim from `scripts/backtest/run_us_rotation_study.w_rplite`, which
cleared the pre-registered Phase 2 screen (Sharpe 0.86, survivorship-free ETF
universe) and the Phase 2b LEAN engine-mechanics parity check (RMSE 3.4bps).
Static universe (no cross-sectional ranking, unlike the NSE factor books) —
monthly rebalance, one inverse-vol weight per configured asset.
"""

from dataclasses import dataclass

from qe.strategy.base import Context


@dataclass(frozen=True)
class RiskParityLiteStrategy:
    assets: tuple[str, ...] = ("SPY", "TLT", "GLD")
    vol_lookback: int = 63
    # The validated pandas screen is a returns-space model with no real cash
    # balance, so a fully-invested (weights sum to 1.0) target never has a
    # solvency question. qe's engine is real-cash-accounting: spending 100%
    # of NAV on assets plus even a few bps of transaction cost needs slightly
    # MORE than 100% of NAV, which trips the risk engine's cash_non_negative
    # check every rebalance (discovered porting this into the real engine,
    # ADR-041 P4). A small buffer — much smaller than the factor books' 2%,
    # since RPLITE's round-trip cost is a few bps vs NSE's ~30+bps — fixes
    # this the same way `FactorBookStrategy.cash_buffer` already does.
    cash_buffer: float = 0.005

    @property
    def name(self) -> str:
        return "risk-parity-lite"

    def rebalance(self, ctx: Context) -> dict[str, float]:
        assets = list(self.assets)
        # Mirrors w_rplite exactly (isna-only guard, no zero-vol special case)
        # before the cash-buffer scale-down — this is a straight port, not a
        # new implementation, so the pre-buffer math must stay bit-for-bit
        # consistent with what Phase 2/2b actually validated.
        vol = ctx.close[assets].pct_change().iloc[-self.vol_lookback :].std()
        if vol.isna().any():
            return {}
        inv = 1.0 / vol
        w = inv / inv.sum() * (1.0 - self.cash_buffer)
        return {s: float(w[s]) for s in assets}
