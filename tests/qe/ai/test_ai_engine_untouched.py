"""The AI layer must not move any engine config hash (ADR-042 / ADR-043 §6).

The paper books fail closed when their config hash drifts. These are the
hashes the stored book states were rebound to on 2026-07-15 (ADR-042); if a
qe.ai change ever moves them, STOP — that is an engine change, not research.
"""

import json
from pathlib import Path

import pytest

from qe.config import RunConfig

REPO = Path(__file__).resolve().parents[3]
BOOK_HASHES = {
    "configs/qe_delivery_book_paper.yaml": "7c95f33da911aaec",
    "configs/qe_momentum_book_paper.yaml": "d377a933b653c17d",
}
STATE_FILES = {
    "configs/qe_delivery_book_paper.yaml": "backtest-data/paper_book/qe_delivery-book-paper_state.json",
    "configs/qe_momentum_book_paper.yaml": "backtest-data/paper_book/qe_momentum-book-paper_state.json",
}


@pytest.mark.parametrize("cfg", sorted(BOOK_HASHES))
def test_paper_book_config_hash_unchanged(cfg):
    assert RunConfig.from_yaml(REPO / cfg).config_hash().startswith(BOOK_HASHES[cfg])


@pytest.mark.parametrize("cfg", sorted(STATE_FILES))
def test_paper_book_config_hash_matches_stored_state(cfg):
    state = REPO / STATE_FILES[cfg]
    if not state.exists():  # book state is local operator data (gitignored)
        pytest.skip(f"no local book state at {state}")
    stored = json.loads(state.read_text())["config_hash"]
    assert RunConfig.from_yaml(REPO / cfg).config_hash() == stored


def test_research_config_is_not_a_run_config():
    # qe.ai config lives in its own file/schema; the engine loader must reject it.
    with pytest.raises(ValueError):
        RunConfig.from_yaml(REPO / "configs" / "qe_ai_research.yaml")
