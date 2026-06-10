"""Strategy adapters for the QuantEmbrace backtesting lab.

Wires the **six production strategies** to the replay engine without modifying
their logic. Each adapter:

    * reuses the production strategy class (no reimplementation),
    * declares the replay interval the engine should feed it,
    * emits a **production-compatible signal** (the canonical ``Signal`` schema)
      enriched with ``strategy_version``, ``data_version``, a bar-stamped
      ``timestamp`` (never future), and the metadata the TradeExitEngine needs
      (entry, stop, target, risk-per-unit, R-target, product_type, exit policy).

Backtest-only: no broker calls, no live dependencies, no Kafka. Fast mode simply
feeds candles to ``on_bar`` / ``generate_signal``. ``scalp_1m`` is **paper-only**.

Interval map (engine feeds only these): vwap_reversion=1m, momentum=5m, orb=1m
(forms the 09:15–09:30 opening range), trend_15m=15m, preclose=5m, scalp_1m=1m.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shared.models.signal import Signal

from strategy_engine.strategies.base_strategy import BaseStrategy
from strategy_engine.strategies.intraday_trend_15m_strategy import IntradayTrend15mStrategy
from strategy_engine.strategies.momentum_strategy import MomentumStrategy
from strategy_engine.strategies.orb_strategy import ORBStrategy
from strategy_engine.strategies.preclose_momentum_strategy import PreCloseMomentumStrategy
from strategy_engine.strategies.scalp_1m_strategy import Scalp1mStrategy
from strategy_engine.strategies.vwap_reversion_strategy import VWAPReversionStrategy

EXIT_POLICY_VERSION = "tee@1.0"


# ── production strategy builders (reuse real classes) ───────────────────────────


def _build_vwap(symbols: list[str], market: str, nav: float, **o: Any) -> BaseStrategy:
    o.pop("paper_trade", None)
    return VWAPReversionStrategy(
        name="vwap_reversion_bt", symbols=symbols, market=market, nav=nav, paper_trade=True, **o
    )


def _build_momentum(symbols: list[str], market: str, nav: float, **o: Any) -> BaseStrategy:
    o.pop("paper_trade", None)  # MomentumStrategy has no paper_trade param
    return MomentumStrategy(name="momentum_bt", symbols=symbols, market=market, nav=nav, **o)


def _build_orb(symbols: list[str], market: str, nav: float, **o: Any) -> BaseStrategy:
    o.pop("paper_trade", None)
    return ORBStrategy(
        name="orb_15m_bt", symbols=symbols, market=market, nav=nav, paper_trade=True, **o
    )


def _build_trend15(symbols: list[str], market: str, nav: float, **o: Any) -> BaseStrategy:
    o.pop("paper_trade", None)
    return IntradayTrend15mStrategy(
        name="intraday_trend_15m_bt", symbols=symbols, market=market, nav=nav, paper_trade=True, **o
    )


def _build_preclose(symbols: list[str], market: str, nav: float, **o: Any) -> BaseStrategy:
    o.pop("paper_trade", None)
    return PreCloseMomentumStrategy(
        name="preclose_momentum_bt", symbols=symbols, market=market, nav=nav, paper_trade=True, **o
    )


def _build_scalp(symbols: list[str], market: str, nav: float, **o: Any) -> BaseStrategy:
    """scalp_1m v2 (hardened, paper-only). Live spread/LTP rejects are disabled for
    OHLCV backtests, but every edge floor is kept so low-edge trades are rejected."""
    o.pop("paper_trade", None)  # paper-only — cannot be overridden
    floors = dict(
        atr_stop_multiplier=0.5,
        rr_ratio=1.5,
        min_body_atr_pct=0.3,
        max_signals_per_day=3,
        min_stop_pct=0.15,
        min_target_pct=0.25,
        min_spread_multiple=3.0,
        min_tick_multiple=5.0,
        max_spread_pct=0.08,
        min_net_edge_pct=0.12,
        reject_if_spread_unavailable=False,  # no live spread feed in a backtest
        reject_if_ltp_stale=False,
    )
    floors.update(o)  # caller may tune, but paper_trade stays forced True
    return Scalp1mStrategy(
        name="scalp_1m_v2_bt", symbols=symbols, market=market, nav=nav, paper_trade=True, **floors
    )


# ── adapter ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StrategyAdapter:
    name: str
    replay_interval: str          # "1m" | "5m" | "15m" — what the engine feeds
    strategy_version: str
    paper_only: bool
    _builder: Callable[..., BaseStrategy]

    def build_strategy(
        self, symbols: list[str], *, market: str = "NSE", nav: float = 1_000_000.0, **overrides: Any
    ) -> BaseStrategy:
        return self._builder(symbols, market, nav, **overrides)

    def enrich(self, signal: Signal, *, data_version: str, last_bar: Any = None) -> dict[str, Any]:
        """Convert a production ``Signal`` to the enriched, production-compatible dict."""
        d = signal.to_dict()
        entry = signal.price_at_signal or (getattr(last_bar, "close", 0.0) if last_bar else 0.0)
        risk = abs(entry - signal.stop_loss) if (signal.stop_loss and entry) else None
        rr = (abs(signal.take_profit - entry) / risk) if (signal.take_profit and risk) else None
        if self.paper_only:
            d["paper_trade"] = True
        d["strategy_version"] = self.strategy_version
        d["data_version"] = data_version
        d["metadata"] = {
            **(d.get("metadata") or {}),
            "strategy_version": self.strategy_version,
            "data_version": data_version,
            "backtest": True,
            "paper_trade": d.get("paper_trade", signal.paper_trade),
            "tee": {
                "strategy": self.name,
                "entry_price": entry,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "risk_per_unit": risk,
                "rr_target": rr,
                "product_type": "MIS",
                "exit_policy_version": EXIT_POLICY_VERSION,
            },
        }
        return d

    async def collect_signals(
        self,
        candles: list[Any],
        symbols: list[str],
        *,
        data_version: str,
        market: str = "NSE",
        nav: float = 1_000_000.0,
        **overrides: Any,
    ) -> list[dict[str, Any]]:
        """Fast-mode run: feed candles in chronological order, return enriched signals.

        No-lookahead: each signal's ``generated_at`` is stamped to the bar that
        produced it, and candles are processed strictly oldest-first.
        """
        strat = self.build_strategy(symbols, market=market, nav=nav, **overrides)
        await strat.initialize()
        out: list[dict[str, Any]] = []
        for c in sorted(candles, key=lambda x: x.timestamp):
            bar = c.to_bar()
            await strat.on_bar(bar)
            sig = await strat.generate_signal()
            if sig is not None:
                sig.generated_at = bar.timestamp  # bar-stamped; never future
                out.append(self.enrich(sig, data_version=data_version, last_bar=bar))
        return out


# ── registry ────────────────────────────────────────────────────────────────────

ADAPTERS: dict[str, StrategyAdapter] = {
    "vwap_reversion": StrategyAdapter(
        "vwap_reversion", "1m", "vwap_reversion@1.0", False, _build_vwap
    ),
    "momentum": StrategyAdapter("momentum", "5m", "momentum@2.0", False, _build_momentum),
    "orb": StrategyAdapter("orb", "1m", "orb_15m@1.0", False, _build_orb),
    "trend_15m": StrategyAdapter("trend_15m", "15m", "intraday_trend_15m@1.0", False, _build_trend15),
    "preclose": StrategyAdapter("preclose", "5m", "preclose_momentum@1.0", False, _build_preclose),
    "scalp_1m": StrategyAdapter("scalp_1m", "1m", "scalp_1m@2.0", True, _build_scalp),
}


def get_adapter(name: str) -> StrategyAdapter:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise ValueError(
            f"Unknown strategy {name!r}. Available: {sorted(ADAPTERS)}"
        ) from None


def list_adapters() -> list[str]:
    return list(ADAPTERS)
