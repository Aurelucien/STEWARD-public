"""LOCAL-0003-R1C2B1 atomic Snapshot derived-index replacement checks."""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.database import SCHEMA_VERSION, database_path, initialize
from local_steward.models import SnapshotReplacementStatus, SnapshotReplayStatus
from local_steward.snapshot_replacement import replace_snapshot_index
from local_steward.snapshot_replay import _destination_digest, replay_snapshot_index
from local_steward.snapshots import get_snapshot, inspect_snapshot_inventory
from local_steward.storage import storage_status, verify_evidence_report

from .test_snapshot_queries import snapshot_fixture


def _ready_candidate(tmp_path: Path):
    config, snapshot = snapshot_fixture(tmp_path)
    candidate = tmp_path / "candidate.sqlite3"
    replay = replay_snapshot_index(config, candidate)
    assert replay.status == SnapshotReplayStatus.READY and replay.replacement_ready
    return config, snapshot, candidate, replay


def _contents(path: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    connection = sqlite3.connect(path)
    try:
        snapshots = list(connection.execute("SELECT * FROM snapshots ORDER BY snapshot_id"))
        entries = list(
            connection.execute(
                "SELECT * FROM snapshot_entries ORDER BY snapshot_id, scope_id, relative_path"
            )
        )
        return snapshots, entries
    finally:
        connection.close()


def _evidence_bytes(config) -> dict[Path, bytes]:
    return {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("kind", ("missing", "empty", "text", "schema"))
def test_candidate_gate_rejects_unsafe_inputs_before_official_change(
    tmp_path: Path, kind: str
) -> None:
    config, _snapshot, candidate, replay = _ready_candidate(tmp_path)
    official = database_path(config)
    before = official.read_bytes()
    if kind == "missing":
        candidate.unlink()
    elif kind == "empty":
        candidate.unlink()
        initialize(candidate, "test", "1970-01-01T00:00:00.000000Z")
    elif kind == "text":
        candidate.write_text("not sqlite", encoding="utf-8")
    else:
        connection = sqlite3.connect(candidate)
        try:
            connection.execute("UPDATE schema_metadata SET schema_version=999")
            connection.commit()
        finally:
            connection.close()
    result = replace_snapshot_index(config, replay)
    assert result.status == SnapshotReplacementStatus.FAILED and not result.replacement_ready
    assert official.read_bytes() == before


def test_replacement_requires_ready_replay_and_matching_digest(tmp_path: Path) -> None:
    config, _snapshot, candidate, replay = _ready_candidate(tmp_path)
    official = database_path(config)
    before = official.read_bytes()
    not_ready = replace(replay, status=SnapshotReplayStatus.FAILED, replacement_ready=False)
    assert replace_snapshot_index(config, not_ready).status == SnapshotReplacementStatus.FAILED
    mismatched = replace(replay, source_snapshot_digest="0" * 64)
    result = replace_snapshot_index(config, mismatched)
    assert result.status == SnapshotReplacementStatus.FAILED
    assert "SNAPSHOT_REPLACEMENT_DIGEST_MISMATCH" in {issue["code"] for issue in result.issues}
    assert candidate.exists() and official.read_bytes() == before


def test_candidate_foreign_key_failure_is_rejected_before_official_change(tmp_path: Path) -> None:
    config, snapshot, candidate, replay = _ready_candidate(tmp_path)
    official = database_path(config)
    before = official.read_bytes()
    connection = sqlite3.connect(candidate)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        evidence_id = connection.execute(
            "SELECT evidence_id FROM snapshots WHERE snapshot_id=?", (snapshot.snapshot_id,)
        ).fetchone()[0]
        connection.execute("DELETE FROM evidence_records WHERE evidence_id=?", (evidence_id,))
        connection.commit()
    finally:
        connection.close()
    result = replace_snapshot_index(config, replay)
    assert result.status == SnapshotReplacementStatus.FAILED
    assert "SNAPSHOT_REPLACEMENT_CANDIDATE_INVALID" in {issue["code"] for issue in result.issues}
    assert official.read_bytes() == before


def test_replacement_protects_official_path_symlink_and_same_inode(tmp_path: Path) -> None:
    config, _snapshot, candidate, replay = _ready_candidate(tmp_path)
    official = database_path(config)
    direct = replace(replay, destination_database=str(official))
    assert replace_snapshot_index(config, direct).issues[0]["code"] == "SNAPSHOT_REPLACEMENT_TARGET_IS_OFFICIAL"
    alias = tmp_path / "official-alias.sqlite3"
    alias.symlink_to(official)
    linked = replace(replay, destination_database=str(alias))
    assert replace_snapshot_index(config, linked).issues[0]["code"] == "SNAPSHOT_REPLACEMENT_TARGET_IS_OFFICIAL"
    hardlink = tmp_path / "official-hardlink.sqlite3"
    hardlink.hardlink_to(official)
    inode_alias = replace(replay, destination_database=str(hardlink))
    assert replace_snapshot_index(config, inode_alias).issues[0]["code"] == "SNAPSHOT_REPLACEMENT_TARGET_IS_OFFICIAL"
    assert candidate.exists()


def test_ready_candidate_replaces_index_atomically_without_changing_facts(tmp_path: Path) -> None:
    config, snapshot, candidate, replay = _ready_candidate(tmp_path)
    official = database_path(config)
    candidate_contents = _contents(candidate)
    evidence_before = _evidence_bytes(config)
    connection = sqlite3.connect(official)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM snapshot_entries WHERE snapshot_id=?", (snapshot.snapshot_id,))
        connection.execute("DELETE FROM snapshots WHERE snapshot_id=?", (snapshot.snapshot_id,))
        connection.commit()
    finally:
        connection.close()
    result = replace_snapshot_index(config, replay)
    assert result.status == SnapshotReplacementStatus.REPLACED and result.replacement_ready
    assert result.schema_version == SCHEMA_VERSION and result.new_database_digest
    assert not candidate.exists() and _contents(official) == candidate_contents
    assert get_snapshot(config, snapshot.snapshot_id).snapshot_digest == snapshot.snapshot_digest
    connection = sqlite3.connect(official)
    connection.row_factory = sqlite3.Row
    try:
        assert _destination_digest(connection) == replay.source_snapshot_digest
    finally:
        connection.close()
    inventory = inspect_snapshot_inventory(config)
    assert inventory.indexed_snapshots == inventory.indexed_entry_groups == 1
    assert storage_status(config).storage_status == "HEALTHY"
    assert verify_evidence_report(config).snapshot_evidence.valid_count == 1
    assert _evidence_bytes(config) == evidence_before
    assert list(config.paths.cache_dir.glob("state-before-snapshot-replacement-*.db"))


def test_interrupted_replace_leaves_old_database_and_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, snapshot, candidate, replay = _ready_candidate(tmp_path)
    official = database_path(config)
    before = official.read_bytes()
    evidence_before = _evidence_bytes(config)
    monkeypatch.setattr(
        "local_steward.snapshot_replacement.os.replace",
        lambda *_args: (_ for _ in ()).throw(OSError("injected replacement interruption")),
    )
    result = replace_snapshot_index(config, replay)
    assert result.status == SnapshotReplacementStatus.FAILED
    assert official.read_bytes() == before and candidate.exists()
    assert get_snapshot(config, snapshot.snapshot_id).snapshot_digest == snapshot.snapshot_digest
    assert _evidence_bytes(config) == evidence_before


def test_replacement_checkpoints_wal_and_removes_stale_sidecars(tmp_path: Path) -> None:
    config, snapshot, candidate, replay = _ready_candidate(tmp_path)
    official = database_path(config)
    connection = sqlite3.connect(official)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "UPDATE snapshots SET config_digest=? WHERE snapshot_id=?",
            ("old-wal-content", snapshot.snapshot_id),
        )
        connection.commit()
        assert Path(f"{official}-wal").exists()
        result = replace_snapshot_index(config, replay)
    finally:
        connection.close()
    assert result.status == SnapshotReplacementStatus.REPLACED
    assert not Path(f"{official}-wal").exists() and not Path(f"{official}-shm").exists()
    connection = sqlite3.connect(official)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
