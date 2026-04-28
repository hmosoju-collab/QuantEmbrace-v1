"""
Instrument Universe Loader.

Reads configs/instruments.yaml and returns the list of active instruments
per market, along with any per-instrument strategy overrides.

This replaces hardcoded symbol lists in the strategy engine. Users control
WHICH stocks are watched by editing instruments.yaml — the algorithm then
auto-decides WHEN to trade based on momentum signals.

Usage:
    loader = InstrumentLoader("configs/instruments.yaml")
    nse_instruments = loader.get_active_instruments("NSE")
    us_instruments  = loader.get_active_instruments("US")

Each returned Instrument carries:
    - symbol          (e.g. "RELIANCE", "AAPL")
    - market          ("NSE" or "US")
    - sector          (for filtering and position concentration checks)
    - lot_size        (minimum tradeable quantity — used for position sizing)
    - strategy_params (merged default + per-instrument overrides)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class StrategyParams:
    """Strategy parameters for a single instrument.

    Merges market-level defaults with any per-instrument overrides
    defined in instruments.yaml.
    """

    name: str = "momentum"
    short_window: int = 10
    long_window: int = 50
    min_confidence: float = 0.60
    quantity: int = 20

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategyParams":
        """Build from a raw YAML dict, ignoring unknown keys."""
        return cls(
            name=data.get("name", "momentum"),
            short_window=int(data.get("short_window", 10)),
            long_window=int(data.get("long_window", 50)),
            min_confidence=float(data.get("min_confidence", 0.60)),
            quantity=int(data.get("quantity", 20)),
        )

    def merge_override(self, override: dict[str, Any]) -> "StrategyParams":
        """Return a new StrategyParams with override values applied on top."""
        merged = StrategyParams(
            name=override.get("name", self.name),
            short_window=int(override.get("short_window", self.short_window)),
            long_window=int(override.get("long_window", self.long_window)),
            min_confidence=float(override.get("min_confidence", self.min_confidence)),
            quantity=int(override.get("quantity", self.quantity)),
        )
        return merged


@dataclass
class Instrument:
    """A single tradeable instrument loaded from instruments.yaml."""

    symbol: str
    name: str
    market: str                          # "NSE" or "US"
    sector: str = "Unknown"
    lot_size: int = 1                    # Minimum tradeable quantity
    active: bool = True
    strategy_params: StrategyParams = field(default_factory=StrategyParams)

    def __repr__(self) -> str:
        return (
            f"Instrument({self.market}:{self.symbol} | {self.sector} | "
            f"lot={self.lot_size} | params={self.strategy_params})"
        )


class InstrumentLoader:
    """
    Loads and validates the instrument universe from a YAML config file.

    The YAML file is the single source of truth for which instruments are
    watched and traded. No code changes are needed to add or remove stocks —
    only a config file edit (and service restart).

    Args:
        config_path: Path to instruments.yaml. Defaults to configs/instruments.yaml
                     relative to the project root.
    """

    DEFAULT_CONFIG_PATH = Path("configs/instruments.yaml")

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._config_path = Path(config_path) if config_path else self.DEFAULT_CONFIG_PATH
        self._config: dict[str, Any] = {}
        self._instruments: dict[str, list[Instrument]] = {"NSE": [], "US": []}
        self._loaded = False

    def load(self) -> None:
        """
        Load and parse instruments.yaml.

        Applies market-level filters (max_instruments, sector exclusions, etc.)
        after loading all active instruments.

        Raises:
            FileNotFoundError: If the config file does not exist.
            ValueError: If the config is malformed or has duplicate symbols.
        """
        if not self._config_path.exists():
            raise FileNotFoundError(
                f"Instrument config not found: {self._config_path}. "
                "Copy configs/instruments.yaml.example and configure your universe."
            )

        with open(self._config_path, "r") as f:
            self._config = yaml.safe_load(f)

        self._instruments = {"NSE": [], "US": []}

        for market_key, market_label in [("nse", "NSE"), ("us", "US")]:
            market_cfg = self._config.get(market_key, {})
            default_strategy_dict = market_cfg.get("default_strategy", {})
            default_params = StrategyParams.from_dict(default_strategy_dict)
            filters = self._config.get("filters", {}).get(market_key, {})

            raw_instruments = market_cfg.get("instruments", [])
            seen_symbols: set[str] = set()

            for entry in raw_instruments:
                symbol = entry.get("symbol", "").strip().upper()

                if not symbol:
                    logger.warning("Skipping instrument with missing symbol in %s config", market_label)
                    continue

                # Skip duplicates (e.g. AMZN listed twice in US config)
                if symbol in seen_symbols:
                    logger.debug("Skipping duplicate symbol %s in %s config", symbol, market_label)
                    continue
                seen_symbols.add(symbol)

                # Skip inactive instruments
                if not entry.get("active", False):
                    logger.debug("Skipping inactive instrument %s:%s", market_label, symbol)
                    continue

                # Apply sector exclusion filter
                excluded_sectors = filters.get("exclude_sectors", [])
                sector = entry.get("sector", "Unknown")
                if sector in excluded_sectors:
                    logger.info(
                        "Excluding %s:%s — sector '%s' is in exclude_sectors filter",
                        market_label, symbol, sector
                    )
                    continue

                # Apply lot_size filters (NSE only)
                lot_size = int(entry.get("lot_size", 1))
                if market_label == "NSE":
                    min_lot = filters.get("min_lot_size", 1)
                    max_lot = filters.get("max_lot_size", 99999)
                    if not (min_lot <= lot_size <= max_lot):
                        logger.info(
                            "Excluding %s:%s — lot_size %d outside range [%d, %d]",
                            market_label, symbol, lot_size, min_lot, max_lot,
                        )
                        continue

                # Merge per-instrument strategy overrides on top of market defaults
                override_dict = entry.get("strategy_override", {})
                params = default_params.merge_override(override_dict)

                # Use lot_size as the quantity if not overridden
                if "quantity" not in override_dict and "quantity" not in default_strategy_dict:
                    params.quantity = lot_size

                instrument = Instrument(
                    symbol=symbol,
                    name=entry.get("name", symbol),
                    market=market_label,
                    sector=sector,
                    lot_size=lot_size,
                    active=True,
                    strategy_params=params,
                )
                self._instruments[market_label].append(instrument)

            # Apply max_instruments cap
            max_instruments = filters.get("max_instruments", 999)
            if len(self._instruments[market_label]) > max_instruments:
                logger.warning(
                    "%s universe capped at %d instruments (had %d active). "
                    "Increase max_instruments in filters or reduce active symbols.",
                    market_label,
                    max_instruments,
                    len(self._instruments[market_label]),
                )
                self._instruments[market_label] = self._instruments[market_label][:max_instruments]

        self._loaded = True

        nse_count = len(self._instruments["NSE"])
        us_count = len(self._instruments["US"])
        logger.info(
            "Instrument universe loaded: %d NSE instruments, %d US instruments",
            nse_count, us_count,
        )

        if nse_count == 0 and us_count == 0:
            logger.warning(
                "No active instruments found in %s. "
                "Set active: true for at least one instrument.",
                self._config_path,
            )

    def get_active_instruments(self, market: str) -> list[Instrument]:
        """
        Return the list of active instruments for a given market.

        Args:
            market: "NSE" or "US" (case-insensitive).

        Returns:
            List of Instrument objects, sorted alphabetically by symbol.

        Raises:
            RuntimeError: If load() has not been called yet.
        """
        if not self._loaded:
            raise RuntimeError(
                "InstrumentLoader.load() must be called before get_active_instruments(). "
                "Call loader.load() during service startup."
            )
        key = market.upper()
        if key not in self._instruments:
            raise ValueError(f"Unknown market '{market}'. Valid values: NSE, US")
        return sorted(self._instruments[key], key=lambda i: i.symbol)

    def get_all_symbols(self, market: str) -> list[str]:
        """Return just the symbol strings for a market (convenience method).

        Args:
            market: "NSE" or "US".

        Returns:
            List of symbol strings, e.g. ["AAPL", "MSFT", "NVDA"].
        """
        return [i.symbol for i in self.get_active_instruments(market)]

    def get_instrument(self, market: str, symbol: str) -> Instrument | None:
        """
        Look up a single instrument by market and symbol.

        Args:
            market: "NSE" or "US".
            symbol: Trading symbol, e.g. "RELIANCE".

        Returns:
            Instrument if found and active, None otherwise.
        """
        if not self._loaded:
            raise RuntimeError("Call load() first.")
        for instrument in self._instruments.get(market.upper(), []):
            if instrument.symbol == symbol.upper():
                return instrument
        return None

    def summary(self) -> str:
        """Return a human-readable summary of the loaded universe."""
        if not self._loaded:
            return "Not loaded yet. Call load() first."

        lines = ["Instrument Universe Summary", "=" * 40]
        for market in ("NSE", "US"):
            instruments = self._instruments.get(market, [])
            lines.append(f"\n{market} ({len(instruments)} active instruments):")
            sectors: dict[str, list[str]] = {}
            for inst in instruments:
                sectors.setdefault(inst.sector, []).append(inst.symbol)
            for sector, symbols in sorted(sectors.items()):
                lines.append(f"  {sector:25s}: {', '.join(symbols)}")
        return "\n".join(lines)
