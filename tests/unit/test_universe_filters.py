"""
Tests for universe filter chain — all filter classes.

Tests cover:
  - NIFTY 50 membership enforcement (PAPER_SAFE_START only contains NIFTY 50)
  - NIFTY 100 + F&O for PAPER_EXPAND
  - LIVE_ADVANCED applies stricter liquidity thresholds
  - Illiquid stock exclusion
  - Penny stock exclusion
  - ASM/GSM exclusion
  - Corporate action exclusion window
  - Suspended/delisted exclusion
  - BSE-only exclusion
  - SME exclusion
  - Snapshot immutability (frozenset)
  - Paper/live snapshot isolation
"""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
import yaml

from shared.universe.data_sources import YamlDataSource
from shared.universe.filters import (
    ExclusionListFilter,
    IndexMembershipFilter,
    LiquidityFilter,
    RiskFilter,
    SurveillanceFilter,
)
from shared.universe.models import ExclusionReason, LiquidityMetrics, RiskMetrics
from shared.universe.modes import UniverseMode


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _make_liq(symbol: str, adv_crores: float = 100.0, adv_volume: int = 500000,
               spread_bps: float = 10.0, active_days: int = 20,
               delivery_pct: float = 0.40, zero_vol_days: int = 0) -> LiquidityMetrics:
    return LiquidityMetrics(
        symbol=symbol,
        trading_date=date(2026, 5, 26),
        adv_crores_20d=adv_crores,
        adv_volume_20d=adv_volume,
        avg_bid_ask_spread_bps=spread_bps,
        active_days_last_20=active_days,
        delivery_pct_20d=delivery_pct,
        zero_volume_days_last_20=zero_vol_days,
        free_float_mcap_crores=10000.0,
        data_available=True,
    )


def _make_risk(symbol: str, price: float = 1000.0, volatility: float = 2.0,
               circuits: int = 0, float_pct: float = 30.0,
               asm: bool = False, gsm: bool = False) -> RiskMetrics:
    return RiskMetrics(
        symbol=symbol,
        trading_date=date(2026, 5, 26),
        last_close_price=price,
        daily_volatility_pct_20d=volatility,
        circuit_hits_last_20d=circuits,
        free_float_pct=float_pct,
        is_asm_listed=asm,
        is_gsm_listed=gsm,
        data_available=True,
    )


class StubDataSource(YamlDataSource):
    """YamlDataSource subclass with injectable stubs for test control."""

    def __init__(
        self,
        index_map: dict[str, set[str]] | None = None,
        asm_symbols: set[str] | None = None,
        gsm_symbols: set[str] | None = None,
        liquidity_map: dict[str, LiquidityMetrics] | None = None,
        risk_map: dict[str, RiskMetrics] | None = None,
        exclusion_map: dict[str, set[str]] | None = None,
    ) -> None:
        super().__init__()
        self._index_map = index_map or {}
        self._asm = asm_symbols or set()
        self._gsm = gsm_symbols or set()
        self._liq_map = liquidity_map or {}
        self._risk_map = risk_map or {}
        self._exc_map = exclusion_map or {}

    def get_index_symbols(self, index_name: str) -> set[str]:
        return self._index_map.get(index_name, set())

    def get_asm_symbols(self) -> set[str]:
        return self._asm

    def get_gsm_symbols(self) -> set[str]:
        return self._gsm

    def get_liquidity_metrics(self, symbol: str, as_of: date) -> Optional[LiquidityMetrics]:
        return self._liq_map.get(symbol)

    def get_risk_metrics(self, symbol: str, as_of: date) -> Optional[RiskMetrics]:
        return self._risk_map.get(symbol)

    def get_exclusion_lists(self) -> dict[str, set[str]]:
        return self._exc_map


# ── IndexMembershipFilter tests ───────────────────────────────────────────────

class TestIndexMembershipFilter:
    def test_nifty50_only_in_paper_safe_start(self) -> None:
        nifty50 = {"RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS"}
        ds = StubDataSource(index_map={"NIFTY_50": nifty50})
        filt = IndexMembershipFilter(ds, required_indices=["NIFTY_50"])

        candidates = nifty50 | {"BEML", "TEXRAIL", "TILAKNAGAR"}  # non-NIFTY stocks
        verdicts = filt.apply(candidates)

        excluded = {v.symbol for v in verdicts}
        assert excluded == {"BEML", "TEXRAIL", "TILAKNAGAR"}

    def test_all_nifty50_approved(self) -> None:
        nifty50 = {"RELIANCE", "HDFCBANK", "ICICIBANK"}
        ds = StubDataSource(index_map={"NIFTY_50": nifty50})
        filt = IndexMembershipFilter(ds, required_indices=["NIFTY_50"])
        verdicts = filt.apply(nifty50)
        assert verdicts == []

    def test_paper_expand_includes_fno(self) -> None:
        nifty100 = {"RELIANCE", "HDFCBANK", "ADANIGREEN"}
        fno_extra = {"HAL", "BEML", "TATAPOWER"}
        ds = StubDataSource(index_map={"NIFTY_100": nifty100, "FNO": fno_extra})
        filt = IndexMembershipFilter(ds, required_indices=["NIFTY_100", "FNO"])

        candidates = nifty100 | fno_extra | {"RANDOMSTOCK"}
        verdicts = filt.apply(candidates)
        excluded = {v.symbol for v in verdicts}
        assert excluded == {"RANDOMSTOCK"}

    def test_empty_index_data_rejects_all(self) -> None:
        ds = StubDataSource(index_map={})
        filt = IndexMembershipFilter(ds, required_indices=["NIFTY_50"])
        verdicts = filt.apply({"RELIANCE", "HDFCBANK"})
        assert len(verdicts) == 2
        assert all(v.reason == ExclusionReason.NOT_IN_INDEX for v in verdicts)

    def test_live_advanced_uses_nifty200_indices(self) -> None:
        nifty200 = {"RELIANCE", "HDFCBANK", "HAL", "BEML", "TATAPOWER"}
        outside = {"TILAKNAGAR", "TEXRAIL"}
        ds = StubDataSource(index_map={"NIFTY_200": nifty200})
        filt = IndexMembershipFilter(ds, required_indices=["NIFTY_200"])

        verdicts = filt.apply(nifty200 | outside)
        excluded = {v.symbol for v in verdicts}
        assert excluded == outside


# ── SurveillanceFilter tests ──────────────────────────────────────────────────

class TestSurveillanceFilter:
    def test_asm_symbol_excluded(self) -> None:
        ds = StubDataSource(asm_symbols={"XYZPENNY"})
        filt = SurveillanceFilter(ds, exclude_asm=True, exclude_gsm=True)
        verdicts = filt.apply({"XYZPENNY", "RELIANCE"})
        excluded = {v.symbol for v in verdicts}
        assert "XYZPENNY" in excluded
        assert "RELIANCE" not in excluded

    def test_gsm_symbol_excluded(self) -> None:
        ds = StubDataSource(gsm_symbols={"GHOSTSTOCK"})
        filt = SurveillanceFilter(ds, exclude_asm=True, exclude_gsm=True)
        verdicts = filt.apply({"GHOSTSTOCK", "SBIN"})
        excluded = {v.symbol for v in verdicts}
        assert "GHOSTSTOCK" in excluded

    def test_asm_reason_code(self) -> None:
        ds = StubDataSource(asm_symbols={"ASMSTK"})
        filt = SurveillanceFilter(ds, exclude_asm=True)
        verdicts = filt.apply({"ASMSTK"})
        assert verdicts[0].reason == ExclusionReason.ASM_LISTED

    def test_gsm_reason_code(self) -> None:
        ds = StubDataSource(gsm_symbols={"GSMSTK"})
        filt = SurveillanceFilter(ds, exclude_gsm=True)
        verdicts = filt.apply({"GSMSTK"})
        assert verdicts[0].reason == ExclusionReason.GSM_LISTED

    def test_surveillance_data_unavailable_warn_policy_includes(self) -> None:
        """WARN policy: data source failure → symbol included with warning."""
        ds = MagicMock()
        ds.get_asm_symbols.side_effect = Exception("NSE API down")
        ds.get_gsm_symbols.side_effect = Exception("NSE API down")
        filt = SurveillanceFilter(ds, exclude_asm=True, exclude_gsm=True, unavailable_policy="WARN")
        verdicts = filt.apply({"RELIANCE"})
        assert verdicts == []  # WARN = include

    def test_surveillance_data_unavailable_exclude_policy_blocks_all(self) -> None:
        """EXCLUDE policy: data source failure → all symbols rejected."""
        ds = MagicMock()
        ds.get_asm_symbols.side_effect = Exception("NSE API down")
        ds.get_gsm_symbols.return_value = set()
        filt = SurveillanceFilter(ds, exclude_asm=True, exclude_gsm=True, unavailable_policy="EXCLUDE")
        verdicts = filt.apply({"RELIANCE", "HDFCBANK"})
        excluded = {v.symbol for v in verdicts}
        assert excluded == {"RELIANCE", "HDFCBANK"}


# ── LiquidityFilter tests ─────────────────────────────────────────────────────

class TestLiquidityFilter:
    def _make_yaml_cfg(self, mode_adv: float = 10.0) -> Path:
        cfg = {
            "liquidity_unavailable_policy": "INCLUDE",
            "defaults": {"enabled": True, "min_adv_crores_20d": 5.0},
            "by_mode": {
                "PAPER_SAFE_START": {"enabled": True, "min_adv_crores_20d": 20.0},
                "PAPER_EXPAND": {"enabled": True, "min_adv_crores_20d": 10.0},
                "LIVE_ADVANCED": {"enabled": True, "min_adv_crores_20d": 25.0},
            }
        }
        import tempfile, os
        path = Path(tempfile.mktemp(suffix=".yaml"))
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f)
        return path

    def test_illiquid_stock_excluded(self) -> None:
        liq_map = {"ILLIQUID": _make_liq("ILLIQUID", adv_crores=0.5)}
        ds = StubDataSource(liquidity_map=liq_map)
        cfg_path = self._make_yaml_cfg()
        filt = LiquidityFilter(ds, UniverseMode.PAPER_EXPAND, config_path=cfg_path)
        verdicts = filt.apply({"ILLIQUID"}, date(2026, 5, 26))
        excluded = {v.symbol for v in verdicts}
        assert "ILLIQUID" in excluded
        assert any(v.reason == ExclusionReason.BELOW_LIQUIDITY_ADV for v in verdicts)

    def test_liquid_stock_passes(self) -> None:
        liq_map = {"RELIANCE": _make_liq("RELIANCE", adv_crores=500.0)}
        ds = StubDataSource(liquidity_map=liq_map)
        cfg_path = self._make_yaml_cfg()
        filt = LiquidityFilter(ds, UniverseMode.PAPER_EXPAND, config_path=cfg_path)
        verdicts = filt.apply({"RELIANCE"}, date(2026, 5, 26))
        assert verdicts == []

    def test_live_mode_stricter_than_paper(self) -> None:
        # 15 crore ADV: passes PAPER_EXPAND (min 10Cr) but fails LIVE_ADVANCED (min 25Cr)
        liq_map = {"MIDCAPSTOCK": _make_liq("MIDCAPSTOCK", adv_crores=15.0)}
        ds = StubDataSource(liquidity_map=liq_map)
        cfg_path = self._make_yaml_cfg()

        paper_filt = LiquidityFilter(ds, UniverseMode.PAPER_EXPAND, config_path=cfg_path)
        paper_verdicts = paper_filt.apply({"MIDCAPSTOCK"}, date(2026, 5, 26))
        assert paper_verdicts == [], "Should pass PAPER_EXPAND"

        live_filt = LiquidityFilter(ds, UniverseMode.LIVE_ADVANCED, config_path=cfg_path)
        live_verdicts = live_filt.apply({"MIDCAPSTOCK"}, date(2026, 5, 26))
        assert any(v.reason == ExclusionReason.BELOW_LIQUIDITY_ADV for v in live_verdicts), \
            "Should fail LIVE_ADVANCED"

    def test_missing_liquidity_data_included_by_default(self) -> None:
        ds = StubDataSource(liquidity_map={})  # no data for SBIN
        cfg_path = self._make_yaml_cfg()
        filt = LiquidityFilter(ds, UniverseMode.PAPER_EXPAND, config_path=cfg_path)
        verdicts = filt.apply({"SBIN"}, date(2026, 5, 26))
        assert verdicts == []  # INCLUDE policy — included with warning


# ── RiskFilter tests ──────────────────────────────────────────────────────────

class TestRiskFilter:
    def _make_yaml_cfg(self) -> Path:
        cfg = {
            "penny_stock": {"enabled": True, "min_price_inr": 10.0,
                           "by_mode": {"PAPER_SAFE_START": {"min_price_inr": 50.0},
                                       "LIVE_ADVANCED": {"min_price_inr": 30.0}}},
            "surveillance": {"enabled": True, "exclude_asm": True, "exclude_gsm": True},
            "circuit_frequency": {"enabled": True, "max_circuit_hits_last_20d": 3,
                                  "by_mode": {"LIVE_ADVANCED": {"max_circuit_hits_last_20d": 2}}},
            "volatility": {"enabled": True, "max_daily_volatility_pct": 8.0,
                           "by_mode": {"LIVE_ADVANCED": {"max_daily_volatility_pct": 6.0}}},
            "low_float": {"enabled": True, "min_free_float_pct": 15.0},
        }
        import tempfile
        path = Path(tempfile.mktemp(suffix=".yaml"))
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f)
        return path

    def test_penny_stock_excluded(self) -> None:
        risk_map = {"PENNYSTK": _make_risk("PENNYSTK", price=5.0)}
        ds = StubDataSource(risk_map=risk_map)
        cfg = self._make_yaml_cfg()
        filt = RiskFilter(ds, UniverseMode.PAPER_EXPAND, config_path=cfg)
        verdicts = filt.apply({"PENNYSTK"}, date(2026, 5, 26))
        assert any(v.reason == ExclusionReason.PENNY_STOCK for v in verdicts)

    def test_asm_stock_excluded_via_risk_metrics(self) -> None:
        risk_map = {"ASMSTK": _make_risk("ASMSTK", asm=True)}
        ds = StubDataSource(risk_map=risk_map)
        cfg = self._make_yaml_cfg()
        filt = RiskFilter(ds, UniverseMode.PAPER_EXPAND, config_path=cfg)
        verdicts = filt.apply({"ASMSTK"}, date(2026, 5, 26))
        assert any(v.reason == ExclusionReason.ASM_LISTED for v in verdicts)

    def test_high_circuit_frequency_excluded(self) -> None:
        risk_map = {"CIRCUITSTK": _make_risk("CIRCUITSTK", circuits=5)}
        ds = StubDataSource(risk_map=risk_map)
        cfg = self._make_yaml_cfg()
        filt = RiskFilter(ds, UniverseMode.PAPER_EXPAND, config_path=cfg)
        verdicts = filt.apply({"CIRCUITSTK"}, date(2026, 5, 26))
        assert any(v.reason == ExclusionReason.CIRCUIT_FREQUENCY_HIGH for v in verdicts)

    def test_abnormal_volatility_excluded(self) -> None:
        risk_map = {"VOLSTK": _make_risk("VOLSTK", volatility=12.0)}
        ds = StubDataSource(risk_map=risk_map)
        cfg = self._make_yaml_cfg()
        filt = RiskFilter(ds, UniverseMode.PAPER_EXPAND, config_path=cfg)
        verdicts = filt.apply({"VOLSTK"}, date(2026, 5, 26))
        assert any(v.reason == ExclusionReason.ABNORMAL_VOLATILITY for v in verdicts)

    def test_live_mode_stricter_volatility(self) -> None:
        # 7% vol: passes PAPER_EXPAND (max 8%) but fails LIVE_ADVANCED (max 6%)
        risk_map = {"MIDVOLSTK": _make_risk("MIDVOLSTK", volatility=7.0)}
        ds = StubDataSource(risk_map=risk_map)
        cfg = self._make_yaml_cfg()

        paper_filt = RiskFilter(ds, UniverseMode.PAPER_EXPAND, config_path=cfg)
        assert paper_filt.apply({"MIDVOLSTK"}, date(2026, 5, 26)) == []

        live_filt = RiskFilter(ds, UniverseMode.LIVE_ADVANCED, config_path=cfg)
        live_verdicts = live_filt.apply({"MIDVOLSTK"}, date(2026, 5, 26))
        assert any(v.reason == ExclusionReason.ABNORMAL_VOLATILITY for v in live_verdicts)

    def test_normal_stock_passes_all_risk_filters(self) -> None:
        risk_map = {"RELIANCE": _make_risk("RELIANCE", price=2800.0, volatility=1.5,
                                            circuits=0, float_pct=40.0)}
        ds = StubDataSource(risk_map=risk_map)
        cfg = self._make_yaml_cfg()
        filt = RiskFilter(ds, UniverseMode.LIVE_ADVANCED, config_path=cfg)
        verdicts = filt.apply({"RELIANCE"}, date(2026, 5, 26))
        assert verdicts == []


# ── ExclusionListFilter tests ─────────────────────────────────────────────────

class TestExclusionListFilter:
    def test_delisted_stock_excluded(self) -> None:
        ds = StubDataSource(exclusion_map={"delisted": {"OLDCOMPANY"}, "emergency": set(),
                                           "sme": set(), "etf": set(), "reit_invit": set(),
                                           "bse_only": set(), "suspended": set()})
        filt = ExclusionListFilter(ds)
        verdicts = filt.apply({"OLDCOMPANY", "RELIANCE"})
        excluded = {v.symbol for v in verdicts}
        assert "OLDCOMPANY" in excluded
        assert "RELIANCE" not in excluded

    def test_sme_stock_excluded(self) -> None:
        ds = StubDataSource(exclusion_map={"sme": {"SMECO"}, "delisted": set(),
                                           "emergency": set(), "etf": set(),
                                           "reit_invit": set(), "bse_only": set(),
                                           "suspended": set()})
        filt = ExclusionListFilter(ds)
        verdicts = filt.apply({"SMECO"})
        assert verdicts[0].reason == ExclusionReason.SME_STOCK

    def test_etf_excluded(self) -> None:
        ds = StubDataSource(exclusion_map={"etf": {"NIFTYBEES"}, "delisted": set(),
                                           "emergency": set(), "sme": set(),
                                           "reit_invit": set(), "bse_only": set(),
                                           "suspended": set()})
        filt = ExclusionListFilter(ds)
        verdicts = filt.apply({"NIFTYBEES"})
        assert verdicts[0].reason == ExclusionReason.ETF

    def test_emergency_exclusion(self) -> None:
        ds = StubDataSource(exclusion_map={"emergency": {"CRISIS"}, "delisted": set(),
                                           "sme": set(), "etf": set(),
                                           "reit_invit": set(), "bse_only": set(),
                                           "suspended": set()})
        filt = ExclusionListFilter(ds)
        verdicts = filt.apply({"CRISIS", "SBIN"})
        excluded = {v.symbol for v in verdicts}
        assert "CRISIS" in excluded
        assert "SBIN" not in excluded

    def test_suspended_stock_excluded(self) -> None:
        ds = StubDataSource(exclusion_map={"suspended": {"SUSPENDEDCO"}, "delisted": set(),
                                           "emergency": set(), "sme": set(),
                                           "etf": set(), "reit_invit": set(),
                                           "bse_only": set()})
        filt = ExclusionListFilter(ds)
        verdicts = filt.apply({"SUSPENDEDCO"})
        assert verdicts[0].reason == ExclusionReason.SUSPENDED
