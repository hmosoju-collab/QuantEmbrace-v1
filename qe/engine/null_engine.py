"""The M1 "null engine": proves the config→snapshot→data→journal spine.

It runs no strategies and makes no decisions. It loads the configured universe
over the configured date range from the lake (snapshot-pinned), and journals
one BARS event per IST trading day plus SESSION_START/SESSION_END. Acceptance
for M1 (RA-1 Phase 4): the journal carries config hash, code SHA, and data
snapshot id, and the run is reproducible from those three identifiers.
"""

from dataclasses import dataclass
from pathlib import Path
import time

from qe.config import RunConfig
from qe.data.lake import IST, LakeError, OhlcvLake
from qe.data.snapshot import SnapshotError, create_snapshot, load_manifest, verify_snapshot
from qe.journal import JournalWriter
from qe.version import code_version


@dataclass(frozen=True)
class NullRunResult:
    session_id: str
    journal_path: Path
    config_hash: str
    code_sha: str
    data_snapshot_id: str
    n_bars: int
    n_days: int
    n_symbols: int
    status: str


def run_null(config: RunConfig, *, base_dir: str | Path = ".") -> NullRunResult:
    """Execute a null run. ``base_dir`` anchors relative lake/journal paths."""
    base_dir = Path(base_dir)
    lake = OhlcvLake(base_dir / config.data.lake_root)
    u = config.universe

    files = lake.resolve_files(
        u.symbols,
        config.start_date,
        config.end_date,
        market=u.market,
        segment=u.segment,
        interval=config.data.interval,
    )

    # Snapshot: pin (and verify) or create. Fail-closed on any mismatch —
    # a run against silently-changed data is exactly the bug class v2 exists to kill.
    if config.data.snapshot_id:
        manifest = load_manifest(lake.root, config.data.snapshot_id)
        problems = verify_snapshot(lake.root, manifest)
        if problems:
            raise SnapshotError(
                f"snapshot {config.data.snapshot_id} verification failed: {problems[:5]}"
            )
    else:
        scope = {
            "market": u.market,
            "segment": u.segment,
            "symbols": list(u.symbols),
            "interval": config.data.interval,
            "start_date": str(config.start_date),
            "end_date": str(config.end_date),
        }
        manifest = create_snapshot(lake.root, files, scope)
    snapshot_id = manifest["snapshot_id"]

    config_hash = config.config_hash()
    code_sha = code_version(base_dir)
    session_id = (
        f"{config.name}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{config_hash[:12]}"
    )
    journal_path = base_dir / config.journal_dir / f"{session_id}.jsonl"

    with JournalWriter(journal_path) as journal:
        journal.session_start(
            session_id=session_id,
            mode=config.mode,
            config_hash=config_hash,
            config=config.model_dump(mode="json"),
            code_sha=code_sha,
            data_snapshot_id=snapshot_id,
        )

        bars = lake.load_bars(
            u.symbols,
            config.start_date,
            config.end_date,
            market=u.market,
            segment=u.segment,
            interval=config.data.interval,
            files=files,
        )
        if bars.empty:
            raise LakeError(f"0 bars for {u.symbols} in {config.start_date}..{config.end_date}")

        by_day = bars.groupby(bars["timestamp"].dt.tz_convert(IST).dt.date, sort=True)
        n_days = 0
        for day, day_bars in by_day:
            journal.write(
                "BARS",
                {
                    "date": str(day),
                    "n_bars": len(day_bars),
                    "n_symbols": int(day_bars["symbol"].nunique()),
                },
            )
            n_days += 1

        summary = {
            "n_bars": len(bars),
            "n_days": n_days,
            "n_symbols": int(bars["symbol"].nunique()),
            "first_day": str(bars["timestamp"].dt.tz_convert(IST).dt.date.min()),
            "last_day": str(bars["timestamp"].dt.tz_convert(IST).dt.date.max()),
        }
        journal.session_end("OK", summary)

    return NullRunResult(
        session_id=session_id,
        journal_path=journal_path,
        config_hash=config_hash,
        code_sha=code_sha,
        data_snapshot_id=snapshot_id,
        n_bars=summary["n_bars"],
        n_days=summary["n_days"],
        n_symbols=summary["n_symbols"],
        status="OK",
    )
