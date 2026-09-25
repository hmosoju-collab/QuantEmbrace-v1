import json

import pytest

from qe.journal import JournalError, JournalWriter, read_header, read_journal


def _start(journal: JournalWriter) -> None:
    journal.session_start(
        session_id="s1",
        mode="null",
        config_hash="c" * 64,
        config={"name": "t"},
        code_sha="abc123-dirty",
        data_snapshot_id="ds-1234",
    )


def test_round_trip_and_monotonic_seq(tmp_path):
    path = tmp_path / "s1.jsonl"
    with JournalWriter(path) as journal:
        _start(journal)
        journal.write("BARS", {"date": "2024-01-01", "n_bars": 2})
        journal.session_end("OK", {"n_bars": 2})

    records = list(read_journal(path))
    assert [r["type"] for r in records] == ["SESSION_START", "BARS", "SESSION_END"]
    assert [r["seq"] for r in records] == [0, 1, 2]

    header = read_header(path)
    assert header["config_hash"] == "c" * 64
    assert header["code_sha"] == "abc123-dirty"
    assert header["data_snapshot_id"] == "ds-1234"


def test_journal_never_appends_to_existing_file(tmp_path):
    path = tmp_path / "s1.jsonl"
    path.write_text("")
    with pytest.raises(JournalError, match="already exists"):
        JournalWriter(path)


def test_session_start_must_be_first(tmp_path):
    with JournalWriter(tmp_path / "s.jsonl") as journal:
        journal.write("BARS", {})
        with pytest.raises(JournalError, match="first record"):
            _start(journal)


def test_reader_fails_closed_on_seq_gap(tmp_path):
    path = tmp_path / "bad.jsonl"
    lines = [
        json.dumps({"seq": 0, "ts": "t", "type": "SESSION_START", "data": {}}),
        json.dumps({"seq": 2, "ts": "t", "type": "BARS", "data": {}}),  # gap
    ]
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(JournalError, match="seq"):
        list(read_journal(path))


def test_abort_is_journaled_on_exception(tmp_path):
    path = tmp_path / "s.jsonl"
    with pytest.raises(ValueError):
        with JournalWriter(path) as journal:
            _start(journal)
            raise ValueError("boom")
    records = list(read_journal(path))
    assert records[-1]["type"] == "SESSION_ABORT"
    assert "boom" in records[-1]["data"]["error"]
