"""
Universe data models.

All models are immutable (frozen dataclasses or pydantic with frozen=True)
to enforce snapshot immutability after creation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from shared.universe.modes import UniverseMode


# ── Enums ─────────────────────────────────────────────────────────────────────

class ExclusionReason(str, Enum):
    """Why a symbol was excluded from the approved universe."""
    BELOW_LIQUIDITY_ADV = "BELOW_LIQUIDITY_ADV"
    BELOW_LIQUIDITY_VOLUME = "BELOW_LIQUIDITY_VOLUME"
    BELOW_FREE_FLOAT_MCAP = "BELOW_FREE_FLOAT_MCAP"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    INSUFFICIENT_ACTIVE_DAYS = "INSUFFICIENT_ACTIVE_DAYS"
    LOW_DELIVERY_PCT = "LOW_DELIVERY_PCT"
    PENNY_STOCK = "PENNY_STOCK"
    ASM_LISTED = "ASM_LISTED"
    GSM_LISTED = "GSM_LISTED"
    CIRCUIT_FREQUENCY_HIGH = "CIRCUIT_FREQUENCY_HIGH"
    ABNORMAL_VOLATILITY = "ABNORMAL_VOLATILITY"
    LOW_FLOAT = "LOW_FLOAT"
    POOR_PRICE_DISCOVERY = "POOR_PRICE_DISCOVERY"
    CORPORATE_ACTION_WINDOW = "CORPORATE_ACTION_WINDOW"
    NOT_IN_INDEX = "NOT_IN_INDEX"
    NOT_EQUITY = "NOT_EQUITY"
    DELISTED = "DELISTED"
    SUSPENDED = "SUSPENDED"
    SME_STOCK = "SME_STOCK"
    BSE_ONLY = "BSE_ONLY"
    ETF = "ETF"
    REIT_OR_INVIT = "REIT_OR_INVIT"
    EMERGENCY_EXCLUSION = "EMERGENCY_EXCLUSION"
    MANUAL_EXCLUSION = "MANUAL_EXCLUSION"
    ISIN_MISMATCH = "ISIN_MISMATCH"
    SYMBOL_MASTER_MISSING = "SYMBOL_MASTER_MISSING"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class CorporateActionType(str, Enum):
    """Types of corporate actions tracked by the universe system."""
    STOCK_SPLIT = "STOCK_SPLIT"
    BONUS_ISSUE = "BONUS_ISSUE"
    DIVIDEND = "DIVIDEND"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    MERGER = "MERGER"
    DEMERGER = "DEMERGER"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    SUSPENSION = "SUSPENSION"
    DELISTING = "DELISTING"
    TRADING_HALT = "TRADING_HALT"


class SnapshotFailureMode(str, Enum):
    """What happened when snapshot generation encountered a data source failure."""
    PARTIAL = "PARTIAL"           # Generated with some data sources unavailable
    FALLBACK = "FALLBACK"         # Used YAML fallback instead of live API
    EMERGENCY = "EMERGENCY"       # Emergency: used previous day's snapshot
    FAILED = "FAILED"             # Could not generate a valid snapshot


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SymbolMaster:
    """
    Canonical representation of an NSE-listed equity instrument.

    This is the ground truth about what a symbol IS (not whether to trade it).
    Used for validation: symbol must exist in master before entering the universe.
    """
    symbol: str
    isin: str
    name: str
    exchange: str                    # "NSE" or "BSE"
    instrument_type: str             # "EQ", "BE", "SM", "N", "W", etc.
    is_fno_eligible: bool = False
    index_memberships: frozenset[str] = field(default_factory=frozenset)
    series: str = "EQ"               # NSE series: EQ=equity, BE=trade-for-trade, SM=SME
    is_active: bool = True
    lot_size: int = 1


@dataclass(frozen=True)
class LiquidityMetrics:
    """Computed liquidity metrics for a symbol over a look-back window."""
    symbol: str
    trading_date: date
    adv_crores_20d: Optional[float] = None     # Average daily traded value ₹ crores, 20-day
    adv_volume_20d: Optional[int] = None       # Average daily volume (shares), 20-day
    adv_crores_60d: Optional[float] = None
    adv_volume_60d: Optional[int] = None
    free_float_mcap_crores: Optional[float] = None
    avg_bid_ask_spread_bps: Optional[float] = None
    active_days_last_20: Optional[int] = None
    delivery_pct_20d: Optional[float] = None
    active_days_last_60: Optional[int] = None
    zero_volume_days_last_20: Optional[int] = None
    data_available: bool = True


@dataclass(frozen=True)
class RiskMetrics:
    """Computed risk metrics for a symbol over a look-back window."""
    symbol: str
    trading_date: date
    last_close_price: Optional[float] = None
    daily_volatility_pct_20d: Optional[float] = None   # Std dev of daily returns, 20-day
    circuit_hits_last_20d: Optional[int] = None
    free_float_pct: Optional[float] = None
    is_asm_listed: bool = False
    is_gsm_listed: bool = False
    asm_stage: Optional[int] = None
    gsm_stage: Optional[int] = None
    data_available: bool = True


@dataclass(frozen=True)
class CorporateActionEvent:
    """
    A pending or recent corporate action that may affect trading.

    The universe builder consults this to apply the exclusion window filter.
    """
    symbol: str
    action_type: CorporateActionType
    ex_date: date                    # Date from which adjustment is effective
    announced_at: Optional[date] = None
    details: str = ""                # e.g. "Ratio: 1:2 split", "Dividend: ₹5.0/share"
    data_source: str = "manual"      # "nse_api" | "manual" | "yaml"


@dataclass(frozen=True)
class UniverseDecision:
    """
    The include/exclude decision for a single symbol for a given snapshot.

    Stored inside UniverseSnapshot.decisions for full transparency.
    """
    symbol: str
    market: str
    approved: bool
    reasons: tuple[str, ...]         # Tuple (immutable) of human-readable reasons
    exclusion_codes: tuple[ExclusionReason, ...] = field(default_factory=tuple)
    index_memberships: frozenset[str] = field(default_factory=frozenset)
    liquidity_metrics: Optional[LiquidityMetrics] = None
    risk_metrics: Optional[RiskMetrics] = None


@dataclass(frozen=True)
class UniverseSnapshot:
    """
    Immutable universe snapshot for a specific mode and trading date.

    This is THE authoritative source of truth for which symbols may be traded.
    Generated once per trading day per mode; never mutated after creation.

    Paper and live snapshots are always separate instances — live snapshot
    must never be derived from or contaminated by paper snapshot state.

    Attributes:
        mode:              Universe mode (PAPER_SAFE_START / PAPER_EXPAND / LIVE_ADVANCED).
        trading_date:      The NSE trading date this snapshot is valid for.
        approved_symbols:  frozenset of approved NSE symbol strings (e.g. {"RELIANCE", "HDFCBANK"}).
        decisions:         Full per-symbol decision record including reasons.
        generated_at:      UTC timestamp when this snapshot was generated.
        data_sources_used: Which data sources contributed (e.g. {"nse_api", "yaml_fallback"}).
        failure_mode:      Non-None if snapshot was generated with degraded data.
        checksum:          SHA-256 of the approved_symbols set for integrity verification.
    """
    mode: UniverseMode
    trading_date: date
    approved_symbols: frozenset[str]
    decisions: tuple[UniverseDecision, ...]
    generated_at: datetime
    data_sources_used: frozenset[str] = field(default_factory=frozenset)
    failure_mode: Optional[SnapshotFailureMode] = None
    failure_details: str = ""

    def __post_init__(self) -> None:
        if not self.approved_symbols and self.failure_mode is None:
            raise ValueError(
                f"UniverseSnapshot for {self.mode}/{self.trading_date} has zero approved "
                "symbols and no failure_mode. This indicates a bug in the builder."
            )

    @property
    def size(self) -> int:
        """Number of approved symbols."""
        return len(self.approved_symbols)

    @property
    def is_paper(self) -> bool:
        return self.mode.is_paper

    @property
    def is_live(self) -> bool:
        return self.mode.is_live

    @property
    def checksum(self) -> str:
        """SHA-256 of the sorted approved symbols — used to detect tampering."""
        sorted_symbols = sorted(self.approved_symbols)
        payload = json.dumps(sorted_symbols, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def contains(self, symbol: str) -> bool:
        """True if the symbol (case-insensitive) is in the approved set."""
        return symbol.upper() in self.approved_symbols

    def excluded_symbols(self) -> set[str]:
        """Return symbols that were considered but excluded."""
        return {d.symbol for d in self.decisions if not d.approved}

    def exclusion_summary(self) -> dict[str, list[str]]:
        """Group excluded symbols by their first exclusion code."""
        summary: dict[str, list[str]] = {}
        for d in self.decisions:
            if d.approved or not d.exclusion_codes:
                continue
            code = d.exclusion_codes[0].value
            summary.setdefault(code, []).append(d.symbol)
        return summary

    def to_audit_dict(self) -> dict[str, Any]:
        """Serialise to a dict for S3 audit log storage."""
        return {
            "mode": self.mode.value,
            "trading_date": self.trading_date.isoformat(),
            "approved_count": self.size,
            "approved_symbols": sorted(self.approved_symbols),
            "generated_at": self.generated_at.isoformat(),
            "data_sources_used": sorted(self.data_sources_used),
            "failure_mode": self.failure_mode.value if self.failure_mode else None,
            "failure_details": self.failure_details,
            "checksum": self.checksum,
            "exclusion_summary": self.exclusion_summary(),
            "decisions": [
                {
                    "symbol": d.symbol,
                    "approved": d.approved,
                    "reasons": list(d.reasons),
                    "exclusion_codes": [e.value for e in d.exclusion_codes],
                }
                for d in self.decisions
            ],
        }


@dataclass
class UniverseAuditLog:
    """
    Event log entry for a universe lifecycle event.

    Written to S3 and CloudWatch for audit and observability.
    """
    event_type: str          # e.g. "SNAPSHOT_BUILT", "SYMBOL_EXCLUDED", "EMERGENCY_EXCLUSION"
    mode: UniverseMode
    trading_date: date
    symbol: Optional[str]
    details: str
    timestamp: datetime
    operator: Optional[str] = None   # Operator email if manually triggered
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    """
    Result of universe order validation.

    Returned by UniverseOrderValidator.validate().
    approved=True means the symbol is in the approved snapshot and the order may proceed.
    approved=False means the order MUST be rejected with reason logged.
    """
    approved: bool
    symbol: str
    market: str
    mode: UniverseMode
    reason: str
    snapshot_date: Optional[date] = None
    snapshot_checksum: Optional[str] = None
