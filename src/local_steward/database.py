"""SQLite v3 derived-index access; no business SQL outside this module."""

import hashlib
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal

from .errors import (
    StorageCorruptionError,
    StorageMigrationRequiredError,
    StorageNotInitializedError,
    StorageBusyError,
    StorageSchemaError,
    StorageSchemaTooNewError,
    StorageMigrationError,
)
from .models import StewardConfig

SCHEMA_VERSION = 3
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def database_path(config: StewardConfig) -> Path:
    return config.paths.data_dir / "state.db"


def connect(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn
    except sqlite3.DatabaseError as error:
        raise StorageCorruptionError(f"invalid SQLite database: {error}") from error


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def validate_schema(conn: sqlite3.Connection) -> None:
    tables = _tables(conn)
    required = {"schema_metadata", "runs", "evidence_records"}
    if not tables:
        raise StorageSchemaError("database is empty or partially initialized")
    if not required.issubset(tables):
        raise StorageSchemaError("database schema is incomplete")
    rows = list(conn.execute("SELECT schema_version FROM schema_metadata"))
    if len(rows) != 1:
        raise StorageSchemaError("schema metadata must contain exactly one record")
    version = rows[0][0]
    if version > SCHEMA_VERSION:
        raise StorageSchemaTooNewError("database schema is newer than this tool")
    if version < SCHEMA_VERSION:
        raise StorageMigrationRequiredError("database schema requires explicit migration")


def _v2_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""CREATE TABLE snapshots (snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), status TEXT NOT NULL, consistency TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT NOT NULL, config_digest TEXT NOT NULL, scope_ids_json TEXT NOT NULL, budget_json TEXT NOT NULL, entry_count INTEGER NOT NULL, observed_count INTEGER NOT NULL, error_count INTEGER NOT NULL, excluded_count INTEGER NOT NULL, total_regular_file_bytes INTEGER NOT NULL, max_depth_observed INTEGER NOT NULL, entries_digest TEXT NOT NULL, snapshot_digest TEXT NOT NULL UNIQUE, evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id), evidence_relative_path TEXT NOT NULL UNIQUE, snapshot_evidence_schema_version INTEGER NOT NULL, hash_policy_json TEXT, allocated_regular_file_bytes_known_sum INTEGER, allocated_regular_file_unknown_count INTEGER, payload_observation_summary_json TEXT, UNIQUE(run_id));
CREATE TABLE snapshot_entries (snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE, scope_id TEXT NOT NULL, relative_path TEXT NOT NULL, entry_id TEXT NOT NULL UNIQUE, object_type TEXT NOT NULL, device_id INTEGER, inode INTEGER, mode INTEGER, uid INTEGER, gid INTEGER, size_bytes INTEGER, mtime_ns INTEGER, ctime_ns INTEGER, birthtime_ns INTEGER, link_count INTEGER, symlink_target_raw TEXT, readable INTEGER NOT NULL, writable INTEGER NOT NULL, executable INTEGER NOT NULL, observation_status TEXT NOT NULL, error_code TEXT, error_message TEXT, excluded INTEGER NOT NULL, allocated_size_bytes INTEGER, payload_observation_json TEXT, PRIMARY KEY(snapshot_id, scope_id, relative_path));
CREATE TABLE snapshot_diffs (diff_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), base_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id), target_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id), created_at TEXT NOT NULL, status TEXT NOT NULL, change_count INTEGER NOT NULL, added_count INTEGER NOT NULL, removed_count INTEGER NOT NULL, type_changed_count INTEGER NOT NULL, metadata_changed_count INTEGER NOT NULL, observation_changed_count INTEGER NOT NULL, changes_digest TEXT NOT NULL, diff_digest TEXT NOT NULL UNIQUE, evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id), evidence_relative_path TEXT NOT NULL UNIQUE);
CREATE TABLE snapshot_diff_entries (diff_id TEXT NOT NULL REFERENCES snapshot_diffs(diff_id) ON DELETE CASCADE, scope_id TEXT NOT NULL, relative_path TEXT NOT NULL, change_type TEXT NOT NULL, changed_fields_json TEXT NOT NULL, before_entry_json TEXT, after_entry_json TEXT, PRIMARY KEY(diff_id, scope_id, relative_path));""")


def migrate_v1_to_v2(path: Path) -> bool:
    """Explicitly migrate precisely schema v1, retaining all existing rows."""
    conn = connect(path)
    try:
        rows = list(conn.execute("SELECT schema_version FROM schema_metadata"))
        if len(rows) != 1:
            raise StorageSchemaError("schema metadata must contain exactly one record")
        version = rows[0][0]
        if version == SCHEMA_VERSION:
            validate_schema(conn)
            return True
        if version != 1:
            if version > SCHEMA_VERSION:
                raise StorageSchemaTooNewError("database schema is newer than this tool")
            raise StorageMigrationRequiredError("unsupported schema migration")
        conn.execute("BEGIN IMMEDIATE")
        _v2_tables(conn)
        conn.execute("UPDATE schema_metadata SET schema_version=?", (SCHEMA_VERSION,))
        if (
            conn.execute("PRAGMA foreign_key_check").fetchone() is not None
            or conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
        ):
            raise StorageMigrationError("database checks failed during migration")
        conn.commit()
        return False
    except sqlite3.Error as error:
        conn.rollback()
        raise StorageMigrationError(f"storage migration failed: {error}") from error
    finally:
        conn.close()


def initialize(path: Path, tool_version: str, created_at: str) -> None:
    if path.exists():
        conn = connect(path)
        try:
            validate_schema(conn)
        finally:
            conn.close()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript("""CREATE TABLE schema_metadata (schema_version INTEGER NOT NULL, created_at TEXT NOT NULL, tool_version TEXT NOT NULL);
CREATE TABLE runs (run_id TEXT PRIMARY KEY, run_kind TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, config_digest TEXT NOT NULL, metadata_json TEXT NOT NULL, last_sequence INTEGER NOT NULL, last_evidence_digest TEXT, terminal INTEGER NOT NULL);
CREATE TABLE evidence_records (evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), sequence INTEGER NOT NULL, evidence_type TEXT NOT NULL, created_at TEXT NOT NULL, relative_path TEXT NOT NULL, previous_evidence_digest TEXT, evidence_digest TEXT NOT NULL, schema_version INTEGER NOT NULL, UNIQUE(run_id, sequence), UNIQUE(relative_path), UNIQUE(evidence_digest));""")
        _v2_tables(conn)
        conn.execute(
            "INSERT INTO schema_metadata VALUES (?, ?, ?)",
            (SCHEMA_VERSION, created_at, tool_version),
        )
        conn.commit()
    except sqlite3.Error as error:
        conn.rollback()
        raise StorageSchemaError(f"unable to initialize schema: {error}") from error
    finally:
        conn.close()


def open_initialized(config: StewardConfig) -> sqlite3.Connection:
    path = database_path(config)
    if not path.is_file():
        raise StorageNotInitializedError("storage is not initialized; run storage init")
    conn = connect(path)
    try:
        validate_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


@dataclass(frozen=True, slots=True)
class _DatabaseFingerprint:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


def _database_fingerprint(path: Path) -> _DatabaseFingerprint:
    """Bind a complete digest to one regular, non-symlink database identity."""
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise StorageNotInitializedError("storage database must be a regular file")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.lstat()
    except FileNotFoundError as error:
        raise StorageNotInitializedError("storage is not initialized; run storage init") from error
    except PermissionError as error:
        raise StorageBusyError("storage database is not readable") from error
    except OSError as error:
        raise StorageBusyError("storage database fingerprint is unavailable") from error
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    confirmed = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity != confirmed:
        raise StorageBusyError("storage database changed during fingerprinting")
    return _DatabaseFingerprint(*identity, digest.hexdigest())


def _sidecar_paths(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in _SIDECAR_SUFFIXES)


def _directory_inventory(path: Path) -> tuple[str, ...]:
    try:
        with os.scandir(path) as entries:
            return tuple(sorted(item.name for item in entries))
    except OSError as error:
        raise StorageBusyError("storage directory inventory is unavailable") from error


def _reject_sidecars(path: Path) -> None:
    present = [item.name for item in _sidecar_paths(path) if os.path.lexists(item)]
    if present:
        raise StorageBusyError("storage has prohibited SQLite sidecar state")


class _GuardedReadSession:
    """Internal immutable reader whose result is valid only after release checks."""

    def __init__(self, config: StewardConfig) -> None:
        self._path = database_path(config)
        self._connection: sqlite3.Connection | None = None
        self._fingerprint: _DatabaseFingerprint | None = None
        self._inventory: tuple[str, ...] | None = None

    def __enter__(self) -> sqlite3.Connection:
        _reject_sidecars(self._path)
        self._inventory = _directory_inventory(self._path.parent)
        self._fingerprint = _database_fingerprint(self._path)
        resolved = self._path.resolve(strict=True)
        uri = f"{resolved.as_uri()}?mode=ro&immutable=1"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                uri,
                timeout=5.0,
                isolation_level=None,
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise StorageCorruptionError("SQLite query-only mode was not enabled")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            validate_schema(connection)
            if _database_fingerprint(self._path) != self._fingerprint:
                raise StorageBusyError("storage database changed during reader admission")
            _reject_sidecars(self._path)
        except sqlite3.DatabaseError as error:
            if "readonly" in str(error).lower():
                raise
            raise StorageCorruptionError(f"invalid SQLite database: {error}") from error
        except Exception:
            if connection is not None:
                connection.close()
            raise
        assert connection is not None
        self._connection = connection
        return connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        close_error: sqlite3.Error | None = None
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error as error:
                close_error = error
            finally:
                self._connection = None
        release_error: Exception | None = None
        try:
            if self._fingerprint is None or self._inventory is None:
                raise StorageBusyError("storage reader admission did not complete")
            if _database_fingerprint(self._path) != self._fingerprint:
                raise StorageBusyError("storage database changed during read")
            _reject_sidecars(self._path)
            if _directory_inventory(self._path.parent) != self._inventory:
                raise StorageBusyError("storage directory changed during read")
        except Exception as error:
            release_error = error
        if release_error is not None:
            raise release_error
        if close_error is not None and exc_type is None:
            raise StorageCorruptionError("unable to close read-only storage session") from close_error
        return False


def open_readonly_initialized(config: StewardConfig) -> _GuardedReadSession:
    """Create one guarded immutable session for a complete top-level read."""
    return _GuardedReadSession(config)
