"""Isolated acceptance for the guarded immutable SQLite reader."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

import local_steward.database as database_module
from local_steward.cli import app
from local_steward.database import (
    database_path,
    initialize,
    open_readonly_initialized,
)
from local_steward.errors import (
    StorageBusyError,
    StorageMigrationRequiredError,
    StorageNotInitializedError,
    StorageSchemaError,
)
from local_steward.file_agent import (
    SharedToolBudget,
    ToolExecutionContext,
    steward_inspect_snapshot,
    steward_list_snapshots,
)
from local_steward.runs import create_run
from local_steward.scan_budget import make_budget
from local_steward.snapshots import (
    _verified_snapshot_entries,
    create_snapshot,
    get_snapshot,
    list_snapshot_entries,
    list_snapshots,
    verify_snapshot,
)
from local_steward.storage import storage_status

from .test_protocol_completion import prepared_config


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inventory(path: Path) -> tuple[str, ...]:
    return tuple(sorted(item.name for item in path.iterdir()))


def _fixture(tmp_path: Path):
    config = prepared_config(tmp_path)
    root = tmp_path / "scope"
    root.mkdir()
    (root / "one.txt").write_text("one", encoding="utf-8")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=root),))
    snapshot = create_snapshot(config, (), make_budget())
    assert not any(Path(f"{database_path(config)}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))
    return config, snapshot


def _fingerprint(path: Path) -> tuple[int, int, int, int, int, str]:
    value = path.stat()
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        _digest(path),
    )


def test_all_governed_reads_leave_nonwritable_data_directory_unchanged(tmp_path: Path) -> None:
    config, snapshot = _fixture(tmp_path)
    path = database_path(config)
    before = _fingerprint(path)
    inventory = _inventory(path.parent)
    original_mode = path.parent.stat().st_mode & 0o777
    path.parent.chmod(0o555)
    try:
        assert storage_status(config).storage_status == "HEALTHY"
        assert list_snapshots(config)[0].snapshot_id == snapshot.snapshot_id
        assert verify_snapshot(config, snapshot.snapshot_id).status == "VALID"
        assert get_snapshot(config, snapshot.snapshot_id).snapshot_id == snapshot.snapshot_id
        assert list_snapshot_entries(config, snapshot.snapshot_id).returned_count == 2
        assert steward_list_snapshots(
            ToolExecutionContext(config, SharedToolBudget()), limit=1
        ).entries_returned == 1
        assert steward_inspect_snapshot(
            ToolExecutionContext(config, SharedToolBudget()), snapshot.snapshot_id, limit=1
        ).entries_returned == 1
    finally:
        path.parent.chmod(original_mode)
    assert _fingerprint(path) == before
    assert _inventory(path.parent) == inventory


def test_reader_rejects_missing_old_and_incomplete_schema(tmp_path: Path) -> None:
    missing = prepared_config(tmp_path / "missing")
    database_path(missing).unlink()
    with pytest.raises(StorageNotInitializedError):
        with open_readonly_initialized(missing):
            pass

    old = prepared_config(tmp_path / "old")
    conn = sqlite3.connect(database_path(old))
    conn.execute("UPDATE schema_metadata SET schema_version=2")
    conn.commit()
    conn.close()
    with pytest.raises(StorageMigrationRequiredError):
        with open_readonly_initialized(old):
            pass

    incomplete = prepared_config(tmp_path / "incomplete")
    database_path(incomplete).unlink()
    conn = sqlite3.connect(database_path(incomplete))
    conn.execute("CREATE TABLE unrelated(value TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(StorageSchemaError):
        with open_readonly_initialized(incomplete):
            pass


@pytest.mark.parametrize("suffix", ("-wal", "-shm", "-journal"))
def test_preexisting_sidecar_blocks_admission_and_is_untouched(
    tmp_path: Path, suffix: str
) -> None:
    config = prepared_config(tmp_path)
    sidecar = Path(f"{database_path(config)}{suffix}")
    sidecar.write_bytes(b"owned-sidecar")
    before = _digest(sidecar)
    with pytest.raises(StorageBusyError):
        with open_readonly_initialized(config):
            pass
    assert sidecar.is_file() and _digest(sidecar) == before


def test_atomic_replacement_during_session_rejects_result(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    path = database_path(config)
    replacement = path.parent / "replacement.db"
    initialize(replacement, "test", "2026-01-01T00:00:00Z")
    published: list[int] = []
    with pytest.raises(StorageBusyError):
        with open_readonly_initialized(config) as conn:
            computed = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
            os.replace(replacement, path)
        published.append(computed)
    assert published == []


def test_content_change_during_session_rejects_result(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    path = database_path(config)
    with pytest.raises(StorageBusyError):
        with open_readonly_initialized(config) as conn:
            assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
            content = bytearray(path.read_bytes())
            content[-1] ^= 1
            path.write_bytes(content)


def test_sidecar_appearing_before_release_rejects_result_without_cleanup(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    sidecar = Path(f"{database_path(config)}-wal")
    with pytest.raises(StorageBusyError):
        with open_readonly_initialized(config) as conn:
            assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
            sidecar.write_bytes(b"appeared")
    assert sidecar.read_bytes() == b"appeared"


def test_cli_maps_reader_sidecar_failure_without_touching_sidecar(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    sidecar = Path(f"{database_path(config)}-shm")
    sidecar.write_bytes(b"existing")
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config.source_path),
            "--format",
            "json",
            "snapshots",
            "list",
        ],
    )
    payload = json.loads(result.stdout)
    assert result.exit_code == 8
    assert payload["errors"][0]["code"] == "STORAGE_BUSY"
    assert sidecar.read_bytes() == b"existing"


def test_query_only_rejects_sql_mutation(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    before = _digest(database_path(config))
    with open_readonly_initialized(config) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE forbidden(value TEXT)")
    assert _digest(database_path(config)) == before


def test_operation_exception_closes_session_and_preserves_primary_error(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    with pytest.raises(ValueError, match="operation failed"):
        with open_readonly_initialized(config) as conn:
            conn.execute("SELECT count(*) FROM runs").fetchone()
            raise ValueError("operation failed")
    with open_readonly_initialized(config) as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "operation", ("entries", "facade-list", "storage-status", "cli-show", "cli-entries")
)
def test_composed_top_level_operation_opens_exactly_one_reader_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str
) -> None:
    config, snapshot = _fixture(tmp_path)
    original = database_module.sqlite3.connect
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(database_module.sqlite3, "connect", counted)
    if operation == "entries":
        verification, _snapshot, page = _verified_snapshot_entries(
            config, snapshot.snapshot_id, limit=1
        )
        assert verification.status == "VALID" and page.returned_count == 1
    elif operation == "facade-list":
        steward_list_snapshots(ToolExecutionContext(config, SharedToolBudget()), limit=1)
    elif operation.startswith("cli-"):
        command = operation.removeprefix("cli-")
        result = CliRunner().invoke(
            app,
            [
                "--config",
                str(config.source_path),
                "snapshots",
                command,
                snapshot.snapshot_id,
            ],
        )
        assert result.exit_code == 0
    else:
        assert storage_status(config).storage_status == "HEALTHY"
    assert calls == 1


def test_writer_contract_still_accepts_mutation_in_writable_fixture(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    run = create_run(config, "reader.writer-regression")
    assert run.run_kind == "reader.writer-regression"
    with open_readonly_initialized(config) as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
