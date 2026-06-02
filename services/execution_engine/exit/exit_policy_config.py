"""
Exit policy configuration loader.

Loads ``configs/exit_policy.yaml`` into validated pydantic models and resolves
per-symbol tier policies for the TradeExitEngine and the backtest harness.

Layer note
----------
This module is PURE CONFIGURATION. It performs no trading actions, no broker
calls, and no DynamoDB access. It is read by the execution engine to
parameterise exit behaviour, and by the backtester so live and backtest exit
maths stay identical.

Signed-quantity / side-aware invariant
--------------------------------------
All ``*_pct`` values are unsigned magnitudes. Direction supplies the sign:

    LONG : favourable = price up   → targets above entry, stop below entry
    SHORT: favourable = price down → targets below entry, stop above entry

The helper methods below encode this once so callers never duplicate the maths.

Safety
------
- If ``mode == "paper"`` then ``live_trading_enabled`` MUST be ``False``. A
  contradictory config raises :class:`ExitPolicyConfigError` at load time
  (fail fast — mirrors the CLAUDE.md HARD BLOCK for paper sessions).
- ``profit_lock_tiers`` must be ascending by ``trigger_pct`` and only ever
  tighten the stop (enforced at runtime by :meth:`ExitTierPolicy.tighter_stop`).
"""
from __future__ import annotations

import logging
from datetime import time
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

_DEFAULT_EXIT_POLICY_YAML = Path("configs/exit_policy.yaml")


class ExitPolicyConfigError(RuntimeError):
    """Raised when the exit policy config is missing, malformed, or unsafe."""


def _parse_hhmm(value: str) -> time:
    """Parse a 'HH:MM' 24h IST string into a datetime.time."""
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except Exception as exc:  # noqa: BLE001 — surface a clear config error
        raise ExitPolicyConfigError(
            f"invalid HH:MM time {value!r} in exit_policy.yaml timing"
        ) from exc


def _direction_sign(direction: str) -> int:
    """+1 for LONG, -1 for SHORT. Raises on anything else."""
    if direction == "LONG":
        return 1
    if direction == "SHORT":
        return -1
    raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")


# ── Tier sub-models ──────────────────────────────────────────────────────────


class ProfitLockTier(BaseModel):
    """One rung of the multi-tier profit-lock ladder (rule D).

    ``trigger_pct`` — favourable move (magnitude) that arms this rung.
    ``lock_pct``    — stop offset from entry toward profit (0.0 == breakeven).
    """

    trigger_pct: float = Field(..., gt=0)
    lock_pct: float = Field(..., ge=0)


class ExitTierPolicy(BaseModel):
    """All exit parameters for one symbol tier (default / blue_chip / mid_cap / small_cap)."""

    # Rule A — initial stop
    initial_stop_loss_pct: float = Field(..., gt=0)
    atr_stop_multiplier: float = Field(1.5, gt=0)
    atr_trail_multiplier: float = Field(2.0, gt=0)

    # Rule B / H — targets
    target_1_pct: float = Field(..., gt=0)
    target_2_pct: float = Field(..., gt=0)
    final_target_pct: float = Field(..., gt=0)

    # Rule C — breakeven
    breakeven_trigger_pct: float = Field(..., gt=0)

    # Rule D — multi-tier profit lock
    profit_lock_tiers: list[ProfitLockTier] = Field(default_factory=list)

    # Rule E — partial profit booking
    partial_profit_trigger_pct: float = Field(..., gt=0)
    partial_profit_qty_pct: float = Field(..., gt=0, le=100)

    # Rule F — trailing stop
    trailing_activation_pct: float = Field(..., gt=0)
    trailing_stop_pct: float = Field(..., gt=0)

    @field_validator("profit_lock_tiers")
    @classmethod
    def _tiers_ascending(cls, v: list[ProfitLockTier]) -> list[ProfitLockTier]:
        triggers = [t.trigger_pct for t in v]
        if triggers != sorted(triggers):
            raise ValueError(
                f"profit_lock_tiers must be ascending by trigger_pct, got {triggers}"
            )
        locks = [t.lock_pct for t in v]
        if locks != sorted(locks):
            raise ValueError(
                f"profit_lock_tiers lock_pct must be non-decreasing, got {locks}"
            )
        return v

    @model_validator(mode="after")
    def _targets_ordered(self) -> "ExitTierPolicy":
        if not (self.target_1_pct <= self.target_2_pct <= self.final_target_pct):
            raise ValueError(
                "targets must satisfy target_1 <= target_2 <= final_target "
                f"(got {self.target_1_pct}, {self.target_2_pct}, {self.final_target_pct})"
            )
        return self

    # ── side-aware price helpers (reused by live engine + backtest) ──────────

    @staticmethod
    def _favourable(entry: float, direction: str, pct: float) -> float:
        """Price `pct`% in the profit direction from entry."""
        return entry * (1.0 + _direction_sign(direction) * pct / 100.0)

    @staticmethod
    def _adverse(entry: float, direction: str, pct: float) -> float:
        """Price `pct`% in the loss direction from entry (used for stops)."""
        return entry * (1.0 - _direction_sign(direction) * pct / 100.0)

    def initial_stop_price(
        self, entry: float, direction: str, atr: Optional[float] = None
    ) -> float:
        """Initial SL. Uses ATR when supplied, else percentage (rule A)."""
        if atr is not None and atr > 0:
            return entry - _direction_sign(direction) * self.atr_stop_multiplier * atr
        return self._adverse(entry, direction, self.initial_stop_loss_pct)

    def target_price(self, entry: float, direction: str, which: str) -> float:
        """which ∈ {'1','2','final'} → side-aware target price (rule B/H)."""
        pct = {
            "1": self.target_1_pct,
            "2": self.target_2_pct,
            "final": self.final_target_pct,
        }[which]
        return self._favourable(entry, direction, pct)

    def breakeven_trigger_price(self, entry: float, direction: str) -> float:
        """Favourable price at which the stop moves to entry (rule C)."""
        return self._favourable(entry, direction, self.breakeven_trigger_pct)

    def partial_trigger_price(self, entry: float, direction: str) -> float:
        """Favourable price at which partial booking fires (rule E)."""
        return self._favourable(entry, direction, self.partial_profit_trigger_pct)

    def trailing_activation_price(self, entry: float, direction: str) -> float:
        """Favourable price at which the trailing stop arms (rule F)."""
        return self._favourable(entry, direction, self.trailing_activation_pct)

    def trailing_stop_from(
        self, best_price: float, direction: str, atr: Optional[float] = None
    ) -> float:
        """Trailing stop trailing behind the best favourable price seen (rule F)."""
        if atr is not None and atr > 0:
            return best_price - _direction_sign(direction) * self.atr_trail_multiplier * atr
        return best_price * (1.0 - _direction_sign(direction) * self.trailing_stop_pct / 100.0)

    def favourable_move_pct(self, entry: float, last_price: float, direction: str) -> float:
        """Signed favourable move in percent (positive = in profit)."""
        return _direction_sign(direction) * (last_price - entry) / entry * 100.0

    def profit_lock_stop(
        self, entry: float, direction: str, favourable_move_pct: float
    ) -> Optional[float]:
        """Tightest profit-lock stop armed by the current favourable move (rule D).

        Returns the lock stop price for the highest tier whose trigger has been
        reached, or ``None`` if no tier is armed yet.
        """
        armed: Optional[ProfitLockTier] = None
        for tier in self.profit_lock_tiers:  # ascending
            if favourable_move_pct + 1e-9 >= tier.trigger_pct:
                armed = tier
            else:
                break
        if armed is None:
            return None
        return self._favourable(entry, direction, armed.lock_pct)

    @staticmethod
    def tighter_stop(existing: Optional[float], candidate: float, direction: str) -> float:
        """Return the more protective stop — trailing/lock only ever tightens.

        LONG  → the higher stop (closer to price from below).
        SHORT → the lower stop (closer to price from above).
        """
        if existing is None:
            return candidate
        if direction == "LONG":
            return max(existing, candidate)
        return min(existing, candidate)


# ── Timing sub-model ─────────────────────────────────────────────────────────


class ExitTimingConfig(BaseModel):
    """IST timing gates shared by TEE, RiskCapManager, and MISSquareOffManager."""

    no_new_entry_after_ist: str = "14:45"
    time_exit_after_ist: str = "14:55"
    mis_square_off_time_ist: str = "15:05"
    square_off_deadline_ist: str = "15:10"
    broker_auto_square_off_ist: str = "15:15"

    @field_validator(
        "no_new_entry_after_ist",
        "time_exit_after_ist",
        "mis_square_off_time_ist",
        "square_off_deadline_ist",
        "broker_auto_square_off_ist",
    )
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        _parse_hhmm(v)  # raises on malformed
        return v

    @model_validator(mode="after")
    def _ordered(self) -> "ExitTimingConfig":
        seq = [
            ("no_new_entry_after_ist", self.no_new_entry_after_ist),
            ("time_exit_after_ist", self.time_exit_after_ist),
            ("mis_square_off_time_ist", self.mis_square_off_time_ist),
            ("square_off_deadline_ist", self.square_off_deadline_ist),
            ("broker_auto_square_off_ist", self.broker_auto_square_off_ist),
        ]
        mins = [(_parse_hhmm(v).hour * 60 + _parse_hhmm(v).minute) for _, v in seq]
        if mins != sorted(mins):
            raise ExitPolicyConfigError(
                "exit_policy timing keys must be chronologically ordered: "
                f"{[v for _, v in seq]}"
            )
        return self

    @property
    def no_new_entry_after(self) -> time:
        return _parse_hhmm(self.no_new_entry_after_ist)

    @property
    def time_exit_after(self) -> time:
        return _parse_hhmm(self.time_exit_after_ist)

    @property
    def mis_square_off_time(self) -> time:
        return _parse_hhmm(self.mis_square_off_time_ist)

    @property
    def square_off_deadline(self) -> time:
        return _parse_hhmm(self.square_off_deadline_ist)

    @property
    def broker_auto_square_off(self) -> time:
        return _parse_hhmm(self.broker_auto_square_off_ist)


# ── Top-level config ─────────────────────────────────────────────────────────


class ExitPolicyConfig(BaseModel):
    """Validated view of configs/exit_policy.yaml."""

    mode: str = "paper"
    live_trading_enabled: bool = False
    timing: ExitTimingConfig = Field(default_factory=ExitTimingConfig)
    default_tier: str = "default"
    tiers: dict[str, ExitTierPolicy]
    symbol_tiers: dict[str, str] = Field(default_factory=dict)

    @field_validator("mode")
    @classmethod
    def _mode_known(cls, v: str) -> str:
        if v not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got {v!r}")
        return v

    @model_validator(mode="after")
    def _validate_coherence(self) -> "ExitPolicyConfig":
        # HARD BLOCK: a paper config must never enable live trading.
        if self.mode == "paper" and self.live_trading_enabled:
            raise ExitPolicyConfigError(
                "exit_policy.yaml is unsafe: mode == 'paper' but "
                "live_trading_enabled == true. Refusing to load."
            )
        if self.default_tier not in self.tiers:
            raise ExitPolicyConfigError(
                f"default_tier {self.default_tier!r} is not defined under tiers"
            )
        for sym, tier in self.symbol_tiers.items():
            if tier not in self.tiers:
                raise ExitPolicyConfigError(
                    f"symbol_tiers[{sym}] references unknown tier {tier!r}"
                )
        return self

    # ── resolution ───────────────────────────────────────────────────────────

    def tier_name_for(self, symbol: str) -> str:
        """Tier name for a symbol; falls back to default_tier when unmapped."""
        return self.symbol_tiers.get(symbol, self.default_tier)

    def resolve_tier(self, symbol: str) -> ExitTierPolicy:
        """Resolve the ExitTierPolicy for a symbol (default tier when unmapped)."""
        return self.tiers[self.tier_name_for(symbol)]

    # ── loading ────────────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExitPolicyConfig":
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> "ExitPolicyConfig":
        """Load and validate the exit policy YAML.

        Resolution order for the default path:
          1. ``configs/exit_policy.yaml`` relative to CWD (repo-root convention).
          2. ``configs/exit_policy.yaml`` relative to the discovered repo root
             (walks up from this file) — robust to CWD differences in tests.
        """
        candidate = Path(path) if path else _DEFAULT_EXIT_POLICY_YAML
        if not candidate.exists() and path is None:
            candidate = _discover_repo_config() or candidate
        if not candidate.exists():
            raise ExitPolicyConfigError(
                f"exit_policy.yaml not found at {candidate}. "
                "Copy configs/exit_policy.yaml and configure tiers."
            )
        with open(candidate, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        try:
            cfg = cls.model_validate(raw)
        except ExitPolicyConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrap pydantic errors with context
            raise ExitPolicyConfigError(
                f"exit_policy.yaml failed validation: {exc}"
            ) from exc
        logger.info(
            "exit_policy.loaded mode=%s live_enabled=%s tiers=%d symbols_mapped=%d",
            cfg.mode,
            cfg.live_trading_enabled,
            len(cfg.tiers),
            len(cfg.symbol_tiers),
        )
        return cfg


def _discover_repo_config() -> Optional[Path]:
    """Walk up from this file looking for configs/exit_policy.yaml."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "configs" / "exit_policy.yaml"
        if cand.exists():
            return cand
    return None
