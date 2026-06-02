"""
Universe filter chain.

Each filter is an independent, composable check. The builder runs all enabled
filters and collects per-symbol decisions with reasons.

Filter execution order:
  1. ExclusionListFilter    — fast reject: delisted, SME, ETF, emergency
  2. IndexMembershipFilter  — must be in the configured index(es) for this mode
  3. EquityTypeFilter       — must be a regular equity (series EQ/BE), not F&O note or SME
  4. SurveillanceFilter     — ASM/GSM rejection
  5. LiquidityFilter        — ADV, volume, spread, active days, delivery pct
  6. RiskFilter             — penny stock, volatility, circuit frequency, low float
  7. CorporateActionFilter  — corporate action exclusion window

Each filter returns a list of (symbol, ExclusionReason, message) tuples for
symbols it rejects. Approved symbols are those surviving all filters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from shared.universe.data_sources import UniverseDataSource
from shared.universe.models import (
    CorporateActionType,
    ExclusionReason,
    LiquidityMetrics,
    RiskMetrics,
)
from shared.universe.modes import UniverseMode

logger = logging.getLogger(__name__)

_DEFAULT_LIQUIDITY_YAML = Path("configs/liquidity_filters.yaml")
_DEFAULT_RISK_YAML = Path("configs/risk_filters.yaml")


@dataclass
class FilterVerdict:
    """One rejection verdict for one symbol from one filter."""
    symbol: str
    reason: ExclusionReason
    message: str


# ── Helper: load YAML config for a specific mode ──────────────────────────────

def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        logger.warning("universe_filter.config_not_found path=%s", path)
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _mode_cfg(cfg: dict[str, Any], mode: UniverseMode) -> dict[str, Any]:
    """Merge default config with mode-specific overrides."""
    defaults = dict(cfg.get("defaults", {}))
    overrides = cfg.get("by_mode", {}).get(mode.value, {})
    defaults.update(overrides)
    return defaults


# ── Filter 1: Exclusion List ──────────────────────────────────────────────────

class ExclusionListFilter:
    """
    Fast reject based on static exclusion lists from exclusion_lists.yaml.

    Rejects: delisted, suspended, SME, ETF, REIT/InvIT, BSE-only, emergency exclusions.
    """

    def __init__(self, data_source: UniverseDataSource) -> None:
        self._data_source = data_source

    def apply(self, candidates: set[str]) -> list[FilterVerdict]:
        verdicts: list[FilterVerdict] = []

        exclusion_lists = self._data_source.get_exclusion_lists()

        _reason_map = {
            "delisted": ExclusionReason.DELISTED,
            "suspended": ExclusionReason.SUSPENDED,
            "sme": ExclusionReason.SME_STOCK,
            "etf": ExclusionReason.ETF,
            "reit_invit": ExclusionReason.REIT_OR_INVIT,
            "bse_only": ExclusionReason.BSE_ONLY,
            "emergency": ExclusionReason.EMERGENCY_EXCLUSION,
        }

        for sym in candidates:
            for list_name, reason in _reason_map.items():
                if sym in exclusion_lists.get(list_name, set()):
                    verdicts.append(FilterVerdict(
                        sym, reason,
                        f"Symbol in exclusion list '{list_name}'"
                    ))
                    break  # first match wins per symbol

        return verdicts


# ── Filter 2: Index Membership ────────────────────────────────────────────────

class IndexMembershipFilter:
    """
    Only approve symbols that are members of at least one configured index for the mode.
    """

    def __init__(
        self,
        data_source: UniverseDataSource,
        required_indices: list[str],
    ) -> None:
        self._data_source = data_source
        self._required_indices = required_indices

    def _resolve_approved_set(self) -> set[str]:
        approved: set[str] = set()
        for index_name in self._required_indices:
            symbols = self._data_source.get_index_symbols(index_name)
            if not symbols:
                logger.warning(
                    "universe_filter.index_membership_empty index=%s — check data source",
                    index_name,
                )
            approved |= symbols
        return approved

    def apply(self, candidates: set[str]) -> list[FilterVerdict]:
        approved_set = self._resolve_approved_set()
        if not approved_set:
            logger.error(
                "universe_filter.index_membership_all_empty indices=%s — "
                "all candidates will be rejected; check configs/universe_modes.yaml",
                self._required_indices,
            )
        verdicts: list[FilterVerdict] = []
        for sym in candidates:
            if sym not in approved_set:
                verdicts.append(FilterVerdict(
                    sym,
                    ExclusionReason.NOT_IN_INDEX,
                    f"Not a member of any required index: {self._required_indices}",
                ))
        return verdicts


# ── Filter 3: Equity Type ─────────────────────────────────────────────────────

class EquityTypeFilter:
    """
    Validates that the symbol is a regular equity (series EQ or BE).
    Excludes ETF, REIT, InvIT, preference shares — cross-check with SymbolMaster.
    """

    _ALLOWED_SERIES = frozenset({"EQ", "BE"})      # NSE series codes for tradable equities
    _BLOCKED_TYPES = frozenset({"MF", "SM", "N", "W", "IL"})  # mutual fund, SME, rights warrants

    def __init__(self, data_source: UniverseDataSource) -> None:
        self._data_source = data_source

    def apply(self, candidates: set[str]) -> list[FilterVerdict]:
        verdicts: list[FilterVerdict] = []
        for sym in candidates:
            master = self._data_source.get_symbol_master(sym)
            if master is None:
                verdicts.append(FilterVerdict(
                    sym,
                    ExclusionReason.SYMBOL_MASTER_MISSING,
                    "Symbol not found in symbol master — cannot validate instrument type",
                ))
                continue
            if master.series in self._BLOCKED_TYPES:
                verdicts.append(FilterVerdict(
                    sym,
                    ExclusionReason.NOT_EQUITY,
                    f"Instrument series '{master.series}' is not a tradable equity series",
                ))
        return verdicts


# ── Filter 4: Surveillance ────────────────────────────────────────────────────

class SurveillanceFilter:
    """
    Exclude symbols on NSE ASM or GSM surveillance lists.
    Policy controls what happens when the surveillance data source is unavailable.
    """

    def __init__(
        self,
        data_source: UniverseDataSource,
        exclude_asm: bool = True,
        exclude_gsm: bool = True,
        unavailable_policy: str = "WARN",
    ) -> None:
        self._data_source = data_source
        self._exclude_asm = exclude_asm
        self._exclude_gsm = exclude_gsm
        self._unavailable_policy = unavailable_policy.upper()

    def apply(self, candidates: set[str]) -> list[FilterVerdict]:
        verdicts: list[FilterVerdict] = []

        asm_set: set[str] = set()
        gsm_set: set[str] = set()

        try:
            if self._exclude_asm:
                asm_set = self._data_source.get_asm_symbols()
            if self._exclude_gsm:
                gsm_set = self._data_source.get_gsm_symbols()
        except Exception as exc:
            msg = f"Surveillance data source failed: {exc}"
            if self._unavailable_policy == "EXCLUDE":
                logger.error("universe_filter.surveillance_source_failed policy=EXCLUDE — blocking all: %s", msg)
                for sym in candidates:
                    verdicts.append(FilterVerdict(sym, ExclusionReason.DATA_UNAVAILABLE, msg))
                return verdicts
            logger.warning("universe_filter.surveillance_source_failed policy=WARN — continuing: %s", msg)
            return verdicts

        for sym in candidates:
            if self._exclude_asm and sym in asm_set:
                verdicts.append(FilterVerdict(sym, ExclusionReason.ASM_LISTED,
                                              "Symbol is on NSE ASM list"))
            elif self._exclude_gsm and sym in gsm_set:
                verdicts.append(FilterVerdict(sym, ExclusionReason.GSM_LISTED,
                                              "Symbol is on NSE GSM list"))
        return verdicts


# ── Filter 5: Liquidity ───────────────────────────────────────────────────────

class LiquidityFilter:
    """
    Multi-criterion liquidity filter.

    Checks: ADV (crores), ADV (volume), free-float market cap, bid-ask spread,
    active trading days, delivery percentage — all configurable per mode.
    """

    def __init__(
        self,
        data_source: UniverseDataSource,
        mode: UniverseMode,
        config_path: Path | None = None,
    ) -> None:
        self._data_source = data_source
        self._mode = mode
        full_cfg = _load_yaml(config_path or _DEFAULT_LIQUIDITY_YAML)
        self._cfg = _mode_cfg(full_cfg, mode)
        # Mode-specific policy takes priority over top-level default.
        # LIVE_ADVANCED overrides to EXCLUDE so unknown liquidity = fail-safe block.
        mode_override = full_cfg.get("by_mode", {}).get(mode.value, {})
        self._unavailable_policy = (
            mode_override.get("liquidity_unavailable_policy")
            or full_cfg.get("liquidity_unavailable_policy", "INCLUDE")
        ).upper()

    def apply(self, candidates: set[str], as_of: date) -> list[FilterVerdict]:
        verdicts: list[FilterVerdict] = []

        if not self._cfg.get("enabled", True):
            return verdicts

        for sym in candidates:
            metrics = self._data_source.get_liquidity_metrics(sym, as_of)

            if metrics is None or not metrics.data_available:
                if self._unavailable_policy == "EXCLUDE":
                    verdicts.append(FilterVerdict(
                        sym, ExclusionReason.DATA_UNAVAILABLE,
                        "Liquidity data unavailable — excluded per EXCLUDE policy",
                    ))
                else:
                    logger.warning(
                        "universe_filter.liquidity_data_missing symbol=%s date=%s — included per WARN policy",
                        sym, as_of,
                    )
                continue

            vd = self._check_metrics(sym, metrics)
            verdicts.extend(vd)

        return verdicts

    def _check_metrics(self, sym: str, m: LiquidityMetrics) -> list[FilterVerdict]:
        verdicts: list[FilterVerdict] = []
        cfg = self._cfg

        if m.adv_crores_20d is not None:
            floor = cfg.get("min_adv_crores_20d", 5.0)
            if m.adv_crores_20d < floor:
                verdicts.append(FilterVerdict(
                    sym, ExclusionReason.BELOW_LIQUIDITY_ADV,
                    f"ADV ₹{m.adv_crores_20d:.1f}Cr < min {floor}Cr (20d)",
                ))

        if m.adv_volume_20d is not None:
            floor_vol = cfg.get("min_adv_volume_20d", 50000)
            if m.adv_volume_20d < floor_vol:
                verdicts.append(FilterVerdict(
                    sym, ExclusionReason.BELOW_LIQUIDITY_VOLUME,
                    f"ADV volume {m.adv_volume_20d:,} < min {floor_vol:,} (20d)",
                ))

        if m.free_float_mcap_crores is not None:
            floor_mcap = cfg.get("min_free_float_mcap_crores", 500.0)
            if m.free_float_mcap_crores < floor_mcap:
                verdicts.append(FilterVerdict(
                    sym, ExclusionReason.BELOW_FREE_FLOAT_MCAP,
                    f"Free-float MCap ₹{m.free_float_mcap_crores:.0f}Cr < min {floor_mcap:.0f}Cr",
                ))

        if m.avg_bid_ask_spread_bps is not None:
            max_spread = cfg.get("max_bid_ask_spread_bps", 100.0)
            if m.avg_bid_ask_spread_bps > max_spread:
                verdicts.append(FilterVerdict(
                    sym, ExclusionReason.SPREAD_TOO_WIDE,
                    f"Spread {m.avg_bid_ask_spread_bps:.1f} bps > max {max_spread:.1f} bps",
                ))

        if m.active_days_last_20 is not None:
            min_days = cfg.get("min_active_days_last_20", 15)
            if m.active_days_last_20 < min_days:
                verdicts.append(FilterVerdict(
                    sym, ExclusionReason.INSUFFICIENT_ACTIVE_DAYS,
                    f"Only {m.active_days_last_20} active days in last 20 (min {min_days})",
                ))

        if m.delivery_pct_20d is not None:
            min_del = cfg.get("min_delivery_pct", 0.10)
            if m.delivery_pct_20d < min_del:
                verdicts.append(FilterVerdict(
                    sym, ExclusionReason.LOW_DELIVERY_PCT,
                    f"Delivery {m.delivery_pct_20d:.1%} < min {min_del:.1%} (20d)",
                ))

        if m.zero_volume_days_last_20 is not None:
            max_zero = cfg.get("max_zero_volume_days_last_20", 2)
            if m.zero_volume_days_last_20 > max_zero:
                verdicts.append(FilterVerdict(
                    sym, ExclusionReason.POOR_PRICE_DISCOVERY,
                    f"{m.zero_volume_days_last_20} zero-volume days in last 20 (max {max_zero})",
                ))

        return verdicts


# ── Filter 6: Risk ────────────────────────────────────────────────────────────

class RiskFilter:
    """
    Risk-based exclusion filter.

    Checks: penny stock price floor, abnormal volatility, circuit frequency, low float.
    """

    def __init__(
        self,
        data_source: UniverseDataSource,
        mode: UniverseMode,
        config_path: Path | None = None,
    ) -> None:
        self._data_source = data_source
        self._mode = mode
        self._full_cfg = _load_yaml(config_path or _DEFAULT_RISK_YAML)

    def _get(self, section: str, key: str, default: Any) -> Any:
        sec_cfg = dict(self._full_cfg.get(section, {}))
        override = sec_cfg.get("by_mode", {}).get(self._mode.value, {})
        return override.get(key, sec_cfg.get(key, default))

    def apply(self, candidates: set[str], as_of: date) -> list[FilterVerdict]:
        verdicts: list[FilterVerdict] = []

        # Mode-specific policy for missing risk metrics: LIVE_ADVANCED = EXCLUDE.
        by_mode = self._full_cfg.get("risk_metrics_by_mode", {}).get(self._mode.value, {})
        unavailable_policy = (
            by_mode.get("risk_metrics_unavailable_policy")
            or self._full_cfg.get("risk_metrics_unavailable_policy", "INCLUDE")
        ).upper()

        for sym in candidates:
            metrics = self._data_source.get_risk_metrics(sym, as_of)

            if metrics is None or not metrics.data_available:
                if unavailable_policy == "EXCLUDE":
                    verdicts.append(FilterVerdict(
                        sym, ExclusionReason.DATA_UNAVAILABLE,
                        "Risk metrics unavailable — excluded per EXCLUDE policy",
                    ))
                else:
                    logger.warning(
                        "universe_filter.risk_metrics_missing symbol=%s date=%s — included",
                        sym, as_of,
                    )
                continue

            verdicts.extend(self._check_metrics(sym, metrics))

        return verdicts

    def _check_metrics(self, sym: str, m: RiskMetrics) -> list[FilterVerdict]:
        verdicts: list[FilterVerdict] = []

        # ASM / GSM (belt-and-suspenders; SurveillanceFilter is primary)
        if m.is_asm_listed:
            verdicts.append(FilterVerdict(sym, ExclusionReason.ASM_LISTED,
                                          f"ASM stage {m.asm_stage}"))
        if m.is_gsm_listed:
            verdicts.append(FilterVerdict(sym, ExclusionReason.GSM_LISTED,
                                          f"GSM stage {m.gsm_stage}"))

        # Penny stock
        if self._full_cfg.get("penny_stock", {}).get("enabled", True):
            floor = self._get("penny_stock", "min_price_inr", 10.0)
            if m.last_close_price is not None and m.last_close_price < floor:
                verdicts.append(FilterVerdict(
                    sym, ExclusionReason.PENNY_STOCK,
                    f"Close ₹{m.last_close_price:.2f} < min ₹{floor:.2f}",
                ))

        # Circuit frequency
        if self._full_cfg.get("circuit_frequency", {}).get("enabled", True):
            max_circuits = self._get("circuit_frequency", "max_circuit_hits_last_20d", 3)
            if m.circuit_hits_last_20d is not None and m.circuit_hits_last_20d >= max_circuits:
                verdicts.append(FilterVerdict(
                    sym, ExclusionReason.CIRCUIT_FREQUENCY_HIGH,
                    f"{m.circuit_hits_last_20d} circuit hits in last 20 days (max {max_circuits})",
                ))

        # Volatility
        if self._full_cfg.get("volatility", {}).get("enabled", True):
            max_vol = self._get("volatility", "max_daily_volatility_pct", 8.0)
            if m.daily_volatility_pct_20d is not None and m.daily_volatility_pct_20d > max_vol:
                verdicts.append(FilterVerdict(
                    sym, ExclusionReason.ABNORMAL_VOLATILITY,
                    f"Daily vol {m.daily_volatility_pct_20d:.1f}% > max {max_vol:.1f}% (20d)",
                ))

        # Low float
        if self._full_cfg.get("low_float", {}).get("enabled", True):
            min_ff = self._get("low_float", "min_free_float_pct", 15.0)
            if m.free_float_pct is not None and m.free_float_pct < min_ff:
                verdicts.append(FilterVerdict(
                    sym, ExclusionReason.LOW_FLOAT,
                    f"Free-float {m.free_float_pct:.1f}% < min {min_ff:.1f}%",
                ))

        return verdicts


# ── Filter 7: Corporate Action ────────────────────────────────────────────────

class CorporateActionFilter:
    """
    Exclude symbols within the exclusion window around major corporate actions.

    Window definitions (from risk_filters.yaml → corporate_action_window):
      - pre_results_days: days before results announcement
      - post_split_days: days after split ex-date
      - post_bonus_days: days after bonus ex-date
      - post_merger_days: days after merger/demerger
    """

    _WINDOW_BY_ACTION: dict[CorporateActionType, str] = {
        CorporateActionType.STOCK_SPLIT: "exclude_post_split_days",
        CorporateActionType.BONUS_ISSUE: "exclude_post_bonus_days",
        CorporateActionType.MERGER: "exclude_post_merger_days",
        CorporateActionType.DEMERGER: "exclude_post_merger_days",
    }

    def __init__(
        self,
        data_source: UniverseDataSource,
        mode: UniverseMode,
        config_path: Path | None = None,
    ) -> None:
        self._data_source = data_source
        self._mode = mode
        cfg = _load_yaml(config_path or _DEFAULT_RISK_YAML)
        self._ca_cfg = cfg.get("corporate_action_window", {})

    def apply(self, candidates: set[str], as_of: date) -> list[FilterVerdict]:
        verdicts: list[FilterVerdict] = []

        if not self._ca_cfg.get("enabled", True):
            return verdicts

        # Mode-specific policy for CA data unavailable: LIVE_ADVANCED = EXCLUDE.
        _by_mode = self._ca_cfg.get("by_mode", {}).get(self._mode.value, {})
        _ca_policy = (
            _by_mode.get("data_source_unavailable_policy")
            or self._ca_cfg.get("data_source_unavailable_policy", "WARN")
        ).upper()

        for sym in candidates:
            try:
                actions = self._data_source.get_corporate_actions(sym, window_days=60)
            except Exception as exc:
                policy = _ca_policy
                if policy == "EXCLUDE":
                    verdicts.append(FilterVerdict(
                        sym, ExclusionReason.DATA_UNAVAILABLE,
                        f"Corporate action data unavailable: {exc}",
                    ))
                else:
                    logger.warning(
                        "universe_filter.corporate_action_data_failed symbol=%s: %s", sym, exc
                    )
                continue

            for action in actions:
                window_key = self._WINDOW_BY_ACTION.get(action.action_type)
                if window_key is None:
                    continue
                window_days = int(self._ca_cfg.get(window_key, 5))
                delta = (as_of - action.ex_date).days
                if 0 <= delta <= window_days:
                    verdicts.append(FilterVerdict(
                        sym,
                        ExclusionReason.CORPORATE_ACTION_WINDOW,
                        f"{action.action_type.value} ex-date {action.ex_date} "
                        f"({delta}d ago; exclusion window {window_days}d)",
                    ))
                    break

        return verdicts
