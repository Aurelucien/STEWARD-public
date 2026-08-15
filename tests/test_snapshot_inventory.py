"""LOCAL-0003-R1C1A read-only bidirectional snapshot inventory checks."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from local_steward.database import database_path
from local_steward.evidence import canonical_json, digest
from local_steward.snapshots import (
    create_snapshot,
    entry_id,
    inspect_snapshot_inventory,
    snapshot_metadata_digest,
)

from .test_snapshot_queries import snapshot_fixture
from .test_protocol_completion import prepared_config


def _connection(config) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path(config))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = OFF")
    return connection


def _snapshot_row(config, snapshot_id: str) -> sqlite3.Row:
    connection = _connection(config)
    try:
        row = connection.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


def _snapshot_document(config, snapshot_id: str) -> tuple[sqlite3.Row, dict[str, object]]:
    row = _snapshot_row(config, snapshot_id)
    path = config.paths.evidence_dir / str(row["evidence_relative_path"])
    return row, json.loads(path.read_text(encoding="utf-8"))


def _write_clone(
    config,
    snapshot_id: str,
    *,
    run_id: str | None = None,
) -> str:
    row, document = _snapshot_document(config, snapshot_id)
    evidence_id = str(uuid4())
    payload = document["payload"]
    assert isinstance(payload, dict)
    snapshot = payload["snapshot"]
    assert isinstance(snapshot, dict)
    document["evidence_id"] = evidence_id
    snapshot["evidence_id"] = evidence_id
    if run_id is not None:
        document["run_id"] = run_id
        snapshot["run_id"] = run_id
        snapshot["snapshot_digest"] = snapshot_metadata_digest(snapshot)
    document["sequence"] = 999
    document["evidence_digest"] = digest(document)  # type: ignore[arg-type]
    target_run = run_id or str(row["run_id"])
    target = config.paths.evidence_dir / "runs" / target_run / "00000999_filesystem.snapshot.json"
    target.write_bytes(canonical_json(document))
    return evidence_id


def _issue_codes(inventory) -> set[str]:
    return {issue["code"] for issue in inventory.issues}


def test_inventory_is_empty_without_snapshots(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    inventory = inspect_snapshot_inventory(config)
    assert inventory.snapshot_evidence_records == 0
    assert inventory.indexed_snapshots == 0
    assert not inventory.items and not inventory.issues


def test_inventory_associates_complete_snapshot_and_is_read_only(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    before_db = database_path(config).read_bytes()
    before_evidence = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    }
    inventory = inspect_snapshot_inventory(config)
    item = next(item for item in inventory.items if item.snapshot_id == snapshot.snapshot_id)
    assert item.evidence_id == _snapshot_row(config, snapshot.snapshot_id)["evidence_id"]
    assert item.evidence_present and item.index_present and item.run_present
    assert item.indexed_entry_count == snapshot.entry_count and not item.issue_codes
    assert database_path(config).read_bytes() == before_db
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    } == before_evidence


def test_inventory_multiple_snapshots_are_stable_and_do_not_scan_scope(tmp_path: Path) -> None:
    config, first = snapshot_fixture(tmp_path)
    second = create_snapshot(config, (), first.budget)
    config = replace(
        config,
        scopes=(replace(config.scopes[0], normalized_path=tmp_path / "missing-scope"),),
    )
    first_result = inspect_snapshot_inventory(config)
    second_result = inspect_snapshot_inventory(config)
    assert first_result == second_result
    assert [item.snapshot_id for item in first_result.items] == sorted(
        [first.snapshot_id, second.snapshot_id]
    )
    assert not first_result.issues


def test_inventory_finds_orphan_and_duplicate_snapshot_evidence(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    cloned_evidence_id = _write_clone(config, snapshot.snapshot_id)
    inventory = inspect_snapshot_inventory(config)
    clone = next(item for item in inventory.items if item.evidence_id == cloned_evidence_id)
    assert {"SNAPSHOT_EVIDENCE_ORPHANED", "SNAPSHOT_ID_DUPLICATE"} <= set(clone.issue_codes)


def test_inventory_finds_missing_and_wrong_type_index_evidence(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    connection = _connection(config)
    try:
        connection.execute(
            "UPDATE snapshots SET evidence_id=?, evidence_relative_path=? WHERE snapshot_id=?",
            (str(uuid4()), "runs/missing/00000001_filesystem.snapshot.json", snapshot.snapshot_id),
        )
        connection.commit()
    finally:
        connection.close()
    assert "SNAPSHOT_INDEX_EVIDENCE_MISSING" in _issue_codes(inspect_snapshot_inventory(config))

    config, snapshot = snapshot_fixture(tmp_path / "wrong-type")
    connection = _connection(config)
    try:
        record = connection.execute(
            "SELECT evidence_id, relative_path FROM evidence_records "
            "WHERE evidence_type='run.created' LIMIT 1"
        ).fetchone()
        assert record is not None
        connection.execute(
            "UPDATE snapshots SET evidence_id=?, evidence_relative_path=? WHERE snapshot_id=?",
            (record["evidence_id"], record["relative_path"], snapshot.snapshot_id),
        )
        connection.commit()
    finally:
        connection.close()
    assert "SNAPSHOT_INDEX_EVIDENCE_TYPE_MISMATCH" in _issue_codes(inspect_snapshot_inventory(config))


def test_inventory_finds_identity_and_index_duplicate_conflicts(tmp_path: Path) -> None:
    config, first = snapshot_fixture(tmp_path)
    second = create_snapshot(config, (), first.budget)
    first_row = _snapshot_row(config, first.snapshot_id)
    connection = _connection(config)
    try:
        connection.execute(
            "UPDATE snapshots SET evidence_relative_path=? WHERE snapshot_id=?",
            ("runs/missing/00000001_filesystem.snapshot.json", first.snapshot_id),
        )
        connection.execute(
            "UPDATE snapshots SET evidence_id=?, evidence_relative_path=? WHERE snapshot_id=?",
            (first_row["evidence_id"], first_row["evidence_relative_path"], second.snapshot_id),
        )
        connection.commit()
    finally:
        connection.close()
    codes = _issue_codes(inspect_snapshot_inventory(config))
    assert "SNAPSHOT_INDEX_SNAPSHOT_ID_MISMATCH" in codes
    assert "SNAPSHOT_EVIDENCE_INDEX_DUPLICATE" in codes


def test_inventory_finds_index_evidence_id_mismatch(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    _write_clone(config, snapshot.snapshot_id)
    assert "SNAPSHOT_INDEX_EVIDENCE_ID_MISMATCH" in _issue_codes(inspect_snapshot_inventory(config))


def test_inventory_finds_duplicate_run_and_missing_run(tmp_path: Path) -> None:
    config, first = snapshot_fixture(tmp_path)
    second = create_snapshot(config, (), first.budget)
    _write_clone(config, second.snapshot_id, run_id=first.run_id)
    assert "SNAPSHOT_RUN_DUPLICATE" in _issue_codes(inspect_snapshot_inventory(config))

    connection = _connection(config)
    try:
        connection.execute("DELETE FROM runs WHERE run_id=?", (first.run_id,))
        connection.commit()
    finally:
        connection.close()
    assert "SNAPSHOT_RUN_MISSING" in _issue_codes(inspect_snapshot_inventory(config))


def test_inventory_finds_orphaned_and_cross_referenced_entries(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    connection = _connection(config)
    try:
        entries = list(
            connection.execute(
            "SELECT scope_id, relative_path FROM snapshot_entries WHERE snapshot_id=? LIMIT 1",
            (snapshot.snapshot_id,),
        )
        )
        entry = entries[0]
        orphan = connection.execute(
            "SELECT scope_id, relative_path FROM snapshot_entries "
            "WHERE snapshot_id=? AND (scope_id, relative_path) != (?, ?) LIMIT 1",
            (snapshot.snapshot_id, entry["scope_id"], entry["relative_path"]),
        ).fetchone()
        assert orphan is not None
        connection.execute(
            "UPDATE snapshot_entries SET entry_id=? WHERE snapshot_id=? AND scope_id=? AND relative_path=?",
            (
                entry_id("another-snapshot", str(entry["scope_id"]), str(entry["relative_path"])),
                snapshot.snapshot_id,
                entry["scope_id"],
                entry["relative_path"],
            ),
        )
        connection.execute(
            "UPDATE snapshot_entries SET snapshot_id='orphan-snapshot' "
            "WHERE snapshot_id=? AND scope_id=? AND relative_path=?",
            (snapshot.snapshot_id, orphan["scope_id"], orphan["relative_path"]),
        )
        connection.commit()
    finally:
        connection.close()
    codes = _issue_codes(inspect_snapshot_inventory(config))
    assert "SNAPSHOT_ENTRY_ORPHANED" in codes
    assert "SNAPSHOT_ENTRY_CROSS_REFERENCE" in codes


def test_damaged_snapshot_evidence_does_not_hide_valid_inventory(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    broken = config.paths.evidence_dir / "runs" / snapshot.run_id / "00000999_filesystem.snapshot.json"
    broken.write_text("{broken", encoding="utf-8")
    inventory = inspect_snapshot_inventory(config)
    assert any(item.snapshot_id == snapshot.snapshot_id and not item.issue_codes for item in inventory.items)
    assert any(
        item.evidence_relative_path == str(broken.relative_to(config.paths.evidence_dir))
        and "SNAPSHOT_EVIDENCE_INVALID" in item.issue_codes
        for item in inventory.items
    )
