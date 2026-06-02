"""
UniverseOrderValidator — enforces the hard order validation rule.

HARD RULE: No paper or live order may be placed unless the symbol exists
in the approved universe snapshot for that trading date and mode.

Usage:
    # Build once at service startup
    snapshot = builder.build(UniverseMode.PAPER_SAFE_START, date.today())
    validator = UniverseOrderValidator(snapshot)

    # Call in execute_approved_signal before any broker call
    result = validator.validate(symbol="RELIANCE", market="NSE")
    if not result.approved:
        raise ValueError(result.reason)  # logged and audited

The validator is synchronous and hot-path safe — it checks a frozenset.
No async I/O, no network calls.

Failure behavior:
    If no snapshot is loaded (validator built with snapshot=None):
    - LIVE mode: REJECT all orders (fail-safe).
    - PAPER mode: WARN and ALLOW (permissive — don't block paper trading).
    This is the "snapshot missing" scenario (e.g. first run before build completes).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from shared.universe.models import SnapshotFailureMode, UniverseSnapshot, ValidationResult
from shared.universe.modes import UniverseMode

logger = logging.getLogger(__name__)


class UniverseOrderValidator:
    """
    Validates that a symbol is approved for trading in the current universe snapshot.

    Thread-safe: the approved_symbols frozenset is immutable and can be checked
    concurrently without locks.

    Args:
        snapshot: The approved universe snapshot for today. None = fail-safe mode.
        mode: Universe mode this validator is enforcing.
              If None, derived from snapshot.mode.
    """

    def __init__(
        self,
        snapshot: Optional[UniverseSnapshot],
        mode: Optional[UniverseMode] = None,
    ) -> None:
        self._snapshot = snapshot
        self._mode = mode or (snapshot.mode if snapshot is not None else None)

    @property
    def snapshot(self) -> Optional[UniverseSnapshot]:
        return self._snapshot

    @property
    def mode(self) -> Optional[UniverseMode]:
        return self._mode

    def validate(
        self,
        symbol: str,
        market: str = "NSE",
        _today: date | None = None,
    ) -> ValidationResult:
        """
        Validate whether a symbol may be traded in the current snapshot.

        Args:
            symbol:  Trading symbol (e.g. "RELIANCE"). Case-insensitive.
            market:  Market identifier (e.g. "NSE", "US"). Currently only NSE validated.
            _today:  Override today's date (test injection only; omit in production).

        Returns:
            ValidationResult with approved=True if symbol is in the approved set.
        """
        sym = symbol.upper()
        mkt = market.upper()
        current_mode = self._mode or UniverseMode.LIVE_ADVANCED  # default to strictest

        # US market: universe validation not yet implemented; allow with warning
        if mkt != "NSE":
            logger.debug(
                "universe_validator.non_nse_market symbol=%s market=%s — allowed (no US universe configured)",
                sym, mkt,
            )
            return ValidationResult(
                approved=True,
                symbol=sym,
                market=mkt,
                mode=current_mode,
                reason="Non-NSE market — universe validation not configured",
            )

        # No snapshot loaded
        if self._snapshot is None:
            if current_mode.is_live:
                reason = (
                    f"No universe snapshot loaded for mode={current_mode.value}. "
                    "Live orders BLOCKED until snapshot is built. "
                    "Call UniverseBuilder.build() before starting execution."
                )
                logger.critical(
                    "universe_validator.no_snapshot_live_blocked symbol=%s mode=%s",
                    sym, current_mode.value,
                )
                return ValidationResult(
                    approved=False,
                    symbol=sym,
                    market=mkt,
                    mode=current_mode,
                    reason=reason,
                )
            else:
                reason = (
                    f"No universe snapshot loaded for mode={current_mode.value}. "
                    "Paper orders allowed with WARNING — build snapshot for full validation."
                )
                logger.warning(
                    "universe_validator.no_snapshot_paper_allowed symbol=%s mode=%s",
                    sym, current_mode.value,
                )
                return ValidationResult(
                    approved=True,
                    symbol=sym,
                    market=mkt,
                    mode=current_mode,
                    reason=reason,
                )

        # Staleness guard — snapshot must be for today's trading date.
        # A stale snapshot may contain symbols since delisted or put on ASM.
        today = _today or datetime.now(timezone.utc).date()
        if self._snapshot.trading_date != today:
            if current_mode.is_live:
                reason = (
                    f"Universe snapshot is STALE: snapshot_date={self._snapshot.trading_date}, "
                    f"today={today}. Live orders BLOCKED until today's snapshot is built. "
                    "Run UniverseBuilder.build() at market open and call update_snapshot()."
                )
                logger.critical(
                    "universe_validator.stale_snapshot_live_blocked "
                    "symbol=%s mode=%s snapshot_date=%s today=%s",
                    sym, current_mode.value, self._snapshot.trading_date, today,
                )
                return ValidationResult(
                    approved=False,
                    symbol=sym,
                    market=mkt,
                    mode=current_mode,
                    reason=reason,
                    snapshot_date=self._snapshot.trading_date,
                    snapshot_checksum=self._snapshot.checksum,
                )
            logger.warning(
                "universe_validator.stale_snapshot_paper_allowed "
                "symbol=%s mode=%s snapshot_date=%s today=%s — allowed with warning",
                sym, current_mode.value, self._snapshot.trading_date, today,
            )

        # PARTIAL snapshot guard — a degraded live universe is too risky to trade.
        # Paper mode: PARTIAL is non-fatal (research/validation context).
        if (
            self._snapshot.failure_mode == SnapshotFailureMode.PARTIAL
            and current_mode.is_live
        ):
            reason = (
                f"Universe snapshot is PARTIAL: only {self._snapshot.size} symbols approved "
                f"(below min_symbols_to_trade) for {self._snapshot.mode.value} on "
                f"{self._snapshot.trading_date}. Live orders BLOCKED until a healthy snapshot "
                "is built. Check universe_builder logs for the exclusion reason breakdown."
            )
            logger.critical(
                "universe_validator.partial_snapshot_live_blocked symbol=%s mode=%s size=%d date=%s",
                sym, current_mode.value, self._snapshot.size, self._snapshot.trading_date,
            )
            return ValidationResult(
                approved=False,
                symbol=sym,
                market=mkt,
                mode=current_mode,
                reason=reason,
                snapshot_date=self._snapshot.trading_date,
                snapshot_checksum=self._snapshot.checksum,
            )

        # Snapshot present — enforce the hard rule
        if self._snapshot.contains(sym):
            return ValidationResult(
                approved=True,
                symbol=sym,
                market=mkt,
                mode=self._snapshot.mode,
                reason=f"Approved in {self._snapshot.mode.value} snapshot ({self._snapshot.trading_date})",
                snapshot_date=self._snapshot.trading_date,
                snapshot_checksum=self._snapshot.checksum,
            )
        else:
            reason = (
                f"Symbol {mkt}:{sym} is NOT in the approved universe snapshot "
                f"for mode={self._snapshot.mode.value} date={self._snapshot.trading_date}. "
                f"Snapshot contains {self._snapshot.size} approved symbols. "
                f"Checksum: {self._snapshot.checksum}. "
                "Add the symbol to the appropriate index membership in universe_modes.yaml "
                "and rebuild the snapshot, or escalate for manual override review."
            )
            logger.warning(
                "universe_validator.rejected symbol=%s market=%s mode=%s date=%s checksum=%s",
                sym, mkt,
                self._snapshot.mode.value,
                self._snapshot.trading_date,
                self._snapshot.checksum,
            )
            return ValidationResult(
                approved=False,
                symbol=sym,
                market=mkt,
                mode=self._snapshot.mode,
                reason=reason,
                snapshot_date=self._snapshot.trading_date,
                snapshot_checksum=self._snapshot.checksum,
            )

    def validate_batch(
        self, symbols: list[tuple[str, str]]
    ) -> dict[str, ValidationResult]:
        """
        Validate multiple (symbol, market) pairs at once.

        Args:
            symbols: List of (symbol, market) tuples.

        Returns:
            Dict mapping symbol → ValidationResult.
        """
        return {sym: self.validate(sym, mkt) for sym, mkt in symbols}

    def update_snapshot(self, new_snapshot: UniverseSnapshot) -> "UniverseOrderValidator":
        """
        Return a new validator with an updated snapshot.

        Does NOT mutate this instance — returns a fresh validator.
        Called after a daily snapshot refresh.
        """
        if self._mode and new_snapshot.mode != self._mode:
            raise ValueError(
                f"Snapshot mode mismatch: validator is configured for {self._mode.value} "
                f"but new snapshot is {new_snapshot.mode.value}. "
                "Paper validator must not accept live snapshot."
            )
        logger.info(
            "universe_validator.snapshot_updated mode=%s date=%s approved=%d checksum=%s",
            new_snapshot.mode.value, new_snapshot.trading_date,
            new_snapshot.size, new_snapshot.checksum,
        )
        return UniverseOrderValidator(snapshot=new_snapshot, mode=self._mode)


def build_validator_for_today(
    mode: UniverseMode,
    modes_config_path: str | None = None,
    fail_if_no_snapshot: bool = False,
    use_live_api: bool = False,
) -> UniverseOrderValidator:
    """
    Convenience factory: build and validate for today's trading date.

    Used at service startup in execution_engine.service.

    Args:
        mode: Universe mode to use.
        modes_config_path: Override path to universe_modes.yaml.
        fail_if_no_snapshot: If True and snapshot cannot be built, raise.
                             If False, return a validator with snapshot=None (permissive paper,
                             blocking live).
        use_live_api: If True, fetch index constituents from NSE archive CSVs
                      (live, daily-updated). Falls back to YAML on failure.
                      If False (default), use static YAML only.

    Returns:
        UniverseOrderValidator for today.
    """
    from datetime import datetime, timezone
    from shared.universe.builder import UniverseBuilder, UniverseSnapshotError
    from shared.universe.data_sources import build_data_source

    today = datetime.now(timezone.utc).date()

    try:
        data_source = build_data_source(
            use_live_api=use_live_api,
            universe_modes_path=modes_config_path,
        )
        builder = UniverseBuilder(
            data_source=data_source,
            modes_config_path=modes_config_path,
        )
        snapshot = builder.build(mode, today)
        return UniverseOrderValidator(snapshot=snapshot, mode=mode)
    except UniverseSnapshotError as exc:
        if fail_if_no_snapshot:
            raise
        logger.error(
            "universe_validator.build_failed mode=%s date=%s error=%s — "
            "returning validator with no snapshot",
            mode.value, today, exc,
        )
        return UniverseOrderValidator(snapshot=None, mode=mode)
    except Exception as exc:
        if fail_if_no_snapshot:
            raise
        logger.error(
            "universe_validator.unexpected_build_error mode=%s date=%s error=%s — "
            "returning validator with no snapshot",
            mode.value, today, exc,
        )
        return UniverseOrderValidator(snapshot=None, mode=mode)
