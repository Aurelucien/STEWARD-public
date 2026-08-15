"""LOCAL-0003-R1C2A strict isolated Snapshot Evidence replay checks."""

import json
import sqlite3
from pathlib import Path

import pytest

from local_steward import __version__
from local_steward.database import SCHEMA_VERSION, database_path, initialize
from local_steward.models import RunStatus, SnapshotReplayStatus
from local_steward.snapshot_replay import replay_snapshot_index

from .test_protocol_completion import prepared_config
from .test_snapshot_inventory import _write_clone
from .test_snapshot_queries import snapshot_fixture


def _counts(path: Path) -> tuple[int, int, int]:
    connection = sqlite3.connect(path)
    try:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("runs", "snapshots", "snapshot_entries")
        )
    finally:
        connection.close()


def _connection(config) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path(config))
    connection.execute("PRAGMA foreign_keys = OFF")
    return connection


def test_empty_ledger_replays_to_ready_empty_isolated_schema(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    destination = tmp_path / "candidate.sqlite3"
    report = replay_snapshot_index(config, destination)
    assert report.status == SnapshotReplayStatus.READY and report.replacement_ready
    assert report.snapshot_evidence_count == report.replayed_snapshot_count == 0
    assert report.source_snapshot_digest == report.destination_snapshot_digest
    assert report.destination_schema_version == SCHEMA_VERSION and _counts(destination) == (0, 0, 0)


def test_complete_and_partial_snapshot_replay_preserves_business_content(tmp_path: Path) -> None:
    config, complete = snapshot_fixture(tmp_path)
    from local_steward.scan_budget import make_budget
    from local_steward.snapshots import create_snapshot

    partial = create_snapshot(config, (), make_budget(max_entries=1))
    destination = tmp_path / "candidate.sqlite3"
    report = replay_snapshot_index(config, destination)
    assert report.status == SnapshotReplayStatus.READY and report.replacement_ready
    assert report.replayed_snapshot_count == 2
    assert report.replayed_entry_count == complete.entry_count + partial.entry_count
    assert report.source_snapshot_digest == report.destination_snapshot_digest
    connection = sqlite3.connect(destination)
    try:
        rows = list(connection.execute("SELECT snapshot_id, run_id, evidence_id FROM snapshots ORDER BY snapshot_id"))
        entries = list(
            connection.execute(
                "SELECT snapshot_id, scope_id, relative_path FROM snapshot_entries "
                "ORDER BY snapshot_id, scope_id, relative_path"
            )
        )
    finally:
        connection.close()
    assert {row[0] for row in rows} == {complete.snapshot_id, partial.snapshot_id}
    assert all(row[1] in {complete.run_id, partial.run_id} and row[2] for row in rows)
    assert entries == sorted(entries)


def test_strict_replay_rejects_invalid_run_without_partial_business_rows(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    connection = _connection(config)
    try:
        connection.execute("UPDATE runs SET status=? WHERE run_id=?", (RunStatus.SCANNING.value, snapshot.run_id))
        connection.commit()
    finally:
        connection.close()
    destination = tmp_path / "candidate.sqlite3"
    report = replay_snapshot_index(config, destination)
    assert report.status == SnapshotReplayStatus.FAILED and not report.replacement_ready
    assert report.replayable_evidence_count == 0 and report.rejected_evidence_count == 1
    assert "SNAPSHOT_RUN_STATUS_INVALID" in {issue["code"] for issue in report.issues}
    assert _counts(destination) == (0, 0, 0)


def test_strict_replay_rejects_invalid_run_ledger_without_partial_business_rows(
    tmp_path: Path,
) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    created = config.paths.evidence_dir / "runs" / snapshot.run_id / "00000001_run.created.json"
    document = json.loads(created.read_text(encoding="utf-8"))
    document["evidence_digest"] = "0" * 64
    created.write_text(json.dumps(document), encoding="utf-8")
    destination = tmp_path / "candidate.sqlite3"
    report = replay_snapshot_index(config, destination)
    assert report.status == SnapshotReplayStatus.FAILED and not report.replacement_ready
    assert report.rejected_evidence_count == 1
    assert "SNAPSHOT_REPLAY_SOURCE_INVALID" in {issue["code"] for issue in report.issues}
    assert _counts(destination) == (0, 0, 0)


def test_strict_replay_collects_duplicate_rejections_and_never_uses_live_snapshot_index(
    tmp_path: Path,
) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    _write_clone(config, snapshot.snapshot_id)
    connection = _connection(config)
    try:
        connection.execute("DELETE FROM snapshot_entries WHERE snapshot_id=?", (snapshot.snapshot_id,))
        connection.execute("DELETE FROM snapshots WHERE snapshot_id=?", (snapshot.snapshot_id,))
        connection.commit()
    finally:
        connection.close()
    destination = tmp_path / "candidate.sqlite3"
    report = replay_snapshot_index(config, destination)
    assert report.status == SnapshotReplayStatus.FAILED
    assert {"SNAPSHOT_ID_DUPLICATE", "SNAPSHOT_RUN_DUPLICATE"} <= {
        issue["code"] for issue in report.issues
    }
    assert _counts(destination) == (0, 0, 0)


def test_target_database_isolation_and_nonempty_refusal(tmp_path: Path) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    live = database_path(config)
    assert replay_snapshot_index(config, live).issues[0]["code"] == "SNAPSHOT_REPLAY_TARGET_IS_LIVE_DATABASE"
    alias = tmp_path / "live-alias.sqlite3"
    alias.symlink_to(live)
    assert replay_snapshot_index(config, alias).issues[0]["code"] == "SNAPSHOT_REPLAY_TARGET_IS_LIVE_DATABASE"
    occupied = tmp_path / "occupied.sqlite3"
    occupied.write_text("do not overwrite", encoding="utf-8")
    report = replay_snapshot_index(config, occupied)
    assert report.issues[0]["code"] == "SNAPSHOT_REPLAY_DESTINATION_INVALID"
    assert occupied.read_text(encoding="utf-8") == "do not overwrite"
    empty_schema = tmp_path / "empty-schema.sqlite3"
    initialize(empty_schema, __version__, "1970-01-01T00:00:00.000000Z")
    assert replay_snapshot_index(config, empty_schema).status == SnapshotReplayStatus.READY


def test_replay_is_deterministic_and_does_not_change_live_facts(tmp_path: Path) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    before_database = database_path(config).read_bytes()
    before_evidence = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    }
    first = replay_snapshot_index(config, tmp_path / "one.sqlite3")
    second = replay_snapshot_index(config, tmp_path / "two.sqlite3")
    assert first.status == second.status == SnapshotReplayStatus.READY
    assert first.source_snapshot_digest == second.source_snapshot_digest
    assert first.destination_snapshot_digest == second.destination_snapshot_digest
    assert database_path(config).read_bytes() == before_database
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    } == before_evidence


def test_write_failure_rolls_back_all_candidate_business_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    monkeypatch.setattr(
        "local_steward.snapshot_replay._insert_snapshot_index_rows",
        lambda *_args: (_ for _ in ()).throw(sqlite3.IntegrityError("injected")),
    )
    destination = tmp_path / "candidate.sqlite3"
    report = replay_snapshot_index(config, destination)
    assert report.status == SnapshotReplayStatus.FAILED and not report.replacement_ready
    assert "SNAPSHOT_REPLAY_WRITE_FAILED" in {issue["code"] for issue in report.issues}
    assert _counts(destination) == (0, 0, 0)


def test_completed_target_is_not_overwritten(tmp_path: Path) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    destination = tmp_path / "candidate.sqlite3"
    assert replay_snapshot_index(config, destination).status == SnapshotReplayStatus.READY
    second = replay_snapshot_index(config, destination)
    assert second.status == SnapshotReplayStatus.FAILED
    assert second.issues[0]["code"] == "SNAPSHOT_REPLAY_TARGET_NOT_EMPTY"
