import pytest

from qe.data.snapshot import SnapshotError
from qe.engine import run_null
from qe.journal import read_header, read_journal


def test_null_run_end_to_end(run_config):
    result = run_null(run_config)

    assert result.status == "OK"
    assert result.n_symbols == 2
    assert result.n_bars > 0
    assert result.journal_path.exists()

    header = read_header(result.journal_path)
    assert header["config_hash"] == run_config.config_hash()
    assert header["config"]["name"] == "m1-test"
    assert header["data_snapshot_id"] == result.data_snapshot_id
    assert header["code_sha"]  # never empty, "unknown" at worst

    records = list(read_journal(result.journal_path))
    assert records[0]["type"] == "SESSION_START"
    assert records[-1]["type"] == "SESSION_END"
    assert records[-1]["data"]["status"] == "OK"
    bars_events = [r for r in records if r["type"] == "BARS"]
    assert len(bars_events) == result.n_days
    assert sum(e["data"]["n_bars"] for e in bars_events) == result.n_bars


def test_pinned_snapshot_reruns_and_fails_closed_on_tamper(run_config, fixture_lake):
    first = run_null(run_config)

    pinned = run_config.model_copy(
        update={
            "name": "m1-test-pinned",
            "data": run_config.data.model_copy(update={"snapshot_id": first.data_snapshot_id}),
        }
    )
    second = run_null(pinned)
    assert second.data_snapshot_id == first.data_snapshot_id
    assert second.n_bars == first.n_bars

    # Tamper with the lake → pinned run must refuse to start.
    victim = next((fixture_lake / "ohlcv").rglob("part-0.parquet"))
    victim.write_bytes(b"corrupted")
    with pytest.raises(SnapshotError, match="verification failed"):
        run_null(pinned.model_copy(update={"name": "m1-test-tampered"}))
