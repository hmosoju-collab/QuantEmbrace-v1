"""python -m qe.ai end to end on an on-disk synthetic lake (fake backend)."""

from pathlib import Path
import subprocess
import sys

import yaml

from qe.ai.cli import main

REPO = Path(__file__).resolve().parents[3]


def _configs(base: Path, backend: str = "fake") -> str:
    (base / "configs").mkdir()
    book = {
        "name": "delivery-book-paper",
        "mode": "paper",
        "start_date": "2024-01-01",
        "end_date": "2027-12-31",
        "universe": {"market": "NSE", "segment": "EQ", "symbols": None},
        "data": {"lake_root": "lake", "interval": "1d"},
        "strategy": {"kind": "factor_book", "factor": "delivery", "top_n": 40, "k": 10},
    }
    ai = {
        "name": "e2e",
        "book_config": "configs/book.yaml",
        "research_mode": "STANDARD",
        "backend": backend,
        "quick_model": {"model_id": "fake-quick", "tier": "quick"},
        "deep_model": {"model_id": "fake-deep", "tier": "deep"},
        "max_symbols": 3,
    }
    (base / "configs" / "book.yaml").write_text(yaml.safe_dump(book))
    (base / "configs" / "ai.yaml").write_text(yaml.safe_dump(ai))
    return "configs/ai.yaml"


def _files(base: Path) -> set[Path]:
    return {p.relative_to(base) for p in base.rglob("*") if p.is_file()}


def test_research_and_report_end_to_end(synthetic_lake, synthetic_panel, capsys):
    base = synthetic_lake.parent
    cfg = _configs(base)
    before = _files(base)
    as_of = synthetic_panel.date_at(400).isoformat()
    assert main(["research", "--config", cfg, "--as-of", as_of, "--base-dir", str(base)]) == 0
    out = capsys.readouterr().out
    assert "signals  : 3" in out and "advisory" in out

    new = _files(base) - before
    roots = {p.parts[:2] for p in new}
    # Only AI locations, plus the snapshot manifest written through qe.data (provenance).
    assert roots <= {
        ("journals", "ai"),
        ("reports", "qe-ai"),
        ("backtest-data", "ai_cache"),
        ("lake", "_snapshots"),
    }
    assert not (base / "reports" / "qe").exists()  # gate evidence untouched
    assert not list((base / "journals").glob("paper-*"))  # live-gate evidence untouched

    journal = next((base / "journals" / "ai").glob("*.jsonl"))
    assert (
        main(["report", "--journal", str(journal.relative_to(base)), "--base-dir", str(base)]) == 0
    )


def test_paid_backend_is_refused_without_spend_flag(synthetic_lake, synthetic_panel, capsys):
    base = synthetic_lake.parent
    cfg = _configs(base, backend="bedrock")
    as_of = synthetic_panel.date_at(400).isoformat()
    assert main(["research", "--config", cfg, "--as-of", as_of, "--base-dir", str(base)]) == 2
    assert "--allow-llm-spend" in capsys.readouterr().err
    assert not (base / "journals").exists()  # refused before any journal/write


def test_module_entry_point_help():
    out = subprocess.run(
        [sys.executable, "-m", "qe.ai", "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0 and "Never trades" in out.stdout
