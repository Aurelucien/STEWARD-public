"""Explicit, manifest-bound atomic restoration of Snapshot derived-index tables."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .database import SCHEMA_VERSION, connect, database_path, validate_schema
from .errors import StewardError
from .evidence import canonical_json
from .faults import FaultInjectionError, FaultInjector, checkpoint as fault_checkpoint
from .models import (
    SnapshotBackupManifest,
    SnapshotBackupReport,
    SnapshotBackupStatus,
    SnapshotRollbackReport,
    SnapshotRollbackStatus,
    StewardConfig,
)
from .snapshot_backup import (
    _file_digest,
    _fsync_directory,
    _fsync_file,
    _remove_sqlite_sidecars,
    _validate_database,
    _write_backup,
)


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


def _same_database(left: Path, right: Path) -> bool:
    try:
        if left.resolve(strict=False) == right.resolve(strict=False):
            return True
        return left.exists() and right.exists() and os.path.samestat(left.stat(), right.stat())
    except OSError:
        return True


def _run_index_digest(conn: sqlite3.Connection) -> str:
    projection = {
        table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        for table, order in (("runs", "run_id"), ("evidence_records", "run_id, sequence"))
    }
    return hashlib.sha256(canonical_json(projection)).hexdigest()


def _manifest_path(backup: Path) -> Path:
    return Path(f"{backup}.manifest.json")


def _load_manifest(path: Path) -> SnapshotBackupManifest:
    if path.is_symlink() or not path.is_file():
        raise ValueError("backup manifest is unavailable")
    document = json.loads(path.read_text(encoding="utf-8"))
    fields = {
        "source_database_digest",
        "source_schema_version",
        "source_snapshot_digest",
        "source_entry_digest",
    }
    optional = "source_snapshot_evidence_schema_versions"
    if not isinstance(document, dict) or (
        set(document) != fields and set(document) != fields | {optional}
    ):
        raise ValueError("backup manifest schema is invalid")
    manifest = SnapshotBackupManifest(
        document["source_database_digest"],
        document["source_schema_version"],
        document["source_snapshot_digest"],
        document["source_entry_digest"],
        tuple(document.get(optional, ())),
    )
    if canonical_json(document) != path.read_bytes():
        raise ValueError("backup manifest is not canonical")
    return manifest


def _failed(
    backup: Path,
    official: Path,
    code: str,
    message: str,
    *,
    validation_result: tuple[dict[str, str], ...] = (),
    snapshot_count: int = 0,
    entry_count: int = 0,
) -> SnapshotRollbackReport:
    return SnapshotRollbackReport(
        SnapshotRollbackStatus.FAILED,
        str(backup),
        str(official),
        "",
        "",
        snapshot_count,
        entry_count,
        validation_result,
        (_issue(code, message, path=backup),),
        (),
    )


def _validate_backup(
    backup: Path, report: SnapshotBackupReport
) -> tuple[tuple[dict[str, str], ...], SnapshotBackupManifest, tuple[int, int, str, str, str]]:
    if report.status != SnapshotBackupStatus.READY or report.manifest is None:
        raise ValueError("backup report is not ready")
    manifest = _load_manifest(_manifest_path(backup))
    if manifest != report.manifest:
        raise ValueError("backup manifest differs from supplied backup report")
    counts = _validate_database(backup, checkpoint=True)
    snapshot_count, entry_count, digest, snapshot_digest, entry_digest = counts
    if (
        manifest.source_schema_version != SCHEMA_VERSION
        or report.schema_version != SCHEMA_VERSION
        or manifest.source_snapshot_digest != snapshot_digest
        or manifest.source_entry_digest != entry_digest
        or report.source_digest != digest
        or report.backup_digest != digest
        or report.snapshot_count != snapshot_count
        or report.entry_count != entry_count
    ):
        raise ValueError("backup content does not match its manifest or backup report")
    validations = (
        {"code": "BACKUP_SCHEMA_VALID", "message": str(SCHEMA_VERSION)},
        {"code": "BACKUP_INTEGRITY_VALID", "message": "ok"},
        {"code": "BACKUP_FOREIGN_KEYS_VALID", "message": "ok"},
        {"code": "BACKUP_MANIFEST_VALID", "message": manifest.source_snapshot_digest},
        {"code": "BACKUP_SNAPSHOT_COUNT", "message": str(snapshot_count)},
        {"code": "BACKUP_ENTRY_COUNT", "message": str(entry_count)},
        {"code": "BACKUP_DIGEST_VALID", "message": digest},
    )
    return validations, manifest, counts


def _copy_table(
    source: sqlite3.Connection, destination: sqlite3.Connection, table: str, order: str
) -> None:
    rows = list(source.execute(f"SELECT * FROM {table} ORDER BY {order}"))
    if not rows:
        return
    columns = tuple(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    destination.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [tuple(row[column] for column in columns) for row in rows],
    )


def _build_restore_candidate(official: Path, backup: Path, candidate: Path) -> tuple[str, str]:
    """Clone current Run/Evidence index, then restore only backed-up Snapshot derived tables."""
    source = connect(official)
    try:
        validate_schema(source)
        checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise sqlite3.OperationalError("official SQLite WAL checkpoint did not complete")
        run_digest = _run_index_digest(source)
        _write_backup(source, candidate)
    finally:
        source.close()
    source_digest = _file_digest(official)
    backup_conn = sqlite3.connect(backup)
    backup_conn.row_factory = sqlite3.Row
    candidate_conn = connect(candidate)
    try:
        candidate_conn.execute("BEGIN IMMEDIATE")
        candidate_conn.execute("DELETE FROM snapshot_diff_entries")
        candidate_conn.execute("DELETE FROM snapshot_diffs")
        candidate_conn.execute("DELETE FROM snapshot_entries")
        candidate_conn.execute("DELETE FROM snapshots")
        _copy_table(backup_conn, candidate_conn, "snapshots", "snapshot_id")
        _copy_table(backup_conn, candidate_conn, "snapshot_entries", "snapshot_id, scope_id, relative_path")
        candidate_conn.commit()
    except sqlite3.Error:
        candidate_conn.rollback()
        raise
    finally:
        backup_conn.close()
        candidate_conn.close()
    return source_digest, run_digest


def restore_snapshot_index_from_backup(
    config: StewardConfig,
    backup_manifest: SnapshotBackupReport,
    official_database: Path,
    *,
    fault_injector: FaultInjector | None = None,
) -> SnapshotRollbackReport:
    """Atomically restore Snapshot/Entry derived tables from one explicit READY backup."""
    official = Path(official_database)
    backup = Path(backup_manifest.backup_database)
    configured_official = database_path(config)
    if not _same_database(official, configured_official):
        return _failed(
            backup,
            official,
            "SNAPSHOT_ROLLBACK_OFFICIAL_MISMATCH",
            "rollback target must be this configuration's official database",
        )
    if official.is_symlink() or not official.is_file() or not os.access(official, os.R_OK):
        return _failed(
            backup,
            official,
            "SNAPSHOT_ROLLBACK_OFFICIAL_UNREADABLE",
            "official database must be a readable regular non-symlink file",
        )
    if _same_database(backup, official) or backup.is_symlink() or not backup.is_file():
        return _failed(
            backup,
            official,
            "SNAPSHOT_ROLLBACK_BACKUP_UNSAFE",
            "backup must be a distinct readable regular database file",
        )
    if Path(backup_manifest.official_database).resolve(strict=False) != official.resolve(strict=False):
        return _failed(
            backup,
            official,
            "SNAPSHOT_ROLLBACK_BACKUP_IDENTITY_INVALID",
            "backup report source does not match the requested official database",
        )
    try:
        if backup.stat().st_dev != official.parent.stat().st_dev:
            return _failed(
                backup,
                official,
                "SNAPSHOT_ROLLBACK_DESTINATION_INVALID",
                "backup and official database must be on the same filesystem",
            )
        fault_checkpoint(fault_injector, "rollback", "before_backup_validation")
        validations, manifest, backup_counts = _validate_backup(backup, backup_manifest)
    except (OSError, sqlite3.Error, StewardError, FaultInjectionError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return _failed(
            backup,
            official,
            "SNAPSHOT_ROLLBACK_BACKUP_INVALID",
            "backup did not pass manifest, schema, integrity, or digest validation",
        )
    snapshot_count, entry_count, backup_digest, snapshot_digest, entry_digest = backup_counts
    candidate = official.parent / f".{official.name}.snapshot-rollback.tmp"
    if candidate.exists() or candidate.is_symlink():
        return _failed(
            backup,
            official,
            "SNAPSHOT_ROLLBACK_CANDIDATE_EXISTS",
            "rollback candidate path already exists and will not be overwritten",
            validation_result=validations,
            snapshot_count=snapshot_count,
            entry_count=entry_count,
        )
    lock: sqlite3.Connection | None = None
    replaced = False
    source_file_digest = ""
    run_digest = ""
    try:
        fault_checkpoint(fault_injector, "rollback", "before_restore_candidate")
        source_file_digest, run_digest = _build_restore_candidate(official, backup, candidate)
        candidate_counts = _validate_database(candidate, checkpoint=True)
        if candidate_counts != backup_counts:
            raise sqlite3.IntegrityError("restore candidate does not match backup Snapshot content")
        candidate_conn = connect(candidate)
        try:
            if _run_index_digest(candidate_conn) != run_digest:
                raise sqlite3.IntegrityError("restore candidate changed Run index content")
        finally:
            candidate_conn.close()
        _remove_sqlite_sidecars(candidate)
        _fsync_file(candidate)
        _fsync_directory(candidate.parent)
        lock = connect(official)
        checkpoint = lock.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise sqlite3.OperationalError("official SQLite WAL checkpoint did not complete")
        lock.execute("BEGIN EXCLUSIVE")
        if _file_digest(official) != source_file_digest or _run_index_digest(lock) != run_digest:
            raise sqlite3.IntegrityError("official database changed while rollback candidate was built")
        fault_checkpoint(fault_injector, "rollback", "before_replace")
        os.replace(candidate, official)
        replaced = True
    except (OSError, sqlite3.Error, StewardError, FaultInjectionError, ValueError, TypeError, KeyError):
        if lock is not None:
            try:
                lock.rollback()
            except sqlite3.Error:
                pass
        if not replaced:
            try:
                if candidate.exists() or candidate.is_symlink():
                    candidate.unlink()
            except OSError:
                pass
        return _failed(
            backup,
            official,
            "SNAPSHOT_ROLLBACK_TRANSACTION_FAILED",
            "rollback did not complete before the official database changed",
            validation_result=validations,
            snapshot_count=snapshot_count,
            entry_count=entry_count,
        )
    finally:
        if lock is not None:
            lock.close()
    try:
        fault_checkpoint(fault_injector, "rollback", "after_replace_before_verify")
        _remove_sqlite_sidecars(official)
        _fsync_file(official)
        _fsync_directory(official.parent)
        restored_counts = _validate_database(official, checkpoint=True)
        post_conn = connect(official)
        try:
            post_run_digest = _run_index_digest(post_conn)
        finally:
            post_conn.close()
        if restored_counts != backup_counts or post_run_digest != run_digest:
            raise sqlite3.IntegrityError("post-restore validation differs from backup or Run index")
    except (OSError, sqlite3.Error, StewardError, FaultInjectionError, ValueError, TypeError, KeyError):
        return SnapshotRollbackReport(
            SnapshotRollbackStatus.FAILED,
            str(backup),
            str(official),
            backup_digest,
            "",
            snapshot_count,
            entry_count,
            validations,
            (
                _issue(
                    "SNAPSHOT_ROLLBACK_POST_RESTORE_VALIDATION_FAILED",
                    "atomic restore completed but post-restore validation failed",
                    path=official,
                ),
            ),
            (),
        )
    return SnapshotRollbackReport(
        SnapshotRollbackStatus.RESTORED,
        str(backup),
        str(official),
        backup_digest,
        restored_counts[2],
        snapshot_count,
        entry_count,
        validations,
        (),
        (),
    )
