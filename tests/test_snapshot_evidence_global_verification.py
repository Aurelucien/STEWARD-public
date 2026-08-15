"""LOCAL-0003-R1C1D global filesystem.snapshot Evidence verification checks."""

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.database import database_path
from local_steward.evidence import canonical_json, digest
from local_steward.models import RunStatus
from local_steward.storage import storage_status, verify_evidence, verify_evidence_report

from .test_protocol_completion import prepared_config
from .test_snapshot_inventory import _write_clone
from .test_snapshot_queries import snapshot_fixture


def _snapshot_path(config, snapshot_id: str) -> Path:
    connection = sqlite3.connect(database_path(config))
    try:
        row = connection.execute(
            "SELECT evidence_relative_path FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        assert row is not None
        return config.paths.evidence_dir / row[0]
    finally:
        connection.close()


def _mutate_snapshot(path: Path, mutation) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutation(document)
    document["evidence_digest"] = digest(document)
    path.write_bytes(canonical_json(document))


def _connection(config) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path(config))
    connection.execute("PRAGMA foreign_keys = OFF")
    return connection


def test_no_snapshot_evidence_preserves_existing_verification_behavior(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    report = verify_evidence_report(config)
    assert not report.verifications
    assert report.snapshot_evidence.evidence_count == report.snapshot_evidence.invalid_count == 0
    assert verify_evidence(config) == []


def test_current_snapshot_creation_path_is_valid_and_calls_intrinsic_validator_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    import local_steward.storage as storage_module

    original = storage_module.validate_snapshot_evidence
    calls = 0

    def validate(document):
        nonlocal calls
        calls += 1
        return original(document)

    monkeypatch.setattr(storage_module, "validate_snapshot_evidence", validate)
    report = verify_evidence_report(config)
    item = report.snapshot_evidence.items[0]
    assert calls == 1
    assert report.snapshot_evidence.evidence_count == report.snapshot_evidence.valid_count == 1
    assert item.snapshot_id == snapshot.snapshot_id and item.valid
    assert all(result.status == "VALID" for result in report.verifications)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda document: document.update({"evidence_digest": "0" * 64}), "SNAPSHOT_EVIDENCE_INVALID"),
        (lambda document: document.update({"payload": {}}), "SNAPSHOT_PAYLOAD_SCHEMA_INVALID"),
        (
            lambda document: document["payload"]["entries"][0].update({"entry_id": "wrong"}),
            "SNAPSHOT_ENTRY_ID_INVALID",
        ),
        (
            lambda document: document["payload"].update(
                {"entries": list(reversed(document["payload"]["entries"]))}
            ),
            "SNAPSHOT_ENTRY_ORDER_INVALID",
        ),
        (
            lambda document: document["payload"]["entries"][0].update({"relative_path": "../escape"}),
            "SNAPSHOT_PATH_INVALID",
        ),
        (
            lambda document: document["payload"]["snapshot"].update({"entry_count": 999}),
            "SNAPSHOT_SUMMARY_INVALID",
        ),
        (
            lambda document: document["payload"]["snapshot"].update({"entries_digest": "0" * 64}),
            "SNAPSHOT_ENTRIES_DIGEST_INVALID",
        ),
        (
            lambda document: document["payload"]["snapshot"].update({"snapshot_digest": "0" * 64}),
            "SNAPSHOT_DIGEST_INVALID",
        ),
    ],
)
def test_intrinsic_snapshot_evidence_failures_are_invalid(
    tmp_path: Path, mutation, expected_code: str
) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    path = _snapshot_path(config, snapshot.snapshot_id)
    if expected_code == "SNAPSHOT_EVIDENCE_INVALID":
        document = json.loads(path.read_text(encoding="utf-8"))
        mutation(document)
        path.write_bytes(canonical_json(document))
    else:
        _mutate_snapshot(path, mutation)
    report = verify_evidence_report(config)
    item = report.snapshot_evidence.items[0]
    assert not item.valid and report.snapshot_evidence.invalid_count == 1
    assert expected_code in {error["code"] for error in item.errors}
    assert verify_evidence(config)[-1].status == "INVALID"


def test_run_relationships_and_global_conflicts_are_invalid(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    connection = _connection(config)
    try:
        connection.execute("UPDATE runs SET run_kind='diagnostic' WHERE run_id=?", (snapshot.run_id,))
        connection.commit()
    finally:
        connection.close()
    report = verify_evidence_report(config)
    assert "SNAPSHOT_RUN_KIND_INVALID" in {issue["code"] for issue in report.snapshot_evidence.issues}

    config, snapshot = snapshot_fixture(tmp_path / "missing")
    connection = _connection(config)
    try:
        connection.execute("DELETE FROM runs WHERE run_id=?", (snapshot.run_id,))
        connection.commit()
    finally:
        connection.close()
    assert "SNAPSHOT_RUN_MISSING" in {
        issue["code"] for issue in verify_evidence_report(config).snapshot_evidence.issues
    }

    config, first = snapshot_fixture(tmp_path / "duplicates")
    clone_id = _write_clone(config, first.snapshot_id)
    duplicate = verify_evidence_report(config).snapshot_evidence
    assert {"SNAPSHOT_ID_DUPLICATE", "SNAPSHOT_RUN_DUPLICATE"} <= {
        issue["code"] for issue in duplicate.issues
    }
    clone_path = config.paths.evidence_dir / "runs" / first.run_id / "00000999_filesystem.snapshot.json"
    original_path = _snapshot_path(config, first.snapshot_id)
    original = json.loads(original_path.read_text(encoding="utf-8"))
    clone = json.loads(clone_path.read_text(encoding="utf-8"))
    assert clone["evidence_id"] == clone_id
    clone["evidence_id"] = original["evidence_id"]
    clone["payload"]["snapshot"]["evidence_id"] = original["evidence_id"]
    clone["evidence_digest"] = digest(clone)
    clone_path.write_bytes(canonical_json(clone))
    assert "SNAPSHOT_EVIDENCE_ID_CONFLICT" in {
        issue["code"] for issue in verify_evidence_report(config).snapshot_evidence.issues
    }


def test_snapshot_run_status_compatibility_uses_complete_and_partial_protocol(tmp_path: Path) -> None:
    config, complete = snapshot_fixture(tmp_path)
    connection = _connection(config)
    try:
        connection.execute("UPDATE runs SET status=? WHERE run_id=?", (RunStatus.PARTIAL.value, complete.run_id))
        connection.commit()
    finally:
        connection.close()
    assert "SNAPSHOT_RUN_STATUS_INVALID" in {
        issue["code"] for issue in verify_evidence_report(config).snapshot_evidence.issues
    }

    config, partial_base = snapshot_fixture(tmp_path / "partial")
    from local_steward.snapshots import create_snapshot
    from local_steward.scan_budget import make_budget

    partial = create_snapshot(config, (), make_budget(max_entries=1))
    assert partial.status.value == "partial"
    report = verify_evidence_report(config).snapshot_evidence
    assert next(item for item in report.items if item.snapshot_id == partial.snapshot_id).valid
    connection = _connection(config)
    try:
        connection.execute("UPDATE runs SET status=? WHERE run_id=?", (RunStatus.SCANNED.value, partial.run_id))
        connection.commit()
    finally:
        connection.close()
    assert "SNAPSHOT_RUN_STATUS_INVALID" in {
        issue["code"]
        for issue in verify_evidence_report(config).snapshot_evidence.issues
        if issue["snapshot_id"] == partial.snapshot_id
    }


def test_bad_snapshot_does_not_hide_good_snapshot_and_report_is_stable(tmp_path: Path) -> None:
    config, first = snapshot_fixture(tmp_path)
    from local_steward.snapshots import create_snapshot

    second = create_snapshot(config, (), first.budget)
    _mutate_snapshot(
        _snapshot_path(config, first.snapshot_id),
        lambda document: document["payload"]["snapshot"].update({"snapshot_digest": "0" * 64}),
    )
    first_report = verify_evidence_report(config).snapshot_evidence
    second_report = verify_evidence_report(config).snapshot_evidence
    assert first_report == second_report
    assert first_report.invalid_count == 1 and first_report.valid_count == 1
    assert any(item.snapshot_id == second.snapshot_id and item.valid for item in first_report.items)
    assert storage_status(config).storage_status == "INCONSISTENT"


def test_legal_orphan_snapshot_evidence_and_index_damage_do_not_affect_evidence_validity(
    tmp_path: Path,
) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    connection = _connection(config)
    try:
        connection.execute("DELETE FROM snapshot_entries WHERE snapshot_id=?", (snapshot.snapshot_id,))
        connection.execute("DELETE FROM snapshots WHERE snapshot_id=?", (snapshot.snapshot_id,))
        connection.commit()
    finally:
        connection.close()
    report = verify_evidence_report(config)
    assert report.snapshot_evidence.valid_count == 1 and not report.snapshot_evidence.issues


def test_evidence_verify_does_not_classify_or_modify_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    before_db = database_path(config).read_bytes()
    before_evidence = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(
        "local_steward.storage.classify_snapshot_inventory",
        lambda _inventory: (_ for _ in ()).throw(AssertionError("classification called")),
    )
    verify_evidence_report(config)
    assert database_path(config).read_bytes() == before_db
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    } == before_evidence


def test_evidence_verify_cli_reports_snapshot_evidence_in_human_and_json(tmp_path: Path) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    runner = CliRunner()
    human = runner.invoke(app, ["--config", str(config.source_path), "evidence", "verify"])
    encoded = runner.invoke(
        app, ["--config", str(config.source_path), "--format", "json", "evidence", "verify"]
    )
    assert human.exit_code == encoded.exit_code == 0
    assert "Snapshot Evidence\nSnapshot Evidence Count: 1" in human.stdout
    assert "Snapshot Issues: none (VALID)" in human.stdout
    assert encoded.stderr == "" and encoded.stdout.count("\n") == 1
    payload = json.loads(encoded.stdout)
    snapshot = payload["result"]["snapshot_evidence"]
    assert snapshot["evidence_count"] == snapshot["valid_count"] == 1
    assert snapshot["invalid_count"] == 0 and snapshot["issues"] == []
