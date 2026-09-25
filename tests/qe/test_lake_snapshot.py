from datetime import date

import pytest

from qe.data import LakeError, OhlcvLake, create_snapshot, load_manifest, verify_snapshot


def test_load_bars_filters_inclusive_and_sorts(fixture_lake):
    lake = OhlcvLake(fixture_lake)
    bars = lake.load_bars(("TESTA", "TESTB"), date(2024, 1, 1), date(2024, 1, 5))
    dates = bars["timestamp"].dt.tz_convert("Asia/Kolkata").dt.date
    assert dates.min() >= date(2024, 1, 1)
    assert dates.max() <= date(2024, 1, 5)
    assert set(bars["symbol"]) == {"TESTA", "TESTB"}
    assert bars["timestamp"].is_monotonic_increasing


def test_load_bars_spans_years(fixture_lake):
    lake = OhlcvLake(fixture_lake)
    bars = lake.load_bars(("TESTA",), date(2023, 1, 1), date(2024, 12, 31))
    years = set(bars["timestamp"].dt.tz_convert("Asia/Kolkata").dt.year)
    assert years == {2023, 2024}


def test_missing_symbol_fails_closed(fixture_lake):
    lake = OhlcvLake(fixture_lake)
    with pytest.raises(LakeError, match="GHOST"):
        lake.resolve_files(("TESTA", "GHOST"), date(2024, 1, 1), date(2024, 1, 31))


def test_not_a_lake_fails_closed(tmp_path):
    with pytest.raises(LakeError, match="not an OHLCV lake"):
        OhlcvLake(tmp_path)


def test_snapshot_idempotent_and_verifiable(fixture_lake):
    lake = OhlcvLake(fixture_lake)
    files = lake.resolve_files(("TESTA",), date(2024, 1, 1), date(2024, 1, 31))
    scope = {"symbols": ["TESTA"]}

    m1 = create_snapshot(fixture_lake, files, scope)
    m2 = create_snapshot(fixture_lake, files, scope)
    assert m1["snapshot_id"] == m2["snapshot_id"]
    assert m1["snapshot_id"].startswith("ds-")

    loaded = load_manifest(fixture_lake, m1["snapshot_id"])
    assert verify_snapshot(fixture_lake, loaded) == []


def test_snapshot_detects_tampering(fixture_lake):
    lake = OhlcvLake(fixture_lake)
    files = lake.resolve_files(("TESTB",), date(2024, 1, 1), date(2024, 1, 31))
    manifest = create_snapshot(fixture_lake, files, {})

    files[0].write_bytes(b"corrupted")
    problems = verify_snapshot(fixture_lake, manifest)
    assert len(problems) == 1
    assert problems[0].startswith("modified:")


def test_panel_tz_default_ist_unchanged(fixture_lake):
    from qe.data.panel import load_panel, resolve_panel_files

    files = resolve_panel_files(fixture_lake, date(2024, 1, 1), date(2024, 1, 31))
    panel = load_panel(files, date(2024, 1, 1), date(2024, 1, 31))
    assert str(panel.index.tz) == "Asia/Kolkata"
    assert panel.date_at(0) == date(2024, 1, 1)


def test_panel_us_market_ny_tz(fixture_lake):
    """US bars stamped 16:00 America/New_York keep their NY trading date when
    loaded with tz=America/New_York (ADR-041); the IST default would shift
    them to the next calendar day."""
    import pandas as pd

    from qe.data.panel import load_panel, resolve_panel_files

    ny = "America/New_York"
    ts = pd.date_range("2024-01-02 16:00", periods=5, freq="B", tz=ny)
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "symbol": "USTEST",
            "close": [100.0 + i for i in range(5)],
            "volume": [1000] * 5,
            "delivery_pct": [None] * 5,
        }
    )
    part_dir = (
        fixture_lake / "ohlcv" / "market=US" / "segment=EQ"
        / "symbol=USTEST" / "interval=1d" / "year=2024"
    )
    part_dir.mkdir(parents=True)
    bars.to_parquet(part_dir / "part-0.parquet", index=False)

    files = resolve_panel_files(fixture_lake, date(2024, 1, 1), date(2024, 1, 31), market="US")
    panel = load_panel(files, date(2024, 1, 1), date(2024, 1, 31), tz=ny)
    assert str(panel.index.tz) == ny
    assert panel.date_at(0) == date(2024, 1, 2)
    assert panel.date_at(len(panel.index) - 1) == date(2024, 1, 8)

    shifted = load_panel(files, date(2024, 1, 1), date(2024, 1, 31))  # IST default
    assert shifted.date_at(0) == date(2024, 1, 3)  # why tz must be market-aware
