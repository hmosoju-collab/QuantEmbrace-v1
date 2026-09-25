"""Strategy lifecycle — an append-only, evidence-gated, human-approved ledger.

States (docs/research/strategy-discovery.md §3):

    CANDIDATE → RESEARCH → BACKTEST → VALIDATION → PAPER → PRODUCTION_ELIGIBLE
         └──────────┴──────────┴───────────┴─────────┴───────────┴──→ GRAVEYARD

Every transition names a HUMAN approver and carries the evidence its target
state requires; evidence that points at the experiment ledger is verified
against ``governance/experiment-registry.jsonl``. There are no automatic
transitions. qe.ai cannot import this module (tests/qe/ai/test_ai_boundary.py),
so AI has no code path to promote anything; PRODUCTION_ELIGIBLE is still not
live — live requires the separate ``qe live`` evidence ceremony (qe/live_gate.py).
"""

from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any

from qe.config import RunConfig
from qe.research.registry import read_registry
from qe.version import code_version

LEDGER_RELPATH = Path("governance") / "strategy-lifecycle.jsonl"
STATES = (
    "CANDIDATE",
    "RESEARCH",
    "BACKTEST",
    "VALIDATION",
    "PAPER",
    "PRODUCTION_ELIGIBLE",
    "GRAVEYARD",
)
FORWARD = {
    None: "CANDIDATE",
    "CANDIDATE": "RESEARCH",
    "RESEARCH": "BACKTEST",
    "BACKTEST": "VALIDATION",
    "VALIDATION": "PAPER",
    "PAPER": "PRODUCTION_ELIGIBLE",
}
# An approver must be a person. These are refused so an automated caller can
# never sign its own promotion (governance: "AI can never promote").
_NON_HUMAN = re.compile(r"(?i)\b(ai|qe[._-]?ai|claude|llm|gpt|agent|bot|auto(mated)?|system)\b")


class LifecycleError(ValueError):
    pass


def ledger_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / LEDGER_RELPATH


def read_ledger(base_dir: str | Path) -> list[dict[str, Any]]:
    path = ledger_path(base_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def current_state(base_dir: str | Path, strategy_id: str) -> str | None:
    state = None
    for rec in read_ledger(base_dir):
        if rec["strategy_id"] == strategy_id:
            state = rec["to_state"]
    return state


def status(base_dir: str | Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for rec in read_ledger(base_dir):
        out[rec["strategy_id"]] = rec["to_state"]
    return out


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise LifecycleError(msg)


def _experiment_runs(base_dir: str | Path, experiment_id: str) -> list[dict[str, Any]]:
    return [r for r in read_registry(base_dir) if r["experiment_id"] == experiment_id]


def transition(
    base_dir: str | Path,
    *,
    strategy_id: str,
    to_state: str,
    approved_by: str,
    family: str | None = None,
    hypothesis_ref: str | None = None,
    experiment_id: str | None = None,
    paper_config: str | None = None,
    gate_evidence: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Validate and append one transition; returns the record. Raises
    LifecycleError (nothing written) when any requirement is unmet."""
    base_dir = Path(base_dir)
    _require(to_state in STATES, f"unknown state {to_state!r}")
    _require(bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,63}", strategy_id)), "bad strategy_id")
    _require(bool(approved_by.strip()), "approved_by is required (a named human)")
    _require(
        not _NON_HUMAN.search(approved_by),
        f"approved_by {approved_by!r} is not a human approver — automation cannot promote",
    )
    history = [r for r in read_ledger(base_dir) if r["strategy_id"] == strategy_id]
    frm = history[-1]["to_state"] if history else None
    _require(frm != "GRAVEYARD", f"{strategy_id} is in GRAVEYARD (terminal)")

    evidence: dict[str, Any] = {}
    versions: dict[str, Any] = {"strategy_version": code_version(base_dir)}
    if to_state == "GRAVEYARD":
        _require(frm is not None, "cannot retire an unregistered strategy")
        _require(bool(reason and reason.strip()), "GRAVEYARD requires a cause-of-death reason")
        evidence["reason"] = reason
    else:
        _require(
            FORWARD.get(frm) == to_state,
            f"illegal transition {frm} → {to_state} (next allowed: {FORWARD.get(frm)} or GRAVEYARD)",
        )
    if to_state == "CANDIDATE":
        _require(bool(family and hypothesis_ref), "CANDIDATE requires family and hypothesis_ref")
        evidence.update({"family": family, "hypothesis_ref": hypothesis_ref})
        if hypothesis_ref and hypothesis_ref.startswith("qe.ai:"):
            versions["research_signal_version"] = "research_signal/1"
    elif to_state == "RESEARCH":
        evidence["accepted_hypothesis"] = history[0]["evidence"].get("hypothesis_ref")
    elif to_state == "BACKTEST":
        _require(bool(experiment_id), "BACKTEST requires the pre-registered experiment_id")
        runs = _experiment_runs(base_dir, experiment_id)
        _require(bool(runs), f"experiment {experiment_id} has no registered study run")
        evidence["experiment_id"] = experiment_id
        versions.update(
            {
                "backtest_version": runs[-1]["run"].get("config_hash"),
                "dataset_version": runs[-1]["run"].get("data_snapshot_id"),
            }
        )
    elif to_state == "VALIDATION":
        exp = next(
            r["evidence"]["experiment_id"]
            for r in reversed(history)
            if "experiment_id" in r["evidence"]
        )
        runs = _experiment_runs(base_dir, exp)
        _require(
            bool(runs) and runs[-1]["run"].get("engine_pass") is True,
            f"experiment {exp}: latest registered run did not pass its pre-registered gates",
        )
        evidence.update({"experiment_id": exp, "session_id": runs[-1]["run"].get("session_id")})
        versions.update(
            {
                "backtest_version": runs[-1]["run"].get("config_hash"),
                "dataset_version": runs[-1]["run"].get("data_snapshot_id"),
            }
        )
    elif to_state == "PAPER":
        _require(bool(paper_config), "PAPER requires the paper book config path")
        cfg = RunConfig.from_yaml(base_dir / paper_config)
        _require(cfg.mode == "paper", f"{paper_config} is mode={cfg.mode}, not paper")
        evidence.update({"paper_config": paper_config, "config_hash": cfg.config_hash()})
    elif to_state == "PRODUCTION_ELIGIBLE":
        _require(bool(gate_evidence), "PRODUCTION_ELIGIBLE requires the forward-gate pass artifact")
        path = base_dir / gate_evidence
        _require(path.exists(), f"gate evidence {gate_evidence} does not exist")
        paper = next(r["evidence"] for r in reversed(history) if r["to_state"] == "PAPER")
        art = json.loads(path.read_text())
        _require(
            art.get("config_hash") == paper["config_hash"],
            "gate evidence is not bound to the paper config this strategy was promoted with",
        )
        evidence.update({"gate_evidence": gate_evidence, "config_hash": paper["config_hash"]})

    record = {
        "strategy_id": strategy_id,
        "from_state": frm,
        "to_state": to_state,
        "approved_by": approved_by.strip(),
        "recorded_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "evidence": evidence,
        "versions": versions,
    }
    path = ledger_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    return record
