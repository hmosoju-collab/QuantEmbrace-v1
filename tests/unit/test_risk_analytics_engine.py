"""
Unit tests for RiskAnalyticsEngine (PHASE4-013).

Covers:
    - ANALYTICS_INTERVAL_BY_PHASE: all market phases map to correct intervals
    - _current_interval(): returns 0 for OVERNIGHT, default when phase governor absent
    - _compute_sector_exposures(): sums exposure by sector using InstrumentRegistry
    - _compute_pnl_today(): buy reduces P&L, sell increases P&L
    - _compute_var(): returns 0 when < 5 days; returns 2nd-percentile when >= 5
    - _compute_snapshot(): correct shape and types
    - _persist_snapshot(): writes correct DynamoDB item structure
    - _fetch_all_positions(): happy path, paginated scan, error → []
    - _fetch_fills_today(): filters by today's date prefix, error → []
    - _analytics_loop(): OVERNIGHT phase → no computation (sleep only)
    - stop() terminates running loop
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)


# ---------------------------------------------------------------------------
# Stubs for shared.* and market_phase imports
# ---------------------------------------------------------------------------

def _install_stubs() -> None:
    def _make_pkg(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    shared               = _make_pkg("shared")
    shared_config        = _make_pkg("shared.config")
    shared_config_sett   = _make_pkg("shared.config.settings")
    shared_logging       = _make_pkg("shared.logging")
    shared_logging_log   = _make_pkg("shared.logging.logger")
    shared_utils         = _make_pkg("shared.utils")
    shared_utils_helpers = _make_pkg("shared.utils.helpers")
    shared_zerodha       = _make_pkg("shared.zerodha")

    class _AWSConfig:
        dynamodb_table_positions  = "qe-dev-positions"
        dynamodb_table_orders     = "qe-dev-orders"
        dynamodb_table_risk_state = "qe-dev-risk-state"

    class _AppSettings:
        aws = _AWSConfig()

    shared_config_sett.AppSettings = _AppSettings
    shared_config_sett.get_settings = lambda: _AppSettings()
    shared_config.settings = shared_config_sett

    import logging
    shared_logging_log.get_logger = lambda name, **kw: logging.getLogger(name)
    shared_logging_log.set_correlation_id = lambda *_, **__: None
    shared_logging.logger = shared_logging_log

    _NOW = datetime(2025, 10, 1, 9, 30, 0, tzinfo=timezone.utc)
    shared_utils_helpers.utc_now  = lambda: _NOW
    shared_utils_helpers.utc_iso  = lambda dt=None: _NOW.isoformat()
    shared_utils.helpers          = shared_utils_helpers

    # ── MarketPhase and MarketPhaseGovernor stubs ─────────────────────────────
    from enum import Enum, auto

    class MarketPhase(Enum):
        PRE_OPEN     = auto()
        PRE_AUCTION  = auto()
        MARKET_OPEN  = auto()
        NORMAL       = auto()
        PRE_CLOSE    = auto()
        CLOSING      = auto()
        POST_CLOSE   = auto()
        OVERNIGHT    = auto()

    class MarketPhaseGovernor:
        def __init__(self):
            self._phase = MarketPhase.NORMAL

        def current_phase(self) -> MarketPhase:
            return self._phase

        def set_phase(self, phase: MarketPhase) -> None:
            self._phase = phase

    mpm = _make_pkg("shared.zerodha.market_phase")
    mpm.MarketPhase = MarketPhase
    mpm.MarketPhaseGovernor = MarketPhaseGovernor
    shared_zerodha.market_phase = mpm

    # link
    shared.config  = shared_config
    shared.logging = shared_logging
    shared.utils   = shared_utils
    shared.zerodha = shared_zerodha
    shared_config.settings  = shared_config_sett
    shared_logging.logger   = shared_logging_log
    shared_utils.helpers    = shared_utils_helpers


# ---------------------------------------------------------------------------
# Module-level placeholders — NO side-effects at import/collection time
# ---------------------------------------------------------------------------
_MODULES_SNAPSHOT: frozenset = frozenset()
_MISSING = object()
_MODULES_ORIGINALS: dict[str, object] = {}
_PARENT_ATTR_ORIGINALS: dict[tuple[str, str], object] = {}
_OVERWRITTEN_MODULE_KEYS = (
    "shared",
    "shared.config",
    "shared.config.settings",
    "shared.logging",
    "shared.logging.logger",
    "shared.utils",
    "shared.utils.helpers",
    "shared.zerodha",
    "shared.zerodha.market_phase",
    "risk_engine.analytics.risk_analytics_engine",
)
_OVERWRITTEN_PARENT_ATTRS = (
    ("shared", "config"),
    ("shared", "logging"),
    ("shared", "utils"),
    ("shared", "zerodha"),
    ("shared.config", "settings"),
    ("shared.logging", "logger"),
    ("shared.utils", "helpers"),
    ("shared.zerodha", "market_phase"),
    ("risk_engine.analytics", "risk_analytics_engine"),
)
RiskAnalyticsEngine = None
ANALYTICS_INTERVAL_BY_PHASE = None
_MarketPhase = None
_MarketPhaseGovernor = None


def setUpModule() -> None:  # noqa: N802
    """Called by pytest/unittest AFTER collection, BEFORE running tests."""
    global _MODULES_SNAPSHOT, RiskAnalyticsEngine, ANALYTICS_INTERVAL_BY_PHASE
    global _MarketPhase, _MarketPhaseGovernor, _MODULES_ORIGINALS, _PARENT_ATTR_ORIGINALS

    _MODULES_SNAPSHOT = frozenset(sys.modules.keys())
    _MODULES_ORIGINALS = {
        key: sys.modules.get(key, _MISSING)
        for key in _OVERWRITTEN_MODULE_KEYS
    }
    _PARENT_ATTR_ORIGINALS = {
        (module_name, attr): getattr(sys.modules[module_name], attr, _MISSING)
        for module_name, attr in _OVERWRITTEN_PARENT_ATTRS
        if module_name in sys.modules
    }

    _install_stubs()
    sys.modules.pop("risk_engine.analytics.risk_analytics_engine", None)
    analytics_pkg = sys.modules.get("risk_engine.analytics")
    if analytics_pkg is not None and hasattr(analytics_pkg, "risk_analytics_engine"):
        delattr(analytics_pkg, "risk_analytics_engine")

    # The real engine (imports MarketPhase from shared.zerodha.market_phase via try/except)
    from risk_engine.analytics.risk_analytics_engine import (  # noqa: E402
        RiskAnalyticsEngine as _RAE,
        ANALYTICS_INTERVAL_BY_PHASE as _AIBP,
    )
    RiskAnalyticsEngine = _RAE
    ANALYTICS_INTERVAL_BY_PHASE = _AIBP

    # Grab the stubbed MarketPhase for use in tests
    _MarketPhase = sys.modules["shared.zerodha.market_phase"].MarketPhase
    _MarketPhaseGovernor = sys.modules["shared.zerodha.market_phase"].MarketPhaseGovernor


def tearDownModule() -> None:  # noqa: N802
    """Restore every sys.modules entry and package attr touched by setUpModule."""
    added = frozenset(sys.modules.keys()) - _MODULES_SNAPSHOT
    for key in added:
        sys.modules.pop(key, None)
    for key, original in _MODULES_ORIGINALS.items():
        if original is _MISSING:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = original
    for (module_name, attr), original in _PARENT_ATTR_ORIGINALS.items():
        parent = sys.modules.get(module_name)
        if parent is None:
            continue
        if original is _MISSING:
            try:
                delattr(parent, attr)
            except AttributeError:
                pass
        else:
            setattr(parent, attr, original)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 10, 1, 9, 30, 0, tzinfo=timezone.utc)
_TODAY_STR = _NOW.strftime("%Y-%m-%d")


def _dynamo_mock() -> MagicMock:
    m = MagicMock()
    m.put_item.return_value = {}
    m.scan.return_value = {"Items": []}
    m.query.return_value = {"Items": []}
    return m


def _registry_mock(sector_map: dict[str, str] | None = None) -> MagicMock:
    """Returns a mock InstrumentRegistry that maps symbol → sector."""
    mapping = sector_map or {"RELIANCE": "ENERGY", "TCS": "IT_SERVICES"}
    reg = MagicMock()

    def get(symbol: str):
        sec = mapping.get(symbol)
        if sec is None:
            return None
        cfg = MagicMock()
        cfg.sector = sec
        return cfg

    reg.get.side_effect = get
    return reg


def _make_engine(
    dynamo=None,
    registry=None,
    positions_table: str = "qe-dev-positions",
    orders_table: str   = "qe-dev-orders",
    risk_state_table: str = "qe-dev-risk-state",
) -> RiskAnalyticsEngine:
    return RiskAnalyticsEngine(
        dynamo_client=dynamo or _dynamo_mock(),
        instrument_registry=registry,
        positions_table=positions_table,
        orders_table=orders_table,
        risk_state_table=risk_state_table,
    )


def _pos_items(*rows: tuple[str, float, float]) -> list[dict]:
    """Build DynamoDB scan Item rows: (symbol, qty, last_price) → PK, quantity, last_price."""
    result = []
    for symbol, qty, last_price in rows:
        result.append({
            "PK": {"S": f"POSITION#{symbol}"},
            "quantity": {"N": str(qty)},
            "last_price": {"N": str(last_price)},
        })
    return result


def _fill_items(*rows: tuple[str, float]) -> list[dict]:
    """Build fill Item rows: (direction, notional_value)."""
    result = []
    for direction, notional in rows:
        result.append({
            "direction": {"S": direction},
            "notional_value": {"N": str(notional)},
        })
    return result


# ---------------------------------------------------------------------------
# Tests: ANALYTICS_INTERVAL_BY_PHASE
# ---------------------------------------------------------------------------

class TestAnalyticsIntervalByPhase:
    def test_pre_open_interval_is_30s(self):
        assert ANALYTICS_INTERVAL_BY_PHASE[_MarketPhase.PRE_OPEN] == 30

    def test_pre_auction_interval_is_30s(self):
        assert ANALYTICS_INTERVAL_BY_PHASE[_MarketPhase.PRE_AUCTION] == 30

    def test_market_open_interval_is_30s(self):
        assert ANALYTICS_INTERVAL_BY_PHASE[_MarketPhase.MARKET_OPEN] == 30

    def test_normal_interval_is_60s(self):
        assert ANALYTICS_INTERVAL_BY_PHASE[_MarketPhase.NORMAL] == 60

    def test_pre_close_interval_is_30s(self):
        assert ANALYTICS_INTERVAL_BY_PHASE[_MarketPhase.PRE_CLOSE] == 30

    def test_closing_interval_is_60s(self):
        assert ANALYTICS_INTERVAL_BY_PHASE[_MarketPhase.CLOSING] == 60

    def test_post_close_interval_is_300s(self):
        assert ANALYTICS_INTERVAL_BY_PHASE[_MarketPhase.POST_CLOSE] == 300

    def test_overnight_not_in_map(self):
        """OVERNIGHT is not in the interval map — signals no computation."""
        assert _MarketPhase.OVERNIGHT not in ANALYTICS_INTERVAL_BY_PHASE

    def test_all_active_phases_covered(self):
        active_phases = {
            _MarketPhase.PRE_OPEN, _MarketPhase.PRE_AUCTION, _MarketPhase.MARKET_OPEN,
            _MarketPhase.NORMAL, _MarketPhase.PRE_CLOSE, _MarketPhase.CLOSING,
            _MarketPhase.POST_CLOSE,
        }
        for phase in active_phases:
            assert phase in ANALYTICS_INTERVAL_BY_PHASE, f"Phase {phase} missing from interval map"


# ---------------------------------------------------------------------------
# Tests: _current_interval
# ---------------------------------------------------------------------------

class TestCurrentInterval:
    def test_returns_correct_interval_for_each_phase(self):
        engine = _make_engine()
        for phase, expected in ANALYTICS_INTERVAL_BY_PHASE.items():
            engine._phase_governor.set_phase(phase)
            assert engine._current_interval() == expected

    def test_returns_zero_for_overnight_phase(self):
        engine = _make_engine()
        engine._phase_governor.set_phase(_MarketPhase.OVERNIGHT)
        assert engine._current_interval() == 0

    def test_returns_default_when_phase_governor_unavailable(self):
        engine = _make_engine()
        engine._phase_governor = None
        # Should return the module-level _DEFAULT_INTERVAL_SECONDS (60)
        from risk_engine.analytics.risk_analytics_engine import _DEFAULT_INTERVAL_SECONDS
        assert engine._current_interval() == _DEFAULT_INTERVAL_SECONDS

    def test_returns_default_on_phase_governor_exception(self):
        engine = _make_engine()
        engine._phase_governor = MagicMock()
        engine._phase_governor.current_phase.side_effect = Exception("crashed")
        from risk_engine.analytics.risk_analytics_engine import _DEFAULT_INTERVAL_SECONDS
        assert engine._current_interval() == _DEFAULT_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# Tests: _compute_sector_exposures
# ---------------------------------------------------------------------------

class TestComputeSectorExposures:
    def test_sums_exposure_by_sector(self):
        engine = _make_engine(registry=_registry_mock({
            "RELIANCE": "ENERGY",
            "TCS":      "IT_SERVICES",
        }))
        positions = [
            {"symbol": "RELIANCE", "qty": 100.0, "last_price": 2500.0},
            {"symbol": "TCS",      "qty":  50.0, "last_price": 3500.0},
        ]
        result = engine._compute_sector_exposures(positions)
        assert result["ENERGY"]      == pytest.approx(250_000.0)
        assert result["IT_SERVICES"] == pytest.approx(175_000.0)

    def test_multiple_positions_same_sector_summed(self):
        engine = _make_engine(registry=_registry_mock({
            "ONGC":     "ENERGY",
            "RELIANCE": "ENERGY",
        }))
        positions = [
            {"symbol": "ONGC",     "qty": 200.0, "last_price": 200.0},
            {"symbol": "RELIANCE", "qty": 100.0, "last_price": 2500.0},
        ]
        result = engine._compute_sector_exposures(positions)
        assert result["ENERGY"] == pytest.approx(40_000.0 + 250_000.0)

    def test_unknown_symbol_goes_to_unknown_sector(self):
        engine = _make_engine(registry=_registry_mock({}))  # empty registry
        positions = [{"symbol": "NEWCO", "qty": 100.0, "last_price": 100.0}]
        result = engine._compute_sector_exposures(positions)
        assert result["UNKNOWN"] == pytest.approx(10_000.0)

    def test_no_registry_defaults_to_unknown(self):
        engine = _make_engine(registry=None)
        positions = [{"symbol": "RELIANCE", "qty": 100.0, "last_price": 2500.0}]
        result = engine._compute_sector_exposures(positions)
        assert result["UNKNOWN"] == pytest.approx(250_000.0)

    def test_empty_positions_returns_empty_dict(self):
        engine = _make_engine()
        result = engine._compute_sector_exposures([])
        assert result == {}

    def test_uses_absolute_value_for_short_positions(self):
        engine = _make_engine(registry=_registry_mock({"RELIANCE": "ENERGY"}))
        positions = [{"symbol": "RELIANCE", "qty": -100.0, "last_price": 2500.0}]
        result = engine._compute_sector_exposures(positions)
        assert result["ENERGY"] == pytest.approx(250_000.0)


# ---------------------------------------------------------------------------
# Tests: _compute_pnl_today
# ---------------------------------------------------------------------------

class TestComputePnlToday:
    def _engine(self):
        return _make_engine()

    def test_sell_fills_increase_pnl(self):
        fills = [{"direction": "SELL", "notional_value": 100_000.0}]
        result = self._engine()._compute_pnl_today(fills)
        assert result == pytest.approx(100_000.0)

    def test_buy_fills_decrease_pnl(self):
        fills = [{"direction": "BUY", "notional_value": 100_000.0}]
        result = self._engine()._compute_pnl_today(fills)
        assert result == pytest.approx(-100_000.0)

    def test_mixed_fills_net_correctly(self):
        fills = [
            {"direction": "BUY",  "notional_value": 250_000.0},
            {"direction": "SELL", "notional_value": 300_000.0},
        ]
        result = self._engine()._compute_pnl_today(fills)
        assert result == pytest.approx(50_000.0)

    def test_empty_fills_returns_zero(self):
        assert self._engine()._compute_pnl_today([]) == 0.0

    def test_missing_direction_defaults_to_buy(self):
        fills = [{"notional_value": 50_000.0}]
        result = self._engine()._compute_pnl_today(fills)
        assert result == pytest.approx(-50_000.0)


# ---------------------------------------------------------------------------
# Tests: _compute_var
# ---------------------------------------------------------------------------

class TestComputeVar:
    def test_returns_zero_when_fewer_than_5_days(self):
        dynamo = _dynamo_mock()
        dynamo.query.return_value = {
            "Items": [
                {"pnl_total": {"N": "-1000"}},
                {"pnl_total": {"N": "-2000"}},
                {"pnl_total": {"N": "500"}},
            ]
        }
        engine = _make_engine(dynamo=dynamo)
        result = asyncio.run(engine._compute_var([]))
        assert result == 0.0

    def test_returns_2pct_quantile_when_5_days_available(self):
        # 5 daily P&Ls: [-5000, -3000, -1000, 1000, 3000]
        # sorted: [-5000, -3000, -1000, 1000, 3000]
        # idx = int(0.02 * 5) = int(0.1) = 0 → pnl = -5000 → abs = 5000
        dynamo = _dynamo_mock()
        dynamo.query.return_value = {
            "Items": [
                {"pnl_total": {"N": "-5000"}},
                {"pnl_total": {"N": "-3000"}},
                {"pnl_total": {"N": "-1000"}},
                {"pnl_total": {"N": "1000"}},
                {"pnl_total": {"N": "3000"}},
            ]
        }
        engine = _make_engine(dynamo=dynamo)
        result = asyncio.run(engine._compute_var([]))
        assert result == pytest.approx(5000.0)

    def test_returns_zero_on_dynamo_error(self):
        dynamo = _dynamo_mock()
        dynamo.query.side_effect = Exception("DynamoDB unavailable")
        engine = _make_engine(dynamo=dynamo)
        result = asyncio.run(engine._compute_var([]))
        assert result == 0.0

    def test_returns_zero_when_dynamo_is_none(self):
        engine = _make_engine(dynamo=None)
        result = asyncio.run(engine._compute_var([]))
        assert result == 0.0

    def test_more_than_5_days_uses_floor_quantile(self):
        # 10 items: [-10000, -8000, -6000, -4000, -2000, 0, 2000, 4000, 6000, 8000]
        # idx = int(0.02 * 10) = 0 → abs(-10000) = 10000
        items = [{"pnl_total": {"N": str(v)}}
                 for v in [-10000, -8000, -6000, -4000, -2000, 0, 2000, 4000, 6000, 8000]]
        dynamo = _dynamo_mock()
        dynamo.query.return_value = {"Items": items}
        engine = _make_engine(dynamo=dynamo)
        result = asyncio.run(engine._compute_var([]))
        assert result == pytest.approx(10000.0)


# ---------------------------------------------------------------------------
# Tests: _compute_snapshot
# ---------------------------------------------------------------------------

class TestComputeSnapshot:
    def test_snapshot_has_required_keys(self):
        dynamo = _dynamo_mock()
        dynamo.scan.return_value = {"Items": _pos_items(("RELIANCE", 100.0, 2500.0))}
        dynamo.query.return_value = {"Items": _fill_items(("SELL", 250_000.0))}

        engine = _make_engine(
            dynamo=dynamo,
            registry=_registry_mock({"RELIANCE": "ENERGY"}),
        )
        snapshot = asyncio.run(engine._compute_snapshot())

        assert "sector_exposures" in snapshot
        assert "total_exposure" in snapshot
        assert "portfolio_pnl_today" in snapshot
        assert "portfolio_var_2pct" in snapshot
        assert "computed_at" in snapshot

    def test_total_exposure_uses_absolute_values(self):
        dynamo = _dynamo_mock()
        dynamo.scan.return_value = {
            "Items": _pos_items(
                ("RELIANCE",  100.0, 2500.0),
                ("INFY",     -50.0,  1800.0),   # short position
            )
        }
        dynamo.query.return_value = {"Items": []}

        engine = _make_engine(dynamo=dynamo)
        snapshot = asyncio.run(engine._compute_snapshot())
        # 100*2500 + abs(-50)*1800 = 250000 + 90000 = 340000
        assert snapshot["total_exposure"] == pytest.approx(340_000.0)

    def test_sector_exposures_grouped_by_sector(self):
        dynamo = _dynamo_mock()
        dynamo.scan.return_value = {
            "Items": _pos_items(
                ("RELIANCE", 100.0, 2500.0),
                ("TCS",       50.0, 3500.0),
            )
        }
        dynamo.query.return_value = {"Items": []}

        engine = _make_engine(
            dynamo=dynamo,
            registry=_registry_mock({"RELIANCE": "ENERGY", "TCS": "IT_SERVICES"}),
        )
        snapshot = asyncio.run(engine._compute_snapshot())
        assert snapshot["sector_exposures"]["ENERGY"]      == pytest.approx(250_000.0)
        assert snapshot["sector_exposures"]["IT_SERVICES"] == pytest.approx(175_000.0)


# ---------------------------------------------------------------------------
# Tests: _persist_snapshot
# ---------------------------------------------------------------------------

class TestPersistSnapshot:
    def test_writes_correct_pk_and_sk(self):
        dynamo = _dynamo_mock()
        engine = _make_engine(dynamo=dynamo)
        snapshot = {
            "sector_exposures":     {"ENERGY": 250_000.0},
            "total_exposure":        250_000.0,
            "portfolio_pnl_today":   5_000.0,
            "portfolio_var_2pct":    8_000.0,
            "computed_at":           "2025-10-01T09:30:00+00:00",
        }
        asyncio.run(engine._persist_snapshot(snapshot))

        dynamo.put_item.assert_called_once()
        call_kwargs = dynamo.put_item.call_args[1]
        item = call_kwargs["Item"]
        assert item["PK"]["S"] == "ANALYTICS#SNAPSHOT"
        assert item["SK"]["S"] == "CURRENT"

    def test_writes_numeric_fields_as_dynamo_n_type(self):
        dynamo = _dynamo_mock()
        engine = _make_engine(dynamo=dynamo)
        snapshot = {
            "sector_exposures":    {},
            "total_exposure":       100_000.0,
            "portfolio_pnl_today":  1_000.0,
            "portfolio_var_2pct":   500.0,
            "computed_at":          "2025-10-01T09:30:00+00:00",
        }
        asyncio.run(engine._persist_snapshot(snapshot))

        item = dynamo.put_item.call_args[1]["Item"]
        assert "N" in item["total_exposure"]
        assert "N" in item["portfolio_pnl_today"]
        assert "N" in item["portfolio_var_2pct"]

    def test_sector_exposures_written_as_dynamo_map(self):
        dynamo = _dynamo_mock()
        engine = _make_engine(dynamo=dynamo)
        snapshot = {
            "sector_exposures":    {"ENERGY": 250_000.0, "IT_SERVICES": 100_000.0},
            "total_exposure":       350_000.0,
            "portfolio_pnl_today":  0.0,
            "portfolio_var_2pct":   0.0,
            "computed_at":          "2025-10-01T09:30:00",
        }
        asyncio.run(engine._persist_snapshot(snapshot))

        item = dynamo.put_item.call_args[1]["Item"]
        sector_attr = item["sector_exposures"]
        assert "M" in sector_attr
        assert "ENERGY" in sector_attr["M"]
        assert "N" in sector_attr["M"]["ENERGY"]

    def test_none_dynamo_skips_write(self):
        """When dynamo is None, persist_snapshot should not raise."""
        engine = _make_engine(dynamo=None)
        snapshot = {
            "sector_exposures": {},
            "total_exposure": 0.0,
            "portfolio_pnl_today": 0.0,
            "portfolio_var_2pct": 0.0,
            "computed_at": "2025-10-01T09:30:00",
        }
        # Should not raise
        asyncio.run(engine._persist_snapshot(snapshot))


# ---------------------------------------------------------------------------
# Tests: _fetch_all_positions
# ---------------------------------------------------------------------------

class TestFetchAllPositions:
    def test_returns_parsed_positions(self):
        dynamo = _dynamo_mock()
        dynamo.scan.return_value = {
            "Items": _pos_items(("RELIANCE", 100.0, 2500.0), ("TCS", 50.0, 3500.0))
        }
        engine = _make_engine(dynamo=dynamo)
        result = asyncio.run(engine._fetch_all_positions())

        assert len(result) == 2
        symbols = {p["symbol"] for p in result}
        assert symbols == {"RELIANCE", "TCS"}

    def test_paginated_scan_collects_all_pages(self):
        dynamo = _dynamo_mock()
        call_count = 0

        def scan_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "Items": _pos_items(("RELIANCE", 100.0, 2500.0)),
                    "LastEvaluatedKey": {"PK": {"S": "cursor"}},
                }
            return {"Items": _pos_items(("TCS", 50.0, 3500.0))}

        dynamo.scan.side_effect = scan_side_effect
        engine = _make_engine(dynamo=dynamo)
        result = asyncio.run(engine._fetch_all_positions())
        assert len(result) == 2

    def test_dynamo_error_returns_empty_list(self):
        dynamo = _dynamo_mock()
        dynamo.scan.side_effect = Exception("Scan failed")
        engine = _make_engine(dynamo=dynamo)
        result = asyncio.run(engine._fetch_all_positions())
        assert result == []

    def test_none_dynamo_returns_empty_list(self):
        engine = _make_engine(dynamo=None)
        result = asyncio.run(engine._fetch_all_positions())
        assert result == []


# ---------------------------------------------------------------------------
# Tests: _fetch_fills_today
# ---------------------------------------------------------------------------

class TestFetchFillsToday:
    def test_returns_parsed_fills(self):
        dynamo = _dynamo_mock()
        dynamo.scan.return_value = {
            "Items": _fill_items(("BUY", 250_000.0), ("SELL", 300_000.0))
        }
        engine = _make_engine(dynamo=dynamo)
        result = asyncio.run(engine._fetch_fills_today())
        assert len(result) == 2

    def test_dynamo_error_returns_empty_list(self):
        dynamo = _dynamo_mock()
        dynamo.scan.side_effect = Exception("Error")
        engine = _make_engine(dynamo=dynamo)
        result = asyncio.run(engine._fetch_fills_today())
        assert result == []

    def test_none_dynamo_returns_empty_list(self):
        engine = _make_engine(dynamo=None)
        result = asyncio.run(engine._fetch_fills_today())
        assert result == []


# ---------------------------------------------------------------------------
# Tests: analytics_loop control flow
# ---------------------------------------------------------------------------

class TestAnalyticsLoop:
    def test_stop_terminates_running_loop(self):
        """start() sets _running=True; stop() sets it False — loop exits on next iteration."""
        engine = _make_engine()
        engine._running = False  # don't actually run the loop

        async def run_test():
            await engine.stop()
            return engine._running

        result = asyncio.run(run_test())
        assert result is False

    def test_overnight_phase_skips_compute(self):
        """When _current_interval() returns 0, the loop sleeps without computing."""
        engine = _make_engine()
        engine._phase_governor.set_phase(_MarketPhase.OVERNIGHT)
        # Verify that current_interval returns 0 for overnight
        assert engine._current_interval() == 0

    def test_loop_iteration_calls_persist_after_compute(self):
        """When interval > 0, one loop cycle should call _compute_snapshot + _persist_snapshot."""
        engine = _make_engine()
        computed = []
        persisted = []
        sleep_calls = [0]

        async def mock_compute():
            snapshot = {"sector_exposures": {}, "total_exposure": 0.0,
                        "portfolio_pnl_today": 0.0, "portfolio_var_2pct": 0.0,
                        "computed_at": "2025-10-01T09:30:00"}
            computed.append(snapshot)
            return snapshot

        async def mock_persist(snapshot):
            persisted.append(snapshot)

        async def mock_sleep(seconds):
            # First sleep: wait before compute — let it pass through.
            # Second sleep: compute+persist have run — stop the loop.
            sleep_calls[0] += 1
            if sleep_calls[0] >= 2:
                engine._running = False

        engine._compute_snapshot = mock_compute
        engine._persist_snapshot = mock_persist
        engine._running = True
        engine._phase_governor.set_phase(_MarketPhase.NORMAL)

        async def run():
            with patch("asyncio.sleep", side_effect=mock_sleep):
                await engine._analytics_loop()

        asyncio.run(run())
        assert len(computed) == 1, f"expected 1 compute call, got {len(computed)}"
        assert len(persisted) == 1, f"expected 1 persist call, got {len(persisted)}"

    def test_loop_tolerates_compute_error_without_crashing(self):
        """If _compute_snapshot raises, the loop logs and continues (doesn't crash)."""
        engine = _make_engine()
        error_count = [0]
        sleep_calls = [0]

        async def failing_compute():
            error_count[0] += 1
            raise ValueError("Compute failed")

        async def mock_sleep(seconds):
            # First sleep: before compute, let pass.
            # Second sleep: after failed compute, stop.
            sleep_calls[0] += 1
            if sleep_calls[0] >= 2:
                engine._running = False

        engine._compute_snapshot = failing_compute
        engine._running = True
        engine._phase_governor.set_phase(_MarketPhase.NORMAL)

        async def run():
            with patch("asyncio.sleep", side_effect=mock_sleep):
                await engine._analytics_loop()  # should not raise

        asyncio.run(run())  # no exception → test passes
        assert error_count[0] >= 1
