"""Unit tests for the backtesting lab data layer (Phase AWS-BT-2).

Covers loading (local CSV, local Parquet, S3 Parquet via a stubbed client) and the
data-quality checks (missing/duplicate candles, invalid OHLC, zero/negative price,
market-hours violation, outlier jump, future timestamp) plus trust/quarantine
classification.

Backtest-only: no broker APIs, no live tables. S3 is exercised with an in-memory
fake client — no network, no real AWS.

Run:  python -m pytest tests/backtest/test_data_layer.py -q
"""

from __future__ import annotations

import io
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

# Standalone-run safety (pytest also injects services via pyproject pythonpath).
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.data_loader import CANONICAL_COLUMNS, load_candles  # noqa: E402
from backtesting.data_quality import Severity, run_quality_checks  # noqa: E402
from backtesting.s3_data_catalog import (  # noqa: E402
    TrustLevel,
    classify_source_trust,
    is_quarantined,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _clean_rows(day: str = "2020-01-01", n: int = 6, start: str = "10:00") -> list[dict]:
    """A handful of valid in-hours 1m candles (naive ISO timestamps)."""
    base = pd.Timestamp(f"{day} {start}:00")
    rows = []
    for i in range(n):
        ts = base + pd.Timedelta(minutes=i)
        rows.append(
            {
                "timestamp": ts.isoformat(),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1000 + i,
            }
        )
    return rows


def _write_csv(tmp_path: Path, rows: list[dict], name: str = "data.csv") -> str:
    p = tmp_path / name
    pd.DataFrame(rows).to_csv(p, index=False)
    return str(p)


def _load_rows(tmp_path, rows, *, interval="1m", source_name="bhavcopy", symbol="RELIANCE"):
    path = _write_csv(tmp_path, rows)
    return load_candles(path, symbol=symbol, interval=interval, source_name=source_name)


def _checks(tmp_path, rows, **kw):
    res = _load_rows(tmp_path, rows, **kw)
    return run_quality_checks(
        res.df,
        interval=kw.get("interval", "1m"),
        source=res.source,
        trust=res.trust_level,
    )


def _issue(result, check):
    return next((i for i in result.issues if i.check == check), None)


# ── loading ──────────────────────────────────────────────────────────────────


def test_loads_local_csv(tmp_path):
    res = _load_rows(tmp_path, _clean_rows())
    assert len(res.df) == 6
    assert list(res.df.columns)[: len(CANONICAL_COLUMNS)] == CANONICAL_COLUMNS
    assert str(res.df["timestamp"].dt.tz) == "Asia/Kolkata"
    assert (res.df["symbol"] == "RELIANCE").all()
    assert res.trust_level is TrustLevel.HIGH


def test_loads_local_parquet(tmp_path):
    p = tmp_path / "data.parquet"
    pd.DataFrame(_clean_rows()).to_parquet(p, index=False)
    res = load_candles(str(p), symbol="RELIANCE", interval="1m", source_name="bhavcopy")
    assert len(res.df) == 6
    assert str(res.df["timestamp"].dt.tz) == "Asia/Kolkata"
    assert res.df["close"].dtype.kind == "f"


def test_loads_s3_parquet_with_stubbed_client(tmp_path):
    # Build parquet bytes in memory.
    buf = io.BytesIO()
    pd.DataFrame(_clean_rows()).to_parquet(buf, index=False)
    body = buf.getvalue()

    class _Body:
        def __init__(self, b):
            self._b = b

        def read(self):
            return self._b

    class FakeS3Client:
        def __init__(self, objects):
            self._objects = objects

        def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None):
            keys = [k for k in self._objects if k.startswith(Prefix)]
            return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

        def get_object(self, Bucket, Key):
            return {"Body": _Body(self._objects[Key])}

    key = "lake/ohlcv/market=NSE/segment=EQ/symbol=RELIANCE/interval=1m/year=2020/part-0.parquet"
    fake = FakeS3Client({key: body})
    res = load_candles(
        "s3://quantembrace-backtest-data/lake/ohlcv/market=NSE/segment=EQ/symbol=RELIANCE/interval=1m/",
        symbol="RELIANCE",
        interval="1m",
        source_name="bhavcopy",
        s3_client=fake,
    )
    assert len(res.df) == 6
    assert res.files_read == [key]
    assert str(res.df["timestamp"].dt.tz) == "Asia/Kolkata"


# ── quality checks ───────────────────────────────────────────────────────────


def test_detects_missing_candles(tmp_path):
    # 6 one-minute bars on a day that should hold 375 → missing.
    result = _checks(tmp_path, _clean_rows(n=6), interval="1m")
    issue = _issue(result, "missing_candles")
    assert issue is not None
    assert issue.severity is Severity.WARN
    assert issue.count == 375 - 6


def test_detects_duplicate_candles(tmp_path):
    rows = _clean_rows(n=3)
    rows.append(dict(rows[0]))  # exact duplicate timestamp
    result = _checks(tmp_path, rows, interval="1m")
    issue = _issue(result, "duplicate_timestamps")
    assert issue is not None and issue.severity is Severity.ERROR
    assert not result.passed


def test_detects_invalid_ohlc(tmp_path):
    rows = _clean_rows(n=3)
    rows[1].update({"open": 100, "high": 95, "low": 99, "close": 102})  # high < low/close
    result = _checks(tmp_path, rows, interval="1m")
    issue = _issue(result, "invalid_ohlc")
    assert issue is not None and issue.severity is Severity.ERROR
    assert not result.passed


def test_detects_zero_and_negative_prices(tmp_path):
    rows = _clean_rows(n=3)
    rows[0].update({"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0})
    rows[2].update({"open": -5.0, "high": -1.0, "low": -6.0, "close": -2.0})
    result = _checks(tmp_path, rows, interval="1m")
    issue = _issue(result, "nonpositive_prices")
    assert issue is not None and issue.severity is Severity.ERROR
    assert issue.count >= 2


def test_detects_market_hour_violation(tmp_path):
    rows = _clean_rows(n=2)
    rows.append(
        {
            "timestamp": "2020-01-01 08:00:00",  # before 09:15 IST
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 500,
        }
    )
    result = _checks(tmp_path, rows, interval="1m")
    issue = _issue(result, "market_hours_violation")
    assert issue is not None and issue.severity is Severity.ERROR
    assert issue.count == 1


def test_detects_outlier_jump(tmp_path):
    rows = _clean_rows(n=2)
    rows[1].update({"open": 150, "high": 151, "low": 149, "close": 150})  # +50% vs 100
    result = _checks(tmp_path, rows, interval="1m")
    issue = _issue(result, "outlier_jumps")
    assert issue is not None and issue.severity is Severity.WARN
    assert issue.count >= 1


def test_rejects_future_timestamp(tmp_path):
    future = (pd.Timestamp.now(tz="Asia/Kolkata") + timedelta(days=2)).strftime("%Y-%m-%d 10:00:00")
    rows = _clean_rows(n=2)
    rows.append({"timestamp": future, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1})
    result = _checks(tmp_path, rows, interval="1m")
    issue = _issue(result, "future_timestamps")
    assert issue is not None and issue.severity is Severity.ERROR
    assert not result.passed


# ── trust / quarantine ───────────────────────────────────────────────────────


def test_github_source_is_low_trust_and_quarantined(tmp_path):
    assert classify_source_trust("github") is TrustLevel.LOW
    assert is_quarantined(TrustLevel.LOW) is True

    res = _load_rows(tmp_path, _clean_rows(), source_name="github")
    assert res.trust_level is TrustLevel.LOW
    assert res.quarantined is True

    result = run_quality_checks(res.df, interval="1m", source="github", trust=res.trust_level)
    assert result.quarantined is True
    assert result.eligible_for_use is False  # LOW trust ⇒ never eligible


def test_official_bhavcopy_is_high_trust_and_eligible(tmp_path):
    res = _load_rows(tmp_path, _clean_rows(n=375, start="09:15"), source_name="bhavcopy")
    result = run_quality_checks(res.df, interval="1m", source="bhavcopy", trust=res.trust_level)
    assert result.trust_level is TrustLevel.HIGH
    assert result.quarantined is False
    assert result.passed is True
    assert result.eligible_for_use is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
