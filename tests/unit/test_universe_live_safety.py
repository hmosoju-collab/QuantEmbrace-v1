"""
Tests: live-mode safety — missing data, stale snapshot, PARTIAL snapshot, non-YAML exclusion.

Production-safety review items T1–T9:

  T1  LIVE_ADVANCED + liquidity data unavailable → symbol EXCLUDED (EXCLUDE policy)
  T2  LIVE_ADVANCED + risk metrics unavailable  → symbol EXCLUDED (EXCLUDE policy)
  T3  LIVE_ADVANCED + surveillance source raises → all symbols BLOCKED (EXCLUDE policy)
  T4  LIVE_ADVANCED + CA data source raises     → symbol EXCLUDED (EXCLUDE policy)
  T5  Stale snapshot (yesterday) + LIVE mode   → validate() rejects
  T6  Stale snapshot (yesterday) + PAPER mode  → validate() warns and allows
  T8  PARTIAL snapshot + LIVE mode             → validate() rejects
  T9  ExclusionListFilter with NseApiDataSource → delisted/ETF symbols correctly excluded

  (T7 — snapshot mode mismatch on update — is already covered in
   test_order_validator_universe.py:TestSnapshotUpdate.test_update_snapshot_rejects_mode_mismatch)
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pytest
import yaml

from shared.universe.data_sources import NseApiDataSource, YamlDataSource
from shared.universe.filters import (
    CorporateActionFilter,
    ExclusionListFilter,
    LiquidityFilter,
    RiskFilter,
    SurveillanceFilter,
)
from shared.universe.models import (
    CorporateActionEvent,
    ExclusionReason,
    LiquidityMetrics,
    RiskMetrics,
    SnapshotFailureMode,
    SymbolMaster,
    UniverseDecision,
    UniverseSnapshot,
    ValidationResult,
)
from shared.universe.modes import UniverseMode
from shared.universe.order_validator import UniverseOrderValidator


# ── Shared test data ──────────────────────────────────────────────────────────

_TODAY = date(2026, 5, 26)
_YESTERDAY = date(2026, 5, 25)

_NIFTY50_SYMBOLS = frozenset({
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "SBIN", "AXISBANK", "KOTAKBANK", "LT", "NTPC",
})


# ── Stub helpers ──────────────────────────────────────────────────────────────

class _NullMetricsDataSource(YamlDataSource):
    """Data source that returns no liquidity or risk metrics (simulates missing data)."""

    def get_liquidity_metrics(self, symbol: str, as_of: date) -> Optional[LiquidityMetrics]:
        return None

    def get_risk_metrics(self, symbol: str, as_of: date) -> Optional[RiskMetrics]:
        return None

    def get_asm_symbols(self) -> set[str]:
        return set()

    def get_gsm_symbols(self) -> set[str]:
        return set()

    def get_corporate_actions(self, symbol: str, window_days: int = 30) -> list[CorporateActionEvent]:
        return []


class _RaisingSurveillanceDataSource(YamlDataSource):
    """Data source that raises when surveillance lists are requested."""

    def get_asm_symbols(self) -> set[str]:
        raise ConnectionError("NSE surveillance endpoint unreachable")

    def get_gsm_symbols(self) -> set[str]:
        raise ConnectionError("NSE surveillance endpoint unreachable")


class _RaisingCorporateActionDataSource(YamlDataSource):
    """Data source that raises when corporate action data is requested."""

    def get_corporate_actions(self, symbol: str, window_days: int = 30) -> list[CorporateActionEvent]:
        raise ConnectionError("Corporate action feed unavailable")


def _make_snapshot(
    mode: UniverseMode,
    symbols: frozenset[str],
    trading_date: date = _TODAY,
    failure_mode: Optional[SnapshotFailureMode] = None,
) -> UniverseSnapshot:
    decisions = tuple(
        UniverseDecision(symbol=s, market="NSE", approved=True, reasons=("All filters passed",))
        for s in symbols
    )
    return UniverseSnapshot(
        mode=mode,
        trading_date=trading_date,
        approved_symbols=symbols,
        decisions=decisions,
        generated_at=datetime(2026, 5, 26, 5, 0, 0, tzinfo=timezone.utc),
        data_sources_used=frozenset(["yaml_fallback"]),
        failure_mode=failure_mode,
        failure_details="" if failure_mode is None else f"Test: {failure_mode.value}",
    )


def _liquidity_config(tmp_path: Path, live_policy: str = "EXCLUDE") -> Path:
    """Write a minimal liquidity config with controllable LIVE_ADVANCED policy."""
    cfg = {
        "liquidity_unavailable_policy": "INCLUDE",
        "defaults": {"enabled": True, "min_adv_crores_20d": 1.0},
        "by_mode": {
            "LIVE_ADVANCED": {
                "liquidity_unavailable_policy": live_policy,
                "enabled": True,
                "min_adv_crores_20d": 25.0,
            }
        },
    }
    p = tmp_path / "liq.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def _risk_config(tmp_path: Path, live_policy: str = "EXCLUDE") -> Path:
    """Write a minimal risk config with controllable LIVE_ADVANCED risk_metrics policy."""
    cfg = {
        "risk_metrics_unavailable_policy": "INCLUDE",
        "risk_metrics_by_mode": {
            "LIVE_ADVANCED": {"risk_metrics_unavailable_policy": live_policy}
        },
        "penny_stock": {"enabled": False},
        "surveillance": {"enabled": False},
        "circuit_frequency": {"enabled": False},
        "volatility": {"enabled": False},
        "low_float": {"enabled": False},
        "corporate_action_window": {"enabled": False},
    }
    p = tmp_path / "risk.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def _ca_config(tmp_path: Path, live_policy: str = "EXCLUDE") -> Path:
    """Write a minimal risk config with controllable LIVE_ADVANCED CA policy."""
    cfg = {
        "corporate_action_window": {
            "enabled": True,
            "data_source_unavailable_policy": "WARN",
            "by_mode": {
                "LIVE_ADVANCED": {"data_source_unavailable_policy": live_policy}
            },
        }
    }
    p = tmp_path / "ca_risk.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def _exclusion_yaml(tmp_path: Path, **lists: list[str]) -> Path:
    """Write an exclusion_lists.yaml with the given symbol lists."""
    defaults = {
        "emergency": [], "delisted": [], "suspended": [], "sme": [],
        "etf": [], "reit_invit": [], "bse_only": [],
        "known_asm": [], "known_gsm": [],
    }
    defaults.update(lists)
    p = tmp_path / "exclusion.yaml"
    p.write_text(yaml.safe_dump(defaults))
    return p


# ── T1: live missing liquidity data ──────────────────────────────────────────

class TestT1LiveMissingLiquidityData:
    def test_live_missing_liquidity_excludes_symbol(self, tmp_path: Path) -> None:
        """T1: LIVE_ADVANCED + liquidity data None → DATA_UNAVAILABLE verdict."""
        cfg = _liquidity_config(tmp_path, live_policy="EXCLUDE")
        ds = _NullMetricsDataSource()
        f = LiquidityFilter(ds, mode=UniverseMode.LIVE_ADVANCED, config_path=cfg)

        verdicts = f.apply({"RELIANCE"}, _TODAY)

        assert len(verdicts) == 1
        assert verdicts[0].symbol == "RELIANCE"
        assert verdicts[0].reason == ExclusionReason.DATA_UNAVAILABLE

    def test_live_missing_liquidity_excludes_all_candidates(self, tmp_path: Path) -> None:
        """T1: every symbol is excluded when all liquidity data is missing in LIVE mode."""
        cfg = _liquidity_config(tmp_path, live_policy="EXCLUDE")
        ds = _NullMetricsDataSource()
        f = LiquidityFilter(ds, mode=UniverseMode.LIVE_ADVANCED, config_path=cfg)

        candidates = {"RELIANCE", "TCS", "INFY"}
        verdicts = f.apply(candidates, _TODAY)

        blocked = {v.symbol for v in verdicts}
        assert blocked == candidates

    def test_paper_missing_liquidity_allows_symbol(self, tmp_path: Path) -> None:
        """T1 contrast: PAPER_SAFE_START uses INCLUDE policy — missing data → allowed."""
        cfg = _liquidity_config(tmp_path, live_policy="EXCLUDE")  # only LIVE is EXCLUDE
        ds = _NullMetricsDataSource()
        f = LiquidityFilter(ds, mode=UniverseMode.PAPER_SAFE_START, config_path=cfg)

        verdicts = f.apply({"RELIANCE"}, _TODAY)

        assert not verdicts


# ── T2: live missing risk data ────────────────────────────────────────────────

class TestT2LiveMissingRiskData:
    def test_live_missing_risk_data_excludes_symbol(self, tmp_path: Path) -> None:
        """T2: LIVE_ADVANCED + risk metrics None → DATA_UNAVAILABLE verdict."""
        cfg = _risk_config(tmp_path, live_policy="EXCLUDE")
        ds = _NullMetricsDataSource()
        f = RiskFilter(ds, mode=UniverseMode.LIVE_ADVANCED, config_path=cfg)

        verdicts = f.apply({"RELIANCE"}, _TODAY)

        assert len(verdicts) == 1
        assert verdicts[0].symbol == "RELIANCE"
        assert verdicts[0].reason == ExclusionReason.DATA_UNAVAILABLE

    def test_live_missing_risk_data_excludes_all_candidates(self, tmp_path: Path) -> None:
        """T2: all symbols excluded when risk metrics unavailable in LIVE mode."""
        cfg = _risk_config(tmp_path, live_policy="EXCLUDE")
        ds = _NullMetricsDataSource()
        f = RiskFilter(ds, mode=UniverseMode.LIVE_ADVANCED, config_path=cfg)

        candidates = {"RELIANCE", "TCS", "HDFCBANK"}
        verdicts = f.apply(candidates, _TODAY)

        assert {v.symbol for v in verdicts} == candidates

    def test_paper_missing_risk_data_allows_symbol(self, tmp_path: Path) -> None:
        """T2 contrast: PAPER_SAFE_START uses INCLUDE policy — missing risk data → allowed."""
        cfg = _risk_config(tmp_path, live_policy="EXCLUDE")
        ds = _NullMetricsDataSource()
        f = RiskFilter(ds, mode=UniverseMode.PAPER_SAFE_START, config_path=cfg)

        verdicts = f.apply({"RELIANCE"}, _TODAY)

        assert not verdicts


# ── T3: live surveillance source unavailable ──────────────────────────────────

class TestT3LiveSurveillanceUnavailable:
    def test_live_surveillance_unavailable_blocks_all(self) -> None:
        """T3: surveillance raises + EXCLUDE policy → all candidates blocked."""
        ds = _RaisingSurveillanceDataSource()
        f = SurveillanceFilter(
            data_source=ds,
            exclude_asm=True,
            exclude_gsm=True,
            unavailable_policy="EXCLUDE",
        )
        candidates = {"RELIANCE", "HDFCBANK", "TCS"}

        verdicts = f.apply(candidates)

        assert {v.symbol for v in verdicts} == candidates
        assert all(v.reason == ExclusionReason.DATA_UNAVAILABLE for v in verdicts)

    def test_live_surveillance_unavailable_blocks_single(self) -> None:
        """T3: single-symbol candidate is blocked when surveillance fails in LIVE mode."""
        ds = _RaisingSurveillanceDataSource()
        f = SurveillanceFilter(
            data_source=ds,
            exclude_asm=True,
            exclude_gsm=True,
            unavailable_policy="EXCLUDE",
        )

        verdicts = f.apply({"SBIN"})

        assert len(verdicts) == 1
        assert verdicts[0].symbol == "SBIN"

    def test_paper_surveillance_unavailable_warns_and_allows(self) -> None:
        """T3 contrast: PAPER uses WARN policy → no verdicts returned on surveillance failure."""
        ds = _RaisingSurveillanceDataSource()
        f = SurveillanceFilter(
            data_source=ds,
            exclude_asm=True,
            exclude_gsm=True,
            unavailable_policy="WARN",
        )

        verdicts = f.apply({"RELIANCE", "HDFCBANK"})

        assert not verdicts


# ── T4: live corporate action data unavailable ────────────────────────────────

class TestT4LiveCorporateActionUnavailable:
    def test_live_ca_unavailable_excludes_symbol(self, tmp_path: Path) -> None:
        """T4: CA source raises + LIVE_ADVANCED EXCLUDE policy → symbol excluded."""
        cfg = _ca_config(tmp_path, live_policy="EXCLUDE")
        ds = _RaisingCorporateActionDataSource()
        f = CorporateActionFilter(
            data_source=ds, mode=UniverseMode.LIVE_ADVANCED, config_path=cfg
        )

        verdicts = f.apply({"RELIANCE"}, _TODAY)

        assert len(verdicts) == 1
        assert verdicts[0].symbol == "RELIANCE"
        assert verdicts[0].reason == ExclusionReason.DATA_UNAVAILABLE

    def test_live_ca_unavailable_excludes_all_candidates(self, tmp_path: Path) -> None:
        """T4: every candidate excluded when CA source is unavailable in LIVE mode."""
        cfg = _ca_config(tmp_path, live_policy="EXCLUDE")
        ds = _RaisingCorporateActionDataSource()
        f = CorporateActionFilter(
            data_source=ds, mode=UniverseMode.LIVE_ADVANCED, config_path=cfg
        )

        candidates = {"RELIANCE", "INFY", "SBIN"}
        verdicts = f.apply(candidates, _TODAY)

        assert {v.symbol for v in verdicts} == candidates

    def test_paper_ca_unavailable_warns_and_allows(self, tmp_path: Path) -> None:
        """T4 contrast: PAPER_SAFE_START WARN policy → no verdicts on CA failure."""
        cfg = _ca_config(tmp_path, live_policy="EXCLUDE")
        ds = _RaisingCorporateActionDataSource()
        f = CorporateActionFilter(
            data_source=ds, mode=UniverseMode.PAPER_SAFE_START, config_path=cfg
        )

        verdicts = f.apply({"RELIANCE"}, _TODAY)

        assert not verdicts


# ── T5 & T6: snapshot staleness ───────────────────────────────────────────────

class TestT5T6SnapshotStaleness:
    def test_stale_snapshot_blocks_live(self) -> None:
        """T5: snapshot.trading_date = yesterday → LIVE_ADVANCED validate() rejects."""
        snap = _make_snapshot(UniverseMode.LIVE_ADVANCED, _NIFTY50_SYMBOLS, _YESTERDAY)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE", _today=_TODAY)

        assert result.approved is False
        assert "STALE" in result.reason
        assert str(_YESTERDAY) in result.reason
        assert str(_TODAY) in result.reason

    def test_stale_snapshot_warns_and_allows_paper(self) -> None:
        """T6: stale snapshot + PAPER_SAFE_START → validate() allows with warning."""
        snap = _make_snapshot(UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS, _YESTERDAY)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE", _today=_TODAY)

        assert result.approved is True

    def test_fresh_snapshot_not_blocked_by_staleness_check(self) -> None:
        """Today's snapshot must not be considered stale."""
        snap = _make_snapshot(UniverseMode.LIVE_ADVANCED, _NIFTY50_SYMBOLS, _TODAY)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE", _today=_TODAY)

        assert result.approved is True

    def test_stale_snapshot_live_blocks_every_symbol(self) -> None:
        """T5: staleness check fires before symbol lookup — even approved symbols blocked."""
        snap = _make_snapshot(UniverseMode.LIVE_ADVANCED, _NIFTY50_SYMBOLS, _YESTERDAY)
        validator = UniverseOrderValidator(snap)

        for sym in list(_NIFTY50_SYMBOLS)[:3]:
            result = validator.validate(sym, "NSE", _today=_TODAY)
            assert result.approved is False, f"Expected {sym} to be blocked by staleness"

    def test_stale_snapshot_live_includes_snapshot_date_in_result(self) -> None:
        """T5: ValidationResult.snapshot_date is populated even on staleness rejection."""
        snap = _make_snapshot(UniverseMode.LIVE_ADVANCED, _NIFTY50_SYMBOLS, _YESTERDAY)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE", _today=_TODAY)

        assert result.snapshot_date == _YESTERDAY

    def test_stale_paper_expand_also_allowed(self) -> None:
        """T6: PAPER_EXPAND mode also allows on stale snapshot (non-fatal for paper)."""
        snap = _make_snapshot(UniverseMode.PAPER_EXPAND, _NIFTY50_SYMBOLS, _YESTERDAY)
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE", _today=_TODAY)

        assert result.approved is True


# ── T8: PARTIAL snapshot + live mode ─────────────────────────────────────────

class TestT8PartialSnapshotLiveBlock:
    def test_partial_snapshot_blocks_live_orders(self) -> None:
        """T8: PARTIAL snapshot + LIVE_ADVANCED → validate() rejects all orders."""
        snap = _make_snapshot(
            UniverseMode.LIVE_ADVANCED, _NIFTY50_SYMBOLS, _TODAY,
            failure_mode=SnapshotFailureMode.PARTIAL,
        )
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE", _today=_TODAY)

        assert result.approved is False
        assert "PARTIAL" in result.reason

    def test_partial_snapshot_blocks_all_symbols(self) -> None:
        """T8: every symbol in a PARTIAL live snapshot is blocked."""
        snap = _make_snapshot(
            UniverseMode.LIVE_ADVANCED, _NIFTY50_SYMBOLS, _TODAY,
            failure_mode=SnapshotFailureMode.PARTIAL,
        )
        validator = UniverseOrderValidator(snap)

        for sym in list(_NIFTY50_SYMBOLS)[:4]:
            result = validator.validate(sym, "NSE", _today=_TODAY)
            assert result.approved is False, f"Expected {sym} blocked by PARTIAL guard"

    def test_partial_snapshot_includes_size_in_reason(self) -> None:
        """T8: rejection reason mentions the degraded snapshot size."""
        snap = _make_snapshot(
            UniverseMode.LIVE_ADVANCED, frozenset({"RELIANCE", "TCS"}), _TODAY,
            failure_mode=SnapshotFailureMode.PARTIAL,
        )
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE", _today=_TODAY)

        assert "2" in result.reason  # size = 2 symbols

    def test_partial_snapshot_paper_mode_allowed(self) -> None:
        """T8 contrast: PAPER + PARTIAL snapshot → allowed (non-fatal for paper)."""
        snap = _make_snapshot(
            UniverseMode.PAPER_SAFE_START, _NIFTY50_SYMBOLS, _TODAY,
            failure_mode=SnapshotFailureMode.PARTIAL,
        )
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE", _today=_TODAY)

        assert result.approved is True

    def test_fallback_snapshot_live_allowed(self) -> None:
        """FALLBACK snapshot (YAML) is acceptable for live — data is accurate for known indices."""
        snap = _make_snapshot(
            UniverseMode.LIVE_ADVANCED, _NIFTY50_SYMBOLS, _TODAY,
            failure_mode=SnapshotFailureMode.FALLBACK,
        )
        validator = UniverseOrderValidator(snap)

        result = validator.validate("RELIANCE", "NSE", _today=_TODAY)

        assert result.approved is True


# ── T9: ExclusionListFilter with NseApiDataSource ─────────────────────────────

class TestT9ExclusionListNonYamlDataSource:
    def test_nse_api_source_excludes_delisted_symbol(self, tmp_path: Path) -> None:
        """T9: NseApiDataSource delegates to YAML fallback — delisted symbols excluded."""
        excl = _exclusion_yaml(
            tmp_path,
            delisted=[{"symbol": "HDFC", "reason": "Merged with HDFCBANK"}],
        )
        fallback = YamlDataSource(exclusion_path=excl)
        ds = NseApiDataSource(yaml_fallback=fallback)
        f = ExclusionListFilter(ds)

        verdicts = f.apply({"HDFC", "RELIANCE"})

        blocked = {v.symbol: v.reason for v in verdicts}
        assert "HDFC" in blocked
        assert blocked["HDFC"] == ExclusionReason.DELISTED
        assert "RELIANCE" not in blocked

    def test_nse_api_source_excludes_etf(self, tmp_path: Path) -> None:
        """T9: ETF symbol on NseApiDataSource exclusion list is blocked."""
        excl = _exclusion_yaml(tmp_path, etf=["NIFTYBEES", "GOLDBEES"])
        fallback = YamlDataSource(exclusion_path=excl)
        ds = NseApiDataSource(yaml_fallback=fallback)
        f = ExclusionListFilter(ds)

        verdicts = f.apply({"NIFTYBEES", "GOLDBEES", "RELIANCE"})

        blocked = {v.symbol for v in verdicts}
        assert "NIFTYBEES" in blocked
        assert "GOLDBEES" in blocked
        assert "RELIANCE" not in blocked
        assert all(
            v.reason == ExclusionReason.ETF
            for v in verdicts if v.symbol in {"NIFTYBEES", "GOLDBEES"}
        )

    def test_nse_api_source_excludes_emergency(self, tmp_path: Path) -> None:
        """T9: Emergency exclusion works through NseApiDataSource."""
        excl = _exclusion_yaml(
            tmp_path,
            emergency=[{"symbol": "ADANIPORTS", "reason": "Emergency block test"}],
        )
        fallback = YamlDataSource(exclusion_path=excl)
        ds = NseApiDataSource(yaml_fallback=fallback)
        f = ExclusionListFilter(ds)

        verdicts = f.apply({"ADANIPORTS", "TCS"})

        blocked = {v.symbol: v.reason for v in verdicts}
        assert "ADANIPORTS" in blocked
        assert blocked["ADANIPORTS"] == ExclusionReason.EMERGENCY_EXCLUSION
        assert "TCS" not in blocked

    def test_nse_api_source_excludes_sme(self, tmp_path: Path) -> None:
        """T9: SME stock exclusion works through NseApiDataSource."""
        excl = _exclusion_yaml(tmp_path, sme=["FAKESME1"])
        fallback = YamlDataSource(exclusion_path=excl)
        ds = NseApiDataSource(yaml_fallback=fallback)
        f = ExclusionListFilter(ds)

        verdicts = f.apply({"FAKESME1", "RELIANCE"})

        blocked = {v.symbol: v.reason for v in verdicts}
        assert blocked.get("FAKESME1") == ExclusionReason.SME_STOCK
        assert "RELIANCE" not in blocked

    def test_nse_api_source_all_exclusion_categories_covered(self, tmp_path: Path) -> None:
        """T9: All 7 exclusion categories work through NseApiDataSource (not just emergency)."""
        excl = _exclusion_yaml(
            tmp_path,
            delisted=["DELIST1"],
            suspended=["SUSP1"],
            sme=["SME1"],
            etf=["ETF1"],
            reit_invit=["REIT1"],
            bse_only=["BSE1"],
            emergency=[{"symbol": "EMERG1", "reason": "test"}],
        )
        fallback = YamlDataSource(exclusion_path=excl)
        ds = NseApiDataSource(yaml_fallback=fallback)
        f = ExclusionListFilter(ds)

        candidates = {"DELIST1", "SUSP1", "SME1", "ETF1", "REIT1", "BSE1", "EMERG1", "SAFE"}
        verdicts = f.apply(candidates)

        blocked = {v.symbol for v in verdicts}
        assert "DELIST1" in blocked
        assert "SUSP1" in blocked
        assert "SME1" in blocked
        assert "ETF1" in blocked
        assert "REIT1" in blocked
        assert "BSE1" in blocked
        assert "EMERG1" in blocked
        assert "SAFE" not in blocked

    def test_yaml_source_same_results_as_nse_api_stub(self, tmp_path: Path) -> None:
        """T9: YamlDataSource and NseApiDataSource(yaml_fallback) produce identical exclusion results."""
        excl = _exclusion_yaml(
            tmp_path,
            delisted=["GONE1"],
            etf=["ETFX"],
        )
        yaml_ds = YamlDataSource(exclusion_path=excl)
        nse_ds = NseApiDataSource(yaml_fallback=YamlDataSource(exclusion_path=excl))

        f_yaml = ExclusionListFilter(yaml_ds)
        f_nse = ExclusionListFilter(nse_ds)

        candidates = {"GONE1", "ETFX", "RELIANCE"}
        verdicts_yaml = {(v.symbol, v.reason) for v in f_yaml.apply(candidates)}
        verdicts_nse = {(v.symbol, v.reason) for v in f_nse.apply(candidates)}

        assert verdicts_yaml == verdicts_nse
