"""Verified SQLite derived-index backups for a future manual rollback protocol."""

import hashlib
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .database import SCHEMA_VERSION, connect, validate_schema
from .errors import StewardError
from .evidence import canonical_json
from .faults import FaultInjectionError, FaultInjector, checkpoint as fault_checkpoint
from .models import (
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    SnapshotBackupManifest,
    SnapshotBackupReport,
    SnapshotBackupStatus,
)
from .paths import contains
from .snapshot_replay import _destination_digest, _entry_projection, _snapshot_projection
from .snapshots import _entry_from_row, _snapshot_from_row


def _issue(code: str, message: str, *, path: Path | None = None) -> dict[str, str]:
    return {
        "code": code,
        "message": message,
        "snapshot_id": "",
        "evidence_id": "",
        "persistent_run_id": "",
        "path": str(path) if path is not None else "",
        "expected": "",
        "actual": "",
    }


def _file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _content_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _same_database(left: Path, right: Path) -> bool:
    try:
        if left.resolve(strict=False) == right.resolve(strict=False):
            return True
        return left.exists() and right.exists() and os.path.samestat(left.stat(), right.stat())
    except OSError:
        return True


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            sidecar.unlink()


def _temporary_paths(destination: Path) -> tuple[Path, Path, Path]:
    temporary_database = destination.parent / f".{destination.name}.snapshot-backup.tmp"
    manifest = Path(f"{destination}.manifest.json")
    temporary_manifest = destination.parent / f".{manifest.name}.snapshot-backup.tmp"
    return temporary_database, manifest, temporary_manifest


def _index_identity(
    conn: sqlite3.Connection,
) -> tuple[int, int, str, str, str]:
    entries_by_snapshot: dict[str, list[FilesystemEntry | FilesystemEntryV2]] = {}
    entries: list[FilesystemEntry | FilesystemEntryV2] = []
    for row in conn.execute(
        "SELECT * FROM snapshot_entries ORDER BY snapshot_id, scope_id, relative_path"
    ):
        entry = _entry_from_row(row)
        entries_by_snapshot.setdefault(row["snapshot_id"], []).append(entry)
        entries.append(entry)
    snapshots: list[FilesystemSnapshot | FilesystemSnapshotV2] = []
    for row in conn.execute("SELECT * FROM snapshots ORDER BY snapshot_id, evidence_id"):
        snapshots.append(_snapshot_from_row(row, tuple(entries_by_snapshot.get(row["snapshot_id"], []))))
    snapshot_digest = _content_digest([_snapshot_projection(item) for item in snapshots])
    entry_digest = _content_digest([_entry_projection(item) for item in entries])
    return len(snapshots), len(entries), _destination_digest(conn), snapshot_digest, entry_digest


def _validate_database(
    path: Path, *, checkpoint: bool
) -> tuple[int, int, str, str, str]:
    conn = connect(path)
    try:
        validate_schema(conn)
        if checkpoint:
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result is None or result[0] != 0:
                raise sqlite3.OperationalError("SQLite WAL checkpoint did not complete")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise sqlite3.IntegrityError("SQLite integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("SQLite foreign key check failed")
        return _index_identity(conn)
    finally:
        conn.close()


def _write_backup(source: sqlite3.Connection, temporary_database: Path) -> None:
    destination = sqlite3.connect(temporary_database)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()


def _failed(
    source: Path,
    destination: Path,
    code: str,
    message: str,
    *,
    snapshot_count: int = 0,
    entry_count: int = 0,
) -> SnapshotBackupReport:
    return SnapshotBackupReport(
        SnapshotBackupStatus.FAILED,
        str(source),
        str(destination),
        SCHEMA_VERSION,
        "",
        "",
        snapshot_count,
        entry_count,
        "unverified",
        None,
        (_issue(code, message, path=destination),),
        (),
    )


def create_snapshot_index_backup(
    official_database: Path,
    backup_destination: Path,
    *,
    fault_injector: FaultInjector | None = None,
) -> SnapshotBackupReport:
    """Create and verify one isolated derived-index backup; it never performs rollback."""
    source = Path(official_database)
    destination = Path(backup_destination)
    if source.is_symlink() or not source.is_file() or not os.access(source, os.R_OK):
        return _failed(
            source,
            destination,
            "SNAPSHOT_BACKUP_OFFICIAL_UNREADABLE",
            "official database must be a readable regular non-symlink SQLite file",
        )
    if _same_database(source, destination):
        return _failed(
            source,
            destination,
            "SNAPSHOT_BACKUP_DESTINATION_IS_OFFICIAL",
            "backup destination must not be the official database or an alias of it",
        )
    if destination.exists() or destination.is_symlink():
        return _failed(
            source,
            destination,
            "SNAPSHOT_BACKUP_DESTINATION_NOT_EMPTY",
            "backup destination already exists and will not be overwritten",
        )
    parent = destination.parent
    try:
        source_parent = source.parent.resolve(strict=True)
        safe_parent = parent.resolve(strict=True)
        if parent.is_symlink() or not safe_parent.is_dir() or not contains(source_parent, safe_parent):
            return _failed(
                source,
                destination,
                "SNAPSHOT_BACKUP_DESTINATION_INVALID",
                "backup destination must be within the official database directory tree",
            )
        if source.stat().st_dev != safe_parent.stat().st_dev:
            return _failed(
                source,
                destination,
                "SNAPSHOT_BACKUP_DESTINATION_INVALID",
                "backup destination must be on the official database filesystem",
            )
    except OSError:
        return _failed(
            source,
            destination,
            "SNAPSHOT_BACKUP_DESTINATION_INVALID",
            "backup destination parent cannot be safely inspected",
        )
    temporary_database, manifest_path, temporary_manifest = _temporary_paths(destination)
    if any(path.exists() or path.is_symlink() for path in (temporary_database, manifest_path, temporary_manifest)):
        return _failed(
            source,
            destination,
            "SNAPSHOT_BACKUP_DESTINATION_NOT_EMPTY",
            "backup destination or its manifest already exists",
        )
    source_conn: sqlite3.Connection | None = None
    try:
        source_conn = connect(source)
        validate_schema(source_conn)
        checkpoint = source_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise sqlite3.OperationalError("official SQLite WAL checkpoint did not complete")
        integrity = source_conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise sqlite3.IntegrityError("official SQLite integrity check failed")
        if source_conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("official SQLite foreign key check failed")
        source_count, entry_count, source_digest, snapshot_digest, entry_digest = _index_identity(
            source_conn
        )
        source_file_digest = _file_digest(source)
        fault_checkpoint(fault_injector, "backup", "before_backup_create")
        fault_checkpoint(fault_injector, "backup", "during_backup_copy")
        _write_backup(source_conn, temporary_database)
        _fsync_file(temporary_database)
        backup_count, backup_entries, backup_digest, backup_snapshot, backup_entry = _validate_database(
            temporary_database, checkpoint=True
        )
        _remove_sqlite_sidecars(temporary_database)
        _fsync_file(temporary_database)
        if (
            (source_count, entry_count, source_digest, snapshot_digest, entry_digest)
            != (backup_count, backup_entries, backup_digest, backup_snapshot, backup_entry)
        ):
            raise sqlite3.IntegrityError("backup business content differs from official database")
        versions = tuple(
            row[0]
            for row in source_conn.execute(
                "SELECT DISTINCT snapshot_evidence_schema_version FROM snapshots ORDER BY snapshot_evidence_schema_version"
            )
        )
        manifest = SnapshotBackupManifest(
            source_file_digest, SCHEMA_VERSION, snapshot_digest, entry_digest, versions
        )
        temporary_manifest.write_bytes(canonical_json(asdict(manifest)))
        _fsync_file(temporary_manifest)
        fault_checkpoint(fault_injector, "backup", "after_backup_before_publish")
        fault_checkpoint(fault_injector, "backup", "before_manifest_publish")
        os.replace(temporary_database, destination)
        os.replace(temporary_manifest, manifest_path)
        _fsync_directory(safe_parent)
    except (OSError, sqlite3.Error, StewardError, FaultInjectionError, ValueError, TypeError, KeyError):
        for path in (temporary_database, temporary_manifest, destination, manifest_path):
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
            except OSError:
                pass
        return _failed(
            source,
            destination,
            "SNAPSHOT_BACKUP_FAILED",
            "backup could not be created and verified without affecting the official database",
        )
    finally:
        if source_conn is not None:
            source_conn.close()
    return SnapshotBackupReport(
        SnapshotBackupStatus.READY,
        str(source),
        str(destination),
        SCHEMA_VERSION,
        source_digest,
        backup_digest,
        source_count,
        entry_count,
        "ok",
        manifest,
        (),
        (),
    )
