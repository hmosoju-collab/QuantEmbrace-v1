"""ResearchManifest — reproducibility record for every alpha research artifact.

A report without a manifest is invalid (ADR-031 #13/#18): re-running 2026 research
in 2029 must reproduce the same numbers. Captures the git commit, the dataset
snapshot identity (id/hash/created_at + universe-definition / corporate-action /
bhavcopy snapshots), the resolved config hash, model versions, and generation time.
The dataset-snapshot fields align with the backtest lab's planned
``qe-bt-datasets`` registry ([PLANNED] integration).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ADR_VERSION = "ADR-031"


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_obj(obj: Any) -> str:
    return sha256_bytes(json.dumps(obj, sort_keys=True, default=str).encode())


@dataclass(frozen=True)
class DatasetSnapshot:
    dataset_id: str = "unspecified"
    dataset_hash: str = ""
    dataset_created_at: str = ""
    universe_definition_version: str = "ADR-019"
    corporate_action_snapshot: str = "none"
    bhavcopy_snapshot: str = "none"


@dataclass(frozen=True)
class ResearchManifest:
    generated_at: str
    git_commit: str
    adr_version: str
    config_hash: str
    model_versions: list[str]
    horizons: list[int]
    dataset: DatasetSnapshot = field(default_factory=DatasetSnapshot)
    inputs: dict[str, str] = field(default_factory=dict)  # name -> sha256

    @classmethod
    def build(
        cls,
        *,
        config: dict | None = None,
        model_versions: list[str] | None = None,
        horizons: list[int] | None = None,
        dataset: DatasetSnapshot | None = None,
        inputs: dict[str, bytes] | None = None,
    ) -> ResearchManifest:
        return cls(
            generated_at=datetime.now(timezone.utc).isoformat(),
            git_commit=_git_commit(),
            adr_version=ADR_VERSION,
            config_hash=sha256_obj(config or {}),
            model_versions=list(model_versions or []),
            horizons=list(horizons or []),
            dataset=dataset or DatasetSnapshot(),
            inputs={name: sha256_bytes(data) for name, data in (inputs or {}).items()},
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return path

    def summary_md(self) -> str:
        return (
            f"- git_commit: `{self.git_commit}`\n"
            f"- adr_version: {self.adr_version}\n"
            f"- dataset_id: {self.dataset.dataset_id} "
            f"(hash `{self.dataset.dataset_hash[:12] or 'n/a'}`, "
            f"corporate_action `{self.dataset.corporate_action_snapshot}`)\n"
            f"- universe_definition_version: {self.dataset.universe_definition_version}\n"
            f"- config_hash: `{self.config_hash[:12]}`\n"
            f"- model_versions: {', '.join(self.model_versions) or 'n/a'}\n"
            f"- generated_at: {self.generated_at}\n"
        )
