"""Unit tests for the Zerodha intraday fetch + backtest pipeline (Phase B).

Covers:
  * Zerodha source is HIGH trust (data-lake contract §1 alignment).
  * Kite date-chunking respects per-interval request caps.
  * The fetcher writes a schema-valid, tz-aware, year-partitioned Parquet lake
    with a HIGH-trust manifest (exercised via an in-memory Kite stub).
  * The intraday backtest runner loads that lake, runs per-day, and returns a
    metrics dict + a defensible verdict.
  * Governance guards: the fetcher places NO orders; the runner touches NO
    broker / Kite APIs at all.

Backtest-only: no network, no real AWS, no broker. The Kite stub generates
synthetic candles in-process.

Run:  python -m pytest tests/backtest/test_intraday_fetch_and_backtest.py -q
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))
sys.path.insert(0, str(_REPO / "scripts" / "backtest"))

import fetch_zerodha_intraday as fz  # noqa: E402
import run_intraday_backtest as rib  # noqa: E402
from backtesting.s3_data_catalog import TrustLevel, classify_source_trust  # noqa: E402


# ── trust classification ─────────────────────────────────────────────────────


def test_zerodha_is_high_trust():
    assert classify_source_trust("zerodha_kite") is TrustLevel.HIGH
    assert classify_source_trust("zerodha") is TrustLevel.HIGH
    assert classify_source_trust("kite") is TrustLevel.HIGH
    # case-insensitive
    assert classify_source_trust("ZERODHA_KITE") is TrustLevel.HIGH
    # unrelated free source still LOW
    assert classify_source_trust("github") is TrustLevel.LOW


def test_fetcher_source_labels_are_high():
    assert fz.SOURCE_NAME == "zerodha_kite"
    assert classify_source_trust(fz.SOURCE_NAME) is TrustLevel.HIGH
    assert fz.TRUST_LEVEL == "HIGH"


# ── chunking ─────────────────────────────────────────────────────────────────


def test_date_chunks_respect_cap_and_are_contiguous():
    chunks = fz._date_chunks(date(2024, 1, 1), date(2024, 6, 30), 60)
    # No chunk exceeds the cap.
    for (a, b) in chunks:
        assert (b - a).days < 60
    # Contiguous, no overlap, full coverage.
    assert chunks[0][0] == date(2024, 1, 1)
    assert chunks[-1][1] == date(2024, 6, 30)
    for (prev, nxt) in zip(chunks, chunks[1:]):
        assert (nxt[0] - prev[1]).days == 1


def test_date_chunks_single_window_when_short():
    chunks = fz._date_chunks(date(2024, 1, 1), date(2024, 1, 10), 60)
    assert chunks == [(date(2024, 1, 1), date(2024, 1, 10))]


# ── fetcher → lake ───────────────────────────────────────────────────────────


def _build_lake(tmp: Path) -> Path:
    rc = fz.fetch(
        kite=fz._StubKite(),
        symbols=["RELIANCE", "INFY"],
        intervals=["1m", "5m", "15m"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
        base=tmp,
        force=False,
        verbose=False,
    )
    assert rc == 0
    return tmp


def test_fetcher_writes_schema_valid_lake(tmp_path):
    base = _build_lake(tmp_path)
    part = (
        base / "lake" / "ohlcv" / "market=NSE" / "segment=EQ"
        / "symbol=RELIANCE" / "interval=1m" / "year=2024" / "part-0.parquet"
    )
    assert part.exists()
    df = pd.read_parquet(part)
    # tz-aware IST timestamps (required by DataFrameBarSource.from_dataframe)
    assert df["timestamp"].dt.tz is not None
    # canonical columns present
    for col in ("symbol", "market", "segment", "interval", "open", "high", "low", "close", "volume"):
        assert col in df.columns
    assert (df["interval"] == "1m").all()
    assert (df["source"] == "zerodha_kite").all()
    assert (df["trust_level"] == "HIGH").all()
    # OHLC sanity preserved
    assert ((df["low"] <= df["open"]) & (df["open"] <= df["high"])).all()


def test_fetcher_idempotent_skip(tmp_path):
    _build_lake(tmp_path)
    part = (
        tmp_path / "lake" / "ohlcv" / "market=NSE" / "segment=EQ"
        / "symbol=INFY" / "interval=15m" / "year=2024" / "part-0.parquet"
    )
    mtime_before = part.stat().st_mtime
    # Re-run without --force: existing partitions must be skipped (not rewritten).
    fz.fetch(
        kite=fz._StubKite(), symbols=["INFY"], intervals=["15m"],
        start=date(2024, 1, 1), end=date(2024, 1, 15),
        base=tmp_path, force=False, verbose=False,
    )
    assert part.stat().st_mtime == mtime_before


def test_fetcher_manifest_is_high_trust(tmp_path):
    _build_lake(tmp_path)
    import json
    man = json.loads(fz._manifest_path(tmp_path).read_text())
    assert man["source"] == "zerodha_kite"
    assert man["trust_level"] == "HIGH"
    assert man["license"] == "kite-connect-personal-use"
    assert len(man["entries"]) >= 1


# ── runner ───────────────────────────────────────────────────────────────────


def test_runner_backtest_returns_metrics(tmp_path):
    _build_lake(tmp_path)
    r = rib._backtest_strategy(
        "orb", ["RELIANCE", "INFY"], str(tmp_path),
        date(2024, 1, 1), date(2024, 1, 15), verbose=False,
    )
    assert r is not None
    assert isinstance(r["metrics"], dict)
    assert "number_of_trades" in r["metrics"]
    assert r["days"] > 0          # per-day execution actually ran
    assert r["interval"] == "1m"
    n = int(r["metrics"].get("number_of_trades", 0))
    assert rib._verdict(r["gates"], n) in {
        "ELIGIBLE_FOR_PAPER_PRIORITIZATION", "PAPER_OPTIMIZATION", "REJECT", "NO_TRADES",
    }


def test_runner_missing_interval_returns_none(tmp_path):
    # Only 1m/5m/15m exist; ask trend_15m (15m) after building only 1m.
    fz.fetch(
        kite=fz._StubKite(), symbols=["RELIANCE"], intervals=["1m"],
        start=date(2024, 1, 1), end=date(2024, 1, 5),
        base=tmp_path, force=False, verbose=False,
    )
    r = rib._backtest_strategy(
        "trend_15m", ["RELIANCE"], str(tmp_path),
        date(2024, 1, 1), date(2024, 1, 5), verbose=False,
    )
    assert r is None  # no 15m partitions → graceful None (caller reports NO DATA)


def test_warm_start_design_invariants():
    """trend_15m must warm-start (carry indicator buffers across days); session-anchored
    strategies (orb/vwap/preclose) must NOT — their reset_daily clears opening range / VWAP,
    which has to reset each day. The NIFTY regime gate is disabled for the backtest."""
    assert "trend_15m" in rib.WARM_START_STRATEGIES
    for s in ("orb", "vwap_reversion", "preclose"):
        assert s not in rib.WARM_START_STRATEGIES, f"{s} must stay fresh-per-day"
    assert rib._STRATEGY_BUILD_OVERRIDES["trend_15m"]["enable_nifty_gate"] is False


def test_verdict_logic():
    assert rib._verdict({"overall_pass": True}, 10) == "ELIGIBLE_FOR_PAPER_PRIORITIZATION"
    assert rib._verdict({"overall_pass": False, "expectancy_gt_0": True}, 10) == "PAPER_OPTIMIZATION"
    assert rib._verdict({"overall_pass": False, "net_pnl_gt_0": True}, 10) == "PAPER_OPTIMIZATION"
    assert rib._verdict({"overall_pass": False}, 10) == "REJECT"
    assert rib._verdict({"overall_pass": True}, 0) == "NO_TRADES"


# ── governance guards ────────────────────────────────────────────────────────


def test_fetcher_places_no_orders():
    """The fetcher may read Kite history but must NEVER place/modify orders."""
    src = Path(fz.__file__).read_text().lower()
    forbidden = ["place_order", "submit_order", "modify_order", "cancel_order"]
    present = [t for t in forbidden if t in src]
    assert present == [], f"fetcher must not touch order APIs: {present}"


def test_runner_has_no_broker_or_kite():
    """The runner is pure offline lake read — no broker, no Kite, no live state."""
    src = Path(rib.__file__).read_text().lower()
    forbidden = ["kiteconnect", "place_order", "submit_order", "zerodhabroker", "alpaca"]
    present = [t for t in forbidden if t in src]
    assert present == [], f"runner must not reference brokers/Kite: {present}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
