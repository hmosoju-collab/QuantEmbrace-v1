"""
Unit tests for Phase 4 context validators (PHASE4-012).

Covers SpreadGateValidator, SectorConcentrationValidator, LiquidityValidator.
All three are synchronous, zero-I/O validators that read only from RiskContext.

SpreadGateValidator:
    - Approved when spread below global threshold
    - Rejected when spread exceeds global threshold
    - Approved with STALE_SPREAD_DATA when live_spread_bps is None
    - Per-symbol override threshold is respected

SectorConcentrationValidator:
    - Approved when proposed sector exposure stays under cap
    - Rejected when proposed sector exposure would breach cap
    - Approved with NO_SECTOR_DATA when analytics snapshot has no sector data
    - Approved with UNKNOWN_SECTOR when symbol not in registry
    - Zero portfolio_nav → approved (avoids divide-by-zero)

LiquidityValidator:
    - Approved when order-to-ADV ratio is within threshold
    - Rejected when order-to-ADV ratio exceeds threshold
    - Approved with LOW_ADV_DATA when adv_20d is None
    - Approved when ADV exceeds liquid floor (1M shares) — skip ratio check
    - Per-symbol override threshold is respected
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
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
# Stubs for shared.* (minimal — only what the three validators need)
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
    shared_models        = _make_pkg("shared.models")
    shared_models_signal = _make_pkg("shared.models.signal")

    # AppSettings stub
    class _AWSConfig:
        dynamodb_table_positions  = "qe-dev-positions"
        dynamodb_table_orders     = "qe-dev-orders"
        dynamodb_table_prices     = "qe-dev-prices"
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

    # Plain class — @dataclass inside a loader function causes __module__
    # lookup failures when the module isn't yet in sys.modules.
    class Signal:
        def __init__(
            self,
            symbol: str,
            market: str,
            quantity: int,
            price_at_signal: float = 2500.0,
            signal_id: str = "sig-test-001",
            paper_trade: bool = True,
        ) -> None:
            self.symbol = symbol
            self.market = market
            self.quantity = quantity
            self.price_at_signal = price_at_signal
            self.signal_id = signal_id
            self.paper_trade = paper_trade

    shared_models_signal.Signal = Signal
    shared_models.signal = shared_models_signal

    # Link package tree (must happen before loading real risk_context.py)
    shared.config  = shared_config
    shared.logging = shared_logging
    shared.utils   = shared_utils
    shared.models  = shared_models
    shared_config.settings  = shared_config_sett
    shared_logging.logger   = shared_logging_log
    shared_utils.helpers    = shared_utils_helpers
    shared_models.signal    = shared_models_signal

    # Load real risk_context.py via importlib so frozen dataclasses work correctly.
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
RiskLimits = None
RiskValidationResult = None
SpreadGateValidator = None
SectorConcentrationValidator = None
LiquidityValidator = None


def setUpModule() -> None:  # noqa: N802
    """Called by pytest/unittest AFTER collection, BEFORE running tests."""
    global _MODULES_SNAPSHOT
    global AnalyticsSnapshot, PositionState, RiskContext
    global RiskLimits, RiskValidationResult
    global SpreadGateValidator, SectorConcentrationValidator, LiquidityValidator

    _MODULES_SNAPSHOT = dict(sys.modules)
    for key in (
        "risk_engine.validators.spread_gate_validator",
        "risk_engine.validators.liquidity_validator",
        "risk_engine.validators.sector_validator",
        "risk_engine.limits.risk_limits",
        "shared.models.risk_context",
    ):
        sys.modules.pop(key, None)

    _install_stubs()

    # Real imports (after stubs + real risk_context loaded)
    from shared.models.risk_context import (  # noqa: E402
        AnalyticsSnapshot as _AS,
        PositionState as _PS,
        RiskContext as _RC,
    )
    AnalyticsSnapshot = _AS
    PositionState = _PS
    RiskContext = _RC

    from risk_engine.limits.risk_limits import (  # noqa: E402
        RiskLimits as _RL,
        RiskValidationResult as _RVR,
    )
    RiskLimits = _RL
    RiskValidationResult = _RVR

    from risk_engine.validators.spread_gate_validator import SpreadGateValidator as _SGV  # noqa: E402
    from risk_engine.validators.sector_validator import SectorConcentrationValidator as _SCV  # noqa: E402
    from risk_engine.validators.liquidity_validator import LiquidityValidator as _LV  # noqa: E402
    SpreadGateValidator = _SGV
    SectorConcentrationValidator = _SCV
    LiquidityValidator = _LV


def tearDownModule() -> None:  # noqa: N802
    """Remove every sys.modules key added during setUpModule."""
    added = frozenset(sys.modules.keys()) - frozenset(_MODULES_SNAPSHOT.keys())
    for key in added:
        sys.modules.pop(key, None)
    for key, module in _MODULES_SNAPSHOT.items():
        sys.modules[key] = module


# ---------------------------------------------------------------------------
# Context builder helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 10, 1, 9, 30, 0, tzinfo=timezone.utc)


def _signal(symbol: str = "RELIANCE", market: str = "NSE", qty: int = 100, price: float = 2500.0):
    from shared.models.signal import Signal
    return Signal(symbol=symbol, market=market, quantity=qty, price_at_signal=price)


def _context(
    symbol: str = "RELIANCE",
    market: str = "NSE",
    qty: int = 100,
    price: float = 2500.0,
    confirmed_qty: float = 0.0,
    current_exposure: float = 0.0,
    portfolio_nav: float = 1_000_000.0,
    sector_exposures: dict | None = None,
    var_2pct: float = 0.0,
    pnl_today: float = 0.0,
    live_spread_bps: float | None = None,
    adv_20d: float | None = None,
    paper_trade: bool = True,
) -> RiskContext:
    analytics = AnalyticsSnapshot(
        sector_exposures=sector_exposures if sector_exposures is not None else {},
        portfolio_var_2pct=var_2pct,
        portfolio_pnl_today=pnl_today,
        computed_at=_NOW,
    )
    ctx = RiskContext(
        signal=_signal(symbol=symbol, market=market, qty=qty, price=price),
        position=PositionState(confirmed_quantity=confirmed_qty),
        current_exposure=current_exposure,
        portfolio_nav=portfolio_nav,
        analytics=analytics,
        live_spread_bps=live_spread_bps,
        adv_20d=adv_20d,
        fetched_at=_NOW,
    )
    ctx.signal.paper_trade = paper_trade
    return ctx


def _limits(
    max_sector_pct: float = 30.0,
    portfolio_value: float = 1_000_000.0,
    max_spread_bps: float = 50.0,
    max_order_adv_pct: float = 1.0,
) -> RiskLimits:
    return RiskLimits(
        max_sector_exposure_pct=max_sector_pct,
        portfolio_value=portfolio_value,
    )


# ---------------------------------------------------------------------------
# SpreadGateValidator
# ---------------------------------------------------------------------------

class TestSpreadGateValidator:
    def _validator(self, max_spread: float = 50.0, overrides: dict | None = None) -> SpreadGateValidator:
        return SpreadGateValidator(max_spread_bps=max_spread, per_symbol_overrides=overrides or {})

    def test_approved_when_spread_below_threshold(self):
        ctx = _context(live_spread_bps=25.0)
        result = self._validator(max_spread=50.0).validate(ctx)
        assert result.approved is True
        assert "25.0" in result.reason
        assert result.validator_name == "spread_gate_validator"

    def test_approved_exactly_at_threshold(self):
        ctx = _context(live_spread_bps=50.0)
        result = self._validator(max_spread=50.0).validate(ctx)
        assert result.approved is True

    def test_rejected_when_spread_exceeds_threshold(self):
        ctx = _context(live_spread_bps=75.0)
        result = self._validator(max_spread=50.0).validate(ctx)
        assert result.approved is False
        assert "75.0" in result.reason
        assert result.details["excess_bps"] == pytest.approx(25.0)

    def test_approved_with_stale_spread_warning_when_none(self):
        ctx = _context(live_spread_bps=None)
        result = self._validator().validate(ctx)
        assert result.approved is True
        assert "STALE_SPREAD_DATA" in result.reason

    def test_live_rejected_when_spread_missing(self):
        ctx = _context(live_spread_bps=None, paper_trade=False)
        result = self._validator().validate(ctx)
        assert result.approved is False
        assert "LIVE_RISK_DATA_UNAVAILABLE" in result.reason

    def test_per_symbol_override_is_used_when_present(self):
        ctx = _context(symbol="NIFTY50", live_spread_bps=30.0)
        validator = self._validator(max_spread=50.0, overrides={"NIFTY50": 20.0})
        result = validator.validate(ctx)
        # 30 > 20 → rejected even though 30 < global 50
        assert result.approved is False
        assert result.details["max_spread_bps"] == 20.0

    def test_global_threshold_used_when_no_override(self):
        ctx = _context(symbol="RELIANCE", live_spread_bps=30.0)
        validator = self._validator(max_spread=50.0, overrides={"OTHER": 10.0})
        result = validator.validate(ctx)
        assert result.approved is True
        assert result.details["max_spread_bps"] == 50.0

    def test_details_include_spread_and_threshold(self):
        ctx = _context(live_spread_bps=20.0)
        result = self._validator(max_spread=50.0).validate(ctx)
        assert result.details["spread_bps"] == 20.0
        assert result.details["max_spread_bps"] == 50.0

    def test_stale_data_details_has_sentinel_negative_spread(self):
        ctx = _context(live_spread_bps=None)
        result = self._validator().validate(ctx)
        # Convention: -1.0 means "no data"
        assert result.details["spread_bps"] == -1.0


# ---------------------------------------------------------------------------
# SectorConcentrationValidator
# ---------------------------------------------------------------------------

class TestSectorConcentrationValidator:

    def _registry(self, symbol_sector: dict[str, str] | None = None) -> MagicMock:
        """Mock InstrumentRegistry that maps symbols to sectors."""
        reg = MagicMock()
        mapping = symbol_sector or {"RELIANCE": "ENERGY", "TCS": "IT_SERVICES"}

        def get(symbol: str):
            sector = mapping.get(symbol)
            if sector is None:
                return None
            cfg = MagicMock()
            cfg.sector = sector
            return cfg

        reg.get.side_effect = get
        return reg

    def _validator(
        self,
        max_sector_pct: float = 30.0,
        symbol_sector: dict | None = None,
    ) -> SectorConcentrationValidator:
        limits = _limits(max_sector_pct=max_sector_pct)
        registry = self._registry(symbol_sector)
        return SectorConcentrationValidator(limits=limits, instrument_registry=registry)

    def test_approved_when_sector_exposure_within_cap(self):
        # Existing ENERGY: 200k/1M = 20%. Adding 50 shares × 2500 = 125k → 325k/1M = 32.5%
        # ... but with max 40% should pass.
        ctx = _context(
            symbol="RELIANCE",
            qty=50,
            price=2500.0,
            portfolio_nav=1_000_000.0,
            sector_exposures={"ENERGY": 200_000.0},
        )
        validator = self._validator(max_sector_pct=40.0, symbol_sector={"RELIANCE": "ENERGY"})
        result = validator.validate(ctx)
        assert result.approved is True
        assert result.details["proposed_sector_pct"] == pytest.approx(32.5)

    def test_rejected_when_sector_would_exceed_cap(self):
        # Existing ENERGY: 280k/1M = 28%. Adding 100 × 2500 = 250k → 530k/1M = 53% > 30%
        ctx = _context(
            symbol="RELIANCE",
            qty=100,
            price=2500.0,
            portfolio_nav=1_000_000.0,
            sector_exposures={"ENERGY": 280_000.0},
        )
        validator = self._validator(max_sector_pct=30.0, symbol_sector={"RELIANCE": "ENERGY"})
        result = validator.validate(ctx)
        assert result.approved is False
        assert "ENERGY" in result.reason
        assert result.details["proposed_sector_pct"] > 30.0

    def test_empty_sector_snapshot_counts_from_zero_when_sector_known(self):
        """Empty sector_exposures can be a valid flat portfolio snapshot."""
        ctx = _context(
            symbol="RELIANCE",
            sector_exposures={},  # empty — analytics not yet run
        )
        validator = self._validator()
        result = validator.validate(ctx)
        assert result.approved is True
        assert result.details["proposed_sector_pct"] == pytest.approx(25.0)

    def test_approved_with_unknown_sector_warning(self):
        """Symbol not in registry → sector is UNKNOWN → approve."""
        ctx = _context(
            symbol="NEWSTOCK",
            sector_exposures={"ENERGY": 50_000.0},  # non-empty so we get past the first check
        )
        validator = self._validator(symbol_sector={"RELIANCE": "ENERGY"})  # NEWSTOCK not mapped
        result = validator.validate(ctx)
        assert result.approved is True
        assert "UNKNOWN_SECTOR" in result.reason

    def test_live_rejected_with_unknown_sector(self):
        """Live symbols missing registry sector must fail closed."""
        ctx = _context(
            symbol="NEWSTOCK",
            sector_exposures={"ENERGY": 50_000.0},
            paper_trade=False,
        )
        validator = self._validator(symbol_sector={"RELIANCE": "ENERGY"})
        result = validator.validate(ctx)
        assert result.approved is False
        assert "LIVE_RISK_DATA_UNAVAILABLE" in result.reason

    def test_no_registry_defaults_to_unknown(self):
        ctx = _context(
            symbol="RELIANCE",
            sector_exposures={"ENERGY": 50_000.0},
        )
        limits = _limits(max_sector_pct=30.0)
        validator = SectorConcentrationValidator(limits=limits, instrument_registry=None)
        result = validator.validate(ctx)
        assert result.approved is True
        assert "UNKNOWN_SECTOR" in result.reason

    def test_zero_portfolio_nav_skips_check(self):
        ctx = _context(
            symbol="RELIANCE",
            portfolio_nav=0.0,
            sector_exposures={"ENERGY": 50_000.0},
        )
        validator = self._validator(symbol_sector={"RELIANCE": "ENERGY"})
        result = validator.validate(ctx)
        assert result.approved is True
        assert "zero" in result.reason.lower()

    def test_live_rejected_with_zero_portfolio_nav(self):
        ctx = _context(
            symbol="RELIANCE",
            portfolio_nav=0.0,
            sector_exposures={"ENERGY": 50_000.0},
            paper_trade=False,
        )
        validator = self._validator(symbol_sector={"RELIANCE": "ENERGY"})
        result = validator.validate(ctx)
        assert result.approved is False
        assert "LIVE_RISK_DATA_UNAVAILABLE" in result.reason

    def test_new_sector_not_yet_in_snapshot_counts_from_zero(self):
        """Signal for a sector with zero existing exposure — should be approved if under cap."""
        ctx = _context(
            symbol="TCS",
            qty=10,
            price=3500.0,
            portfolio_nav=1_000_000.0,
            sector_exposures={"ENERGY": 200_000.0},  # IT not present
        )
        validator = self._validator(
            max_sector_pct=30.0,
            symbol_sector={"TCS": "IT_SERVICES"},
        )
        result = validator.validate(ctx)
        # IT_SERVICES starts at 0, adding 10×3500=35k → 3.5% < 30%
        assert result.approved is True

    def test_details_include_sector_and_percentages(self):
        ctx = _context(
            symbol="RELIANCE",
            qty=100,
            price=1000.0,
            portfolio_nav=1_000_000.0,
            sector_exposures={"ENERGY": 100_000.0},
        )
        validator = self._validator(max_sector_pct=50.0, symbol_sector={"RELIANCE": "ENERGY"})
        result = validator.validate(ctx)
        assert result.approved is True
        assert result.details["sector"] == "ENERGY"
        assert "proposed_sector_pct" in result.details
        assert "max_sector_pct" in result.details


# ---------------------------------------------------------------------------
# LiquidityValidator
# ---------------------------------------------------------------------------

class TestLiquidityValidator:
    def _validator(
        self,
        max_adv_pct: float = 1.0,
        overrides: dict | None = None,
    ) -> LiquidityValidator:
        return LiquidityValidator(
            max_order_adv_pct=max_adv_pct,
            per_symbol_overrides=overrides or {},
        )

    def test_approved_when_order_within_adv_threshold(self):
        # ADV = 100k (below _LIQUID_ADV_FLOOR of 1M so ratio check runs),
        # order = 50 shares → 0.05% << 1% → approved with order_adv_pct in details.
        ctx = _context(qty=50, adv_20d=100_000.0)
        result = self._validator(max_adv_pct=1.0).validate(ctx)
        assert result.approved is True
        assert result.details["order_adv_pct"] < 1.0

    def test_rejected_when_order_exceeds_adv_threshold(self):
        # ADV = 10,000 shares, order = 200 → 2.0% > 1%
        ctx = _context(qty=200, adv_20d=10_000.0)
        result = self._validator(max_adv_pct=1.0).validate(ctx)
        assert result.approved is False
        assert "2.00%" in result.reason

    def test_exactly_at_threshold_is_approved(self):
        # ADV = 10,000, order = 100 → exactly 1%
        ctx = _context(qty=100, adv_20d=10_000.0)
        result = self._validator(max_adv_pct=1.0).validate(ctx)
        assert result.approved is True

    def test_approved_with_low_adv_data_warning_when_none(self):
        ctx = _context(qty=100, adv_20d=None)
        result = self._validator().validate(ctx)
        assert result.approved is True
        assert "LOW_ADV_DATA" in result.reason

    def test_live_rejected_when_adv_missing(self):
        ctx = _context(qty=100, adv_20d=None, paper_trade=False)
        result = self._validator().validate(ctx)
        assert result.approved is False
        assert "LIVE_RISK_DATA_UNAVAILABLE" in result.reason

    def test_approved_with_low_adv_data_warning_when_zero(self):
        ctx = _context(qty=100, adv_20d=0.0)
        result = self._validator().validate(ctx)
        assert result.approved is True
        assert "LOW_ADV_DATA" in result.reason

    def test_approved_above_liquid_floor_skips_ratio_check(self):
        """ADV >= 1M shares → skip ratio check regardless of order size."""
        # 200k order / 1M ADV = 20% which would normally be rejected
        # but liquid floor kicks in first
        ctx = _context(qty=200_000, adv_20d=1_000_000.0)
        result = self._validator(max_adv_pct=1.0).validate(ctx)
        assert result.approved is True
        assert "Liquid instrument" in result.reason

    def test_per_symbol_override_threshold_used(self):
        # ZOMATO has lenient 5% threshold; order is 3% of ADV → approved
        ctx = _context(symbol="ZOMATO", qty=1500, adv_20d=50_000.0)
        validator = self._validator(max_adv_pct=1.0, overrides={"ZOMATO": 5.0})
        result = validator.validate(ctx)
        assert result.approved is True
        assert result.details["max_order_adv_pct"] == 5.0

    def test_global_threshold_used_without_override(self):
        ctx = _context(symbol="RELIANCE", qty=200, adv_20d=10_000.0)
        validator = self._validator(max_adv_pct=1.0, overrides={"OTHER": 5.0})
        result = validator.validate(ctx)
        # 2% > 1% global → reject
        assert result.approved is False
        assert result.details["max_order_adv_pct"] == 1.0

    def test_details_include_adv_and_order_qty(self):
        ctx = _context(qty=50, adv_20d=100_000.0)
        result = self._validator(max_adv_pct=1.0).validate(ctx)
        assert result.approved is True
        assert result.details["adv_20d"] == 100_000.0
        assert result.details["order_qty"] == 50.0
        assert result.details["order_adv_pct"] == pytest.approx(0.05)

    def test_validator_name_is_set(self):
        ctx = _context(qty=50, adv_20d=100_000.0)
        result = self._validator().validate(ctx)
        assert result.validator_name == "liquidity_validator"


# ---------------------------------------------------------------------------
# Cross-validator: graceful degradation is consistent
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    """Confirms all three validators approve when their data fields are None/empty."""

    def test_spread_gate_approves_on_none_spread(self):
        ctx = _context(live_spread_bps=None)
        result = SpreadGateValidator(max_spread_bps=50.0).validate(ctx)
        assert result.approved is True

    def test_liquidity_approves_on_none_adv(self):
        ctx = _context(adv_20d=None)
        result = LiquidityValidator(max_order_adv_pct=1.0).validate(ctx)
        assert result.approved is True

    def test_sector_approves_on_empty_sector_exposures(self):
        ctx = _context(sector_exposures={})
        limits = _limits(max_sector_pct=30.0)
        result = SectorConcentrationValidator(limits=limits, instrument_registry=None).validate(ctx)
        assert result.approved is True

    def test_all_three_approve_on_fresh_deployment_context(self):
        """Simulate context immediately after deployment: no analytics, no quotes, no ADV."""
        ctx = _context(
            live_spread_bps=None,
            adv_20d=None,
            sector_exposures={},
        )
        spread_result = SpreadGateValidator().validate(ctx)
        liq_result = LiquidityValidator().validate(ctx)
        limits = _limits()
        sector_result = SectorConcentrationValidator(limits=limits).validate(ctx)

        assert spread_result.approved is True
        assert liq_result.approved is True
        assert sector_result.approved is True


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

def _run_tests() -> int:
    import traceback
    test_classes = [
        TestSpreadGateValidator,
        TestSectorConcentrationValidator,
        TestLiquidityValidator,
        TestGracefulDegradation,
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
