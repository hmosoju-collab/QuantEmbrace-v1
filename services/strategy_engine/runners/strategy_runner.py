"""
StrategyRunner — per-strategy failure domain with dual-threshold circuit breaker.

Wraps every strategy (tick-based and candle-based) in a consistent safety layer:

    enabled        → if False, dispatch_tick/bar returns None immediately
    paper_trade    → if True, Signal.paper_trade is set to True before publishing
    circuit breaker → isolates failures so one bad strategy doesn't grind the others
    max_signals_per_day → hard cap per calendar day (UTC)
    state persistence → calls strategy.save_state() on every successful dispatch

Interface types:
    TICK   — strategy.on_tick() then strategy.generate_signal()
    CANDLE — strategy.on_bar() then strategy.generate_signal()

One runner per strategy. All lifecycle parameters are configurable via DynamoDB
strategy-config hot-reload (StrategyConfigLoader pushes updates via apply_config()).

CloudWatch metrics emitted (via caller — service.py collects and flushes):
    CircuitBreakerState    0=CLOSED, 1=OPEN, 2=HALF_OPEN
    ConsecutiveErrors      reset to 0 on any success
    ErrorsInLast5Min       rolling rate count
    SignalsByStrategy      signals published (all)
    PaperSignalsByStrategy paper-only signal count
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from shared.logging.logger import get_logger
from shared.models.signal import Signal
from strategy_engine.runners.circuit_breaker import CircuitBreaker, CircuitState
from strategy_engine.strategies.base_strategy import Bar, BaseStrategy, DataQuality

logger = get_logger(__name__, service_name="strategy_engine")


class InterfaceType(str, Enum):
    """Whether this runner dispatches tick events or candle bar events."""
    TICK   = "TICK"
    CANDLE = "CANDLE"


@dataclass
class StrategyConfig:
    """
    Per-strategy runtime configuration.

    Loaded from DynamoDB strategy-config table and hot-reloaded every 60s.
    All fields have safe defaults so an absent row doesn't crash the runner.
    """
    enabled:                         bool = True
    paper_trade:                     bool = True   # All strategies start paper
    max_signals_per_day:             int  = 0      # 0 = unlimited (use strategy defaults)
    circuit_breaker_threshold_consecutive: int = 5
    circuit_breaker_threshold_rate:  int  = 10


class StrategyRunner:
    """
    Single wrapper class that turns any BaseStrategy into a self-contained failure domain.

    One StrategyRunner instance per strategy per StrategyEngineService.
    All strategies — tick-based and candle-based — use this same class.
    The interface_type controls whether dispatch_tick() or dispatch_bar() is meaningful.

    Args:
        strategy:       The strategy instance to wrap.
        interface_type: TICK or CANDLE.
        config:         Initial StrategyConfig (updated via apply_config()).
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        interface_type: InterfaceType,
        config: Optional[StrategyConfig] = None,
    ) -> None:
        self._strategy      = strategy
        self._interface     = interface_type
        self._config        = config or StrategyConfig()
        self._circuit       = CircuitBreaker(
            strategy_name=strategy.name,
            consecutive_threshold=self._config.circuit_breaker_threshold_consecutive,
            rate_threshold=self._config.circuit_breaker_threshold_rate,
        )

        # Per-day signal cap tracking
        self._signals_today: int       = 0
        self._cap_date:       date     = datetime.now(timezone.utc).date()

        # Monotonic count for metrics (never resets)
        self._total_signals:       int = 0
        self._total_paper_signals: int = 0

    # ── Public accessors ──────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._strategy.name

    @property
    def interface_type(self) -> InterfaceType:
        return self._interface

    @property
    def circuit_state(self) -> CircuitState:
        return self._circuit.state

    @property
    def config(self) -> StrategyConfig:
        return self._config

    @property
    def consecutive_errors(self) -> int:
        return self._circuit.consecutive_errors

    @property
    def errors_in_window(self) -> int:
        return self._circuit.errors_in_window

    # ── Config hot-reload ─────────────────────────────────────────────────────

    def apply_config(self, new_config: StrategyConfig, reset_cb: bool = False) -> bool:
        """
        Apply a fresh StrategyConfig loaded from DynamoDB.

        Args:
            new_config: Updated configuration.
            reset_cb:   True if operator set circuit_breaker_reset in DynamoDB.
                        Forces a manual circuit breaker reset.

        Returns:
            True if circuit breaker was reset (caller should log/alert).
        """
        self._config = new_config
        self._circuit.update_thresholds(
            consecutive_threshold=new_config.circuit_breaker_threshold_consecutive,
            rate_threshold=new_config.circuit_breaker_threshold_rate,
        )
        if reset_cb:
            self._circuit.reset()
            logger.info(
                "strategy_runner.circuit_breaker_reset_manual strategy=%s", self.name
            )
            return True
        return False

    # ── Dispatch: tick interface ──────────────────────────────────────────────

    async def dispatch_tick(
        self,
        symbol: str,
        price: float,
        volume: int,
        timestamp: datetime,
    ) -> Optional[Signal]:
        """
        Dispatch one tick to the wrapped strategy and return a Signal if generated.

        Returns None if:
            - strategy is disabled
            - circuit is OPEN
            - daily signal cap is reached
            - strategy generated no signal
            - strategy raised an exception (circuit breaker records the failure)
        """
        if self._interface != InterfaceType.TICK:
            logger.warning(
                "strategy_runner.wrong_interface strategy=%s expected=TICK got=%s",
                self.name, self._interface.value,
            )
            return None

        if not self._should_dispatch():
            return None

        try:
            await self._strategy.on_tick(symbol, price, volume, timestamp)
            signal = await self._strategy.generate_signal()
            self._circuit.record_success()
            if signal is not None:
                return self._apply_paper_flag(self._check_cap(signal))
            return None

        except Exception as exc:
            opened = self._circuit.record_failure()
            self._on_failure(exc, opened)
            return None

    # ── Dispatch: candle interface ────────────────────────────────────────────

    async def dispatch_bar(self, bar: Bar) -> Optional[Signal]:
        """
        Dispatch one OHLCV candle bar to the wrapped strategy and return a Signal.

        Returns None if:
            - strategy is disabled
            - circuit is OPEN
            - daily signal cap is reached
            - strategy generated no signal
            - strategy raised an exception
        """
        if self._interface != InterfaceType.CANDLE:
            logger.warning(
                "strategy_runner.wrong_interface strategy=%s expected=CANDLE got=%s",
                self.name, self._interface.value,
            )
            return None

        if bar.data_quality != DataQuality.NORMAL:
            logger.debug(
                "strategy_runner.bar_suppressed_data_quality strategy=%s quality=%s",
                self.name, bar.data_quality.value,
            )
            return None

        if not self._should_dispatch():
            return None

        try:
            await self._strategy.on_bar(bar)
            signal = await self._strategy.generate_signal()
            self._circuit.record_success()
            if signal is not None:
                return self._apply_paper_flag(self._check_cap(signal))
            return None

        except Exception as exc:
            opened = self._circuit.record_failure()
            self._on_failure(exc, opened)
            return None

    # ── Daily cap management ──────────────────────────────────────────────────

    def reset_daily_cap(self) -> None:
        """
        Reset the daily signal counter.

        Called by StrategyEngineService at market POST_CLOSE phase transition
        (or at startup if a new trading day has begun since last run).
        """
        self._signals_today = 0
        self._cap_date = datetime.now(timezone.utc).date()

    def signals_today(self) -> int:
        return self._signals_today

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _should_dispatch(self) -> bool:
        """Gate check: enabled + circuit not OPEN."""
        if not self._config.enabled:
            return False
        if self._circuit.is_open:
            logger.debug(
                "strategy_runner.circuit_open_skip strategy=%s state=%s",
                self.name, self._circuit.state.value,
            )
            return False
        return True

    def _check_cap(self, signal: Optional[Signal]) -> Optional[Signal]:
        """Enforce max_signals_per_day cap. Returns signal or None."""
        if signal is None:
            return None

        # Roll over cap counter if calendar date changed
        today = datetime.now(timezone.utc).date()
        if today != self._cap_date:
            self.reset_daily_cap()

        cap = self._config.max_signals_per_day
        if cap > 0 and self._signals_today >= cap:
            logger.warning(
                "strategy_runner.daily_cap_reached strategy=%s cap=%d signals_today=%d",
                self.name, cap, self._signals_today,
            )
            return None

        self._signals_today += 1
        self._total_signals += 1
        return signal

    def _apply_paper_flag(self, signal: Optional[Signal]) -> Optional[Signal]:
        """Stamp paper_trade flag from current config onto the signal."""
        if signal is None:
            return None
        signal.paper_trade = self._config.paper_trade
        if self._config.paper_trade:
            self._total_paper_signals += 1
        return signal

    def _on_failure(self, exc: Exception, circuit_just_opened: bool) -> None:
        """Log failure and emit circuit-open alarm data if circuit just tripped."""
        logger.error(
            "strategy_runner.dispatch_error strategy=%s interface=%s consec=%d rate=%d state=%s error=%s",
            self.name, self._interface.value,
            self._circuit.consecutive_errors, self._circuit.errors_in_window,
            self._circuit.state.value, str(exc),
            exc_info=True,
        )

        if circuit_just_opened:
            logger.critical(
                "strategy_runner.circuit_breaker_opened strategy=%s consec_threshold=%d rate_threshold=%d consec=%d rate=%d",
                self.name, self._circuit.consecutive_threshold, self._circuit.rate_threshold,
                self._circuit.consecutive_errors, self._circuit.errors_in_window,
            )
            # CloudWatch metric emission happens in the caller (service.py) which
            # checks circuit_state after each dispatch and emits CircuitBreakerState.
