"""
InstrumentRegistry — per-instrument risk parameters loaded from instruments.yaml.

Single source of truth for:
    - Sector classification (used by SectorConcentrationValidator)
    - Per-instrument spread threshold (used by SpreadGateValidator)
    - Per-instrument ADV threshold (used by LiquidityValidator)
    - Market assignment (NSE vs US)

Usage:
    registry = InstrumentRegistry.load()      # load from default path
    config = registry.get("RELIANCE")         # returns InstrumentConfig or None
    if config:
        sector = config.sector                # "ENERGY"
        max_spread = config.max_spread_bps    # 30.0

YAML format (instruments.yaml):
    RELIANCE:
      sector: ENERGY
      market: NSE
      max_spread_bps: 30          # optional override
      max_order_adv_pct: 0.5      # optional override

Adding a new instrument:
    1. Add entry to instruments.yaml.
    2. Restart risk engine (or wait for hot-reload if implemented).
    3. Enable the strategy via `scripts/strategy/config.py go-live`.
    Do NOT add an instrument without a YAML entry — it will be
    classified as "UNKNOWN" sector and bypass SectorConcentrationValidator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="risk_engine")

# Default path — relative to this file's location
_DEFAULT_YAML_PATH = Path(__file__).parent / "instruments.yaml"

# Global defaults used when per-instrument values are not specified in YAML.
_DEFAULT_MAX_SPREAD_BPS: float = 50.0
_DEFAULT_MAX_ORDER_ADV_PCT: float = 1.0


@dataclass(frozen=True)
class InstrumentConfig:
    """
    Per-instrument risk configuration.

    Attributes:
        symbol:             Ticker symbol (e.g. "RELIANCE", "AAPL").
        sector:             GICS-style sector string (e.g. "ENERGY", "US_TECH").
        market:             "NSE" or "US".
        max_spread_bps:     Maximum acceptable bid-ask spread in basis points.
        max_order_adv_pct:  Maximum order size as % of average daily volume.
        max_position_size:  Optional hard cap on shares held (None = use global limit).
    """

    symbol: str
    sector: str
    market: str
    max_spread_bps: float = _DEFAULT_MAX_SPREAD_BPS
    max_order_adv_pct: float = _DEFAULT_MAX_ORDER_ADV_PCT
    max_position_size: Optional[int] = None

    @classmethod
    def from_yaml_entry(cls, symbol: str, data: dict[str, Any]) -> InstrumentConfig:
        """Parse one YAML entry into an InstrumentConfig.

        Args:
            symbol: Instrument ticker.
            data:   Dict from parsed YAML for this symbol.

        Returns:
            InstrumentConfig with all fields populated.

        Raises:
            ValueError: If required fields (sector, market) are missing.
        """
        sector = data.get("sector")
        market = data.get("market")

        if not sector:
            raise ValueError(f"instruments.yaml: {symbol} is missing required 'sector' field")
        if not market:
            raise ValueError(f"instruments.yaml: {symbol} is missing required 'market' field")
        if market not in ("NSE", "US"):
            raise ValueError(
                f"instruments.yaml: {symbol} has invalid market '{market}' — must be 'NSE' or 'US'"
            )

        return cls(
            symbol=symbol,
            sector=str(sector).upper(),
            market=str(market).upper(),
            max_spread_bps=float(data.get("max_spread_bps", _DEFAULT_MAX_SPREAD_BPS)),
            max_order_adv_pct=float(data.get("max_order_adv_pct", _DEFAULT_MAX_ORDER_ADV_PCT)),
            max_position_size=data.get("max_position_size"),
        )


class InstrumentRegistry:
    """
    Loaded instrument registry.  Maps symbol → InstrumentConfig.

    Created once at startup via ``InstrumentRegistry.load()``.  Immutable
    after creation — restart the service to pick up YAML changes.

    Usage:
        registry = InstrumentRegistry.load()
        config = registry.get("RELIANCE")
        sector = config.sector if config else "UNKNOWN"
    """

    def __init__(self, instruments: dict[str, InstrumentConfig]) -> None:
        self._instruments = instruments

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, yaml_path: Optional[Path] = None) -> InstrumentRegistry:
        """
        Load instrument configurations from a YAML file.

        Args:
            yaml_path: Path to instruments.yaml.  Defaults to the bundled
                registry file at ``services/risk_engine/registry/instruments.yaml``.

        Returns:
            Populated InstrumentRegistry.

        Raises:
            FileNotFoundError: If the YAML file does not exist.
            ValueError: If any entry is malformed.
        """
        path = yaml_path or _DEFAULT_YAML_PATH

        # Allow override via environment variable for deployment flexibility
        env_path = os.environ.get("QUANTEMBRACE_INSTRUMENTS_YAML")
        if env_path:
            path = Path(env_path)

        if not path.exists():
            raise FileNotFoundError(
                f"InstrumentRegistry: instruments.yaml not found at {path}. "
                "Every traded symbol must have an entry. "
                "Set QUANTEMBRACE_INSTRUMENTS_YAML to override the path."
            )

        try:
            import yaml  # PyYAML — available in requirements.txt
        except ImportError:
            raise ImportError(
                "PyYAML is required for InstrumentRegistry. "
                "Run: pip install pyyaml"
            )

        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)

        if not raw or not isinstance(raw, dict):
            logger.warning("InstrumentRegistry: instruments.yaml is empty or malformed")
            return cls({})

        instruments: dict[str, InstrumentConfig] = {}
        errors: list[str] = []

        for symbol, data in raw.items():
            if not isinstance(data, dict):
                errors.append(f"{symbol}: value is not a dict")
                continue
            try:
                instruments[str(symbol).upper()] = InstrumentConfig.from_yaml_entry(
                    str(symbol).upper(), data
                )
            except ValueError as exc:
                errors.append(str(exc))

        if errors:
            error_summary = "\n  ".join(errors)
            raise ValueError(
                f"InstrumentRegistry: {len(errors)} error(s) in instruments.yaml:\n  "
                f"{error_summary}"
            )

        logger.info(
            "InstrumentRegistry loaded: %d instruments (%d NSE, %d US)",
            len(instruments),
            sum(1 for c in instruments.values() if c.market == "NSE"),
            sum(1 for c in instruments.values() if c.market == "US"),
        )

        return cls(instruments)

    # ── Lookup ────────────────────────────────────────────────────────────────

    def get(self, symbol: str) -> Optional[InstrumentConfig]:
        """
        Look up configuration for a symbol.

        Args:
            symbol: Ticker (case-insensitive — normalized to uppercase).

        Returns:
            InstrumentConfig if found, None otherwise.
        """
        return self._instruments.get(symbol.upper())

    def get_sector(self, symbol: str) -> str:
        """
        Return the sector for a symbol.  Returns "UNKNOWN" if not registered.

        Args:
            symbol: Ticker symbol.

        Returns:
            Sector string (e.g. "ENERGY") or "UNKNOWN".
        """
        config = self.get(symbol)
        return config.sector if config is not None else "UNKNOWN"

    def all_symbols(self) -> list[str]:
        """Return all registered symbols (sorted)."""
        return sorted(self._instruments.keys())

    def symbols_by_market(self, market: str) -> list[str]:
        """Return all symbols for a given market ("NSE" or "US")."""
        return sorted(
            sym for sym, cfg in self._instruments.items()
            if cfg.market == market.upper()
        )

    def __len__(self) -> int:
        return len(self._instruments)

    def __contains__(self, symbol: str) -> bool:
        return symbol.upper() in self._instruments
