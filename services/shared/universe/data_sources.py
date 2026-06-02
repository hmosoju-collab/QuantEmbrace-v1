"""
Universe data source interfaces and stub implementations.

The universe builder consults these to fetch:
  - Index membership (NIFTY 50, NIFTY 100, etc.)
  - F&O eligible stock list
  - Surveillance lists (ASM, GSM)
  - Symbol master (ISIN, instrument type, listing status)
  - Liquidity metrics (ADV, volume, spread)
  - Corporate action events

Two implementations are provided:
  YamlDataSource — reads from YAML config files (offline, always available).
  NseApiDataSource — fetches live data from NSE (production, requires network access).

The builder falls back to YamlDataSource if NseApiDataSource fails or is disabled.

Adding a new data source:
  1. Implement the UniverseDataSource protocol below.
  2. Wire it into UniverseBuilder in builder.py.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from shared.universe.models import (
    CorporateActionEvent,
    LiquidityMetrics,
    RiskMetrics,
    SymbolMaster,
)

logger = logging.getLogger(__name__)

_DEFAULT_UNIVERSE_MODES_YAML = Path("configs/universe_modes.yaml")
_DEFAULT_EXCLUSION_YAML = Path("configs/exclusion_lists.yaml")
_DEFAULT_LIQUIDITY_YAML = Path("configs/liquidity_filters.yaml")
_DEFAULT_RISK_YAML = Path("configs/risk_filters.yaml")


# ── Protocol / Abstract Base ──────────────────────────────────────────────────

class UniverseDataSource(ABC):
    """Abstract base class for all universe data source implementations."""

    @abstractmethod
    def get_index_symbols(self, index_name: str) -> set[str]:
        """Return the set of NSE symbols that are constituents of the given index.

        Args:
            index_name: e.g. "NIFTY_50", "NIFTY_100", "NIFTY_200", "FNO"

        Returns:
            Set of NSE symbol strings (uppercase). Empty set if unavailable.
        """

    @abstractmethod
    def get_fno_symbols(self) -> set[str]:
        """Return all NSE symbols currently eligible for F&O trading."""

    @abstractmethod
    def get_asm_symbols(self) -> set[str]:
        """Return all NSE symbols currently on ASM list."""

    @abstractmethod
    def get_gsm_symbols(self) -> set[str]:
        """Return all NSE symbols currently on GSM list."""

    @abstractmethod
    def get_symbol_master(self, symbol: str) -> Optional[SymbolMaster]:
        """Return symbol master data for a single symbol."""

    @abstractmethod
    def get_liquidity_metrics(self, symbol: str, as_of: date) -> Optional[LiquidityMetrics]:
        """Return liquidity metrics for a symbol as of a given date."""

    @abstractmethod
    def get_risk_metrics(self, symbol: str, as_of: date) -> Optional[RiskMetrics]:
        """Return risk metrics for a symbol as of a given date."""

    @abstractmethod
    def get_corporate_actions(
        self, symbol: str, window_days: int = 30
    ) -> list[CorporateActionEvent]:
        """Return corporate actions for a symbol within a look-ahead/look-back window."""

    @abstractmethod
    def get_emergency_exclusions(self) -> set[str]:
        """Return symbols currently on the emergency exclusion list."""

    @abstractmethod
    def get_exclusion_lists(self) -> dict[str, set[str]]:
        """Return all static exclusion lists keyed by category.

        Required keys: delisted, suspended, sme, etf, reit_invit, bse_only, emergency.
        Each value is a set of uppercase NSE symbol strings.
        """

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable name for this data source (used in audit logs)."""


# ── YAML Data Source — offline fallback ──────────────────────────────────────

class YamlDataSource(UniverseDataSource):
    """
    Reads universe data from YAML config files.

    This is always available and serves as the authoritative fallback when
    live NSE API is disabled or unreachable.

    Config files read:
      - configs/universe_modes.yaml     → index membership, mode definitions
      - configs/exclusion_lists.yaml    → emergency exclusions, known ASM/GSM
    """

    def __init__(
        self,
        universe_modes_path: str | Path | None = None,
        exclusion_path: str | Path | None = None,
    ) -> None:
        self._modes_path = Path(universe_modes_path or _DEFAULT_UNIVERSE_MODES_YAML)
        self._exclusion_path = Path(exclusion_path or _DEFAULT_EXCLUSION_YAML)
        self._modes_cfg: dict[str, Any] = {}
        self._exclusion_cfg: dict[str, Any] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._modes_path.exists():
            with open(self._modes_path, "r", encoding="utf-8") as fh:
                self._modes_cfg = yaml.safe_load(fh) or {}
        else:
            logger.warning("universe_data_source.modes_yaml_missing path=%s", self._modes_path)

        if self._exclusion_path.exists():
            with open(self._exclusion_path, "r", encoding="utf-8") as fh:
                self._exclusion_cfg = yaml.safe_load(fh) or {}
        else:
            logger.warning("universe_data_source.exclusion_yaml_missing path=%s", self._exclusion_path)

        self._loaded = True

    def _resolve_index_symbols(self, index_name: str) -> set[str]:
        """Resolve index symbols, handling derived indexes (e.g. NIFTY_100)."""
        self._ensure_loaded()
        index_cfg = self._modes_cfg.get("index_symbols", {}).get(index_name, {})
        if not index_cfg:
            return set()

        result: set[str] = set()

        # Direct symbol list
        for s in index_cfg.get("symbols", []):
            result.add(str(s).upper())

        # Derived from other indices
        for base_index in index_cfg.get("derived_from", []):
            result |= self._resolve_index_symbols(base_index)

        # Additional symbols on top of derived
        for s in index_cfg.get("additional_symbols", []):
            result.add(str(s).upper())

        return result

    def get_index_symbols(self, index_name: str) -> set[str]:
        return self._resolve_index_symbols(index_name)

    def get_fno_symbols(self) -> set[str]:
        return self._resolve_index_symbols("FNO")

    def get_asm_symbols(self) -> set[str]:
        self._ensure_loaded()
        asm_list = self._exclusion_cfg.get("known_asm", []) or []
        return {str(s).upper() for s in asm_list if s}

    def get_gsm_symbols(self) -> set[str]:
        self._ensure_loaded()
        gsm_list = self._exclusion_cfg.get("known_gsm", []) or []
        return {str(s).upper() for s in gsm_list if s}

    def get_symbol_master(self, symbol: str) -> Optional[SymbolMaster]:
        # YAML fallback has no symbol master data — return minimal stub
        return SymbolMaster(
            symbol=symbol.upper(),
            isin="",
            name=symbol.upper(),
            exchange="NSE",
            instrument_type="EQ",
            is_fno_eligible=False,
            index_memberships=frozenset(),
        )

    def get_liquidity_metrics(self, symbol: str, as_of: date) -> Optional[LiquidityMetrics]:
        # No liquidity data in YAML — caller must handle None as "data unavailable"
        return None

    def get_risk_metrics(self, symbol: str, as_of: date) -> Optional[RiskMetrics]:
        # No risk metrics in YAML — caller must handle None
        return None

    def get_corporate_actions(
        self, symbol: str, window_days: int = 30
    ) -> list[CorporateActionEvent]:
        return []

    def get_emergency_exclusions(self) -> set[str]:
        self._ensure_loaded()
        emergency_list = self._exclusion_cfg.get("emergency", []) or []
        result: set[str] = set()
        for entry in emergency_list:
            if isinstance(entry, dict):
                sym = entry.get("symbol", "")
                if sym:
                    result.add(str(sym).upper())
            elif isinstance(entry, str):
                result.add(entry.upper())
        return result

    def get_exclusion_lists(self) -> dict[str, set[str]]:
        """Return all static exclusion lists (delisted, suspended, SME, ETF, etc.)."""
        self._ensure_loaded()

        def _to_set(key: str) -> set[str]:
            items = self._exclusion_cfg.get(key, []) or []
            result: set[str] = set()
            for item in items:
                if isinstance(item, dict):
                    sym = item.get("symbol", "")
                    if sym:
                        result.add(str(sym).upper())
                elif isinstance(item, str):
                    result.add(item.upper())
            return result

        return {
            "delisted": _to_set("delisted"),
            "suspended": _to_set("suspended"),
            "sme": _to_set("sme"),
            "etf": _to_set("etf"),
            "reit_invit": _to_set("reit_invit"),
            "bse_only": _to_set("bse_only"),
            "emergency": self.get_emergency_exclusions(),
        }

    @property
    def source_name(self) -> str:
        return "yaml_fallback"


# ── NSE API Data Source — production live feed ───────────────────────────────

class NseApiDataSource(UniverseDataSource):
    """
    Fetches live universe data from NSE public APIs and the NSE Bhavcopy archive.

    Feed objects are created lazily on first use to avoid network calls at
    import/init time. Each feed is cached for the lifetime of this instance.

    Data source strategy per feed:
      Surveillance (ASM/GSM) — live NSE API, RAISES ConnectionError on failure
        (LIVE_ADVANCED EXCLUDE policy fires when unavailable)
      Index membership       — live NSE API, falls back to YAML on failure
      Liquidity              — Bhavcopy archive, returns None on failure
      Corporate actions      — live NSE API, RAISES ConnectionError on failure
      Exclusion lists        — always YAML (operator-curated, static)
      Symbol master          — always YAML (no NSE bulk export available)

    Args:
        yaml_fallback: Used for index fallback, symbol master, and all
                       exclusion list queries.
        bhavcopy_cache: Directory for disk-cached Bhavcopy JSON files.
                        Strongly recommended to avoid re-downloading on restart.
    """

    def __init__(
        self,
        yaml_fallback: Optional["YamlDataSource"] = None,
        bhavcopy_cache: Optional[Path] = None,
    ) -> None:
        self._fallback = yaml_fallback or YamlDataSource()
        self._bhavcopy_cache = bhavcopy_cache
        self._nse_client: Optional[Any] = None
        self._bhavcopy: Optional[Any] = None
        self._surveillance: Optional[Any] = None
        self._index_fetcher: Optional[Any] = None
        self._ca_fetcher: Optional[Any] = None

    # ── Lazy feed constructors ─────────────────────────────────────────────────

    def _get_nse_client(self) -> Any:
        if self._nse_client is None:
            from shared.universe.feeds.nse_client import NseHttpClient
            self._nse_client = NseHttpClient()
        return self._nse_client

    def _get_bhavcopy(self) -> Any:
        if self._bhavcopy is None:
            from shared.universe.feeds.bhavcopy import BhavcopySeries
            self._bhavcopy = BhavcopySeries(cache_dir=self._bhavcopy_cache)
        return self._bhavcopy

    def _get_surveillance(self) -> Any:
        if self._surveillance is None:
            from shared.universe.feeds.surveillance import SurveillanceFetcher
            self._surveillance = SurveillanceFetcher(self._get_nse_client())
        return self._surveillance

    def _get_index_fetcher(self) -> Any:
        if self._index_fetcher is None:
            from shared.universe.feeds.index_fetcher import IndexConstituentFetcher
            self._index_fetcher = IndexConstituentFetcher(self._get_nse_client())
        return self._index_fetcher

    def _get_ca_fetcher(self) -> Any:
        if self._ca_fetcher is None:
            from shared.universe.feeds.corporate_actions import CorporateActionFetcher
            self._ca_fetcher = CorporateActionFetcher(self._get_nse_client())
        return self._ca_fetcher

    # ── Interface implementation ───────────────────────────────────────────────

    def get_index_symbols(self, index_name: str) -> set[str]:
        try:
            return self._get_index_fetcher().get_index_symbols(index_name)
        except Exception as exc:
            logger.warning(
                "nse_api_data_source.index_fetch_failed index=%s error=%s — YAML fallback",
                index_name, exc,
            )
            return self._fallback.get_index_symbols(index_name)

    def get_fno_symbols(self) -> set[str]:
        try:
            return self._get_index_fetcher().get_index_symbols("FNO")
        except Exception as exc:
            logger.warning(
                "nse_api_data_source.fno_fetch_failed error=%s — YAML fallback", exc
            )
            return self._fallback.get_fno_symbols()

    def get_asm_symbols(self) -> set[str]:
        # Intentionally raises — LIVE_ADVANCED EXCLUDE policy fires if unavailable.
        return self._get_surveillance().get_asm_symbols()

    def get_gsm_symbols(self) -> set[str]:
        # Intentionally raises — LIVE_ADVANCED EXCLUDE policy fires if unavailable.
        return self._get_surveillance().get_gsm_symbols()

    def get_symbol_master(self, symbol: str) -> Optional[SymbolMaster]:
        return self._fallback.get_symbol_master(symbol)

    def get_liquidity_metrics(self, symbol: str, as_of: date) -> Optional[LiquidityMetrics]:
        try:
            return self._get_bhavcopy().compute_liquidity_metrics(symbol, as_of)
        except Exception as exc:
            logger.warning(
                "nse_api_data_source.liquidity_fetch_failed symbol=%s error=%s — no data",
                symbol, exc,
            )
            return None

    def get_risk_metrics(self, symbol: str, as_of: date) -> Optional[RiskMetrics]:
        """Return a partial RiskMetrics using bhavcopy availability as the data gate."""
        try:
            liq = self._get_bhavcopy().compute_liquidity_metrics(symbol, as_of)
            if liq is None or not liq.data_available:
                return None
            # Return data_available=True to clear the EXCLUDE gate; optional fields
            # (volatility, circuit hits) are None — RiskFilter skips None checks.
            return RiskMetrics(
                symbol=symbol.upper(),
                trading_date=as_of,
                data_available=True,
            )
        except Exception as exc:
            logger.warning(
                "nse_api_data_source.risk_metrics_failed symbol=%s error=%s — no data",
                symbol, exc,
            )
            return None

    def get_corporate_actions(
        self, symbol: str, window_days: int = 30
    ) -> list[CorporateActionEvent]:
        # Intentionally raises — LIVE_ADVANCED EXCLUDE policy fires if unavailable.
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date()
        return self._get_ca_fetcher().get_corporate_actions(symbol, today, window_days)

    def get_emergency_exclusions(self) -> set[str]:
        return self._fallback.get_emergency_exclusions()

    def get_exclusion_lists(self) -> dict[str, set[str]]:
        return self._fallback.get_exclusion_lists()

    @property
    def source_name(self) -> str:
        return "nse_api"


class NseCsvDataSource(YamlDataSource):
    """
    Live universe data source backed by NSE archive CSV files.

    Overrides get_index_symbols() and get_fno_symbols() with live downloads
    from NSE's public archive server (no session cookies required). All other
    methods (exclusion lists, symbol master, liquidity, risk, corporate
    actions) are served by the YAML fallback.

    Falls back to YAML transparently if a CSV download fails, so a network
    blip never blocks service startup.
    """

    def __init__(
        self,
        universe_modes_path: str | Path | None = None,
        exclusion_path: str | Path | None = None,
    ) -> None:
        super().__init__(universe_modes_path=universe_modes_path, exclusion_path=exclusion_path)
        from shared.universe.feeds.csv_feed import NseCsvFeed
        self._csv_feed = NseCsvFeed()

    def get_index_symbols(self, index_name: str) -> set[str]:
        """Fetch live index constituents from NSE archive CSV; fall back to YAML."""
        live = self._csv_feed.get_index_symbols(index_name)
        if live:
            return live
        logger.warning(
            "nse_csv_data_source.live_empty index=%s — using YAML fallback",
            index_name,
        )
        return super().get_index_symbols(index_name)

    def get_fno_symbols(self) -> set[str]:
        """Fetch live F&O eligible symbols from NSE archive CSV; fall back to YAML."""
        live = self._csv_feed.get_fno_symbols()
        if live:
            return live
        logger.warning("nse_csv_data_source.fno_live_empty — using YAML fallback")
        return super().get_fno_symbols()

    @property
    def source_name(self) -> str:
        return "nse_csv_archive"


def build_data_source(
    use_live_api: bool = False,
    universe_modes_path: str | Path | None = None,
    exclusion_path: str | Path | None = None,
    bhavcopy_cache: Optional[Path] = None,
) -> UniverseDataSource:
    """
    Factory function to build the appropriate data source.

    Args:
        use_live_api: If True, return NseCsvDataSource (live NSE archive CSVs
                      with YAML fallback). If False, return YamlDataSource only.
        universe_modes_path: Override path to universe_modes.yaml.
        exclusion_path: Override path to exclusion_lists.yaml.
        bhavcopy_cache: Unused; kept for API compatibility.

    Returns:
        UniverseDataSource implementation.
    """
    if use_live_api:
        return NseCsvDataSource(
            universe_modes_path=universe_modes_path,
            exclusion_path=exclusion_path,
        )
    return YamlDataSource(
        universe_modes_path=universe_modes_path,
        exclusion_path=exclusion_path,
    )
