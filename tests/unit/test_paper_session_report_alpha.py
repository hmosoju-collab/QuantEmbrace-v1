"""Unit tests for paper_session_report alpha per-model breakdown (ADR-031 follow-up)."""

from __future__ import annotations

import sys
import os
from dataclasses import asdict
from datetime import date
from unittest.mock import MagicMock

import pytest

# paper_session_report lives in scripts/, not services/
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCRIPTS_DIR = os.path.join(_ROOT, "scripts", "monitoring")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from paper_session_report import AlphaMetrics, ModelAccuracy, _fetch_alpha_metrics  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────────

def _dyn_item(model_id: str, label_status: str, hit: bool | None = None) -> dict:
    """Build a minimal DynamoDB-format alpha-forecasts item."""
    item: dict = {
        "label_status": {"S": label_status},
        "model_id": {"S": model_id},
    }
    if hit is not None:
        item["hit"] = {"BOOL": hit}
    return item


def _mock_dynamo(items: list[dict]) -> MagicMock:
    """Return a low-level boto3 DynamoDB client mock that paginates one page."""
    page = {"Items": items}
    paginator = MagicMock()
    paginator.paginate.return_value = [page]
    client = MagicMock()
    client.get_paginator.return_value = paginator
    return client


# ── ModelAccuracy unit tests ──────────────────────────────────────────────────

class TestModelAccuracy:
    def test_accuracy_pct_no_labeled(self):
        m = ModelAccuracy(total=5, pending=5)
        assert m.accuracy_pct == 0.0

    def test_accuracy_pct_all_correct(self):
        m = ModelAccuracy(correct=8, incorrect=0)
        assert m.accuracy_pct == 100.0

    def test_accuracy_pct_mixed(self):
        m = ModelAccuracy(correct=7, incorrect=3)
        assert m.accuracy_pct == pytest.approx(70.0)

    def test_accuracy_pct_all_wrong(self):
        m = ModelAccuracy(correct=0, incorrect=4)
        assert m.accuracy_pct == 0.0

    def test_dataclass_asdict_compatible(self):
        m = ModelAccuracy(total=10, labeled=5, correct=3, incorrect=2, pending=5)
        d = asdict(m)
        assert d["total"] == 10
        assert d["correct"] == 3


# ── AlphaMetrics.by_model field ──────────────────────────────────────────────

class TestAlphaMetricsByModel:
    def test_by_model_defaults_empty(self):
        a = AlphaMetrics()
        assert a.by_model == {}

    def test_two_instances_dont_share_by_model(self):
        a1 = AlphaMetrics()
        a2 = AlphaMetrics()
        a1.by_model["x"] = ModelAccuracy(total=1)
        assert "x" not in a2.by_model

    def test_asdict_converts_model_accuracy_values(self):
        a = AlphaMetrics(total_forecasts=3)
        a.by_model["alpha_orb_v2"] = ModelAccuracy(total=3, correct=2, incorrect=1)
        d = asdict(a)
        assert isinstance(d["by_model"]["alpha_orb_v2"], dict)
        assert d["by_model"]["alpha_orb_v2"]["correct"] == 2


# ── _fetch_alpha_metrics per-model breakdown ──────────────────────────────────

class TestFetchAlphaMetricsPerModel:
    _DATE = date(2026, 6, 13)

    def test_single_model_correct_forecast(self):
        dynamo = _mock_dynamo([
            _dyn_item("alpha_orb_v2", "LABELED", hit=True),
        ])
        m = _fetch_alpha_metrics(dynamo, "qe-dev", self._DATE)
        assert m.total_forecasts == 1
        assert m.correct == 1
        assert "alpha_orb_v2" in m.by_model
        assert m.by_model["alpha_orb_v2"].correct == 1

    def test_single_model_incorrect_forecast(self):
        dynamo = _mock_dynamo([
            _dyn_item("alpha_orb_v2", "LABELED", hit=False),
        ])
        m = _fetch_alpha_metrics(dynamo, "qe-dev", self._DATE)
        assert m.incorrect == 1
        assert m.by_model["alpha_orb_v2"].incorrect == 1

    def test_pending_forecast_counted_per_model(self):
        dynamo = _mock_dynamo([
            _dyn_item("alpha_vwap_reversion_v2", "PENDING"),
        ])
        m = _fetch_alpha_metrics(dynamo, "qe-dev", self._DATE)
        assert m.pending == 1
        assert m.by_model["alpha_vwap_reversion_v2"].pending == 1

    def test_unlabelable_forecast_counted_per_model(self):
        dynamo = _mock_dynamo([
            _dyn_item("alpha_orb_v2", "UNLABELABLE"),
        ])
        m = _fetch_alpha_metrics(dynamo, "qe-dev", self._DATE)
        assert m.unlabelable == 1
        assert m.by_model["alpha_orb_v2"].unlabelable == 1

    def test_two_models_grouped_separately(self):
        dynamo = _mock_dynamo([
            _dyn_item("alpha_orb_v2", "LABELED", hit=True),
            _dyn_item("alpha_orb_v2", "LABELED", hit=False),
            _dyn_item("alpha_vwap_reversion_v2", "LABELED", hit=True),
            _dyn_item("alpha_vwap_reversion_v2", "PENDING"),
        ])
        m = _fetch_alpha_metrics(dynamo, "qe-dev", self._DATE)
        assert m.total_forecasts == 4
        assert set(m.by_model.keys()) == {"alpha_orb_v2", "alpha_vwap_reversion_v2"}
        orb = m.by_model["alpha_orb_v2"]
        assert orb.total == 2
        assert orb.correct == 1
        assert orb.incorrect == 1
        vwap = m.by_model["alpha_vwap_reversion_v2"]
        assert vwap.total == 2
        assert vwap.correct == 1
        assert vwap.pending == 1

    def test_model_totals_match_aggregate_totals(self):
        dynamo = _mock_dynamo([
            _dyn_item("alpha_orb_v2", "LABELED", hit=True),
            _dyn_item("alpha_orb_v2", "LABELED", hit=True),
            _dyn_item("alpha_vwap_reversion_v2", "LABELED", hit=False),
            _dyn_item("alpha_vwap_reversion_v2", "UNLABELABLE"),
            _dyn_item("alpha_intraday_trend_15m", "PENDING"),
        ])
        m = _fetch_alpha_metrics(dynamo, "qe-dev", self._DATE)
        model_total = sum(mv.total for mv in m.by_model.values())
        assert model_total == m.total_forecasts
        model_correct = sum(mv.correct for mv in m.by_model.values())
        assert model_correct == m.correct
        model_pending = sum(mv.pending for mv in m.by_model.values())
        assert model_pending == m.pending

    def test_missing_model_id_falls_back_to_unknown(self):
        item = {"label_status": {"S": "PENDING"}}  # no model_id field
        dynamo = _mock_dynamo([item])
        m = _fetch_alpha_metrics(dynamo, "qe-dev", self._DATE)
        assert "unknown" in m.by_model

    def test_empty_table_returns_zeroed_metrics(self):
        dynamo = _mock_dynamo([])
        m = _fetch_alpha_metrics(dynamo, "qe-dev", self._DATE)
        assert m.total_forecasts == 0
        assert m.by_model == {}

    def test_dynamo_exception_returns_zeroed_metrics(self):
        dynamo = MagicMock()
        dynamo.get_paginator.side_effect = RuntimeError("DynamoDB unavailable")
        m = _fetch_alpha_metrics(dynamo, "qe-dev", self._DATE)
        assert m.total_forecasts == 0
        assert m.by_model == {}

    def test_pk_uses_correct_format(self):
        dynamo = _mock_dynamo([])
        _fetch_alpha_metrics(dynamo, "qe-dev", date(2026, 6, 13))
        _, kwargs = dynamo.get_paginator.return_value.paginate.call_args
        assert kwargs["ExpressionAttributeValues"][":pk"]["S"] == "DATE#2026-06-13#NSE"

    def test_projection_includes_model_id(self):
        dynamo = _mock_dynamo([])
        _fetch_alpha_metrics(dynamo, "qe-dev", date(2026, 6, 13))
        _, kwargs = dynamo.get_paginator.return_value.paginate.call_args
        assert "model_id" in kwargs["ProjectionExpression"]
