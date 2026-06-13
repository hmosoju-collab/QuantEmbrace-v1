"""Unit tests for alpha_engine.research.replay (ADR-031)."""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from alpha_engine.research.replay import (  # noqa: E402
    build_offline_models,
    label_forecasts,
    replay_models,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _mock_forecast_dict(symbol: str = "RELIANCE", horizon: int = 15) -> dict:
    return {
        "forecast_id": "f-001",
        "model_id": "alpha_orb_v2",
        "model_version": "v1",
        "alpha_family": "momentum",
        "symbol": symbol,
        "market": "NSE",
        "universe": "NIFTY50",
        "direction": "BUY",
        "timeframe": "1m",
        "horizon_minutes": horizon,
        "forecast_return_bps": 80.0,
        "net_edge_bps": 60.0,
        "confidence": 0.8,
        "decision_price": 2500.0,
        "decision_ts": "2026-06-13T04:00:00+00:00",
        "edge_band": ">=50",
    }


class _MockAlphaForecast:
    def __init__(self, symbol: str, horizon: int) -> None:
        self._d = _mock_forecast_dict(symbol, horizon)

    def to_dict(self) -> dict:
        return dict(self._d)


class _MockModel:
    """Minimal AlphaModel stub for replay tests."""

    def __init__(self, timeframe: str, forecasts: list[Any]) -> None:
        self.timeframe = timeframe
        self._forecasts = forecasts

    async def on_bar(self, bar: Any) -> list[Any]:
        return list(self._forecasts)


class _MockCostModel:
    def apply(self, forecast: Any) -> Any:
        return forecast


class _MockBar:
    def __init__(self, interval: str) -> None:
        self.interval = interval


# ── build_offline_models ──────────────────────────────────────────────────────


class TestBuildOfflineModels:
    def test_unknown_model_id_is_silently_skipped(self):
        models = build_offline_models(
            ["nonexistent_model_xyz"],
            "v1.0",
            symbols=["RELIANCE"],
            horizons_minutes=[15],
        )
        assert models == []

    def test_empty_model_ids_returns_empty_list(self):
        models = build_offline_models([], "v1.0", symbols=["RELIANCE"], horizons_minutes=[15])
        assert models == []

    def test_mix_of_valid_and_invalid_skips_invalid(self):
        models = build_offline_models(
            ["alpha_orb_v2", "does_not_exist"],
            "v1.0",
            symbols=["RELIANCE"],
            horizons_minutes=[15],
        )
        assert len(models) == 1

    def test_all_three_known_model_ids_produce_three_adapters(self):
        models = build_offline_models(
            ["alpha_orb_v2", "alpha_vwap_rev_v2", "alpha_trend_15m"],
            "v1.0",
            symbols=["RELIANCE"],
            horizons_minutes=[15, 30, 60],
        )
        assert len(models) == 3

    def test_model_version_stored_on_adapter(self):
        models = build_offline_models(
            ["alpha_orb_v2"],
            "test-version-99",
            symbols=["RELIANCE"],
            horizons_minutes=[15],
        )
        assert len(models) == 1
        assert models[0].model_version == "test-version-99"

    def test_default_universe_resolver_returns_unknown(self):
        models = build_offline_models(
            ["alpha_orb_v2"],
            "v1",
            symbols=["RELIANCE"],
            horizons_minutes=[15],
            universe_resolver=None,
        )
        assert len(models) == 1

    def test_custom_universe_resolver_produces_one_model(self):
        resolver = lambda s: "NIFTY50_TEST"
        models = build_offline_models(
            ["alpha_orb_v2"],
            "v1",
            symbols=["RELIANCE"],
            horizons_minutes=[15],
            universe_resolver=resolver,
        )
        assert len(models) == 1
        assert models[0].model_id == "alpha_orb_v2"


# ── replay_models ─────────────────────────────────────────────────────────────


class TestReplayModels:
    async def test_empty_bars_returns_empty_dataframe(self):
        result = await replay_models([], [])
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    async def test_no_models_returns_empty_dataframe(self):
        bar = _MockBar("minute")
        result = await replay_models([bar], [])
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    async def test_matching_timeframe_produces_records(self):
        forecast = _MockAlphaForecast("RELIANCE", 15)
        # CostModel.apply(forecast) — mock passes forecast through
        mock_cost = _MockCostModel()
        model = _MockModel(timeframe="1m", forecasts=[forecast])
        bar = _MockBar("minute")
        result = await replay_models([bar], [model], cost_model=mock_cost)
        assert len(result) == 1
        assert result.iloc[0]["symbol"] == "RELIANCE"

    async def test_mismatched_timeframe_skipped(self):
        forecast = _MockAlphaForecast("RELIANCE", 15)
        mock_cost = _MockCostModel()
        model = _MockModel(timeframe="15m", forecasts=[forecast])
        bar = _MockBar("minute")  # 1m bar, but model is 15m
        result = await replay_models([bar], [model], cost_model=mock_cost)
        assert result.empty

    async def test_multiple_bars_multiple_models_produces_all_records(self):
        forecast = _MockAlphaForecast("RELIANCE", 15)
        mock_cost = _MockCostModel()
        model = _MockModel(timeframe="1m", forecasts=[forecast])
        bars = [_MockBar("minute"), _MockBar("minute")]
        result = await replay_models(bars, [model], cost_model=mock_cost)
        assert len(result) == 2

    async def test_default_cost_model_applied(self):
        """replay_models uses CostModel() by default — verify no error."""
        model = _MockModel(timeframe="1m", forecasts=[])
        bar = _MockBar("minute")
        result = await replay_models([bar], [model], cost_model=None)
        assert result.empty


# ── label_forecasts ───────────────────────────────────────────────────────────


class TestLabelForecasts:
    def _prices_df(self, symbol: str = "RELIANCE") -> pd.DataFrame:
        return pd.DataFrame({
            "symbol": [symbol] * 3,
            "timestamp": pd.date_range("2026-06-13 04:00", periods=3, freq="15min", tz="UTC"),
            "price": [2500.0, 2520.0, 2510.0],
        })

    def _forecasts_df(self, symbol: str = "RELIANCE") -> pd.DataFrame:
        return pd.DataFrame({
            "symbol": [symbol],
            "decision_ts": pd.to_datetime(["2026-06-13 04:00:00+00:00"]),
            "horizon_minutes": [15],
            "direction": ["BUY"],
            "forecast_return_bps": [80.0],
        })

    def test_empty_forecasts_returns_with_realized_column(self):
        empty = pd.DataFrame(columns=["symbol", "decision_ts", "horizon_minutes"])
        result = label_forecasts(
            empty,
            self._prices_df(),
        )
        assert "realized_fwd_return_bps" in result.columns

    def test_labeled_column_added_to_nonempty_forecasts(self):
        def mock_forward_return(series, ts, delta):
            return 0.008  # 80 bps raw

        with patch.dict("sys.modules", {
            "backtesting": MagicMock(),
            "backtesting.model_dataset_builder": MagicMock(forward_return=mock_forward_return),
        }):
            result = label_forecasts(self._forecasts_df(), self._prices_df())

        assert "realized_fwd_return_bps" in result.columns
        assert len(result) == 1

    def test_realized_return_converted_to_bps(self):
        """forward_return returns a fraction; label_forecasts multiplies by 10_000."""
        def mock_forward_return(series, ts, delta):
            return 0.01  # 1% = 100 bps

        with patch.dict("sys.modules", {
            "backtesting": MagicMock(),
            "backtesting.model_dataset_builder": MagicMock(forward_return=mock_forward_return),
        }):
            result = label_forecasts(self._forecasts_df(), self._prices_df())

        assert result.iloc[0]["realized_fwd_return_bps"] == pytest.approx(100.0)

    def test_none_from_forward_return_stored_as_none(self):
        def mock_forward_return(series, ts, delta):
            return None

        with patch.dict("sys.modules", {
            "backtesting": MagicMock(),
            "backtesting.model_dataset_builder": MagicMock(forward_return=mock_forward_return),
        }):
            result = label_forecasts(self._forecasts_df(), self._prices_df())

        assert result.iloc[0]["realized_fwd_return_bps"] is None

    def test_unknown_symbol_gets_none_realized(self):
        # price_lookup won't have UNKNOWN_SYM → forward_return called with series=None
        # Real forward_return treats None series as unlabelable and returns None.
        def mock_forward_return(series, ts, delta):
            return None if series is None else 0.01

        forecasts = self._forecasts_df("UNKNOWN_SYM")

        with patch.dict("sys.modules", {
            "backtesting": MagicMock(),
            "backtesting.model_dataset_builder": MagicMock(forward_return=mock_forward_return),
        }):
            result = label_forecasts(forecasts, self._prices_df("RELIANCE"))

        assert result.iloc[0]["realized_fwd_return_bps"] is None

    def test_original_dataframe_not_mutated(self):
        def mock_forward_return(series, ts, delta):
            return 0.005

        original = self._forecasts_df()
        original_cols = set(original.columns)

        with patch.dict("sys.modules", {
            "backtesting": MagicMock(),
            "backtesting.model_dataset_builder": MagicMock(forward_return=mock_forward_return),
        }):
            label_forecasts(original, self._prices_df())

        assert set(original.columns) == original_cols
