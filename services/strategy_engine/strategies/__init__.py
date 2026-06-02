"""
Strategy Engine — strategies package.

Exports all production strategies and supporting infrastructure.

Gate 1 (original):
    MomentumStrategy    — dual MA crossover with ATR sizing

Gate 4 (new — candle-based, paper_trade=True until 5-day validation):
    ORBStrategy             — Opening Range Breakout (1m candles, MARKET_OPEN/NORMAL)
    Scalp1mStrategy         — EMA(9/21) crossover + volume filter (1m, NORMAL)
    VWAPReversionStrategy   — VWAP band mean-reversion with wick confirmation (1m, NORMAL)
    IntradayTrend15mStrategy — EMA/ADX trend following (15m, NORMAL)
    PreCloseMomentumStrategy — Directional bias pre-close trade (5m, PRE_CLOSE)

Adapter:
    CandleBarAdapter    — bridges IntradayCandleStream → strategy.on_bar()

Math helpers (internal):
    _math module — sma, ema, atr_wilder, vwap, vwap_bands, adx, rolling_stddev
"""

from strategy_engine.strategies.base_strategy import Bar, BaseStrategy, StrategyState
from strategy_engine.strategies.candle_adapter import CandleBarAdapter
from strategy_engine.strategies.intraday_trend_15m_strategy import IntradayTrend15mStrategy
from strategy_engine.strategies.momentum_strategy import MomentumStrategy
from strategy_engine.strategies.orb_strategy import ORBStrategy
from strategy_engine.strategies.preclose_momentum_strategy import PreCloseMomentumStrategy
from strategy_engine.strategies.scalp_1m_strategy import Scalp1mStrategy
from strategy_engine.strategies.vwap_reversion_strategy import VWAPReversionStrategy

__all__ = [
    # Infrastructure
    "Bar",
    "BaseStrategy",
    "StrategyState",
    "CandleBarAdapter",
    # Gate 1
    "MomentumStrategy",
    # Gate 4
    "ORBStrategy",
    "Scalp1mStrategy",
    "VWAPReversionStrategy",
    "IntradayTrend15mStrategy",
    "PreCloseMomentumStrategy",
]
