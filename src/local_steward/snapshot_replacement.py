"""Atomic installation of a previously validated isolated Snapshot replay candidate."""

import hashlib
import os
import sqlite3
import uuid
from pathlib import Path

from .database import SCHEMA_VERSION, connect, database_path, validate_schema
from .errors import StewardError
from .faults import FaultInjectionError, FaultInjector, checkpoint as fault_checkpoint
from .models import (
    ClassifiedOperationalReplayReport,
    SnapshotReplacementReport,
    SnapshotReplacementStatus,
    SnapshotBackupStatus,
    SnapshotReplayReport,
    SnapshotReplayStatus,
    StewardConfig,
)
from .snapshot_backup import _index_identity, create_snapshot_index_backup
from .snapshot_replay import _destination_digest


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
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_resolve(path: Path) -> Path:
    return path.resolve(strict=False)


def _same_database(left: Path, right: Path) -> bool:
    try:
        if _safe_resolve(left) == _safe_resolve(right):
            return True
        if left.exists() and right.exists():
            return os.path.samestat(left.stat(), right.stat())
    except OSError:
        return True
    return False


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


def _checkpoint_candidate(path: Path) -> None:
    conn = connect(path)
    try:
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise sqlite3.OperationalError("candidate WAL checkpoint did not complete")
    finally:
        conn.close()
    _remove_sqlite_sidecars(path)
    _fsync_file(path)
    _fsync_directory(path.parent)


def _candidate_validation(
    candidate: Path, replay: SnapshotReplayReport | ClassifiedOperationalReplayReport
) -> tuple[tuple[dict[str, str], ...], tuple[dict[str, str], ...], int, int, str]:
    """Read-only candidate gate based on the R1C2A replay report and candidate facts."""
    validations: list[dict[str, str]] = []
    issues: list[dict[str, str]] = []
    if replay.status != SnapshotReplayStatus.READY or not replay.replacement_ready:
        issues.append(
            _issue(
                "SNAPSHOT_REPLACEMENT_NOT_READY",
                "candidate replay report is not replacement-ready",
                path=candidate,
            )
        )
        return (), tuple(issues), 0, 0, ""
    if candidate.is_symlink() or not candidate.is_file() or not os.access(candidate, os.R_OK):
        issues.append(
            _issue(
                "SNAPSHOT_REPLACEMENT_CANDIDATE_UNREADABLE",
                "candidate database must be a readable regular non-symlink file",
                path=candidate,
            )
        )
        return (), tuple(issues), 0, 0, ""
    try:
        conn = connect(candidate)
        try:
            validate_schema(conn)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise sqlite3.IntegrityError("candidate integrity check failed")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("candidate foreign key check failed")
            snapshot_count = conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
            entry_count = conn.execute("SELECT count(*) FROM snapshot_entries").fetchone()[0]
            candidate_digest = _destination_digest(conn)
        finally:
            conn.close()
    except (OSError, sqlite3.Error, StewardError, ValueError, TypeError, KeyError):
        issues.append(
            _issue(
                "SNAPSHOT_REPLACEMENT_CANDIDATE_INVALID",
                "candidate database did not pass schema or SQLite integrity validation",
                path=candidate,
            )
        )
        return (), tuple(issues), 0, 0, ""
    validations.extend(
        (
            {"code": "SCHEMA_VALID", "message": str(SCHEMA_VERSION)},
            {"code": "INTEGRITY_VALID", "message": "ok"},
            {"code": "FOREIGN_KEYS_VALID", "message": "ok"},
            {"code": "SNAPSHOT_COUNT", "message": str(snapshot_count)},
            {"code": "ENTRY_COUNT", "message": str(entry_count)},
            {"code": "SNAPSHOT_IDENTITY_VALID", "message": "canonical derived content"},
        )
    )
    if replay.destination_schema_version != SCHEMA_VERSION:
        issues.append(_issue("SNAPSHOT_REPLACEMENT_SCHEMA_MISMATCH", "replay schema version differs"))
    if snapshot_count != replay.replayed_snapshot_count or entry_count != replay.replayed_entry_count:
        issues.append(_issue("SNAPSHOT_REPLACEMENT_COUNT_MISMATCH", "candidate counts differ from replay report"))
    if (
        candidate_digest != replay.source_snapshot_digest
        or candidate_digest != replay.destination_snapshot_digest
    ):
        issues.append(
            _issue("SNAPSHOT_REPLACEMENT_DIGEST_MISMATCH", "candidate digest differs from replay report")
        )
    if issues:
        return tuple(validations), tuple(issues), snapshot_count, entry_count, candidate_digest
    validations.append({"code": "REPLAY_DIGEST_VALID", "message": candidate_digest})
    return tuple(validations), (), snapshot_count, entry_count, candidate_digest


def _failed(
    candidate: Path,
    official: Path,
    code: str,
    message: str,
    *,
    validation_result: tuple[dict[str, str], ...] = (),
    snapshot_count: int = 0,
    entry_count: int = 0,
) -> SnapshotReplacementReport:
    return SnapshotReplacementReport(
        SnapshotReplacementStatus.FAILED,
        str(candidate),
        str(official),
        False,
        "",
        "",
        snapshot_count,
        entry_count,
        SCHEMA_VERSION,
        validation_result,
        (_issue(code, message, path=candidate),),
    )


def replace_snapshot_index(
    config: StewardConfig,
    replay: SnapshotReplayReport | ClassifiedOperationalReplayReport,
    *,
    fault_injector: FaultInjector | None = None,
) -> SnapshotReplacementReport:
    """Atomically install one READY isolated replay candidate; never invokes replay itself."""
    official = database_path(config)
    candidate = Path(replay.destination_database)
    if _same_database(candidate, official):
        return _failed(
            candidate,
            official,
            "SNAPSHOT_REPLACEMENT_TARGET_IS_OFFICIAL",
            "candidate must not be the official database or an alias of it",
        )
    if not candidate.exists():
        return _failed(
            candidate,
            official,
            "SNAPSHOT_REPLACEMENT_CANDIDATE_MISSING",
            "candidate database does not exist",
        )
    if official.is_symlink() or not official.is_file() or not os.access(official, os.R_OK):
        return _failed(
            candidate,
            official,
            "SNAPSHOT_REPLACEMENT_OFFICIAL_UNREADABLE",
            "official database must be a readable regular non-symlink file",
        )
    try:
        if candidate.stat().st_dev != official.parent.stat().st_dev:
            return _failed(
                candidate,
                official,
                "SNAPSHOT_REPLACEMENT_DESTINATION_INVALID",
                "candidate and official database must be on the same filesystem",
            )
    except OSError:
        return _failed(
            candidate,
            official,
            "SNAPSHOT_REPLACEMENT_DESTINATION_INVALID",
            "candidate or official database path cannot be inspected",
        )
    try:
        fault_checkpoint(fault_injector, "replacement", "before_validate")
        validations, issues, snapshot_count, entry_count, candidate_digest = _candidate_validation(
            candidate, replay
        )
        fault_checkpoint(fault_injector, "replacement", "after_candidate_validation")
    except FaultInjectionError:
        return _failed(
            candidate,
            official,
            "SNAPSHOT_REPLACEMENT_FAULT_INJECTED",
            "replacement fault was injected before atomic replacement",
        )
    if issues:
        return SnapshotReplacementReport(
            SnapshotReplacementStatus.FAILED,
            str(candidate),
            str(official),
            False,
            "",
            "",
            snapshot_count,
            entry_count,
            SCHEMA_VERSION,
            validations,
            issues,
        )
    pre_backup_digest = _file_digest(official)
    backup = config.paths.cache_dir / (
        f"state-before-snapshot-replacement-{pre_backup_digest}-{uuid.uuid4()}.db"
    )
    backup_report = create_snapshot_index_backup(official, backup)
    if backup_report.status != SnapshotBackupStatus.READY or backup_report.manifest is None:
        return _failed(
            candidate,
            official,
            "SNAPSHOT_REPLACEMENT_BACKUP_FAILED",
            "official database backup was not ready before atomic replacement",
            validation_result=validations,
            snapshot_count=snapshot_count,
            entry_count=entry_count,
        )
    lock: sqlite3.Connection | None = None
    old_digest = ""
    replacement_complete = False
    try:
        lock = connect(official)
        checkpoint = lock.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise sqlite3.OperationalError("official WAL checkpoint did not complete")
        lock.execute("BEGIN EXCLUSIVE")
        validations, issues, snapshot_count, entry_count, candidate_digest = _candidate_validation(
            candidate, replay
        )
        if issues:
            return SnapshotReplacementReport(
                SnapshotReplacementStatus.FAILED,
                str(candidate),
                str(official),
                False,
                "",
                "",
                snapshot_count,
                entry_count,
                SCHEMA_VERSION,
                validations,
                issues,
            )
        integrity = lock.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise sqlite3.IntegrityError("official database integrity changed before replacement")
        if lock.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("official database foreign keys changed before replacement")
        current_count, current_entries, current_digest, current_snapshot, current_entry = _index_identity(lock)
        if (
            (current_count, current_entries, current_digest, current_snapshot, current_entry)
            != (
                backup_report.snapshot_count,
                backup_report.entry_count,
                backup_report.source_digest,
                backup_report.manifest.source_snapshot_digest,
                backup_report.manifest.source_entry_digest,
            )
            or _file_digest(official) != backup_report.manifest.source_database_digest
        ):
            raise sqlite3.IntegrityError("official database changed after backup validation")
        _checkpoint_candidate(candidate)
        old_digest = _file_digest(official)
        fault_checkpoint(fault_injector, "replacement", "before_replace")
        os.replace(candidate, official)
        replacement_complete = True
    except (OSError, sqlite3.Error, StewardError, FaultInjectionError, ValueError, TypeError, KeyError):
        if lock is not None:
            try:
                lock.rollback()
            except sqlite3.Error:
                pass
        return _failed(
            candidate,
            official,
            "SNAPSHOT_REPLACEMENT_TRANSACTION_FAILED",
            "atomic replacement did not complete before the official database changed",
            validation_result=validations,
            snapshot_count=snapshot_count,
            entry_count=entry_count,
        )
    finally:
        if lock is not None:
            lock.close()
    if not replacement_complete:
        return _failed(
            candidate,
            official,
            "SNAPSHOT_REPLACEMENT_TRANSACTION_FAILED",
            "atomic replacement did not complete",
            validation_result=validations,
            snapshot_count=snapshot_count,
            entry_count=entry_count,
        )
    try:
        fault_checkpoint(fault_injector, "replacement", "after_replace_before_verify")
    except FaultInjectionError:
        return SnapshotReplacementReport(
            SnapshotReplacementStatus.FAILED,
            str(candidate),
            str(official),
            False,
            old_digest,
            "",
            snapshot_count,
            entry_count,
            SCHEMA_VERSION,
            validations,
            (
                _issue(
                    "SNAPSHOT_REPLACEMENT_POST_REPLACE_FAULT",
                    "atomic replacement completed before the injected post-replace failure",
                    path=official,
                ),
            ),
        )
    try:
        _remove_sqlite_sidecars(official)
        _fsync_file(official)
        _fsync_directory(official.parent)
        new_digest = _file_digest(official)
    except OSError:
        return SnapshotReplacementReport(
            SnapshotReplacementStatus.REPLACED,
            str(candidate),
            str(official),
            True,
            old_digest,
            "",
            snapshot_count,
            entry_count,
            SCHEMA_VERSION,
            validations,
            (
                _issue(
                    "SNAPSHOT_REPLACEMENT_DURABILITY_WARNING",
                    "replacement completed but post-replace fsync failed",
                    path=official,
                ),
            ),
        )
    return SnapshotReplacementReport(
        SnapshotReplacementStatus.REPLACED,
        str(candidate),
        str(official),
        True,
        old_digest,
        new_digest,
        snapshot_count,
        entry_count,
        SCHEMA_VERSION,
        validations,
        (),
    )
