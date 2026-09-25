"""Experiment tracker — the multiple-testing ledger (RA-1 §2.4, NS-1 §11).

Every gated study run appends one JSONL record to
``governance/experiment-registry.jsonl`` (version-controlled — the ledger must
survive lake wipes and be reviewable in PRs). The experiment id is
deterministic from (name, hypothesis); the FAMILY count is the honest
multiple-testing denominator: the more hypotheses a family has consumed, the
less a marginal pass in that family means.
"""

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from qe.config import ExperimentConfig

REGISTRY_RELPATH = Path("governance") / "experiment-registry.jsonl"


def experiment_id(name: str, hypothesis: str) -> str:
    digest = hashlib.sha256(f"{name}\n{hypothesis}".encode()).hexdigest()
    return f"exp-{digest[:12]}"


def registry_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / REGISTRY_RELPATH


def read_registry(base_dir: str | Path) -> list[dict[str, Any]]:
    path = registry_path(base_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def family_experiment_count(base_dir: str | Path, family: str) -> int:
    """Distinct experiments (not runs) recorded in a family."""
    ids = {r["experiment_id"] for r in read_registry(base_dir) if r.get("family") == family}
    return len(ids)


def register_run(
    base_dir: str | Path, experiment: ExperimentConfig, run_record: dict[str, Any]
) -> dict[str, Any]:
    """Append one run record and return it. The family test-budget count as of
    this registration (including this experiment) is PERSISTED in the record,
    so the ledger itself shows how many hypotheses a family had consumed when
    each result was produced — reviewable in the PR, not recomputed later (F-12)."""
    exp_id = experiment_id(experiment.name, experiment.hypothesis)
    prior = {
        r["experiment_id"] for r in read_registry(base_dir) if r.get("family") == experiment.family
    }
    record = {
        "experiment_id": exp_id,
        "name": experiment.name,
        "family": experiment.family,
        "hypothesis": experiment.hypothesis,
        "registered_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "family_experiment_count": len(prior | {exp_id}),
        "run": run_record,
    }
    path = registry_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    return record
