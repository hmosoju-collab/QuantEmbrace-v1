"""Data snapshots: pin the exact lake bytes a run reads.

A snapshot manifest lists every file in the run's scope with its size and
SHA-256. The snapshot id is a content hash of that list, so identical scopes
yield identical ids (idempotent), and any post-hoc mutation of the lake is
detectable by re-verification. Manifests live in ``{lake_root}/_snapshots/``.
"""

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any


class SnapshotError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_dir(lake_root: Path) -> Path:
    return Path(lake_root) / "_snapshots"


def create_snapshot(
    lake_root: str | Path, files: list[Path], scope: dict[str, Any]
) -> dict[str, Any]:
    """Hash ``files`` (paths relative to lake_root), write the manifest, and
    return it. Re-creating an identical snapshot is a no-op returning the same id."""
    lake_root = Path(lake_root)
    entries = []
    for f in sorted(files):
        rel = str(Path(f).resolve().relative_to(lake_root.resolve()))
        entries.append({"path": rel, "size": f.stat().st_size, "sha256": _sha256_file(f)})
    ident = hashlib.sha256(
        "\n".join(f"{e['path']}:{e['size']}:{e['sha256']}" for e in entries).encode()
    ).hexdigest()
    snapshot_id = f"ds-{ident[:16]}"
    manifest = {
        "snapshot_id": snapshot_id,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "scope": scope,
        "n_files": len(entries),
        "files": entries,
    }
    out = _manifest_dir(lake_root) / f"{snapshot_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        out.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    return manifest


def load_manifest(lake_root: str | Path, snapshot_id: str) -> dict[str, Any]:
    path = _manifest_dir(Path(lake_root)) / f"{snapshot_id}.json"
    if not path.exists():
        raise SnapshotError(f"unknown snapshot: {snapshot_id} (no {path})")
    return json.loads(path.read_text())


def verify_snapshot(lake_root: str | Path, manifest: dict[str, Any]) -> list[str]:
    """Re-hash every file in the manifest; return a list of mismatch
    descriptions (empty list == snapshot intact)."""
    lake_root = Path(lake_root)
    problems = []
    for entry in manifest["files"]:
        path = lake_root / entry["path"]
        if not path.exists():
            problems.append(f"missing: {entry['path']}")
        elif path.stat().st_size != entry["size"] or _sha256_file(path) != entry["sha256"]:
            problems.append(f"modified: {entry['path']}")
    return problems
