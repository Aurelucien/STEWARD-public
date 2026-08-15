"""LOCAL-0003-R1C2B2A verified derived-index backup checks."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from local_steward.database import database_path
from local_steward.models import SnapshotBackupStatus
from local_steward.snapshot_backup import create_snapshot_index_backup

from .test_snapshot_queries import snapshot_fixture


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


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_backup_rejects_missing_or_non_sqlite_official_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    assert create_snapshot_index_backup(missing, destination).status == SnapshotBackupStatus.FAILED
    source = tmp_path / "not-sqlite.sqlite3"
    source.write_text("not sqlite", encoding="utf-8")
    assert create_snapshot_index_backup(source, destination).status == SnapshotBackupStatus.FAILED
    assert not destination.exists()


def test_backup_destination_path_protection(tmp_path: Path) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    source = database_path(config)
    occupied = config.paths.cache_dir / "occupied.sqlite3"
    occupied.write_text("do not overwrite", encoding="utf-8")
    assert create_snapshot_index_backup(source, occupied).issues[0]["code"] == "SNAPSHOT_BACKUP_DESTINATION_NOT_EMPTY"
    assert create_snapshot_index_backup(source, source).issues[0]["code"] == "SNAPSHOT_BACKUP_DESTINATION_IS_OFFICIAL"
    alias = config.paths.cache_dir / "source-alias.sqlite3"
    alias.symlink_to(source)
    assert create_snapshot_index_backup(source, alias).issues[0]["code"] == "SNAPSHOT_BACKUP_DESTINATION_IS_OFFICIAL"
    hardlink = config.paths.cache_dir / "source-hardlink.sqlite3"
    hardlink.hardlink_to(source)
    assert create_snapshot_index_backup(source, hardlink).issues[0]["code"] == "SNAPSHOT_BACKUP_DESTINATION_IS_OFFICIAL"
    unsafe = tmp_path.parent / "outside.sqlite3"
    assert create_snapshot_index_backup(source, unsafe).issues[0]["code"] == "SNAPSHOT_BACKUP_DESTINATION_INVALID"


def test_verified_backup_has_matching_content_manifest_and_no_source_fact_changes(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    source = database_path(config)
    destination = config.paths.cache_dir / "snapshot-backup.sqlite3"
    source_before = _digest(source)
    evidence_before = _evidence_bytes(config)
    report = create_snapshot_index_backup(source, destination)
    assert report.status == SnapshotBackupStatus.READY and report.integrity_check == "ok"
    assert report.source_digest == report.backup_digest and report.snapshot_count == 1
    assert report.entry_count == snapshot.entry_count and report.manifest is not None
    assert report.manifest.source_database_digest == source_before
    assert destination.exists() and Path(f"{destination}.manifest.json").exists()
    assert _contents(destination) == _contents(source)
    assert _digest(source) == source_before and _evidence_bytes(config) == evidence_before


def test_backup_is_business_deterministic_and_handles_wal_and_shm(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    source = database_path(config)
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "UPDATE snapshots SET config_digest=? WHERE snapshot_id=?",
            ("wal-backed-index", snapshot.snapshot_id),
        )
        connection.commit()
        assert Path(f"{source}-wal").exists() and Path(f"{source}-shm").exists()
        first = create_snapshot_index_backup(source, config.paths.cache_dir / "first.sqlite3")
    finally:
        connection.close()
    second = create_snapshot_index_backup(source, config.paths.cache_dir / "second.sqlite3")
    assert first.status == second.status == SnapshotBackupStatus.READY
    assert first.source_digest == first.backup_digest == second.source_digest == second.backup_digest
    for path in (Path(first.backup_database), Path(second.backup_database)):
        connection = sqlite3.connect(path)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()


def test_backup_failure_removes_temporary_artifacts_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    source = database_path(config)
    destination = config.paths.cache_dir / "failure.sqlite3"
    source_before = _digest(source)
    evidence_before = _evidence_bytes(config)
    monkeypatch.setattr(
        "local_steward.snapshot_backup._write_backup",
        lambda *_args: (_ for _ in ()).throw(OSError("injected write interruption")),
    )
    report = create_snapshot_index_backup(source, destination)
    assert report.status == SnapshotBackupStatus.FAILED
    assert not destination.exists() and not Path(f"{destination}.manifest.json").exists()
    assert not list(destination.parent.glob(".failure.sqlite3.snapshot-backup.tmp"))
    assert _digest(source) == source_before and _evidence_bytes(config) == evidence_before


def test_post_write_validation_failure_removes_partial_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    source = database_path(config)
    destination = config.paths.cache_dir / "post-write.sqlite3"
    source_before = _digest(source)
    monkeypatch.setattr(
        "local_steward.snapshot_backup._validate_database",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.IntegrityError("injected")),
    )
    report = create_snapshot_index_backup(source, destination)
    assert report.status == SnapshotBackupStatus.FAILED
    assert not destination.exists() and not Path(f"{destination}.manifest.json").exists()
    assert not list(destination.parent.glob(".post-write.sqlite3.snapshot-backup.tmp"))
    assert _digest(source) == source_before


def test_backup_rejects_schema_and_foreign_key_damage(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    source = database_path(config)
    connection = sqlite3.connect(source)
    try:
        connection.execute("UPDATE schema_metadata SET schema_version=999")
        connection.commit()
    finally:
        connection.close()
    assert create_snapshot_index_backup(source, config.paths.cache_dir / "schema.sqlite3").status == SnapshotBackupStatus.FAILED

    config, snapshot = snapshot_fixture(tmp_path / "foreign")
    source = database_path(config)
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        evidence_id = connection.execute(
            "SELECT evidence_id FROM snapshots WHERE snapshot_id=?", (snapshot.snapshot_id,)
        ).fetchone()[0]
        connection.execute("DELETE FROM evidence_records WHERE evidence_id=?", (evidence_id,))
        connection.commit()
    finally:
        connection.close()
    assert create_snapshot_index_backup(source, config.paths.cache_dir / "foreign.sqlite3").status == SnapshotBackupStatus.FAILED
