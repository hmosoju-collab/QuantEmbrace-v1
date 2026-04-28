"""
Full-featured backtesting engine for QuantEmbrace strategies.

Replays historical OHLCV bars through a strategy and computes a comprehensive
set of performance metrics:

    - Total & annualised return
    - Sharpe ratio (annualised, risk-free rate configurable)
    - Sortino ratio
    - Maximum drawdown (% and absolute)
    - Win rate, profit factor
    - Average win / loss size
    - Total trades, trade log

Execution model:
    - Entry/exit at bar close price (conservative).
    - Commission charged on both entry and exit legs.
    - Stop-loss and take-profit respected if present on the signal:
        * Checked against the *next* bar's high/low for realistic fill.
    - Each symbol tracked independently; portfolio-level P&L is the sum.
    - Short selling supported (SELL signal on flat position opens a short).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from shared.logging.logger import get_logger

from strategy_engine.signals.signal import Direction, Signal
from strategy_engine.strategies.base_strategy import Bar, BaseStrategy

logger = get_logger(__name__, service_name="strategy_engine")


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    """
    A completed round-trip trade (entry + exit).

    Attributes:
        symbol:      Instrument traded.
        direction:   BUY (long) or SELL (short).
        entry_price: Fill price on entry.
        exit_price:  Fill price on exit.
        quantity:    Units traded.
        entry_time:  Bar timestamp of entry.
        exit_time:   Bar timestamp of exit.
        exit_reason: "signal", "stop_loss", "take_profit", or "eod" (end of data).
        pnl:         Net profit/loss after commissions.
        commission:  Total commission paid (both legs).
    """

    symbol: str
    direction: Direction
    entry_price: float
    exit_price: float
    quantity: int
    entry_time: datetime
    exit_time: datetime
    exit_reason: str
    pnl: float
    commission: float

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0


@dataclass
class BacktestResult:
    """
    Complete results from a backtest run.

    Attributes:
        strategy_name:        Name of the backtested strategy.
        start_date:           First bar timestamp.
        end_date:             Last bar timestamp.
        initial_capital:      Starting capital.
        final_capital:        Ending portfolio value.
        total_return_pct:     (final - initial) / initial × 100.
        annualised_return_pct: CAGR assuming 252 trading days/year.
        max_drawdown_pct:     Largest peak-to-trough drawdown (%).
        max_drawdown_abs:     Largest peak-to-trough drawdown (currency).
        sharpe_ratio:         Annualised Sharpe (daily returns, configurable RF).
        sortino_ratio:        Annualised Sortino (downside deviation).
        total_trades:         Completed round-trip trade count.
        win_rate:             % of profitable trades.
        profit_factor:        Gross profit / gross loss (0 if no losses).
        avg_win:              Average profit on winning trades.
        avg_loss:             Average loss on losing trades (negative value).
        largest_win:          Largest single-trade profit.
        largest_loss:         Largest single-trade loss (negative value).
        total_commission:     Total commissions paid.
        signals_generated:    Total raw signals from strategy.
        buy_signals:          Count of BUY signals.
        sell_signals:         Count of SELL signals.
        trades:               Full trade log.
        equity_curve:         List of (timestamp, portfolio_value) snapshots.
        daily_returns:        List of daily return fractions (for Sharpe/Sortino).
    """

    strategy_name: str = ""
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    initial_capital: float = 0.0
    final_capital: float = 0.0

    total_return_pct: float = 0.0
    annualised_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_abs: float = 0.0

    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0

    total_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    total_commission: float = 0.0

    signals_generated: int = 0
    buy_signals: int = 0
    sell_signals: int = 0

    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    daily_returns: list[float] = field(default_factory=list)

    def summary(self) -> str:
        """One-line human-readable summary."""
        return (
            f"{self.strategy_name} | "
            f"Return={self.total_return_pct:+.2f}% | "
            f"Ann={self.annualised_return_pct:+.2f}% | "
            f"MaxDD={self.max_drawdown_pct:.2f}% | "
            f"Sharpe={self.sharpe_ratio:.3f} | "
            f"Sortino={self.sortino_ratio:.3f} | "
            f"Trades={self.total_trades} | "
            f"WinRate={self.win_rate:.1f}% | "
            f"PF={self.profit_factor:.2f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Position tracker
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _OpenPosition:
    """Internal tracker for an open position."""

    symbol: str
    direction: Direction
    quantity: int
    entry_price: float
    entry_time: datetime
    stop_loss: Optional[float]
    take_profit: Optional[float]
    entry_commission: float


# ─────────────────────────────────────────────────────────────────────────────
# Backtester
# ─────────────────────────────────────────────────────────────────────────────


class Backtester:
    """
    Replay historical bars through a strategy and compute performance metrics.

    Usage::

        strategy = MomentumStrategy(symbols=["RELIANCE"], market="NSE")
        bt = Backtester(strategy=strategy, initial_capital=1_000_000)
        result = await bt.run(bars)
        print(result.summary())
    """

    TRADING_DAYS_PER_YEAR = 252

    def __init__(
        self,
        strategy: BaseStrategy,
        initial_capital: float = 1_000_000.0,
        commission_pct: float = 0.03,
        risk_free_rate_annual: float = 0.06,
        allow_short: bool = False,
    ) -> None:
        """
        Initialise the backtester.

        Args:
            strategy:               Strategy instance to replay.
            initial_capital:        Starting capital (same currency as prices).
            commission_pct:         Commission as a percentage of trade value per leg.
                                    (0.03 = 0.03%, i.e. 3 basis points)
            risk_free_rate_annual:  Annual risk-free rate for Sharpe calculation.
                                    Default 6% (INR government bond proxy).
            allow_short:            If False, SELL signals on flat positions are ignored.
        """
        self._strategy = strategy
        self._initial_capital = initial_capital
        self._commission_pct = commission_pct / 100.0  # Convert to fraction
        self._rf_annual = risk_free_rate_annual
        self._allow_short = allow_short

    async def run(self, bars: list[Bar]) -> BacktestResult:
        """
        Run a full backtest over chronologically ordered bars.

        Args:
            bars: List of OHLCV bars sorted oldest-to-newest.

        Returns:
            BacktestResult populated with all metrics.
        """
        if not bars:
            logger.warning("No bars provided for backtest")
            return BacktestResult(
                strategy_name=self._strategy.name,
                initial_capital=self._initial_capital,
                final_capital=self._initial_capital,
            )

        await self._strategy.initialize()

        result = BacktestResult(
            strategy_name=self._strategy.name,
            start_date=bars[0].timestamp,
            end_date=bars[-1].timestamp,
            initial_capital=self._initial_capital,
        )

        cash = self._initial_capital
        open_positions: dict[str, _OpenPosition] = {}
        pending_signals: dict[str, Signal] = {}  # symbol → signal waiting to enter next bar

        prev_day: Optional[datetime] = None
        prev_portfolio_value: float = self._initial_capital
        peak_value: float = self._initial_capital
        max_dd_pct: float = 0.0
        max_dd_abs: float = 0.0

        for i, bar in enumerate(bars):
            # ── Step 1: Check stop-loss / take-profit on open position ─────
            if bar.symbol in open_positions:
                pos = open_positions[bar.symbol]
                exit_reason, exit_price = self._check_exits(bar, pos)
                if exit_reason:
                    trade, cash = self._close_position(pos, exit_price, exit_reason, bar.timestamp, cash)
                    result.trades.append(trade)
                    del open_positions[bar.symbol]

            # ── Step 2: Process pending entry signal from previous bar ──────
            if bar.symbol in pending_signals and bar.symbol not in open_positions:
                sig = pending_signals.pop(bar.symbol)
                cash, pos = self._open_position(sig, bar.close, bar.timestamp, cash)
                if pos is not None:
                    open_positions[bar.symbol] = pos

            # ── Step 3: Feed bar to strategy ───────────────────────────────
            await self._strategy.on_bar(bar)
            signal = await self._strategy.generate_signal()

            if signal is not None:
                result.signals_generated += 1
                if signal.direction == Direction.BUY:
                    result.buy_signals += 1
                else:
                    result.sell_signals += 1

                pending_signals[signal.symbol] = signal  # Enter at *next* bar open/close

            # ── Step 4: Mark-to-market portfolio value ─────────────────────
            mtm = cash
            for sym, pos in open_positions.items():
                if sym == bar.symbol:
                    mtm += self._position_value(pos, bar.close)
                else:
                    mtm += self._position_value(pos, pos.entry_price)  # Stale price

            result.equity_curve.append((bar.timestamp, mtm))

            # Track daily returns
            bar_date = bar.timestamp.date()
            if prev_day is not None and bar_date != prev_day:
                daily_ret = (mtm - prev_portfolio_value) / max(prev_portfolio_value, 1e-9)
                result.daily_returns.append(daily_ret)
                prev_portfolio_value = mtm
            prev_day = bar_date

            # Drawdown
            if mtm > peak_value:
                peak_value = mtm
            dd_abs = peak_value - mtm
            dd_pct = dd_abs / peak_value * 100.0 if peak_value > 0 else 0.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_abs = dd_abs

        # ── Close any remaining open positions at last bar price ───────────
        last_bar_map: dict[str, Bar] = {}
        for bar in reversed(bars):
            if bar.symbol not in last_bar_map:
                last_bar_map[bar.symbol] = bar
            if len(last_bar_map) == len(open_positions):
                break

        for sym, pos in list(open_positions.items()):
            last_bar = last_bar_map.get(sym, bars[-1])
            trade, cash = self._close_position(pos, last_bar.close, "eod", last_bar.timestamp, cash)
            result.trades.append(trade)

        # ── Final portfolio value ──────────────────────────────────────────
        result.final_capital = cash
        result.max_drawdown_pct = max_dd_pct
        result.max_drawdown_abs = max_dd_abs

        # ── Metrics computation ────────────────────────────────────────────
        self._compute_trade_metrics(result)
        self._compute_return_metrics(result, bars)
        self._compute_risk_adjusted_metrics(result)

        logger.info("Backtest complete: %s", result.summary())
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Position management
    # ─────────────────────────────────────────────────────────────────────────

    def _open_position(
        self,
        signal: Signal,
        price: float,
        timestamp: datetime,
        cash: float,
    ) -> tuple[float, Optional[_OpenPosition]]:
        """Open a new position. Returns (updated_cash, position or None if rejected)."""
        if signal.direction == Direction.SELL and not self._allow_short:
            return cash, None

        trade_value = price * signal.quantity
        commission = trade_value * self._commission_pct

        if signal.direction == Direction.BUY:
            cost = trade_value + commission
            if cost > cash:
                return cash, None  # Insufficient capital
            cash -= cost
        else:
            # Short: receive proceeds minus commission
            cash += trade_value - commission

        pos = _OpenPosition(
            symbol=signal.symbol,
            direction=signal.direction,
            quantity=signal.quantity,
            entry_price=price,
            entry_time=timestamp,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            entry_commission=commission,
        )
        return cash, pos

    def _close_position(
        self,
        pos: _OpenPosition,
        exit_price: float,
        exit_reason: str,
        exit_time: datetime,
        cash: float,
    ) -> tuple[TradeRecord, float]:
        """Close an open position. Returns (trade_record, updated_cash)."""
        trade_value = exit_price * pos.quantity
        exit_commission = trade_value * self._commission_pct
        total_commission = pos.entry_commission + exit_commission

        if pos.direction == Direction.BUY:
            proceeds = trade_value - exit_commission
            cash += proceeds
            pnl = (exit_price - pos.entry_price) * pos.quantity - total_commission
        else:
            # Short: we sold high, need to buy back
            cost = trade_value + exit_commission
            cash -= cost
            pnl = (pos.entry_price - exit_price) * pos.quantity - total_commission

        trade = TradeRecord(
            symbol=pos.symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            exit_reason=exit_reason,
            pnl=pnl,
            commission=total_commission,
        )
        return trade, cash

    def _check_exits(
        self, bar: Bar, pos: _OpenPosition
    ) -> tuple[Optional[str], float]:
        """
        Check if stop-loss or take-profit was triggered on this bar.

        Returns (reason, fill_price) or (None, 0.0) if no exit.
        We assume the stop/TP was breached at exactly the trigger price
        (conservative: could have slipped further intrabar).
        """
        if pos.direction == Direction.BUY:
            if pos.stop_loss is not None and bar.low <= pos.stop_loss:
                return "stop_loss", pos.stop_loss
            if pos.take_profit is not None and bar.high >= pos.take_profit:
                return "take_profit", pos.take_profit
        else:  # SHORT
            if pos.stop_loss is not None and bar.high >= pos.stop_loss:
                return "stop_loss", pos.stop_loss
            if pos.take_profit is not None and bar.low <= pos.take_profit:
                return "take_profit", pos.take_profit
        return None, 0.0

    @staticmethod
    def _position_value(pos: _OpenPosition, price: float) -> float:
        """Mark-to-market value of an open position (positive = asset or credit)."""
        if pos.direction == Direction.BUY:
            return pos.quantity * price
        else:
            # Short: value is the unrealised gain (pos.entry_price - price) × qty
            return pos.quantity * (pos.entry_price - price)

    # ─────────────────────────────────────────────────────────────────────────
    # Metrics
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_trade_metrics(self, result: BacktestResult) -> None:
        """Populate win rate, profit factor, avg win/loss from trade log."""
        trades = result.trades
        result.total_trades = len(trades)
        result.total_commission = sum(t.commission for t in trades)

        if not trades:
            return

        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [t.pnl for t in trades if t.pnl <= 0]

        result.win_rate = len(wins) / len(trades) * 100.0
        result.avg_win = sum(wins) / len(wins) if wins else 0.0
        result.avg_loss = sum(losses) / len(losses) if losses else 0.0
        result.largest_win = max(wins) if wins else 0.0
        result.largest_loss = min(losses) if losses else 0.0

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    def _compute_return_metrics(self, result: BacktestResult, bars: list[Bar]) -> None:
        """Total return and CAGR."""
        result.total_return_pct = (
            (result.final_capital - result.initial_capital) / result.initial_capital * 100.0
        )

        # CAGR: years = trading_days / 252
        if bars[0].timestamp and bars[-1].timestamp:
            delta_days = (bars[-1].timestamp - bars[0].timestamp).days
            years = max(delta_days / 365.25, 1 / self.TRADING_DAYS_PER_YEAR)
            if years > 0 and result.initial_capital > 0:
                ratio = result.final_capital / result.initial_capital
                if ratio > 0:
                    result.annualised_return_pct = ((ratio ** (1.0 / years)) - 1.0) * 100.0

    def _compute_risk_adjusted_metrics(self, result: BacktestResult) -> None:
        """Sharpe and Sortino ratios from daily return series."""
        daily = result.daily_returns
        if len(daily) < 2:
            return

        rf_daily = (1 + self._rf_annual) ** (1 / self.TRADING_DAYS_PER_YEAR) - 1
        excess = [r - rf_daily for r in daily]
        mean_excess = sum(excess) / len(excess)
        std_excess = _std(excess)

        if std_excess > 0:
            result.sharpe_ratio = (mean_excess / std_excess) * math.sqrt(self.TRADING_DAYS_PER_YEAR)

        # Sortino: penalise only downside deviation
        downside = [r for r in excess if r < 0]
        if downside:
            downside_dev = _std(downside, population=True)
            if downside_dev > 0:
                result.sortino_ratio = (mean_excess / downside_dev) * math.sqrt(
                    self.TRADING_DAYS_PER_YEAR
                )


# ─────────────────────────────────────────────────────────────────────────────
# Math utilities
# ─────────────────────────────────────────────────────────────────────────────


def _std(values: list[float], population: bool = False) -> float:
    """Sample (default) or population standard deviation."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n if population else n - 1)
    return math.sqrt(variance)
