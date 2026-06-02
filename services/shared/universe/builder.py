"""
UniverseBuilder — assembles an immutable UniverseSnapshot for a given mode and trading date.

Usage:
    builder = UniverseBuilder.from_yaml_config()
    snapshot = builder.build(UniverseMode.PAPER_SAFE_START, date.today())

The builder:
  1. Loads the candidate symbol pool from index_membership config for the mode.
  2. Runs all enabled filters in sequence (exclusion → index → type → surveillance →
     liquidity → risk → corporate_action).
  3. Produces a UniverseSnapshot with approved_symbols and full decision audit trail.
  4. Logs observability metrics (size, added, removed, per-reason counts).

Failure modes:
  - If index data source returns empty, raises unless failure_policy is WARN.
  - If result is fewer than min_symbols_to_trade, snapshot is flagged as PARTIAL
    and a CRITICAL alert is emitted.
  - If snapshot generation fails entirely, raises UniverseSnapshotError.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from shared.universe.data_sources import UniverseDataSource, YamlDataSource, build_data_source
from shared.universe.filters import (
    CorporateActionFilter,
    ExclusionListFilter,
    EquityTypeFilter,
    FilterVerdict,
    IndexMembershipFilter,
    LiquidityFilter,
    RiskFilter,
    SurveillanceFilter,
)
from shared.universe.models import (
    ExclusionReason,
    SnapshotFailureMode,
    UniverseAuditLog,
    UniverseDecision,
    UniverseSnapshot,
)
from shared.universe.modes import UniverseMode

logger = logging.getLogger(__name__)

_DEFAULT_MODES_YAML = Path("configs/universe_modes.yaml")
_DEFAULT_RISK_YAML = Path("configs/risk_filters.yaml")


class UniverseSnapshotError(RuntimeError):
    """Raised when snapshot generation fails in a way that cannot be recovered."""


class UniverseBuilder:
    """
    Builds a UniverseSnapshot for a given mode and trading date.

    Args:
        data_source: Universe data source to use.
        modes_config_path: Path to universe_modes.yaml.
        liquidity_config_path: Path to liquidity_filters.yaml.
        risk_config_path: Path to risk_filters.yaml.
        skip_liquidity_filter: Disable liquidity filter (e.g. when data unavailable).
        skip_risk_filter: Disable risk filter (e.g. when data unavailable).
    """

    def __init__(
        self,
        data_source: UniverseDataSource | None = None,
        modes_config_path: str | Path | None = None,
        liquidity_config_path: str | Path | None = None,
        risk_config_path: str | Path | None = None,
    ) -> None:
        self._data_source = data_source or YamlDataSource()
        self._modes_path = Path(modes_config_path or _DEFAULT_MODES_YAML)
        self._liq_path = Path(liquidity_config_path) if liquidity_config_path else None
        self._risk_path = Path(risk_config_path) if risk_config_path else None
        self._modes_cfg: dict[str, Any] = {}

    def _load_modes_cfg(self) -> None:
        if self._modes_cfg:
            return
        if not self._modes_path.exists():
            raise UniverseSnapshotError(
                f"universe_modes.yaml not found at {self._modes_path}. "
                "Copy configs/universe_modes.yaml and configure index membership."
            )
        with open(self._modes_path, "r", encoding="utf-8") as fh:
            self._modes_cfg = yaml.safe_load(fh) or {}

    def _get_mode_cfg(self, mode: UniverseMode) -> dict[str, Any]:
        self._load_modes_cfg()
        cfg = self._modes_cfg.get("modes", {}).get(mode.value)
        if cfg is None:
            raise UniverseSnapshotError(
                f"Mode '{mode.value}' not found in {self._modes_path}. "
                "Add it to the modes section."
            )
        return cfg

    def _get_candidate_symbols(self, mode_cfg: dict[str, Any]) -> set[str]:
        """Get the initial candidate set from the configured index memberships."""
        indices = mode_cfg.get("index_membership", [])
        candidates: set[str] = set()
        for index_name in indices:
            symbols = self._data_source.get_index_symbols(index_name)
            logger.info(
                "universe_builder.index_loaded index=%s symbols=%d", index_name, len(symbols)
            )
            candidates |= symbols
        return candidates

    def build(self, mode: UniverseMode, trading_date: date) -> UniverseSnapshot:
        """
        Build and return an immutable UniverseSnapshot.

        Args:
            mode: Which universe mode to build.
            trading_date: The trading date for which this snapshot is valid.

        Returns:
            UniverseSnapshot with approved_symbols and full decision record.

        Raises:
            UniverseSnapshotError: If snapshot cannot be built (e.g. no candidates).
        """
        logger.info(
            "universe_builder.build_start mode=%s trading_date=%s data_source=%s",
            mode.value, trading_date, self._data_source.source_name,
        )

        mode_cfg = self._get_mode_cfg(mode)
        candidates = self._get_candidate_symbols(mode_cfg)

        if not candidates:
            raise UniverseSnapshotError(
                f"No candidate symbols found for mode {mode.value}. "
                "Check index_membership in universe_modes.yaml and data source connectivity."
            )

        logger.info(
            "universe_builder.candidates_loaded mode=%s count=%d", mode.value, len(candidates)
        )

        # ── Run filter chain ──────────────────────────────────────────────────
        all_verdicts: dict[str, list[FilterVerdict]] = {sym: [] for sym in candidates}

        # Filter 1: Exclusion lists (fast reject)
        excl_filter = ExclusionListFilter(self._data_source)
        for v in excl_filter.apply(candidates):
            all_verdicts[v.symbol].append(v)

        # Filter 2: Index membership (redundant safety check)
        indices = mode_cfg.get("index_membership", [])
        idx_filter = IndexMembershipFilter(self._data_source, required_indices=indices)
        for v in idx_filter.apply(candidates):
            all_verdicts[v.symbol].append(v)

        # Filter 3: Equity type check
        eq_filter = EquityTypeFilter(self._data_source)
        for v in eq_filter.apply(candidates):
            all_verdicts[v.symbol].append(v)

        # Filter 4: Surveillance (ASM/GSM)
        risk_yaml = _load_yaml(self._risk_path) if self._risk_path else {}
        surv_cfg = risk_yaml.get("surveillance", {})
        surv_filter = SurveillanceFilter(
            data_source=self._data_source,
            exclude_asm=surv_cfg.get("exclude_asm", True),
            exclude_gsm=surv_cfg.get("exclude_gsm", True),
            unavailable_policy=(
                (surv_cfg.get("by_mode", {}).get(mode.value, {}).get("data_source_unavailable_policy")
                 or surv_cfg.get("data_source_unavailable_policy", "WARN"))
            ),
        )
        for v in surv_filter.apply(candidates):
            all_verdicts[v.symbol].append(v)

        # Filter 5: Liquidity
        if mode_cfg.get("apply_liquidity_filters", True):
            liq_filter = LiquidityFilter(
                data_source=self._data_source,
                mode=mode,
                config_path=self._liq_path,
            )
            for v in liq_filter.apply(candidates, trading_date):
                all_verdicts[v.symbol].append(v)

        # Filter 6: Risk
        if mode_cfg.get("apply_risk_filters", True):
            risk_filter = RiskFilter(
                data_source=self._data_source,
                mode=mode,
                config_path=self._risk_path,
            )
            for v in risk_filter.apply(candidates, trading_date):
                all_verdicts[v.symbol].append(v)

        # Filter 7: Corporate action window
        if mode_cfg.get("apply_corporate_action_filters", True):
            ca_filter = CorporateActionFilter(
                data_source=self._data_source,
                mode=mode,
                config_path=self._risk_path,
            )
            for v in ca_filter.apply(candidates, trading_date):
                all_verdicts[v.symbol].append(v)

        # ── Build decisions and approved set ─────────────────────────────────
        decisions: list[UniverseDecision] = []
        approved_symbols: set[str] = set()

        max_symbols = mode_cfg.get("max_symbols", 999)

        for sym in sorted(candidates):
            verdicts = all_verdicts[sym]
            rejected_codes = tuple(v.reason for v in verdicts)
            rejected_messages = tuple(v.message for v in verdicts)

            if verdicts:
                decisions.append(UniverseDecision(
                    symbol=sym,
                    market="NSE",
                    approved=False,
                    reasons=rejected_messages,
                    exclusion_codes=rejected_codes,
                ))
            else:
                approved_symbols.add(sym)
                decisions.append(UniverseDecision(
                    symbol=sym,
                    market="NSE",
                    approved=True,
                    reasons=("All filters passed",),
                ))

        # Cap at max_symbols (alphabetical order = deterministic)
        if len(approved_symbols) > max_symbols:
            sorted_approved = sorted(approved_symbols)
            capped = set(sorted_approved[:max_symbols])
            for sym in sorted_approved[max_symbols:]:
                decisions.append(UniverseDecision(
                    symbol=sym,
                    market="NSE",
                    approved=False,
                    reasons=(f"Capped at max_symbols={max_symbols}",),
                    exclusion_codes=(ExclusionReason.MANUAL_EXCLUSION,),
                ))
            approved_symbols = capped
            logger.warning(
                "universe_builder.capped_at_max mode=%s max=%d final=%d",
                mode.value, max_symbols, len(approved_symbols),
            )

        # ── Failure mode detection ─────────────────────────────────────────
        min_symbols = mode_cfg.get("min_symbols_to_trade", 10)
        failure_mode: SnapshotFailureMode | None = None
        failure_details = ""

        if not approved_symbols:
            failure_mode = SnapshotFailureMode.FAILED
            failure_details = (
                f"Zero approved symbols for mode={mode.value} date={trading_date}. "
                "Check index data sources and filter thresholds."
            )
            logger.critical(
                "universe_builder.snapshot_failed mode=%s date=%s reason=%s",
                mode.value, trading_date, failure_details,
            )
        elif len(approved_symbols) < min_symbols:
            failure_mode = SnapshotFailureMode.PARTIAL
            failure_details = (
                f"Only {len(approved_symbols)} symbols approved, below min_symbols_to_trade={min_symbols}. "
                "Degraded-mode alert raised."
            )
            logger.critical(
                "universe_builder.snapshot_degraded mode=%s date=%s approved=%d min=%d",
                mode.value, trading_date, len(approved_symbols), min_symbols,
            )
        elif self._data_source.source_name == "yaml_fallback":
            failure_mode = SnapshotFailureMode.FALLBACK
            failure_details = "Built from YAML config fallback; live NSE API not available."
            logger.warning(
                "universe_builder.snapshot_from_yaml_fallback mode=%s date=%s approved=%d",
                mode.value, trading_date, len(approved_symbols),
            )

        snapshot = UniverseSnapshot(
            mode=mode,
            trading_date=trading_date,
            approved_symbols=frozenset(approved_symbols),
            decisions=tuple(decisions),
            generated_at=datetime.now(timezone.utc),
            data_sources_used=frozenset([self._data_source.source_name]),
            failure_mode=failure_mode,
            failure_details=failure_details,
        )

        self._log_metrics(snapshot, candidates)
        return snapshot

    def _log_metrics(self, snapshot: UniverseSnapshot, candidates: set[str]) -> None:
        excluded = candidates - snapshot.approved_symbols
        exc_summary = snapshot.exclusion_summary()

        logger.info(
            "universe_builder.snapshot_built mode=%s date=%s approved=%d excluded=%d "
            "failure_mode=%s checksum=%s",
            snapshot.mode.value,
            snapshot.trading_date,
            snapshot.size,
            len(excluded),
            snapshot.failure_mode.value if snapshot.failure_mode else "None",
            snapshot.checksum,
        )

        for reason, syms in exc_summary.items():
            logger.info(
                "universe_builder.exclusion_reason reason=%s count=%d symbols=%s",
                reason, len(syms), ",".join(sorted(syms)[:10]) + ("..." if len(syms) > 10 else ""),
            )

    @classmethod
    def from_yaml_config(
        cls,
        modes_config_path: str | Path | None = None,
        liquidity_config_path: str | Path | None = None,
        risk_config_path: str | Path | None = None,
    ) -> "UniverseBuilder":
        """Convenience factory: build with YAML data source (offline, always safe)."""
        return cls(
            data_source=YamlDataSource(universe_modes_path=modes_config_path),
            modes_config_path=modes_config_path,
            liquidity_config_path=liquidity_config_path,
            risk_config_path=risk_config_path,
        )


def _load_yaml(path: Path) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
