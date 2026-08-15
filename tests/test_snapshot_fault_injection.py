"""LOCAL-0003-R1C2B2C deterministic safety-boundary fault-injection checks."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from local_steward.database import database_path
from local_steward.faults import FaultInjectionError
from local_steward.models import (
    FaultInjectionReport,
    SnapshotBackupStatus,
    SnapshotReplacementStatus,
    SnapshotRollbackStatus,
)
from local_steward.snapshot_backup import create_snapshot_index_backup
from local_steward.snapshot_replacement import replace_snapshot_index
from local_steward.snapshot_replay import replay_snapshot_index
from local_steward.snapshot_rollback import restore_snapshot_index_from_backup

from .test_snapshot_queries import snapshot_fixture


class _FailAt:
    def __init__(self, operation: str, stage: str) -> None:
        self.operation = operation
        self.stage = stage
        self.calls: list[tuple[str, str]] = []

    def inject(self, operation: str, stage: str) -> None:
        self.calls.append((operation, stage))
        if (operation, stage) == (self.operation, self.stage):
            raise FaultInjectionError(f"{operation}:{stage}")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence_bytes(config) -> dict[Path, bytes]:
    return {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    }


def _ready_replay(tmp_path: Path):
    config, snapshot = snapshot_fixture(tmp_path)
    candidate = tmp_path / "candidate.sqlite3"
    replay = replay_snapshot_index(config, candidate)
    assert replay.replacement_ready
    return config, snapshot, candidate, replay


def _backup_for_rollback(tmp_path: Path):
    config, snapshot, _candidate, _replay = _ready_replay(tmp_path)
    official = database_path(config)
    backup = create_snapshot_index_backup(official, config.paths.cache_dir / "rollback.sqlite3")
    assert backup.status == SnapshotBackupStatus.READY
    connection = sqlite3.connect(official)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM snapshot_entries WHERE snapshot_id=?", (snapshot.snapshot_id,))
        connection.execute("DELETE FROM snapshots WHERE snapshot_id=?", (snapshot.snapshot_id,))
        connection.commit()
    finally:
        connection.close()
    return config, snapshot, official, backup


def test_fault_injection_report_is_path_free_and_immutable() -> None:
    report = FaultInjectionReport(
        "replace-before", "before_replace", "replacement", "FAILED", "a", "a", "b", "", ()
    )
    assert report.scenario == "replace-before" and report.official_before_digest == report.official_after_digest
    with pytest.raises(AttributeError):
        report.result = "REPLACED"  # type: ignore[misc]


@pytest.mark.parametrize(
    "stage",
    ("before_backup_create", "during_backup_copy", "after_backup_before_publish", "before_manifest_publish"),
)
def test_backup_faults_leave_no_artifact_or_source_change(tmp_path: Path, stage: str) -> None:
    config, _snapshot, _candidate, _replay = _ready_replay(tmp_path)
    official = database_path(config)
    destination = config.paths.cache_dir / f"backup-{stage}.sqlite3"
    before = _digest(official)
    evidence_before = _evidence_bytes(config)
    injector = _FailAt("backup", stage)
    report = create_snapshot_index_backup(official, destination, fault_injector=injector)
    assert report.status == SnapshotBackupStatus.FAILED and ("backup", stage) in injector.calls
    assert not destination.exists() and not Path(f"{destination}.manifest.json").exists()
    assert _digest(official) == before and _evidence_bytes(config) == evidence_before


@pytest.mark.parametrize("stage", ("before_validate", "after_candidate_validation", "before_replace"))
def test_pre_replace_faults_preserve_official_database(tmp_path: Path, stage: str) -> None:
    config, _snapshot, candidate, replay = _ready_replay(tmp_path)
    official = database_path(config)
    before = _digest(official)
    evidence_before = _evidence_bytes(config)
    report = replace_snapshot_index(config, replay, fault_injector=_FailAt("replacement", stage))
    assert report.status == SnapshotReplacementStatus.FAILED
    assert _digest(official) == before and _evidence_bytes(config) == evidence_before
    assert candidate.exists()
    if stage == "before_replace":
        assert list(config.paths.cache_dir.glob("state-before-snapshot-replacement-*.db"))


def test_post_replace_fault_reports_failure_with_recoverable_backup(tmp_path: Path) -> None:
    config, _snapshot, candidate, replay = _ready_replay(tmp_path)
    official = database_path(config)
    evidence_before = _evidence_bytes(config)
    report = replace_snapshot_index(
        config, replay, fault_injector=_FailAt("replacement", "after_replace_before_verify")
    )
    assert report.status == SnapshotReplacementStatus.FAILED and not candidate.exists()
    assert report.issues[0]["code"] == "SNAPSHOT_REPLACEMENT_POST_REPLACE_FAULT"
    assert list(config.paths.cache_dir.glob("state-before-snapshot-replacement-*.db"))
    assert _evidence_bytes(config) == evidence_before and sqlite3.connect(official).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    "stage",
    ("before_backup_validation", "before_restore_candidate", "before_replace"),
)
def test_pre_restore_faults_preserve_corrupted_official_for_manual_retry(
    tmp_path: Path, stage: str
) -> None:
    config, _snapshot, official, backup = _backup_for_rollback(tmp_path)
    before = _digest(official)
    evidence_before = _evidence_bytes(config)
    report = restore_snapshot_index_from_backup(
        config, backup, official, fault_injector=_FailAt("rollback", stage)
    )
    assert report.status == SnapshotRollbackStatus.FAILED
    assert _digest(official) == before and _evidence_bytes(config) == evidence_before


def test_post_restore_fault_is_explicit_and_does_not_change_evidence_or_runs(tmp_path: Path) -> None:
    config, _snapshot, official, backup = _backup_for_rollback(tmp_path)
    evidence_before = _evidence_bytes(config)
    before_runs = sqlite3.connect(official).execute("SELECT * FROM runs ORDER BY run_id").fetchall()
    report = restore_snapshot_index_from_backup(
        config, backup, official, fault_injector=_FailAt("rollback", "after_replace_before_verify")
    )
    assert report.status == SnapshotRollbackStatus.FAILED
    assert report.issues[0]["code"] == "SNAPSHOT_ROLLBACK_POST_RESTORE_VALIDATION_FAILED"
    assert _evidence_bytes(config) == evidence_before
    connection = sqlite3.connect(official)
    try:
        assert connection.execute("SELECT * FROM runs ORDER BY run_id").fetchall() == before_runs
    finally:
        connection.close()
