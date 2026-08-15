"""LOCAL-0003-R1C1C storage-status Snapshot integrity integration checks."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.database import database_path
from local_steward.models import EvidenceVerificationResult, SnapshotInventory, SnapshotInventoryItem
from local_steward.snapshots import classify_snapshot_inventory
from local_steward.storage import storage_status

from .test_protocol_completion import prepared_config
from .test_snapshot_queries import snapshot_fixture


def _inventory(issue_codes: tuple[str, ...] = ()) -> SnapshotInventory:
    item = SnapshotInventoryItem(
        "snapshot-a",
        "evidence-a",
        "run-a",
        True,
        True,
        True,
        3,
        "runs/run-a/00000003_filesystem.snapshot.json",
        issue_codes,
    )
    return SnapshotInventory(1, 1, 1, 1, (item,), (), 3)


def test_storage_status_calls_inventory_and_classifier_once_and_preserves_report(
    monkeypatch, tmp_path: Path
) -> None:
    config = prepared_config(tmp_path)
    calls = {"inspect": 0, "classify": 0}
    inventory = _inventory()
    report = classify_snapshot_inventory(inventory)

    def inspect(_config):
        calls["inspect"] += 1
        return inventory

    def classify(value):
        calls["classify"] += 1
        assert value is inventory
        return report

    monkeypatch.setattr(
        "local_steward.storage._inspect_snapshot_inventory",
        lambda _config, _conn: inspect(_config),
    )
    monkeypatch.setattr("local_steward.storage.classify_snapshot_inventory", classify)
    result = storage_status(config)
    assert calls == {"inspect": 1, "classify": 1}
    assert result.storage_status == "HEALTHY" and result.snapshot_integrity is report
    assert not result.issues


def test_snapshot_severity_composes_with_existing_storage_status(monkeypatch, tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    reports = {
        "healthy": classify_snapshot_inventory(_inventory()),
        "degraded": classify_snapshot_inventory(_inventory(("SNAPSHOT_INDEX_INCOMPLETE",))),
        "invalid": classify_snapshot_inventory(_inventory(("SNAPSHOT_RUN_MISSING",))),
    }
    monkeypatch.setattr(
        "local_steward.storage._inspect_snapshot_inventory", lambda _config, _conn: _inventory()
    )
    monkeypatch.setattr("local_steward.storage.classify_snapshot_inventory", lambda _value: reports["healthy"])
    assert storage_status(config).storage_status == "HEALTHY"
    monkeypatch.setattr("local_steward.storage.classify_snapshot_inventory", lambda _value: reports["degraded"])
    assert storage_status(config).storage_status == "DEGRADED"
    monkeypatch.setattr("local_steward.storage.classify_snapshot_inventory", lambda _value: reports["invalid"])
    assert storage_status(config).storage_status == "INCONSISTENT"
    monkeypatch.setattr(
        "local_steward.storage.classify_snapshot_inventory",
        lambda _value: replace(reports["healthy"], status="UNKNOWN"),
    )
    assert storage_status(config).storage_status == "INCONSISTENT"


def test_existing_inconsistent_storage_is_not_overridden_by_healthy_snapshot(
    monkeypatch, tmp_path: Path
) -> None:
    config = prepared_config(tmp_path)
    monkeypatch.setattr(
        "local_steward.storage._inspect_snapshot_inventory", lambda _config, _conn: _inventory()
    )
    monkeypatch.setattr(
        "local_steward.storage.classify_snapshot_inventory",
        lambda _value: classify_snapshot_inventory(_inventory()),
    )
    monkeypatch.setattr(
        "local_steward.storage.verify_evidence",
        lambda _config, **_kwargs: [
            EvidenceVerificationResult("run-a", "INCOMPLETE", True, False, (), 1)
        ],
    )
    monkeypatch.setattr("local_steward.storage.load_run_files", lambda _root, _run: ([], []))
    assert storage_status(config).storage_status == "INCONSISTENT"


def test_snapshot_issues_are_structured_once_with_identity(monkeypatch, tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    report = classify_snapshot_inventory(_inventory(("SNAPSHOT_EVIDENCE_ORPHANED",)))
    monkeypatch.setattr(
        "local_steward.storage._inspect_snapshot_inventory", lambda _config, _conn: _inventory()
    )
    monkeypatch.setattr("local_steward.storage.classify_snapshot_inventory", lambda _value: report)
    result = storage_status(config)
    assert result.storage_status == "DEGRADED"
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue == {
        "code": "SNAPSHOT_EVIDENCE_ORPHANED",
        "message": "Snapshot integrity issue: SNAPSHOT_EVIDENCE_ORPHANED",
        "severity": "DEGRADED",
        "snapshot_id": "snapshot-a",
        "evidence_id": "evidence-a",
        "persistent_run_id": "run-a",
        "path": "runs/run-a/00000003_filesystem.snapshot.json",
    }


def test_snapshot_check_failure_is_a_structured_storage_issue(monkeypatch, tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    monkeypatch.setattr(
        "local_steward.storage._inspect_snapshot_inventory",
        lambda _config, _conn: (_ for _ in ()).throw(OSError("injected")),
    )
    result = storage_status(config)
    assert result.storage_status == "INCONSISTENT"
    assert result.snapshot_integrity is None
    assert result.issues[0]["code"] == "SNAPSHOT_INTEGRITY_CHECK_FAILED"
    assert "injected" not in result.errors[0]


def test_storage_status_is_read_only_and_json_and_human_include_snapshot_report(tmp_path: Path) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    before_db = database_path(config).read_bytes()
    before_evidence = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    }
    runner = CliRunner()
    human = runner.invoke(app, ["--config", str(config.source_path), "storage", "status"])
    encoded = runner.invoke(
        app, ["--config", str(config.source_path), "--format", "json", "storage", "status"]
    )
    assert human.exit_code == encoded.exit_code == 0
    assert "Snapshot Storage\nStatus: HEALTHY" in human.stdout
    assert "Issues: none" in human.stdout and "SnapshotStorageIntegrity" not in human.stdout
    assert encoded.stderr == "" and encoded.stdout.count("\n") == 1
    payload = json.loads(encoded.stdout)
    UUID(payload["run_id"])
    snapshot = payload["result"]["snapshot_integrity"]
    assert snapshot["status"] == "HEALTHY"
    assert snapshot["snapshot_evidence_count"] == snapshot["indexed_snapshot_count"] == 1
    assert snapshot["indexed_entry_count"] == 5 and snapshot["issues"] == []
    assert database_path(config).read_bytes() == before_db
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    } == before_evidence


def test_cli_renders_degraded_and_invalid_snapshot_summaries(monkeypatch, tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    reports = [
        classify_snapshot_inventory(_inventory(("SNAPSHOT_INDEX_INCOMPLETE",))),
        classify_snapshot_inventory(_inventory(("SNAPSHOT_RUN_MISSING",))),
    ]
    runner = CliRunner()
    for report, expected_status, expected_exit in (
        (reports[0], "DEGRADED", 0),
        (reports[1], "INCONSISTENT", 4),
    ):
        monkeypatch.setattr(
            "local_steward.storage._inspect_snapshot_inventory",
            lambda _config, _conn: _inventory(),
        )
        monkeypatch.setattr("local_steward.storage.classify_snapshot_inventory", lambda _value: report)
        result = runner.invoke(app, ["--config", str(config.source_path), "storage", "status"])
        assert result.exit_code == expected_exit
        assert f"Storage Status: {expected_status}" in result.stdout
        assert "Snapshot Storage" in result.stdout and "Issues:" in result.stdout
