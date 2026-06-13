"""Unit tests for alpha_engine.research.alpha_metrics (ADR-031)."""

from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from alpha_engine.research.alpha_metrics import (  # noqa: E402
    calibration_curve,
    conflict_win_rates,
    cost_adjusted_expectancy_bps,
    dir_sign,
    edge_band_study,
    hit_rate,
    ic_decay_curve,
    per_regime_breakdown,
    rank_ic,
    rolling_regime_ic,
    signed_realized_bps,
)


def _df(**extra) -> pd.DataFrame:
    """Minimal labeled forecast DataFrame.

    Signed realized:
      row 0: BUY × +80  =  +80  (correct)
      row 1: BUY × -20  =  -20  (incorrect)
      row 2: SELL × -70 =  +70  (correct)
      row 3: SELL × +30 =  -30  (incorrect)
    Mean signed = (80 - 20 + 70 - 30) / 4 = 25 bps.
    """
    base = {
        "model_id": ["alpha_orb_v2"] * 4,
        "direction": ["BUY", "BUY", "SELL", "SELL"],
        "horizon_minutes": [15, 15, 30, 30],
        "net_edge_bps": [60.0, 40.0, 55.0, 35.0],
        "confidence": [0.8, 0.6, 0.7, 0.5],
        "decision_ts": pd.to_datetime(
            ["2026-06-13 04:00", "2026-06-13 04:15", "2026-06-13 04:30", "2026-06-13 04:45"]
        ),
        "realized_fwd_return_bps": [80.0, -20.0, -70.0, 30.0],
    }
    base.update(extra)
    return pd.DataFrame(base)


def _all_unlabeled() -> pd.DataFrame:
    df = _df()
    df["realized_fwd_return_bps"] = float("nan")
    return df


class TestDirSign:
    def test_buy_variants_are_positive(self):
        s = dir_sign(pd.Series(["BUY", "buy", "Buy"]))
        assert list(s) == [1.0, 1.0, 1.0]

    def test_sell_variants_are_negative(self):
        s = dir_sign(pd.Series(["SELL", "sell", "Sell"]))
        assert list(s) == [-1.0, -1.0, -1.0]

    def test_unknown_is_zero(self):
        s = dir_sign(pd.Series(["LONG", "SHORT", ""]))
        assert list(s) == [0.0, 0.0, 0.0]

    def test_mixed_series(self):
        s = dir_sign(pd.Series(["BUY", "SELL", "BUY"]))
        assert list(s) == [1.0, -1.0, 1.0]


class TestSignedRealizedBps:
    def test_buy_positive_realized_stays_positive(self):
        signed = signed_realized_bps(_df())
        assert signed.iloc[0] == pytest.approx(80.0)

    def test_sell_negative_realized_becomes_positive(self):
        signed = signed_realized_bps(_df())
        assert signed.iloc[2] == pytest.approx(70.0)

    def test_buy_negative_realized_stays_negative(self):
        signed = signed_realized_bps(_df())
        assert signed.iloc[1] == pytest.approx(-20.0)

    def test_sell_positive_realized_becomes_negative(self):
        signed = signed_realized_bps(_df())
        assert signed.iloc[3] == pytest.approx(-30.0)


class TestRankIC:
    def test_no_labeled_rows_returns_none_ic(self):
        result = rank_ic(_all_unlabeled())
        assert result["ic"] is None
        assert result["n_groups"] == 0

    def test_single_observation_per_group_returns_none_ic(self):
        result = rank_ic(_df().iloc[:1], group_by_day=True)
        assert result["ic"] is None

    def test_ungrouped_four_rows_returns_n_equals_4(self):
        result = rank_ic(_df(), group_by_day=False)
        assert result["n"] == 4

    def test_perfect_positive_correlation_returns_ic_1(self):
        df = pd.DataFrame({
            "direction": ["BUY"] * 4,
            "horizon_minutes": [15] * 4,
            "net_edge_bps": [10.0, 20.0, 30.0, 40.0],
            "confidence": [0.5] * 4,
            "decision_ts": pd.to_datetime(["2026-06-13"] * 4),
            "realized_fwd_return_bps": [10.0, 20.0, 30.0, 40.0],
        })
        result = rank_ic(df, group_by_day=False)
        assert result["ic"] == pytest.approx(1.0)

    def test_perfect_negative_correlation_returns_ic_minus_1(self):
        df = pd.DataFrame({
            "direction": ["BUY"] * 4,
            "horizon_minutes": [15] * 4,
            "net_edge_bps": [40.0, 30.0, 20.0, 10.0],
            "confidence": [0.5] * 4,
            "decision_ts": pd.to_datetime(["2026-06-13"] * 4),
            "realized_fwd_return_bps": [10.0, 20.0, 30.0, 40.0],
        })
        result = rank_ic(df, group_by_day=False)
        assert result["ic"] == pytest.approx(-1.0)

    def test_t_stat_none_for_single_group(self):
        df = pd.DataFrame({
            "direction": ["BUY"] * 4,
            "horizon_minutes": [15] * 4,
            "net_edge_bps": [10.0, 20.0, 30.0, 40.0],
            "confidence": [0.5] * 4,
            "decision_ts": pd.to_datetime(["2026-06-13"] * 4),
            "realized_fwd_return_bps": [10.0, 20.0, 30.0, 40.0],
        })
        result = rank_ic(df, group_by_day=False)
        assert result["t_stat"] is None  # only 1 group → no std


class TestICDecayCurve:
    def test_one_row_per_horizon(self):
        curve = ic_decay_curve(_df())
        assert set(curve["horizon_minutes"]) == {15, 30}
        assert len(curve) == 2

    def test_sorted_by_horizon_ascending(self):
        curve = ic_decay_curve(_df())
        horizons = list(curve["horizon_minutes"])
        assert horizons == sorted(horizons)

    def test_columns_present(self):
        curve = ic_decay_curve(_df())
        assert set(curve.columns) >= {"horizon_minutes", "ic", "n"}


class TestHitRate:
    def test_no_labeled_returns_none(self):
        assert hit_rate(_all_unlabeled()) is None

    def test_all_correct_returns_one(self):
        df = pd.DataFrame({
            "direction": ["BUY"] * 3,
            "realized_fwd_return_bps": [50.0, 30.0, 10.0],
        })
        assert hit_rate(df) == pytest.approx(1.0)

    def test_all_incorrect_returns_zero(self):
        df = pd.DataFrame({
            "direction": ["BUY"] * 3,
            "realized_fwd_return_bps": [-10.0, -20.0, -30.0],
        })
        assert hit_rate(df) == pytest.approx(0.0)

    def test_half_correct(self):
        # rows 0,2 correct; rows 1,3 incorrect
        assert hit_rate(_df()) == pytest.approx(0.5)


class TestCostAdjustedExpectancy:
    def test_no_labeled_returns_none(self):
        assert cost_adjusted_expectancy_bps(_all_unlabeled()) is None

    def test_known_values_with_default_cost(self):
        # mean signed = 25; expectancy = 25 - 20 = 5
        assert cost_adjusted_expectancy_bps(_df()) == pytest.approx(5.0)

    def test_zero_cost(self):
        assert cost_adjusted_expectancy_bps(_df(), cost_bps=0.0) == pytest.approx(25.0)

    def test_high_cost_makes_expectancy_negative(self):
        assert cost_adjusted_expectancy_bps(_df(), cost_bps=50.0) == pytest.approx(-25.0)


class TestCalibrationCurve:
    def test_no_labeled_returns_empty_with_correct_columns(self):
        result = calibration_curve(_all_unlabeled())
        assert list(result.columns) == ["bucket", "n", "mean_confidence", "hit_rate"]
        assert result.empty

    def test_hit_rate_within_zero_one(self):
        result = calibration_curve(_df())
        for val in result["hit_rate"]:
            assert 0.0 <= val <= 1.0

    def test_bucket_count_at_most_n_buckets(self):
        df = pd.DataFrame({
            "direction": ["BUY"] * 10,
            "confidence": [i * 0.1 for i in range(10)],
            "realized_fwd_return_bps": [10.0] * 10,
        })
        result = calibration_curve(df, n_buckets=5)
        assert len(result) <= 5

    def test_n_sums_to_total_labeled(self):
        result = calibration_curve(_df())
        assert result["n"].sum() == 4


class TestEdgeBandStudy:
    def test_default_four_bands(self):
        result = edge_band_study(_df())
        assert list(result["min_net_edge_bps"]) == [20.0, 30.0, 40.0, 50.0]

    def test_higher_band_has_fewer_or_equal_rows(self):
        result = edge_band_study(_df())
        ns = list(result["n"])
        for i in range(len(ns) - 1):
            assert ns[i] >= ns[i + 1]

    def test_custom_bands(self):
        result = edge_band_study(_df(), bands=[50.0, 100.0])
        assert list(result["min_net_edge_bps"]) == [50.0, 100.0]

    def test_band_above_all_edges_has_zero_rows(self):
        result = edge_band_study(_df(), bands=[999.0])
        assert result.iloc[0]["n"] == 0


class TestPerRegimeBreakdown:
    def test_no_regime_column_returns_empty(self):
        result = per_regime_breakdown(_df())
        assert result.empty

    def test_two_regimes_returns_two_rows(self):
        df = _df(regime=["bull", "bull", "bear", "bear"])
        result = per_regime_breakdown(df, regime_col="regime")
        assert set(result["regime"]) == {"bull", "bear"}
        assert len(result) == 2

    def test_columns_present(self):
        df = _df(regime=["bull"] * 4)
        result = per_regime_breakdown(df, regime_col="regime")
        assert set(result.columns) >= {"regime", "n", "ic", "hit_rate"}


class TestConflictWinRates:
    def test_no_required_columns_returns_empty(self):
        assert conflict_win_rates(_df()).empty

    def test_single_family_conflict_skipped(self):
        df = pd.DataFrame({
            "direction": ["BUY", "SELL"],
            "realized_fwd_return_bps": [100.0, -50.0],
            "conflict_group_id": ["g1", "g1"],
            "alpha_family": ["momentum", "momentum"],
        })
        assert conflict_win_rates(df).empty

    def test_two_family_conflict_winner_identified(self):
        # BUY momentum: signed +100; SELL reversal: signed +10 (raw -10 × -1)
        df = pd.DataFrame({
            "direction": ["BUY", "SELL"],
            "realized_fwd_return_bps": [100.0, -10.0],
            "conflict_group_id": ["g1", "g1"],
            "alpha_family": ["momentum", "reversal"],
        })
        result = conflict_win_rates(df)
        assert len(result) == 2
        winner = result[result["wins"] == 1]["alpha_family"].iloc[0]
        assert winner == "momentum"

    def test_win_rate_is_wins_over_appearances(self):
        df = pd.DataFrame({
            "direction": ["BUY", "SELL", "BUY", "SELL"],
            "realized_fwd_return_bps": [100.0, -10.0, 5.0, -200.0],
            "conflict_group_id": ["g1", "g1", "g2", "g2"],
            "alpha_family": ["momentum", "reversal", "reversal", "momentum"],
        })
        result = conflict_win_rates(df)
        for _, row in result.iterrows():
            assert row["win_rate"] == row["wins"] / row["appearances"]


class TestRollingRegimeIC:
    def test_no_regime_column_returns_empty(self):
        result = rolling_regime_ic(_df(), regime="bull")
        assert result.empty

    def test_regime_not_in_data_returns_empty(self):
        df = _df(regime=["bear"] * 4)
        result = rolling_regime_ic(df, regime="bull", regime_col="regime")
        assert result.empty

    def test_one_row_per_window(self):
        df = pd.DataFrame({
            "direction": ["BUY"] * 12,
            "realized_fwd_return_bps": [50.0] * 12,
            "net_edge_bps": [60.0] * 12,
            "confidence": [0.7] * 12,
            "horizon_minutes": [15] * 12,
            "decision_ts": pd.date_range("2026-01-01", periods=12, freq="7D"),
            "regime": ["bull"] * 12,
        })
        result = rolling_regime_ic(df, regime="bull", regime_col="regime", windows=[30, 60])
        assert len(result) == 2
        assert set(result["window_days"]) == {30, 60}
