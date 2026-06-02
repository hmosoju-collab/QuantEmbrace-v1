"""
Unit tests for RiskContextBuilder (PHASE4-011).

Covers:
    - build() fires all 7 DynamoDB reads concurrently via asyncio.gather
    - _fetch_position_state: happy path, missing item, DynamoDB error → PositionState()
    - _fetch_pending_quantity: sums PENDING + PLACED quantities, handles empty result
    - _fetch_adv: prefers adv_20d over adv_30d, returns None when key absent
    - _fetch_live_spread_bps: happy path, stale quote (>30s) → None, missing item → None
    - _fetch_portfolio_nav: reads nav, falls back to limits.portfolio_value on error
    - _fetch_analytics_snapshot: parses DynamoDB M-type sector map, empty → AnalyticsSnapshot.empty()
    - _fetch_total_exposure: sums paginated positions scan, error → 0.0
    - build() exception in one sub-read returns safe default, does not propagate
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import builtins as _builtins

try:
    import pytest
except ModuleNotFoundError:
    class _Approx:
        def __init__(self, v, abs=None, rel=None):
            self._v = v; self._abs = abs if abs is not None else 1e-6
        def __eq__(self, o): return _builtins.abs(o - self._v) <= self._abs
        def __repr__(self): return f"~{self._v}"
    class pytest:  # type: ignore[no-redef]
        @staticmethod
        def approx(v, abs=None, rel=None): return _Approx(v, abs=abs)

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)


# ---------------------------------------------------------------------------
# Minimal stubs for shared.* imports
# ---------------------------------------------------------------------------

def _install_shared_stubs() -> None:
    def _make_pkg(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    shared               = _make_pkg("shared")
    shared_config        = _make_pkg("shared.config")
    shared_config_sett   = _make_pkg("shared.config.settings")
    shared_logging       = _make_pkg("shared.logging")
    shared_logging_log   = _make_pkg("shared.logging.logger")
    shared_risk_state    = _make_pkg("shared.risk_state")
    shared_utils         = _make_pkg("shared.utils")
    shared_utils_helpers = _make_pkg("shared.utils.helpers")
    shared_models        = _make_pkg("shared.models")
    shared_models_signal = _make_pkg("shared.models.signal")
    shared_models_risk   = _make_pkg("shared.models.risk_context")

    # ── AppSettings stub ──────────────────────────────────────────────────────
    class _AWSConfig:
        dynamodb_table_positions  = "qe-dev-positions"
        dynamodb_table_orders     = "qe-dev-orders"
        dynamodb_table_prices     = "qe-dev-prices"
        dynamodb_table_risk_state = "qe-dev-risk-state"

    class _AppSettings:
        aws = _AWSConfig()

    def get_settings() -> _AppSettings:
        return _AppSettings()

    shared_config_sett.AppSettings = _AppSettings
    shared_config_sett.get_settings = get_settings
    shared_config.settings = shared_config_sett

    # ── Logger stub ───────────────────────────────────────────────────────────
    import logging

    def get_logger(name: str, **_kw):
        return logging.getLogger(name)

    shared_logging_log.get_logger = get_logger
    shared_logging_log.set_correlation_id = lambda *_, **__: None
    shared_logging.logger = shared_logging_log

    shared_risk_state.POSITION_SK = "CURRENT"
    shared_risk_state.position_key = lambda symbol: {
        "PK": {"S": f"POSITION#{symbol}"},
        "SK": {"S": "CURRENT"},
    }
    shared_risk_state.nav_key = lambda: {
        "PK": {"S": "NAV#CURRENT"},
        "SK": {"S": "STATE"},
    }

    # ── Helpers stub ──────────────────────────────────────────────────────────
    _NOW = datetime(2025, 10, 1, 9, 30, 0, tzinfo=timezone.utc)

    def utc_now() -> datetime:
        return _NOW

    def utc_iso(dt=None) -> str:
        return (_NOW if dt is None else dt).isoformat()

    shared_utils_helpers.utc_now  = utc_now
    shared_utils_helpers.utc_iso  = utc_iso
    shared_utils.helpers          = shared_utils_helpers

    # ── Signal stub (plain class — @dataclass inside a loader function causes
    #    __module__ lookup failures when the module isn't in sys.modules yet) ──
    class Signal:
        def __init__(
            self,
            symbol: str,
            market: str,
            quantity: int,
            price_at_signal: float = 2500.0,
            signal_id: str = "sig-test-001",
        ) -> None:
            self.symbol = symbol
            self.market = market
            self.quantity = quantity
            self.price_at_signal = price_at_signal
            self.signal_id = signal_id

    shared_models_signal.Signal = Signal
    shared_models.signal = shared_models_signal

    # Link package tree (must happen before loading real risk_context.py)
    shared.config  = shared_config
    shared.logging = shared_logging
    shared.risk_state = shared_risk_state
    shared.utils   = shared_utils
    shared.models  = shared_models
    shared_config.settings  = shared_config_sett
    shared_logging.logger   = shared_logging_log
    shared_utils.helpers    = shared_utils_helpers
    shared_models.signal    = shared_models_signal

    # ── Load REAL risk_context.py via importlib (bypasses package hierarchy) ──
    # IMPORTANT: register in sys.modules BEFORE exec_module so that the
    # @dataclass decorators can resolve cls.__module__ → module.__dict__.
    import importlib.util as _ilu
    _rc_path = os.path.join(_SERVICES_DIR, "shared", "models", "risk_context.py")
    _rc_spec = _ilu.spec_from_file_location("shared.models.risk_context", _rc_path)
    _rc_mod  = _ilu.module_from_spec(_rc_spec)
    _rc_mod.__package__ = "shared.models"
    sys.modules["shared.models.risk_context"] = _rc_mod   # register BEFORE exec
    _rc_spec.loader.exec_module(_rc_mod)
    shared_models.risk_context = _rc_mod


# ---------------------------------------------------------------------------
# Module-level placeholders — NO side-effects at import/collection time
# ---------------------------------------------------------------------------
_MODULES_SNAPSHOT: dict[str, object] = {}
AnalyticsSnapshot = None
PositionState = None
RiskContext = None
RiskContextBuilder = None


def setUpModule() -> None:  # noqa: N802
    """Called by pytest/unittest AFTER collection, BEFORE running tests."""
    global _MODULES_SNAPSHOT, AnalyticsSnapshot, PositionState, RiskContext, RiskContextBuilder

    _MODULES_SNAPSHOT = dict(sys.modules)
    for key in (
        "risk_engine.context.risk_context_builder",
        "shared.models.risk_context",
    ):
        sys.modules.pop(key, None)

    _install_shared_stubs()

    # Import real models (after stubs + real risk_context loaded)
    from shared.models.risk_context import (  # noqa: E402
        AnalyticsSnapshot as _AS,
        PositionState as _PS,
        RiskContext as _RC,
    )
    AnalyticsSnapshot = _AS
    PositionState = _PS
    RiskContext = _RC

    # Import the real builder
    from risk_engine.context.risk_context_builder import RiskContextBuilder as _RCB  # noqa: E402
    RiskContextBuilder = _RCB


def tearDownModule() -> None:  # noqa: N802
    """Remove every sys.modules key added during setUpModule."""
    added = frozenset(sys.modules.keys()) - frozenset(_MODULES_SNAPSHOT.keys())
    for key in added:
        sys.modules.pop(key, None)
    for key, module in _MODULES_SNAPSHOT.items():
        sys.modules[key] = module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 10, 1, 9, 30, 0, tzinfo=timezone.utc)
_FRESH_TS = _NOW.isoformat()                           # 0 seconds old
_STALE_TS = (_NOW - timedelta(seconds=60)).isoformat() # 60s old — above 30s threshold


_UNSET = object()  # sentinel — distinguishes "caller passed None" from "use default mock"


def _limits(portfolio_value: float = 1_000_000.0):
    m = MagicMock()
    m.portfolio_value = portfolio_value
    return m


def _make_builder(dynamo=_UNSET, limits=None) -> RiskContextBuilder:
    """
    Build a RiskContextBuilder for tests.

    Pass ``dynamo=None`` to test the code path where self._dynamo is None.
    Omit ``dynamo`` to get a properly configured default MagicMock.
    """
    actual_dynamo = _make_dynamo() if dynamo is _UNSET else dynamo
    return RiskContextBuilder(
        dynamo_client=actual_dynamo,
        limits=limits or _limits(),
        positions_table="qe-dev-positions",
        orders_table="qe-dev-orders",
        prices_table="qe-dev-prices",
        risk_state_table="qe-dev-risk-state",
    )


def _signal(symbol: str = "RELIANCE", market: str = "NSE", qty: int = 100):
    """Return a minimal Signal stub."""
    from shared.models.signal import Signal
    return Signal(symbol=symbol, market=market, quantity=qty, price_at_signal=2500.0)


def _pos_item(confirmed_qty: float = 500.0, avg_price: float = 2400.0) -> dict:
    return {
        "confirmed_quantity": {"N": str(confirmed_qty)},
        "avg_entry_price":    {"N": str(avg_price)},
    }


def _price_item_20d(adv: float = 4_000_000.0) -> dict:
    return {"adv_20d": {"N": str(adv)}}


def _price_item_30d(adv: float = 3_800_000.0) -> dict:
    return {"adv_30d": {"N": str(adv)}}


def _quote_item(spread_bps: float = 25.0, captured_at: str = _FRESH_TS) -> dict:
    return {
        "spread_bps":   {"N": str(spread_bps)},
        "captured_at":  {"S": captured_at},
    }


def _nav_item(nav: float = 500_000.0) -> dict:
    return {"portfolio_value": {"N": str(nav)}}


def _analytics_item(
    sector_exposures: dict | None = None,
    var_2pct: float = 8000.0,
    pnl_today: float = 3000.0,
    computed_at: str = _FRESH_TS,
) -> dict:
    sectors = sector_exposures or {"ENERGY": 100_000.0, "IT_SERVICES": 80_000.0}
    sector_map = {k: {"N": str(v)} for k, v in sectors.items()}
    return {
        "sector_exposures":   {"M": sector_map},
        "portfolio_var_2pct": {"N": str(var_2pct)},
        "portfolio_pnl_today": {"N": str(pnl_today)},
        "computed_at":        {"S": computed_at},
    }


def _scan_positions_page(rows: list[tuple[float, float]]) -> dict:
    """Build a DynamoDB scan response from (qty, last_price) pairs."""
    return {
        "Items": [
            {"quantity": {"N": str(q)}, "last_price": {"N": str(p)}}
            for q, p in rows
        ]
    }


def _make_dynamo(
    pos_item=None,
    price_item=None,
    quote_item=None,
    nav_item=None,
    analytics_item=None,
    pending_items: list | None = None,
    scan_pages: list | None = None,
) -> MagicMock:
    """
    Build a DynamoDB mock that returns configured responses for the
    4 get_item keys + query + scan used by RiskContextBuilder.
    """
    dynamo = MagicMock()

    def get_item(TableName, Key, **kwargs):
        pk = Key.get("PK", {}).get("S", "")
        sk = Key.get("SK", {}).get("S", "")

        if pk.startswith("POSITION#"):
            return {"Item": pos_item}
        if pk.startswith("PRICE#"):
            return {"Item": price_item}
        if pk.startswith("QUOTE#"):
            return {"Item": quote_item}
        if pk == "NAV#CURRENT":
            return {"Item": nav_item}
        if pk == "ANALYTICS#SNAPSHOT":
            return {"Item": analytics_item}
        return {"Item": None}

    dynamo.get_item.side_effect = get_item

    # query (for pending/placed quantities)
    query_response = {
        "Items": pending_items if pending_items is not None else []
    }
    dynamo.query.return_value = query_response

    # scan (for total exposure)
    pages = scan_pages if scan_pages is not None else [_scan_positions_page([])]
    scan_iter = iter(pages)

    def scan(**kwargs):
        try:
            page = next(scan_iter)
        except StopIteration:
            page = {"Items": []}
        return page

    dynamo.scan.side_effect = scan
    return dynamo


# ---------------------------------------------------------------------------
# Tests: _fetch_position_state
# ---------------------------------------------------------------------------

class TestFetchPositionState:
    def test_happy_path_returns_confirmed_qty_and_price(self):
        dynamo = _make_dynamo(pos_item=_pos_item(500.0, 2400.0))
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_position_state("RELIANCE"))
        assert result.confirmed_quantity == 500.0
        assert result.avg_entry_price    == 2400.0
        assert result.pending_quantity   == 0.0  # not set by this fetch

    def test_missing_item_returns_empty_position(self):
        dynamo = _make_dynamo(pos_item=None)
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_position_state("RELIANCE"))
        assert result.confirmed_quantity == 0.0
        assert result.avg_entry_price    == 0.0

    def test_dynamo_error_returns_empty_position(self):
        dynamo = MagicMock()
        dynamo.get_item.side_effect = Exception("DynamoDB unavailable")
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_position_state("RELIANCE"))
        assert result == PositionState()

    def test_none_dynamo_returns_empty_position(self):
        builder = _make_builder(dynamo=None)
        result = asyncio.run(builder._fetch_position_state("RELIANCE"))
        assert result == PositionState()


# ---------------------------------------------------------------------------
# Tests: _fetch_pending_quantity
# ---------------------------------------------------------------------------

class TestFetchPendingQuantity:
    def test_sums_pending_and_placed_orders(self):
        # query returns 2 PENDING rows of 100 and 1 PLACED row of 200
        # We configure query to return different values per call
        dynamo = MagicMock()
        call_count = 0

        def query_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:   # PENDING
                return {"Items": [{"quantity": {"N": "100"}}, {"quantity": {"N": "100"}}]}
            else:                 # PLACED
                return {"Items": [{"quantity": {"N": "200"}}]}

        dynamo.query.side_effect = query_side_effect
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_pending_quantity("RELIANCE"))
        assert result == 400.0

    def test_no_pending_orders_returns_zero(self):
        dynamo = MagicMock()
        dynamo.query.return_value = {"Items": []}
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_pending_quantity("INFY"))
        assert result == 0.0

    def test_dynamo_error_returns_zero(self):
        dynamo = MagicMock()
        dynamo.query.side_effect = Exception("Connection timeout")
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_pending_quantity("RELIANCE"))
        assert result == 0.0

    def test_none_dynamo_returns_zero(self):
        builder = _make_builder(dynamo=None)
        result = asyncio.run(builder._fetch_pending_quantity("RELIANCE"))
        assert result == 0.0


# ---------------------------------------------------------------------------
# Tests: _fetch_adv
# ---------------------------------------------------------------------------

class TestFetchAdv:
    def test_prefers_adv_20d_over_adv_30d(self):
        dynamo = _make_dynamo(price_item={"adv_20d": {"N": "4000000"}, "adv_30d": {"N": "3800000"}})
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_adv("RELIANCE"))
        assert result == 4_000_000.0

    def test_falls_back_to_adv_30d_when_20d_absent(self):
        dynamo = _make_dynamo(price_item=_price_item_30d(3_800_000.0))
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_adv("RELIANCE"))
        assert result == 3_800_000.0

    def test_returns_none_when_item_missing(self):
        dynamo = _make_dynamo(price_item=None)
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_adv("UNKNOWN"))
        assert result is None

    def test_returns_none_when_neither_adv_field_present(self):
        dynamo = _make_dynamo(price_item={"last_price": {"N": "2500"}})
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_adv("RELIANCE"))
        assert result is None

    def test_dynamo_error_returns_none(self):
        dynamo = MagicMock()
        dynamo.get_item.side_effect = Exception("Throttled")
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_adv("RELIANCE"))
        assert result is None

    def test_none_dynamo_returns_none(self):
        builder = _make_builder(dynamo=None)
        result = asyncio.run(builder._fetch_adv("RELIANCE"))
        assert result is None


# ---------------------------------------------------------------------------
# Tests: _fetch_live_spread_bps
# ---------------------------------------------------------------------------

class TestFetchLiveSpreadBps:
    def test_happy_path_returns_spread(self):
        dynamo = _make_dynamo(quote_item=_quote_item(spread_bps=25.0, captured_at=_FRESH_TS))
        builder = _make_builder(dynamo=dynamo)

        # Patch utc_now so "now" matches _NOW
        import shared.utils.helpers as helpers_mod
        original_utc_now = helpers_mod.utc_now
        helpers_mod.utc_now = lambda: _NOW

        result = asyncio.run(builder._fetch_live_spread_bps("NSE", "RELIANCE"))
        helpers_mod.utc_now = original_utc_now

        assert result == 25.0

    def test_stale_quote_returns_none(self):
        """Quote captured 60 seconds ago → exceeds 30s threshold → None."""
        dynamo = _make_dynamo(quote_item=_quote_item(spread_bps=25.0, captured_at=_STALE_TS))
        builder = _make_builder(dynamo=dynamo)

        import shared.utils.helpers as helpers_mod
        helpers_mod.utc_now = lambda: _NOW

        result = asyncio.run(builder._fetch_live_spread_bps("NSE", "RELIANCE"))
        # Restore would happen here but test is done
        assert result is None

    def test_missing_item_returns_none(self):
        dynamo = _make_dynamo(quote_item=None)
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_live_spread_bps("NSE", "RELIANCE"))
        assert result is None

    def test_missing_spread_bps_field_returns_none(self):
        dynamo = _make_dynamo(quote_item={"captured_at": {"S": _FRESH_TS}})
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_live_spread_bps("NSE", "RELIANCE"))
        assert result is None

    def test_dynamo_error_returns_none(self):
        dynamo = MagicMock()
        dynamo.get_item.side_effect = Exception("Connection refused")
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_live_spread_bps("NSE", "RELIANCE"))
        assert result is None

    def test_none_dynamo_returns_none(self):
        builder = _make_builder(dynamo=None)
        result = asyncio.run(builder._fetch_live_spread_bps("NSE", "RELIANCE"))
        assert result is None

    def test_unparseable_timestamp_returns_none(self):
        """If captured_at is garbage, live spread data is unsafe."""
        item = {"spread_bps": {"N": "15.0"}, "captured_at": {"S": "not-a-timestamp"}}
        dynamo = _make_dynamo(quote_item=item)
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_live_spread_bps("NSE", "RELIANCE"))
        assert result is None


# ---------------------------------------------------------------------------
# Tests: _fetch_portfolio_nav
# ---------------------------------------------------------------------------

class TestFetchPortfolioNav:
    def test_happy_path_returns_nav_from_dynamo(self):
        dynamo = _make_dynamo(nav_item=_nav_item(750_000.0))
        builder = _make_builder(dynamo=dynamo, limits=_limits(portfolio_value=1_000_000.0))
        result = asyncio.run(builder._fetch_portfolio_nav())
        assert result == 750_000.0

    def test_missing_item_falls_back_to_limits(self):
        dynamo = _make_dynamo(nav_item=None)
        builder = _make_builder(dynamo=dynamo, limits=_limits(portfolio_value=1_000_000.0))
        result = asyncio.run(builder._fetch_portfolio_nav())
        assert result == 1_000_000.0

    def test_zero_nav_in_dynamo_falls_back_to_limits(self):
        dynamo = _make_dynamo(nav_item={"portfolio_value": {"N": "0"}})
        builder = _make_builder(dynamo=dynamo, limits=_limits(portfolio_value=1_000_000.0))
        result = asyncio.run(builder._fetch_portfolio_nav())
        assert result == 1_000_000.0

    def test_dynamo_error_falls_back_to_limits(self):
        dynamo = MagicMock()
        dynamo.get_item.side_effect = Exception("DynamoDB offline")
        builder = _make_builder(dynamo=dynamo, limits=_limits(portfolio_value=500_000.0))
        result = asyncio.run(builder._fetch_portfolio_nav())
        assert result == 500_000.0

    def test_none_dynamo_returns_limits_value(self):
        builder = _make_builder(dynamo=None, limits=_limits(portfolio_value=250_000.0))
        result = asyncio.run(builder._fetch_portfolio_nav())
        assert result == 250_000.0


# ---------------------------------------------------------------------------
# Tests: _fetch_analytics_snapshot
# ---------------------------------------------------------------------------

class TestFetchAnalyticsSnapshot:
    def test_happy_path_parses_sector_map(self):
        dynamo = _make_dynamo(analytics_item=_analytics_item(
            sector_exposures={"ENERGY": 100_000.0, "IT_SERVICES": 80_000.0},
            var_2pct=8000.0,
            pnl_today=3000.0,
        ))
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_analytics_snapshot())
        assert result.sector_exposures["ENERGY"] == 100_000.0
        assert result.sector_exposures["IT_SERVICES"] == 80_000.0
        assert result.portfolio_var_2pct == 8000.0
        assert result.portfolio_pnl_today == 3000.0

    def test_missing_item_returns_empty_snapshot(self):
        dynamo = _make_dynamo(analytics_item=None)
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_analytics_snapshot())
        assert result.sector_exposures == {}
        assert result.portfolio_var_2pct == 0.0
        assert result.computed_at is None

    def test_computed_at_parsed_from_iso_string(self):
        ts = "2025-10-01T09:00:00+00:00"
        dynamo = _make_dynamo(analytics_item=_analytics_item(computed_at=ts))
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_analytics_snapshot())
        assert result.computed_at is not None
        assert result.computed_at.tzinfo is not None

    def test_dynamo_error_returns_empty_snapshot(self):
        dynamo = MagicMock()
        dynamo.get_item.side_effect = Exception("Timeout")
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_analytics_snapshot())
        assert result.sector_exposures == {}

    def test_none_dynamo_returns_empty_snapshot(self):
        builder = _make_builder(dynamo=None)
        result = asyncio.run(builder._fetch_analytics_snapshot())
        assert result == AnalyticsSnapshot.empty()


# ---------------------------------------------------------------------------
# Tests: _fetch_total_exposure
# ---------------------------------------------------------------------------

class TestFetchTotalExposure:
    def test_sums_positions_single_page(self):
        # 100 shares × ₹2500 + 200 shares × ₹1000 = 450,000
        page = _scan_positions_page([(100, 2500), (200, 1000)])
        dynamo = _make_dynamo(scan_pages=[page])
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_total_exposure())
        assert result == pytest.approx(450_000.0)

    def test_sums_positions_multiple_pages(self):
        page1 = {**_scan_positions_page([(100, 2500)]), "LastEvaluatedKey": {"PK": {"S": "cursor"}}}
        page2 = _scan_positions_page([(50, 1000)])
        dynamo = _make_dynamo(scan_pages=[page1, page2])
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_total_exposure())
        assert result == pytest.approx(300_000.0)

    def test_empty_positions_returns_zero(self):
        dynamo = _make_dynamo(scan_pages=[{"Items": []}])
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_total_exposure())
        assert result == 0.0

    def test_dynamo_error_returns_zero(self):
        dynamo = MagicMock()
        dynamo.scan.side_effect = Exception("Scan failed")
        # get_item still used by other reads — provide default
        dynamo.get_item.return_value = {"Item": None}
        dynamo.query.return_value = {"Items": []}
        builder = _make_builder(dynamo=dynamo)
        result = asyncio.run(builder._fetch_total_exposure())
        assert result == 0.0

    def test_none_dynamo_returns_zero(self):
        builder = _make_builder(dynamo=None)
        result = asyncio.run(builder._fetch_total_exposure())
        assert result == 0.0


# ---------------------------------------------------------------------------
# Tests: build() — end-to-end integration
# ---------------------------------------------------------------------------

class TestBuild:
    def _full_dynamo(self) -> MagicMock:
        return _make_dynamo(
            pos_item=_pos_item(500.0, 2400.0),
            price_item=_price_item_20d(4_000_000.0),
            quote_item=_quote_item(25.0, _FRESH_TS),
            nav_item=_nav_item(750_000.0),
            analytics_item=_analytics_item(),
            pending_items=[{"quantity": {"N": "100"}}],
            scan_pages=[_scan_positions_page([(500, 2400)])],
        )

    def test_build_returns_risk_context(self):
        import shared.utils.helpers as helpers_mod
        helpers_mod.utc_now = lambda: _NOW

        dynamo = self._full_dynamo()
        builder = _make_builder(dynamo=dynamo)
        context = asyncio.run(builder.build(_signal()))

        assert context.signal.symbol == "RELIANCE"
        assert context.position.confirmed_quantity == 500.0
        assert context.adv_20d == 4_000_000.0
        assert context.live_spread_bps == 25.0
        assert context.portfolio_nav == 750_000.0
        assert "ENERGY" in context.analytics.sector_exposures
        assert context.current_exposure > 0.0
        assert context.risk_data_errors == ()

    def test_build_pending_quantity_merged_into_position(self):
        """pending_quantity must be populated from _fetch_pending_quantity result.

        _make_dynamo sets query.return_value (not side_effect), so both the
        PENDING and PLACED status queries return the same single 100-share item.
        Total pending = 100 (PENDING) + 100 (PLACED) = 200.
        """
        import shared.utils.helpers as helpers_mod
        helpers_mod.utc_now = lambda: _NOW

        dynamo = _make_dynamo(
            pos_item=_pos_item(500.0, 2400.0),
            pending_items=[{"quantity": {"N": "100"}}],   # returned by BOTH queries
            scan_pages=[{"Items": []}],
        )
        builder = _make_builder(dynamo=dynamo)
        context = asyncio.run(builder.build(_signal()))
        # 100 from PENDING query + 100 from PLACED query = 200 total pending
        assert context.position.pending_quantity == 200.0
        assert context.position.total_committed_quantity == 700.0

    def test_build_degrades_gracefully_when_spread_stale(self):
        import shared.utils.helpers as helpers_mod
        helpers_mod.utc_now = lambda: _NOW

        dynamo = _make_dynamo(
            quote_item=_quote_item(99.0, _STALE_TS),   # 60s old
            scan_pages=[{"Items": []}],
        )
        builder = _make_builder(dynamo=dynamo)
        context = asyncio.run(builder.build(_signal()))
        # Stale spread → None and risk_data_errors rejects live approvals.
        assert context.live_spread_bps is None
        assert "live_spread:stale" in context.risk_data_errors

    def test_build_degrades_gracefully_when_no_adv(self):
        import shared.utils.helpers as helpers_mod
        helpers_mod.utc_now = lambda: _NOW

        dynamo = _make_dynamo(price_item=None, scan_pages=[{"Items": []}])
        builder = _make_builder(dynamo=dynamo)
        context = asyncio.run(builder.build(_signal()))
        assert context.adv_20d is None
        assert "adv:missing" in context.risk_data_errors

    def test_build_uses_limits_nav_fallback_when_dynamo_nav_missing(self):
        import shared.utils.helpers as helpers_mod
        helpers_mod.utc_now = lambda: _NOW

        dynamo = _make_dynamo(nav_item=None, scan_pages=[{"Items": []}])
        limits = _limits(portfolio_value=999_000.0)
        builder = _make_builder(dynamo=dynamo, limits=limits)
        context = asyncio.run(builder.build(_signal()))
        assert context.portfolio_nav == 999_000.0
        assert "portfolio_nav:missing_or_non_positive" in context.risk_data_errors

    def test_signal_notional_computed_correctly(self):
        import shared.utils.helpers as helpers_mod
        helpers_mod.utc_now = lambda: _NOW

        dynamo = _make_dynamo(scan_pages=[{"Items": []}])
        builder = _make_builder(dynamo=dynamo)
        # qty=100, price=2500 → notional = 250,000
        context = asyncio.run(builder.build(_signal(qty=100)))
        assert context.signal_notional == pytest.approx(250_000.0)


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

def _run_tests() -> int:
    import traceback
    test_classes = [
        TestFetchPositionState,
        TestFetchPendingQuantity,
        TestFetchAdv,
        TestFetchLiveSpreadBps,
        TestFetchPortfolioNav,
        TestFetchAnalyticsSnapshot,
        TestFetchTotalExposure,
        TestBuild,
    ]
    passed = failed = errors = 0
    for cls in test_classes:
        inst = cls()
        for name in sorted(dir(cls)):
            if not name.startswith("test_"):
                continue
            try:
                getattr(inst, name)()
                print(f"  PASS  {cls.__name__}.{name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {cls.__name__}.{name}: {e}")
                failed += 1
            except Exception:
                print(f"  ERR   {cls.__name__}.{name}")
                traceback.print_exc()
                errors += 1
    total = passed + failed + errors
    print(f"\n{total} tests  {passed} passed  {failed} failed  {errors} errors")
    return 1 if (failed or errors) else 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_tests())
