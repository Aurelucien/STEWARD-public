"""LOCAL-0002-R1 protocol validation: failure, integrity, recovery, and contention."""

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

import pytest

from local_steward.config import load_config
from local_steward.evidence import canonical_json, digest, write_evidence
from local_steward.errors import EvidenceError, StorageBusyError
from local_steward.models import RunStatus
from local_steward.runs import create_run, get_run, transition_run
from local_steward.storage import initialize_storage, rebuild_index, storage_status, verify_evidence

from .conftest import write_config


def prepared_config(tmp_path: Path):
    for name in ("data/cache", "data/evidence", "data/quarantine"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    config = load_config(write_config(tmp_path), project_root=tmp_path)
    initialize_storage(config)
    return config


def run_file(config, run_id: str, sequence: int = 1) -> Path:
    return config.paths.evidence_dir / "runs" / run_id / f"{sequence:08d}_run.created.json"


def replace_json(path: Path, change) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    change(value)
    path.write_bytes(canonical_json(value))


def make_record() -> dict[str, object]:
    item: dict[str, object] = {
        "run_id": "00000000-0000-4000-8000-000000000000",
        "sequence": 1,
        "evidence_type": "run.created",
    }
    item["evidence_digest"] = digest(item)  # type: ignore[arg-type]
    return item


def test_evidence_write_failure_never_creates_final(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = make_record()

    def fail_replace(*_args: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("local_steward.evidence.os.replace", fail_replace)
    with pytest.raises(EvidenceError):
        write_evidence(tmp_path, record)  # type: ignore[arg-type]
    assert not list(tmp_path.rglob("*.json"))
    assert not list(tmp_path.rglob("*.tmp"))


def test_evidence_fsync_failure_never_creates_final(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = make_record()
    monkeypatch.setattr(
        "local_steward.evidence.os.fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("injected fsync failure")),
    )
    with pytest.raises(EvidenceError):
        write_evidence(tmp_path, record)  # type: ignore[arg-type]
    assert not list(tmp_path.rglob("*.json"))
    assert not list(tmp_path.rglob("*.tmp"))


def test_sqlite_failure_after_evidence_creates_detectable_orphan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = prepared_config(tmp_path)
    import local_steward.runs as runs_module

    original = runs_module.open_initialized

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO runs"):
                raise sqlite3.IntegrityError("injected insert failure")
            return self.connection.execute(sql, params)

        def rollback(self):
            self.connection.rollback()

        def close(self):
            self.connection.close()

    monkeypatch.setattr(
        runs_module, "open_initialized", lambda _config: FailingConnection(original(config))
    )
    with pytest.raises(sqlite3.IntegrityError):
        create_run(config, "test")
    run_dirs = list((config.paths.evidence_dir / "runs").iterdir())
    assert len(run_dirs) == 1
    result = verify_evidence(config, run_dirs[0].name)[0]
    assert result.ledger_valid and not result.index_consistent


def test_commit_failure_leaves_no_database_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = prepared_config(tmp_path)
    import local_steward.runs as runs_module

    original = runs_module.open_initialized

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, params=()):
            return self.connection.execute(sql, params)

        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

        def rollback(self):
            self.connection.rollback()

        def close(self):
            self.connection.close()

    monkeypatch.setattr(
        runs_module, "open_initialized", lambda _config: FailingConnection(original(config))
    )
    with pytest.raises(StorageBusyError):
        create_run(config, "test")
    assert storage_status(config).run_count == 0
    assert len(list((config.paths.evidence_dir / "runs").iterdir())) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["payload"].update({"metadata": {"tampered": True}}),
        lambda data: data.update({"previous_evidence_digest": "0" * 64}),
        lambda data: data.update({"evidence_digest": "0" * 64}),
        lambda data: data.update({"sequence": 2}),
        lambda data: data.pop("sequence"),
        lambda data: data.update({"schema_version": 99}),
        lambda data: data.update({"evidence_type": "unknown.event"}),
    ],
)
def test_tampered_evidence_is_invalid(tmp_path: Path, mutation) -> None:
    config = prepared_config(tmp_path)
    run = create_run(config, "test")
    path = run_file(config, run.run_id)
    replace_json(path, mutation)
    result = verify_evidence(config, run.run_id)[0]
    assert result.status == "INVALID" and not result.ledger_valid


def test_chain_gap_and_duplicate_identity_are_invalid(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    run = create_run(config, "test")
    transition_run(config, run.run_id, RunStatus.SCANNING, "protocol test")
    second = config.paths.evidence_dir / "runs" / run.run_id / "00000002_run.state_transition.json"
    second.unlink()
    assert verify_evidence(config, run.run_id)[0].status == "INVALID"


def test_duplicate_identity_and_digest_are_invalid(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    run = create_run(config, "test")
    transition_run(config, run.run_id, RunStatus.SCANNING, "protocol test")
    first = run_file(config, run.run_id)
    second = config.paths.evidence_dir / "runs" / run.run_id / "00000002_run.state_transition.json"
    first_data = json.loads(first.read_text(encoding="utf-8"))

    def duplicate(data):
        data["evidence_id"] = first_data["evidence_id"]
        data["evidence_digest"] = digest(data)

    replace_json(second, duplicate)
    assert verify_evidence(config, run.run_id)[0].status == "INVALID"


def test_rebuild_recovers_deleted_index_idempotently(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    run = create_run(config, "test", {"case": "recovery"})
    transition_run(config, run.run_id, RunStatus.SCANNING, "start")
    before = get_run(config, run.run_id)
    (config.paths.data_dir / "state.db").unlink()
    rebuild_index(config)
    after = get_run(config, run.run_id)
    assert before == after
    rebuild_index(config)
    assert get_run(config, run.run_id) == after


def test_rebuild_refuses_invalid_ledger_without_replacing_index(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    run = create_run(config, "test")
    before = get_run(config, run.run_id)
    replace_json(
        run_file(config, run.run_id), lambda data: data.update({"evidence_digest": "0" * 64})
    )
    with pytest.raises(EvidenceError):
        rebuild_index(config)
    assert get_run(config, run.run_id) == before


def test_storage_status_detects_missing_db_and_temp_and_orphan(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    run = create_run(config, "test")
    temp = config.paths.evidence_dir / "runs" / run.run_id / ".orphan.tmp"
    temp.touch()
    assert storage_status(config).storage_status == "DEGRADED"
    (config.paths.data_dir / "state.db").unlink()
    status = storage_status(config)
    assert status.storage_status == "UNINITIALIZED" and status.orphaned_evidence_count == 1


def test_storage_status_detects_corrupt_database(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    database = config.paths.data_dir / "state.db"
    database.write_text("not sqlite", encoding="utf-8")
    assert storage_status(config).storage_status == "CORRUPT"


def test_process_concurrent_creates_are_unique(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    script = "from pathlib import Path; from local_steward.config import load_config; from local_steward.runs import create_run; import sys; print(create_run(load_config(Path(sys.argv[1])), 'test').run_id)"
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(config.source_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    identifiers = [child.communicate(timeout=10)[0].strip() for child in children]
    assert all(child.returncode == 0 for child in children)
    assert len(set(identifiers)) == 2
    assert len({UUID(value) for value in identifiers}) == 2


def test_busy_timeout_is_finite(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    lock = sqlite3.connect(config.paths.data_dir / "state.db", isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    with pytest.raises(StorageBusyError):
        create_run(config, "test")
    lock.rollback()
    lock.close()
    # SQLite owns a 5-second busy timeout. Allow scheduling overhead on shared
    # runners without weakening the finite-timeout assertion.
    assert time.monotonic() - started < 12


def test_hundred_creates_have_unique_ids(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    identifiers = {create_run(config, "test").run_id for _ in range(100)}
    assert len(identifiers) == 100


def test_two_process_transitions_have_one_winner(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    run = create_run(config, "test")
    script = "from pathlib import Path; from local_steward.config import load_config; from local_steward.runs import transition_run; from local_steward.models import RunStatus; import sys; transition_run(load_config(Path(sys.argv[1])), sys.argv[2], RunStatus.SCANNING, 'race')"
    children = [
        subprocess.Popen([sys.executable, "-c", script, str(config.source_path), run.run_id])
        for _ in range(2)
    ]
    assert sum(child.wait(timeout=10) == 0 for child in children) == 1
    assert get_run(config, run.run_id).last_sequence == 2
