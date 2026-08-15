"""LOCAL-0003-R1C2B2B manifest-bound Snapshot derived-index rollback checks."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.database import database_path
from local_steward.models import SnapshotBackupStatus, SnapshotRollbackStatus
from local_steward.snapshot_backup import create_snapshot_index_backup
from local_steward.snapshot_rollback import restore_snapshot_index_from_backup
from local_steward.snapshot_replay import _destination_digest
from local_steward.snapshots import get_snapshot, inspect_snapshot_inventory
from local_steward.storage import storage_status, verify_evidence_report

from .test_snapshot_queries import snapshot_fixture


def _backup_fixture(tmp_path: Path):
    config, snapshot = snapshot_fixture(tmp_path)
    official = database_path(config)
    backup = config.paths.cache_dir / "rollback-source.sqlite3"
    report = create_snapshot_index_backup(official, backup)
    assert report.status == SnapshotBackupStatus.READY
    return config, snapshot, official, backup, report


def _contents(path: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    connection = sqlite3.connect(path)
    try:
        return (
            list(connection.execute("SELECT * FROM snapshots ORDER BY snapshot_id")),
            list(
                connection.execute(
                    "SELECT * FROM snapshot_entries ORDER BY snapshot_id, scope_id, relative_path"
                )
            ),
        )
    finally:
        connection.close()


def _evidence_bytes(config) -> dict[Path, bytes]:
    return {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    }


def _corrupt_snapshot_index(official: Path, snapshot_id: str) -> None:
    connection = sqlite3.connect(official)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM snapshot_entries WHERE snapshot_id=?", (snapshot_id,))
        connection.execute("DELETE FROM snapshots WHERE snapshot_id=?", (snapshot_id,))
        connection.commit()
    finally:
        connection.close()


def test_ready_backup_restores_snapshot_index_without_changing_evidence_or_runs(tmp_path: Path) -> None:
    config, snapshot, official, backup, report = _backup_fixture(tmp_path)
    backup_contents = _contents(backup)
    evidence_before = _evidence_bytes(config)
    connection = sqlite3.connect(official)
    try:
        run_before = connection.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
    finally:
        connection.close()
    _corrupt_snapshot_index(official, snapshot.snapshot_id)
    result = restore_snapshot_index_from_backup(config, report, official)
    assert result.status == SnapshotRollbackStatus.RESTORED
    assert result.backup_digest == result.restored_digest == report.backup_digest
    assert result.snapshot_count == 1 and result.entry_count == snapshot.entry_count
    assert _contents(official) == backup_contents
    assert get_snapshot(config, snapshot.snapshot_id).snapshot_digest == snapshot.snapshot_digest
    inventory = inspect_snapshot_inventory(config)
    assert inventory.indexed_snapshots == inventory.indexed_entry_groups == 1
    assert storage_status(config).storage_status == "HEALTHY"
    assert verify_evidence_report(config).snapshot_evidence.valid_count == 1
    assert _evidence_bytes(config) == evidence_before
    connection = sqlite3.connect(official)
    try:
        assert connection.execute("SELECT * FROM runs ORDER BY run_id").fetchall() == run_before
        connection.row_factory = sqlite3.Row
        assert _destination_digest(connection) == report.backup_digest
    finally:
        connection.close()


def test_rollback_rejects_missing_manifest_missing_backup_and_wrong_source_identity(
    tmp_path: Path,
) -> None:
    config, snapshot, official, backup, report = _backup_fixture(tmp_path)
    _corrupt_snapshot_index(official, snapshot.snapshot_id)
    before = official.read_bytes()
    Path(f"{backup}.manifest.json").unlink()
    assert restore_snapshot_index_from_backup(config, report, official).status == SnapshotRollbackStatus.FAILED
    assert official.read_bytes() == before

    missing = replace(report, backup_database=str(config.paths.cache_dir / "missing.sqlite3"))
    assert restore_snapshot_index_from_backup(config, missing, official).status == SnapshotRollbackStatus.FAILED
    wrong_source = replace(report, official_database=str(config.paths.cache_dir / "different.sqlite3"))
    assert restore_snapshot_index_from_backup(config, wrong_source, official).status == SnapshotRollbackStatus.FAILED


def test_rollback_rejects_manifest_and_backup_content_mismatches_without_official_change(
    tmp_path: Path,
) -> None:
    config, snapshot, official, backup, report = _backup_fixture(tmp_path)
    _corrupt_snapshot_index(official, snapshot.snapshot_id)
    before = official.read_bytes()
    manifest_path = Path(f"{backup}.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_entry_digest"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = restore_snapshot_index_from_backup(config, report, official)
    assert result.status == SnapshotRollbackStatus.FAILED
    assert official.read_bytes() == before

    config, snapshot, official, backup, report = _backup_fixture(tmp_path / "digest")
    _corrupt_snapshot_index(official, snapshot.snapshot_id)
    before = official.read_bytes()
    connection = sqlite3.connect(backup)
    try:
        connection.execute("UPDATE snapshots SET config_digest='tampered'")
        connection.commit()
    finally:
        connection.close()
    assert restore_snapshot_index_from_backup(config, report, official).status == SnapshotRollbackStatus.FAILED
    assert official.read_bytes() == before


def test_rollback_rejects_schema_corruption_and_unsafe_backup_paths(tmp_path: Path) -> None:
    config, snapshot, official, backup, report = _backup_fixture(tmp_path)
    _corrupt_snapshot_index(official, snapshot.snapshot_id)
    before = official.read_bytes()
    connection = sqlite3.connect(backup)
    try:
        connection.execute("UPDATE schema_metadata SET schema_version=999")
        connection.commit()
    finally:
        connection.close()
    assert restore_snapshot_index_from_backup(config, report, official).status == SnapshotRollbackStatus.FAILED
    assert official.read_bytes() == before

    direct = replace(report, backup_database=str(official))
    assert restore_snapshot_index_from_backup(config, direct, official).status == SnapshotRollbackStatus.FAILED
    alias = config.paths.cache_dir / "official-alias.sqlite3"
    alias.symlink_to(official)
    linked = replace(report, backup_database=str(alias))
    assert restore_snapshot_index_from_backup(config, linked, official).status == SnapshotRollbackStatus.FAILED
    hardlink = config.paths.cache_dir / "official-hardlink.sqlite3"
    hardlink.hardlink_to(official)
    inode_alias = replace(report, backup_database=str(hardlink))
    assert restore_snapshot_index_from_backup(config, inode_alias, official).status == SnapshotRollbackStatus.FAILED


def test_candidate_and_replace_failures_leave_official_and_facts_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, snapshot, official, _backup, report = _backup_fixture(tmp_path)
    _corrupt_snapshot_index(official, snapshot.snapshot_id)
    before = official.read_bytes()
    evidence_before = _evidence_bytes(config)
    monkeypatch.setattr(
        "local_steward.snapshot_rollback._build_restore_candidate",
        lambda *_args: (_ for _ in ()).throw(OSError("injected candidate failure")),
    )
    assert restore_snapshot_index_from_backup(config, report, official).status == SnapshotRollbackStatus.FAILED
    assert official.read_bytes() == before and _evidence_bytes(config) == evidence_before

    monkeypatch.undo()
    monkeypatch.setattr(
        "local_steward.snapshot_rollback.os.replace",
        lambda *_args: (_ for _ in ()).throw(OSError("injected replace failure")),
    )
    assert restore_snapshot_index_from_backup(config, report, official).status == SnapshotRollbackStatus.FAILED
    assert official.read_bytes() == before and _evidence_bytes(config) == evidence_before
